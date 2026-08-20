"""同屋有谁、他此刻在做什么 —— 她读得到吗。

这一份钉的是一条病:**世界里发生的事,提示词里不写。**

两处现场,同一个病:

- 晚潮那 11 个标着「得有人一起」的动词(并排坐着 · 陪一次夜播 · 一起听完一面 …)
  写了 238 天、161648 条事件,**一次都没被发出去过**。机制全在:`interact` 早在
  自主面上、参数里就有 `with`、同意与 `joint_gate` 与共同经历都写好了。缺的只是
  `_autonomy_context.present` 只数玩家 —— 一个站在三个同事中间的角色,提示词第一句
  写着「这会儿你身边没有别人」,而下一句正让她用 `with` 点名。名单从来没给过她。
- 齐老板对着一个刚擦完窗、上过发条、对过表的玩家说「**我没见你动过手**」。
  那句话在事实上是对的:感知块里那个人只有量,没有动作。而这不是玩家特殊 ——
  同屋的角色彼此也看不见对方在干嘛。

所以这里的断言分两类:**名单上有没有人**,和**名单上的人在不在做事**。
两类都要求 NPC 与玩家走同一段代码 —— 各写一遍必然分叉,而分叉那天不报错。
"""
from __future__ import annotations

import json

import pytest

from _worldfile import write_seed_file


_SEED = {
    "locations": [
        {"id": "cafe", "name": "咖啡店", "description": "拐角那家"},
        {"id": "yard", "name": "后院", "description": "堆着几只空木箱"},
    ],
    "agents": [
        {"id": "夏", "name": "苏晚夏", "location": "cafe", "personality": "开朗"},
        {"id": "遥", "name": "沈遥", "location": "cafe", "personality": "话少"},
        {"id": "柔", "name": "林柔", "location": "yard", "personality": "温吞"},
    ],
    "kinds": [
        # `here` 而不是 `self`:别人看得见她身上这个量,于是她也会落进
        # `_settle_actor_place` 那张可见性表 —— 感知块里那一行的前提。
        {"id": "agent", "quantities": {
            "体力": {"default": 100, "visibility": "here"},
        }},
        {"id": "bench", "gloss": "一条长椅", "quantities": {
            "坐过几回": {"default": 0.0, "visibility": "here"},
        }, "affordances": {
            "同坐": {
                "label": "一起坐会儿",
                "participants": {"min": 1, "max": 2},
                "requires": ["me_体力 >= 5"],
                "costs": {"体力": "me_体力 - 5"},
                "set": {"坐过几回": "坐过几回 + 1"},
            },
            # 占着人的长过程 —— 这一件才是"此刻在做什么"答得出名字的那一类。
            "夜播": {
                "label": "陪一次夜播",
                "duration": 6,
                "occupies": True,
                "requires": ["me_体力 >= 5"],
                "costs": {"体力": "me_体力 - 5"},
                "set": {"坐过几回": "坐过几回 + 1"},
            },
            "擦一擦": {"label": "擦一擦", "requires": ["me_体力 >= 1"],
                       "costs": {"体力": "me_体力 - 1"}},
        }},
    ],
    "entities": [{"id": "bench:oak", "name": "橡木长椅", "location": "cafe"}],
    "relations": [
        {"a": "夏", "b": "遥", "sentiment": 0.6},
        {"a": "遥", "b": "夏", "sentiment": 0.6},
    ],
}


class PromptSpy:
    """她真正收到的 system prompt。提示词里有没有那句话,只能在这上面验。"""

    def __init__(self, reply: str = "（夏点头。）嗯。") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def _record(self, messages) -> str:
        self.prompts.append("\n".join(
            m["content"] for m in messages if m.get("role") == "system"
        ))
        return self.reply

    async def stream(self, messages):
        yield self._record(messages)

    async def complete(self, messages) -> str:
        return self._record(messages)


@pytest.fixture
def world(open_world, tmp_path):
    seed = json.loads(json.dumps(_SEED))
    w = open_world(world_file=write_seed_file(tmp_path / "w.cyberworld", seed))
    w.tick(1)   # 让 _settle_actor_place 把在场的人落进可见性表
    yield w


def _ctx(world, agent_id: str = "夏"):
    return world._autonomy_context(agent_id, world.scheduler.world_time())


def _prompt_to(world, player_id: str = "p1", display_name: str = "阿檀") -> str:
    spy = PromptSpy()
    world.chat_service._llm = spy
    world.chat_reply("夏", [{"role": "user", "content": "在吗"}],
                     player_id=player_id, display_name=display_name)
    return spy.prompts[-1]


