"""世界文件里只存**作者动过的**配置,别的现场从引擎取(DB-SPLIT.md 移动 1)。

创世播默认值这件事看着无害,坏处要过一个版本才显形:

    ① 新世界:chat.recall_k = 3
    ② 引擎下一版把默认值改成 99(因为 3 被证明太少)
    ③ 老世界用新引擎打开: 3   ← 还是旧的,永远
    ④ 新建的世界:        99

而且**无声** —— 两个世界行为不同,`config list` 看上去一模一样,没人会想到去比。
`config` 表也没有任何一列记着"这是谁写的",于是引擎分辨不了作者的决定和创世那天的快照。

下面这几条把两件事钉住:创世**不写**默认值(于是表里剩下的就是作者的意见),
读的时候现场回落到引擎当前的默认值(于是改进过的默认值老世界也吃得到)。
"""
from __future__ import annotations

import json

import pytest

from anima_world import config_store as config_mod
from anima_world import prompt_store as prompt_mod
from anima_world.api import World
from _worldfile import open_world_at


def _rows(db_path: str, table: str) -> list[str]:
    """世界里真正落了盘的行(Redis hash 的 field)。"""
    from _worldfile import redis_for

    key = "anima:w:config" if table == "config" else "anima:w:prompts"
    return sorted((redis_for(db_path).hgetall(key) or {}).keys())


def _seed(tmp_path, config: dict) -> str:
    seed = {
        "agents": [{"id": "a", "name": "A", "location": "x", "personality": "p"}],
        "locations": [{"id": "x", "name": "X", "description": "d"}],
        "config": config,
    }
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_genesis_writes_no_engine_defaults(tmp_path, bare_seed):
    """毛坯世界建完,`config` 与 `prompt_templates` 两张表都是空的。

    用 `bare_seed` 而不是内置种子:内置种子有 `"config": {...}`(橱窗替这个世界
    点亮了七个开关),拿它来验"创世不写默认值"会把作者的意见算成默认值。
    """
    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, seed_path=bare_seed, force_mock_llm=True):
        pass
    assert _rows(db_path, "config") == []
    assert _rows(db_path, "prompt_templates") == []


def test_only_what_the_author_decided_lands_in_the_world_file(tmp_path):
    """表里剩下的就是"这个世界的作者决定了什么" —— 一眼可见。"""
    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, seed_path=_seed(tmp_path, {"needs.enabled": True}),
                    force_mock_llm=True) as world:
        assert world.config_get("needs.enabled") is True
    assert _rows(db_path, "config") == ["needs.enabled"]


def test_an_improved_engine_default_reaches_an_existing_world(tmp_path, bare_seed, monkeypatch):
    """这一条是移动 1 的全部理由:老世界吃得到引擎改进过的默认值。"""
    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, seed_path=bare_seed, force_mock_llm=True) as world:
        assert world.config_get("chat.recall_k") == 3

    改进后 = dict(config_mod._DEFAULTS)
    改进后["chat.recall_k"] = (99, "int", "chat", False, "改进过的默认值")
    monkeypatch.setattr(config_mod, "_DEFAULTS", 改进后)

    with open_world_at(db_path, force_mock_llm=True) as world:
        assert world.config_get("chat.recall_k") == 99


def test_an_authors_decision_still_wins_over_a_changed_default(tmp_path, monkeypatch):
    """回落只对"没人说过话"的键生效。作者写下的值仍然锁死 —— 需要可复现的场合,
    显式写进种子的 `config` 即可,那本来就是作者的意见。"""
    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, seed_path=_seed(tmp_path, {"chat.recall_k": 7}),
                    force_mock_llm=True):
        pass

    改进后 = dict(config_mod._DEFAULTS)
    改进后["chat.recall_k"] = (99, "int", "chat", False, "改进过的默认值")
    monkeypatch.setattr(config_mod, "_DEFAULTS", 改进后)

    with open_world_at(db_path, force_mock_llm=True) as world:
        assert world.config_get("chat.recall_k") == 7


def test_an_improved_default_template_reaches_an_existing_world(tmp_path, bare_seed, monkeypatch):
    """提示词模板同理 —— 31 行里作者动过的是 **0** 行,全是引擎默认值。"""
    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, seed_path=bare_seed, force_mock_llm=True) as world:
        name = next(iter(prompt_mod._DEFAULTS))
        assert world.scheduler.prompt_store.get(name) == prompt_mod._DEFAULTS[name][0]

    改进后 = dict(prompt_mod._DEFAULTS)
    改进后[name] = ("改进过的模板", "改进过的说明")
    monkeypatch.setattr(prompt_mod, "_DEFAULTS", 改进后)

    with open_world_at(db_path, force_mock_llm=True) as world:
        assert world.scheduler.prompt_store.get(name) == "改进过的模板"


