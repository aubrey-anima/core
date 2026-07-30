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
