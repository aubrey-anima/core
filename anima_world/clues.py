"""线索:一个「知道 / 不知道」的东西(玩法层批 3c §2.4,3.13.0)。

**零新原语。** 一条线索是一个实体(`entity:clue`),「他知不知道」是一条边
(`clues.knows`,玩家 → 那条线索)—— `kind` / 实例 / 边 / 可见性,四样今天全有。

## 🔴 三条纪律,每条都是可验的

1. **线索板只报「存在与解锁状态」,不报内容。**
   设计 §5 那张表里 `clues` 那一行的原话就是这句。内容是作者写在实体上的字,
   而**谁读得到它**由可见性那一层答,不由这一层。
   落法:`board()` 给的是**计数与档词**,一个未解锁线索的名字与正文都不出现 ——
   闸拿一个哨兵字符串扫每一扇只读门。

2. **解锁没有第四条路。** 只有三条:动词的 `effects`(`link`)、剧情拍的 op、
   编剧的 `reveal`。
   ⚠️ **这一句要写进契约**,免得创作台等一个不会来的 `judge`
   (和 3b 那句「作者层写不了 `confront`」同一课:一个等不来的东西
   会让人一直等着,而不是换个写法)。

3. **不报「怀疑」那一档。** 设计里那三档(已知 / 怀疑 / 没问过谁)的中间一档
   要**置信度**,而置信度是判定那一族的东西(批 4)。
   **报一个算不出来的档,就是让屏幕替引擎撒谎。**
"""
from __future__ import annotations

from typing import Any

PLUGIN_ID = "clues"

#: 线索那个种类的 id(实例形如 `clue:老橡树的来历`)。
CLUE_KIND = "clue"

#: 「他知道这条线索」那条边。**边在 = 知道**,没有第二处状态。
KNOWS_EDGE = "knows"

#: 线索板报得出的那两档 —— **闭集,而且有意只有两档**(见纪律 3)。
CLUE_STATES = ("known", "unknown")

#: 线索板那几格(`contract.clues.options_keys`)。
#: 🔴 **一个 `text` 都没有** —— 那正是纪律 1 的落点:报存在与状态,不报内容。
BOARD_KEYS = ("total", "known", "unknown", "known_ids", "text")

#: 解锁的那三条路 —— **契约里点名,免得创作台等一个不会来的第四条**。
UNLOCK_PATHS = ("verb_effect", "beat_op", "director_reveal")


def factory_plugin() -> dict[str, Any]:
    """出厂的线索插件 —— **只声明那条边**,别的一个字都不加。

    ⚠️ **不声明 `clue` 这个种类**:线索是**作者写的东西**(每个世界的线索
    各不相同),而这一层只提供「知不知道」这个关系。
    出厂插件替作者声明种类,就是替他决定线索长什么样。
    """
    return {
        "id": PLUGIN_ID, "version": "1.0.0", "label": "线索",
        "edges": {KNOWS_EDGE: {
            "label": "知道这件事", "from": "player", "to": f"entity:{CLUE_KIND}",
            "facts": {
                # 什么时候知道的 —— **给的是排序,不是内容**。
                "since_tick": {"shape": "number", "default": 0.0,
                               "visibility": "hidden", "label": "何时知道"},
            }}},
    }


def board(known_ids: Any, total: int) -> dict[str, Any]:
    """线索板那一份 —— **计数与档词,一个内容字都没有**(纪律 1)。

    `known_ids` 是他**已经知道**的那几条的 id:已知的那几条可以点名
    (他本来就知道),**未知的那几条连 id 都不给** ——
    一个未解锁线索的 id 往往就是它的谜面(`clue:昂热知道路明非的身世`)。
    """
    known = sorted({str(x) for x in (known_ids or ()) if str(x)})
    n = max(int(total or 0), len(known))
    unknown = n - len(known)
    return {
        "total": n, "known": len(known), "unknown": unknown,
        "known_ids": known,
        "text": board_text(len(known), n),
    }


def board_text(known: int, total: int) -> str:
    """线索板那一句人话 —— **引擎给,宿主不自己译**(和 `tension_text` 同一条)。"""
    if total <= 0:
        return "这个世界还没有线索。"
    if known <= 0:
        return f"{total} 条线索,你一条都还没问出来。"
    if known >= total:
        return f"{total} 条线索,你都知道了。"
    return f"{total} 条线索,你知道 {known} 条。"
