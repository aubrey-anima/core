"""**一个人离开了这个世界** —— 以及那句话在事件溯源里的正确形状。

线上 `night-tide` 跑了几周之后,`anima:night-tide:contact` 里躺着 6 个早就不存在
的试玩账号(`aubrey`、`claude-playtest-001`、`playtest-aubrey`、两个裸 UUID…)。
江晚和其中 5 个"有关系" —— 那些关系**占着她的联系配额和社交需求**:她会想起一个
永远不会再出现的人,而这一层的每一次触发都是从别人那里挪走的。

**这不是一个删数据的口子。** 关系是投影(`state_change/sentiment_delta` 折出来的),
联系态是世界自己写的演化态 —— 手改一行演化态 = 伪造历史 = 投影和日志对不上,而且
没有任何地方会报错。所以正确的形状是**往日志里追加一条事实**:「这个人离开了这个
世界」,然后由折叠端和世界自己去响应它。

三条不变量,每条一个测试:

1. **对账即重放仍然成立**:把日志重放一遍,得到的关系表和跑着的世界逐位相同 ——
   如果告别是一次 DELETE,重放会把那 5 段关系原样折回来。
2. **历史一个字不改**:事件、记忆、转录、账本全留着。她记得这个人来过 —— 只是
   不再等他。走的是**朝前看**的那一半(关系、联系冷却、姿态、静音、回头找你)。
3. **幂等**,而且 `dry_run` 一个字节都不写(这类命令动的是世界,先看一眼是用法)。
"""
from __future__ import annotations

import json

import pytest

from _worldfile import open_world_at, run_cli, redis_for

from anima_world.projection import project_events


def _knows(world, agent_id: str, player_id: str) -> bool:
    return (agent_id, player_id) in world.scheduler._memory_projection.relations


def _befriend(world, agent_id: str, player_id: str, name: str = "阿檀") -> None:
    """让世界里真的长出一段"她和这个玩家"的关系 —— 走既有的那条路,不手写投影。"""
    world.scheduler.contact_store.note_contact(
        agent_id, player_id, tick=world.scheduler.clock, name=name,
    )
    world.scheduler._record_and_deliver({
        "type": "state_change", "who": agent_id,
        "payload": {"kind": "sentiment_delta", "as": agent_id, "target": player_id,
                    "delta": 0.5, "as_name": world.scheduler.agent_display_name(agent_id),
                    "target_name": name},
    })


@pytest.fixture
def world(tmp_path):
    w = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    yield w
    w.close()


def test_告别是一条事件_重放之后关系照样是空的(world):
    """**这一条就是"为什么不是 DELETE"。**

    关系不是一张表,是 `state_change` 折出来的投影 —— 删掉投影里那一行,下一次
    重放(换个进程、重启一次、`catch_up_projection` 一次)会原样把它折回来,而
    世界照跑、日志干净。日志里有那条告别,重放才收敛。
    """
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-1")
    assert _knows(world, agent_id, "ghost-1"), "夹具前提没成立"

    world.forget_player("ghost-1")
    assert not _knows(world, agent_id, "ghost-1"), "告别之后她不该还惦记着他"

    # 对账即重放:拿整条日志重折一遍,得到的关系表必须一样。
    replayed = project_events(world.scheduler.event_log.replay())
    assert (agent_id, "ghost-1") not in replayed.relations, (
        "重放把那段关系折回来了 —— 这一层是在改投影而不是在追加事实"
    )
    assert (agent_id, "ghost-1") not in world.state()["relations"] and \
        f"{agent_id}|ghost-1" not in world.state()["relations"]


def test_两个方向都要走(world):
    """关系是有向的两行。只清一边的话,反向那行照样喂给 planner / 八卦。"""
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-2")
    world.scheduler._record_and_deliver({
        "type": "state_change", "who": "ghost-2",
        "payload": {"kind": "sentiment_delta", "as": "ghost-2", "target": agent_id,
                    "delta": 0.4},
    })
    assert ("ghost-2", agent_id) in world.scheduler._memory_projection.relations

    world.forget_player("ghost-2")
    replayed = project_events(world.scheduler.event_log.replay())
    assert not any("ghost-2" in key for key in replayed.relations), \
        "反向那一行还在"


def test_告别之后联系那一层不再算他(world):
    """`contact` 的行是她"上次想起他是什么时候"的当前值 —— 幽灵留着它,等于
    永远占着一个名额。"""
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-3")
    assert any(r["player_id"] == "ghost-3" for r in world.scheduler.contact_store.all())

    world.forget_player("ghost-3")
    assert not any(r["player_id"] == "ghost-3" for r in world.scheduler.contact_store.all())
    assert not any(row["player_id"] == "ghost-3" for row in world.contact_forecast())


