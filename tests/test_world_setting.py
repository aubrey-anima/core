"""世界观从世界文件走到她收到的提示词里 —— 整条通道。

**这条路上一整版是断的,而且是静默的。** 线格式把 `world_setting` 归进"对象型"
(body 必须是 dict),播种函数 `__main__._seed_world_setting` 只认字符串:任何
`.cyberworld` 写下的世界观都送不进世界,世界照样建得起来、角色照样说话,只是
每个自定义世界都跑在引擎写死的那份默认世界观下。`demo.cyberworld` 里没有这条记录,
也没有任何测试碰过它 —— **零覆盖正是它能安静烂掉的原因**,所以这份文件盯的是
两头对不对得上,而不是某一头写得对不对。

盯的是**这段话到底有没有进她的提示词**,不是"某个函数被调用了":中间任何一环
改形状,这条都会红。
"""
from __future__ import annotations

import pytest

from anima_world.world_file import WorldFileError, author_records_to_seed

_SETTING = "旧港区,常年下雨。港务局说了算,夜里没人提三号码头。"

_BASE = [
    {"kind": "manifest", "version": 3, "world_id": "harbor", "name": "旧港区"},
    {"kind": "author", "type": "location",
     "body": {"id": "cafe", "name": "咖啡店", "description": "拐角那间"}},
    {"kind": "author", "type": "agent",
     "body": {"id": "夏", "name": "苏晚夏", "location": "cafe", "personality": "话少"}},
]


def _write(path, *extra) -> str:
    """一份手写的世界(裸 JSONL,不 gzip)—— 创作台产出的就是这个形状。"""
    import json

    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [*_BASE, *extra]),
        encoding="utf-8",
    )
    return str(path)


def _world_file(tmp_path, setting=_SETTING, name: str = "harbor.cyberworld") -> str:
    return _write(tmp_path / name,
                  {"kind": "author", "type": "world_setting", "body": setting})


def test_世界文件的世界观进得了她的提示词(open_world, tmp_path):
    """端到端:文件 → `:prompts` 的 `world.setting` → `ChatService.prompt_blocks`。

    最后一段是关键。只验"库里存下了"验不出这条通道有没有接上 —— 而她读不到的
    世界观和没有世界观是同一件事。
    """
    world = open_world(world_file=_world_file(tmp_path))

    assert world.scheduler.prompt_store.get("world.setting") == _SETTING

    seen = world.debug_prompt("夏")
    (block,) = [b for b in seen["blocks"] if b["label"] == "world.setting"]
    assert block["text"] == _SETTING
    assert _SETTING in seen["system"]


def test_没写世界观的世界照旧用内置那份(open_world, tmp_path):
    """**声明本身就是开关**:不写这条记录的世界行为逐位如旧,而不是变成空世界观。"""
    world = open_world(world_file=_write(tmp_path / "bare.cyberworld"))
    default = world.scheduler.prompt_store.get("world.setting")
    assert default and default.strip(), "内置世界观不该因为文件没写而变成空的"


def test_热改留得住重启不拿文件盖回去(open_world, fresh_redis, tmp_path):
    """M5 规矩(和 llm.* 同一条):文件只在**首启**说一次话,之后 `:prompts`
    那一行是运行期权威。反过来的话,一次重启会悄悄改掉一个跑了半年的世界。"""
    path = _world_file(tmp_path)
    world = open_world("w", redis=fresh_redis, world_file=path)
    world.prompt_set("world.setting", "港务局倒了,现在没人说了算。")
    world.close()

    again = open_world("w", redis=fresh_redis, world_file=path)
    assert again.scheduler.prompt_store.get("world.setting") == "港务局倒了,现在没人说了算。"


def test_世界观写成对象的世界当场开不了机(open_world, tmp_path):
    """作者指名的文件坏了**不许降级** —— 世界文件只会被读进空库一次,静默换成
    内置世界观是不可挽回的。而"写成 `{"text": …}`"正是下游最可能写错的那一种。
    """
    path = _world_file(tmp_path, {"text": _SETTING}, name="bad.cyberworld")
    with pytest.raises(WorldFileError) as excinfo:
        open_world(world_file=path)
    assert "world_setting" in str(excinfo.value)


def test_内置演示世界仍然不带世界观():
    """橱窗里没有这条记录 —— 那正是这个洞一直没暴露的原因。哪天它进了橱窗,
    这条会红,顺手提醒改它的人:上面几条测试才是通道的闸。"""
    from _worldfile import bundled_seed

    assert "world_setting" not in bundled_seed()


def test_一条空白的世界观是错的不是无声的():
    """空字符串和"没写"在文件里长得几乎一样,而它们的意思完全不同。"""
    with pytest.raises(WorldFileError):
        author_records_to_seed([{"kind": "author", "type": "world_setting", "body": ""}])
