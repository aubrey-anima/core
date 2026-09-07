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
        # 🔴 载荷键表以契约为准 —— 少一格,消费方照它写解析就永远读不到
        # (`test_event_payload_keys` 把这条事件登记成「归这儿盯」)。
        from anima_world.events import EVENT_PAYLOAD_KEYS

        assert set(row) <= set(EVENT_PAYLOAD_KEYS["director_log_refused"]), (
            f"真发的键不在表里:{sorted(set(row) - set(EVENT_PAYLOAD_KEYS['director_log_refused']))}")
        assert set(EVENT_PAYLOAD_KEYS["director_log_refused"]) == set(row), (
            f"表里报了没发出来的格:"
            f"{sorted(set(EVENT_PAYLOAD_KEYS['director_log_refused']) - set(row))}")


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


# ── §2.1 交织的第一条路径:**撞见**,不是共享剧情 ───────────────────────────

def _showcase(tmp_path, name):
    return open_world_at(tmp_path / f"{name}.db", force_mock_llm=True)


def _act(world, pid):
    """他真做一件事 —— 走 `player_tool` 那条真路。**这儿没得做就说出来**,
    别让一条 `StopIteration` 冒充"这条路走不通"。"""
    menu = world.player_options(pid)
    for target in menu["targets"]:
        for verb in target["verbs"]:
            if verb["available"]:
                world.player_tool(pid, "interact",
                                  {"target": target["id"], "verb": verb["verb"]})
                return target["id"]
    raise AssertionError(
        f"{pid} 站的地方({menu['location']})没有一样点得动的东西 —— 换个地方")


def test_当着他的面做一件事_他那一屏开口并指名道姓(tmp_path):
    """🔴 **老板那句「玩家 A 的选择成为玩家 B 的事件」的可验形式**(§2.1)。

    判据是任务单写死的那两条,**一正一反**:
    · A 当着 B 的面做一件事 → B 下一屏 `source != "cached"`,
      而且屏上那句的主语是 **A 的显示名**;
    · A 在**别处**做同一件事 → B 那一屏 `cached`。

    ⚠️ 反那一条是承重的:少了它,「一有人动手就叫醒所有人」也能让正那条绿 ——
    而那不是撞见,是广播。
    """
    with _showcase(tmp_path, "meet") as world:
        for pid, name in (("p1", "楚子航"), ("p2", "路明非")):
            world.player_move(pid, "cafe", display_name=name)
        world.tick(3)
        world.host_turn("p1")
        world.host_turn("p2")

        _act(world, "p1")
        seen = world.host_turn("p2")
        assert seen["scene"]["source"] != "cached", (
            "A 当着 B 的面做了一件事,而 B 那一屏纹丝不动")
        assert "楚子航" in seen["scene"]["text"], (
            f"屏上没说是谁做的 —— 别拿 id,也别兜底成「有人」:"
            f"{seen['scene']['text']}")

        # 反那一条:A 走去别处,再做一件事。
        # ⚠️ 中间那两次 `host_turn("p2")` 是**有意的**:A 走出去这件事
        # B 当然看得见(`travel` 的 `loc` 是**出发地**),那一屏该开 ——
        # 要验的是**他到了别处之后做的那件事**不该再叫醒 B。
        world.host_turn("p2")
        world.player_walk("p1", "workshop")
        for _ in range(60):        # 等他真到站(在路上时那儿没东西可点)
            world.tick(1)
            if world.player_location("p1") == "workshop":
                break
        assert world.player_location("p1") == "workshop", "他没走到"
        world.host_turn("p2")
        # ⚠️ 工作室里未必有点得动的东西 —— 用**走一步**当那件"在别处做的事"
        # (`travel` 也在 `CROSSING_EVENT_TYPES` 上,而它的 `loc` 是出发地
        # `workshop`,不是 B 站的 `cafe`)。判据一样,而且不挑地方。
        world.player_walk("p1", "cafe")
        away = world.host_turn("p2")
        assert away["scene"]["source"] == "cached", (
            f"A 在别处做的事把 B 的屏叫醒了 —— 那是广播,不是撞见:"
            f"{away['scene']['source']}")


def test_撞见不许把别人的线带过来(tmp_path):
    """🔴 **§2.1 的总口径**:交织的价值在「你的选择被另一个人撞见」,
    **不在「两个人共享一份剧情」**。

    所以 B 那一屏上可以有「楚子航端详了老橡树」,**但不许有** A 的
    `promise` / `why` / 张力 / 线 —— 那是别人的 GM 笔记(§2.2)。
    """
    with _showcase(tmp_path, "priv") as world:
        agent = next(iter(world.scheduler.agents))
        for pid, name in (("p1", "楚子航"), ("p2", "路明非")):
            world.player_move(pid, "cafe", display_name=name)
        world.tick(3)
        world.host_turn("p1")
        world.host_turn("p2")
        world._director_apply(
            "p1", {"move": "reveal", "who": agent, "line": "她欲言又止",
                   "why": "把他往楚子航那边推", "promise": "那本旧相册",
                   "stake": {"kind": "relation", "amount": 0.2, "what": "她的信任"},
                   "source": "mock"},
            tension_before=0.3, phase="setup", tick=int(world.scheduler.clock),
            place="cafe", thread=None, pin_ticks=12, due_ticks=50, capped=False,
            forbidden_ops=set(), recap=[], place_name="咖啡店")
        _act(world, "p1")

        screen = world.host_turn("p2")["scene"]["text"]
        story = world.player_story("p2")

    for secret in ("那本旧相册", "把他往楚子航那边推", "她的信任"):
        assert secret not in screen, f"别人的线漏到 B 的屏上了:{secret}"
        assert secret not in str(story), f"别人的线漏进 B 的故事页了:{secret}"
    assert story["threads"] == [], "B 平白多了一条不是他的线"
    # ⚠️ **B 自己那几拍是该有的**:他看见 A 做了一件事,那是他这一屏的输入
    # (§2.3)—— 判据是「这几拍**都是他的**」,不是「他一拍都没有」。
    # 上一版写成 `== []`,那是把「不许读到别人的」和「不许有自己的」记成了一件事。
    for row in story["recent_log"]:
        assert (row["payload"] or {}).get("player_id") == "p2", (
            f"A 的拍漏进 B 的故事页了:{row['payload']}")


def test_三张表有意不同_而分岔本身要有一处写下来():
    """🔴 三个问题,三张表 —— 我在 3.11.1 / 3.11.2 / 3.11.3 为「两处各写各的」
    栽过三次,所以这一张**从代码里生成不出来**,必须显式写下来。

    ⚠️ 同时钉一句:**看得见的动作**才进 `CROSSING_EVENT_TYPES`。
    钱包变了、拿到东西都不许进 —— 站在旁边的人看不见别人的账,
    把它们放进来就是**拿交织当剧透**。
    """
    from anima_world import host as H

    assert "payment" not in H.CROSSING_EVENT_TYPES
    assert "item_transfer" not in H.CROSSING_EVENT_TYPES
    assert "director_log" not in H.CROSSING_EVENT_TYPES
    for kind in H.CROSSING_EVENT_TYPES:
        assert kind in H.PLAYER_MOVE_EVENT_TYPES, (
            f"{kind} 不是「他自己动了手」那一族 —— 交织收的是动作,不是后果")
    # 而它确实和那两张不是同一张
    assert set(H.CROSSING_EVENT_TYPES) != set(H.PLAYER_MOVE_EVENT_TYPES)
    assert set(H.CROSSING_EVENT_TYPES) != set(H.RECAP_EVENT_TYPES)
