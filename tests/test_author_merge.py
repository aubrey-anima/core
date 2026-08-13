"""作者指名的世界文件是一次**逐项**的合并 —— 只增不改。

CLAUDE.md 与 `__main__` 的注释都写着"给了 `--world-file` 就是一次明示的编辑,
语义是只填缺不覆盖"。那句话曾经是假的:**"只填缺"的粒度是整张表,不是每一项** ——
`RedisRulesStore.seed` 看见 `len(self._rows)` 就掉头,`RedisLocationStore.seed_defaults`
看见 `self.all()` 就掉头,角色那一摞被 `if not persisted:` 挡在门外。于是往一个跑了
5347 条事件的世界里补一条规律、一个角色、一个地点、一个种类、一件物品,**一样都没
进去,而且一个字都没说** —— 这个仓库最怕的"照跑但给错东西"。

这里钉三件事:

1. 新东西真的落地了,而老东西一个都没被动过(钱、随身物品、规律定义、地点描述、
   时钟)。走的是 `build_serve_scheduler` 那条真路 —— 直接调播种函数验不出这个 bug,
   因为挡住它的那几道闸有一半在调用方身上。
2. **没给 `--world-file` 时内置橱窗不许漏进来**。合并这条路只对"作者指名的那份"
   开;把它开给内置兜底那份,等于把刚修过的那个 bug(别人的世界里凭空多一棵橡树)
   重新打开。
3. 幂等:同一份文件连开两次,第二次一样都不加,而且不刷屏。
"""
from __future__ import annotations

import copy
import json
import logging

import fakeredis
import pytest

from _worldfile import write_seed_file

from anima_world.__main__ import build_serve_scheduler

MERGE_MARKER = "作者层合并"


def _base_seed() -> dict:
    """一个自定义世界:两个角色、两个地点、一条规律、一个种类、一棵树。"""
    return {
        "agents": [
            {"id": "a", "name": "阿岚", "location": "cafe", "personality": "安静",
             "money": 30, "inventory": [{"item": "old_watch", "qty": 1}]},
            {"id": "b", "name": "小北", "location": "workshop", "personality": "外向"},
        ],
        "locations": [
            {"id": "cafe", "name": "咖啡馆", "description": "临海的小店",
             "kind": "point", "x": 0.2, "y": 0.2},
            {"id": "workshop", "name": "工坊", "description": "满地刨花",
             "kind": "point", "x": 0.6, "y": 0.6},
        ],
        "items": [
            {"id": "old_watch", "name": "父亲的旧怀表", "kind": "durable", "base_price": 0.0},
        ],
        "kinds": [
            {"id": "agent", "quantities": {
                "体力": {"default": 100, "visibility": "self", "label": "体力"},
            }},
            {"id": "tree", "gloss": "一棵树", "quantities": {
                "树高": {"default": 1.0, "visibility": "here", "label": "树高"},
                "生长速度": 0.004,
                "最大树高": 12,
            }, "affordances": {
                "照料": {
                    "when": ["树高 < 最大树高"],
                    "requires": ["me_体力 >= 5"],
                    "set": {"树高": "min(树高 + 0.05, 最大树高)"},
                    "costs": {"体力": "me_体力 - 5"},
                },
            }},
        ],
        "entities": [
            {"id": "tree:老槐", "name": "院里那棵老槐", "location": "cafe"},
        ],
        "rules": [
            {"id": "tree_growth", "every": {"days": 1}, "for_each": {"kind": "tree"},
             "set": {"树高": "min(树高 + 生长速度 * dt, 最大树高)"}},
        ],
        "stocks": [
            {"owner": "world", "values": {"季节": 2}},
            {"owner": "tree:老槐", "values": {"树高": 3.2}},
        ],
        "stock_visibility": [
            {"kind": "world", "key": "季节", "visible": "public"},
        ],
        "stock_places": [
            {"owner": "tree:老槐", "location": "cafe", "label": "老槐"},
        ],
        "relations": [{"a": "a", "b": "b", "sentiment": 0.4}],
        "memories": [
            {"agent_id": "a", "summary": "她记得开店那天下着雨", "importance": 0.6,
             "anchor": True},
        ],
    }


