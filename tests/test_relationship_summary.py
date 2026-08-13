"""**四个 -1~1 的浮点数不该直接给玩家看。**

## 缺口原本长什么样

一次真人试玩里,玩家那一侧唯一能拿到的关系数据是 `World.state()["relations"]`:

    "wan|8f3c-…": {"sentiment": 0.668, "trust": 0.0,
                   "affection": 0.0, "respect": 0.0}

四个裸浮点数。恋爱陪伴产品里把这个显示出来 = 把一段关系变成一根进度条,而**刷分
是这类产品最不该长出来的东西**。不显示又更坏:玩家聊了两个小时,不知道有没有发生
过任何事 —— 而世界里其实发生了。

引擎这一侧的责任不是"别给数字",是**给得出一句人话**:她这会儿把你当什么、粗到
什么档、以及**上一次改变它的是哪一件事**。最后那半句是这一层的分量所在:一句
"你们更亲近了"如果说不出出处,和一根进度条没有区别 —— 玩家学不到"我做了什么让
它变的"。

## 三条不变量,每条一个测试

1. **人话是派生的,数字仍是契约。** `state()["relations"]` 一个字不动(宿主的
   代码在读它),人话是**加**出来的一层。
2. **档是算出来的,不是存的。** 和 `memory_triggers.band()` 同一个函数、同一份
   `BAND_NAMES` —— 另写一份阈值表的下场是同一段关系在两处显示成两档。
3. **说得出出处。** 上一次改变它的那条事件:哪一 tick、多大、哪一场对话、
   那场对话讲了什么。查不到就**明说查不到**,不编。
"""
from __future__ import annotations

import json
import time

import pytest

from _worldfile import open_world_at, run_cli, redis_for

from anima_world.memory_triggers import BAND_NAMES, band


def _bump(world, agent_id, target, delta, **payload):
    return world.scheduler._record_and_deliver({
        "type": "state_change", "who": agent_id,
        "payload": {"kind": "sentiment_delta", "as": agent_id, "target": target,
                    "delta": delta,
                    "as_name": world.scheduler.agent_display_name(agent_id),
                    "target_name": payload.pop("target_name", "阿檀"),
                    **payload},
    })


@pytest.fixture
def world(tmp_path):
    w = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    yield w
    w.close()


@pytest.fixture
def agent(world):
    return next(iter(world.scheduler.agents))


