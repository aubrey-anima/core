"""一起做事:在**真世界**里走一遍。

`test_together.py` 验的是判定与 schema(纯函数,拆开来测)。这一份验的是它落进世界的
样子 —— 而这个仓库的教训正是这两者会分家:一个"照跑但给错东西"的 bug 只在真路径上
现形(创世投影折两遍那个,就是因为测试都直接调 `scheduler.perform_affordance`,
没走 `World.act()` 那条真路)。所以这里**一律走 `World.act()`**。
"""
from __future__ import annotations

import json

import pytest

from _worldfile import write_seed_file

from anima_world import together


_SEED = {
    "locations": [
        {"id": "cafe", "name": "咖啡店", "description": "拐角那家"},
        {"id": "yard", "name": "后院", "description": "堆着几只空木箱"},
    ],
    "agents": [
        {"id": "夏", "name": "苏晚夏", "location": "cafe", "personality": "开朗"},
        {"id": "遥", "name": "沈遥", "location": "cafe", "personality": "话少"},
        {"id": "柔", "name": "林柔", "location": "cafe", "personality": "温吞"},
    ],
    "kinds": [
        {"id": "agent", "quantities": {
            "体力": {"default": 100, "visibility": "self"},
            "随和": {"default": 1.0, "visibility": "self"},
        }},
        {"id": "bench", "gloss": "一条长椅", "quantities": {
            "坐过几回": {"default": 0.0, "visibility": "here"},
        }, "affordances": {
            "look": {},
            # 一下子的事:效果、代价、关系账全在同一 tick 里落完。
            "同坐": {
                "label": "一起坐会儿",
                "participants": {"min": 1, "max": 2},
                "requires": ["me_体力 >= 5"],
                "costs": {"体力": "me_体力 - 5"},
                "set": {"坐过几回": "坐过几回 + 1"},
            },
            # 长过程:代价当场付,效果与共同经历都等到收尾那一刻。
            "长谈": {
                "label": "坐着长谈",
                "participants": {"min": 1, "max": 2},
                "duration": 4,
                "requires": ["me_体力 >= 10"],
                "costs": {"体力": "me_体力 - 10"},
                "set": {"坐过几回": "坐过几回 + 10"},
            },
            # 单人的老样子 —— 对照组。
            "擦一擦": {"label": "擦一擦", "requires": ["me_体力 >= 1"],
                       "costs": {"体力": "me_体力 - 1"}},
        }},
    ],
    "entities": [{"id": "bench:oak", "name": "橡木长椅", "location": "cafe"}],
    # **关系不是可有可无的夹具。** 没有关系的两个人本来就不该坐下来一起待着
    # (那正是红线 ① 的默认答案),所以这个世界里他们是认识的。
    "relations": [
        {"a": "夏", "b": "遥", "sentiment": 0.6},
        {"a": "遥", "b": "夏", "sentiment": 0.6},
        {"a": "夏", "b": "柔", "sentiment": 0.6},
        {"a": "柔", "b": "夏", "sentiment": 0.6},
    ],
}


@pytest.fixture
def world(open_world, tmp_path):
    seed = json.loads(json.dumps(_SEED))
    w = open_world(world_file=write_seed_file(tmp_path / "w.cyberworld", seed))
    yield w


def _act(world, who, verb, **params):
    return world.act(who, "interact", {"target": "bench:oak", "verb": verb, **params},
                     surface="body")


def _stamina(world, who):
    return world.scheduler.stock_store.of(f"agent:{who}").get("体力")


def _seats(world):
    return world.scheduler.stock_store.of("bench:oak").get("坐过几回")


def _sentiment(world, a, b):
    rel = world.scheduler._memory_projection.relations.get((a, b))
    return rel.sentiment if rel is not None else 0.0


# ── 一个人调不动,而且说得出为什么 ──────────────────────────────────────────


def test_一个人调不动一件得有人一起的事(world):
    """静默降级成单人的话,作者写下的"这件事一个人做不成"一声不响地没了。"""
    result = _act(world, "夏", "同坐")
    assert result["ok"] is False
    assert "得有人一起" in result["error"]
    assert _seats(world) == 0, "拒绝时一个字都不该写"


def test_单人的能力给了名单也拒(world):
    """反过来的那一半。收下的话,调用方点名的那几个人一声不响地没了。

    **而且这一句要排在同意之前**:先问人的话,回执写的是"沈遥他不想",于是调用方
    去改名单,而错的是动词。
    """
    result = _act(world, "夏", "擦一擦", **{"with": ["遥"]})
    assert result["ok"] is False and "一个人做的事" in result["error"]


