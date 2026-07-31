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
"""
from __future__ import annotations

import sqlite3

import pytest

from anima_world import tools as tools_mod
from anima_world.api import World

# 故意不改世界的动词。**每一条都要有理由** —— 这不是垃圾桶,是"为什么它是例外"的位置。
CHANGES_NOTHING = {
    # 让位只改这一轮对话的流程(谁接着说),世界里什么也没发生 —— 它是 #17 那条
    # 连续输出的正常出口,不是一个动作。
    "wait_for_user": "显式让位:只改本轮流程,世界不变",
}


@pytest.fixture()
def world(tmp_path):
    w = World.open(str(tmp_path / "world.db"), force_mock_llm=True)
    w.config_set("chat.tools.enabled", True)
    w.tick(50)
    yield w
    w.close()


def _agents(world: World) -> list[str]:
    return sorted(world.scheduler.agents)


def _bring_together(world: World, mover: str, where: str) -> bool:
    world._tool_runtime.move_agent(mover, where)
    for _ in range(80):
        world.tick(1)
        if world._tool_runtime.agent_location(mover) == where:
            return True
    return False


def _snapshot(world: World, places: tuple[str, ...]) -> dict[str, object]:
    """世界在这些地方现在是什么样。表数行数,事件数那个类型的条数。"""
    path = getattr(world.scheduler, "db_path", None)
    assert path, "拿不到世界文件路径 —— 这条测试没在验它想验的"
    conn = sqlite3.connect(str(path))
    out: dict[str, object] = {}
    for place in places:
        if place.startswith("events:"):
            kind = place.split(":", 1)[1]
            out[place] = sum(
                1 for e in world.history(limit=5000)["events"] if e["type"] == kind
            )
        else:
            out[place] = conn.execute(f"SELECT count(*) FROM {place}").fetchone()[0]
    conn.close()
    return out


def _setup_for(world: World, verb: str) -> tuple[dict, dict]:
    """让这个动词有意义所需要的现场,返回 (调用参数, act 的 kwargs)。"""
    agent, other = _agents(world)[0], _agents(world)[1]
    here = world._tool_runtime.agent_location(agent)
    if verb in ("broadcast", "talk_to"):
        assert _bring_together(world, other, here), "没能把两个人凑到一起"
    if verb == "reach_out":
        assert _bring_together(world, other, here)
        world.player_move("p1", here)
    if verb in ("mute", "delay_reply", "walk_away", "end_conversation", "refuse_topic"):
        world.player_move("p1", here)

    params: dict = {
        "broadcast": {"text": "明天上午店里休息"},
        "talk_to": {"target": other},
        "mute": {"minutes": 5},
        "delay_reply": {"minutes": 10},
        "refuse_topic": {"keyword": "彩票"},
        "walk": {"location": next(p for p in world._tool_runtime.point_ids() if p != here)},
        "reach_out": {"player_id": "p1", "text": "在吗"},
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

    unchanged = [place for place in spec.writes if after.get(place) == before.get(place)]
    assert not unchanged, (
        f"{verb} 声明了写 {list(spec.writes)},但 {unchanged} 一点没变 —— "
        f"声明了却没兑现。前:{before} 后:{after}"
    )


def test_the_declarations_are_visible_from_outside(world):
    """外面的进程要能看见"这个动词会改世界的哪儿" —— 否则它只能猜。"""
    entries = {v["id"]: v for v in world.verbs(_agents(world)[0])}
    assert entries, "动词目录是空的"
    for verb, entry in entries.items():
        assert "writes" in entry, f"{verb} 的目录条目没带 writes"
        assert list(entry["writes"]) == list(tools_mod.get(verb).writes)
