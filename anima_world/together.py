"""together:**一起做事**。

这一层补的是"共同生活"里最基本、而引擎此前完全说不出的一半:两个人站在同一个
地方,却只能各干各的。`interact` 一直是单人的 —— 一个人、一个东西、一个瞬间
(`ontology.Affordance` 的长注释就是这么定义它的)。于是世界里有"她在这儿"和
"他也在这儿",没有"他们一起吃了顿饭"。

而**共同经历正是关系的主要来源**。此前关系只有两条来路:说了多少句话(聊天判定)
和听说了什么(八卦 / 吃醋)。两条都是**语言**。一个只靠说话改变关系的世界,和一个
聊天机器人的差别只剩下背景板 —— 一起熬过的一夜该比一百句寒暄更算数,而引擎连
"一起熬过一夜"这件事都记不下来。

## 三条红线,每条都决定了这个模块长什么样

### 1. 别人凭什么答应

一个"拉着谁就一起吃饭"的能力**等于取消对方的意志**,而这个引擎的全部立场就是
角色自己做决定。所以邀请必须能被拒绝,而且**拒绝的理由要分得开**:

- **世界说不行**(硬闸,`GATE_LABELS`)—— 不在同一个地方、睡着了、手上有别的事、
  把你静音了、这件事她做不了(力气不够 / 没带工具)。这些**不是她的意思**,
  是世界的状态,所以它们判在她之前,而且一个字都不写。
- **她不答应** —— 过了硬闸之后的那一句。这一句必须**由性格决定**。

第二段有两条路,而且**两条都不许是"引擎替她答应"**:

1. **有判定器**(配了 key):`RelationshipJudge.judge_invite` 读她的人设、她跟
   邀请人的关系、她最近记得的事,给一个 accept/reject 加一句理由。和吃醋那条
   逐字同源 —— 同一句"一起吃个饭吧",别扭的人会推掉,坦荡的人会跟着去,
   而这个差别只有读得到人设的东西给得出来。
2. **没有判定器**(默认状态):退回 `willingness()` —— 关系有多近 × 她有多随和 ×
   她上一轮对他的关系性意图。这**不是掷骰子,也不是引擎做主**:
   - 关系是**世界长出来的**(别扭的人和你的好感爬得慢,于是她本来就更晚才肯);
   - 「随和」是**作者在数据里声明过的**(`social.joint.consent_stock`,
     没声明 = 1.0)—— 和 `contact.initiative_stock` 逐字同构,和本体层
     "声明本身就是开关"同一条。

   这个替身站得住,是因为"她跟谁走得近就更肯跟谁一起做事"这句话**与邀请的内容
   无关**,所以一个不看内容的近似仍然是一个真实的判断(对照 `judge_hearsay`:
   吃醋的全部内容就是"这句话",任何不看内容的替身都只能是"听到就扣 0.05",
   于是白霜和「零」得到同一个数字 —— 那种替身宁可不要)。

   **降级仍然要吭声**(`note_subsystem("joint_consent", …)`)。

⚠️ 没有 `consent` 这个开关,**同意永远是必须的**。给作者一个 `consent: false`
等于把上面整段话交回给作者去关掉,而"拉着谁就一起吃饭"正是它要挡的东西。
少一个能问的问题,比多一分表达力值钱(和 `consumes` 只收整数同一条)。

### 2. 代价对每个人各扣一次,而顺序不许有意义

`participants: ["柔", "白霜"]` 和 `["白霜", "柔"]` 必须给出**逐位相同**的世界。
做法和规律那一层的双缓冲同源:**先把所有人的量与随身物读成快照,在快照上逐个
算,全过了才动手写**。边算边写的话,第一个人扣掉的体力会成为第二个人 `requires`
的输入 —— 于是名单顺序会决定谁做得成,而那是一个**没有任何人写下过**的规则。

**一个人过不了闸,整件事就不发生**(`拒绝时一个字都不写`,和 `apply_affordance`
同一条)。三个人吃饭,一个人没钱,不该变成两个人吃饭 —— 那是引擎替他们改了计划。

### 3. 关系变化是共同经历的效果,不是再调一次判定

**这一条是这一批需求要治的病本身。** 再调一次 LLM 判定的话,"一起过了一夜"和
"多聊了两句"又回到同一个入口上,而这一层存在的全部理由就是它们不该一样。

所以效果是**算出来的**,而且只看那次经历本身的三样:

    delta = step × 人数因子 × 时长因子 × (1 - |当前值|)

- **人数因子** `2 / max(2, n)`:两个人最亲,人越多越淡。十个人的宴会不该让每一对
  都长出一顿双人晚饭的分量。
- **时长因子** `0.5 + 0.5 × min(1, duration / full)`:一起待得越久越算数。
  一下子的事(`duration: 0`)拿一半 —— 不是零,一起看一眼雪也是一起看过。
- **按剩余空间衰减**:和 `DeterministicRelationshipJudge` 逐字同一条 —— 确定性、
  越熟越难再进一步、渐近而永不饱和。

`DEFAULT_RELATION_STEP`(0.06)**大于** `DeterministicRelationshipJudge.STEP`
(0.04),这是有意的,而且有一条测试钉着它:**一起经历过的事,应该比说过多少句话
更能改变关系。** 哪天有人把这两个数字调反,那条测试会红。

这个模块是**纯函数**:不掷骰子、不 import 存储层、不认识 Redis 也不认识 LLM ——
和 `contact.py` / `gossip.py` 同一条纪律。判定要能被单独测,而不是只能"开一个
世界跑一遍看看"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

# ── 默认刻度 ────────────────────────────────────────────────────────────────

DEFAULT_CONSENT_STOCK = "随和"

# 没有判定器时的门槛。**照橱窗那个世界对过账**(和 `contact` 那几个刻度照线上那个
# 世界定的是同一条纪律:一个在真世界里从不触发的默认值,表现和机制没接上一模一样)。
#
#     夏 → 柔   closeness 0.6×0.55 = 0.33 × 随和 1.0  = 0.33   坐得下来
#     柔 → 夏   closeness 0.6×0.55 = 0.33 × 随和 1.4  = 0.46   坐得下来
#     夏 → 遥   closeness 0.6×0.35 = 0.21 × 随和 0.35 = 0.07   遥推掉
#     遥 → 柔   closeness 0.6×0.15 = 0.09 × 随和 1.0  = 0.09   柔推掉
#
# 0.20 而不是 `contact.min_closeness` 的 0.35:**被站在旁边的人当面邀一次,门槛
# 天然比"隔着半张地图想起一个人"低**。两个数字管的不是同一件事。
DEFAULT_MIN_WILLINGNESS = 0.20
DEFAULT_RELATION_STEP = 0.06
# 一起待多久算"待满了"。12 tick = 5 分钟/tick 时的一个世界小时 —— 一顿饭。
DEFAULT_FULL_DURATION_TICKS = 12

# 一份等着人回答的邀请能等多久。**按世界时钟数**(见 `invitation_settled`):
# 墙钟会让同一份日志重放出两个结果,而两边都不报错。
DEFAULT_INVITE_TTL_TICKS = 12
# 同一个她、同一个他,一个世界日里她最多开口几次。**这是「像个人」和「像推送」
# 的分界**:一个不设上限的邀请者,在玩家眼里和一个消息推送没有区别,而她本该是
# 一个人。用完了不是错 —— 她今天不再开口而已。
DEFAULT_INVITES_PER_PLAYER_PER_DAY = 2

# 一份邀请的结局。**「拒绝」和「过期」必须分得开**,而且只有前者留痕:
#
# - `accepted` 他答应了 —— 那件事这就去做(做之前还要再过一次世界的闸)。
# - `declined` 他按了"不" —— 这是一个**人做出的决定**,所以它进记忆、进关系。
# - `expired` 他没答 —— 这**不是拒绝**,是错过。手机上的人放下手机去吃了顿饭,
#   回来发现她问过他一句;把这记成"他拒绝了我"是引擎在替他说话,而且是说反话。
#   所以这一支**不落记忆、不动关系**,一个字都不写在他头上。
# - `cancelled` 邀请的人自己撤了(她走开了 / 睡着了 / 手上有了别的事)。
INVITE_OUTCOMES = ("accepted", "declined", "expired", "cancelled")

# 亲密度的三轴加权。**和 `contact.CLOSENESS_WEIGHTS` 是同一份**,理由也同一条:
# `respect` 不进(敬重不使人想念,也不使人想一起吃饭),而 `sentiment` 必须占
# 大头 —— 线上那个世界二十条关系的细三轴无一例外全是 0(见 contact.py 的长注释),
# 均摊权重下这一层在真世界里**一次都不会通过**,而表现和"她不想跟你吃饭"一样。
CONSENT_WEIGHTS: dict[str, float] = {
    "sentiment": 0.6,
    "affection": 0.25,
    "trust": 0.15,
}

# 她上一轮对他的关系性意图怎么改她肯不肯。中性是 1.0,所以 **stance 关着的世界
# 行为逐位不变**(和 `contact.STANCE_FACTORS` 同一条)。
STANCE_FACTORS: dict[str, float] = {
    "neutral": 1.0,
    "avoid": 0.20,      # 「回避」的人不会跟你一起坐下来
    "provoke": 0.65,
    "test": 1.05,
    "defer": 1.15,      # 「让步」的人更容易被拉着走
    "vent": 0.90,
    "please": 1.30,
    "seduce": 1.35,
}

# 硬闸。**这几条不是她的意思,是世界的状态** —— 所以它们判在性格之前,而且分开
# 列是为了说得出是哪一条:一句笼统的"她没答应"会让玩家(和她自己)以为被拒绝的
# 是这个人,而真正的原因可能只是他在赶路。
#
# **每一句都写成"接在名字后面"的样子**(「沈遥」+「不在这儿」),而不是自带主语的
# 完整句。回执是 `名字 + 这一句` 拼出来的,自带一个「他」就会拼成"沈遥他不在这儿"——
# 一句读起来像机器写的话。
#
# ⚠️ **"单独看也仍然成立"这句话从前写在这儿,而它是假的**(3.6.0 第五轮改口):
# `player_where_unknown` 是「〈名字〉**在哪**,世界这会儿不知道」这种把名字嵌进
# 句首的写法,拆掉名字剩下的是个没有主语的残句。有意留着这个写法 —— 接在名字
# 后面时它是这一族里最像人话的一句,而**这一格从来只在名字后面出现**:
# `api._invitee` / `Scheduler._joint_refusal` 两处拼的都是 `名字 + 这一句`,
# `Consent.explain()` 在仓库里只有一处消费方(`tests/test_together.py`,验的是
# "说得出是哪一条闸")。哪天真有人把 `explain()` 单独印给玩家看,要改的是这一格
# 的措辞,不是把这条丑话删掉。
GATE_LABELS: dict[str, str] = {
    "self": "就是发起的那个人 —— 一个人做不成「一起」",
    "unknown": "不在这个世界里",
    "in_transit": "在赶路,这会儿不在任何地方",
    "elsewhere": "不在这儿",
    "asleep": "睡着了",
    "busy": "手上还有一件走不开的事",
    "muted": "把发起的这个人静音了",
    "incapable": "这会儿做不了这件事",
    "conditions": "这会儿还轮不到这件事",
    # 「不在她跟前」这**一句话原本装着四件事**:他真的在别处 / 世界不知道他在哪 /
    # 开口的那个人在赶路 / 世界不知道开口的那个人在哪(`face_to_face()` 把这四种
    # 折成同一个 False)。合成一句的报应是具体的:一个宿主还没调过 `player_move`
    # 的世界,每一次相约都读起来像"玩家自己站错了地方",而他做什么都改不掉 ——
    # 那句话是假的。分法逐字照 `intent._colocation_refusal` 的三分表,多出来的
    # 一条是"她在赶路"(那边由 `agent_in_transit` 那一支单独兜着)。
    "player_not_here": "不在她跟前 —— 一起做事得当面",
    "player_where_unknown": "在哪,世界这会儿不知道 —— 一起做事得当面,"
                            "而没有人告诉过世界他在哪",
    "inviter_in_transit": "还当不了面 —— 开口的那个人在赶路,这会儿不在任何地方",
    "inviter_where_unknown": "还当不了面 —— 世界不知道开口的那个人在哪",
    "player_not_you": "不是这次调用的那个玩家,替不了他答应",
    # 这两条是**世界的状态**(作者关了那扇门 / 她今天问够了),不是他的意思 ——
    # 所以和上面几条同一族,判在性格之前,而且一个字都不写在他头上。
    # ✅ **「这个世界」这三个字核过了,照实,不改**(3.6.0 第六轮 2026-08-20):
    # 唯一产出 `invites_off` 的是 `Scheduler._invite_config()`,它读的是**世界级**
    # 配置键 `social.joint.npc_may_invite_player`(scope `social`,默认 True),
    # 仓库里没有任何按玩家覆盖它的路径 —— 这道闸的开关在世界手上,不在他手上,
    # 写成「你关掉了…」才是假话。记在这儿是为了下一个人不必再核一遍。
    "player_invites_off": "这个世界不让角色主动开口相约",
    "invite_capped": "今天已经被约过了 —— 再问下去就不像人,像推送了",
}

# 上面那四条里,哪几条的意思是「这件事得当面而没当成」。**分开列是因为下游要按它
# 分支**:`World.answer_invitation()` 拿它决定要不要去查"到底是谁不在场"
# (`_invite_absence`)。写成 `== "player_not_here"` 的话,拆闸这件事就会**悄悄
# 关掉那一支** —— 拆之前那个判断覆盖 100% 的当面失败,拆之后只剩四分之一,
# 而少掉的那三种恰好是最需要点名的。
COLOCATION_GATES: frozenset[str] = frozenset({
    "player_not_here", "player_where_unknown",
    "inviter_in_transit", "inviter_where_unknown",
})

# 她开了口,他还没答。**这不是拒绝,也不是硬闸** —— 硬闸的意思是"这件事这会儿
# 办不成",而这一条的意思是"这件事正等着一个人回话"。合成一个的话,玩家读到的
# "她约了你"和"她约不动你"长得一模一样。
INVITE_PENDING = "invited"
INVITE_PENDING_LABEL = "还没答话 —— 已经问过了,等他点头"

# 她自己给的那一句。和硬闸分开放,理由和 `contact.REFUSAL_LABELS` 逐字同一条:
# 调用方不该能把"她不肯"当成硬闸传进来 —— 那是性格那一层的判断。
DECLINE_REASON = "declined"
DECLINE_LABEL = "不想去"


@dataclass(frozen=True)
class Invitee:
    """一个被邀请的人,以及**判断他要用到的全部输入**。

    收成一个 dataclass 而不是一串位置参数,是因为这些字段一半来自调度器、一半
    来自存储层,而这个模块两样都不认识 —— 边界画在这里,判定才测得动。
    """

    id: str
    name: str = ""
    is_player: bool = False
    # 世界那一段:这几样任意一条成立就整个不问。
    gate: str = ""
    # 性格那一段的输入。
    relation: Any = None            # 他 → 发起人 的关系行(有三轴属性即可)
    agreeableness: float = 1.0      # 作者声明的那个量;没声明 = 1.0
    stance: str = ""                # 他上一轮对发起人的关系性意图
    personality: str = ""
    memories: tuple[str, ...] = ()
    # 他和发起人**这会儿还在说的话**。和 `memories` 是两件事:记忆是会话关闭
    # 那一刻才落的,而邀请正发生在会话中间 —— 只给记忆的话,判定器判的是一个
    # 「我不认识这个人」的处境,而他们刚聊了两轮。
    recent_talk: tuple[str, ...] = ()


@dataclass
class Consent:
    """一个人对一次邀请的答复,**连同它是怎么来的**。

    `source` 三种:`gate`(世界说不行)、`judge`(读了人设的判定)、
    `willingness`(没有判定器时按关系与声明的量算)。分得开是必须的 ——
    否则"这个世界里她从来不肯"和"这个世界根本没配 key"长得一模一样。
    """

    who: str
    accepted: bool
    reason: str = ""
    source: str = "willingness"
    score: float = 0.0
    threshold: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "who": self.who, "accepted": self.accepted,
            "reason": self.reason, "source": self.source,
        }
        if self.note:
            payload["note"] = self.note
        if self.source == "willingness":
            payload["score"] = round(self.score, 4)
            payload["threshold"] = round(self.threshold, 4)
        return payload

    def explain(self) -> str:
        """一句人话 —— 回执里给玩家看的那半张脸。"""
        if self.reason in GATE_LABELS:
            return GATE_LABELS[self.reason]
        if self.note:
            return self.note
        return DECLINE_LABEL


def closeness(relation: Any) -> float:
    """三轴加权到 [0, 1]。没有关系行 = 0(陌生不是敌意,所以不取负)。"""
    if relation is None:
        return 0.0
    total = 0.0
    for axis, weight in CONSENT_WEIGHTS.items():
        total += weight * float(getattr(relation, axis, 0.0) or 0.0)
    return max(0.0, min(1.0, total))


def willingness(
    relation: Any, *, agreeableness: float = 1.0, stance: str = ""
) -> float:
    """他有多肯 —— **纯函数**,同样的输入永远同样的输出。

    乘法而不是加法:三个因子**各自都能单独把他挡下来**。一个跟你毫无来往的人,
    再随和也不会跟你坐下来吃饭;一个「回避」着你的人,关系再好这会儿也不想。
    """
    factor = max(0.0, min(2.0, float(agreeableness)))
    score = closeness(relation) * factor
    if stance:
        score *= STANCE_FACTORS.get(stance, 1.0)
    return max(0.0, min(2.0, score))


def decide_alone(
    invitee: Invitee, *, min_willingness: float = DEFAULT_MIN_WILLINGNESS
) -> Consent:
    """没有判定器时的那一条路。硬闸优先,然后按关系与声明的量算。"""
    if invitee.gate:
        return Consent(who=invitee.id, accepted=False, reason=invitee.gate,
                       source="gate", note=GATE_LABELS.get(invitee.gate, invitee.gate))
    score = willingness(
        invitee.relation, agreeableness=invitee.agreeableness, stance=invitee.stance
    )
    limit = float(min_willingness)
    return Consent(
        who=invitee.id, accepted=score >= limit,
        reason="" if score >= limit else DECLINE_REASON,
        source="willingness", score=score, threshold=limit,
    )


# ── 共同经历的效果 ──────────────────────────────────────────────────────────


def party_factor(party_size: int) -> float:
    """人数因子:两个人最亲,人越多越淡。`2 / max(2, n)`,所以 n=2 时是 1.0。"""
    return 2.0 / max(2.0, float(party_size))


def duration_factor(
    duration_ticks: int, *, full_duration: int = DEFAULT_FULL_DURATION_TICKS
) -> float:
    """时长因子。**一下子的事拿一半,不是零** —— 一起看一眼雪也是一起看过。"""
    span = max(1, int(full_duration))
    return 0.5 + 0.5 * min(1.0, max(0, int(duration_ticks)) / span)


def experience_delta(
    *,
    current: float,
    party_size: int,
    duration_ticks: int,
    step: float = DEFAULT_RELATION_STEP,
    full_duration: int = DEFAULT_FULL_DURATION_TICKS,
) -> float:
    """一次共同经历让 a 对 b 的好感动多少。

    **按剩余空间衰减**(`1 - |当前值|`),和 `DeterministicRelationshipJudge._drift`
    逐字同一条:确定性(世界的可重放性不能靠随机数)、越熟越难再进一步、
    渐近而永不饱和 —— 一起吃一百顿饭不会把一段关系顶到 +1.0。
    """
    try:
        value = float(current)
    except (TypeError, ValueError):
        value = 0.0
    raw = (
        float(step)
        * party_factor(party_size)
        * duration_factor(duration_ticks, full_duration=full_duration)
        * (1.0 - min(1.0, abs(value)))
    )
    return round(max(-1.0, min(1.0, raw)), 4)


def experience_axes(delta: float) -> dict[str, float]:
    """三轴跟着主轴走。**和聊过天不一样的地方在这里**:

    说过话最先长出的是喜爱与信任(`DeterministicRelationshipJudge` 给的是
    `affection=d, trust=d/2, respect=0`);而**一起把一件事做完**是敬重唯一
    长得出来的地方 —— 这个引擎里 `respect` 从来没有动过一次(线上二十条关系
    全是 0),不是因为它不重要,是因为此前没有任何机制会写它。一起干过活会。
    """
    return {
        "affection": round(delta, 4),
        "trust": round(delta * 0.5, 4),
        "respect": round(delta * 0.5, 4),
    }


# 被当面推掉,相对于"一起做过一次"的分量。**比一次同行轻** —— 一次拒绝不该
# 抵消掉三次同行,否则一段关系会被"她问了三次、他推了三次"清零,而那三次里
# 他可能只是在忙。取 1/3:一起做一件一下子的事是 `step × 1.0 × 0.5` = 半份,
# 一次拒绝是三分之一份,两者相比 ≈ 0.67 : 1。
DECLINE_FACTOR = 1.0 / 3.0


def decline_delta(current: float, *, step: float = DEFAULT_RELATION_STEP) -> float:
    """他当面推掉一次,她对他的好感动多少。**负数,而且比一次同行轻。**

    ⚠️ **只有明确的拒绝走这一条,过期不走**(见 `INVITE_OUTCOMES`)。

    同样按剩余空间衰减(`1 - |当前值|`),和 `experience_delta` 逐字同一条:
    一个跟她已经很生分的人再推一次,扣不动多少 —— 关系的下限和上限一样,
    是渐近的,不是一条一路向下的滑梯。
    """
    try:
        value = float(current)
    except (TypeError, ValueError):
        value = 0.0
    raw = -float(step) * DECLINE_FACTOR * (1.0 - min(1.0, abs(value)))
    return round(max(-1.0, min(1.0, raw)), 4)


def decline_axes(delta: float) -> dict[str, float]:
    """一次拒绝动哪几轴。**`respect` 不动** —— 他有权说不,而这个引擎的立场
    正是"邀请必须能被拒绝"(红线 1)。让敬重跟着掉,等于引擎一边说着"你可以
    拒绝",一边为此扣他的分。
    """
    return {
        "affection": round(delta, 4),
        "trust": round(delta * 0.5, 4),
    }


@dataclass(frozen=True)
class PairDelta:
    """一条"我对他"的变化。有向 —— 两个人对同一段经历的收获不必相同。"""

    who: str
    about: str
    delta: float
    axes: Mapping[str, float] = field(default_factory=dict)


def pair_deltas(
    party: Sequence[str],
    current: Mapping[tuple[str, str], float],
    *,
    duration_ticks: int,
    step: float = DEFAULT_RELATION_STEP,
    full_duration: int = DEFAULT_FULL_DURATION_TICKS,
    minimum: float = 0.005,
) -> list[PairDelta]:
    """一整场共同经历的关系账。**每一对有序对各一条。**

    `party` 的**顺序不影响结果**(红线 2 的另一半):每一对各自只读 `current`
    里那一条,谁先算谁后算都一样。返回值按 `(who, about)` 排序,所以事件落进
    日志的次序也是确定的 —— 重放两次要得到逐字相同的历史。

    `minimum` 以下的一律丢掉:判了个 0 等于没判,别为它发一条事件
    (和 `judge_hearsay` 里 `abs(delta) < 0.01` 那一句同一条)。
    """
    people = list(dict.fromkeys(str(p) for p in party if str(p)))
    size = len(people)
    out: list[PairDelta] = []
    for who in people:
        for about in people:
            if who == about:
                continue
            delta = experience_delta(
                current=current.get((who, about), 0.0),
                party_size=size, duration_ticks=duration_ticks,
                step=step, full_duration=full_duration,
            )
            if abs(delta) < minimum:
                continue
            out.append(PairDelta(who=who, about=about, delta=delta,
                                 axes=experience_axes(delta)))
    return sorted(out, key=lambda p: (p.who, p.about))


# ── 判定器要读的那一段提示词 ────────────────────────────────────────────────
#
# 模板住在 `relationship_judge.py` 和它的兄弟们一起(`judge.*` 前缀,进
# `prompt_store` 才调得动)。这里只放**把邀请写成一句人话**的那一步 —— 它同时
# 是给玩家的回执文案,两处各写一遍迟早说出两件事。


def describe_invitation(
    *, inviter: str, verb_label: str, target_name: str, others: Sequence[str] = ()
) -> str:
    """「苏晚夏指着门口那棵老橡树,叫你一起照料」。

    **动词用人话那一份**(`Affordance.label`):她读到的、玩家听到的、判定器收到
    的必须是同一个词。给一个 `tend` 过去,判定器判的是一件她根本没听说过的事。

    ⚠️ **「一起」这个前缀会撞。** 共做的能力,作者起名时本来就爱写「一起听一段」
    「一起躲会儿雨」(晚潮那个世界 15 个共做动词里 3 个如此),再套一层就成了
    「阿布叫你**一起一起**听一段」—— 判定器和玩家读到的是同一句结巴话。

    ⚠️ **东西的名字不能直接焊在动词后面。** 作者写的 label 是一句完整的话
    (「树下坐会儿」「听完一面」),后面接一个名词就成了「叫你一起树下坐会儿
    江堤上的老樟树」—— 语法上讲不通,而判定器只会照着这句读不通的话去判她答不
    答应。所以东西先出场(「指着…」),动词留在句尾:两种 label 都读得通。
    """
    company = ""
    if others:
        company = f"(还有{'、'.join(others)})"
    lead = "" if verb_label.startswith("一起") else "一起"
    return f"{inviter}指着{target_name},叫你{lead}{verb_label}{company}"
