"""REFERENCE §3 那一行 `host_turn` 的两张键表,必须和 `contract --json` 逐格一致。

这道闸是**赔出来的**(3.9.0 验收第 2 轮,站点那侧量到的):
`contract.host.option_keys` 加了 `who`、真镜像输出也带着它,而 REFERENCE §3 那一行
还是十格。下游抄的正是那一行 —— 于是站点的 README、`PlayerHostOption` 类型、
后端夹具**三处同缺一格**,而**没有一处会红**:那一格是可选的、少了它前端只是拼不出
人名,页面照画、测试照绿。

所以它和 §9 那两张 op/谓词表同一条理由:**一份没人验的键表,和一句没人验的话是同一种
东西** —— 而这一份的读者不是作者,是另一个仓库。
"""
from __future__ import annotations

import re
from pathlib import Path

from anima_world.__main__ import contract_payload

_REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "REFERENCE.md"
_ROW_PREFIX = "| `world.host_turn("
# 行里那两团用反引号包着的形状:`{"player_id",…}` 与 `{"id","kind",…}`
_SHAPE = re.compile(r"`(\{\".+?\})`")


def _row() -> str:
    for line in _REFERENCE.read_text(encoding="utf-8").splitlines():
        if line.startswith(_ROW_PREFIX):
            return line
    raise AssertionError(f"REFERENCE §3 里找不到 {_ROW_PREFIX!r} 那一行")


def _top_level_keys(shape: str) -> list[str]:
    """一团 `{…}` 的**顶层**键,按出现顺序。

    嵌套那一层有意跳过(`"scene":{"text","source","seq"}` 只算 `scene`)——
    这道闸比的是"这一屏有哪几格",不是把整棵树抄一遍;`door` 那一格同理。
    """
    keys: list[str] = []
    depth = 0
    for token in re.finditer(r'[\{\}\[\]]|"([a-z_]+)"', shape):
        char = token.group(0)
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        elif depth == 1:
            keys.append(token.group(1))
    return keys


def _shapes() -> tuple[list[str], list[str]]:
    found = _SHAPE.findall(_row())
    assert len(found) >= 2, f"那一行里没找到两团形状,只有 {len(found)} 团"
    return _top_level_keys(found[0]), _top_level_keys(found[1])


def test_解析器真的读到了那两团形状():
    """解析器坏掉的话,下面两条会变成永远绿的空断言 —— §9 那族已经写过的教训。"""
    turn_keys, option_keys = _shapes()
    assert len(turn_keys) >= 8, turn_keys
    assert len(option_keys) >= 8, option_keys


def test_一屏的键表和契约逐格一致():
    turn_keys, _ = _shapes()
    want = list(contract_payload()["host"]["turn_keys"])
    assert turn_keys == want, (
        f"REFERENCE §3 那一行:{turn_keys}\ncontract.host.turn_keys:{want}\n"
        "—— 下游抄的是 REFERENCE 那一行"
    )


def test_一项选项的键表和契约逐格一致():
    _, option_keys = _shapes()
    want = list(contract_payload()["host"]["option_keys"])
    assert option_keys == want, (
        f"REFERENCE §3 那一行:{option_keys}\ncontract.host.option_keys:{want}\n"
        "—— `who` 那一格就是这么漏到站点三处去的"
    )


def test_自由输入那一项的键序照着那张有序表():
    """JSON 键序**不是**契约 —— 但"我自己声明了一张有序表、自己的产出却不照它"
    是另一回事:一个照表写解析器的人会先怀疑自己。别的项都同序,别落下这一项。"""
    from anima_world.host import free_option

    want = list(contract_payload()["host"]["option_keys"])
    got = list(free_option())
    assert got == [k for k in want if k in got], f"{got} ≠ 表序 {want}"


