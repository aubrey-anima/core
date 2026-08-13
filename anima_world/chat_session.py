"""Session lifecycle: the single seam between chat and the event log (M3.5).

`close_conversation` is called by both close triggers — the idle reaper and the
manual close endpoint. It generates a summary (LLM, with a template fallback),
closes the conversation in the store, and emits exactly ONE `conversation`
event. A zero-message conversation closes without emitting an event.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from anima_world.chat_store import ChatStore
from anima_world.llm_client import LLMClientProtocol

logger = logging.getLogger(__name__)

EmitEvent = Callable[[dict[str, Any]], Any]
# judge_hook({"agent_id", "player_id", "player_name", "transcript"}) — fired on
# close so the player conversation gets a relationship verdict (player-visitor).
JudgeHook = Callable[[dict[str, Any]], Any]

_DEFAULT_IDLE_TIMEOUT = 600  # seconds of inactivity before a session auto-closes
_DEFAULT_SUMMARY_TEMPLATE = "用一句中文概括这次对话的主要内容和情绪基调。只输出摘要，不要解释。"

# ── 摘要提示词的两半,以及边界为什么划在这儿 ────────────────────────────────
#
# **引擎拼「谁是谁、在哪、不许编」,作者的模板只管语气与详略。**
#
# 这条摘要不是印一下就完了:它进她的长期记忆(重要度 0.8 那一档),又进下一场对话
# 的记忆块(`chat_service` 的 `past_summaries` → 「你和对方过去的对话回顾」)。所以
# 一次幻觉会被她当成既成事实反复复述,而玩家永远查不出是哪一句造成的 —— 真事:在
# 旧港的**咖啡店**跟夏聊完,摘要里写着**酒吧**和**酒保**,这个世界两样都没有。
# 成因是这一层原本只喂给模型 `user:` / `assistant:` 两个标签的转录:谁是谁、在哪、
# 什么身份,模型只能猜,猜出来的就是酒保。
#
# 于是事实与禁令由引擎拼,**不能只写进 `chat.session_summary`** —— 那个模板是作者
# 可覆盖的,作者一覆盖闸就没了,**而没了不报错**,只是从某一天起摘要又开始长出酒吧。
# 留给模板的是它真正独有的那部分:一句话还是三句话、什么口气、要不要带情绪基调。
_SUMMARY_FACT_HEADER = "以下是这场对话的事实，由世界给出，不可更改："
_SUMMARY_GUARD = (
    "只依据上面的事实和下面的转录写摘要：不要写出其中没有出现过的人物、地点、"
    "场所、身份或职业，也不要替谁补上没说过的话。拿不准就略过，宁可少写。"
)


class ChatSessionManager:
    """Closes chat sessions and emits the one summary event per closed session."""

    def __init__(
        self,
        store: ChatStore,
        llm: LLMClientProtocol,
        emit_event: EmitEvent,
        *,
        idle_timeout: int = _DEFAULT_IDLE_TIMEOUT,
        clock: Callable[[], int] | None = None,
        config_store: Any | None = None,
        prompt_store: Any | None = None,
        judge_hook: JudgeHook | None = None,
        meta_provider: Callable[[int], dict[str, Any]] | None = None,
        place_name: Callable[[str], str] | None = None,
        agent_name: Callable[[str], str] | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._emit_event = emit_event
        self._idle_timeout = idle_timeout
        self._clock = clock or (lambda: int(time.time()))
        self._config_store = config_store
        self._prompt_store = prompt_store
        self._judge_hook = judge_hook
        # chat-agent:整场会话的 stance / intent 分布 + 调过的工具。**只在关闭时
        # 出去一次** —— 每轮一个事件等于把聊天转录搬进世界的历史,而那条不变量
        # (聊天子系统与事件核解耦)是这个子系统存在的前提。
        self._meta_provider = meta_provider
        # 地点 id → 人话名。摘要**进的是她的长期记忆和下一场的记忆块**,所以一个
        # 漏进去的 `cafe` 不是印错一次:她从此会把这个英文 id 当成地名说出口。
        # 接不上的世界照旧用 id —— 那仍然比空着强(空着正是模型自己补一个的地方)。
        self._place_name = place_name
        # 角色 id → 人话名,同一条理由的另一半。地点那一半早就接上了,她自己这一半
        # 没有:转录行上只有 `agent_id`,于是摘要写的是「店主 wan 表示唱机转但不稳」,
        # 而这条摘要进她的长期记忆、进下一场的记忆块、还被八卦原样转述。
        self._agent_name = agent_name

    async def close_conversation(self, conversation_id: int) -> bool:
        """Close a conversation. Returns True iff a `conversation` event was emitted."""
        conv = self._store.get(conversation_id)
        if conv is None or conv["status"] == "closed":
            return False

        # 墙钟,而且**只给转录用**:会话的开/关/闲置回收是按秒算的(`chat.idle_timeout`
        # 是秒),`started_at` / `last_activity_at` 也都存的是秒。世界事件的 `ts` 不走
        # 这个钟 —— 见下面那一段。
        ts = self._clock()
        messages = self._store.messages_for(conversation_id)
        if not messages:
            # Nothing was said — close quietly, no world event.
            self._store.close(conversation_id, summary="", ts=ts)
            return False

        summary = await self._summarize(conv, messages)
        self._store.close(conversation_id, summary=summary, ts=ts)
        participants = conv.get("participants") or []
        payload: dict[str, Any] = {
            "agent_id": conv["agent_id"],
            "conversation_id": conversation_id,
            "summary": summary,
            "message_count": conv["message_count"],
            "started_at": conv["started_at"],
            "closed_at": ts,
            # player-visitor: who was in the room, and where — the
            # event finally names the human side of the conversation.
            "participants": participants,
            "location": conv.get("location"),
        }
        if self._meta_provider is not None:
            try:
                payload.update(self._meta_provider(conversation_id) or {})
            except Exception:  # noqa: BLE001 - 观测量读不到不该挡住关闭
                logger.warning("读会话观测量失败 conversation=%s", conversation_id, exc_info=True)
        # **不盖 `ts`** —— 让 `Scheduler._record_event` 的
        # `event.setdefault("ts", self.clock)` 盖世界时钟,和引擎里**其余每一条
        # 事件**同一条路。此前这里盖的是墙钟(`int(time.time())`),于是这一条
        # 是全引擎唯一一条带着 1.78e9 的活事件,而它的下场没有一处是报错:
        #   - `TriggerEngine._on_conversation` 把 ts 抄进记忆的 `tick`,
        #     `MemoryStore.query` 按 `(tick, id)` DESC 排 —— 于是**每个角色召回
        #     的前 k 条永远是跟玩家的对话**,占比 5% 的东西吃掉了 100% 的位置;
        #   - `memory_retrieval.score` 的 `age = now_tick - tick` 变成负数被
        #     `max(0, …)` 夹成 0 → recency 恒等于 1(满分);
        #   - `decayed_strength` 的 idle_days 同样恒为 0 → 它永不衰减,而同期
        #     真正的世界记忆衰减到 1e-160。
        # 三样都"照跑",一样都不报错。此前的补法是在每个消费方前面加一道
        # `WALL_CLOCK_FLOOR` 闸(时钟恢复、运行摘要各一道),而记忆这一路漏了 ——
        # 补闸就要补到每一个消费方,漏一个就是这次的样子。所以改在源头。
        # `payload` 里的 `started_at` / `closed_at` **照旧是墙钟**:它们是从转录行上
        # 抄下来的,那一层本来就按秒记账,把其中一个换成 tick 才是把两套时基混进
        # 同一个 payload。
        self._emit_event({
            "type": "conversation",
            "who": conv["agent_id"],
            "payload": payload,
        })
        if self._judge_hook is not None:
            user = next((p for p in participants if p.get("kind") == "user"), None)
            try:
                self._judge_hook({
                    "agent_id": conv["agent_id"],
                    "player_id": (user or {}).get("id") or conv.get("player_id") or "user",
                    "player_name": (user or {}).get("name"),
                    "transcript": [
                        {"role": m["role"], "content": m["content"]} for m in messages
                    ],
                    # **出处跟着判定一起走。** 判定落地成一条 `sentiment_delta`,
                    # 而那条事件此前只带着一个 delta —— 于是"是哪一场对话让它变的"
                    # 在日志里查不回来。这两样在这儿都在手上,不传等于让下游
                    # 去猜(而它猜的办法只能是按时间就近认一条,认错了不报错)。
                    "conversation_id": conversation_id,
                    "conversation_summary": summary,
                })
            except Exception:  # noqa: BLE001 - a judge failure must never block a close
                logger.warning("judge_hook failed for conversation %s", conversation_id, exc_info=True)
        return True

    async def reap_idle(self) -> list[int]:
        """Close every conversation idle past the timeout. Returns closed ids."""
        now = self._clock()
        idle_timeout = (
            self._config_store.get("chat.idle_timeout", default=self._idle_timeout)
            if self._config_store is not None
            else self._idle_timeout
        )
        closed: list[int] = []
        for conv in self._store.idle_open_conversations(now, idle_timeout):
            if await self.close_conversation(int(conv["id"])):
                closed.append(int(conv["id"]))
        return closed

    async def reap_orphans(self) -> list[int]:
        """Close EVERY open conversation, ignoring the idle timeout.

        Only correct at boot: conversations open and close inside a single
        `record_chat_turn` call, and a running world owns its db exclusively,
        so on a freshly opened World any 'open' row is a crash leftover. The
        messages are already durable — closing here turns "崩溃丢总结" into
        "总结晚到", the same one-event contract as a normal close.
        """
        now = self._clock()
        closed: list[int] = []
        for conv in self._store.idle_open_conversations(now, 0):
            if await self.close_conversation(int(conv["id"])):
                closed.append(int(conv["id"]))
        return closed

    def _speaker_labels(self, conv: dict[str, Any]) -> tuple[str, str]:
        """(她叫什么, 对方叫什么) —— **只认转录行上真有的字段,不猜**。

        名字优先取 `participants` 上的(`chat()` 那条路把玩家的显示名记了进来)。

        ⚠️ **取不到名字时退的是「访客」,不是玩家 id。** 空标签确实是模型自己补一个
        身份出来的地方,但 id 补的是**另一种**错:这条摘要进的是她的长期记忆,于是
        `p1` / `player-3f9a2c` 会被她当成这个人的名字说出口 —— 线上 `night-tide`
        就是这么让玩家改名叫「旅人」的。**编一个泛称比漏一个 id 强**,因为「访客」
        一望而知是泛称,而 `p1` 看上去像个名字。

        ⚠️ **而她自己那一头,这条纪律上面写着、下面没做**:兜底直接是 `agent_id`,
        于是线上摘要里躺着「店主 wan 表示唱机转但不稳」「yun 因江堤上没有树荫……」,
        八卦再把它转述出去。最离谱的一条是她读完之后自己写下的:
        「隐约对那个 'wan' 有点好奇」—— 她对一个字符串产生了好奇。名册查得到就用
        名册(和地点那一半同一个做法),查不到才退泛称「她」。
        """
        agent_id = str(conv.get("agent_id") or "").strip()
        agent_label = ""
        if agent_id and self._agent_name is not None:
            try:
                agent_label = str(self._agent_name(agent_id) or "").strip()
            except Exception:  # noqa: BLE001 - 查不到名字不该让一场会话关不掉
                logger.warning("摘要里的角色 %s 翻不成人话", agent_id, exc_info=True)
        if agent_label == agent_id:
            agent_label = ""   # 名册没这个人时回落的就是 id 本身
        agent_label = agent_label or "她"
        player_label = ""
        for participant in conv.get("participants") or []:
            kind = participant.get("kind")
            name = str(participant.get("name") or "").strip()
            if kind == "agent" and name:
                agent_label = name
            elif kind == "user" and name:
                player_label = name
        return agent_label, player_label or "访客"

    def _summary_facts(
        self, conv: dict[str, Any], agent_label: str, player_label: str
    ) -> list[str]:
        facts = [
            f"- 说话的角色是{agent_label}，这个世界里的人。",
            f"- 和她说话的是{player_label}。",
        ]
        # 转录行上存的是地点 **id**(`cafe`),而摘要要进她的长期记忆和下一场的
        # 记忆块 —— 一个漏进去的英文 id 会被她当成地名说出口(这一轮已经在提示词
        # 那一层修过同一个病)。所以宿主给了译名就用译名;没给才退回 id,而 id
        # 仍然比空着强:空着正是模型自己补一个地点的地方。
        location = str(conv.get("location") or "").strip()
        if location:
            if self._place_name is not None:
                try:
                    location = str(self._place_name(location) or location).strip() or location
                except Exception:  # noqa: BLE001 - 查不到地名不该让一场会话关不掉
                    logger.warning("摘要里的地点 %s 翻不成人话", location, exc_info=True)
            facts.append(f"- 地点：{location}。")
        else:
            # 没记地点就明说没有 —— **留空正是模型自己补一个地点的地方**,
            # 而补出来的那个会被她当成去过的地方反复复述。
            facts.append("- 这场对话没有记录地点，所以摘要里不要写地点。")
        return facts

    async def _summarize(self, conv: dict[str, Any], messages: list[dict[str, Any]]) -> str:
        agent_label, player_label = self._speaker_labels(conv)
        # 转录的标签换成真名字:`user:` / `assistant:` 不告诉模型谁是谁,
        # 而"谁说的这句"正是它编身份时缺的那一样。
        speakers = {"assistant": agent_label, "user": player_label}
        transcript = "\n".join(
            f'{speakers.get(m["role"], m["role"])}: {m["content"]}' for m in messages
        )
        instruction = (
            self._prompt_store.get("chat.session_summary", default=_DEFAULT_SUMMARY_TEMPLATE)
            if self._prompt_store is not None
            else _DEFAULT_SUMMARY_TEMPLATE
        )
        # 事实在前、作者的模板在中、禁令在末:模板夹在中间就改不掉两头,而**位置即
        # 权重** —— 最后一句是这份提示词里最重的一句,禁令该待在那儿。
        system = "\n".join([
            _SUMMARY_FACT_HEADER,
            *self._summary_facts(conv, agent_label, player_label),
            "",
            instruction,
            "",
            _SUMMARY_GUARD,
        ])
        prompt = [
            {"role": "system", "content": system},
            {"role": "user", "content": transcript},
        ]
        try:
            summary = (await self._llm.complete(prompt)).strip()
            if summary:
                return summary
            # 降级不许无声(和 llm degraded_reason 同一条纪律):退回模板摘要之后
            # 世界里躺着一条「与夏的一次对话,共 4 条消息」,它看上去完全正常。
            logger.warning("会话 %s 的摘要为空,退回模板摘要", conv.get("id"))
        except Exception:  # noqa: BLE001 - 摘要失败不该挡住关闭
            logger.warning(
                "会话 %s 的摘要生成失败,退回模板摘要", conv.get("id"), exc_info=True
            )
        return self._template_summary(conv, messages)

    @staticmethod
    def _template_summary(conv: dict[str, Any], messages: list[dict[str, Any]]) -> str:
        return f'与{conv["agent_id"]}的一次对话，共{len(messages)}条消息'
