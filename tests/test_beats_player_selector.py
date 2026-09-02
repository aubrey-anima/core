"""剧情拍指得到「任何玩家」了(3.9.0,2026-09-02 裁决 §2.2)。

世界文件写在玩家出现之前,所以一条拍**写不出**玩家的 id。3.8.0 上写
`agent_id: "player"` 的真实下场不是"少一个特性",是一个**静默作废**的洞:
离线两扇门答 `loadable: true`、开机不报错、运行期一句 warning 跳过,而 `_fire_beat`
照样 `mark_fired` 并写下 `beat_fired` —— **这一拍永久失效,而且重启不重放**。

这一族钉四件:

- **`for_each` 是声明,声明本身就是开关** —— 不写它的老拍逐字不变。
- **零点是他自己进来那一天**:老板在世界第 40 天点进来,要拿到的是**他的**第 1 天
  那封信,不是一条 39 天前就烧掉的拍。而那个"第一次"记在**账本**上,不记在带 TTL
  的在场上(挂在在场上的话,他的第一周每次登录重开一遍)。
- **`once` 按玩家各一次**:按拍 id 记账的话,第一个玩家的到来把整份剧情烧光。
- **每一格写不写得下 `player` 是加载期判的**,拒了当场说,没有第三种"静默跳过"。
"""
from __future__ import annotations

import pytest

from _worldfile import open_world_at, write_seed_file

from anima_world.beats import (
    BEAT_KEYS,
    PLAYER_ALLOWED_OP_FIELDS,
    PLAYER_ALLOWED_PREDICATE_FIELDS,
    PLAYER_TOKEN,
    BeatDirector,
    BeatScript,
    BeatScriptError,
    bind_player,
    is_per_player,
)
from anima_world.types import Projection
from anima_world.world_time import world_time


def _script(*beats) -> dict:
    return {"beats": list(beats)}


def _load(*beats) -> BeatScript:
    return BeatScript.from_data(_script(*beats))


LETTER = {
    "id": "day1-letter", "for_each": {"node": "player"},
    "trigger": {"at": {"day": 1}},
    "payload": [{"op": "grant_item", "agent_id": "player", "item_id": "letter"}],
}


# ── 加载期:声明本身就是开关,而收拒表逐格有结论 ──────────────────────────────

def test_for_each_只有一种写法_而且是闭集():
    _load(LETTER)                      # 收
    for bad in ({"node": "actor"}, {"node": "player", "extra": 1}, "player", {}):
        with pytest.raises(BeatScriptError) as err:
            _load({**LETTER, "for_each": bad})
        assert "for_each" in str(err.value)


def test_一条拍的顶层键是闭集_不认识的当场报():
    """3.8.0 一个都不查 —— 于是写了 `for_each` 的包在那一版上开得了机再静默烧掉。
    这个闭集救不了 3.8.0,它救的是**下一个**不认识的键。"""
    with pytest.raises(BeatScriptError) as err:
        _load({**LETTER, "fro_each": {"node": "player"}})
    assert "fro_each" in str(err.value)
    assert set(BEAT_KEYS) == {"id", "for_each", "trigger", "payload", "once"}


@pytest.mark.parametrize("op,field", [
    ("sentiment_delta", "target"), ("r_type", "target"),
    ("pay", "from"), ("pay", "to"), ("grant_item", "agent_id"),
])
def test_收下的那几个op格_逐个真的收(op, field):
    base = {
        "sentiment_delta": {"op": "sentiment_delta", "as": "夏", "target": "夏", "delta": 0.1},
        "r_type": {"op": "r_type", "as": "夏", "target": "夏", "r_type": "朋友"},
        "pay": {"op": "pay", "from": "__world__", "to": "夏", "amount": 5},
        "grant_item": {"op": "grant_item", "agent_id": "夏", "item_id": "letter"},
    }[op]
    _load({**LETTER, "payload": [{**base, field: PLAYER_TOKEN}]})


@pytest.mark.parametrize("op,field,why", [
    ("memory", "agent_id", "没有记忆表"),
    ("persona_update", "agent_id", "没有 persona"),
    ("agent_leave", "agent_id", "在场"),
    ("agent_return", "agent_id", "在场"),
    ("sentiment_delta", "as", "主语只能是角色"),
    ("r_type", "as", "主语只能是角色"),
])
def test_拒掉的那几个op格_逐个当场拒_而且说得出理由(op, field, why):
    base = {
        "memory": {"op": "memory", "agent_id": "夏", "summary": "x"},
        "persona_update": {"op": "persona_update", "agent_id": "夏", "spec": {}},
        "agent_leave": {"op": "agent_leave", "agent_id": "夏"},
        "agent_return": {"op": "agent_return", "agent_id": "夏", "location": "cafe"},
        "sentiment_delta": {"op": "sentiment_delta", "as": "夏", "target": "夏", "delta": 0.1},
        "r_type": {"op": "r_type", "as": "夏", "target": "夏", "r_type": "朋友"},
    }[op]
    with pytest.raises(BeatScriptError) as err:
        _load({**LETTER, "payload": [{**base, field: PLAYER_TOKEN}]})
    text = str(err.value)
    assert PLAYER_TOKEN in text and why[:4] in text, text


