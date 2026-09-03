"""主持人:世界永远先开口(3.9.0,2026-09-02 裁决 §2.1)。

玩家点进一个世界,从前看到的是名册、地图,和**一个空白的聊天框** —— 他不知道自己
是谁、该找谁、说什么、说了会怎样。跑团桌上从来没有这个问题,因为 GM 永远先开口:
「你们站在英灵殿门口,门房抬头看了你一眼。你要做什么?」**场景 → 选项 → 后果。**

这个模块只做前两拍里**纯算术**的那一半:从候选池里挑 3–5 项,并把要交给 LLM 的那几句
话拼出来。它不认识 Redis、不认识 `World`,所以挑法可以被单独测。

## 三条这一层的纪律

1. 🔴 **挑哪几项是纯算术,LLM 只写字。** 这不是新发明,是 `contact.py` 已经写下的
   那句:「LLM 在这一层没有否决权,它只写那句线索……给它否决权还有第二个坏处:
   有 key 的世界和没 key 的世界会有两套行为,而差别只在一个环境变量上。」
   可验的说法:**同一个世界同一时刻挑两次,逐项相同**。
2. 🔴 **每一项都指向今天已经存在的那扇门**(`door`)。主持人是**荐者不是执行者**,
   它一条新的"写世界"的路都不开。这一条买到三样:壳只加一扇门、网站不必在前端拼
   第二套「能做什么」、引擎侧零新增写路径。
3. 🔴 **藏起来的人一个字都不许漏。** `card.billing == "hidden"` 的角色不进候选,
   **而且根本不进给 LLM 的那份提示** —— 不是"给了再叮嘱它别说"。理由是结构性的:
   那三扇结构化的门(roster / perception / player-options)壳能按行筛掉藏起来的人,
   而主持人交出去的是**散文**,名字是模型写进去的,壳筛不了;**筛一半比不筛更坏**。
   落法是让这份提示**没有别的名字来源** —— 它只由已经筛过的那几项拼出来。
"""

from __future__ import annotations

from typing import Any, Iterable

# 四个时刻。**这不是一份说明,是引擎里那道闸的取值** —— `World.host_turn` 只在
# 时刻钥匙变了的时候开口,别处没有第二条生成场景的路。
# 🆕 3.10.0(2a-②):第五个时刻 **`return`** ——「你回来了」。
# 离线超过 `host.away_ticks`,或者他上一屏之后有新的内容包落地。
# **零新状态**:`host_scene` 事件载荷里已经有 `tick`、投影里每个玩家一条,
# 而 `pack_installed` 也在同一条日志上 —— 两个判据都是减法。
# 🆕 3.11.0(玩法层批 3a):第六个时刻 **`acted`** ——「他刚做了一件事」。
#
# 🔴 **这一格是「实时编剧」的前提,而缺的不是一次模型调用,是钥匙上的一格。**
# 老板 09-02:「用户每操作一次应该就有新的剧情触发」。而在这一格之前,钥匙是
# `(place, day, beat_seq)` —— 一个玩家在**同一个地方、同一天**里点十次动词,
# `_host_trigger` 十次都答 `None`、`scene.source` 十次都是 `cached`:
# 屏幕一动不动,而世界里真的发生了十件事。
#
# 排在 `beat` **之后**是有意的:一条为他响的拍是「剧情安排的事」,比「他自己
# 刚做的事」更该决定这一屏怎么进场;而两样同时变时报最强的那个,是这张表
# 从 3.9.0 起就写着的次序纪律。
HOST_MOMENTS = ("arrive", "new_day", "beat", "acted", "ask", "return")

#: 每个时刻印在**玩家屏**上的那几个字。
#:
#: 🔴 **`HOST_MOMENTS` 加一格就要在这儿加一句人话**(3.10.2,验收 C ①):
#: 2a-② 加了 `return` 而这张表没跟,于是玩家屏上出现 `〔return · 模板〕`
#: —— **一个裸英文枚举名印在给玩家看的那一屏上**,而另外四个都是中文。
#: 闸在 `tests/test_host_doc_contract.py`(和「几个时刻」那两条同处)。
MOMENT_LABELS: dict[str, str] = {
    "arrive": "你到了",
    "new_day": "新的一天",
    "beat": "有事发生",
    # 🆕 3.11.0(批 3a):他自己刚做了一件事。
    "acted": "你做了点什么",
    "ask": "你问了一句",
    "return": "你回来了",
}
OPTION_KINDS = ("invitation", "beat", "talk", "verb", "travel", "free")
# 点下去走哪条**今天已经有**的门。闭集 —— 多一种就是多一条写世界的路。
DOOR_METHODS = ("answer_invitation", "chat", "player_walk", "player_tool", "free")
# 心流那三挑的标记(一个安全的、一个有风险的、一个社交的)。**不是难度数值** ——
# 引擎里没有难度这个东西,给它一个数字等于凭空发明一份世界不认识的真相。
TONES = ("safe", "risky", "social")

