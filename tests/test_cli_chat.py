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


def test_命令行上你只有一个身份(tmp_path):
    """`play` 从前默认 `p1`,`chat` / `prompt` 默认 `cli`。

    于是玩完 `play` 再照 `--help` 去 `prompt` 看她收到什么,看到的是**另一个人**:
    名字、关系、玩家教过的规则全记在 `p1` 头上,而调试视图问的是一个从没说过话的
    `cli`。它不报错,只是空着 —— "调试视图撒谎比没有调试视图更坏"的一种,而且这
    一次撒的谎是"她根本不认识你"。

    ⚠️ **这条从前钉在 parser 的默认值上,而那一层钉不住它要的东西。** `chat`/`play`
    会 `player_move` 把 `cli` 挪进世界、当场变成真人;`prompt` 是「看,但不碰」,
    永远不会 —— 三个默认值一模一样,而 `prompt` 那个必然是世界不认得的幽灵,身份/
    在场/关系三块因此整个换一套算法(见 `test_debug_prompt.py` 末尾那一组)。所以
    `prompt` 的默认值现在**有意**是 `None`,而这句话搬到了它真正成立的那一层:
    **玩过之后再去看,看到的还是同一个人。**
    """
    import json

    from anima_world.__main__ import DEFAULT_PLAYER_ID, _build_parser

    parser = _build_parser()
    assert parser.parse_args(["chat"]).player_id == DEFAULT_PLAYER_ID
    assert parser.parse_args(["play"]).player_id == DEFAULT_PLAYER_ID
    assert parser.parse_args(["prompt", "--agent", "夏"]).player_id is None, (
        "prompt 不该跟着默认成 cli —— 它不写玩家状态,那个 id 在这条命令上永远是幽灵"
    )

    # 真路径:先以默认身份聊一句(`cli` 由此被挪进世界),再不带 --player-id 去看。
    db = _a_world(tmp_path)
    said = _chat(db, "--agent", "夏", "--name", "阿檀", stdin="在吗\n\n")
    assert said.returncode == 0, said.stderr
    seen = run_cli("prompt", "--world-id", "w", "--agent", "夏", "--json")
    assert seen.returncode == 0, seen.stderr
    asker = json.loads(seen.stdout)["asker"]
    assert asker["player_id"] == DEFAULT_PLAYER_ID, f"看到的是另一个人:{asker}"
    assert asker["known"], f"玩过一轮之后世界还是不认得他:{asker}"


def test_the_header_names_the_place_in_chinese(tmp_path):
    """抬头印的是「苏晚夏 @ 家」,不是「苏晚夏 @ home」。

    同一个命令的名册早就把地点 id 翻成人话了,抬头漏了 —— 于是用户第一眼看到
    的是「@ home」,而紧接着的地图、提示词、她自己的台词里全写「家」。它不报错,
    只是这个世界对着用户说了半句英文,而中文优先是明文纪律。
    """
    result = _chat(_a_world(tmp_path), "--agent", "夏", stdin="\n")
    assert result.returncode == 0, result.stderr
    header = next(
        (line for line in result.stdout.splitlines() if "苏晚夏" in line and "@" in line),
        "",
    )
    assert header, f"没找到抬头:{result.stdout!r}"
    assert "home" not in header and "cafe" not in header, (
        f"抬头把地点 id 直接印出来了:{header!r}"
    )


def test_a_turns_observations_land_on_the_message_row(tmp_path, monkeypatch):
    """她这一轮摆的姿态得跟着消息一起落库。

    `chat_reply` 的 `meta` 是出参 —— 姿态、意图、调过的能力都从那里回来,而
    `record_chat_turn(..., meta=meta)` 才把它们写到消息行的四列上。`chat` 这条门
    从来没建过那个 dict:消息本身好好地落了库,四列永远是 null。运维台上气泡照常
    显示,只是永远没有 tag,而没有一处会报错。`play` 那条门一直是对的 ——
    **两条门,一条把观测量丢了**。
    """
    from anima_world import llm_client as llm_mod

    class _DeclaresAStance(llm_mod.MockLLMClient):
        async def complete(self, messages):
            return "〔stance:provoke〕\n哼。"

        async def stream(self, messages):
            yield "〔stance:provoke〕\n哼。"

    monkeypatch.setattr(llm_mod, "MockLLMClient", _DeclaresAStance)

    db = _a_world(tmp_path)
    result = _chat(db, "--agent", "夏", "--name", "阿檀", stdin="你这店真差劲\n\n")
    assert result.returncode == 0, result.stderr

    with open_world_at(db, force_mock_llm=True) as world:
        # 观测量不走 `messages_for`(那一份只给 role/content/created_at)——
        # 运维台读的是 `annotation_rows` / `conversation_meta` 这一份。
        metas = [
            world.chat_state.conversation_meta(conversation["id"])
            for conversation in world.conversations("夏")
        ]
        assert metas, "她一句话都没说,这条测试验不了"
        assert any(m.get("stances") or m.get("stance") for m in metas), (
            f"她声明了姿态,消息行上却一个字没记:{metas!r}"
        )


