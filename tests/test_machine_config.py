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

from _worldfile import open_world_at, run_cli

import json
import os
import stat
import subprocess
import sys

import pytest

from anima_world import machine_config
from anima_world.api import World


def _cli(*argv: str) -> subprocess.CompletedProcess:
    return run_cli(*argv)


def test_the_resolution_order(tmp_path, monkeypatch):
    w = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
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
    w = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    try:
        machine_config.set_value("llm.timeout", "45")
        assert w.config_get("llm.timeout") == 45.0
        assert isinstance(w.config_get("llm.timeout"), float)
    finally:
        w.close()


def test_the_key_never_lands_in_the_world_file(tmp_path):
    """**打包发出去的世界不该带着你的钥匙。**"""
    from _worldfile import current_client

    done = _cli("config", "set", "llm.api_key", "sk-测试用的", "--world-id", "w")
    assert done.returncode == 0, done.stderr
    assert "config.json" in done.stdout

    stored = json.loads(machine_config.config_path().read_text(encoding="utf-8"))
    assert stored["llm.api_key"] == "sk-测试用的"

    # 世界(:config hash)里一个字节都不许有
    raw = current_client().hgetall("anima:w:config") or {}
    assert not any("sk-测试用的" in v for v in raw.values()), "钥匙落进了世界"


def test_the_file_is_0600(tmp_path):
    """里面是明文 key —— 和创作台的 `settings.json` 同一个形状,同一个权限。"""
    machine_config.set_value("llm.api_key", "sk-x")
    mode = stat.S_IMODE(machine_config.config_path().stat().st_mode)
    assert mode == 0o600, f"权限是 {oct(mode)},里面有明文钥匙"


def test_setting_a_world_key_still_goes_to_the_world(tmp_path):
    """这道路由不许误伤:只有 `llm.*` 属于机器,别的照旧进世界。"""
    db = str(tmp_path / "w.db")
    open_world_at(db, force_mock_llm=True).close()
    done = _cli("config", "set", "needs.enabled", "true", "--world-id", "w")
    assert done.returncode == 0, done.stderr
    assert "config.json" not in done.stdout
    assert machine_config.load().get("needs.enabled") is None


def test_an_env_var_that_would_shadow_the_write_is_called_out(tmp_path, monkeypatch):
    """写了但不生效是最难查的一种 —— 当场说破。"""
    monkeypatch.setenv("ANIMA_LLM_MODEL", "环境变量赢了")
    done = _cli("config", "set", "llm.model", "我刚设的", "--world-id", "w")
    assert done.returncode == 0
    assert "此刻不生效" in done.stdout, "改了不生效,而它一声没吭"



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
    open_world_at(str(db), force_mock_llm=True).close()
    assert not (tmp_path / "fresh.db.key").exists(), "新世界还在生成 keyfile"


def test_an_empty_cipher_is_not_a_lost_key(tmp_path):
    """**假警报会让人学会忽略真警报。**

    创世会给每个 secret 键播一行(加密过的空串),而新世界不再生成 keyfile ——
    于是每次开机都会用一把临时钥匙去解那个空串,解不开。把它报成"你的钥匙丢了"
    是假警报,而且是每次开机都来一遍的那种。
    """
    db = str(tmp_path / "w.db")
    w = open_world_at(db, force_mock_llm=True)
    try:
        assert w.scheduler.config_store.undecryptable_secrets() == [], (
            "把一个空串报成了丢钥匙"
        )
    finally:
        w.close()
    done = _cli("doctor", "--world-id", "w", "--skip-probe")
    assert "解不开" not in done.stdout, "doctor 报了假警报"


