"""关会话时那条摘要:她是谁、在哪,由世界说,不由模型猜。

真事故:在旧港的**咖啡店**跟夏聊完一场,关闭会话时生成的摘要里写着**酒吧**和
**酒保** —— 这个世界两样都没有。成因是喂给模型的转录只有 `user:` / `assistant:`
两个标签,而提示词里没有一句话禁止它补充转录里没有的人物与场所。

它不是印一下就完了:这条摘要进她的长期记忆,又进下一场对话的记忆块
(`past_summaries` →「你和对方过去的对话回顾」),于是一次幻觉被她当成既成事实
反复复述,而玩家永远查不出是哪一句造成的 —— 照跑、日志干净、给错东西。

所以这里断言的是**送进模型的那份 prompt**,不是模型吐出来的字:后者不确定,拿它
当闸只会得到一条时红时绿的测试。分两半验:
  - 引擎拼的那一半(事实 + 禁令)**作者覆盖模板也带着**;
  - 模板那一半照旧生效(语气与详略是作者的)。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from anima_world.chat_session import ChatSessionManager


class RecordingLLM:
    """记下最后一次拿到的 prompt;可以指定回什么,或者直接炸。"""

    def __init__(self, reply: str = "他们聊了聊天气。", boom: bool = False) -> None:
        self.reply = reply
        self.boom = boom
        self.prompts: list[list[dict[str, Any]]] = []

    async def complete(self, messages) -> str:
        self.prompts.append([dict(m) for m in messages])
        if self.boom:
            raise RuntimeError("模型不通")
        return self.reply

    async def stream(self, messages):  # pragma: no cover - 摘要只走 complete
        yield await self.complete(messages)

    @property
    def system(self) -> str:
        return self.prompts[-1][0]["content"]

    @property
    def transcript(self) -> str:
        return self.prompts[-1][-1]["content"]


class FakeStore:
    """只实现 `close_conversation` 用得到的那几个方法(转录存储的最小切片)。"""

    def __init__(self, conv: dict[str, Any], messages: list[dict[str, Any]]) -> None:
        self.conv = conv
        self.messages = messages
        self.closed_with: str | None = None

    def get(self, conversation_id: int):
        return dict(self.conv) if int(conversation_id) == int(self.conv["id"]) else None

    def messages_for(self, conversation_id: int):
        return [dict(m) for m in self.messages]

    def close(self, conversation_id: int, summary: str, ts: int) -> None:
        self.closed_with = summary
        self.conv["status"] = "closed"


class FixedPromptStore:
    def __init__(self, text: str) -> None:
        self.text = text

    def get(self, name: str, default: str | None = None) -> str:
        return self.text if name == "chat.session_summary" else (default or "")


def _conv(**over: Any) -> dict[str, Any]:
    row = {
        "id": 7,
        "agent_id": "夏",
        "status": "open",
        "started_at": 100,
        "closed_at": None,
        "summary": None,
        "message_count": 2,
        "participants": [
            {"id": "p1", "kind": "user", "name": "阿檀"},
            {"id": "夏", "kind": "agent"},
        ],
        "location": "cafe",
        "player_id": "p1",
    }
    row.update(over)
    return row


def _messages() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "今天忙吗", "created_at": 101},
        {"role": "assistant", "content": "还行，刚磨完一批豆子", "created_at": 102},
    ]


def _close(store: FakeStore, llm: RecordingLLM, prompt_store=None) -> list[dict]:
    events: list[dict] = []
    manager = ChatSessionManager(
        store=store, llm=llm, emit_event=events.append,
        clock=lambda: 200, prompt_store=prompt_store,
    )
    asyncio.run(manager.close_conversation(int(store.conv["id"])))
    return events


def test_summary_prompt_carries_who_and_where_from_the_world():
    """她是谁、对方叫什么、在哪 —— 三样都在提示词里,而且转录带真名字。"""
    store, llm = FakeStore(_conv(), _messages()), RecordingLLM()
    _close(store, llm)

    assert "夏" in llm.system
    assert "阿檀" in llm.system
    assert "cafe" in llm.system
    # 转录不再只有 `user:` / `assistant:`:那两个标签不告诉模型谁是谁,
    # 而"谁说的这句"正是它编身份时缺的那一样。
    assert "夏: 还行，刚磨完一批豆子" in llm.transcript
    assert "阿檀: 今天忙吗" in llm.transcript
    assert "assistant:" not in llm.transcript


def test_summary_prompt_forbids_inventing_people_and_places():
    """「不许编」是一句显式的禁令,不是靠模型自觉。"""
    store, llm = FakeStore(_conv(), _messages()), RecordingLLM()
    _close(store, llm)

    system = llm.system
    assert "不要写出其中没有出现过的人物、地点、场所、身份或职业" in system
    # 位置即权重:禁令是这份提示词的最后一句。
    assert system.rstrip().endswith("宁可少写。")


def test_author_template_cannot_delete_the_facts_or_the_guard():
    """作者覆盖 `chat.session_summary` 只改语气与详略,两头的闸还在。

    把「不许编」写进那个模板等于把闸交给作者:他一覆盖闸就没了,**而没了不报错**,
    只是从某一天起摘要又开始长出酒吧。
    """
    store, llm = FakeStore(_conv(), _messages()), RecordingLLM()
    _close(store, llm, prompt_store=FixedPromptStore("写三句，用力煽情。"))

    assert "写三句，用力煽情。" in llm.system      # 模板那一半照旧生效
    assert "夏" in llm.system and "cafe" in llm.system
    assert "不要写出其中没有出现过的人物、地点、场所、身份或职业" in llm.system


def test_missing_location_says_so_instead_of_leaving_a_hole():
    """没记地点就明说没有 —— 留空正是模型自己补一个地点的地方。"""
    store, llm = FakeStore(_conv(location=None), _messages()), RecordingLLM()
    _close(store, llm)

    assert "没有记录地点" in llm.system


def test_unnamed_player_falls_back_to_访客_never_to_the_player_id():
    """宿主没给显示名时退「访客」,**绝不退玩家 id**。

    空标签确实是模型自己补一个身份出来的地方,但 id 补的是另一种错:这条摘要进的是
    她的长期记忆,于是 `p1` / `player-3f9a2c` 会被她当成这个人的名字说出口 —— 线上
    `night-tide` 就是这么让玩家改名叫「旅人」的。**编一个泛称比漏一个 id 强**:
    「访客」一望而知是泛称,而 `p1` 看上去像个名字。
    """
    conv = _conv(participants=[{"id": "p1", "kind": "user"}, {"id": "夏", "kind": "agent"}])
    store, llm = FakeStore(conv, _messages()), RecordingLLM()
    _close(store, llm)

    assert "访客" in llm.system
    assert "访客: 今天忙吗" in llm.transcript
    assert "p1" not in llm.system and "p1" not in llm.transcript


def test_summary_failure_is_logged_not_swallowed(caplog):
    """降级不许无声:退回的模板摘要看上去完全正常,不说就查不出来。"""
    store, llm = FakeStore(_conv(), _messages()), RecordingLLM(boom=True)
    with caplog.at_level("WARNING", logger="anima_world.chat_session"):
        events = _close(store, llm)

    assert "退回模板摘要" in caplog.text
    assert events and events[0]["payload"]["summary"] == store.closed_with
    assert "共2条消息" in store.closed_with


def test_empty_summary_is_logged_too(caplog):
    """模型回了个空串也是降级 —— 它和抛异常的下场一模一样。"""
    store, llm = FakeStore(_conv(), _messages()), RecordingLLM(reply="   ")
    with caplog.at_level("WARNING", logger="anima_world.chat_session"):
        _close(store, llm)

    assert "退回模板摘要" in caplog.text


@pytest.fixture
def world(open_world, bare_seed):
    return open_world(world_file=bare_seed)


def test_real_close_path_grounds_the_summary(world):
    """走真路径(`record_chat_turn` → `close_conversation`),不是只调 `_summarize`。

    直接调内部函数的测试验不到接线:摘要用的 `location` 是转录行上的那个,而它
    由 `start_conversation` 从玩家此刻在哪抄下来 —— 那一段断了这里才会红。
    """
    world.player_move("p1", "cafe")
    llm = RecordingLLM(reply="夏和阿檀在店里聊了几句。")
    world.session_manager._llm = llm

    world.record_chat_turn("夏", "p1", [
        {"role": "user", "content": "今天忙吗"},
        {"role": "assistant", "content": "还行，刚磨完一批豆子"},
    ])

    assert llm.prompts, "关会话时应该真的问了一次模型"
    assert "夏" in llm.system
    # 地点是**人话**,不是 `cafe`:这条摘要进她的长期记忆和下一场的记忆块,
    # 一个漏进去的英文 id 会被她当成地名说出口(这一轮在提示词那一层修过同一个病)。
    assert "咖啡店" in llm.system
    assert "cafe" not in llm.system
    assert "不要写出其中没有出现过的人物、地点、场所、身份或职业" in llm.system
    assert "assistant:" not in llm.transcript
