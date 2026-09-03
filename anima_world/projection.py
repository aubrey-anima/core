"""Fold an event stream into an in-memory Projection."""

from __future__ import annotations

from typing import Any

from anima_world import host as host_mod
from anima_world.types import AgentState, Capability, Event, Projection, Relation

#: 投影式插件事实的事件后缀:`<插件>.<事实>.delta`(3.8.0 第 2 期 2b)。
#:
#: 🔴 **折叠端认后缀,不认插件名 —— 只有一个处理器。** 每个插件各注册一个的话,
#: 一个**卸掉的**插件留下的那串 delta 在下一次重放时无人认领,而无人认领的样子是
#: "这个数悄悄回到 0";更糟的是它只在重放那一刻发生,跑着的世界看不出来。
FACT_DELTA_SUFFIX = ".delta"

SETTLED_INVITATIONS_KEPT = 200
"""结局记到第几份为止(`Projection.settled_invitations` 的上界)。

**它进得了他手机上那句话,所以它必须有界。** 200 份是"他这会儿还可能去按一下"的
那一小段:一份邀请只活 12 个 tick,超出这一段的必然早就不在任何一块屏幕上了。"""


def project_events(events: list[Event], base: Projection | None = None) -> Projection:
    """Fold events into a Projection, optionally on top of a base projection.

    M1 supports 4 event types: agent_join, agent_move, state_change, location_join.
    Unknown event types are silently ignored (projection is lossy by design;
    full history lives in the events table).
    """
    proj = base if base is not None else Projection()
    for e in events:
        _apply_event(proj, e)
    return proj


def _apply_event(proj: Projection, e: Event) -> None:
    if e.type == "agent_join":
        _apply_agent_join(proj, e)
    elif e.type == "agent_move":
        _apply_agent_move(proj, e)
    elif e.type == "state_change":
        _apply_state_change(proj, e)
    elif e.type == "location_join":
        _apply_location_join(proj, e)
    elif e.type == "agent_action":
        _apply_agent_action(proj, e)
    elif e.type == "agent_idle":
        _apply_agent_idle(proj, e)
    elif e.type == "narrative":
        _apply_narrative(proj, e)
    elif e.type == "user_message":
        _apply_user_message(proj, e)
    elif e.type == "capability_registered":
        _apply_capability_registered(proj, e)
    elif e.type == "agent_leave":
        _apply_agent_leave(proj, e)
    elif e.type == "agent_return":
        _apply_agent_return(proj, e)
    elif e.type == "payment":
        _apply_payment(proj, e)
    elif e.type == "item_transfer":
        _apply_item_transfer(proj, e)
    elif e.type == "item_consume":
        _apply_item_consume(proj, e)
    elif e.type == "player_join":
        _apply_player_join(proj, e)
    elif e.type == "host_scene":
        _apply_host_scene(proj, e)
    elif e.type == "beat_fired":
        _apply_beat_fired(proj, e)
    elif e.type == "player_departed":
        _apply_player_departed(proj, e)
    elif e.type == "pack_installed":
        _apply_pack_installed(proj, e)
    elif e.type == "pack_disabled":
        _apply_pack_disabled(proj, e)
    elif e.type == "pack_enabled":
        _apply_pack_enabled(proj, e)
    elif e.type == "director_log":
        _apply_director_log(proj, e)
    elif e.type == "agent_invites":
        _apply_agent_invites(proj, e)
    elif e.type == "invitation_settled":
        _apply_invitation_settled(proj, e)
    elif e.type.endswith(FACT_DELTA_SUFFIX):
        _apply_fact_delta(proj, e)
    # 🆕 3.11.0(批 3a):**不是 elif** —— 同一条事件可能既被上面某个分支折
    # (`state_change` 折关系、`payment` 折余额),又算「这个玩家动了一次手」。
    # 挂进上面那条 if/elif 链的任何一格都会漏掉另外几格,而漏了不报错:
    # 屏幕安静地不再更新。
    if e.type in host_mod.PLAYER_MOVE_EVENT_TYPES:
        _apply_player_move(proj, e)
    if proj.fact_sources and e.type in proj.fact_sources:
        # ⚠️ **不是 elif**:一条内核事件可能既被别的分支折(`payment` 折 `balances`),
        # 又被某个插件认成自己的 delta。两件事互不相干,都要做。
        _apply_fact_source(proj, e)


# ── 谁在等你回答 ────────────────────────────────────────────────────────────


def _apply_agent_invites(proj: Projection, e: Event) -> None:
    """她开口叫了他一起做一件事 —— 这份邀请从此挂着,等一个答复。

    **挂在投影里而不是另存一份状态**:一份"谁在等你"的清单要是自己有一张表,
    每个进程手里就各有一份可能不一样的答案(世界壳一个、维护容器一个、tick
    进程一个),而分叉那天没有一处会报错。折出来的清单靠 `catch_up_projection`
    免费对齐,重放也必然得到同一份。

    **它的 id 就是这条事件的 `seq`**,不另发一个号。另发一个的话世界里就多了
    一个 id 命名空间(要保证唯一、要进 `.cyberworld`、要跨进程不撞),换来的是
    一个和 seq 一一对应的数字 —— 而 `seq` 本来就是账本发的、天然唯一、翻页
    (`_filtered_page`)用的也是它。

    **`loc` 从事件的顶层抄一格进来**:她是**在哪儿**开的口。答复那扇门要靠它才
    分得出"她走开了"和"你走开了" —— 只知道两个人此刻各在哪的话,一句"你们不在
    一处"说不出是谁动的,而那两件事在他手机上是两句完全不同的话。抄一格而不是
    去翻日志:那条事件已经在手上了,翻回去要 O(整条日志)。
    """
    seq = int(e.seq or 0)
    if seq <= 0:
        return
    row = dict(e.payload)
    row["seq"] = seq
    row["loc"] = e.loc
    proj.invitations[seq] = row


