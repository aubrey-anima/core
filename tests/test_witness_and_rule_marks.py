"""两件事:**发生过的事得有人经历它**,以及**规律的节流水位得落库**。

## 一、事件发生了,得有人经历它(witness-memory)

规律 `emit` 出来的事件此前只进日志。线上那个雨季世界的高潮「江水漫堤」发生了 ——
事件在,`who` / `loc` 都是 null —— 而四个角色关于它的记忆是 **0 条**。江晚当时就
站在堤上,水从她脚边漫过第二级台阶,她永远不会记得这件事,永远不会在深夜档里
提起它。**世界的历史和角色的经历是两个不相交的集合。**

作者其实知道这个洞:那个世界的节拍脚本里用 `broadcast_memory` + 逐人手写 `memory`
拼出开场广播 —— 作者在手工搭这座桥,而这座桥该是引擎的。

这一层的形状和 perception / ontology 逐字同构:**声明本身就是开关**。作者在
`emit` 上写下 `importance`,才有人记得住;不写的世界行为逐位相同(这条有测试)。

## 二、规律的节流水位落库(一次生产事故的修)

`stocks.evaluate_due` 的 docstring 说得很自信:水位"重启清空是安全的,最多多算
一次,**值不会错** —— 结果由 dt 决定"。**那句话只对 dt 化的表达式成立。**

线上那个世界的「梅雨」是常数步长(`雨天数 + 1`、`江水位 + 雨势*0.9 - 0.15`),
调试期滚动重启频繁,于是**每次重启多烧一整天的雨**:实测雨天数 56 而按时钟应为
~50;洪水本该第 3.2 天、实际发生在第 1 天(雨天数一天内 31→35);此后水位顶死
clamp 上限 17 个世界日 —— 那个世界唯一的叙事高潮被烧在了没人看的第一天,而**日志
一条错都没有**。这是本仓库最怕的"照跑但给错东西"。

水位现在住 `:meta` 的 `rule_marks` 行。**回归测试不许用固定 tick 数假绿**:下面
那条先在一个进程里把日频规律烧过一次,再换一个 `World.open` 重来,断言那个量
**一个数都没动** —— 值是判据,tick 数只是布景。
"""
from __future__ import annotations

import pytest

from _worldfile import write_seed_file


# ── 夹具:一个只有必需品的小世界 ───────────────────────────────────────────


def _seed(tmp_path, name: str, **sections) -> str:
    """两个地点、两个角色,加上调用方要的 stocks / rules / kinds / entities。

    不从内置橱窗派生:这几条测试问的是规律引擎与记忆之间那条线,橱窗那棵中文名的
    橡树、它的本体声明和八条配置只会给它添噪音(而且橱窗一变这里就莫名其妙地红)。
    """
    seed = {
        "locations": [
            {"id": "cafe", "name": "咖啡店", "description": "拐角那家"},
            {"id": "workshop", "name": "作坊", "description": "河对岸"},
        ],
        "agents": [
            {"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"},
            {"id": "乙", "name": "乙", "location": "workshop", "personality": "话多"},
        ],
        **sections,
    }
    return write_seed_file(tmp_path / name, seed)


def _witness_memories(world, agent_id: str) -> list[dict]:
    return [
        row for row in world.memories(agent_id)
        if row.get("kind") == "witness"
    ]


def _meta_row(redis, world_id: str, field: str):
    from anima_world.redis_state import meta_rows

    return meta_rows(redis, world_id).get(field)


# 江水位每 tick 涨 1,涨到 2 的那一刻漫堤。**边沿触发**,所以只发一次。
# `importance` / `text` 是作者在 `emit` 上的声明(生产端在 `rules.py`/`stocks.py`)。
FLOOD = {
    "id": "涨水",
    "every": {"ticks": 1},
    "for_each": {"owner": "world"},
    "set": {"江水位": "江水位 + 1"},
    "emit": [{
        "when": "江水位 >= 2",
        "type": "江水漫堤",
        "importance": 0.9,
        "text": "江水漫过了第二级台阶",
    }],
}
RIVER = {"owner": "world", "values": {"江水位": 0.0}}


