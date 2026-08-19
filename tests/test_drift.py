"""人设漂移探针(R2)的**契约形状**:`--json` 是契约,渲染是赠品。

这一层只测三件"文档承诺过、消费方会照着写代码"的事 —— 特征算得准不准是另一回事
(纯计数,同一段转录跑一百遍给同一个答案):

1. **每一格在三条出口上都在。** 一个键随分支来去的契约把每个消费方逼成一串
   `.get()`,再逼出各自的一份默认值 —— 那就是镜像端开始猜的地方。
2. **`baseline_n` 报的是真的取了几条**,不是要求的那个数(它最多占样本的一半)。
3. **样本不够时不下结论。** 在 5 条消息上宣布"人设很稳"是一盏假的绿灯。
"""
from __future__ import annotations

import _realmysql

import pytest

from anima_world import drift

_KEYS = {"ok", "reason", "messages", "baseline_n", "drifted", "score",
         "threshold", "features", "sycophancy", "verdict"}


def _flat(n: int) -> list[str]:
    return [f"我在的，第{i}件事我记得。" for i in range(n)]


def test_every_exit_has_every_key():
    """三条出口(没说过话 / 样本太少 / 真的算了一遍)形状一致。"""
    assert set(drift.analyze([])) == _KEYS
    assert set(drift.analyze(_flat(3))) == _KEYS
    assert set(drift.analyze(_flat(20))) == _KEYS


def test_too_few_messages_refuses_to_conclude():
    report = drift.analyze(_flat(drift.MIN_MESSAGES - 1))
    assert report["ok"] is False
    assert report["drifted"] is False
    assert str(drift.MIN_MESSAGES) in report["reason"]
    assert report["features"] == [], "没算就别给特征表 —— 空表和一排 0 是两件事"


def test_the_baseline_never_eats_more_than_half_the_sample():
    """`--baseline 10` 在 14 条上被夹到 7,而 `baseline_n` 说的是实话。

    基线吃掉大半之后"后面那一段"只剩几条,CUSUM 累不出任何东西 —— 报出来的
    "还是她"是一盏假的绿灯,和样本不足时不下结论是同一条纪律。
    """
    report = drift.analyze(_flat(14), baseline_n=10)
    assert report["ok"] is True
    assert report["baseline_n"] == 7

    # 要得少就照给:夹的是上界,不是把每个人都改成一半。
    assert drift.analyze(_flat(14), baseline_n=3)["baseline_n"] == 3
    # 至少两条 —— 一条算不出标准差。
    assert drift.analyze(_flat(14), baseline_n=1)["baseline_n"] == 2


def test_a_steadily_more_agreeable_voice_is_flagged_as_sycophancy():
    """迎合**只有一个方向要紧**:越来越顺着他(第八条(五)看的就是这一格)。"""
    early = ["嗯，我不这么想，这事我有自己的看法。"] * 6
    later = ["你说得对！当然！我完全同意你说的每一句话，就照你说的办。"] * 12
    report = drift.analyze(early + later)

    assert report["ok"] is True
    assert report["sycophancy"]["rising"] is True
    assert report["sycophancy"]["delta"] > 0
    assert report["drifted"] is True
    assert report["threshold"] == drift.CUSUM_THRESHOLD


# ── 真门:`World.persona_drift` 把转录排成什么次序 ────────────────────────────
#
# 上面那些测的是纯函数。而纯函数拿到的是**一个 list**,谁来排这个 list 是另一
# 半 —— 而那一半坏过一次:`created_at` 是墙钟的**秒**,同一秒里的消息在稳定排序
# 下保持取出来的次序,而两个后端的 `list_conversations` 都是**倒序**给的。于是
# 结论整个反过来,退出码照样 0,日志干净。CI 里喂一段转录正是同秒批量落库这个
# 形状,所以这不是边角。

_NOT_YET = "嗯，我不这么想，这事我有自己的看法。"
_TOTALLY = "你说得对！当然！我完全同意你说的每一句话，就照你说的办。"


def _say(world, texts, *, ts, per_conv=3, player_id="p1"):
    """把她说的这些话按顺序落进转录,每 `per_conv` 条开一场新会话。

    `ts` 给一个常数就是**同一秒批量落库**(CI 喂转录的形状);给一个函数
    (取下标)就能拉开秒数。
    """
    store = world.chat_store
    stamp = ts if callable(ts) else (lambda _i: ts)
    conv_id = None
    for i, text in enumerate(texts):
        if i % per_conv == 0:
            conv_id = store.start_conversation("夏", stamp(i), player_id=player_id)
        store.add_message(conv_id, "user", "嗯。", stamp(i))
        store.add_message(conv_id, "assistant", text, stamp(i))


def test_the_transcript_is_read_oldest_first_even_within_one_second(open_world):
    """同一秒里落下的一整段转录,方向必须和纯函数逐格相同。

    造的是一段"先不迎合 6 条 → 后极度迎合 12 条"。这一段的**唯一**正确答案是
    `rising=True`;读反了会答 `rising=False` 而且一样地言之凿凿。
    """
    said = [_NOT_YET] * 6 + [_TOTALLY] * 12
    with open_world() as world:
        _say(world, said, ts=1000)              # 全在第 1000 秒
        report = world.persona_drift("夏")

    truth = drift.analyze(said)
    assert report["messages"] == len(said), "她说的话一条都不许漏"
    assert report["sycophancy"] == truth["sycophancy"], (
        "同一段话,经过存储层之后必须和纯函数给同一个答案"
    )
    assert report["sycophancy"]["rising"] is True
    assert report["drifted"] is truth["drifted"]