def test_陌生人本来就不会坐下来一起待着(world, open_world, tmp_path):
    """红线 ① 的默认答案。这**不是**这一层坏了 —— 一个跟你毫无来往的人不肯陪你
    坐会儿,正是"别人凭什么答应"该给出的回答。"""
    seed = json.loads(json.dumps(_SEED))
    seed.pop("relations")
    w = open_world(world_file=write_seed_file(tmp_path / "strangers.cyberworld", seed))
    result = _act(w, "夏", "同坐", **{"with": ["遥"]})
    assert result["ok"] is False
    assert result["detail"]["consents"][0]["source"] == "willingness"


def test_人数超过上限就拒_并且说清楚要几个(world):
    result = _act(world, "夏", "同坐", **{"with": ["遥", "柔", "夏"]})
    assert result["ok"] is False


def test_认不出的人当场说_不静默丢掉(world):
    """丢掉之后人数就对不上 `min`,而报出来的会是"人不够" —— 于是真正的原因
    (我不认识白霜)永远说不出口。"""
    result = _act(world, "夏", "同坐", **{"with": ["白霜"]})
    assert result["ok"] is False and "白霜" in result["error"]


# ── ① 别人凭什么答应 ────────────────────────────────────────────────────────


def test_不在同一个地方的人拉不进来_而且回执点名(world):
    world.act("遥", "walk", {"location": "yard"}, surface="body")
    world.tick(30)
    result = _act(world, "夏", "同坐", **{"with": ["遥"]})
    assert result["ok"] is False
    assert "沈遥" in result["error"], "说不出是谁,玩家就不知道下一步该找谁"
    assert _stamina(world, "夏") == 100, "被拒时一个字都不该写"


def test_他做不了的时候和他不想是两句话(world):
    """`incapable` 和「他不想」混成一句的话,玩家不知道该换个人还是该等他缓过来。"""
    world.scheduler.stock_store.set_many("agent:遥", {"体力": 1.0}, tick=0)
    result = _act(world, "夏", "同坐", **{"with": ["遥"]})
    assert result["ok"] is False and "做不了" in result["error"]


def test_拒绝那句话里_作者写的名字要划得出边界(world, open_world, tmp_path):
    """**中文不分词,而名字和动词是作者写的。**

    `沈遥` 后面紧跟着 `在赶路` 还看得懂,换成一个西文名字就成了
    `Dr. Eleanor Finch在赶路`(线上原文);而作者若把人叫做「老陈的猫」,
    `老陈的猫不在这儿` 会被读成"老陈的、猫不在这儿"。动词那一头同理:
    `一起坐会儿 是一个人做的事` 靠一个空格顶着,而空格在中文里不是分隔符。

    这不是排版讲究 —— 拒绝那句话是玩家唯一能读到的解释,读岔了他改错东西。
    引擎自己的字(`是一个人做的事`)不划边界,划的是**数据里来的那一截**。
    同一条纪律的第一个落点在 `_current_action` 那句忙碌拒绝(用「」不用反引号,
    反引号是 markdown,玩家屏幕上就是两个撇号)。
    """
    seed = json.loads(json.dumps(_SEED))
    seed["agents"][1]["name"] = "Dr. Eleanor Finch"
    w = open_world(world_file=write_seed_file(tmp_path / "latin.cyberworld", seed))
    w.act("遥", "walk", {"location": "yard"}, surface="body")
    w.tick(30)

    gated = _act(w, "夏", "同坐", **{"with": ["遥"]})
    assert gated["ok"] is False
    assert "「Dr. Eleanor Finch」" in gated["error"], "名字是数据,要划边界"

    solo = _act(w, "夏", "擦一擦", **{"with": ["柔"]})
    assert solo["ok"] is False
    assert "「一起坐会儿」" not in solo["error"]
    assert "「擦一擦」是一个人做的事" in solo["error"], "动词的人话也是作者写的"

    short = _act(w, "夏", "同坐")
    assert short["ok"] is False
    assert "「一起坐会儿」得有人一起" in short["error"]

    w.scheduler.stock_store.set_many("agent:柔", {"体力": 1.0}, tick=0)
    incapable = _act(w, "夏", "同坐", **{"with": ["柔"]})
    assert incapable["ok"] is False
    assert "「林柔」这会儿做不了" in incapable["error"]


