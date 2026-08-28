"""设计者写的算术表达式 —— 解析、校验、求值。

世界的"规律"要成为数据(见 `rules.py`),就意味着引擎要执行**别人写的字符串**。
这是这个包里唯一一处有安全含义的地方,所以规矩定死:

- **绝不 `eval`。** 表达式被解析成 AST、逐个节点过白名单,然后由这里自己的
  解释器求值。白名单之外的一切(属性访问、下标、lambda、推导式、导入…)在
  **解析时**就被拒,不是求值时。
- **解析在加载时,不在求值时。** 坏表达式必须在世界启动前当场报错 —— 和节拍
  脚本同一条硬要求。一个在第 300 tick 才炸的公式,等于把作者的错误藏了 300 tick。
- **自由变量是可见的。** `Expression.names` 报出这个表达式读了什么,调用方据此
  校验"它引用的量存在吗",而不是等到求值时才发现读了个不存在的名字。

支持的东西刻意很少:四则、比较、与或非、三元、以及几个数学函数。**这是有意的** ——
设计者要的是"算数 + 一点条件",不是一门语言。需要更强的表达力时,那说明它该是一段
注册进来的 Python 函数,而不是一个更长的字符串。

唯一一个不是纯函数的名字是 `rand()`,而它是**假的随机**,见 `world_dice`。
"""

from __future__ import annotations

import ast
import hashlib
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping


class ExpressionError(ValueError):
    """表达式写错了(解析期),或者求值时算不出来(除零、读了不存在的名字)。"""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# 白名单函数。加新的之前先问:它会不会引入不可预测的开销或副作用。
_FUNCTIONS: dict[str, Any] = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "clamp": _clamp,
    "floor": math.floor,
    "ceil": math.ceil,
}

# **不是纯函数的那一个。** 它的值不在表达式里,而由调用方按"这是哪一刻"给出
# (`Expression.evaluate(ns, dice=…)`),所以它不在 `_FUNCTIONS` 里。
DICE_NAME = "rand"

#: 边上那三个前缀 —— **两层点号只在它们下面开**(见 `_validate` 的 `Attribute` 那一支)。
#: 它们是内核保留字(`plugins.RESERVED_IDS`),所以插件 id 永远拿不到它们,
#: 于是"这是边的前缀"和"这是某个插件"分得开 —— 而分不开的样子是安静的。
#:
#: 🔴 **为什么不是 `from` / `to`(设计稿里写的那两个词)**:`from` 是 **Python 的
#: 关键字**,而这一层的表达式是 `ast.parse` 解析的 —— `from.qi.灵力` 连语法都过不去
#: (实测 `invalid syntax`)。**一个解析不出来的名字不是名字**,所以这里换成
#: `src` / `dst`。⚠️ **边的声明里那两个键仍然叫 `from` / `to`**(那是 JSON,不是
#: 表达式,不受这条限制);两套词只在这一处分岔,而分岔的理由写在这儿。
EDGE_PREFIXES = frozenset({"src", "dst", "edge"})

# `a ** b` 里 b 的上限。没有它,一个手滑的 `2 ** 999999999` 能把 tick 线程按住 ——
# 而规律是跑在 tick 上的纯算术,它卡住就是整个世界卡住。
_MAX_EXPONENT = 64

_BINARY_OPS: dict[type, Any] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
}

_COMPARE_OPS: dict[type, Any] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}


