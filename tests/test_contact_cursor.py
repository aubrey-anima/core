"""**一个人的收件箱不该被别人的事件饿死。**

`contact_requests()` / `inbox()` 都是"取一页 → 按 player_id 过滤 → 只把过滤后的
list 交出去"。`history()` 算出来的 `next_seq` 在函数里被丢掉了,于是宿主只剩一个办法
推游标:拿**最后一条的 seq**。而在一个热闹的世界里,一整窗都可能是别人的事件 ——
这个人拿到空页 → 没有"最后一条" → 游标一步都推不动 → 他自己那条「她想你了」
就停在窗外,**永远**。世界照跑,日志干净。

运维台已经自己绕过去了(空页时再查一次 `history(kind=)` 拿 `next_seq`),但那是
每个宿主都得重新发明一遍的东西,而且要多打一次库、多扫一次全表。根因在引擎。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def busy(open_world):
    """一个热闹的世界:p_noise 收到一堆敲门,p_quiet 的那条排在最后。"""
    world = open_world("world")
    for i in range(12):
        world._record_and_fan({
            "type": "agent_hail", "who": "夏", "loc": "cafe",
            "payload": {"player_id": "p_noise", "text": f"吵闹 {i}"},
        })
    world._record_and_fan({
        "type": "agent_hail", "who": "夏", "loc": "cafe",
        "payload": {"player_id": "p_quiet", "text": "她想你了"},
    })
    return world


def test_老写法会把一个人的收件箱饿死(busy):
    """先把病演一遍:只拿 list 的宿主,游标一步都推不动。"""
    since = 0
    seen: list[dict] = []
    for _ in range(3):
        page = busy.inbox("p_quiet", since_seq=since, limit=5)
        if not page:
            break                      # 没有"最后一条",宿主推不动游标
        seen.extend(page)
        since = page[-1]["seq"]
    assert seen == [], "这条测试的前提没成立 —— 换一个更热闹的窗口"
    assert since == 0, "游标居然自己动了"


def test_空页也能把游标推过去(busy):
    since = 0
    seen: list[dict] = []
    for _ in range(6):
        page = busy.inbox_page("p_quiet", since_seq=since, limit=5)
        seen.extend(page["events"])
        assert page["cursor"] >= since, "游标退回去了 —— 会重复拉同一段"
        since = page["cursor"]
        if page["next_seq"] is None:
            break
    assert [e["payload"]["text"] for e in seen] == ["她想你了"], \
        "「她想你了」还停在窗外"


def test_一页里没有他的也不用宿主再打一次库(busy):
    """空页那一次仍然要带着游标、带着"后面还有" —— 否则宿主只能再查一遍。"""
    page = busy.inbox_page("p_quiet", since_seq=0, limit=5)
    assert page["events"] == []
    assert page["cursor"] > 0, "扫过了 5 条却说游标是 0"
    assert page["next_seq"] == page["cursor"]
    assert page["scanned"] == 5, "宿主要看得出这一窗扫了多少条"


def test_contact_requests_同一个洞同一份修法(open_world):
    world = open_world()
    for i in range(8):
        world._record_and_fan({
            "type": "agent_wants_contact", "who": "夏", "loc": "cafe",
            "payload": {"player_id": "p_noise", "n": i},
        })
    world._record_and_fan({
        "type": "agent_wants_contact", "who": "夏", "loc": "cafe",
        "payload": {"player_id": "p_quiet"},
    })
    assert world.contact_requests(player_id="p_quiet", limit=4) == []
    page = world.contact_requests_page(player_id="p_quiet", limit=4)
    assert page["events"] == [] and page["cursor"] > 0
    page2 = world.contact_requests_page(
        player_id="p_quiet", since_seq=page["cursor"], limit=4)
    page3 = world.contact_requests_page(
        player_id="p_quiet", since_seq=page2["cursor"], limit=4)
    got = page["events"] + page2["events"] + page3["events"]
    assert len(got) == 1 and got[0]["payload"]["player_id"] == "p_quiet"


def test_不给_player_id_就是不过滤_游标照旧给(open_world):
    world = open_world()
    for i in range(3):
        world._record_and_fan({
            "type": "agent_hail", "who": "夏", "loc": "cafe",
            "payload": {"player_id": f"p{i}"},
        })
    page = world.inbox_page(None, limit=2)
    assert len(page["events"]) == 2
    assert page["cursor"] == page["events"][-1]["seq"]
    assert page["next_seq"] is not None


def test_老签名一个字没变(busy):
    """只加不改:返回 list 的两个门照旧返回 list。"""
    assert isinstance(busy.inbox("p_noise", limit=5), list)
    assert isinstance(busy.contact_requests(limit=5), list)


def test_CLI_的_json_吐得出游标(tmp_path):
    import json

    from _worldfile import open_world_at, run_cli

    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    try:
        for i in range(12):
            world._record_and_fan({
                "type": "agent_hail", "who": "夏", "loc": "cafe",
                "payload": {"player_id": "p_noise", "text": f"吵闹 {i}"},
            })
        world._record_and_fan({
            "type": "agent_hail", "who": "夏", "loc": "cafe",
            "payload": {"player_id": "p_quiet", "text": "她想你了"},
        })
        world.scheduler.checkpoint()
    finally:
        world.close()
    done = run_cli("contact", "--world-id", "w", "--inbox",
                   "--player", "p_quiet", "--limit", "5", "--json")
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["cursor"] > 0, "宿主照着 --json 写脚本,一样会饿死"
    assert payload["next_seq"] == payload["cursor"]
    assert payload["scanned"] == 5
