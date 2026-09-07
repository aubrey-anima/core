"""阵营:站队、声望、士气(玩法层博 3c §2.6,3.13.0)。

**今天全有的四样**(设计 §5 那张表 `factions` 那一行的原话):
`group:` 种类 + **exclusive 成员边** + 每人声望(`facts on player`)+
世界级士气(`facts on world`)。

## 🔴 站了就回不去,而**那是内核在 `link` 那一刻查的**

`exclusive` 不是一句建议(`plugins.Edge` 的 docstring 逐字):
「一个人只能在一个门派」写成 `exclusive`,而**放行它的样子是安静的** ——
两条 `member_of` 同时挂着,`plugin list` 看不出来,而提示词里她同时是两个门派的人。
所以这一层**不自己判**:声明 `exclusive`,让内核拒。

## 🔴 **计票 / 人数上限 / 禁地 / 全城声望:一行都不许有**

四样**全是聚合或 gates**,而 `plugins.py` 开篇写着「边、判定、聚合归后面几期」。
设计 §4.4 自己也把它们标成第 4 期。

**在一个没有聚合的引擎上手写一遍计票,就是把第 4 期那件事做进一个插件里** ——
而那份实现以后要被删掉重写,并且在被删掉之前,它是**第二份真相**
(引擎算一遍、插件算一遍,而两边不一致时没有一处会报错)。

⚠️ 这条闸拦的是**我自己**:计票"顺手写一下"很便宜。
"""
from __future__ import annotations

from typing import Any

PLUGIN_ID = "factions"

#: 阵营那个种类的前缀(实例形如 `group:狮心会`)。
FACTION_KIND = "group"

#: 「他站了这一边」那条边。🔴 `exclusive` —— **站了回不去**。
MEMBER_EDGE = "member_of"

#: 每人身上那一格声望,和世界级那一格士气。
STANDING_FACT = "声望"
MORALE_FACT = "士气"

#: 阵营那一格报得出什么(`contract.factions.options_keys`)。
#: 🔴 **没有「有几个人」那一格** —— 那要聚合,而聚合是批 4(见模块 docstring)。
BOARD_KEYS = ("faction", "faction_name", "standing", "standing_text", "text")

#: 🔴 **这一版有意不做的五样**,契约里点名 —— 免得创作台等一个不会来的东西。
#:
#: 🆕 `leave`(3.13.0,A 末轮 ④):**放人走**。它不在这一版里,而理由不是"没空" ——
#: 「站了就回不去」是 3c §2.10 ④ 的**产品裁决**,不是一句实现细节。
#: 要放人走,得先答一个产品问题(叛出要不要留痕?声望清零还是带走?
#: 原阵营那边看不看得见?),而那是批 4 连着聚合一起做的事。
DEFERRED = ("tally", "member_cap", "forbidden_area", "town_reputation", "leave")

#: 🔴 **公共边上关掉的那几个 op**(3.13.0,A 末轮 ④,调度台裁)。
#:
#: 3.13.0 把出厂插件的边放开给所有插件连(`plugins.shared_edge_types`)——
#: 而那一放,**作者写一条 `unlink` 就能让人叛出改投**,
#: 于是「站了就回不去」这句写在 gloss 头一行的话,被一条动词绕开了。
#:
#: **一条产品裁决不该由一个作者的动词来推翻。** 关掉 `unlink`/`transfer`:
#: `link` 那一刻内核拒第二次站队,而这一格保证**没有第二条路把第一条边摘掉**。
#:
#: ⚠️ **「没有第二条路」这句话,自己差点变成假的**(3.13.0,A 四轮 ①):
#: 判断第一版只写在插件效果那一层,而**剧情拍那扇门整个绕过它** ——
#: 一条三行的拍就能叛出改投。现在它在 `Scheduler.apply_edge_effect` 里,
#: **拍与动词共用一份**(加载期那一半在 `beats._validate_payload`)。
#: **写在一扇门上的规矩不是规矩。**
#:
#: ⚠️ 拒的是这条口子,**不是内核直接写库那条路**:抹除走 `edge_store.unlink`,
#: 照旧扫得干净 —— 一个已经抹掉的人不许以边的形式留在世界里,那条更硬。
#: 批 4 那条「放人走」要走哪扇门,由它自己决定。
SHARED_EDGE_CLOSED_OPS: dict[str, tuple[str, ...]] = {
    MEMBER_EDGE: ("unlink", "transfer"),
}

#: 声望的分档词。**分档表只有一份**(和 `tension_text` 逐字同一条):
#: 各译一遍的话,同一个数会在引擎屏 / 创作台 / 站点上说三种话。
STANDING_BANDS = ((0.0, "生面孔"), (0.3, "有人记得你"),
                  (0.6, "说得上话"), (0.85, "一句顶十句"))


def factory_plugin() -> dict[str, Any]:
    """出厂的阵营插件 —— **只声明那条边和那两格量**。

    ⚠️ **不声明具体的阵营**:哪几个阵营是**作者写的**(`group:狮心会`),
    和线索那一层逐字同一条分工。
    """
    return {
        "id": PLUGIN_ID, "version": "1.0.0", "label": "阵营",
        "facts": {
            STANDING_FACT: {
                "bearer": "player", "shape": "number", "default": 0.0,
                "range": [0, 1], "visibility": "self", "label": "声望",
                "bands": [list(b) for b in STANDING_BANDS],
            },
            MORALE_FACT: {
                "bearer": "world", "shape": "number", "default": 0.5,
                "range": [0, 1], "visibility": "public", "label": "士气",
            },
        },
        "edges": {MEMBER_EDGE: {
            "label": "站在这一边", "from": "player",
            # ⚠️ **写的是那个种类的名字,不是通配符**(3.13.0,A 末轮 ③)。
            # 上一版写着 `group:*`,而端点那一层没有通配符这回事 ——
            # 它把 `*` 读成种类名,于是**每一条真的 `member_of` 都对不上**。
            # 它一直没被发现,是因为 `link` 那一刻从前根本不查两端。
            "to": f"{FACTION_KIND}:{FACTION_KIND}",
            # 🔴 **起点唯一** —— 一个人只能站一边,而**内核在 `link` 那一刻查**。
            # 放行的样子是安静的:两条边同时挂着,`plugin list` 看不出来,
            # 而提示词里他同时属于两个阵营。
            "exclusive": True,
        }},
    }


def standing_text(value: float) -> str:
    """这个声望读作哪个词 —— **引擎给人话,宿主不自己译**。"""
    try:
        got = max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return STANDING_BANDS[0][1]
    word = STANDING_BANDS[0][1]
    for start, name in STANDING_BANDS:
        if got >= start:
            word = name
    return word


def board(faction: str, faction_name: str, standing: float) -> dict[str, Any]:
    """阵营那一格 —— **没有「有几个人」**(那要聚合,批 4)。"""
    side = str(faction or "")
    if not side:
        return {"faction": "", "faction_name": "", "standing": 0.0,
                "standing_text": "", "text": "你还没站队。"}
    name = str(faction_name or side)
    word = standing_text(standing)
    return {
        "faction": side, "faction_name": name,
        "standing": round(float(standing or 0.0), 4),
        "standing_text": word,
        "text": f"你站在{name}这一边,{word}。",
    }