def world_dice(world_id: str, rule_id: str, owner: str, tick: int) -> float:
    """`rand()` 摇出来的那个数:[0,1),由**四个坐标**折出来,不是随机数。

    **它是"这个世界这一刻的骰子",不是随机数。** 引擎的 replay 纪律没有松:同一个
    世界、同一条规律、同一个 owner、同一 tick —— 永远得同一个值。换掉四个坐标里的
    任何一个才是另一个数。于是"阵雨、意外、运气"表达得出来,而一条时间线重放两遍
    仍然逐位一致(在此之前雨势只能是常数 0.8,世界里一切偶然性没有出口)。

    三条实现上的硬要求:

    - **不许用 `random` 模块,不许读时间。** 那两样都让"同一刻"变成两个答案,
      而一个不可重放的世界连 bug 都复现不了。
    - **不许用内置 `hash()`。** 它对 str 加了每进程一份的盐(PYTHONHASHSEED),
      于是同一个世界在两个进程里摇出两副骰子 —— 而这个引擎的世界本来就是很多
      进程同时在操作的。blake2b 是**跨进程、跨机器、跨版本**都一样的那种折法。
    - **取 53 位再除 2**53**,不是 64 位除 2**64。`(2**64-1)/2**64` 在双精度里
      **会舍入成 1.0**,于是 `rand() < 1` 偶尔为假 —— 一个几千次才出现一次的
      破例,查起来要命。53 位是 double 的尾数宽度,除得干净。
    """
    material = "\x1f".join((world_id, rule_id, owner, str(int(tick)))).encode("utf-8")
    raw = int.from_bytes(hashlib.blake2b(material, digest_size=8).digest(), "big")
    return (raw >> 11) * 2.0 ** -53


@dataclass(frozen=True)
class Expression:
    """一条解析并校验过的表达式。`names` 是它读到的自由变量。"""

    source: str
    names: frozenset[str]
    _tree: ast.expr

    def evaluate(
        self, namespace: Mapping[str, Any], *, dice: Callable[[], float] | None = None
    ) -> Any:
        """按命名空间求值。读不到的名字、除零 → `ExpressionError`。

        `dice` 是 `rand()` 的来源:调用方知道"这是哪个世界、哪条规律、哪个 owner、
        哪一 tick",表达式自己不知道。**不给就等于这里没有骰子**,`rand()` 当场
        报错而不是悄悄给个 0 —— 一个永远返回 0 的骰子会让"三成概率下雨"变成
        "永远下雨",而且不报错。
        """
        return _evaluate(self._tree, namespace, self.source, dice)

    def __str__(self) -> str:  # 出错信息里要看得见原文
        return self.source


def compile_expression(source: str, *, dice: bool = False) -> Expression:
    """把一行算术编译成可求值的东西。写错了当场抛 `ExpressionError`。

    `dice` 是**编译这一处有没有骰子**,由调用方声明,默认**没有**。

    这一层的分工是"加载时严格、运行期降级":一处写了 `rand()` 而那儿根本摇不出
    骰子的声明,应当在**加载时**就被拒掉,而不是等到她真的去做那件事时才报错 ——
    能力(affordance)的 `when` / `requires` / `costs` / `set` 走的是不带骰子的求值,
    从前在那儿写 `rand()` 编译得过、开机也不响,直到某个玩家点下那个按钮才炸。
    默认收紧成"没有骰子"之后,那份声明连世界都开不起来,而错处指得到具体哪个动词。

    规律层是唯一有骰子的地方(它有那四个坐标),所以只有 `rules.py` 传 `dice=True`。
    要给能力也发骰子,得先给它一组自己的坐标(世界/种类+动词/施动者/对象/tick),
    在那之前**别把默认值改成 True** —— 那等于让一处没有骰子的地方编译得过。
    """
    if not isinstance(source, str) or not source.strip():
        raise ExpressionError("表达式是空的")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"{source!r} 语法不对:{exc.msg}") from None

    names: set[str] = set()
    _validate(tree.body, source, names, dice=dice)
    return Expression(source=source, names=frozenset(names), _tree=tree.body)


_COMPARE_SYMBOLS: dict[type, str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
}


