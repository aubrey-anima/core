"""Beat director: authored story beats fired into a running world.

The beat script is CONFIGURATION (like world_seed.json and the map): the
engine never edits it and it stays out of the event log. Which beats have
FIRED is simulation history: each firing records one `beat_fired` event and
the fired-set is rebuilt from those events on recovery (beat-director D1).

Loading is strict — a malformed script refuses to start, with every error
listed, because the script is authored data and feedback must be immediate.
Firing degrades — a predicate that fails to evaluate reads as "not met", a
bad payload op is skipped with a warning — because a running world must
never crash over one beat (D4).

This module is pure data + evaluation; it imports no scheduler/storage code.
The Scheduler owns the side effects (recording events, registering agents).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable

from anima_world.types import Projection
from anima_world.world_time import MINUTES_PER_DAY, WorldTime

logger = logging.getLogger(__name__)

# Ops whose expansion is pure event construction (`expand_event_op`).
# `agent_join` and `location_desc` need scheduler side effects and are
# handled there (Scheduler._expand_beat_op).
EVENT_OPS = {
    "memory", "broadcast_memory", "sentiment_delta", "r_type", "persona_update",
    # 物质层:op 曾经只能改"她怎么想",改不了"她有什么"。一个作者写不出"父亲的
    # 怀表在这一幕里被弄丢了"——只能写一条"她觉得很难过"的记忆去暗示。这两条
    # 展开成账本已有的事件类型(payment / item_transfer),余额与库存本来就是它们
    # 的投影,所以不新增 schema、不改 db 格式。
    "pay", "grant_item",
}
# 🆕 3.10.0(批 1.2 ②):`hail` —— **让一个角色主动来找玩家搭话。**
#
# 老板原话:「不让他们自己搭话吗」。剧情往下走从前只有一条路:玩家自己去点某个人。
# 这一条让作者写得出「芬格尔在车站等你」那种**世界主动来找你**的时刻。
# 它走的是**已有那条**(`agent_hail` 事件 + 站点已有的 `hail.ts`),
# 引擎这一层一条新的"写世界"的路都不开。
VALID_OPS = EVENT_OPS | {"agent_join", "location_desc", "agent_leave", "agent_return",
                         "hail"}

# 物质 op 里 `from`/`to` 允许写的非角色持有者。金库允许负债(economy.TOWN),
# 世界是凭空来源 —— 一件道具"本来就在她口袋里"不需要有人先失去它。
BEAT_WORLD_HOLDER = "__world__"
_TOWN_HOLDER = "__town__"  # economy.TOWN,这里不 import 存储层

# 导演能观察到的世界。谓词曾经只有两个(关系值、同地),于是节拍脚本对世界的绝大
# 部分状态是瞎的 —— 需求、钱、物品、关系描述、记忆一律看不见,剧情只能靠"到点了"
# 和"两个人碰上了"来推。这些都是投影/黑板里已经有的量,不进事件日志、不改 db。
_VALID_PREDICATES = {
    "sentiment", "co_located", "r_type", "need", "money", "has_item", "memory",
}

NEED_NAMES = ("energy", "hunger", "social")

# 谓词的必填字段 —— 与 `OP_REQUIRED_FIELDS` 同一个用途:作者照文档写就该能过,
# 而镜像端照这份表写自己的校验。`anima-world contract` 报它。
PREDICATE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "sentiment": ("as", "target", "op", "value"),
    "co_located": ("agents",),
    "r_type": ("as", "target", "contains"),
    "need": ("agent", "need", "op", "value"),
    "money": ("agent", "op", "value"),
    "has_item": ("agent", "item"),
    "memory": ("agent", "contains"),
}
_AGENT_BUNDLE_KEYS = {"id", "name", "location", "personality"}  # world_seed agent entry shape

# ── `player` 选择器(3.9.0,2026-09-02 裁决 §2.2)──────────────────────────────
#
# 世界文件写在玩家出现之前,所以一条剧情拍**写不出**玩家的 id。从前的下场不是
# "少一个特性",是一个**静默作废**的洞:写 `agent_id: "player"` 时离线两扇门答
# `loadable: true`、开机不报错、运行期一句 warning 跳过,而 `_fire_beat` 照样
# `mark_fired` 并写下 `beat_fired` —— **这一拍永久失效,且重启不重放**。
#
# 修法是**两半一起,而它们是同一个概念**:
#   ① 一条拍顶层写 `"for_each": {"node": "player"}` —— 用 `for_each` 这个词是为了
#      和 `rules.py` / 插件触发器同名同义(那两处都是"对每一个……各跑一遍");
#   ② 声明之后,`payload` / `trigger.when` 里的保留字 `"player"` 指**这一趟展开的
#      那个人**。没声明 `for_each` 时 `"player"` 就只是一个普通 agent id。
# **声明本身就是开关** —— 和 perception、本体层逐字同构,老世界一个字不用改。
PLAYER_TOKEN = "player"
FOR_EACH_NODES = ("player",)

# 一条拍认哪几个顶层键。⚠️ **3.8.0 一个都不查** —— 于是写了 `for_each` 的包在
# 那一版上**开得了机**,拍按世界时响一次、`mark_fired`、烧掉。这个闭集救不了
# 3.8.0(它已经发出去了),它救的是**下一个**不认识的键:让"我又漏了一层"有人喊。
# 🆕 3.10.0(批 1.1 ⑤):`narrate` —— **这一拍响的时候,给玩家看的那一句话。**
#
# 真站实测的那条:第一拍响了(录取通知 + 一部 N96 + 800 块),而屏幕上一个字没提。
# 主持人那一屏的回顾(`host.recap_lines`)想说这件事,可它手上**只有 op 名**
# (`memory` / `pay` / `grant_item`)—— 把「这一拍响了」翻译成一句剧情是**作者的活**,
# 引擎编一句的下场是屏幕上出现一句世界里没有的话,而它读起来像真的。
# 这一格就是作者写下那句话的地方。
BEAT_KEYS = ("id", "for_each", "trigger", "payload", "once", "narrate")

# ── 拍的**零点**:第三种(3.10.0,周更链路 2a-①)──────────────────────────────
#
# 从前只有两种零点:世界第 0 天(默认)与这个玩家入场那天(`for_each: {node: player}`)。
# 第三种是**这条拍所属的那份内容包落地那天** —— 而它不是一个"可选特性",
# 它是**「一个跑着的世界改得了剧情」这件事的前提**:
#
#   `trigger.at` 的语义是「**不早于**」(tick 粒度对不上等号)。于是一份写着
#   `day: 0..6` 的第 2 周包装进一个已经跑到第 40 天的世界,**八拍在同一 tick
#   全部烧掉**,零报错 —— 而这正是引擎从前拒绝合并节拍的唯一正确理由
#   (`RedisBeatsStore.seed` 的 docstring)。
#
# 🔴 **所以零点跟着「这条拍是怎么进来的」走,而不是靠作者记得写一个关键字。**
# 默认必须落在安全的那一边:这一层写错的样子不是报错,是整份剧情在一 tick 里烧完。
#
#   创世那批(世界文件首启装的,没有 pack)     → 世界第 0 天,**逐字如旧**
#   一个 pack 装进来的                          → 那个 pack **第一次**落地那天
#   写了 `for_each: {"node": "player"}`         → 他入场那天(3.9.0 已有)
#   **两者都有**                                → `max(两者)`
#
# 最后一行是承重的,而且**只有 `max` 能同时让两句话成立**:老玩家从包落地起算
# (否则第 2 周的剧情对他永远不响);包落地三天后才进来的新玩家从他自己那天起算
# (否则他一进门就被一堆过期的拍砸中)。
#
# 逃生舱是**显式**的:`trigger.at.since: "world"` = 我要的就是世界第 N 天
# (「世界第 100 天」这种绝对时刻)。不写 = `"pack"` = 按来路。
# ⚠️ 一条不属于任何 pack 的拍写 `since: "pack"`,零点就是世界第 0 天 —— 它本来
# 就是那样。**不报错是有意的**:同一份文件既可能当创世用,也可能当一份包装进去,
# 而作者在写它的时候不知道是哪一种。
AT_SINCE = ("pack", "world")
AT_KEYS = ("day", "minute_of_day", "since")
# ⚠️ **名字里带 `BEAT_` 不是啰嗦**:`plugins.TRIGGER_KEYS` 已经占着这个名字
# (那是插件触发器的键),而 `run_contract` 把两边都 import 进同一个作用域 ——
# 第一版就撞了,契约里那一格印出来的是插件那五个键。**撞了不报错**,
# 它只是安安静静地答另一个问题。
BEAT_TRIGGER_KEYS = ("at", "after", "when")


def day_zero_for(
    beat: dict[str, Any], pack_days: dict[str, int] | None = None,
    join_day: int | None = None,
) -> int:
    """这一趟从哪一天算起 —— **零点只有这一处算得出来**。

    `pack_days` 是 `{拍 id: 它那个包落地那天}`,由 `Projection.packs` 折出来
    (`BeatDirector.due_beats` 自己折,调用方不用传第二份 —— 传第二份就是
    「一份名单和它要判的那份数据来自两次不同的合并」那个形状)。
    """
    at = (beat.get("trigger") or {}).get("at")
    since = (at or {}).get("since") or AT_SINCE[0]
    base = 0 if since == "world" else int((pack_days or {}).get(str(beat.get("id")), 0))
    if join_day is None:
        return base
    return max(base, int(join_day))


def disabled_beats_from(projection: Any) -> set[str]:
    """停用了的那几份包带来的拍(3.10.0,K7)。**它们不再进候选。**

    已经响过的照旧响过 —— `beat_fired` 是历史,而停用管的是朝前看的那一半。
    """
    out: set[str] = set()
    for row in (getattr(projection, "packs", None) or {}).values():
        if not row.get("disabled"):
            continue
        for bid in ((row.get("sections") or {}).get("beats") or ()):
            out.add(str(bid))
    return out


def pack_days_from(projection: Any) -> dict[str, int]:
    """`{拍 id: 它那个包落地那天}` —— 折自 `Projection.packs`,**不另存一份**。"""
    out: dict[str, int] = {}
    for row in (getattr(projection, "packs", None) or {}).values():
        try:
            day = int(row.get("day") or 0)
        except (TypeError, ValueError):
            day = 0
        # 🔴 **每一拍记自己的落地日**(`beat_days`),整包那个 `day` 只是回落。
        # 一个包升级时带的是**新的**几拍,而上一版那几拍的零点不该跟着动 ——
        # 从前它们共用整包一个 `day`,而 `sections` 又被整片替换,于是升级那一刻
        # 上一版的拍**从这张表上消失**,零点读作 0,下一 tick 全烧掉。
        per_beat = row.get("beat_days") or {}
        for bid in ((row.get("sections") or {}).get("beats") or ()):
            try:
                out[str(bid)] = int(per_beat.get(str(bid), day))
            except (TypeError, ValueError):
                out[str(bid)] = day
    return out

# 🔴 **逐个 op、逐个谓词裁「玩家能不能出现在这一格」,而拒绝在加载期当场说。**
# 从前这一层的形状是"不认识就 warning 一句跳过",而那正是上面那个洞的形状。
# 「坏声明一个字都不写」在这一层的落法:**每一格都有结论,没有第三种"静默跳过"**。
#
# 收下的那几格,共同点是**玩家那一头真的有这样东西**:关系表用 `player:<id>` 做
# target、账本和库存按 holder 记账、位置有 `_present_players` 那条路。
# 拒掉的那几格,共同点是**玩家那一头根本没有那样东西**,而写下去只会安静地什么
# 都不发生:玩家没有记忆表(`memory_triggers` 自己写着这句)、没有 persona、
# 没有需求黑板、不走 `agent_join`(唯一窄口是 `World._touch_player`)。
# 关系的**主语**只能是角色:`as: "player"` 拒、`target: "player"` 收 —— 引擎里
# "他对她的看法"是角色那一侧的四轴,不是玩家的。
PLAYER_ALLOWED_OP_FIELDS: dict[str, tuple[str, ...]] = {
    # 🆕 3.10.0:`hail` 的 `target` **只写得下玩家** —— 「角色去找角色搭话」是
    # 行为树那条路的事,不该由剧情拍代劳(那会是第二份"谁去找谁"的判断)。
    "hail": ("target",),
    "sentiment_delta": ("target",),
    "r_type": ("target",),
    "pay": ("from", "to"),
    "grant_item": ("agent_id",),
}
PLAYER_ALLOWED_PREDICATE_FIELDS: dict[str, tuple[str, ...]] = {
    "co_located": ("agents",),
    "money": ("agent",),
    "has_item": ("agent",),
    "sentiment": ("target",),
    "r_type": ("target",),
}
# 拒绝语要说得出**为什么**:一句"不支持"会让作者去试第二种写法,而这几格没有第二种。
PLAYER_REFUSALS: dict[str, str] = {
    "memory": "玩家那一头没有记忆表 —— 记忆是角色的",
    "broadcast_memory": "它写的是在场角色的记忆,玩家不是记忆的主人",
    "persona_update": "玩家没有 persona(他的称呼写在 `player.*` 那一侧)",
    "agent_join": "玩家不走 agent_join —— 唯一窄口是 `World._touch_player`",
    "agent_leave": "玩家的来去是在场,不是世界事件",
    "agent_return": "玩家的来去是在场,不是世界事件",
    "need": "需求住在角色的黑板上,玩家身上没有",
    "as": "关系的主语只能是角色 —— 「他对你的看法」写 `target: \"player\"`",
}


def player_field_error(kind: str, field: str, *, is_op: bool) -> str | None:
    """这一格写 `player` 收不收?收 → `None`;不收 → 一句说得出理由的拒绝语。"""
    table = PLAYER_ALLOWED_OP_FIELDS if is_op else PLAYER_ALLOWED_PREDICATE_FIELDS
    if field in table.get(kind, ()):
        return None
    why = PLAYER_REFUSALS.get(kind) or PLAYER_REFUSALS.get(field) or (
        f"{kind!r} 这一层还没有玩家那一侧的东西"
    )
    return f"{kind!r} 的 {field!r} 写不了 {PLAYER_TOKEN!r}:{why}"


class BeatScriptError(ValueError):
    """A beat script failed validation; carries every error found."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("invalid beat script:\n" + "\n".join(f"- {e}" for e in errors))


