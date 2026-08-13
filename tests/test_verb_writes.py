"""每个动词声明它把世界改在哪儿,而这里逐个验它真的改了。

CLAUDE.md 有一条硬不变量:

> **她的选择必须在世界里兑现** —— `walk_away` 真的起程,`delay_reply` 到点真的回来
> 敲门,`narrative_direction` 真的把人挪过来。只改提示词的版本("她走了"但下一 tick
> 还站在原地)就是这几条 issue 要修的病本身。

它此前是一句**人得自己记住**的话,而这一版靠人肉找出了八处违反它的地方,每一处都是
"能跑、不报错、给错东西",每一处都是玩到了才发现的:

- `broadcast` 告诉她"世界里的人都能看到",而它只发了一行没有任何角色消费的日志
- `walk_away` 对着不在场的人是空动作,连着四趟没意义的行程
- world-rules 写 `world_x` 落在别人名下,而 `rule_stats()` 报 `written: 5, skipped: 0`

`ToolSpec.writes` 把这条不变量变成机器能验的:动词声明它写哪些表 / 发哪些事件,
这里在真世界里调一遍,比对声明的地方**到底变没变**。

三条规矩:

1. **不许留空**(除非显式登记为"故意不改世界")—— 空的 `writes` 是"我没想过",
   而不是"它不改世界"
2. **声明的地方必须真的变** —— 这是全部意义
3. **不给动词写第二份判断** —— 这里只观察世界的前后差,不去检查动词内部做了什么

第四条是这条测试自己教出来的:**"改在哪儿"要连着"什么时候"一起说**。`talk_to` 的
记忆是关系判定的产物,而判定按硬不变量跑在判定线程池上,`act()` 返回时它还没落 ——
于是这里"量前后差"的那一瞬间**四分之一的时候量早了**,红在一个和它要验的东西无关的
地方。落点分两种(`ToolSpec.writes_late`),量法也就分两种:当场落的量这一瞬间,
稍后落的等它落。**"稍后"不是"可以不落"** —— 等不到照样红。
"""
from __future__ import annotations

import time

from _worldfile import open_world_at


import pytest

from anima_world import tools as tools_mod
from anima_world.api import World

# 故意不改世界的动词。**每一条都要有理由** —— 这不是垃圾桶,是"为什么它是例外"的位置。
CHANGES_NOTHING = {
    # 让位只改这一轮对话的流程(谁接着说),世界里什么也没发生 —— 它是 #17 那条
    # 连续输出的正常出口,不是一个动作。
    "wait_for_user": "显式让位:只改本轮流程,世界不变",
}


# 橱窗世界里那棵树 —— `interact` 要有个真东西可碰。
_TREE = "tree:harbor_oak"


@pytest.fixture()
def world(tmp_path):
    w = open_world_at(str(tmp_path / "world.db"), force_mock_llm=True)
    w.config_set("chat.tools.enabled", True)
    w.tick(50)
    yield w
    w.close()


def _agents(world: World) -> list[str]:
    return sorted(world.scheduler.agents)


def _settle(world: World, agent: str, limit: int = 288) -> None:
    """等到她手上没有正在赶的路 —— 不然**每一个**动词都会被同一句话挡回来。

    在途时 `act()` 一律回"世界这会儿不接这个动作(她在赶路…)",于是这条测试连
    "它改没改世界"都还没走到就红了。从前不必等,只是因为所有世界都从午夜起步、跑
    50 tick 恰好是凌晨没人动;几点开门如今是这个世界作者的意见(`world.start_time`),
    再靠那个巧合就是让这道闸站在沙子上。

    放在 `_setup_for` 的**开头**:后面 `_bring_together` 要照她此刻站的地方把人凑过来,
    先等她落地,算出来的 `here` 才不是她刚离开的那个地方。
    """
    for _ in range(limit):
        if agent not in world.scheduler._transit:
            return
        world.tick(1)
    raise AssertionError(f"{agent} 整整一个世界日都在路上,这条测试没法起头")


def _bring_together(world: World, mover: str, where: str) -> bool:
    world._tool_runtime.move_agent(mover, where)
    for _ in range(80):
        world.tick(1)
        if world._tool_runtime.agent_location(mover) == where:
            return True
    return False