def test_一个人过不了闸整件事就不发生(world):
    """三个人吃饭,一个人没钱,不该变成两个人吃饭 —— 那是引擎替他们改了计划。"""
    world.scheduler.stock_store.set_many("agent:柔", {"体力": 1.0}, tick=0)
    result = _act(world, "夏", "同坐", **{"with": ["遥", "柔"]})
    assert result["ok"] is False
    assert _stamina(world, "遥") == 100, "过得了闸的那个人也一个字都不该被扣"
    assert _seats(world) == 0


def test_没有判定器时按关系与声明的量判_而且降级要吭声(world):
    """默认状态(Mock LLM)走的是这一条。**白霜拒绝、「零」答应**的引擎侧对应物:
    同样的关系,「随和」低的那个不肯。"""
    world.scheduler.stock_store.set_many("agent:遥", {"随和": 0.05}, tick=0)
    world.config_set("social.joint.consent_stock", "随和")
    world.config_set("social.joint.min_willingness", 0.2)
    result = _act(world, "夏", "同坐", **{"with": ["遥"]})
    assert result["ok"] is False
    health = world.state()["runtime"].get("subsystems", {})
    assert "joint_consent" in json.dumps(health, ensure_ascii=False), health

    # 随和的人在同样的关系下答应。
    world.scheduler.stock_store.set_many("agent:遥", {"随和": 2.0}, tick=0)
    world.scheduler._memory_projection.relations.clear()
    world._record_and_fan({
        "type": "state_change", "who": "遥",
        "payload": {"kind": "sentiment", "as": "遥", "target": "夏", "sentiment": 0.8},
    })
    world.scheduler.catch_up_projection()
    assert _act(world, "夏", "同坐", **{"with": ["遥"]})["ok"] is True


def test_判定器一旦在场_答不答应由它说了算(world, monkeypatch):
    """有 key 的世界走这一条,而**它有否决权** —— 这正是红线 ①。"""
    from anima_world.relationship_judge import InviteVerdict

    class _Judge:
        def __init__(self, accept):
            self.accept, self.seen = accept, []

        def judge_invite(self, **kwargs):
            self.seen.append(kwargs)
            return InviteVerdict(accept=self.accept, reason="因为性格")

    # 关系好到爆表 —— 确定性那条路一定会答应,所以这次拒绝只可能来自判定器。
    world._record_and_fan({
        "type": "state_change", "who": "遥",
        "payload": {"kind": "sentiment", "as": "遥", "target": "夏", "sentiment": 1.0},
    })
    world.scheduler.catch_up_projection()

    judge = _Judge(accept=False)
    world.scheduler.relationship_judge = judge
    refused = _act(world, "夏", "同坐", **{"with": ["遥"]})
    assert refused["ok"] is False and "因为性格" in refused["error"]
    assert judge.seen[0]["a"]["personality"] == "话少", "判定读不到他是谁就判不出因人而异"
    assert "一起坐会儿" in judge.seen[0]["invitation"], "给它的必须是她读到的那几个字"

    world.scheduler.relationship_judge = _Judge(accept=True)
    assert _act(world, "夏", "同坐", **{"with": ["遥"]})["ok"] is True


def test_睡着的人不去问判定器(world):
    """拿一个睡着的人去问模型"你想不想去",它一定给得出一句像话的回答,而那句话是编的。"""
    from anima_world.actions import ActionDescriptor

    class _Judge:
        called = 0

        def judge_invite(self, **kwargs):
            type(self).called += 1
            raise AssertionError("硬闸没拦住,判定器被问了")

    world.scheduler._current_action["遥"] = ActionDescriptor("sleep", {})
    world.scheduler.relationship_judge = _Judge()
    result = _act(world, "夏", "同坐", **{"with": ["遥"]})
    assert result["ok"] is False and _Judge.called == 0
    assert "睡" in result["error"]


# ── ② 代价各扣一次,顺序不许有意义 ──────────────────────────────────────────


def test_代价对每个参与者各扣一次(world):
    assert _act(world, "夏", "同坐", **{"with": ["遥"]})["ok"] is True
    assert _stamina(world, "夏") == 95
    assert _stamina(world, "遥") == 95
    assert _seats(world) == 1, "目标身上的量只该变一次,不是一人一次"


