"""1.0.2 活体持久化:交互即检查点、活体导出、开机补完孤儿会话。

守的是同一条线:玩家碰过世界的那一刻,db 就必须完整 —— 不用先关世界。
全程 Mock/降级 LLM,离线。
"""
from __future__ import annotations

import json
import sqlite3
import time
import zipfile

import pytest

from anima_world.api import World
from anima_world.world_package import import_world_package, inspect_world_package


def _db_meta(world: World, key: str):
    row = world.scheduler.event_log.conn.execute(
        "SELECT value FROM db_meta WHERE key=?", (key,)
    ).fetchone()
    return row[0] if row else None


def test_interaction_flushes_checkpoints_without_close(tmp_path):
    """玩家动作/聊天回合结束的那一刻,时钟检查点就得在 db 里 —— 不等关停。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as w:
        w.tick(5)
        assert _db_meta(w, "clock") is None or int(_db_meta(w, "clock")) < w.scheduler.clock
        w.player_action("p1", "挥手")
        assert _db_meta(w, "clock") is not None, "player_action 后时钟检查点必须已落盘"
        assert int(_db_meta(w, "clock")) == w.scheduler.clock

        w.tick(3)
        w.record_chat_turn(
            "夏", "p1",
            [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀"}],
        )
        assert int(_db_meta(w, "clock")) == w.scheduler.clock, "聊天回合结束即检查点"


def test_export_snapshot_while_running(tmp_path):
    """世界不关,当场打包:包完整、无密文、导入方拿到的时钟不缩水。"""
    out = tmp_path / "live.cyberworld"
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as w:
        w.config_set("llm.api_key", "sk-live-export-test")
        w.tick(7)
        w.player_action("p1", "挥手")
        clock_at_export = w.scheduler.clock

        manifest = w.export_snapshot(out, world_id="live-test", name="活体导出")
        assert manifest.export_mode == "snapshot"
        w.tick(1)  # 世界还活着,导出不毁世界
        assert w.scheduler.clock == clock_at_export + 1

    inspect_world_package(out)  # 独立再验一遍包契约
    imported = import_world_package(out, tmp_path / "instances")
    # 先查包里的 db 本身(开世界会重播配置默认值,必须在那之前验):
    conn = sqlite3.connect(imported.path / "world.db")
    try:
        secrets = conn.execute("SELECT COUNT(*) FROM config WHERE is_secret=1").fetchone()[0]
    finally:
        conn.close()
    assert secrets == 0, "分发包永远不含密文"
    with World.open(str(imported.path / "world.db"), force_mock_llm=True) as w2:
        assert w2.scheduler.clock == clock_at_export, "安静尾巴不许在导出里缩水"
        assert not w2.config_get("llm.api_key"), "密钥不许跟着包旅行"


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

    with World.open(str(tmp_path / "w.db"), seed_path=str(seed_path), force_mock_llm=True) as w:
        assert "岚" in w.scheduler.agents
        w.tick(2)
        w.export_snapshot(out, world_id="genesis-test", name="出生证明")

    with zipfile.ZipFile(out) as archive:
        packaged = json.loads(archive.read("world_seed.json").decode("utf-8"))
    assert packaged == seed, "包里的种子必须是建库时真正用的那份"


def test_orphan_conversation_closed_on_reopen(tmp_path):
    """崩在 record_chat_turn 中途留下的 open 会话,下次开机补总结、发事件。"""
    db = str(tmp_path / "w.db")
    with World.open(db, force_mock_llm=True) as w1:
        ts = int(time.time())
        cid = w1.chat_store.start_conversation(
            "夏", ts,
            participants=[{"id": "p1", "kind": "user"}, {"id": "夏", "kind": "agent"}],
            location=None, player_id="p1",
        )
        w1.chat_store.add_message(cid, "user", "你好", ts)
        w1.chat_store.add_message(cid, "assistant", "你好呀", ts)
        # 模拟崩溃:不走 close_conversation,直接关世界
    with World.open(db, force_mock_llm=True) as w2:
        conv = w2.chat_store.get(cid)
        assert conv["status"] == "closed", "开机必须收割孤儿会话"
        assert conv["summary"], "总结要补上"
        n = w2.scheduler.event_log.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='conversation'"
        ).fetchone()[0]
        assert n == 1, "补关也要发那一个 conversation 事件"