def _apply_invitation_settled(proj: Projection, e: Event) -> None:
    """这份邀请有了结局(答应 / 拒绝 / 过期 / 撤回)—— 它不再等人了。

    **四种结局都只是把它从"还等着"里拿掉**,结局本身留在日志里。所以"他拒绝了"
    和"他没看见"这两件事永远分得开:清单上都不在了,而账本上写着不同的字。

    **顺手把结局本身留一小段**(`settled_invitations`)。他手指按下去和世界收到
    这一下之间,那份邀请可能刚刚有了结局 —— 那扇门此刻只知道"它不在清单上了",
    于是只能回一句"要么答过了、要么已经过期",而**她把话收回去**这一种恰好被
    这句话排除在外。留一格 `outcome` 就够说清楚,而且不必回头翻日志(按 kind
    翻是 O(整条日志),在他按一下的那条路上不能这么花)。
    """
    try:
        invite_seq = int(e.payload.get("invite_seq") or 0)
    except (TypeError, ValueError):
        return
    proj.invitations.pop(invite_seq, None)
    outcome = str(e.payload.get("outcome") or "")
    if not outcome:
        return
    # 重放会把同一条再折一遍,所以先删后插:dict 保插入序,重排到队尾才是"最近"。
    proj.settled_invitations.pop(invite_seq, None)
    proj.settled_invitations[invite_seq] = outcome
    while len(proj.settled_invitations) > SETTLED_INVITATIONS_KEPT:
        proj.settled_invitations.pop(next(iter(proj.settled_invitations)))


def _apply_player_departed(proj: Projection, e: Event) -> None:
    """「这个人离开了这个世界」—— 折掉他和所有人之间的关系,两个方向都折。

    **为什么这是一条事件而不是一次删除。** 关系不是一张表,是
    `state_change/sentiment_delta` 折出来的投影。手删投影里那一行,下一次重放
    (换个进程、重启一次、`catch_up_projection` 一次)会原样把它折回来 —— 世界
    照跑、日志干净,而"她还惦记着一个不存在的人"这件事一天之内自己长回来。
    追加一条事实,重放才收敛;**对账即重放**这条不变量因此仍然成立。

    **它不碰历史。** 那些 `sentiment_delta` 一条不少地留在日志里,记忆、转录、
    账本也一样 —— 她记得这个人来过,只是不再等他。走的是**朝前看**的那一半。
    """
    player_id = str(e.payload.get("player_id") or "").strip()
    if not player_id:
        return
    # 🆕 3.9.0:朝前看的那一半还多一格 —— **剧情拍从此不再指着他**。
    # `players_joined` 有意不动(那是零点,是历史);"该为谁响"是另一份名单,
    # 而它们从前是同一份 —— 一个已经告别的人三天后照样被他的 per-player 拍击中,
    # 关系刚折掉又重建、钱照付,零报错。
    proj.players_departed.add(player_id)
    for key in [k for k in proj.relations if player_id in k]:
        proj.relations.pop(key, None)
    # 🆕 3.8.0 第 2 期 2b:他身上那些**投影式**插件事实也一起折掉。
    #
    # ⚠️ **按整个 owner 比,不按子串** —— `aubrey` 是 `aubrey-player` 的子串,
    # 而两个人的名字长得像是常态(`RedisEdgeStore.touching` 那条同款教训)。
    # ⚠️ **历史一个字没删**:那几条 delta 原样躺在日志里,走的是朝前看的那一半。
    # 直接去量表里删那一行是没用的:下一次重放会原样把它折回来,世界照跑、
    # 日志干净,而"他的钱包"一天之内自己长回来。
    for owner in (f"agent:player:{player_id}", f"player:{player_id}", player_id):
        proj.plugin_facts.pop(owner, None)


# ── economy-v4: the ledger is a projection — audit = replay ────────────────


def _apply_payment(proj: Projection, e: Event) -> None:
    """一笔钱从一头挪到另一头。**进位在这儿,不在显示那一层。**

    二进制浮点存不下 0.1,所以 `60 − 5.23 − 1.16 − …` 折下来是
    `0.3799999999999921`。前端 `toFixed(2)` 把它盖住了,而账本是这个引擎对"钱"
    的定义、不是屏幕上那一行:门禁读的是这个数,一笔"正好够"的交易迟早会被它
    拒掉,而那一次不报错也不留痕。钱有最小单位,所以折账的时候就进位。

    **两头一起进** —— 单边进的话玩家少的和镇上多的对不上,账本不再守恒。
    """
    payload = e.payload
    src, dst = payload.get("from"), payload.get("to")
    try:
        amount = float(payload.get("amount", 0.0))
    except (TypeError, ValueError):
        return
    if not src or not dst or amount <= 0:
        return
    proj.balances[src] = round(proj.balances.get(src, 0.0) - amount, 2)
    proj.balances[dst] = round(proj.balances.get(dst, 0.0) + amount, 2)
    # 见面礼发过没有,记在账本这一侧 —— 理由见 `Projection.allowances`。
    if payload.get("reason") == "allowance":
        proj.allowances.add(str(dst))


def _apply_item_transfer(proj: Projection, e: Event) -> None:
    payload = e.payload
    src, dst, item_id = payload.get("from"), payload.get("to"), payload.get("item_id")
    try:
        qty = int(payload.get("qty", 1))
    except (TypeError, ValueError):
        return
    if not item_id or qty <= 0 or not dst:
        return
    if src:
        holding = proj.inventories.setdefault(src, {})
        holding[item_id] = holding.get(item_id, 0) - qty
        if holding[item_id] <= 0:
            holding.pop(item_id, None)
    receiving = proj.inventories.setdefault(dst, {})
    receiving[item_id] = receiving.get(item_id, 0) + qty