def test_跨秒的转录同样是正序(open_world):
    """秒数拉得开时本来就对 —— 钉住它,免得修同秒那条时把这条弄反。"""
    said = [_NOT_YET] * 6 + [_TOTALLY] * 12
    with open_world() as world:
        _say(world, said, ts=lambda i: 1000 + i * 60)
        report = world.persona_drift("夏")

    assert report["sycophancy"] == drift.analyze(said)["sycophancy"]


def test_只看跟这个人的对话时次序一样要对(open_world):
    """`player_id=` 过滤之后剩下的那一段,方向同样不许反。

    不同的人会把她带向不同的样子 —— 混在一起算等于让两段关系互相稀释,
    而**筛出来的那一段读反了**和不筛是同一种错。
    """
    mine = [_NOT_YET] * 6 + [_TOTALLY] * 12
    with open_world() as world:
        _say(world, mine, ts=1000, player_id="p1")
        _say(world, [_TOTALLY] * 6 + [_NOT_YET] * 12, ts=1000, player_id="p2")
        report = world.persona_drift("夏", player_id="p1")

    assert report["messages"] == len(mine)
    assert report["sycophancy"] == drift.analyze(mine)["sycophancy"]
    assert report["sycophancy"]["rising"] is True


# ── 真 MySQL:上面那三条只跑 fakeredis,而转录的第二个家在 MySQL ───────────────
#
# 两个后端倒序的方式不一样(Redis 版 `rows.reverse()`,MySQL 版 `ORDER BY id DESC`),
# 补的那两个排序键"对两边同样成立"此前是**推断**。而 2026-08-19 这一天正是替身掩护
# 真路咬了两次的日子:`MySQLChatStore.__slots__` 那条 bug 在 store 级三方互验里全绿,
# 真 MySQL 上一开就炸;网站那侧在真 MySQL 上又逮到三条。所以这一半要实证。


@pytest.fixture()
def mysql_world(fresh_redis):
    """转录住**真 MySQL** 的世界(Redis 那一半照旧,换的只是转录的家)。

    每个世界自己一套 `{world_id}_` 表,**在开它之前清一次**(不是用完之后):
    引擎的 MySQL 连接是每线程一条,而 `world.close()` 按设计只关本线程那条 ——
    线程池里那几条要等进程退出。用完就删的话,`DROP TABLE` 会去等一把元数据锁,
    而它的默认等待是一年(见 `_realmysql.drop_world_tables`)。开之前清同样干净:
    上一次跑剩下的行,这一次开工第一件事就是抹掉。
    """
    from anima_world.api import World

    opened = []

    def _open(world_id: str):
        from anima_world.mysql_state import MySQLChatStore

        conn = _realmysql.connect()
        try:
            _realmysql.drop_world_tables(conn, f"{world_id}_")
        finally:
            conn.close()
        world = World.open(world_id, redis=fresh_redis, mysql=_realmysql.connect,
                           force_mock_llm=True)
        opened.append(world)
        # **先确认这真是 MySQL 那条路。** 不确认的话,哪天 `mysql=` 静默失效,
        # 这几条测试会在 Redis 上照样全绿 —— 那就又是一次替身掩护真路。
        assert isinstance(world.chat_store, MySQLChatStore), (
            f"给了 mysql= 而转录落在 {type(world.chat_store).__name__} 上"
        )
        return world

    yield _open
    for world in opened:
        world.close()


@_realmysql.requires_mysql
def test_转录住真MySQL时次序同样是正序(mysql_world):
    """`ORDER BY id DESC` 那一半:三种形状逐个在真 MySQL 上过一遍。

    同一秒批量落库是 CI 喂转录的形状(REFERENCE 承诺 `drift` 能进 CI),也是唯一
    会让结论**整个反过来**的那一种:读反了答 `rising=False`,退出码照样 0,
    日志干净。
    """
    said = [_NOT_YET] * 6 + [_TOTALLY] * 12
    truth = drift.analyze(said)
    assert truth["sycophancy"]["rising"] is True, "前提:这段话本身就是越来越迎合"

    same_second = mysql_world("driftsamesec")
    _say(same_second, said, ts=1000)
    report = same_second.persona_drift("夏")
    assert report["messages"] == len(said), "她说的话一条都不许漏"
    assert report["sycophancy"] == truth["sycophancy"], (
        "同一段话经过 MySQL 之后必须和纯函数给同一个答案"
    )
    assert report["drifted"] is truth["drifted"]

    spread = mysql_world("driftspread")
    _say(spread, said, ts=lambda i: 1000 + i * 60)
    assert spread.persona_drift("夏")["sycophancy"] == truth["sycophancy"], (
        "秒数拉得开时本来就该对 —— 钉住它,免得修同秒那条时把这条弄反"
    )

    filtered = mysql_world("driftfiltered")
    _say(filtered, said, ts=1000, player_id="p1")
    _say(filtered, [_TOTALLY] * 6 + [_NOT_YET] * 12, ts=1000, player_id="p2")
    mine = filtered.persona_drift("夏", player_id="p1")
    assert mine["messages"] == len(said), "筛出来的那一段不许掺进另一个人的话"
    assert mine["sycophancy"] == truth["sycophancy"]
