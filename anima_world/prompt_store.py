"""提示词模板即数据(M5):默认模板、回落规则与保存前渲染校验。

Chat/narrative 的提示词措辞曾是散在 `chat_service.py` / `chat_session.py` /
`narrative.py` 里的字符串字面量;现在按名字存进世界,拼提示词时现读,
管理员经 `World.prompt_set` 改完下一次调用即生效,无需重启(design.md D3)。
保存前按调用点真实传入的实参集合试渲染(design.md D7),打错占位符在保存时
报错,而不是在她开口那一刻静默丢块;只存当前值,不存历史。

这个模块是**后端无关的那一半**:`_DEFAULTS`(引擎当前的默认模板)、
`resolve`(回落规则)、`merged_listing`(带 `source` 的合并视图)、
`check_renders`(渲染校验)。存储在
`anima_world.redis_state.RedisPromptStore`;SQLite 版 `PromptStore` 已随
world.db 层退役。
"""

from __future__ import annotations

import string
from typing import Any

from anima_world.planner import _DEFAULT_PROMPT as _PLANNER_FREETIME
from anima_world.chat_service import (
    _DEFAULT_SYSTEM_PERSONA_TEMPLATE as _CHAT_SYSTEM_PERSONA,
)
# R1 人设尾部重注。**引用,不复制** —— 这个文件里每个模板都是从 `chat_service`
# 引过来的别名,理由就是不许有第二份:两份逐字相同的默认模板,改一份就静默分叉。
from anima_world.chat_service import (
    _DEFAULT_PERSONA_ANCHOR_TEMPLATE as _CHAT_PERSONA_ANCHOR,
)
from anima_world.chat_service import (
    _DEFAULT_PRESENCE_TEMPLATE as _CHAT_PRESENCE,
)
from anima_world.chat_service import (
    _DEFAULT_RELATION_TEMPLATE as _CHAT_RELATION,
)
from anima_world.chat_service import (
    _DEFAULT_RESPONSE_FORMAT_TEMPLATE as _CHAT_RESPONSE_FORMAT,
)
from anima_world.chat_service import (
    _DEFAULT_WORLD_MEMORY_TEMPLATE as _CHAT_WORLD_MEMORY,
)
from anima_world.chat_service import (
    _DEFAULT_SILENT_TURN_TEMPLATE as _CHAT_SILENT_TURN,
)
from anima_world.chat_service import (
    _DEFAULT_LOOP_CONTINUE_TEMPLATE as _CHAT_LOOP_CONTINUE,
)
from anima_world.chat_service import (
    _DEFAULT_LOOP_INTERRUPT_TEMPLATE as _CHAT_LOOP_INTERRUPT,
)
from anima_world.chat_service import (
    _DEFAULT_OVERRIDES_BLOCK_TEMPLATE as _CHAT_OVERRIDES,
)
from anima_world.chat_service import (
    _DEFAULT_REFUSED_TOPIC_TEMPLATE as _CHAT_REFUSED_TOPIC,
)
from anima_world.chat_service import (
    _DEFAULT_TOOLS_BLOCK_TEMPLATE as _CHAT_TOOLS,
)
from anima_world.intent import DEFAULT_CLASSIFIER_PROMPT as _INTENT_CLASSIFIER
from anima_world.autonomy import DEFAULT_DECIDE_PROMPT as _AUTONOMY_DECIDE
from anima_world.contact import DEFAULT_COMPOSE_PROMPT as _CONTACT_COMPOSE
from anima_world.perception import (
    DEFAULT_PERCEPTION_BLOCK_TEMPLATE as _CHAT_PERCEPTION,
)
from anima_world.stance import DEFAULT_STANCE_BLOCK_TEMPLATE as _CHAT_STANCE
from anima_world.narrative import (
    _MOCK_MEMORY_SUFFIX,
    _MOCK_TEMPLATE_DEFAULTS,
    MOCK_MEMORY_SUFFIX_NAME,
    MOCK_TEMPLATE_PREFIX,
)
from anima_world.relationship_judge import _DEFAULT_PROMPT as _JUDGE_RELATIONSHIP
from anima_world.relationship_judge import _DEFAULT_RELABEL_PROMPT as _JUDGE_RELABEL
from anima_world.relationship_judge import _DEFAULT_USER_JUDGE_PROMPT as _JUDGE_USER
from anima_world.relationship_judge import _DEFAULT_HEARSAY_PROMPT as _JUDGE_HEARSAY
from anima_world.relationship_judge import _DEFAULT_INVITE_PROMPT as _JUDGE_INVITE


