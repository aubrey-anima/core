"""人和 NPC 共用同一套行动(玩家侧)。

盯的还是那条不变量:**选择必须在世界里兑现**。`player_action` 当年就是反面
教材 —— 它"能跑、不报错",而点一下"走到哈尔滨"世界里只多一行日志。所以这里
每一条都查世界的真实状态(位置、行程、事件、别人的记忆),不查返回值好不好看。
"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world import tools as tools_mod

PLAYER = "p1"


@pytest.fixture()
def world(tmp_path):
    w = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    yield w
    w.close(wait=False)


def _events(world, kind: str) -> list[dict]:
    page = world.history(since_seq=0, limit=500, kind=kind)
    return list(page["events"])


# ── 菜单 ────────────────────────────────────────────────────────────────


def test_player_menu_is_the_same_registry(world):
    """人那份清单和她那份出自同一个注册表 —— 不是手抄的第二份。"""
    ids = {row["id"] for row in world.player_tools()}
    # 门槛是"在世界里真发生了什么":walk 真在途,broadcast 真进别人的记忆,
    # interact 真扣他的体力(`test_player_affordance.py` 守这条)。
    assert ids == {"walk", "broadcast", "interact"}
    # 这两条**刻意**不给人:对人没有意义 / 人本来就是主动方
    assert "wait_for_user" not in ids
    assert "reach_out" not in ids
    # 这些的玩家侧只落一行日志、不投递给任何角色的感知 —— 在补上玩家侧的账
    # (需求带/钱包/物品)之前不开,否则菜单上那句"付钱是副作用"就是假话
    for empty in ("work", "eat", "sleep", "wander", "seek_company", "talk_to"):
        assert empty not in ids
    for row in world.player_tools():
        assert row["params_schema"] == tools_mod.get(row["id"]).params_schema


def test_tool_off_the_player_surface_is_refused(world):
    """不在 PLAYER 面上的能力一律拒绝,**不静默降级**。"""
    for off_surface in ("wait_for_user", "eat", "work", "talk_to"):
        with pytest.raises(tools_mod.ToolCallError):
            world.player_tool(PLAYER, off_surface)
    with pytest.raises(tools_mod.ToolCallError):
        world.player_tool(PLAYER, "reach_out", {"target": "夏"})


# ── 走路要花时间 ────────────────────────────────────────────────────────


def test_first_step_lands_immediately(world):
    """第一次进世界不收路费 —— 没有出发地的话"走过去"量不出来。"""
    out = world.player_tool(PLAYER, "walk", {"location": "cafe"})
    assert out["ok"] is True
    assert world.player_location(PLAYER) == "cafe"
    assert world.player_in_transit(PLAYER) is False


def test_walking_takes_time_and_lands_on_arrival(world):
    """**人走路和她走路一样要花时间**,而且到点真的落地。"""
    world.player_move(PLAYER, "cafe")
    out = world.player_tool(PLAYER, "walk", {"location": "workshop"})
    assert out["ok"] is True

    assert world.player_in_transit(PLAYER) is True
    assert world.player_location(PLAYER) == "cafe", "在路上就还算在出发地"

    travel = [e for e in _events(world, "travel") if e["who"] == f"player:{PLAYER}"]
    assert len(travel) == 1
    assert travel[0]["payload"]["from"] == "cafe"
    assert travel[0]["payload"]["to"] == "workshop"
    assert travel[0]["payload"]["minutes"] > 0

    world.tick(int(travel[0]["payload"]["arrive_at"]) - world.scheduler.clock)
    assert world.player_location(PLAYER) == "workshop"
    assert world.player_in_transit(PLAYER) is False

    joins = [
        e for e in _events(world, "state_change")
        if e["who"] == f"player:{PLAYER}"
        and (e.get("payload") or {}).get("kind") == "location_join"
    ]
    assert joins and joins[-1]["payload"]["location"] == "workshop", "落地要留痕"


def test_player_move_is_still_instant(world):
    """`player_move` 的旧契约不变:宿主把他放哪就是哪,顺带作废在途的行程。"""
    world.player_move(PLAYER, "cafe")
    world.player_tool(PLAYER, "walk", {"location": "workshop"})
    assert world.player_in_transit(PLAYER) is True

    world.player_move(PLAYER, "home")
    assert world.player_location(PLAYER) == "home"
    assert world.player_in_transit(PLAYER) is False


# ── 路上可以说话,但干不了活 ────────────────────────────────────────────


def test_on_the_road_you_can_still_talk_but_not_work(world):
    """老大 2026-08-08 定的:赶路挡住的是**手上的活**,不是嘴。"""
    world.player_move(PLAYER, "home")
    world.player_tool(PLAYER, "walk", {"location": "cafe"})
    assert world.player_in_transit(PLAYER) is True

    rt = world._tool_runtime
    for kind in ("work", "eat", "sleep", "idle_wander", "idle_social"):
        assert rt.player_do_action(PLAYER, kind, {}) is False, f"{kind} 不该在半路上做成"

    # 而嘴是通的 —— 同地的人搭得上话(柔在 home,他从 home 出发,还没走到)
    assert rt.player_do_action(PLAYER, "chat", {"target": "柔"}) is True, "路上必须还能说话"


def test_talk_to_needs_the_same_place(world):
    """同地才搭得上话 —— 和 `Scheduler._is_colocated` 同一条规矩。"""
    rt = world._tool_runtime
    world.player_move(PLAYER, "cafe")          # 夏在 cafe,遥在 workshop
    assert rt.player_do_action(PLAYER, "chat", {"target": "夏"}) is True
    assert rt.player_do_action(PLAYER, "chat", {"target": "遥"}) is False


def test_unknown_target_is_refused(world):
    world.player_move(PLAYER, "cafe")
    assert world._tool_runtime.player_do_action(PLAYER, "chat", {"target": "查无此人"}) is False


# ── 动作真的落进世界 ────────────────────────────────────────────────────


def test_player_actions_land_as_events(world):
    """过日子的动作要在事件流里留下痕迹,不是返回一个 True 就算完。"""
    rt = world._tool_runtime
    world.player_move(PLAYER, "cafe")
    for kind in ("work", "eat", "idle_wander"):
        assert rt.player_do_action(PLAYER, kind, {}) is True

    mine = [e for e in _events(world, "player_action") if e.get("player_id") == PLAYER]
    kinds = [e["action"] for e in mine]
    assert kinds == ["work", "eat", "idle_wander"]
    assert all(e["loc"] == "cafe" for e in mine), "动作要记在他站的地方"


def test_player_broadcast_is_remembered_by_whoever_is_there(world):
    """人当众说的一句,**在场的角色真的会记住** —— 和她广播走同一条兑现路径。"""
    world.player_move(PLAYER, "cafe")          # 夏在 cafe,遥/柔不在
    out = world.player_tool(PLAYER, "broadcast", {"text": "我回来了"})
    assert out["ok"] is True
    assert out["detail"]["heard_by"] == ["夏"]

    seeds = [
        e for e in _events(world, "memory_seed")
        if "我回来了" in ((e.get("payload") or {}).get("summary") or "")
    ]
    assert seeds, "听见的人要留下记忆种子"
    assert seeds[0]["payload"]["agent_id"] == "夏"

    said = [e for e in _events(world, "agent_broadcast") if e["who"] == f"player:{PLAYER}"]
    assert said and said[0]["payload"]["text"] == "我回来了"


def test_broadcast_with_nobody_around_hears_nobody(world):
    """没人在跟前就是没人听见 —— 一句喊话不该传遍地图。"""
    world.player_move(PLAYER, "workshop")      # 遥在 workshop
    world.player_move(PLAYER, "cafe")
    world.player_tool(PLAYER, "walk", {"location": "home"})   # 在路上
    out = world.player_tool(PLAYER, "broadcast", {"text": "喂"})
    assert out["ok"] is True
    assert out["detail"]["heard_by"] == ["夏"], "还没走到,他仍算在 cafe"


# ── 施动者不会记错 ──────────────────────────────────────────────────────


def test_actor_defaults_to_agent(world):
    """`actor` 默认是 agent —— 这个字段出现之前的调用点一行都不用改。"""
    ctx = tools_mod.ToolContext(
        agent_id="夏", player_id=PLAYER, runtime=world._tool_runtime,
    )
    assert ctx.is_player is False
    assert ctx.actor_id == "夏"

    human = tools_mod.ToolContext(
        agent_id="夏", player_id=PLAYER, runtime=world._tool_runtime,
        actor=tools_mod.PLAYER_ACTOR,
    )
    assert human.is_player is True
    assert human.actor_id == PLAYER


def test_agent_walk_is_untouched(world):
    """她那条路一个字没动:同一个 `walk`,施动者是她时照旧走行为树。"""
    before = world._tool_runtime.agent_location("夏")
    ctx = tools_mod.ToolContext(
        agent_id="夏", player_id=PLAYER, runtime=world._tool_runtime,
    )
    out = tools_mod.call(ctx, "walk", {"location": "workshop"})
    assert out.ok is True
    assert before == "cafe"
    assert "夏" in world.scheduler._transit, "她走路走的仍是 scheduler 那条"
