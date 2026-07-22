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
    ) -> None:
        self._store = store
        self._llm = llm
        self._emit_event = emit_event
        self._idle_timeout = idle_timeout
        self._clock = clock or (lambda: int(time.time()))
        self._config_store = config_store
        self._prompt_store = prompt_store
        self._judge_hook = judge_hook

    async def close_conversation(self, conversation_id: int) -> bool:
        """Close a conversation. Returns True iff a `conversation` event was emitted."""
        conv = self._store.get(conversation_id)
        if conv is None or conv["status"] == "closed":
            return False

        ts = self._clock()
        messages = self._store.messages_for(conversation_id)
        if not messages:
            # Nothing was said — close quietly, no world event.
            self._store.close(conversation_id, summary="", ts=ts)
            return False

        summary = await self._summarize(conv, messages)
        self._store.close(conversation_id, summary=summary, ts=ts)
        participants = conv.get("participants") or []
        self._emit_event(
            {
                "type": "conversation",
                "who": conv["agent_id"],
                "ts": ts,
                "payload": {
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
                },
            }
        )
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

    async def _summarize(self, conv: dict[str, Any], messages: list[dict[str, Any]]) -> str:
        transcript = "\n".join(f'{m["role"]}: {m["content"]}' for m in messages)
        instruction = (
            self._prompt_store.get("chat.session_summary", default=_DEFAULT_SUMMARY_TEMPLATE)
            if self._prompt_store is not None
            else _DEFAULT_SUMMARY_TEMPLATE
        )
        prompt = [
            {
                "role": "system",
                "content": instruction,
            },
            {"role": "user", "content": transcript},
        ]
        try:
            summary = (await self._llm.complete(prompt)).strip()
            if summary:
                return summary
        except Exception:
            pass
        return self._template_summary(conv, messages)

    @staticmethod
    def _template_summary(conv: dict[str, Any], messages: list[dict[str, Any]]) -> str:
        return f'与{conv["agent_id"]}的一次对话，共{len(messages)}条消息'
