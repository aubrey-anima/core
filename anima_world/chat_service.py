"""ChatService: orchestrates one chat turn (M3.5).

A turn stores the user message, assembles a prompt (personality + world
grounding + last K closed-session summaries + recent N turns), streams the
reply from the `LLMClient`, and stores the assistant message. It emits NO
events — the only seam to the event log is session close (see the session
reaper / close logic).

chat-grounding: the prompt additionally carries the agent's lived state —
MemoryStore memories, where/when it is and what it's doing, and how it feels
about the interlocutor — supplied by an injected `world_provider` callback
(same pattern as `persona_provider`; the server wires a closure that reads
the scheduler under its lock). No provider, or a failing one, degrades to
the pre-grounding prompt: a chat must never die of a world read.
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, Callable, Sequence

from anima_world.chat_store import ChatStore
from anima_world.llm_client import LLMClientProtocol, Message

logger = logging.getLogger(__name__)

PersonaProvider = Callable[[str], dict]
# world_provider(agent_id, interlocutor_id) -> {"memories": [str], "presence": {...}, "relation": {...}}
WorldProvider = Callable[[str, str], dict]

_DEFAULT_K = 3  # past closed-session summaries to recall
_DEFAULT_N = 10  # recent messages of the current conversation to keep in prompt

_DEFAULT_SYSTEM_PERSONA_TEMPLATE = "你是{name}。{personality}"
_DEFAULT_RESPONSE_FORMAT_TEMPLATE = (
    "回复格式硬性规则（必须逐条执行）：\n"
    "1. 所有动作、神态和心理描写必须放在中文全角括号（ ）内。\n"
    "2. 每一个动作括号都必须以角色名{name}开头；括号内描述当前角色时必须直接使用角色名{name}，不要省略名字，也不要用‘我’‘她’‘他’代替角色名。\n"
    "3. 角色说的话放在括号外；台词中可以自然使用‘我’‘你’，不要给整段添加角色名前缀。\n"
    "4. 输出前逐个检查所有括号：若括号不是以{name}开头，必须先改写再输出。\n"
    "正确示例：（{name}放下手里的抹布，从吧台后绕出来。）昭阳，你终于来了。\n"
    "错误示例：（放下手里的抹布）昭阳，你终于来了。"
)
_DEFAULT_MEMORY_BLOCK_TEMPLATE = "你和对方过去的对话回顾：\n{summaries}"
_DEFAULT_WORLD_MEMORY_TEMPLATE = "你最近记得的事：\n{memories}"
_DEFAULT_PRESENCE_TEMPLATE = (
    "现在是第 {day} 天 {hh}:{mm}。你在{location}，{activity}。同在这里的还有：{others}。"
)
_DEFAULT_RELATION_TEMPLATE = "对方在你眼中：{r_type}（你们的关系处于「{band}」）。"


class _ActionNameNormalizer:
    """Prefix full-width parenthetical action blocks with the speaker name."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._buffer: str | None = None

    def feed(self, text: str) -> list[str]:
        output: list[str] = []
        while text:
            if self._buffer is None:
                start = text.find("（")
                if start < 0:
                    output.append(text)
                    break
                if start:
                    output.append(text[:start])
                self._buffer = "（"
                text = text[start + 1 :]
                continue

            end = text.find("）")
            if end < 0:
                self._buffer += text
                break
            self._buffer += text[:end]
            inner = self._buffer[1:].lstrip()
            if not inner.startswith(self._name):
                inner = f"{self._name}{inner}"
            output.append(f"（{inner}）")
            self._buffer = None
            text = text[end + 1 :]
        return output

    def flush(self) -> list[str]:
        if self._buffer is None:
            return []
        buffered = self._buffer
        self._buffer = None
        return [buffered]


