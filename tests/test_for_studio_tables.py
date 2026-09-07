"""FOR-STUDIO 里那几张**手抄**的表,和真表逐格相等(3.13.0,验收 B ③)。

🔴 **这个仓库为「一份没人验的键表」红过三轮**,而回执这一侧一直是裸的:
`docs/FOR-STUDIO.md` 是**发给创作台的那一份**,tool 照着它填 effects、
照着它判「哪些动作会被别人看见」—— 而它的每一张表都是**手抄**的。
B 复验实测:把 `target_fields` 改回六格(带上 3.13.0 已经删掉的 `who`),
**77 条用例一条不红**;撞见那五格同理。

判据的形状照 REFERENCE §4.8 那条(`test_contract_command.py`):
**按名字逐格比,不比条数** —— 只比条数的话,加一格的同时漏掉另一格仍然是绿的。
⚠️ 定位**不许写死会变的东西**:靠段落标题与那个反引号里的字段名定位,
不靠行号,也不靠表里的内容 —— 一道靠会变的东西定位的闸,
**每次真该它响的时候都先自己瞎掉**。
"""
from __future__ import annotations

import re
from pathlib import Path

FOR_STUDIO = Path(__file__).resolve().parent.parent / "docs" / "FOR-STUDIO.md"


def _text() -> str:
    return FOR_STUDIO.read_text(encoding="utf-8")


def test_对人动词那几格_和引擎那张表逐格相等():
    """`contract.person_verbs.target_fields` 那一行。

    ⚠️ 这张表**不是装饰**:tool 照它写 `{"as": "$target"}`。多抄一格,
    作者会写下一个**永远不会被替换**的字段名,而运行时一声不吭。
    """
    from anima_world.person_verbs import TARGET_FIELDS, TARGET_PLACEHOLDER

    text = _text()
    found = re.search(r"`target_fields`\(([^)]*)\)", text)
    assert found, (
        "FOR-STUDIO 里那句 `target_fields`(…) 不见了或换了写法 —— "
        "这道闸靠它定位;改写法就顺手改这里,别把闸留成一句永远找不到东西的话")
    listed = re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", found.group(1))
    assert listed == list(TARGET_FIELDS), (
        f"回执那一行说 {listed},而引擎那张表是 {list(TARGET_FIELDS)} —— "
        "少一格 tool 就不会用它,多一格 tool 会写下一个永远不被替换的字段名")
    assert TARGET_PLACEHOLDER in text, (
        f"占位名 {TARGET_PLACEHOLDER} 在回执里一次都没出现")


def test_撞见那几种事_和引擎那张表逐格相等():
    """§3.70(b) 那一块:表、`state_change` 收窄到哪一支、一屏最多几行。

    三样都手抄过一遍,而**三样各自都会烂**:
    表会漏掉新加的一种、收窄那一支会跟着 `CROSSING_STATE_KINDS` 变、
    「今天 6」是**一个写在文档里的数**。
    """
    from anima_world.host import (
        CROSSING_EVENT_TYPES, CROSSING_LIMIT, CROSSING_STATE_KINDS,
    )

    text = _text()
    head = "### (b) 哪几种事会被撞见 —— `contract.director.crossing_event_types`"
    assert head in text, "§3.70(b) 那个标题换了写法 —— 这道闸靠它定位"
    block = text.split(head, 1)[1].split("\n\n", 2)[1]
    listed = re.findall(r"`([A-Za-z_][A-Za-z0-9_.]*)`", block)
    kinds = [k for k in listed if k in CROSSING_STATE_KINDS]
    listed = [k for k in listed if k not in CROSSING_STATE_KINDS]
    assert listed == list(CROSSING_EVENT_TYPES), (
        f"回执那几格说 {listed},而引擎那张表是 {list(CROSSING_EVENT_TYPES)} —— "
        "tool 照它判「哪些动作会被别人看见」,漏一种就是少告诉作者一件事")
    assert kinds == list(CROSSING_STATE_KINDS), (
        f"`state_change` 收窄到哪一支写岔了:回执 {kinds},引擎 "
        f"{list(CROSSING_STATE_KINDS)}")
    said = re.search(r"`contract\.director\.crossing_limit`\s*行(?:\(|（)今天 (\d+)",
                     text)
    assert said, "「一屏最多 …(今天 N)」那句换了写法 —— 这道闸靠它定位"
    assert int(said.group(1)) == CROSSING_LIMIT, (
        f"回执写着今天 {said.group(1)} 行,而引擎是 {CROSSING_LIMIT} —— "
        "**一个写在文档里的数,是一个迟早会烂的判据**,所以它必须有闸")
