"""LLM 的钥匙和端点住在这台机器上,不住在世界里。

`llm.api_key` 此前存在**世界文件**里。它是 secret,所以要加密,所以有了
`world.db.key`,所以有了那条"**密钥必须随 db 搬迁**"的不变量。整条耦合链的根,
就是把一把 API key 存进了世界文件。

而它带来的还不只是麻烦:`.cyberworld` 是**分发物**。一个世界打包发出去,里面躺着
作者的 key。种子里禁止写密文键防的正是这件事,而世界文件这条路一直开着。

盯五件事:

1. **解析顺序**:环境变量 → 机器配置 → 世界配置(旧世界兼容)→ 默认值
2. **人不手写环境变量**:`config set` 自动路由,写的人不必知道哪个键去哪儿
3. **钥匙不进世界文件**
4. **旧世界照旧能读**,但会被点名
5. **来源看得见** —— "为什么我改了配置没生效"几乎总是这个问题
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest

from anima_world import machine_config
from anima_world.api import World


def _cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "anima_world", *argv],
        capture_output=True, text=True, env={**os.environ},
    )


def test_the_resolution_order(tmp_path, monkeypatch):
    w = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    try:
        store = w.scheduler.config_store
        assert store.get("llm.model") == "gpt-4o-mini"
        assert store.provenance("llm.model") in ("默认值", "世界文件")

        machine_config.set_value("llm.model", "机器上配的")
        assert store.get("llm.model") == "机器上配的"
        assert "机器配置" in store.provenance("llm.model")

        monkeypatch.setenv("ANIMA_LLM_MODEL", "环境变量的")
        assert store.get("llm.model") == "环境变量的"
        assert store.provenance("llm.model") == "环境变量"
    finally:
        w.close()


def test_values_are_coerced_to_the_declared_type(tmp_path):
    """环境变量和 JSON 里都可能是字符串,而每个读它的人都指望 float/int。"""
    w = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    try:
        machine_config.set_value("llm.timeout", "45")
        assert w.config_get("llm.timeout") == 45.0
        assert isinstance(w.config_get("llm.timeout"), float)
    finally:
        w.close()


def test_the_key_never_lands_in_the_world_file(tmp_path):
    """**打包发出去的世界不该带着你的钥匙。**"""
    import sqlite3

    db = str(tmp_path / "w.db")
    done = _cli("config", "set", "llm.api_key", "sk-测试用的", "--db-path", db)
    assert done.returncode == 0, done.stderr
    assert "config.json" in done.stdout

    stored = json.loads(machine_config.config_path().read_text(encoding="utf-8"))
    assert stored["llm.api_key"] == "sk-测试用的"

    # 判据是**解密之后的值**,不是那一列非不非空:创世会播一个加密过的空串进去,
    # 而密文非空 ≠ 值非空 —— 我第一版就是这么判错的。
    from anima_world.config_store import ConfigStore
    from anima_world.db import open_db

    conn = open_db(db)
    try:
        # 用 `world_value` 而不是 `get`:`get()` 现在会先问机器配置,
        # 所以它回答不了"这个**世界文件**里有没有它"。
        assert not ConfigStore(conn).world_value("llm.api_key"), "钥匙落进了世界文件"
    finally:
        conn.close()
    _ = sqlite3


def test_the_file_is_0600(tmp_path):
    """里面是明文 key —— 和创作台的 `settings.json` 同一个形状,同一个权限。"""
    machine_config.set_value("llm.api_key", "sk-x")
    mode = stat.S_IMODE(machine_config.config_path().stat().st_mode)
    assert mode == 0o600, f"权限是 {oct(mode)},里面有明文钥匙"


def test_setting_a_world_key_still_goes_to_the_world(tmp_path):
    """这道路由不许误伤:只有 `llm.*` 属于机器,别的照旧进世界。"""
    db = str(tmp_path / "w.db")
    World.open(db, force_mock_llm=True).close()
    done = _cli("config", "set", "needs.enabled", "true", "--db-path", db)
    assert done.returncode == 0, done.stderr
    assert "config.json" not in done.stdout
    assert machine_config.load().get("needs.enabled") is None


def test_an_env_var_that_would_shadow_the_write_is_called_out(tmp_path, monkeypatch):
    """写了但不生效是最难查的一种 —— 当场说破。"""
    monkeypatch.setenv("ANIMA_LLM_MODEL", "环境变量赢了")
    done = _cli("config", "set", "llm.model", "我刚设的", "--db-path", str(tmp_path / "w.db"))
    assert done.returncode == 0
    assert "此刻不生效" in done.stdout, "改了不生效,而它一声没吭"


def test_an_old_world_still_reads_but_gets_called_out(tmp_path):
    """1.3.0 之前建的世界里真的有那一行 —— 读得出来就照用,同时说清它该搬走。"""
    from anima_world.config_store import ConfigStore, load_or_create_key
    from anima_world.db import open_db

    db = str(tmp_path / "old.db")
    conn = open_db(db)
    try:
        # **老世界里那一行是密文**,不是明文:创世播默认值时带着 `is_secret=True`,
        # 而那时每个世界都有 keyfile。用明文建这个"老世界"曾经也能通过 ——
        # 只因为 `set()` 拿不到元数据就当普通值存;那个洞已经堵上(元数据回落到
        # 引擎的声明),而这条测试的 setup 也就必须跟着变忠实。
        ConfigStore(conn, fernet_key=load_or_create_key(db)).set(
            "llm.api_key", "sk-旧世界里的"
        )
    finally:
        conn.close()
    assert (tmp_path / "old.db.key").exists(), "老世界该有 keyfile,不然这条没在验它想验的"

    w = World.open(db, force_mock_llm=True)
    try:
        assert w.config_get("llm.api_key") == "sk-旧世界里的", "旧世界读不出来了"
        assert w.scheduler.config_store.provenance("llm.api_key") == "世界文件"
    finally:
        w.close()

    done = _cli("doctor", "--db-path", db, "--skip-probe")
    assert "还在**世界文件**里" in done.stdout
    assert "带着你的钥匙" in done.stdout
    assert "config set llm.api_key" in done.stdout, "说了问题没说怎么办"


def test_tests_never_touch_the_real_home():
    """**这条是踩出来的,而且踩得很难看。**

    机器配置刚落地时没有隔离,于是一个测试把 `sk-typed-in` 写进了开发机的
    `~/.anima-world/`,**别的测试读到它、真的去连了 OpenAI**,十八条一起红 ——
    而单看任何一条都看不出原因。全局单例路径 + 没有隔离 = 测试之间互相污染。

    `conftest.py` 里那个 `autouse` fixture 是防线,这条是它的哨兵。
    """
    from pathlib import Path

    assert os.environ.get("ANIMA_WORLD_HOME"), "隔离没生效 —— 这一跑会写你的家目录"
    assert machine_config.home() != Path.home() / ".anima-world"


# ---- world.db.key 那条耦合链 -------------------------------------------------


def test_a_new_world_no_longer_needs_a_keyfile(tmp_path):
    """**`llm.api_key` 是唯一声明为密文的键,而它归了机器配置。**

    于是世界里一个 secret 都没有 —— 那条"Fernet 密钥必须随 db 搬迁,丢了就全线降级
    Mock"的不变量整个不需要成立了。整条链的根,就是把一把 API key 存进了世界文件。
    """
    from anima_world.config_store import _DEFAULTS

    secrets = [k for k, v in _DEFAULTS.items() if v[3]]
    assert all(machine_config.is_machine_key(k) for k in secrets), (
        f"这些 secret 还留在世界里:{[k for k in secrets if not machine_config.is_machine_key(k)]}"
    )

    db = tmp_path / "fresh.db"
    World.open(str(db), force_mock_llm=True).close()
    assert not (tmp_path / "fresh.db.key").exists(), "新世界还在生成 keyfile"


def test_an_empty_cipher_is_not_a_lost_key(tmp_path):
    """**假警报会让人学会忽略真警报。**

    创世会给每个 secret 键播一行(加密过的空串),而新世界不再生成 keyfile ——
    于是每次开机都会用一把临时钥匙去解那个空串,解不开。把它报成"你的钥匙丢了"
    是假警报,而且是每次开机都来一遍的那种。
    """
    db = str(tmp_path / "w.db")
    w = World.open(db, force_mock_llm=True)
    try:
        assert w.scheduler.config_store.undecryptable_secrets() == [], (
            "把一个空串报成了丢钥匙"
        )
    finally:
        w.close()
    done = _cli("doctor", "--db-path", db, "--skip-probe")
    assert "解不开" not in done.stdout, "doctor 报了假警报"


def test_a_genuinely_lost_key_is_still_reported(tmp_path):
    """**保守的那一半**:真存过 secret 的世界,钥匙丢了照旧要报。

    漏报比误报坏 —— 一个真丢了钥匙的世界会静默降级成 Mock,而产物上看不出来。
    所以判据是"这个世界有没有过 keyfile"(`had_keyfile`),缺省是 True:不知道
    来历就照旧报警。
    """
    from anima_world.config_store import ConfigStore, load_or_create_key
    from anima_world.db import open_db

    db = str(tmp_path / "old.db")
    conn = open_db(db)
    try:
        store = ConfigStore(conn, fernet_key=load_or_create_key(db))
        # **必须显式声明 is_secret**:没有元数据的键会按普通值明文存,那样这条测试
        # 验的就不是加密路径了(我第一版就是这么写的,它"通过"得毫无意义)。
        store.set("llm.api_key", "sk-真的", value_type="str",
                  category="llm", is_secret=True)
    finally:
        conn.close()
    assert (tmp_path / "old.db.key").exists(), "旧路径没生成 keyfile,这条没在验它想验的"

    (tmp_path / "old.db.key").unlink()          # 钥匙丢了,密文还在
    conn = open_db(db)
    try:
        # had_keyfile 缺省 True = 保守:不知道来历就当它存过真东西
        store = ConfigStore(conn, fernet_key=load_or_create_key(db, create=False))
        # 问**世界那一层**:`get()` 会先看机器配置,而这条验的是世界里的密文。
        assert store.world_value("llm.api_key") is None, "钥匙没了还读得出来?"
        assert "llm.api_key" in store.undecryptable_secrets(), (
            "真丢了钥匙却没报 —— 世界会静默降级成 Mock,而产物上看不出来"
        )
    finally:
        conn.close()