# ── 一、在场的人记得住 ────────────────────────────────────────────────────


def test_a_world_wide_event_becomes_everyones_memory(tmp_path, open_world):
    """江水漫堤发生了 —— **名册里每个人都记得**,而且检索得回来。

    走的是真路径:`World.open` + `tick()`,不是直接戳 scheduler 的内部函数。
    (本仓库吃过这个亏:测试全走 `perform_affordance`,于是"创世投影折两遍"那个
    bug 谁都没看见。)
    """
    path = _seed(tmp_path, "flood.cyberworld", stocks=[RIVER], rules=[FLOOD])
    world = open_world(world_file=path)

    world.tick(3)

    assert any(e["type"] == "江水漫堤" for e in world.events()), "规律根本没发事件"
    for agent_id in ("甲", "乙"):
        remembered = _witness_memories(world, agent_id)
        assert [row["summary"] for row in remembered] == ["江水漫过了第二级台阶"], (
            f"{agent_id} 不记得江水漫堤 —— 世界的历史和她的经历又成了两个集合"
        )
        assert remembered[0]["importance"] == pytest.approx(0.9)
        # 记得住不等于想得起来:检索是她真正会用到的那条路。
        recalled = world.retrieve_memories(agent_id, "江水", k=5)
        assert any("江水漫过" in row["summary"] for row in recalled), (
            f"{agent_id} 记下了却检索不回来 —— 那和没记一样"
        )


def test_only_the_people_standing_there_witness_a_thing(tmp_path, open_world):
    """挂在某样**东西**身上的事,只有此刻和它同处一地的人看得见。

    `owner == "world"` 才是全世界的事。一棵树的事只有站在那棵树旁边的人看得见 ——
    否则一个世界里所有人都会记得每一棵树的每一次抽芽。
    """
    seed_args = {
        "stocks": [{"owner": "tree:老槐", "values": {"树高": 1.0}}],
        "stock_places": [{"owner": "tree:老槐", "location": "cafe", "label": "老槐"}],
        "rules": [{
            "id": "长高",
            "every": {"ticks": 1},
            "for_each": {"kind": "tree"},
            "set": {"树高": "树高 + 1"},
            "emit": [{
                "when": "树高 >= 2",
                "type": "老槐开花",
                "importance": 0.6,
                "text": "门口那棵老槐一夜之间开了花",
            }],
        }],
    }
    path = _seed(tmp_path, "tree.cyberworld", **seed_args)
    world = open_world(world_file=path)

    world.tick(3)

    # 前提自证:这条测试的意思全挂在"他俩不在同一个地方"上。谁要是溜达走了,
    # 断言就该当场翻脸,而不是安静地验一件别的事。
    boards = {aid: brain.agent.blackboard.read("loc") for aid, brain in world.scheduler.agents.items()}
    assert boards == {"甲": "cafe", "乙": "workshop"}, boards

    assert [row["summary"] for row in _witness_memories(world, "甲")] == [
        "门口那棵老槐一夜之间开了花"
    ]
    assert _witness_memories(world, "乙") == [], (
        "乙在河对岸的作坊里,却记得咖啡店门口那棵树开了花"
    )


def test_a_thing_that_is_nowhere_has_no_witnesses_and_the_clock_keeps_running(
    tmp_path, open_world
):
    """位置查不到 = **没有见证者**,不是"所有人"。而且绝不许掀翻 tick。

    猜成"所有人"的话,一棵不知道在哪的树倒了,全世界都记得它 —— 而作者永远
    不会知道自己漏写了一行 `stock_places`。
    """
    path = _seed(
        tmp_path, "nowhere.cyberworld",
        stocks=[{"owner": "rock:无名", "values": {"裂缝": 0.0}}],
        rules=[{
            "id": "开裂",
            "every": {"ticks": 1},
            "for_each": {"kind": "rock"},
            "set": {"裂缝": "裂缝 + 1"},
            "emit": [{
                "when": "裂缝 >= 2",
                "type": "巨石开裂",
                "importance": 0.8,
                "text": "巨石裂了一道缝",
            }],
        }],
    )
    world = open_world(world_file=path)

    world.tick(4)

    assert world.scheduler.clock == 4, "见证者算不出来把世界的钟也停了"
    assert any(e["type"] == "巨石开裂" for e in world.events()), "事件本身还是该发"
    assert _witness_memories(world, "甲") == []
    assert _witness_memories(world, "乙") == []


