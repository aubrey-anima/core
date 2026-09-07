"""载荷键表:**引擎真发的每一个键,都得在契约那张表里**(3.12.0,platform 带回)。

🔴 **这道闸存在的理由是一个手抄表漏了两次。** 壳那侧的送达门有一份手抄的
`_KNOWN_PAYLOAD_KEYS`,而 `line` 和 `source` 两次漏发都出在那儿:引擎加了一格、
手抄那份没跟上,**于是那一格永远送不到消费方,而两边都不报错**。
一份手抄的键表,和一句没人验的话是同一种东西。

**判据是包含,不是相等**:有几种事件的载荷是**按情况带格**的(`state_change`
按 `kind` 分支、一起做事时才有 `party`),要求逐格相等会让这道闸在一次完全
正常的世界里红 —— 而**一道会在正常世界里红的闸,很快就没人看了**。
"""
from __future__ import annotations

import pytest

from _worldfile import open_world_at
from anima_world.events import EVENT_PAYLOAD_KEYS


def _exercise(world) -> dict[str, set[str]]:
    """把该发的事件尽量都发一遍,收它们真带了哪几个键。"""
    agent = next(iter(world.scheduler.agents))
    world.player_move("p1", "cafe")
    world.tick(20)
    world.player_topup("p1", 100)
    world.player_buy("p1", "cafe", "garden_shears")
    world.player_tool("p1", "interact",
                      {"target": "tree:harbor_oak", "verb": "look"})
    world.player_tool("p1", "interact",
                      {"target": "tree:harbor_oak", "verb": "嫁接"})
    world.record_chat_turn(agent, "p1", [{"role": "user", "content": "嗨"},
                                         {"role": "assistant", "content": "嗯"}])
    world.player_walk("p1", "workshop")
    world.player_location("p1")
    thread = {"id": "t1", "promise": "那本书", "with": agent,
              "stake": {"kind": "money", "amount": 5}}
    for move in ("approach", "confront", "reward", "callback"):
        world._director_apply(
            "p1", {"move": move, "who": agent, "line": "来一趟", "why": "推一把",
                   "promise": "那本书",
                   "stake": {"kind": "money", "amount": 5, "what": "五块钱"},
                   "source": "mock"},
            tension_before=0.5, phase="climax", tick=int(world.scheduler.clock),
            place="cafe", thread=thread, pin_ticks=12, due_ticks=50,
            capped=False, forbidden_ops=set(), recap=[], place_name="咖啡店",
            moves_made=2)
    world.tick(40)
    seen: dict[str, set[str]] = {}
    for event in world.events():
        seen.setdefault(event["type"], set()).update(
            (event.get("payload") or {}).keys())
    return seen


def test_引擎真发的键_一个都不许漏在表外(tmp_path):
    """**下次加一格,这条自动红。** 那正是它的全部意义。"""
    with open_world_at(tmp_path / "pk.db") as world:
        seen = _exercise(world)

    missing: list[str] = []
    checked = 0
    for kind, declared in EVENT_PAYLOAD_KEYS.items():
        got = seen.get(kind)
        if not got:
            continue                     # 这一趟没发出来的,由下面那条管
        checked += 1
        extra = sorted(got - set(declared))
        if extra:
            missing.append(f"{kind}: 真发了 {extra},而契约那张表里没有")
    assert not missing, (
        "契约那张载荷键表漏了几格 —— **消费方照它写解析,漏的那格永远读不到**:\n"
        + "\n".join(missing))
    assert checked >= 8, f"这一趟只验到 {checked} 种事件 —— 这道闸多半自己瞎了"


def test_没被这条用例跑到的事件_要么补进夹具_要么明说为什么(tmp_path):
    """⚠️ **一张没被验过的表,和没有那张表是同一种东西。**

    跑不到的那几种要**点名登记**,而不是让它们安静地躺在表里没人验。
    """
    with open_world_at(tmp_path / "pk2.db") as world:
        seen = _exercise(world)

    # 这几种要么要一个特制的世界(实体生灭、消耗品),要么要一条真会话关闭,
    # 要么要一次真的建世界(`agent_join`)—— 它们各有自己的用例文件盯着。
    KNOWN_UNTESTED_HERE = {
        "entity_destroy", "entity_spawn", "item_consume", "agent_join",
        "agent_hail",      # 要她真开口 / 编剧真派得成人,`test_director_world` 盯着
        # 要一个**装了插件、而那条插件想跨玩家写**的世界 ——
        # `tests/test_crossing_privacy.py` 逐格盯着(它就是为这条闸写的)。
        "director_log_refused",
    }
    never = sorted(set(EVENT_PAYLOAD_KEYS) - set(seen) - KNOWN_UNTESTED_HERE)
    assert not never, (
        f"这几种在表里、却没有任何地方验过它们的键:{never} —— "
        "补进上面那个夹具,或者加进 KNOWN_UNTESTED_HERE **并写明它归谁盯**"
    )