class BeatScript:
    """A validated, ordered list of beat dicts."""

    def __init__(self, beats: list[dict[str, Any]]):
        self.beats = beats
        #: 库里存量的拍上那几处 3.10.0 才开始拒的写法。**`doctor` 数它** ——
        #: 一句只写在开机日志里的话,托管环境里没有人读得到。
        self.lenient_warnings: list[str] = []

    @classmethod
    def load(cls, path: str | Path) -> "BeatScript":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BeatScriptError([f"cannot read beat script {path}: {exc}"]) from exc
        return cls.from_data(data)

    @classmethod
    def from_data(cls, data: Any, *, stored: bool = False) -> "BeatScript":
        """把一份脚本读成一个校验过的 `BeatScript`。

        🔴 **`stored=True` = 这几拍已经在这个世界的库里了**(3.10.0,2a-① 验收 B)。

        3.10.0 给 `trigger` / `trigger.at` 加了闭集,而**开机会把库里存量的拍
        重验一遍** —— 于是一个 3.9.0 上跑得好好的世界(它那一版这两层一个键都不查,
        写错一个字母是照收然后丢掉),换上 3.10.0 就 `BOOT FAILED`。
        **一次收紧不许把已经发出去的世界锁在门外。**

        分界是**这几拍是从哪儿来的**:
        - 库里那份(`stored=True`)—— 新加的那几条只**警告**,世界照开;
        - 一份文件 / 一份内容包 —— 照旧**严格**,当场拒。

        ⚠️ **只放宽 3.10.0 新加的那几条**。3.9.0 就会拒的(坏 op、id 重复、
        `for_each` 写错)照旧拒 —— 一个带着那种拍的世界本来就开不了机,
        放宽它等于假装它曾经好过。
        """
        errors, lenient = _validate_script(data, stored=stored)
        if errors:
            raise BeatScriptError(errors)
        for problem in lenient:
            logger.warning(
                "库里这一拍写着 3.10.0 不再认的东西,这一版只警告不拦:%s", problem)
        beats = [dict(b) for b in data["beats"]]
        _warn_unpaired_leaves(beats)
        script = cls(beats)
        script.lenient_warnings = list(lenient)
        return script