def _wait_for(predicate, timeout=5.0):
    """判定跑在线程池上 —— 等**产物**出现,别等固定的时间片。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = predicate()
        if got:
            return got
        time.sleep(0.01)
    return predicate()


# ── 一句能直接显示的话 ──────────────────────────────────────────────────────


def test_给的是一句人话和一个档_不是四个浮点数(world, agent):
    _bump(world, agent, "p1", 0.55)
    out = world.relationship_summary(agent, "p1")
    assert out["band_name"] in BAND_NAMES
    assert out["band"] == band(out["axes"]["sentiment"])
    assert isinstance(out["summary"], str) and out["summary"].strip()
    assert out["agent_name"] and out["other_name"] == "阿檀", (
        "一句带着 uuid 的人话,和一个浮点数一样不能给人看"
    )
    # 数字**照旧给得出来** —— 宿主要画什么是宿主的事,但它得住在一个显式的
    # 格子里,而不是这一层的返回值本身。
    assert set(out["axes"]) == {"sentiment", "trust", "affection", "respect"}


def test_那句话跟着档走(world, agent):
    """档变了话没变的话,这一层等于把同一句话印给每一段关系。"""
    _bump(world, agent, "cold", -0.7, target_name="沈")
    _bump(world, agent, "warm", 0.9, target_name="檀")
    cold = world.relationship_summary(agent, "cold")
    warm = world.relationship_summary(agent, "warm")
    assert cold["band"] < warm["band"]
    assert cold["summary"] != warm["summary"]
    assert cold["band_name"] == "宿敌" and warm["band_name"] == "挚交"


def test_没有来往的两个人是没有关系_不是敌意(world, agent):
    """0 不是负数。空关系报成"交恶"的话,一个刚进来的新玩家开局就被讨厌。

    **档位那一格同样不许填。** `exists` 是 False 时四个轴都是 0.0,而 0.0 落在
    档表上是「不远不近」—— 于是这一行一边说"没有关系",一边给出一个档名。宿主拿到
    的是一份自相矛盾的载荷,而它只要照着 `band_name` 渲染(那是这一格的用途),
    一个刚进门的新玩家开局就被每个角色打上一个档名。空白不是一个档位。
    """
    out = world.relationship_summary(agent, "stranger")
    assert out["exists"] is False
    assert out["axes"]["sentiment"] == 0.0
    assert out["band"] is None, "没有关系的一对不该落在档表上"
    assert out["band_name"] == "", "空白不是一个档位"
    assert out["last_change"] is None
    assert "还不认识" in out["summary"] or "没有来往" in out["summary"]


def test_起点那一档的名字说的是距离不是她的态度(world, agent):
    """−0.2…0.2 是**每一段关系的起点**,而档名直接印在玩家那一屏上。

    线上现场:刚跟她热络地聊完一场(她冒雨给你煮了杯热的、叫你进棚子躲雨),
    关系面板上一行写着档名、下一行写着「上一次两个人的来往让它更近了一步」。
    档名从前是「淡漠」—— 同一屏两个情绪,而玩家会信上面那个词。「淡漠」是一句
    **判词**(她对你冷淡),可这一档的真话是"你俩还不熟",两回事。

    这条守的是那个词的**性质**,不是那三个字:档名可以再改,但它不许再变回一句
    对她态度的判定 —— 那正是 `_closeness_phrase` 那一半已经从散文里拿掉的东西。
    """
    _bump(world, agent, "新客", 0.03, conversation_summary="大雨天她给他煮了杯热的")
    out = world.relationship_summary(agent, "新客")
    assert out["band"] == 2, out
    assert out["exists"] is True
    assert out["band_name"] not in ("淡漠", "冷淡", "无感"), (
        f"起点那一档不该是一句判词:{out['band_name']!r}"
    )
    # 同一屏上的另一行说的是"更近了一步" —— 两行不许一个说远一个说冷。
    assert "更近" in out["summary"], out["summary"]


def test_档和引擎那份是同一个函数(world, agent):
    """另写一份阈值表的话,同一段关系会在两个地方显示成两档。"""
    for value in (-0.61, -0.6, -0.2, 0.2, 0.5, 0.8, 0.99):
        who = f"x{value}"
        _bump(world, agent, who, value)
        out = world.relationship_summary(agent, who)
        assert out["band"] == band(value), value
        assert out["band_name"] == BAND_NAMES[band(value)]


# ── 说得出出处 ──────────────────────────────────────────────────────────────


def test_上一次是什么改变了它(world, agent):
    """一句"你们更亲近了"如果说不出出处,和一根进度条没有区别 —— 玩家学不到
    "我做了什么让它变的"。"""
    _bump(world, agent, "p1", 0.2)
    last = _bump(world, agent, "p1", 0.4, conversation_id=7,
                 conversation_summary="他把伞留给了她")
    out = world.relationship_summary(agent, "p1")
    change = out["last_change"]
    assert change is not None
    assert change["seq"] == last["seq"], "报的不是最后那一条"
    assert round(change["delta"], 6) == 0.4
    assert change["direction"] == "up"
    assert change["conversation_id"] == 7
    assert change["summary"] == "他把伞留给了她"
    assert change["tick"] == last["ts"]


def test_出处查不到时明说查不到_不编(world, agent):
    _bump(world, agent, "p1", 0.4)
    change = world.relationship_summary(agent, "p1")["last_change"]
    assert change["conversation_id"] is None
    assert change["summary"] == ""


def test_只认这一对人的那条(world, agent):
    """别人和别人之间发生的事,不该被读成"你们俩之间刚发生了什么"。"""
    _bump(world, agent, "p1", 0.4)
    _bump(world, agent, "p2", 0.9)
    change = world.relationship_summary(agent, "p1")["last_change"]
    assert round(change["delta"], 6) == 0.4


def test_一整份名单也拿得到(world, agent):
    """一个玩家要看"她们几个都怎么看我",一次一个地问等于让宿主自己攒。"""
    _bump(world, agent, "p1", 0.4)
    rows = world.relationship_summaries(other_id="p1")
    assert [r["agent_id"] for r in rows] == [agent]
    assert rows[0]["band_name"] == world.relationship_summary(agent, "p1")["band_name"]


# ── 事件本身要带得出出处 ────────────────────────────────────────────────────


def test_一场玩家对话判出来的事件带着会话号(world, agent):
    """`sentiment_delta` 此前只带 delta 和两个名字 —— 于是"是哪一场对话让它变的"
    在日志里查不回来,而那正是这一层唯一说得出的出处。"""
    world.scheduler.submit_user_chat_judgment(
        agent, "p9", "阿檀",
        [{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "在"}],
        conversation_id=42, conversation_summary="他说他明天还来",
    )
    rows = _wait_for(lambda: [
        e for e in world.scheduler.event_log.replay()
        if e.type == "state_change" and (e.payload or {}).get("target") == "p9"
    ])
    assert rows, "判定没落地"
    assert rows[-1].payload["conversation_id"] == 42
    assert rows[-1].payload["conversation_summary"] == "他说他明天还来"
    # 出处一路折进投影 —— 玩家那一侧问的是这个,不是日志。
    change = world.relationship_summary(agent, "p9")["last_change"]
    assert change["conversation_id"] == 42
    assert change["summary"] == "他说他明天还来"


# ── CLI 出口 ────────────────────────────────────────────────────────────────


def test_cli_打得出一段关系的人话(tmp_path):
    db = tmp_path / "w.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        agent_id = next(iter(world.scheduler.agents))
        _bump(world, agent_id, "p1", 0.55)

    result = run_cli("relationship", "--world-id", "w",
                     "--agent", agent_id, "--with", "p1", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["band_name"] in BAND_NAMES
    assert payload["summary"].strip()
    # 人看的那张脸不许把四个浮点数摊在脸上。
    plain = run_cli("relationship", "--world-id", "w",
                    "--agent", agent_id, "--with", "p1")
    assert plain.returncode == 0, plain.stderr
    assert payload["band_name"] in plain.stdout
    assert "0.55" not in plain.stdout


def test_cli_不给对方就打全部(tmp_path):
    db = tmp_path / "w.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        agent_id = next(iter(world.scheduler.agents))
        _bump(world, agent_id, "p1", 0.55)
        _bump(world, agent_id, "p2", -0.7)

    result = run_cli("relationship", "--world-id", "w", "--json")
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)["relationships"]
    assert {r["other_id"] for r in rows} >= {"p1", "p2"}


def test_cli_对不存在的世界一律拒绝(tmp_path):
    redis_for(tmp_path / "w.db")
    result = run_cli("relationship", "--world-id", "nope")
    assert result.returncode == 2


# ── 一个人有好几行联系态,而人是会改名的 ────────────────────────────────────


def test_名字挑最近一次来往那行_不是先读到的那行(world):
    """**同一个玩家在联系态里有好几行**(一个角色一行),名字挑最近的那一行。

    线上真的踩了:刘俊康 在 `yu` 那行还叫 `player-5688afd1`(他改名之前那次),
    在 `wan` 那行叫「刘俊康」。折叠用的是 `setdefault`,而底下是 `HGETALL` ——
    于是他读到的是「江晚和player-5688afd1还谈不上什么交情」,而**哪个名字露面
    全看这次的哈希顺序**。带 uuid 的人话和一个浮点数一样不能给人看,这条本来
    就写在 `_party_name` 的 docstring 里。
    """
    agents = list(world.scheduler.agents)
    early, late = agents[0], agents[1]
    contacts = world.scheduler.contact_store
    contacts.note_contact(early, "p9", tick=100, name="player-5688afd1")
    contacts.note_contact(late, "p9", tick=900, name="刘俊康")

    _bump(world, early, "p9", 0.3, target_name="player-5688afd1")
    _bump(world, late, "p9", 0.4, target_name="刘俊康")

    rows = world.relationship_summaries(other_id="p9")
    assert rows, "两条来往都记下了,关系名单不该是空的"
    for row in rows:
        assert row["other_name"] == "刘俊康", (
            f"{row['agent_id']} 这一行把他叫成 {row['other_name']!r} —— "
            "挑名字要按最近一次来往,不是任凭哈希顺序"
        )
        assert "player-5688afd1" not in row["summary"]


# ── 说过话了,但这一轮还没结算 ──────────────────────────────────────────────
#
# 判定器跑在**对话关闭**的时候(默认静默 600 秒才算关),而玩家聊完就去看那一屏。
# 于是三种状态被压成两种:
#
#     从没来往过    → 名单里没有这一行   ← 对
#     结算过        → 有一行,带档带人话  ← 对
#     来往过没结算  → 名单里没有这一行   ← **错,和"从没来往过"一模一样**
#
# 玩家刚跟她聊完一整场,点开关系那一屏,空的。他学到的是「聊天没有用」——
# 而恋爱陪伴产品里这是最要紧的一屏。这正是这个仓库最怕的坏法:两件不同的事
# 长得一模一样,而且一条错都不报。
#
# 修法不是提前判(判定要花一次 LLM 调用,而且没关的对话本来就还没讲完),是
# **把第三种状态说出来**:引擎知道他们说过话(`contact_store` 就是记这个的),
# 那就给一行,并且这一行诚实地说"还没落定"。


def test_聊过一场但还没结算_名单里也得有这一行(world):
    agent = next(iter(world.scheduler.agents))
    world.scheduler.contact_store.note_contact(agent, "p_new", tick=7, name="小新")

    rows = world.relationship_summaries(other_id="p_new")

    assert rows, "他刚跟她聊完一场,关系名单不该是空的 —— 空的等于告诉他聊天没用"
    row = rows[0]
    assert row["agent_id"] == agent
    assert row["other_name"] == "小新"
    assert row["met"] is True, "引擎知道这两个人说过话,这一格就得是 True"
    assert row["exists"] is False, "还没结算 —— `exists` 说的是判定落没落地,不许改它的意思"
    assert "还不认识" not in row["summary"], (
        f"说过话了还说「还不认识」是句假话:{row['summary']!r}"
    )


def test_从没来往过的人不该冒出一行(world):
    assert world.relationship_summaries(other_id="p_never") == []


def test_结算之后同一对只有一行(world):
    agent = next(iter(world.scheduler.agents))
    world.scheduler.contact_store.note_contact(agent, "p_both", tick=7, name="小双")
    _bump(world, agent, "p_both", 0.5, target_name="小双")

    rows = world.relationship_summaries(other_id="p_both")

    assert len(rows) == 1, f"联系态和判定各出一行的话玩家会看见两个她:{rows}"
    assert rows[0]["exists"] is True
    assert rows[0]["met"] is True


def test_单独问一对时也说得出说过话没有(world):
    agent = next(iter(world.scheduler.agents))
    world.scheduler.contact_store.note_contact(agent, "p_one", tick=7, name="小独")

    assert world.relationship_summary(agent, "p_one")["met"] is True
    assert world.relationship_summary(agent, "p_nobody")["met"] is False
