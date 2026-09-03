"""实时编剧:世界里一个不上场的角色,在玩家玩的时候即兴写下一拍(3.11.0,批 3a)。

老板 2026-09-02:「原著里的所有人都是做吉祥物的,我们要做的是让玩家玩出自己的剧情
和精彩的故事……我觉得应该是实时编剧,这个周更只能算是指导」;以及那五条口径 ——
**每操作一次就有新剧情** · 张力决定写多大 · 预设的拍是锚点 · 主线由玩家写 ·
剧情要有张力、NPC 要配合。

裁决全文在 `../../docs/任务单/2026-09-02-玩法层批3-实时编剧.md` §2。

## 这个模块是什么,以及**不是**什么

**是**:一个纯函数模块 —— 闭集动作、候选池怎么筛、提示词怎么拼、回包怎么读、
没 key 时那句话怎么写、张力怎么随时间衰减。它**不认识 Redis、不认识 `World`、
不掷骰子**,和 `contact.py` / `together.py` / `autonomy.py` 同一条纪律:
判定要能被单独测,而不是只能"开一个世界跑一遍看看"。

**不是**:执行者。写世界的那一下由 `World` 那一层用**已有的 op**(`beats.expand_event_op`
/ `Scheduler._expand_beat_op`)兑现 —— 这一层一条新的"写世界"的路都不开。

## 四条这一层的纪律

1. 🔴 **LLM 在这一层没有否决权,它只挑闭集里的一项、挑一个已经筛过的人、写字。**
   照抄 `contact.py` 那句:「给它否决权还有第二个坏处:有 key 的世界和没 key 的
   世界会有两套行为,而差别只在一个环境变量上」。**合法性由引擎验**。
2. 🔴 **筛在前,不是"给了再叮嘱别说"。** 藏起来的人、禁区里的人、硬闸拦下的人
   **根本不进给模型的那份提示** —— 和主持人那一屏逐字同构(`host._host_hidden_agents`
   的教训:这一层交出去的是**散文**,宿主筛不了,**筛一半比不筛更坏**)。
3. 🔴 **不许沉默。** 超上限 / 同意门拒了 / 锚点刚响过 —— 每一种都退成一句
   `breathe`(指着他刚做的事的模板句),而不是什么都不发生。
   老板那句「每操作一次就有新剧情」的可验形式就是这一条。
4. 🔴 **`breathe` 是退路,不是常态。** 常态是**把这条线往前推一格**(升级是默认
   行为,口径 4)。一个每次都 `breathe` 的编剧和没有编剧是同一件事。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

# ── 闭集:五个戏剧动作(3a)────────────────────────────────────────────────
#
# 设计稿列了八个,3a 砍到五个,而砍法各自成立(裁决 §2.1③):
#   · `confront` —— 没有判定它就是「引擎宣布你输了」;判定是 3b。
#   · `reward`   —— `pay`/`grant_item` 早就能做,缺的不是 op 是**节制**,
#                   而节制没有判定就是拍脑袋。
#   · `callback` —— 要 `threads` 真攒起来才有东西可收,**上线当天必然是死代码**,
#                   而三道闸那条写着:橱窗里展示不了的不是超前,是死代码。
#
# ⚠️ **闭集加一项是加法,减一项是破坏消费方**(`contract.director.moves` 报它)。
MOVES = ("breathe", "approach", "invite", "reveal", "complicate")

#: 每个动作给这条线的张力加多少。**写死在引擎里,指导只调曲线的形状**
#: —— 曲线通用归引擎、阈值不通用归作者,和 needs/economy 的曲线归引擎、
#: 「树怎么长」归作者逐字同一条。
MOVE_TENSION: dict[str, float] = {
    "breathe": -0.15,     # 喘一口气:唯一往下压的
    "approach": +0.05,    # 有人来找他
    "invite": +0.08,      # 有人约他
    "reveal": +0.15,      # 揭一角
    "complicate": +0.25,  # 添乱 —— 必须带真赌注(见 `STAKE_KINDS`)
}

#: 那几个闭集**印在人屏上时该说什么**(3.11.1,验收 C ⑤)。
#:
#: 🔴 **`〔breathe〕`「setup」印在玩家屏上,而那是给机器读的名字。**
#: 和主持人那张 `MOMENT_LABELS` 逐字同一条:**闭集加一项就要在这儿加一句人话**,
#: 闸在 `tests/test_director.py`(不许纯 ASCII)。
MOVE_LABELS: dict[str, str] = {
    "breathe": "喘口气", "approach": "有人来找你", "invite": "有人约你",
    "reveal": "你知道了一件事", "complicate": "出事了",
    # 结算那一支不是编剧写的,但它也进同一份日志、同一屏。
    "collect": "到期了",
}
PHASE_LABELS: dict[str, str] = {
    "setup": "刚起头", "escalation": "越缠越紧", "climax": "到节骨眼了", "release": "松下来了",
}
STAKE_LABELS: dict[str, str] = {
    "relation": "一段交情", "money": "一笔钱", "item": "一样东西", "deadline": "一个期限",
}

#: 张力的分档词。**边界写成"从几起",不写区间** —— 区间要维护两个数,
#: 而两个数迟早对不上(`host._DAYPARTS` 逐字同一条)。
_TENSION_BANDS: tuple[tuple[float, str], ...] = (
    (0.0, "松弛"), (0.25, "有点绷"), (0.55, "紧"), (0.8, "顶到头了"),
)


def tension_text(value: float) -> str:
    """这个张力读作哪个词 —— **引擎给人话,宿主不自己译**(3.11.1,player 带回)。

    🔴 照 `_ask_ready_text` 那条先例:`tension` 是个浮点、`phase` 是个枚举,
    而站点按纪律**两样都不上屏**(不自己译、不给玩家看数字)。于是这一格
    在引擎里有、在屏上没有 —— **一个到不了消费方的读数,等于没有这个读数**。
    分档表**只有一份**:CLI 那一屏和站点读的是同一句。
    """
    try:
        got = max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return _TENSION_BANDS[0][1]
    word = _TENSION_BANDS[0][1]
    for start, name in _TENSION_BANDS:
        if got >= start:
            word = name
    return word


def due_text(hours_left: float | None) -> str:
    """这条线还剩多久要有个交代 —— **按世界小时折成人话**,空着就是没期限。

    ⚠️ **不说"还剩 0 小时"**:到点了就说「到期了」——一个念不通的读数
    和一句错的一样贵。
    """
    if hours_left is None:
        return ""
    try:
        left = float(hours_left)
    except (TypeError, ValueError):
        return ""
    if left <= 0:
        return "到期了"
    if left < 24:
        return f"还剩约 {max(1, round(left))} 个世界小时"
    return f"还剩约 {round(left / 24, 1):g} 个世界日"


#: 一条线走的四相。**这是「张力是目标曲线不是上限」那句口径的落点**(口径 4):
#: 编剧挑的不是"张力够不够高",是"这条线此刻该往哪一相走"。
PHASES = ("setup", "escalation", "climax", "release")

#: `guidance.pacing.target_curve` 那张表认哪几个键 —— **是 `PHASES` 去掉最后一相**。
#:
#: 🔴 **`release` 有意不在里面**(3.11.1,tool 带回的一条):`target_curve` 说的是
#: 「这一相**停留几个世界日**」,而 `release` 是一条线**走完之后**的样子 ——
#: 它没有时长,写它没有意义。
#: 拿 `phases` 那四格去判会**多放一格**,而多放的那一格作者写下去不报错、也不生效
#: —— 正是这一层最贵的那种错。所以契约里单报一格 `target_curve_keys`,
#: 别让下游去猜(`contract.director.target_curve_keys`,§3.66(b) 同句)。
TARGET_CURVE_PHASES = PHASES[:-1]

#: 每一相的目标张力。挑动作时按「离目标还差多少」选,而不是按一个全局上限。
PHASE_TARGET: dict[str, float] = {
    "setup": 0.25, "escalation": 0.55, "climax": 0.85, "release": 0.2,
}

#: 赌注的四种,**每一种都对得上一个已有的 op** —— 一个兑现不了的赌注就是一句
#: 标签,而「真赌注」正是口径 4 要的东西(裁决 §2.2③)。
#:   relation → `sentiment_delta` · money → `pay` · item → `grant_item` 负数
#:   deadline → 只发 `director_log`(这条线作废,不改数)
STAKE_KINDS = ("relation", "money", "item", "deadline")

#: `complicate` **必须**带赌注。写不出赌注的 complicate 退成 `reveal` ——
#: 「写下去、开得了机、什么都不发生」是这一层最贵的那种错。
MOVES_REQUIRING_STAKE = ("complicate",)

#: 一条线开出来之后,默认多少个世界**小时**要有个交代。到点没收 → 结算。
DEFAULT_DUE_HOURS = 48

#: 张力每世界小时衰减到原来的多少。**纯时间函数,所以它不必被存** ——
#: 重折廉价且必然正确(和「记忆投影仍在进程里」同一条)。
TENSION_DECAY_PER_HOUR = 0.97

#: 没有指导时,编剧只许做这两件。**保守默认不是「这一层缺席」** ——
#: 老板要的是每个世界都有编剧,所以开关是 `director.enabled`,
#: 不是「有没有写 guidance」(裁决 §2.3)。
NO_GUIDANCE_MOVES = ("breathe", "approach")
NO_GUIDANCE_CEILING = 0.5


def tension_now(value: float, since_tick: int, now_tick: int,
                ticks_per_hour: float) -> float:
    """这一刻的张力 —— **算出来的,不是存下来的**。

    `tension(now) = f(上一条 director_log 的 tick, 那时的值, 现在的 tick)`。
    存一份会随时间变旧的数,就多出一种和日志对不上的坏法,而这一层对不上的样子是
    「编剧以为他还紧张着,而他已经三天没上线了」。
    """
    try:
        base = max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
    hours = max(0.0, (int(now_tick) - int(since_tick)) / max(1e-9, float(ticks_per_hour)))
    if hours <= 0:
        return base
    # 衰减是幂,不是线性:半衰期恒定,而线性会让高张力掉得比低张力快。
    return round(base * (TENSION_DECAY_PER_HOUR ** hours), 4)


def next_phase(phase: str, move: str) -> str:
    """这一拍之后,这条线走到哪一相。**升级是默认行为**(口径 4)。

    `breathe` 是唯一把线往回带的动作(climax 之后的 release),别的都往前推。
    ⚠️ **到了 `release` 就停住** —— 再往前是"这条线该收了",而收线是 3b 的
    `callback`;这一版让它停在那儿并由 `due` 那条结算兜底。
    """
    if phase not in PHASES:
        phase = PHASES[0]
    i = PHASES.index(phase)
    if move == "breathe":
        # 已经到 climax 的线,喘一口气就是 release;还没到的原地不动。
        return PHASES[3] if i >= 2 else phase
    return PHASES[min(i + 1, 3)]


def pick_move(*, tension: float, phase: str, allowed: Sequence[str],
              capped: bool, anchor_fired: bool, ceiling: float) -> str:
    """**算术那一半**:这一轮最多能写多大。LLM 在这之后才被叫到,而且只能在
    这个结果**及以下**里挑(见 `parse_decision` 的 `allowed`)。

    三种情形一律 `breathe`,而且都不许沉默:
      · `capped`       —— 这一小时的额度用完了
      · `anchor_fired` —— 这一 tick 有一条为他响的拍(口径 2:不和锚点撞车)
      · 张力已经顶到 `ceiling` —— 安全阀
    """
    if capped or anchor_fired or float(tension) >= float(ceiling):
        return "breathe"
    target = PHASE_TARGET.get(phase, 0.5)
    gap = float(target) - float(tension)
    # 离目标越远,写得越大 —— 这就是「张力是目标曲线」那句话的落点。
    if gap <= 0.02:
        wanted = "breathe" if float(tension) > target + 0.15 else "approach"
    elif gap < 0.15:
        wanted = "approach"
    elif gap < 0.30:
        wanted = "reveal"
    else:
        wanted = "complicate"
    if wanted in allowed:
        return wanted
    # 退而求其次:按张力从大到小找一个允许的,最后一定落到 breathe
    for fallback in ("complicate", "reveal", "invite", "approach", "breathe"):
        if fallback in allowed and MOVE_TENSION[fallback] <= MOVE_TENSION.get(wanted, 0):
            return fallback
    return "breathe"


def select_cast(candidates: Iterable[dict[str, Any]], *, cast_pool: Sequence[str] = (),
                forbidden: Sequence[str] = (), hidden: Sequence[str] = (),
                gated: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """编剧这一轮**能派谁**。**筛在前** —— 筛掉的人根本不进提示词。

    四道闸,顺序是承重的(裁决 §2.1⑤):
      ① `hidden`   —— 引擎的闸:玩家不该知道这个人存在
      ② `forbidden`—— 创作者的闸:这个人存在,但这一周别碰
      ③ `cast_pool`—— 写了就只许用池子里的;**没写 = 全世界**(减前两道)
      ④ `gated`    —— 世界说不行(睡着 / 在赶路 / 手上有事 / 把他静音了)

    🔴 **`elsewhere` 有意不在 `gated` 里**:「她不在这儿」正是 `approach` 要
    解决的事,把它当闸等于让编剧只能叫来已经站在他面前的人。

    ⚠️ 一份把 hidden 的人写进 `cast_pool` 的指导:他照旧不出现,**而且要吭声**
    —— 静默满足一个作者写下的要求是这一层最贵的错。这里返回筛掉的理由,
    调用方负责把它说出来。
    """
    blocked = {str(a) for a in hidden} | {str(a) for a in forbidden}
    pool = {str(a) for a in cast_pool}
    gates = dict(gated or {})
    out: list[dict[str, Any]] = []
    for row in candidates:
        aid = str(row.get("id") or "")
        if not aid or aid in blocked:
            continue
        if pool and aid not in pool:
            continue
        if gates.get(aid):
            continue
        out.append(dict(row))
    # **确定**:同一个世界同一时刻挑两次逐项相同(和 `host.select_options` 同一条)。
    out.sort(key=lambda r: str(r.get("id") or ""))
    return out


def cast_pool_warnings(cast_pool: Sequence[str], *, hidden: Sequence[str],
                       forbidden: Sequence[str]) -> list[str]:
    """指导里写进池子、而**永远不会被派出来**的那几个人 —— 一行一条。

    **静默满足一个作者写下的要求,是这一层最贵的错**:他写了十三个人,而其中
    两个永远不出场,屏幕上什么都不少。
    """
    said: list[str] = []
    hid, forb = {str(a) for a in hidden}, {str(a) for a in forbidden}
    for aid in cast_pool:
        aid = str(aid)
        if aid in hid:
            # ⚠️ **别印 Python 的 `repr`**(`{aid!r}` 出来是 `'夏'` 带引号),
            # 也**别写死「他」**(这个世界里的角色不都是他)—— 3.11.1,验收 C ⑧。
            said.append(f"`cast_pool` 里的 {aid} 是藏起来的人(`billing: \"hidden\"`)"
                        "—— TA 不会被派出来,而这一格不会报错")
        elif aid in forb:
            said.append(f"`cast_pool` 里的 {aid} 同时写在 `forbidden.agents` 里"
                        "—— 禁区赢,TA 不会被派出来")
    return said


# ── 给模型的那一次调用 ────────────────────────────────────────────────────

_SYSTEM = (
    "你是一个文字冒险游戏的**编剧**,不是旁白也不是角色。"
    "你的活是:看玩家刚做了什么,然后决定「接下来世界怎么回应他」。"
    "用中文写,克制、具体、有画面。"
    "「不要」替玩家做决定,「不要」写玩家的心理活动,「不要」写成段的旁白。"
)


def decide_messages(*, recap: Sequence[str], cast: Sequence[dict[str, Any]],
                    allowed: Sequence[str], thread: dict[str, Any] | None,
                    guidance: dict[str, Any] | None, place_name: str,
                    day: int, tension: float, phase: str,
                    anchor: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """交给背景槽的那一次调用。**一次调用同时挑动作、挑人、写台词**。

    🔴 **这份提示里没有第二个名字来源** —— 它只由**已经筛过**的 `cast` 拼出来
    (和 `host.scene_messages` 逐字同构)。模型手上根本没有藏起来的人的名字,
    所以漏出去需要它凭空编一个它没见过的名字。
    """
    guide = guidance or {}
    who = "\n".join(
        f"- {c.get('name') or c['id']}(id={c['id']})"
        + (f",此刻{c['doing']}" if c.get("doing") else "")
        + (f",{c['where']}" if c.get("where") else "")
        for c in cast
    ) or "(这会儿没有人可以被派出来)"
    did = "".join(f"- {line}\n" for line in recap) or "- (他刚进来,还没做什么)\n"
    themes = "、".join(str(t) for t in (guide.get("themes") or ())) or "(没写)"
    tone = str(guide.get("tone") or "").strip()
    forbid = "".join(f"- {t}\n" for t in (guide.get("forbidden") or {}).get("text") or ())
    line_thread = ""
    if thread:
        line_thread = (
            f"\n他手上开着的这条线:{thread.get('promise') or '(没写)'}"
            f"(第 {thread.get('phase') or 'setup'} 相"
            f",还剩 {thread.get('hours_left', '?')} 个世界小时要有个交代)\n"
        )
    line_anchor = ""
    if anchor:
        line_anchor = (
            f"\n⚠️ 创作者预设的下一个路口:{anchor.get('steer') or anchor.get('id')}"
            "。**绕着它写、冲着它写,但别替它发生** —— 它自己会响。\n"
        )
    user = (
        f"玩家在{place_name or '某个地方'},第 {day} 天。\n"
        f"**他刚做了这些**(这是你这一拍最重要的输入,必须冲着它来):\n{did}"
        + line_thread + line_anchor
        + f"\n这个世界讲的是:{themes}\n"
        + (f"语气:{tone}\n" if tone else "")
        + (f"**绝对不许发生的**:\n{forbid}" if forbid else "")
        + f"\n你这一拍能派的人(**只能从这里面挑,不许写别的名字**):\n{who}\n"
        + f"\n你这一拍能做的事(**只能挑一个**):{'、'.join(allowed)}\n"
        + "  breathe = 只写一句冲着他刚做的事的旁白,不派人\n"
        + "  approach = 派一个人来找他搭话\n"
        + "  invite = 派一个人约他一起做件事\n"
        + "  reveal = 让他知道一件他本来不知道的事\n"
        + "  complicate = 出一件麻烦事,而且**必须押上点什么**\n"
        + "\n只输出一个 JSON 对象,不要有别的字:\n"
        + '{"move": "…", "who": "上面那个 id(breathe 时留空)", '
        + '"line": "他说的那一句(≤30 字;breathe 时写旁白)", '
        + '"why": "你为什么这么写,一句话给创作者看", '
        + '"promise": "这一拍开了什么口子(≤20 字,没有就留空)", '
        + '"stake": {"kind": "relation|money|item|deadline", "amount": 数字, '
        + '"what": "他可能失去什么(≤12 字)"}}\n'
        + "⚠️ complicate 必须写 stake,别的动作可以留空。\n"
        + "⚠️ `line` 是**那个人自己说的话**,不是旁白 —— breathe 除外。"
    )
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_decision(text: str, *, allowed: Sequence[str],
                   cast_ids: Sequence[str]) -> dict[str, Any] | None:
    """把回包读成一拍。**读不懂就退 `None`,绝不猜** —— 调用方退成 `breathe`。

    三道闸,都在这儿(**不是提示词里的叮嘱**):
      ① `move` 必须在这一轮 `allowed` 里(模型挑了一个更大的动作 → 不认)
      ② `who` 必须在**已经筛过**的 `cast_ids` 里(模型编了个名字 → 不认)
      ③ `complicate` 必须带 `stake`(写不出赌注的添乱 → 降成 `reveal`)
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _JSON_BLOCK.search(raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    move = str(data.get("move") or "").strip()
    if move not in allowed:
        logger.info("编剧挑了 %r,而这一轮只许 %s —— 不认", move, list(allowed))
        return None
    who = str(data.get("who") or "").strip()
    if move != "breathe":
        if who not in set(cast_ids):
            logger.info("编剧挑了一个不在候选里的人 %r —— 不认", who)
            return None
    else:
        who = ""
    stake = data.get("stake")
    stake_out: dict[str, Any] | None = None
    if isinstance(stake, dict) and str(stake.get("kind") or "") in STAKE_KINDS:
        try:
            amount = float(stake.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        stake_out = {"kind": str(stake["kind"]), "amount": amount,
                     "what": str(stake.get("what") or "")[:12]}
    if move in MOVES_REQUIRING_STAKE and stake_out is None:
        # **降级而不是丢弃**:一句写好了的台词不该因为少一格就整条作废,
        # 但「添乱」这个名分得收回来 —— 没有赌注的添乱只是一句吓唬。
        logger.info("编剧写了 complicate 却没写 stake —— 降成 reveal")
        move = "reveal" if "reveal" in allowed else "approach"
        if move not in allowed:
            return None
    return {
        "move": move, "who": who,
        "line": str(data.get("line") or "").strip()[:60],
        "why": str(data.get("why") or "").strip()[:80],
        "promise": str(data.get("promise") or "").strip()[:20],
        "stake": stake_out,
    }


def mock_move(recap: Sequence[str], *, place_name: str = "") -> dict[str, Any]:
    """没 key / 读不懂 / 超上限时那一拍。**永远是 `breathe`,而且永远有一句话。**

    🔴 **没配 key 是这个引擎的默认状态**,所以这不是降级路上的边角料 ——
    它就是「每操作一次都有回应」这句承诺在默认状态下的兑现方式。
    一句**指着他刚做的那件事**的旁白,一次模型都不调。
    """
    last = ""
    for line in reversed(list(recap)):
        said = str(line or "").strip()
        if said:
            last = said
            break
    if last:
        # 🔴 **别把他刚做的那句话再抄一遍**(3.11.0,端到端实跑逮的)。
        # 编剧这一句是**排在回顾后面**的,而回顾第一行就是「你端详了门口那棵
        # 老橡树。」—— 第一版把 `last` 缝进自己这句里,于是屏上是
        # 「你端详了门口那棵老橡树。你端详了门口那棵老橡树。这一下之后……」
        # **同一句话连说两遍**,而没有一处会报错。
        # 这一句要接的是**那件事之后**,不是那件事本身。
        return {"move": "breathe", "who": "", "line": "这一下之后,四下安静了一瞬。",
                "why": "没配 key:模板句", "promise": "", "stake": None,
                "source": "mock"}
    # ⚠️ **走到这儿说明他刚做的那件事没进回顾**(走路、答邀请那几种今天不在
    # `RECAP_EVENT_TYPES` 上)—— 但那不等于"什么都没发生"(3.11.1,验收 A ⑤)。
    # 上一版这句是「在咖啡店一时没什么动静。」,**把指向他刚做的事那半句丢了**,
    # 而那是这一层唯一的立身之本(设计稿 §6 最后一条:输入里「他刚做了什么」
    # 权重最高)。改成一句**指得回去**的话:他刚动过,只是世界还没回应。
    where = f"在{place_name}" if place_name else "这儿"
    return {"move": "breathe", "who": "",
            "line": f"你这一下之后,{where}静了静,没人接话。",
            "why": "没配 key:模板句", "promise": "", "stake": None, "source": "mock"}
