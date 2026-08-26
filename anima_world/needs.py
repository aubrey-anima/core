"""needs-v3:需求曲线 —— 角色从"按表办事"到"活着"。

四条需求:energy / hunger / social 随 tick 衰减、由动作恢复;mood 是前三者的
派生(木桶效应),**永不存储**(第二真相源教训)。连续曲线也不进事件日志 ——
它们是纯数学,重放 tick 即可重建;当前值的持久化在存储层
(`anima_world.redis_state.RedisNeedsStore`),日切与关闭时落盘。

默认关闭(config `needs.enabled`):不点亮的世界行为与 v2 逐 tick 一致。

这个模块是**纯逻辑**:曲线、恢复表、紧急带阈值。SQLite 存取已随 world.db 层退役。
"""

from __future__ import annotations

NEEDS = ("energy", "hunger", "social")

#: 出厂插件的 id。**它是命名空间** —— 三个量在量表里叫 `needs.energy` 这样。
PLUGIN_ID = "needs"

# 每 tick(默认 5 世界分钟)的衰减:一天不睡精力见底、约 18 小时饿透、
# 一天半不社交就孤独。
DECAY_PER_TICK = {"energy": 1.0 / 288, "hunger": 1.0 / 216, "social": 1.0 / 432}

# 动作的恢复速率(净值 = 恢复 - 衰减):睡 8 小时(96 tick)回满,
# 吃一顿(约 1 小时)回 0.6,聊天/闲坐缓慢回社交。
RESTORE_PER_TICK: dict[str, dict[str, float]] = {
    "sleep": {"energy": 1.0 / 80},
    "eat": {"hunger": 0.05},
    "chat": {"social": 0.02},
    "idle_social": {"social": 0.015},
}

URGENT = 0.15  # 需求带的触发线:低于它,恢复动作压过 duty

# 释放线:开始恢复之后,**吃到饱/睡到醒**才收工,而不是跨回触发线就走。
#
# 单阈值(只有 URGENT)的后果实测过:hunger 掉到 0.15 → 吃一个 tick 净回 0.045 →
# 已经 0.195 高于触发线 → 立刻回去干活 → 十来个 tick 后再饿回来。角色永远卡在
# 16% 的饥饿度上,一顿饱饭都没吃过;而每一次切换都发一条 agent_action + 一条
# narrative,于是 12 世界日的事件量 19.7×、narrative 32×(配了真 key 就是 32 倍
# LLM 账单)、耗时 7×。世界并没有变得 32 倍有趣,只是抖得厉害。
#
# 取值按"一次恢复动作应该持续多久"倒推,与 RESTORE_PER_TICK 的设计口径一致:
#   hunger 0.15→0.75 ≈ 13 tick ≈ 一小时,正好是上面写的"吃一顿约 1 小时回 0.6"
#   energy 0.15→0.85 ≈ 78 tick ≈ 6.5 小时,一个补觉的长度(整夜是 duty 的事)
#   social 0.15→0.50 ≈ 20 tick ≈ 一个半小时的长谈
RELEASE = {"energy": 0.85, "hunger": 0.75, "social": 0.50}


def restores(action_kind: str | None) -> frozenset[str]:
    """这个动作正在补哪几条需求。迟滞的判据 —— 正在补的那条才用释放线。"""
    return frozenset(RESTORE_PER_TICK.get(action_kind or "", {}))


def mood_of(values: dict[str, float]) -> float:
    """三条需求 → 心气儿。**木桶效应**:最低的那条说了算。

    ⚠️ **它永不存储**(第二真相源那条教训):存下来的话,某个进程改了 energy 而
    没重算 mood,世界里就有两个互相矛盾的答案,而两个都"看上去正常"。
    现算是廉价的 —— 三个数取一次 min。

    ⚠️ **推进那一半 3.8.0 起不在这个模块里了。** 衰减与恢复现在是 `needs` 这个
    **出厂插件**的七条规律(见 `factory_plugin`),跑在世界自己那条规律引擎上。
    从前住在这儿的 `settle()` 因此退役 —— 曲线的三个常数一个字没改(还在上面
    那两张表里),换的是**谁来跑它**。
    """
    return 0.2 + 0.8 * min(float(values[need]) for need in NEEDS)


