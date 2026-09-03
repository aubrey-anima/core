"""骰子:世界这一刻的一次掷点(3.12.0,批 3b · 裁决 §2.2)。

老板 09-02:「剧情要有张力」。而张力要有**输赢**,输赢要有**一次掷点** ——
主持人那一屏上标着 `tone: "risky"` 的项,点下去总该有个悬念。

## 它是 `world_dice` 的有档位版本,不是第二套随机

`expressions.world_dice` 已经是这个引擎的骰子:blake2b 折出 [0,1),
**不许用 `random` 模块、不许读时间、不许用内置 `hash()`**(那三条的理由写在
它自己的 docstring 里,一条都不重复)。这里做的只有两件:

1. **给它加一格坐标**(见下),
2. 把 [0,1) 折成 `d20` 的点数,再按 `bands` 落到一个档位上。

**代拍(自主程序,裁决 §2.10 第 3 条):只做 d20 一种。** 面数是**表达力幻觉** ——
档位由 `bands` 给,而两种面数只会让创作者在两套直觉之间猜。

## 🔴 种子五个坐标,而底稿写的四个不够

底稿写 `(world, tick, actor, verb)`。**同一 tick、同一个人、同一个动词掷两次
会得到同一个数** —— 而那正是玩家连点会撞上的情形:3a 实测「同地同日连点二十次」
二十拍都落在同一批 tick 里。第五格是 `nonce`。

**`nonce` 用 `stories[pid].moves`**(每玩家单调、折自日志、天然可重放),
不用计数器、不用 uuid —— 那两样都让"同一份日志重放两遍"得到两副骰子。

⚠️ **非玩家路径要自带一个可重放的 nonce,别复用玩家那个**(裁决 §2.11 第 3 条):
`moves` 是**每玩家**单调的,两个 NPC 决斗时它没有来源。那一天来了,给它一个
自己的单调量(比如那条线的 `opened_tick`),而不是把玩家的借过去。
"""

from __future__ import annotations

from typing import Any, Sequence

from anima_world.expressions import world_dice

#: **只做一种面数**(裁决 §2.10 代拍 3)。它进 `contract.roll.faces`,
#: 消费方照它画骰子 —— 改成可配等于让每个世界的"一次冒险"是不同的东西。
FACES = 20

#: 默认档位:`[[下限点数, 档名], …]`,**严格升序、两头封口**,和
#: `stock_visibility` 的 `bands` 逐字同一种形状(作者已经在同一份文件里写过它)。
#: 🔴 **`bands` 是给人读的那一格,不是判定** —— 成败由调用方拿 `result` 自己比,
#: 而档名是**印在屏上**的那几个字。合成一个的话,一次"险胜"和一次"惨胜"在
#: 世界里就是同一件事。
DEFAULT_BANDS: tuple[tuple[int, str], ...] = (
    (1, "砸了"), (6, "差一点"), (11, "过了"), (16, "漂亮"), (20, "神了"),
)


def band_of(result: int, bands: Sequence[Sequence[Any]] | None = None) -> str:
    """这一点落在哪一档 —— 取**最后一个 `<=` 它**的那一档(和 `daypart` 同一种)。

    ⚠️ **边界写成「从几点起」,不写区间**:区间要维护两个数,而两个数迟早对不上。
    """
    table = [(int(row[0]), str(row[1])) for row in (bands or DEFAULT_BANDS)]
    word = table[0][1] if table else ""
    for start, name in table:
        if int(result) >= start:
            word = name
    return word


def roll(*, world_id: str, tick: int, actor: str, verb: str, nonce: Any,
         faces: int = FACES,
         bands: Sequence[Sequence[Any]] | None = None) -> dict[str, Any]:
    """掷一次 —— **纯函数,五个坐标一样就永远是同一个数**。

    返回 `{faces, result, band, seed}`;`seed` 是那五个坐标拼出来的那一行,
    **进事件**,好让"为什么是这个点数"事后答得出来
    (和「对账即重放」逐字同一条:一个答不出来的随机数,和一个假的没有区别)。
    """
    # 第五格并进 `rule_id` 那一格 —— **`world_dice` 只有一份**,
    # 在它旁边另写一个五参数的折法就是第二套随机。
    # ⚠️ 用 `\x1f` 分隔,和它内部那个分隔符同一个:换一个字符就是换一副骰子。
    coordinate = f"roll\x1f{verb}\x1f{nonce}"
    unit = world_dice(str(world_id), coordinate, str(actor), int(tick))
    sides = max(2, int(faces))
    result = int(unit * sides) + 1          # [0,1) → 1..sides,均匀且取不到 sides+1
    return {
        "faces": sides,
        "result": result,
        "band": band_of(result, bands),
        "seed": f"{world_id}|{tick}|{actor}|{verb}|{nonce}",
    }
