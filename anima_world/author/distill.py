"""Distill pass: turn scan-phase dossiers into an opening-state world seed.

Public API
----------
run_distill(llm, store, job_id, *, min_appearances=3) -> None
    Full pipeline: status → major chars → per-char LLM → relations → world_setting → locations → store.
_distill_character(llm, name, aliases, brief, notes, relation_lines, location_ids) -> dict
    Single character distillation with retry-on-error.
distill_relations(facts, major_ids) -> list[dict]
    Pure function: filter + map + dedup relation facts.
layout_locations(locations) -> list[dict]
    Pure function: grid-layout of location entities.
assemble_seed(store, job_id) -> dict
    Assemble & validate a rich world seed from final_seed_parts.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

from anima_world.world_seed import is_valid_world_seed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentinel for "no location candidate list available"
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n|\n?```$", re.MULTILINE)

# Sentiment mapping word lists
_HOSTILE_WORDS = frozenset(["仇", "恨", "敌", "仇人", "宿敌", "杀"])
_INTIMATE_WORDS = frozenset(["爱", "恋", "夫妻", "挚友", "兄弟", "姐妹", "亲"])


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip()).strip()


def _nature_to_sentiment(nature: str) -> float:
    """Map a Chinese relation description to a sentiment score."""
    for word in _HOSTILE_WORDS:
        if word in nature:
            return -0.5
    for word in _INTIMATE_WORDS:
        if word in nature:
            return 0.6
    return 0.2


# ---------------------------------------------------------------------------
# Pure function: distill_relations
# ---------------------------------------------------------------------------


def distill_relations(facts: list[dict], major_ids: set[str]) -> list[dict]:
    """Filter relation facts to major characters with initial origin, dedup by unordered pair.

    Args:
        facts: list of relation fact dicts (a, b, nature, origin, chunk_idx, ...)
        major_ids: set of entity_ids that are "major" characters

    Returns:
        list of dicts with keys: a, b, sentiment, r_type, r_type_back
    """
    # Group by unordered pair; only initial relations between major ids
    pair_facts: dict[frozenset, list[dict]] = {}
    for fact in facts:
        a = fact.get("a", "")
        b = fact.get("b", "")
        if fact.get("origin") != "initial":
            continue
        if a not in major_ids or b not in major_ids:
            continue
        key = frozenset({a, b})
        pair_facts.setdefault(key, []).append(fact)

    result = []
    for key, group in pair_facts.items():
        # Last fact in the group wins for r_type
        last = group[-1]
        nature = last.get("nature", "")
        sentiment = _nature_to_sentiment(nature)
        a, b = last.get("a", ""), last.get("b", "")
        result.append({
            "a": a,
            "b": b,
            "sentiment": sentiment,
            "r_type": nature,
            "r_type_back": nature,
        })
    return result


# ---------------------------------------------------------------------------
# Pure function: layout_locations
# ---------------------------------------------------------------------------


def layout_locations(locations: list[dict]) -> list[dict]:
    """Arrange location entities in a grid layout with normalized 0-1 coordinates.

    Args:
        locations: list of dicts with entity_id, name, brief fields

    Returns:
        list of dicts with keys: id, name, description, kind, x, y
        If locations is empty, returns a single synthetic "stage" point.
    """
    if not locations:
        return [{
            "id": "stage",
            "name": "舞台",
            "description": "故事发生的地方",
            "kind": "point",
            "x": 0.5,
            "y": 0.5,
        }]

    # Sort by entity_id for deterministic ordering
    sorted_locs = sorted(locations, key=lambda loc: loc.get("entity_id", ""))

    n = len(sorted_locs)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)

    result = []
    for i, loc in enumerate(sorted_locs):
        col = i % cols
        row = i // cols
        x = (col + 0.5) / cols
        y = (row + 0.5) / rows
        description = loc.get("brief", "") or loc.get("name", "")
        result.append({
            "id": loc["entity_id"],
            "name": loc.get("name", ""),
            "description": description,
            "kind": "point",
            "x": x,
            "y": y,
        })
    return result


# ---------------------------------------------------------------------------
# LLM call: _distill_character
# ---------------------------------------------------------------------------

_CHAR_SYSTEM = """你是一个多智能体模拟小说的世界构建助手。你的任务是根据角色档案，为该角色生成开局状态的人设、初始位置和开局前往事记忆。
只输出一个 JSON 对象，不要任何解释文字。"""

_CHAR_PROMPT_TEMPLATE = """## 角色：{name}
别名：{aliases}
简介：{brief}

## 关系档案
{relation_lines}

## 已有笔记
{notes}

