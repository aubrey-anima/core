"""`anima-world validate seed|beats` —— 不建世界就检查作者写的东西。

CLAUDE.md 写着"创作台经 CLI 委托校验",而这个入口一直不存在:作者唯一的检查办法是
真开一次世界,而**种子只读进空库一次**,试错的代价是重建世界。

这里最要紧的一条语义是:**提醒不算失败**。引用完整性只能是 advisory —— 一个 beat
完全可以先 `agent_join` 一个新角色、后面的 beat 再对他做事,那时种子里当然没有他。
把它升级成拒绝,就是把"照跑但给错东西"换成"本来能跑却不让跑",而后者更糟:一个
设计正确的世界会在一次小版本升级之后开不了机。
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from _worldfile import write_seed_file


def _validate(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "anima_world", "validate", *args],
        capture_output=True, text=True,
    )


def _write(path, data) -> str:
    """节拍脚本仍是 JSON;世界(`.cyberworld`)走世界文件格式。"""
    if str(path).endswith(".cyberworld"):
        return write_seed_file(path, data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


_GOOD_SEED = {
    "agents": [{"id": "夏", "name": "苏晚夏", "location": "cafe", "personality": "开朗"}],
    "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
}


def test_a_good_seed_passes(tmp_path):
    result = _validate("world", _write(tmp_path / "s.cyberworld", _GOOD_SEED), "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["errors"] == [] and payload["warnings"] == []


def test_a_seed_missing_required_fields_is_refused(tmp_path):
    bad = {"agents": [{"id": "夏"}], "locations": []}
    result = _validate("world", _write(tmp_path / "s.cyberworld", bad), "--json")
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["valid"] is False and payload["errors"]


def test_an_agent_standing_somewhere_undefined_is_a_warning_not_an_error(tmp_path):
    """地点表只从种子的 locations 播种,不会因为被引用就自动补 —— 但这不该拦住开机。"""
    seed = json.loads(json.dumps(_GOOD_SEED))
    seed["agents"][0]["location"] = "打错的地名"
    result = _validate("world", _write(tmp_path / "s.cyberworld", seed), "--json")

    assert result.returncode == 0, "引用问题只提醒,不算失败"
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert any("打错的地名" in w for w in payload["warnings"])


def test_a_duplicate_id_is_a_warning(tmp_path):
    seed = json.loads(json.dumps(_GOOD_SEED))
    seed["agents"].append(dict(seed["agents"][0], name="另一个人"))
    result = _validate("world", _write(tmp_path / "s.cyberworld", seed), "--json")
    payload = json.loads(result.stdout)
    assert any("不止一次" in w for w in payload["warnings"])


def test_a_stock_row_the_loader_would_drop_is_an_error_not_a_warning(tmp_path):
    """引用完整性只提醒,**形状读不懂是硬错误** —— 这两类要分得开。

    提醒说的是"你八成写错了",而引擎没有合法值全集,所以拒绝它会让设计正确的世界
    在小版本升级后开不了机。这一条说的是"你写的这行没有人读":装载器只认
    `{"owner","values"}`,逐条的 `{"owner","key","value"}` 整条丢掉 —— 在任何引擎
    版本上都一样,而作者到发现那个量三个月没动过才知道。灯塔湾丢了 11 行。
    """
    seed = json.loads(json.dumps(_GOOD_SEED))
    seed["stocks"] = [{"owner": "agent:夏", "key": "initiative", "value": 1.5}]
    result = _validate("world", _write(tmp_path / "s.cyberworld", seed), "--json")

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    # 报的是**该怎么写**,不是一句"不合法" —— 作者看不出自己这行错在哪儿。
    assert any("values" in e for e in payload["errors"]), payload["errors"]


def test_a_stock_value_that_is_not_a_number_is_refused_too(tmp_path):
    """和装载器同一个判据(`float(raw)`)。两处各写一份,迟早表现成"预检说没问题,
    开机还是失败"。"""
    seed = json.loads(json.dumps(_GOOD_SEED))
    seed["stocks"] = [{"owner": "agent:夏", "values": {"initiative": "很高"}}]
    result = _validate("world", _write(tmp_path / "s.cyberworld", seed), "--json")
    assert result.returncode == 2
    assert any("不是一个数" in e for e in json.loads(result.stdout)["errors"])


def test_a_well_formed_stocks_section_still_passes(tmp_path):
    seed = json.loads(json.dumps(_GOOD_SEED))
    seed["stocks"] = [{"owner": "agent:夏", "values": {"initiative": 1.5}}]
    result = _validate("world", _write(tmp_path / "s.cyberworld", seed), "--json")
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["errors"] == []


def test_a_broken_beat_script_is_refused_with_every_error_at_once(tmp_path):
    """加载期严格 —— 而且一次列全,不是修一个报一个。"""
    bad = {"beats": [{"id": "a", "trigger": {"at": {"day": 0}},
                      "payload": [{"op": "不存在的op"}]}]}
    result = _validate("beats", _write(tmp_path / "b.json", bad), "--json")
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["errors"]


def test_a_beat_referencing_a_stranger_is_a_warning_when_a_seed_is_given(tmp_path):
    """引用错一个 id,那个 beat 会静默作废并被永久标记已触发 —— 沉默不行。"""
    script = {"beats": [{
        "id": "开场", "trigger": {"at": {"day": 0, "minute_of_day": 60}},
        "payload": [{"op": "memory", "agent_id": "没有这个人", "summary": "……"}],
    }]}
    result = _validate(
        "beats", _write(tmp_path / "b.json", script),
        "--world-file", _write(tmp_path / "s.cyberworld", _GOOD_SEED), "--json",
    )
    assert result.returncode == 0, "提醒不算失败"
    payload = json.loads(result.stdout)
    assert any("没有这个人" in w for w in payload["warnings"])


def test_an_agent_the_script_brings_in_itself_is_not_reported_as_missing(tmp_path):
    """先 agent_join 再使用是合法写法,不该误报。"""
    script = {"beats": [
        {"id": "入场", "trigger": {"at": {"day": 0, "minute_of_day": 60}},
         "payload": [{"op": "agent_join", "agent": {
             "id": "新人", "name": "新人", "location": "cafe", "personality": "沉默"}}]},
        {"id": "之后", "trigger": {"at": {"day": 1, "minute_of_day": 60}},
         "payload": [{"op": "memory", "agent_id": "新人", "summary": "第一天"}]},
    ]}
    result = _validate(
        "beats", _write(tmp_path / "b.json", script),
        "--world-file", _write(tmp_path / "s.cyberworld", _GOOD_SEED), "--json",
    )
    payload = json.loads(result.stdout)
    assert not any("新人" in w for w in payload["warnings"]), payload["warnings"]


def test_validating_beats_without_a_seed_says_what_it_could_not_check(tmp_path):
    script = {"beats": [{"id": "a", "trigger": {"at": {"day": 0, "minute_of_day": 60}},
                         "payload": [{"op": "memory", "agent_id": "谁", "summary": "…"}]}]}
    result = _validate("beats", _write(tmp_path / "b.json", script), "--json")
    payload = json.loads(result.stdout)
    assert any("--world-file" in w for w in payload["warnings"])


def test_a_missing_file_is_an_error_not_a_traceback(tmp_path):
    result = _validate("world", str(tmp_path / "nope.json"), "--json")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert json.loads(result.stdout)["errors"]