def _apply_item_consume(proj: Projection, e: Event) -> None:
    payload = e.payload
    who, item_id = payload.get("who") or e.who, payload.get("item_id")
    if not who or not item_id:
        return
    try:
        qty = int(payload.get("qty", 1))
    except (TypeError, ValueError):
        qty = 1
    if qty <= 0:
        return
    holding = proj.inventories.get(who)
    if holding and item_id in holding:  # 直接吃货架上的餐不经过随身库存,no-op
        holding[item_id] -= qty
        if holding[item_id] <= 0:
            holding.pop(item_id, None)


def _doing(state: Any) -> dict[str, Any]:
    """`state` 装的是她**在干什么**,不装她**在哪**。

    "在哪"只有一个家:`location_join` 折出来的 `agent.location`。而 `state` 是个
    自由字典,谁都能往里塞一个 `location` —— 塞进去之后它只增不减(`work` 写,
    `sleep` 和 `walk` 都不清),于是 `World.state()` 对同一个问题给出两个答案,
    宿主读哪个全凭运气。线上量出来是 21 个人里 13 个对不上。

    **过滤要留在读事件这一侧**,光修 `actions.py` 不够:投影是重放事件折出来的,
    历史里每一条老 `work` 事件都会把那份陈旧的拷贝原样带回来。
    """
    if not isinstance(state, dict):
        return {}
    # 拷贝之后再删 —— 事件那个 dict 是**已经发生过的事实**,就地改它等于改写历史
    # 的显示(见 `_apply_agent_join` 里 spec 那段的教训)。
    return {k: v for k, v in state.items() if k != "location"}


def _apply_host_scene(proj: Projection, e: Event) -> None:
    """主持人开过的口。**每人只留最后一条** —— 这一格答的是"此刻那一屏是什么",
    而"他这一路听过哪些开场"是日志的活,不是投影的。"""
    payload = e.payload or {}
    player_id = str(payload.get("player_id") or "")
    if not player_id:
        return
    proj.host_scenes[player_id] = {**payload, "seq": int(e.seq or 0)}


def _apply_player_move(proj: Projection, e: Event) -> None:
    """他自己动手的最后一条 —— 主持人时刻钥匙的第四格(3.11.0,批 3a)。

    **谁算数由 `host.player_move_seq_of` 判,这里一个字都不另写。** 那张表里
    有两条的 `who` 是**她**不是他(`conversation` / `invitation_settled`),
    照 `who` 筛的话它们永远筛不出来,而下场是「聊完一轮屏幕不动」、零报错。

    ⚠️ **一条事件可能同时属于好几个玩家**(一场会话只有一个 user,但将来
    未必),所以这里逐个玩家问,而不是从事件里反解出"他是谁"。名单取
    `players_joined` —— 一个从没进过这个世界的人不会有操作。
    """
    payload = e.payload or {}
    who = str(e.who or "")
    seq = int(e.seq or 0)
    for player_id in proj.players_joined:
        if host_mod.player_move_seq_of(e.type, payload, who, f"player:{player_id}"):
            proj.player_move_seq[player_id] = seq


def _apply_beat_fired(proj: Projection, e: Event) -> None:
    """指着某个玩家的那条拍响过了 —— 记下 seq,主持人的时刻钥匙要读它。

    **只认带 `for` 的那些**:世界级的拍不是"对他发生的事",拿它去叫醒每个人的
    主持人,一条公共剧情就会让所有人的屏幕同时重开。
    """
    subject = str((e.payload or {}).get("for") or "")
    if not subject.startswith("player:"):
        return
    proj.player_beat_seq[subject[len("player:"):]] = int(e.seq or 0)


def _apply_player_join(proj: Projection, e: Event) -> None:
    """他走进这个世界是哪一天。**一段停留之内第一次赢**(`setdefault`)——
    同一个人再露一次面不该把零点往后推,那正是"第一周每次登录重开一遍"那个错的形状。

    **但告别之后再回来,是新的一段停留**:把他从 `players_departed` 划掉,并给他一个
    **新的零点**。「第一次赢」管的是一段停留之内,不是一辈子 —— 一个走了三个月又回来
    的人,他的"第一周"该从他回来那天算,而不是从三个月前那个已经作废的零点算。
    """
    payload = e.payload or {}
    player_id = str(payload.get("player_id") or "")
    if not player_id:
        return
    try:
        day = int(payload.get("day", 0))
    except (TypeError, ValueError):
        day = 0
    if player_id in proj.players_departed:
        proj.players_departed.discard(player_id)
        proj.players_joined[player_id] = day        # 新的一段停留,新的零点
        return
    proj.players_joined.setdefault(player_id, day)


#: `wrote` 里**第一次写下的那个值才算数**的那几格。
#:
#: 🔴 **`config_before` 记的是「装这个包**之前**那一格是什么」**,所以它是
#: 一个**历史事实**,不是"我最近写了什么" —— 而这两种语义在同一个 `wrote`
#: 字典里并排住着,合并规则正相反(2026-09-02 验收 A ④)。
#:
#: 病是这样的:装前 = 3,v1 写 5(`config_before[k] = 3`),v2 写 7
#: (`config_before[k] = 5`)。`update()` 让**新的赢**,于是 `disable` 之后
#: 回落到 **5**,而屏幕上还印着「回落到装包前那个值」——
#: 装包前是 3。**一句写错的回执,和一次写错的回滚一样贵。**
_WROTE_FIRST_WINS = ("config_before",)


