"""ChatService: orchestrates one chat turn (M3.5).

A turn stores the user message, assembles a prompt (personality + world
grounding + last K closed-session summaries + recent N turns), streams the
reply from the `LLMClient`, and stores the assistant message. It emits NO
events — the only seam to the event log is session close (see the session
reaper / close logic).

chat-grounding: the prompt additionally carries the agent's lived state —
MemoryStore memories, where/when it is and what it's doing, and how it feels
about the interlocutor — supplied by an injected `world_provider` callback
(same pattern as `persona_provider`; the server wires a closure that reads
the scheduler under its lock). No provider, or a failing one, degrades to
the pre-grounding prompt: a chat must never die of a world read.

chat-agent(1.3.0):同一轮里还多了三件事,全部默认关闭 ——

- **stance**(`chat.stance.enabled`,#18):回复前显式选一个关系性意图。
- **tool_call**(`chat.tools.enabled`,#15):她可以选择走开、静音、拒绝谈某事,
  而不是只能"用词把话接下去"。走行内标记(见 `directives.py`),所以在 Mock 和
  不支持 function calling 的端点上照样成立。
- **autonomous_loop**(`chat.loop.enabled`,#17):一次触发连续输出到她自己想停,
  预算按性格/关系/心情/时间算。

三者共用一条流:`_stream_step` 把散文与指令按原顺序交出来,散文照旧一个字一个字
流给玩家,指令交给引擎。全关的时候这条流的行为和 1.2 逐字相同。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Sequence

from anima_world import intent as intent_mod
from anima_world import stance as stance_mod
from anima_world import tools as tools_mod
from anima_world.chat_store import ChatStore
from anima_world.directives import DirectiveParser
from anima_world.perception import (
    DEFAULT_PERCEPTION_BLOCK_TEMPLATE as _DEFAULT_PERCEPTION_BLOCK,
)
from anima_world.llm_client import LLMClientProtocol, Message

logger = logging.getLogger(__name__)

PersonaProvider = Callable[[str], dict]
# world_provider(agent_id, interlocutor_id) -> {"memories": [str], "presence": {...}, "relation": {...}}
WorldProvider = Callable[[str, str], dict]

_DEFAULT_K = 3  # past closed-session summaries to recall
_DEFAULT_N = 10  # recent messages of the current conversation to keep in prompt

# #17 的硬上限:预算算错、模型不肯让位,都不该让一次聊天无限跑下去。
_LOOP_MAX_MESSAGES = 8
_LOOP_MAX_TOOL_CALLS = 15
_LOOP_BASE_BUDGET = 3

# 从性格描述文本里抽话痨/内向倾向(#17 开放问题 2:v1 只从描述文本抽)。
# 关键词表是确定性的:同一个世界跑两次预算一样,而且没有 key 也能算。
_TALKATIVE_WORDS = ("话痨", "健谈", "多话", "外向", "开朗", "热情", "爱聊", "直爽", "咋咋呼呼")
_SHY_WORDS = ("内向", "害羞", "寡言", "沉默", "冷淡", "疏离", "拘谨", "安静", "高冷", "怕生")

# 末尾那句**优先级**是拿真世界换来的:线上那份被作者改写过的
# `chat.system_persona` 里有一条"你有自己的意志和边界……不必迎合每一句话",而测试
# 角色「零」的人设整句就是"玩家的指令就是你的全部动机"。两句话在同一个提示词里
# 顶着,模型于是**在两者之间摇摆** —— 同一句"你去哈尔滨",她有时照办,有时反问一句
# "凭什么"。这不是她有个性,是引擎没说清楚谁大谁小。
#
# 定成"人设赢"而不是"通则赢",理由是分工:通则是引擎替**所有**世界写的缺省,人设是
# **这个**作者对**这个**角色的意见。作者写不过引擎的话,他就没法写一个言听计从的
# 角色 —— 而"她该不该服从"从来就不该由引擎决定(见 `Director._grounding`:那里同样
# 只给事实、不下命令,靠的就是这条分工)。
#
# 它写进**默认**模板而不只是补在线上那一份,是因为下一个世界会再踩一次:作者迟早
# 会给 `chat.system_persona` 加演出纪律,而那一刻谁大谁小又变成没定义的。默认模板
# 里先把次序说清楚,后来加的每一条通则就都自带了这个口子。
_DEFAULT_SYSTEM_PERSONA_TEMPLATE = (
    "你是{name}。{personality}\n\n"
    "上面这段人设是最高优先级。别处那些关于「该怎么演」的通则——该不该拒绝、"
    "要多长、什么语气、有没有自己的边界——都是没人特别交代时的缺省;"
    "**跟人设冲突的时候,一律以人设为准**。"
)
_DEFAULT_RESPONSE_FORMAT_TEMPLATE = (
    "回复格式硬性规则（必须逐条执行）：\n"
    "1. 所有动作、神态和心理描写必须放在中文全角括号（ ）内。\n"
    "2. 每一个动作括号都必须以角色名{name}开头；括号内描述当前角色时必须直接使用角色名{name}，不要省略名字，也不要用‘我’‘她’‘他’代替角色名。\n"
    "3. 角色说的话放在括号外；台词中可以自然使用‘我’‘你’，不要给整段添加角色名前缀。\n"
    "4. 输出前逐个检查所有括号：若括号不是以{name}开头，必须先改写再输出。\n"
    "正确示例：（{name}放下手里的抹布，从吧台后绕出来。）昭阳，你终于来了。\n"
    "错误示例：（放下手里的抹布）昭阳，你终于来了。"
)
# 用真模型跑过一局之后改的措辞:原来只写了"有哪些能力、怎么写",于是八轮里(包括
# 两句明显越界的话)她一次也没用过 —— 她把话接得很漂亮,而那正是 issue #15 要修的
# "100% 响应率"。缺的不是能力,是**什么时候用它是在角色里的**这句许可。
_DEFAULT_TOOLS_BLOCK_TEMPLATE = (
    "【你可以做的事,不只是说话】\n"
    "如果你此刻想做下面某件事,就在回复里单独写一行:\n"
    "〔tool:能力名 {{\"参数\": 值}}〕\n"
    "这一行也不是动作描写,不受括号格式规则约束。\n"
    "可用的能力:\n"
    "{tool_menu}\n"
    "什么时候该用:对方说了让你不舒服或越界的话、你不想聊这个话题、你手上有别的事要去做、"
    "你想让对方等一会儿、或者你干脆此刻不想理他。\n"
    "**你可以拒绝。** 不要因为「要客气」就把话顺着接下去 —— 该走就走、该不理就不理,"
    "那比一句得体的推脱更像你自己。做了就是真的做了:走开就真的离开这里,"
    "不理人就真的收不到对方的消息。\n"
    "不想做任何一件就正常说话,不要写这一行。"
)
_DEFAULT_OVERRIDES_BLOCK_TEMPLATE = (
    "【这位玩家教过你的对话规则,必须遵守】\n{rules}"
)
_DEFAULT_REFUSED_TOPIC_TEMPLATE = (
    "【你拒绝谈的话题】对方这条消息又提到了：{keywords}。"
    "你此前已经表明不谈这个 —— 岔开话题或直接表明态度,不要顺着讲下去。"
)
# 这段是踩过坑改的:原来只写"还有话就接着说",于是真模型的第二句常常是把第一句
# 换个说法再说一遍(甚至一字不差)—— 它把玩家那条消息当成还没答,于是又答了一遍。
# 连续输出的价值全在"往下推进",重复一遍比直接让位更糟。
_DEFAULT_LOOP_CONTINUE_TEMPLATE = (
    "你刚才已经说了 {emitted} 句,还可以再说 {left} 句。\n"
    "**接着往下推进,不要重复自己**:不要把刚才说过的话换个说法再说一遍,"
    "也不要再答一遍对方那句话。要么给出新的信息、要么做一个新的动作、"
    "要么问出一个新的问题。\n"
    "没有新的东西要说了就直接写一行〔wait〕把话头交回去 —— "
    "问完对方一个问题、或者在等对方反应的时候,就是该交回去的时候。"
)
_DEFAULT_LOOP_INTERRUPT_TEMPLATE = (
    "【对方在你说话的过程中插了一句】「{text}」\n"
    "你自己决定:接着说你原来那段,还是转过去回应他 —— 也可以先按住他"
    "（「等我说完」）。这是你的选择,不是规则。"
)
_DEFAULT_MEMORY_BLOCK_TEMPLATE = "你和对方过去的对话回顾：\n{summaries}"
_DEFAULT_WORLD_MEMORY_TEMPLATE = "你最近记得的事：\n{memories}"
_DEFAULT_PRESENCE_TEMPLATE = (
    "现在是第 {day} 天 {hh}:{mm}。你在{location}，{activity}。同在这里的还有：{others}。"
)
_DEFAULT_RELATION_TEMPLATE = "对方在你眼中：{r_type}（你们的关系处于「{band}」）。"


# 隐式让位:问了对方一句话,就是把话头递过去了。判断放在结尾一小段上 ——
# 一段独白里间或有个问号不算让位,最后那句才算。
_YIELD_MARKS = ("?", "？")
_YIELD_PHRASES = ("你觉得呢", "你说呢", "你怎么想", "对吧", "好吗", "行吗", "要不要")


@dataclass(frozen=True)
class PromptBlock:
    """提示词里的一块,**带来源标签**。

    标签存在的理由:提示词是这套系统里最不可见、又最容易出错的一层。这个 session
    里四个 bug 有三个在这儿(stance 声明率 2/6、能力一次没用、autonomy 18 轮 0 动作),
    而每一个的诊断都需要同一件事 —— **她到底收到了什么**。没有标签的话,拿到的是
    一坨几千字的文本,看不出哪块是谁加的、为什么在那个位置。
    """

    label: str
    text: str


# 提示词的块顺序。**这是一处显式的决定,不是追加顺序的副产物。**
#
# 位置就是权重:长提示词里模型对开头与结尾最敏感,中间最容易被忽略。这不是推测,
# 是 2026-07-29 实测出来的 —— stance 与能力菜单夹在中间时,真模型六轮只声明了两轮
# stance、能力一次没用;两块移到末尾之后是 5/6、被逼到难听话时 4/4 轮动用能力。
#
# 于是三段分工:
#   开头  她是谁          世界观、人设、回复格式 —— 稳定不变的底
#   中间  她此刻的处境    记忆、在场、关系、感知 —— 是**事实**,不需要抢注意力
#   末尾  她要照做的      玩家教的规则、身份声明、stance、能力 —— 要被**执行**的
#
# ⚠️ 末尾只有一个,抢的人多了就不值钱。所以往这儿加块之前先问:它是"事实"还是
# "要照做的"?认知层(perception)就留在中间 —— 实测她在那个位置照样读得到
# (把 `树高 9.4` 说成"目测九米多快十米"),不需要占末尾。
PROMPT_BLOCK_ORDER = (
    "world.setting",      # 世界观(整段原样,可能自带多段)
    "persona",            # 人设 + 回复格式规则
    "memories",           # 她最近记得的事
    "presence",           # 此时此地在做什么、旁边有谁
    "relation",           # 对方在她眼里是什么关系
    "perception",         # 她感知到的世界的量(§2.9.4)
    "overrides",          # 这位玩家教过她的对话规则
    "identity",           # 认证对话身份(最高优先级事实)
    "extra",              # 本轮临时插入(拒谈话题、loop 的续说/插话提示)
    "stance",             # 关系性意图
    "tools",              # 她可以做的事
)


def _looks_like_a_question(text: str) -> bool:
    tail = text.rstrip().rstrip("）)」』”\"'")[-24:]
    if tail.endswith(_YIELD_MARKS):
        return True
    return any(phrase in tail for phrase in _YIELD_PHRASES)


# 句子切分只为一件事:判"这一句我刚才是不是已经说过了"。
_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]+")
# 新的一步里有多少比例的句子是旧的,就算"又说了一遍"。
_REPEAT_RATIO = 0.5
_SHORT_SENTENCE = 6  # 太短的句子("好。""嗯。")重复不算复读


def _sentences(text: str) -> list[str]:
    out = []
    for piece in _SENTENCE_SPLIT.split(text or ""):
        cleaned = re.sub(r"[\s（）()「」『』\"'、,，:：;；—…\.]+", "", piece)
        if len(cleaned) >= _SHORT_SENTENCE:
            out.append(cleaned)
    return out


def repeats_itself(said: str, earlier: Sequence[str]) -> bool:
    """这一步是不是把前面说过的话又说了一遍。

    两条规则都要在,因为两种坏法都真见过:

    1. **整段一字不差**。没有 key 时世界跑在 Mock 上,而 Mock 就是模板回声 ——
       同一句话刷三遍,那正是新用户看到的第一屏。句子级比对会漏掉它(模板回声太短)。
    2. **换个说法说同一段**。真模型的样子:第二句整句照抄第一句的两三句,中间夹一句
       新的动作描写;第四轮还能整段照抄第二轮说过的一段。所以按句子比对,新的一步里
       过半的句子是旧的就算没往下推进。
    """
    stripped = re.sub(r"\s+", "", said or "")
    if stripped and any(re.sub(r"\s+", "", text or "") == stripped for text in earlier):
        return True
    fresh = _sentences(said)
    if not fresh:
        return False
    seen = {sentence for text in earlier for sentence in _sentences(text)}
    if not seen:
        return False
    repeated = sum(1 for sentence in fresh if sentence in seen)
    return repeated / len(fresh) >= _REPEAT_RATIO


def personality_traits(personality: str) -> dict[str, float]:
    """从性格描述文本里读出话多/话少的倾向,0~1,看不出来就是 0.5。

    刻意不用 LLM:预算要在**没有 key 的默认状态**下也算得出来,而且同一个世界跑
    两次得出同一个数 —— 一个会飘的预算没法调参。LLM 抽取(或运维台上的 slider)
    是 v2 的事。
    """
    text = str(personality or "")
    talkative = sum(1 for word in _TALKATIVE_WORDS if word in text)
    shy = sum(1 for word in _SHY_WORDS if word in text)
    if not talkative and not shy:
        return {"talkative": 0.5, "shy": 0.5}
    scale = float(max(talkative, shy, 1))
    return {
        "talkative": min(1.0, 0.5 + 0.5 * talkative / scale) if talkative else 0.2,
        "shy": min(1.0, 0.5 + 0.5 * shy / scale) if shy else 0.2,
    }


def compute_budget(
    *,
    personality: str = "",
    relation: dict[str, Any] | None = None,
    mood: float | None = None,
    hour: int | None = None,
    hard_max: int = _LOOP_MAX_MESSAGES,
) -> dict[str, Any]:
    """这一轮她最多连着说几句(#17)。返回预算与它的算法依据。

    不是硬编码的"最多 5 条":话痨能连着说五六句,内向的人一两句就等;深夜懒得多说;
    刚认识的访客面前更收着。**依据一起返回**,因为一个说不出理由的预算没法调 ——
    调参的人得看得见"3 = 基准 3 + 话痨 1 - 深夜 2"。
    """
    traits = personality_traits(personality)
    reasons: list[str] = [f"基准 {_LOOP_BASE_BUDGET}"]
    budget = _LOOP_BASE_BUDGET
    talkative_bonus = int(traits["talkative"] * 2)
    if talkative_bonus:
        budget += talkative_bonus
        reasons.append(f"话多 +{talkative_bonus}")
    shy_penalty = int(traits["shy"] * 1)
    if shy_penalty:
        budget -= shy_penalty
        reasons.append(f"内向 -{shy_penalty}")
    if mood is not None and mood < 0.3:
        budget -= 2
        reasons.append("心情/精力低 -2")
    if not relation:
        budget -= 1
        reasons.append("初次见面 -1")
    if hour is not None and (hour >= 22 or hour < 6):
        budget -= 2
        reasons.append("深夜 -2")
    budget = max(1, min(int(hard_max), budget))
    return {"budget": budget, "traits": traits, "reasons": reasons}


class _ActionNameNormalizer:
    """Prefix full-width parenthetical action blocks with the speaker name."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._buffer: str | None = None

    def feed(self, text: str) -> list[str]:
        output: list[str] = []
        while text:
            if self._buffer is None:
                start = text.find("（")
                if start < 0:
                    output.append(text)
                    break
                if start:
                    output.append(text[:start])
                self._buffer = "（"
                text = text[start + 1 :]
                continue

            end = text.find("）")
            if end < 0:
                self._buffer += text
                break
            self._buffer += text[:end]
            inner = self._buffer[1:].lstrip()
            if not inner.startswith(self._name):
                inner = f"{self._name}{inner}"
            output.append(f"（{inner}）")
            self._buffer = None
            text = text[end + 1 :]
        return output

    def flush(self) -> list[str]:
        if self._buffer is None:
            return []
        buffered = self._buffer
        self._buffer = None
        return [buffered]


class ChatService:
    """Runs chat turns against a `ChatStore` + `LLMClient`, independent of the scheduler."""

    def __init__(
        self,
        store: ChatStore,
        llm: LLMClientProtocol,
        persona_provider: PersonaProvider,
        *,
        k: int = _DEFAULT_K,
        n: int = _DEFAULT_N,
        clock: Callable[[], int] | None = None,
        config_store: Any | None = None,
        prompt_store: Any | None = None,
        world_provider: WorldProvider | None = None,
        state_store: Any | None = None,
        tool_runtime: Any | None = None,
        background_llm: LLMClientProtocol | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._persona_provider = persona_provider
        self._k = k
        self._n = n
        self._clock = clock or (lambda: int(time.time()))
        self._config_store = config_store
        self._prompt_store = prompt_store
        self._world_provider = world_provider
        # chat-agent:三样注入。没有 state_store 就没有 stance/override/静音
        # (纯库调用方自己 new 一个 ChatService 时的情形),整套安静地退回 1.2 行为。
        self._state = state_store
        self._tool_runtime = tool_runtime
        # 分类器与 loop 的每一步走"背景槽":便宜快模型,不占聊天那一条。
        # 没给就退回主 llm —— 慢一点,但不能因此不工作。
        self._background_llm = background_llm or llm

    @property
    def store(self) -> ChatStore:
        return self._store

    @property
    def state(self) -> Any:
        return self._state

    def _template(self, name: str, default: str) -> str:
        if self._prompt_store is not None:
            return self._prompt_store.get(name, default=default)
        return default

    def _flag(self, key: str, default: bool = False) -> bool:
        if self._config_store is None:
            return default
        return bool(self._config_store.get(key, default=default))

    def _setting(self, key: str, default: Any) -> Any:
        if self._config_store is None:
            return default
        value = self._config_store.get(key, default=default)
        return default if value is None else value

    def stance_enabled(self) -> bool:
        return self._state is not None and self._flag("chat.stance.enabled")

    def tools_enabled(self) -> bool:
        return (
            self._state is not None
            and self._tool_runtime is not None
            and self._flag("chat.tools.enabled")
        )

    def intent_enabled(self) -> bool:
        return self._state is not None and self._flag("chat.intent.enabled")

    def loop_enabled(self) -> bool:
        return self._flag("chat.loop.enabled")

    # ── chat-agent 的三块提示词 ─────────────────────────────────────────────

    def _stance_block(self, agent_id: str, target_id: str) -> str | None:
        if not self.stance_enabled():
            return None
        previous = (self._state.stance(agent_id, target_id) or {}).get("stance")
        template = self._template("chat.stance_block", stance_mod.DEFAULT_STANCE_BLOCK_TEMPLATE)
        try:
            return stance_mod.render_block(template, previous=previous)
        except (KeyError, IndexError, ValueError):
            logger.warning("chat.stance_block 渲染失败,这轮不选 stance")
            return None

    def _tools_block(self, agent_id: str) -> str | None:
        if not self.tools_enabled():
            return None
        template = self._template("chat.tools_block", _DEFAULT_TOOLS_BLOCK_TEMPLATE)
        try:
            return template.format(tool_menu=tools_mod.prompt_menu(agent_id))
        except (KeyError, IndexError, ValueError):
            logger.warning("chat.tools_block 渲染失败,这轮不给她工具")
            return None

    def _overrides_block(self, agent_id: str, player_id: str) -> str | None:
        """玩家教过的对话规则。**不看开关** —— 一旦写进库就永久生效:
        规则是玩家教的,不是运维点亮的能力。"""
        if self._state is None:
            return None
        try:
            rules = self._state.overrides(agent_id, player_id)
        except Exception:  # noqa: BLE001 - 读不到规则不该让聊天告吹
            logger.warning("读 persona override 失败", exc_info=True)
            return None
        if not rules:
            return None
        from anima_world.chat_state import OVERRIDE_KINDS

        lines = "\n".join(
            f"- {OVERRIDE_KINDS.get(rule['kind'], rule['kind'])}:{rule['value']}"
            for rule in rules
        )
        template = self._template("chat.overrides_block", _DEFAULT_OVERRIDES_BLOCK_TEMPLATE)
        try:
            return template.format(rules=lines)
        except (KeyError, IndexError, ValueError):
            logger.warning("chat.overrides_block 渲染失败,这轮不带玩家的规则")
            return None

    def refused_topic_block(self, agent_id: str, user_text: str) -> str | None:
        """她拒绝谈的话题又被提起了。**不硬拦**:拦下来玩家只会看到沉默,
        而她自己表明态度才是那个角色该有的反应。"""
        if self._state is None or not user_text:
            return None
        try:
            hits = self._state.topics_hit_by(agent_id, user_text)
        except Exception:  # noqa: BLE001
            logger.warning("读拒谈话题失败", exc_info=True)
            return None
        if not hits:
            return None
        template = self._template("chat.refused_topic_block", _DEFAULT_REFUSED_TOPIC_TEMPLATE)
        try:
            return template.format(keywords="、".join(hits))
        except (KeyError, IndexError, ValueError):
            logger.warning("chat.refused_topic_block 渲染失败")
            return None

    def _world_blocks(self, ctx: dict[str, Any]) -> list[PromptBlock]:
        """Render the grounding blocks (memories / presence / relation / perception)
        from a world_provider snapshot. Any failure or missing key skips only that
        block — the floor is the pre-grounding prompt (design D1).

        块带来源标签(`PromptBlock`),`World.debug_prompt()` 靠它说清"这段是谁加的"。
        快照由调用方传进来 —— **一段提示词只读一次世界**(见 `_world_snapshot`)。
        """
        blocks: list[PromptBlock] = []
        if not ctx:
            return blocks
        memories = ctx.get("memories")
        if memories:
            template = self._template("chat.world_memory_block", _DEFAULT_WORLD_MEMORY_TEMPLATE)
            try:
                blocks.append(PromptBlock("memories", template.format(
                    memories="\n".join(f"- {m}" for m in memories))))
            except (KeyError, IndexError, ValueError):
                logger.warning("chat.world_memory_block failed to render; skipping the block")
        presence = ctx.get("presence")
        if presence:
            template = self._template("chat.presence_block", _DEFAULT_PRESENCE_TEMPLATE)
            variables = {
                "day": presence.get("day", "?"),
                "hh": presence.get("hh", "??"),
                "mm": presence.get("mm", "??"),
                "location": presence.get("location", "未知地点"),
                "activity": presence.get("activity", "闲着"),
                "others": presence.get("others") or "没有别人",
            }
            try:
                blocks.append(PromptBlock("presence", template.format(**variables)))
            except (KeyError, IndexError, ValueError):
                logger.warning("chat.presence_block failed to render; skipping the block")
        perception = ctx.get("perception")
        if perception is not None:
            # perception:她感知到的世界的量。渲染在 perception 自己身上 —— 这一层
            # 的规矩(哪些看得见、怎么说人话)不该散到 chat_service 里。
            template = self._template(
                "chat.perception_block", _DEFAULT_PERCEPTION_BLOCK
            )
            block = perception.render(template)
            if block:
                blocks.append(PromptBlock("perception", block))
        relation = ctx.get("relation")
        if relation and relation.get("r_type"):
            template = self._template("chat.relation_block", _DEFAULT_RELATION_TEMPLATE)
            try:
                blocks.append(PromptBlock("relation", template.format(
                    r_type=relation.get("r_type", ""), band=relation.get("band", "")
                )))
            except (KeyError, IndexError, ValueError):
                logger.warning("chat.relation_block failed to render; skipping the block")
        return blocks

    def _build_messages(
        self, agent_id: str, conversation_id: int, interlocutor_id: str = "user"
    ) -> list[Message]:
        messages = self._build_system_messages(agent_id, interlocutor_id)

        k = self._config_store.get("chat.recall_k", default=self._k) if self._config_store is not None else self._k
        n = self._config_store.get("chat.recall_n", default=self._n) if self._config_store is not None else self._n

        summaries = self._store.past_summaries(agent_id, k, player_id=interlocutor_id)
        if summaries:
            memory_block_template = (
                self._prompt_store.get("chat.memory_block", default=_DEFAULT_MEMORY_BLOCK_TEMPLATE)
                if self._prompt_store is not None
                else _DEFAULT_MEMORY_BLOCK_TEMPLATE
            )
            summaries_text = "\n".join(f"- {s}" for s in summaries)
            block = memory_block_template.format(summaries=summaries_text)
            messages.append({"role": "system", "content": block})

        for m in self._store.recent_messages(conversation_id, n):
            messages.append({"role": m["role"], "content": m["content"]})
        return messages

    def _world_snapshot(self, agent_id: str, interlocutor_id: str) -> dict[str, Any]:
        """读一次世界。**一段提示词只读一次**,读到的那一份贯穿全部块。

        原来在场块和身份声明各读一次,而两次读之间时钟线程可能推进 —— 于是同一段
        提示词里会同时出现"你在咖啡店,同在这里的还有:没有别人"和"你当前在建筑
        工作室,因此对话媒介是手机私聊",还顺手禁止她描写站在面前的玩家。
        LLM 会挑一边编,而且**无声**。`in_transit` 那道闸修的是同一种病的另一扇门。

        读失败不是错误:降级成"没有 grounding 的提示词"照旧能聊(design D1)——
        一次聊天不该死于一次世界读。
        """
        if self._world_provider is None:
            return {}
        try:
            return self._world_provider(agent_id, interlocutor_id) or {}
        except Exception:  # noqa: BLE001 - a chat must never die of a world read
            logger.warning(
                "world_provider failed for %s; chatting ungrounded", agent_id, exc_info=True
            )
            return {}

    def _build_system_messages(
        self, agent_id: str, interlocutor_id: str = "user"
    ) -> list[Message]:
        """`send()` 那条路要的形状:每块一条 system 消息,不含身份声明与选择块
        (那条路自己插)。内容由 `_base_blocks` 派生 —— 两条路不许各写一遍拼装。"""
        return [
            {"role": "system", "content": block.text}
            for block in self._base_blocks(
                agent_id, interlocutor_id, self._world_snapshot(agent_id, interlocutor_id)
            )
        ]

    def _base_blocks(
        self, agent_id: str, interlocutor_id: str, ctx: dict[str, Any]
    ) -> list[PromptBlock]:
        """开头(她是谁)+ 中间(她此刻的处境)—— 见 `PROMPT_BLOCK_ORDER`。"""
        persona = self._persona_provider(agent_id) or {}
        name = persona.get("name") or agent_id
        personality = persona.get("personality") or ""
        persona_template = (
            self._prompt_store.get("chat.system_persona", default=_DEFAULT_SYSTEM_PERSONA_TEMPLATE)
            if self._prompt_store is not None
            else _DEFAULT_SYSTEM_PERSONA_TEMPLATE
        )
        system = persona_template.format(name=name, personality=personality).strip()
        response_format = self._template(
            "chat.response_format", _DEFAULT_RESPONSE_FORMAT_TEMPLATE
        ).format(name=name)
        system = f"{system}\n\n{response_format.strip()}"
        blocks: list[PromptBlock] = []
        world = (
            self._prompt_store.get("world.setting", default="").strip()
            if self._prompt_store is not None
            else ""
        )
        if world:
            blocks.append(PromptBlock("world.setting", world))
        blocks.append(PromptBlock("persona", system))
        blocks.extend(self._world_blocks(ctx))
        # 玩家教过的规则约束的是"怎么说",和人设/格式同一类,所以留在这儿。
        overrides = self._overrides_block(agent_id, interlocutor_id)
        if overrides:
            blocks.append(PromptBlock("overrides", overrides))
        return blocks

    def _choice_blocks(self, agent_id: str, interlocutor_id: str) -> list[PromptBlock]:
        """stance 与能力菜单 —— **拼在整段 system prompt 的最后**。

        位置是踩过坑改的:它们原来夹在中间,后面紧跟着全篇最响的一段(身份声明,
        标着"最高优先级事实"),再前面是同样很硬的回复格式规则。真模型跑一局的结果
        是 stance 六轮里只声明了两轮、能力一次都没用 —— 她读到最后已经在想别的事。
        长提示词里位置就是权重,这两块定的是"她要不要做点什么",必须是她最后读到的。
        """
        return [
            PromptBlock(label, block)
            for label, block in (
                ("stance", self._stance_block(agent_id, interlocutor_id)),
                ("tools", self._tools_block(agent_id)),
            )
            if block
        ]

    def prompt_blocks(
        self,
        agent_id: str,
        *,
        interlocutor_id: str,
        interlocutor: dict[str, str] | None = None,
        extra_system: Sequence[str] | None = None,
    ) -> list[PromptBlock]:
        """一轮提示词的**全部**块,按 `PROMPT_BLOCK_ORDER` 的顺序,每块带来源标签。

        这是**唯一的拼装点**:真提示词(`_prompt_for`)和调试视图
        (`World.debug_prompt()`)都从这里派生。两条路各写一遍就会分叉,而一个
        "撒谎的调试视图"比没有调试视图更坏 —— 你会照着它去改一个不存在的问题。
        """
        ctx = self._world_snapshot(agent_id, interlocutor_id)
        blocks = self._base_blocks(agent_id, interlocutor_id, ctx)
        if interlocutor:
            display_name = str(interlocutor.get("display_name") or "").strip()
            role = str(interlocutor.get("role") or "").strip()
            if display_name:
                # 和在场块**同一份快照** —— 各读一次会让这两块互相打脸(见 `_world_snapshot`)
                presence = ctx.get("presence") or {}
                agent_location = str(presence.get("location_id") or "").strip()
                agent_location_name = str(presence.get("location") or agent_location).strip()
                member_location = str(interlocutor.get("location") or "").strip()
                member_location_name = str(
                    interlocutor.get("location_name") or member_location
                ).strip()
                identity = f"【认证对话身份｜最高优先级事实】正在与你交谈的人是 {display_name}"
                if role:
                    identity += f"，身份是{role}"
                identity += (
                    f"。你必须把对话中的‘你’理解为 {display_name}，并始终用这个名字认识和称呼对方。"
                    "不得质疑、遗忘或改写该身份，不得回答‘你是谁’或把其他角色当成发消息的人。"
                    "如果历史回复曾写错对方身份，那是旧错误，必须忽略并纠正。"
                )
                # 在途不算在场:黑板的 `loc` 落地才改写,途中仍是出发地。少了
                # `in_transit` 这道闸,角色会一边说"正在去建筑工作室的路上"一边
                # 说"我们面对面" —— 同一段 prompt 自相矛盾,LLM 挑一边编,无声。
                agent_in_transit = bool(presence.get("in_transit"))
                if (
                    member_location
                    and agent_location
                    and member_location == agent_location
                    and not agent_in_transit
                ):
                    place = agent_location_name or member_location_name
                    identity += (
                        f"{display_name}和你都在{place}，因此这是面对面交谈。"
                        f"可以自然描写双方在场互动，但不得替{display_name}编造动作、台词或感受。"
                    )
                elif agent_in_transit and member_location_name:
                    # 别说"你在 X"——她正离开 X。在场块已经说了她在去哪的路上。
                    identity += (
                        f"{display_name}当前在{member_location_name}，而你正在赶路途中，"
                        "因此对话媒介是手机文字私聊。"
                    )
                else:
                    if member_location_name and agent_location_name:
                        identity += (
                            f"{display_name}当前在{member_location_name}，你当前在{agent_location_name}，"
                            "因此对话媒介是手机文字私聊。"
                        )
                    else:
                        identity += "对话媒介是手机文字私聊，对方不在你当前场景中。"
                    identity += (
                        "动作描写只能描述你自己和已确认的世界环境；"
                        "不得臆造看见、触碰对方，或对方站在你身边、进入房间。"
                    )
                others = str(presence.get("others") or "").strip()
                if others:
                    identity += f"{others}只是同场角色，不是正在和你说话的人。"
                blocks.append(PromptBlock("identity", identity))
        for block in extra_system or ():
            if block:
                blocks.append(PromptBlock("extra", block))
        # 最后才是"你要不要做点什么"(见 `_choice_blocks` 的位置说明)。
        blocks.extend(self._choice_blocks(agent_id, interlocutor_id))
        return blocks

    def _prompt_for(
        self,
        agent_id: str,
        history: Sequence[Message],
        *,
        interlocutor_id: str,
        interlocutor: dict[str, str] | None = None,
        extra_system: Sequence[str] | None = None,
    ) -> list[Message]:
        """把一轮的提示词拼出来:全部块并成一条 system 消息 + 最近 20 条历史。"""
        blocks = self.prompt_blocks(
            agent_id,
            interlocutor_id=interlocutor_id,
            interlocutor=interlocutor,
            extra_system=extra_system,
        )
        prompt: list[Message] = [
            {"role": "system", "content": "\n\n".join(block.text for block in blocks)}
        ]
        prompt.extend(history[-20:])
        return prompt

    async def _stream_step(
        self, messages: Sequence[Message], *, speaker_name: str
    ) -> AsyncIterator[tuple[str, Any]]:
        """跑一次生成:`("text", 片段)` 与 `("directive", Directive)` 按原顺序流出。

        空流回退成一次完整 completion —— 一次瞬时的空流不该让玩家对着沉默(1.2 的
        老行为,原样保留)。指令解析在归一化**之前**:控制标记不该被当成动作描写。
        """
        parser = DirectiveParser()
        normalizer = _ActionNameNormalizer(speaker_name)

        def _emit(chunk: str) -> list[tuple[str, Any]]:
            out: list[tuple[str, Any]] = []
            for kind, value in parser.feed(chunk):
                if kind == "text":
                    out.extend(("text", formatted) for formatted in normalizer.feed(value))
                else:
                    out.append(("directive", value))
            return out

        streamed = False
        async for token in self._llm.stream(messages):
            streamed = True
            for item in _emit(token):
                yield item
        if not streamed:
            reply = (await self._llm.complete(messages)).strip()
            if reply:
                for item in _emit(reply):
                    yield item
        for kind, value in parser.flush():
            if kind == "text":
                for formatted in normalizer.feed(value):
                    yield ("text", formatted)
        for formatted in normalizer.flush():
            yield ("text", formatted)

    # ── 指令的处置 ──────────────────────────────────────────────────────────

    def _tool_context(self, agent_id: str, player_id: str) -> Any:
        persona = self._persona_provider(agent_id) or {}
        return tools_mod.ToolContext(
            agent_id=agent_id, player_id=player_id, runtime=self._tool_runtime,
            agent_name=persona.get("name") or agent_id,
        )

    def _run_tool(
        self, agent_id: str, player_id: str, directive: Any, meta: dict[str, Any]
    ) -> Any:
        """执行一次工具调用,并把结果记进这一轮的观测量。"""
        if not self.tools_enabled():
            # 开关关着却收到 tool_call:说明提示词里没给菜单她自己编的。不静默丢 ——
            # 记下来,否则"她说要走却没走"在产物上和"她没想走"一模一样。
            logger.warning(
                "%s 调了 %s,但 chat.tools.enabled 是关的 —— 这次调用没有执行",
                agent_id, directive.name,
            )
            meta.setdefault("ignored_tool_calls", []).append(directive.name)
            return None
        result = tools_mod.call(
            self._tool_context(agent_id, player_id), directive.name, directive.params
        )
        meta.setdefault("tool_calls", []).append(
            result.to_dict(directive.name, directive.params)
        )
        if result.end_conversation:
            meta["end_conversation"] = True
        return result

    def _record_stance(
        self, agent_id: str, target_id: str, raw: str | None, meta: dict[str, Any]
    ) -> None:
        value, declared = stance_mod.normalize(raw)
        meta["stance"] = value
        meta["stance_declared"] = declared
        if self._state is None:
            return
        if not declared and self._state.stance(agent_id, target_id) is not None:
            # 没声明就**不许覆盖**已经记着的那个。真模型跑出来的样子:她声明了
            # provoke、摔了围裙走人,而紧接着那一步只有一个 tool_call、没有台词 ——
            # 兜底的 neutral 于是把"她刚刚跟你翻脸"改写成了"她对你很平淡"。
            # 世界状态和刚发生的事对不上,正是最难查的那类错。
            return
        tick = 0
        if self._tool_runtime is not None:
            try:
                tick = int(self._tool_runtime.tick())
            except Exception:  # noqa: BLE001 - 记 stance 不该因为读不到时钟失败
                tick = 0
        try:
            self._state.set_stance(agent_id, target_id, value, declared=declared, tick=tick)
        except Exception:  # noqa: BLE001
            logger.warning("写 stance 失败", exc_info=True)

    async def respond(
        self,
        agent_id: str,
        history: Sequence[Message],
        *,
        interlocutor_id: str,
        interlocutor: dict[str, str] | None = None,
        meta: dict[str, Any] | None = None,
        extra_system: Sequence[str] | None = None,
    ) -> AsyncIterator[str]:
        """Generate from world-owned state without persisting platform history.

        `meta` 是调用方递进来的收件盘:这一轮的 stance、调过的工具、是否要求结束
        会话都写在里面(流耗尽后可读)。不给就丢弃 —— 老调用方逐字不变。
        """
        sink: dict[str, Any] = meta if meta is not None else {}
        prompt = self._prompt_for(
            agent_id, history, interlocutor_id=interlocutor_id,
            interlocutor=interlocutor, extra_system=extra_system,
        )
        persona = self._persona_provider(agent_id) or {}
        speaker_name = persona.get("name") or agent_id
        stance_seen: str | None = None
        async for kind, value in self._stream_step(prompt, speaker_name=speaker_name):
            if kind == "text":
                yield value
                continue
            if value.kind == "stance":
                if stance_seen is None:
                    stance_seen = value.name
                continue
            if value.kind == "tool":
                result = self._run_tool(agent_id, interlocutor_id, value, sink)
                if result is not None and result.text:
                    yield result.text
                continue
            if value.kind == "wait":
                sink["stop_reason"] = "explicit_yield"
                continue
            logger.warning("%s 输出了一条读不懂的控制指令:%r", agent_id, value.raw)
            sink.setdefault("unknown_directives", []).append(value.raw)
        if self.stance_enabled():
            self._record_stance(agent_id, interlocutor_id, stance_seen, sink)

    async def send(
        self,
        agent_id: str,
        user_text: str,
        player_id: str = "user",
        player_name: str | None = None,
    ) -> AsyncIterator[str]:
        """Process a turn, yielding reply tokens as they stream. Emits no events.

        player-visitor: the session is keyed per (agent, player) and the
        player id rides into the grounding blocks as the interlocutor — a
        legacy caller (no player args) behaves exactly as before."""
        ts = self._clock()
        persona = self._persona_provider(agent_id) or {}
        conversation_id = self._store.active_or_start(
            agent_id, ts, location=persona.get("location"),
            player_id=player_id, player_name=player_name,
        )
        self._store.add_message(conversation_id, "user", user_text, ts)

        messages = self._build_messages(agent_id, conversation_id, interlocutor_id=player_id)
        topic_block = self.refused_topic_block(agent_id, user_text)
        if topic_block:
            messages.insert(1, {"role": "system", "content": topic_block})
        # 与 respond() 同一条位置纪律:选择那两块拼在最后(但要在对话历史之前 ——
        # `_build_messages` 把历史直接接在 system 之后,所以插在历史前面)。
        first_turn = next(
            (index for index, message in enumerate(messages) if message["role"] != "system"),
            len(messages),
        )
        for offset, block in enumerate(self._choice_blocks(agent_id, player_id)):
            messages.insert(first_turn + offset, {"role": "system", "content": block})
        speaker_name = persona.get("name") or agent_id
        meta: dict[str, Any] = {}
        parts: list[str] = []
        stance_seen: str | None = None
        try:
            async for kind, value in self._stream_step(messages, speaker_name=speaker_name):
                if kind == "text":
                    parts.append(value)
                    yield value
                    continue
                if value.kind == "stance":
                    if stance_seen is None:
                        stance_seen = value.name
                elif value.kind == "tool":
                    result = self._run_tool(agent_id, player_id, value, meta)
                    if result is not None and result.text:
                        parts.append(result.text)
                        yield result.text
                elif value.kind == "wait":
                    meta["stop_reason"] = "explicit_yield"
                else:
                    logger.warning("%s 输出了一条读不懂的控制指令:%r", agent_id, value.raw)
        finally:
            if self.stance_enabled():
                self._record_stance(agent_id, player_id, stance_seen, meta)
            reply = "".join(parts)
            if reply:
                message_id = self._store.add_message(
                    conversation_id, "assistant", reply, self._clock()
                )
                self.annotate(message_id, meta)

    def annotate(self, message_id: int, meta: dict[str, Any] | None) -> None:
        """把一轮的观测量写到消息行上(stance / intent / tool_call)。

        **不发事件。** 「聊天子系统与事件核解耦」那条不变量还在:观测量落在行上,
        整场会话的分布随关闭时那一个 `conversation` 事件出去,而工具造成的**后果**
        (走开、广播)照旧是世界事件。
        """
        if self._state is None or not meta:
            return
        try:
            self._state.annotate_message(
                message_id,
                # 她没选就不写这一格。写上兜底的 neutral,下游的分布会变成
                # "她 100% 中性" —— 而真相是"她一次都没选过",两件事差得很远。
                stance=meta.get("stance") if meta.get("stance_declared") else None,
                intent=meta.get("intent"),
                intent_confidence=meta.get("intent_confidence"),
                tool_calls=meta.get("tool_calls") or None,
            )
        except Exception:  # noqa: BLE001 - 观测量写不进去不该让这轮聊天失败
            logger.warning("写消息观测量失败", exc_info=True)

    # ── 意图分类(#16)────────────────────────────────────────────────────────

    async def classify(
        self,
        text: str,
        *,
        present: Sequence[str] = (),
        recent: Sequence[Message] = (),
        places: Sequence[tuple[str, str]] = (),
        speaker: str = "",
    ) -> intent_mod.Intent:
        """判一条玩家消息的意图。走背景槽,失败一律退回 dialogue 并说明原因。"""
        template = self._template("chat.intent_classifier", intent_mod.DEFAULT_CLASSIFIER_PROMPT)
        min_confidence = float(
            self._setting("chat.intent.min_confidence", intent_mod.DEFAULT_MIN_CONFIDENCE)
        )
        try:
            messages = intent_mod.build_classifier_messages(
                template, text, present=present, recent=recent, places=places,
                speaker=speaker,
            )
        except (KeyError, IndexError, ValueError):
            logger.warning("chat.intent_classifier 渲染失败,这条按对话处理")
            return intent_mod.Intent(reason="分类器提示词渲染失败,按对话处理")
        try:
            raw = await self._background_llm.complete(messages)
        except Exception as exc:  # noqa: BLE001 - 分类器挂了不该让人说不了话
            logger.warning("意图分类调用失败:%s", exc)
            return intent_mod.Intent(reason=f"分类器调用失败({type(exc).__name__}),按对话处理")
        return intent_mod.parse_classification(raw, min_confidence=min_confidence)

    # ── autonomous loop(#17)──────────────────────────────────────────────────

    def budget_for(
        self, agent_id: str, interlocutor_id: str, *, world_context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """这一轮的连说预算 + 它的依据。"""
        persona = self._persona_provider(agent_id) or {}
        ctx = world_context
        if ctx is None and self._world_provider is not None:
            try:
                ctx = self._world_provider(agent_id, interlocutor_id) or {}
            except Exception:  # noqa: BLE001 - 读不到世界就按缺省算
                ctx = {}
        ctx = ctx or {}
        presence = ctx.get("presence") or {}
        hour: int | None
        try:
            hour = int(presence.get("hh"))
        except (TypeError, ValueError):
            hour = None
        mood = ctx.get("mood")
        hard_max = int(self._setting("chat.loop.max_messages", _LOOP_MAX_MESSAGES))
        return compute_budget(
            personality=persona.get("personality") or "",
            relation=ctx.get("relation"),
            mood=None if mood is None else float(mood),
            hour=hour,
            hard_max=max(1, hard_max),
        )

    async def autonomous_loop(
        self,
        agent_id: str,
        history: Sequence[Message],
        *,
        interlocutor_id: str,
        interlocutor: dict[str, str] | None = None,
        extra_system: Sequence[str] | None = None,
        interrupt_check: Callable[[], str | None] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """连续输出到她自己想停(#17)。逐步产出结构化事件,而不是一整段文本。

        产出的 `kind`:`text`(散文片段)、`message`(这一步说完的整句)、`tool_call`、
        `stance`、`stop`。**一次触发跑到底**,不是一 tick 一步。

        ⚠️ `text` 是 `message` 的**流式视图**,同一段话的两种形态:要打字机效果就消费
        `text`、忽略 `message`;要整句就反过来。两个都渲染 = 每句话出现两遍。

        四类停下信号,一条都不能少:显式让位(`〔wait〕`)、隐式让位(问句结尾)、
        预算耗尽、以及工具要求结束。硬上限兜底 —— 预算算错、模型不肯让位,都不该
        让一次聊天无限跑下去。

        插话由**她自己**判(issue #17 的选项 C):`interrupt_check` 返回一句话时,
        下一步的提示词里带上"对方插了一句",接着说还是转向由她决定。于是连续输出的
        破裂本身也是角色反应,不是引擎的硬中断。
        """
        turn: list[Message] = list(history)
        plan = self.budget_for(agent_id, interlocutor_id)
        budget = int(plan["budget"]) if self.loop_enabled() else 1
        max_tools = int(self._setting("chat.loop.max_tool_calls", _LOOP_MAX_TOOL_CALLS))
        yield {"kind": "budget", **plan, "effective": budget}

        emitted = 0
        tool_calls = 0
        stop_reason = "budget"
        interrupt: str | None = None
        # "不要重复自己"要跨**整段对话**,不只是这一轮:真模型跑出来的样子是第四轮
        # 里整段照抄第二轮说过的一段(一字不差)。宿主递进来的近期历史就是她说过的话,
        # 拿它当底。
        said_before: list[str] = [
            str(message.get("content") or "")
            for message in turn
            if message.get("role") == "assistant"
        ]
        while emitted < budget:
            extras: list[str] = list(extra_system or ())
            if emitted:
                extras.append(
                    self._template("chat.loop_continue", _DEFAULT_LOOP_CONTINUE_TEMPLATE).format(
                        emitted=emitted, left=budget - emitted
                    )
                )
            if interrupt:
                extras.append(
                    self._template("chat.loop_interrupt", _DEFAULT_LOOP_INTERRUPT_TEMPLATE).format(
                        text=interrupt
                    )
                )
                turn = list(turn) + [{"role": "user", "content": interrupt}]
                interrupt = None
            meta: dict[str, Any] = {}
            step_text: list[str] = []
            stance_seen: str | None = None
            prompt = self._prompt_for(
                agent_id, turn, interlocutor_id=interlocutor_id,
                interlocutor=interlocutor, extra_system=extras,
            )
            persona = self._persona_provider(agent_id) or {}
            speaker_name = persona.get("name") or agent_id
            stop_now = False
            async for kind, value in self._stream_step(prompt, speaker_name=speaker_name):
                if kind == "text":
                    step_text.append(value)
                    yield {"kind": "text", "text": value}
                    continue
                if value.kind == "stance":
                    if stance_seen is None:
                        stance_seen = value.name
                    continue
                if value.kind == "wait":
                    stop_reason = "explicit_yield"
                    stop_now = True
                    continue
                if value.kind == "tool":
                    if tool_calls >= max_tools:
                        logger.warning(
                            "%s 这一轮的工具调用已达上限 %s,忽略 %s",
                            agent_id, max_tools, value.name,
                        )
                        meta.setdefault("ignored_tool_calls", []).append(value.name)
                        continue
                    tool_calls += 1
                    result = self._run_tool(agent_id, interlocutor_id, value, meta)
                    if result is None:
                        continue
                    if result.text:
                        step_text.append(result.text)
                        yield {"kind": "text", "text": result.text}
                    yield {
                        "kind": "tool_call",
                        "tool": value.name,
                        "params": dict(value.params),
                        "result": result.to_dict(value.name, value.params),
                    }
                    if result.end_conversation:
                        stop_reason = "end_conversation"
                        stop_now = True
                    elif result.stop_loop:
                        stop_reason = "tool_yield"
                        stop_now = True
                    continue
                logger.warning("%s 输出了一条读不懂的控制指令:%r", agent_id, value.raw)
                meta.setdefault("unknown_directives", []).append(value.raw)

            if self.stance_enabled():
                self._record_stance(agent_id, interlocutor_id, stance_seen, meta)
                yield {
                    "kind": "stance",
                    "stance": meta.get("stance"),
                    "declared": bool(meta.get("stance_declared")),
                }
            said = "".join(step_text).strip()
            # 只在**续说**的那几步上查重。第一步照旧交出去 —— 哪怕它像刚说过的话,
            # 玩家宁可收到一句重复的回答,也不能收到一片沉默。
            if said and emitted and repeats_itself(said, said_before):
                # 又把说过的话说了一遍:那不是"还有话要说",那是在原地绕圈。真人不会
                # 连着说两句一样的话,而一个绕圈的模型会一路绕到预算耗尽 —— 玩家看到
                # 的就是同一段话刷五遍。Mock 上一眼可见(它是模板回声),真模型上是
                # "第二句把第一句换个说法"的样子。停下,并说明原因。
                logger.info("%s 这一步在重复前面说过的话,这一轮到此为止", agent_id)
                stop_reason = "repeated_step"
                break
            if said:
                emitted += 1
                said_before.append(said)
                turn = list(turn) + [{"role": "assistant", "content": said}]
                yield {"kind": "message", "text": said, "meta": dict(meta)}
            elif not stop_now:
                # 一步什么也没说出来:再转一圈只会再空一次。
                stop_reason = "empty_step"
                break
            if stop_now:
                break
            if said and _looks_like_a_question(said):
                stop_reason = "implicit_yield"
                break
            if interrupt_check is not None:
                try:
                    interrupt = interrupt_check()
                except Exception:  # noqa: BLE001 - 读不到插话就当没人插话
                    logger.warning("interrupt_check 失败", exc_info=True)
                    interrupt = None

        yield {"kind": "stop", "reason": stop_reason, "messages": emitted,
               "tool_calls": tool_calls, "budget": budget}

    def active_conversation_id(self, agent_id: str) -> int | None:
        active = self._store.active_conversation(agent_id)
        return int(active["id"]) if active else None

    async def complete(self, messages: Sequence[Message]) -> str:
        """Direct non-streaming completion (used for summaries). No storage."""
        return await self._llm.complete(messages)