def test_名单顺序不影响世界(world, open_world, tmp_path):
    """红线 ②。同一份世界跑两遍,只把名单调个个儿 —— 量、关系、事件都必须逐位相同。"""
    def run(order):
        seed = json.loads(json.dumps(_SEED))
        w = open_world(world_file=write_seed_file(
            tmp_path / f"w-{'-'.join(order)}.cyberworld", seed))
        assert _act(w, "夏", "同坐", **{"with": list(order)})["ok"] is True
        return (
            {a: _stamina(w, a) for a in ("夏", "遥", "柔")},
            _seats(w),
            sorted(
                (e["payload"]["as"], e["payload"]["target"], round(e["payload"]["delta"], 6))
                for e in w.events()
                if e["type"] == "state_change"
                and e["payload"].get("cause") == "joint_activity"
            ),
        )

    assert run(("遥", "柔")) == run(("柔", "遥"))


def test_有人做不了时一个人的量都不动(world):
    """`拒绝时一个字都不写` —— 边算边写的话,第一个人的体力已经扣掉了。"""
    world.scheduler.stock_store.set_many("agent:柔", {"体力": 2.0}, tick=0)
    before = {a: _stamina(world, a) for a in ("夏", "遥", "柔")}
    _act(world, "夏", "同坐", **{"with": ["遥", "柔"]})
    assert {a: _stamina(world, a) for a in ("夏", "遥", "柔")} == before


# ── ③ 关系变化是共同经历的效果 ──────────────────────────────────────────────


def test_一次共同经历长出每一对的关系变化_而且不调判定器(world):
    """红线 ③。判定器在场也不该被问 —— 关系是这段经历的**效果**,不是一次新判定。"""
    class _Judge:
        def judge_invite(self, **kwargs):
            from anima_world.relationship_judge import InviteVerdict
            return InviteVerdict(accept=True)

        def judge(self, *a, **k):
            raise AssertionError("共同经历不该再调一次关系判定")

        def judge_user(self, *a, **k):
            raise AssertionError("共同经历不该再调一次关系判定")

    world.scheduler.relationship_judge = _Judge()
    before = (_sentiment(world, "夏", "遥"), _sentiment(world, "遥", "夏"))
    assert _act(world, "夏", "同坐", **{"with": ["遥"]})["ok"] is True
    world.scheduler.catch_up_projection()
    assert _sentiment(world, "夏", "遥") > before[0]
    assert _sentiment(world, "遥", "夏") > before[1]

    causes = [
        e["payload"]["cause"] for e in world.events()
        if e["type"] == "state_change" and e["payload"].get("kind") == "sentiment_delta"
    ]
    assert causes and set(causes) == {"joint_activity"}, (
        "由头要写进事件 —— 查不出是哪一次经历的话,关系又变回一个说不出来路的数字"
    )


def test_每个人自己也记得这件事(world):
    """数字动了而她说不出为什么,是这一层最容易长成的假。"""
    assert _act(world, "夏", "同坐", **{"with": ["遥"]})["ok"] is True
    seeds = [
        e["payload"] for e in world.events()
        if e["type"] == "memory_seed"
        and e["payload"].get("kind") == "shared_experience"
    ]
    assert {s["agent_id"] for s in seeds} == {"夏", "遥"}
    assert "沈遥" in next(s["summary"] for s in seeds if s["agent_id"] == "夏")


def test_敬重真的动了(world):
    """这个引擎里 `respect` 从来没动过一次 —— 一起把一件事做完是它唯一的来路。"""
    assert _act(world, "夏", "同坐", **{"with": ["遥"]})["ok"] is True
    world.scheduler.catch_up_projection()
    rel = world.scheduler._memory_projection.relations[("夏", "遥")]
    assert rel.respect > 0


# ── 长过程:代价当场付,共同经历等到收尾 ────────────────────────────────────


def test_长过程每个人各占一件事_效果只落一次(world):
    started = _act(world, "夏", "长谈", **{"with": ["遥"]})
    assert started["ok"] is True and started["detail"]["started"] is True
    assert _stamina(world, "夏") == 90 and _stamina(world, "遥") == 90
    assert _seats(world) == 0, "效果要等到收尾"

    busy = _act(world, "遥", "擦一擦")
    assert busy["ok"] is False, "占着他的长过程期间,别的能力一律 busy"

    world.tick(6)
    assert _seats(world) == 10, "三个人各结算一遍的话这里会是 20 / 30"
    assert _sentiment(world, "夏", "遥") > 0


