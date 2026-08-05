"""Boot-time diagnostics for the ways a world degrades without complaining.

Every case here used to look identical to a healthy world from the outside:
the process starts, the clock ticks, events land. What is missing — a real
LLM, an applied seed file — only shows up in the quality of the text hours
later. These tests pin the moment each one becomes visible.
"""
from __future__ import annotations

from _worldfile import open_world_at

import json
import logging

import pytest

from anima_world.__main__ import build_serve_scheduler


@pytest.fixture
def minimal_seed(tmp_path):
    path = tmp_path / "seed.json"
    path.write_text(
        json.dumps(
            {
                "agents": [
                    {"id": "a", "name": "阿岚", "location": "cafe", "personality": "安静"}
                ],
                "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_seed_is_applied_to_a_fresh_db_without_warning(tmp_path, minimal_seed, caplog):
    with caplog.at_level(logging.WARNING, logger="anima_world.__main__"):
        import fakeredis

        scheduler = build_serve_scheduler(
            "w", fakeredis.FakeStrictRedis(decode_responses=True),
            seed_path=minimal_seed, force_mock_llm=True,
        )
    try:
        assert list(scheduler.agents) == ["a"]
        assert not any("--seed" in r.getMessage() for r in caplog.records)
    finally:
        scheduler.stop()


def test_seed_against_a_populated_db_is_ignored_and_says_so(tmp_path, minimal_seed, caplog):
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    build_serve_scheduler("w", client, seed_path=minimal_seed, force_mock_llm=True).stop()

    with caplog.at_level(logging.WARNING, logger="anima_world.__main__"):
        scheduler = build_serve_scheduler(
            "w", client, seed_path=minimal_seed, force_mock_llm=True
        )
    try:
        assert any("--seed" in r.getMessage() for r in caplog.records), (
            "a seed file silently ignored on an existing world is the trap this warns about"
        )
    finally:
        scheduler.stop()


def test_state_names_the_reason_the_llm_is_mocked(tmp_path):
    from anima_world.api import World

    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        llm = world.state()["runtime"]["llm"]
    assert llm["mock"] is True
    assert llm["degraded_reason"] == "llm.api_key is not configured"


def test_malformed_rich_seed_sections_degrade_instead_of_stranding_the_world(tmp_path, caplog):
    """relations/memories 不在最小 schema 校验里,而它们的播种跑在创世事件已落盘
    之后——这里崩溃会留下一个半初始化且永不重播种的世界。必须逐条降级。"""
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps(
            {
                "agents": [
                    {"id": "a", "name": "阿岚", "location": "cafe", "personality": "安静"},
                    {"id": "b", "name": "小北", "location": "cafe", "personality": "外向"},
                ],
                "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}],
                "relations": {"a": "b"},  # 该是 list 却给了 dict
                "memories": [
                    "not-an-object",
                    {"agent_id": ["a"], "summary": "unhashable id"},
                    {"agent_id": "a", "summary": "好记忆", "importance": "很重要"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    import fakeredis

    with caplog.at_level(logging.WARNING, logger="anima_world.__main__"):
        scheduler = build_serve_scheduler(
            "w", fakeredis.FakeStrictRedis(decode_responses=True),
            seed_path=seed, force_mock_llm=True,
        )
    try:
        assert sorted(scheduler.agents) == ["a", "b"], "坏 relations/memories 不得阻断世界初始化"
        messages = [r.getMessage() for r in caplog.records]
        assert any("relations" in m for m in messages)
        assert any("importance" in m for m in messages)
    finally:
        scheduler.stop()
