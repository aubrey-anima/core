"""记忆准入闸(R4):阈值只有一个数,而闸开在复读上、不开在日常上。

为什么这一条值得一个测试文件:这道闸**拒了不报错**。它拦掉一条记忆的样子和
"这件事没发生"逐位相同 —— 日志一条不错、记忆表看上去正常、容量也没超。所以它的
每一条校准都必须有一条测试钉着,尤其是**阈值**:

- 阈值有过**两份真相**:算分那侧(`memory_admission.DEFAULT_THRESHOLD`)校准成
  0.20、文档写着 0.20,而 `config_store._DEFAULTS` 里抄着一份 0.35 —— 而
  `ConfigStore.get` 的 `default=` 只在键**没声明过**时才轮得到,于是调用方写的
  `default=DEFAULT_THRESHOLD` 永远不生效,真正生效的是那份抄件。
  后果不是算错:一条**全新的** `state_change` 满分就是 0.35,窗口里有一条同类就
  掉到 0.333 —— **照文档开闸的世界静默丢掉正常记忆。**
"""
from __future__ import annotations

import re
from pathlib import Path

from anima_world import memory_admission
from anima_world.config_store import _DEFAULTS

REFERENCE = Path(__file__).resolve().parent.parent / "docs" / "REFERENCE.md"


def test_the_threshold_is_one_number_in_all_three_places():
    """算分那侧、配置声明、REFERENCE —— 三处必须是同一个数。

    ⚠️ **只对着模块常量断言是不够的**:出问题的那一版里模块常量是对的,坏的是
    配置声明里那份抄件,而它才是 `ConfigStore.get` 真正返回的东西。
    """
    declared = _DEFAULTS["memory.admission.threshold"][0]
    assert declared == memory_admission.DEFAULT_THRESHOLD, (
        "配置声明里的默认值必须就是算分那一侧的那个数 —— 抄一份过去就是给"
        "「两份真相、生效的是坏的那份」留位置"
    )
    row = next(
        line for line in REFERENCE.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `memory.admission.threshold`")
    )
    documented = re.match(r"\|[^|]*\|[^|]*\|\s*([0-9.]+)\s*\|", row)
    assert documented, f"REFERENCE 那一行读不出默认值:{row}"
    assert float(documented.group(1)) == memory_admission.DEFAULT_THRESHOLD, (
        "REFERENCE 写的默认值和代码不一致 —— 宿主照文档开闸,拿到的是另一个行为"
    )


def test_a_brand_new_state_change_gets_in_with_the_gate_open(open_world, bare_seed):
    """开了闸的世界里,一条**全新的** `state_change` 必须收得下。

    这是那份 0.35 抄件的实测复现:`state_change` 的类型先验 0.7、触发器给的
    importance 0.5,于是它的**满分**就是 0.35;窗口里只要有一条同类,近因因子把它
    压到 0.333 —— 一条和已有记忆零重合的新事件被静默拒掉,而这道闸的本意只是挡掉
    第七次「在吗」。**阈值必须坐在"一条普通的、新鲜的低价值事件"之下。**
    """
    world = open_world(world_file=bare_seed)
    scheduler = world.scheduler
    agent = next(iter(scheduler.agents))
    world.config_set("memory.admission.enabled", True)
    scheduler.memory_store.add(agent, tick=10, kind="state_change",
                               summary="她开始照料门口那棵老橡树", importance=0.5)

    assert scheduler._admit_memory(
        agent, "港口的轮渡今天晚点了两个小时", "state_change", 0.5, False
    ) is True, "和已有记忆零重合的新事件不该被拒 —— 闸开在复读上,不开在日常上"


def test_a_repeat_is_still_refused_and_says_why(open_world, bare_seed):
    """闸没被调松:同一句话再来一遍照样拒,而且说得出为什么。"""
    world = open_world(world_file=bare_seed)
    scheduler = world.scheduler
    agent = next(iter(scheduler.agents))
    world.config_set("memory.admission.enabled", True)
    said = "她说她睡下了"
    scheduler.memory_store.add(agent, tick=10, kind="state_change",
                               summary=said, importance=0.5)

    assert scheduler._admit_memory(agent, said, "state_change", 0.5, False) is False
    health = scheduler.subsystem_health()["memory_admission"]
    # 拒了几条要**数得出来**:`state()` 上"开了闸但一条都没拦"和"拦掉了半个世界"
    # 必须分得开(两支写同一行的版本在读数上看不出这道闸拦过没有)。
    assert health["refused"] == 1
    assert "复读" in health["last_refusal"]


def test_an_anchor_is_never_refused(open_world, bare_seed):
    """锚定的永不拒 —— 一道打分闸拦下创世记忆就是把她的出身拦下来。"""
    world = open_world(world_file=bare_seed)
    scheduler = world.scheduler
    agent = next(iter(scheduler.agents))
    world.config_set("memory.admission.enabled", True)
    world.config_set("memory.admission.threshold", 0.99)
    said = "她记得自己是在海边长大的"
    scheduler.memory_store.add(agent, tick=0, kind="seed", summary=said, importance=0.9)

    assert scheduler._admit_memory(agent, said, "seed", 0.9, True) is True
