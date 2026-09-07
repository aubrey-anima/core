"""对人动词:玩家对**一个人**做一件事(3.12.0,批 3b · 裁决 §2.6)。

「拜师」「决斗」「说服」——这一族和 `interact` 长得像,而它们**永不走 affordance**。

## 为什么不走 affordance(插件设计 §10 第 3 期裁决,照抄)

`interact` 的定义是「一个人、一个东西、一个瞬间」,而它整条路上没有一处问过
**对方肯不肯**。把人塞进 `target` 那一格,等于让「拉着谁就一起吃饭」成立 ——
而这个引擎的全部立场是**角色自己做决定**(`together.py` 红线 1 逐字)。

所以它走**工具路**(和 `walk_away` / `contact` 同一条),执行共用
`apply_affordance` 那个求值器(代价、材料、时间照旧算),而中间多**一道门**。

## 🔴 那道门的方向,和编剧那道**正相反** —— 这一条必须写死

| | 谁发起 | 同意门 | 为什么 |
|---|---|---|---|
| 编剧派人来(3a) | **世界** | 只挡出戏的极端(hidden / forbidden / 四项硬闸 / 频率);**`willingness` 有意不用** | GM 说 NPC 进门,NPC 就进门 |
| 玩家的对人动词(本层) | **玩家** | **要 `willingness` / `judge_invite` 那条真门** | 「我要拜师」是一次请求,她有真正的否决权 |

**两道门语义相反,不合并;但共用 `together.GATE_LABELS` 那张硬闸表** ——
世界说不行的那几条(睡着了 / 在赶路 / 手上有事 / 不在这儿 / 把你静音了)对谁都一样。

⚠️ **不写清楚的话,下一个人一定会照其中一处去「统一」另一处,而两处都不会报错。**

## 三条这一层的纪律

1. **拒绝语是她的话,不是系统话。** 「她不肯」和「世界说不行」要分得开 ——
   前者进气泡,后者是一句规则说明。
2. **拒绝时一个字都不写**(和 `apply_affordance` 逐字同一条):没成的事不留痕。
3. **`verb_target_forms` 不加 `agent`** —— 那一格一旦混进去,对人动词就从工具路
   掉回 affordance,而那正是这一层明令禁止的。闸在 `tests/test_person_verbs.py`。
"""

from __future__ import annotations

from typing import Any, Sequence

#: 一条对人动词认哪几个键。闭集 —— 和 `PLUGIN_KEYS` / `BEAT_KEYS` 同一条纪律:
#: 不认识的键**当场说**,而不是照收然后丢掉。
PERSON_VERB_KEYS = ("id", "label", "description", "requires", "costs",
                    "consumes", "duration", "consent", "effects", "refusal")

#: 同意门的答案。**闭集**。
#:
#: 🔴 **`conditional` 3.12.1 拿掉了**(验收 A ⑤):它是一个**死档** ——
#: 引擎里**没有任何一条路产出得了它**,而它躺在契约的 `answers` 里,
#: 让消费方为一种永远不会到达的情况写一个分支。
#: **一个报得出、却永远不会发生的取值,比不报它更坏**:
#: 它让下游以为自己漏处理了什么。
#: ⚠️ 要它回来,得先有**产出它的那条路**(她说「你先帮我做件事」——
#: 那是一次带条件的同意,需要一个待办与一次回查),而那是另一单的事。
ANSWERS = ("accepted", "declined")

#: 她**不必**点头的那几种(作者显式写 `consent: "none"`)。
#: ⚠️ **默认是要点头的**,而且没有 `consent: false` 这种写法 ——
#: 和 `together` 那条「没有 `consent` 开关,同意永远是必须的」逐字同一条:
#: 给作者一个关掉同意门的开关,等于把「拉着谁就一起吃饭」交回去让他自己选。
#: `none` 只给**不需要对方配合**的动词(比如「远远看她一眼」)。
CONSENT_MODES = ("required", "none")

