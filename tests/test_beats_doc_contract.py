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


def _table(start_marker: str, end_marker: str) -> dict[str, list[str]]:
    """§9 里一张 `| \\`名字\\` | 必填字段 | … |` 表读出来的映射。"""
    text = _REFERENCE.read_text(encoding="utf-8")
    start = text.index(start_marker)
    end = text.index(end_marker, start)
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


def _documented_ops() -> dict[str, list[str]]:
    return _table("**Op 清单**", "**谓词清单**")


def _documented_predicates() -> dict[str, list[str]]:
    return _table("**谓词清单**", "**校验语义**")


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


def test_every_predicate_is_documented_with_the_fields_the_validator_wants():
    """谓词表同样是机器校验的 —— 加载期严格对谓词一视同仁。"""
    from anima_world.beats import PREDICATE_REQUIRED_FIELDS, _VALID_PREDICATES

    documented = _documented_predicates()
    assert set(documented) == set(_VALID_PREDICATES), (
        f"文档多出:{sorted(set(documented) - set(_VALID_PREDICATES))};"
        f"文档缺少:{sorted(set(_VALID_PREDICATES) - set(documented))}"
    )
    mismatched = {
        pred: {"文档": documented[pred], "代码": sorted(fields)}
        for pred, fields in PREDICATE_REQUIRED_FIELDS.items()
        if documented.get(pred) != sorted(fields)
    }
    assert not mismatched, f"字段对不上:{mismatched}"


# ── §9.1 的两张收拒表(3.9.0)────────────────────────────────────────────────
#
# 上面那两张清单有闸,而 3.9.0 新加的这两张一格都没扫过(验收 B 逮的)。
# 理由和上面逐字相同:**加载期严格的全部价值,押在"作者能知道什么是对的"上面**,
# 而 `player` 这一族拒得比谁都硬(写错一格当场开不了机)。表错了就是刁难。

_PLAYER_ROW = re.compile(r"^\|\s*`(?P<name>[a-z_]+)`\s*\|(?P<fields>[^|]*)\|")


def _player_table(start_marker: str, end_marker: str) -> dict[str, list[str]]:
    """一行一个名字的收拒表;`—` / 空 = 一格都写不下(**不进映射**,和代码同构)。"""
    text = _REFERENCE.read_text(encoding="utf-8")
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    table: dict[str, list[str]] = {}
    for line in text[start:end].splitlines():
        match = _PLAYER_ROW.match(line.strip())
        if not match:
            continue
        fields = [
            f.strip().strip("`")
            for f in re.split(r"[,、/]", match.group("fields"))
            if f.strip() and f.strip() not in ("—", "-")
        ]
        if fields:
            table[match.group("name")] = sorted(fields)
    return table


def test_那两张收拒表真的被解析到了():
    """解析器坏掉的话,下面两条会变成永远绿的空断言 —— 上面那条同款。"""
    assert _player_table("**op 的收拒表**", "**谓词的收拒表**"), "op 那张一行都没读出来"
    assert _player_table("**谓词的收拒表**", "⚠️ **这两张表是机器校验的"), "谓词那张一行都没读出来"


def test_op收拒表和校验器一致():
    from anima_world.beats import PLAYER_ALLOWED_OP_FIELDS, VALID_OPS

    documented = _player_table("**op 的收拒表**", "**谓词的收拒表**")
    code = {op: sorted(fields) for op, fields in PLAYER_ALLOWED_OP_FIELDS.items()}
    assert documented == code, f"文档 {documented} ≠ 代码 {code}"
    # **拒掉的那些也要逐个写在表上** —— 少写一行,作者只会以为"文档没提 = 大概能写"。
    listed = set(_all_names("**op 的收拒表**", "**谓词的收拒表**"))
    assert listed == set(VALID_OPS), (
        f"表上多出:{sorted(listed - set(VALID_OPS))};缺:{sorted(set(VALID_OPS) - listed)}"
    )


def test_谓词收拒表和校验器一致():
    from anima_world.beats import PLAYER_ALLOWED_PREDICATE_FIELDS, _VALID_PREDICATES

    end = "⚠️ **这两张表是机器校验的"
    documented = _player_table("**谓词的收拒表**", end)
    code = {p: sorted(f) for p, f in PLAYER_ALLOWED_PREDICATE_FIELDS.items()}
    assert documented == code, f"文档 {documented} ≠ 代码 {code}"
    listed = set(_all_names("**谓词的收拒表**", end))
    assert listed == set(_VALID_PREDICATES), (
        f"表上多出:{sorted(listed - set(_VALID_PREDICATES))};"
        f"缺:{sorted(set(_VALID_PREDICATES) - listed)}"
    )


def _all_names(start_marker: str, end_marker: str) -> list[str]:
    """表上出现过的每一个名字 —— **含拒掉的那些**(它们不进上面那个映射)。"""
    text = _REFERENCE.read_text(encoding="utf-8")
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return [
        m.group("name")
        for line in text[start:end].splitlines()
        if (m := _PLAYER_ROW.match(line.strip()))
    ]