## 候选地点（从下列 id 中选一个作为初始位置，如无合适选项输出 null）
{location_ids}

## 输出要求

输出一个 JSON 对象：
{{
  "personality": "80~160字中文人设（开局状态，吃透全部档案，后期揭晓写成暗线而非已知事实）",
  "location": "候选地点id之一，或 null",
  "memories": [
    {{"summary": "开局前的经历摘要", "importance": 0.0~1.0之间的浮点数}},
    ...最多5条
  ]
}}

注意：personality 必须非空；memories 里没有 summary 的条目会被丢弃。只输出 JSON。"""

_RETRY_SUFFIX = "\n\n上一次输出未通过校验：{error}\n请修正后重新只输出 JSON 对象。"


def _validate_char_result(data: dict, location_ids: list[str]) -> dict:
    """Validate and normalize a character distillation result dict.

    Raises:
        ValueError: if personality is missing/empty
    """
    personality = data.get("personality", "")
    if not isinstance(personality, str) or not personality.strip():
        raise ValueError("personality 为空或不是字符串")

    location = data.get("location")
    if location is not None and location not in location_ids:
        location = None

    raw_memories = data.get("memories") or []
    memories = []
    for mem in raw_memories:
        if not isinstance(mem, dict):
            continue
        summary = mem.get("summary")
        if not summary:
            continue
        importance = mem.get("importance", 0.5)
        try:
            importance = float(importance)
        except (TypeError, ValueError):
            importance = 0.5
        importance = max(0.0, min(1.0, importance))
        memories.append({"summary": summary, "importance": importance})

    # Cap at 5 memories
    memories = memories[:5]

    return {
        "personality": personality,
        "location": location,
        "memories": memories,
    }


async def _distill_character(
    llm: Any,
    name: str,
    aliases: list[str],
    brief: str,
    notes: list[str],
    relation_lines: list[str],
    location_ids: list[str],
) -> dict:
    """Distill a single character via LLM with one retry on failure.

    Args:
        llm: LLM client with ``await complete(messages) -> str``
        name: character's name
        aliases: list of aliases
        brief: one-sentence description
        notes: list of note texts from store
        relation_lines: list of relation description strings
        location_ids: list of valid location entity ids for placement

    Returns:
        dict with keys: personality (str), location (str|None), memories (list)

    Raises:
        ValueError: if both attempts fail
    """
    aliases_str = "、".join(aliases) if aliases else "无"
    notes_str = "\n".join(f"- {n}" for n in notes) if notes else "（暂无笔记）"
    rel_str = "\n".join(f"- {r}" for r in relation_lines) if relation_lines else "（暂无关系档案）"
    loc_str = "\n".join(f"- {lid}" for lid in location_ids) if location_ids else "（暂无候选地点，输出 null）"

    base_prompt = _CHAR_PROMPT_TEMPLATE.format(
        name=name,
        aliases=aliases_str,
        brief=brief or "（无简介）",
        relation_lines=rel_str,
        notes=notes_str,
        location_ids=loc_str,
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _CHAR_SYSTEM},
        {"role": "user", "content": base_prompt},
    ]

    last_error = ""
    for attempt in range(2):
        if attempt > 0 and last_error:
            retry_content = base_prompt + _RETRY_SUFFIX.format(error=last_error)
            messages = [
                {"role": "system", "content": _CHAR_SYSTEM},
                {"role": "user", "content": retry_content},
            ]

        try:
            raw = await llm.complete(messages)
        except Exception as exc:  # noqa: BLE001
            last_error = f"LLM 调用失败：{exc}"
            logger.warning("_distill_character name=%r attempt=%d llm error: %s", name, attempt, exc)
            continue

        try:
            data = json.loads(_strip_fences(raw))
            if not isinstance(data, dict):
                raise ValueError(f"顶层必须是 dict，得到 {type(data).__name__}")
            result = _validate_char_result(data, location_ids)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = f"解析校验失败：{exc}"
            logger.warning(
                "_distill_character name=%r attempt=%d parse error: %s", name, attempt, exc
            )
            continue

        return result

    raise ValueError(f"distill character {name!r} failed after 2 attempts: {last_error}")


# ---------------------------------------------------------------------------
# LLM call: world_setting synthesis
# ---------------------------------------------------------------------------

_LORE_SYSTEM = """你是一个多智能体模拟小说的世界构建助手。请根据提供的世界观笔记，合成一段300~600字的世界观设定文本。
只输出设定文本，不要JSON，不要任何解释。"""

_LORE_PROMPT_TEMPLATE = """## 世界观笔记

{lore_text}