class PromptRenderError(ValueError):
    """A template references a placeholder its call site doesn't provide."""


# name -> representative variables the corresponding call site passes at
# runtime, used to render-check a template before it's persisted.
_SAMPLE_VARS: dict[str, dict[str, Any]] = {
    "chat.system_persona": {"name": "夏", "personality": "开朗热情"},
    "chat.memory_block": {"summaries": "- 上次聊了咖啡馆的事"},
    "chat.response_format": {"name": "夏"},
    "chat.persona_anchor": {"name": "夏", "personality": "开朗热情"},
    "chat.session_summary": {},
    "narrative.describe": {},
    "world.setting": {},
    # #9: what the Mock provider actually passes — an authored template that
    # reaches for anything else gets rejected at save time, not at tick time.
    MOCK_MEMORY_SUFFIX_NAME: {"summary": "昨天在咖啡店和夏聊过"},
    **{
        f"{MOCK_TEMPLATE_PREFIX}{kind}": {"agent": "遥", "location": "cafe", "target": "夏"}
        for kind in _MOCK_TEMPLATE_DEFAULTS
    },
    "judge.relationship": {
        "a_name": "夏", "a_personality": "开朗", "a_goals": "把店开好", "a_memories": "（无）",
        "b_name": "遥", "b_personality": "冷静", "b_goals": "（无特别目标）", "b_memories": "（无）",
        "a_to_b": 0.1, "b_to_a": 0.0, "r_type": "朋友", "r_type_back": "朋友",
        "location": "cafe",
    },
    "judge.relabel": {
        "a_name": "夏", "a_personality": "开朗", "b_name": "遥", "b_personality": "冷静",
        "old_r_type": "点头之交", "old_band": "熟识", "new_band": "亲近", "memories": "（无）",
    },
    "judge.hearsay": {
        "a_name": "夏", "a_personality": "开朗", "memories": "（无）",
        "roster": "- 沈遥：+0.30\n- 阿檀：+0.60",
        "rumor": "听沈遥说:阿檀最近老往柔那儿跑", "location": "cafe",
    },
    "judge.invite": {
        "a_name": "遥", "a_personality": "话少,慢热", "memories": "（无）",
        "inviter": "苏晚夏", "a_to_b": "0.42", "location": "cafe",
        "invitation": "苏晚夏叫你一起在门口那棵老橡树下小坐",
        # 他俩这会儿还在说的话。**整块由引擎渲染**(没说过话就是空串),作者只决定
        # 它摆在哪儿 —— 所以样本给的是渲染好的那个样子,不是一份消息列表。
        "recent_talk": "遥和苏晚夏这会儿正说着话：\n  苏晚夏：你还记得那把旧伞吗\n",
    },
    "chat.world_memory_block": {"memories": "- 债务解除了"},
    "chat.presence_block": {
        "day": 5, "hh": "20", "mm": "15", "location": "酒吧", "activity": "在值班",
        "others": "罗本",
        # 她去得了哪儿。**只有名字,没有 id** —— `walk` / `walk_away` 都收人话了
        # (`resolve_location`),而这一行是给她读的那份(`places_menu(with_ids=False)`)。
        # 作者拿这个样本预览时看见的写法,就是她真收到的那个写法。
        "places": "酒吧、码头、老陈的面馆",
    },
    "chat.relation_block": {"r_type": "有点好奇的新面孔", "band": "熟识"},
    "chat.perception_block": {"lines": "- 你自己:功力 120"},
    # chat-agent(1.3.0):四块新提示词的实参。作者改坏了要在**保存时**报错,
    # 而不是在她该选 stance 那一刻静默丢块。
    "chat.stance_block": {
        "stance_menu": "- neutral（中性）：平常回话", "current": "上一轮你对 ta 的意图是「讨好」。\n",
    },
    "chat.tools_block": {"tool_menu": "- mute：屏蔽这个人一段时间 参数:minutes:必填"},
    "chat.overrides_block": {"rules": "- 怎么称呼玩家：霜霜"},
    "chat.refused_topic_block": {"keywords": "彩票"},
    "chat.intent_classifier": {
        "present": "林素", "recent": "user: 冷不冷?",
        "places": "咖啡店(cafe)、家(home)、建筑工作室(workshop)",
        "speaker": "苏晚夏",
    },
    "chat.silent_turn": {"name": "苏晚夏"},
    "chat.loop_continue": {"emitted": 2, "left": 3},
    "chat.loop_interrupt": {"text": "等等,我不是这个意思"},
    "autonomy.decide": {
        "name": "苏晚夏", "personality": "开朗热情", "day": 3, "hh": "20", "mm": "15",
        "location": "咖啡店", "activity": "闲着",
        "present_block": "这会儿在你身边的人:阿檀(p1)。\n",
        "state_block": "- 你此刻的心气儿:0.62(0~1)\n",
        "tool_menu": "- reach_out:主动去找一个此刻在场的人开口 参数:player_id, text",
    },
    "contact.compose": {
        "name": "苏晚夏", "personality": "开朗热情", "day": 3, "hh": "20", "mm": "15",
        "location": "咖啡店", "player": "阿檀",
        "reason_block": "- 他交代过我一件事:阿檀要我去把窗边那束花换掉\n",
        "state_block": "- 你此刻的心气儿:0.62(0~1)\n",
    },
    "judge.user_relationship": {
        "a_name": "夏", "a_personality": "开朗", "player_name": "阿檀",
        "a_to_b": 0.1, "b_to_a": 0.0, "r_type": "初次见面的访客",
        "transcript": "user: 你好\nassistant: 你好呀", "location": "cafe",
    },
    "planner.freetime": {
        "name": "苏晚夏", "personality": "开朗热情", "day": 3,
        "free_windows": "- 18:00 到 23:00", "action_space": "- walk（location 可选：cafe）",
        "memories": "",
        # prompt-grounding: the call site renders goals as one block; listed
        # here so an admin-saved template referencing {goals} passes
        # check_renders instead of being rejected.
        "goals": "你当前的目标：\n- 示例目标\n\n",
        # 同上:处境也是一个整块,列在这里让引用 {situation} 的模板能过渲染检查。
        "situation": "你此刻的处境：\n- 你现在在咖啡店\n\n",
    },
}

