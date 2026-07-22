"""Narrative provider: turns agent actions into human-readable text.

Pluggable: default is MockNarrativeProvider (offline, deterministic).
Override with an LLM-backed provider when narrative.enabled + API key exist.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol, runtime_checkable

_DEFAULT_DESCRIBE_TEMPLATE = (
    "请用中文生成一句适合作为这个 agent 的世界叙事或聊天回复。只输出正文，不要解释。"
)


@runtime_checkable
class NarrativeProvider(Protocol):
    """A narrative provider describes agent actions in human text."""

    def describe(
        self,
        action: dict[str, Any],
        agent_id: str,
        blackboard: dict[str, Any],
    ) -> str:
        ...


class MockNarrativeProvider:
    """Default offline stub: deterministic one-line summaries for each action kind."""

    _TEMPLATES: dict[str, str] = {
        "walk": "{agent} walked to {location}",
        "chat": "{agent} chatted with {target}",
        "work": "{agent} worked at {location}",
        "sleep": "{agent} went to sleep",
        "idle_wander": "{agent} wandered around",
        "idle_social": "{agent} looked for someone to talk to",
        "custom": "{agent} did something custom",
    }

    def __init__(self, memory_store: Any | None = None) -> None:
        self.memory_store = memory_store

    def describe(
        self,
        action: dict[str, Any],
        agent_id: str,
        blackboard: dict[str, Any],
    ) -> str:
        kind = action.get("kind", "custom")
        params = action.get("params", {})
        template = self._TEMPLATES.get(kind, self._TEMPLATES["custom"])
        # Fill from params; fall back to "{k}" when missing
        kwargs = {k: params.get(k, "{" + k + "}") for k in ("location", "target")}
        text = template.format(agent=agent_id, **kwargs)
        memory_suffix = self._memory_suffix(agent_id)
        return text + memory_suffix if memory_suffix else text

    def _memory_suffix(self, agent_id: str) -> str:
        """Past-tense-framed nod to the agent's most recent memory (M4 §7)."""
        if self.memory_store is None:
            return ""
        memories = self.memory_store.query(agent_id=agent_id)
        if not memories:
            return ""
        return f"——还记着{memories[0]['summary']}"


