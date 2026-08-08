"""控制标记的解析器 —— 它错一次,玩家就会看到引擎的内脏。

这一层没有 LLM、没有世界,是纯字符串处理,所以它值得单独钉:上面那四条 issue 全部
经过它,而它唯一的失败模式就是**把不该给玩家看的东西给他看**,或者反过来**把她的话
吞掉**。两者都是"照跑但给错东西"。
"""
from __future__ import annotations

from anima_world.directives import DirectiveParser, parse_body, strip_directives


def _stream(text: str, chunk: int) -> tuple[str, list]:
    """按固定大小切碎再喂 —— 真流式下标记一定会跨 token。"""
    parser = DirectiveParser()
    prose: list[str] = []
    found: list = []
    for index in range(0, len(text), chunk):
        for kind, value in parser.feed(text[index : index + chunk]):
            (prose if kind == "text" else found).append(value)
    for kind, value in parser.flush():
        (prose if kind == "text" else found).append(value)
    return "".join(prose), found


def test_a_marker_survives_being_split_across_every_token_boundary():
    text = '〔stance:provoke〕（她把杯子推开。）你自己看着办。〔tool:mute {"minutes": 5}〕'
    for chunk in range(1, 12):
        prose, found = _stream(text, chunk)
        assert prose == "（她把杯子推开。）你自己看着办。", f"chunk={chunk} 时漏了标记"
        assert [d.kind for d in found] == ["stance", "tool"], f"chunk={chunk}"
        assert found[1].params == {"minutes": 5}


def test_a_lone_bracket_in_prose_is_given_back_not_swallowed():
    """模型在散文里手写了这个符号。宁可玩家看到一个怪符号,也不能吞掉她的话。"""
    prose, found = strip_directives("她写下〔这是我的答案")
    assert prose == "她写下〔这是我的答案"
    assert found == []


def test_a_very_long_unclosed_bracket_stops_eating_the_reply():
    tail = "她说" + "很长的一段话" * 60
    prose, found = strip_directives("〔" + tail)
    assert tail in prose, "超过上限还不闭合就该判定这不是指令"
    assert found == []


def test_parameters_that_are_not_json_are_refused_instead_of_guessed():
    directive = parse_body("tool:mute minutes=5")
    assert directive.kind == "unknown" and directive.name == "mute"
    directive = parse_body('tool:mute ["five"]')
    assert directive.kind == "unknown", "参数不是对象就不该往下猜"


def test_full_width_colons_and_yield_aliases_are_accepted():
    """模型会写全角冒号,也会写 yield 而不是 wait。收敛在这里,别让它变成 unknown。"""
    assert parse_body("stance：please").kind == "stance"
    assert parse_body("stance：please").name == "please"
    for alias in ("wait", "WAIT", "yield", "wait_for_user"):
        assert parse_body(alias).kind == "wait", alias


def test_an_unknown_directive_is_reported_rather_than_dropped():
    directive = parse_body("sing_a_song")
    assert directive.kind == "unknown" and directive.raw == "sing_a_song"


# ── ASCII 方括号:真模型隔一会儿就把〔〕打成 [] ────────────────────────────────
#
# 拿真模型跑盘点时抓到的:她想让位,写的是 `[tool:wait_for_user {}]`。解析器只认全角,
# 于是那一行**一个字不落地进了玩家看到的正文**,而让位没有发生。两件事一起坏:
# 玩家看见引擎的内脏,她的选择在世界里没兑现。


def test_ascii_brackets_are_accepted_like_the_full_width_ones():
    for text in ('[tool:mute {"minutes": 5}]', '〔tool:mute {"minutes": 5}〕'):
        prose, found = strip_directives("好啊。" + text)
        assert prose == "好啊。", f"{text} 漏进了正文"
        assert [d.kind for d in found] == ["tool"]
        assert found[0].name == "mute" and found[0].params == {"minutes": 5}


def test_ascii_markers_also_survive_every_token_boundary():
    text = '[stance:provoke]（她把杯子推开。）你自己看着办。[tool:wait_for_user {}]'
    for chunk in range(1, 12):
        prose, found = _stream(text, chunk)
        assert prose == "（她把杯子推开。）你自己看着办。", f"chunk={chunk} 时漏了标记"
        assert [d.kind for d in found] == ["stance", "tool"], f"chunk={chunk}"


def test_square_brackets_in_prose_are_left_alone():
    """`[` 在散文里太常见 —— 它不是无条件的开括号,否则这一层会开始吞她的话。"""
    for text in ("（她笑了）[停顿]然后走开了。", "我等你 [waiting] 呢",
                 "markdown [链接](http://x) 照旧", "数组 [1, 2, 3] 也照旧"):
        prose, found = strip_directives(text)
        assert prose == text, f"{text!r} 被当成指令吃掉了"
        assert found == []


def test_a_json_array_argument_is_not_cut_at_the_wrong_bracket():
    """ASCII 的闭括号会出现在 JSON 里 —— 不数花括号深度就会从中间截断。"""
    prose, found = strip_directives('看这个 [tool:broadcast {"text":"八点见","tags":["a","b"]}] 完事')
    assert prose == "看这个  完事"
    assert found[0].name == "broadcast"
    assert found[0].params["tags"] == ["a", "b"]


# ── 兜底:没解析成的标记不许漏给玩家 ─────────────────────────────────────────


def test_a_mangled_marker_is_swallowed_instead_of_shown_to_the_player():
    """打残了的控制标记(少个闭括号)照原文吐出去,就是让玩家看见引擎的内脏。

    判据是"开头像不像指令",不是"解析得成解析不成" —— 后者会把
    `〔她愣了一下` 也一起吞掉,而那是她的话。
    """
    for text in ("好的〔tool:wait_for_user {}", "好的[tool:walk_away {}",
                 "好的[stance:provoke"):
        prose, found = strip_directives(text)
        assert prose == "好的", f"{text!r} 的标记漏进了正文"
        assert [d.kind for d in found] == ["unknown"], "咽掉了却没记账"


def test_swallowing_only_applies_to_things_that_look_like_directives():
    """反过来那一半:散文里的怪符号照旧原样还给玩家,一个字都不许少。"""
    prose, found = strip_directives("她写下〔这是我的答案")
    assert prose == "她写下〔这是我的答案"
    assert found == []


def test_a_mangled_marker_is_swallowed_even_when_it_runs_past_the_length_cap():
    prose, found = strip_directives("〔tool:mute " + "很长的一段话" * 60)
    assert prose == "", "超长的打残标记漏给了玩家"
    assert [d.kind for d in found] == ["unknown"]
