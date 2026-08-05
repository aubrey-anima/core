"""有界的东西住内存,无限增长的东西住 MySQL —— 而**有界性是要被守住的**。

## 判据是怎么变锐的

第一版的判据是"这是不是世界"。它拆不动转录:转录当然属于这个世界,只是不该在内存里。
第二版换成"随时间涨,还是随世界的规模涨",拆得动了,而且量得出来。

第三版是用的人给的:**Redis 装的是她带得进上下文的那些东西,而 LLM 的上下文本来就
有上限。** 两个"有上限"是同一个上限 —— 所以真正的判据是:

    进得了提示词的  →  必须有界  →  Redis
    进不了提示词的  →  可以无限  →  MySQL(要用时按 k 取回来)

这条比前两条都好用,因为它**可验**:如果分对了,提示词的长度就不该随世界变老而增长。
实测(同一个角色,同一个世界):

```
 世界日   提示词    MySQL 记忆   MySQL 事件
   0天    2251字        10          50
   5天    2286字        66         329
  20天    2281字       223        1119
  60天    2272字       612        3264      ← 后端涨了 61 倍,提示词纹丝不动
```

## 判据还有另一半:有界性是**渲染器**的属性,不是存储的属性

本体层加进来之后,世界里可以有一万棵树。它们住 Redis 还是 MySQL 一样把提示词撑爆 ——
**存储分层保护不了提示词**。所以"每个能进提示词的类型必须声明一个带上限的选择器"
这条归渲染那一层(`perception.perceive` 的 budget、`Ontology.budget_of`),
而不是归分家那一张表。

## 这个文件守四件事

1. **提示词不随世界变老而增长。** 破了它的样子不是报错,是某天一个跑了很久的世界
   忽然超上下文 —— 而在那之前它一直好好的。
2. **提示词也不随世界变大而增长。** 同一个角色站在 20 棵树中间和站在 3000 棵树
   中间,收到的字数必须一样(截掉了多少要照实说,见 `Perception.overflow`)。
3. **黑板上的量按她的树封顶,不按世界的规模。** 按量分支这条路是一个新的入口,
   它本可以把整个世界的量搬上黑板。
4. **让有界的东西保持有界的那个前提。** `edges` 有上界,只因为谓词是闭集;哪天让
   LLM 自己造谓词,它就不再有界,而分家的账要重算。这条把那个前提钉在明面上。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def redis():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeStrictRedis(decode_responses=True)


def test_the_prompt_does_not_grow_as_the_world_ages(tmp_path, redis):
    """**她带进上下文的东西必须有界,因为上下文有上限。**

    世界跑得越久,事件和记忆越多。如果提示词跟着涨,那么"能跑多久"就取决于模型的
    上下文窗口 —— 而这件事会在某个说不准的日子忽然发生,在那之前一切正常。

    这条跑一个真世界到第 40 个世界日,比对第 0 天与第 40 天的提示词长度。
    """
    from anima_world.api import World

    world = World.open("bounded", redis=redis, force_mock_llm=True)
    try:
        agent = next(iter(world.state()["agents"]))

        def prompt_size() -> int:
            blocks = world.debug_prompt(agent)["blocks"]
            return sum(len(b["text"]) for b in blocks if b.get("text"))

        fresh = prompt_size()
        world.tick(288 * 40)
        aged = prompt_size()

        # 后端确实涨了 —— 否则这条测试什么都没验到
        assert world.scheduler.event_log.count() > 1000, (
            "世界没怎么跑,这条测试是空的"
        )
        # 记忆有容量淘汰(memory.capacity=50,按强度逐出)—— 涨满即证明后端在写。
        assert len(world.scheduler.memory_store.query(agent)) >= 50, "记忆没涨"

        # 而提示词没有。留 1.5 倍的余量:允许波动,不允许随年龄线性涨。
        assert aged <= fresh * 1.5, (
            f"提示词从 {fresh} 字涨到 {aged} 字 —— 它在随世界变老而增长,"
            f"那么这个世界能跑多久取决于模型的上下文窗口"
        )
    finally:
        world.close()


def _forest(tmp_path, count: int, *, watch: bool = False):
    """一个角色,和这么多棵树站在同一个地方。"""
    import json

    from _worldfile import open_world_at

    agent: dict = {"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"}
    if watch:
        agent["duties"] = [{
            "name": "tend", "start": "00:00", "end": "23:59", "kind": "idle_wander",
            "when_stock": {"owner": "tree:t00000", "key": "树高", "op": "<", "value": 9},
        }]
    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
        "agents": [agent],
        "kinds": [{
            "id": "tree", "gloss": "一棵树",
            "quantities": {"树高": {"default": 1.0, "visibility": "here", "unit": "米"}},
            "prompt": {"budget": 3},
        }],
        "entities": [
            {"id": f"tree:t{i:05d}", "name": f"第{i}棵", "location": "cafe"}
            for i in range(count)
        ],
        "stocks": [{"owner": f"tree:t{i:05d}", "values": {"树高": 1.0}} for i in range(count)],
    }
    path = tmp_path / f"seed{count}.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return open_world_at(str(tmp_path / f"w{count}.db"), seed_path=str(path), force_mock_llm=True)


def test_the_prompt_does_not_grow_as_the_world_gets_bigger(tmp_path):
    """**一万棵树住哪儿都一样炸提示词。** 所以有界性归渲染器,不归存储。

    同一个角色,一次站在 20 棵树中间,一次站在 3000 棵中间。字数必须几乎一样 ——
    差的那几个字是"这里还有 2997 样别的东西"里的数字,而**那句话本身是必须在的**:
    截断了却不吭声,等于让她在一个"她以为只有三棵树"的世界里做决定,而她永远不会
    知道自己被骗了。
    """
    def texts(world) -> list[str]:
        return [b["text"] for b in world.debug_prompt("甲")["blocks"] if b.get("text")]

    with _forest(tmp_path, 20) as small:
        small.tick(2)
        few = sum(len(t) for t in texts(small))
        assert any("你没细看" in t for t in texts(small)), "截断了却没说"
    with _forest(tmp_path, 3000) as big:
        big.tick(2)
        many = sum(len(t) for t in texts(big))

    assert abs(many - few) < 30, (
        f"树多了 150 倍,提示词从 {few} 字变成 {many} 字 —— 它在随世界变大而增长"
    )


def test_the_blackboard_is_capped_by_her_tree_not_by_the_world(tmp_path):
    """按量分支是一个新入口 —— 它本可以把整个世界的量搬上黑板。

    settle 的是**她的树问到的那几个**,不是这儿所有东西的所有量。3000 棵树的
    世界里,她的黑板上该只有一个 `stock.*`。
    """
    with _forest(tmp_path, 3000, watch=True) as world:
        world.tick(2)
        keys = [k for k in world.scheduler.agents["甲"].agent.blackboard.snapshot()
                if k.startswith("stock.")]
        assert keys == ["stock.tree:t00000.树高"], keys


def test_the_predicate_vocabulary_stays_closed(tmp_path):
    """**`edges` 有上界,只因为谓词是闭集。**

    关系边有 `UNIQUE(subject, predicate, object)`,主宾都是 `agent:<id>` ——
    所以上界是 2×N²,按世界的规模封顶。它因此该住 Redis,不住 MySQL。
    (它一度被分进 MySQL,是照着"像不像历史"分的,而不是照着增长性。)

    这个上界**整个挂在谓词是闭集这件事上**。哪天让 LLM 自己造谓词(很自然的一步:
    "她们之间是什么关系"交给模型说),边就不再有界,而分家的账要重算。

    这条不阻止那件事发生 —— 它只保证那件事发生时**有人当场知道**。
    """
    import re

    from anima_world import scheduler as scheduler_mod

    source = pathlib_read(scheduler_mod.__file__)
    calls = re.findall(r"knowledge_graph\.add\(\s*(.*?)\)", source, re.S)
    assert calls, "找不到写关系边的地方 —— 这条测试的锚点没了"

    for call in calls:
        predicate = call.split(",")[1].strip()
        assert predicate == "predicate", (
            f"关系边的谓词换成了 {predicate} —— 如果它不再来自那个闭集,"
            f"edges 就不再有上界,mysql_state.GROWS_FOREVER 的账要重算"
        )

    # 那个闭集本身:全文件只有这两个字面量赋给 predicate
    literals = set(re.findall(r'predicate = "([^"]+)"', source))
    assert literals == {"friendship", "rivalry"}, (
        f"谓词表变成了 {sorted(literals)} —— 变大了就重新算 edges 的上界,"
        f"变小了就更新这条"
    )


def test_edges_is_not_in_the_mysql_list(tmp_path):
    """判据是判据:有界的东西不进 MySQL。"""
    from anima_world.mysql_state import GROWS_FOREVER, SCHEMA

    assert "edges" not in GROWS_FOREVER, (
        "edges 按世界规模封顶(2×N²),把它放进 MySQL 等于把一个有界的热读"
        "放进最慢的后端"
    )
    assert "edges" not in SCHEMA
    assert set(GROWS_FOREVER) == set(SCHEMA), (
        "名单和真建的表对不上 —— 其中一个在撒谎"
    )


def pathlib_read(path: str) -> str:
    import pathlib

    return pathlib.Path(path).read_text(encoding="utf-8")
