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
    # **一道只覆盖我记得去扫的地方的闸,和那道只认得自己语法的闸是同一个毛病**
    # (这个仓库为裸星号那一族记过一次)。
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in (
            _REFERENCE,
            _REFERENCE.parents[1] / "anima_world" / "api.py",
            _REFERENCE.parents[1] / "anima_world" / "host.py",
        )
    )
    right = f"只在{digits[want]}个时刻开口"
    assert right in text, f"REFERENCE 里找不到「{right}」—— 现在有 {want} 个时刻"
    for n, word in digits.items():
        if n == want:
            continue
        wrong = f"只在{word}个时刻开口"
        assert wrong not in text, f"REFERENCE 里还写着「{wrong}」,而实际有 {want} 个"


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