# ── 时段的分档词(3.10.0,批 1.1 ②)────────────────────────────────────────
#
# 🔴 **实测:day 0 00:25 的场景里写着「黄昏」「暮色」**(2026-09-02 验收 C 在真站量的)。
# 病根有两处,而它们互相盖住了:① 这一层从前只有四档,而且 `hour < 9` 一律读作
# 「清晨」—— 半夜十二点二十五分于是是「清晨」;② 给 LLM 的那份提示里**根本没有
# 时段词**,只有 `第 0 天 00:25` 这一行数字,模型自己挑了一个。
#
# 所以两件一起做:分档表放在这里(**一份,两条路共用** —— mock 那句和给模型的
# 提示各挑一次的话,同一时刻的世界会对同一个人说两种时辰),并且**把词喂进提示、
# 在模板里硬写**,不指望模型从 `00:25` 自己推。
#
# ⚠️ 边界写成"从几点起",不写成区间:区间要维护两个数,而两个数迟早对不上。
_DAYPARTS: tuple[tuple[int, str], ...] = (
    (0, "深夜"), (5, "清晨"), (9, "上午"), (12, "午后"), (17, "黄昏"), (20, "夜里"),
)


def daypart(hour: int) -> str:
    """这个钟点是一天里的哪一档。**世界钟说了算,不由模型猜。**"""
    word = _DAYPARTS[0][1]
    for start, name in _DAYPARTS:
        if int(hour) >= start:
            word = name
    return word


# ── 上一屏之后跟这个玩家有关的事(3.10.0,批 1.1 ①)──────────────────────────
#
# 🔴 **真站实测:第一拍响了(录取通知 + 一部 N96 + 800 块),而屏幕上一个字没提** ——
# 玩家的钱包突然多了 800,没有一处说为什么。主持人这一屏是他**唯一**读得到的地方,
# 而它从前只描景:「你在哪、这儿有谁、你要做什么」。
#
# **"世界永远先开口"的另一半是"先说刚发生了什么"** —— 跑团桌上 GM 不会在你抽完
# 一张牌之后跳过它直接问"你要做什么"。
#
# 一条纪律:**这一段是从事件日志折出来的,不是另攒一份**。它和余额折自 `payment`
# 逐字同一种 —— 攒一份"给玩家看的消息队列"就多出一种和日志对不上的坏法,而这一层
# 对不上的样子是"屏幕上说他拿到了,而库存里没有"。
#
# ⚠️ **白名单是策展的,和 `SUBSCRIBABLE_EVENTS` 同一个理由**:世界每 tick 都在发
# 事件,而其中绝大多数与他无关(别人走路、别人吃饭)。把全部倒给他等于没有这一段。
RECAP_EVENT_TYPES = (
    "beat_fired", "payment", "item_transfer", "agent_invites", "entity_interaction",
    # 🆕 3.10.1(验收 C ⑰):**他不在的时候有人来找过他。**
    # `agent_hail` 从前只进收件箱,于是「你回来了」那一屏对着两条 hail
    # 一个字不提 —— 而那正是 `return` 这个时刻最该说的一件事。
    # ⚠️ 它照旧过 `hidden_agents` 那道闸(下面 `who()`)。
    "agent_hail",
)
#: 一屏最多回顾几条。**截断了必须吭声**(和 perception 的 `overflow` 同一条):
#: 不说的话他在一个"只发生过三件事"的世界里做决定,而他永远不会知道自己被瞒了。
RECAP_LIMIT = 6

# ── 「他刚做了一件事」是哪几种事件(3.11.0,批 3a)─────────────────────────────
#
# **和 `RECAP_EVENT_TYPES` 并排住,有意不合并。** 两张表回答的是**两个不同的
# 问题**:那一张是「这一屏该跟他说哪几件事」,这一张是「他自己动没动过手」。
# `payment` / `grant_item` 进得了回顾(他的钱包确实变了),但那是**世界对他做的**,
# 不是他做的 —— 合成一张表的话,一条剧情拍打进来的钱会被当成「他操作了一次」,
# 于是编剧对着一件他没做过的事写下一拍。
#
# 🔴 **`who` 不是通用判据,这一格是这一层最容易写错的地方**(裁决 §2.10 ①):
#   `travel` / `entity_interaction` / `player_action` / `state_change` —— `who` 是 `player:<id>`
#   `conversation` —— `who` 是**她**,人在 `payload.participants` 里
#   `invitation_settled` —— `who` 也是**她**,人在 `payload.player_id` 里
# 照 `who` 筛的话后两种**永远筛不出来**,而下场是「聊完一轮屏幕不动」,零报错。
PLAYER_MOVE_EVENT_TYPES = (
    "travel",              # 他起程
    "state_change",        # 他到站(`kind == "location_join"`,到达那一刻补发的)
    "entity_interaction",  # 他点了一个动词
    "conversation",        # 一轮聊完(整场只在关闭时发这一条)
    "invitation_settled",  # 他答了一份邀请
    "player_action",       # 宿主自己报的那条
)
#: 邀请的四种结局里,**只有他真的答了的那两种算「他做了一件事」**。
#: `expired` 是他没来得及(手机上的人放下手机去吃了顿饭),`cancelled` 是她把话
#: 收回去了 —— 两样都不是他动的手,而这条分界正是邀请那一层写死的
#: 「『拒绝』和『过期』必须分得开」。
PLAYER_MOVE_INVITE_OUTCOMES = ("accepted", "declined")