def _patched_seed() -> dict:
    """同一个世界,作者改过一轮:五样新的,外加两处**改动**(那两处不许生效)。"""
    seed = copy.deepcopy(_base_seed())
    # ── 新增 ────────────────────────────────────────────────────────────────
    seed["agents"].append({
        "id": "c", "name": "老陈", "location": "park", "personality": "话少",
        "money": 12, "inventory": [{"item": "broom", "qty": 2}],
        "goals": ["把院子扫干净"],
    })
    seed["locations"].append({
        "id": "park", "name": "小公园", "description": "两条长椅",
        "kind": "point", "x": 0.8, "y": 0.3,
    })
    seed["items"].append({"id": "broom", "name": "扫帚", "kind": "durable", "base_price": 3.0})
    seed["kinds"].append({
        "id": "bench", "gloss": "一条长椅",
        "quantities": {"油漆": {"default": 1.0, "visibility": "here", "label": "油漆"}},
    })
    seed["entities"].append({"id": "bench:门口", "name": "门口那条长椅", "location": "park"})
    seed["rules"].append({
        "id": "掉漆", "every": {"days": 1}, "for_each": {"kind": "bench"},
        "set": {"油漆": "max(油漆 - 0.01 * dt, 0)"},
    })
    seed["stocks"].append({"owner": "bench:门口", "values": {"油漆": 0.7}})
    # ⚠️ 这一条是**同一个 owner 上多一个量**,和上一条(整个 owner 是新的)不是一回事:
    # 按 owner 判的话,一个已经有 `季节` 的 world 永远补不进 `雨势` —— 不报错,那个量
    # 从此恒为 0。线上要补的十个世界量全挂在 `world` 这一个 owner 底下。
    seed["stocks"][0]["values"]["雨势"] = 0.8
    seed["stock_visibility"].append({"kind": "world", "key": "雨势", "visible": "public"})
    seed["stock_places"].append({"owner": "bench:门口", "location": "park", "label": "长椅"})
    seed["relations"].append({"a": "a", "b": "c", "sentiment": -0.2})
    seed["memories"].append({"agent_id": "c", "summary": "他刚搬来", "importance": 0.5})
    # ── 改动(**一处都不许生效**:只增不改)───────────────────────────────
    seed["locations"][0]["description"] = "作者后来改过的描述"
    seed["rules"][0]["set"] = {"树高": "0"}
    seed["agents"][0]["money"] = 9999
    seed["stocks"][0]["values"]["季节"] = 9
    return seed


def _boot(world_id, client, *, world_file=None, ticks: int = 0):
    scheduler = build_serve_scheduler(
        world_id, client, world_file=world_file, force_mock_llm=True,
    )
    try:
        for _ in range(ticks):
            scheduler.tick()
        return _snapshot(scheduler, client, world_id)
    finally:
        scheduler.stop()


def _snapshot(scheduler, client, world_id) -> dict:
    projection = scheduler._memory_projection
    return {
        "roster": sorted(scheduler.agents),
        "clock": scheduler.clock,
        "rules": {f: client.hget(f"anima:{world_id}:world_rules", f)
                  for f in client.hkeys(f"anima:{world_id}:world_rules")},
        "locations": sorted(client.hkeys(f"anima:{world_id}:locations")),
        "cafe": client.hget(f"anima:{world_id}:locations", "cafe"),
        "kinds": sorted(client.hkeys(f"anima:{world_id}:kinds")),
        "entities": sorted(client.hkeys(f"anima:{world_id}:entities")),
        # ⚠️ 键名是 `item_defs`,不是 `items` —— 拼错的那一版里 before/after 双双
        # 是空表,于是"物品没漏进来"和"物品进来了"两条断言一个真green 一个假green。
        "items": sorted(client.hkeys(f"anima:{world_id}:item_defs")),
        "shelves": sorted(client.hkeys(f"anima:{world_id}:shop_stock")),
        "balances": dict(projection.balances),
        "inventories": {k: dict(v) for k, v in projection.inventories.items()},
        "visibility": sorted(client.hkeys(f"anima:{world_id}:visibility")),
        "stock_places": sorted(client.hkeys(f"anima:{world_id}:stock_places")),
        "world_stock": {k: client.hget(f"anima:{world_id}:stock:world", k)
                        for k in client.hkeys(f"anima:{world_id}:stock:world")},
        "memory_count": scheduler.memory_store.count(),
        "events": scheduler.event_log.count(),
    }


@pytest.fixture
def base_file(tmp_path):
    return write_seed_file(tmp_path / "base.cyberworld", _base_seed())


@pytest.fixture
def patch_file(tmp_path):
    return write_seed_file(tmp_path / "patch.cyberworld", _patched_seed())


