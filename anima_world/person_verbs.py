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

#: 同意门的三种答案。**闭集** —— 「有条件」是第三种,不是"拒绝的一种"。
ANSWERS = ("accepted", "declined", "conditional")

#: 她**不必**点头的那几种(作者显式写 `consent: "none"`)。
#: ⚠️ **默认是要点头的**,而且没有 `consent: false` 这种写法 ——
#: 和 `together` 那条「没有 `consent` 开关,同意永远是必须的」逐字同一条:
#: 给作者一个关掉同意门的开关,等于把「拉着谁就一起吃饭」交回去让他自己选。
#: `none` 只给**不需要对方配合**的动词(比如「远远看她一眼」)。
CONSENT_MODES = ("required", "none")

#: 事件三条:提了 / 答应了 / 被回了。**三种结局三种事件**(和判定那一层同构):
#: 合成一条按字段分辨的话,运维台数「她被拒了几次」要先解析载荷。
EVENTS = ("person_verb.proposed", "person_verb.accepted", "person_verb.refused")


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
    kind = {"accepted": EVENTS[1], "declined": EVENTS[2],
            "conditional": EVENTS[1]}.get(answer, EVENTS[2])
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