def _stand_together(world: World, a: str, b: str, where: str) -> bool:
    """把两个人凑到 `where`,而且**同时**站在那儿。

    只搬一个是不够的:搬他要 tick 最多 80 次,而这 80 tick 里行为树完全可能让
    **另一个**起身走掉 —— 于是 `talk_to` / `broadcast` 被"你们不在同一个地方"
    挡回来,测试红在一个和它要验的东西无关的地方。全量套件里逮到过一次
    (隔离重跑 5/5 全绿),那正是一道会假绿的闸最难看的样子:它红不红取决于
    这一回她想不想出门。
    """
    for _ in range(6):
        drifted = [
            who for who in (a, b)
            if world._tool_runtime.agent_location(who) != where
            or who in world.scheduler._transit
        ]
        if not drifted:
            return True
        for who in drifted:
            if not _bring_together(world, who, where):
                return False
    return False


def _snapshot(world: World, places: tuple[str, ...]) -> dict[str, object]:
    """世界在这些地方现在是什么样。表数行数,事件数那个类型的条数。"""
    # 逻辑表名 → Redis 键(world.db 退役后动词的 writes 声明仍用表名说话)。
    redis = world.scheduler.redis
    wid = world.scheduler.world_id
    hash_of = {
        "agent_mutes": "mutes", "agent_followups": "followups",
        "agent_refused_topics": "refused_topics", "agent_stance": "stance",
        "persona_overrides": "overrides", "conversations": "conversations",
        "messages": "messages", "memories": "memories", "edges": "edges",
    }
    out: dict[str, object] = {}
    for place in places:
        if place.startswith("events:"):
            kind = place.split(":", 1)[1]
            out[place] = sum(
                1 for e in world.history(limit=5000)["events"] if e["type"] == kind
            )
        elif place == "stocks":
            # 量不是"一张表"(每个 owner 一个 hash),而且**数行数在这儿会假绿**:
            # 照料一棵树改的是已有的那个值,行数一行没多。所以这一项比的是值本身。
            store = world.scheduler.stock_store
            out[place] = {owner: store.of(owner) for owner in sorted(store.owners())}
        else:
            assert place in hash_of, f"不认识的落点 {place} —— 给它指一个 Redis 键"
            out[place] = int(redis.hlen(f"anima:{wid}:{hash_of[place]}") or 0)
    return out


def _setup_for(world: World, verb: str) -> tuple[dict, dict]:
    """让这个动词有意义所需要的现场,返回 (调用参数, act 的 kwargs)。"""
    agent, other = _agents(world)[0], _agents(world)[1]
    _settle(world, agent)
    if verb == "eat":
        # **吃要有得吃。** `eat` 声明它发 `item_consume`,而那一条只在她站的地方
        # 货架上真有饭时才发得出来 —— 站在自己家里吃是一件世界里没有账的事。
        # 从前不必挪,只因为所有世界都从午夜起步、跑 50 tick 时她恰好在店里;
        # 几点开门如今是这个世界作者的意见,那个巧合没了。**问世界哪儿有饭**,
        # 别写死 "cafe":橱窗改了菜单这条该跟着走,而不是变红。
        pantry = next(
            (p for p in world._tool_runtime.point_ids()
             if world.scheduler.economy_store.cheapest_meal(p)), None
        )
        assert pantry, "这个世界没有一处货架上有饭 —— 这条测试验不了 eat"
        assert _bring_together(world, agent, pantry), "没能把她挪到有饭的地方"
    here = world._tool_runtime.agent_location(agent)
    if verb in ("broadcast", "talk_to"):
        assert _stand_together(world, agent, other, here), "没能把两个人凑到一起"
    if verb == "reach_out":
        assert _stand_together(world, agent, other, here)
        world.player_move("p1", here)
    if verb in ("mute", "delay_reply", "walk_away", "end_conversation", "refuse_topic"):
        world.player_move("p1", here)
    if verb == "interact":
        # 在场是这个动词的前提(隔着半个地图照料不到),所以先把她挪到树那儿去。
        where = world.scheduler.visibility_store.place_of(_TREE)
        assert _bring_together(world, agent, where), "没能把她挪到树那儿"

    params: dict = {
        "broadcast": {"text": "明天上午店里休息"},
        "talk_to": {"target": other},
        "mute": {"minutes": 5},
        "delay_reply": {"minutes": 10},
        "refuse_topic": {"keyword": "彩票"},
        "walk": {"location": next(p for p in world._tool_runtime.point_ids() if p != here)},
        "reach_out": {"player_id": "p1", "text": "在吗"},
        "interact": {"target": _TREE, "verb": "tend"},
    }.get(verb, {})
    surface = next(iter(tools_mod.get(verb).surfaces))
    return params, {"surface": surface, "player_id": "p1"}


