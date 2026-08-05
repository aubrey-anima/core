"""认知层:世界的量里,**她感知得到哪些**(perception)。

`stocks` 加进来之后,引擎有了一个很丰富的客观世界,和一群对它一无所知的角色 ——
两套系统跑在同一个进程里。这一层是把它们接起来的那一环。

但接法很要命:**把量整个倒进提示词就是"无所不知的角色"**,她会随口说出矿的确切
储量、别人暗中的恨意、隔着半个地图那棵树的高度。那比她什么都不知道糟得多 ——
不知道最坏是"她没注意到"(玩家看得见),知道太多是**当场破戏且不可挽回**。

所以这些测试的重心不在"她能看见",而在**"她看不见的东西确实没漏出去"**。
"""
from __future__ import annotations

from _worldfile import open_world_at

import json

import pytest

from anima_world.api import World


class PromptSpy:
    """把她真正收到的 system prompt 留下来 —— 泄漏与否只能在这上面验。"""

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


def _world(tmp_path, bare_seed, **kwargs) -> World:
    """素配种子:内置橱窗自己带了存量/规律/可见性声明,拿它来验"没声明会怎样"
    是自相矛盾(见 conftest)。"""
    return open_world_at(str(tmp_path / "w.db"), seed_path=bare_seed,
                      force_mock_llm=True, **kwargs)


def _say(world: World, spy: PromptSpy, text: str = "在吗") -> str:
    world.chat_service._llm = spy
    return world.chat_reply("夏", [{"role": "user", "content": text}],
                            player_id="p1", display_name="阿檀")


def _where(world: World, agent_id: str = "夏") -> str:
    brain = world.scheduler.agents[agent_id]
    return brain.agent.blackboard.read("loc") or brain.agent.location


# ── 默认不可见 ──────────────────────────────────────────────────────────────


def test_an_undeclared_quantity_is_invisible(tmp_path, bare_seed):
    """**这是这一层最重要的一条。** 没声明 = 感知不到。

    反过来(默认公开)的错不可挽回:作者加一个"暗中的恨意"的量,角色下一句就说
    出来了。默认不可见最坏只是"她没注意到"。

    ⚠️ 这里**必须先声明一个别的量**:`perceive()` 在"一条声明都没有"时会提前返回,
    于是不声明任何东西的写法根本走不到默认值那条路 —— 那样的测试把默认值改成
    "公开"也照样绿(第一版就是这么写的,验出来是假绿)。
    """
    with _world(tmp_path, bare_seed) as world:
        world.declare_visibility("world", "季节", "public")   # 让这一层真的启用
        world.set_stock("world", "季节", 1)
        world.set_stock("agent:夏", "暗中的恨意", 0.9)         # 这两个没声明
        world.set_stock("world", "瘟疫", 1)
        spy = PromptSpy()
        _say(world, spy)

        assert "季节 1" in spy.prompts[0], "前提:这一层得真的在工作"
        assert "暗中的恨意" not in spy.prompts[0], "没声明的量漏进了提示词"
        assert "瘟疫" not in spy.prompts[0], "没声明的量漏进了提示词"
        perceived = world.perception("夏")
        assert perceived["own"] == {} and perceived["public"] == {"季节": 1.0}


def test_an_undeclared_quantity_on_a_visible_thing_stays_hidden(tmp_path, bare_seed):
    """同一个东西身上,声明过的看得见、没声明的看不见 —— 逐个量算,不是整体放行。

    这是最像真实事故的一种:一棵树的 size 声明了可见,而作者后来给树加了一个
    `内部编号` 或 `作者备注`,那个量绝不该跟着一起被角色看见。
    """
    with _world(tmp_path, bare_seed) as world:
        world.declare_visibility("tree", "size", "here")
        world.set_stocks("tree:oak_01", {"size": 8, "内部编号": 4217})
        world.place_stock("tree:oak_01", _where(world), label="老橡树")
        spy = PromptSpy()
        _say(world, spy)

        assert "size 8" in spy.prompts[0], "前提:声明过的那个得看得见"
        assert "4217" not in spy.prompts[0], "同一个东西上没声明的量被一起放行了"
        assert world.perception("夏")["here"] == {"tree:oak_01": {"size": 8.0}}


