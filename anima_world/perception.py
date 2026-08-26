"""认知层:世界的量里,**她感知得到哪些**。

`stocks` 是客观状态(树多高、矿还剩多少、她功力多少)。但客观存在 ≠ 她知道 ——
这两层混成一层,就会得到一个**无所不知的角色**:她随口说出矿的确切储量、别人暗中
的恨意、隔着半个地图那棵树的高度。那比"她什么都不知道"糟得多:不知道最坏是她没
注意到(玩家看得见她没注意),而知道得太多是**当场破戏,而且不可挽回**。

所以这一层的默认值定死:**没声明 = 感知不到。** 作者要哪个量被看见,就显式声明它
是哪一档:

| 档 | 意思 | 例子 |
|---|---|---|
| `self`   | 只有主人自己知道 | 她自己的功力、饿不饿 |
| `here`   | 得在同一个地方才知道 | 这棵树多高(要 `stock_places` 说它在哪) |
| `public` | 人人皆知 | 季节、粮价、战争 |
| `hidden` | 谁也不知道(默认) | 矿的真实储量、暗中的恨意 |

声明按 `(owner 种类, 量名)` 走,`*` 是通配 —— 因为可见性是"这类量是什么性质"的属性,
不是每个实例的属性:所有树的 `size` 都是在场可见,不必一棵棵写。

**声明本身就是开关。** 没有 `perception.enabled` 这种配置项:一个没声明过任何可见性
的世界,这一层是空的、不进提示词、不花一个 token。要点亮就去声明,粒度天然比一个
全局开关细。

## 名字(`label`)与意义(`bands`):她读到的那一行由这两样拼出来

    size 3.2  →(label=树高)→  树高 3.2  →(bands)→  树高 齐人高

**`label` 换的是名字。** 量在存储里叫什么(`size` / `stamina`)是引擎的账;她读到的
由 `label` 决定 —— "英文世界靠 `label` 机制与英文种子,不靠引擎换语言"这条路整个挂在
它上面。⚠️ 它曾经**只是存着**:声明里有、`ontology --json` 报得出,而这里渲染时用的
是原始量名。作者认认真真写了 `{"key": "size", "label": "树高"}`,世界照跑、日志干净,
她读到的还是 `size 3.2`。
⚠️ **量的名字和东西的名字是两张表**(`own/here/public_labels` 与 `labels`):
`老橡树` 是那棵树,`树高` 是它身上那个量,同一行里各占一个位置;合成一张就会互相盖,
而那只是读着别扭,不报错。反查**按种类做**:树的 `size` 是树高,矿的 `size` 是储量。

**`bands` 换的是值。** 看得见、有名字还不够:她感知到的是 `江水位 6.5` —— 一个
**引擎的账**,不是一个住在江边的人眼里的世界。把浮点数直接塞进提示词有两个后果,
都不报错:她把内脏当风景来描述;而 0.8 的雨算大算小全靠 LLM 猜,**两个模型猜出
两种雨**。

所以可见性声明上可以再挂一份**可选**的 `bands` —— 阈值升序,取最后一个 `<=` 当前值
的那一档的词:

    {"kind": "world", "key": "雨势", "visible": "public",
     "bands": [[0, "毛毛雨"], [0.4, "大雨"], [0.75, "瓢泼大雨"]]}

四条边界:

- **不写 `bands` 的量,行为逐位不变。** 和可见性、和本体逐字同构:声明本身就是开关。
  分档是作者的选择 —— 一个"库存 37 件"的量翻成"还剩不少"是在替他丢掉他要的精度。
- **第一档向下封口**:低于第一档阈值的值仍归第一档。另一种做法是回落成数字,而那
  会让同一个量时而是词、时而是数 —— 作者调试时看到的和上线后某个低值时刻看到的
  不是一回事。**想让下界有自己的词,就把第一档写在下界上**(通常是 0)。
- **最后一档向上封口**,同理。
- **渲染是赠品,键与数字是契约**:人话只进她的提示词。`Perception.own/here/public`
  里的**量名和数原样不动**,`to_dict()` 只**加** `words` / `labels` 两段(宿主要画
  水位曲线,那根线不该在某次升级之后突然画不出来;而把键换成 label 会让宿主的代码
  在作者改一个字的那天读到 KeyError)。她**做决定**读的也仍然是真值 —— 行为树的
  `StockCondition`、能力的 `when`/`requires` 一个字都没改:这两样只影响她怎么说。

坏声明**当场开不了机,一次列全**(`visibility_band_errors`)。跳过一条比报错坏得多:
世界照跑、日志干净,而她一直在报数字,作者要到三个月后才发现自己写错了一个方括号。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SELF, HERE, PUBLIC, HIDDEN = "self", "here", "public", "hidden"
VISIBILITIES = (SELF, HERE, PUBLIC, HIDDEN)
ANY_KIND = "*"

# 提示词里那一段。三行分别是"你自己/你这儿/人人都知道",空的那档不出现。
DEFAULT_PERCEPTION_BLOCK_TEMPLATE = (
    "【你此刻感觉到的】\n{lines}\n"
    "这些是你**确实知道**的事,可以自然地提到;没写在这儿的你就不知道,不要猜、"
    "也不要编具体数字。"
)


def _trim(value: float) -> str:
    """`8.0` → `8`,`0.35` → `0.35` —— 提示词里不要出现 `8.000000000001`。"""
    rounded = round(float(value), 3)
    return str(int(rounded)) if rounded == int(rounded) else str(rounded)


@dataclass
class Perception:
    """她此刻感知到的东西。分三档,便于渲染成人话,也便于宿主自己用。"""

    own: dict[str, float] = field(default_factory=dict)
    here: dict[str, dict[str, float]] = field(default_factory=dict)
    public: dict[str, float] = field(default_factory=dict)
    # 分档过的量:她读到的那个词。**和上面三档并列,不是替换** —— 数字是给宿主的
    # 契约,词是给她的。没声明 `bands` 的量在这三个字典里根本不出现。
    own_words: dict[str, str] = field(default_factory=dict)
    here_words: dict[str, dict[str, str]] = field(default_factory=dict)
    public_words: dict[str, str] = field(default_factory=dict)
    # 量的**人话名字**(作者写过 `label` 的才在里面)。⚠️ 和下面的 `labels` 是
    # **两张表**:那个是**东西**的名字(老橡树),这两个是**量**的名字(树高)。
    # 合成一张的话它们互相盖 —— 要么每棵树都叫"树高",要么每个量都叫"老橡树",
    # 而两种都只是提示词里读着别扭,不报错。
    # 🆕 3.8.0:**这一档是什么感觉。** 档词回答"多少",描述回答"那是什么滋味" ——
    # 老板那句「95 是亲密无间,然后加入描述」要的正是后半句。它和 `*_words` 并列,
    # **不是替换**:没写描述的量在这两个字典里根本不出现,于是**没有描述的世界
    # 提示词逐字节不变**(那条有一条单独的测试钉着)。
    own_notes: dict[str, str] = field(default_factory=dict)
    here_notes: dict[str, dict[str, str]] = field(default_factory=dict)
    own_labels: dict[str, str] = field(default_factory=dict)
    here_labels: dict[str, dict[str, str]] = field(default_factory=dict)
    public_labels: dict[str, str] = field(default_factory=dict)
    # 单位。⚠️ **这两格补的是一个洞**:`units` 那张 owner 表只填了 `here` 那一档,
    # 于是同一个 `unit: "点"` 在树身上念得出「树高 3.2米」,在她自己身上念成
    # 「体力 100」—— 作者写了一个字,引擎按感知档次决定认不认。
    own_units: dict[str, str] = field(default_factory=dict)
    public_units: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)   # 东西的名字(owner → 老橡树)
    # 本体给的两样:一行人话、她能对它做什么。没有本体的世界这两个是空的。
    glosses: dict[str, str] = field(default_factory=dict)
    verbs: dict[str, list[str]] = field(default_factory=dict)
    units: dict[str, dict[str, str]] = field(default_factory=dict)
    # 这儿的**人**此刻在做什么(`agent:齐` → `在陪一次夜播`)。闲着的人不在里面 ——
    # 没有那一句本身就是闲着。措辞由调用方给(`World._activities_now`),这一层只负责
    # 把它印在对的那一行上:同一个世界里"某某在做什么"只能有一种说法,而这一层要是
    # 自己再拼一遍,她说话时看到的和她做决定时看到的就是两个世界。
    activities: dict[str, str] = field(default_factory=dict)
    # 这里还有多少样东西没带进来。**必须说出来** —— 截断了却不吭声,等于让她在一个
    # "她以为只有三棵树"的世界里做决定,而她永远不会知道自己被骗了。
    overflow: int = 0

    def is_empty(self) -> bool:
        return not (self.own or self.here or self.public)

    def to_dict(self) -> dict[str, Any]:
        """给宿主的那份。**数字原样,档词只是多出来的一段。**

        `words` / `labels` 是**加**上去的(各自 `{"own","here","public"}`,没分档 /
        没写 label 的量不出现):宿主要自己排版,而把 `public["江水位"]` 从 6.5 换成
        "上了江岸街"、或者把键从 `江水位` 换成 label,都会让宿主的代码在作者改一个
        字的那天读到 KeyError 或者画不出那根水位曲线 —— **键与数字是契约,人话是
        赠品**。

        `verbs` 那一栏是唯一不能靠别处推出来的:量给得出数字,数字不告诉你
        「擦一擦」这个词存不存在,而**按实例查**才认得出作者只给这一扇窗开的口子。
        宿主自己拼一份动词表出来是这一层最容易犯的错 —— 拼错了不报错,按钮点下去
        才发现世界不认。`names` 是这几样东西的人话名字,`glosses` 是那一行说明。
        """
        return {"own": dict(self.own), "here": {k: dict(v) for k, v in self.here.items()},
                "public": dict(self.public), "overflow": self.overflow,
                "words": {
                    "own": dict(self.own_words),
                    "here": {k: dict(v) for k, v in self.here_words.items()},
                    "public": dict(self.public_words),
                },
                "labels": {
                    "own": dict(self.own_labels),
                    "here": {k: dict(v) for k, v in self.here_labels.items()},
                    "public": dict(self.public_labels),
                },
                # 只加不改的一格(3.8.0):描述是**给她的**,数字仍然是给宿主的契约。
                "notes": {
                    "own": dict(self.own_notes),
                    "here": {k: dict(v) for k, v in self.here_notes.items()},
                },
                "verbs": {k: list(v) for k, v in self.verbs.items()},
                "names": dict(self.labels),
                "glosses": dict(self.glosses),
                # `units` 按 owner 排(和 `here` 一个键),`own_units` /
                # `public_units` 跟着 `own` / `public` 那两个**扁平**字典走。
                # 把三档合成一张嵌套表读起来更整齐,但那是改形状 —— 镜像端会当场
                # 读到 KeyError,而这一格从来只加不改。
                "units": {k: dict(v) for k, v in self.units.items()},
                "own_units": dict(self.own_units),
                "public_units": dict(self.public_units),
                # 只加不改的又一格:宿主那一屏上"这儿的人此刻在做什么"此前问不出来,
                # 只能自己去猜 —— 而猜出来的和她读到的不是同一句话。
                "activities": dict(self.activities)}

    def describe_own(self) -> str:
        """`功力 120点、体力 有点累` —— 她自己那一行的正文。"""
        return _describe(self.own, self.own_words, self.own_units, self.own_labels)

    def describe_public(self) -> str:
        """`粮价 7文、雨势 瓢泼大雨` —— 人人都知道那一行的正文。"""
        return _describe(self.public, self.public_words, self.public_units,
                         self.public_labels)

    def describe_here(self, owner: str) -> str:
        """`门口那棵老橡树[tree:harbor_oak](树冠遮住半条街):树高 3.2米。可以照料、收取产出`

        一行里有**两种名字**,各归各的:`老橡树` 是这个东西的名字(`labels`),
        `树高` 是它身上那个量的名字(`here_labels`)。

        这儿的人还多一段:`齐老板,在陪一次夜播:体力 87`。它**另占一个逗号**,不并进
        `gloss` 那对括号 —— 括号里是这个人一直是什么样,逗号后面是他这一刻在做什么,
        合起来会让一件转瞬的事读着像身份。
        """
        name = self.labels.get(owner) or owner
        gloss = self.glosses.get(owner)
        body = _describe(
            self.here.get(owner, {}), self.here_words.get(owner, {}),
            self.units.get(owner, {}), self.here_labels.get(owner, {}),
            self.here_notes.get(owner, {}),
        )
        verbs = self.verbs.get(owner)
        # id 只在她**真能对它做点什么**时才露出来:那是 `interact` 的参数,不是装饰。
        # 一个只能看不能碰的东西带上 `tree:harbor_oak` 这种字符串,纯粹是提示词噪音,
        # 还会诱她去调一个必然被拒的调用。
        line = f"{name}[{owner}]" if verbs and name != owner else name
        line = f"{line}({gloss})" if gloss else line
        doing = self.activities.get(owner)
        line = f"{line},{doing}" if doing else line
        line = f"{line}:{body}" if body else line
        # 能力放在最后,因为它是**她下一步能做的事** —— 属性她只会念,动词她会用。
        return f"{line}。可以{'、'.join(verbs)}" if verbs else line

    def render(self, template: str = DEFAULT_PERCEPTION_BLOCK_TEMPLATE) -> str | None:
        """渲染成提示词里的一段。感知不到任何东西就返回 None —— 空块不进提示词。"""
        if self.is_empty():
            return None
        lines: list[str] = []
        if self.own:
            lines.append(f"- 你自己:{self.describe_own()}")
        for owner in sorted(self.here):
            lines.append(f"- 这里的{self.describe_here(owner)}")
        if self.overflow:
            lines.append(f"- 这里还有 {self.overflow} 样别的东西,你没细看")
        if self.public:
            lines.append(f"- 人人都知道:{self.describe_public()}")
        # **描述单占一节,紧跟在感知之后**,而且只列**她自己身上**有描述的那几样。
        # 挤进上面那一行的话,一行里会同时有"多少"和"那是什么滋味",而她读到的
        # 是一段越来越长、越来越难分主次的话。别人身上那几句跟在档词后面(见
        # `describe_here`)—— 那是她**看出来的**,不是她**体会到的**,两者不该并排。
        if self.own_notes:
            lines.append("")
            for key in sorted(self.own_notes):
                said = self.own_labels.get(key) or key
                word = self.own_words.get(key)
                head = f"{said} {word}" if word else said
                lines.append(f"- {head} —— {self.own_notes[key]}")
        try:
            return template.format(lines="\n".join(lines))
        except (KeyError, IndexError, ValueError):
            logger.warning("perception 块渲染失败,这轮不带感知")
            return None


def _describe(
    values: dict[str, float], words: dict[str, str], units: dict[str, str] | None = None,
    labels: dict[str, str] | None = None, notes: dict[str, str] | None = None,
) -> str:
    """一档量渲染成提示词里的正文:**`label` 换名字,`bands` 换值。**

        size 3.2   →(label=树高)→   树高 3.2   →(bands)→   树高 齐人高

    两样正交,各占一个位置。让档词顶掉名字的话,`- 这里的老橡树:齐人高` 读起来
    像那棵树叫齐人高;让名字顶掉档词的话,`bands` 白写。

    没写 `label` 的量出量名本身,没分档的量出数字 —— 两个开关各自独立,都不写
    就和从前逐位相同。单位跟着**数字**走:`树高 齐屋檐高米` 是引擎自己印上去的
    噪音,而她会照着念出来。

    **排序按量名(key),不按 label。** 她读到的次序于是不随作者改一个字而变,
    而次序不确定的提示词是没法 diff 的。
    """
    units, labels, notes = units or {}, labels or {}, notes or {}
    return "、".join(
        readout_text(key, values[key], words.get(key), units.get(key, ""), labels.get(key))
        # 🆕 3.8.0:别人身上那句描述**跟在档词后面**,用括号收着 —— 它是她**看出来
        # 的**,和自己那一节(体会到的)不是一回事,所以措辞与位置都不同。
        + (f"({notes[key]})" if notes.get(key) else "")
        for key in sorted(values)
    )


def readout_text(
    key: str, value: float, word: str | None = None, unit: str = "",
    label: str | None = None,
) -> str:
    """**一个量该被念成什么** —— 这个仓库里唯一的那条措辞规矩。

    `label` 换名字、`bands` 换值、单位跟着**数字**走(档词后面不跟单位)。
    抽出来是因为它有第二个读者:玩家那一屏(`World.player_options`)。同一个世界
    的同一个量,她读到「雨势 瓢泼大雨」而玩家读到 `雨势 0.82`,不是排版问题 ——
    是两个人看见了两种东西。各写一遍的话它们必然分叉,而分叉了不报错。
    """
    name = label or key
    return f"{name} {word}" if word else f"{name} {_trim(value)}{unit}"


def readouts(
    values: dict[str, float], words: dict[str, str], units: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """一档量摊成宿主那一屏上的一排:`[{key,label,value,word,unit,text}, …]`。

    **`text` 是给人看的,其余五格是给代码用的** —— 宿主想自己排版(把档词做成
    一个色块、把数字画成一根曲线)就拿分开的那五格,不想排版就印 `text`。
    只给 `text` 等于逼所有人接受引擎的排版;只给五格等于让每个宿主各自拼一遍
    措辞,而拼出来的和她读到的不一样。

    `label` 这一格**总是印得出来**(作者没写就是量名本身),和 `_describe` 里那个
    `name` 同一条回落 —— 宿主拿它当显示名,不必自己再写一次 `label or key`。
    排序同样按量名(key),理由见 `_describe`。
    """
    units, labels = units or {}, labels or {}
    rows: list[dict[str, Any]] = []
    for key in sorted(values):
        word = words.get(key) or ""
        unit = units.get(key, "")
        rows.append({
            "key": key,
            "label": labels.get(key) or key,
            "value": float(values[key]),
            "word": word,
            "unit": unit,
            "text": readout_text(key, values[key], word or None, unit, labels.get(key)),
        })
    return rows


def band_word(bands: Any, value: float) -> str | None:
    """这个值落在哪一档:**最后一个阈值 `<=` 它的那一档**。没分档就返回 `None`。

    两头都封口:低于第一档阈值的归第一档(见模块头),高于最后一档的归最后一档。
    """
    if not bands:
        return None
    chosen = bands[0][1]
    for threshold, word in bands:
        if value >= threshold:
            chosen = word
        else:
            break            # 阈值升序,后面的更够不着
    return str(chosen)


def band_note(bands: Any, notes: Any, value: float) -> str:
    """这个值落在哪一档,那一档写了什么描述。没写(或没分档)就是空串。

    **和 `band_word` 挑的是同一档**,所以两句话永远说的是同一件事 —— 各自再算
    一遍"落在第几档"的话,某个刚好等于阈值的值上,词和描述会来自两档。
    """
    if not bands or not notes:
        return ""
    chosen = 0
    for position, row in enumerate(bands):
        if float(value) >= float(row[0]):
            chosen = position
    notes = list(notes)
    return str(notes[chosen]) if chosen < len(notes) else ""


def parse_bands(raw: Any) -> tuple[tuple[float, str], ...]:
    """存储/声明里那份 `[[0, "毛毛雨"], …]` 规范成 `((0.0, "毛毛雨"), …)`。

    **只规范,不判对错** —— 判断在 `band_errors`,而且是在写库之前判的。这里再判
    一遍就是第二份判断,两份迟早给出不同答案。形状不对的当没写(闸在前面)。
    """
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[tuple[float, str]] = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            return ()
        threshold, word = entry
        try:
            out.append((float(threshold), str(word)))
        except (TypeError, ValueError):
            return ()
    return tuple(out)


def band_errors(raw: Any, *, label: str) -> list[str]:
    """一份 `bands` 声明的毛病,一行一条(空 = 没问题;没写 `bands` 也是空)。

    每一条都对着一种"不拦就静默变成报数字"的写法。作者面向的话一律中文 ——
    读它的是世界的作者,不是宿主的开发者。
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)) or isinstance(raw, str):
        return [f"{label}.bands 必须是一个数组:[[阈值, 词], …],收到 {type(raw).__name__}"]
    if len(raw) == 0:
        return [f"{label}.bands 是空的 —— 不分档就整个别写这一项(写了却不生效,"
                f"和写错一样查不出来)"]

    errors: list[str] = []
    previous: float | None = None
    for index, entry in enumerate(raw):
        where = f"{label}.bands[{index}]"
        if isinstance(entry, str) or not isinstance(entry, (list, tuple)) or len(entry) != 2:
            errors.append(f"{where} 必须是 [阈值, 词] 两项,收到 {entry!r}")
            continue
        threshold, word = entry
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
                or not math.isfinite(float(threshold)):
            # NaN 和任何数比大小都是 False,于是那一档**永远选不中**,而它看上去
            # 写得好好的 —— 和阈值写成字符串同一类坏法。
            errors.append(f"{where} 的阈值必须是数字(有限的),收到 {threshold!r}")
            continue
        if not isinstance(word, str) or not word.strip():
            errors.append(
                f"{where} 的词必须是字符串,收到 {word!r}" if not isinstance(word, str)
                else f"{where} 的档词不能是空的 —— 她读到的就是这几个字"
            )
            continue
        threshold = float(threshold)
        if previous is not None and threshold <= previous:
            # 相等也不行:后一条永远盖住前一条,前一条是死档 —— 作者以为世界里
            # 有三种雨,其实只有两种。
            errors.append(
                f"{where} 的阈值 {threshold:g} 必须严格大于上一档的 {previous:g} —— "
                f"阈值要**升序**,取的是最后一个 <= 当前值的那一档"
            )
        previous = threshold
    return errors