@pytest.mark.parametrize(
    "verb", sorted(spec.id for spec in tools_mod.tools_for("*"))
)
def test_every_verb_declares_where_it_changes_the_world(verb):
    """留空是"我没想过",不是"它不改世界" —— 后者要显式登记并写明理由。"""
    spec = tools_mod.get(verb)
    if verb in CHANGES_NOTHING:
        assert spec.writes == (), (
            f"{verb} 登记为不改世界,却声明了 {spec.writes} —— 两边对不上"
        )
        return
    assert spec.writes, (
        f"{verb} 没声明它把世界改在哪儿。填 `writes=(...)`,或者加进 "
        "CHANGES_NOTHING **并写明为什么它不改世界**。"
    )


@pytest.mark.parametrize(
    "verb",
    sorted(
        spec.id for spec in tools_mod.tools_for("*")
        if spec.writes and spec.id not in {"end_conversation", "walk_away", "delay_reply"}
    ),
)
def test_a_verb_really_changes_what_it_declared(world, verb):
    """**全部意义在这一条。**

    在真世界里调一遍,比对声明的地方到底变没变。声明了却没兑现 —— 这一版靠人肉抓了
    八次的那类 bug —— 从此在这里当场红。

    三个动词暂时不在这条里:`end_conversation` / `walk_away` / `delay_reply` 需要一场
    真的会话才有 `conversations` 行可改,而建一场会话要走聊天子系统。它们由
    `test_chat_tools.py` 各自盯着,这里不重复。
    """
    spec = tools_mod.get(verb)
    agent = _agents(world)[0]
    params, kwargs = _setup_for(world, verb)

    before = _snapshot(world, spec.writes)
    result = world.act(agent, verb, params, **kwargs)
    assert result["ok"] is True, f"这一下没成,验不了它改没改:{result.get('error')}"
    after = _snapshot(world, spec.writes)

    # **当场落的那几处,量的就是这一瞬间。** 声明成 `writes_late` 的不在这一批里:
    # 它们由别的线程稍后写(`talk_to` 的记忆是关系判定的产物,而判定按硬不变量
    # 永不跑在 tick 线程上)。此前这里把两种落点混着量,于是这条测试**四分之一
    # 概率红在一个和它要验的东西无关的地方** —— 拿一个同步的瞬间去量一件异步的事,
    # 红不红取决于这一回线程池抢没抢赢。
    now = [place for place in spec.writes if place not in spec.writes_late]
    unchanged = [place for place in now if after.get(place) == before.get(place)]
    assert not unchanged, (
        f"{verb} 声明了写 {list(spec.writes)},但 {unchanged} 一点没变 —— "
        f"声明了却没兑现。前:{before} 后:{after}"
    )

    # **稍后落的照等不误。** 等不到一样红 —— `writes_late` 是"晚一点",不是
    # "可以不落"。超时只是保险丝(和 `test_relationship_axes_land._await` 同一条
    # 路数):这不是放宽断言,断言仍然是"它必须变",放宽的只是"什么时候量"。
    if spec.writes_late:
        late = _await_change(world, spec.writes_late, before)
        never = [place for place in spec.writes_late if late.get(place) == before.get(place)]
        assert not never, (
            f"{verb} 声明了稍后写 {list(spec.writes_late)},等满 {_LATE_TIMEOUT} 秒 "
            f"{never} 还是一点没变 —— 声明了却没兑现。前:{before} 后:{late}"
        )


_LATE_TIMEOUT = 10.0


def _await_change(world: World, places: tuple[str, ...], before: dict) -> dict:
    """等到这几处都变了(或者等满保险丝)。返回最后一次看见的样子。"""
    deadline = time.monotonic() + _LATE_TIMEOUT
    while True:
        seen = _snapshot(world, places)
        if all(seen.get(place) != before.get(place) for place in places):
            return seen
        if time.monotonic() >= deadline:
            return seen
        time.sleep(0.01)


def test_the_declarations_are_visible_from_outside(world):
    """外面的进程要能看见"这个动词会改世界的哪儿" —— 否则它只能猜。"""
    entries = {v["id"]: v for v in world.verbs(_agents(world)[0])}
    assert entries, "动词目录是空的"
    for verb, entry in entries.items():
        assert "writes" in entry, f"{verb} 的目录条目没带 writes"
        assert list(entry["writes"]) == list(tools_mod.get(verb).writes)
        # **"什么时候落"和"落在哪儿"一样要看得见。** 只给 `writes` 的话,宿主会在
        # 调用返回的那一瞬间去查,然后随机读到一个还没变的世界 —— 这条测试自己
        # 就是这么红了四分之一次的。
        assert "writes_late" in entry, f"{verb} 的目录条目没带 writes_late"
        assert list(entry["writes_late"]) == list(tools_mod.get(verb).writes_late)
