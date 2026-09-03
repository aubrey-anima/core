"""Core dataclasses for the anima_world event engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    seq: int
    ts: int
    type: str
    loc: str | None
    payload: dict[str, Any]
    who: str | None = None

    def to_row(self) -> tuple:
        """Convert to a tuple for SQLite insertion (seq omitted—AUTOINCREMENT)."""
        import json

        return (self.ts, self.type, self.who, self.loc, json.dumps(self.payload))

    @classmethod
    def from_row(cls, row: tuple) -> "Event":
        """Build Event from a SQLite row (seq, ts, type, who, loc, payload)."""
        import json

        seq, ts, type_, who, loc, payload_json = row
        return cls(seq=seq, ts=ts, type=type_, who=who, loc=loc, payload=json.loads(payload_json))


@dataclass
class AgentState:
    spec: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    location: str | None = None
    joined_at: int = 0
    updated_at: int = 0


@dataclass
class Relation:
    r_type: str = "acquaintance"
    r_type_back: str = "acquaintance"
    sentiment: float = 0.0
    # relations-v5: three finer axes riding the same delta machinery.
    # `sentiment` stays the headline (bands/edges/relabel key off it).
    trust: float = 0.0
    affection: float = 0.0
    respect: float = 0.0
    # 上一次改变它的那一条 —— 出处。
    #
    # 它**也是折出来的**,和 sentiment 同一条路、同一次重放:所以它不是缓存,
    # 没有"和事实对不上"的坏法。放在这儿而不是每次去翻日志,是因为翻日志要
    # 从头扫(事件表只有 ts / type 索引),而这一层是**每渲染一帧问一次**的。
    #
    # `None` 的意思是"这段关系没有一条说得出来的出处"(创世注入的好感度就是
    # 这样:那是作者的一句声明,不是这两个人之间发生过的事)。
    # 形状:{seq, tick, delta, direction, conversation_id, summary,
    #        as_name, target_name}
    last_change: dict[str, Any] | None = None


@dataclass
class Location:
    id: str = ""
    name: str = ""
    description: str = ""


@dataclass
class Capability:
    id: str = ""
    kind: str = ""
    description: str = ""
    params_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class Projection:
    agents: dict[str, AgentState] = field(default_factory=dict)
    relations: dict[tuple[str, str], Relation] = field(default_factory=dict)
    locations: dict[str, Location] = field(default_factory=dict)
    narrative_log: list[dict[str, Any]] = field(default_factory=list)
    """M2: accumulated narrative entries (agent, text, ts) — backward compat default empty."""
    capabilities: dict[str, Capability] = field(default_factory=dict)
    """M6: capability catalog, folded from capability_registered events."""
    balances: dict[str, float] = field(default_factory=dict)
    """economy-v4: folded from payment events. Audit = replay."""
    inventories: dict[str, dict[str, int]] = field(default_factory=dict)
    """economy-v4: holder → {item_id: qty}, folded from item_transfer/consume."""
    plugin_facts: dict[str, dict[str, float]] = field(default_factory=dict)
    """插件那些 `mode:"projected"` 的事实:`owner → {事实键: 值}`(3.8.0 第 2 期 2b)。

    **和 `balances` 逐字同一种东西**:量表里那个数只是物化视图,真相是日志里
    那一串 `<插件>.<事实>.delta`。搬成一个直接写的事实就丢掉了「可重放」——
    而「你为什么只剩三块钱」的唯一答案正是那一串事件,一个直接写的余额答不出
    这个问题,**而且它答不出来的时候不报错**。

    🔴 **折叠端只有一个处理器,不是每个插件一个**(`_apply_fact_delta`):
    事件类型按 `.delta` 结尾认,载荷里的 `owner` / `delta` 就是全部所需。
    每个插件一个的话,一个卸掉的插件留下的那串 delta 会在下一次重放时
    **无人认领**,而无人认领的样子是"这个数悄悄回到 0"。
    """
    fact_sources: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)
    """`事件类型 → (那几条 source 声明, …)`(3.8.0 第 2 期 ④)。

    🔴 **它是注册表,不是折出来的东西** —— 由装载那一层在重放之前塞进来
    (`__main__._install_plugins`),`reset_projection` 要把它**带过去**。
    带不过去的下场是安静的:重开那一刻钱包从日志里折出来的是 0,而跑着的世界照旧对。

    为什么必须有它:折叠端只认 `.delta` 后缀,而 `payment` 是**内核**事件 ——
    改名破坏消费方,两条都发是同一笔钱记两遍账。**只有"多一格声明"这条不破坏任何人。**
    """
    host_scenes: dict[str, dict[str, Any]] = field(default_factory=dict)
    """主持人此刻给每个玩家的**那一屏**,折自 `host_scene` 事件(3.9.0)。

    **每人只留最后一条**,所以它按玩家数封顶 —— 和 `SETTLED_INVITATIONS_KEPT` 同一种
    有界法。场景那段话是生成出来的、不可复现,所以它必须被存下来;而存的地方是
    **事件日志**,这一格只是它的投影。刷新一次页面就重生成一段的话,同一个时刻的
    世界会对同一个人说两种话,而两次都"对"。
    """
    stories: dict[str, dict[str, Any]] = field(default_factory=dict)
    """每个玩家的**故事状态**,折自 `director_log`(3.11.0,批 3a)。

    `{player_id: {"tension", "tension_tick", "phase", "threads": [...],
                  "recent": [tick, …], "intent": {...}, "moves": int}}`

    🔴 **零新键,折自事件** —— 判据是这个仓库自己写下的先例,逐字:
    「**邀请是事件,不是易失态。**它落 `agent_invites`,折进投影;不开新的
    Redis 键、不进 `volatile_keys` —— **存储契约一格不动**。于是它免费得到
    跨进程一致(`catch_up_projection`)和可重放。」
    连带的好处要说出来:`storage` 段一个字不动 → 运维台那条只比 `.storage` 的
    `deepStrictEqual` **不会红**,这一批不需要同轮认账。

    ⚠️ **`tension` 存的是「上一次那个值 + 那一刻的 tick」,不是此刻的值** ——
    此刻的值由 `director.tension_now` 算出来。存一个会随时间变旧的数,
    就多出一种和日志对不上的坏法(而这一层对不上的样子是"编剧以为他还紧张着,
    而他已经三天没上线了")。

    ⚠️ **有界**:`threads` 封顶 `STORY_THREADS_KEPT`、`recent` 只留最近一段 ——
    它进得了提示词,所以它必须有界(和 `settled_invitations` 同一把尺子)。
    """
    player_move_seq: dict[str, int] = field(default_factory=dict)
    """这个玩家**自己动手**的最后一条事件的 seq(3.11.0,批 3a)。

    它是主持人那把时刻钥匙的**第四格**,和 `player_beat_seq` 逐字同构 —— 那一格
    答「剧情安排的事发生了没有」,这一格答「他自己动没动过手」。

    🔴 **没有这一格,「每操作一次就有新剧情」结构性地做不到**:钥匙从前是
    `(place, day, beat_seq)`,一个在同一个地方、同一天里连点十次动词的玩家,
    十次都拿到 `scene.source == "cached"` —— 屏幕一动不动,而世界里真发生了十件事。

    哪几种事件算数由 `host.player_move_seq_of` 判(**一处判断**),
    而那张表和 `RECAP_EVENT_TYPES` **有意不合并**:一件事进不进回顾、算不算
    他的操作,是两个问题。
    """
    player_beat_seq: dict[str, int] = field(default_factory=dict)
    """指着这个玩家的最后一条 `beat_fired` 的 seq(3.9.0)。

    它是主持人那把**时刻钥匙**的第三格:「剧情拍响了」是四个开口时刻之一,而
    "响没响过"唯一可靠的读法就是日志里那条事件的序号。
    """
    packs: dict[str, dict[str, Any]] = field(default_factory=dict)
    """装进这个世界的内容包,折自 `pack_installed`(3.10.0)。

    `{pack_id: {"version", "note", "day", "tick", "seq", "sections": {段: [id, …]}}}`。

    🔴 **它是投影,不是账本。** 真相是那一串 `pack_installed` 事件 —— 和 `balances`
    逐字同一种东西。存一份直接写的"装了哪几周"就多出一种和日志对不上的坏法,
    而这一层对不上的样子是「这一周的拍从哪天起算」答错,**没有一处会报错**。

    ⚠️ **`day` 这一格是承重的**:一条 pack 装进来的拍,它 `trigger.at.day` 的零点
    就是这个数(`beats.day_zero_for`)。少了它,一份写着 `day: 0..6` 的第 2 周包
    装进一个跑到第 40 天的世界,**八拍在同一 tick 全部烧掉**,零报错。

    ⚠️ **同一个 pack 升级会重写这一行,而 `day` 不跟着动** —— 零点是**第一次**
    落地那天:一份第 2 周的剧情不该因为作者改了个错别字就整体往后推一周。
    """
    players_departed: set[str] = field(default_factory=set)
    """说过再见、世界不再等他的那些人,折自 `player_departed`(3.9.0 验收 A 逮的)。

    🔴 **它存在的理由是"一份名单只该有一个用途"。** `players_joined` 从前一个人兼两职:
    既是剧情拍的**零点**,又是"该为谁响"的**名单**;而 `_apply_player_departed`
    有意不清它(零点还要用,那是对的)—— 于是一个已经告别的人,三天后照样被他的
    per-player 拍击中:关系刚被折掉又重建、钱照付,而世界照跑、日志干净。
    两个用途拆成两份之后,那道减法写得出来也说得清:**该为谁响 = 来过 − 走了**。

    **他再回来算新入场**:`player_join` 会把他从这一格里划掉,并给他一个**新的零点**
    (「第一次赢」那条规矩管的是一段停留之内,不是一辈子)。
    """
    players_joined: dict[str, int] = field(default_factory=dict)
    """每个玩家**第一次**走进这个世界那一天(世界日),折自 `player_join` 事件。

    它是 `for_each: {"node": "player"}` 那种剧情拍的**零点**:一份"新手第一周"
    按世界时写只对第一个玩家成立,之后每一个人点进来看到的都是一条 39 天前就烧掉
    的拍。按这一格算,他的第 1 天就是他自己的第 1 天。

    🔴 **和 `allowances` 逐字同一条理由:这个"第一次"必须记在账本上,不记在在场上。**
    在场(`RedisPlayerPresence`)带 TTL —— 挂在那里的话,一个下线再上线的人就是又一次
    "第一次",他的第一周**每次登录重开一遍**。线上晚潮的 `dogfood-2e7fbb4` 因为同一个
    错**领了四次见面礼**,那笔账是这一格的病历。

    **第一次赢**:`player_departed` 不清它 —— 他确实来过,而告别不该让整份剧情重来。
    """
    allowances: set[str] = field(default_factory=set)
    """领过见面礼的 holder。**这是"只给一次"那个一次的家。**

    它必须挂在一件不会过期的事上,而在场(`RedisPlayerPresence`)有 TTL —— 挂在
    那里的话,"他这辈子头一回露面"会悄悄变成"他这一刻钟里头一回露面"。账本是
    投影、由全量重放折出来,所以这一格重启、换进程、TTL 到头都还在。

    **和 `balances` 同生共死**:`player_forget` 不清余额,所以也不清这一格 ——
    清了的话一个被忘掉又回来的人会带着旧钱再领一次新钱。
    """
    invitations: dict[int, dict[str, Any]] = field(default_factory=dict)
    """还等着人回答的邀请:**那条 `agent_invites` 的 `seq` → payload**。

    `agent_invites` 加进来,`invitation_settled` 拿出去 —— **它是从账本折出来的,
    不是第二张真相表**。这一条是有意的:一份"谁在等你"的清单如果自己存一份状态,
    那么每个进程手里都有一份可能不一样的答案(线上世界壳、维护容器、tick 进程
    各一个),而分叉的那一天没有任何一处会报错。折出来的话,`catch_up_projection`
    免费把它们对齐,重放也必然得到同一份清单。

    **它不用另开一个 Redis 键**,所以这一层不动存储契约:邀请是事件,不是易失态。
    """
    settled_invitations: dict[int, str] = field(default_factory=dict)
    """已经有了结局的邀请:`seq` → `outcome`。**只留最近这一小段。**

    他按下"好"的时候那份邀请可能已经不在清单上了,而"已经不在了"有四种意思
    (他自己答过了 / 他说了不去 / 他没来得及 / **她把话收回去了**)。不记这一格
    的话,答复那扇门只能回一句"要么答过了、要么已经过期" —— 一句**把真正的原因
    排除在外**的话:她走开撤回的那一支,恰恰是这四种里唯一不是他的责任的那种。

    **有界**(`SETTLED_INVITATIONS_KEPT`):它进得了他手机上那句话,所以它必须有界
    (「进得了提示词的 → 必须有界」同一条尺子)。掉出这一段的老邀请回落成那句
    笼统的话 —— 说不出来就别猜,而不是让这张表随世界一起长。
    """