def test_a_world_that_never_declares_importance_behaves_exactly_as_before(
    tmp_path, open_world
):
    """**声明本身就是开关。** 不写 `importance` 的世界,这一层整个缺席。

    世界的量每天有几十万次变化 —— 默认让每一次门槛跨越都变成全员的记忆,等于用
    世界的物理法则把她的记忆冲刷干净。
    """
    quiet = {**FLOOD, "emit": [{"when": "江水位 >= 2", "type": "江水漫堤"}]}
    path = _seed(tmp_path, "quiet.cyberworld", stocks=[RIVER], rules=[quiet])
    world = open_world(world_file=path)

    before = {aid: len(world.memories(aid)) for aid in ("甲", "乙")}
    world.tick(3)

    assert any(e["type"] == "江水漫堤" for e in world.events()), "规律没发事件,这条测试是空的"
    for agent_id in ("甲", "乙"):
        assert _witness_memories(world, agent_id) == []
        assert len(world.memories(agent_id)) == before[agent_id], (
            "没声明 importance 的世界凭空多了记忆 —— 行为不再逐位相同"
        )


def test_importance_zero_is_still_nobodys_memory(tmp_path, open_world):
    """`importance: 0` 是"这件事不值得记住",不是"记一条不重要的"。

    这条横跨两层(声明那一层不往下发,消费这一层也不收)—— **有意的重复**:
    判据落在哪一层将来可能会变,而"作者写了 0 就一个人都不记得"这句话不该变。
    """
    numb = {**FLOOD, "emit": [{
        "when": "江水位 >= 2", "type": "江水漫堤",
        "importance": 0, "text": "水涨了一点",
    }]}
    path = _seed(tmp_path, "numb.cyberworld", stocks=[RIVER], rules=[numb])
    world = open_world(world_file=path)

    world.tick(3)
    assert _witness_memories(world, "甲") == []


def test_a_falling_edge_is_witnessed_exactly_like_a_rising_one(tmp_path, open_world):
    """退潮也是一件发生过的事。**消费端不认识方向** —— `edge` 只是 payload 上的
    一个字,而"谁在场"和"她该多当回事"跟涨落没关系。"""
    ebb = {
        "id": "退水",
        "every": {"ticks": 1},
        "for_each": {"owner": "world"},
        "set": {"江水位": "max(江水位 - 1, 0)"},
        "emit": [{
            "when": "江水位 >= 2", "type": "江水退去", "on": "fall",
            "importance": 0.7, "text": "水退回了堤下,台阶上留着一圈泥",
        }],
    }
    path = _seed(tmp_path, "ebb.cyberworld",
                 stocks=[{"owner": "world", "values": {"江水位": 3.0}}], rules=[ebb])
    world = open_world(world_file=path)

    world.tick(3)

    fired = [e for e in world.events() if e["type"] == "江水退去"]
    assert len(fired) == 1, fired
    assert fired[0]["payload"]["edge"] == "fall"
    assert [row["summary"] for row in _witness_memories(world, "甲")] == [
        "水退回了堤下,台阶上留着一圈泥"
    ]


def test_the_consumer_reads_the_payload_and_survives_a_missing_text(
    tmp_path, open_world
):
    """消费端这一侧的契约,单独钉一遍:`importance` / `text` / `owner` 从 payload 上读。

    生产端今天保证 `text` 一定跟着 `importance` 出现,所以这条走的是**回落**那一支:
    没有那句话时,她记住的是事件的名字。`江水漫堤` 也是一句真话,一条空记忆不是 ——
    而一条空记忆检索得回来、也进得了提示词,只是什么都没说。
    """
    path = _seed(tmp_path, "raw.cyberworld")
    world = open_world(world_file=path)

    world.scheduler._emit_rule_event({
        "type": "江水漫堤", "who": None,
        "payload": {"rule": "涨水", "owner": "world", "edge": "rise", "importance": 0.9},
    })

    assert [row["summary"] for row in _witness_memories(world, "甲")] == ["江水漫堤"]


