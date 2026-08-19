"""记忆的准入闸(R4)。**纯函数,不碰存储。**

## 它补的洞

触发器(`memory_triggers`)回答的是"这条事件配不配成为记忆",判据是事件的**类型**:
一场对话、一次关系跃迁、一件礼物。它答不了的是"**这一条**配不配":同一个人今天
第七次跟她说「在吗」,和他第一次说的那句,走的是同一条分支、拿到同一个重要度。

于是记忆位(`memory.capacity`,每人 50 行)被套话吃掉,而淘汰是按强度淘汰的 ——
一条刚写进来的套话强度是满的 1.0,它挤掉的是一条被想起过好几次的旧记忆。
**结果是她记得一堆「在吗」,忘了他说过他要走。** 这个坏法安静得可怕:日志一条不错、
每条记忆单看都合法、容量也没超。

**量出来的**(内置橱窗世界,跑 40 个世界日):不开闸时她的 50 条记忆**归并之后只有
11 件不同的事** —— 同一场对话 9 遍、同一句「我睡下了」8 遍、同一场雨 8 遍。开闸之后
留下 11 条,**一件不少,少的全是重复行**。所以这道闸删掉的不是信息,是占着容量的副本:
那 39 个位置本来该留给别的记忆。

A-MAC(arxiv 2603.04549)把这件事叫准入控制,五个因子。这里照抄那五个因子,
但**每一个都要能用世界里已有的东西算出来**,不引入向量库、不调模型:

| 因子 | 这里怎么算 | 为什么是它 |
|---|---|---|
| 效用 utility | 触发器给的 `importance` | 已有的判断,不重造 |
| 置信 confidence | 摘要长度是否够撑起一件事 | 空摘要和三个字的摘要进了库也没法被想起 |
| 新颖 novelty | 和她已有记忆的**字面**重合度 | 她真正的重复是复读式的,不需要语义向量就抓得住 |
| 近因 recency | 同一 kind 在**最近这一小段**里写过几条 | 一天里第七次「路过」不值得再记一条 |
| 类型先验 type prior | 每个 kind 一个系数 | 反思和创世记忆天然更该留下 |

## ⚠️ 它管得到哪一半(量出来的,别想当然)

**闸只装在引擎自己推断的那条路上**(`TriggerEngine`),`memory_seed` 不过闸 ——
那是世界的**显式声明**(创世记忆、关系判定、八卦反应、反思),而世界明说要记、引擎
悄悄不记,是比记一堆重复更坏的坏法(`test_verb_writes` 当场逮到过:`talk_to` 声明会写
记忆,两条 seed 全被拒)。

代价必须说清楚:**内置橱窗世界里的重复大多走的正是声明那条路**,于是开闸之后 ——

    不开闸  50 条记忆 / 10 件不同的事   {witness 18, state_change 14, reflection 8, …}
    开  闸  50 条记忆 /  7 件不同的事   {witness 27, state_change  1, reflection 10, …}

**开闸让它更差了**:闸把唯一管得到的 `state_change` 清干净,而腾出来的容量被管不到的
重复填上了。所以内置橱窗**没有点亮它** —— 这条违反"新特性要在橱窗里展示"那道闸,
是有意的:拿一个实测更差的配置去当新用户看到的第一屏,比不展示更坏。

它对什么样的世界有用:**状态类事件刷屏**的世界(她一天里 `agent_state` 跳十几次,
而那些「我睡下了」正在挤掉别的记忆)。作者自己量一遍再决定。

## 三条硬纪律

- **默认关**(`memory.admission.enabled`)。它会让世界少记东西 —— 这是行为变更,
  而引擎默认值是"没人说话时的样子"。**橱窗也没点它**,理由见上面那一段。
- **锚定的永不拒**。创世记忆是她是谁的一部分(`anchor=True`),一道打分闸把它拦下来
  就是把她的出身拦下来。
- **拒了要说得出为什么**(`AdmissionVerdict.reason`)。一条静默丢掉的记忆是查不了的:
  作者只会看到"她怎么不记得这件事",而日志里什么都没有。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

#: 低于这个总分就不收。
#:
#: **阈值必须坐在"一条普通的、新鲜的低价值事件"之下** —— 这条是量出来的,不是拍的:
#: 一条 `state_change`(importance 0.5,类型先验 0.7)满分是 0.35。第一版的阈值就取
#: 0.35,于是它**正好压在那条线上**,任何一点扣分都让整个类型被拒 —— 实测跑 40 个
#: 世界日,她的记忆从 50 条掉到 11 条,而这条闸的本意只是挡掉第七次「在吗」。
#:
#: 0.20 是这么定的:新鲜的 `state_change` 拿 0.35 进得来;而同一件事复读一遍
#: (新颖度掉到 0.33)只有 0.11,拒。**闸开在复读上,不开在日常上。**
DEFAULT_THRESHOLD = 0.20

#: 摘要短于这个字数,认为它撑不起一件事(置信因子压到底)。
_THIN_SUMMARY = 6

#: 近因窗口:同一个 kind 往前数这么多条自己的记忆里出现过几次。
_RECENCY_WINDOW = 12

#: 算重合度前先扔掉的噪声字符(标点、括号)——它们在每句话里都有,只会稀释判断。
_NOISE = frozenset("，。！？、；：「」『』（）()《》〈〉“”‘’…—-·,.!?;:'\"")

#: 近因惩罚的斜率。窗口里有 6 条同类时降到 0.77,而不是 0.4 —— 见 `judge` 里的说明。
_RECENCY_SLOPE = 0.05

#: 类型先验。**没列的一律 1.0** —— 一张要求穷举的表会在作者自造 kind 时
#: 悄悄把它压成 0,而作者不会知道。
TYPE_PRIOR: dict[str, float] = {
    "reflection": 1.3,      # 想明白的事比看见的事更该留下
    "seed": 1.3,            # 创世记忆(通常也是 anchor)
    "memory_seed": 1.3,
    "relation_shift": 1.2,  # 关系跃迁是她后面一切判断的地基
    "user_conversation": 1.0,
    "gift": 1.0,
    "state_change": 0.7,    # 「谁开始干活了」——量大、单条价值低
    "agent_state": 0.7,
}


@dataclass(frozen=True)
class AdmissionVerdict:
    """收不收,以及为什么。**拒绝要能被读懂** —— 见模块开头第三条。"""

    admit: bool
    score: float
    reason: str
    factors: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {"admit": self.admit, "score": round(self.score, 4),
                "reason": self.reason,
                "factors": {k: round(v, 4) for k, v in self.factors.items()}}


def _bigrams(text: str) -> set[str]:
    """相邻两字组成的集合。空白与常见标点先去掉 —— 它们只会稀释重合度。"""
    clean = "".join(ch for ch in (text or "") if not ch.isspace() and ch not in _NOISE)
    return {clean[i:i + 2] for i in range(len(clean) - 1)} or ({clean} if clean else set())


def _overlap(a: str, b: str) -> float:
    """两段话的字面重合度(Jaccard,**按相邻二字**)。

    **为什么是二字不是单字** —— 这是拿真世界演一遍才看见的:第一版按单字算,而中文
    里两句毫不相干的话必然共享「的了在她一不」这些字,于是不相干的两条也能撞出 0.4~0.5
    的重合度,新颖度被**系统性**压低。后果不是"挡住了复读",是**几乎全拒**:实测一个
    跑了 40 个世界日的世界,她的记忆从 50 条掉到 9 条 —— 而这条闸的本意只是挡掉
    第七次「在吗」。二字组把这个洞堵上了:不相干的句子几乎不共享二字组,而复读的句子
    共享几乎全部。

    引一个分词器进来是过度:这一层要的只是"这句话我是不是刚说过"的粗判,
    而二字组在中文上已经足够,还不带依赖。
    """
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def judge(
    summary: str,
    *,
    kind: str,
    importance: float,
    anchor: bool = False,
    existing: Sequence[dict[str, Any]] = (),
    threshold: float = DEFAULT_THRESHOLD,
) -> AdmissionVerdict:
    """这一条该不该进她的记忆。`existing` 是她**已有的记忆行**(新的在前)。

    五个因子相乘再乘类型先验 —— **乘不是加**:加法允许一个因子的高分把另一个
    因子的零分盖过去,而这五个里任何一个到底,这条记忆就是不该进的
    (一条和上一条一模一样的复读,importance 再高也只是复读)。
    """
    text = str(summary or "").strip()
    if anchor:
        return AdmissionVerdict(True, 1.0, "锚定记忆一律收(她是谁的一部分)",
                                {"anchor": 1.0})
    if not text:
        return AdmissionVerdict(False, 0.0, "摘要是空的 —— 记下来也想不起任何事",
                                {"confidence": 0.0})

    utility = max(0.0, min(1.0, float(importance)))
    confidence = 1.0 if len(text) >= _THIN_SUMMARY else len(text) / _THIN_SUMMARY

    rows = list(existing)
    # 新颖:和**最像的那一条**比,不和平均比 —— 平均会被一堆不相干的记忆稀释掉,
    # 而复读的定义就是"和其中某一条几乎一样"。
    worst = max((_overlap(text, str(r.get("summary") or "")) for r in rows[:40]),
                default=0.0)
    novelty = max(0.0, 1.0 - worst)

    same_kind = sum(1 for r in rows[:_RECENCY_WINDOW] if r.get("kind") == kind)
    # 同类在最近窗口里越多,这一条越不值钱。**斜率放得比直觉缓**:五个因子是
    # 相乘的,而一个正常活跃的世界里同 kind 本来就多(她一天里会目击好几件事)——
    # 陡的曲线配上相乘,拒的就不是复读而是日常。
    recency = 1.0 / (1.0 + _RECENCY_SLOPE * same_kind)

    prior = TYPE_PRIOR.get(kind, 1.0)
    score = utility * confidence * novelty * recency * prior
    factors = {"utility": utility, "confidence": confidence,
               "novelty": novelty, "recency": recency, "type_prior": prior}

    if score >= threshold:
        return AdmissionVerdict(True, score, "", factors)
    # 说出**最低的那个因子**:作者要的不是分数,是"为什么她没记住这件事"。
    culprit = min(("utility", "confidence", "novelty", "recency"),
                  key=lambda k: factors[k])
    why = {
        "utility": "这件事本身不重要",
        "confidence": "摘要太短,撑不起一件事",
        "novelty": "和她已经记得的一条几乎一样(复读)",
        "recency": f"最近已经记过 {same_kind} 条同类的了",
    }[culprit]
    return AdmissionVerdict(False, score, why, factors)