def visibility_band_errors(seed: Any) -> list[str]:
    """作者层里所有 `bands` 声明的毛病,**一次列全**。

    加载时当场报错的那道闸就是它 —— 修一条重开一次的话,一份写错三处的文件要开机
    三次,而每次开机都可能在别的表上留下半截东西。
    """
    if not isinstance(seed, dict):
        return []
    rows = seed.get("stock_visibility")
    if not isinstance(rows, (list, tuple)):
        return []
    errors: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or "bands" not in row:
            continue
        name = str(row.get("key") or "").strip()
        label = f"stock_visibility[{index}]" + (f" ({name})" if name else "")
        errors.extend(band_errors(row.get("bands"), label=label))
    return errors


def bands_of(
    rules: dict[tuple[str, str], Any], owner_kind: str, key: str
) -> tuple[tuple[float, str], ...]:
    """先精确匹配 `(种类, 量名)`,再通配 `(*, 量名)` —— 和 `visibility_of` 同一条路。"""
    found = rules.get((owner_kind, key))
    if found is None:
        found = rules.get((ANY_KIND, key))
    return parse_bands(found) if found else ()


def label_of(rules: dict[tuple[str, str], str], owner_kind: str, key: str) -> str:
    """这个量的人话名字;没写过就是空串(调用方回落成量名本身)。

    **反查按种类做,不是全世界一张表** —— 同一个量名在两个种类上是两个东西
    (树的 `size` 是树高,矿的 `size` 是储量)。一张全局表只留得下一个,而剩下
    那个会把另一个的名字顶掉:世界照跑、日志干净,只是矿脉报了个树高。
    (动词的人话反查踩过同一个坑,那里的修法也是这个。)
    """
    found = rules.get((owner_kind, key))
    if found is None:
        found = rules.get((ANY_KIND, key))
    return str(found or "")


