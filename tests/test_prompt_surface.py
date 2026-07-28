"""世界作者够得到哪些提示词。

`prompt_store` 的 `_DEFAULTS` 就是这个面:`World.prompt_list()` 列的是它,种子化写进
库的是它,`check_renders` 用 `_SAMPLE_VARS` 校验的也是它。**一个调用点读了某个名字、
而这个名字没注册进 `_DEFAULTS`,后果是无声的**:模板照样生效(读的是同一个 store),
但作者在 `prompt_list()` 里看不见它存在,于是那段文字永远改不掉 —— 除非他去读源码。

`chat.response_format` 就是这么漏的:一段写死的中文全角括号排版规则,每次聊天都注入
系统提示,一个英文世界或一个不想要动作描写的世界完全没有办法关掉它。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from anima_world import prompt_store as ps
from anima_world.api import World

_CHAT_SERVICE = Path(__file__).resolve().parents[1] / "anima_world" / "chat_service.py"


def _template_names_read_by(source: Path) -> set[str]:
    """源码里 `self._template("x", …)` / `self._prompt_store.get("x", …)` 读的名字。

    只认这两个入口 —— `_config_store.get("chat.recall_k")` 长得一模一样但读的是
    配置表,把它算成提示词会让这条测试要求注册一个不存在的模板。
    """
    text = source.read_text(encoding="utf-8")
    return set(
        re.findall(r'(?:_template|_prompt_store\.get)\(\s*"([a-z_]+\.[a-z_.]+)"', text)
    )


def test_every_prompt_chat_reads_is_registered_as_an_authorable_default():
    """防漂移:聊天读到的每个提示词名字都必须在作者够得到的面上。"""
    read = _template_names_read_by(_CHAT_SERVICE)
    assert read, "正则没匹配到任何模板名 —— 这条测试失去了意义,先修正则"
    missing = sorted(name for name in read if name not in ps._DEFAULTS)
    assert not missing, f"chat_service 读了但没注册进 _DEFAULTS:{missing}"


def test_every_registered_default_has_sample_vars_or_needs_none():
    """注册了却没有样本变量的模板,`check_renders` 校验不到 —— 存进去才发现炸。"""
    for name, (template, _) in ps._DEFAULTS.items():
        placeholders = set(re.findall(r"\{(\w+)\}", template))
        if not placeholders:
            continue
        sample = ps._sample_for(name)
        assert placeholders <= set(sample), (
            f"{name} 用了 {sorted(placeholders)},样本变量只有 {sorted(sample)}"
        )


def test_the_reply_format_rules_are_authorable(tmp_path):
    """世界作者能把那段排版规则整个换掉,并且立刻生效。"""
    captured: dict[str, str] = {}

    class _Spy:
        async def stream(self, messages):
            captured["system"] = "\n".join(
                m["content"] for m in messages if m["role"] == "system"
            )
            yield "……"

        async def complete(self, messages):
            captured["system"] = "\n".join(
                m["content"] for m in messages if m["role"] == "system"
            )
            return "……"

    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        assert "chat.response_format" in {p["name"] for p in world.prompt_list()}, (
            "作者在 prompt_list() 里看不见它,就等于这段规则不存在"
        )

        world.prompt_set("chat.response_format", "Reply in English. No stage directions.")
        world.chat_service._llm = _Spy()
        agent_id = next(iter(world.scheduler.agents))
        world.chat_reply(agent_id, [{"role": "user", "content": "hi"}], player_id="p1")

        assert "Reply in English" in captured["system"]
        assert "中文全角括号" not in captured["system"], "换掉之后旧规则不该还在"
