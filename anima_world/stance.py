"""stance:一轮聊天输出前显式选定的**关系性意图**(issue #18)。

在这之前,角色说的每句话都没有关系性意图:不管她此刻想讨好你、故意惹你,还是在
试探你的反应,语气都差不多 —— 那一层被压平成了"回话"。真人聊天里这一层永远在场,
只是不明说。

三条设计决定,都写在这里,免得后面有人以为是漏的:

1. **枚举有限、可穷举。** 开放字符串一开就散:LLM 每次编一个新词,下游没法消费,
   统计也做不了。v1 八个,扩展要显式加(见 `STANCES`)。
2. **stance 是 (agent, target) 的属性**,不是 agent 全局属性。她可以同时对你找茬、
   对别人讨好 —— 存储也照这个形状(`agent_stance` 表的主键)。
3. **惯性是提示词压力,不是引擎摇骰子。** issue 里写的是"上一轮不为 neutral 时
   本轮 60% 保留"。用随机数在引擎侧强行保留,等于让引擎**覆盖角色自己的选择**,
   而且同一句话跑两次给两个结果、日志上看不出为什么。这里的做法是把上一轮的
   stance 连同"真人不会一句讨好下一句找茬"一起写进提示词,让惯性成为她自己的
   倾向。可观测、可复现,而且解释权仍在角色身上。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_STANCE = "neutral"

# stance -> (给人看的名字, 表达特征 —— 直接进提示词)
STANCE_LABELS: dict[str, tuple[str, str]] = {
    "neutral": ("中性", "平常回话,没有特别的关系性意图。"),
    "please": ("讨好", "小心遣词,多用「好吗」「你说呢」,动作轻,姿态放低。"),
    "provoke": ("讨坏", "说话锋利,故意找茬或反着来,也可以是一句冷嘲。"),
    "test": ("试探", "半探性地问,让对方先亮牌;可以说「你以前不是这样的」。"),
    "avoid": ("回避", "短句,「嗯」「随便」,把话题拉开,不接对方递过来的话。"),
    "vent": ("宣泄", "长段独白,情绪先出来,不太顾对方接不接得住。"),
    "seduce": ("挑逗", "暗示性的语言和肢体挑动,不把话说明。"),
    "defer": ("顺从", "「都听你的」「你决定」,把选择权交回去。"),
}

STANCES: tuple[str, ...] = tuple(STANCE_LABELS)

# 提示词里那一段。`{stance_menu}` 是枚举表,`{current}` 是上一轮的 stance 说明。
#
# 措辞上有两处是**踩过坑改的**(2026-07-29 用真模型跑一局之后):
# 1. 必须明说这一行**不受回复格式规则约束**。回复格式那段是全篇最硬的一段
#    ("所有动作描写必须放在括号内""输出前逐个检查所有括号"),而它的正确示例是
#    以动作括号开头 —— 两段直接对撞,模型多半照那段走,于是标记根本不出现。
# 2. 必须说"漏了就等于没选"。原来写成"先选一个……并输出",模型读成可选项:
#    六轮里只有两轮真的声明了 stance,其余全落到兜底的中性上。
DEFAULT_STANCE_BLOCK_TEMPLATE = (
    "【关系性意图(stance)——每一次回复都必须以它开头】\n"
    "回复的**第一行**必须是这个,单独一行,不许省略:\n"
    "〔stance:xxx〕\n"
    "这一行是给系统看的标记,**不是动作描写**,不受上面括号格式规则的约束;"
    "写完这一行,再照原来的格式正常说话。漏了这一行就等于你没选。\n"
    "可选的 xxx 只有这几个,不许自创:\n"
    "{stance_menu}\n"
    "选择依据是你的性格、你和对方的关系、你此刻的心情和刚发生的事 —— "
    "哪怕你喜欢对方,今天也可以找茬。\n"
    "{current}"
    "选完就照那个意图说话:同一个意图不要每次用相同的方式表达,"
    "有时候一句刺人的话就够,有时候一个不理不睬的动作就够。"
)

_INERTIA_LINE = (
    "上一轮你对 ta 的意图是「{previous_label}」。真人不会一句讨好下一句找茬 —— "
    "除非中间发生了让你改主意的事,否则倾向于延续它。\n"
)


def is_stance(value: str | None) -> bool:
    return isinstance(value, str) and value in STANCE_LABELS


def normalize(value: str | None) -> tuple[str, bool]:
    """把 LLM 报上来的 stance 收敛到枚举里。返回 (stance, 是否真的声明了)。

    读不懂就退回 neutral —— 但**返回 False**,让上层能把"她选了中性"和"她没选、
    我们兜的底"分开。这两件事在产物上一模一样,只有这个标记能分开它们。
    """
    if value is None:
        return DEFAULT_STANCE, False
    candidate = str(value).strip().lower()
    if candidate in STANCE_LABELS:
        return candidate, True
    if candidate:
        logger.warning("LLM 报了一个不在枚举里的 stance %r,这轮按中性处理", value)
    return DEFAULT_STANCE, False


def stance_menu() -> str:
    return "\n".join(
        f"- {name}({label}):{hint}" for name, (label, hint) in STANCE_LABELS.items()
    )


def label(stance: str) -> str:
    return STANCE_LABELS.get(stance, STANCE_LABELS[DEFAULT_STANCE])[0]


def render_block(template: str, *, previous: str | None) -> str:
    """渲染 stance 提示块。`previous` 为空或 neutral 时不加惯性那句。"""
    current = ""
    if previous and previous != DEFAULT_STANCE and is_stance(previous):
        current = _INERTIA_LINE.format(previous_label=label(previous))
    return template.format(stance_menu=stance_menu(), current=current)
