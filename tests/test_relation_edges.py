"""关系图的边:什么该冻住,什么该撤销。

两件事长得很像,判断相反:

- `(subject, predicate, object)` 重复写**不覆盖**(INSERT OR IGNORE),于是
  `source_event_seq` 冻在第一次 —— **这是设计**。边记的是"这两人是朋友"这个
  *事实*,不是"他们又亲近了一次"这个*事件*。关系在「亲近」「挚交」之间来回
  跨档时事实没变;重写出处只会让"这条边是哪件事立起来的"每次都换个答案。
  值本身的变化在 relations 那一层,边不是它的第二份拷贝。
- 边**只增不减**是缺陷,而且正是被上面那条挡住看不见的那个:一对从「亲近」
  跌进「交恶」的人身上会同时挂着 friendship 与 rivalry,永远。
  `cliques.compute_cliques` 只看 friendship,于是那个小团体里坐着两个此刻互相
  看不顺眼的人 —— 算得出来、画得出来、一条日志都不报错。
"""
from __future__ import annotations

import pytest

from anima_world.memory_store import MemoryDescriptor


@pytest.fixture
def graph():
    import fakeredis

    from anima_world.redis_state import RedisKnowledgeGraph

    return RedisKnowledgeGraph(fakeredis.FakeStrictRedis(decode_responses=True), "t")


def test_repeated_add_keeps_the_first_provenance(graph):
    """同一条边写两次,出处冻在第一次 —— 设计,不是缺陷。"""
    graph.add("agent:夏", "friendship", "agent:遥", source_event_seq=713, created_at=100)
    graph.add("agent:夏", "friendship", "agent:遥", source_event_seq=1360, created_at=3264)

    rows = graph.query(subject="agent:夏")
    assert len(rows) == 1, "同一个三元组只有一条边"
    assert rows[0]["source_event_seq"] == 713, "第一次被证实的那一刻说了算"
    assert rows[0]["created_at"] == 100


def test_drop_reports_whether_the_edge_was_there(graph):
    graph.add("agent:夏", "friendship", "agent:遥")
    assert graph.drop("agent:夏", "friendship", "agent:遥") is True
    assert graph.drop("agent:夏", "friendship", "agent:遥") is False
    assert graph.query(subject="agent:夏") == []


def _shift(scheduler, who, target, value, *, tick, seq):
    """走 `_on_relation_shift` 那条真路,而不是直接调 knowledge_graph。"""
    scheduler._on_relation_shift(
        {"type": "state_change", "who": who,
         "payload": {"kind": "sentiment", "as": who, "target": target, "sentiment": value}},
        MemoryDescriptor(agent_id=who, tick=tick, kind="relation_shift",
                         summary="", importance=0.5, event_seq=seq),
    )


def test_a_reversal_retracts_the_opposite_edge(open_world, bare_seed):
    """反目要撤掉 friendship,否则小团体里坐着两个仇人。"""
    world = open_world(world_file=bare_seed)
    scheduler = world.scheduler
    graph = scheduler.knowledge_graph

    _shift(scheduler, "夏", "遥", 0.6, tick=100, seq=1)
    assert {r["predicate"] for r in graph.query(subject="agent:夏")} == {"friendship"}
    assert graph.query(subject="agent:夏")[0]["created_at"] == 100, (
        "出处得说得出哪一刻 —— 此前默认 0,每条边都自称生于创世"
    )

    _shift(scheduler, "夏", "遥", -0.6, tick=400, seq=2)
    predicates = {r["predicate"] for r in graph.query(subject="agent:夏")}
    assert predicates == {"rivalry"}, f"friendship 必须被撤掉,实得 {predicates}"
    # 反向的那条也要撤 —— 边是成对加的,只撤一半等于留一条单向的假朋友。
    assert {r["predicate"] for r in graph.query(subject="agent:遥")} == {"rivalry"}


