"""`World.act()`:外面的进程改变这个世界的唯一入口。

为什么需要它:此前"她做了什么"只能由引擎内部触发 —— 聊天那一轮、定时轮次、节拍
脚本。一个住在别的进程里、由 LLM 驱动的角色**碰不到任何动词**,于是"很多进程操作
同一个世界"在引擎这一侧是断的(见 `docs/AGENT-RUNTIME.md`)。

盯五件事:

1. 它真的改世界 —— 不是记一行日志(`broadcast` 就这么假过一次)
2. **一个动作是原子的** —— 整个执行期持有那把唯一的锁
3. **面是硬的** —— 需要"对面有个人"的动词不能在没人的场合被调用
4. **坏调用不掀翻世界,但也不静默** —— 返回原因,而且原因要能照着修
5. 结果的形状和聊天里那批工具调用**逐字相同** —— 两条路不该长得不一样
"""
from __future__ import annotations

import threading

import pytest

from anima_world import tools as tools_mod
from anima_world.api import World


@pytest.fixture()
def world(tmp_path):
    w = World.open(str(tmp_path / "world.db"), force_mock_llm=True)
    w.config_set("chat.tools.enabled", True)
    yield w
    w.close()


def _an_agent(world: World) -> str:
    return sorted(world.scheduler.agents)[0]


def test_an_action_really_changes_the_world(world):
    """全部意义在这一条:提交一个动词,世界真的变了。

    `broadcast` 是这条测试存在的理由 —— 它曾经只发一行日志,而菜单告诉她"世界里的
    人都能看到"。声明了却没兑现,是这个仓库最在意的那类坏。
    """
    speaker = _an_agent(world)
    here = world._tool_runtime.agent_location(speaker)
    listener = next(a for a in sorted(world.scheduler.agents) if a != speaker)
    world._tool_runtime.move_agent(listener, here)
    for _ in range(60):  # 起程不是瞬移,在途的人不在任何地方
        world.tick(1)
        if world._tool_runtime.agent_location(listener) == here:
            break
    assert world._tool_runtime.agent_location(listener) == here
    before = {m.get("summary") for m in world.memories(listener)}

    result = world.act(speaker, "broadcast", {"text": "明天上午店里休息"})

    assert result["ok"] is True
    assert result["detail"]["heard_by"] == [listener]
    fresh = [
        m.get("summary") for m in world.memories(listener)
        if m.get("summary") not in before
    ]
    assert any("明天上午店里休息" in (s or "") for s in fresh), (
        f"在场的人没记住 —— 动作没在世界里兑现:{fresh}"
    )


def test_the_result_looks_exactly_like_a_chat_tool_call(world):
    """两条路共用 `ToolResult.to_dict`,所以形状必须一样。

    另写一份返回结构就会分叉,而宿主拿到两种形状的同一件事,只能各写一遍处理。
    """
    agent = _an_agent(world)
    result = world.act(agent, "broadcast", {"text": "喂"})
    assert set(result) >= {"tool", "params", "ok"}
    assert result["tool"] == "broadcast"
    assert result["params"] == {"text": "喂"}


def test_the_surface_is_enforced(world):
    """需要"对面有个人"的动词,在没人说话的场合是空动作 —— 当场拒。

    而且拒绝要说清它在哪个面上,否则调用方只知道"不行",不知道下一步。
    """
    agent = _an_agent(world)
    result = world.act(agent, "walk_away", {"to_location": "home"})
    assert result["ok"] is False
    assert "autonomy" in result["error"] and "chat" in result["error"]

    # 换到对的面上就能用(她此刻不一定走得掉,但不该被面挡住)
    world.player_move("p1", world._tool_runtime.agent_location(agent))
    allowed = world.act(agent, "walk_away", {}, player_id="p1", surface="chat")
    assert "面上没有" not in allowed.get("error", "")


def test_a_bad_verb_does_not_take_the_world_down(world):
    """一个 agent 进程挑错了动词,不该让世界跟着崩 —— 但也不许静默。"""
    agent = _an_agent(world)
    result = world.act(agent, "并不存在的动词", {})
    assert result["ok"] is False
    assert "并不存在的动词" in result["error"]
    assert "broadcast" in result["error"], "只说不行不够,得告诉他这个面上有什么"
    # 世界还活着
    assert world.act(agent, "broadcast", {"text": "还在"})["ok"] is True


def test_an_unknown_agent_is_a_keyerror(world):
    """未知角色是调用方搞错了对象,不是一次失败的尝试 —— 两者不该长得一样。"""
    with pytest.raises(KeyError):
        world.act("并不存在的人", "broadcast", {"text": "x"})


def test_the_whole_action_holds_the_world_lock(world):
    """**一个动作是原子的。**

    不是为了性能(锁每次只持 62 微秒,而一次 LLM 往返 6.5 秒),是因为 world-rules
    的双缓冲、三源仲裁、`events.seq` 的折叠顺序都要求"一个动作期间世界不会从下面
    被换掉"。多进程 agent 之后这条是正确性的地基。
    """
    agent = _an_agent(world)
    seen: list[bool] = []
    real = tools_mod.call

    def spy(ctx, tool_id, params):
        # 另一个线程此刻应该拿不到锁
        got = threading.Event()

        def grab():
            if world.scheduler._lock.acquire(blocking=False):
                world.scheduler._lock.release()
                got.set()

        t = threading.Thread(target=grab)
        t.start()
        t.join()
        seen.append(got.is_set())
        return real(ctx, tool_id, params)

    tools_mod.call = spy
    try:
        world.act(agent, "broadcast", {"text": "锁着吗"})
    finally:
        tools_mod.call = real
    assert seen == [False], "动作执行期间别的线程拿到了世界的锁 —— 动作不是原子的"


def test_verbs_lists_what_she_can_do(world):
    """给了能力却不给目录等于没给 —— 外面的进程要先知道自己能选什么。"""
    agent = _an_agent(world)
    autonomy = {v["id"] for v in world.verbs(agent, "autonomy")}
    chat = {v["id"] for v in world.verbs(agent, "chat")}
    assert "broadcast" in autonomy and "reach_out" in autonomy
    assert "walk_away" in chat and "reach_out" not in chat

    everything = world.verbs(agent)
    assert {v["id"] for v in everything} == autonomy | chat
    for entry in everything:
        assert entry["surfaces"], f"{entry['id']} 没说自己在哪个面上"
        assert entry["description"], f"{entry['id']} 没有给人读的说明"