@pytest.mark.parametrize("pred,ok_field", [
    ({"pred": "co_located", "agents": ["夏", PLAYER_TOKEN]}, "agents"),
    ({"pred": "money", "agent": PLAYER_TOKEN, "op": "gte", "value": 1}, "agent"),
    ({"pred": "has_item", "agent": PLAYER_TOKEN, "item": "letter"}, "agent"),
    ({"pred": "sentiment", "as": "夏", "target": PLAYER_TOKEN, "op": "gte", "value": 0.1}, "target"),
])
def test_收下的那几个谓词格(pred, ok_field):
    _load({**LETTER, "trigger": {"at": {"day": 1}, "when": [pred]}})
    assert ok_field in PLAYER_ALLOWED_PREDICATE_FIELDS[pred["pred"]]


@pytest.mark.parametrize("pred", [
    {"pred": "need", "agent": PLAYER_TOKEN, "need": "energy", "op": "gte", "value": 1},
    {"pred": "memory", "agent": PLAYER_TOKEN, "contains": "信"},
    {"pred": "sentiment", "as": PLAYER_TOKEN, "target": "夏", "op": "gte", "value": 0.1},
])
def test_拒掉的那几个谓词格(pred):
    with pytest.raises(BeatScriptError):
        _load({**LETTER, "trigger": {"at": {"day": 1}, "when": [pred]}})


def test_没写for_each的拍_这张表整个不生效():
    """声明本身就是开关:老拍写 `player` 照旧只是一个 id(归 warnings,不归拒绝)。"""
    _load({"id": "old", "trigger": {"at": {"day": 1}},
           "payload": [{"op": "memory", "agent_id": "player", "summary": "x"}]})


def test_两张收拒表和op_谓词的全集对得上():
    """表里出现一个不存在的 op / 谓词,等于一格永远走不到的分支。"""
    from anima_world.beats import VALID_OPS, _VALID_PREDICATES
    assert set(PLAYER_ALLOWED_OP_FIELDS) <= VALID_OPS
    assert set(PLAYER_ALLOWED_PREDICATE_FIELDS) <= _VALID_PREDICATES


# ── 绑定:换的是 `player:<id>`,不是 `agent:player:<id>` ──────────────────────

def test_绑定换的是关系账本那个形状():
    op = {"op": "sentiment_delta", "as": "夏", "target": "player", "delta": 0.1}
    assert bind_player(op, "player:p1")["target"] == "player:p1"
    assert bind_player(op, "")["target"] == "player", "世界级的拍一个字都不该动"
    pred = {"pred": "co_located", "agents": ["夏", "player"]}
    assert bind_player(pred, "player:p1")["agents"] == ["夏", "player:p1"]


# ── 记账:once 按玩家各一次,零点按他自己那一天 ────────────────────────────────

def _due(script, day, players, fired=()):
    director = BeatDirector(script, fired=set(fired))
    return director, [
        (b["id"], s) for b, s in director.due_beats(
            world_time(day * 288), Projection(), {}, None, players=players)
    ]


def test_两个玩家各响一次_而且互不解锁():
    script = _load(LETTER)
    director, due = _due(script, day=9, players={"p1": 8, "p2": 8})
    assert due == [("day1-letter", "player:p1"), ("day1-letter", "player:p2")]
    director.mark_fired("day1-letter", "player:p1")
    _, again = _due(script, day=9, players={"p1": 8, "p2": 8},
                    fired={("day1-letter", "player:p1")})
    assert again == [("day1-letter", "player:p2")], "一个人响过不该把别人的也烧掉"


def test_零点是他自己进来那一天():
    """世界第 40 天点进来的人,拿到的仍然是**他的**第 1 天。"""
    script = _load(LETTER)
    _, early = _due(script, day=40, players={"late": 40})
    assert early == [], "他今天才来,第 1 天还没到"
    _, later = _due(script, day=41, players={"late": 40})
    assert later == [("day1-letter", "player:late")]


def test_名册空的时候一条都不响_但也一条都不烧():
    script = _load(LETTER)
    director, due = _due(script, day=99, players={})
    assert due == []
    assert director.fired == set(), "没人来 ≠ 这一拍作废"


def test_老的beat_fired事件读成世界级():
    """载荷里没有 `for` 那一格的是 3.9.0 之前写下的 —— 那正是它们当初的语义。"""
    world_beat = {"id": "w", "trigger": {"at": {"day": 0}},
                  "payload": [{"op": "location_desc", "location": "cafe", "description": "x"}]}
    script = _load(world_beat)
    director = BeatDirector(script, fired={"w"})          # 老形状:裸 id
    assert director.fired == {("w", "")}
    assert director.due_beats(world_time(288), Projection(), {}, None) == []