#: 事件三条:提了 / 答应了 / 被回了。**三种结局三种事件**(和判定那一层同构):
#: 合成一条按字段分辨的话,运维台数「她被拒了几次」要先解析载荷。
EVENTS = ("person_verb.proposed", "person_verb.accepted", "person_verb.refused")


#: 🔴 **`effects` 里指「你刚才问的那个人」的那个词**(3.13.0,tool 诉求 E ①)。
#:
#: 没有它之前,作者只能写死一个角色 id —— 而一条对人动词的全部意义就是
#: **对谁做**在运行时才知道。tool 真发包时写了 `{"as": "$target"}`,
#: 引擎**照收**(`world check` rc 0),运行时去找一个叫 `$target` 的角色、
#: 展开不出、`except` 吞掉,而玩家侧 `ok:true / accepted` **一切正常** ——
#: 于是龙族装着的三个对人动词**同意了也什么都不发生**。
#: **一个静默吞掉的 op,比一个报错的 op 贵得多。**
TARGET_PLACEHOLDER = "$target"

#: `effects` 里哪几个字段是**指人的**(会被占位符替换)。
#: ⚠️ 只替换这几格,不做全文替换:一个叫 `$target` 的**物品 id** 不该被改写。
#: ⚠️ **只放真有消费方的那几格**(3.13.0,验收 A 二轮 ⑦):
#: 上一版还列着 `who`,而**没有任何一个 op 用这个字段名** ——
#: 一格报得出、却永远不会被替换的名字,会让作者写下 `{"who": "$target"}`
#: 然后发现什么都没发生。**契约里的死格和一句假话是同一种东西。**
TARGET_FIELDS = ("as", "agent_id", "target", "from", "to")


def resolve_effects(effects: Any, *, agent_id: str) -> list[dict[str, Any]]:
    """把 `effects` 里的占位符换成**这一次问的那个人**。

    ⚠️ **只认 `TARGET_FIELDS` 那几格**,而且**逐字相等**才换 ——
    子串替换会把 `"$targets_note"` 这种名字也改掉。
    """
    out: list[dict[str, Any]] = []
    for op in (effects or ()):
        if not isinstance(op, dict):
            continue
        row = dict(op)
        for field in TARGET_FIELDS:
            if str(row.get(field) or "") == TARGET_PLACEHOLDER:
                row[field] = agent_id
        out.append(row)
    return out


def effect_errors(entry: Any, label: str, *, known_agents: Any = None) -> list[str]:
    """`effects` 里那几个指人的格子,**指得到东西吗** —— 加载期一次列全。

    🔴 **这条是 tool 诉求 E ① 的另一半**:上一版 `world check` 对一个指不到的
    角色名**一声不吭**(rc 0),而运行时静默吞掉。
    **一个装得进去、却什么都不做的动词,比一个装不进去的动词坏得多**:
    前者要人去线上一个一个试,后者当场就告诉你。
    """
    out: list[str] = []
    if not isinstance(entry, dict):
        return out
    names = None if known_agents is None else {str(a) for a in known_agents}
    for i, op in enumerate(entry.get("effects") or ()):
        if not isinstance(op, dict):
            out.append(f"{label}.effects[{i}]: 不是一个对象")
            continue
        for field in TARGET_FIELDS:
            said = str(op.get(field) or "")
            if not said or said == TARGET_PLACEHOLDER:
                continue
            if said.startswith("player:") or said.startswith("$"):
                if said.startswith("$"):
                    out.append(
                        f"{label}.effects[{i}].{field}: 不认识的占位符 {said!r} "
                        f"—— 指「你刚才问的那个人」只有一个写法:"
                        f"`{TARGET_PLACEHOLDER}`")
                continue
            if names is not None and said not in names:
                out.append(
                    f"{label}.effects[{i}].{field}: 指不到 {said!r} —— "
                    f"这个世界里没有这个角色。要指「你刚才问的那个人」"
                    f"就写 `{TARGET_PLACEHOLDER}`")
    return out


