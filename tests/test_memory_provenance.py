"""记忆分型(R3):**出处丢了之后,再多的检索精度也救不回来。**

这一格是后加的,所以它有一条最容易错的路:**分型之前写下的那些行**。它们身上没有
`provenance`,而默认值写在读的那一侧(不为了一个新字段去改写一整张历史表)——
问题是那个默认值是什么:

一律读成「亲历」的话,老的 `kind='reaction'`(八卦传过来的)就被报成她亲眼所见,
而这**正是这一格要治的那个病本身**:她把一条传闻讲得斩钉截铁。所以老行按 kind 补,
走 `memory_store.provenance_of()` —— 和引擎写新行时用的是同一个函数。

⚠️ **两个后端必须给同一个答案。** 一条记忆在 Redis 上叫「听说」、搬去 MySQL 之后
叫「亲历」,是这个仓库最怕的那类不一致:两处都"照跑",而分叉的那天没人会发现。
"""
from __future__ import annotations

import pytest

from anima_world.memory_store import PROVENANCE_BY_KIND, provenance_of
from anima_world.redis_state import RedisMemoryStore


@pytest.fixture
def store(fresh_redis):
    return RedisMemoryStore(fresh_redis, "t")


def test_the_map_is_not_exhaustive_on_purpose():
    """没列的一律亲历 —— 要求穷举的表会在作者自造 kind 时悄悄判成别的东西。"""
    assert provenance_of("reaction") == "heard"
    assert provenance_of("reflection") == "believed"
    assert provenance_of("作者自己造的kind") == "experienced"
    assert provenance_of("") == "experienced"


def test_new_rows_carry_what_the_writer_said(store):
    store.add("夏", tick=1, kind="reaction", summary="听说遥要走了", provenance="heard")
    assert store.query("夏")[0]["provenance"] == "heard"


def test_a_new_row_and_an_old_row_of_the_same_kind_read_the_same(store):
    """**没说出处的新行按 kind 判,不是一律亲历。**

    写侧原先硬写着 `provenance="experienced"`,于是 `add(kind="reaction")` 落下的
    **新**行读作亲历,而同一个 kind 的**老**行(读侧按 kind 补)读作听说 ——
    同一格数据两个答案,只差在这一行是什么时候写的,而两边都不报错。
    这和阈值那条(`memory_admission.DEFAULT_THRESHOLD`)是同一个形状:
    **抄一份默认值就是给"两份真相"留位置。**
    """
    new_row = store.add("夏", tick=1, kind="reaction", summary="听说遥要走了")
    old_row = store.add("夏", tick=1, kind="reaction", summary="也是听说的")
    row = store._rows.get(str(old_row))
    row.pop("provenance")                 # 分型之前写下的行长这样
    store._rows.put(str(old_row), row)

    by_id = {r["id"]: r["provenance"] for r in store.query("夏")}
    assert by_id[new_row] == by_id[old_row] == provenance_of("reaction") == "heard"


def test_the_writer_still_gets_the_last_word(store):
    """按 kind 判的是**兜底**,不是覆盖:说了就照说的记。"""
    store.add("夏", tick=1, kind="reaction", summary="这条我在场", provenance="experienced")
    assert store.query("夏")[0]["provenance"] == "experienced"


def test_a_descriptor_that_says_nothing_says_none_not_experienced():
    """`MemoryDescriptor.provenance` 的默认值必须是 `None`(= 没说)。

    它原先是 `"experienced"` —— 一个**真值**,于是 `Scheduler` 那句
    `descriptor.provenance or self._provenance_of(kind)` 一次都没有生效过,
    而触发器产出的 `reaction` 就此一律落成亲历。
    """
    from anima_world.memory_store import MemoryDescriptor

    assert MemoryDescriptor(agent_id="夏", tick=0, kind="reaction",
                            summary="听来的").provenance is None


def test_an_old_row_without_the_column_is_typed_by_its_kind(store):
    """分型之前写下的行:按 kind 补,不是一律亲历。"""
    mid = store.add("夏", tick=1, kind="reaction", summary="听说遥要走了")
    row = store._rows.get(str(mid))
    row.pop("provenance")                 # 老行长这样:压根没有这一格
    store._rows.put(str(mid), row)

    assert store.query("夏")[0]["provenance"] == "heard", (
        "八卦传过来的那条被报成亲历,正是分型要治的病本身"
    )


def test_both_backends_answer_the_same_for_an_old_row():
    """Redis 与 MySQL 的读侧对同一条老行必须给同一个答案。

    **不需要一台 MySQL**:归一发生在 `_dicts()` 里,而它是行 → dict 的纯函数。
    有服务才能跑的测试在没有服务的机器上是 skip,而 skip 掩护过真 bug
    (`MySQLChatStore.__slots__` 那条)。
    """
    from anima_world.mysql_state import MySQLMemoryStore

    mysql = MySQLMemoryStore.__new__(MySQLMemoryStore)
    for kind, expected in list(PROVENANCE_BY_KIND.items()) + [("obs", "experienced")]:
        row = tuple(
            None if col != "kind" else kind for col in MySQLMemoryStore._COLS
        )
        (item,) = mysql._dicts([row])
        assert item["provenance"] == expected == provenance_of(kind)