# ── validation (strict, load-time) ───────────────────────────────────────────


def _validate_script(data: Any, *, stored: bool = False) -> tuple[list[str], list[str]]:
    """`(拦下来的, 只警告的)`。第二格只在 `stored=True` 时非空(见 `from_data`)。"""
    if not isinstance(data, dict) or not isinstance(data.get("beats"), list):
        return (["script must be an object with a 'beats' list"], [])
    errors: list[str] = []
    lenient: list[str] = []
    ids: set[str] = set()
    beats = data["beats"]
    for i, beat in enumerate(beats):
        label = f"beats[{i}]"
        if not isinstance(beat, dict):
            errors.append(f"{label}: not an object")
            continue
        beat_id = beat.get("id")
        if not isinstance(beat_id, str) or not beat_id:
            errors.append(f"{label}: missing or empty 'id'")
        elif beat_id in ids:
            errors.append(f"{label}: duplicate id {beat_id!r}")
        else:
            ids.add(beat_id)
            label = f"beat {beat_id!r}"
        unknown = sorted(set(beat) - set(BEAT_KEYS))
        if unknown:
            errors.append(
                f"{label}: 不认识的字段 {unknown} —— 一条拍只有 {list(BEAT_KEYS)}"
            )
        if beat.get("once") not in (None, True):
            errors.append(f"{label}: 'once' must be true — repeating beats are not supported (v1)")
        errors.extend(_validate_narrate(beat, label))
        per_player = False
        if "for_each" in beat:
            for_each_errors, per_player = _validate_for_each(beat.get("for_each"), label)
            errors.extend(for_each_errors)
        hard, soft = _validate_trigger(beat.get("trigger"), label,
                                       per_player=per_player, stored=stored)
        errors.extend(hard)
        lenient.extend(soft)
        errors.extend(_validate_payload(beat.get("payload"), label, per_player=per_player))
    errors.extend(_validate_after_graph(beats, ids))
    return (errors, lenient)


def _validate_for_each(for_each: Any, label: str) -> tuple[list[str], bool]:
    """`for_each` 只有一种写法,而它是**闭集**:猜错了不报错才是这一层最贵的错。"""
    if not isinstance(for_each, dict) or set(for_each) != {"node"}:
        return ([f"{label}: 'for_each' 只写得下一格 'node'(例:"
                 '{"node": "player"})'], False)
    node = for_each.get("node")
    if node not in FOR_EACH_NODES:
        return ([f"{label}: for_each.node {node!r} 不认识 —— "
                 f"这一层只收 {list(FOR_EACH_NODES)}"], False)
    return ([], node == PLAYER_TOKEN)


def _validate_narrate(beat: dict[str, Any], label: str) -> list[str]:
    """`narrate` 那一格(3.10.0)。**非空文本,而且只写得在指着玩家的拍上。**

    🔴 **世界级的拍写它是当场拒绝,不是静默无效。** 这一句走的是「玩家的叙事流」
    那条路(`narrative`,`speaker = player:<id>`),而一条世界级的拍没有"那个人" ——
    写下去它谁也到不了。**写下去、开得了机、什么都不发生**正是这一层最贵的那种错
    (`for_each` 这一格 3.8.0 上就是那么烂掉的:照收然后丢掉,拍烧了、零报错)。
    """
    if "narrate" not in beat:
        return []
    said = beat.get("narrate")
    if not isinstance(said, str) or not said.strip():
        return [f"{label}: 'narrate' 要是一段非空文本 —— 它是这一拍响的时候"
                "给玩家看的那句话"]
    for_each = beat.get("for_each")
    node = for_each.get("node") if isinstance(for_each, dict) else None
    if node != PLAYER_TOKEN:
        return [
            f"{label}: 只有写了 `\"for_each\": {{\"node\": \"{PLAYER_TOKEN}\"}}` 的拍"
            "才写得下 'narrate' —— 这一句走的是「那个玩家的叙事流」,"
            "而一条世界级的拍没有「那个人」,写下去它谁也到不了"
        ]
    return []


def _validate_trigger(trigger: Any, label: str, *, per_player: bool = False,
                     stored: bool = False) -> tuple[list[str], list[str]]:
    """`(拦下来的, 只警告的)`。`stored=True` 时 **3.10.0 新加的那三条**只警告。"""
    if not isinstance(trigger, dict) or not any(k in trigger for k in BEAT_TRIGGER_KEYS):
        return ([f"{label}: 'trigger' must contain at least one of at/after/when"], [])
    errors: list[str] = []
    lenient: list[str] = []
    # 3.10.0 新加的那几条进这里;`stored=True` 时它们只是警告。
    new_in_3_10 = lenient if stored else errors
    # 🔴 **闭集,两层都是**(3.10.0)。这两层从前**一个键都不查**,于是 `since`
    # 写下去会被照收然后丢掉 —— 而"不认识的键照收然后丢掉"这一族,这个仓库
    # 一层一层收过五轮,**一层一层收本身就是这个 bug 的形状**。加 `since` 这一格
    # 的同一轮就把它所在的那两层一起收掉。
    unknown = sorted(set(trigger) - set(BEAT_TRIGGER_KEYS))
    if unknown:
        new_in_3_10.append(
            f"{label}: trigger 里不认识的字段 {unknown} —— 只有 {list(BEAT_TRIGGER_KEYS)}"
        )
    at = trigger.get("at")
    if at is not None:
        if isinstance(at, dict):
            unknown_at = sorted(set(at) - set(AT_KEYS))
            if unknown_at:
                new_in_3_10.append(
                    f"{label}: trigger.at 里不认识的字段 {unknown_at} —— "
                    f"只有 {list(AT_KEYS)}"
                )
            since = at.get("since")
            if since is not None and since not in AT_SINCE:
                new_in_3_10.append(
                    f"{label}: trigger.at.since {since!r} 不认识 —— 只收 "
                    f"{list(AT_SINCE)}(`pack` = 从这份内容包落地那天算起,也是缺省;"
                    "`world` = 从世界第 0 天算起)"
                )
        if not isinstance(at, dict) or not isinstance(at.get("day"), int) or at["day"] < 0:
            errors.append(f"{label}: trigger.at needs an integer day >= 0")
        else:
            minute = at.get("minute_of_day", 0)
            if not isinstance(minute, int) or not (0 <= minute < MINUTES_PER_DAY):
                errors.append(f"{label}: trigger.at.minute_of_day must be an integer in [0, {MINUTES_PER_DAY})")
    after = trigger.get("after")
    if after is not None and (not isinstance(after, str) or not after):
        errors.append(f"{label}: trigger.after must be a beat id string")
    when = trigger.get("when")
    if when is not None:
        if not isinstance(when, list):
            errors.append(f"{label}: trigger.when must be a list of predicates")
        else:
            for pred in when:
                errors.extend(_validate_predicate(pred, label, per_player=per_player))
    return (errors, lenient)