def _merge_wrote(have: Any, incoming: Any) -> dict[str, dict[str, str]]:
    """`{格: {谁: 我写下去的那个值}}` —— 按格并入。

    **新的赢**,除了 `_WROTE_FIRST_WINS` 那几格 —— 它们记的是"装之前那一格是
    什么",是历史事实,**第一次写下的才算数**。
    """
    out: dict[str, dict[str, str]] = {
        str(k): dict(v) for k, v in (have or {}).items() if isinstance(v, dict)
    }
    for field, rows in (incoming or {}).items():
        if not isinstance(rows, dict):
            continue
        bucket = out.setdefault(str(field), {})
        for k, v in rows.items():
            if str(field) in _WROTE_FIRST_WINS:
                bucket.setdefault(str(k), str(v))   # 装包前那个值,只记第一次
            else:
                bucket[str(k)] = str(v)             # CAS 那几格:我上次写的才算
    return out


def _apply_pack_installed(proj: Projection, e: Event) -> None:
    """一份内容包落地了(3.10.0)。

    🔴 **`day` 只在第一次落地时定,升级不动它** —— 零点是"这一周的内容是什么时候
    进这个世界的",而不是"作者最后一次改它是什么时候"。让它跟着升级走的话,
    一次错别字修订会把整份第 2 周剧情往后推一周,而**没有一处会报错**:那几拍
    只是"还没到"。
    """
    payload = e.payload or {}
    pack_id = str(payload.get("pack_id") or "")
    if not pack_id:
        return
    try:
        day = int(payload.get("day", 0))
    except (TypeError, ValueError):
        day = 0
    have = proj.packs.get(pack_id)
    # 🔴 **按段并入,不是整片替换**(2a-① 验收 A 逮的)。
    #
    # 从前这里是 `dict(payload["sections"])` —— 于是第 40 天装 v1.0.0(社团 day5 /
    # 夜宵 day6)、第 41 天装 v1.1.0 带**别的**拍,`sections["beats"]` 整片换成新的
    # 那几条,`beats.pack_days_from` 里就**再也没有社团和夜宵** ——
    # `day_zero_for(社团)` 于是读作 0,下一 tick 两拍一起烧掉。
    # 而这一族最阴的是:那条升级用例装的两份都不带 `beats`,所以它看不见。
    sections: dict[str, Any] = dict((have or {}).get("sections") or {})
    for name, ids in (payload.get("sections") or {}).items():
        merged = list(sections.get(name) or [])
        merged += [i for i in (ids or ()) if i not in merged]
        sections[name] = merged
    # **每一拍记自己的落地日** —— 升级不改已装那几拍的零点。整包一个 `day`
    # 只在"这个包从头到尾一次装完"时才对,而升级正是它不对的那一次。
    beat_days: dict[str, int] = dict((have or {}).get("beat_days") or {})
    for bid in ((payload.get("sections") or {}).get("beats") or ()):
        beat_days.setdefault(str(bid), day)
    row = {
        "version": str(payload.get("version") or ""),
        "note": str(payload.get("note") or ""),
        # 第一次落地那天赢 —— 升级只换版本号与清单,不换这个包的零点。
        "day": int(have["day"]) if have else day,
        "tick": int(have["tick"]) if have else int(payload.get("tick", 0) or 0),
        "sections": sections,
        "beat_days": beat_days,
        # 「我这一版往那几格里写了什么」——  **按格并入**,和 `sections` 同一条:
        # 这一版没碰的那几个人,上一版写的那句还得记着,否则下一次 CAS 会把
        # 「我写过而且没人动过」错判成「不是我写的」,于是作者改不动自己上周那一句。
        "wrote": _merge_wrote((have or {}).get("wrote"), payload.get("wrote")),
        "declared": dict(payload.get("declared") or {}),
        # ⚠️ **这一格写 `False` 是因为「能走到这儿的包一定是启用着的」** ——
        # 装一份**停用着**的包在 `install_authored_pack` 里就当场拒了
        # (3.10.2,验收 A ①)。此前这儿的注释写着「再装一次 = 重新启用」,
        # 而那句话 `91a0fb9` 自己刚判为假(对带拍的包 `install` 会 rc 2),
        # 却把无拍那一半留在原地:重装一份已停用的无拍包 rc 0、这一格翻回
        # `False`、而它带来的人**还站在场外** —— 之后 `pack enable` 答
        # 「本来就是启用的」,人永远回不来。
        # **回来那条路只有一扇门:`enable_pack`。**
        "disabled": False,
        "seq": int(e.seq or 0),
    }
    proj.packs[pack_id] = row


#: 一个玩家身上留几条线。**有界** —— 它进得了提示词,所以它必须有界
#: (和 `SETTLED_INVITATIONS_KEPT` 同一把尺子)。掉出去的老线不再被回收,
#: 而那是对的:一条三十天没人管的线,收它比不收更出戏。
STORY_THREADS_KEPT = 6
#: 每小时上限那把尺要数最近几拍。留 `24` 条足够算任何一个小时的窗口。
STORY_RECENT_KEPT = 24


