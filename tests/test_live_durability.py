"""活体持久化:活体导出、出生种子、开机补完孤儿会话。

world.db 时代这里还有"交互即检查点"一组 —— 时钟检查点是 SQLite 的惰性落盘,
玩家碰过世界的那一刻必须刷进 db。RedisClock 每次推进即持久,那个主题整个
不存在了(时钟住哪儿由 test_redis_state 守)。全程 Mock/降级 LLM,离线。
"""
from __future__ import annotations

import json
import time
import zipfile

from _worldfile import open_world_at

from anima_world.world_package import import_world_package, inspect_world_package


def test_export_snapshot_while_running(tmp_path):
    """世界不关,当场打包:包完整、无密文、导入方拿到的时钟不缩水。"""
    import fakeredis

    from anima_world.api import World

    out = tmp_path / "live.cyberworld"
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as w:
        w.tick(7)
        w.player_action("p1", "挥手")
        clock_at_export = w.scheduler.clock

        manifest = w.export_snapshot(out, world_id="live-test", name="活体导出")
        assert manifest.export_mode == "snapshot"
        w.tick(1)  # 世界还活着,导出不毁世界
        assert w.scheduler.clock == clock_at_export + 1

    inspect_world_package(out)  # 独立再验一遍包契约
    # 先查包本身(而不是 config_get —— 后者会读到这台机器的配置):零密文。
    with zipfile.ZipFile(out) as archive:
        state = json.loads(archive.read("world_state.json"))
    config_entry = state["redis"].get("config") or {}
    rows = config_entry.get("value") or {}
    for raw in (rows.values() if isinstance(rows, dict) else []):
        row = json.loads(raw) if isinstance(raw, str) else raw
        assert not (row.get("is_secret") and row.get("value")), "分发包永远不含密文"

    target = fakeredis.FakeStrictRedis(decode_responses=True)
    import_world_package(out, redis=target, world_id="restored")
    with World.open("restored", redis=target, force_mock_llm=True) as w2:
        assert w2.scheduler.clock == clock_at_export, "安静尾巴不许在导出里缩水"


def test_export_snapshot_uses_genesis_seed(tmp_path):
    """活体导出默认带世界自己的出生种子,不是内置演示种子。"""
    seed = {
        "agents": [{"id": "岚", "name": "岚", "location": "哨站", "personality": "寡言,靠得住"}],
        "locations": [{"id": "哨站", "name": "北哨站", "description": "山脊上的瞭望塔",
                       "kind": "point", "x": 0.5, "y": 0.5}],
    }
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "genesis.cyberworld"

    with open_world_at(str(tmp_path / "w.db"), seed_path=str(seed_path), force_mock_llm=True) as w:
        assert "岚" in w.scheduler.agents
        w.tick(2)
        w.export_snapshot(out, world_id="genesis-test", name="出生证明")

    with zipfile.ZipFile(out) as archive:
        packaged = json.loads(archive.read("world_seed.json").decode("utf-8"))
    assert packaged == seed, "包里的种子必须是建库时真正用的那份"


def test_orphan_conversation_closed_on_reopen(tmp_path):
    """崩在 record_chat_turn 中途留下的 open 会话,下次开机补总结、发事件。"""
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as w1:
        ts = int(time.time())
        cid = w1.chat_store.start_conversation(
            "夏", ts,
            participants=[{"id": "p1", "kind": "user"}, {"id": "夏", "kind": "agent"}],
            location=None, player_id="p1",
        )
        w1.chat_store.add_message(cid, "user", "你好", ts)
        w1.chat_store.add_message(cid, "assistant", "你好呀", ts)
        # 模拟崩溃:不走 close_conversation,直接关世界
    with open_world_at(db, force_mock_llm=True) as w2:
        conv = w2.chat_store.get(cid)
        assert conv["status"] == "closed", "开机必须收割孤儿会话"
        assert conv["summary"], "总结要补上"
        n = sum(
            1 for e in w2.scheduler.event_log.replay() if e.type == "conversation"
        )
        assert n == 1, "补关也要发那一个 conversation 事件"