def test_指名的世界文件逐项合并进一个跑过的世界(base_file, patch_file, caplog):
    """新东西全部落地,老东西一个都没变 —— 走的是真路(build_serve_scheduler)。"""
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    before = _boot("w", client, world_file=base_file, ticks=6)
    assert before["events"] > 0 and before["clock"] > 0, "夹具没跑起来,后面验的就不是'已有世界'"

    with caplog.at_level(logging.INFO, logger="anima_world.__main__"):
        after = _boot("w", client, world_file=patch_file)

    # ── 新的进来了 ─────────────────────────────────────────────────────────
    assert "掉漆" in after["rules"], "新规律没进去"
    assert "park" in after["locations"], "新地点没进去"
    assert "c" in after["roster"], "新角色没进去"
    assert "bench" in after["kinds"], "新种类没进去"
    assert "bench:门口" in after["entities"], "新实体没进去"
    assert "broom" in after["items"], "新物品没进去"
    assert "world\x00雨势" in after["visibility"], "新可见性声明没进去"
    assert "bench:门口" in after["stock_places"], "新的东西在哪没进去"
    assert client.hget("anima:w:stock:bench:门口", "油漆"), "新实体的量没落地"
    # 同一个 owner 上多出来的那个量(线上十个世界量都是这个形状)。存的是
    # `[值, 写它的那个 tick]`,所以取第 0 项。
    assert "雨势" in after["world_stock"], (
        "已有 owner 上的新量没补进去 —— 它不报错,只是那个量从此恒为 0"
    )
    assert json.loads(after["world_stock"]["雨势"])[0] == pytest.approx(0.8)
    # 新角色随身带的东西与钱包(只给新人播,老人一个字都不重发)
    assert after["balances"].get("c") == 12
    assert after["inventories"].get("c", {}).get("broom") == 2
    # 新角色的记忆真的进了记忆库(它走的不是创世那条 rebuild —— 那条见了非空表就掉头)
    assert any(m["summary"] == "他刚搬来"
               for m in scheduler_memories(client, "w", "c")), "新角色的创世记忆没落地"
    assert MERGE_MARKER in " ".join(r.getMessage() for r in caplog.records), (
        "合并了东西却一个字都没说 —— 这个 bug 之所以危险就是因为它沉默"
    )

    # ── 老的**声明**改得动 ─────────────────────────────────────────────────
    # 「只填缺,不覆盖」说的是**状态**(下面那一段)。声明不是状态 —— 一条规律身上
    # 没有任何东西会随时间漂,所以那条理由在它身上不成立,而照搬的代价是作者永远
    # 改不了自己世界的物理法则:线上灯塔湾十条规律全在漂,修一条要连四个真人的
    # 进度一起抹掉重建。
    assert json.loads(after["rules"]["tree_growth"])["definition"]["set"] == {"树高": "0"}, (
        "作者指名的文件里那条规律没生效 —— 一个跑着的世界从此改不了自己的物理法则"
    )
    assert after["cafe"] == before["cafe"], "老地点的描述被今天的文件盖掉了"
    assert after["balances"].get("a") == before["balances"].get("a"), "老角色的钱变了"
    assert after["inventories"].get("a") == before["inventories"].get("a"), "老角色的随身物品变了"
    assert after["world_stock"]["季节"] == before["world_stock"]["季节"], (
        "已有的量被今天的文件写回创世值 —— 世界跑了六个 tick 的现在被倒带"
    )
    assert after["clock"] >= before["clock"], "时钟被倒带回创世那一刻"
    assert after["memory_count"] >= before["memory_count"]


def scheduler_memories(client, world_id, agent_id) -> list[dict]:
    import json

    rows = client.hgetall(f"anima:{world_id}:memories")
    return [json.loads(v) for v in rows.values()
            if json.loads(v).get("agent_id") == agent_id]


def test_没给世界文件时内置橱窗不许漏进一个已有的世界(base_file):
    """硬约束的另一半。内置兜底那份每次开机都在手上 —— 让它也走逐项合并,
    等于把橱窗的橡树、地图、规律一次性塞进别人的世界(世界照跑、日志干净)。"""
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    before = _boot("w2", client, world_file=base_file, ticks=3)
    after = _boot("w2", client)          # 第二次不给 --world-file
    assert after["roster"] == before["roster"], "橱窗的角色漏进来了"
    assert after["locations"] == before["locations"], "橱窗的地图漏进来了"
    assert set(after["rules"]) == set(before["rules"]), "橱窗的规律漏进来了"
    assert after["kinds"] == before["kinds"], "橱窗的种类漏进来了"
    assert after["entities"] == before["entities"], "橱窗的橡树漏进来了"
    assert after["items"] == before["items"], "橱窗的物品漏进来了"


def test_同一份世界文件连开两次不会再加一遍(base_file, patch_file, caplog):
    """幂等。第二次一样都不新增,而且**不说话** —— 每次重启都刷一行汇总,
    等于训练人忽略它。"""
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    _boot("w3", client, world_file=base_file, ticks=3)
    first = _boot("w3", client, world_file=patch_file)
    with caplog.at_level(logging.INFO, logger="anima_world.__main__"):
        second = _boot("w3", client, world_file=patch_file)
    for key in ("roster", "rules", "locations", "kinds", "entities", "items",
                "visibility", "stock_places", "balances", "inventories"):
        assert second[key] == first[key], f"{key} 在第二次开机时又被加了一遍"
    assert second["memory_count"] == first["memory_count"], "记忆被重播了一遍"
    assert MERGE_MARKER not in " ".join(r.getMessage() for r in caplog.records), (
        "一样都没加还是刷了一行汇总"
    )