def _apply_director_log(proj: Projection, e: Event) -> None:
    """编剧写了一拍(3.11.0,批 3a)。**故事状态是它的投影,没有第二张表。**

    和余额折自 `payment` 逐字同一种。存一份直接写的"他的故事走到哪儿了",
    就多出一种和日志对不上的坏法,而这一层对不上的样子是**编剧对着一个错的
    张力值决定写多大** —— 没有一处会报错,只是剧情忽然变吵或者忽然死掉。
    """
    payload = e.payload or {}
    player_id = str(payload.get("player_id") or "")
    if not player_id:
        return
    row = proj.stories.setdefault(player_id, {
        "tension": 0.0, "tension_tick": 0, "phase": "setup",
        "threads": [], "recent": [], "intent": {}, "moves": 0,
    })
    try:
        tick = int(payload.get("tick", 0) or 0)
    except (TypeError, ValueError):
        tick = 0
    move = str(payload.get("move") or "")

    # ① 结算那一支只收线,不动张力也不算一拍 —— 它不是编剧写的,是到点了。
    if move == "collect":
        closed = str(payload.get("thread_id") or "")
        for thread in row["threads"]:
            if str(thread.get("id")) == closed:
                thread["closed"] = True
                thread["outcome"] = str(payload.get("outcome") or "expired")
        return

    row["moves"] = int(row.get("moves") or 0) + 1
    try:
        row["tension"] = max(0.0, min(1.0, float(payload.get("tension_after", 0.0))))
    except (TypeError, ValueError):
        row["tension"] = 0.0
    row["tension_tick"] = tick
    row["phase"] = str(payload.get("phase") or row.get("phase") or "setup")
    recent = list(row.get("recent") or [])
    recent.append(tick)
    row["recent"] = recent[-STORY_RECENT_KEPT:]

    # ② 这一拍开了一条线吗。**`due` 那一格现在就得记** —— 开线那一刻不记,
    #    以后补不回来(推迟功能可以,推迟数据不行)。
    thread_id = str(payload.get("thread_id") or "")
    if thread_id and not any(str(t.get("id")) == thread_id for t in row["threads"]):
        row["threads"].append({
            "id": thread_id,
            "promise": str(payload.get("promise") or ""),
            "with": str(payload.get("target") or ""),
            "phase": row["phase"],
            "opened_tick": tick,
            "stake": payload.get("stake") or None,
            "due_tick": int(payload.get("due_tick", 0) or 0),
            "closed": False,
            "outcome": "",
        })
        row["threads"] = row["threads"][-STORY_THREADS_KEPT:]
    else:
        for thread in row["threads"]:
            if str(thread.get("id")) == thread_id:
                thread["phase"] = row["phase"]

    # ③ 编剧意图:NPC 配合那一块的来源(口径 5)。**带有效期** ——
    #    过期自己消失,不另存一份"该不该拼这一块"。
    who = str(payload.get("target") or "")
    if move != "breathe" and who:
        row["intent"] = {
            "agent_id": who,
            "line": str(payload.get("line") or ""),
            "goal": str(payload.get("why") or ""),
            "until_tick": int(payload.get("pin_until", 0) or 0),
        }
    elif move == "breathe":
        # 喘一口气 = 这一拍没派人。**上一拍那个意图到此为止** ——
        # 留着的话她会在一个已经翻篇的意图上继续演。
        row["intent"] = {}


def _apply_pack_enabled(proj: Projection, e: Event) -> None:
    """一份内容包重新启用了(3.10.1)—— `pack_disabled` 的逆,**只翻一格**。

    🔴 **它是一扇独立的门,不是"再装一次"**:一份带拍的包停用之后,
    `pack install` 会被「这几拍的 id 已经有了」拒掉(`beat_fired` 那份历史按 id
    配对),而 `disable` 又有意不删 `:beats` —— 两条规矩都对,中间少一扇门。
    """
    payload = e.payload or {}
    pack_id = str(payload.get("pack_id") or "")
    row = proj.packs.get(pack_id)
    if row is None:
        return
    row["disabled"] = False


def _apply_pack_disabled(proj: Projection, e: Event) -> None:
    """一份内容包停用了(3.10.0,K7)。

    🔴 **只标一格,不删任何东西。** 玩家的记忆里有这一周发生过的事,他的钱包里有
    那 800 块 —— 删掉那几条事件 = 让历史指向不存在的东西,而"对账即重放"会让投影
    和日志对不上,**且没有任何地方会报错**(`forget_player` 那条 docstring 写的
    是同一句)。停用管的是**朝前看**的那一半。

    ⚠️ **回来那条路只有一扇门:`enable_pack`**(3.10.2)。这里曾经写着
    「再装一次同一个包会把它重新启用」,而那句话是假的:带拍的包重装会因为拍 id
    撞车 rc 2,无拍的包重装则把这一格翻回 `False` 而**人还站在场外** ——
    两种都回不来,只是坏的样子不同。现在装一份停用着的包**当场拒**并指向那扇门。
    """
    payload = e.payload or {}
    pack_id = str(payload.get("pack_id") or "")
    row = proj.packs.get(pack_id)
    if row is None:
        return
    row["disabled"] = True
    row["disabled_day"] = int(payload.get("day", 0) or 0)


def _apply_agent_join(proj: Projection, e: Event) -> None:
    payload = e.payload
    agent_id = e.who
    if agent_id is None:
        return
    spec = payload.get("spec", {})
    location = payload.get("location")
    proj.agents[agent_id] = AgentState(
        # **拷一份,别拿事件那个 dict 当自己的状态。** 投影是从事件折出来的**派生
        # 数据**,而事件是已经发生过的**事实** —— 共用一个可变 dict 的话,后来的
        # 一条 `persona_update` 会顺着 `agent.spec.update(...)` 就地改写那条创世
        # `agent_join` 在内存里的样子。于是"那条事件说了什么"有两个答案:
        # `World.events()`(内存窗口)说有 goals,`events export` 与任何一次重放说
        # 没有 —— 而日志才是对的。
        #
        # 没造成过数据损失(goals 自己有一条 persona_update 事件),但这是个地雷:
        # 投影往后加的任何一处写,都会静默地改写"历史显示成什么样"。
        spec=dict(spec) if isinstance(spec, dict) else spec,
        state=_doing(payload.get("state")),
        location=location,
        joined_at=e.ts,
        updated_at=e.ts,
    )


