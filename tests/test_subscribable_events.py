"""插件订得到的那张事件表 —— **策展的,不是全集**(3.8.0,设计-插件系统 §2/§6)。

这个文件守两条,而它们是相反方向的两条:

1. **表上的每一条都真有人在发。** 一张列着不存在的事件的公开契约,会让第一个照它
   写触发器的人以为自己的插件坏了 —— 而没有任何一处会报错,他只是永远等不到那条事件。
   设计稿第 13 条自己写着「事件白名单我一条都没核,`law.wanted` 是编的」,这条测试
   就是那句话的对面。
2. **表是策展的,内部事件不许混进来。** `subsystem_health` / `memory_seed` / `plan` /
   `legacy_seq_gap` 的载荷形状为引擎自己的用途服务,明天就可能因为一次内部重构而变。
   🔴 **进了这张表就是一句公开契约,拿不掉** —— 加一条是加法(便宜),删一条是破坏
   消费方(和改线格式同级)。

**判据用 `ast` 而不是 `git grep`,理由是这个仓库栽过的那一条**:grep 分不清一行代码
和一句注释。`entity_spawn` 在 `world_file.py` 与 `world_package.py` 的文档里各出现过
一次(讲的是 `zcat | grep` 那个例子),grep 数出来是三处而真发它的只有一处;反过来,
一个只活在注释里的 type 会被 grep 判成"真有人在发"。`ast` 只收**真的 dict 字面量**里
那一格 `"type"`,注释和 docstring 自然落不进来。
"""
from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys

from anima_world.events import SUBSCRIBABLE_EVENTS

_PKG = pathlib.Path(__file__).resolve().parent.parent / "anima_world"

#: 明确不许进这张表的那几个 —— 内部管道。列出来而不是靠"我记得没加",
#: 是因为下一个人加一条只需要一行,而这条断言会当场拦住它并问一句为什么。
_INTERNAL = (
    "subsystem_health", "memory_seed", "plan", "legacy_seq_gap",
    "capability_registered", "player_erased",
    # 这几个根本不是事件,是 Redis 键的类型描述(`migrate_v1` / 工具的参数 schema)
    "hash", "set", "string", "number", "array", "batch",
)


def _emitted_type_literals() -> dict[str, list[str]]:
    """全仓库真的写在 dict 字面量里的那些 `"type": "…"`,连出处一起。"""
    found: dict[str, list[str]] = {}
    for path in sorted(_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "type"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    found.setdefault(value.value, []).append(
                        f"{path.name}:{node.lineno}")
    return found


def test_表上每一条都真有人在发():
    emitted = _emitted_type_literals()
    missing = [t for t in SUBSCRIBABLE_EVENTS if t not in emitted]
    assert not missing, (
        f"这几个 type 在契约里公布了,而全仓库没有一处真的发它:{missing} —— "
        "照它写触发器的人会永远等不到那条事件,而一处不报错"
    )


def test_这条判据自己有牙():
    """**一个只会说"都在"的检查器不算检查器。** 塞一个编出来的 type 进去,它要红。"""
    emitted = _emitted_type_literals()
    assert "law_wanted" not in emitted, "夹具前提:这是设计稿里编的那个名字"


def test_内部事件不许混进来():
    leaked = [t for t in _INTERNAL if t in SUBSCRIBABLE_EVENTS]
    assert not leaked, (
        f"内部事件混进了公开契约:{leaked} —— 它们的载荷形状为引擎自己的用途服务,"
        "明天就可能因为一次内部重构而变,而这张表进了就拿不掉"
    )


def test_宁少勿多_第一版不超过十二条():
    assert len(SUBSCRIBABLE_EVENTS) <= 12, (
        f"第一版建议 ≤ 12 条,现在 {len(SUBSCRIBABLE_EVENTS)} 条 —— "
        "加一条是加法(便宜),删一条是破坏消费方(和改线格式同级)"
    )


def test_每条都说得出数字格与当事人格():
    """**空列表和缺席是两件事。** 空 = "这类事情本身不带数"(一个人走进这个世界,
    没有一个数可读);缺席 = 写这张表的人没想过这个问题。"""
    for name, spec in SUBSCRIBABLE_EVENTS.items():
        assert set(spec) >= {"gloss", "numbers", "parties"}, name
        assert isinstance(spec["numbers"], list), name
        assert isinstance(spec["parties"], list), name
        assert spec["gloss"], name


def test_顶层的location_join不在表上():
    """**这个名字底下有两件事,别订错。** 顶层那条 `location_join` 是创世时播下的
    一个**地点**(配置,不是发生的事);"有人走进了一个地方"是
    `state_change{kind: "location_join"}` —— 后者在表上(`state_change`),前者不在。"""
    assert "location_join" not in SUBSCRIBABLE_EVENTS


def test_关系四轴不作为一张可写的表出现():
    """老板 2026-08-26 拍的 D40 ③:插件读得到、emit 得出,**写不进**内置四轴。
    所以四轴的变化只以 `state_change{kind:"sentiment_delta"}` 这一种**事件**形式进来。"""
    assert "state_change" in SUBSCRIBABLE_EVENTS
    note = str(SUBSCRIBABLE_EVENTS["state_change"]["note"])
    assert "sentiment_delta" in note and "投影" in note, (
        "订 `state_change` 的人必须在这一格上读到「四轴是投影,写不进」"
    )
    # 没有第二条以四轴为名的入口 —— 有的话就是给了它一张可以直接写的表。
    assert not [t for t in SUBSCRIBABLE_EVENTS if "sentiment" in t or "relation" in t]


def test_契约照原样报出来():
    done = subprocess.run(
        [sys.executable, "-m", "anima_world", "contract", "--json"],
        capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    section = payload["plugins"]["subscribable_events"]
    assert set(section) == set(SUBSCRIBABLE_EVENTS)
    for name, spec in SUBSCRIBABLE_EVENTS.items():
        assert section[name] == spec, name
    # **这一期 `plugins` 段只有这一格** —— 别的等第 1 期。
    assert list(payload["plugins"]) == ["subscribable_events"]


def test_人看的那一份也印得出():
    done = subprocess.run(
        [sys.executable, "-m", "anima_world", "contract"],
        capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr
    assert "可订事件" in done.stdout
    assert "策展表不是全集" in done.stdout, "没把「这不是全集」印给人看"
