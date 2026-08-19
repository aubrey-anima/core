"""人设漂移探针(R2)的**契约形状**:`--json` 是契约,渲染是赠品。

这一层只测三件"文档承诺过、消费方会照着写代码"的事 —— 特征算得准不准是另一回事
(纯计数,同一段转录跑一百遍给同一个答案):

1. **每一格在三条出口上都在。** 一个键随分支来去的契约把每个消费方逼成一串
   `.get()`,再逼出各自的一份默认值 —— 那就是镜像端开始猜的地方。
2. **`baseline_n` 报的是真的取了几条**,不是要求的那个数(它最多占样本的一半)。
3. **样本不够时不下结论。** 在 5 条消息上宣布"人设很稳"是一盏假的绿灯。
"""
from __future__ import annotations

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