def test_a_world_with_no_declarations_adds_nothing_to_the_prompt(tmp_path, bare_seed):
    """声明本身就是这一层的开关 —— 没声明过的世界不该多一个 token。"""
    with _world(tmp_path, bare_seed) as world:
        world.set_stock("tree:oak_01", "size", 8)
        spy = PromptSpy()
        _say(world, spy)
        assert "感觉到" not in spy.prompts[0], "空的感知块也进了提示词"


# ── 三档 ────────────────────────────────────────────────────────────────────


def test_she_knows_her_own_quantities(tmp_path, bare_seed):
    with _world(tmp_path, bare_seed) as world:
        world.declare_visibility("agent", "功力", "self")
        world.set_stock("agent:夏", "功力", 120)
        spy = PromptSpy()
        _say(world, spy)

        assert "功力 120" in spy.prompts[0]
        assert world.perception("夏")["own"] == {"功力": 120.0}


def test_a_self_quantity_is_private_to_its_owner(tmp_path, bare_seed):
    """`self` 档只有主人知道 —— 她不知道别人的功力。"""
    with _world(tmp_path, bare_seed) as world:
        world.declare_visibility("agent", "功力", "self")
        world.set_stock("agent:遥", "功力", 999)
        spy = PromptSpy()
        _say(world, spy)

        assert "999" not in spy.prompts[0], "她读到了别人的私有量"
        assert world.perception("夏")["own"] == {}


def test_a_here_quantity_needs_to_be_in_the_same_place(tmp_path, bare_seed):
    """在场可见:同地才知道这棵树多高。"""
    with _world(tmp_path, bare_seed) as world:
        world.declare_visibility("tree", "size", "here")
        world.set_stock("tree:oak_01", "size", 8)
        here = _where(world)
        elsewhere = next(loc for loc in ("home", "cafe", "workshop") if loc != here)

        world.place_stock("tree:oak_01", elsewhere, label="老橡树")
        spy = PromptSpy()
        _say(world, spy)
        assert "老橡树" not in spy.prompts[0], "隔着半个地图就看见那棵树了"

        world.place_stock("tree:oak_01", here)
        spy2 = PromptSpy()
        _say(world, spy2)
        assert "老橡树" in spy2.prompts[0] and "size 8" in spy2.prompts[0]


def test_a_public_quantity_is_known_everywhere(tmp_path, bare_seed):
    with _world(tmp_path, bare_seed) as world:
        world.declare_visibility("world", "season", "public")
        world.set_stock("world", "season", 3)
        spy = PromptSpy()
        _say(world, spy)

        assert "season 3" in spy.prompts[0]
        assert world.perception("夏")["public"] == {"season": 3.0}


def test_hidden_stays_hidden_even_when_declared_explicitly(tmp_path, bare_seed):
    with _world(tmp_path, bare_seed) as world:
        world.declare_visibility("ore", "reserve", "hidden")
        world.set_stock("ore:iron_01", "reserve", 42)
        world.place_stock("ore:iron_01", _where(world), label="铁矿")
        spy = PromptSpy()
        _say(world, spy)
        assert "42" not in spy.prompts[0] and "铁矿" not in spy.prompts[0]


def test_a_wildcard_declaration_covers_every_kind(tmp_path, bare_seed):
    """可见性是"这类量什么性质"的属性 —— 所有树的 size 不必一棵棵声明。"""
    with _world(tmp_path, bare_seed) as world:
        world.declare_visibility("*", "size", "here")
        for index in range(3):
            world.set_stock(f"tree:oak_{index}", "size", index + 1)
            world.place_stock(f"tree:oak_{index}", _where(world))
        assert len(world.perception("夏")["here"]) == 3


# ── 数字要像人话 ────────────────────────────────────────────────────────────