def _needs_fields(kind: str) -> tuple[str, ...]:
    """每个谓词的必填字段 —— 与 op 表同一个用途,`contract` 也报它。"""
    return PREDICATE_REQUIRED_FIELDS.get(kind, ())


def _validate_predicate(pred: Any, label: str, *, per_player: bool = False) -> list[str]:
    if not isinstance(pred, dict):
        return [f"{label}: predicate is not an object"]
    kind = pred.get("pred")
    if kind not in _VALID_PREDICATES:
        return [f"{label}: unknown predicate {kind!r} (supported: {sorted(_VALID_PREDICATES)})"]
    errors: list[str] = []
    for field in _needs_fields(kind):
        if field not in pred:
            errors.append(f"{label}: {kind} predicate needs {field!r}")
    for field in ("as", "target", "agent", "need", "item"):
        if field in _needs_fields(kind) and field in pred:
            if not isinstance(pred.get(field), str) or not pred[field]:
                errors.append(f"{label}: {kind} predicate needs a non-empty {field!r}")
    if "op" in _needs_fields(kind) and pred.get("op") not in ("gte", "lte"):
        errors.append(f"{label}: {kind} predicate op must be 'gte' or 'lte'")
    if "value" in _needs_fields(kind) and not isinstance(pred.get("value"), (int, float)):
        errors.append(f"{label}: {kind} predicate needs a numeric 'value'")
    if kind == "need" and pred.get("need") not in (None, *NEED_NAMES):
        errors.append(
            f"{label}: unknown need {pred.get('need')!r} (supported: {sorted(NEED_NAMES)})"
        )
    if kind == "co_located":
        agents = pred.get("agents")
        if (
            not isinstance(agents, list)
            or len(agents) < 2
            or not all(isinstance(a, str) and a for a in agents)
        ):
            errors.append(f"{label}: co_located predicate needs a list of >= 2 agent ids")
    if kind in ("r_type", "memory") and not isinstance(pred.get("contains"), str):
        errors.append(f"{label}: {kind} predicate needs a string 'contains'")
    errors.extend(_player_token_errors(pred, kind, label, is_op=False, per_player=per_player))
    return errors


def _player_token_errors(row: dict[str, Any], kind: str, label: str, *,
                         is_op: bool, per_player: bool) -> list[str]:
    """这一条里写了 `player` 的那几格,逐格判 —— **加载期,不是运行期**。

    没声明 `for_each` 的拍里写 `player` 不在这儿判:那时它只是一个普通 agent id,
    而"这个 id 不在世界里"归 `beat_script_warnings`(只警告不拒绝,因为一条拍
    完全可以先 `agent_join` 一个人再用他)。
    """
    if not per_player:
        return []
    errors: list[str] = []
    fields = ("agent_id", "as", "target", "from", "to", "agent", "location")
    for field in fields:
        if str(row.get(field) or "") != PLAYER_TOKEN:
            continue
        problem = player_field_error(kind, field, is_op=is_op)
        if problem:
            errors.append(f"{label}: {problem}")
    for who in row.get("agents") or []:
        if str(who or "") != PLAYER_TOKEN:
            continue
        problem = player_field_error(kind, "agents", is_op=is_op)
        if problem:
            errors.append(f"{label}: {problem}")
    return errors


_OP_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "memory": ("agent_id", "summary"),
    "broadcast_memory": ("location", "summary"),
    "sentiment_delta": ("as", "target", "delta"),
    "r_type": ("as", "target"),
    "persona_update": ("agent_id", "spec"),
    # `target` 只写得下 `player`(见 `PLAYER_ALLOWED_OP_FIELDS`);`line` 可选。
    "hail": ("agent_id", "target"),
    "location_desc": ("location", "description"),
    "agent_leave": ("agent_id",),
    "agent_return": ("agent_id", "location"),
    "pay": ("from", "to", "amount"),
    "grant_item": ("agent_id", "item_id"),
}


# 对外自述(`anima-world contract`)用的完整表。`agent_join` 的必填是一个 agent
# 对象(形状 = 种子里的 agent 条目),它走 `_validate_agent_bundle` 而不是通用的
# 必填字段循环 —— 但契约面上不该因为实现分了两条路就缺一格,镜像端会照着这份
# 表去写自己的校验。`VALID_OPS` 与它必须逐项对齐(test_contract_command.py 守着)。
OP_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    **_OP_REQUIRED_FIELDS,
    "agent_join": ("agent",),
}


def _validate_payload(payload: Any, label: str, *, per_player: bool = False) -> list[str]:
    if not isinstance(payload, list) or not payload:
        return [f"{label}: 'payload' must be a non-empty list of ops"]
    errors: list[str] = []
    for j, op in enumerate(payload):
        op_label = f"{label}.payload[{j}]"
        if not isinstance(op, dict):
            errors.append(f"{op_label}: not an object")
            continue
        kind = op.get("op")
        if kind not in VALID_OPS:
            errors.append(f"{op_label}: unknown op {kind!r} (supported: {sorted(VALID_OPS)})")
            continue
        for field in _OP_REQUIRED_FIELDS.get(kind, ()):
            if field not in op:
                errors.append(f"{op_label}: op {kind!r} requires field {field!r}")
        if kind == "sentiment_delta" and not isinstance(op.get("delta"), (int, float)):
            errors.append(f"{op_label}: sentiment_delta 'delta' must be numeric")
        if kind == "r_type" and "r_type" not in op and "r_type_back" not in op:
            errors.append(f"{op_label}: r_type op needs r_type and/or r_type_back")
        if kind == "persona_update" and not isinstance(op.get("spec"), dict):
            errors.append(f"{op_label}: persona_update 'spec' must be an object")
        if kind == "hail":
            # 🔴 **`target` 只写得下保留字 `player`。** 两个理由,都在加载期就能说:
            # ① 世界文件写在玩家出现之前,**一条拍写不出玩家的 id**;
            # ② 「角色去找角色搭话」是行为树那条路的事,由剧情拍代劳就是第二份
            #    「谁去找谁」的判断。放行的样子是安静的:运行期一句 warning 跳过,
            #    而这一拍照旧 `mark_fired` —— **永久失效,且重启不重放**
            #    (`for_each` 那个洞逐字同一种形状)。
            if str(op.get("target") or "") != PLAYER_TOKEN:
                errors.append(
                    f"{op_label}: hail 的 'target' 只写得下 {PLAYER_TOKEN!r} —— "
                    "世界文件写在玩家出现之前,一条拍写不出他的 id;而"
                    "「角色去找角色搭话」走的是行为树那条路,不由剧情拍代劳"
                )
            if not str(op.get("agent_id") or "").strip():
                errors.append(f"{op_label}: hail 少了 'agent_id'(谁来找他)")
            line = op.get("line")
            if line is not None and not isinstance(line, str):
                errors.append(f"{op_label}: hail 的 'line' 要是一段文本")
        if kind in ("memory", "broadcast_memory"):
            errors.extend(_validate_memory_fields(op, op_label))
        if kind == "agent_join":
            errors.extend(_validate_agent_bundle(op.get("agent"), op_label))
        errors.extend(_player_token_errors(op, kind, op_label, is_op=True,
                                           per_player=per_player))
    return errors