def drag_mood(mood: float, debt: float) -> float:
    """一笔**世界自己声明的债**把心气儿拖下去多少。纯函数,clamp 到 [0, 1]。

    这是"熬夜有代价"落到她的决定上的那一步,而它刻意长成这个形状:

    - **债由世界定义,不由引擎定义。** 引擎不知道什么叫「睡眠债」,只知道
      `needs.mood_penalty_stock` 指着她身上的哪个量。写进 `settle()` 的话,
      每个世界都得吃同一条曲线 —— 而"熬夜"在一个修真世界里可能根本不是代价。
      量怎么攒、怎么还,是作者用一条规律写的(见演示世界的 `熬夜攒睡眠债`)。
    - **声明本身就是开关。** 没配那个键的世界这里一次都不会被调到,行为逐位不变
      (和 perception / ontology 同一条)。
    - **拖,不是清零。** 一个熬了通宵的人不是没有心气儿,是心气儿差 —— 减法而
      不是乘法,是因为乘法在 mood 已经很低时几乎不起作用,而起床气恰恰是在
      "又累又困"的时候最明显。
    - **债本身要有界(0~1)**,由作者在规律里 clamp。这里再兜一次底:一个写漏了
      上限的规律会让 mood 永远贴着 0,而那看起来和"这个人永远心情很差"一模一样。
    """
    try:
        owed = float(debt)
    except (TypeError, ValueError):
        return mood
    return max(0.0, min(1.0, float(mood) - max(0.0, min(1.0, owed))))


# ── 出厂插件:needs 是第一个搬出去的内置系统(3.8.0,设计稿 §9)─────────────────
#
# 🔴 **这一段是这个模块存在方式的整个转身。** 从前它是"引擎替所有世界写死的三个量
# 加两张表";现在它是**一份声明** —— 和作者自己写的插件用**同一种形状**、走同一条
# 装载路、吃同一个规律引擎。设计稿那句检验标准只有一条:**形状对不对,不看例子,
# 看能不能把出厂的东西用同一形状搬出去。**
#
# 曲线的三个常数一个字没改(上面那两张表就是它们的家),搬的是**谁来跑它** ——
# 从 `Scheduler._settle_agent_needs` 里那段 Python,变成七条规律。
#
# ⚠️ **为什么每个量要两条规律(而 `social` 是三条,一共七条)。** 今天的 `settle()` 一次做两件事:总是衰减,
# 而且**如果她正在做那件恢复动作**就再加一份。规律层没有"读她此刻在做什么"这种
# 表达式,只有选择器 —— 所以拆成**互不相交**的两条:`{"action": …}` 那条是
# 「正在做的人」(衰减 + 恢复),`{"not_action": [...]}` 那条是它的补集(只衰减)。
# 两条**划分**了所有人,所以永远不会抢同一个量。
# `social` 有两个恢复动作,于是它是三条,而那正是 `not_action` 从单数变成
# 一列的理由(见 `rules._parse_selector`)。


def _decay(need: str) -> str:
    return f"{PLUGIN_ID}.{need} - {DECAY_PER_TICK[need]!r} * dt"


def _fact(need: str) -> dict:
    """三个量的声明。**`hidden` 是有意的** —— 今天 needs 一格都不进感知块
    (她的饿由行为树的紧急带表达,不由提示词里一行数字表达),而
    "没声明 = 感知不到"正是这一层的默认值。改成 `self` 会当场改变每个世界的提示词。
    """
    return {"bearer": "agent", "shape": "number", "default": 1.0,
            "range": [0.0, 1.0], "visibility": "hidden", "label": need}


def factory_plugin() -> dict:
    """出厂的 needs 插件声明。**每次现算,不做成模块级常量** ——
    调用方会把它塞进世界文件那条合并路,而一份被人就地改过的常量会跟着世界跑。
    """
    rules: list[dict] = []
    for need in NEEDS:
        restoring = sorted(
            action for action, table in RESTORE_PER_TICK.items() if need in table
        )
        for action in restoring:
            gain = RESTORE_PER_TICK[action][need]
            rules.append({
                "id": f"{need}_{action}", "every": {"ticks": 1},
                "for_each": {"action": action},
                "set": {f"{PLUGIN_ID}.{need}":
                        f"clamp({_decay(need)} + {gain!r} * dt, 0.0, 1.0)"},
            })
        # 补集:此刻没在做任何一件恢复这条需求的事 —— 只衰减。
        rules.append({
            "id": f"{need}_decay", "every": {"ticks": 1},
            "for_each": ({"not_action": restoring} if restoring
                         else {"kind": "agent"}),
            "set": {f"{PLUGIN_ID}.{need}": f"clamp({_decay(need)}, 0.0, 1.0)"},
        })
    return {
        "id": PLUGIN_ID, "version": "1.0.0", "label": "需求",
        "facts": {need: _fact(need) for need in NEEDS},
        "rules": rules,
    }