请根据以上内容合成300~600字的世界观设定文本，以中文行文，语言凝练而富有画面感。"""

_LORE_EMPTY_FALLBACK = "（未从原文提炼出世界观设定）"


async def _synthesize_world_setting(llm: Any, lore_notes: list[dict]) -> tuple[str, str | None]:
    """Synthesize world_setting text from lore notes via LLM.

    Returns:
        (setting_text, error_or_None) — if error is not None the pipeline should fail.
    """
    if not lore_notes:
        return _LORE_EMPTY_FALLBACK, None

    lore_texts = [n.get("text", "") for n in lore_notes if n.get("text")]
    lore_text = "\n".join(f"- {t}" for t in lore_texts)

    messages = [
        {"role": "system", "content": _LORE_SYSTEM},
        {"role": "user", "content": _LORE_PROMPT_TEMPLATE.format(lore_text=lore_text)},
    ]

    last_error = ""
    for attempt in range(2):
        if attempt > 0 and last_error:
            retry_content = (
                _LORE_PROMPT_TEMPLATE.format(lore_text=lore_text)
                + _RETRY_SUFFIX.format(error=last_error)
            )
            messages = [
                {"role": "system", "content": _LORE_SYSTEM},
                {"role": "user", "content": retry_content},
            ]

        try:
            result = await llm.complete(messages)
            if not result or not result.strip():
                last_error = "LLM 返回空内容"
                continue
            return result.strip(), None
        except Exception as exc:  # noqa: BLE001
            last_error = f"LLM 调用失败：{exc}"
            logger.warning("_synthesize_world_setting attempt=%d error: %s", attempt, exc)

    return "", last_error


# ---------------------------------------------------------------------------
# Main pipeline: run_distill
# ---------------------------------------------------------------------------


async def run_distill(
    llm: Any,
    store: Any,
    job_id: str,
    *,
    min_appearances: int = 3,
) -> None:
    """Run the full distill pass for a job.

    Steps:
    1. set_job_status('distilling')
    2. Identify major characters (kind=character, appearances >= min_appearances;
       fallback to all characters if none qualify)
    3. Per-major-character LLM call → personality/location/memories
    4. Distill relations (pure function)
    5. Synthesize world_setting (one LLM call)
    6. Layout locations (pure function)
    7. store.replace_final(...)
    8. set_job_status('done')

    On any LLM double-failure: set_job_status('failed', error=...) and return (no raise).
    """
    store.set_job_status(job_id, "distilling")

    # --- 1. Identify location entities for placement ---
    all_entities = store.entities(job_id)
    location_entities = [e for e in all_entities if e.get("kind") == "location"]
    location_ids = [e["entity_id"] for e in location_entities]

    # --- 2. Identify major characters ---
    characters = [e for e in all_entities if e.get("kind") == "character"]
    major_chars = [
        c for c in characters
        if len(c.get("appearance_chunks", [])) >= min_appearances
    ]
    if not major_chars:
        # Fallback: use all characters
        major_chars = characters

    major_ids = {c["entity_id"] for c in major_chars}

    # Preload all relation facts for filtering
    all_facts = store.relation_facts(job_id)

    # --- 3. Per-character LLM distillation ---
    distilled_agents: list[dict] = []
    all_memories: list[dict] = []

    for char in major_chars:
        entity_id = char["entity_id"]
        name = char.get("name", entity_id)
        aliases = char.get("aliases", [])
        brief = char.get("brief", "")

        # Gather notes for this character
        notes_rows = store.entity_notes(job_id, entity_id)
        notes = [n.get("text", "") for n in notes_rows if n.get("text")]

        # Gather relation lines involving this character
        relation_lines = []
        for fact in all_facts:
            if fact.get("origin") != "initial":
                continue
            a, b = fact.get("a"), fact.get("b")
            if a == entity_id or b == entity_id:
                other = b if a == entity_id else a
                # Find the other entity's name
                other_name = next(
                    (e.get("name", other) for e in all_entities if e["entity_id"] == other),
                    other,
                )
                nature = fact.get("nature", "")
                relation_lines.append(f"与 {other_name} 的关系：{nature}")

        try:
            char_result = await _distill_character(
                llm,
                name=name,
                aliases=aliases,
                brief=brief,
                notes=notes,
                relation_lines=relation_lines,
                location_ids=location_ids,
            )
        except ValueError as exc:
            error_msg = f"角色 {name!r} 蒸馏失败：{exc}"
            logger.error("run_distill job=%s: %s", job_id, error_msg)
            store.set_job_status(job_id, "failed", error=error_msg)
            return

        distilled_agents.append({
            "entity_id": entity_id,
            "name": name,
            "location": char_result["location"],
            "personality": char_result["personality"],
        })

        for mem in char_result["memories"]:
            all_memories.append({
                "agent_id": entity_id,
                "summary": mem["summary"],
                "importance": mem["importance"],
            })

    # --- 4. Distill relations ---
    distilled_relations = distill_relations(all_facts, major_ids)

    # --- 5. Synthesize world_setting ---
    lore_notes = store.lore_notes(job_id)
    world_setting, lore_error = await _synthesize_world_setting(llm, lore_notes)
    if lore_error is not None:
        error_msg = f"世界观合成失败：{lore_error}"
        logger.error("run_distill job=%s: %s", job_id, error_msg)
        store.set_job_status(job_id, "failed", error=error_msg)
        return

    # --- 6. Layout locations ---
    laid_out = layout_locations(location_entities)

    # Convert to replace_final format: final_locations expects entity_id field
    final_locations = [
        {
            "entity_id": loc["id"],
            "name": loc["name"],
            "description": loc["description"],
            "x": loc["x"],
            "y": loc["y"],
        }
        for loc in laid_out
    ]

    # --- 7. Persist ---
    store.replace_final(
        job_id,
        agents=distilled_agents,
        relations=distilled_relations,
        memories=all_memories,
        locations=final_locations,
        world_setting=world_setting,
    )

    # --- 8. Done ---
    store.set_job_status(job_id, "done")


# ---------------------------------------------------------------------------
# assemble_seed
# ---------------------------------------------------------------------------


def assemble_seed(store: Any, job_id: str) -> dict:
    """Assemble a rich world seed dict from the store's final_seed_parts.

    Args:
        store: AuthorStore (or duck-typed equivalent)
        job_id: the job to assemble

    Returns:
        dict with keys: world_setting, agents, locations, relations, memories
        Each agent.location is guaranteed to be in the locations id set.
        Memories and relations with invalid ids are dropped.
        Agents are required; raises ValueError if none.

    Raises:
        ValueError: if final_seed_parts is None or agents list is empty
    """
    parts = store.final_seed_parts(job_id)
    if parts is None:
        raise ValueError("job has no distilled output")

    raw_agents = parts.get("agents", [])
    if not raw_agents:
        raise ValueError("job has no distilled output")

    raw_locations = parts.get("locations", [])
    raw_relations = parts.get("relations", [])
    raw_memories = parts.get("memories", [])
    world_setting = parts.get("world_setting", "")

    # Build laid-out locations from stored final_locations
    # final_locations rows: entity_id, name, description, x, y
    locations = [
        {
            "id": loc["entity_id"],
            "name": loc.get("name", ""),
            "description": loc.get("description", ""),
            "kind": "point",
            "x": loc.get("x", 0.5),
            "y": loc.get("y", 0.5),
        }
        for loc in raw_locations
    ]

    # If no locations (shouldn't happen after run_distill, but be safe)
    if not locations:
        locations = [{
            "id": "stage",
            "name": "舞台",
            "description": "故事发生的地方",
            "kind": "point",
            "x": 0.5,
            "y": 0.5,
        }]

    loc_ids = {loc["id"] for loc in locations}
    first_loc_id = locations[0]["id"]

    # Build agents: map entity_id → id, fix invalid locations
    agents = []
    for raw in raw_agents:
        agent_id = raw.get("entity_id") or raw.get("id", "")
        location = raw.get("location")
        if not location or location not in loc_ids:
            location = first_loc_id
        agents.append({
            "id": agent_id,
            "name": raw.get("name", ""),
            "location": location,
            "personality": raw.get("personality", ""),
        })

    if not agents:
        raise ValueError("job has no distilled output")

    agent_ids = {a["id"] for a in agents}

    # Filter relations: both a and b must be in agent_ids
    relations = [
        {
            "a": rel["a"],
            "b": rel["b"],
            "sentiment": rel.get("sentiment", 0.2),
            "r_type": rel.get("r_type", ""),
            "r_type_back": rel.get("r_type_back", ""),
        }
        for rel in raw_relations
        if rel.get("a") in agent_ids and rel.get("b") in agent_ids
    ]

    # Filter memories: agent_id must be in agent_ids
    memories = [
        {
            "agent_id": mem["agent_id"],
            "summary": mem.get("summary", ""),
            "importance": mem.get("importance", 0.5),
        }
        for mem in raw_memories
        if mem.get("agent_id") in agent_ids
    ]

    seed = {
        "world_setting": world_setting,
        "agents": agents,
        "locations": locations,
        "relations": relations,
        "memories": memories,
    }

    if not is_valid_world_seed(seed):
        raise ValueError(f"assembled seed failed is_valid_world_seed: {seed!r}")

    return seed