def test_东西没了时一件事只记一条收不了尾_否则体检会算出负数(world):
    """起头一件、收尾一条 —— 两头**必须是同一个单位**。

    `doctor` 的「起了几件」有意不数参与者(一顿三个人的饭是一件事),而抹掉目标
    那条循环从前给每个人各发一条 `entity_disengage`,于是两个人的一场长谈会被数成
    「起了 1 件、2 件收不了尾」:`1 - 0 - 0 - 2 = -1`,被 `max(0, …)` 抹成 0,
    屏幕上还是一行绿勾。所以这里按 `doctor` 的口径把账重算一遍。
    """
    assert _act(world, "夏", "长谈", **{"with": ["遥"]})["ok"] is True
    assert world.engagements("夏") and world.engagements("遥")
    # 她被占着,拆不动 —— 直接从调度器那一层抹掉(等价于别的进程干的)。
    world.scheduler._destroy_entity("夏", "bench:oak", "长谈", "cafe")
    assert not world.engagements("夏") and not world.engagements("遥"), "人不该挂在一个不存在的东西上"
    # 少发一条事件不等于少放一个人:参与者照样解占,不然他到原定的结束 tick 才动得了。
    assert world.scheduler._current_action.get("遥") is None

    started = len([
        e for e in world.events()
        if e["type"] == "entity_engage"
        and (e["payload"] or {}).get("joint_role") != "participant"
    ])
    gone = len([
        e for e in world.events()
        if e["type"] == "entity_disengage"
        and (e["payload"] or {}).get("reason") == "gone"
    ])
    assert (started, gone) == (1, 1), "一件事起一条、收一条,分子分母才是同一个单位"


def test_中途走开的人不算一起吃过饭(world):
    """起头时每个人都过了同处一地那道闸,而这段时间里谁都可能走开 —— 照名单发关系
    变化的话,一个开席就离场的人也算"一起吃过饭了",而世界里根本没有那顿饭。"""
    before = (_sentiment(world, "夏", "遥"), _sentiment(world, "遥", "夏"))
    assert _act(world, "夏", "长谈", **{"with": ["遥"]})["ok"] is True
    # 把他挪走(不经行为树:他正被占着,这里要的只是"他不在那儿了"这个事实)。
    world.scheduler.agents["遥"].agent.blackboard.write("loc", "yard")
    world.tick(6)
    world.scheduler.catch_up_projection()
    assert (_sentiment(world, "夏", "遥"), _sentiment(world, "遥", "夏")) == before
    assert not [
        e for e in world.events()
        if e["type"] == "state_change"
        and e["payload"].get("cause") == "joint_activity"
    ], "他开席就走了,世界里根本没有那顿饭"


def test_更长的共同经历更算数(world, open_world, tmp_path):
    """时长因子:一起待得越久越算数。"""
    def sentiment_after(duration):
        seed = json.loads(json.dumps(_SEED))
        seed["kinds"][1]["affordances"]["长谈"]["duration"] = duration
        w = open_world(world_file=write_seed_file(
            tmp_path / f"w{duration}.cyberworld", seed))
        assert _act(w, "夏", "长谈", **{"with": ["遥"]})["ok"] is True
        w.tick(duration + 2)
        w.scheduler.catch_up_projection()
        return _sentiment(w, "夏", "遥")

    assert sentiment_after(24) > sentiment_after(2)


# ── 玩家也能一起 ────────────────────────────────────────────────────────────


def test_玩家自己按下的那一下_不用去问他肯不肯(world):
    """他就是发起这次调用的那个人 —— 替他去问一个 LLM 是荒谬的。

    ⚠️ **这条路是 `player_tool`,不是 `act`。** 这个断言从前挂在
    `world.act("夏", …, player_id="p1")` 上,而那条路上"发起这次调用的那个人"
    是**她**:她在对话里点了他的名,引擎替他点了头。同一句话在一条路上是真的、
    在另一条路上是假的,而两条路当时共用一个分支 —— 3.6.0 把它们分开了
    (假的那一半见 `test_她点他的名时_他得自己答`)。
    """
    from anima_world.relationship_judge import InviteVerdict

    class _Judge:
        seen: list = []

        def judge_invite(self, **kwargs):
            type(self).seen.append(kwargs)
            return InviteVerdict(accept=True, reason="好啊")

    world.player_move("p1", "cafe")
    world.scheduler.relationship_judge = _Judge()
    result = world.player_tool(
        "p1", "interact", {"target": "bench:oak", "verb": "同坐", "with": ["夏"]})
    assert result["ok"] is True, result
    # 被问的只有**她**。他没有被问,因为问的人就是他。
    assert len(_Judge.seen) == 1 and _Judge.seen[0]["a"]["name"] == "苏晚夏"
    world.scheduler.catch_up_projection()
    assert _sentiment(world, "夏", "p1") > 0
    assert _sentiment(world, "p1", "夏") > 0