def _apply_agent_move(proj: Projection, e: Event) -> None:
    agent_id = e.who
    if agent_id is None:
        return
    # payload to_loc wins over event.loc (loc is only a fallback for current location)
    to_loc = e.payload.get("to_loc") or e.loc
    if to_loc is None:
        return
    agent = proj.agents.get(agent_id)
    if agent is None:
        # §3.2 constraint: system does not question event authenticity; create placeholder
        agent = AgentState(spec={}, joined_at=e.ts)
        proj.agents[agent_id] = agent
    agent.location = to_loc
    agent.updated_at = e.ts


def _apply_state_change(proj: Projection, e: Event) -> None:
    """Apply state_change events. Supported sub-kinds via payload['kind']:
    - 'sentiment': set relations[(as, target).sentiment] = payload['sentiment']
    - 'r_type': set relations[(as, target).r_type/r_type_back]
    - 'agent_state': merge payload['state'] into agents[who].state
    - 'location_join': walk-emitted, no-ops without payload['id'] (see actions.py)
    - 'agent_join_fallback': create agents[who] if missing (for injected memory)
    - 'persona_update': merge payload['spec'] into agents[who].spec (M6)
    - 'location_desc_update': set locations[loc_id].description (M6)
    """
    payload = e.payload
    kind = payload.get("kind", "sentiment")

    if kind == "sentiment":
        as_id = payload.get("as") or e.who
        target_id = payload.get("target")
        if as_id is None or target_id is None:
            return
        key = (as_id, target_id)
        rel = proj.relations.get(key)
        if rel is None:
            rel = Relation()
            proj.relations[key] = rel
        rel.sentiment = float(payload["sentiment"])

    elif kind == "sentiment_delta":
        # llm-relationship-judge: relationship as an INTEGRAL of history.
        # The absolute `sentiment` kind assigns (genesis injection keeps it);
        # deltas accumulate — one small talk can no longer erase a seeded
        # -0.7 enmity by overwriting it with +0.1 (w1 Round-3 smoke).
        as_id = payload.get("as") or e.who
        target_id = payload.get("target")
        if as_id is None or target_id is None:
            return
        key = (as_id, target_id)
        rel = proj.relations.get(key)
        if rel is None:
            rel = Relation()
            proj.relations[key] = rel
        try:
            delta = float(payload.get("delta", 0.0))
        except (TypeError, ValueError):
            return
        rel.sentiment = max(-1.0, min(1.0, rel.sentiment + delta))
        # relations-v5: optional finer axes ride the same event; absent or
        # malformed axes leave the single-axis fold untouched (old events
        # replay identically).
        axes = payload.get("axes")
        if isinstance(axes, dict):
            for axis in ("trust", "affection", "respect"):
                if axis in axes:
                    try:
                        step = float(axes[axis])
                    except (TypeError, ValueError):
                        continue
                    value = getattr(rel, axis) + step
                    setattr(rel, axis, max(-1.0, min(1.0, value)))
        # 出处:上一次改变它的就是**这一条**。和上面几行同一次折叠 ——
        # 一句"你们更亲近了"如果说不出出处,和一根进度条没有区别。
        conversation_id = payload.get("conversation_id")
        try:
            conversation_id = None if conversation_id is None else int(conversation_id)
        except (TypeError, ValueError):
            conversation_id = None
        rel.last_change = {
            "seq": e.seq,
            "tick": e.ts,
            "delta": delta,
            "direction": "up" if delta > 0 else ("down" if delta < 0 else "flat"),
            "conversation_id": conversation_id,
            "summary": str(payload.get("conversation_summary") or ""),
            "as_name": str(payload.get("as_name") or ""),
            "target_name": str(payload.get("target_name") or ""),
        }

    elif kind == "r_type":
        as_id = payload.get("as") or e.who
        target_id = payload.get("target")
        if as_id is None or target_id is None:
            return
        key = (as_id, target_id)
        rel = proj.relations.get(key)
        if rel is None:
            rel = Relation()
            proj.relations[key] = rel
        rel.r_type = payload.get("r_type", rel.r_type)
        rel.r_type_back = payload.get("r_type_back", rel.r_type_back)

    elif kind == "agent_state":
        agent_id = e.who
        if agent_id is None or agent_id not in proj.agents:
            return
        agent = proj.agents[agent_id]
        agent.state.update(_doing(payload.get("state")))
        agent.updated_at = e.ts

    elif kind == "location_join":
        # 角色走进一个地点(actions.py 的 walk 发的就是这条)。**这是"谁在哪"
        # 的唯一持续来源** —— agent_join 只说得出出生地。位置必须落进投影:
        # 开机 roster 读的正是 `projected.location`(__main__.py:334),投影不
        # 更新就等于每次重开把所有人传送回出生地,而世界照跑、没有任何报错。
        location = payload.get("location")
        if location:
            agent = proj.agents.get(e.who) if e.who else None
            if agent is not None:  # 未知角色不造占位:名册的权威是 agent_join
                agent.location = location
                agent.updated_at = e.ts
            return
        # 老分支:没有 location 的 location_join 是"注册一个地点"。地点表的
        # 权威是 `locations` 表,这里只兜底任何历史里已有的这类事件。
        loc_id = payload.get("id")
        if loc_id is None:
            return
        from anima_world.types import Location
        proj.locations[loc_id] = Location(
            id=loc_id,
            name=payload.get("name", loc_id),
            description=payload.get("description", ""),
        )

    elif kind == "agent_join_fallback":
        agent_id = e.who
        if agent_id is None:
            return
        if agent_id not in proj.agents:
            proj.agents[agent_id] = AgentState(
                spec=payload.get("spec", {}),
                joined_at=e.ts,
                updated_at=e.ts,
            )
        if payload.get("location"):
            proj.agents[agent_id].location = payload["location"]

    elif kind == "persona_update":
        agent_id = e.who
        if agent_id is None:
            return
        agent = proj.agents.get(agent_id)
        if agent is None:
            agent = AgentState(joined_at=e.ts)
            proj.agents[agent_id] = agent
        agent.spec.update(payload.get("spec", {}))
        agent.updated_at = e.ts

    # nested-map D7: `location_desc_update` is retired — the map is
    # configuration (the `locations` table), not history. Events of that
    # sub-kind survive in old logs and fall through here, ignored, like any
    # other unknown kind.