def test_那两处五个时刻后面列的名字_和契约的moments逐格一致():
    """🔴 **这道闸是赔出来的第二次,和上面那张键表逐字同一种病。**

    3.10.0 加了第五个时刻 `return`,`contract.host.moments` 跟上了、代码跟上了,
    而 REFERENCE 那两处写着「只在五个时刻开口」、后面**只列了四个**——
    数字和名单在同一句话里互相打脸,而没有一处会红。

    **一份没人验的名单,和一句没人验的话是同一种东西**;这一份的读者是宿主
    (站点按 `trigger` 分支渲染这一屏怎么进场)。

    ⚠️ 比的是**集合**不是顺序:那两处一处按契约序列、一处按「谁排最前」讲,
    而后者的顺序恰恰是**优先级**、和契约里那张表的顺序不是一回事。
    """
    want = set(contract_payload()["host"]["moments"])
    text = _REFERENCE.read_text(encoding="utf-8")
    for marker in ("只在五个时刻开口", "只在六个时刻开口", "只在四个时刻开口"):
        if marker not in text:
            continue
        for chunk in text.split(marker)[1:]:
            window = chunk[:420]
            named = {m for m in want if f"`{m}`" in window}
            assert named == want, (
                f"「{marker}」后面这一段里点到的时刻是 {sorted(named)},"
                f"而 contract.host.moments 是 {sorted(want)} —— "
                f"少的那几个:{sorted(want - named)}\n段落:{window[:200]}"
            )


def test_那句话里的数字_和契约里有几个时刻对得上():
    """「五个」这个**数字**本身也要对 —— 名单补齐了而数字还写着四,
    是同一句话的另一半在撒谎(而且它是读者第一眼看到的那半)。"""
    want = len(contract_payload()["host"]["moments"])
    digits = {4: "四", 5: "五", 6: "六", 7: "七"}
    # ⚠️ **不只扫 REFERENCE**(3.10.2,验收 B):同一句话在 `api.py` 的
    # docstring 里也有一份,而它停在「四个时刻」上停了整整一版 ——
    # **一道只覆盖我记得去扫的地方的闸,和那道只认得自己语法的闸是同一个毛病**。
    #
    # 🔴 **逐份断言,不是 `join` 成一段再判**(3.11.0,验收 B 对 3.10.2):
    # 拼成一段之后「对的那份」会替「烂的那份」把闸喂饱 —— 只要有**任意一份**
    # 写对了,`right in text` 就成立,而另外两份可以一直烂着。
    # **一道分不出是谁烂了的闸,离"什么都没查"只差一步。**
    sources = {
        "docs/REFERENCE.md": _REFERENCE,
        "anima_world/api.py": _REFERENCE.parents[1] / "anima_world" / "api.py",
        "anima_world/host.py": _REFERENCE.parents[1] / "anima_world" / "host.py",
    }
    right = f"只在{digits[want]}个时刻开口"
    seen_anywhere = False
    for name, path in sources.items():
        body = path.read_text(encoding="utf-8")
        for n, word in digits.items():
            if n == want:
                continue
            wrong = f"只在{word}个时刻开口"
            assert wrong not in body, (
                f"{name} 里还写着「{wrong}」,而实际有 {want} 个时刻")
        seen_anywhere = seen_anywhere or right in body
    assert seen_anywhere, f"三份里没有一份写着「{right}」—— 现在有 {want} 个时刻"


def test_host_turn那段docstring里的形状_也和契约对得上():
    """🔴 **同一道闸扫两处**(3.11.0,验收 C ⑤)。

    `api.py` 里 `host_turn` 的 docstring 也画着那两张表,而它**三处同时过期**:
    `trigger` 缺 `return`、顶层缺 `ask_ready*`、一项缺 `who` ——
    而 `who` 正是 3.9.0 漏到站点三处去的那一格。
    REFERENCE 那一行有闸而它没有,于是「哪一份先烂」全看运气。
    """
    from anima_world.api import World

    doc = World.host_turn.__doc__ or ""
    payload = contract_payload()["host"]
    for moment in payload["moments"]:
        assert moment in doc, f"docstring 的 trigger 那一行缺 {moment!r}"
    for key in payload["turn_keys"]:
        assert f'"{key}"' in doc, f"docstring 的顶层形状缺 {key!r}"
    for key in payload["option_keys"]:
        if key == "id":
            continue      # `"id"` 太短,别拿它当判据
        assert f'"{key}"' in doc, f"docstring 的「一项」形状缺 {key!r}"