def test_a_key_nobody_set_is_still_a_key_you_can_read_and_describe(tmp_path, bare_seed):
    """没有行 ≠ 不存在。`config get` / `config list` 照旧看得见它,
    否则移动 1 的代价就是"一个新世界里什么配置都没有"。"""
    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, seed_path=bare_seed, force_mock_llm=True) as world:
        store = world.scheduler.config_store
        assert store.has("chat.recall_k")
        assert (store.meta("chat.recall_k") or {})["value_type"] == "int"
        listed = {row["key"]: row for row in world.config_list()}
        assert "chat.recall_k" in listed
        assert listed["chat.recall_k"]["source"] == "默认值"


def test_the_source_of_every_value_is_visible(tmp_path):
    """"为什么我改了配置没生效"几乎总是这个问题。"""
    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, seed_path=_seed(tmp_path, {"needs.enabled": True}),
                    force_mock_llm=True) as world:
        listed = {row["key"]: row for row in world.config_list()}
        assert listed["needs.enabled"]["source"] == "世界文件"
        assert listed["economy.enabled"]["source"] == "默认值"


@pytest.mark.parametrize("key", sorted(config_mod._DEFAULTS))
def test_no_key_is_lost_when_nothing_is_seeded(tmp_path, bare_seed, key):
    """逐键盯着:创世不播之后,每一个声明过的键都还读得出它的默认值。"""
    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, seed_path=bare_seed, force_mock_llm=True) as world:
        expected = config_mod._DEFAULTS[key][0]
        if key in ("llm.api_key", "llm.base_url", "llm.model",
                   "llm.background.model", "llm.timeout", "llm.max_retries"):
            pytest.skip("机器配置那一层的键,解析顺序另有规矩")
        assert world.config_get(key) == expected


def test_a_declared_secret_is_refused_by_the_world(tmp_path):
    """**世界是分发物,secret 不许进去 —— 现在是当场报错,不是加密。**

    world.db 时代靠 Fernet + keyfile;钥匙搬进机器配置后,引擎手里没有加密
    钥匙,"退回明文"就是那种照跑但给错东西的坏 —— 所以 `set()` 对非空密文值
    直接 RuntimeError,指路机器配置。
    """
    import fakeredis

    from anima_world.redis_state import RedisConfigBackend

    secrets = [key for key, spec in config_mod._DEFAULTS.items() if spec[3]]
    assert secrets, "一个声明为密文的键都没有 —— 这条测试的锚点没了"

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    store = config_mod.ConfigStore(RedisConfigBackend(client, "t"))
    for key in secrets:
        with pytest.raises(RuntimeError, match="machine config"):
            store.set(key, f"sk-{key}-的明文")
    raw = client.hgetall("anima:t:config") or {}
    assert not any("的明文" in v for v in raw.values()), "明文躺在世界里"


def test_moving_to_redis_does_not_rebuild_the_snapshot_there(tmp_path):
    """**刚从 SQLite 拆掉的快照,不许在 Redis 里原样重建。**

    `PromptStore.list()` 返回的是**合并视图**(引擎声明的 31 条 + 世界里多出来的),
    而搬家是照着 `list()` 搬的 —— 整份搬过去,31 条默认值就全落进了 Redis。
    从此改进过的模板到不了这个世界,而且照样无声:移动 1 拆掉的病,换个后端复发。

    判据和别处一样:**搬的是作者的意见,不是引擎的默认值。**
    """
    fakeredis = pytest.importorskip("fakeredis")

    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    db_path = str(tmp_path / "w.db")
    name = next(iter(prompt_mod._DEFAULTS))

    with World.open("p", redis=redis, force_mock_llm=True) as world:
        assert redis.hlen("anima:p:prompts") == 0, (
            f"没人动过一条模板,Redis 里却有 {redis.hlen('anima:p:prompts')} 条 —— "
            f"引擎的默认值被当成这个世界的内容搬过去了"
        )
        # 而读得到 —— "不搬"不等于"没有"
        assert world.scheduler.prompt_store.get(name) == prompt_mod._DEFAULTS[name][0]
        assert world.scheduler.prompt_store.has(name)

        world.scheduler.prompt_store.set(name, "作者改写的")
        assert redis.hlen("anima:p:prompts") == 1, "作者动过的那条没落进 Redis"
        assert world.scheduler.prompt_store.get(name) == "作者改写的"


def test_an_improved_default_template_reaches_a_redis_world(tmp_path, monkeypatch):
    """上一条的实质:Redis 世界也吃得到改进过的默认模板。"""
    fakeredis = pytest.importorskip("fakeredis")

    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    db_path = str(tmp_path / "w.db")
    name = next(iter(prompt_mod._DEFAULTS))
    with World.open("p", redis=redis, force_mock_llm=True):
        pass

    改进后 = dict(prompt_mod._DEFAULTS)
    改进后[name] = ("改进过的模板", "改进过的说明")
    monkeypatch.setattr(prompt_mod, "_DEFAULTS", 改进后)

    with World.open("p", redis=redis, force_mock_llm=True) as world:
        assert world.scheduler.prompt_store.get(name) == "改进过的模板"