def test_a_reversal_takes_them_out_of_the_same_clique(open_world, bare_seed):
    """病本身:撤销之前,`cliques` 会把这对仇人算成一个小团体。"""
    from anima_world.cliques import compute_cliques

    world = open_world(world_file=bare_seed)
    scheduler = world.scheduler

    _shift(scheduler, "夏", "遥", 0.6, tick=100, seq=1)
    assert compute_cliques(scheduler.knowledge_graph.query()), "先得真有一个小团体"

    _shift(scheduler, "夏", "遥", -0.6, tick=400, seq=2)
    assert compute_cliques(scheduler.knowledge_graph.query()) == [], (
        "反目之后不该还在同一个小团体里"
    )


def test_drifting_into_the_neutral_band_does_not_retract(open_world, bare_seed):
    """淡下来不等于反目 —— 撤了的话边会随数字的小幅摆动来回闪。"""
    world = open_world(world_file=bare_seed)
    scheduler = world.scheduler

    _shift(scheduler, "夏", "遥", 0.6, tick=100, seq=1)
    _shift(scheduler, "夏", "遥", 0.0, tick=400, seq=2)

    assert {r["predicate"] for r in scheduler.knowledge_graph.query(subject="agent:夏")} \
        == {"friendship"}


def test_a_retracted_edge_still_answers_for_the_time_it_held(open_world, bare_seed):
    """作废要**说得出哪一刻** —— 否则"他俩正是朋友的那一刻"被答成"从来没有过"。

    这是 R3(有效期)存在的理由本身:一段关系结束了是**又一件发生过的事**。
    唯一的生产调用方(`_on_relation_shift`)一度不传 `at=`,于是 `invalid_at` 落成
    默认的 0 —— 比 `valid_from` 还早,不报错,而 `query(as_of=250)` 返回 `[]`。
    """
    world = open_world(world_file=bare_seed)
    graph = world.scheduler.knowledge_graph

    _shift(world.scheduler, "夏", "遥", 0.6, tick=100, seq=1)
    _shift(world.scheduler, "夏", "遥", -0.6, tick=400, seq=2)

    dead = [r for r in graph.query(subject="agent:夏", include_invalid=True)
            if r["predicate"] == "friendship"]
    assert len(dead) == 1
    assert dead[0]["valid_from"] == 100
    assert dead[0]["invalid_at"] == 400, (
        "作废的时刻必须是反目那一刻 —— 恒为 0 的版本让每条死边都自称"
        "在成立之前就作废了"
    )

    at_the_time = graph.query(subject="agent:夏", as_of=250)
    assert [r["predicate"] for r in at_the_time] == ["friendship"], (
        "那时候他俩就是朋友,这一问必须答得出来"
    )
    assert [r["predicate"] for r in graph.query(subject="agent:夏")] == ["rivalry"], (
        "此刻只剩 rivalry —— 默认视图不许把曾经是朋友当成是朋友"
    )
    assert graph.query(subject="agent:夏", as_of=50) == [], "还没立起来的那一刻是空的"