_WORLD_SETTING = """【世界】
这里是临海市旧港区，二零四几年的一个平常年份。城市早就数字化了，旧港区却还慢着：手冲咖啡、纸质草图、颜料、和总也下不完的雨。
街区只有三个地方——咖啡店（cafe）、建筑工作室（workshop）、以及一间用来画画的家（home）。走出这三处，世界就模糊了，没人会去想那之外有什么。

【居民】
住在这里的都是有血有肉的普通人：会累，会走神，会对彼此生出好感或疏远，也会遗忘。
每个人只记得自己亲身经历过的事，读不到别人心里在想什么。
偶尔会有一位「访客」来搭话。没人知道他从哪来，就当是个新面孔，自然地招呼、闲聊。

【怎么说话】
说中文，口语，短句。始终以角色本人的身份开口。
不解释设定，不提"AI""模型""提示词"，也不承认自己是虚构的。"""

_DEFAULTS: dict[str, tuple[str, str]] = {
    "planner.freetime": (
        _PLANNER_FREETIME,
        "LLM 规划 agent 自由时间动作序列的 prompt",
    ),
    "world.setting": (
        _WORLD_SETTING,
        "世界基础设定（世界观）——注入所有 agent 的 chat 与 narrative 提示词",
    ),
    # 和 `chat_service._DEFAULT_SYSTEM_PERSONA_TEMPLATE` 逐字一致 ——
    # 那边写着为什么末尾要有"以人设为准"这一句。
    "chat.system_persona": (
        _CHAT_SYSTEM_PERSONA,
        "Chat 系统人设模板（末句定下人设优先于通则，作者才写得出言听计从的角色）",
    ),
    "chat.persona_anchor": (
        _CHAT_PERSONA_ANCHOR,
        "Chat 人设尾部重注——整段提示词最后一块，把她按回原样"
        "（R1，默认关，chat.persona_anchor.enabled 点亮）",
    ),
    "chat.memory_block": (
        "你和对方过去的对话回顾：\n{summaries}",
        "Chat 跨会话记忆拼接模板",
    ),
    "chat.response_format": (
        _CHAT_RESPONSE_FORMAT,
        "Chat 回复格式规则——动作描写的括号与角色名前缀（英文世界或不要动作描写的世界改这里）",
    ),
    "chat.session_summary": (
        "用一句中文概括这次对话的主要内容和情绪基调。只输出摘要，不要解释。",
        # 只管语气与详略:谁是谁、在哪、不许编造转录里没有的人物地点,由引擎拼在
        # 这段文字的前后(`chat_session._SUMMARY_FACT_HEADER` / `_SUMMARY_GUARD`)——
        # 覆盖这个模板改不掉那两半,理由见那边的注释。
        "会话关闭时生成总结用的 prompt（只定语气与详略）",
    ),
    "chat.world_memory_block": (
        _CHAT_WORLD_MEMORY,
        "Chat 世界记忆块——MemoryStore top-K 进 prompt（chat-grounding）",
    ),
    "chat.presence_block": (
        _CHAT_PRESENCE,
        "Chat 在场块——时间/地点/当前活动/同地者（chat-grounding）",
    ),
    "chat.relation_block": (
        _CHAT_RELATION,
        "Chat 关系块——对来访者的 r_type 与档位（chat-grounding）",
    ),
    "autonomy.decide": (
        _AUTONOMY_DECIDE,
        "定时轮次的 prompt——没人跟她说话时问她要不要自己做点什么（autonomy.enabled）",
    ),
    "contact.compose": (
        _CONTACT_COMPOSE,
        "她想起一个不在跟前的玩家时，那句「想说什么」的线索（contact.enabled；"
        "**判定不在这儿**，这条只写线索）",
    ),
    "chat.perception_block": (
        _CHAT_PERCEPTION,
        "她感知到的世界的量（perception；声明了可见性才有内容，见 §2.9.4）",
    ),
    "chat.stance_block": (
        _CHAT_STANCE,
        "Chat 关系性意图块——八个 stance 的菜单与惯性提示（#18；chat.stance.enabled）",
    ),
    "chat.tools_block": (
        _CHAT_TOOLS,
        "Chat 能力菜单——她可以走开/静音/拒谈话题（#15；chat.tools.enabled）",
    ),
    "chat.overrides_block": (
        _CHAT_OVERRIDES,
        "玩家教给这个角色的对话规则块（#16，写进库就永久生效）",
    ),
    "chat.refused_topic_block": (
        _CHAT_REFUSED_TOPIC,
        "对方又提到她拒绝谈的话题时插进去的一段（#15）",
    ),
    "chat.intent_classifier": (
        _INTENT_CLASSIFIER,
        "意图分类器的 prompt——dialogue / narrative_direction / style_adjust（#16）",
    ),
    "chat.silent_turn": (
        _CHAT_SILENT_TURN,
        "她整轮只调了个能力、一句台词都没有时，补给玩家的那句旁白"
        "（**不是提示词**，是直接进回复的一行；空着零个字节和「应用崩了」没有区别）",
    ),
    "chat.loop_continue": (
        _CHAT_LOOP_CONTINUE,
        "连续输出时提醒她还能说几句、以及怎么让位（#17）",
    ),
    "chat.loop_interrupt": (
        _CHAT_LOOP_INTERRUPT,
        "玩家在她说话时插话——接着说还是转向由她自己判（#17）",
    ),
    "judge.relationship": (
        _JUDGE_RELATIONSHIP,
        "chat 后 LLM 判定对话摘要与双向好感变化的 prompt（llm-relationship-judge）",
    ),
    "judge.relabel": (
        _JUDGE_RELABEL,
        "关系跨档后 LLM 改写 r_type 描述的 prompt（relationship-stage-machine）",
    ),
    "judge.user_relationship": (
        _JUDGE_USER,
        "玩家会话关闭后按真实转录判双向好感变化的 prompt（player-visitor）",
    ),
    "judge.hearsay": (
        _JUDGE_HEARSAY,
        "她听到一句八卦之后的反应——吃醋走的是这条判定，不是自动扣分"
        "（social.hearsay_reaction.enabled）",
    ),
    "judge.invite": (
        _JUDGE_INVITE,
        "有人叫她一起做件事，她答不答应——一起做事那条拒绝路径走的是这份人设，"
        "不是一条「关系够近就答应」的规则",
    ),
    "narrative.describe": (
        "请用中文生成一句适合作为这个 agent 的世界叙事或聊天回复。只输出正文，不要解释。",
        "LLM narrative provider 的描述指令",
    ),
    MOCK_MEMORY_SUFFIX_NAME: (
        _MOCK_MEMORY_SUFFIX,
        "没有 LLM 时,叙事后面缀的那句「还记着…」（{summary}）",
    ),
    # #9: Mock 叙事的每一种动作各一条模板。没有配 key 是默认状态,所以这些是
    # 第一屏的文字 —— 它们必须跟着世界走,而不是跟着引擎写死的语言走。
    **{
        f"{MOCK_TEMPLATE_PREFIX}{kind}": (
            template,
            f"没有 LLM 时,{kind} 动作的叙事模板（{{agent}}/{{location}}/{{target}}）",
        )
        for kind, template in _MOCK_TEMPLATE_DEFAULTS.items()
    },
}