def visibility_of(
    rules: dict[tuple[str, str], str], owner_kind: str, key: str
) -> str:
    """先精确匹配 (种类, 量名),再通配 (`*`, 量名),都没有就是 `hidden`。"""
    if (owner_kind, key) in rules:
        return rules[(owner_kind, key)]
    if (ANY_KIND, key) in rules:
        return rules[(ANY_KIND, key)]
    return HIDDEN


def why_not_perceivable(
    rules: dict[tuple[str, str], str],
    *,
    agent_id: str,
    here: str,
    owner: str,
    key: str,
    place_of: Any = None,
) -> str:
    """她此刻**感知不到** `(owner, key)` 的理由;感知得到就返回空串。

    这是 `perceive()` 那三档的逐项版本,给行为树的 `StockCondition` 用。三个理由
    区别很大,所以分开说而不是返回一个 bool:

    | 理由 | 意思 | 作者该怎么办 |
    |---|---|---|
    | `hidden` | 这个量根本没声明过可见性 | **写错了** —— 这条分支永不触发,要吼一声 |
    | `not_mine` | 声明成 `self`,而这是别人的量 | 写错了,同上 |
    | `elsewhere` | 声明成 `here`,而她此刻不在那儿 | **正常** —— 走过去就看得见了 |

    前两条是静态的(世界怎么跑都不会变),第三条随她走动而变。把它们混成一个
    bool,调用方就只能要么对正常情况刷屏、要么对写错的声明一声不吭。
    """
    kind = owner.split(":", 1)[0] if ":" in owner else owner
    level = visibility_of(rules, kind, key)
    if level == HIDDEN:
        return "hidden"
    if owner == f"agent:{agent_id}":
        return ""  # 自己身上的量:声明成哪一档,她自己都知道
    if level == PUBLIC:
        return ""
    if level == SELF:
        return "not_mine"
    where = place_of(owner) if place_of is not None else None
    return "" if here and where == here else "elsewhere"