def test_numbers_are_not_rendered_as_floating_point_noise(tmp_path, bare_seed):
    """提示词里不该出现 `8.000000000001` —— 她会照着念。"""
    with _world(tmp_path, bare_seed) as world:
        world.declare_visibility("agent", "功力", "self")
        world.set_stock("agent:夏", "功力", 8.0)
        spy = PromptSpy()
        _say(world, spy)
        assert "功力 8" in spy.prompts[0]
        assert "8.0" not in spy.prompts[0]


# ── 她能据此行动 ────────────────────────────────────────────────────────────


def test_what_she_perceives_reaches_her_autonomous_decision(tmp_path, bare_seed):
    """光进提示词不够 —— 她**做决定**时也得看得见,否则"矿富了所以我去挖"
    这种事永远不会发生。"""
    prompts: list[str] = []

    class Decider:
        async def complete(self, messages):
            prompts.append("\n".join(m["content"] for m in messages))
            return "无"

        async def stream(self, messages):
            yield await self.complete(messages)

    world = open_world_at(str(tmp_path / "w.db"), agents=1, force_mock_llm=True)
    with world:
        world.declare_visibility("world", "粮价", "public")
        world.set_stock("world", "粮价", 7)
        world.chat_service._background_llm = Decider()
        world.config_set("chat.tools.enabled", True)
        world.config_set("autonomy.enabled", True)
        world.config_set("autonomy.interval_ticks", 1)
        world._install_autonomy()
        world.player_move("p1", _where(world))

        world.tick(1)
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not prompts:
            time.sleep(0.02)
        assert prompts, "定时轮次没跑"
        assert "粮价 7" in prompts[0], "她做决定时看不见世界的量"


# ── 种子 ────────────────────────────────────────────────────────────────────


def test_visibility_can_be_declared_in_the_seed(tmp_path, bare_seed):
    from importlib import resources

    seed = json.loads(
        (resources.files("anima_world") / "world_seed.json").read_text(encoding="utf-8")
    )
    seed["stocks"] = [{"owner": "world", "values": {"season": 2}}]
    seed["stock_visibility"] = [
        {"kind": "world", "key": "season", "visible": "public"},
        {"kind": "agent", "key": "功力", "visible": "self"},
    ]
    # 这个世界自己定义可见性,不要橱窗那棵树的声明掺进来(种类声明同时就是可见性
    # 声明 —— 那条正是 test_ontology 在验的,这里验的是显式声明那一段)。
    seed.pop("kinds", None), seed.pop("entities", None)
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

    with open_world_at(str(tmp_path / "w.db"), seed_path=str(path),
                    force_mock_llm=True) as world:
        declared = {(r["kind"], r["key"]): r["visibility"] for r in world.visibility_rules()}
        assert declared == {("world", "season"): "public", ("agent", "功力"): "self"}
        assert world.perception("夏")["public"] == {"season": 2.0}


def test_a_bad_visibility_value_is_dropped_not_fatal(tmp_path, bare_seed):
    """写错一档的后果是"她本该知道却不知道",不该拦住启动。"""
    from importlib import resources

    seed = json.loads(
        (resources.files("anima_world") / "world_seed.json").read_text(encoding="utf-8")
    )
    seed["stock_visibility"] = [
        {"kind": "world", "key": "season", "visible": "所有人都能看见"},
        {"kind": "world", "key": "粮价", "visible": "public"},
    ]
    seed.pop("kinds", None), seed.pop("entities", None)
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

    with open_world_at(str(tmp_path / "w.db"), seed_path=str(path),
                    force_mock_llm=True) as world:
        declared = {r["key"] for r in world.visibility_rules()}
        assert declared == {"粮价"}, "坏的那条该被丢掉,好的那条该留下"


def test_declarations_survive_a_reopen(tmp_path, bare_seed):
    db = str(tmp_path / "w.db")
    with open_world_at(db, seed_path=bare_seed, force_mock_llm=True) as world:
        world.declare_visibility("world", "season", "public")
        world.set_stock("world", "season", 4)
    with open_world_at(db, force_mock_llm=True) as reopened:
        assert reopened.perception("夏")["public"] == {"season": 4.0}