# ── 一次一问(3.7.0,看板 D5)────────────────────────────────────────────────


def test_一次一问_说完就退_不读stdin(tmp_path):
    """**这条门存在的理由是"驱动 REPL 脆在排版上"。**

    创作台的"一键试玩 / 性格试镜"从 2026-07-27 起卡在这条上:它只能喂 stdin、
    按提示符切 stdout,而抬头、降级提示、`名字 > ` 那几段任何一版换了样子,
    子进程那一侧就切错 —— **而它不会报错,只会把半句抬头当成她说的话。**

    所以这里钉的是两件:说完真的退(**不读一个字的 stdin**),以及那一轮真的
    进了世界(和 REPL 那条路共用 `record_chat_turn`,不是另写一条)。
    """
    db = _a_world(tmp_path)
    # ⚠️ stdin 里塞一句话 —— **它一个字都不该被读到**。喂空串的话,
    # "说完就退"和"读到 EOF 才退"这两种实现给出同一个答案。
    result = _chat(db, "--agent", "夏", "-m", "在吗", stdin="这句不许被读到\n")
    assert result.returncode == 0, result.stderr
    assert "苏晚夏 >" in result.stdout, result.stdout

    with open_world_at(db, force_mock_llm=True) as world:
        conversations = world.conversations("夏")
        assert len(conversations) == 1, "一次一问也是一轮一记,不是退出时才补一笔"
        said = {
            message["content"]
            for message in world.conversation_messages(conversations[0]["id"])
            if message["role"] == "user"
        }
        assert said == {"在吗"}, said


def test_一次一问_可重复_而且共用同一份转录(tmp_path):
    """给三句就是三轮,顺序不许乱。**共用一份进程内转录** —— 各喂各的话,多轮的
    连贯性就没了(世界只收当轮有限历史,完整转录归宿主,这条一个字没变)。"""
    import json as _json

    db = _a_world(tmp_path)
    result = _chat(db, "--agent", "夏", "-m", "一", "-m", "二", "-m", "三", "--json")
    assert result.returncode == 0, result.stderr
    payload = _json.loads(result.stdout)
    assert [row["said"] for row in payload["turns"]] == ["一", "二", "三"]
    assert payload["agent_id"] == "夏" and payload["agent_name"] == "苏晚夏"
    # **降级绝不无声**:Mock 上她照样回得出话,而那几句是模板。
    assert payload["degraded_reason"], payload


def test_json那条路上_stdout只有那一份json(tmp_path):
    """混着印的话,调用方就得先剥壳 —— 而剥壳的写法迟早在某一版排版上碎掉,
    **那正是这道门要治的病本身**。

    ⚠️ 散场那行"这一场引擎记了 N 条警告"**仍然印,只是走 stderr** —— 静音它就把
    "收着不丢"换成了"丢掉",而丢掉是这个仓库最怕的那种。
    """
    import json as _json

    db = _a_world(tmp_path)
    result = _chat(db, "--agent", "夏", "-m", "喂", "--json")
    _json.loads(result.stdout)          # 整个 stdout 必须是一份 JSON,不许有壳
    assert "@" not in result.stdout, "抬头漏进 stdout 了"

    # 对照组(期望非 0 的那一条判据):不给 --json 时抬头照旧在,而且在 stdout 上。
    human = _chat(db, "--agent", "夏", "-m", "喂")
    assert "苏晚夏 @" in human.stdout, human.stdout
