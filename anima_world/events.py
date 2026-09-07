"""事件模块:`Event` 类型的老家(re-export)+ 事件日志的接口占位。

SQLite 版 `EventLog` 已随 world.db 层整体退役。真实现在别处:

- `anima_world.redis_state.RedisEventLog`(默认)
- `anima_world.mysql_state.MySQLEventLog`(给了 `mysql=` 的世界)

两者接口逐字相同:`append` / `replay` / `count` / `max_seq` / `page`。
这里留下的 `EventLog` 只是那份接口的占位(scheduler / __main__ 用它做类型注解),
不可实例化 —— 构造即报错,免得有人拿着一个空壳日志静默地丢事件。
"""

from __future__ import annotations

from anima_world.types import Event

__all__ = ["Event", "EventLog", "EVENT_PAYLOAD_KEYS",
           "PLAYER_FACING_EVENTS", "PLAYER_FACING_PRIVATE_KEYS",
           "SUBSCRIBABLE_EVENTS"]


class EventLog:
    """事件日志的接口占位(仅供类型注解)。

    SQLite 实现已退役;要一个能用的日志,用 `RedisEventLog` 或 `MySQLEventLog`。
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "SQLite 版 EventLog 已退役:请用 anima_world.redis_state.RedisEventLog "
            "或 anima_world.mysql_state.MySQLEventLog"
        )


# ── 插件订得到的那张表(3.8.0,`docs/设计-插件系统.md` §2 / §6)────────────────
#
# **这是一张策展表,不是全集。** 引擎里在发的 type 有四十来个(拿 `ast` 数过:
# 每一处 `{"type": "…"}` 字面量),而其中一半是**内部管道**:`subsystem_health`
# 是子系统档位切换、`memory_seed` 是记忆层自己的种子、`plan` 是规划器的回执、
# `legacy_seq_gap` 是 1.x 迁移留下的补丁 —— 它们的载荷形状为引擎自己的用途服务,
# 明天就可能因为一次内部重构而变。
#
# 🔴 **进了这张表就是一句公开契约,拿不掉。** 所以宁少勿多:今天十条,
# 加一条是加法(便宜),删一条是破坏消费方(和改线格式同级)。一个不在表上的事件
# **照旧在发**,只是插件订不到它 —— 需要它的那天,由一次显式的加法把它放进来。
#
# 每一条报两样,而这两样是**问出来的两个不同的问题**:
#
# - `numbers` —— **数字格**:载荷里哪几格是数,插件的规律/触发器拿它做算术
#   (`event.amount`、`event.changed`)。这一格是空列表的事件**不是漏了**,
#   是"这类事情本身不带数"(一个人走进这个世界,没有一个数可读)。
# - `parties` —— **当事人格**:载荷里哪几格装着"这件事还牵涉到谁"(对方、目标、
#   收款人)。它是**给读的人和作者看的**,不是触发器的取人依据。
#   🔴 **2026-08-27 更正:这一段此前写着「它决定触发器的 `for_each` 能不能对得上
#   人」,而那句话是假的** —— `for_each` 一个字都不读这张表。真正取人的是
#   `Scheduler._trigger_bearer`,而它读的是**事件顶层**的 `who`(`agent`,经
#   `stock_owner_of`)、顶层的 `loc`(`location`)、载荷里的 `target` / `entity`
#   (`entity:<kind>`);`world` 是常量。**这件事在契约里有一格**,叫
#   `plugins.trigger_bearer_keys` —— 一格「从哪儿取」比一段「它决定……」值钱得多,
#   后者要人读、要人记,而它正好被记错了一轮。
#   ⚠️ 事件顶层还有一个 `who`(做这件事的那个人,玩家写成 `player:<id>`)与
#   `loc`,所以不在这张表里逐条重复。**「每条都有」这句话量过六种**(2026-08-27
#   拿橱窗跑 300 tick:`agent_join` / `entity_interaction` / `item_transfer` /
#   `payment` / `state_change` / `travel` 各若干条,顶层 `who` 全都非空);
#   另外四种那一趟没跑到,**别把这句话当成量过的十种**。
#
# ⚠️ **关系四轴不在这张表上,而这是老板 2026-08-26 拍的(D40 ③)**:插件**读得到、
# emit 得出,写不进**内置四轴 —— 它们是 `sentiment_delta` 事件的**投影**,不是一张
# 可以直接写的表,直写就等于把关系从"可重放"变成"直接写"。所以四轴的变化只以
# `state_change{kind: "sentiment_delta"}` 这**一种事件形式**进来。
#
# ⚠️ **`location_join` 这个名字底下有两件事,别订错**:顶层那条 `location_join`
# 是**创世时播下的一个地点**(配置,不是发生的事),所以它不在这张表上;
# "有人走进了一个地方"是 `state_change{kind: "location_join"}`。
#: **玩家面的那几种事件** —— 它们的载荷是**写给某一个玩家的**(3.13.0,批 3c §2.2)。
#:
#: 🔴 它们进插件触发器时要过两道:**剥掉 `why`**(那是编剧写给创作者的 GM 笔记,
#: 不该出门),而且**由它触发的效果只许作用于当事人** —— 拿别人的剧情去动别人的
#: 世界,是这一层最难查的越权:边连上了、`plugin list` 看不出来,而两个玩家的
#: 线从此互相污染。
#: ⚠️ 实测过那个口子是真的(不是理论):一条订 `director_log` 的触发器把
#: `link` 的 `to` 写死成 `player:p2`,边真的连上了。
PLAYER_FACING_EVENTS = ("director_log", "confront_settled", "reward_settled",
                        "callback_settled")

#: 玩家面事件里**不许出门**的那几格(进触发器时剥掉)。
#: `why` 是编剧写给创作者的一句话 —— 给别的玩家的插件看,就是把 GM 的笔记摊开。
PLAYER_FACING_PRIVATE_KEYS = ("why",)

#: **每种事件的载荷键表** —— `{事件类型: (键, …)}`(3.12.0,platform 带回)。
#:
#: 🔴 **它存在的理由是一个手抄表漏了两次。** 壳那侧的送达门有一份手抄的
#: `_KNOWN_PAYLOAD_KEYS`,而 `line` 和 `source` 两次漏发都出在那儿:
#: 引擎加了一格,手抄那份没跟上,**于是那一格永远送不到消费方,而两边都不报错**。
#: 一份手抄的键表和一句没人验的话是同一种东西。
#:
#: **判据是包含,不是相等**(`tests/test_event_payload_keys.py`):引擎**真发**
#: 的每一个键都必须在这张表里。反过来不判 —— 有几种事件的载荷是**按情况带格**
#: 的(`state_change` 按 `kind` 分支、一起做事时才有 `party`),
#: 要求逐格相等会让这道闸在一次完全正常的世界里红。
#:
#: ⚠️ **这张表比 `SUBSCRIBABLE_EVENTS` 宽**:那一张是**插件触发器**能订哪几种
#: 内核事件(≤12 条的闭集),这一张是**消费方读得到什么**。两件事,别合并。
EVENT_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "agent_join": ("location", "spec", "state"),
    "agent_hail": ("agent_id", "agent_name", "player_id", "player_name",
                   "location", "location_name", "player_present", "reason",
                   "source", "line", "opening", "note"),
    "conversation": ("agent_id", "closed_at", "conversation_id", "location",
                     "message_count", "participants", "started_at", "summary"),
    "director_log": ("player_id", "move", "tick", "place", "source", "target",
                     "target_name", "line", "why", "thread_id", "promise",
                     "phase", "due_tick", "closes_thread", "tension_before",
                     "tension_after", "tension_text", "stake", "ops_applied",
                     "capped", "refused_by", "pin_until", "outcome", "roll"),
    "entity_destroy": ("target", "kind", "reason", "by"),
    "entity_interaction": ("target", "target_name", "verb", "verb_label",
                           "changed", "consumed", "me_changed", "me_delta",
                           "spent", "party", "joint_role"),
    "entity_spawn": ("target", "kind", "name", "location", "from"),
    "item_consume": ("agent_id", "item_id", "qty", "reason", "location"),
    "item_transfer": ("from", "to", "item_id", "item_name", "qty", "note",
                      "reason"),
    "payment": ("from", "to", "amount", "reason"),
    "roll": ("actor", "verb", "player_id", "faces", "result", "band", "seed"),
    # ⚠️ `state_change` 是**按 `kind` 分支**的一族(到站 / 关系变了 / 人设改写
    # …),所以这几格里多数是"某几种 kind 才有"。这正是这张表判**包含**
    # 而不判相等的理由。
    "state_change": ("kind", "target", "target_name", "as", "as_name", "state",
                     "spec", "sentiment", "delta", "axes",
                     "r_type", "r_type_back",
                     "conversation_id", "conversation_summary"),
    "travel": ("player_id", "from", "to", "minutes", "arrive_at"),
    # 结算那三条(玩家面)—— 三种共用一张表,见 `director.SETTLE_KEYS`。
    "confront_settled": ("player_id", "move", "thread_id", "thread", "with",
                         "with_name", "stake", "stake_text", "outcome",
                         "outcome_text", "tick"),
    "reward_settled": ("player_id", "move", "thread_id", "thread", "with",
                       "with_name", "stake", "stake_text", "outcome",
                       "outcome_text", "tick"),
    "callback_settled": ("player_id", "move", "thread_id", "thread", "with",
                         "with_name", "stake", "stake_text", "outcome",
                         "outcome_text", "tick"),
}

def _director_log_note() -> str:
    """`director_log` 那一句说明 —— **格数与闭集都从权威表数出来**。

    ⚠️ 延迟到调用时 `import`:`director` 反过来不 import 这里,
    但保持单向依赖比省一次 import 值钱。
    """
    from anima_world.director import DIRECTOR_LOG_KEYS, MOVES, SOURCES

    return (
        f"⚠️ **载荷是一张固定的表,缺的写空不省略**"
        f"(`director.DIRECTOR_LOG_KEYS`,{len(DIRECTOR_LOG_KEYS)} 格):"
        f"`move` 是闭集({len(MOVES)} 个动作 + 引擎自己收账的 `collect`),"
        f"`source` 分得出 {' / '.join('`%s`' % s for s in SOURCES)}。"
        "🔴 **`why` 是写给创作者与运维的 GM 笔记,别上玩家屏**;"
        "`roll` 只有 `confront` 那一支非空"
    )


SUBSCRIBABLE_EVENTS: dict[str, dict[str, object]] = {
    # 🆕 3.12.0(批 3b,裁决 §2.7):**编剧写的那一拍。**
    #
    # 🔴 **3a 有意没进,而进来的条件是写死的三条,今天三条都满足了**:
    #   ① 3b 三个动作(`confront`/`reward`/`callback`)落地,**这张载荷表定稿**
    #      (`director.DIRECTOR_LOG_KEYS`,两条发射点共用);
    #   ② **真有消费方要订它** —— 运维台的编剧日志视图(「以后可能有人要」不算);
    #   ③ 那张 ≤12 的闸转完是 11 条,**给后来者留最后一格**,所以
    #      **同一批里不许再往这张表加第二条**。
    #
    # ⚠️ **进了就是一句拿不掉的公开契约** —— 删一条是破坏消费方,和改线格式同级。
    "director_log": {
        "gloss": "实时编剧写了一拍(或者一条线到期被结算)—— **按 `payload.move` 二级分发**",
        "numbers": ["tick", "tension_before", "tension_after", "due_tick", "pin_until"],
        "parties": ["player_id", "target"],
        # 🔴 **这一句从表生成,不手抄**(3.12.1,验收 B+C ③)。
        # 手抄那一版把两处写错了:格数写「23」而实际 24,`source` 里列了一个
        # **不存在的 `engine`**(闭集是那八个)。
        # **一份手抄的说明和一句没人验的话是同一种东西** —— 而这一段的读者
        # 正是照它写解析的三个仓库。
        "note": _director_log_note(),
    },
    "conversation": {
        "gloss": "一场对话结束了(整场只发这一条,在关闭时)",
        "numbers": ["message_count", "started_at", "closed_at"],
        "parties": ["agent_id", "participants"],
        "note": "⚠️ `started_at` / `closed_at` 是**墙钟秒**,不是 tick"
                "(转录那一层按秒记账)。拿它做 tick 算术会把「第几天」算成六百多万",
    },
    "state_change": {
        "gloss": "世界的状态变了 —— **按 `payload.kind` 二级分发**",
        "numbers": ["delta", "axes"],
        "parties": ["as", "target"],
        "note": "订它必须连 `kind` 一起判(`sentiment_delta` 关系变了 / "
                "`location_join` 有人走进一个地方 / `agent_state` 她在干什么 / "
                "`r_type` 关系的名分变了 / `persona_update` 人设被改写)。"
                "**内置关系四轴只以这一种形式进来**(D40 ③:插件读得到、"
                "emit 得出,写不进 —— 四轴是这条事件的投影,直写等于把关系从"
                "「可重放」变成「直接写」)",
    },
    "entity_interaction": {
        "gloss": "有人对一样东西用了一个动词,而且做成了",
        "numbers": ["changed", "me_changed", "me_delta", "consumed"],
        "parties": ["target"],
        "note": "`changed` 是**目标身上的量现在是多少**,`me_delta` 是"
                "**这一次让她身上的量变了多少**(带符号)—— 两栏答的是两个问题。"
                "⚠️ 一起做事时**只有发起人那条带 `changed`**,别的参与者那条是空的:"
                "每条都带的话,按事件重算「这棵树长了多少」会得到人数倍",
    },
    "entity_spawn": {
        "gloss": "世界里长出了一样新东西",
        "numbers": ["values"],
        "parties": ["entity", "from"],
        "note": "`kind` 是它的种类,`location` 是它落在哪儿,`from` 是它从哪个"
                "东西上长出来的。id 由引擎发且**只增不减**",
    },
    "entity_destroy": {
        "gloss": "一样东西没了(`destroys_target`)",
        "numbers": [],
        "parties": ["entity"],
        "note": "抹掉时实例 / 量 / 位置 / 挂在它身上的长过程**四样一起走**",
    },
    "item_transfer": {
        "gloss": "一样东西换了主人",
        "numbers": ["qty"],
        "parties": ["from", "to"],
        "note": "库存是它的投影。`from_name` / `item_name` 是**那一刻的人话**,"
                "老事件缺这两格,读的一方要回落",
    },
    "item_consume": {
        "gloss": "一样东西被用掉了(`consumes`,或者吃了一顿饭)",
        "numbers": ["qty"],
        "parties": ["who"],
        "note": "`source` 说它是被什么用掉的(`<动词>:<目标>` 或 `shop:<地点>`)。"
                "⚠️ `qty` 只有能力那条路带,吃饭那条没有 —— 缺席读作 1",
    },
    "payment": {
        "gloss": "一笔钱",
        "numbers": ["amount"],
        "parties": ["from", "to"],
        "note": "**经济账本的唯一真相**,余额是它的投影 —— 没有 `balances` 表,"
                "对账即重放",
    },
    "travel": {
        "gloss": "有人出发去别的地方(角色与玩家共用这一条)",
        "numbers": ["minutes", "arrive_at"],
        "parties": ["player_id"],
        "note": "`arrive_at` 是**到达的那个 tick**,不是时长。在途的人"
                "**不在任何地方** —— 「他现在在哪」在这两个 tick 之间是没有答案的。"
                "⚠️ **`player_id` 只有玩家那条路带**(`World.player_walk`);角色"
                "出发那条(`Scheduler._start_journey`)没有这一格 —— "
                "**缺席 = 出发的是角色**,而她是谁在事件顶层的 `who` 上"
                "(和 `item_consume.qty` 同一种读法)。"
                "🔴 **别照这一格写触发器**:`for_each` 取人取的是顶层 `who`,"
                "**角色与玩家两半都对得上**(2026-08-27 实测:角色出发与玩家出发"
                "各让那个事实 +1)。这一格说的是「这条事件里还写着谁」,"
                "不是「这条事件落在谁头上」",
    },
    "agent_join": {
        "gloss": "一个角色进了这个世界(创世的那批 `ts=0`,后来的是节拍或宿主加的)",
        "numbers": [],
        "parties": [],
        "note": "**这一条没有当事人格**:是谁在事件顶层的 `who` 上。"
                "`spec` 里有 `name` / `personality` / `goals` 与可选的 `card`",
    },
}
