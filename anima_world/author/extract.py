from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Reuse the same fence-stripping pattern as generate.py
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n|\n?```$", re.MULTILINE)

# Valid entity id: lowercase letters, digits, hyphens
_ID_RE = re.compile(r"^[a-z0-9-]+$")

# Valid relation origins
_VALID_ORIGINS = {"initial", "plot"}


@dataclass(frozen=True)
class Chunk:
    idx: int
    title: str
    start: int
    end: int


def split_novel(text: str, *, target_size: int = 4000) -> list[Chunk]:
    if not text or not text.strip():
        raise ValueError("novel text is empty")

    chapter_pattern = r'^第([一二三四五六七八九十百千零两0-9]+[章回节卷])\s*(.*)'
    matches = list(re.finditer(chapter_pattern, text, re.MULTILINE))

    if len(matches) < 3:
        return _split_by_size(text, target_size)

    chunks = []
    for i, match in enumerate(matches):
        title = match.group(0).strip()
        if i == 0:
            start = 0
        else:
            start = match.start()
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(text)

        chunks.append(Chunk(idx=i, title=title, start=start, end=end))

    return chunks


def _split_by_size(text: str, target_size: int) -> list[Chunk]:
    chunks = []
    idx = 0
    pos = 0

    while pos < len(text):
        chunk_start = pos
        chunk_end = min(pos + target_size, len(text))

        if chunk_end < len(text):
            newline_pos = text.rfind('\n', pos, chunk_end)
            if newline_pos > pos:
                chunk_end = newline_pos + 1

        title = f"片段 {idx + 1}"
        chunks.append(Chunk(idx=idx, title=title, start=chunk_start, end=chunk_end))

        pos = chunk_end
        idx += 1

    return chunks


# ---------------------------------------------------------------------------
# Scan pass: LLM-driven chunk analysis
# ---------------------------------------------------------------------------

_SCAN_SYSTEM = """你是一个多智能体模拟小说的世界构建助手。你的任务是阅读小说章节，从中抽取角色、地点、关系等世界信息。
只输出一个 JSON 对象，不要任何解释文字。"""

_SCAN_PROMPT_TEMPLATE = """## 当前花名册（已知实体）

角色与地点列表（格式：id｜名字｜别名: 别名1,别名2｜简介）：
{roster_lines}

## 当前章节

标题：{chunk_title}

内容：
{chunk_text}

## 输出要求

请仔细阅读上述章节，输出一个 JSON 对象，字段如下：

{{
  "appeared": [已在本章出现的实体id列表，只引用已知花名册中的id或本章 new_entities 中的id],
  "new_entities": [
    {{
      "kind": "character" 或 "location",
      "id": "小写字母/数字/连字符，如 zhang-san",
      "name": "中文名",
      "aliases": ["别名1", "别名2"],
      "brief": "一句话简介"
    }}
  ],
  "alias_additions": {{ "已知实体id": ["新发现的别名"] }},
  "notes": [
    {{ "entity_id": "某实体id", "text": "关于该实体的重要信息" }}
  ],
  "relation_facts": [
    {{
      "a": "实体id",
      "b": "实体id",
      "nature": "关系描述（如：朋友、宿敌、师徒）",
      "origin": "initial（开局就存在的关系）或 plot（剧情中才产生的关系）"
    }}
  ],
  "lore": ["世界观/背景设定信息（不属于特定实体的通用知识）"]
}}

注意：
- appeared、notes、relation_facts 中引用的 id 必须来自已知花名册或本章 new_entities，否则该条会被丢弃
- new_entities 的 id 必须是 ^[a-z0-9-]+$ 格式
- origin 只能是 initial 或 plot，其他值会被归为 plot
- 只输出 JSON，不要有任何注释或解释

已知实体地点列表：
{location_lines}
"""

_RETRY_SUFFIX = "\n\n上一次输出未通过校验：{error}\n请修正后重新只输出 JSON 对象。"


def build_scan_prompt(
    roster: list[dict[str, Any]],
    locations: list[dict[str, Any]],
    chunk_title: str,
    chunk_text: str,
) -> str:
    """Build the LLM prompt for scanning a single chunk.

    Args:
        roster: list of entity dicts (id/name/aliases/brief fields expected)
        locations: list of location-only dicts (id/name fields)
        chunk_title: the chunk's title string
        chunk_text: the raw novel text for this chunk

    Returns:
        A complete prompt string (Chinese).
    """
    roster_lines = "\n".join(
        f"- {e['id']}｜{e['name']}｜别名: {','.join(e.get('aliases', []))}｜{e.get('brief', '')}"
        for e in roster
    ) or "（暂无）"

    location_lines = "\n".join(
        f"- {loc['id']}｜{loc['name']}"
        for loc in locations
    ) or "（暂无）"

    return _SCAN_PROMPT_TEMPLATE.format(
        roster_lines=roster_lines,
        chunk_title=chunk_title,
        chunk_text=chunk_text,
        location_lines=location_lines,
    )


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip()).strip()


def parse_scan_result(raw: str, known_ids: set[str]) -> dict[str, Any]:
    """Parse and validate the LLM's JSON output for a chunk scan.

    Args:
        raw: raw LLM reply (may have code fences)
        known_ids: entity ids already known before this chunk (from store.entities)

    Returns:
        Normalized result dict suitable for store.merge_chunk_result.

    Raises:
        ValueError: if the JSON is malformed or top-level structure is wrong.
    """
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"输出不是合法 JSON：{exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"顶层必须是 dict，得到 {type(data).__name__}")

    # --- new_entities: validate and collect ids introduced in this chunk ---
    raw_new_entities: list[dict] = data.get("new_entities", []) or []
    valid_new_entities: list[dict[str, Any]] = []
    chunk_ids: set[str] = set()
    for ent in raw_new_entities:
        if not isinstance(ent, dict):
            continue
        kind = ent.get("kind")
        if kind not in {"character", "location"}:
            logger.debug("drop new_entity: invalid kind %r", kind)
            continue
        eid = ent.get("id", "")
        if not isinstance(eid, str) or not _ID_RE.match(eid):
            logger.debug("drop new_entity: invalid id %r", eid)
            continue
        name = ent.get("name")
        if not name:
            continue
        valid_new_entities.append({
            "kind": kind,
            "id": eid,
            "name": name,
            "aliases": list(ent.get("aliases") or []),
            "brief": ent.get("brief") or "",
        })
        chunk_ids.add(eid)

    # All ids visible in this chunk: known before + introduced here
    all_ids = known_ids | chunk_ids

    # --- appeared: drop ids not in all_ids ---
    appeared = [eid for eid in (data.get("appeared") or []) if eid in all_ids]

    # --- alias_additions: keep only known entity ids ---
    raw_aliases: dict = data.get("alias_additions") or {}
    alias_additions: dict[str, list[str]] = {
        k: list(v) for k, v in raw_aliases.items() if k in all_ids
    }

    # --- notes: drop entries whose entity_id is unknown ---
    notes = [
        {"entity_id": n["entity_id"], "text": n.get("text", "")}
        for n in (data.get("notes") or [])
        if isinstance(n, dict) and n.get("entity_id") in all_ids
    ]

    # --- relation_facts: drop if either a or b is unknown; fix origin ---
    relation_facts = []
    for fact in (data.get("relation_facts") or []):
        if not isinstance(fact, dict):
            continue
        a, b = fact.get("a"), fact.get("b")
        if a not in all_ids or b not in all_ids:
            logger.debug("drop relation_fact: unknown ids a=%r b=%r", a, b)
            continue
        origin = fact.get("origin")
        if origin not in _VALID_ORIGINS:
            origin = "plot"
        relation_facts.append({
            "a": a,
            "b": b,
            "nature": fact.get("nature", ""),
            "origin": origin,
        })

    # --- lore: plain list of strings ---
    lore = [str(t) for t in (data.get("lore") or []) if t]

    return {
        "appeared": appeared,
        "new_entities": valid_new_entities,
        "alias_additions": alias_additions,
        "notes": notes,
        "relation_facts": relation_facts,
        "lore": lore,
    }


async def scan_chunk(
    llm: Any,
    store: Any,
    job_id: str,
    chunk: dict[str, Any],
    novel_text: str,
) -> bool:
    """Scan a single chunk via LLM, then persist the result.

    Args:
        llm: LLM client with ``await complete(messages) -> str``
        store: AuthorStore (or duck-typed equivalent)
        job_id: the job this chunk belongs to
        chunk: pending chunk dict (idx/title/start_off/end_off)
        novel_text: the full novel text (chunk offsets index into this)

    Returns:
        True on success, False if both attempts failed (chunk is marked failed).
    """
    chunk_idx: int = chunk["idx"]
    chunk_title: str = chunk.get("title", "")
    start_off: int = chunk.get("start_off", 0)
    end_off: int = chunk.get("end_off", len(novel_text))
    chunk_text = novel_text[start_off:end_off]

    # Build the known-id set from entities persisted before this chunk
    entities = store.entities(job_id)
    known_ids: set[str] = {
        e.get("entity_id") or e.get("id", "") for e in entities
    }

    roster = [
        {
            "id": e.get("entity_id") or e.get("id", ""),
            "name": e.get("name", ""),
            "aliases": e.get("aliases", []),
            "brief": e.get("brief", ""),
        }
        for e in entities
    ]
    location_entities = [
        {"id": e.get("entity_id") or e.get("id", ""), "name": e.get("name", "")}
        for e in entities
        if e.get("kind") == "location"
    ]

    base_prompt = build_scan_prompt(roster, location_entities, chunk_title, chunk_text)
    messages: list[dict[str, str]] = [{"role": "user", "content": base_prompt}]

    last_error: str = ""
    for attempt in range(2):
        if attempt > 0 and last_error:
            retry_content = base_prompt + _RETRY_SUFFIX.format(error=last_error)
            messages = [{"role": "user", "content": retry_content}]

        try:
            raw = await llm.complete(messages)
        except Exception as exc:  # noqa: BLE001
            last_error = f"LLM 调用失败：{exc}"
            logger.warning("scan_chunk job=%s idx=%d attempt=%d llm error: %s", job_id, chunk_idx, attempt, exc)
            continue

        try:
            result = parse_scan_result(raw, known_ids)
        except ValueError as exc:
            last_error = f"解析校验失败：{exc}"
            logger.warning("scan_chunk job=%s idx=%d attempt=%d parse error: %s", job_id, chunk_idx, attempt, exc)
            continue

        store.merge_chunk_result(job_id, chunk_idx, result)
        return True

    store.mark_chunk_failed(job_id, chunk_idx, last_error)
    return False


async def run_scan(
    llm: Any,
    store: Any,
    job_id: str,
    novel_text: str,
    *,
    should_stop: Any = None,
) -> None:
    """Run the scan pass over all pending chunks for a job.

    Args:
        llm: LLM client
        store: AuthorStore
        job_id: the import job
        novel_text: full novel text
        should_stop: optional callable(); if it returns True, abort before the next chunk
                     (status is NOT updated — caller can resume later)
    """
    pending = store.pending_chunks(job_id)
    for chunk in pending:
        if should_stop is not None and should_stop():
            return

        await scan_chunk(llm, store, job_id, chunk, novel_text)

    store.set_job_status(job_id, "scan_done")