# ── ① 身边有人这件事,得先让她知道 ───────────────────────────────────────


def test_同屋只有NPC时在场名单也不是空的(world):
    """`present` 从前只数玩家 —— 那是那 11 个动词一次都没发出去的**总闸**。

    一个人站在同事旁边,提示词第一句写着「这会儿你身边没有别人」,而下面
    `interact` 的说明正让她用 `with` 点名跟她一起做的人。她不是不肯叫人,
    是**没人告诉过她身边有谁**。整条链的其余部分早就通了。
    """
    ctx = _ctx(world)
    assert ctx.present, "同屋站着一个沈遥,而在场名单是空的"
    assert [p["id"] for p in ctx.present] == ["遥"]
    assert ctx.present[0]["kind"] == "agent", (
        "少了 kind,菜单和宿主就分不清跟前站的是角色还是玩家"
    )
    assert ctx.present[0]["name"] == "沈遥"

    block = ctx.present_block()
    assert "这会儿你身边没有别人" not in block
    assert "沈遥(遥)" in block, "id 要露出来 —— 那是她写进 with 的那个东西"

    # 不同屋的人不算,在场名单不是花名册。
    assert "柔" not in [p["id"] for p in ctx.present]
    assert not _ctx(world, "柔").present, "后院只有她一个人"


def test_在途的人不算站在你身边(world):
    """和 `_agent_locations()` 同一条规矩:只比地点的话,一个正在赶路的人会被
    判成和她面对面 —— 而她会当着一个不在的人的面说话。"""
    scheduler = world.scheduler
    scheduler._transit["遥"] = {
        "from": "cafe", "to": "yard", "arrive_at": scheduler.clock + 99,
    }
    assert not _ctx(world).present


# ── ② 菜单:名单补上了,不该多摆一个必然被拒的选项 ────────────────────────


def test_只有NPC在场时菜单留得住联合动词_但不摆reach_out(world):
    """两件事一起钉。

    **留得住的那半**:`interact` 声明的是 `requires_target_entity`(手边得有东西),
    不是 `requires_colocation`。所以身边只有同事时它照旧在菜单上 —— 而现在
    `present` 里终于有名字可以写进 `with` 了,这一格才真的可用。

    **摆不上的那半**:`requires_colocation` 那格声明逐字写着「这个能力要**玩家**
    真的在她跟前」,自主面上唯一带它的 `reach_out` 只收玩家 id。`present` 这一轮
    补进了同地的角色,若照单全收去数,一个身边只有三个同事的人就会被摆上
    `reach_out`,然后必然收到「这会儿她身边没有人」。摆一个必然被拒的选项不只是
    浪费一次调用 —— 她挑了、被拒了,而这次失败教不会她任何事。
    """
    ctx = _ctx(world)
    assert ctx.present and all(p["kind"] == "agent" for p in ctx.present)
    assert ctx.targets, "前提没成立:长椅该是她这儿能被做点什么的东西"

    menu = {spec.id for spec in world._autonomy_menu(ctx)}
    assert "interact" in menu, "身边有人、手边有东西,联合动词却不在菜单上"
    assert "reach_out" not in menu, (
        "身边一个玩家都没有,却摆上了一个只收玩家 id 的能力 —— 必然被拒"
    )

    # 玩家一进屋,那一格立刻回来:闸数的是玩家,不是"有没有人"。
    world.player_move("p1", "cafe")
    assert "reach_out" in {spec.id for spec in world._autonomy_menu(_ctx(world))}


# ── ③ 名单上的人在做什么 ─────────────────────────────────────────────────


