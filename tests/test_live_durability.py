"""活体持久化:活体导出、出生种子、开机补完孤儿会话。

world.db 时代这里还有"交互即检查点"一组 —— 时钟检查点是 SQLite 的惰性落盘,
玩家碰过世界的那一刻必须刷进 db。RedisClock 每次推进即持久,那个主题整个
不存在了(时钟住哪儿由 test_redis_state 守)。全程 Mock/降级 LLM,离线。
"""
from __future__ import annotations

import gzip
import json
import time

from _worldfile import open_world_at

from anima_world.world_package import import_world_file, inspect_world_file


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
        assert manifest.world_id == "live-test"
        w.tick(1)  # 世界还活着,导出不毁世界
        assert w.scheduler.clock == clock_at_export + 1

    assert inspect_world_file(out)["runnable"]      # 独立再验一遍封皮

    # 先查包本身(而不是 config_get —— 后者会读到这台机器的配置):零密文。
    for line in gzip.open(out, "rt", encoding="utf-8"):
        record = json.loads(line)
        if record.get("kind") != "redis" or record.get("key") != "config":
            continue
        for raw in (record.get("value") or {}).values():
            row = json.loads(raw) if isinstance(raw, str) else raw
            assert not (row.get("is_secret") and row.get("value")), "分发包永远不含密文"

    target = fakeredis.FakeStrictRedis(decode_responses=True)
    import_world_file(out, redis=target, world_id="restored")
    with World.open("restored", redis=target, force_mock_llm=True) as w2:
        assert w2.scheduler.clock == clock_at_export, "安静尾巴不许在导出里缩水"


def test_导出的是这个世界后来的样子不是它出生时的样子(tmp_path):
    """**v3 把"出生证明"整个去掉了。**

    v2 的包里带着世界的创世种子,而那是同一份内容的第二种写法 —— 世界文件把
    "人写的描述"和"机器的 dump"合成了一种格式之后,再单独存一份种子就是纯粹的
    重复,而两份真相里有一份不更新是这个仓库最怕的坏法。

    于是导出的语义变干净了:**一个跑过的世界导出来是它此刻的样子。** 它出生时
    叫什么、从哪份文件建起来的,由建它的那个人留着那份文件去记 —— 那本来就是
    那份文件的工作。
    """
    from _worldfile import write_seed_file

    source = write_seed_file(tmp_path / "birth.cyberworld", {
        "agents": [{"id": "岚", "name": "岚", "location": "哨站", "personality": "寡言,靠得住"}],
        "locations": [{"id": "哨站", "name": "北哨站", "description": "山脊上的瞭望塔",
                       "kind": "point", "x": 0.5, "y": 0.5}],
    })
    out = tmp_path / "later.cyberworld"

    with open_world_at(str(tmp_path / "w.db"), seed_path=source, force_mock_llm=True) as w:
        assert "岚" in w.scheduler.agents
        w.tick(2)
        w.export_snapshot(out, world_id="genesis-test", name="后来的样子")

    kinds = {json.loads(line)["kind"] for line in gzip.open(out, "rt", encoding="utf-8")}
    assert "author" not in kinds, "跑过的世界导出来不该再带作者层"
    assert "redis" in kinds and "manifest" in kinds


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
