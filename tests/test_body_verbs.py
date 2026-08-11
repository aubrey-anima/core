"""过日子的动作成为一等动词(走 / 吃 / 干活 / 睡 / 搭话 / 待着)。

修的是一个此前没人注意到的割裂:**同一个人有两套能力,取决于谁在触发她**。聊天里
她能"走开",排班表里没有这个词;排班表里她会"走到咖啡店",而她自己决定时挑不了 ——
因为那 6 个动作住在 `bt_actions` 表里,只有行为树够得着。

顺带还原了 `bt_actions` 的真实身份:它不是动词表,是**已经绑好参数的调用表**
(`go_to_cafe` = `walk(location="cafe")`,`chat_with_夏` = `chat(target="夏")`),
所以那 20 行是 6 个动词的若干绑定,而 `chat_with_*` 会随人口线性增长。

盯四件事:

1. **和行为树是同一条路** —— "她自己决定走"和"排班让她走"在世界里必须是同一件事
2. **世界说"还不行"要照实报** —— 不假装成功
3. **面是隔开的** —— 这批动词不进聊天/自主的菜单,所以提示词逐字不变
4. **编造的地名当场拒** —— 而且告诉她有哪些地方
"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world import tools as tools_mod
from anima_world.api import World


@pytest.fixture()
def world(tmp_path):
    w = open_world_at(str(tmp_path / "world.db"), force_mock_llm=True)
    w.tick(50)
    yield w
    w.close()


def _an_agent(world: World) -> str:
    return sorted(world.scheduler.agents)[0]


def _settle(world: World, agent: str, limit: int = 288) -> None:
    """等到她手上没有正在赶的路。

    在途时任何动作都会被"她在赶路"挡回来 —— 那是引擎该有的闸(`test_the_world_saying_
    not_yet_is_reported_honestly` 专门验它),而下面几条要验的是别的事。从前这里不必等,
    只是因为所有世界都从午夜起步、跑 50 tick 恰好是凌晨没人动;几点开门如今是这个世界
    作者的意见(`world.start_time`),再靠那个巧合就是让测试站在沙子上。
    """
    for _ in range(limit):
        if agent not in world.scheduler._transit:
            return
        world.tick(1)
    raise AssertionError(f"{agent} 整整一个世界日都在路上,这条测试没法起头")


def _bring_to(world: World, agent: str, where: str, limit: int = 288) -> None:
    """把她挪到 where,并等她**真的走到**。

    在场是 `interact` 的前提(隔着半个地图照料不到,`test_隔着半个地图照料不到` 验的
    就是这道闸),所以要验"照料真的让树长高"就得先让她站在树跟前。这一步走的是
    `move_agent` —— BT 走的那条路,不是把位置直接写进表里:后者会造出一个"她瞬移到了
    cafe"的世界,而那种世界里验出来的东西不作数。
    """
    _settle(world, agent)
    if world._tool_runtime.agent_location(agent) == where:
        return
    world._tool_runtime.move_agent(agent, where)
    for _ in range(limit):
        world.tick(1)
        if (agent not in world.scheduler._transit
                and world._tool_runtime.agent_location(agent) == where):
            return
    raise AssertionError(f"没能把 {agent} 挪到 {where}")


def test_deciding_to_walk_is_the_same_thing_as_being_scheduled_to_walk(world):
    """全部意义在这一条:两条路发同样的事件、花同样的时间。

    另写一份"外部版本的走路"迟早和行为树那份分叉,而分叉的那天没人会发现 ——
    她会在提示词里"走了",在世界里还站在原地。
    """
    agent = _an_agent(world)
    _settle(world, agent)   # 这一趟必须由这条测试发起,不能是她本来就在赶的那一趟
    start = world._tool_runtime.agent_location(agent)
    target = next(p for p in world._tool_runtime.point_ids() if p != start)
    before = len(world.history(limit=999)["events"])

    result = world.act(agent, "walk", {"location": target}, surface="body")

    assert result["ok"] is True and result["detail"]["took"] is True
    fresh = world.history(limit=999)["events"][before:]
    kinds = [e["type"] for e in fresh]
    assert "travel" in kinds, f"没发 travel —— 这不是行为树那条路:{kinds}"
    # 起程不是瞬移:她此刻在路上,还没到
    assert world.scheduler._transit.get(agent), "走路没花时间 —— 变成瞬移了"

    for _ in range(60):
        world.tick(1)
        if not world.scheduler._transit.get(agent):
            break
    assert world._tool_runtime.agent_location(agent) == target


def test_the_world_saying_not_yet_is_reported_honestly(world):
    """`emit_action` 返回 False 是"世界这会儿不接",不是成功 —— 不许粉饰。"""
    agent = _an_agent(world)
    other = next(a for a in sorted(world.scheduler.agents) if a != agent)
    here = world._tool_runtime.agent_location(agent)
    if world._tool_runtime.agent_location(other) == here:
        # 把对方支走,制造"要找的人不在这儿"
        away = next(p for p in world._tool_runtime.point_ids() if p != here)
        world._tool_runtime.move_agent(other, away)
        for _ in range(60):
            world.tick(1)
            if world._tool_runtime.agent_location(other) == away:
                break

    result = world.act(agent, "talk_to", {"target": other}, surface="body")
    assert result["ok"] is False
    assert result["detail"]["took"] is False
    assert "不在这儿" in result["error"] or "赶路" in result["error"]


def test_a_made_up_place_is_refused_with_the_real_list(world):
    """模型编地名是常事。当场拒,并告诉她有哪些地方 —— 只说"不行"等于没说。"""
    agent = _an_agent(world)
    result = world.act(agent, "walk", {"location": "月球"}, surface="body")
    assert result["ok"] is False
    for place in world._tool_runtime.point_ids():
        assert place in result["error"]


def test_body_verbs_stay_off_the_chat_and_autonomy_menus(world):
    """**纯加法**:这批动词不进那两个面,所以提示词逐字不变。

    把 `walk` 摆进自主轮次的菜单是另一件事 —— 它会改提示词,而改提示词必须接真模型
    验过再说(这个仓库为此付过学费:位置就是权重,给了菜单不等于她会用)。
    """
    agent = _an_agent(world)
    chat = {v["id"] for v in world.verbs(agent, "chat")}
    autonomy = {v["id"] for v in world.verbs(agent, "autonomy")}
    body = {v["id"] for v in world.verbs(agent, "body")}

    assert body == {"walk", "work", "eat", "sleep", "talk_to", "wander", "seek_company",
                    "interact"}
    assert not (body & chat), f"日常动词漏进了聊天菜单:{body & chat}"
    assert not (body & autonomy), f"日常动词漏进了自主菜单:{body & autonomy}"
    # 而它们确实在总目录里
    assert body <= {v["id"] for v in world.verbs(agent)}


def test_the_body_verbs_delegate_and_do_not_reimplement(world):
    """实现必须委托 `emit_action` —— 有第二份实现就会分叉。"""
    agent = _an_agent(world)
    calls: list[tuple[str, dict]] = []
    real = world.scheduler.emit_action

    def spy(agent_obj, descriptor):
        calls.append((descriptor.kind, dict(descriptor.params)))
        return real(agent_obj, descriptor)

    world.scheduler.emit_action = spy
    try:
        world.act(agent, "eat", {}, surface="body")
        world.act(agent, "wander", {}, surface="body")
    finally:
        world.scheduler.emit_action = real
    assert [k for k, _ in calls] == ["eat", "idle_wander"], (
        f"没走 emit_action —— 有人另写了一份实现:{calls}"
    )


def test_talking_to_yourself_or_a_stranger_is_refused(world):
    agent = _an_agent(world)
    assert world.act(agent, "talk_to", {"target": agent}, surface="body")["ok"] is False
    assert world.act(agent, "talk_to", {"target": "查无此人"}, surface="body")["ok"] is False


def test_bt_actions_are_bindings_of_these_verbs(world):
    """`bt_actions` 里每一行的 `kind`,现在都必须是注册表里的一个动词。

    这是"一张表"的机械证据:如果行为树能做而动词表里没有,割裂就还在。
    """
    store = world.scheduler.bt_store
    assert store is not None, "这个世界没有 bt_store —— 这条测试没在验它想验的"
    kinds = {row["kind"] for row in store.actions()}
    assert kinds, "bt_actions 是空的 —— 同上"
    verbs = {spec.kind for spec in tools_mod.tools_for("*")}
    missing = sorted(kinds - verbs)
    assert not missing, (
        f"行为树能做但动词表里没有:{missing} —— 割裂还在,"
        "同一个人还是两套能力"
    )


# ── 对东西做事:本体声明的能力必须在世界的量上兑现 ────────────────────────


def _tree_height(world: World) -> float:
    return float(world.scheduler.stock_store.of("tree:harbor_oak")["树高"])


def _at_the_tree(world: World, agent: str = "夏") -> str:
    """让她站到那棵树跟前,返回树在哪儿。**问世界要地名,不写死 "cafe"** ——
    橱窗世界哪天把橡树挪到码头去,这几条该跟着走,而不是变红。"""
    where = world.scheduler.visibility_store.place_of("tree:harbor_oak")
    _bring_to(world, agent, where)
    return where


def test_照料真的让树长高(world):
    """这一条是整个本体层的收口。

    在这之前她的提示词里写着"可以照料",而**没有任何路径让她照料** —— 那正是
    `tools/base.py` 开头那句"声明了却没人兑现的能力,比没有更坏"。判据只有一个:
    世界的量变没变。

    先把她挪到树跟前:在场是这个动词的前提(隔壁那条测试验的就是它),而她此刻站在
    哪、有没有在赶路,取决于这个世界几点开门(`world.start_time`)——不是这条要验的事。
    `before` 因此必须在挪完之后取:生长规律每 tick 都在长树,挪之前取就把那几格算进来了。
    """
    _at_the_tree(world)
    before = _tree_height(world)
    result = world.act("夏", "interact", {"target": "tree:harbor_oak", "verb": "tend"},
                       surface="body")

    assert result["ok"] is True, result
    assert _tree_height(world) > before, "她照料了,树一动没动 —— 这就是说得出做不到"
    assert result["detail"]["changed"]["树高"] == _tree_height(world)


def test_她读到的动词就是她调得动的动词(world):
    """提示词里是"照料",引擎里是 `tend`。**两边必须对得上** —— 她照着提示词说的话
    被引擎回一句"不认识这个动词",是这一层最容易埋进去的谎。

    她得真的站在树跟前:这条要验的是**动词表对不对得上**,而不是在场那道闸 —— 不挪
    过去的话,一句"她在赶路"就会把这条测试变成绿的另一种谎(它不再验动词表了)。
    """
    where = _at_the_tree(world)
    line = world._perceive("夏", where).describe_here("tree:harbor_oak")
    assert "照料" in line
    assert "tree:harbor_oak" in line, "没给她 id,她说得出却指不着"

    result = world.act("夏", "interact", {"target": "tree:harbor_oak", "verb": "照料"},
                       surface="body")
    assert result["ok"] is True, result


def test_隔着半个地图照料不到(world):
    """在场语义由引擎守住,和 `walk` 拒绝编造的地名同一条规矩:世界的量会真的变,
    而没有一行日志说这不对劲。"""
    before = _tree_height(world)
    result = world.act("遥", "interact", {"target": "tree:harbor_oak", "verb": "tend"},
                       surface="body")

    assert result["ok"] is False
    assert "不在你这儿" in result["error"]
    # 两头都要说出来:只说"它在 cafe"会读成一句谎
    assert "cafe" in result["error"] and "workshop" in result["error"]
    assert _tree_height(world) == before


def test_做不到的事当场拒绝而不是静默成功(world):
    for params, fragment in (
        ({"target": "tree:nope", "verb": "tend"}, "这儿没有"),
        ({"target": "tree:harbor_oak", "verb": "enter"}, "不能被"),
    ):
        result = world.act("夏", "interact", params, surface="body")
        assert result["ok"] is False and fragment in result["error"]


def test_交互进世界的历史(world):
    """她对世界做的事是**世界里发生的事**,所以它是事件 —— 不是一行日志。

    事件上那个 `loc` 要的是"这事发生在哪儿",所以拿她真正站的地方去对,而不是抄一个
    "橡树在 cafe"的常数:两者只在她恰好在树跟前时才相等,而那正是这条要连带钉住的。
    """
    where = _at_the_tree(world)
    world.act("夏", "interact", {"target": "tree:harbor_oak", "verb": "tend"}, surface="body")
    events = [e for e in world.history(limit=999)["events"] if e["type"] == "entity_interaction"]
    assert events and events[-1]["who"] == "夏"
    assert events[-1]["loc"] == where
    assert events[-1]["payload"]["target"] == "tree:harbor_oak"