def _apply_location_join(proj: Projection, e: Event) -> None:
    payload = e.payload
    loc_id = payload.get("id") or e.loc
    if loc_id is None:
        return
    from anima_world.types import Location
    proj.locations[loc_id] = Location(
        id=loc_id,
        name=payload.get("name", loc_id),
        description=payload.get("description", ""),
    )


def _apply_agent_action(proj: Projection, e: Event) -> None:
    """M2: record agent_action — update state with last_action + bt_node_path."""
    agent_id = e.who
    if agent_id is None or agent_id not in proj.agents:
        return
    agent = proj.agents[agent_id]
    action_name = e.payload.get("action", "unknown")
    agent.state["last_action"] = action_name
    agent.state["bt_node_path"] = e.payload.get("path", "")
    agent.updated_at = e.ts


def _apply_agent_idle(proj: Projection, e: Event) -> None:
    """M2: record agent_idle — update state with last_idle_ts."""
    agent_id = e.who
    if agent_id is None or agent_id not in proj.agents:
        return
    agent = proj.agents[agent_id]
    agent.state["last_idle_ts"] = e.ts
    agent.state["last_action"] = "idle"
    agent.updated_at = e.ts


def _speaker_name(proj: Projection, speaker: str | None, payload: dict[str, Any]) -> str:
    """发言人的**人话名**,永远非空。

    三级回落:事件自己带的 → 名册(`agent_join` 那条事件里的 spec)→ id 本身。

    **中间那一级是为了老日志。** 线上世界的库里已经躺着几千条只有 `speaker`
    的 `narrative`,而重放是这个引擎的真相模型 —— 只在发射端补名字,等于说
    "这个 bug 修好了,但你已经有的历史永远是坏的"。名册就在同一条日志里
    (`agent_join.payload.spec.name`),重放到这一条时它必然已经折进来了。
    """
    given = str(payload.get("speaker_name") or "").strip()
    if given:
        return given
    if speaker:
        agent = proj.agents.get(speaker)
        if agent is not None:
            name = str(agent.spec.get("name") or "").strip()
            if name:
                return name
        return speaker
    return ""


def _apply_narrative(proj: Projection, e: Event) -> None:
    """M2: append narrative entry to projection.narrative_log."""
    agent_id = e.who
    speaker = e.payload.get("speaker", agent_id)
    text = e.payload.get("text", "")
    entry: dict[str, Any] = {
        "agent": agent_id,
        "speaker": speaker,
        # **加一个字段,不改 `speaker` 的含义。** `speaker` 是宿主已经在用的
        # 机器可读键(去重、按人过滤都靠它),把它换成名字等于跨仓库破坏。
        # 而它同时被当成"发言人"渲染在玩家脸上 —— 那一半由这个字段接手。
        "speaker_name": _speaker_name(proj, speaker, e.payload) or (agent_id or ""),
        "text": text,
        "ts": e.ts,
    }
    proj.narrative_log.append(entry)


def _apply_agent_leave(proj: Projection, e: Event) -> None:
    """agent-leave-return: mark the agent off-stage. Replay/observation only —
    the running world's presence truth is scheduler.agents membership (D2)."""
    agent = proj.agents.get(e.who) if e.who else None
    if agent is None:
        return
    agent.state["away"] = True
    agent.updated_at = e.ts


def _apply_agent_return(proj: Projection, e: Event) -> None:
    if e.who is None:
        return
    agent = proj.agents.get(e.who)
    if agent is None:
        agent = AgentState(joined_at=e.ts)
        proj.agents[e.who] = agent
    agent.state["away"] = False
    loc = e.payload.get("location") or e.loc
    if loc:
        agent.location = loc
    agent.updated_at = e.ts


def _apply_capability_registered(proj: Projection, e: Event) -> None:
    """M6: register/replace a capability catalog entry. Ignores events missing 'id'."""
    payload = e.payload
    cap_id = payload.get("id")
    if cap_id is None:
        return
    proj.capabilities[cap_id] = Capability(
        id=cap_id,
        kind=payload.get("kind", ""),
        description=payload.get("description", ""),
        params_schema=payload.get("params_schema", {}),
    )


def _apply_user_message(proj: Projection, e: Event) -> None:
    """M3: append user_message to narrative_log with speaker='user'."""
    text = e.payload.get("text", "")
    payload = e.payload
    # `speaker` 是字面量 `"user"` —— 它同样会被渲染成发言人,所以这条也要有名字。
    # 宿主给了就用宿主的(玩家的名字只有宿主知道),没给退回一句人话。
    name = str(
        payload.get("speaker_name") or payload.get("player_name") or ""
    ).strip() or "玩家"
    proj.narrative_log.append({
        "agent": e.who or "user",
        "speaker": "user",
        "speaker_name": name,
        "text": text,
        "ts": e.ts,
    })


# ── 插件的投影式事实(3.8.0 第 2 期 2b)────────────────────────────────────


