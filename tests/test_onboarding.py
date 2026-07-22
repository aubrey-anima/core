"""The first-run surface: `start` / `config` / `doctor`.

These commands exist because the engine's honest defaults are hostile to a
newcomer — an unset key degrades silently, and a fresh world's clock runs at
1:1 with reality, so the page looks frozen for the first five minutes anyone
watches it. What is pinned here is the *reporting*, since that is the part
that silently rots: a status that stops distinguishing "unset" from
"unreadable" is worse than no status at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from anima_world import onboarding
from anima_world.__main__ import _coerce_config_value, main
from anima_world.config_store import ConfigStore
from anima_world.db import open_db


@pytest.fixture
def world(tmp_path):
    """A world db with seeded config, plus its ConfigStore.

    Keyed off the real sibling keyfile, not an ephemeral key: the CLI reopens
    the db through `load_or_create_key`, and a mismatch here would put every
    test into the "lost keyfile" branch instead of the one it means to check.
    """
    from anima_world.config_store import load_or_create_key, seed_defaults

    db = tmp_path / "w.db"
    conn = open_db(db)
    store = ConfigStore(conn, fernet_key=load_or_create_key(db))
    seed_defaults(store)
    yield db, store, conn
    conn.close()


# ── clock, in words ──────────────────────────────────────────────────────────


def test_packaged_default_clock_reads_as_real_time():
    # 1 tick / 300s at 5 world-minutes a tick == wall clock. If this ever stops
    # saying so, the "why does nothing happen" trap is back.
    assert "与现实 1:1" in onboarding.human_tick_rate(1 / 300, 5)
    assert "24 小时" in onboarding.human_tick_rate(1 / 300, 5)


def test_demo_clock_reads_as_minutes_per_day():
    described = onboarding.human_tick_rate(1.0, 5)
    assert "1 tick/秒" in described
    assert "5 分钟走完一个世界日" in described
    assert "300 倍速" in described


def test_stopped_clock_is_not_a_division_by_zero():
    assert onboarding.human_tick_rate(0, 5) == "时钟已停"


# ── LLM status ───────────────────────────────────────────────────────────────


def test_status_unset_carries_the_fix_command(world):
    _, store, _ = world
    status = onboarding.llm_status(store)
    assert status.state == "unset"
    assert status.degraded
    assert "config set llm.api_key" in status.fix


def test_status_ok_masks_the_key(world):
    _, store, _ = world
    store.set("llm.api_key", "sk-abcdefghijklmnop")
    status = onboarding.llm_status(store)
    assert status.state == "ok"
    assert not status.degraded
    assert status.masked_key == "sk-***mnop"
    assert "sk-abcdefghijklmnop" != status.masked_key  # never the raw key


def test_status_tells_a_lost_keyfile_apart_from_an_unset_key(tmp_path):
    """The two states are indistinguishable through `get()` and have opposite
    fixes — restore a file vs. go get a key."""
    from anima_world.config_store import seed_defaults

    db = tmp_path / "w.db"
    conn = open_db(db)
    store = ConfigStore(conn, fernet_key=Fernet.generate_key())
    seed_defaults(store)
    store.set("llm.api_key", "sk-real")
    conn.close()

    conn = open_db(db)
    try:
        reopened = ConfigStore(conn, fernet_key=Fernet.generate_key())  # keyfile lost
        status = onboarding.llm_status(reopened, db)
        assert status.state == "unreadable"
        assert str(db) in status.summary and "key" in status.summary
    finally:
        conn.close()


def test_no_config_store_is_its_own_state():
    assert onboarding.llm_status(None).state == "no-store"


# ── config CLI ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,value_type,expected",
    [
        ("1", "int", 1),
        ("0.00333", "float", 0.00333),
        ("true", "bool", True),
        ("off", "bool", False),
        ("LongCat-2.0", "str", "LongCat-2.0"),
    ],
)
def test_config_values_are_coerced_to_the_declared_type(raw, value_type, expected):
    # Storing "0.00333" as a str under a float key poisons the in-memory cache
    # until the next process start — the tick loop would then compare a string.
    assert _coerce_config_value(raw, value_type) == expected


def test_config_rejects_a_value_of_the_wrong_type():
    with pytest.raises(ValueError):
        _coerce_config_value("快点", "float")


def test_config_set_then_get_round_trips_through_the_cli(world, capsys):
    db, _, conn = world
    conn.close()  # the CLI opens its own connection

    assert main(["config", "--db-path", str(db), "set", "scheduler.tick_rate", "0.5"]) == 0
    capsys.readouterr()
    assert main(["config", "--db-path", str(db), "get", "scheduler.tick_rate"]) == 0
    assert capsys.readouterr().out.strip() == "0.5"


def test_config_set_refuses_an_unknown_key(world, capsys):
    db, _, conn = world
    conn.close()
    assert main(["config", "--db-path", str(db), "set", "llm.apikey", "x"]) == 2
    assert "config list" in capsys.readouterr().err


def test_config_get_never_prints_a_raw_secret(world, capsys):
    db, store, conn = world
    store.set("llm.api_key", "sk-supersecretvalue")
    conn.close()
    assert main(["config", "--db-path", str(db), "get", "llm.api_key"]) == 0
    assert "sk-supersecretvalue" not in capsys.readouterr().out


# ── doctor ───────────────────────────────────────────────────────────────────


def test_doctor_on_a_missing_world_points_at_start(tmp_path, capsys):
    assert main(["doctor", "--db-path", str(tmp_path / "nope.db")]) == 1
    assert "anima-world start" in capsys.readouterr().out


def test_doctor_flags_a_degraded_world_without_calling_out(world, capsys):
    db, _, conn = world
    conn.close()
    assert main(["doctor", "--db-path", str(db), "--skip-probe"]) == 1
    out = capsys.readouterr().out
    assert "LLM 未配置" in out
    assert "与现实 1:1" in out  # the clock is reported in words, always


# ── guided setup ─────────────────────────────────────────────────────────────


def test_empty_key_is_a_supported_answer_not_an_error(world):
    db, store, _ = world
    configured = onboarding.configure_llm_interactively(
        store, db, input_fn=lambda _: "", secret_input_fn=lambda _: ""
    )
    assert configured is False
    assert onboarding.llm_status(store).state == "unset"


def test_guided_setup_stores_key_url_and_model(world, capsys):
    db, store, _ = world
    answers = iter(["https://api.longcat.chat/openai/v1", "LongCat-2.0"])
    configured = onboarding.configure_llm_interactively(
        store, db, input_fn=lambda _: next(answers), secret_input_fn=lambda _: "sk-typed-in"
    )
    assert configured is True
    assert store.get("llm.api_key") == "sk-typed-in"
    assert store.get("llm.model") == "LongCat-2.0"
    assert store.get("llm.base_url") == "https://api.longcat.chat/openai/v1"
    assert "sk-typed-in" not in capsys.readouterr().out  # echoed masked only


def test_guided_setup_accepts_defaults_on_bare_enter(world):
    db, store, _ = world
    onboarding.configure_llm_interactively(
        store, db, input_fn=lambda _: "", secret_input_fn=lambda _: "sk-typed-in"
    )
    assert store.get("llm.base_url") == onboarding.DEFAULT_BASE_URL
    assert store.get("llm.model") == onboarding.DEFAULT_MODEL