def test_an_unnamed_moment_never_lands_before_the_edge_stood_up(graph):
    """不知道哪一刻,兜底也得留下一段**读得出来**的有效期 —— 绝不是空区间。

    ⚠️ **这一条的断言改过一次,原因写在这里。** 它原先钉的是
    `invalid_at == valid_from`,而那正是它自己的说明里描述的那个失败:有效区间
    是半开的 `[valid_from, invalid_at)`,零长区间在 `query(as_of=…)` 上**任何
    一刻**都答 `[]` —— 和 `invalid_at=0` 逐位同一个读数,只是把"从来没有过"
    从 0 挪到了 `valid_from`。而"从来没有过"是 `hard=True` 的意思,
    soft drop 的意思是"它成立过、现在不成立了",两件事必须分得开。

    兜底能说的最小的一句真话是:**她至少在成立的那一刻是成立的**
    (`add()` 写下 `valid_from` 就是因为那一刻它立住了)。
    """
    graph.add("agent:夏", "friendship", "agent:遥", created_at=100)
    assert graph.drop("agent:夏", "friendship", "agent:遥") is True

    row = graph.query(subject="agent:夏", include_invalid=True)[0]
    assert row["invalid_at"] > row["valid_from"] == 100, "空区间 = 从来没有过"
    assert graph.query(subject="agent:夏", as_of=100), (
        "她成立的那一刻必须还答得出来 —— 否则这一层和 hard drop 没有区别"
    )
    assert graph.query(subject="agent:夏", as_of=101) == []

    graph.add("agent:夏", "rivalry", "agent:遥", created_at=200)
    # 调用方手里的 tick 不对时也一样往回夹(并且 warning 一声)——
    # 存储层不该安静地写下一个不可能的区间。
    assert graph.drop("agent:夏", "rivalry", "agent:遥", at=5) is True
    row = [r for r in graph.query(subject="agent:夏", include_invalid=True)
           if r["predicate"] == "rivalry"][0]
    assert row["invalid_at"] > 200
    assert [r["predicate"] for r in graph.query(subject="agent:夏", as_of=200)] == ["rivalry"]


def test_a_row_that_came_in_through_another_door_is_read_the_same_way(graph):
    """闸装在 `drop()` 里挡不住别的门 —— 所以**读侧走同一个函数**。

    边行不只经 `drop()` 落盘:装一份世界文件、维护脚本直写 `RedisRows`,
    都能带着一个不可能的区间进来(`invalid_at=0` 就是原来那个 bug 的形状)。
    读的那一侧照单全收的话,"他俩正是朋友的那一刻"仍然答"从来没有过"。
    """
    graph.add("agent:夏", "friendship", "agent:遥", created_at=100)
    field = "agent:夏\x00friendship\x00agent:遥"
    row = graph._rows.get(field)
    row["invalid_at"] = 0                      # 绕过 drop(),直写
    graph._rows.put(field, row)

    assert graph.query(subject="agent:夏") == [], "此刻它确实已经作废了"
    assert graph.query(subject="agent:夏", as_of=100), (
        "它成立过 —— 直写进来的坏区间不该把这件事抹掉"
    )
    assert graph.query(subject="agent:夏", as_of=101) == []


def test_making_up_revives_the_same_edge(graph):
    """绝交之后又和好:**同一条边复活**(id 不变,它是同一件事)。"""
    graph.add("agent:夏", "friendship", "agent:遥", source_event_seq=1, created_at=100)
    first_id = graph.query(subject="agent:夏")[0]["id"]
    graph.drop("agent:夏", "friendship", "agent:遥", at=400)
    assert graph.query(subject="agent:夏") == []

    graph.add("agent:夏", "friendship", "agent:遥", source_event_seq=9, created_at=900)
    alive = graph.query(subject="agent:夏")
    assert len(alive) == 1, "复活是同一行,不是新加一条(死边 + 新边 = 两条)"
    assert alive[0]["id"] == first_id
    assert alive[0]["valid_from"] == 900, "「从什么时候起又是朋友」要答得出来"
    assert alive[0]["invalid_at"] is None
    # ⚠️ **一条边只有一份有效期**,所以复活会把它之前的那一段一起挪走:900 之前
    # 的任何一刻现在都答"不是朋友",连第一段(100~400)也一样。这是有意的取舍 ——
    # 记全整段历史要一行一段(edges 从此按时间无界增长,而它归 Redis 正是因为
    # 上界按世界的规模封顶,见 CLAUDE.md 有界性那条)。要的是"此刻"与"刚才那一段",
    # 不是完整的关系年鉴;完整的那份在事件日志里,重放答得出来。
    assert graph.query(subject="agent:夏", as_of=600) == []
    assert graph.query(subject="agent:夏", as_of=250) == []