def _apply_fact_delta(proj: Projection, e: Event) -> None:
    """`<插件>.<事实>.delta` —— 一次变化。**折出来那个数才是真相。**

    量表里那个值只是物化视图:抹掉它、换个进程、重开一次,这一串折一遍就回来了。
    一个直接写的事实做不到这件事,而做不到的样子是"抹掉就是抹掉了"。

    **不认识的载荷一律跳过,不猜。** 一条少了 `owner` 的 delta 折不进任何人身上,
    而随便挑一个人正是这一层最贵的错法。
    """
    payload = e.payload or {}
    owner = str(payload.get("owner") or "").strip()
    fact = str(payload.get("fact") or "").strip()
    if not owner or not fact:
        return
    # 🔴 **这条 delta 是谁发的,决定了它改得动谁的事实**(2026-08-26 验收 A 逮的)。
    #
    # 从前这儿只看 `payload.fact`,不看事件类型 —— 于是一个 `thief` 插件 emit 一条
    # `thief.伪造.delta{fact: "bank.存款"}` 就改得动别家的钱包。**它运行期不显形**
    # (物化视图没动),**重开那一刻才长出来**,而且零报错。
    #
    # 判据是**同一性**,不是白名单:事件类型必须**恰好**是 `<那个事实>.delta`。
    # 于是"谁发的"和"改谁的"是同一个名字,插件的命名空间边界(emit 只发得出
    # 自己命名空间的事件,加载期查过)自动把这道门也关上了 —— **一份判断,
    # 不是第二道闸**。
    if e.type != f"{fact}{FACT_DELTA_SUFFIX}":
        return
    try:
        delta = float(payload.get("delta") or 0.0)
    except (TypeError, ValueError):
        return
    row = proj.plugin_facts.setdefault(owner, {})
    row[fact] = round(row.get(fact, 0.0) + delta, 6)


def _apply_fact_source(proj: Projection, e: Event) -> None:
    """一条**内核**事件,被某个插件声明成了自己那个投影式事实的 delta(裁决 ④)。

    🔴 **这一格是 2d 的前置**:设计 §9.3 说「`payment` 事件照旧是 `economy.coins`
    的 delta」,而折叠端只认 `.delta` 后缀 —— 两句话对不上。改发一条新事件是
    **破坏消费方**(`payment` 在白名单上),两条都发是**同一笔钱记两遍账**;
    只有"多一格声明"这条不破坏任何人。

    **符号不给作者算**:`credit` 加、`debit` 减,两个键名写死。给一个 `sign` 让作者
    自己填的话,写反了不报错 —— 而一个反着记的账,对账即重放这条纪律就成了一句空话。
    """
    for owner, fact, delta, digits in _with_digits(
            proj.fact_sources.get(e.type, ()), e.payload or {}):
        row = proj.plugin_facts.setdefault(owner, {})
        # **每一步都进位**,和 `_apply_payment` 逐字同一条:折完再进和边折边进,
        # 在一串小数上给的是两个数。
        row[fact] = round(row.get(fact, 0.0) + delta, digits)


def fact_source_updates(
    specs: Any, payload: dict[str, Any],
) -> list[tuple[str, str, float]]:
    """一条内核事件按 `sources` 折出来的 `(owner, 事实, 变化量)`。

    🔴 **只有这一份算法。** 折叠端(重放)和调度器(运行期写物化视图)读的是同一个
    函数 —— 各写一遍的下场是本仓最怕的那种:跑着的世界一个数、重开之后另一个数,
    而两边都不报错。

    **符号不给作者算**:`credit` 加、`debit` 减,两个键名写死。给一个 `sign` 让作者
    自己填的话,写反了不报错 —— 而一个反着记的账,让「对账即重放」成了一句空话。
    """
    out: list[tuple[str, str, float]] = []
    for spec in specs or ():
        fact = str(spec.get("fact") or "")
        if not fact:
            continue
        try:
            amount = float(payload.get(str(spec.get("amount") or "amount")) or 0.0)
        except (TypeError, ValueError):
            continue
        if not amount:
            continue
        actor_form = str(spec.get("owner_form") or "actor") == "actor"
        # **进位跟着这个事实自己声明的位数走**,不是折叠端写死一个 6:
        # 钱要 2 位(见 `_apply_payment`),而两套进位就是两个钱包。
        digits = int(spec.get("round", 6))
        for key, sign in ((str(spec.get("credit") or ""), 1.0),
                          (str(spec.get("debit") or ""), -1.0)):
            if not key:
                continue
            who = str(payload.get(key) or "").strip()
            if not who:
                continue
            # `actor` = 载荷里那个名字是人的 id → 前缀成量表的 owner key。
            # **和 `Scheduler.stock_owner_of` 逐字同一条规则** —— 两处写岔的下场是
            # 折进了一个没人读的 owner 名下,而那一格永远是 0、永远不报错。
            # ⚠️ **一条事件的两头形状不同时,写成两条声明**(一条只给 `credit`、
            # 一条只给 `debit`):`payment` 的 `to` 可能是个人而 `from` 是
            # `__town__`。给一个 `owner_form` 让它同时管两头,是让作者在一个格子里
            # 说两件事 —— 而说不清的那一件会静默地记在一个没人读的名下。
            # 🔴 **delta 不预折**(2026-08-27 复核评审实测:`balance()=63.13`
            # 而量表 `63.12`)。`_apply_payment` 只折**累加结果**,这儿要是把
            # 每一笔先折一次,两边就是**两个钱包** —— 而它们分家的那一位,
            # 正是"一笔正好够的交易会不会被门禁拒掉"那一位。
            # **位数交给累加那一处用**(`_apply_fact_source` / `_flush_source_writes`
            # 都折累加值),这里只给差额。
            out.append((f"agent:{who}" if actor_form else who, fact, sign * amount))
    return out


def _with_digits(specs: Any, payload: dict[str, Any]):
    """`fact_source_updates` 的四元组版:多带一格"这个事实折到第几位"。"""
    by_fact = {str(spec.get("fact") or ""): int(spec.get("round", 6))
               for spec in (specs or ())}
    for owner, fact, delta in fact_source_updates(specs, payload):
        yield owner, fact, delta, by_fact.get(fact, 6)