def test_正在做事的人不再被写成闲着(world):
    """`describe_here` 那一行从前只有量:`- 这里的沈遥:体力 95`。

    她读到的是一个站在那儿一动不动的人,而世界里他正忙着一件要占他六个 tick、
    真花了体力的事。`_ACTIVITY_LABELS` 那张表当时连 `interact` 都没有 ——
    一个正在干活的人被写成「闲着」,而「闲着」在中文里的意思是"可以打扰"。
    """
    started = world.act("遥", "interact",
                        {"target": "bench:oak", "verb": "夜播"}, surface="body")
    assert started["ok"], started

    doing = world._activities_now()
    assert doing.get("agent:遥") == "在陪一次夜播", (
        f"长过程占着他,而这份快照说他 {doing.get('agent:遥')!r}"
    )

    perceived = world.perception("夏")
    assert perceived["activities"].get("agent:遥") == "在陪一次夜播"
    assert "agent:遥" in perceived["here"], "前提没成立:他没落进她的感知"

    # 三个块共用同一份措辞 —— 各拼一遍必然分叉,而分叉那天不报错。
    line = world._perceive("夏", "cafe").describe_here("agent:遥")
    assert "沈遥,在陪一次夜播" in line, line
    assert "在陪一次夜播" in world.world_context("夏", "p1")["presence"]["others"]
    assert "沈遥(遥,在陪一次夜播)" in _ctx(world).present_block()

    # 那件事做完之后,这一句要跟着消失 —— 留着就是一句谎。
    world.tick(7)
    assert "遥" not in "".join(world._activities_now())
    assert world.perception("夏")["activities"] == {}


def test_闲着的人不缀那一句(world):
    """给每个人都缀一句「闲着」是提示词噪音,而**没有那句话本身就是闲着**。"""
    assert world._activities_now() == {}
    assert world.perception("夏")["activities"] == {}
    assert "沈遥(遥)" in _ctx(world).present_block()


# ── ④ 齐老板那句话的回归测试 ─────────────────────────────────────────────


def test_我没见你动过手(world):
    """线上原话。一个玩家刚在她眼皮底下擦了窗、上了发条、对过表,她说：我没见你动过手。

    **那句话在事实上是对的。** 感知块里那个人只有量(`手上的活儿 上过手、耳根发红
    没什么`),没有一个字讲他做了什么;presence 块里只有一个光名字。世界里那三次
    动作有真实的状态变更和体力开销,一条都没进她的提示词。

    修法不是给玩家开一条特殊通道 —— 病根本不是玩家特殊(同屋的两个角色彼此也看
    不见对方在干嘛)。所以这里连断言都要求两边同形:同一个动词、同一句措辞、
    同一份 `_activities_now()`。
    """
    world.player_move("p1", "cafe")
    world.players["p1"]["display_name"] = "阿檀"
    world.tick(1)

    out = world.player_tool("p1", "interact",
                            {"target": "bench:oak", "verb": "夜播"})
    assert out["ok"], out

    # 一、他在做事这件事,引擎答得出来 —— 而且键和角色那半在同一个命名空间里。
    doing = world._activities_now()
    assert doing.get("agent:player:p1") == "在陪一次夜播", doing

    # 二、presence 块里看得见(§3.1 验收 ④ 的字面)。用另一个对话者,是因为
    #     正在跟她说话的那一位由身份块单独讲,不进 others。
    others = world.world_context("夏", "p2")["presence"]["others"]
    assert "阿檀(在陪一次夜播)" in others, others

    # 三、线上那一行本身:他就是对话者,而她读到的是感知块那一行。
    line = world._perceive("夏", "cafe").describe_here("agent:player:p1")
    assert "阿檀,在陪一次夜播" in line, line
    text = _prompt_to(world)
    assert "在陪一次夜播" in text, "她的提示词里还是没有一个字讲他做了什么"

    # 四、**一处分支都不要有**:同一件事换成角色去做,措辞逐字相同。
    world.tick(7)
    world.act("遥", "interact", {"target": "bench:oak", "verb": "夜播"},
              surface="body")
    assert world._activities_now().get("agent:遥") == "在陪一次夜播"


def test_她读自己那一句_和别人读她那一句_是同一句话(world):
    """**一处分支都不要有**,而这里曾经还剩最后一处:她自己那一行。

    同屋的人走 `_activities_now()`(它认得 `:engaged` 那件占着人的长过程),
    她自己那一行却单独走 `_ACTIVITY_LABELS`(排班那一层的动作名,`interact`
    在那张表里是「在忙手上的事」,而 `:engaged` 它根本不知道)。于是同一份
    提示词里两句话互相打脸:她读到「你在咖啡店,闲着」,下一行写着
    「沈遥(在陪一次夜播)」—— 同一分钟、同一个房间。

    「闲着」在中文里的意思是"可以打扰",所以这不是措辞问题:她会照着那句话
    去起另一件事。
    """
    started = world.act("夏", "interact",
                        {"target": "bench:oak", "verb": "夜播"}, surface="body")
    assert started["ok"], started
    world.player_move("p1", "cafe")

    # 一、她自己读到的那一句(聊天的 presence 块)。
    presence = world.world_context("夏", "p1")["presence"]
    assert presence["activity"] == "在陪一次夜播", presence["activity"]
    assert "闲着" not in _prompt_to(world)

    # 二、别人读到的她 —— 逐字同一句。
    others = world.world_context("遥", "p1")["presence"]["others"]
    assert "苏晚夏(在陪一次夜播)" in others, others

    # 三、自主上下文那一头同一条(她要不要再起一件事,读的就是这句话)。
    assert _ctx(world).activity == "在陪一次夜播"

    # 四、做完之后两边一起变回来 —— 只改一边就是把分叉挪了个地方。
    world.tick(7)
    assert world.world_context("夏", "p1")["presence"]["activity"] != "在陪一次夜播"
    assert _ctx(world).activity != "在陪一次夜播"