def test_reopening_the_world_does_not_write_the_memory_a_second_time(
    tmp_path, open_world
):
    """**重放不许重复写。** 记忆是持久状态,重开一次不该让她把那件事记两遍。

    `open_world` 的两次调用连的是同一个 fakeredis、同一个 world_id —— 那就是
    "换一个进程重开同一个世界"在这套夹具里的写法。

    走的是现成的 `memory_seed` 那条路,所以这条性质是继承来的
    (`MemoryStore.rebuild`:有行就一动不动)—— 但继承来的性质也要有人钉住,
    否则哪天换一条写记忆的路,谁都不会想起来验它。
    """
    path = _seed(tmp_path, "twice.cyberworld", stocks=[RIVER], rules=[FLOOD])
    world = open_world("twice", world_file=path)
    world.tick(3)
    assert len(_witness_memories(world, "甲")) == 1
    world.close()

    again = open_world("twice")
    assert len(_witness_memories(again, "甲")) == 1, (
        "重开一次,她把江水漫堤记了两遍"
    )


def test_witness_memories_are_evicted_like_every_other_memory(tmp_path, open_world):
    """有界性:见证记忆走的是同一个容量淘汰,所以它不破坏"进提示词的必须有界"。

    直接喂事件而不是等规律发 —— 门槛事件是**边沿触发**的,要拿真规律凑够两百次
    跨越,得把这个世界跑成另一副样子(而那时验的就是别的东西了)。这里问的是
    "写进去的东西受不受容量管",喂的是 `_emit_rule_event` —— 规律引擎走的那个口子。
    """
    path = _seed(tmp_path, "many.cyberworld")
    world = open_world(world_file=path)
    capacity = int(world.config_get("memory.capacity", 50))

    for index in range(capacity * 4):
        world.scheduler._emit_rule_event({
            "type": "水位变化",
            "who": None,
            "payload": {"rule": "涨水", "owner": "world",
                        "importance": 0.5, "text": f"水位到了第 {index} 级"},
        })

    for agent_id in ("甲", "乙"):
        assert len(world.memories(agent_id)) <= capacity, (
            "见证记忆绕过了容量淘汰 —— 它会随世界变老把提示词撑爆"
        )
    assert _witness_memories(world, "甲"), "一条都没落地,这条测试是空的"


# ── 二、规律的节流水位落库 ────────────────────────────────────────────────


# 日频、常数步长 —— 正是线上那条「梅雨」的形状。dt 化的表达式看不出这个 bug。
RAIN = {
    "id": "梅雨",
    "every": {"days": 1},
    "for_each": {"owner": "world"},
    "set": {"雨天数": "雨天数 + 1"},
}
SKY = {"owner": "world", "values": {"雨天数": 0.0}}


def test_a_restart_does_not_burn_another_day_of_rain(tmp_path, open_world, fresh_redis):
    """**这条是那次生产事故的回归测试。**

    一条日频的常数步长规律,在一个进程里烧过一次;换一个 `World.open`(= 换一个
    进程)再跑几个 tick —— `雨天数` 必须**一个数都没动**。

    判据是**值**,不是 tick 数:世界逐次不确定,拿固定 tick 数去数"跑了几次"会
    假绿。而这个量只有一条规律写得到它,所以它就是账本本身。
    """
    path = _seed(tmp_path, "rain.cyberworld", stocks=[SKY], rules=[RAIN])

    world = open_world("rain", world_file=path)
    world.tick(1)
    assert world.stock("world", "雨天数") == pytest.approx(1.0), "第一次开机就没烧"
    world.tick(5)
    assert world.stock("world", "雨天数") == pytest.approx(1.0), "288 tick 的节流在同一个进程里就漏了"
    saved = _meta_row(fresh_redis, "rain", "rule_marks")
    assert saved and saved["marks"] == {"梅雨": 1}, f"水位没落库:{saved!r}"
    world.close()

    restarted = open_world("rain")
    restarted.tick(5)
    assert restarted.stock("world", "雨天数") == pytest.approx(1.0), (
        "重启一次多烧了一天的雨 —— 世界的物理法则挂在了运维重启了几次上"
    )