def lower_bounds(expression: Expression) -> dict[str, float]:
    """这条式子要成立,每个名字**至少**得有多大。`名字 >= 数` / `名字 > 数` 那种。

    只认**整条式子就是一个比较**的那种写法(`me_主动 >= 1.2`),`and` / `or` 一概
    不认 —— 一个 or 里的分支不成立不等于整条不成立,而这一层的每个调用方都在问
    「不满足会怎样」。认错了就会去报一道其实开得了的门。
    """
    node = expression._tree
    if not isinstance(node, ast.Compare):
        return {}
    out: dict[str, float] = {}
    operands = [node.left, *node.comparators]
    for (left, right), op in zip(zip(operands, operands[1:]), node.ops):
        if isinstance(op, (ast.GtE, ast.Gt)) and isinstance(left, ast.Name):
            need, name = _numeric(right), left.id
        elif isinstance(op, (ast.LtE, ast.Lt)) and isinstance(right, ast.Name):
            need, name = _numeric(left), right.id
        else:
            continue
        if need is not None:
            out[name] = max(out.get(name, -math.inf), need)
    return out


def reachable_ceiling(expression: Expression, name: str, base: float) -> float:
    """这条式子写回 `name` 之后,`name` 最高能到多少?**不知道就答 `inf`。**

    给「够不到的门槛」那道 lint 用(`ontology.unreachable_requirements`):一道
    `me_主动 >= 1.2` 的门,如果这个世界里没有任何一处抬得动「主动」,那它就是一个
    **数学上永远开不了**的门 —— 而玩家看得见那个按钮、点得到、每次收到的都是
    「你的主动不够」。他会一直试。**预防不了的失败教不会他任何东西**,而这一条
    比拼错量名更坏:拼错还有闸,这个连一句话都没有。

    只认得出**几种封得死的写法**,别的一律 `inf`(= 不知道 = 不报)。误报够多次的
    警告等于没有警告,这一条和 `drift_warnings` 同一条纪律:

        me_X                   → base          自己等于自己
        me_X - k   (k ≥ 0)     → base          只减不增
        max(f, k)              → max(ceil(f), k)   下限会把值**抬**到 k
        min(f, k)              → min(ceil(f), k)
        clamp(f, lo, hi)       → 夹一道
        其它(含 `me_X + …`)   → inf

    ⚠️ `max(f, k)` 那一行是这里唯一容易写错的地方:直觉上 `max` 是"保底",而保底
    正是一种**抬升** —— `max(me_主动 - 0.02, 5)` 会把一个 1.0 的量顶到 5。把它当
    非增处理的话,这道 lint 会去报一个其实开得了的门,而那种误报比漏报贵得多。
    """
    return _ceiling(expression._tree, name, base)


