"""人数上限是运营的参数,不是引擎写死的意见:`scheduler.max_agents`,0 = 不限。

此前 `MAX_AGENTS = 100` 硬编码在 register 里,超了直接 RuntimeError ——
创作侧造得出 120 人的世界,引擎开机就拒。现在 100 只是声明在
config_store._DEFAULTS 里的默认值,运营一句 `config set scheduler.max_agents 0`
就能放开;报错信息里带着这句解法,撞上限的人当场知道钥匙在哪。
"""

from __future__ import annotations

import threading

import pytest

from anima_world.scheduler import MAX_AGENTS, Scheduler


class _Brain:
    def __init__(self, agent_id: str):
        self.agent = type("A", (), {"id": agent_id})()


class _Config:
    def __init__(self, value: int):
        self.value = value

    def get(self, key, default=None):
        assert key == "scheduler.max_agents"
        return self.value


def _bare_scheduler(config) -> Scheduler:
    # register 只碰 _lock / agents / config_store,外加"她声明过的量在这儿落地"
    # 那一步要的 ontology / stock_store(两个都 None 时它整个跳过)——
    # 白盒到此为止,多一个协作者这里就该跟着改。
    s = object.__new__(Scheduler)
    s._lock = threading.RLock()
    s.agents = {}
    s.config_store = config
    s.ontology = None
    s.stock_store = None
    return s


def test_the_cap_is_a_declared_config_key_defaulting_to_the_old_constant():
    from anima_world.config_store import _DEFAULTS

    default, kind, category, _secret, note = _DEFAULTS["scheduler.max_agents"]
    assert default == MAX_AGENTS and kind == "int" and category == "scheduler"
    assert "0" in note  # 说明里要写着 0 = 不限,不然没人知道


def test_zero_means_unlimited():
    s = _bare_scheduler(_Config(0))
    for i in range(MAX_AGENTS + 5):
        s.register(_Brain(f"a{i}"))
    assert len(s.agents) == MAX_AGENTS + 5


def test_an_operator_cap_is_enforced_and_the_error_names_the_remedy():
    s = _bare_scheduler(_Config(2))
    s.register(_Brain("a"))
    s.register(_Brain("b"))
    with pytest.raises(RuntimeError, match="scheduler.max_agents"):
        s.register(_Brain("c"))


def test_without_a_config_store_the_default_still_guards():
    s = _bare_scheduler(None)
    for i in range(MAX_AGENTS):
        s.register(_Brain(f"a{i}"))
    with pytest.raises(RuntimeError):
        s.register(_Brain("overflow"))