# Templates whose consumers use the text RAW (never .format()ed) — brace
# characters in them are prose, not placeholders. Running placeholder
# validation against these rejected perfectly good worldview text containing
# a literal '{' (prompt-grounding code review #1).
_RAW_TEMPLATES = {"world.setting"}

# 世界观住的那个槽位名。作者层那一段(`.cyberworld` 的 `type: "world_setting"`)
# 落进世界之后就叫这个,她提示词里的**第一块**读的也是它
# (`chat_service.PROMPT_BLOCK_ORDER` 的头一项)。
# 3.8.0 起它有了热改的门(`World.set_world_setting` / `anima-world world setting`),
# 所以这个名字从字面量升成常量 —— 那扇门和这里必须指着同一个槽位。
WORLD_SETTING_PROMPT = "world.setting"


def _sample_for(name: str) -> dict[str, Any]:
    """The variables a template's call site passes, for render-checking.

    Mock narration templates are keyed by ACTION KIND, and a world may invent
    kinds this engine never heard of (a beat script's custom action). Those
    still render through `MockNarrativeProvider.describe`, which always passes
    the same three variables — so they check against the same sample rather
    than against an empty one, which would reject every template it saw (#9).
    """
    if name in _SAMPLE_VARS:
        return _SAMPLE_VARS[name]
    if name.startswith(MOCK_TEMPLATE_PREFIX):
        return {"agent": "遥", "location": "cafe", "target": "夏"}
    return {}