def _validate_memory_fields(mem: Any, label: str) -> list[str]:
    """Numeric/typed checks shared by memory ops and agent_join.memories —
    a bad importance must fail at LOAD, not as a float() at fire time (the
    record loop runs outside the per-op guard)."""
    errors: list[str] = []
    if "importance" in mem and not isinstance(mem.get("importance"), (int, float)):
        errors.append(f"{label}: 'importance' must be numeric")
    if "summary" in mem and not isinstance(mem.get("summary"), str):
        errors.append(f"{label}: 'summary' must be a string")
    return errors


def _validate_agent_bundle(bundle: Any, label: str) -> list[str]:
    """agent_join sub-structures (relations/memories/goals) are validated as
    strictly as top-level ops — their expansion events bypass the per-op
    guard once recorded, so garbage must never load."""
    if not isinstance(bundle, dict) or not _AGENT_BUNDLE_KEYS.issubset(bundle):
        return [f"{label}: agent_join needs an 'agent' object with {sorted(_AGENT_BUNDLE_KEYS)}"]
    errors: list[str] = []
    goals = bundle.get("goals")
    if goals is not None and not isinstance(goals, (str, list)):
        errors.append(f"{label}: agent.goals must be a string or list")
    duties = bundle.get("duties")
    if duties is not None and not isinstance(duties, list):
        errors.append(f"{label}: agent.duties must be a list")
    relations = bundle.get("relations")
    if relations is not None:
        if not isinstance(relations, list):
            errors.append(f"{label}: agent.relations must be a list")
        else:
            for k, rel in enumerate(relations):
                rel_label = f"{label}.agent.relations[{k}]"
                if not isinstance(rel, dict) or not isinstance(rel.get("with"), str) or not rel["with"]:
                    errors.append(f"{rel_label}: needs a non-empty 'with' agent id")
                elif "sentiment" in rel and not isinstance(rel["sentiment"], (int, float)):
                    errors.append(f"{rel_label}: 'sentiment' must be numeric")
    memories = bundle.get("memories")
    if memories is not None:
        if not isinstance(memories, list):
            errors.append(f"{label}: agent.memories must be a list")
        else:
            for k, mem in enumerate(memories):
                mem_label = f"{label}.agent.memories[{k}]"
                if not isinstance(mem, dict) or not isinstance(mem.get("summary"), str):
                    errors.append(f"{mem_label}: needs a string 'summary'")
                else:
                    errors.extend(_validate_memory_fields(mem, mem_label))
    # 角色卡走**和作者层同一份判断**(`character_card.card_errors`):中途入场的人
    # 一样要出现在玩家的通讯录里,而两份判断迟早给出不同答案。
    if "card" in bundle:
        from anima_world.character_card import card_errors

        errors.extend(card_errors(bundle.get("card"), label=f"{label}.agent"))
    return errors


_OP_AGENT_FIELDS = ("agent_id", "as", "target")


def beat_script_warnings(
    data: Any,
    *,
    known_agents: Iterable[str] = (),
    known_locations: Iterable[str] = (),
) -> list[str]:
    """脚本引用了世界里不存在的东西 —— 一行一条,**只警告不拒绝**。

    为什么不能拒绝:一个 beat 完全可以先 `agent_join` 一个新角色,后面的 beat 再对
    他做事;那时 `known_agents` 里当然没有他。同理,节拍可以指向种子之外的地点。
    把这类检查升级成加载期错误,会让设计正确的脚本在升级后开不了机。

    但沉默也不行:引用错一个 id,那个 beat 会**静默作废并且被永久标记已触发**
    (`beat_fired` 是历史,重启不重放)—— 剧情就这么没了,而且不可挽回。
    """
    if not isinstance(data, dict):
        return []
    beats = data.get("beats")
    if not isinstance(beats, list):
        return []

    agents = set(known_agents)
    # 脚本自己引进来的角色也算数,否则"先入场再使用"会被误报。
    for beat in beats:
        for op in (beat.get("payload") or []) if isinstance(beat, dict) else []:
            if isinstance(op, dict) and op.get("op") == "agent_join":
                bundle = op.get("agent")
                if isinstance(bundle, dict) and bundle.get("id"):
                    agents.add(str(bundle["id"]))
    locations = set(known_locations)

    warnings: list[str] = []

    def _check(kind: str, value: Any, pool: set[str], label: str) -> None:
        name = str(value or "")
        if not pool or not name or name in pool:
            return
        warnings.append(f"{label}: {kind} {name!r} 不在这个世界里(已知:{sorted(pool)})")

    for index, beat in enumerate(beats):
        if not isinstance(beat, dict):
            continue
        label = f"beats[{index}] ({beat.get('id', '?')})"
        # `for_each` 的拍里,`player` 是**保留字**不是 id —— 报它"不在这个世界里"
        # 是一句假警告,而假警告的代价是作者去改一行没错的东西(那条教训这个仓库
        # 记过:「拒绝语指错病灶,和没有拒绝语一样贵」)。收不收这一格由加载期那张
        # 表判,这里一个字都不该说。
        # ⚠️ **每条拍一份**,不是就地改那个共享的集合:一条 per-player 拍之后,
        # 后面那些普通拍会跟着一起不报 —— 一次放行悄悄放行了它后面的所有人。
        pool = (agents | {PLAYER_TOKEN}) if is_per_player(beat) else agents
        for pred in (beat.get("trigger") or {}).get("when") or []:
            if not isinstance(pred, dict):
                continue
            for key in ("as", "target"):
                _check("角色", pred.get(key), pool, f"{label}.trigger")
            for who in pred.get("agents") or []:
                _check("角色", who, pool, f"{label}.trigger")
        for j, op in enumerate(beat.get("payload") or []):
            if not isinstance(op, dict):
                continue
            op_label = f"{label}.payload[{j}] {op.get('op', '?')}"
            if op.get("op") != "agent_join":
                for key in _OP_AGENT_FIELDS:
                    _check("角色", op.get(key), pool, op_label)
            _check("地点", op.get("location"), locations, op_label)
    return warnings


