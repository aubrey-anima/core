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


def _validate(node: ast.AST, source: str, names: set[str], *, dice: bool = False) -> None:
    """逐个节点过白名单。白名单之外的一切在这里就被拒。"""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)):
            return
        raise ExpressionError(f"{source!r} 里有不支持的常量 {node.value!r}")

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
                # 而能力那一层今天还没有自己的坐标(见 `compile_expression`)。
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
