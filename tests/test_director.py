"""实时编剧那一层的**纯函数**(3.11.0,批 3a)。

这个文件只测 `anima_world/director.py` —— 它不认识 Redis、不认识 `World`、
不掷骰子,所以「哪几个人能被派、这一轮最多写多大、回包怎么读、没 key 时说什么」
可以被单独测,而不是只能"开一个世界跑一遍看看"
(和 `contact` / `together` / `autonomy` 同一条纪律)。
"""
from __future__ import annotations

from anima_world import director as D


# ── 筛在前:藏起来的人根本不进提示词 ──────────────────────────────────────

def test_藏起来的人和禁区里的人_根本不进候选():
    """🔴 **不是"给了再叮嘱别说",是根本不给。**

    和主持人那一屏逐字同构:这一层交出去的是**散文**,名字是模型写进去的,
    宿主筛不了 —— **筛一半比不筛更坏**。
    """
    pool = [{"id": "夏"}, {"id": "遥"}, {"id": "反转人物"}]
    got = D.select_cast(pool, hidden=["遥"], forbidden=["反转人物"])
    assert [c["id"] for c in got] == ["夏"]


def test_写了人物池就只许用池子里的_没写就是全世界():
    pool = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert [c["id"] for c in D.select_cast(pool, cast_pool=["a", "c"])] == ["a", "c"]
    assert [c["id"] for c in D.select_cast(pool)] == ["a", "b", "c"]


def test_世界说不行的人不进候选_而不在这儿有意不算():
    """🔴 **`elsewhere` 有意不是闸**:「她不在这儿」正是 `approach` 要解决的事,
    把它当闸等于让编剧只能叫来已经站在他面前的人。"""
    pool = [{"id": "睡着的"}, {"id": "远处的"}]
    got = D.select_cast(pool, gated={"睡着的": "asleep"})
    assert [c["id"] for c in got] == ["远处的"]


def test_把藏起来的人写进人物池_要吭声():
    """**静默满足一个作者写下的要求,是这一层最贵的错** —— 他写了十三个人,
    其中两个永远不出场,而屏幕上什么都不少。"""
    said = D.cast_pool_warnings(["夏", "遥", "反转"], hidden=["遥"], forbidden=["反转"])
    assert len(said) == 2
    assert "遥" in said[0] and "hidden" in said[0]
    assert "反转" in said[1] and "禁区" in said[1]


def test_挑法是确定的_同一份候选挑两次逐项相同():
    pool = [{"id": "c"}, {"id": "a"}, {"id": "b"}]
    assert D.select_cast(pool) == D.select_cast(pool)


# ── 算术那一半:这一轮最多写多大 ──────────────────────────────────────────

def test_三种情形一律breathe_而且都不许沉默():
    """超上限 / 锚点刚响过 / 顶到安全阀 —— 三种都退成 `breathe`,
    **不是什么都不发生**(老板口径 1 的可验形式)。"""
    for kw in ({"capped": True}, {"anchor_fired": True}, {"tension": 0.9}):
        args = {"tension": 0.1, "phase": "escalation", "allowed": D.MOVES,
                "capped": False, "anchor_fired": False, "ceiling": 0.7}
        args.update(kw)
        assert D.pick_move(**args) == "breathe", kw


def test_离目标越远写得越大_这就是目标曲线那句话的落点():
    """张力是**目标曲线**不是上限(口径 4):挑的不是"够不够高",
    是"这条线此刻该往哪一相走"。"""
    big = D.pick_move(tension=0.0, phase="climax", allowed=D.MOVES,
                      capped=False, anchor_fired=False, ceiling=0.9)
    small = D.pick_move(tension=0.5, phase="escalation", allowed=D.MOVES,
                        capped=False, anchor_fired=False, ceiling=0.9)
    assert D.MOVE_TENSION[big] > D.MOVE_TENSION[small]


def test_没有指导时只许两个动作():
    got = D.pick_move(tension=0.0, phase="climax", allowed=D.NO_GUIDANCE_MOVES,
                      capped=False, anchor_fired=False, ceiling=D.NO_GUIDANCE_CEILING)
    assert got in D.NO_GUIDANCE_MOVES


def test_升级是默认行为_而breathe是唯一往回带的():
    assert D.next_phase("setup", "reveal") == "escalation"
    assert D.next_phase("escalation", "complicate") == "climax"
    assert D.next_phase("climax", "breathe") == "release"
    # 还没到 climax 的线,喘一口气不该把它打回去
    assert D.next_phase("setup", "breathe") == "setup"


def test_张力随世界时间衰减_而且不必被存():
    """`tension(now) = f(上一次那个值, 那时的 tick, 现在的 tick)` ——
    **算出来的,不是存下来的**:存一份会变旧的数就多一种和日志对不上的坏法。"""
    assert D.tension_now(0.8, 0, 0, 12) == 0.8
    a = D.tension_now(0.8, 0, 12, 12)      # 一个世界小时后
    b = D.tension_now(0.8, 0, 24 * 12, 12)  # 一天后
    assert 0.0 < b < a < 0.8


# ── 回包那三道闸 ──────────────────────────────────────────────────────────

def test_模型挑了一个更大的动作_不认():
    got = D.parse_decision('{"move":"complicate","who":"a"}',
                           allowed=["breathe", "approach"], cast_ids=["a"])
    assert got is None


