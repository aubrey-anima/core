"""种子能不能替一个新世界点亮开关(`"config": {...}`)。

这是"内置世界能展示自己"的那一半机制:开关此前只能建完世界再一条条 `config set`,
于是**内置世界无法点亮自己的任何特性**。

四条纪律,每条都有理由:

- **创世时一次,空库才认** —— 和其它 seed_defaults 同一条契约
- **未知键跳过不拒绝** —— 种子会比引擎活得久
- **密文键一律拒绝** —— 种子是分发物,不许携带 API key
- **跳过绝不无声** —— 作者以为点亮了、实际没有,是这个仓库最在意的那类错
"""
from __future__ import annotations

import json

import pytest

from anima_world.api import World
from anima_world.config_store import ConfigStore, load_or_create_key, seed_defaults
from anima_world.db import open_db
from anima_world.world_seed import apply_seed_config


def _store(tmp_path) -> ConfigStore:
    # 要有真的 Fernet 密钥:播默认值时要写 `llm.api_key` 这个密文键。
    path = str(tmp_path / "cfg.db")
    store = ConfigStore(open_db(path), fernet_key=load_or_create_key(path))
    seed_defaults(store)
    return store


def _seed(tmp_path, config: dict, name: str = "s.json") -> str:
    seed = {
        "agents": [{"id": "a", "name": "A", "location": "x", "personality": "p"}],
        "locations": [{"id": "x", "name": "X", "description": "d"}],
        "config": config,
    }
    path = tmp_path / name
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_a_seed_can_light_up_a_flag_at_genesis(tmp_path):
    with World.open(str(tmp_path / "w.db"),
                    seed_path=_seed(tmp_path, {"needs.enabled": True}),
                    force_mock_llm=True) as world:
        assert world.config_get("needs.enabled") is True


def test_values_are_coerced_to_the_declared_type(tmp_path):
    """种子是 JSON,作者会写 "true" / "72" 这种字符串。按声明类型强转,
    和 `World.config_set` 共用同一份规则 —— 否则 db 里存一个、内存里是另一个。"""
    path = _seed(tmp_path, {"needs.enabled": "true", "autonomy.interval_ticks": "36"})
    with World.open(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        assert world.config_get("needs.enabled") is True
        assert world.config_get("autonomy.interval_ticks") == 36


def test_an_existing_world_is_never_rewritten_by_todays_seed(tmp_path):
    """已有的世界不认种子的 config:那些开关此时是**运行数据**,作者可能早就改过。
    拿今天的种子回头覆盖,等于让一次重启悄悄改掉一个跑了半年的世界的行为。"""
    db = str(tmp_path / "w.db")
    path = _seed(tmp_path, {"needs.enabled": True})
    with World.open(db, seed_path=path, force_mock_llm=True) as world:
        assert world.config_get("needs.enabled") is True
        world.config_set("needs.enabled", False)     # 作者后来自己关掉了

    with World.open(db, seed_path=path, force_mock_llm=True) as reopened:
        assert reopened.config_get("needs.enabled") is False, "种子把作者的决定覆盖了"


def test_an_unknown_key_is_skipped_not_fatal(tmp_path):
    """种子会比引擎活得久:一个 1.4 的种子写了 1.3 没有的开关,正确的行为是
    开机并少点亮一项,而不是让整个世界打不开。"""
    store = _store(tmp_path)
    report = apply_seed_config(store, {"config": {
        "needs.enabled": True,
        "a.flag.from.the.future": True,
    }})
    assert report["applied"] == {"needs.enabled": True}
    assert [key for key, _ in report["skipped"]] == ["a.flag.from.the.future"]


def test_a_secret_key_is_refused(tmp_path):
    """种子是**分发物**(`.cyberworld` 里就带着它)。一个能携带 `llm.api_key`
    的种子等于把作者的钥匙寄给每一个拿到这个世界的人。"""
    store = _store(tmp_path)
    report = apply_seed_config(store, {"config": {"llm.api_key": "sk-leaked"}})

    assert report["applied"] == {}
    assert report["skipped"] and "密文" in report["skipped"][0][1]
    assert store.get("llm.api_key") in (None, ""), "密钥被种子写进去了"


def test_a_value_that_cannot_be_coerced_is_skipped_with_a_reason(tmp_path):
    store = _store(tmp_path)
    report = apply_seed_config(store, {"config": {"autonomy.interval_ticks": "每六小时"}})

    assert report["applied"] == {}
    key, reason = report["skipped"][0]
    assert key == "autonomy.interval_ticks" and "int" in reason


def test_a_config_block_of_the_wrong_shape_is_skipped_whole(tmp_path):
    store = _store(tmp_path)
    report = apply_seed_config(store, {"config": ["needs.enabled"]})
    assert report["applied"] == {} and report["skipped"][0][0] == "config"


def test_a_seed_without_config_changes_nothing(tmp_path):
    """缺字段 = 今天的行为 —— 种子格式一贯的宽容原则。"""
    store = _store(tmp_path)
    before = store.get("needs.enabled")
    assert apply_seed_config(store, {"agents": [], "locations": []}) == {
        "applied": {}, "skipped": [],
    }
    assert store.get("needs.enabled") == before


def test_skipped_keys_are_named_in_the_log(tmp_path, caplog):
    """跳过绝不无声:作者以为点亮了、实际没有,正是最难查的那类错。"""
    path = _seed(tmp_path, {"a.flag.from.the.future": True})
    with caplog.at_level("WARNING"):
        World.open(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True).close()
    assert any("a.flag.from.the.future" in record.getMessage() for record in caplog.records)
