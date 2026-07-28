"""一个 async 宿主(网站后端)能不能用这个包。

门面是同步的,这是设计:世界活在调用方进程里,聊天流式在调用方线程上消费。但"同步
门面"和"只能从非 async 代码里调用"是两回事 —— 而今天是后者:门面内部用
`asyncio.run()` 和一个新建的事件循环去驱动聊天子系统,两者在**已经有 running loop
的线程**上都会当场 RuntimeError,并且漏一个 never-awaited coroutine。

这不是边角料:FastAPI / aiohttp 的请求处理函数就是 async def。README 写着"嵌入到
应用里(主要用法)",而主要用法里最常见的那种应用,调一次 chat 就炸。
"""
from __future__ import annotations

import asyncio

import pytest

from anima_world.api import World


def _in_async(fn):
    """在一个真正 running 的事件循环里跑 fn(同步函数),返回它的结果。"""
    async def _main():
        return fn()
    return asyncio.run(_main())


def test_chat_works_from_inside_a_running_event_loop(tmp_path):
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        reply = _in_async(lambda: world.chat_reply(
            "夏", [{"role": "user", "content": "在吗"}],
            player_id="p1", display_name="阿檀",
        ))
        assert reply.strip(), "async 宿主里聊天必须能出回复"


def test_recording_a_turn_works_from_inside_a_running_event_loop(tmp_path):
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        def _turn():
            reply = world.chat_reply("夏", [{"role": "user", "content": "在吗"}],
                                     player_id="p1", display_name="阿檀")
            return world.record_chat_turn("夏", "p1", [
                {"role": "user", "content": "在吗"},
                {"role": "assistant", "content": reply},
            ])

        conversation_id = _in_async(_turn)
        assert isinstance(conversation_id, int)
        kinds = {
            row[0] for row in world.scheduler.event_log.conn.execute(
                "SELECT type FROM events WHERE type = 'conversation'"
            )
        }
        assert kinds == {"conversation"}, "会话事件必须真的落库,而不是被异常吞掉"


def test_closing_a_conversation_works_from_inside_a_running_event_loop(tmp_path):
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        store = world.chat_service.store
        conversation = store.active_or_start("夏", 0, player_id="p1")
        store.add_message(conversation, "user", "在吗", 0)
        assert _in_async(lambda: world.close_conversation(conversation)) is True


def test_opening_a_world_from_inside_a_running_event_loop_still_reaps_orphans(tmp_path):
    """开机补完孤儿会话是自动恢复,不该因为宿主是 async 就默默失效。"""
    db = str(tmp_path / "w.db")
    with World.open(db, force_mock_llm=True) as world:
        store = world.chat_service.store
        conversation = store.active_or_start("夏", 0, player_id="p1")
        store.add_message(conversation, "user", "留一句没关掉的话", 0)

    def _reopen():
        return World.open(db, force_mock_llm=True)

    reopened = _in_async(_reopen)
    try:
        open_rows = reopened.scheduler.event_log.conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE status = 'open'"
        ).fetchone()[0]
        assert open_rows == 0, "async 宿主里开机,孤儿会话也必须被补完"
    finally:
        reopened.close()


def test_achat_streams_on_the_hosts_own_loop(tmp_path):
    """真 async 的那扇门:流式转发,不占宿主线程。"""
    async def _main(world):
        chunks = []
        async for token in world.achat(
            "夏", [{"role": "user", "content": "在吗"}],
            player_id="p1", display_name="阿檀",
        ):
            chunks.append(token)
        return "".join(chunks)

    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        assert asyncio.run(_main(world)).strip()