def player_move_seq_of(event_type: str, payload: dict[str, Any], who: str,
                       player_key: str) -> bool:
    """这一条事件算不算**这个玩家**的一次操作。**纯函数,一处判断两处用**
    (投影折叠 + 任何想问同一个问题的地方)。

    `player_key` 是 `player:<id>`(账本、库存、事件顶层 `who`、在场位置一律
    是这个形状);邀请那一条里躺着的是**裸 id**,所以两种都要认得。
    """
    kind = str(event_type or "")
    if kind not in PLAYER_MOVE_EVENT_TYPES:
        return False
    bare = player_key.split(":", 1)[-1]
    if kind == "state_change":
        # 到站那一条。**别的 `state_change` 一概不算** —— 关系变了、人设被改写
        # 都是世界对他做的,不是他做的。
        return (str(payload.get("kind") or "") == "location_join"
                and str(who or "") == player_key)
    if kind == "conversation":
        for person in payload.get("participants") or []:
            if not isinstance(person, dict):
                continue
            if str(person.get("kind") or "") == "user" and str(person.get("id") or "") == bare:
                return True
        return False
    if kind == "invitation_settled":
        return (str(payload.get("player_id") or "") == bare
                and str(payload.get("outcome") or "") in PLAYER_MOVE_INVITE_OUTCOMES)
    return str(who or "") == player_key

FREE_OPTION_ID = "opt:free"
FREE_LABEL = "自己说点什么……"

# 候选排序里各类的先后。**故事线在前,杂事在后**:一个玩家点进来最该看到的是
# 「有人在等你答话」和「你这条线走到哪儿了」,不是「你可以端详布告栏」。
_KIND_RANK = {kind: i for i, kind in enumerate(OPTION_KINDS)}


def interaction_line(verb_label: str, target_name: str) -> str:
    """「你<动词>了<东西>。」—— **一次交互给玩家的那一句模板人话**(3.10.0)。

    🔴 **一处写法,两个读者**:主持人那一屏的回顾(`recap_lines`)和叙事流里
    那一句(`Scheduler._record_player_action_line`)共用它。各写一套的话,
    同一件事在两个地方是两种说法,而**没有一处会报错**。
    """
    verb, target = str(verb_label or "").strip(), str(target_name or "").strip()
    if not verb or not target:
        return ""
    return f"你{verb}了{target}。"


#: 回顾里提到一个**藏起来的人**时,用来顶替名字的那三个字。
#: 「有人」而不是整条不提,是有意的:钱包里多了 500 块这件事**必须说**
#: (不说的话他在一个"钱莫名其妙变了"的世界里做决定),而**谁给的**才是要藏的。
HIDDEN_WHO = "有人"


