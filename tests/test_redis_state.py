"""运行时状态住进 Redis:进程不再持有变量。

这一层解的是"很多进程操作同一个世界"里最硬的那个结。世界的真相此前**只有一半在
db 里**:事件、记忆、关系、量都落库,而**黑板**(每个角色 20 个键:她在哪、在干嘛、
饿不饿、打算做什么、行为树这一 tick 选了哪个动作)、时钟、在途集合全是 Python 对象。
两个进程各开同一个世界文件,会读到同一份历史,然后**在各自内存里跑出两个不同的世界**。

盯四件事:

1. **另一个只有 Redis 连接的进程,读得到、改得动** —— 这是全部意义
2. 搬家不是清空 —— 创世写进黑板的性格、位置必须跟过去
3. 接口和进程内那个黑板**逐字相同**,所以行为树/需求/调度器一行都不用改
4. 代价照实报,不假装没有
"""
from __future__ import annotations

import pytest

fakeredis = pytest.importorskip("fakeredis")

from anima_world.api import World  # noqa: E402
from anima_world.bt_nodes import Blackboard  # noqa: E402
from anima_world.redis_state import (  # noqa: E402
    CachedRedisBlackboard,
    RedisBlackboard,
    agent_key,
)


@pytest.fixture()
def redis():
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture()
def world(tmp_path, redis):
    w = World.open(
        str(tmp_path / "world.db"), force_mock_llm=True, redis=redis, world_id="t"
    )
    yield w
    w.close()


def _an_agent(world: World) -> str:
    return sorted(world.scheduler.agents)[0]


def test_another_process_sees_and_can_change_the_same_agent(world, redis):
    """**全部意义在这一条。**

    第二个"进程"只有一个 Redis 连接 —— 没有 World、没有调度器、没有任何本地状态。
    它必须读得到她此刻在哪、在干嘛,而且改了之后第一个进程立刻看得见。
    """
    agent = _an_agent(world)
    world.tick(20)
    mine = world.scheduler.agents[agent].agent.blackboard

    outsider = RedisBlackboard(redis, agent_key("t", agent))
    assert outsider.read("loc") == mine.read("loc"), "另一个进程读到的位置对不上"
    assert outsider.read("need.energy") is not None

    outsider.write("state.status", "被别的进程改了")
    assert mine.read("state.status") == "被别的进程改了", (
        "别的进程改了,这个进程没看见 —— 状态还是进程私有的"
    )


def test_moving_in_carries_what_was_already_on_the_board(world):
    """搬家不是清空:创世写进去的性格、位置得跟过去,否则第一个 tick 她没有性格。"""
    agent = _an_agent(world)
    board = world.scheduler.agents[agent].agent.blackboard
    assert isinstance(board, RedisBlackboard)
    assert board.read("loc"), "位置没跟过来"
    assert board.read("personality"), "性格没跟过来"


def test_the_interface_matches_the_in_process_blackboard(redis):
    """接口逐字相同,所以它是直接替换 —— 行为树/需求/调度器一行都不用改。

    `snapshot()` 也在这条里:此前 scheduler 直接摸 `blackboard._data`,而那个属性
    在 Redis 版上根本不存在。接口漏一个洞,替换就会在运行期炸。
    """
    plain, backed = Blackboard(), RedisBlackboard(redis, "t:x")
    for board in (plain, backed):
        board.write("loc", "cafe")
        board.write("plan.params", {"location": "home"})
        board.write("goals", ["把店开起来", "多认识人"])
        assert board.read("loc") == "cafe"
        assert board.read("plan.params") == {"location": "home"}
        assert board.read("goals") == ["把店开起来", "多认识人"]
        assert board.read("从没写过的键") is None
        assert board.snapshot()["loc"] == "cafe"


def test_a_world_on_redis_still_runs(world):
    """换了黑板,世界照样活 —— 这一层不该改变任何行为。"""
    agent = _an_agent(world)
    before = world._tool_runtime.agent_location(agent)
    world.tick(288)
    assert world.scheduler.clock >= 288
    assert world.scheduler.agents[agent].agent.blackboard.read("loc")
    assert world.history(limit=10)["events"], "跑了一天一个事件都没有"
    assert before is not None


def test_two_worlds_on_one_redis_do_not_share_brains(tmp_path, redis):
    """一个 Redis 上跑十个世界是常态。键撞车的后果是两个世界的角色共用一个脑子。"""
    a = World.open(str(tmp_path / "a.db"), force_mock_llm=True, redis=redis, world_id="a")
    b = World.open(str(tmp_path / "b.db"), force_mock_llm=True, redis=redis, world_id="b")
    try:
        agent = _an_agent(a)
        a.scheduler.agents[agent].agent.blackboard.write("state.status", "A 世界的")
        b.scheduler.agents[agent].agent.blackboard.write("state.status", "B 世界的")
        assert a.scheduler.agents[agent].agent.blackboard.read("state.status") == "A 世界的", (
            "两个世界的同名角色共用了一个黑板"
        )
    finally:
        a.close()
        b.close()


def test_the_cached_variant_batches_but_says_so(redis):
    """批量版把往返从每 tick 80 次降到 2 次 —— 代价是那一 tick 状态确实在进程里。

    所以它只在"同一时刻只有一个进程推这个世界的时钟"的前提下能用。这条测试钉住
    那个代价是**真实存在**的,免得有人把它当成免费的加速。
    """
    board = CachedRedisBlackboard(redis, "t:cached")
    board.write("loc", "cafe")

    board.begin()
    board.write("loc", "workshop")
    # 还没 flush:别的进程看到的仍是旧值 —— 这就是那个代价
    assert RedisBlackboard(redis, "t:cached").read("loc") == "cafe"
    assert board.read("loc") == "workshop", "自己读自己该看见新值"

    board.flush()
    assert RedisBlackboard(redis, "t:cached").read("loc") == "workshop"