def test_在路上那一句留着_它答的是你在哪儿(world):
    """收敛不是把话都抹平。「正在去后院的路上」比「正准备出门」多一格终点,
    而路上的人本来就不在做别的事 —— 这一支不是分支,是多知道了一件事。"""
    scheduler = world.scheduler
    scheduler._transit["夏"] = {
        "from": "cafe", "to": "yard", "arrive_at": scheduler.clock + 99,
    }
    presence = world.world_context("夏", "p1")["presence"]
    assert presence["activity"] == "正在去后院的路上", presence["activity"]


def test_在路上那一句_自主上下文也读得到(world):
    """🔴 **第四处分叉,潜伏着的那一处。** 在途这一支从前只写在 `world_context`
    里,`_autonomy_context` 没有 —— 于是同一个正在去后院路上的人,她跟人说话时
    那份提示词说「正在去后院的路上」,没人跟她说话时那份说**「闲着」**,
    而「闲着」在中文里的意思是"可以打扰"。

    ⚠️ 它此前走不到:`_maybe_run_autonomy` 把在途的人整个排除在外
    (「在赶路的人不做别的事」),所以活路上没人撞见过。**照样钉** ——
    潜伏的分叉不是没有分叉,它只是在等一个改动把它放出来;而这条测试直接问
    `_autonomy_context`,不经过那道过滤,所以它验的是措辞本身。
    """
    scheduler = world.scheduler
    scheduler._transit["夏"] = {
        "from": "cafe", "to": "yard", "arrive_at": scheduler.clock + 99,
    }
    assert _ctx(world).activity == "正在去后院的路上"
    # 两头逐字同一句 —— 这才是"唯一一份措辞"的判据。
    assert (_ctx(world).activity
            == world.world_context("夏", "p1")["presence"]["activity"])


# ── 行为树撞上联合动词要说一句 ───────────────────────────────────────────


def test_行为树叫不上人的时候不再静默重试(world, caplog):
    """排班里写一个标着 participants 的动词,树每 tick 试一次、每次被同一句话
    拒回来,只留一行 `logger.debug` —— 作者看到的是"这个动词从来没发生过",
    看不到"她一直在试"。而那正是这一单要查的那条缝本身。

    **不替她挑同伴**:叫人要先征得同意,征同意要走网络,而这里跑在时钟线程的
    锁里。时钟永不等网络。
    """
    from anima_world.actions import ActionDescriptor

    scheduler = world.scheduler
    with caplog.at_level("INFO", logger="anima_world.scheduler"):
        ok = scheduler.emit_action(
            scheduler.agents["夏"].agent,
            ActionDescriptor("interact", {"target": "bench:oak", "verb": "同坐"}),
        )
    assert ok is False, "行为树本来就发不出「一起」—— 这一条没变"
    assert "得有人一起" in caplog.text
    assert "自主轮次" in caplog.text, "光说不行还不够,得说她该走哪条路"

    health = world.state()["runtime"]["subsystems"]["joint_from_tree"]
    assert health["status"] == "degraded" and health["degraded"] >= 1

    # 再撞一次:计数照加,而日志不再刷 —— 持续降级的子系统会把自己的健康报告
    # 变成噪音,那是 `note_subsystem` 早写下的一课。
    caplog.clear()
    with caplog.at_level("INFO", logger="anima_world.scheduler"):
        scheduler.emit_action(
            scheduler.agents["夏"].agent,
            ActionDescriptor("interact", {"target": "bench:oak", "verb": "同坐"}),
        )
    assert "得有人一起" not in caplog.text
    assert world.state()["runtime"]["subsystems"]["joint_from_tree"]["degraded"] >= 2
