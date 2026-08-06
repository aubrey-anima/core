"""内置世界是**橱窗**:装上包、开箱,就该看见这个引擎能干的事。

这一整个文件盯的是一件事:**新特性不许做完就藏起来**。1.3.0 加了 stance / 能力 /
意图分派 / 定时轮次一大堆东西,而内置种子此前连 config 字段都不支持 —— 于是
`anima-world start` 看到的还是 1.0 那个"只会走路说话"的世界,新用户完全看不到
这几版的成果。做了等于没做。

所以这些断言是**产品断言**,不是引擎断言(引擎默认值全关,那部分在 conftest 的
素配种子上验)。橱窗里少一件展品,这里就该红。
"""
from __future__ import annotations

from _worldfile import bundled_seed, open_world_at

import json
from importlib import resources

import pytest

from anima_world.api import World
from anima_world.world_seed import is_valid_world_seed


def _bundled_seed() -> dict:
    return bundled_seed()


@pytest.fixture
def flagship(tmp_path):
    """开箱世界:不指定种子 = 用内置的那一份。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    yield world
    world.close()


def test_the_bundled_seed_is_still_a_valid_seed():
    """富化不能把它写成一份非法种子 —— 那会让开箱直接降级到硬编码默认世界。"""
    assert is_valid_world_seed(_bundled_seed())


def test_out_of_the_box_the_showcase_features_are_lit(flagship):
    """开箱即见:这些开关由种子点亮(引擎默认值仍然是关的)。"""
    for flag in (
        "needs.enabled",         # 会累、会饿、会去睡
        "economy.enabled",       # 有钱、有货架、发工资
        "social.enabled",        # 八卦与小团体
        "chat.stance.enabled",   # 她说话背后有关系性意图
        "chat.tools.enabled",    # 她能走开 / 静音 / 拒谈
        "chat.intent.enabled",   # 导演场景 / 改对话规则
        "autonomy.enabled",      # 没人说话时她也会主动
    ):
        assert flagship.config_get(flag) is True, f"橱窗里 {flag} 没点亮"


def test_the_loop_stays_off_in_the_showcase(flagship):
    """连续输出**不**默认开:它把每轮的 LLM 调用乘 2~5 倍,而且不是每个世界都要
    这种节奏。橱窗要摆得满,但不能替用户做一个持续烧钱的决定。"""
    assert flagship.config_get("chat.loop.enabled") is False


def test_the_characters_already_know_each_other(flagship):
    """关系不从零开始 —— 否则社交/八卦/聊天 grounding 开箱全是空的。"""
    relations = flagship.scheduler._memory_projection.relations
    assert len(relations) >= 6, "三个人两两互相认识 = 6 个方向"
    for (a, b), rel in relations.items():
        assert rel.r_type, f"{a}→{b} 没有关系描述"
        assert rel.sentiment != 0, f"{a}→{b} 的好感是 0,等于没播"


def test_everyone_has_a_past_to_recall(flagship):
    """记忆是这个引擎的招牌能力,开箱就得有东西可召回。"""
    for agent_id in flagship.scheduler.agents:
        memories = flagship.memories(agent_id)
        assert memories, f"{agent_id} 一条记忆都没有"
        assert any(m.get("anchor") for m in memories), (
            f"{agent_id} 没有一条锚定记忆 —— 那是「她是谁」的底,不该被遗忘曲线冲掉"
        )


def test_the_material_world_is_furnished(flagship):
    """经济点亮了却没东西可买,等于点了个空开关。"""
    shelf = {row["item_id"]: row for row in flagship.shop("cafe")}
    assert "coffee" in shelf, "咖啡店里没有咖啡"
    assert shelf["coffee"]["quantity"] > 0
    for agent_id in flagship.scheduler.agents:
        assert flagship.balance(agent_id) > 0, f"{agent_id} 身上一分钱都没有"
        assert flagship.inventory(agent_id), f"{agent_id} 什么都没带"


def test_everyone_wants_something(flagship):
    """目标进 planner 的 prompt —— 没有目标的角色只会闲逛。"""
    for agent_id, brain in flagship.scheduler.agents.items():
        assert brain.agent.blackboard.read("goals"), f"{agent_id} 没有目标"


def test_doing_something_costs_her_something(flagship):
    """**一个总能成功的动作产生不了任何决策。**

    橱窗里那棵老橡树原本是个"谁按谁灵"的按钮:照料一次长 5 厘米,不累、不限次、
    谁来做都一样。于是这个世界里没有任何理由挑先做哪件事、没有理由歇一会儿、
    也没有理由变强 —— 而丰富的决策全是从"做不到"里长出来的。

    这条把那一整条链演一遍:她有体力 → 照料要花体力 → 花光了当场做不了 →
    过一天缓过来。断在任何一环,橱窗就退回那个按钮。
    """
    agent_id = next(iter(flagship.scheduler.agents))
    assert flagship.stocks(f"agent:{agent_id}")["体力"] == 100.0

    tended = 0
    while True:
        result = flagship.scheduler.perform_affordance(agent_id, "tree:harbor_oak", "tend")
        if not result["ok"]:
            break
        tended += 1
        assert tended < 50, "她怎么都累不倒 —— 代价没落到她身上"
    assert tended > 1, "她一次都没干成,橱窗展示的是一堵墙不是一个世界"
    # 而"做不了"和"这会儿不行"必须分得开:她该去歇着,不是去等这棵树。
    assert result["reason"] == "incapable", result

    # 她自己知道这件事 —— 不知道的话,她会在一个"以为自己精神得很"的世界里做决定。
    assert flagship.perception(agent_id)["own"]["体力"] < 100.0

    flagship.tick(288)   # 一个世界日
    assert flagship.stocks(f"agent:{agent_id}")["体力"] == 100.0, "缓不过来 = 只能干一天活"


def test_a_no_key_world_still_boots_clean(flagship):
    """开箱**没有 key** 是默认状态,而橱窗点亮了一堆要 LLM 的开关。

    这条钉的是:那些开关在 Mock 上一律降级成无害的空操作,世界照跑 —— 丰富度不能
    以"没配 key 就开不了机"为代价。
    """
    flagship.tick(5)
    assert flagship.world_time().minute_of_day >= 0
    # 定时轮次问过了、也没做什么(Mock 给不出能力标记),而且没有崩
    stats = flagship.autonomy_stats()
    assert stats["failed"] == 0 or stats["acted"] == 0


def test_一件要花一小时的事橱窗里演得出来(flagship):
    """**做了却开箱看不见,等于没做。**

    时间是这个引擎里唯一封得住"她一天能干多少事"的代价 —— 量能睡回来、材料能买
    回来,而一段时间过不去就是过不去。橱窗里那棵老橡树因此有一件要花一小时的事:
    嫁接(自造的动词,12 tick = 一小时),做完把这棵树的上限抬高两米。

    这条演的是整条链:起头 → 代价当场付而树一点没变 → 这期间她腾不出手 →
    到点了效果才落。断在任何一环,`duration` 这一层在橱窗里就等于不存在。
    """
    agent_id = "夏"                      # 剪子在她手上(遥 没有,那是 §2.9.6.1 的演出)
    tree = "tree:harbor_oak"
    flagship.tick(24)                    # 让树先长过 `when: 树高 >= 2` 那道门
    ceiling = flagship.stocks(tree)["最大树高"]
    stamina = flagship.stocks(f"agent:{agent_id}")["体力"]

    started = flagship.scheduler.perform_affordance(agent_id, tree, "嫁接")
    assert started["ok"] and started["started"] is True, started
    assert started["duration"] == 12
    assert flagship.stocks(f"agent:{agent_id}")["体力"] == stamina - 25, "代价该当场付"
    assert flagship.stocks(tree)["最大树高"] == ceiling, "一小时还没过,树不该已经变了"

    # 这期间她腾不出手 —— 而且理由自成一类(不是"这会儿不行",也不是"她做不了")。
    busy = flagship.scheduler.perform_affordance(agent_id, tree, "tend")
    assert not busy["ok"] and busy["reason"] == "busy", busy

    engaged = flagship.engagements(agent_id)
    assert engaged and engaged[0]["verb"] == "嫁接" and engaged[0]["occupies"]

    flagship.tick(12)
    assert flagship.stocks(tree)["最大树高"] == ceiling + 2, "到点了效果该落"
    assert not flagship.engagements(agent_id)
    assert flagship.scheduler.perform_affordance(agent_id, tree, "tend")["ok"], "手腾出来了"


def test_橱窗里的世界自己长得出新东西也抹得掉(flagship):
    """**一个不能长出新东西的世界是个西洋镜,不是世界。**

    橱窗里那棵老橡树因此能育苗:花两小时(24 tick)、一包肥、二十点体力,长出一株
    树苗——一个创世时**不存在**的实体。而树苗能被拔掉,因为代价只封得住"多久生
    一个",封不住"世界里一共有多少个":会生的东西都得会死,否则每个世界最后都挤爆,
    而且漏得很慢、很安静。

    这条演的是整条链:够不够格 → 代价当场付 → 到点了才生下来 → 生下来的东西真的
    在世界里(有量、在场、能被交互、经得起自检)→ 也真的抹得掉。
    """
    agent_id = "夏"                     # 剪子和肥都在她手上
    tree = "tree:harbor_oak"
    assert flagship.entities("sapling") == [], "创世时世界里还没有树苗"

    started = flagship.scheduler.perform_affordance(agent_id, tree, "育苗")
    assert started["ok"] and started["started"] is True, started
    assert started["consumed"] == {"fertilizer": 1}
    assert flagship.entities("sapling") == [], "两小时还没过,苗不该已经在那儿"

    flagship.tick(24)
    saplings = flagship.entities("sapling")
    assert len(saplings) == 1, "到点了苗该长出来"
    sapling = saplings[0]
    assert sapling["name"] == "新育的树苗"
    assert sapling["location"] == flagship.entities("tree")[0]["location"], \
        "没写 location 就生在这件事发生的地方"
    assert sapling["values"] == {"苗高": 0.2}, "声明过的量要逐个落地"
    assert flagship.check_entity(sapling["id"])[0]["ok"], "生下来就得活得了"

    # 它是世界里一个真的东西:她碰得到,而且能被抹掉。
    assert flagship.scheduler.perform_affordance(agent_id, sapling["id"], "look")["ok"]
    pulled = flagship.scheduler.perform_affordance(agent_id, sapling["id"], "拔掉")
    assert pulled["ok"] and pulled["destroyed"] == sapling["id"]
    assert flagship.entities("sapling") == []
    assert flagship.stocks(sapling["id"]) == {}, "抹掉了量还留着 = 一个不存在的东西还有高度"