def split_against_stored(
    authored: list[dict[str, Any]], stored: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    """这份文件里的拍,对着库里那份分成三堆:`(一模一样的, 同 id 改过的, 新增的)`。

    🔴 **这个函数存在的理由是一次线上开不了机**(3.10.1,2026-09-02)。
    3.10.0 把「文件里有 beat 而世界已经有 beat」判成当场拒绝(退出码 2),
    理由是对的 —— 一份写着 `day: 0..6` 的第 2 周包装进一个跑到第 40 天的世界,
    那几拍会在同一 tick 里全部烧掉。**但那个判据取错了**:它问的是
    `beats_store.seed()` 有没有播下去,而 `seed()` 的语义是「空的时候才播」——
    于是**「同一份文件第二次开机」和「一份带着新剧情的包」在它眼里长得一模一样**。
    而舰队每次开机都带 `--world-file`,所以第二次开机起,那个世界**再也起不来了**。

    正确的判据是**逐拍比**,而三堆各有各的正确反应:

    - **一模一样的** —— 同一份文件又开了一次机。什么都不做,rc 0。
      这是舰队上的常态,它一个字都不该说得像出了事。
    - **同 id 改过的** —— 作者改了一拍的内容,而库里那份和 `beat_fired` 那份历史
      已经配好对了。**说一句,但不拒绝开机**:库里那份说了算(`:beats` 那条
      「之后这里的行说了算」的契约),而作者需要知道他的改动没生效。
    - **新增的** —— 这才是 3.10.0 那条拒绝真正要挡的东西:一份**新**剧情正试图
      走 `--world-file` 混进一个跑着的世界,而它的零点会是世界第 0 天。
      照旧 rc 2,并指向 `pack install`。

    ⚠️ **比的是「按 JSON 规范化之后的字节」,不是 `==`**:同一份文件读两次,
    键序可能不同(dict 的字面量顺序进不了 JSON 的语义),而 `==` 对 dict 本来就
    不看顺序 —— 这里用 `sort_keys` 的 dumps 是为了让**嵌套 list 里的 dict**
    也按同一把尺比,并且让"改过没有"这件事有一个可以印出来的形状。
    """
    def _norm(beat: Any) -> str:
        try:
            return json.dumps(beat, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            # 序列化不了的拍照旧算"改过" —— 猜"没变"会让一份坏拍安静地留在库里。
            return repr(beat)

    have = {}
    for beat in stored:
        if isinstance(beat, dict) and beat.get("id"):
            have[str(beat["id"])] = _norm(beat)
    same: list[str] = []
    changed: list[str] = []
    added: list[str] = []
    for beat in authored:
        if not isinstance(beat, dict) or not beat.get("id"):
            # id 都没有的拍归"新增" —— 严格校验器会在别处拦它,而这里**不许**
            # 把它算成"一样的"然后放行。
            added.append(str((beat or {}).get("id") or "?"))
            continue
        bid = str(beat["id"])
        if bid not in have:
            added.append(bid)
        elif have[bid] == _norm(beat):
            same.append(bid)
        else:
            changed.append(bid)
    return (same, changed, added)


def is_per_player(beat: dict[str, Any]) -> bool:
    """这一条拍是不是"对每个玩家各跑一遍"的。"""
    for_each = beat.get("for_each")
    return isinstance(for_each, dict) and for_each.get("node") == PLAYER_TOKEN


_PLAYER_BINDABLE_FIELDS = ("agent_id", "as", "target", "from", "to", "agent")


def bind_player(row: dict[str, Any], subject: str) -> dict[str, Any]:
    """把保留字 `player` 换成**这一趟的那个人**(`player:<id>`)。

    ⚠️ **换的是 `player:<id>` 这个形状,不是 `agent:player:<id>`。** 两个形状在这个
    引擎里都真实存在,而它们各管一头:量表按 `agent:player:<id>` 存(`me_*` 读的
    是"一个人身上的量",这件事对两种人是同一件),而**关系、账本、库存、事件顶层
    的 `who`、在场位置一律是 `player:<id>`** —— 节拍的谓词和 op 读的全是后面这些。
    挑错一个,每一条都安静地不成立。

    `subject` 是空串(世界级的拍)时原样返回,一个字都不碰。
    """
    if not subject:
        return row
    out = dict(row)
    for field in _PLAYER_BINDABLE_FIELDS:
        if str(out.get(field) or "") == PLAYER_TOKEN:
            out[field] = subject
    agents = out.get("agents")
    if isinstance(agents, list):
        out["agents"] = [subject if str(a or "") == PLAYER_TOKEN else a for a in agents]
    return out


def _validate_after_graph(beats: list[Any], ids: set[str]) -> list[str]:
    """`after` refs must point at existing beats and must not form a cycle."""
    errors: list[str] = []
    after_of: dict[str, str] = {}
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        beat_id, trigger = beat.get("id"), beat.get("trigger")
        after = trigger.get("after") if isinstance(trigger, dict) else None
        if not isinstance(beat_id, str) or not isinstance(after, str) or not after:
            continue
        if after not in ids:
            errors.append(f"beat {beat_id!r}: trigger.after references unknown beat {after!r}")
        elif after == beat_id:
            errors.append(f"beat {beat_id!r}: trigger.after forms a cycle (references itself)")
        else:
            after_of[beat_id] = after
    for start in after_of:
        seen = {start}
        node = after_of.get(start)
        while node is not None:
            if node in seen:
                errors.append(f"beat {start!r}: trigger.after forms a cycle")
                break
            seen.add(node)
            node = after_of.get(node)
    return errors


def _warn_unpaired_leaves(beats: list[dict[str, Any]]) -> None:
    """An `agent_leave` with no `agent_return` anywhere in the script is
    usually an authoring slip — but permanent departure is legitimate drama,
    so this warns and never blocks (agent-leave-return D5)."""
    left: set[str] = set()
    returned: set[str] = set()
    for beat in beats:
        for op in beat.get("payload", []):
            if op.get("op") == "agent_leave":
                left.add(op.get("agent_id"))
            elif op.get("op") == "agent_return":
                returned.add(op.get("agent_id"))
    for agent_id in sorted(left - returned):
        logger.warning("beat script: agent_leave for %r has no agent_return — permanent departure", agent_id)


# ── trigger evaluation (runtime, degrade-never-raise) ────────────────────────


def trigger_ready(
    beat: dict[str, Any],
    now: WorldTime,
    fired: set[tuple[str, str]],
    projection: Projection,
    agent_locs: dict[str, str],
    reader: Any | None = None,
    *,
    subject: str = "",
    day_zero: int = 0,
) -> bool:
    """All trigger conditions ANDed. `at` means "no earlier than" — the tick
    granularity (5 world minutes by default) would miss an equality match.
    A predicate that errors reads as not-met: firing wrongly is worse than
    firing late, and the beat retries every tick anyway.

    🔴 **`day_zero` 是"这一趟从哪一天算起"**(3.9.0)。世界级的拍是 0,也就是从前
    那个样子;而 `for_each: {"node": "player"}` 的拍从**这个玩家第一次进这个世界
    那一天**算起。理由是老板会在世界第 40 天点进来,而他要拿到的是**他的**第 1 天
    那封信,不是一条 39 天前就烧掉的拍 —— 一份"新手第一周"按世界时写,只对第一个
    玩家成立,对之后每一个人都是空的。
    """
    trigger = beat.get("trigger") or {}
    at = trigger.get("at")
    if at is not None:
        due = (int(at.get("day", 0)) + int(day_zero), int(at.get("minute_of_day", 0)))
        if (now.day, now.minute_of_day) < due:
            return False
    after = trigger.get("after")
    if after is not None:
        # 要等的那一拍:**要么是为我响的,要么是世界级的**。只认前者的话,
        # 一条挂在世界级开场后面的玩家拍永远等不到;只认后者的话,
        # 两个玩家的第二拍会被第一个人的第一拍解锁。
        if (after, subject) not in fired and (after, "") not in fired:
            return False
    for pred in trigger.get("when") or []:
        try:
            if not _eval_predicate(bind_player(pred, subject), projection, agent_locs, reader):
                return False
        except Exception:  # noqa: BLE001 - a broken predicate must not stop the world
            logger.warning(
                "beat %r: predicate %r failed to evaluate — treated as not met",
                beat.get("id"), pred, exc_info=True,
            )
            return False
    return True


def _compare(value: float, pred: dict[str, Any]) -> bool:
    threshold = float(pred["value"])
    return value >= threshold if pred.get("op") == "gte" else value <= threshold


def _eval_predicate(
    pred: dict[str, Any],
    projection: Projection,
    agent_locs: dict[str, str],
    reader: Any | None = None,
) -> bool:
    """一个谓词的求值。读不到的东西一律读作"未满足" —— 与运行期降级同一条规矩:
    宁可晚触发,不可错触发,而且下个 tick 还会再试。"""
    kind = pred.get("pred")
    if kind == "sentiment":
        rel = projection.relations.get((pred["as"], pred["target"]))
        return _compare(rel.sentiment if rel is not None else 0.0, pred)
    if kind == "co_located":
        # 用活黑板的位置,不用投影。**理由不是"投影不追落地"**(1.1.1 起它追了)——
        # 是投影不知道"在途":transit 是纯内存态,重启即丢。两个正在赶路的人若按
        # 投影算就成了还在起点同处一室,节拍照常触发、还写进记忆。
        # 与 `Scheduler._is_colocated` 同一条规矩:在途即不在场。
        locs = set()
        for agent_id in pred["agents"]:
            loc = agent_locs.get(agent_id)
            if not loc:
                return False
            locs.add(loc)
        return len(locs) == 1
    if kind == "r_type":
        rel = projection.relations.get((pred["as"], pred["target"]))
        return bool(rel is not None and str(pred["contains"]) in (rel.r_type or ""))
    if kind == "money":
        return _compare(float(projection.balances.get(pred["agent"], 0.0)), pred)
    if kind == "has_item":
        held = projection.inventories.get(pred["agent"], {})
        return int(held.get(pred["item"], 0)) >= int(pred.get("min", 1))
    if kind == "need":
        if reader is None:
            return False
        value = reader.need(str(pred["agent"]), str(pred["need"]))
        return False if value is None else _compare(float(value), pred)
    if kind == "memory":
        if reader is None:
            return False
        needle = str(pred["contains"])
        return any(needle in text for text in reader.memories(str(pred["agent"])))
    return False


# ── payload expansion (pure ops only) ────────────────────────────────────────


# 目标之间的分隔符。作者(和替作者写目标的那个模型)拿它们串一行,
# 而 `{goals}` 是逐条渲染的 —— 不拆开的话,一整行会变成"一个目标"。
_GOAL_SPLIT = re.compile(r"[；;、\n]+")


def _split_goal_text(text: str) -> list[str]:
    return [part.strip() for part in _GOAL_SPLIT.split(text) if part.strip()]


def _looks_char_split(items: list[str]) -> bool:
    """这份 goals 是不是一整句话被按**字**拆开的结果。

    判据要窄:元素多(≥4)、而且**每一个都只有一个字**。真目标没有一个字的
    ("学会放手"最短也四个字),更不会一连四个都是。窄到这个程度之后,误判需要
    一个作者真的写下 `["静", "默", "等", "待"]` —— 那本来也是一份坏数据。
    """
    return len(items) >= 4 and all(len(item.strip()) == 1 for item in items)


def coerce_goals(value: Any) -> list[Any]:
    """把作者写的 goals 收成一列**人读得出**的目标。

    两种坏形状,同一个下场:`{goals}` 直接进 planner 的提示词,而它是逐条渲染的。

    1. 一整个字符串(`"摆脱母亲的控制；重新定义自己的人生"`)。对它做 `list(...)`
       会按字符拆开;此前这里只挡住了这一步,把整行当成**一个**目标 —— 不炸,
       但那一条里塞着两个目标,分隔符原样进提示词。现在按分隔符拆开。
    2. **已经被按字拆开的一列**(`["摆","脱","母","亲",…]`)。这一形状是
       `list[str]`,合法得挑不出毛病,于是它一路畅通无阻地进了世界:线上那个
       897e282865f5 九个角色全中,每人背着十几二十个单字目标,而 planner 每天
       照着它排一天的日子。上游(创作台 `concept.py` 的 `_short_lines`)已经
       堵住了产出侧,但**引擎这一头当时一声不吭地收下了** —— 这正是这个仓库
       最怕的那类:照跑、日志干净、作者三个月后才发现。

    修法是把字拼回去再按分隔符拆:`["摆","脱",…,"；","重",…]` → 拼 →
    `"摆脱母亲的控制；重新定义自己的人生"` → 拆 → 两条。拼接是**无损**的,
    所以这不是猜一个答案出来,是把丢掉的那一步倒回去。
    """
    if isinstance(value, str):
        return _split_goal_text(value)
    if isinstance(value, (list, tuple)):
        items = [item for item in value]
        texts = [str(item) for item in items]
        if _looks_char_split(texts):
            repaired = _split_goal_text("".join(texts))
            logger.warning(
                "goals 是一列单字(%d 条)—— 按字拆开的痕迹,已拼回并重新拆成 %d 条:%s",
                len(texts), len(repaired), repaired,
            )
            return repaired
        # 一条里塞着分隔符的照样拆开(模型常把两个目标写进同一个数组元素),
        # 非字符串的元素原样留着 —— 拆它没有意义,而丢它是另一种静默的少装。
        out: list[Any] = []
        for item in items:
            if isinstance(item, str):
                out.extend(_split_goal_text(item))
            else:
                out.append(item)
        return out
    return []


def memory_seed_event(agent_id: str, mem: dict[str, Any], default_kind: str = "beat") -> dict[str, Any]:
    """One `memory_seed` event, payload identical to rich-injection's genesis
    shape (consumed by MemoryStore via Scheduler._apply_memory_trigger)."""
    return {
        "type": "memory_seed",
        "who": agent_id,
        "payload": {
            "agent_id": agent_id,
            "kind": str(mem.get("kind", default_kind)),
            "summary": str(mem.get("summary", "")),
            "importance": float(mem.get("importance", 0.5)),
            "anchor": bool(mem.get("anchor", False)),
        },
    }


def expand_relations(
    agent_id: str, relations: list[dict[str, Any]], known_agents: set[str]
) -> list[dict[str, Any]]:
    """Symmetric relation-seed events for a joining agent, genesis-style
    (_seed_relations): both directions, absolute sentiment. Values are
    coerced HERE, inside the per-op guard — a bad value must fail during
    expansion, never in the unguarded record loop. Payloads carry
    `seed: True` so TriggerEngine knows this is exogenous backfill, not a
    lived relationship swing (no relation_shift memory, no friendship edge)."""
    events: list[dict[str, Any]] = []
    for rel in relations:
        other = rel.get("with")
        if other not in known_agents:
            logger.warning("beat agent_join relation references unknown agent %r — skipping", other)
            continue
        if "sentiment" in rel:
            sentiment = float(rel["sentiment"])
            for as_id, target_id in ((agent_id, other), (other, agent_id)):
                events.append({
                    "type": "state_change",
                    "who": as_id,
                    "payload": {"kind": "sentiment", "as": as_id, "target": target_id,
                                "sentiment": sentiment, "seed": True},
                })
        if "r_type" in rel or "r_type_back" in rel:
            fwd = str(rel.get("r_type", "acquaintance"))
            back = str(rel.get("r_type_back", "acquaintance"))
            for as_id, target_id, f, b in ((agent_id, other, fwd, back), (other, agent_id, back, fwd)):
                events.append({
                    "type": "state_change",
                    "who": as_id,
                    "payload": {"kind": "r_type", "as": as_id, "target": target_id,
                                "r_type": f, "r_type_back": b, "seed": True},
                })
    return events


def expand_event_op(
    op: dict[str, Any], *, agent_locs: dict[str, str], known_agents: set[str]
) -> list[dict[str, Any]]:
    """Expand one EVENT_OPS op into raw event dicts (Scheduler records them).

    Every event type here already exists: `memory_seed` (rich-injection),
    `sentiment_delta` (llm-relationship-judge), `r_type`/`persona_update`
    (genesis injection / M6). An op referencing an unknown agent expands to
    nothing, with a warning — runtime degrade, never raise (D4).
    """
    kind = op.get("op")

    if kind == "memory":
        agent_id = op.get("agent_id")
        if agent_id not in known_agents:
            logger.warning("beat memory op references unknown agent %r — skipping", agent_id)
            return []
        return [memory_seed_event(agent_id, op)]

    if kind == "broadcast_memory":
        location = op.get("location")
        witnesses = [aid for aid, loc in agent_locs.items() if loc == location]
        if not witnesses:
            logger.warning("beat broadcast_memory at %r found no witnesses — nothing injected", location)
            return []
        return [memory_seed_event(aid, op) for aid in witnesses]

    if kind == "pay":
        src, dst = str(op.get("from") or ""), str(op.get("to") or "")
        try:
            amount = float(op.get("amount"))
        except (TypeError, ValueError):
            logger.warning("beat pay op has a non-numeric amount %r — skipping", op.get("amount"))
            return []
        for holder in (src, dst):
            if holder not in known_agents and holder not in (_TOWN_HOLDER, BEAT_WORLD_HOLDER):
                logger.warning("beat pay references unknown holder %r — skipping", holder)
                return []
        if amount <= 0:
            # 投影对 amount<=0 是 no-op(payment 只加不减),所以负数不是"反向转账",
            # 是一条什么也不做的事件。作者以为钱转了,其实没有 —— 明说。
            logger.warning("beat pay amount must be > 0 (got %r) — skipping;反向转账请把 from/to 调过来", amount)
            return []
        return [{
            "type": "payment", "who": dst,
            "payload": {"from": src, "to": dst, "amount": amount,
                        "reason": str(op.get("reason") or "beat")},
        }]

    if kind == "grant_item":
        agent_id = str(op.get("agent_id") or "")
        if agent_id not in known_agents:
            logger.warning("beat grant_item references unknown agent %r — skipping", agent_id)
            return []
        try:
            qty = int(op.get("qty", 1))
        except (TypeError, ValueError):
            qty = 0
        if qty == 0:
            logger.warning("beat grant_item qty must be non-zero — skipping")
            return []
        source = str(op.get("from") or BEAT_WORLD_HOLDER)
        # qty 为负 = 拿走。投影只认正数,所以调换两端而不是发一条 no-op。
        holder_from, holder_to = (source, agent_id) if qty > 0 else (agent_id, source)
        return [{
            "type": "item_transfer", "who": agent_id,
            "payload": {"from": holder_from, "to": holder_to,
                        "item_id": str(op.get("item_id")), "qty": abs(qty)},
        }]

    if kind == "sentiment_delta":
        as_id, target = op.get("as"), op.get("target")
        for aid in (as_id, target):
            if aid not in known_agents:
                logger.warning("beat sentiment_delta references unknown agent %r — skipping", aid)
                return []
        return [{
            "type": "state_change",
            "who": as_id,
            "payload": {"kind": "sentiment_delta", "as": as_id, "target": target,
                        "delta": float(op.get("delta", 0.0))},
        }]

    if kind == "r_type":
        as_id, target = op.get("as"), op.get("target")
        for aid in (as_id, target):
            if aid not in known_agents:
                logger.warning("beat r_type references unknown agent %r — skipping", aid)
                return []
        payload: dict[str, Any] = {"kind": "r_type", "as": as_id, "target": target}
        if "r_type" in op:
            payload["r_type"] = str(op["r_type"])
        if "r_type_back" in op:
            payload["r_type_back"] = str(op["r_type_back"])
        return [{"type": "state_change", "who": as_id, "payload": payload}]

    if kind == "persona_update":
        agent_id = op.get("agent_id")
        if agent_id not in known_agents:
            logger.warning("beat persona_update references unknown agent %r — skipping", agent_id)
            return []
        spec = dict(op.get("spec") or {})
        if "goals" in spec:
            spec["goals"] = coerce_goals(spec["goals"])
        return [{
            "type": "state_change",
            "who": agent_id,
            "payload": {"kind": "persona_update", "spec": spec},
        }]

    logger.warning("beat op %r is not an event op — skipping", kind)
    return []


# ── the director ─────────────────────────────────────────────────────────────


class BeatDirector:
    """Tracks which beats have fired and answers "what is due now?".

    Owned by the Scheduler and only ever called under its lock. The fired-set
    starts from replayed `beat_fired` events (recovery) — the script file and
    the log are aligned by beat id, so an already-fired id never re-fires and
    a newly added id participates normally (D1).
    """

    def __init__(self, script: BeatScript, fired: set[tuple[str, str]] | None = None):
        self.script = script
        # 🔴 **键是 `(拍 id, 主语)`,不再只是拍 id**(3.9.0)。主语是空串 = 世界级
        # (从前那个样子,逐字不变);`player:<id>` = 这一拍为这个人响过了。
        # 一份"新手第一周"要对**每一个**玩家各响一次,而按拍 id 记账的话,
        # 第一个玩家的到来就把整份剧情烧光了 —— 而且 `beat_fired` 是历史,
        # 重启不重放,烧光了就再也回不来。
        self.fired: set[tuple[str, str]] = {
            item if isinstance(item, tuple) else (item, "") for item in (fired or ())
        }
        # Membership, not arithmetic: the replayed fired-set may contain ids
        # from an earlier script version that this script no longer has —
        # counting them made has_pending() short-circuit a script whose own
        # beats had never fired (caught by agent-leave-return's restart test).
        self._per_player_ids = {b["id"] for b in script.beats if is_per_player(b)}
        self._pending: set[str] = (
            {b["id"] for b in script.beats if not is_per_player(b)}
            - {bid for bid, subject in self.fired if not subject}
        )

    def due_beats(
        self, now: WorldTime, projection: Projection, agent_locs: dict[str, str],
        reader: Any | None = None, players: dict[str, int] | None = None,
    ) -> list[tuple[dict[str, Any], str]]:
        """这一 tick 该响的 `(拍, 主语)`。世界级的主语是空串。

        `players` 是 `{player_id: 他进这个世界那一天}`,由调度器给
        (`Projection.players_joined`)。**空的话每一条 per-player 拍都不响** ——
        而"不响"不等于"作废":它没被 `mark_fired`,人来了再说。

        Computed against the fired-set as of NOW: a beat whose `after`
        prerequisite fires this same tick becomes due on the next one.
        """
        due: list[tuple[dict[str, Any], str]] = []
        # 🔴 **一次折出来,不缓存**(3.10.0):`{拍 id: 它那个包落地那天}` 折自
        # 同一份 `projection` —— 也就是这一层判「该不该响」用的那一份。
        # 存第二份的下场是"零点"和"这一拍响没响"来自两次不同的合并,而这个仓库
        # 为那个形状红过一次(2026-08-28 的插件命名空间回归)。
        pack_days = pack_days_from(projection)
        # **停用了的那几拍不再进候选**(K7)。折自同一份 `projection`,
        # 和零点那张表来自同一次合并 —— 两次不同的合并是这个仓库红过的形状。
        disabled = disabled_beats_from(projection)
        for beat in self.script.beats:
            if str(beat.get("id")) in disabled:
                continue
            if is_per_player(beat):
                for player_id, join_day in sorted((players or {}).items()):
                    subject = f"player:{player_id}"
                    if (beat["id"], subject) in self.fired:
                        continue
                    if trigger_ready(
                        beat, now, self.fired, projection, agent_locs, reader,
                        subject=subject,
                        day_zero=day_zero_for(beat, pack_days, int(join_day)),
                    ):
                        due.append((beat, subject))
                continue
            if (beat["id"], "") in self.fired:
                continue
            if trigger_ready(beat, now, self.fired, projection, agent_locs, reader,
                             day_zero=day_zero_for(beat, pack_days)):
                due.append((beat, ""))
        return due

    def mark_fired(self, beat_id: str, subject: str = "") -> None:
        self.fired.add((beat_id, subject))
        if not subject:
            self._pending.discard(beat_id)

    def has_pending(self) -> bool:
        """False once every beat OF THIS SCRIPT has fired — the scheduler's
        short-circuit so an exhausted script costs nothing per tick.

        ⚠️ **有 per-player 拍的脚本永远 pending**,而这不是漏了短路:那种拍等的是
        **还没来的人**,而"这个世界以后不会再有新玩家"是引擎无从知道的一件事。
        真正的省钱在上面 —— 名册空的时候那一层一条都不算。
        """
        return bool(self._pending or self._per_player_ids)
