"""批 3c §2.2:**别人的 GM 笔记不出门,别人的世界也不许被你的剧情动**。

🔴 **先立闸,再开交织** —— 顺序是承重的:先开口子再补闸,中间那段时间里
**泄漏是真的发生了,而事件日志抹不掉**(任务单 §3.5)。

裁决(调度台 2026-09-07,按投递路实测改口)四款:
剥 `why` · 载荷带 `player_id` · **效果只许作用于当事人** · 拒了记
`director_log_refused`。
⚠️ 原文那条闸写的是「插件试图 **hail** 另一个玩家」,而量完发现**插件 hail
不了任何人**(`effects` 只有 `set`/`emit`/`link`/`unlink`/`transfer`,
`hail` 是拍的 op)—— 那条闸会永远绿。这里打的是**实测出来的真口子**。
"""
from __future__ import annotations

import pathlib

import pytest

from _worldfile import open_world_at, write_seed_file

_SPY = {
    "id": "spy", "version": "1.0.0", "label": "窥探",
    "facts": {"记号": {"bearer": "agent", "shape": "number", "default": 0.0,
                      "visibility": "self"}},
    "edges": {"盯上": {"from": "agent", "to": "agent"}},
    "triggers": [{"id": "顺藤摸瓜", "on": {"event": "director_log"},
                  "effects": [{"link": {"type": "spy.盯上", "from": "self",
                                        "to": "player:p2"}}]}],
}
_SELF = {
    "id": "mine", "version": "1.0.0", "label": "自己",
    "facts": {"记号": {"bearer": "agent", "shape": "number", "default": 0.0,
                      "visibility": "self"}},
    "edges": {"记住": {"from": "agent", "to": "agent"}},
    "triggers": [{"id": "记自己", "on": {"event": "director_log"},
                  "effects": [{"link": {"type": "mine.记住", "from": "self",
                                        "to": "event.who"}}]}],
}
_BARE = {
    "agents": [{"id": "阿岚", "name": "阿岚", "location": "cafe",
                "personality": "安静"}],
    "locations": [{"id": "cafe", "name": "咖啡馆", "description": "小店"}],
}


def _world(tmp_path, *plugins, name="x"):
    path = write_seed_file(tmp_path / f"{name}.cyberworld",
                           {**_BARE, "plugins": [dict(p) for p in plugins]})
    return open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                         force_mock_llm=True)


def _one_beat(world, pid="p1", why="GM 的私话"):
    agent = next(iter(world.scheduler.agents))
    world._director_apply(
        pid, {"move": "reveal", "who": agent, "line": "她欲言又止", "why": why,
              "promise": "", "stake": None, "source": "mock"},
        tension_before=0.3, phase="setup", tick=int(world.scheduler.clock),
        place="cafe", thread=None, pin_ticks=12, due_ticks=0, capped=False,
        forbidden_ops=set(), recap=[], place_name="咖啡馆")
    world.tick(2)


def _edges(world):
    store = world.scheduler.edge_store
    return {t: store.all(t) for t in store.types()}


def test_订director_log的插件_动不了别的玩家_而且拒了留痕(tmp_path):
    """🔴 **这个口子是实测出来的,不是想出来的**:改闸之前跑同一份插件,
    边真的连上了(`spy.盯上: [('agent:player:p1', 'player:p2', {})]`)——
    **拿 A 的剧情去动 B 的世界**,而 `plugin list` 看不出来。
    """
    with _world(tmp_path, _SPY, name="spy") as world:
        for pid in ("p1", "p2"):
            world.player_move(pid, "cafe")
        world.tick(2)
        _one_beat(world, "p1")

        assert not any(_edges(world).values()), (
            f"边连上了 —— 拿 A 的剧情动了 B 的世界:{_edges(world)}")
        refused = [e for e in world.events()
                   if e["type"] == "director_log_refused"]
        assert refused, "拒了却一声不吭 —— 作者会以为那条规律在跑"
        row = refused[-1]["payload"]
        assert row["reason"] == "cross_player", row
        assert row["plugin"] == "spy" and row["trigger"] == "顺藤摸瓜", row
        assert row["because"] == "director_log", row


def test_只动自己那一支_照旧放行(tmp_path):
    """⚠️ **这道闸拦的是「跨玩家」,不是「插件不许动边」** ——
    一条只连当事人自己的触发器要照常跑。
    **一道拦过头的闸,和一道漏掉的闸一样坏**,只是坏的方向相反。
    """
    with _world(tmp_path, _SELF, name="mine") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        _one_beat(world, "p1")

        linked = _edges(world).get("mine.记住") or []
        assert linked, f"只动自己的那一支被拦了:{_edges(world)}"
        assert not [e for e in world.events()
                    if e["type"] == "director_log_refused"], "误伤"


def test_触发器看不到why_而日志里那一条原样留着(tmp_path):
    """🔴 `why` 是**编剧写给创作者的一句话** —— 一条插件触发器订得到它,
    就等于把 GM 的笔记摊开。

    ⚠️ **剥的是交给触发器的那一份,不是日志里那一条**:运维台与创作者读的是
    `history`,一个字都不该少。**就地改载荷会让日志和它自己的投影对不上。**
    """
    seen: list[dict] = []

    with _world(tmp_path, _SELF, name="why") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        real = world.scheduler._fire_trigger

        def _spy(trigger, event, *a, **kw):
            seen.append(dict(event.get("payload") or {}))
            return real(trigger, event, *a, **kw)

        world.scheduler._fire_trigger = _spy
        _one_beat(world, "p1", why="这一拍是为了把他往楚子航那边推")

        assert seen, "触发器一次都没被点到"
        for payload in seen:
            assert "why" not in payload, f"GM 那句话漏给插件了:{payload}"
            # 而当事人是谁要留着 —— 第 3 款靠它认人
            assert payload.get("player_id") == "p1", payload

        logged = [e["payload"] for e in world.events()
                  if e["type"] == "director_log"]
        assert logged and "楚子航" in str(logged[-1].get("why")), (
            f"日志里那一条被就地改了:{logged[-1] if logged else None}")
