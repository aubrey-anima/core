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


def test_解锁那两条路_和契约逐格相等():
    """🔴 **A 末轮 ②**:这张表 3.13.0 刚从三条收到两条,而它**零闸** ——
    改回三条,99 条用例一条不红。而 tool 的第 3 周底稿正照着它写。
    """
    from anima_world.clues import UNLOCK_PATHS

    text = _text()
    head = "### (c) 🔴 解锁**只有两条路** —— 而这两条都真的通"
    assert head in text, "§3.71(c) 那个标题换了写法 —— 这道闸靠它定位"
    # 标题之后第三段就是那张表(第一段是空的,第二段是「`…unlock_paths`:」)
    block = text.split(head, 1)[1].split("\n\n", 3)[2]
    listed = re.findall(r"`([a-z_]+)`(?:\(|(?:（))", block)
    assert listed == list(UNLOCK_PATHS), (
        f"回执那几条说 {listed},而引擎那张表是 {list(UNLOCK_PATHS)} —— "
        "多一条就是让人去写一条走不通的路,少一条就是让他绕远")


def test_出厂公共边那份名单_和引擎逐格相等():
    """`contract.plugins.shared_edge_types`。**多抄一条最贵**:
    作者照它写下一条 `link`,而加载期当场拒 —— 一盏假红灯。
    """
    from anima_world.plugins import shared_edge_types

    text = _text()
    found = re.search(
        r"`contract\.plugins\.shared_edge_types` 那几条\(([^)]*)\)", text)
    assert found, "「`contract.plugins.shared_edge_types` 那几条(…)」那句换了写法"
    listed = sorted(re.findall(r"`([A-Za-z_][A-Za-z0-9_.]*)`", found.group(1)))
    assert listed == sorted(shared_edge_types()), (
        f"回执那几条说 {listed},而引擎是 {sorted(shared_edge_types())}")


def test_出厂插件那份id名单_和引擎逐格相等():
    """§3.71(c-2) 第 1 条那份「别拿来当自己插件 id」的名单。

    ⚠️ **抄错的方向两种都疼**:少写一个,作者拿它当 id 而加载期当场拒(他不知道
    为什么);多写一个,他绕开一个其实能用的名字。
    """
    from anima_world.__main__ import FACTORY_PLUGINS

    text = _text()
    found = re.search(r"别给自己的插件起名叫([^—]*)——", text)
    assert found, "§3.71(c-2) 那句「别给自己的插件起名叫 …」换了写法"
    listed = sorted(re.findall(r"`([a-z_]+)`", found.group(1)))
    assert listed == sorted(FACTORY_PLUGINS), (
        f"回执那份名单说 {listed},而出厂插件是 {sorted(FACTORY_PLUGINS)}")


def test_玩家节点那个形状_和引擎逐字相等():
    """🔴 图上玩家**只有一个形状**,而这个仓库为「写的人和读的人各写各的」
    在同一版里红过一次(A 整体 ②)。回执里那一行要是抄成裸 `player:<id>`,
    下一个照它写 `link` 的人会连出一条**谁都读不到**的边。
    """
    from anima_world.plugins import EDGE_NODE_ID_FORMS

    want = EDGE_NODE_ID_FORMS["player"].replace("<player_id>", "<id>")
    text = _text()
    found = re.search(r"玩家做时它是\s*`([^`]+)`", text)
    assert found, "§3.71(b-2) 那句「玩家做时它是 `…`」换了写法"
    assert found.group(1) == want, (
        f"回执写着 `{found.group(1)}`,而图上玩家的形状是 `{want}`")


def test_公共边上放开哪几个op_和引擎逐格相等():
    """🔴 又一张手抄表(3.13.0,A 末轮 ④)—— **抄错的方向两种都疼**:
    多抄一个,作者照它写下一条加载期被拒的动词(假红灯);
    少抄一个,他绕开一条其实写得出的路。
    """
    from anima_world.plugins import shared_edge_ops

    text = _text()
    real = shared_edge_ops()
    found = re.search(
        r"`contract\.plugins\.shared_edge_ops` 判:\n\n(.+?)\n\n", text, re.S)
    assert found, "§3.71(b-2) 那张 `shared_edge_ops` 表换了写法 —— 这道闸靠它定位"
    listed: dict[str, list[str]] = {}
    for line in found.group(1).splitlines():
        row = re.match(r"\s*`([A-Za-z_][A-Za-z0-9_.]*)`\s*→\s*(.+)", line.strip())
        if row:
            listed[row.group(1)] = re.findall(r"[a-z]+", row.group(2).replace(
                "**只有", "").replace("**", ""))
    assert listed == {k: list(v) for k, v in real.items()}, (
        f"回执那张表说 {listed},而引擎是 {{k: list(v) for k, v in real.items()}} —— "
        f"真值:{ {k: list(v) for k, v in real.items()} }")


def test_成员边那两端的形状_三处镜像都跟着真声明():
    """🟡 **A 四轮 ③**:`group:*` 那个**从来没生效过**的写法,在三处留了镜像 ——
    `config_store` 的说明(它进 `contract.config.factions.enabled.description`)、
    FOR-STUDIO §3.72(a)、REFERENCE 的配置行。

    真声明改掉之后,这三处**一处都不会红** —— 而它们是三个不同的读者各自照着写的
    那一行。判据统一成:**那两端的形状从真声明里读出来,三处都得对得上**。
    """
    from pathlib import Path

    from anima_world.config_store import _DEFAULTS
    from anima_world.factions import MEMBER_EDGE, factory_plugin
    from anima_world.plugins import EDGE_NODE_ID_FORMS

    edge = factory_plugin()["edges"][MEMBER_EDGE]
    src_form = EDGE_NODE_ID_FORMS[str(edge["from"])]        # agent:player:<player_id>
    dst_kind = str(edge["to"]).partition(":")[2]            # group

    said = _DEFAULTS["factions.enabled"][4]
    assert "group:*" not in said, (
        "`config_store` 那句说明还写着 `group:*` —— 那个写法从来没生效过,"
        "而它经 `contract.config` 直接发给下游")
    assert src_form.split(":<")[0] in said, (
        f"那句说明里没有起点那一端的形状({src_form}):{said}")

    for name in ("FOR-STUDIO.md", "REFERENCE.md"):
        text = (Path(__file__).resolve().parent.parent / "docs" / name).read_text(
            encoding="utf-8")
        head, _, _ = text.partition("## 3.7")  # 只扫到正文足够远
        body = text
        assert "`factions.member_of`,玩家 → `group:*`" not in body, name
        assert "`factions.member_of`(玩家 → `group:*`)" not in body, name
        assert f"`{dst_kind}:" in body, (
            f"{name} 里那条成员边的终点没写成 `{dst_kind}:<…>`")
        assert "group:*" not in body, (
            f"{name} 里还留着 `group:*` —— 那个写法从来没生效过,"
            "而它把作者往一条连不上的路上带")
