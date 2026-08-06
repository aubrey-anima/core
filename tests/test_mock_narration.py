"""Mock 叙事跟着世界走,不跟着引擎写死的语言走(#9)。

**没有配 key 是默认状态** —— `anima-world start` 明确支持直接回车先用 Mock 试试,
README 展示的也正是那一幕。所以这不是降级路径上的边角料,是第一屏。

从前那一屏是这样的:中文名字、中文时间戳、中文命令行外壳、英文动词,再拼一句
中文的记忆后缀 —— 一行之内三种口径。而"翻成中文"不是答案:一个播种英文世界的
宿主会撞上完全对称的另一半,引擎无从知道自己在跑哪个世界。
"""
from __future__ import annotations

from _worldfile import bundled_seed, open_world_at, run_cli

import json
import subprocess
import sys
from importlib import resources

from anima_world.api import World
from anima_world.narrative import MockNarrativeProvider


class _OneMemory:
    def query(self, agent_id):  # noqa: ARG002
        return [{"summary": "昨天在咖啡店和夏聊过"}]


def test_mock_narration_is_not_a_language_salad():
    """一行之内不许出现两种语言 —— 这条曾经是**每一行**都不满足。"""
    provider = MockNarrativeProvider(memory_store=_OneMemory())
    line = provider.describe({"kind": "idle_wander", "params": {}}, "遥", {})
    assert not any("a" <= ch.lower() <= "z" for ch in line), (
        f"内置模板与内置记忆后缀必须同一种语言:{line!r}"
    )


def test_every_action_kind_the_engine_emits_has_a_template():
    """`eat` 曾经掉进 custom,变成"做了点别的"——需求系统一开就天天出现。"""
    provider = MockNarrativeProvider()
    for kind in ("walk", "chat", "work", "sleep", "eat", "idle_wander", "idle_social"):
        line = provider.describe(
            {"kind": kind, "params": {"location": "cafe", "target": "夏"}}, "遥", {}
        )
        assert line.startswith("遥") and "custom" not in line
        assert "{" not in line, f"{kind} 的模板留了没填上的占位符:{line!r}"


def test_templates_come_from_the_prompt_store_and_are_read_live(tmp_path):
    """模板住在 prompt_store,`prompt_set` 立刻生效 —— 与 llm.* 同一条 live-read 规矩。"""
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        provider = MockNarrativeProvider(prompt_store=world.scheduler.prompt_store)
        assert provider.describe({"kind": "sleep", "params": {}}, "夏", {}) == "夏睡下了"
        world.prompt_set("narrative.mock.sleep", "{agent} 打烊回家了")
        assert provider.describe({"kind": "sleep", "params": {}}, "夏", {}) == "夏 打烊回家了"


def test_a_seed_can_ship_narration_that_sounds_like_its_world(tmp_path):
    """种子决定世界说什么语言,所以模板也归种子 —— 顺带让 Mock 输出可被创作。"""
    seed = bundled_seed()
    seed["mock_narration"] = {
        "sleep": "{agent} turned in for the night",
        "idle_wander": "{agent} drifted about",
        "memory_suffix": " — still thinking about {summary}",
        "在天上飞": "{agent} soared over {location}",   # 引擎没听说过的动作种类
        "broken": "{agent} went to {nowhere}",          # 未知占位符,该被逐条丢弃
    }
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

    with open_world_at(str(tmp_path / "w.db"), seed_path=str(seed_path),
                    force_mock_llm=True) as world:
        provider = MockNarrativeProvider(prompt_store=world.scheduler.prompt_store)
        assert provider.describe({"kind": "sleep", "params": {}}, "Ada", {}) == \
            "Ada turned in for the night"
        # 世界可以自带引擎没听说过的动作种类的说法(节拍脚本的自定义动作)。
        assert provider.describe({"kind": "在天上飞", "params": {"location": "cafe"}},
                                 "Ada", {}) == "Ada soared over cafe"
        # 坏模板逐条丢弃、不拦启动,也不落库。
        names = {row["name"] for row in world.prompt_list()}
        assert "narrative.mock.broken" not in names
        assert provider.describe({"kind": "work", "params": {"location": "cafe"}},
                                 "Ada", {}) == "Ada在cafe忙活", "没覆盖的种类保持默认"


def test_a_failing_llm_falls_back_to_the_worlds_own_templates(tmp_path):
    """真 LLM 挂掉时走的就是 Mock —— 那正是一个英文世界会突然改口说中文的地方。"""
    from anima_world.narrative import OpenAICompatibleNarrativeProvider

    seed = bundled_seed()
    seed["mock_narration"] = {"sleep": "{agent} turned in for the night"}
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

    with open_world_at(str(tmp_path / "w.db"), seed_path=str(seed_path),
                    force_mock_llm=True) as world:
        def _explode(*_args, **_kwargs):
            raise TimeoutError("provider is down")

        provider = OpenAICompatibleNarrativeProvider(
            api_key="sk-test", base_url="https://example.invalid/v1", model="m",
            post_json=_explode, prompt_store=world.scheduler.prompt_store,
        )
        assert provider.describe({"kind": "sleep", "params": {}}, "Ada", {}) == \
            "Ada turned in for the night"


# 被替换掉的英文模板。盯具体措辞而不是"有没有英文字母":地点 id 本来就是英文
# (`走去了cafe` 完全正常),按字母判会把 id 误伤成回归,而且哪些动作在头 80 个
# tick 里发生逐次不同 —— 那种写法会既假绿又假红。
_RETIRED_ENGLISH = (
    "wandered around", "went to sleep", "walked to",
    "chatted with", "worked at", "did something custom",
    "looked for someone to talk to",
)


def test_the_first_screen_speaks_the_worlds_language(tmp_path):
    """端到端:没配 key 跑一段,叙事里不该再冒出英文动词。"""
    from _worldfile import redis_for

    db = tmp_path / "w.db"
    redis_for(db)   # CLI 与 open_world_at 用同一个客户端
    result = run_cli("simulate", "--world-id", "w",
         "--ticks", "300", "--llm", "mock")
    assert result.returncode == 0, result.stderr
    with open_world_at(str(db), force_mock_llm=True) as world:
        lines = [entry["text"] for entry in world.state()["narrative_log"]]
    assert lines, "跑 300 tick 总该有叙事"
    for line in lines:
        for phrase in _RETIRED_ENGLISH:
            assert phrase not in line, f"英文模板又回来了:{line!r}"


def test_no_action_kind_still_renders_an_english_template():
    """逐个种类盯措辞 —— 端到端那条只覆盖当次真的发生了的动作。"""
    provider = MockNarrativeProvider()
    rendered = [
        provider.describe({"kind": kind, "params": {"location": "cafe", "target": "夏"}},
                          "遥", {})
        for kind in ("walk", "chat", "work", "sleep", "eat", "idle_wander",
                     "idle_social", "custom", "从没听说过的动作")
    ]
    for line in rendered:
        for phrase in _RETIRED_ENGLISH:
            assert phrase not in line, f"{line!r} 还在用退役的英文模板"
