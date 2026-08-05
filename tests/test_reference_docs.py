"""`docs/REFERENCE.md` 说的话必须是真的。

为什么值得一个测试文件:REFERENCE 是**宿主照着写代码**的那份东西(CLAUDE.md 把它
和 README 一起列为"改架构前先读"),而它和代码之间此前没有任何机械联系。一次人工
对账查出四条:

- `world.declare_visibility(kind, …)` —— 真实形参是 `owner_kind`,照文档写关键字
  参数直接 TypeError(而种子里的字段**确实**叫 `kind`,所以这个错特别容易犯)
- `world.close_conversation(id)` / `conversation_messages(id)` —— 真实是 `conversation_id`
- `world.history` / `fast_forward` / `report` —— 三个真实公开 API,REFERENCE 里
  **零次出现**

一次性改完没有用:下次加个方法照样飘。所以把对账钉成闸门 —— 加公开方法就必须写文档,
改形参名就必须改文档。API 面本来就是"只加不改"的跨仓库契约,这道闸和那条纪律同源。
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from anima_world.api import World

REFERENCE = Path(__file__).resolve().parent.parent / "docs" / "REFERENCE.md"

# 文档里**当成方法调用**写的地方(带括号)。只认带括号的,是因为 `world.db` 是
# 文件名、`world.setting` / `world.minutes_per_tick` 是配置键 —— 它们和 `World`
# 的成员同名同前缀,不加这一条会把配置表整个误报成幽灵方法。
_CALL = re.compile(r"`(?:world|World)\.([a-z_][a-z0-9_]*)\(")
# 覆盖率检查放宽到"被提到过"就算:属性(如 `world.paused`)写不出括号。
_MENTION = re.compile(r"`(?:world|World)\.([a-z_][a-z0-9_]*)`")

# 有意不写进 REFERENCE 的公开名字。**每一条都要有理由** —— 这个清单是给
# "为什么它不在文档里"留的位置,不是垃圾桶。
UNDOCUMENTED_ON_PURPOSE = {
    # 底层对象,REFERENCE 的"持久化与底层"一节整体交代过(绕过它们直写 db 违反纪律 1)
    "scheduler", "chat_store", "chat_state", "chat_service", "players",
    # async 版本:参数与同步版逐字相同,文档在同步版那一行里点名
    "achat", "achat_burst", "achat_reply",
    # 上下文管理器协议
    "open", "close",
}


def _documented_calls() -> set[str]:
    """文档里当成方法调用写的名字 —— 这些必须真的存在、形参必须对得上。"""
    return set(_CALL.findall(REFERENCE.read_text(encoding="utf-8")))


def _documented_any() -> set[str]:
    """被提到过就算写过(属性没有括号)。"""
    text = REFERENCE.read_text(encoding="utf-8")
    return _documented_calls() | set(_MENTION.findall(text))


def _public() -> set[str]:
    return {name for name in dir(World) if not name.startswith("_")}


def test_every_documented_method_exists():
    """文档里写了、World 上没有 —— 宿主照着写会 AttributeError。"""
    ghosts = sorted(_documented_calls() - _public())
    assert not ghosts, (
        f"REFERENCE 提到了不存在的成员:{ghosts}。"
        "要么改文档,要么它是被删掉的 API —— 而 API 面是只加不改的。"
    )


def test_every_public_method_is_documented():
    """加了公开方法就必须写进 REFERENCE。

    API 面是宿主依赖的跨仓库契约。一个没人知道的方法等于没加,而一个**半知道**的
    方法更糟:宿主从 `dir()` 里翻出来用,而它的语义从来没被写下来过。
    """
    missing = sorted(_public() - _documented_any() - UNDOCUMENTED_ON_PURPOSE)
    assert not missing, (
        f"这些公开方法在 REFERENCE 里一次都没出现:{missing}。"
        "写进 §3 的 API 表,或者加进 UNDOCUMENTED_ON_PURPOSE **并说明理由**。"
    )


def test_documented_parameter_names_match_the_real_signature():
    """文档写的形参名必须真的存在。

    `declare_visibility(kind, …)` 就是这么飘的:种子里那个字段叫 `kind`,文档跟着
    写了 `kind`,而形参是 `owner_kind`。位置调用照样能用,所以永远不会有人发现 ——
    直到有人写了关键字参数。
    """
    text = REFERENCE.read_text(encoding="utf-8")
    problems: list[str] = []
    for match in re.finditer(r"`(?:world|World)\.([a-z_][a-z0-9_]*)\(([^`]*)\)`", text):
        name, written = match.group(1), match.group(2).strip()
        if not written or name not in _public():
            continue
        attribute = getattr(World, name)
        try:
            signature = inspect.signature(attribute)
        except (TypeError, ValueError):
            continue  # property 之类,没有签名可比
        real = {p for p in signature.parameters if p != "self"}
        if any(p.kind is p.VAR_KEYWORD for p in signature.parameters.values()):
            continue  # 收 **kwargs 的,写什么都算数
        for chunk in re.split(r",(?![^{\[(]*[}\])])", written):
            chunk = chunk.strip().lstrip("*").split("=")[0].split(":")[0].strip()
            if re.fullmatch(r"[a-z_][a-z0-9_]*", chunk) and chunk not in real:
                problems.append(
                    f"world.{name}:文档写了 `{chunk}`,真实签名是 ({', '.join(sorted(real))})"
                )
    assert not problems, "REFERENCE 的形参名和代码对不上:\n  " + "\n  ".join(problems)


def test_the_contract_tool_catalog_says_which_surface_each_tool_is_on():
    """`contract --json` 的 `chat_tools` **不只有 chat 面**,所以每条得带 `surfaces`。

    名字是历史包袱(运维台镜像已经在读 `chat_tools`,改名等于跨仓库破坏),但内容是
    全目录:`reach_out` 只在定时轮次里出现,聊天里永远调不到。照着字段名做一个
    "聊天能力"列表就会把它也列进去 —— 而那是一个用户永远等不到的按钮。
    """
    import json
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "anima_world", "contract", "--json"],
        capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr
    catalog = json.loads(done.stdout)["chat_tools"]
    assert catalog, "能力目录是空的"
    for entry in catalog:
        assert entry.get("surfaces"), f"{entry['id']} 没说自己在哪个面上"

    by_id = {entry["id"]: entry["surfaces"] for entry in catalog}
    assert "chat" not in by_id["reach_out"], (
        "reach_out 被标成了聊天能力 —— 它只在定时轮次里出现"
    )
    assert "chat" in by_id["walk_away"]


def test_the_contract_storage_section_matches_the_code():
    """跨仓库的存储契约由 `contract --json` 传出去,运维台镜像读的就是它。"""
    import json
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "anima_world", "contract", "--json"],
        capture_output=True, text=True,
    )
    payload = json.loads(done.stdout)["storage"]
    assert payload["backend"] == "redis"
    assert payload["key_prefix"] == "anima:{world_id}:"