class OpenAICompatibleNarrativeProvider:
    """Narrative provider backed by an OpenAI-compatible chat completions API.

    Chinese model vendors commonly expose OpenAI-compatible endpoints. Configure
    `base_url` to the vendor's `/v1` root and `model` to the vendor model id.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 20.0,
        temperature: float = 0.7,
        fallback: NarrativeProvider | None = None,
        post_json: Any | None = None,
        memory_store: Any | None = None,
        prompt_store: Any | None = None,
        config_store: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.fallback = fallback or MockNarrativeProvider()
        self._post_json = post_json or _post_json
        self.memory_store = memory_store
        self.prompt_store = prompt_store
        # M5: when given, llm.* connection settings are read live on every
        # describe() call instead of frozen at construction (design.md D3) —
        # mirrors ConfigBackedLLMClient's live-read pattern for the chat path.
        self.config_store = config_store

    def _resolved_settings(self) -> tuple[str, str, str, float]:
        cfg = self.config_store
        if cfg is None:
            return self.api_key, self.base_url, self.model, self.timeout
        # No `or self.api_key` fallback here: an empty string from cfg is a
        # deliberate "key cleared" signal (an admin blanking it via
        # World.config_set) and must fall through to the mock provider below,
        # not silently resurrect the stale constructor-time key.
        api_key = cfg.get("llm.api_key", default=self.api_key)
        base_url = (cfg.get("llm.base_url", default=self.base_url) or self.base_url).rstrip("/")
        model = cfg.get("llm.model", default=self.model)
        timeout = cfg.get("llm.timeout", default=self.timeout)
        return api_key, base_url, model, timeout

    def describe(
        self,
        action: dict[str, Any],
        agent_id: str,
        blackboard: dict[str, Any],
    ) -> str:
        api_key, base_url, model, timeout = self._resolved_settings()
        if not api_key:
            return self.fallback.describe(action, agent_id, blackboard)
        payload = {
            "model": model,
            "messages": self._messages(action, agent_id, blackboard),
            "temperature": self.temperature,
            **_disable_thinking_payload(base_url),
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            data = self._post_json(self._chat_completions_url(base_url), headers, payload, timeout)
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, str) and content.strip():
                return content.strip()
        except (KeyError, IndexError, TimeoutError, TypeError, ValueError, urllib.error.URLError):
            pass
        return self.fallback.describe(action, agent_id, blackboard)

    def _chat_completions_url(self, base_url: str) -> str:
        return _chat_completions_url(base_url)

    def _messages(
        self,
        action: dict[str, Any],
        agent_id: str,
        blackboard: dict[str, Any],
    ) -> list[dict[str, str]]:
        kind = action.get("kind", "custom")
        params = action.get("params", {})
        raw = blackboard.get("raw", {}) if isinstance(blackboard, dict) else {}
        personality = raw.get("personality") or raw.get("state.personality") or "自然、简洁、有角色感"
        user_input = params.get("user_input") or params.get("text") or params.get("reply") or ""
        # prompt-grounding: without the agent's whereabouts the model set its
        # "slice of life" text in whatever scenery the worldview suggested
        location = raw.get("loc") or ""
        location_line = f"location={location}\n" if location else ""
        instruction = (
            self.prompt_store.get("narrative.describe", default=_DEFAULT_DESCRIBE_TEMPLATE)
            if self.prompt_store is not None
            else _DEFAULT_DESCRIBE_TEMPLATE
        )
        prompt = (
            f"agent_id={agent_id}\n"
            f"action_kind={kind}\n"
            f"action_params={json.dumps(params, ensure_ascii=False)}\n"
            f"{location_line}"
            f"personality={personality}\n"
            f"user_message={user_input}\n\n"
            f"{instruction}"
            f"{self._memory_context(agent_id)}"
        )
        system = "你是赛博世界里的叙事引擎，负责生成简短、自然、可直接展示的中文文本。"
        world = (
            self.prompt_store.get("world.setting", default="").strip()
            if self.prompt_store is not None
            else ""
        )
        if world:
            system = f"{system}\n\n{world}"
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

    def _memory_context(self, agent_id: str, k: int = 3) -> str:
        """Recent-memories addendum for the prompt (M4 §7). Empty when unset/empty."""
        if self.memory_store is None:
            return ""
        memories = self.memory_store.query(agent_id=agent_id)[:k]
        if not memories:
            return ""
        bullets = "; ".join(m["summary"] for m in memories)
        return f"\n\nRecent memories: [{bullets}]"


def _chat_completions_url(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _disable_thinking_payload(base_url: str) -> dict[str, Any]:
    """LongCat's reasoning models default to slow "deep thinking"; turn it off.

    Only applied when the base URL is LongCat's, since other OpenAI-compatible
    vendors (including real OpenAI) may reject an unrecognized top-level field.
    """
    if "longcat" in base_url.lower():
        return {"thinking": {"type": "disabled"}}
    return {}


def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_llm_env_settings(config_store: Any | None = None) -> tuple[str, str, str, float] | None:
    """Resolve (api_key, base_url, model, timeout) from config_store or env vars.

    Returns None when no API key is configured — the same "is an LLM
    configured" detection `create_narrative_provider_from_env()` uses to
    decide between `OpenAICompatibleNarrativeProvider` and
    `MockNarrativeProvider`, factored out so other first-boot generation
    steps (M6's capability catalog) can reuse it instead of duplicating the
    config_store-vs-env-var precedence rules.
    """
    if config_store is not None:
        api_key = config_store.get("llm.api_key", default="") or ""
    else:
        api_key = os.getenv("ANIMA_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    if not api_key:
        return None

    if config_store is not None:
        base_url = config_store.get("llm.base_url", default="") or "https://api.openai.com/v1"
        model = config_store.get("llm.model", default="gpt-4o-mini")
        timeout = config_store.get("llm.timeout", default=20.0)
    else:
        base_url = (
            os.getenv("ANIMA_LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        model = os.getenv("ANIMA_LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        timeout = 20.0
    return api_key, base_url, model, timeout


def create_narrative_provider_from_env(
    prompt_store: Any | None = None,
    config_store: Any | None = None,
) -> NarrativeProvider:
    """Create the configured narrative provider.

    `config_store` (M5), when given, is threaded into the provider so its
    `llm.*` connection settings (api_key/base_url/model/timeout) are read
    live on every call instead of frozen at process start (design.md D3) —
    the initial key check below also prefers it over raw env vars, since a
    fresh DB is seeded from these same env vars and the DB row is the real
    source of truth afterward. Falls back to env vars entirely when no
    `config_store` is wired (e.g. the headless `story` CLI):
    - `ANIMA_LLM_API_KEY` or `OPENAI_API_KEY`
    - `ANIMA_LLM_BASE_URL` or `OPENAI_BASE_URL`
    - `ANIMA_LLM_MODEL` or `OPENAI_MODEL`

    `prompt_store`, when given, is threaded in so `narrative.describe` is
    read live instead of the hardcoded default.
    """
    settings = resolve_llm_env_settings(config_store)
    if settings is None:
        return MockNarrativeProvider()
    api_key, base_url, model, timeout = settings
    return OpenAICompatibleNarrativeProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=timeout,
        prompt_store=prompt_store,
        config_store=config_store,
    )


_DEFAULT_CAPABILITIES: list[dict[str, Any]] = [
    {"id": "talk", "kind": "talk", "description": "与另一个 agent 对话", "params_schema": {"target": "agent_id"}},
    {"id": "move", "kind": "move", "description": "移动到另一个地点", "params_schema": {"location": "location_id"}},
]

_CAPABILITY_CATALOG_PROMPT = (
    "生成一个 JSON 数组，每项包含 id/kind/description/params_schema 字段，"
    "描述这个赛博世界里 agent 可执行的原子能力。至少包含 talk 和 move。"
    "只输出 JSON 数组本身，不要任何解释或 Markdown 代码块标记。"
)


def generate_capability_catalog(
    config_store: Any | None = None,
    post_json: Any | None = None,
    force_mock: bool = False,
) -> list[dict[str, Any]]:
    """Generate the phase-1 capability catalog (design.md D9/D10).

    One LLM call when an LLM is configured (same detection as
    `create_narrative_provider_from_env`); otherwise, or on any failure
    (no key, request error, unusable/malformed response), falls back to the
    deterministic `[talk, move]` default — mirrors
    `OpenAICompatibleNarrativeProvider`'s existing fallback-to-mock
    discipline so a flaky LLM provider can never block `serve` from
    starting. force_mock (novel-benchmark-loop: `simulate --no-llm`) skips
    the settings/network check outright, even when a real key is present.
    """
    if force_mock:
        return list(_DEFAULT_CAPABILITIES)
    settings = resolve_llm_env_settings(config_store)
    if settings is None:
        return list(_DEFAULT_CAPABILITIES)
    api_key, base_url, model, timeout = settings
    post = post_json or _post_json
    try:
        data = post(
            _chat_completions_url(base_url),
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是赛博世界的能力目录生成器，只输出 JSON。"},
                    {"role": "user", "content": _CAPABILITY_CATALOG_PROMPT},
                ],
                "temperature": 0.7,
                **_disable_thinking_payload(base_url),
            },
            timeout,
        )
        content = data["choices"][0]["message"]["content"]
        catalog = json.loads(content)
        if not isinstance(catalog, list) or not catalog:
            raise ValueError("empty or non-list capability catalog")
        for item in catalog:
            if not isinstance(item, dict) or not item.get("id"):
                raise ValueError("malformed capability entry")
        return catalog
    except (
        KeyError,
        IndexError,
        TimeoutError,
        TypeError,
        ValueError,
        urllib.error.URLError,
    ):
        return list(_DEFAULT_CAPABILITIES)