def perceive(
    *,
    agent_id: str,
    here: str,
    stock_store: Any,
    visibility: Any,
    world_owner: str = "world",
    ontology: Any = None,
    default_budget: int = 5,
    activities: dict[str, str] | None = None,
) -> Perception:
    """这个角色此刻感知到什么。纯读,无 LLM。

    `visibility` 是鸭子类型(`RedisVisibilityStore`),要求 `rules_map` / `at` 两个方法;
    SQLite 版 `VisibilityStore` 已退役。

    三档各查一次:自己身上的、同地那些东西的、全局公开的。没有任何声明时三次查询
    都会落空,整块为空 —— 所以未声明的世界在这里几乎不花代价。

    `ontology` 给的是两样**不改变她看得见什么**的东西:同地那一档的上限(按种类),
    以及每样东西的一行人话与能力动词。没有本体的世界照旧跑,只是列表按
    `default_budget` 封顶、没有人话也没有动词。

    名字与分档同理,而且互相独立:写过 `label` 的量额外带一个人话名字(`*_labels`),
    声明过 `bands` 的量额外带一个词(`*_words`),**量名与数字照旧原样在**。
    两样都没写的世界这一半整个不存在,和从前逐位相同。

    `activities` 是**可选**的一份"谁此刻在做什么"(`{owner: 一句人话}`,由
    `World._activities_now()` 给)。不给就是这一层从前的样子。措辞有意不在这儿算:
    同一个世界里"某某在做什么"只能有一种说法,而这里另拼一遍就会和提示词的
    presence 块分叉 —— 分叉了不报错。
    """
    rules = visibility.rules_map()
    result = Perception()
    if not rules:
        return result
    # 鸭子类型再宽一格:老的可见性 store 没有 `bands_map` / `labels_map`,那样的
    # 世界就是"一个量都没分档、都没起名字",而不是开不了机。
    bands_rules = getattr(visibility, "bands_map", None)
    bands_rules = bands_rules() if callable(bands_rules) else {}
    label_rules = getattr(visibility, "labels_map", None)
    label_rules = label_rules() if callable(label_rules) else {}

    notes_rules = getattr(visibility, "notes_map", None)
    notes_rules = notes_rules() if callable(notes_rules) else {}

    def word_of(kind: str, key: str, value: float) -> str | None:
        return band_word(bands_of(bands_rules, kind, key), value) if bands_rules else None

    def note_of(kind: str, key: str, value: float) -> str:
        """这一档那句描述。**没写就是空串,而空串不进任何一个字典** ——
        「没有描述的世界提示词逐字节不变」这条整个挂在这一句上。"""
        if not notes_rules:
            return ""
        return band_note(bands_of(bands_rules, kind, key),
                         notes_rules.get((kind, key)) or (), value)

    def name_of(kind: str, key: str) -> str:
        # 和量名一样的 label 不留 —— 本体那条路上没写 label 的量存的就是量名本身
        # (`render_label()` 的回落),留着只会让"作者到底起没起过名字"看不出来。
        name = label_of(label_rules, kind, key) if label_rules else ""
        return "" if name == key else name

    own_owner = f"agent:{agent_id}"
    # 单位按**种类**查,所以玩家(`agent:player:p1`)和角色查的是同一份声明。
    own_units = ontology.units_of(own_owner) if ontology is not None else {}
    for key, value in stock_store.of(own_owner).items():
        level = visibility_of(rules, "agent", key)
        if level in (SELF, HERE, PUBLIC):
            # 自己的量:`self` 当然看得见;声明成 here/public 的更宽,也看得见。
            result.own[key] = value
            word = word_of("agent", key, value)
            if word:
                result.own_words[key] = word
            note = note_of("agent", key, value)
            if note:
                result.own_notes[key] = note
            name = name_of("agent", key)
            if name:
                result.own_labels[key] = name
            unit = own_units.get(key)
            if unit:
                result.own_units[key] = unit

    if here:
        # **按种类分别封顶。** 有上限本身不是可选的:这一格里站着一万棵树的话,
        # 它们住 Redis 还是 MySQL 都一样把提示词撑爆 —— 存储分层保护不了提示词,
        # 所以有界性归这里(渲染器),不归存储。
        # 排序按 owner id:要的是**确定**,不是"最有意思的那几个"(那需要一个
        # 谁也说不清的重要度,而不确定的截断会让同一个世界每次给她不同的现实)。
        by_kind: dict[str, list[tuple[str, str | None]]] = {}
        for owner, label in sorted(visibility.at(here).items()):
            if owner == own_owner:
                continue
            by_kind.setdefault(owner.split(":", 1)[0] if ":" in owner else owner, []).append(
                (owner, label)
            )
        for kind, members in by_kind.items():
            budget = ontology.budget_of(members[0][0]) if ontology is not None else default_budget
            for owner, label in members[:budget]:
                seen = {
                    key: value
                    for key, value in stock_store.of(owner).items()
                    if visibility_of(rules, kind, key) in (HERE, PUBLIC)
                }
                if not seen:
                    continue
                result.here[owner] = seen
                words = {
                    key: word for key, value in seen.items()
                    if (word := word_of(kind, key, value))
                }
                if words:
                    result.here_words[owner] = words
                seen_notes = {
                    key: note for key, value in seen.items()
                    if (note := note_of(kind, key, value))
                }
                if seen_notes:
                    result.here_notes[owner] = seen_notes
                names = {key: name for key in seen if (name := name_of(kind, key))}
                if names:
                    result.here_labels[owner] = names
                if label:
                    result.labels[owner] = label   # 东西的名字,不是量的名字
                if ontology is not None:
                    gloss, verbs = ontology.describe(owner)
                    if gloss:
                        result.glosses[owner] = gloss
                    if verbs:
                        result.verbs[owner] = verbs
                    units = ontology.units_of(owner)
                    if units:
                        result.units[owner] = units
            result.overflow += max(0, len(members) - budget)
        # 只留这儿真的看得见的那几个。整份带过来的话,一个隔着半个地图的人在做的事
        # 会被 `to_dict()` 报给宿主 —— 而这一层的默认值定死是"没声明 = 感知不到",
        # 一份旁路的名单会从背面把它拆掉。
        if activities:
            result.activities = {
                owner: text for owner, text in activities.items() if owner in result.here
            }

    world_units = ontology.units_of(world_owner) if ontology is not None else {}
    for key, value in stock_store.of(world_owner).items():
        if visibility_of(rules, "world", key) == PUBLIC:
            result.public[key] = value
            word = word_of("world", key, value)
            if word:
                result.public_words[key] = word
            name = name_of("world", key)
            if name:
                result.public_labels[key] = name
            unit = world_units.get(key)
            if unit:
                result.public_units[key] = unit

    return result