def recap_lines(
    events: Iterable[dict[str, Any]], *, player_key: str,
    item_names: dict[str, str] | None = None,
    agent_names: dict[str, str] | None = None,
    hidden_agents: Iterable[str] = (),
) -> list[str]:
    """上一屏之后**跟这个玩家有关**的那几件事,一件一行人话(3.10.0,批 1.1 ①)。

    `player_key` 是 `player:<id>` —— 账本、库存、事件顶层的 `who`、在场位置一律是
    这个形状(`beats.bind_player` 的 docstring 逐字写着这条)。

    **纯函数**:进来的是事件字典,出去的是几句中文。它不认识 Redis、不认识 `World`,
    所以"哪几件算数、怎么说"可以被单独测,而且 mock 那条路和给模型的提示**共用它** ——
    各拼一份的话,没 key 的世界和有 key 的世界会对同一段历史说两套话。

    ⚠️ **一律第二人称**:这一段是说给他听的(批 1.1 ②)。
    ⚠️ **超出 `RECAP_LIMIT` 要吭声** —— 最后一行会说还有几件没列。

    🔴 **`hidden_agents` 是 3.10.1 补的,而它补的是一个真的漏**(2026-09-02 验收 A ①)。
    `card.billing == "hidden"` 的人**不进候选、不进给模型的那份提示** —— 那道闸
    3.9.0 就立了,理由是这一屏交出去的是**散文**,宿主筛不了。可**回顾这一段
    从来没过那道闸**:一个藏起来的角色给玩家转 500 块,屏幕上就印着
    「黑衣人给了你 500 块。」—— 而它同时进 `mock_scene` 和 `scene_messages`,
    **两条路一起漏**。

    补法照那道闸的原样:**不是"给了再叮嘱别说",是根本不给名字** ——
    当事人是藏起来的人时,名字换成 `HIDDEN_WHO`(「有人」)。
    ⚠️ **事情本身照说**:钱包多了 500 必须说,要藏的是**谁给的**。
    整条吞掉会让他在一个"钱莫名其妙变了"的世界里做决定,而那是另一种坏。
    """
    lines: list[str] = []
    items = item_names or {}
    people = agent_names or {}
    hidden = {str(a) for a in (hidden_agents or ())}

    def who(holder: str) -> str:
        holder = str(holder or "")
        if holder == player_key:
            return "你"
        if holder in ("__town__", "__world__"):
            return ""
        # 🔴 藏起来的人:名字换掉,事情照说(见 docstring)。
        if holder in hidden:
            return HIDDEN_WHO
        return people.get(holder, holder.split(":")[-1] or holder)

    for event in events:
        kind = str(event.get("type") or "")
        payload = event.get("payload") or {}
        if kind == "beat_fired":
            # 🔴 **只有作者写了 `narrate` 的拍才进这一段。** 别的拍这一层
            # 一个字都不编 —— 它手上只有 op 名(`memory` / `pay`),而把
            # 「这一拍响了」翻译成一句剧情是**作者的活**(批 1.1 ⑤ 开的正是这扇门)。
            # 编一句的下场是屏幕上出现一句世界里没有的话,而它读起来像真的。
            if str(payload.get("for") or "") != player_key:
                continue
            said = str(payload.get("narrate") or "").strip()
            if said:
                lines.append(said)
            continue
        if kind == "payment":
            src, dst = str(payload.get("from") or ""), str(payload.get("to") or "")
            if player_key not in (src, dst):
                continue
            try:
                amount = float(payload.get("amount") or 0)
            except (TypeError, ValueError):
                continue
            money = f"{amount:g}"
            if dst == player_key:
                giver = who(src)
                lines.append(f"{giver}给了你 {money} 块。" if giver
                             else f"你多了 {money} 块。")
            else:
                taker = who(dst)
                lines.append(f"你付出去 {money} 块" + (f",给了{taker}。" if taker else "。"))
            continue
        if kind == "item_transfer":
            src, dst = str(payload.get("from") or ""), str(payload.get("to") or "")
            if player_key not in (src, dst):
                continue
            item_id = str(payload.get("item_id") or "")
            # 事件里那格**当时的人话**优先(老事件缺它,读的一方要回落 —— 契约里
            # `item_transfer` 那一条逐字写着这句)。
            name = str(payload.get("item_name") or "") or items.get(item_id, item_id)
            try:
                qty = int(payload.get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1
            tail = f"(×{qty})" if qty > 1 else ""
            if dst == player_key:
                giver = who(src)
                lines.append(f"{giver}给了你{name}{tail}。" if giver
                             else f"你手上多了{name}{tail}。")
            else:
                taker = who(dst)
                lines.append(f"你把{name}{tail}给了{taker}。" if taker
                             else f"你的{name}{tail}没了。")
            continue
        if kind == "agent_invites":
            if str(payload.get("player_id") or "") != player_key.split(":", 1)[-1]:
                continue
            # 这一支不走 `who()`,所以它要自己过那道闸 —— 漏在这儿的样子是
            # 「黑衣人问你要不要一起……」,和转账那条一样响。
            asker = (HIDDEN_WHO if str(payload.get("agent_id") or "") in hidden
                     else str(payload.get("agent_name")
                              or payload.get("agent_id") or HIDDEN_WHO))
            verb = str(payload.get("verb_label") or payload.get("verb") or "")
            target = str(payload.get("target_name") or payload.get("target") or "")
            what = f"一起{verb}{target}" if verb else "一起做件事"
            lines.append(f"{asker}问你要不要{what}。")
            continue
        if kind == "agent_hail":
            if str(payload.get("player_id") or "") != player_key.split(":", 1)[-1]:
                continue
            # 藏起来的人照旧只叫「有人」——`who()` 那道闸在这儿也要过。
            caller = who(str(payload.get("agent_id") or "")) or HIDDEN_WHO
            said = str(payload.get("line") or "").strip()
            lines.append(f"{caller}来找过你,说「{said}」。" if said
                         else f"{caller}来找过你。")
            continue
        if kind == "entity_interaction":
            if str(event.get("who") or "") != player_key:
                continue
            verb = str(payload.get("verb_label") or payload.get("verb") or "")
            target = str(payload.get("target_name") or payload.get("target") or "")
            said = interaction_line(verb, target)
            if said:
                lines.append(said)
            continue
    if len(lines) > RECAP_LIMIT:
        extra = len(lines) - RECAP_LIMIT
        lines = lines[:RECAP_LIMIT] + [f"还有 {extra} 件事没细说。"]
    return lines


# ── 世界先开口,而这一条要开到**对话里**去(3.10.0,批 1.2)────────────────
#
# 老板 2026-09-02 刷新之后真进去玩,原话:
#   「让我去跟他们说话我不知道说啥,剧情没法往下走啊,不让他们自己搭话吗」
#
# 🔴 **「世界永远先开口」这条纪律,3.9.0 只做进了主持人那一屏,没做进对话里。**
# 玩家点「跟夏说说话」,拿到的还是一个空白输入框 —— 而跑团桌上 GM 不会把 NPC
# 推到你面前然后闭嘴。这一族三件:她先说第一句 · 拍子能让她主动来找你 ·
# 输入框上方几句现成的话。

OPENING_SYSTEM = (
    "这一轮「你先开口」:对方还没有说任何话。"
    "用一到三句话主动搭话 —— 说你此刻在做什么、为什么注意到他,"
    "或者把你想跟他说的那件事说出来。"
    "「不要」替他说话,「不要」问他「你想聊什么」,也「不要」复述这条指示。"
)


def opening_context(*, line: str = "", hook: str = "", beat_note: str = "",
                    place_name: str = "") -> list[str]:
    """她先开口那一轮,除了平常那一整套之外还要知道的几件。

    **只加"这一刻的由头",不加第二份人设/记忆/感知** —— 那一整套走的是
    `ChatService.prompt_blocks`,和玩家先说话那条路**逐字同一份**。
    各拼一份的话,她主动开口时会是另一个人,而没有一处会报错。
    """
    out: list[str] = [OPENING_SYSTEM]
    if place_name:
        out.append(f"你们此刻都在{place_name}。")
    if line:
        # 作者写在 `hail` 上的**她的台词**。给模型当由头,不强迫它逐字念 ——
        # 逐字念就成了一句永远不变的台词,而她此刻的心情、你们的关系都白算了。
        out.append(f"你正想对他说的是这个意思:「{line}」")
    if beat_note:
        # 指着他的那一拍刚响过 —— **这就是"剧情往下走"的那个由头**。
        out.append(f"你正惦记着这件事:{beat_note}")
    if hook:
        # 主持人那一屏给这一项写的钩子。同一件事在两处说同一句话。
        out.append(f"他看到的那句提示是:{hook}")
    return out


#: 一句自由文本收尾的标点。缺了就补一个句号 —— 而**已经有的不叠加**
#: (「……停了一下。。」同样是一句念不通的话)。
_SENTENCE_END = "。!?…」』.!?"


def _as_clause(text: str) -> str:
    """把一句自由文本收成能直接拼进段落的样子:去掉首尾空白,补一个句号。"""
    said = str(text or "").strip()
    if not said:
        return ""
    return said if said[-1] in _SENTENCE_END else said + "。"


def mock_opening(agent_name: str, *, line: str = "", hook: str = "",
                 beat_note: str = "") -> str:
    """没配 key / LLM 挂了时她开的那一句。**没配 key 是默认状态**,所以这不是
    降级路上的边角料,而是很多人看到的第一句话。

    🔴 **`line` 和 `beat_note` 不是同一种东西,别当成同一种用**(拿真 CLI 敲出来的):
    `line` 是作者写在 `hail` 上的**她的台词**(「师弟!下来一趟。」),
    `beat_note` 是拍上的 `narrate`,是**旁白**(「手机震了一下,是个没存过的号码。」)。
    把旁白塞进引号里当她的台词念,出来的是一句念不通的话 —— 而**一句念不通的话
    和一句错的一样贵**。有台词就用台词,只有旁白就把旁白当旁白写。
    """
    name = str(agent_name or "").strip() or "她"
    if line:
        return f"{name}朝你走过来:「{line}」"
    if beat_note:
        return f"{beat_note}{name}抬眼看见你。"
    if hook:
        # 🔴 **`hook` 是一句自由文本,缝进句子中间永远不安全**(3.11.0,验收 A 实测:
        # 「楚子航**正**他刚从楼梯口拐过来,看见你,看见你,停了一下。」)。
        # 它由模型写、写的是「此刻看上去是什么样」,而那可能是一个词组
        # (「低头擦一把长刀」)也可能是一整句带主语的话(「他刚从楼梯口拐过来」)
        # —— 两种形状用同一个 `正{hook},` 的模子去套,后一种必然出病句。
        # **要么标点隔开,要么单独成句**:这里用破折号隔开,两种形状都读得通,
        # 而且不必去猜它是哪一种。
        return f"{name}在那边 —— {_as_clause(hook)}"
    return f"{name}抬头看见你,朝你点了点头:「你来了。」"


#: 建议句一屏给几条。**2–3 条** —— 给一条等于替他做决定,给五条又变成另一份菜单。
SUGGESTION_LIMIT = 3


def suggestion_seeds(*, hook: str = "", beat_note: str = "",
                     agent_name: str = "", stance: str = "") -> list[str]:
    """没有 LLM 时那几句建议 —— **纯算术,同一时刻挑两次逐字相同**。

    🔴 和主持人挑选项那一层同一条纪律:**挑什么是算出来的,LLM 只写字**。
    有 key 时这几句是给模型的**由头**,没 key 时它们直接就是那几句。
    """
    name = str(agent_name or "").strip()
    # 🔴 **一屏之内人称不许混**(3.10.1,验收 C ⑱)。上一版这几句写死「他」,
    # 只有最后一句用名字 —— 于是同一个输入框上方并排着「问问**他**刚才说的那件事」
    # 和「问问**沈亦柔**最近怎么样」,读起来像在说两个人。
    # 名字知道就一律用名字,不知道就一律用「TA」(不猜性别 —— 猜错的那一半
    # 每次都在同一个角色身上错)。
    who = name or "TA"
    out: list[str] = []
    if beat_note:
        out.append(f"问问{who}刚才说的那件事")
    if hook:
        out.append(f"接着{who}手上那件事往下问")
    if stance in ("试探", "回避", "刺"):
        out.append(f"直接问{who}怎么了")
    out.append(f"问问{who}最近怎么样")
    # ⚠️ **四句是同一种形状**(3.10.2,验收 C ⑧):都是「你打算说什么」的
    # 描述句,都点名说给谁听。上一版最后一句是光秃秃的「说说你自己」——
    # 前三句在说「跟谁」,它在说「说什么」,并排读像换了个人在讲话。
    out.append(f"跟{who}说说你自己")
    seen: list[str] = []
    for line in out:
        if line not in seen:
            seen.append(line)
    return seen[:SUGGESTION_LIMIT]


def free_option() -> dict[str, Any]:
    """自由输入那一项。**永远在,永远最后,而且不占 `host.max_options` 的名额。**

    跑团的规矩正是这样:GM 给选项,玩家可以不选,但 GM 先说话。把它算进名额里,
    一个选项多的时刻就会把"我想说点别的"挤掉 —— 而那恰恰是这一层要保住的自由。
    """
    return {
        # 键序照 `contract.host.option_keys` —— **JSON 键序不是契约,而"我自己声明了
        # 一张有序表、自己的产出却不照它"是另一回事**:一个照表写解析器的人会先
        # 怀疑自己。别的项都是同序的,就这一项从前把 `who` 排在 `hook` 后面。
        "id": FREE_OPTION_ID, "kind": "free", "label": FREE_LABEL, "who": "", "hook": "",
        "tone": "social", "available": True, "reason": "", "refusal": "", "cost": "",
        "door": {"method": "free", "params": {}},
    }


def select_options(candidates: Iterable[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """从候选池里挑不超过 `limit` 项,**确定且可复现**,末尾补上自由输入。

    挑法(照设计稿 §3.2 的起点:一个安全的、一个有风险的、一个社交的):
    先按 (类别, id) 排成一个**确定**的池子,再各取一个三种口味的,剩下的名额按池子
    顺序补。**排序按 id 而不是"最有意思的那几个"** —— 和 perception 的截断同一条
    理由:要的是确定,而不确定的挑法会让同一个世界每次给他不同的现实。
    """
    pool = sorted(candidates, key=lambda o: (_KIND_RANK.get(o["kind"], 99), o["id"]))
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    cap = max(0, int(limit))
    for tone in TONES:
        if len(chosen) >= cap:
            break
        for option in pool:
            if option["id"] in seen or option.get("tone") != tone:
                continue
            chosen.append(option)
            seen.add(option["id"])
            break
    for option in pool:
        if len(chosen) >= cap:
            break
        if option["id"] not in seen:
            chosen.append(option)
            seen.add(option["id"])
    chosen.sort(key=lambda o: (_KIND_RANK.get(o["kind"], 99), o["id"]))
    chosen.append(free_option())
    return chosen


def welcome_back(*, away_days: int = 0, packs: list[dict[str, Any]] | None = None
                 ) -> list[str]:
    """「你回来了」那一屏开头的一两句(3.10.0,2a-②)。

    🔴 **「本周更新」读的是 `pack_installed`,不是一份另攒的公告栏** —— 攒一份就多
    一种和日志对不上的坏法,而这一层对不上的样子是"横幅上写着这周加了三件事,
    而世界里一件都没有"。
    """
    out: list[str] = []
    if away_days > 0:
        out.append(f"你有 {away_days} 天没来了。")
    # 🔴 **「这段时间世界更新了」只说一次**(3.10.2,验收 C ⑧)。
    # 上一版按包逐条说,于是他离线期间装了三份包,屏上就是三句
    # 「这段时间世界更新了:…」 —— **同一句开场白连说三遍**,读起来像卡带了。
    # 这一句是**一段时间**的总结,不是每份包各自的公告。
    rows = list(packs or ())
    if rows:
        notes = [str(r.get("note") or "").strip() or f"({r.get('id')})" for r in rows]
        said = "、".join(notes)
        out.append(f"这段时间世界更新了:{said}。" if len(rows) == 1
                   else f"这段时间世界更新了 {len(rows)} 次:{said}。")
    return out


def mock_scene(*, place_name: str, day: int, hour: int,
               options: list[dict[str, Any]], going_to: str = "",
               recap: list[str] | None = None) -> str:
    """没有 key / LLM 挂了 / 超时时那一段话。

    **没配 key 是这个引擎的默认状态**,所以这不是降级路径上的边角料,而是很多人看到
    的第一屏(#15 那一课)。它只用**已经筛过**的那几项拼,和真提示词同一个来源 ——
    两边各拼一份的话,mock 迟早会说出一个藏起来的人的名字。
    """
    # **分档表只有一份**(`daypart`),给模型的那条路读的是同一份 —— 各挑一次的话,
    # 同一时刻的世界会对同一个人说两种时辰。
    when = daypart(hour)
    # 🔴 **先说刚发生了什么,再说景**(批 1.1 ①)。顺序是承重的:玩家读到的第一句
    # 该是「一封录取通知躺在你桌上」,不是「你在你家」—— 真站那一趟,第一拍响了
    # (信 + 手机 + 800 块),而屏幕上一个字没提。
    said = "".join(recap or ())
    # ⚠️ **他可能还没落脚**(刚进来、还没走过一步)。拿一个空的地名去拼,出来的是
    # 「你在。」—— 一句念不通的话,而这个仓库的口径是**一句念不通的拒绝语,和一句
    # 错的一样贵**。这一格是拿真 CLI 敲出来的,不是想出来的。
    if going_to:
        # 在路上的人不该被告知"你在出发地" —— 那正是两扇门说两句话的那一格。
        return said + f"第 {day} 天{when},你在去{going_to}的路上。到了地方再说。"
    if not place_name:
        return said + f"第 {day} 天{when}。你还没落个脚 —— 先挑个地方站过去。"
    head = said + f"第 {day} 天{when},你在{place_name}。"
    # ⚠️ **拿 `who` 那一格,不是 `label`**:label 是一句祈使("和苏晚夏说说话"),
    # 拼进"这儿有人:…"就成了「这儿有人:和苏晚夏说说话。」—— **一句念不通的话**。
    # 候选自己带着人名,这里就不该再从按钮上的字里去抠。
    people = [o["who"] for o in options
              if o["kind"] in ("talk", "invitation") and o.get("who")]
    if people:
        head += "这儿有人:" + "、".join(people[:3]) + "。"
    elif len(options) > 1:
        head += "四下没什么人。"
    return head + "你要做什么?"


def scene_messages(*, place_name: str, place_desc: str, day: int, hour: int,
                   minute: int, world_setting: str,
                   options: list[dict[str, Any]], going_to: str = "",
                   recap: list[str] | None = None) -> list[dict[str, str]]:
    """交给背景槽的那一次调用。**一次调用同时写场景那段话和每一项的钩子。**

    🔴 **这份提示里没有第二个名字来源。** 它只由 `options` 拼出来,而 `options` 已经
    按 `billing` 筛过 —— 藏起来的人不在里面,所以模型手上根本没有他的名字。
    这比"给了再让它别说"强的地方在于:后者失手一次就漏了,而且漏在一段散文里,
    宿主筛不掉;这里失手需要模型凭空编出一个它没见过的名字。
    """
    listed = "\n".join(
        f"{i}. {o['label']}" for i, o in enumerate(options, 1) if o["kind"] != "free"
    )
    # 🔴 **人称统一「你」**(批 1.1 ②)。上一版这份提示整份用「他」,而 `mock_scene`
    # 用「你」—— 于是同一个世界,配了 key 和没配 key 的两屏是两种人称,而**差别只在
    # 一个环境变量上**(`contact.py` 那句纪律的另一面)。模型多半会跟着提示里的人称
    # 写,所以这不是一句叮嘱,是把提示本身改成第二人称。
    system = (
        "你是一个文字冒险游戏的主持人,对着**玩家本人**说话。用中文写,"
        "**一律用第二人称「你」**,不许用「他」指玩家。口吻克制、具体、有画面,"
        "「不要」替玩家做决定,「不要」替任何角色说出成段的台词。"
    )
    # 🔴 **刚发生的事排在最前,并且明说"先说这个"**(批 1.1 ①)。
    # 它是这一屏唯一会讲"钱包为什么多了 800"的地方。
    happened = "".join(f"- {line}\n" for line in (recap or ()))
    user = (
        (f"世界:{world_setting}\n" if world_setting else "")
        + (f"上一屏之后刚发生的事(**必须先说这几件,再写景**):\n{happened}"
           if happened else "")
        + (f"地点:你在去{going_to}的路上,还没到。\n" if going_to
           else f"地点:{place_name}。{place_desc}\n" if place_name
           else "地点:你还没落脚,不在任何地方。\n")
        # ⚠️ **时段词喂进去,不让模型从 `00:25` 自己推** —— 真站实测它把
        # 第 0 天 00:25 写成了「黄昏」「暮色」。
        + f"时间:第 {day} 天 {hour:02d}:{minute:02d}({daypart(hour)})\n"
        + (f"此刻你可以做的事:\n{listed}\n" if listed else "此刻你没有什么特别能做的。\n")
        + "\n请按下面的格式输出,不要有别的字:\n"
        + ("第一行:先用一两句说清上面那几件刚发生的事,再接一段开场,"
           "说清你在哪、看得见什么、气氛怎样;整段 40–100 字。\n"
           if happened else
           "第一行:一段 30–80 字的开场,说清你在哪、看得见什么、气氛怎样。\n")
        + "之后每行一句不超过 20 字的钩子,顺序对应上面那几件事,"
        + "写它此刻「看上去」是什么样,不要写结果。\n"
        + f"⚠️ 时辰按上面给的「{daypart(hour)}」写,别自己另挑一个。\n"
        + "⚠️ 只许提到上面出现过的人和地方,不许写出别的名字。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_scene_reply(text: str, *, options: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """把模型那几行拆成 `(场景, 每一项的钩子)`。**读不懂就少给,绝不猜。**

    对不齐的时候按顺序尽量填,填不满的那几项钩子是空串 —— 而空钩子是合法的
    (`hook` 本来就可空)。硬要求模型给出严格 JSON 是这一层最容易碎的地方:
    一次格式失手就是整屏没有场景,而这一屏正是玩家点进来看到的第一样东西。
    """
    lines = [line.strip() for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return "", []
    scene = lines[0]
    hooks: list[str] = []
    wanted = len([o for o in options if o["kind"] != "free"])
    for line in lines[1:]:
        # 模型爱写「1. …」「钩子 2:…」——去掉序号,留那句话。
        for sep in (". ", "、", ":", ":"):
            head, found, tail = line.partition(sep)
            if found and len(head) <= 6 and any(ch.isdigit() for ch in head):
                line = tail.strip()
                break
        if line:
            hooks.append(line)
    return scene, hooks[:wanted]


def suggestion_messages(*, agent_name: str, beat_note: str, doing: str,
                        seeds: list[str]) -> list[dict[str, str]]:
    """把那几个"由头"交给背景槽写成人话。**一次调用,失败即模板,不重试。**

    🔴 **模型只写字,不挑事**:给它的是已经挑好的那几条,它把每一条写成一句
    玩家可以直接说出口的短话。让它自己想说什么,同一个世界同一时刻会给出不同的
    现实 —— 那正是主持人那一层写死过的同一条。
    """
    listed = "\n".join(f"{i}. {s}" for i, s in enumerate(seeds, 1))
    system = (
        "你在帮一个文字冒险游戏的玩家想「接下来可以说什么」。"
        "用中文写,每条不超过 15 字,是玩家「直接说得出口」的一句话,"
        "「不要」写成旁白或指示,「不要」加编号以外的任何符号。"
    )
    user = (
        f"对方是{agent_name}。"
        + (f"他此刻{doing}。\n" if doing else "\n")
        + (f"刚发生的事:{beat_note}\n" if beat_note else "")
        + f"请把下面每一条改写成一句玩家能直接说的话,一行一条,顺序不变:\n{listed}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_suggestions(text: str, *, limit: int) -> list[str]:
    """把模型那几行拆成建议句。**读不懂就少给,绝不猜** —— 和 `parse_scene_reply`
    逐字同一个姿势;调用方拿不到就回落到那几条种子。"""
    out: list[str] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        for sep in (". ", "、", ":", ":", ") ", ")"):
            head, found, tail = line.partition(sep)
            if found and len(head) <= 3 and any(ch.isdigit() for ch in head):
                line = tail.strip()
                break
        line = line.strip("-·— 「」\"'")
        if line:
            out.append(line[:20])
        if len(out) >= limit:
            break
    return out