def check_renders(name: str, template: str) -> None:
    """Raise `PromptRenderError` if `template` references an unknown placeholder."""
    if name in _RAW_TEMPLATES:
        return
    sample = _sample_for(name)
    try:
        string.Formatter().vformat(template, (), sample)
    except (KeyError, IndexError, ValueError) as exc:
        raise PromptRenderError(
            f"template '{name}' references an unknown placeholder: {exc}"
        ) from exc


# **创世不播默认模板**(DB-SPLIT.md 移动 1)。播下去的 31 行里作者动过的是 **0** 行,
# 全是引擎快照 —— 于是改进过的措辞已有的世界一句都吃不到,而且无声。
# 表里剩下的就是作者改写过的那几条,别的现场从 `_DEFAULTS` 取。


def resolve(name: str, stored: str | None, default: str = "") -> str:
    """行里有 → 行里的;没有 → 引擎当前的默认模板;都没有 → 调用方的 `default`。

    所有存储实现共用这一份 —— 回落规则写两遍就会有两种回落。
    """
    if stored is not None:
        return stored
    entry = _DEFAULTS.get(name)
    return entry[0] if entry is not None else default


def merged_listing(stored: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """引擎声明的模板加上世界里多出来的,每一行带 `source`。

    ⚠️ **`source` 的第二档 2026-08-27 从「世界文件」改叫「这个世界」**(C 视角验收
    挑出来的)。旧那个词在**热改**这条路上是假的:`prompt_set` / `world setting
    --set` 写下的值也报「世界文件」,而它根本不来自任何文件 —— 更难看的是,
    `world setting` 同一屏上printed 的下一句正是「不会被世界文件里那段旧的盖回去」。
    **同一条命令的两句话互相矛盾,读的人只能挑一句信。**

    新词对**两种**来路都成立:创世时从作者层播下来的,和后来热改写下的 ——
    这一层本来就不记来路,所以说到「这个世界自己有一行」为止,是它知道的全部。
    ⚠️ `config_store.merged_listing` 那一份**照旧叫「世界文件」**,有意没动:
    它是 `config list` 的面,而那句话在**四仓**都有消费方(`test_config_provenance`
    / `test_flagship_seed` 钉着)。同一个词在两处不同,是**这一轮有意留下的账**,
    不是漏改 —— 提示词这边没有外部消费方,配置那边有。"""
    items: list[dict[str, Any]] = []
    for name, (template, description) in _DEFAULTS.items():
        row = stored.get(name)
        items.append({
            "name": name,
            "template": template if row is None else str(row.get("template", template)),
            # 说明描述的是**这个槽位**而不是里面的值:作者改写模板时不必也写一份说明,
            # 写了就用他的。
            "description": (row.get("description") if row else None) or description,
            "source": "默认值" if row is None else "这个世界",
        })
    for name in sorted(k for k in stored if k not in _DEFAULTS):
        row = stored[name]
        items.append({
            "name": name,
            "template": str(row.get("template", "")),
            "description": row.get("description"),
            "source": "这个世界",
        })
    return items
