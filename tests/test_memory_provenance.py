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