class ChatService:
    """Runs chat turns against a `ChatStore` + `LLMClient`, independent of the scheduler."""

    def __init__(
        self,
        store: ChatStore,
        llm: LLMClientProtocol,
        persona_provider: PersonaProvider,
        *,
        k: int = _DEFAULT_K,
        n: int = _DEFAULT_N,
        clock: Callable[[], int] | None = None,
        config_store: Any | None = None,
        prompt_store: Any | None = None,
        world_provider: WorldProvider | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._persona_provider = persona_provider
        self._k = k
        self._n = n
        self._clock = clock or (lambda: int(time.time()))
        self._config_store = config_store
        self._prompt_store = prompt_store
        self._world_provider = world_provider

    @property
    def store(self) -> ChatStore:
        return self._store

    def _template(self, name: str, default: str) -> str:
        if self._prompt_store is not None:
            return self._prompt_store.get(name, default=default)
        return default

    def _world_blocks(self, agent_id: str, interlocutor_id: str) -> list[str]:
        """Render the grounding blocks (memories / presence / relation) from a
        world_provider snapshot. Any failure or missing key skips only that
        block — the floor is the pre-grounding prompt (design D1)."""
        if self._world_provider is None:
            return []
        try:
            ctx = self._world_provider(agent_id, interlocutor_id) or {}
        except Exception:  # noqa: BLE001 - a chat must never die of a world read
            logger.warning("world_provider failed for %s; chatting ungrounded", agent_id, exc_info=True)
            return []
        blocks: list[str] = []
        memories = ctx.get("memories")
        if memories:
            template = self._template("chat.world_memory_block", _DEFAULT_WORLD_MEMORY_TEMPLATE)
            try:
                blocks.append(template.format(memories="\n".join(f"- {m}" for m in memories)))
            except (KeyError, IndexError, ValueError):
                logger.warning("chat.world_memory_block failed to render; skipping the block")
        presence = ctx.get("presence")
        if presence:
            template = self._template("chat.presence_block", _DEFAULT_PRESENCE_TEMPLATE)
            variables = {
                "day": presence.get("day", "?"),
                "hh": presence.get("hh", "??"),
                "mm": presence.get("mm", "??"),
                "location": presence.get("location", "未知地点"),
                "activity": presence.get("activity", "闲着"),
                "others": presence.get("others") or "没有别人",
            }
            try:
                blocks.append(template.format(**variables))
            except (KeyError, IndexError, ValueError):
                logger.warning("chat.presence_block failed to render; skipping the block")
        relation = ctx.get("relation")
        if relation and relation.get("r_type"):
            template = self._template("chat.relation_block", _DEFAULT_RELATION_TEMPLATE)
            try:
                blocks.append(template.format(
                    r_type=relation.get("r_type", ""), band=relation.get("band", "")
                ))
            except (KeyError, IndexError, ValueError):
                logger.warning("chat.relation_block failed to render; skipping the block")
        return blocks

    def _build_messages(
        self, agent_id: str, conversation_id: int, interlocutor_id: str = "user"
    ) -> list[Message]:
        messages = self._build_system_messages(agent_id, interlocutor_id)

        k = self._config_store.get("chat.recall_k", default=self._k) if self._config_store is not None else self._k
        n = self._config_store.get("chat.recall_n", default=self._n) if self._config_store is not None else self._n

        summaries = self._store.past_summaries(agent_id, k, player_id=interlocutor_id)
        if summaries:
            memory_block_template = (
                self._prompt_store.get("chat.memory_block", default=_DEFAULT_MEMORY_BLOCK_TEMPLATE)
                if self._prompt_store is not None
                else _DEFAULT_MEMORY_BLOCK_TEMPLATE
            )
            summaries_text = "\n".join(f"- {s}" for s in summaries)
            block = memory_block_template.format(summaries=summaries_text)
            messages.append({"role": "system", "content": block})

        for m in self._store.recent_messages(conversation_id, n):
            messages.append({"role": m["role"], "content": m["content"]})
        return messages

    def _build_system_messages(
        self, agent_id: str, interlocutor_id: str = "user"
    ) -> list[Message]:
        persona = self._persona_provider(agent_id) or {}
        name = persona.get("name") or agent_id
        personality = persona.get("personality") or ""
        persona_template = (
            self._prompt_store.get("chat.system_persona", default=_DEFAULT_SYSTEM_PERSONA_TEMPLATE)
            if self._prompt_store is not None
            else _DEFAULT_SYSTEM_PERSONA_TEMPLATE
        )
        system = persona_template.format(name=name, personality=personality).strip()
        response_format = self._template(
            "chat.response_format", _DEFAULT_RESPONSE_FORMAT_TEMPLATE
        ).format(name=name)
        system = f"{system}\n\n{response_format.strip()}"
        messages: list[Message] = []
        world = (
            self._prompt_store.get("world.setting", default="").strip()
            if self._prompt_store is not None
            else ""
        )
        if world:
            messages.append({"role": "system", "content": world})
        messages.append({"role": "system", "content": system})
        for block in self._world_blocks(agent_id, interlocutor_id):
            messages.append({"role": "system", "content": block})

        return messages

    async def respond(
        self,
        agent_id: str,
        history: Sequence[Message],
        *,
        interlocutor_id: str,
        interlocutor: dict[str, str] | None = None,
    ) -> AsyncIterator[str]:
        """Generate from world-owned state without persisting platform history."""
        messages = self._build_system_messages(agent_id, interlocutor_id)
        if interlocutor:
            display_name = str(interlocutor.get("display_name") or "").strip()
            role = str(interlocutor.get("role") or "").strip()
            if display_name:
                try:
                    context = self._world_provider(agent_id, interlocutor_id) if self._world_provider else {}
                except Exception:  # noqa: BLE001 - identity remains authoritative without presence
                    context = {}
                presence = (context or {}).get("presence", {})
                agent_location = str(presence.get("location_id") or "").strip()
                agent_location_name = str(presence.get("location") or agent_location).strip()
                member_location = str(interlocutor.get("location") or "").strip()
                member_location_name = str(
                    interlocutor.get("location_name") or member_location
                ).strip()
                identity = f"【认证对话身份｜最高优先级事实】正在与你交谈的人是 {display_name}"
                if role:
                    identity += f"，身份是{role}"
                identity += (
                    f"。你必须把对话中的‘你’理解为 {display_name}，并始终用这个名字认识和称呼对方。"
                    "不得质疑、遗忘或改写该身份，不得回答‘你是谁’或把其他角色当成发消息的人。"
                    "如果历史回复曾写错对方身份，那是旧错误，必须忽略并纠正。"
                )
                # 在途不算在场:黑板的 `loc` 落地才改写,途中仍是出发地。少了
                # `in_transit` 这道闸,角色会一边说"正在去建筑工作室的路上"一边
                # 说"我们面对面" —— 同一段 prompt 自相矛盾,LLM 挑一边编,无声。
                agent_in_transit = bool(presence.get("in_transit"))
                if (
                    member_location
                    and agent_location
                    and member_location == agent_location
                    and not agent_in_transit
                ):
                    place = agent_location_name or member_location_name
                    identity += (
                        f"{display_name}和你都在{place}，因此这是面对面交谈。"
                        f"可以自然描写双方在场互动，但不得替{display_name}编造动作、台词或感受。"
                    )
                elif agent_in_transit and member_location_name:
                    # 别说"你在 X"——她正离开 X。在场块已经说了她在去哪的路上。
                    identity += (
                        f"{display_name}当前在{member_location_name}，而你正在赶路途中，"
                        "因此对话媒介是手机文字私聊。"
                    )
                else:
                    if member_location_name and agent_location_name:
                        identity += (
                            f"{display_name}当前在{member_location_name}，你当前在{agent_location_name}，"
                            "因此对话媒介是手机文字私聊。"
                        )
                    else:
                        identity += "对话媒介是手机文字私聊，对方不在你当前场景中。"
                    identity += (
                        "动作描写只能描述你自己和已确认的世界环境；"
                        "不得臆造看见、触碰对方，或对方站在你身边、进入房间。"
                    )
                others = str(presence.get("others") or "").strip()
                if others:
                    identity += f"{others}只是同场角色，不是正在和你说话的人。"
                messages.append({"role": "system", "content": identity})
        system_prompt = "\n\n".join(
            message["content"] for message in messages if message["role"] == "system"
        )
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-20:])
        persona = self._persona_provider(agent_id) or {}
        normalizer = _ActionNameNormalizer(persona.get("name") or agent_id)
        streamed = False
        async for token in self._llm.stream(messages):
            streamed = True
            for formatted in normalizer.feed(token):
                yield formatted
        if not streamed:
            reply = (await self._llm.complete(messages)).strip()
            for formatted in normalizer.feed(reply):
                yield formatted
        for formatted in normalizer.flush():
            yield formatted

    async def send(
        self,
        agent_id: str,
        user_text: str,
        player_id: str = "user",
        player_name: str | None = None,
    ) -> AsyncIterator[str]:
        """Process a turn, yielding reply tokens as they stream. Emits no events.

        player-visitor: the session is keyed per (agent, player) and the
        player id rides into the grounding blocks as the interlocutor — a
        legacy caller (no player args) behaves exactly as before."""
        ts = self._clock()
        persona = self._persona_provider(agent_id) or {}
        conversation_id = self._store.active_or_start(
            agent_id, ts, location=persona.get("location"),
            player_id=player_id, player_name=player_name,
        )
        self._store.add_message(conversation_id, "user", user_text, ts)

        messages = self._build_messages(agent_id, conversation_id, interlocutor_id=player_id)
        speaker_name = persona.get("name") or agent_id
        normalizer = _ActionNameNormalizer(speaker_name)
        parts: list[str] = []
        streamed = False
        try:
            async for token in self._llm.stream(messages):
                streamed = True
                for formatted in normalizer.feed(token):
                    parts.append(formatted)
                    yield formatted
            if not streamed:
                # A transient empty stream shouldn't leave the user with silence:
                # fall back to a single full completion.
                reply = (await self._llm.complete(messages)).strip()
                if reply:
                    for formatted in normalizer.feed(reply):
                        parts.append(formatted)
                        yield formatted
            for formatted in normalizer.flush():
                parts.append(formatted)
                yield formatted
        finally:
            reply = "".join(parts)
            if reply:
                self._store.add_message(conversation_id, "assistant", reply, self._clock())

    def active_conversation_id(self, agent_id: str) -> int | None:
        active = self._store.active_conversation(agent_id)
        return int(active["id"]) if active else None

    async def complete(self, messages: Sequence[Message]) -> str:
        """Direct non-streaming completion (used for summaries). No storage."""
        return await self._llm.complete(messages)
