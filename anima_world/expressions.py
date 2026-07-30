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
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from typing import Any, Mapping


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


@dataclass(frozen=True)
class Expression:
    """一条解析并校验过的表达式。`names` 是它读到的自由变量。"""

    source: str
    names: frozenset[str]
    _tree: ast.expr

    def evaluate(self, namespace: Mapping[str, Any]) -> Any:
        """按命名空间求值。读不到的名字、除零 → `ExpressionError`。"""
        return _evaluate(self._tree, namespace, self.source)

    def __str__(self) -> str:  # 出错信息里要看得见原文
        return self.source


def compile_expression(source: str) -> Expression:
    """把一行算术编译成可求值的东西。写错了当场抛 `ExpressionError`。"""
    if not isinstance(source, str) or not source.strip():
        raise ExpressionError("表达式是空的")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"{source!r} 语法不对:{exc.msg}") from None

    names: set[str] = set()
    _validate(tree.body, source, names)
    return Expression(source=source, names=frozenset(names), _tree=tree.body)


def _validate(node: ast.AST, source: str, names: set[str]) -> None:
    """逐个节点过白名单。白名单之外的一切在这里就被拒。"""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)):
            return
        raise ExpressionError(f"{source!r} 里有不支持的常量 {node.value!r}")

    if isinstance(node, ast.Name):
        if not isinstance(node.ctx, ast.Load):
            raise ExpressionError(f"{source!r} 里不能给名字赋值")
        names.add(node.id)
        return

    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Pow):
            _check_exponent(node.right, source)
        elif type(node.op) not in _BINARY_OPS:
            raise ExpressionError(f"{source!r} 里有不支持的运算 {type(node.op).__name__}")
        _validate(node.left, source, names)
        _validate(node.right, source, names)
        return

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
            raise ExpressionError(f"{source!r} 里有不支持的一元运算 {type(node.op).__name__}")
        _validate(node.operand, source, names)
        return

    if isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _COMPARE_OPS:
                raise ExpressionError(f"{source!r} 里有不支持的比较 {type(op).__name__}")
        _validate(node.left, source, names)
        for comparator in node.comparators:
            _validate(comparator, source, names)
        return

    if isinstance(node, ast.BoolOp):
        for value in node.values:
            _validate(value, source, names)
        return

    if isinstance(node, ast.IfExp):   # 条件运行:`a if 条件 else b`
        _validate(node.test, source, names)
        _validate(node.body, source, names)
        _validate(node.orelse, source, names)
        return

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            allowed = ", ".join(sorted(_FUNCTIONS))
            raise ExpressionError(
                f"{source!r} 里调了不认识的函数;能用的只有:{allowed}"
            )
        if node.keywords:
            raise ExpressionError(f"{source!r}:函数不支持关键字参数")
        for arg in node.args:
            _validate(arg, source, names)
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


def _evaluate(node: ast.AST, ns: Mapping[str, Any], source: str) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id not in ns:
            raise ExpressionError(f"{source!r} 读了一个不存在的量:{node.id}")
        return ns[node.id]

    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, ns, source)
        right = _evaluate(node.right, ns, source)
        if isinstance(node.op, ast.Pow):
            return left ** right
        try:
            return _BINARY_OPS[type(node.op)](left, right)
        except ZeroDivisionError:
            raise ExpressionError(f"{source!r}:除以零") from None
        except TypeError as exc:
            raise ExpressionError(f"{source!r}:{exc}") from None

    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, ns, source)
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.USub):
            return -value
        return not value

    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, ns, source)
        for op, right_node in zip(node.ops, node.comparators):
            right = _evaluate(right_node, ns, source)
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
                if not _evaluate(value, ns, source):
                    return False
            return True
        for value in node.values:
            if _evaluate(value, ns, source):
                return True
        return False

    if isinstance(node, ast.IfExp):
        if _evaluate(node.test, ns, source):
            return _evaluate(node.body, ns, source)
        return _evaluate(node.orelse, ns, source)

    if isinstance(node, ast.Call):
        args = [_evaluate(arg, ns, source) for arg in node.args]
        try:
            return _FUNCTIONS[node.func.id](*args)  # type: ignore[union-attr]
        except (TypeError, ValueError) as exc:
            raise ExpressionError(f"{source!r}:{node.func.id} 调用失败 —— {exc}") from None  # type: ignore[union-attr]

    # 校验过的树不该走到这儿 —— 走到了就是校验和求值脱节了。
    raise ExpressionError(f"{source!r}:内部错误,校验放过了 {type(node).__name__}")