def test_告别之后关系图上不许还挂着他(world):
    """**关系有两份记法,告别只走了一份。**

    投影里的 `relations` 是可变的数值,关系图上的 `edges` 是"这两人是朋友"这个
    **事实**。前者被 `player_departed` 折掉了,后者是一张自己的表 —— 边只增不减
    (`add` 是 INSERT OR IGNORE),于是告别之后那条 `friendship` 还在。

    看得见的后果是小团体:`compute_cliques` 只看 friendship 的连通分量,一条指向
    幽灵的边把他算进她的团体里,`World.cliques()` 就报得出一个不存在的人 ——
    而这一层没有任何一处会报错。线上 `night-tide` 上真的有两条(苏念 ⇄ 一个已经
    清干净的试玩账号,两个方向都在)。

    `drop()` 这个原语本来就是为撤销存在的(关系反转时用着),只是告别没调它。
    """
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-6")
    world.scheduler._record_and_deliver({
        "type": "state_change", "who": "ghost-6",
        "payload": {"kind": "sentiment_delta", "as": "ghost-6", "target": agent_id,
                    "delta": 0.5},
    })
    graph = world.scheduler.knowledge_graph
    assert graph.query(subject=f"agent:{agent_id}", object="agent:ghost-6"), "夹具前提没成立"
    assert graph.query(subject="agent:ghost-6"), "反向那条边也该先立起来"

    receipt = world.forget_player("ghost-6")
    assert receipt["edges"] >= 2, "回执得说出撤了几条边"
    assert not graph.query(subject=f"agent:{agent_id}", object="agent:ghost-6")
    assert not graph.query(subject="agent:ghost-6")
    assert not any("ghost-6" in r["subject"] or "ghost-6" in r["object"]
                   for r in graph.query()), "关系图上还挂着一个不存在的人"

    assert world.forget_player("ghost-6")["edges"] == 0, "重入不该再撤一遍"


def test_dry_run_也要数得出关系图上的边(world):
    """先看一眼是这类命令的用法 —— 数不出来的那一格等于没修。"""
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-7")
    preview = world.forget_player("ghost-7", dry_run=True)
    assert preview["edges"] >= 1
    assert world.scheduler.knowledge_graph.query(object="agent:ghost-7"), "dry_run 动了世界"


def test_历史一个字不改(world):
    """她记得这个人来过 —— 走的只是"还惦记着"。"""
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-4")
    before = len(world.scheduler.event_log.replay())

    world.forget_player("ghost-4")
    after = world.scheduler.event_log.replay()
    assert len(after) == before + 1, "告别只该往日志里加一条,不许删任何一条"
    assert any(
        e.type == "state_change"
        and (e.payload or {}).get("target") == "ghost-4"
        for e in after
    ), "那段关系是怎么来的,日志里还得查得到"
    departed = [e for e in after if e.type == "player_departed"]
    assert len(departed) == 1
    assert departed[0].payload["player_id"] == "ghost-4"


def test_告别是幂等的_而且dry_run不写(world):
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-5")

    preview = world.forget_player("ghost-5", dry_run=True)
    assert preview["relations"] >= 1 and preview["dry_run"] is True
    assert _knows(world, agent_id, "ghost-5"), "dry_run 动了世界"
    assert not [e for e in world.scheduler.event_log.replay() if e.type == "player_departed"]

    first = world.forget_player("ghost-5", reason="账号注销")
    assert first["relations"] >= 1
    second = world.forget_player("ghost-5")
    assert second["relations"] == 0, "重入不该报错,也不该再清一遍"


# ── CLI 出口 ────────────────────────────────────────────────────────────────


def test_cli_能读一个玩家的收件箱(tmp_path):
    """`contact_requests()` / `inbox()` 都对,但 CLI 上一个出口都没有 —— 于是
    运维只能 `redis-cli HGETALL anima:night-tide:contact`。三道闸里"有 CLI 出口"
    这一条,这一层本来是不合格的。"""
    db = tmp_path / "w.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        agent_id = next(iter(world.scheduler.agents))
        brain = world.scheduler.agents[agent_id]
        here = brain.agent.blackboard.read("loc") or brain.agent.location
        world.player_move("p1", here)
        world.scheduler._maybe_hail_player(brain.agent)

    result = run_cli("contact", "--world-id", "w", "--player", "p1",
                     "--inbox", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["inbox"], "CLI 读不到收件箱"
    assert payload["inbox"][0]["payload"]["agent_name"]
    # 增量拉取和 `contact_requests` 同一个用法。
    last = payload["inbox"][-1]["seq"]
    again = run_cli("contact", "--world-id", "w", "--player", "p1",
                    "--inbox", "--since-seq", str(last), "--json")
    assert json.loads(again.stdout)["inbox"] == []


def test_cli_能让世界跟一个走掉的人告别(tmp_path):
    db = tmp_path / "w.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        agent_id = next(iter(world.scheduler.agents))
        _befriend(world, agent_id, "claude-playtest-001")

    preview = run_cli("player", "forget", "--world-id", "w",
                      "--player", "claude-playtest-001", "--dry-run", "--json")
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["relations"] >= 1

    with open_world_at(str(db), force_mock_llm=True) as world:
        assert _knows(world, agent_id, "claude-playtest-001"), "--dry-run 动了世界"

    done = run_cli("player", "forget", "--world-id", "w",
                   "--player", "claude-playtest-001", "--reason", "试玩账号", "--json")
    assert done.returncode == 0, done.stderr

    with open_world_at(str(db), force_mock_llm=True) as world:
        assert not _knows(world, agent_id, "claude-playtest-001")


def test_cli_对不存在的世界一律拒绝(tmp_path):
    """只读命令那条规矩,写命令更要守 —— 抄错名字会当场创世。"""
    redis_for(tmp_path / "w.db")
    result = run_cli("player", "forget", "--world-id", "nope", "--player", "x")
    assert result.returncode == 2