def test_a_world_that_never_ran_the_rule_starts_from_scratch_without_catching_up(
    tmp_path, open_world, fresh_redis
):
    """**升级路径**:一个没有这一行的老世界,第一次开机既不炸,也不补烧。

    补烧才是把这个 bug 反着犯一遍 —— 停机三天回来一次性下三天的雨,同样是
    "日志干净、值是错的"。
    """
    path = _seed(tmp_path, "old.cyberworld", stocks=[SKY], rules=[RAIN])
    world = open_world("old", world_file=path)
    world.tick(600)     # 两个世界日多一点:自然烧过 3 次
    burned = world.stock("world", "雨天数")
    assert burned == pytest.approx(3.0), burned
    world.close()

    # 老世界的样子:水位那一行压根不存在。
    from anima_world.redis_state import meta_rows

    meta_rows(fresh_redis, "old").drop("rule_marks")
    upgraded = open_world("old")
    upgraded.tick(1)
    assert upgraded.stock("world", "雨天数") == pytest.approx(4.0), (
        "没有水位的老世界该照今天的样子跑一次,不多不少"
    )
    upgraded.tick(5)
    assert upgraded.stock("world", "雨天数") == pytest.approx(4.0), "跑完一次之后水位就该管住它"


def test_a_watermark_from_the_future_is_ignored(tmp_path, open_world, fresh_redis):
    """水位比世界时钟还新(世界被导进新前缀 / 钟被人挪过)—— 当作没有水位。

    留着它的下场是这几条规律一直等到时钟追上来,而那是一段**没人看得见的静默**:
    日志干净、规律不算、量不动。
    """
    path = _seed(tmp_path, "future.cyberworld", stocks=[SKY], rules=[RAIN])
    world = open_world("future", world_file=path)
    world.tick(1)
    world.close()

    from anima_world.redis_state import meta_rows

    meta_rows(fresh_redis, "future").put(
        "rule_marks", {"marks": {"梅雨": 999_999}, "updated_tick": 999_999}
    )
    reopened = open_world("future")
    reopened.tick(1)
    assert reopened.stock("world", "雨天数") == pytest.approx(2.0), (
        "一条来自未来的水位把这条规律永远关在了门外"
    )


def test_a_per_tick_rule_leaves_no_watermark_at_all(tmp_path, open_world, fresh_redis):
    """`every` 是 1 的规律没有节流可丢 —— 给它记水位,是每 tick 一次 Redis 写去
    记一件不影响任何判断的事。"""
    path = _seed(tmp_path, "everytick.cyberworld", stocks=[SKY],
                 rules=[{**RAIN, "every": {"ticks": 1}}])
    world = open_world("everytick", world_file=path)
    world.tick(10)

    assert world.stock("world", "雨天数") == pytest.approx(10.0), "每 tick 的规律没每 tick 算"
    assert _meta_row(fresh_redis, "everytick", "rule_marks") is None, (
        "给一条每 tick 都算的规律记了水位"
    )


def test_the_watermark_is_written_only_when_it_moves(tmp_path, open_world, fresh_redis):
    """写回要节制:一轮里没有任何一条节流规律到点,就不该碰 Redis。"""
    path = _seed(tmp_path, "quietwrite.cyberworld", stocks=[SKY], rules=[RAIN])
    world = open_world("quietwrite", world_file=path)
    world.tick(1)

    writes: list[str] = []
    inner = world.scheduler.meta_store

    class _Counting:
        def get(self, field):
            return inner.get(field)

        def put(self, field, row):
            writes.append(field)
            return inner.put(field, row)

    world.scheduler.meta_store = _Counting()
    world.tick(20)
    assert "rule_marks" not in writes, (
        f"水位没动,却写了 {writes.count('rule_marks')} 次 Redis"
    )
