"""REFERENCE §9 的 op 表必须和校验器一致。

节拍脚本是**加载期严格**的:坏脚本当场列出全部错误、拒绝启动。这条纪律的价值全押
在"作者能知道什么是对的"上面 —— 而作者唯一的依据就是 §9 那张表。表错了,严格校验
就从护栏变成了刁难:照文档写,一个字没打错,世界开不了机。

所以这张表是**机器校验的**,不是散文。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from anima_world.beats import OP_REQUIRED_FIELDS, VALID_OPS

_REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "REFERENCE.md"
_ROW = re.compile(r"^\|\s*`(?P<op>[a-z_]+)`\s*\|(?P<fields>[^|]*)\|")


def _documented_ops() -> dict[str, list[str]]:
    """§9 「Op 清单」那张表读出来的 op → 必填字段。"""
    text = _REFERENCE.read_text(encoding="utf-8")
    start = text.index("**Op 清单**")
    end = text.index("**校验语义**", start)
    table: dict[str, list[str]] = {}
    for line in text[start:end].splitlines():
        match = _ROW.match(line.strip())
        if not match:
            continue
        fields = [
            f.strip().strip("`")
            for f in re.split(r"[,、]", match.group("fields"))
            if f.strip() and f.strip() != "—"
        ]
        table[match.group("op")] = sorted(fields)
    return table


def test_the_reference_table_was_parsed_at_all():
    """解析器坏掉的话,下面两条会变成永远绿的空断言。"""
    documented = _documented_ops()
    assert len(documented) >= len(VALID_OPS), f"只解析出 {sorted(documented)}"


def test_every_op_is_documented_and_no_ghosts_are():
    documented = _documented_ops()
    assert set(documented) == set(VALID_OPS), (
        f"文档多出:{sorted(set(documented) - set(VALID_OPS))};"
        f"文档缺少:{sorted(set(VALID_OPS) - set(documented))}"
    )


def test_the_documented_required_fields_are_the_ones_the_validator_wants():
    """照文档写的脚本必须能过校验 —— 这是加载期严格唯一说得通的前提。"""
    documented = _documented_ops()
    mismatched = {
        op: {"文档": documented[op], "代码": sorted(fields)}
        for op, fields in OP_REQUIRED_FIELDS.items()
        if documented.get(op) != sorted(fields)
    }
    assert not mismatched, f"字段对不上:{mismatched}"