def test_玩家不在她跟前就一起不了_这一条不挂开关(world):
    """新加的能力可以从第一天起就严格 —— 需要开关的只有**已经在跑的**那些路径
    (`presence.enforce_colocation` 管的是 give / 导演那一批)。"""
    world.player_move("p1", "yard")
    result = world.act(
        "夏", "interact",
        {"target": "bench:oak", "verb": "同坐", "with": ["player:p1"]},
        surface="body", player_id="p1",
    )
    assert result["ok"] is False and "跟前" in result["error"]


def _open_talk(world, agent_id, player_id, turns):
    """给他俩支一场**还开着**的会话。会话关闭那一刻才落记忆,而邀请正发生在中间。"""
    store = world.chat_store
    ts = world.scheduler.clock
    cid = store.active_or_start(agent_id, ts, player_id=player_id,
                                player_name=world.players[player_id]["display_name"])
    for role, text in turns:
        store.add_message(cid, role, text, ts)
    return cid


def test_玩家叫人时_判定器读得到他俩这会儿正说的话(world):
    """线上撞出来的一整条:跟她聊完两轮再叫她,判定器手里**一个字都没有那两轮** ——
    记忆是会话关闭那一刻才落的,而邀请正发生在会话中间。她于是在"我压根不记得跟
    这个人说过话"的前提下判"熟不熟",一叫就推;玩家眼里是"我跟她聊得好好的,
    一叫她就说不熟"。**世界照跑,日志一行不错。**
    """
    from anima_world.relationship_judge import InviteVerdict

    class _Judge:
        def __init__(self):
            self.seen = []

        def judge_invite(self, **kwargs):
            self.seen.append(kwargs)
            return InviteVerdict(accept=True, reason="刚聊得挺好")

    world.player_move("p1", "cafe")
    world.players["p1"]["display_name"] = "阿布"
    _open_talk(world, "遥", "p1", [
        ("user", "你还记得那把旧伞吗"),
        ("assistant", "记得，你说过要还我。"),
    ])

    judge = _Judge()
    world.scheduler.relationship_judge = judge
    out = world.player_tool(
        "p1", "interact", {"target": "bench:oak", "verb": "同坐", "with": ["遥"]})
    assert out["ok"] is True, out

    talk = list(judge.seen[0]["recent_talk"])
    assert any("旧伞" in line for line in talk), f"当轮对话没进判定:{talk}"
    assert talk[0].startswith("阿布："), "他读到的必须是名字,不是 user/assistant"
    assert talk[1].startswith("沈遥：")


def test_没在说话时这一格是空的_不是编一段出来(world):
    from anima_world.relationship_judge import InviteVerdict

    class _Judge:
        seen: list = []

        def judge_invite(self, **kwargs):
            type(self).seen.append(kwargs)
            return InviteVerdict(accept=True, reason="")

    world.player_move("p1", "cafe")
    world.scheduler.relationship_judge = _Judge()
    assert world.player_tool(
        "p1", "interact", {"target": "bench:oak", "verb": "同坐", "with": ["遥"]},
    )["ok"] is True
    assert _Judge.seen[0]["recent_talk"] == ()


def test_一个人叫另一个人时没有转录可读_而且不该去找(world):
    """NPC 之间没有会话这回事 —— 去读一个不存在的转录只会让每次邀请多一次白跑的
    IO,而结果永远是空。"""
    from anima_world.relationship_judge import InviteVerdict

    class _Judge:
        seen: list = []

        def judge_invite(self, **kwargs):
            type(self).seen.append(kwargs)
            return InviteVerdict(accept=True, reason="")

    world.scheduler.relationship_judge = _Judge()
    assert _act(world, "夏", "同坐", **{"with": ["遥"]})["ok"] is True
    assert _Judge.seen[0]["recent_talk"] == ()


def test_替别人点头是不行的(world):
    """替别人点头等于把他的意志也取消掉,只是换成了玩家。"""
    world.player_move("p1", "cafe")
    world.player_move("p2", "cafe")
    result = world.act(
        "夏", "interact",
        {"target": "bench:oak", "verb": "同坐", "with": ["player:p2"]},
        surface="body", player_id="p1",
    )
    assert result["ok"] is False