def verb_errors(entry: Any, label: str) -> list[str]:
    """一条对人动词写得对不对 —— **加载期,一次列全**。"""
    if not isinstance(entry, dict):
        return [f"{label}: 不是一个对象"]
    out: list[str] = []
    unknown = sorted(set(entry) - set(PERSON_VERB_KEYS))
    if unknown:
        out.append(f"{label}: 不认识的字段 {'、'.join(unknown)} —— 只有 "
                   f"{'、'.join(PERSON_VERB_KEYS)}")
    vid = str(entry.get("id") or "")
    if not vid:
        out.append(f"{label}: 少了 'id'")
    elif any(ch.isspace() for ch in vid) or ":" in vid:
        out.append(f"{label}: id {vid!r} 里不许有空白或冒号(冒号是实例 id 的分隔符)")
    if vid.isascii() and not str(entry.get("label") or "").strip():
        # 和本体层那条逐字同构:纯 ASCII 的动词必须给 `label` ——
        # 她读到的是那几个字,「拜师、duel」里的 duel 是噪音。
        out.append(f"{label}: 纯 ASCII 的 id 必须给一个 'label'(她读到的是那几个字)")
    consent = entry.get("consent")
    if consent is not None and str(consent) not in CONSENT_MODES:
        out.append(f"{label}: consent {consent!r} 不认识 —— 只有 "
                   f"{'、'.join(CONSENT_MODES)}。⚠️ 没有「关掉同意门」这种写法:"
                   "默认要点头,`none` 只给不需要对方配合的动词")
    if entry.get("duration") is not None and not isinstance(entry["duration"], int):
        out.append(f"{label}: duration 要是一个整数(tick 为单位)")
    return out


def consent_needed(entry: dict[str, Any]) -> bool:
    """这条动词要不要她点头。**默认要**(见 `CONSENT_MODES`)。"""
    return str((entry or {}).get("consent") or "required") == "required"


def refusal_line(entry: dict[str, Any], *, agent_name: str, gate: str = "",
                 gate_label: str = "") -> str:
    """被回了那一句 —— **她的话和世界的话要分得开**(纪律 1)。

    `gate` 非空 = **世界说不行**(睡着了 / 在赶路 / 不在这儿):那不是她的意思,
    所以说的是一句规则说明,而且**照 `together.GATE_LABELS` 那张表的措辞**。
    `gate` 空 = **她不肯**:用作者写的 `refusal`,没写就一句克制的兜底。
    """
    if gate:
        return f"{agent_name}这会儿不行 —— {gate_label or gate}。"
    said = str((entry or {}).get("refusal") or "").strip()
    return said or f"{agent_name}没有答应。"


def proposal_event(*, verb: str, entry: dict[str, Any], actor: str, target: str,
                   agent_name: str, answer: str, gate: str = "",
                   note: str = "") -> dict[str, Any]:
    """一次对人动词的结局事件。**三种结局三种事件**(见 `EVENTS`)。"""
    kind = {"accepted": EVENTS[1], "declined": EVENTS[2]}.get(answer, EVENTS[2])
    return {
        "type": kind,
        "who": actor,
        "payload": {
            "verb": verb,
            "verb_label": str(entry.get("label") or verb),
            "actor": actor, "target": target, "agent_name": agent_name,
            "answer": answer,
            # `gate` 分得出「世界说不行」和「她不肯」—— 运维台数后者才有意义。
            "gate": gate,
            "note": note,
        },
    }


def declared(plugins: Sequence[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """这个世界声明过哪些对人动词 —— `{id: 那一条}`,后装的赢。"""
    out: dict[str, dict[str, Any]] = {}
    for body in (plugins or ()):
        for entry in (body.get("person_verbs") or ()):
            if isinstance(entry, dict) and entry.get("id"):
                out[str(entry["id"])] = dict(entry)
    return out
