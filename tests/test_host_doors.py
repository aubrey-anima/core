"""宿主自己驱动世界时够得到的门。

`anima-world simulate` 一直能快进 + 出摘要,但那是**命令行**能做的事。一个 import
本包的宿主(网站后端)只有 `tick(n)`:它拿不到"规划有没有跟上"这个判断,也拿不到
运行摘要 —— 除非把世界关掉、再起一个子进程。

这里守两条:
1. `fast_forward` 返回的不是一个 int。一个安静的世界和一个规划全程没跟上的世界,
   产物看起来一模一样,只有 `planner_gave_up` 能把它们分开。
2. 快进的等规划纪律只有一份实现 —— CLI 和门面共用,不会慢慢长出两种行为。
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from anima_world.api import World
from anima_world.sim_report import REPORT_FORMAT_VERSION


def test_fast_forward_advances_the_clock_and_says_how_it_went(tmp_path):
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        before = world.scheduler.clock
        outcome = world.fast_forward(300)

        assert outcome["ticks"] == 300
        assert outcome["clock"] == before + 300 == world.scheduler.clock
        assert outcome["planner_gave_up"] is False, "Mock 档位的规划是即时的,不该被判死"
        assert outcome["exhausted_days"] == 0


def test_fast_forward_can_be_told_not_to_wait_at_all(tmp_path):
    """`plan_wait_cap<=0` 是显式的"不等",不是"planner 死了"。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        outcome = world.fast_forward(100, plan_wait_cap=0)
        assert outcome["planner_gave_up"] is False
        assert outcome["exhausted_days"] == 0


def test_report_reads_the_world_the_host_just_ran(tmp_path):
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.fast_forward(288 * 2)
        report = world.report()

        assert report["report_format_version"] == REPORT_FORMAT_VERSION
        assert report["world"]["ticks"] == world.scheduler.clock
        assert {a["id"] for a in report["agents"]} == set(world.scheduler.agents)
        assert report["events"]["total"] > 0
        # 1.1.1 的时基防护也必须在这条路径上生效
        assert report["world"]["days"] < 10


def test_report_covers_a_player_conversation_without_exploding(tmp_path):
    """聊过天的世界:墙钟事件不该把摘要撑爆,也不该被吞掉。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.fast_forward(50)
        reply = world.chat_reply("夏", [{"role": "user", "content": "在吗"}],
                                 player_id="p1", display_name="阿檀")
        world.record_chat_turn("夏", "p1", [
            {"role": "user", "content": "在吗"},
            {"role": "assistant", "content": reply},
        ])
        report = world.report()
        assert report["events"]["wall_clock_events"] >= 1
        assert report["world"]["days"] < 10


def test_the_cli_and_the_facade_share_one_fast_forward(tmp_path):
    """两条快进路径必须是同一份实现 —— 否则它们会慢慢长出不同的行为。"""
    db = tmp_path / "cli.db"
    result = subprocess.run(
        [sys.executable, "-m", "anima_world", "simulate", "--db-path", str(db),
         "--ticks", "120", "--llm", "mock", "--report", str(tmp_path / "r.json")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    cli_report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))

    with World.open(str(tmp_path / "api.db"), force_mock_llm=True) as world:
        world.fast_forward(120)
        api_report = world.report()

    assert api_report["report_format_version"] == cli_report["report_format_version"]
    assert set(api_report["events"]) == set(cli_report["events"])
    assert set(api_report["world"]) == set(cli_report["world"])
