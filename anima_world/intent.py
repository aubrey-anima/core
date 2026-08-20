"""意图分类器 + 三条 handler(issue #16)。

在这之前,**用户的每条消息都被当成 in-character dialogue 处理**。于是:

- "让林素也进来" —— 用户在**导演场景**,角色只能"想象林素在场";林素本人不在
  agents 里,不走一 tick,世界里根本没有这个人在场。
- "以后叫我霜霜" —— 用户在**改对话本身的规则**,角色应一两轮就忘。

三类走三条不同的路:`dialogue` 照旧(不动),`style_adjust` 写 persona override
(按 (角色, 玩家) 永久),`narrative_direction` 交给 director —— 真改世界,通过
**世界事件流**让所有人看见,而不是往提示词里塞一句"想象林素在场"。

#15 是她的**出**(她能做什么),这里是她的**入**(她怎么听你说话);两个叠起来
才是"真 agent 化"。

**分类往 dialogue 上偏(开放问题 1)。** 两种错的代价不对称:该 narrative 判成
dialogue,你看着她"想象化"很别扭;该 dialogue 判成 narrative,你正说的话被吞掉,
只回一句系统确认。后者更贵,所以低置信度一律退回 dialogue,并且把置信度和退回
原因一起交出去 —— 降级不许无声。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

logger = logging.getLogger(__name__)

INTENTS = ("dialogue", "narrative_direction", "style_adjust")
DEFAULT_INTENT = "dialogue"
DEFAULT_MIN_CONFIDENCE = 0.6

DEFAULT_CLASSIFIER_PROMPT = (
    "你是一个意图分类器,不是角色。判断玩家这条消息属于哪一类,只输出 JSON。\n"
    "类别:\n"
    "- dialogue:玩家在和角色说话(提问、回应、闲聊、调情、争吵都算)\n"
    "- narrative_direction:玩家在导演场景,要求世界里的某个人做某事、出场或离场\n"
    "- style_adjust:玩家在改对话本身的规则(怎么称呼他、要不要括号描写、语气偏好)\n"
    "输出格式(不要解释、不要代码块):\n"
    '{{"intent": "...", "confidence": 0.0~1.0, "params": {{}}}}\n'
    "narrative_direction 的 params:"
    '{{"target": "被指挥的人的名字",'
    ' "action": "come_here|go|leave|sleep|eat|work|seek_company|wander'
    '|talk_to|interact|together|give|act",'
    ' "place": "去哪儿", "object": "这件事的对象", "verb": "对它做什么",'
    ' "detail": "要他做什么"}}\n'
    "  - come_here:叫他到你这儿来\n"
    "  - go:让他去某个具体的地方 —— **place 必填**,只能从下面那份地点清单里挑一个\n"
    "  - leave:让他走开(不指定去哪)\n"
    "  - sleep / eat / work:让他去睡觉 / 吃东西 / 干活,不用填别的\n"
    "  - seek_company:让他去找个人待着(不指定是谁);wander:让他就在原地待着\n"
    "    —— **别把这两个判成 leave**,他不是要离开这儿\n"
    "  - talk_to:让他去找**另一个人**说话 —— **object 填那个人的名字**\n"
    "  - interact:让他对身边某样东西动手 —— object 填东西的 id,verb 填做什么\n"
    "  - together:玩家要**和他一起**做一件事(「我们一起吃饭」「陪我坐会儿」)——"
    " object 填东西的 id,verb 填做什么;还有别人就填 with(名字的数组)。\n"
    "    和 interact 的区别只有一条:**玩家自己在不在里面**。"
    "「你去雕那座冰雕」是 interact,「我们一起雕」是 together\n"
    "    **「带我去X」「陪我去X」也是 together** —— 这时 place 填那个地方,"
    "别判成 go:go 是他一个人走,而玩家要的是两个人一起过去\n"
    "  - give:玩家把一样东西给他 —— **object 填东西的名字**\n"
    "  - act:以上都不是的时候才用,让他做一件别的事,detail 必填\n"
    "  - **被指挥的人就是正在跟玩家说话的那个人,也照样判 narrative_direction**"
    "(「你去哈尔滨」是最常见的一种)。玩家嘴里的「你」指的就是 {speaker} ——"
    " 这时 target 请填 {speaker},不要填「你」。\n"
    "style_adjust 的 params:"
    '{{"kind": "address_form|description_style|tone_preference|forbidden_topics|nickname_for_player",'
    ' "value": "具体规则"}}\n'
    "拿不准就判 dialogue 并给一个低 confidence。\n"
    "正在跟玩家说话的人:{speaker}\n"
    "这个世界里的地方:{places}\n"
    "这个场景里在场的人:{present}\n"
    "最近的对话:\n{recent}"
)

# 玩家嘴里的第二人称。**引擎自己也要认得它们**,不能只靠提示词教分类器 ——
# 「你去哈尔滨」是这一层最常见的一句,而它此前的下场是
# `target="你"` → "我不认识你" → 世界一动不动。真模型第一次实测就是这个样子。
SECOND_PERSON = frozenset({
    "你", "妳", "您", "你自己", "妳自己", "自己", "本人",
    "you", "yourself", "u",
})

# 玩家嘴里的**他自己**。「我们一起吃饭」里那个"我"要翻得回一个 `player:<id>` ——
# 翻不回去的话,一起做事这条路上玩家永远只能当旁观者,而这句话的主语正是他。
FIRST_PERSON = frozenset({
    "我", "我自己", "咱", "俺", "本人", "玩家",
    "me", "myself", "i", "player",
})

# ── 自报家门:引擎自己认得,不经分类器 ──────────────────────────────────────
#
# 「我叫林越,你叫我小林就行」。这一句**不该**判成 style_adjust —— 它确实是在说话,
# 判过去的下场是玩家自报家门却收到一句「(记下了:…)」的系统回执,他刚说的话被整个
# 吞掉(见本模块开头那段不对称)。所以这一层做的事只有一件:**记下来,然后放行**,
# 这一轮照旧是对话。
#
# 不交给分类器还有两条这一处独有的理由:分类器默认不跑(`chat.intent.enabled` 默认
# 关),而身份块每个世界都在;而且身份块**已经许下了这个承诺** —— 它两支结尾都写着
# 「他要是告诉了你名字,这一轮之后就照那个名字认他」,而在这之前没有任何一行代码
# 兑现它。一份自称"最高优先级事实"的提示词许一个引擎不做的诺,是这个仓库最怕的
# 那种坏法:她当场叫得出来(原文还在上下文里),下一场开局又不认识他了。
#
# **认出来的名字不升格成 `display_name`。** 那一个是宿主认证过的身份(纪律 3),
# 而这里是一次正则猜测;混成一格的话,一次误判就变成她口中"最高优先级"的事实。
# 它落进 (角色, 玩家) 的 override,身份块照着说「他**告诉过你**他叫 X」—— 出处
# 是他自己,而不是世界替他担保。

# 先按标点切段再逐段**从头**匹配。切段让「我叫林越,你叫我小林就行」两半各归各的;
# 锚在段首让「别叫我先生」「我不叫小林」自然落空 —— 否则要靠一串否定前瞻去堵,
# 而那种堵法漏一个就是把玩家的原话反过来记。
_INTRO_SEGMENT = re.compile(
    "[,，。.!！?？;；、：:~～—…\n]+"
)

# 「叫」后面紧跟着的这几个字一出现,这句话就不是自报家门:「我叫**他**小林」是
# 我怎么称呼别人,「我叫**了**一杯咖啡」压根是另一个动词义。**在这儿挡,不在名字
# 那一头挡** —— 捕获到手再去挑,挑剩下的("他小林")一样会被记下来。
_NOT_INTRO_HEAD = r"(?![了过着起来他她它你您我咱一两个这那什么])"

_INTRO_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("player_name", re.compile(rf"^我(?:就)?(?:叫做|叫作|名叫|叫){_NOT_INTRO_HEAD}(.+)$")),
    ("player_name", re.compile(r"^我(?:的)?名字(?:就)?(?:是|叫做|叫)(.+)$")),
    ("player_name", re.compile(r"^my name(?:'s|s| is|:)\s*(.+)$", re.I)),
    ("address_form", re.compile(
        rf"^(?:你|您|你们|大家)?(?:可以|能够|能|就|请|都)*叫我{_NOT_INTRO_HEAD}(.+)$"
    )),
    ("address_form", re.compile(r"^(?:just\s+)?call me\s+(.+)$", re.I)),
)

# 名字后面挂着的语气尾巴。「你叫我小林就行」里的"就行"不是名字的一部分。
_INTRO_TAIL = re.compile(r"(?:就行|就好|就可以|就是|好了|即可|行了|吧|啦|呀|啊|哦|喔|嘛|呢|了)+$")
_INTRO_QUOTES = "「」『』\"'“”‘’()()《》 \t"

# 匹配上了、但捕获到的不是名字。**空手比记错强**:记错的那个会进身份块、进转录、
# 进她的长期记忆,而玩家永远不会知道是哪一句让她这么叫他的。
_NOT_A_NAME = frozenset({
    "什么", "啥", "谁", "哪个", "名字", "一声", "一下", "过来", "来", "去",
    "他", "她", "它", "你", "您", "我", "咱", "人", "一个", "这个", "那个",
    "what", "who", "me", "him", "her", "you",
})
# 名字有多长。**汉字那一档卡得比拉丁字母狠得多**,因为它是这一层唯一还剩下的闸:
# 「我叫苏晚夏做一杯咖啡」结构上和「我叫林越」一模一样,分不开 —— 但没有人叫
# 「苏晚夏做一杯咖啡」。中文名字连复姓带名到头是四个字,昵称再宽一点。
_INTRO_MAX_LEN = 16
_INTRO_MAX_LEN_CJK = 6


def _too_long(name: str) -> bool:
    ascii_only = all(ord(ch) < 128 for ch in name)
    return len(name) > (_INTRO_MAX_LEN if ascii_only else _INTRO_MAX_LEN_CJK)


def read_self_introduction(text: str) -> dict[str, str]:
    """玩家这句话里有没有自报家门 —— 有就返回 `{override kind: 值}`,没有是空 dict。

    两种 kind,因为**名字和称呼不是一件事**:「我叫林越」给 `player_name`,
    「你叫我小林就行」给 `address_form`。一句话里两样都有就两样都返回。

    这不是分类,是识别 —— 调用方记下来之后**必须让这一轮继续走对话**。
    """
    found: dict[str, str] = {}
    for raw in _INTRO_SEGMENT.split(str(text or "")):
        segment = raw.strip()
        if not segment:
            continue
        for kind, pattern in _INTRO_RULES:
            if kind in found:
                continue
            match = pattern.match(segment)
            if match is None:
                continue
            name = _INTRO_TAIL.sub("", match.group(1).strip().strip(_INTRO_QUOTES)).strip()
            name = name.strip(_INTRO_QUOTES).strip()
            if not name or _too_long(name) or name.casefold() in _NOT_A_NAME:
                continue
            found[kind] = name
    return found


# 玩家亲手做的事**要当面**;玩家开口让她做的事不要。
#
# 判据是**施动者是谁**,不是这件事重不重要:「你去睡觉」隔着电话说得出来,而
# 一条围巾隔着三亚递不过去。把导演那几条一起挡掉,等于宣称"异地就不能跟她说话",
# 而那正是这一层想保住的另一半(见 `chat_service.respond` 的两段身份声明)。
#
# ⚠️ 这张表**默认不生效**:闸挂在 `presence.enforce_colocation` 上,默认关,
# 关着时行为与今天逐位相同。理由是引擎侧收紧会当场打断线上世界 —— `player_move`
# 是宿主可选调用,今天线上根本没人调,于是"异地"是默认值。
FACE_TO_FACE_ACTIONS = frozenset({"give", "together"})

# 玩家指令 → 引擎里那个动作。值是 `(行为树的 kind, 成了怎么说, 没成怎么说)`。
# **只收引擎真有的那几个**:多写一个进去,就是又一次"照做了"而世界一动不动。
# 这张表和 `tools/body.py` 那几个 `@tool` 是同一批 kind,有意的 —— 排班让她睡、
# 她自己决定睡、玩家让她睡,在世界里必须是同一件事。
_BODY_ACTIONS: dict[str, tuple[str, str, str]] = {
    "sleep": ("sleep", "睡下了", "睡不了(她在赶路)"),
    "eat": ("eat", "去吃东西了", "吃不上(她在赶路)"),
    "work": ("work", "干活去了", "干不了(她在赶路)"),
    # 「闲着」有两种,照实登记别合并(和 `tools/body.py` 那两条同一个理由):
    # `seek_company` 是"想找人",`wander` 是"什么也不特意做"。合成一个,她就再也
    # 表达不了"我想找人",而那是需求系统里 social 那条曲线唯一的出口。
    #
    # 它们进这张表还有一个更急的理由:**不给分类器这个选项,它会挑一个更坏的**。
    # 实测「你去找个人待着」被判成 `leave`,于是她真的动身去了大理 —— 一次**错误
    # 但真实**的世界改动,比什么都不做坏得多。
    "seek_company": ("idle_social", "去找人待着了", "这会儿走不开(她在赶路)"),
    "wander": ("idle_wander", "就在这儿待着了", "这会儿停不下来(她在赶路)"),
}

# 一次分类的结果。`reason` 只在退回 dialogue 时有值 —— 它是"为什么按对话处理"。
@dataclass
class Intent:
    intent: str = DEFAULT_INTENT
    confidence: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "intent": self.intent,
            "confidence": round(float(self.confidence), 3),
        }
        if self.params:
            payload["params"] = dict(self.params)
        if self.reason:
            payload["reason"] = self.reason
        return payload


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_classification(text: str, *, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> Intent:
    """把分类器的回包收敛成一个 Intent。读不懂 = dialogue + 说明原因。"""
    raw = (text or "").strip()
    match = _JSON_BLOCK.search(raw)
    if not match:
        return Intent(reason="分类器没给出 JSON,按对话处理", raw=raw)
    try:
        loaded = json.loads(match.group(0))
    except ValueError:
        return Intent(reason="分类器的 JSON 解析失败,按对话处理", raw=raw)
    if not isinstance(loaded, dict):
        return Intent(reason="分类器给的不是对象,按对话处理", raw=raw)

    intent = str(loaded.get("intent") or "").strip()
    try:
        confidence = float(loaded.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    params = loaded.get("params")
    params = dict(params) if isinstance(params, dict) else {}

    if intent not in INTENTS:
        return Intent(
            confidence=confidence, params=params, raw=raw,
            reason=f"分类器报了一个不认识的类别 {intent!r},按对话处理",
        )
    if intent != DEFAULT_INTENT and confidence < min_confidence:
        return Intent(
            confidence=confidence, params=params, raw=raw,
            reason=f"意图不明({intent} 只有 {confidence:.2f}),按对话处理",
        )
    return Intent(intent=intent, confidence=confidence, params=params, raw=raw)


def build_classifier_messages(
    template: str,
    text: str,
    *,
    present: Sequence[str],
    recent: Sequence[dict[str, str]],
    places: Sequence[tuple[str, str]] = (),
    speaker: str = "",
) -> list[dict[str, str]]:
    """`places` 是 (id, 人话名) 的清单 —— **不给它,分类器就只能编地名**。

    编出来的地名会被 `resolve_place` 挡下来并给出回执,不至于静默;但那一次往返
    白费,而玩家看到的是一句"没有哈尔滨这个地方",尽管这个世界里明明有
    "哈尔滨·冰雪大世界"。把清单交给它,匹配这件事就大多在分类器那一步就成了。
    """
    recent_text = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')}" for m in list(recent)[-5:]
    ) or "(没有)"
    place_text = "、".join(
        f"{name}({pid})" if name and name != pid else str(pid) for pid, name in places
    ) or "(不知道)"
    system = template.format(
        present="、".join(present) or "(只有你们两个)",
        recent=recent_text,
        places=place_text,
        speaker=speaker or "(不知道)",
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


# ── 地点解析 ───────────────────────────────────────────────────────────────

# 地名里的分隔符与常见后缀。「哈尔滨」要对上「哈尔滨·冰雪大世界」,靠的就是这一层:
# 玩家嘴里的地名永远比世界里的 id 短。
_PLACE_NOISE = re.compile(r"[·・·\s\-_/、,,。.!!??的]+")


def _normalize_place(text: str) -> str:
    return _PLACE_NOISE.sub("", str(text or "")).strip().casefold()


def resolve_place(raw: str, points: dict[str, str]) -> tuple[str | None, list[str]]:
    """玩家嘴里的地名 → 世界里的 point id。

    返回 `(point_id, 候选)`。`point_id` 是 None 时 `候选` 要么是空的(压根没这地方),
    要么是**不止一个**(说得不够准)—— 两种"没解析出来"必须分得开:前者的下一步是
    "换一个地名",后者的下一步是"说得再准一点",给同一句回执等于让玩家瞎试。

    **绝不在歧义时随便挑一个** —— 挑错了她真的会走过去,而世界里没有一行日志说
    这不是玩家要的那个地方。
    """
    wanted = str(raw or "").strip()
    if not wanted:
        return None, []
    if wanted in points:
        return wanted, [wanted]
    normalized = _normalize_place(wanted)
    if not normalized:
        return None, []
    # 由准到松,**第一层命中就收手**:精确名字命中时不许再让包含匹配把候选搅成歧义。
    for candidates in (
        [pid for pid, name in points.items() if _normalize_place(name) == normalized],
        [pid for pid in points if _normalize_place(pid) == normalized],
        [
            pid for pid, name in points.items()
            if normalized in _normalize_place(name) or _normalize_place(name) in normalized
        ],
        [pid for pid in points if normalized in _normalize_place(pid)],
    ):
        if len(candidates) == 1:
            return candidates[0], candidates
        if candidates:
            return None, sorted(candidates)
    return None, []


def places_menu(points: dict[str, str], *, with_ids: bool = True) -> str:
    """列一份"有的是哪些地方"。

    `with_ids` 分的是**读者**,不是排版。给模型看的那份必须带 id —— `walk` 只收 id
    (`point_ids()`),不给它就等于让它接着猜。给玩家看的那份不该带:他打的是人话,
    而人话在 `resolve_place` 的第一层就命中了。

    线上读到的那一行是这样的:玩家说了句「你去哈尔滨吧」,她那一轮的开头是

        (没有 哈尔滨 这个地方;有的是 铁匠巷(alley)、剃头铺(barber)、江渡浴室
        (bathhouse)、……、念姐的小院(yard)。)

    二十个拉丁字母的 id 铺在一个中文世界的第一行,而它们一个字都没告诉他这个世界的事
    (#18 那条判据);更坏的是它们**看着像要他照着打的东西**。同一个函数的成功回执
    `({name}往{place_name}去了。)` 从来只说人话 —— 一进一出两种写法,错的是失败那半。

    重名的那几个照旧带 id:`小院、小院;说准一点` 是一句没法照着做的回执,而"说得出
    该怎么办"正是回执存在的理由。只给重名的带,所以一个重名不会把整份清单打回原形。
    """
    names = {str(pid): str(name or pid) for pid, name in points.items()}
    if with_ids:
        return "、".join(
            f"{name}({pid})" if name != pid else pid
            for pid, name in sorted(names.items())
        )
    duplicated = {
        name for name in names.values()
        if list(names.values()).count(name) > 1
    }
    return "、".join(
        f"{name}({pid})" if name != pid and name in duplicated else name
        for pid, name in sorted(names.items())
    )


# ── director:narrative_direction 的兑现 ────────────────────────────────────

# 拒绝的意思是「**我**没读懂你的话」,不是「世界里没有这回事」。
#
# 分界线是一句可以当场问出口的话:**这句回执告诉玩家的是这个世界的事吗?**
# 留下的那些都是:「世界里没有哈尔滨」「你在这头他在那头,这件事得当面」
# 「他手上没有那样东西」「果子还没熟」—— 每一句都教给玩家一点世界的规矩。
# 这几个一句都不是:它们说的是分类器**没把自己的字段填全**,而玩家根本不知道
# 有个分类器。线上抓到的现场:玩家问「我想去看看你说的那个地方,能带我去吗」,
# 分类器判成 `together` 却把 `object` 留空(`detail` 里倒是写着"带玩家去潮汐里
# 3号"),于是她那一轮的**第一行**是 `(一起做什么?说具体一点 —— 得有个东西。)`
# —— 引擎越过她,当面责怪玩家没说清楚,而玩家那句话再清楚不过;紧接着她的散文
# 回答好得很,还真答应了下楼。那句回执是纯多出来的噪音。
#
# 退回对话这条路**上面两个分支就在用**:`style_adjust` 少了 kind/value 时正是
# 记一行日志、按对话处理、玩家什么也不会看见。导演这条漏了同一手。
UNDERSPECIFIED_REASONS = frozenset({
    "empty_object", "empty_detail", "empty_place", "unknown_player",
})


@dataclass
class DirectorOutcome:
    """导演一次的结果。

    `text` 是给玩家的一句回话(拒绝也走这里)。另外两个字段是"指挥对话方本人"这条
    路要用的:

    - `self_directed` —— 被指挥的就是正在说话的那个人。这条路上**不许顶掉她的话**,
      所以 `text` 不再是她这一轮的全部回复。
    - `grounding` —— 塞进这一轮提示词的一句**事实**:刚刚在世界里真发生了什么。
      她照着它回话,于是"她一边答应一边真的走"是同一件事的两面,而不是提示词里
      的一句想象。
    """

    ok: bool
    text: str
    detail: dict[str, Any] = field(default_factory=dict)
    self_directed: bool = False
    grounding: str = ""

    @property
    def underspecified(self) -> bool:
        """这次拒绝是"我没读懂",不是"世界不答应" —— 见 `UNDERSPECIFIED_REASONS`。

        这几条都在任何一次世界写之前返回,所以退回对话是**干净**的:没有半件
        已经落地的事需要回滚。
        """
        return not self.ok and self.detail.get("reason") in UNDERSPECIFIED_REASONS


class Director:
    """把 narrative_direction 变成世界里真发生的事。

    **只对已经存在的角色动手。** 不认识的人一律拒绝并说清楚下一步 —— 自然语言
    造人(`author_agent`)是以后的事,那里要有每日上限、作者 opt-in、`authored_by_user`
    标记与冲突处理;没有那些守卫就开这道门,等于让一句话往世界里塞进不可回滚的人。

    关键:**不进提示词,进世界。** 让林素过来 = 一次真的行程事件,于是白霜下一次读
    `world_context` 时会真的看到"林素在场"。往提示词里塞"想象林素在场"正是要修的病。

    **「指挥正在跟你说话的那个人」是头等场景,不是要挡掉的边界情况。** 这条路原先
    回一句"直接跟她说就好"并把玩家那句话整个吞掉 —— 而玩家嘴里绝大多数指令(「你去
    哈尔滨」)恰恰就是这一类。现在它照常兑现,而且**不接管她的回话**:指令进世界,
    `grounding` 进这一轮的提示词,她自己开口。一边答应一边真的走,才是这一层想要的
    样子;只回一句系统确认的版本,和"只改提示词不改世界"是同一种假。

    **地点是参数,不是引擎替玩家挑的。** `leave` 从前把目的地取成 `point_ids()[0]`
    (排序第一个),于是玩家永远说不了去哪 —— 一个"照做了但去了别处"的动作,世界里
    一行日志都不报错。现在 `go` 收 `place`,对不上就拒绝并列出有的是哪些地方。
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def _resolve(self, target: str, speaker: str = "") -> str | None:
        """被指挥的是谁。**第二人称归说话人** —— 这一条不能只写在提示词里。

        真模型第一次实测就撞上了:「你去哈尔滨」被规规矩矩地判成
        `narrative_direction`,置信度 1.0,而 `target` 是字符串 `"你"` —— 于是
        "我不认识你",世界一动不动。提示词那一头当然要教(见 `{speaker}`),但
        分类器是一次 LLM 往返,它有权抽风;**引擎手上认得的东西不许依赖它**。

        `target` 整个空着也归说话人:我们已经在 narrative 分支里了,玩家确实在
        指挥某个人,而这句话里能确定的只有一个人 —— 正在跟他说话的那个。
        猜错的代价是她走一趟(看得见、走得回来);拒绝的代价是玩家收到一句
        "我不认识这个人",而他根本没提过谁。
        """
        target = str(target or "").strip()
        if speaker and (not target or target.casefold() in SECOND_PERSON):
            return speaker
        if not target:
            return None
        names = self._runtime.agent_names()
        if target in names:
            return target
        for agent_id, name in names.items():
            if str(name).strip() == target:
                return agent_id
        return None

    def _points(self) -> dict[str, str]:
        """地点 id → 人话名。运行时没实现 `point_names` 就退回只有 id 的那份。"""
        getter = getattr(self._runtime, "point_names", None)
        if getter is not None:
            try:
                return {str(k): str(v or k) for k, v in (getter() or {}).items()}
            except Exception:  # noqa: BLE001 - 读不到名字不该让导演告吹
                logger.warning("读地点名字失败,按 id 匹配", exc_info=True)
        return {str(pid): str(pid) for pid in self._runtime.point_ids()}

    def _go(self, resolved: str, name: str, raw_place: str) -> DirectorOutcome:
        """把一个人送到一个**玩家指定**的地方。"""
        points = self._points()
        if not raw_place.strip():
            # 玩家一个地名都没说 —— 那就没有"世界里没有它"这回事可报。此前这里落到
            # 下面那句上,拼出来的是 `(没有 你说的那个地方 这个地方;有的是 …)`:
            # 一句话本身就不通,而它还把二十个地名铺在她开口之前。线上的现场是
            # 「带我去个安静点的地方吧,我想跟你说会儿话」—— 分类器判了 `go` 却把
            # 提示词里写着**必填**的 place 留空,于是引擎越过她责怪玩家没说地方,
            # 而玩家问的本来就是"你挑一个"。和 empty_object 是同一件事,漏在这儿。
            return DirectorOutcome(
                ok=False,
                text="",
                detail={"target": resolved, "reason": "empty_place", "place": raw_place},
            )
        where, candidates = resolve_place(raw_place, points)
        if where is None:
            if candidates:
                readable = places_menu(
                    {pid: points.get(pid) or pid for pid in candidates}, with_ids=False
                )
                return DirectorOutcome(
                    ok=False,
                    text=f"({raw_place} 对得上好几个地方:{readable};说准一点。)",
                    detail={"target": resolved, "reason": "ambiguous_place",
                            "place": raw_place, "candidates": candidates},
                )
            return DirectorOutcome(
                ok=False,
                text=f"(没有 {raw_place or '你说的那个地方'} 这个地方;"
                     f"有的是 {places_menu(points, with_ids=False)}。)",
                detail={"target": resolved, "reason": "unknown_place", "place": raw_place},
            )
        place_name = points.get(where) or where
        moved = self._runtime.move_agent(resolved, where)
        return DirectorOutcome(
            ok=True,
            text=f"({name}往{place_name}去了。)",
            detail={"target": resolved, "action": "go", "place": where,
                    "place_name": place_name, **moved},
        )

    def direct(
        self, *, agent_id: str, params: dict[str, Any], player_id: str | None = None
    ) -> DirectorOutcome:
        target = str(params.get("target") or "").strip()
        action = str(params.get("action") or "come_here").strip()
        detail = str(params.get("detail") or "").strip()
        place = str(params.get("place") or params.get("location") or "").strip()
        obj = str(params.get("object") or params.get("item") or "").strip()
        verb = str(params.get("verb") or "").strip()
        resolved = self._resolve(target, speaker=agent_id)
        if resolved is None:
            # 这一层的边界,而且要给出下一步:纯拒绝会让人以为这条路坏了。
            return DirectorOutcome(
                ok=False,
                text=f"(我不认识{target or '这个人'}。这个世界里现在只有"
                     f"{'、'.join(self._runtime.agent_names().values())};"
                     f"要让新的人进来得先把 ta 造出来。)",
                detail={"target": target, "reason": "unknown_agent"},
            )
        self_directed = resolved == agent_id
        here = self._runtime.agent_location(agent_id)
        name = self._runtime.agent_names().get(resolved, resolved)

        blocked = self._colocation_refusal(resolved, name, action, player_id)
        if blocked is not None:
            return blocked

        # `go` 有目的地就走 `go`;`leave` 带了目的地也一样 —— 玩家已经说了去哪,
        # 再去挑"排序第一个地点"就是把他刚说的话丢掉。
        if action == "go" or (action == "leave" and place):
            outcome = self._go(resolved, name, place)
        elif action == "leave":
            outcome = self._leave(resolved, name, here)
        elif action in _BODY_ACTIONS:
            outcome = self._body(resolved, name, action, detail)
        elif action == "talk_to":
            outcome = self._talk_to(resolved, name, obj, detail)
        elif action == "interact":
            outcome = self._interact(resolved, name, obj, verb, detail)
        elif action == "together":
            outcome = self._together(resolved, name, obj, verb, player_id, params)
        elif action == "give":
            outcome = self._give(resolved, name, obj, player_id, detail)
        elif action == "act":
            outcome = self._act(resolved, name, detail, player_id)
        else:
            outcome = self._come_here(agent_id, resolved, name, here, self_directed, player_id)

        if not self_directed:
            return outcome
        outcome.self_directed = True
        outcome.grounding = self._grounding(outcome, action=action, detail=detail)
        return outcome

    # ── 过日子的动作:让世界真做,而不是让她"知道有人要她做" ──────────────────

    def _body(self, resolved: str, name: str, action: str, detail: str) -> DirectorOutcome:
        """「你去睡觉 / 吃点东西 / 干活」—— 走行为树那条路真的做。

        和 `_act` 的分界在**引擎有没有这个动作**,不在"该不该替她答应":

        - 有(睡/吃/干活/走/找人说话/对东西动手)—— 兑现,和 `go` 一模一样。一句
          「你去睡觉」跟一句「你去哈尔滨」在玩家嘴里是同一种话,而此前前者只落一条
          记忆、后者真的起程,这个不一致没有任何道理。
        - 没有(「把冰鞋扔了」)—— 退回 `_act`:照实说"她知道了",不假装做过。

        兑现走 `do_action`,也就是**行为树走的那条路**(`Scheduler.emit_action`)——
        另写一份"外部版本的睡觉"迟早和行为树那份分叉。`do_action` 回 `False` 是世界
        说"这会儿不接"(她在半路上),那不是失败,照实报;`_grounding` 会把这句实话
        塞进她这一轮的提示词,于是她不会一边站在原地一边说"我睡下了"。
        """
        kind, done, refused = _BODY_ACTIONS[action]
        params = self._body_params(resolved, kind)
        if not self._runtime.do_action(resolved, kind, params):
            return DirectorOutcome(
                ok=False, text=f"({name}这会儿{refused}。)",
                detail={"target": resolved, "action": action, "kind": kind,
                        "reason": "not_taken"},
            )
        return DirectorOutcome(
            ok=True, text=f"({name}{done}。)",
            detail={"target": resolved, "action": action, "kind": kind,
                    "params": params, "took": True},
        )

    def _entities(self) -> dict[str, str]:
        """东西的 id → 人话名。运行时没实现 `entity_names` 就当这一层不在。"""
        getter = getattr(self._runtime, "entity_names", None)
        if getter is None:
            return {}
        try:
            return {str(k): str(v or k) for k, v in (getter() or {}).items()}
        except Exception:  # noqa: BLE001 - 读不到名字不该让导演告吹
            logger.warning("读实体名字失败,按 id 匹配", exc_info=True)
            return {}

    def _body_params(self, resolved: str, kind: str) -> dict[str, Any]:
        """`work` 要知道在哪儿干 —— 和 `tools/body.work` 逐字同一份取法。"""
        if kind != "work":
            return {}
        here = self._runtime.agent_location(resolved)
        return {"location": here} if here else {}

    def _talk_to(self, resolved: str, name: str, obj: str, detail: str) -> DirectorOutcome:
        """「你去找林雪瑶说话」。

        两头都要认得出来:**对方也是这个世界里的人**,所以走同一个 `_resolve`
        (但**不给 speaker 兜底** —— 这里的"你"没有意义,而兜底会让"你去找你说话"
        变成她跟自己搭话)。对方不在她这儿的话 `do_action` 会回 `False`,那是引擎
        守住的在场语义,照实报。
        """
        wanted = obj or detail
        other = self._resolve(wanted)
        if other is None:
            return DirectorOutcome(
                ok=False,
                text=f"(我不认识{wanted or '你说的那个人'}。这个世界里现在只有"
                     f"{'、'.join(self._runtime.agent_names().values())}。)",
                detail={"target": resolved, "action": "talk_to",
                        "reason": "unknown_agent", "object": wanted},
            )
        if other == resolved:
            return DirectorOutcome(
                ok=False, text=f"({name}没法跟自己搭话。)",
                detail={"target": resolved, "action": "talk_to", "reason": "self_target"},
            )
        other_name = self._runtime.agent_names().get(other, other)
        if not self._runtime.do_action(resolved, "chat", {"target": other}):
            return DirectorOutcome(
                ok=False,
                text=f"({other_name}这会儿不在{name}那儿,搭不上话。)",
                detail={"target": resolved, "action": "talk_to", "object": other,
                        "reason": "not_colocated"},
            )
        return DirectorOutcome(
            ok=True, text=f"({name}去找{other_name}说话了。)",
            detail={"target": resolved, "action": "talk_to", "object": other,
                    "object_name": other_name, "took": True},
        )

    def _interact(
        self, resolved: str, name: str, obj: str, verb: str, detail: str
    ) -> DirectorOutcome:
        """「你去雕那座冰雕」—— 走本体层那条统一路径(`interact_with`)。

        拒绝理由**原样带出来**,不合并:本体层特意把"这会儿不行 / 你做不了 /
        你手上有别的事"分成三四类(见 CLAUDE.md 的能力那一节),在这儿合成一句
        "没成"等于把那份区分丢掉,而玩家和她都需要知道下一步该干什么。
        """
        if not obj:
            return DirectorOutcome(
                ok=False, text="(对哪样东西?说具体一点。)",
                detail={"target": resolved, "action": "interact", "reason": "empty_object"},
            )
        # 玩家说的是名字,世界里存的是 id ——「天鹅冰雕」对「半成的天鹅冰雕」
        # (`icesculpture:swan`)。和地名走**同一个** `resolve_place`:两处都是
        # "玩家嘴里的名字永远比 id 短",另写一份匹配迟早给出两套答案。
        # 认不出来就原样交给下一层 —— 那里会报"这儿没有 X",并列出有的是哪些。
        entities = self._entities()
        if entities and obj not in entities:
            matched, candidates = resolve_place(obj, entities)
            if matched is not None:
                obj = matched
            elif candidates:
                readable = "、".join(f"{entities.get(e) or e}({e})" for e in candidates)
                return DirectorOutcome(
                    ok=False, text=f"({obj} 对得上好几样东西:{readable};说准一点。)",
                    detail={"target": resolved, "action": "interact", "object": obj,
                            "reason": "ambiguous_object", "candidates": candidates},
                )
        # `interact_with` 把"讲不通的调用"(没这东西、没这动词、东西不在她这儿)抛成
        # `ToolCallError`,只有"世界说这会儿不行"才原样返回。两种都要变成给玩家的一句
        # 话,所以在这儿收成同一个形状 —— 但**理由不合并**,原文照抄进 text。
        from anima_world.tools.base import ToolCallError

        try:
            outcome = self._runtime.interact_with(resolved, obj, verb or detail or "look")
        except ToolCallError as exc:
            return DirectorOutcome(
                ok=False, text=f"({exc})",
                detail={"target": resolved, "action": "interact", "object": obj,
                        "verb": verb, "reason": "invalid"},
            )
        if not outcome.get("ok"):
            refusal = str(outcome.get("refusal") or "这会儿不行")
            # **两套词分两格。** 这一层的 `reason` 是导演动作自己的词表
            # (`unknown_place` / `not_colocated` / `not_held` / `refused` / …),
            # 而能力那一层的词是 `conditions` / `incapable` / `busy` / `absent`。
            # `**outcome` 摊在后面会把前者盖掉,于是同一个键在不同的失败上说着
            # 两种语言 —— 宿主照着 `reason` 分支,遇上"她累坏了"就掉进 else。
            # 细的那个照样交出去,只是它有自己的名字。
            world_said = {k: v for k, v in outcome.items() if k != "reason"}
            return DirectorOutcome(
                ok=False, text=f"({name}没能动手:{refusal})",
                detail={"target": resolved, "action": "interact", "object": obj,
                        "verb": verb, **world_said, "reason": "refused",
                        "refusal_kind": str(outcome.get("reason") or "")},
            )
        return DirectorOutcome(
            ok=True, text=f"({name}对{obj}动手了。)",
            detail={"target": resolved, "action": "interact", "object": obj,
                    "verb": verb, **outcome},
        )

    # ── 异地就只能打电话 ────────────────────────────────────────────────────

    def _colocation_refusal(
        self, resolved: str, name: str, action: str, player_id: str | None
    ) -> DirectorOutcome | None:
        """他不在她跟前时,哪几条导演动作办不到。过得了就是 `None`。

        在这之前玩家是个幽灵:不管角色在哈尔滨还是三亚,他都能面对面说话、给东西、
        一起做事 —— **位置这个维度等于白设计了**。而引擎里位置从来都是真的
        (走路花时间、同地才看得见对方身上的量、`reach_out` 老早就拒绝不在场的人),
        只有玩家这一侧一直没人管。

        **默认不生效**(`presence.enforce_colocation`)。这不是犹豫,是账:引擎侧
        收紧会**当场打断线上世界** —— `player_move` 是宿主可选调用,今天线上根本
        没人调,于是"异地"是每一次调用的默认值,一开就是 `give` 全线开始拒绝。
        迁移的次序只能是"先让宿主调 `player_move`,再开这个开关"。

        拒绝要给**明确回执**,而且三种原因分得开:不在同一个地方 / 世界不知道你在
        哪 / 世界不知道**她**在哪。合成一句"你不在她跟前"的话,一个宿主根本没接
        `player_move` 的世界,看起来会像是玩家自己站错了地方 —— 而他做什么都改不了。
        """
        if action not in FACE_TO_FACE_ACTIONS:
            return None
        try:
            enforced = bool(self._runtime.config("presence.enforce_colocation", False))
        except Exception:  # noqa: BLE001 - 读不到配置就维持今天的行为
            enforced = False
        if not enforced:
            return None
        if not player_id:
            return DirectorOutcome(
                ok=False, text="(不知道你是谁,这件事得当面。)",
                detail={"target": resolved, "action": action,
                        "reason": "unknown_player"},
            )
        here = self._runtime.agent_location(resolved)
        where = ""
        try:
            where = str(self._runtime.player_location(player_id) or "")
        except Exception:  # noqa: BLE001
            where = ""
        if self._runtime.face_to_face(resolved, player_id):
            return None
        points = self._points()
        if not where:
            reason, text = "unknown_player_location", (
                f"(这件事得当面,而世界不知道你这会儿在哪 —— "
                f"{name}在{points.get(here) or here or '别处'}。"
                f"隔着这么远,你只能跟她说话。)"
            )
        elif not here or here == where:
            # 两处地名一样却不是面对面 —— **只可能是她在赶路**(`face_to_face` 与
            # `_where_is` 同一条规矩:在途即不在任何地方)。照 `agent_location` 那份
            # 直说的话,回执会写成"你在咖啡店,她在咖啡店 —— 这件事得当面",
            # 一句技术上没错、而玩家读起来是谎的话。
            reason, text = "agent_in_transit", (
                f"({name}这会儿在路上,不在任何地方 —— 等她落脚再说;"
                f"话倒是随时说得上。)"
            )
        else:
            reason, text = "elsewhere", (
                f"(你在{points.get(where) or where}，{name}在"
                f"{points.get(here) or here} —— 这件事得当面。"
                f"隔着这么远,你只能跟她说话。)"
            )
        return DirectorOutcome(
            ok=False, text=text,
            detail={"target": resolved, "action": action, "reason": reason,
                    "player_location": where, "agent_location": here,
                    "enforced_by": "presence.enforce_colocation"},
        )

    def _together(
        self, resolved: str, name: str, obj: str, verb: str,
        player_id: str | None, params: dict[str, Any],
    ) -> DirectorOutcome:
        """「我们一起在树下坐会儿」—— 玩家把自己算进这件事里。

        和 `_interact` 的分界是**玩家在不在里面**:那一条是"你去雕那座冰雕"
        (她动手,他看着),这一条是"我们一起雕"(两个人都在,两个人都付代价,
        而且**她可以不答应**)。走的仍然是 `interact_with` 那条统一路径,只是
        多带一份名单 —— 另写一份"玩家版本的一起做事"迟早和她自己发起的那份分叉。

        **「带我去某个地方」也是一起做的一件事**,而且是玩家最先想得到的那一件 ——
        见 `_go_together`。
        """
        place = str(params.get("place") or params.get("location") or "").strip()
        if place:
            # 分类器早就把它填进 `place` 了(线上原话「带我去江堤走走」判出来的是
            # `{"action":"together","place":"江堤","object":"江堤","verb":"走走"}`),
            # 而这一层从前只认 `object`:拿「江堤」去**实体**表里查,查出四样东西
            # (长椅、路灯、斜坡阶、老樟树),于是她开口之前先有一句
            # 「江堤 对得上好几样东西……说准一点」。他要的是一个**地方**,
            # 而这个世界里正好有一个叫江堤的地方。
            points = self._points()
            where, _ = resolve_place(place, points)
            if where is not None and (
                not obj or resolve_place(obj, self._entities())[0] is None
            ):
                # `object` 也指得着一样东西时不抢:「我们去江堤那棵老樟树下坐会儿」
                # 说的是那棵树,而能力那条路上有作者声明的效果、代价与她的同意,
                # 比"走过去"具体得多。
                return self._go_together(resolved, name, where, points, player_id)
        if not obj:
            return DirectorOutcome(
                ok=False, text="(一起做什么?说具体一点 —— 得有个东西。)",
                detail={"target": resolved, "action": "together", "reason": "empty_object"},
            )
        if not player_id:
            return DirectorOutcome(
                ok=False, text="(不知道你是谁,算不进这件事里。)",
                detail={"target": resolved, "action": "together", "reason": "unknown_player"},
            )
        entities = self._entities()
        if entities and obj not in entities:
            matched, candidates = resolve_place(obj, entities)
            if matched is not None:
                obj = matched
            elif candidates:
                readable = "、".join(f"{entities.get(e) or e}({e})" for e in candidates)
                return DirectorOutcome(
                    ok=False, text=f"({obj} 对得上好几样东西:{readable};说准一点。)",
                    detail={"target": resolved, "action": "together", "object": obj,
                            "reason": "ambiguous_object", "candidates": candidates},
                )
        # 名单 = 玩家 + 玩家点名的其他人。**玩家自己一定在里面** —— 这句话的主语
        # 就是他,不把他算进去的话"一起"两个字就没有落点。
        party = [f"player:{player_id}"]
        raw_with = params.get("with") or params.get("others") or []
        if isinstance(raw_with, str):
            raw_with = [p for p in raw_with.replace("、", ",").split(",") if p.strip()]
        party.extend(str(p).strip() for p in raw_with if str(p).strip())

        from anima_world.tools.base import ToolCallError

        try:
            # **他自己开的口 = 他的同意。** 走私有的 `_interact_with` 只为了这一
            # 件事:把他自己那一票先记上。不记的话,3.6.0 的邀请门会对着刚说出
            # 「陪我听完这一面」的那个人落一条 `agent_invites`,问他要不要做他
            # 刚说的事 —— 而那封信今天没有任何一处看得见,于是这句话在世界里
            # 什么也没发生。**她点他的名那条路一个字不变**(`tools/body.py` 的
            # `interact(with=["我"])`):那是她的意思,他仍然得自己答。
            outcome = self._runtime._interact_with(
                resolved, obj, verb or "look", participants=party, player_id=player_id,
                accepted_ids=[f"player:{player_id}"],
            )
        except ToolCallError as exc:
            return DirectorOutcome(
                ok=False, text=f"({exc})",
                detail={"target": resolved, "action": "together", "object": obj,
                        "verb": verb, "reason": "invalid"},
            )
        if not outcome.get("ok"):
            # **拒绝理由原样带出来。** 「他睡着了」「他不想」「她这会儿做不了」
            # 是三件事,玩家的下一步完全不同 —— 合成一句"没成"等于把那份区分丢掉。
            return DirectorOutcome(
                ok=False, text=f"({outcome.get('refusal') or '这会儿不行'})",
                detail={"target": resolved, "action": "together", "object": obj,
                        "verb": verb, **outcome},
            )
        return DirectorOutcome(
            ok=True, text=f"(你和{name}一起做了这件事。)",
            detail={"target": resolved, "action": "together", "object": obj,
                    "verb": verb, **outcome},
        )

    def _go_together(
        self, resolved: str, name: str, where: str,
        points: dict[str, str], player_id: str | None,
    ) -> DirectorOutcome:
        """「带我去江堤走走」—— 她起程,**而他真的跟着一起走**。

        这是玩家最先想得到的那一件"一起做的事",而在这之前世界里没有任何动词兑现
        得了它:`walk` 只挪她一个人。线上两次现场都是同一个样子 —— 她答应得好好的
        (「行，走吧。潮汐里三号不远，过两条巷子就到」「走吧，从这儿过去十来分钟，
        你跟紧点」),然后**她一个人走了**,玩家还站在原地;要么就是她压根没动,
        两轮之后散文里的人已经在三楼掏钥匙,而世界里她还在唱片店。issue #15
        那句话("说'我走了'也没真走")漏在了"带上我"这一格。

        三条:

        - **两个人是分别走的两段路。** 她走她那条(`move_agent`,和排班走的是同一条
          路:发 travel、花时间、在途不可打断),他走他那条(`player_walk`)。
          瞬移一个、走路一个,才是把"一起"两个字写成谎。
        - **他走不了就整件事不算数**(他自己正在赶别的路)。只挪她一个的话,回执
          写着"带你去",而世界里是她走了、他留下 —— 比不做更坏。所以**先问他那半**:
          她那半只会因为参数不合法而失败,而那两样在上面已经查过了。
        - **必须同处一地,而且这一条不挂在 `presence.enforce_colocation` 上。**
          那个开关默认关,理由是引擎侧收紧会当场打断线上世界 —— 而这条路是新的,
          没有旧行为可打断;隔着半个镇子说"带我去"本来就不成立,兑现它等于让她
          在两条街外把他领走。那时候该她说的是"你先过来"。
        """
        place_name = points.get(where) or where
        if not player_id:
            return DirectorOutcome(
                ok=False, text="(不知道你是谁,带不上你。)",
                detail={"target": resolved, "action": "go_together", "place": where,
                        "reason": "unknown_player"},
            )
        if not self._runtime.face_to_face(resolved, player_id):
            return DirectorOutcome(
                ok=False, text=f"(你们不在一块儿,{name}带不上你 ——"
                               f"让他先过来,或者你自己走过去。)",
                detail={"target": resolved, "action": "go_together", "place": where,
                        "reason": "not_colocated"},
            )
        if self._runtime.agent_location(resolved) == where:
            # 已经在那儿了 —— 这不是失败,只是没有路可走。**别发一次假的行程**:
            # 回执说"往江堤去了"而谁也没动,是这一层最不该有的那种话。
            return DirectorOutcome(
                ok=False, text=f"(你们已经在{place_name}了。)",
                detail={"target": resolved, "action": "go_together", "place": where,
                        "reason": "already_there"},
            )
        if not self._runtime.player_do_action(player_id, "walk", {"location": where}):
            return DirectorOutcome(
                ok=False, text="(你这会儿在赶别的路,跟不过去。)",
                detail={"target": resolved, "action": "go_together", "place": where,
                        "reason": "player_busy"},
            )
        moved = self._runtime.move_agent(resolved, where)
        return DirectorOutcome(
            ok=True, text=f"({name}带着你往{place_name}去了。)",
            detail={"target": resolved, "action": "go_together", "place": where,
                    "place_name": place_name, "took": True, **moved},
        )

    def _give(
        self, resolved: str, name: str, obj: str, player_id: str | None, detail: str
    ) -> DirectorOutcome:
        """「我把这条红围巾给你」—— 玩家把随身的一样东西交给她。

        这一条和其余几条**反过来**:动作的施动者是玩家,不是角色。所以它不走
        `do_action`,走账本(`item_transfer`)—— 经济那一层的第一条设计是"库存是
        事件的投影",凭空给她一件东西而不记账,下一次重放就把它抹掉了。

        **玩家手上没有的东西给不出去**:不挡的话,一句话就能把任何东西变出来,
        而库存扣不到负数,于是账面上连痕迹都没有。
        """
        wanted = obj or detail
        if not wanted:
            return DirectorOutcome(
                ok=False, text="(给什么?说具体一点。)",
                detail={"target": resolved, "action": "give", "reason": "empty_object"},
            )
        if not player_id:
            return DirectorOutcome(
                ok=False, text="(不知道你是谁,东西递不过去。)",
                detail={"target": resolved, "action": "give", "reason": "unknown_player"},
            )
        # `LookupError` 是"你手上没有这个",`ToolCallError` 是"这个调用讲不通"。
        # 两种都得在这儿收成一句给玩家的话:漏出去的话它会一路穿过 `World.chat`
        # 打到宿主的请求处理器上,而玩家那一句只是"我把围巾给你"。
        from anima_world.tools.base import ToolCallError

        try:
            handed = self._runtime.give_item(player_id, resolved, wanted)
        except (LookupError, ToolCallError) as exc:
            return DirectorOutcome(
                ok=False, text=f"({exc})",
                detail={"target": resolved, "action": "give", "object": wanted,
                        "reason": "not_held"},
            )
        return DirectorOutcome(
            ok=True, text=f"({handed['item_name']}给{name}了。)",
            detail={"target": resolved, "action": "give", **handed},
        )

    def _leave(self, resolved: str, name: str, here: str) -> DirectorOutcome:
        """没指定去哪的"走开"。目的地由引擎挑,所以回执里说清楚挑了哪儿。"""
        points = self._points()
        options = [pid for pid in sorted(points) if pid != here]
        if not options:
            return DirectorOutcome(
                ok=False, text="(这个世界只有一个地方,走不掉。)",
                detail={"target": resolved, "reason": "nowhere_to_go"},
            )
        where = options[0]
        place_name = points.get(where) or where
        moved = self._runtime.move_agent(resolved, where)
        return DirectorOutcome(
            ok=True, text=f"({name}走了 —— 往{place_name}去的。)",
            detail={"target": resolved, "action": "leave", "place": where,
                    "place_name": place_name, **moved},
        )

    def _act(
        self, resolved: str, name: str, detail: str, player_id: str | None
    ) -> DirectorOutcome:
        """「让他做件事」—— 兑现成**他真的知道了这件事**。

        从前这一条只发一个 `agent_action{action:"directed"}` 事件:全仓库一个写入点、
        零个读取点,行为树不认、planner 不认、提示词里也没有它,而引擎回给玩家的是
        "(X 照做了。)"。那是这个仓库最怕的那种坏 —— **什么都没做却报成功**,比不支持
        更坏,因为玩家永远不会知道。

        兑现走现成的那条路(和 `broadcast` 同一条):给他发一条 `memory_seed`。记忆
        本来就是这个引擎里"角色知道一件事"的表示,而它有真的消费方 —— 聊天提示词的
        记忆块、`Planner._memory_block` 排明天的一天。于是玩家这句话进得了他的决定。

        **兑现的是"他知道了",不是"他做了"**,所以回执也只敢这么说。让他做不做得成,
        那是他的性格、他的行为树、他手上的事说了算 —— 引擎替他答应下来,就又是一次
        撒谎。要一句"言听计从",办法是给他写一份言听计从的人设,不是在这儿开后门。
        """
        if not detail:
            return DirectorOutcome(
                ok=False, text="(要他做什么?说具体一点。)",
                detail={"target": resolved, "reason": "empty_detail"},
            )
        loc = self._runtime.agent_location(resolved) or None
        who = "有人"
        if player_id:
            try:
                who = self._runtime.player_name(player_id) or player_id
            except Exception:  # noqa: BLE001 - 读不到名字不该让指令落空
                who = player_id
        self._runtime.emit({
            "type": "agent_action",
            "who": resolved,
            "loc": loc,
            "payload": {"action": "directed", "detail": detail, "directed_by": "player"},
        })
        self._runtime.emit({
            "type": "memory_seed",
            "who": resolved,
            "loc": loc,
            "payload": {
                "agent_id": resolved,
                "kind": "directive",
                "summary": f"{who}要我{detail}",
                # 比八卦重、比创世锚点轻:一条"有人当面要求我做的事"该在下一次
                # 想起来的前几条里,但不该永远压住她自己的人生。
                "importance": 0.7,
            },
        })
        return DirectorOutcome(
            ok=True,
            text=f"(带到了 —— {name}知道你要她{detail}了;做不做是{name}自己的事。)",
            detail={"target": resolved, "action": "act", "detail": detail,
                    "delivered_as": "memory"},
        )

    def _come_here(
        self,
        agent_id: str,
        resolved: str,
        name: str,
        here: str,
        self_directed: bool,
        player_id: str | None,
    ) -> DirectorOutcome:
        """缺省动作:把人真的挪到"这儿"。

        **"这儿"是谁那儿,取决于被指挥的是谁。** 指挥别人 = 到说话人身边来;而
        「你过来」是对**她**说的,说话人就是她自己,那句话里的"这儿"只能是**玩家**
        那儿。照旧读说话人的位置的话,自指的 `come_here` 永远是"她本来就在这儿"——
        一个恒真的空动作。
        """
        destination = here
        if self_directed:
            destination = ""
            if player_id:
                try:
                    destination = str(self._runtime.player_location(player_id) or "")
                except Exception:  # noqa: BLE001
                    destination = ""
            if not destination:
                return DirectorOutcome(
                    ok=False, text="(不知道你这会儿在哪,过不去。)",
                    detail={"target": resolved, "reason": "unknown_player_location"},
                )
        if not destination:
            return DirectorOutcome(
                ok=False, text="(不知道你们这会儿在哪,叫不过来。)",
                detail={"target": resolved, "reason": "unknown_location"},
            )
        points = self._points()
        place_name = points.get(destination) or destination
        if self._runtime.agent_location(resolved) == destination:
            return DirectorOutcome(
                ok=True, text=f"({name}本来就在这儿。)",
                detail={"target": resolved, "action": "come_here", "place": destination,
                        "place_name": place_name, "already_here": True},
            )
        moved = self._runtime.move_agent(resolved, destination)
        return DirectorOutcome(
            ok=True, text=f"({name}过来了。)",
            detail={"target": resolved, "action": "come_here", "place": destination,
                    "place_name": place_name, **moved},
        )

    # ── 指挥她本人时,塞进这一轮提示词的那句事实 ──────────────────────────

    def _grounding(self, outcome: DirectorOutcome, *, action: str, detail: str) -> str:
        """**只陈述刚刚真发生了什么,不下命令。**

        写成"你必须答应"就是在判定逻辑外面开了第二道后门:一个引擎强加的服从。
        言听计从该由人设(见测试角色「零」)决定,不由这一句决定 —— 所以这里只给
        事实,连"该不该照办"都不提。唯一硬的一句是**别自相矛盾**:她已经在路上了
        还说自己没动,那就是提示词和世界打架,而玩家看到的是引擎在撒谎。
        """
        info = outcome.detail or {}
        if not outcome.ok:
            return (
                "【刚刚发生的事｜按这个事实说话】"
                f"对方要你做的这件事没能在世界里兑现:{outcome.text.strip('()（）')}。"
                "你**没有**动,也没有做成。别顺口答应成「我已经去了」。"
            )
        place = str(info.get("place_name") or info.get("place") or "").strip()
        done = info.get("action")
        if done in _BODY_ACTIONS:
            # **说"已经在做"而不是"该去做"** —— 世界里那条 `agent_action` 已经发了。
            # 写成命令的话,提示词和世界又变成两份说法,而玩家看得见的是她的那一份。
            return (
                "【刚刚发生的事｜按这个事实说话】"
                f"对方让你{_BODY_ACTIONS[done][1].rstrip('了')},而你**已经动手了**。\n"
                "这不是打算,是已经发生的事:别说「我这就去」然后还杵在原地。"
            )
        if done == "talk_to":
            other = info.get("object_name") or info.get("object") or "那个人"
            return (
                "【刚刚发生的事｜按这个事实说话】"
                f"对方让你去找{other}说话,而你**已经去了**。按「已经去搭话了」说。"
            )
        if done == "interact":
            return (
                "【刚刚发生的事｜按这个事实说话】"
                f"对方让你对{info.get('object')}动手,而你**已经做了**,而且世界里真的"
                "变了一点。按「刚做完」说,别说成打算。"
            )
        if done == "give":
            item = info.get("item_name") or info.get("item_id") or "那样东西"
            return (
                "【刚刚发生的事｜按这个事实说话】"
                f"对方刚把{item}递给你,你**已经拿在手上了** —— 它此刻真的在你随身的"
                "东西里。收不收得下已经不是问题了;要还回去是另一件事,得你自己开口。"
            )
        if info.get("action") == "act":
            return (
                "【刚刚发生的事｜按这个事实说话】"
                f"对方当面要你:{detail}。这件事你已经听见并记住了。\n"
                "答应不答应、做不做,由你自己是谁决定 —— 但**别只在嘴上答应**:"
                "你要是答应了,接下来就得真去做。"
            )
        if info.get("already_here"):
            return (
                "【刚刚发生的事｜按这个事实说话】"
                f"对方叫你过去,而你本来就在{place or '他那儿'} —— 你们已经在一起了。"
            )
        if info.get("in_transit"):
            return (
                "【刚刚发生的事｜按这个事实说话】"
                f"对方让你去{place or '那儿'},而你**已经动身了,此刻正在路上**。\n"
                "这不是打算,是已经发生的事:别说「我就不去」,也别说「我这就去」然后"
                "还站在原地 —— 你人已经在往那儿走了。"
            )
        return (
            "【刚刚发生的事｜按这个事实说话】"
            f"对方让你去{place or '那儿'},而你**已经到了**。回话时按「人已经在这儿」说。"
        )
