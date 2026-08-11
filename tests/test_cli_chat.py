"""`anima-world chat`:引擎最像人的那件能力,总算有了一道门(#6)。

README 开篇讲的是"会记住你的角色",而"和角色说句话"过去只有写 Python 才够得着
—— `World.chat_reply` / `record_chat_turn` 早就齐全,缺的只是入口。所以这里验的
是**入口**:聊完的东西真的进了世界,而不是打印在屏幕上就没了。
"""
from __future__ import annotations

from _worldfile import open_world_at, run_cli

import subprocess
import sys

from anima_world.api import World


def _a_world(tmp_path) -> str:
    """建一个世界但不推进它(`--ticks 0` 是唯一的无头创世口径)。"""
    from _worldfile import redis_for

    db = tmp_path / "w.db"
    redis_for(db)   # CLI 与 open_world_at 用同一个客户端
    result = run_cli("simulate", "--world-id", "w",
         "--ticks", "0", "--llm", "mock")
    assert result.returncode == 0, result.stderr
    return str(db)


def _chat(db: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return run_cli("chat", "--world-id", "w", *args, input=stdin)


def test_chat_without_an_agent_lists_who_lives_here(tmp_path):
    """一个世界文件至今没法自报家门 —— 拿到一个 .cyberworld 也不知道里面住着谁。"""
    result = _chat(_a_world(tmp_path))
    assert result.returncode == 0, result.stderr
    for agent_id in ("夏", "遥", "柔"):
        assert agent_id in result.stdout
    assert "苏晚夏" in result.stdout, "名册要给人看的名字,不只是 id"
    assert "--agent" in result.stdout, "列完要指路,否则用户还得回去翻 help"


def test_chat_with_an_unknown_agent_refuses_and_shows_the_cast(tmp_path):
    """打错名字是最常见的一种失败,它该顺手回答"那有谁"。"""
    result = _chat(_a_world(tmp_path), "--agent", "张三")
    assert result.returncode == 2
    assert "张三" in result.stderr
    assert "夏" in result.stdout


def test_a_chat_turn_lands_in_the_world_not_just_on_the_screen(tmp_path):
    """说完一句话那一刻 db 就是完整的:会话关闭、摘要生成、事件落库。"""
    db = _a_world(tmp_path)
    result = _chat(db, "--agent", "夏", "--name", "阿檀", stdin="你好呀\n最近忙吗\n\n")
    assert result.returncode == 0, result.stderr

    with open_world_at(db, force_mock_llm=True) as world:
        conversations = world.conversations("夏")
        assert len(conversations) == 2, "一轮一记,而不是退出时才补一笔"
        assert all(c["status"] == "closed" for c in conversations)
        said = {
            message["content"]
            for conversation in conversations
            for message in world.conversation_messages(conversation["id"])
            if message["role"] == "user"
        }
        assert said == {"你好呀", "最近忙吗"}


def test_chat_does_not_advance_the_world_clock(tmp_path):
    """对话发生在世界的此刻。一个 CLI 不该趁你打字偷偷推进别人的世界 ——
    要边活边聊是宿主应用的事(`World.open` + `start_clock` + `chat`)。"""
    db = _a_world(tmp_path)
    with open_world_at(db, force_mock_llm=True) as world:
        before = world.scheduler.clock
    _chat(db, "--agent", "夏", stdin="在吗\n\n")
    with open_world_at(db, force_mock_llm=True) as world:
        assert world.scheduler.clock == before


def test_命令行上你只有一个身份():
    """`play` 从前默认 `p1`,`chat` / `prompt` 默认 `cli`。

    于是玩完 `play` 再照 `--help` 去 `prompt` 看她收到什么,看到的是**另一个人**:
    名字、关系、玩家教过的规则全记在 `p1` 头上,而调试视图问的是一个从没说过话的
    `cli`。它不报错,只是空着 —— "调试视图撒谎比没有调试视图更坏"的一种,而且这
    一次撒的谎是"她根本不认识你"。
    """
    from anima_world.__main__ import DEFAULT_PLAYER_ID, _build_parser

    parser = _build_parser()
    defaults = {
        cmd: parser.parse_args([cmd] + extra).player_id
        for cmd, extra in (("chat", []), ("play", []), ("prompt", ["--agent", "夏"]))
    }
    assert set(defaults.values()) == {DEFAULT_PLAYER_ID}, defaults