def test_每个时刻都有一句人话_而且不许是裸英文():
    """🔴 **`〔return · 模板〕`** —— 一个裸英文枚举名印在给玩家看的那一屏上
    (3.10.2,验收 C ①)。2a-② 加了第五个时刻,而那张人话表没跟,
    另外四个都是中文。

    **`HOST_MOMENTS` 加一格就要在这儿加一句人话**,而这条闸让"忘了加"有人喊。
    """
    from anima_world.host import HOST_MOMENTS, MOMENT_LABELS

    missing = [m for m in HOST_MOMENTS if not str(MOMENT_LABELS.get(m) or "").strip()]
    assert not missing, f"这几个时刻没有人话:{missing} —— 屏上会印裸英文枚举名"
    extra = sorted(set(MOMENT_LABELS) - set(HOST_MOMENTS))
    assert not extra, f"人话表里有 `HOST_MOMENTS` 之外的名字:{extra}"
    for name, said in MOMENT_LABELS.items():
        assert not said.isascii(), f"时刻 {name!r} 的人话是 {said!r} —— 那是裸英文"


def test_argparse那几串help里不许有过期的时刻数与子命令表():
    """🔴 **验收 C ⑥**:`player host --help` 写着「第四个开口时刻」(而 `ask` 是
    第五个)、`player` 那句错误提示写着「四个子命令」(而 `story` 是第五个)。

    ⚠️ **那道「几个时刻」的闸此前只扫 REFERENCE / api.py / host.py 三份源码,
    一次都没扫 argparse 那几串 help** —— 而 help 是**用户真的会读到的那一屏**。
    「别数源码里的星号,去问屏幕」那条判据,在这一格上同样适用。
    """
    import re

    from anima_world.__main__ import _build_parser

    def _all_help(parser, out):
        for action in parser._actions:                     # noqa: SLF001
            if action.help:
                out.append(str(action.help))
            # ⚠️ `choices` 对普通参数是**元组**(取值枚举),只有子命令那一格
            # 才是 `{名字: parser}` —— 不分开会当场 AttributeError。
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                for sub in choices.values():
                    if hasattr(sub, "_actions"):
                        _all_help(sub, out)
        return out

    blob = "\n".join(_all_help(_build_parser(), []))
    want = len(contract_payload()["host"]["moments"])
    digits = {"三": 3, "四": 4, "五": 5, "六": 6, "七": 7}
    for word, n in digits.items():
        if n == want:
            continue
        assert f"第{word}个开口时刻" not in blob, (
            f"help 串里还写着「第{word}个开口时刻」,而实际有 {want} 个")
    # `player` 那几个子命令:数字词要跟真的子命令表对得上
    from anima_world.__main__ import _build_parser as _bp

    real = None
    for action in _bp()._actions:                          # noqa: SLF001
        choices = getattr(action, "choices", None)
        player = choices.get("player") if isinstance(choices, dict) else None
        if player is not None:
            for sub in player._actions:                    # noqa: SLF001
                sub_choices = getattr(sub, "choices", None)
                if isinstance(sub_choices, dict):
                    real = len(sub_choices)
    assert real, "找不到 player 的子命令表"
    for word, n in digits.items():
        if n == real:
            continue
        assert f"{word}个子命令" not in blob, (
            f"help / 错误提示里写着「{word}个子命令」,而 player 真有 {real} 个")
