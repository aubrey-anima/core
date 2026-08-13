"""玩家站在角色面前的时候,角色得知道。

`chat_service.respond` 会给角色一段最高优先级的身份声明,里面**明确写死对话媒介**:
同地是「面对面交谈」,否则是「手机文字私聊,对方不在你当前场景中」,并附一条禁令
——「不得臆造看见、触碰对方,或对方站在你身边」。

问题是面对面那一支经公开门面从来不可达:`World.chat` 组 `interlocutor` 时只传
display_name 和 role,而判定读的是 `interlocutor["location"]`,恒为空串。CLI 明明
先把你走到她跟前(`__main__.py:1502` 调 `player_move`),提示词照样告诉她你不在场。
于是她只剩"在手机上收到"可演 —— 世界照跑,给的却是错的东西。

顺带守住反向:**在途不算在场**。黑板的 `loc` 要落地才改写,途中仍是出发地
(`_agent_locations` 专门跳过 `_transit` 就是在补这个洞)。少了这道闸,角色会一边
说"正在去建筑工作室的路上"一边说"我们面对面",同一段 prompt 自相矛盾。
"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world.api import World


class _PromptSpy:
    """把系统提示原样截下来。世界的回复内容不是这里要断言的东西。"""

    def __init__(self) -> None:
        self.system = ""

    async def stream(self, messages):
        self.system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        yield "……"

    async def complete(self, messages):
        self.system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        return "……"


def _say(world: World, agent_id: str, player_id: str = "p1") -> str:
    spy = _PromptSpy()
    world.chat_service._llm = spy
    world.chat_reply(agent_id, [{"role": "user", "content": "在吗"}],
                     player_id=player_id, display_name="阿檀")
    return spy.system


def _somebody(world: World) -> tuple[str, str]:
    agent_id = next(iter(world.scheduler.agents))
    brain = world.scheduler.agents[agent_id]
    return agent_id, (brain.agent.blackboard.read("loc") or brain.agent.location)


def test_standing_in_the_same_place_is_a_face_to_face_conversation(tmp_path):
    """走到她跟前再开口,她就该知道你在跟前。"""
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        agent_id, where = _somebody(world)
        world.player_move("p1", where)

        system = _say(world, agent_id)
        assert "面对面交谈" in system
        assert "手机文字私聊" not in system


def test_being_somewhere_else_is_still_a_phone_chat(tmp_path):
    """不在同一个地方就还是手机 —— 这一支是对的,不许被顺手改掉。"""
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        agent_id, where = _somebody(world)
        elsewhere = next(
            loc for loc in ("home", "cafe", "workshop") if loc != where
        )
        world.player_move("p1", elsewhere)

        system = _say(world, agent_id)
        assert "手机文字私聊" in system
        assert "面对面交谈" not in system


def test_a_player_who_never_moved_is_a_phone_chat(tmp_path):
    """宿主没调过 player_move 就是没告诉世界你在哪 —— 维持今天的行为,不猜。"""
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        agent_id, _ = _somebody(world)
        system = _say(world, agent_id)
        assert "手机文字私聊" in system


def test_an_agent_on_the_road_is_never_face_to_face(tmp_path):
    """在途不算在场。

    黑板的 `loc` 要落地才改写,所以一个正走向别处的角色读出来仍是出发地。只比
    地点就会得到「正在去 workshop 的路上」+「你们都在 cafe,因此这是面对面」——
    自相矛盾,而 LLM 会挑一边编,没有任何报错。
    """
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        agent_id, where = _somebody(world)
        world.player_move("p1", where)
        world.scheduler._transit[agent_id] = {
            "from": where, "to": "workshop", "arrive_at": world.scheduler.clock + 99,
        }

        system = _say(world, agent_id)
        assert "面对面交谈" not in system, "她正在路上,不可能和你面对面"
        assert "手机文字私聊" in system


# ── 体检那两扇门:找不到人的时候说的是什么 ────────────────────────────────
#
# 真人试玩顺手翻出来的两件,和上面是同一个病根:一个世界不认得的 player_id 递进去,
# 门照样一本正经地作答,而答的是另一件事。

from _worldfile import run_cli


def _cli_world() -> str:
    done = run_cli("simulate", "--world-id", "w", "--ticks", "0", "--llm", "mock")
    assert done.returncode == 0, done.stderr
    return "w"


def _land(player_id: str, agent_id: str = "夏") -> None:
    from _worldfile import current_client

    w = World.open("w", redis=current_client(), force_mock_llm=True)
    try:
        w.player_move(player_id, w._tool_runtime.agent_location(agent_id))
    finally:
        w.close()


def test_presence_does_not_blame_the_world_for_one_bad_id():
    """筛过之后的空,和世界本身的空,是两件事。

    `--player-id` 抄错时说「这个世界跟谁都还没打过交道」,会让人跑去查宿主接没接上
    player_move —— 而世界里明明有人,只是这一个 id 不对。
    """
    _cli_world()
    _land("玩家甲")

    one = run_cli("presence", "--world-id", "w", "--player-id", "玩家鉒")
    assert "跟谁都还没打过交道" not in one.stdout, (
        "世界里有玩家,却说这个世界谁都没见过 —— 读的人会去查错的那一层"
    )
    assert "不认得" in one.stdout and "玩家鉒" in one.stdout

    every = run_cli("presence", "--world-id", "w")
    assert "玩家甲" in every.stdout, "前提没成立:这个世界里本来就没人"


def test_presence_lines_up_when_the_ids_are_real_uuids():
    """真部署里 player_id 是 membership 的 uuid(36 字)—— 写死 20 列会糊成一坨。

    只有 `p1`/`cli` 那种短名字才看着是对的,而线上一个都不是。
    """
    _cli_world()
    long_id = "f6e79cbb-51a2-47b8-a9d4-a5a58c463fab"
    _land(long_id)

    done = run_cli("presence", "--world-id", "w")
    line = next(l for l in done.stdout.splitlines() if long_id in l)
    assert line.rstrip() != f"  {long_id}", "前提没成立:这一行本来就没有地点"
    assert line.startswith(f"  {long_id} "), f"id 和地点糊在一起了:{line!r}"


def test_the_cli_no_longer_claims_player_location_is_process_local():
    """3.2.0 把在场搬进了 Redis,`presence` 改了口,`player options` 漏了。

    留着更坏:它教人把一个**真**信号(宿主没接 player_move)当成 CLI 的已知毛病
    挥手过去。判据是这两条命令说的是不是同一件事。
    """
    _cli_world()
    blocked = run_cli("player", "options", "--world-id", "w", "--player", "没这个人")
    assert "进程内" not in blocked.stdout, (
        "这一句 3.2.0 起是假的 —— 位置住 Redis,跨进程、扛重启"
    )
    assert "player_move" in blocked.stdout, "去掉了假话,也把唯一那条线索一起去掉了"

    here = run_cli("presence", "--world-id", "w")
    assert "进程内" not in here.stdout, "前提没成立:另一条也还在说这句"