def test_有per_player拍的脚本永远pending():
    """那种拍等的是**还没来的人**,而"以后不会再有新玩家"引擎无从知道。"""
    script = _load(LETTER)
    director = BeatDirector(script, fired={("day1-letter", "player:p1")})
    assert director.has_pending() is True
    assert is_per_player(script.beats[0]) is True


def test_after_要么是为我响的_要么是世界级的():
    opening = {"id": "opening", "trigger": {"at": {"day": 0}},
               "payload": [{"op": "location_desc", "location": "cafe", "description": "x"}]}
    second = {"id": "second", "for_each": {"node": "player"},
              "trigger": {"after": "opening"},
              "payload": [{"op": "grant_item", "agent_id": "player", "item_id": "letter"}]}
    script = _load(opening, second)
    _, blocked = _due(script, day=1, players={"p1": 0})
    assert ("second", "player:p1") not in blocked, "开场还没响,第二拍不该响"
    _, unlocked = _due(script, day=1, players={"p1": 0}, fired={("opening", "")})
    assert ("second", "player:p1") in unlocked


# ── 真世界一趟:开机 → 玩家进来 → 拍响在他自己的第 1 天 ───────────────────────

SEED = {
    "agents": [{"id": "芬格尔", "name": "芬格尔", "location": "宿舍", "personality": "话痨"}],
    "locations": [{"id": "宿舍", "name": "学生宿舍", "description": "三个人的房间。"}],
    "beats": [
        {"id": "见面", "for_each": {"node": "player"},
         "trigger": {"at": {"day": 1}, "when": [{"pred": "co_located",
                                                 "agents": ["芬格尔", "player"]}]},
         "payload": [{"op": "sentiment_delta", "as": "芬格尔", "target": "player",
                      "delta": 0.2, "reason": "他搭了话"}]},
    ],
}


def test_真的跑一趟_玩家进来之后他自己的第一天那一拍才响(tmp_path):
    path = write_seed_file(tmp_path / "seed.json", SEED)
    with open_world_at(tmp_path / "w.db", world_file=path) as w:
        w.player_move("p1", "宿舍")
        w.tick(2)
        proj = w.scheduler._memory_projection
        assert "p1" in proj.players_joined, "他进来这件事得记在账本上"
        join_day = proj.players_joined["p1"]
        assert w.scheduler.beat_director.fired == set(), "他的第 1 天还没到"

        w.tick(288)                                   # 走过一个世界日
        w.player_move("p1", "宿舍")                    # 他还在那儿(在场有 TTL)
        w.tick(2)
        assert ("见面", "player:p1") in w.scheduler.beat_director.fired
        rel = proj.relations.get(("芬格尔", "player:p1"))
        assert rel is not None and rel.sentiment > 0, "拍真的落在了这个玩家身上"
        assert proj.players_joined["p1"] == join_day, "零点只认第一次"


def test_beat_fired事件带着为谁响的那一格(tmp_path):
    path = write_seed_file(tmp_path / "seed2.json", SEED)
    with open_world_at(tmp_path / "w2.db", world_file=path) as w:
        w.player_move("p9", "宿舍")
        w.tick(290)
        w.player_move("p9", "宿舍")
        w.tick(2)
        rows = [e for e in w.scheduler.event_log.replay() if e.type == "beat_fired"]
        assert rows and rows[-1].payload["for"] == "player:p9"


def test_下线再上线不会把他的第一周重开一遍(tmp_path):
    """在场带 TTL —— 拿 `fresh` 当"第一次"的话,他每次登录都是新人。"""
    path = write_seed_file(tmp_path / "seed3.json", SEED)
    with open_world_at(tmp_path / "w3.db", world_file=path) as w:
        w.player_move("p7", "宿舍")
        w.tick(1)
        first = dict(w.scheduler._memory_projection.players_joined)
        w.presence_store.forget("p7")   # TTL 到头 = 那一行没了,`create` 会再答 fresh
        w.tick(300)
        w.player_move("p7", "宿舍")
        w.tick(1)
        assert w.scheduler._memory_projection.players_joined == first
        joins = [e for e in w.scheduler.event_log.replay() if e.type == "player_join"]
        assert len(joins) == 1, "一辈子一条"


def test_升级那一刻正在线的玩家也补得上零点(tmp_path):
    """他的在场行还在(`fresh` 是 False)—— 把这一笔挂进 `fresh` 分支的话,
    他要等 TTL 过期、再回来一次才有零点,而在那之前他每条拍都没有零点可算。"""
    path = write_seed_file(tmp_path / "seed4.json", SEED)
    with open_world_at(tmp_path / "w4.db", world_file=path) as w:
        # 直接建在场行 = 3.9.0 之前就在世界里的那种人:有行,没有 player_join。
        w.presence_store.create("old-timer", {"role": "", "location": "宿舍"})
        assert "old-timer" not in w.scheduler._memory_projection.players_joined
        w.player_move("old-timer", "宿舍")
        assert "old-timer" in w.scheduler._memory_projection.players_joined, (
            "这一笔多半被挂进了 `fresh` 分支里"
        )