def _nonnegative(node: ast.expr) -> bool:
    """这一段的值**一定** ≥ 0 吗?拿不准就答 False。

    只为衰减那种写法存在:`量 - 0.006 * dt`。不认 `dt` 的话每一条按流逝折算的
    衰减规律都算成"不知道",而**按流逝折算恰恰是这个引擎劝作者写的那一种**
    (`drift_warnings` 整条就在劝这个)—— 于是这道 lint 在真实世界里永远不响。

    `dt` 是"过去了几个 tick",引擎里它永不为负;别的名字符号一概不知道。
    """
    number = _numeric(node)
    if number is not None:
        return number >= 0
    if isinstance(node, ast.Name):
        return node.id == "dt"
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult, ast.Div)):
        return _nonnegative(node.left) and _nonnegative(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in ("min", "max", "abs", "floor", "ceil", "clamp"):
            return all(_nonnegative(arg) for arg in node.args)
    return False


def _ceiling(node: ast.expr, name: str, base: float) -> float:
    if isinstance(node, ast.Name):
        return base if node.id == name else math.inf
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
        # 减一个**符号确定为非负**的东西才算得准。减一个量的话符号不知道,而
        # `me_X - me_疲劳` 在疲劳为负时是在加。
        if _nonnegative(node.right):
            return _ceiling(node.left, name, base)
        return math.inf
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        args = node.args
        if node.func.id == "max" and len(args) == 2:
            floor = _numeric(args[1])
            if floor is not None:
                return max(_ceiling(args[0], name, base), floor)
        elif node.func.id == "min" and len(args) == 2:
            cap = _numeric(args[1])
            if cap is not None:
                return min(_ceiling(args[0], name, base), cap)
        elif node.func.id == "clamp" and len(args) == 3:
            low, high = _numeric(args[1]), _numeric(args[2])
            if low is not None and high is not None:
                return min(max(_ceiling(args[0], name, base), low), high)
    return math.inf


def rewrite_source(
    expression: Expression,
    name_text: Callable[[str], str | None] | None = None,
    threshold_text: Callable[[str, float, str], tuple[str, str] | None] | None = None,
) -> str:
    """把原文里的**名字**和**阈值**换成别的说法,别处一个字不动。

    给拒绝语用(`ontology.speak_expression`):那句话最后印在玩家屏幕上,而
    `me_体力` 和 `0.55` 都不是这个世界里的说法。这一层只回答"哪一段文字是个名字、
    哪一段是某个量的阈值",**说法由调用方给** —— 词汇归本体那边。

    **按语法树上的位置改,不按正则。** 拿名字拼一条正则去替换有两个洞:换出来的
    字会被后一轮再换一次(`土湿` 换成「土」,轮到名字 `土` 那一遍再咬它一口),
    而字符串字面量里恰好写着一个量名时也一样被改掉。位置两样都没有。

    `threshold_text(name, value, op)` **只在比较的另一头是个光名字时才问** ——
    `土 > 0.55` 问得到,`土 > 湿度 * 2` 里那个 `2` 问不到:它不是「土」的一个值,
    把它念成档词就是胡说。链式比较(`0 < 土 < 1`)按相邻两两拆开,各问各的。
    它回一对 `(阈值怎么说, 比较号怎么说)` —— **比较号也归它**,因为换掉数的那一方
    才知道换出来的说法配不配得上原来那个号(见 `speak_expression` 里 `>=` 那条)。

    ⚠️ 比较号在语法树上**没有位置**(`ast.cmpop` 不带 `lineno`),所以它是在两个
    操作数之间那一段里找的 —— 那一段除了空白就只有它,切得干净。

    ⚠️ `ast` 的 `col_offset` 数的是 **UTF-8 字节**,不是字符 —— 这个仓库里的量名
    全是中文,照字符切会切在字节中间。
    """
    lines = expression.source.splitlines(keepends=True) or [""]
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def at(lineno: int, col: int) -> int:
        line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        return starts[lineno - 1] + len(line.encode("utf-8")[:col].decode("utf-8"))

    def span(node: ast.expr) -> tuple[int, int]:
        return (
            at(node.lineno, node.col_offset),
            at(node.end_lineno or node.lineno, node.end_col_offset or node.col_offset),
        )

    edits: list[tuple[int, int, str]] = []
    thresholds: set[int] = set()
    if threshold_text is not None:
        for node in ast.walk(expression._tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            for (left, right), op in zip(zip(operands, operands[1:]), node.ops):
                symbol = _COMPARE_SYMBOLS.get(type(op))
                if symbol is None:
                    continue
                for name_node, value_node in ((left, right), (right, left)):
                    if not isinstance(name_node, ast.Name):
                        continue
                    number = _numeric(value_node)
                    if number is None or id(value_node) in thresholds:
                        continue
                    said = threshold_text(name_node.id, number, symbol)
                    if said is None:
                        continue
                    word, said_symbol = said
                    thresholds.add(id(value_node))
                    edits.append((*span(value_node), word))
                    if said_symbol != symbol:
                        gap = (span(left)[1], span(right)[0])
                        found = expression.source.find(symbol, *gap)
                        if found >= 0:
                            edits.append((found, found + len(symbol), said_symbol))

    if name_text is not None:
        # 🔴 **整段换掉命名空间那一层之后,别再去换它里面那个 `Name`**
        # (3.8.0,2026-08-28 第三波 B4)。`ast.walk` 会**两个都走到**:
        # `me_mana.魔力` 先作为 `Attribute` 换成一句人话,里面的 `Name('me_mana')`
        # 又被换第二次 —— 两处 span 重叠,而下面那趟替换是从后往前盲改的,
        # 于是玩家屏幕上出现「你的mana 5」:`.魔力` 和 `>=` 一起被吃掉了。
        # **一句念不通的拒绝语,和一句错的拒绝语一样贵** —— 他会去找一样
        # 屏幕上根本不存在的东西(这一层的模块说明为同一件事写过两次)。
        covered: set[int] = set()
        for node in ast.walk(expression._tree):
            # 命名空间那一层(`needs.energy`)整段换掉 —— 拿 `Name` 那一支去换的话
            # 只会换掉点号左边的 `needs`,而她读到的是「needs 的 energy 不够」。
            if isinstance(node, ast.Attribute):
                base = node.value
                if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
                    whole = f"{base.value.id}.{base.attr}.{node.attr}"
                elif isinstance(base, ast.Name):
                    whole = f"{base.id}.{node.attr}"
                else:
                    continue
                said = name_text(whole)
                if said:
                    edits.append((*span(node), said))
                    inner = base.value if isinstance(base, ast.Attribute) else base
                    covered.add(id(inner))
                continue
            if isinstance(node, ast.Name):
                if id(node) in covered:
                    continue
                said = name_text(node.id)
                if said:
                    edits.append((*span(node), said))

    out = expression.source
    for start, end, said in sorted(edits, reverse=True):
        out = out[:start] + said + out[end:]
    return out


def _numeric(node: ast.expr) -> float | None:
    """这个节点是不是一个写死的数(带得动一个负号)。是就给出它的值。

    `-0.3` 在语法树上不是常量而是 `UnaryOp(USub, 0.3)` —— 不认这一层的话,
    负阈值(`me_心情 < -0.3`)会原样漏成数字,而它恰恰是最需要翻译的那种。
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _numeric(node.operand)
        if inner is None:
            return None
        return -inner if isinstance(node.op, ast.USub) else inner
    if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
        if isinstance(node.value, (int, float)):
            return float(node.value)
    return None


def _validate(node: ast.AST, source: str, names: set[str], *, dice: bool = False) -> None:
    """逐个节点过白名单。白名单之外的一切在这里就被拒。"""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)):
            return
        raise ExpressionError(f"{source!r} 里有不支持的常量 {node.value!r}")

    if isinstance(node, ast.Attribute):
        # **命名空间:`<插件id>.<事实名>` 就是一个自由变量,不是属性访问**(3.8.0)。
        #
        # 插件系统给这一层出了一道题:事实必须带命名空间(`needs.energy`),否则两个
        # 插件各声明一个「灵力」就会在同一个 hash 里撞车,而撞车的样子是安静的。
        # 而作者写的是一行算术,`ast` 把点号解析成 `Attribute` —— 这个求值器从前
        # 一个字都不认它。
        #
        # **语法由这一层定死,只收一层**(`a.b` 收,`a.b.c` 拒):
        #
        # - 名字就是 `f"{value.id}.{attr}"` 那个**整串**,存储键与它逐字相同
        #   (`stock:{owner}` 那个 hash 里的字段名就叫 `needs.energy`)。
        #   于是规律层、感知层、`stocks.evaluate_due` 的 namespace **一行都不用改** ——
        #   它们本来就是按字符串键查的。
        # - **不是属性访问,也永远不会变成属性访问**:求值器只拿这个串去查
        #   `namespace`,不碰任何 Python 对象的 `__getattr__`。这条是安全边界,
        #   和"绝不 `eval`"同一级 —— 放开成真的属性访问,就等于把宿主进程里的
        #   对象图交给作者写的字符串。
        # - **两层不收**,理由是它没有意义而不是难做:事实只有 `<插件>.<键>` 这一种
        #   形状,`a.b.c` 一定是作者写错了,而放行会让它变成一个永远读不到的名字
        #   (运行期报「读了一个不存在的量」,而作者看不出错在哪一层)。
        if not isinstance(node.ctx, ast.Load):
            raise ExpressionError(f"{source!r} 里不能给名字赋值")
        # 🆕 3.8.0 第 2 期:**两层只在边的三个前缀下面开**。
        #
        # 一条作用在边上的规律读得到三样东西:`edge.<事实>`(边自己的)、
        # `from.<量>` / `to.<量>`(两端节点身上内核的量)—— 这三样都是**一层**。
        # 而两端节点身上**插件的**事实本来就带命名空间(`qi.灵力`),于是它必然是
        # `from.qi.灵力` —— **两层**。
        #
        # 所以开的口子是**有限的**:两层的根只准是 `from` / `to` / `edge` 这三个
        # **内核保留字**(插件 id 拿不到它们,`plugins.RESERVED_IDS` 挡着)。
        # `随便什么.b.c` 照旧当场拒 —— 第 1 期那句理由一个字没变:事实只有
        # `<插件>.<键>` 这一种形状,两层一定是写错了,而放行会让它变成一个永远
        # 读不到的名字。
        base = node.value
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            if base.value.id not in EDGE_PREFIXES:
                raise ExpressionError(
                    f"{source!r}:两层点号只在边的这三个前缀下面开 —— "
                    f"{sorted(EDGE_PREFIXES)}(`src.qi.灵力` 是"
                    "「起点那一端身上、qi 这个插件的灵力」;⚠️ 不是 `from`,"
                    "那是 Python 关键字,连语法都过不去)。别处写成 `a.b.c` 的话"
                    "中间那一层指不到任何东西,而它会安静地变成一个永远读不到的名字"
                )
            if base.attr.startswith("_") or node.attr.startswith("_"):
                raise ExpressionError(
                    f"{source!r}:命名空间和事实名都不许以下划线开头"
                )
            names.add(f"{base.value.id}.{base.attr}.{node.attr}")
            return
        if not isinstance(base, ast.Name):
            raise ExpressionError(
                f"{source!r}:点号只收一层 —— `<插件id>.<事实名>`(比如 "
                "`needs.energy`、`me_needs.energy`);边上多一层前缀"
                f"({sorted(EDGE_PREFIXES)})。别的写法里中间那一层"
                "指不到任何东西,而它会安静地变成一个永远读不到的名字"
            )
        # **函数名当不了命名空间** —— `rand` 单独拦过一次,而 `min.x` / `clamp.x`
        # 那一族当时漏了(2026-08-26 验收 A):同一类只挡了一个。
        # 放行它们的下场不是报错,是一个永远读不到的名字。
        if node.value.id in _FUNCTIONS or node.value.id == DICE_NAME:
            raise ExpressionError(
                f"{source!r}:`{node.value.id}` 是个函数,不是命名空间 —— "
                f"函数名有 {sorted({*_FUNCTIONS, DICE_NAME})},它们都当不了插件 id"
            )
        # 🔴 **下划线开头的一律拒,而这一条是安全边界不是洁癖。**
        # `self.__class__` 在语法树上和 `needs.energy` 是同一种节点 —— 放行它,
        # 这一层看上去就成了"属性访问",而 `tests/test_world_rules.py` 里那条
        # 「设计者写的字符串会被求值」的判据当场变红(2026-08-26 真红过一次,
        # 就是这一行加进来之前)。求值器其实只拿整串去查字典、碰不到任何对象,
        # **但一条读起来像属性访问的语法迟早会被某个人实现成属性访问** ——
        # 而事实名本来也不许以下划线开头,所以这里一个字都不损失。
        if node.attr.startswith("_") or node.value.id.startswith("_"):
            raise ExpressionError(
                f"{source!r}:命名空间和事实名都不许以下划线开头 —— "
                "这一层不是属性访问,`self.__class__` 这种写法在这儿没有意义"
            )
        names.add(f"{node.value.id}.{node.attr}")
        return

    if isinstance(node, ast.Name):
        if not isinstance(node.ctx, ast.Load):
            raise ExpressionError(f"{source!r} 里不能给名字赋值")
        if node.id == DICE_NAME:
            # 光秃秃的 `rand` 在从前只是一个自由变量,于是它要么在运行期报
            # "读了一个不存在的量",要么 —— 更坏 —— 撞上一个真叫这个名字的量。
            # 这个名字现在被骰子占了,当场说清楚。
            raise ExpressionError(
                f"{source!r}:`rand` 是个函数,要写成 `rand()` —— "
                "这个名字被骰子占了,不能当量名"
            )
        names.add(node.id)
        return

    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Pow):
            _check_exponent(node.right, source)
        elif type(node.op) not in _BINARY_OPS:
            raise ExpressionError(f"{source!r} 里有不支持的运算 {type(node.op).__name__}")
        _validate(node.left, source, names, dice=dice)
        _validate(node.right, source, names, dice=dice)
        return

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
            raise ExpressionError(f"{source!r} 里有不支持的一元运算 {type(node.op).__name__}")
        _validate(node.operand, source, names, dice=dice)
        return

    if isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _COMPARE_OPS:
                raise ExpressionError(f"{source!r} 里有不支持的比较 {type(op).__name__}")
        _validate(node.left, source, names, dice=dice)
        for comparator in node.comparators:
            _validate(comparator, source, names, dice=dice)
        return

    if isinstance(node, ast.BoolOp):
        for value in node.values:
            _validate(value, source, names, dice=dice)
        return

    if isinstance(node, ast.IfExp):   # 条件运行:`a if 条件 else b`
        _validate(node.test, source, names, dice=dice)
        _validate(node.body, source, names, dice=dice)
        _validate(node.orelse, source, names, dice=dice)
        return

    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == DICE_NAME:
            if not dice:
                # **这一处没有骰子** —— 加载时就说,别等她真去做那件事时才炸。
                # 骰子要四个坐标(世界/规律/owner/tick)才摇得出可重放的那个数,
                # 而能力那一层还没有自己的坐标(3.6.0 / 2026-08-20 仍然如此;
                # 见 `compile_expression`)。给了它坐标就来改这一条与那句人话。
                raise ExpressionError(
                    f"{source!r}:这里没有骰子 —— `rand()` 只在世界的规律(rules)里"
                    "摇得出来。能力(affordance)的 when / requires / costs / set "
                    "还没有自己的那组坐标,写了它这个世界就不该开机"
                )
            # **一刻只投一次。** 同一条规律、同一个 owner、同一 tick 里的两处
            # `rand()` 是同一个数(骰子由那四个坐标折出来),所以收参数只会让人
            # 以为有第二次投掷。要两个互不相干的数,拆成两条规律 —— 那样两个
            # rule_id 就是两副骰子。
            if node.args or node.keywords:
                raise ExpressionError(
                    f"{source!r}:rand() 不收参数 —— 同一条规律、同一个 owner、"
                    "同一 tick 只投一次骰子(所以同一个表达式里两处 rand() 是同一个数)。"
                    "要两个互不相干的数,拆成两条规律"
                )
            return
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            allowed = ", ".join(sorted({*_FUNCTIONS, DICE_NAME}))
            raise ExpressionError(
                f"{source!r} 里调了不认识的函数;能用的只有:{allowed}"
            )
        if node.keywords:
            raise ExpressionError(f"{source!r}:函数不支持关键字参数")
        for arg in node.args:
            _validate(arg, source, names, dice=dice)
        return

    raise ExpressionError(
        f"{source!r} 里有不支持的写法 {type(node).__name__} —— "
        "只支持四则、比较、与或非、三元和几个数学函数"
    )


def _check_exponent(node: ast.AST, source: str) -> None:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, (int, float)):
        raise ExpressionError(f"{source!r}:乘方的指数必须是写死的数")
    if abs(node.value) > _MAX_EXPONENT:
        raise ExpressionError(f"{source!r}:乘方的指数不许超过 {_MAX_EXPONENT}")


def _evaluate(
    node: ast.AST,
    ns: Mapping[str, Any],
    source: str,
    dice: Callable[[], float] | None = None,
) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Attribute):
        # 命名空间那一层:整串当一个名字查,**不碰任何对象的属性**(见 `_validate`)。
        base = node.value
        if isinstance(base, ast.Attribute):           # 边的两层:from.qi.灵力
            key = f"{base.value.id}.{base.attr}.{node.attr}"   # type: ignore[union-attr]
        else:
            key = f"{base.id}.{node.attr}"            # type: ignore[union-attr]
        if key not in ns:
            raise ExpressionError(f"{source!r} 读了一个不存在的量:{key}")
        return ns[key]

    if isinstance(node, ast.Name):
        if node.id not in ns:
            raise ExpressionError(f"{source!r} 读了一个不存在的量:{node.id}")
        return ns[node.id]

    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, ns, source, dice)
        right = _evaluate(node.right, ns, source, dice)
        if isinstance(node.op, ast.Pow):
            return left ** right
        try:
            return _BINARY_OPS[type(node.op)](left, right)
        except ZeroDivisionError:
            raise ExpressionError(f"{source!r}:除以零") from None
        except TypeError as exc:
            raise ExpressionError(f"{source!r}:{exc}") from None

    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, ns, source, dice)
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.USub):
            return -value
        return not value

    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, ns, source, dice)
        for op, right_node in zip(node.ops, node.comparators):
            right = _evaluate(right_node, ns, source, dice)
            try:
                if not _COMPARE_OPS[type(op)](left, right):
                    return False
            except TypeError as exc:
                raise ExpressionError(f"{source!r}:{exc}") from None
            left = right
        return True

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            for value in node.values:
                if not _evaluate(value, ns, source, dice):
                    return False
            return True
        for value in node.values:
            if _evaluate(value, ns, source, dice):
                return True
        return False

    if isinstance(node, ast.IfExp):
        if _evaluate(node.test, ns, source, dice):
            return _evaluate(node.body, ns, source, dice)
        return _evaluate(node.orelse, ns, source, dice)

    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == DICE_NAME:
            if dice is None:
                # **降级不许无声。** 这里没有骰子只有一个可能:调用方不是"世界的
                # 规律"那条路(能力的 requires/costs、出生自检…),而那几条路上
                # 没有"哪一 tick、哪个 owner"这组坐标可折。给个 0 会让
                # "三成概率下雨"变成"永远不下雨",而且日志干净。
                raise ExpressionError(
                    f"{source!r}:这里没有骰子 —— `rand()` 只在世界的规律(rules)里"
                    "投得动,那儿才有(世界, 规律, owner, tick)这组坐标把它折出来"
                )
            return dice()
        args = [_evaluate(arg, ns, source, dice) for arg in node.args]
        try:
            return _FUNCTIONS[node.func.id](*args)  # type: ignore[union-attr]
        except (TypeError, ValueError) as exc:
            raise ExpressionError(f"{source!r}:{node.func.id} 调用失败 —— {exc}") from None  # type: ignore[union-attr]

    # 校验过的树不该走到这儿 —— 走到了就是校验和求值脱节了。
    raise ExpressionError(f"{source!r}:内部错误,校验放过了 {type(node).__name__}")
