"""内置世界是**橱窗**:装上包、开箱,就该看见这个引擎能干的事。

这一整个文件盯的是一件事:**新特性不许做完就藏起来**。1.3.0 加了 stance / 能力 /
意图分派 / 定时轮次一大堆东西,而内置种子此前连 config 字段都不支持 —— 于是
`anima-world start` 看到的还是 1.0 那个"只会走路说话"的世界,新用户完全看不到
这几版的成果。做了等于没做。

所以这些断言是**产品断言**,不是引擎断言(引擎默认值全关,那部分在 conftest 的
素配种子上验)。橱窗里少一件展品,这里就该红。
"""
from __future__ import annotations

import json
from importlib import resources

import pytest

from anima_world.api import World
from anima_world.world_seed import is_valid_world_seed


def _bundled_seed() -> dict:
    return json.loads(
        (resources.files("anima_world") / "world_seed.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def flagship(tmp_path):
    """开箱世界:不指定种子 = 用内置的那一份。"""
    world = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
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
