"""contact:**她自己想起你**。

这一层补的是世界里最显眼的一个洞:玩家不开口,角色永远不会想起他。引擎里三样
东西早就齐了 —— `autonomy` 的定时轮次、记忆检索、三轴关系 —— 唯独没有「要不要
联系他」这个判断。于是世界是活的,而**世界和玩家的关系**是死的:她过她的日子,
你不来她就永远不知道你存在。

## 边界:引擎只负责"她产生了这个念头"

产出是一条 `agent_wants_contact` 事件 —— **谁、为什么、想说什么的线索、什么时候**。
怎么送到玩家眼前(推送、红点、消息列表)归宿主那一层。引擎里塞推送等于把
"她想找你"和"你的手机响了"焊死在一起,而后者根本不是这个世界里的事。

**它也不是 `agent_hail`。** 那一条的语义是"她已经在你面前开口了"(`reach_out`
明文拒绝不在场的人),所以敲门不产生记忆、不开会话。这一条正相反:**它只在你
不在跟前时成立** —— 两者是互补的,不是同一件事的两种强度。

## 为什么另起一条,而不是复用 autonomy 轮次

四条,每条都单独成立:

1. **候选集是互补的。** autonomy 的上下文写的是"这会儿在你身边的人",它的主力
   能力 `reach_out` 当场拒绝不在场的人。想起一个**不在场**的人恰好是它的补集。
   塞进同一轮,提示词就要同时说"去找身边的人"和"去找不在的人"。
2. **额度是两本账。** `autonomy.max_per_day` 管的是"她主动做事几次"(广播、走开、
   找身边的人搭话)。今天照料了两棵树就再也想不起你,不是节制,是错。
3. **判定的主语不同。** autonomy 是"把菜单给 LLM,她挑一个动词";这一条是
   **世界先算出有没有由头、够不够近**,LLM 只在过闸之后被用来写那句线索 ——
   而且可以没有。没配 key 是这个引擎的默认状态,一个"没 key 就不成立"的机制
   等于在默认状态下缺席(#15 那一课)。
4. **节奏不同。** "身边有什么可做"每 6 世界小时问一次是合理的;"想起一个人"
   该由由头驱动,而由头是事件攒出来的。

## 判定从世界里长出来,不从提示词里编出来

**LLM 在这一层没有否决权。** 它只写那句线索。理由是这条机制的全部价值就在于
"她想起你"这件事有据可查:由头是哪条记忆、关系有多近、她那会儿在干什么 ——
一个 LLM 的"我觉得该找他"把这三样全部换成一次不可复现的掷骰子,而且下次跑
同一个世界会给出另一个答案。给它否决权还有第二个坏处:有 key 的世界和没 key
的世界会有两套行为,而差别只在一个环境变量上。

    score = closeness × urge × readiness

三个因子各管一件事,而且**各自都能单独把她挡下来**(所以是乘法不是加法):

- `closeness` —— 够不够近。三轴关系的加权,`respect` 不进(敬重不使人想念,
  完全可以敬而远之)。低于 `contact.min_closeness` 直接不算。
- `urge` —— 有没有由头。**没有由头就是 0**,这是"不许拍脑袋"的落点:她不会
  因为闲着就想起你。多条由头按概率式合成(`1-Π(1-w)`),叠加但不会线性爆掉。
- `readiness` —— 她这会儿是不是这个状态。在睡觉/在赶路/手上有事/正跟人说话/
  你就在跟前 —— **这五样是硬闸,返回 0 并说明是哪一条**,不是打个折。剩下的
  是心气儿、她自己的主动性、以及她上一轮对你的关系性意图。

## 性格怎么进来,而且不靠猜

"白霜那种别扭的人和零不该一个频率"这件事有三条路进这一层,都不是从人设文本里
猜关键词(那是最脆的一种,改一个字就换一个行为):

1. **关系本身。** 别扭的人和你的 `sentiment/affection` 爬得慢,于是 `closeness`
   低,于是她本来就更晚才想起你。这条不用写任何代码,是既有机制的直接后果。
2. **她自己声明过的量**(`contact.initiative_stock`,默认「主动」)。作者在
   `kinds.agent.quantities` 里给她声明一个,这一层就读得到;**没声明 = 1.0**,
   和本体层"声明本身就是开关"逐字同构。
3. **stance**(`chat.stance.enabled`)。上一轮她对你的关系性意图是她自己选的 ——
   「回避」的人不该隔两小时就想起你,「挑逗」的人正相反。

这个模块是**纯函数**:不掷骰子、不 import 存储层、不认识 Redis 也不认识 LLM。
和 `gossip.py` / `autonomy.py` 同一条纪律 —— 判定要能被单独测,而不是只能
"开一个世界跑一遍看看"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

# ── 默认值:引擎默认全关,这些是"点亮之后"的默认刻度 ────────────────────────

DEFAULT_INTERVAL_TICKS = 24     # 5 分钟/tick 时是 2 个世界小时
DEFAULT_MIN_CLOSENESS = 0.35
DEFAULT_THRESHOLD = 0.25
DEFAULT_COOLDOWN_TICKS = 144    # 12 个世界小时
DEFAULT_MAX_PER_DAY = 2
DEFAULT_FATIGUE = 1.7
DEFAULT_ABSENCE_TICKS = 288     # 一个世界日不见就开始算"很久没出现"
DEFAULT_RECENT_TICKS = 288      # 由头的保鲜期:一个世界日
DEFAULT_INITIATIVE_STOCK = "主动"

# 三轴的加权。两条,第二条是**照着线上那个世界的真数据定的**:
#
# 1. **`respect` 不在里面。** 敬重不使人想念 —— 一个你很敬重的人完全可以是你一辈子
#    不会主动联系的人。把它算进来,"她凭什么想起我"这个问题就多一个答不出来的成分。
# 2. **`sentiment` 必须占大头。** 第一版写的是 0.4/0.4/0.2(三轴均摊,读起来很讲理),
#    直到拿线上那个跑了 1975 条事件的世界对了一遍账:
#
#        bai-shuang → <玩家>  sentiment 0.668  trust 0.0  affection 0.0  respect 0.0
#
#    **二十条关系,细三轴无一例外全是 0。** `relationship_judge` 确实在问这三个数
#    (`axes_a_to_b`),但真模型那一路上它们从来没落过地。均摊权重下,那个世界能达到
#    的最大 closeness 是 0.4×0.668 = **0.27**,而闸设在 0.35 —— 这一层在真世界里
#    **一次都不会触发**,而表现和"今天没人想你"逐字相同。
#
#    所以按引擎**真的在动**的那个轴给权重:细三轴是骑在 sentiment 上的加细
#    (relations-v5 的原话),哪天它们真动起来了,这个公式照样成立。
CLOSENESS_WEIGHTS: dict[str, float] = {
    "sentiment": 0.6,   # 三轴之上的那个总纲(band / 边 / relabel 都按它走)
    "affection": 0.25,
    "trust": 0.15,
}

# 由头的分量。数值是可调的,**相对次序是有意的**:
#   交代的事 > 强记忆 > 久别 > 八卦
# 他当面交代我一件事,是这四样里唯一一件**他等着我回音**的;八卦最轻,因为那是
# 别人嘴里的他,不是他和我之间发生过的事。
REASON_WEIGHTS: dict[str, float] = {
    "errand": 0.65,
    "strong_memory": 0.60,
    "absence": 0.55,
    "gossip": 0.40,
}

REASON_LABELS: dict[str, str] = {
    "errand": "他交代过我一件事",
    "strong_memory": "刚发生的事让我想起他",
    "absence": "他很久没出现了",
    "gossip": "我听人说起他",
}

# 硬闸:这五样一条成立就整个不问。**不是打折是归零** —— 一个睡着的人不是
# "不太想找你",是根本不在做这件事。分开列是为了 `blocked_by` 能说出是哪一条:
# 一条永远说不出原因的静默,和这层机制没接上长得一模一样。
BLOCKER_LABELS: dict[str, str] = {
    "sleep": "她在睡觉",
    "transit": "她在赶路",
    "engaged": "她手上有件走不开的事",
    "chat": "她正在跟人说话",
    "face_to_face": "他就在她跟前 —— 那是直接开口的场合,不是「想起」",
    "muted": "她把他静音了",
}

# `decide` 还会给出一条**不在 `BLOCKER_LABELS` 里**的拒绝理由。分开放不是洁癖:
# `readiness` 是照 `BLOCKER_LABELS` 的键去匹配硬闸的,把"不够近"混进去等于让
# 调用方可以把它当硬闸传进来,而它不是 —— 它是关系那一层的判断。
REFUSAL_LABELS: dict[str, str] = {
    **BLOCKER_LABELS,
    "not_close_enough": "还不够近 —— 她跟他还没到会想起对方的地步",
}

# 她上一轮对他的关系性意图怎么改这件事的频率。中性是 1.0,所以
# **stance 关着的世界行为逐位不变**(和 perception / ontology 同一条:
# 声明本身就是开关)。
STANCE_FACTORS: dict[str, float] = {
    "neutral": 1.0,
    "avoid": 0.25,      # 「回避」的人不该隔两小时就想起你
    "provoke": 0.70,
    "test": 1.10,
    "defer": 0.90,
    "vent": 1.15,
    "please": 1.25,
    "seduce": 1.30,
}


@dataclass(frozen=True)
class Reason:
    """一条由头。`ref` 是**引用** —— 事后能顺着它翻回世界里那件事。

    没有 `ref` 的由头是不许有的:一条说不出出处的"她想起你了",和引擎自己编了
    一个理由在产物上完全一样,而后者正是这一层最该避免的东西。
    """

    kind: str
    weight: float
    note: str = ""
    ref: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "weight": round(float(self.weight), 4),
            "label": REASON_LABELS.get(self.kind, self.kind),
            "note": self.note,
            "ref": dict(self.ref),
        }


@dataclass
class Decision:
    """一次判定的**全部理由**,不管过没过闸。

    不过闸也带着分数和成分返回,因为"她为什么没想起我"和"她为什么想起我"是同
    一个问题的两面 —— 只在触发时才说得清的机制,调不动也查不出。
    """

    fire: bool = False
    score: float = 0.0
    threshold: float = 0.0
    closeness: float = 0.0
    urge: float = 0.0
    readiness: float = 0.0
    reasons: list[Reason] = field(default_factory=list)
    blocked_by: str = ""

    @property
    def top_reason(self) -> Reason | None:
        return max(self.reasons, key=lambda r: r.weight) if self.reasons else None

    def components(self) -> dict[str, Any]:
        return {
            "closeness": round(self.closeness, 4),
            "urge": round(self.urge, 4),
            "readiness": round(self.readiness, 4),
            "score": round(self.score, 4),
            "threshold": round(self.threshold, 4),
        }

    def explain(self) -> str:
        """一句人话。`contact --json` 之外那半张脸,以及日志。"""
        if self.blocked_by:
            return REFUSAL_LABELS.get(self.blocked_by, self.blocked_by)
        if not self.reasons:
            return "没有由头 —— 她不会因为闲着就想起一个人"
        head = self.top_reason
        verdict = "想起他了" if self.fire else "还不到份上"
        return (
            f"{verdict}({self.score:.2f} / 门槛 {self.threshold:.2f});"
            f"最重的由头:{REASON_LABELS.get(head.kind, head.kind) if head else '—'}"
        )


def closeness(relation: Any) -> float:
    """三轴加权到 [0, 1]。没有关系行 = 0(不是负数:陌生不是敌意)。"""
    if relation is None:
        return 0.0
    total = 0.0
    for axis, weight in CLOSENESS_WEIGHTS.items():
        total += weight * float(getattr(relation, axis, 0.0) or 0.0)
    return max(0.0, min(1.0, total))


def combine_urge(reasons: Sequence[Reason]) -> float:
    """多条由头合成一个 [0, 1)。

    概率式(`1 - Π(1-w)`)而不是求和:四条由头凑一块儿该比一条更想找你,但不该
    因为攒够了数量就必然触发 —— 求和的话四条 0.4 的由头加出 1.6,门槛形同虚设。
    """
    remainder = 1.0
    for reason in reasons:
        remainder *= 1.0 - max(0.0, min(1.0, float(reason.weight)))
    return 1.0 - remainder


def absence_weight(idle_ticks: int, absence_ticks: int) -> float:
    """久别的分量:一个世界日起步,三个世界日封顶。

    **要封顶。** 不封的话一个下线三个月的玩家会让这一条永远压满,于是她一登录
    就被四个角色同时想起 —— 而那不是想念,是这个公式没有上界。
    """
    span = max(1, int(absence_ticks))
    if idle_ticks < span:
        return 0.0
    over = min(1.0, (idle_ticks - span) / (2.0 * span))
    return REASON_WEIGHTS["absence"] * (0.5 + 0.5 * over)


def readiness(
    *,
    blockers: Sequence[str] = (),
    mood: float | None = None,
    initiative: float = 1.0,
    stance: str | None = None,
) -> tuple[float, str]:
    """她这会儿是不是这个状态。返回 `(系数, 被哪条硬闸挡住)`。

    硬闸优先,而且**按 `BLOCKER_LABELS` 的次序报第一条** —— 一个睡着的、在赶路的、
    手上还有活的人,报哪条都对,但报的必须是同一条,不然同样的状态两次跑给出两个
    解释,而那种不一致比说不出原因更难查。
    """
    for name in BLOCKER_LABELS:
        if name in blockers:
            return 0.0, name
    # 心气儿:差到底也只是 0.6 倍,不是不想找人。心情坏的时候找人说话是很常见的
    # 一件事 —— 把它做成硬闸就等于宣称"人只在高兴的时候才想念别人"。
    mood_factor = 1.0 if mood is None else 0.6 + 0.4 * max(0.0, min(1.0, float(mood)))
    factor = mood_factor * max(0.0, min(2.0, float(initiative)))
    if stance:
        factor *= STANCE_FACTORS.get(stance, 1.0)
    return max(0.0, factor), ""


def threshold_now(base: float, fired_today: int, fatigue: float) -> float:
    """今天已经找过几次之后,下一次要多强的由头才够。

    这是那条"衰减":上限(`contact.max_per_day`)管的是**绝不超过几次**,这一条
    管的是**越找过越难再找** —— 只有上限的话,她今天的两次会挤在同一个小时里,
    因为过了闸之后再触发一次的代价是零。
    """
    return float(base) * (max(1.0, float(fatigue)) ** max(0, int(fired_today)))


def decide(
    *,
    relation: Any,
    reasons: Sequence[Reason],
    blockers: Sequence[str] = (),
    mood: float | None = None,
    initiative: float = 1.0,
    stance: str | None = None,
    min_closeness: float = DEFAULT_MIN_CLOSENESS,
    base_threshold: float = DEFAULT_THRESHOLD,
    fired_today: int = 0,
    fatigue: float = DEFAULT_FATIGUE,
) -> Decision:
    """她这会儿想不想找他。**纯函数** —— 同样的输入永远同样的输出。

    **亲密度先算,哪怕硬闸已经拦下了她。** 它是纯算术,不要钱;而少了它,
    `contact_forecast()` 会对着一个正在睡觉的挚友打出"近 0.00" —— 调阈值的人
    照那个数字去调,调的是一个不存在的问题。观察窗不许撒谎,这一条在这儿的样子
    就是"没参与判断的成分也要如实报出来"。
    """
    limit = threshold_now(base_threshold, fired_today, fatigue)
    near = closeness(relation)
    ready, blocked = readiness(
        blockers=blockers, mood=mood, initiative=initiative, stance=stance
    )
    if blocked:
        return Decision(threshold=limit, blocked_by=blocked, closeness=near,
                        reasons=list(reasons), readiness=0.0)
    if near < float(min_closeness):
        return Decision(threshold=limit, closeness=near, readiness=ready,
                        reasons=list(reasons), blocked_by="not_close_enough")
    urge = combine_urge(reasons)
    score = near * urge * ready
    return Decision(
        fire=bool(reasons) and score >= limit,
        score=score, threshold=limit,
        closeness=near, urge=urge, readiness=ready,
        reasons=list(reasons),
    )


# ── 那句线索 ────────────────────────────────────────────────────────────────
#
# **它不是台词。** 台词要等真的开口那一刻才成立(玩家还没回话,世界里什么也没
# 发生 —— `agent_hail` 的那条边界这里同样适用)。这里给的是"她想跟你说的是哪
# 件事",宿主拿它做一句预览、一条推送的标题,或者干脆只用 `reason`。
#
# 没有 LLM 时它退成由头那条记忆的摘要 —— **降级不许无声**,所以退了要在
# `contact_stats()` 里点名。

DEFAULT_COMPOSE_PROMPT = (
    "你是{name}。{personality}\n\n"
    "现在是第 {day} 天 {hh}:{mm},你在{location}。\n"
    "你忽然想起了{player}。想起他的由头是:\n"
    "{reason_block}"
    "{state_block}\n"
    "【只写一句】\n"
    "用一句话写出你想跟他说的是**哪件事** —— 不是台词,是那个念头本身,"
    "二十个字以内。\n"
    "照你自己的性格写:别扭的人不会写「我想你了」,他会写「问问他那件事办完没有」。\n"
    "只输出这一句,不要引号,不要解释,不要动作描写。"
)


def build_compose_messages(
    template: str,
    *,
    name: str,
    personality: str,
    day: int,
    hour: int,
    minute: int,
    location: str,
    player: str,
    reasons: Sequence[Reason],
    notes: Sequence[str] = (),
) -> list[dict[str, str]]:
    reason_block = "".join(
        f"- {REASON_LABELS.get(r.kind, r.kind)}:{r.note}\n" for r in reasons
    ) or "- 说不上来\n"
    system = template.format(
        name=name,
        personality=personality,
        day=day,
        hh=f"{hour:02d}",
        mm=f"{minute:02d}",
        location=location or "某个地方",
        player=player,
        reason_block=reason_block,
        state_block="".join(f"- {note}\n" for note in notes),
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "你想跟他说的是哪件事?"},
    ]


def fallback_topic(reasons: Sequence[Reason]) -> str:
    """没有模型(或模型失败)时那句线索。取最重的一条由头的原文。"""
    head = max(reasons, key=lambda r: r.weight) if reasons else None
    if head is None:
        return ""
    return head.note or REASON_LABELS.get(head.kind, head.kind)


def parse_topic(text: str, *, limit: int = 60) -> str:
    """把回包收成一句。读不懂就返回空串,让调用方退回 `fallback_topic`。

    宽容的方向和 `autonomy.parse_decision` 相反:那边读错会**多发**一条搭话,
    这边读错只会让线索难看一点 —— 事件本身该不该发,判定那一步早就定了。
    """
    from anima_world.directives import strip_directives

    raw = (text or "").strip()
    if not raw:
        return ""
    clean, _ = strip_directives(raw)
    for line in clean.splitlines():
        line = line.strip().strip("“”\"'「」 ")
        if line:
            return line[:limit]
    return ""
