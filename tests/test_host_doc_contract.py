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