def test_模型编了一个不在候选里的名字_不认():
    """**这一条是「筛一半比不筛更坏」的最后一道**:提示里没给的名字,
    模型仍可能凭空编 —— 编了就不认。"""
    got = D.parse_decision('{"move":"approach","who":"从没出现过的人"}',
                           allowed=D.MOVES, cast_ids=["a"])
    assert got is None


def test_写不出赌注的添乱_降成揭一角而不是整条作废():
    """`complicate` **必须**带真赌注(口径 4)。但一句写好了的台词不该因为少一格
    整条作废 —— 收回的是「添乱」这个名分,不是那句话。"""
    got = D.parse_decision('{"move":"complicate","who":"a","line":"他把门关上了"}',
                           allowed=D.MOVES, cast_ids=["a"])
    assert got["move"] == "reveal"
    assert got["line"] == "他把门关上了"


def test_带赌注的添乱_四种赌注都收得下而且别的不收():
    for kind in D.STAKE_KINDS:
        got = D.parse_decision(
            '{"move":"complicate","who":"a","line":"x","stake":'
            f'{{"kind":"{kind}","amount":5,"what":"面子"}}}}',
            allowed=D.MOVES, cast_ids=["a"])
        assert got["move"] == "complicate" and got["stake"]["kind"] == kind
    # 不认识的赌注种类 = 没写赌注 → 降级
    got = D.parse_decision(
        '{"move":"complicate","who":"a","line":"x",'
        '"stake":{"kind":"名声","amount":5}}', allowed=D.MOVES, cast_ids=["a"])
    assert got["move"] == "reveal"


def test_读不懂就退None_绝不猜():
    for bad in ("", "我觉得应该让恺撒出场", "{不是 json}", '{"move":"跑"}'):
        assert D.parse_decision(bad, allowed=D.MOVES, cast_ids=["a"]) is None


# ── 没 key 那条路 ─────────────────────────────────────────────────────────

def test_没配key也一定有一句话_而且指着他刚做的事():
    """🔴 **没配 key 是这个引擎的默认状态**,所以这不是降级路上的边角料 ——
    它就是「每操作一次都有回应」在默认状态下的兑现方式。"""
    got = D.mock_move(["你端详了门口那棵老橡树。"])
    assert got["move"] == "breathe" and got["source"] == "mock"
    assert got["line"], got
    # 🔴 **别把他刚做的那句再抄一遍**(端到端实跑逮的):这一句排在回顾**后面**,
    # 而回顾第一行就是那句话 —— 缝进来就是屏上连说两遍。
    assert "老橡树" not in got["line"], f"把回顾那句抄进来了:{got['line']}"
    # 他还没做过什么时也得有话说,而且不能是空串
    blank = D.mock_move([], place_name="咖啡店")
    assert blank["line"] and "咖啡店" in blank["line"]


# ── 提示词:没有第二个名字来源 ────────────────────────────────────────────

def test_提示里只有筛过的那几个名字():
    """🔴 和 `host.scene_messages` 逐字同构:这份提示**只由已经筛过的候选拼出来**,
    所以模型手上根本没有藏起来的人的名字 —— 漏出去需要它凭空编一个没见过的名字。"""
    cast = D.select_cast([{"id": "夏", "name": "苏晚夏"}, {"id": "遥", "name": "陈遥"}],
                         hidden=["遥"])
    messages = D.decide_messages(
        recap=["你端详了老橡树。"], cast=cast, allowed=D.MOVES, thread=None,
        guidance={"themes": ["血统"], "forbidden": {"text": ["不许剧透"]}},
        place_name="咖啡店", day=3, tension=0.2, phase="setup")
    blob = "".join(m["content"] for m in messages)
    assert "苏晚夏" in blob
    assert "陈遥" not in blob and "遥" not in blob, blob
    assert "不许剧透" in blob


# ── 闭集印在人屏上时的人话(3.11.1,验收 C ⑤)────────────────────────────────

def test_每个闭集取值都有一句人话_而且不许是裸英文():
    """🔴 **`〔breathe〕`「setup」印在玩家屏上** —— 那是给机器读的名字。

    和主持人那张 `MOMENT_LABELS` 逐字同一条:**闭集加一项就要在这儿加一句人话**,
    而这道闸让"忘了加"有人喊。
    """
    for table, names, what in ((D.MOVE_LABELS, D.MOVES, "动作"),
                               (D.PHASE_LABELS, D.PHASES, "相"),
                               (D.STAKE_LABELS, D.STAKE_KINDS, "赌注")):
        missing = [n for n in names if not str(table.get(n) or "").strip()]
        assert not missing, f"这几个{what}没有人话:{missing} —— 屏上会印裸英文"
        for name, said in table.items():
            assert not said.isascii(), f"{what} {name!r} 的人话是 {said!r} —— 那是裸英文"


def test_target_curve那三格_有意不含release():
    """`release` 是一条线**走完之后**的样子,没有时长 —— 拿 `phases` 四格去判
    会**多放一格**,而作者写下去不报错、也不生效。契约里单报一格
    `target_curve_keys`,别让下游去猜(tool 带回的那一条)。"""
    assert list(D.TARGET_CURVE_PHASES) == list(D.PHASES[:-1])
    assert "release" not in D.TARGET_CURVE_PHASES
