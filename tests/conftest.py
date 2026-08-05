"""测试共用的夹具。

**为什么需要一份"素配种子"。** 内置的 `world_seed.json` 是**产品橱窗**:它替这个
世界点亮了一堆开关(needs / economy / social / stance / tools / intent / autonomy)、
播了关系、记忆、钱和货架 —— 因为一个展示不了自己特性的内置世界没人会用。

但引擎测试里有一整类问的是**引擎自己的默认行为**:"开关不点亮时,行为逐 tick 和上
一版一致吗"、"创世安家费是多少"、"关系从零开始爬一档要多久"。拿橱窗世界去验这些,
验的其实是橱窗的布置,不是引擎的默认值 —— 而且橱窗以后每加一件展品,这些测试就会
莫名其妙地红一次。

所以这里把橱窗**剥回毛坯**:同样的角色、地点、作息(测试到处引用 `夏`/`cafe`),
但不带任何富化字段。要验橱窗本身的测试另有其人(`test_flagship_seed.py`)。
"""
from __future__ import annotations

import json
from importlib import resources

import pytest

# 橱窗独有的顶层字段 —— 剥掉它们就回到毛坯。
# **橱窗每加一件展品,这里就要跟一行** —— 不跟的话,一堆"验引擎默认行为"的测试会
# 因为世界变富了而莫名其妙地红(1.3.0 里已经发生过两次:先是 config/relations,
# 再是 stocks/rules)。
_SHOWCASE_TOP_LEVEL = (
    "config", "relations", "memories", "items",
    "stocks", "rules", "stock_visibility", "stock_places",
    "kinds", "entities",
)
# 角色/地点身上的富化字段。
_SHOWCASE_AGENT_FIELDS = ("money", "inventory", "goals")
_SHOWCASE_LOCATION_FIELDS = ("stock",)


def _bundled_seed() -> dict:
    return json.loads(
        (resources.files("anima_world") / "world_seed.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def bare_seed(tmp_path) -> str:
    """内置世界剥掉富化之后的样子,返回种子文件路径。

    从**内置种子派生**而不是另写一份:角色 id、地点、作息一旦变了,这里跟着变,
    不会悄悄和被测世界脱节。
    """
    seed = _bundled_seed()
    for field in _SHOWCASE_TOP_LEVEL:
        seed.pop(field, None)
    for agent in seed.get("agents", []):
        for field in _SHOWCASE_AGENT_FIELDS:
            agent.pop(field, None)
    for location in seed.get("locations", []):
        for field in _SHOWCASE_LOCATION_FIELDS:
            location.pop(field, None)

    path = tmp_path / "bare_seed.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return str(path)

@pytest.fixture(autouse=True)
def _machine_config_stays_out_of_your_home(tmp_path_factory, monkeypatch):
    """**测试永远不许碰真实的 `~/.anima-world/`。**

    这条是踩出来的,而且踩得很难看:机器配置刚落地时没有隔离,于是一个测试把
    `sk-typed-in` 写进了开发机的家目录,**别的测试读到它、真的去连了 OpenAI**,
    十八条测试一起红。测试之间靠一个全局单例路径互相污染,而单看任何一条都看不出来。

    `autouse` 而不是"记得加这个 fixture":需要人记住的隔离迟早会漏掉一处,
    而漏掉的那一处会往真实家目录里写东西。
    """
    monkeypatch.setenv("ANIMA_WORLD_HOME", str(tmp_path_factory.mktemp("anima-home")))
    # 环境里可能真的有这些(开发机上就有)—— 它们优先级最高,会盖掉测试的意图。
    for name in ("ANIMA_LLM_API_KEY", "OPENAI_API_KEY", "LONGCAT_API_KEY",
                 "ANIMA_LLM_BASE_URL", "OPENAI_BASE_URL",
                 "ANIMA_LLM_MODEL", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fresh_redis():
    """一个干净的 fakeredis —— world.db 退役后,测试世界的家。

    `decode_responses=True` 是**契约的一部分**:引擎全线假设 str 进 str 出,
    裸 bytes 客户端下量名会变成 b'树高' 而规律静默失配(开机那条警告就是这么来的)。
    """
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture
def open_world(fresh_redis):
    """开测试世界的门:`world = open_world()`,或 `open_world(seed_path=bare_seed)`。

    world_id 自动唯一(同一个 fakeredis 上开两个世界互不相扰),缺省 Mock LLM,
    teardown 统一 close —— 忘了关的世界会把线程池留到下一个测试里。
    """
    import itertools

    from anima_world.api import World

    opened = []
    counter = itertools.count()

    def _open(world_id: str | None = None, *, redis=None, **kwargs):
        kwargs.setdefault("force_mock_llm", True)
        world = World.open(
            world_id or f"t{next(counter)}", redis=redis or fresh_redis, **kwargs
        )
        opened.append(world)
        return world

    yield _open
    for world in opened:
        try:
            world.close()
        except Exception:  # noqa: BLE001 - teardown 里别把真错埋了
            pass


@pytest.fixture(autouse=True)
def _cli_connects_to_the_test_world(monkeypatch):
    """CLI 测试的 `main([...])` 连到本测试的 fakeredis,而不是真的 127.0.0.1。

    `_worldfile.open_world_at` 每建一个世界就记下客户端;CLI 随后连的就是它 ——
    "同一个 db 路径 = 同一个世界"的旧语义在 CLI 侧由这条保住。
    """
    import _worldfile

    _worldfile.reset_current()
    monkeypatch.setattr(
        "anima_world.__main__._connect_redis",
        lambda url=None: _worldfile.current_client(),
    )
