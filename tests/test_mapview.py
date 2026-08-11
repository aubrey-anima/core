"""把地图和轨迹画出来 —— 而"画错了"不会报错,只会好看地骗人。

这一层的每个坏法都长一个样:**图渲染出来了,排版也整齐,只是内容是假的。**
所以这个文件盯的不是"有没有输出",是四件具体的事:

1. **坐标是相对父级的**,照原始值画,每个东西都在错的地方
2. **中文是双宽字符**,按字符个数排版会把边框推歪 —— 而这个引擎的世界是中文的
3. **两个人走同一条路**,后画的把先画的盖掉,那个人就从图上消失了
4. **在路上的人不站在任何地方**,漏了这一层她会在图上凭空消失半段路
5. **地点名写穿区域边框**,于是框里的地点看上去在框外 —— 图给出的是一句
   排版整齐的假话
"""
from __future__ import annotations

from _worldfile import open_world_at, run_cli

import os

import pytest

from anima_world.mapview import (
    MapPlace,
    SHARED,
    Track,
    _fit,
    display_width,
    legend,
    markers_for,
    render,
    tracks_from_events,
)


def _places() -> list[MapPlace]:
    """一个两层地图,几何**已经是绝对坐标**(换算是 LocationStore 的事)。"""
    return [
        MapPlace("outer", "旧港区", "region", 0.05, 0.05, 0.9, 0.9),
        MapPlace("inner", "港街", "region", 0.1, 0.1, 0.4, 0.4),
        MapPlace("cafe", "咖啡店", "point", 0.2, 0.2),
        MapPlace("home", "家", "point", 0.8, 0.8),
    ]


def _wall_hugging_places() -> list[MapPlace]:
    """内置世界「旧港」的形状:一个长名字的地点贴在内层区域的右边框上。

    这就是那一格出事的地方 —— 建筑工作室在港街**里**,靠右;它的名字往右一写,
    港街的 `│` 就没了。
    """
    return [
        MapPlace("outer", "旧港区", "region", 0.05, 0.05, 0.9, 0.9),
        MapPlace("inner", "港街", "region", 0.1, 0.1, 0.4, 0.4),
        MapPlace("cafe", "咖啡店", "point", 0.2, 0.25),
        MapPlace("workshop", "建筑工作室", "point", 0.45, 0.35),
        MapPlace("home", "家", "point", 0.8, 0.8),
    ]


def _cells(line: str) -> list[str]:
    """把一行摊成**显示格**:宽字符占两格,右半格记成 `''`(它不是一个字符)。

    按字符下标去数第几列,中文一多就全错 —— 而这个模块的世界是中文的。
    """
    out: list[str] = []
    for ch in line:
        out.append(ch)
        if display_width(ch) == 2:
            out.append("")
    return out


def _assert_frames_are_closed(art: str) -> None:
    """每个区域框的左右两条边,**每一行该在的那一列上都得是边框字符**。

    只断言"没抛异常"或者"名字出现了"是抓不到这个 bug 的:图照样画得出来、
    排版照样整齐,只是少了一格 `│` —— 而少的那一格把"在里面"变成了"在外面"。
    """
    lines = art.split("\n")
    rows = [_cells(line) for line in lines]
    tops = [
        (y, r.index("┌"), r.index("┐"))
        for y, r in enumerate(rows) if "┌" in r and "┐" in r
    ]
    assert tops, "一个区域框都没画出来 —— 这条测试验不到它想验的"
    for y0, x0, x1 in tops:
        bottom = next(
            (y for y in range(y0 + 1, len(rows))
             if x0 < len(rows[y]) and rows[y][x0] == "└"),
            None,
        )
        assert bottom is not None, f"第 {y0} 行开的那个框没有底边"
        for y in range(y0 + 1, bottom):
            for x, side in ((x0, "左"), (x1, "右")):
                got = rows[y][x] if x < len(rows[y]) else ""
                assert got == "│", (
                    f"第 {y} 行第 {x} 列的{side}边框被写掉了(那一格是 {got!r})——"
                    f"框里的地点看上去跑到框外去了:\n" + "\n".join(lines[y0:bottom + 1])
                )


# ── 标签不许写穿边框 ────────────────────────────────────────────────────────


def test_a_long_label_never_writes_over_a_region_border():
    """**这是这一层最坏的坏法:图没错在难看,错在事实。**

    「建筑工作室」在港街里,它的名字却把港街的右边框写掉了 —— 读图的人只会
    得到一个结论:这个地点在港街外面。而没有任何东西会报错。
    """
    art = render(_wall_hugging_places(), width=68, height=22)
    assert "建筑工作室" in art, "长名字整个没画出来 —— 这条测试验不到它想验的"
    _assert_frames_are_closed(art)


def test_a_label_flips_to_the_left_when_the_wall_is_on_the_right():
    """右边是框线就往左摆:换边只是不好看,写穿框线是说假话。"""
    art = render(_wall_hugging_places(), width=68, height=22)
    row = next(line for line in art.split("\n") if "建筑工作室" in line)
    assert row.count("◉") == 1, f"这一行不止一个地点,验不到摆在哪边:{row!r}"
    assert row.index("建筑工作室") < row.index("◉"), (
        f"名字还摆在记号右边,而右边是墙:{row!r}"
    )


def test_a_truncated_label_says_that_it_was_truncated():
    """两边都摆不下时才截,**而截了必须留痕**。

    「建筑工作」看上去是一个完整的地名 —— 读的人没有任何办法知道自己看的是
    半个名字,和边框断掉一样是无声地给错东西。
    """
    places = [
        MapPlace("inner", "港街", "region", 0.1, 0.1, 0.25, 0.6),
        MapPlace("workshop", "建筑工作室", "point", 0.22, 0.4),
    ]
    art = render(places, width=60, height=18)
    _assert_frames_are_closed(art)
    assert "建筑工作室" not in art, "这么窄的框里居然整个名字都写下了 —— 换个更窄的框"
    assert "…" in art, f"名字被截了却一声不吭:\n{art}"


def test_a_truncated_label_never_splits_a_wide_char():
    """按字符个数截会把一个双宽字劈成半个,整行往左错一格 —— 框线又歪了。"""
    for room in range(1, 12):
        shown = _fit("建筑工作室", room)
        assert display_width(shown) <= room, (
            f"截到 {room} 格,结果占了 {display_width(shown)} 格:{shown!r}"
        )
        if shown and shown != "建筑工作室":
            assert shown.endswith("…"), f"截了没留痕:{shown!r}"


def test_standing_agents_do_not_break_the_border_either():
    """站在这儿的人那一格 `[abc]` 和地点名同一条规矩 —— 它也在框线上写字。"""
    places = _wall_hugging_places()
    crowd = ["夏", "柔", "遥", "宁", "岚"]
    art = render(places, marks={"workshop": crowd},
                 markers=markers_for(crowd), width=68, height=22)
    _assert_frames_are_closed(art)


def test_the_builtin_world_map_has_no_broken_borders(tmp_path):
    """**拿真产物演一遍那个用户故事。**

    这个 bug 是玩内置演示世界「旧港」时看见的,不是构造出来的:真实几何、
    真实地名、CLI 默认的 78 格画布。
    """
    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, force_mock_llm=True) as world:
        world.tick(288)

    done = run_cli("map", "--world-id", "w", "--width", "78")
    assert done.returncode == 0, done.stderr
    art = done.stdout.split("世界时钟")[0]
    _assert_frames_are_closed(art)


# ── 中文世界的排版 ──────────────────────────────────────────────────────────


def test_every_line_is_the_same_display_width(tmp_path):
    """**这个引擎的世界是中文的。**

    地点叫「咖啡店」,角色叫「夏」。按字符个数排版,一个中文标签就把整条边框往右
    推两格 —— 而框线是这张图唯一的结构。一眼看上去只是"有点歪",实际是排版账算错了。
    """
    art = render(_places(), width=60, height=20)
    lines = [line for line in art.split("\n") if line.strip()]
    assert lines, "什么都没画出来"
    for line in lines:
        assert display_width(line) <= 60, (
            f"这一行占了 {display_width(line)} 格,超出画布 60 —— "
            f"多半是拿字符个数当宽度算了:{line!r}"
        )


def test_a_wide_label_does_not_shift_the_frame(tmp_path):
    """中文标签所在那一行,右边框必须还在原来那一列。"""
    art = render(_places(), width=60, height=20)
    rights = {
        display_width(line.rstrip()) for line in art.split("\n")
        if line.rstrip().endswith("│") or line.rstrip().endswith("┐")
        or line.rstrip().endswith("┘")
    }
    assert len(rights) == 1, f"右边框落在了不同的列:{sorted(rights)} —— 排版被宽字符推歪了"


def test_display_width_counts_cjk_as_two():
    assert display_width("abc") == 3
    assert display_width("咖啡店") == 6
    assert display_width("a咖b") == 4


# ── 两个人走同一条路 ────────────────────────────────────────────────────────


def test_two_agents_on_the_same_route_do_not_erase_each_other():
    """**后画的把先画的整条盖掉,那个人就从图上消失了。**

    单看图完全正常 —— 只是少了一个人,而没有任何东西会报错。重合处标 `#`。
    """
    places = _places()
    same = [(0, "cafe"), (10, "home")]
    art = render(places, tracks=[Track("夏", same), Track("柔", list(same))])
    marks = markers_for(["夏", "柔"])
    assert SHARED in art, "两个人走了同一条路,图上却看不出重合"
    # 谁都不该被独占那条线
    for agent in ("夏", "柔"):
        assert art.count(marks[agent]) <= art.count(SHARED), (
            f"{agent} 的记号独占了那条共用的路 —— 另一个人被抹掉了"
        )


def test_one_agent_alone_keeps_their_own_marker():
    """只有一个人走过时不该标成重合 —— 那会让"就她一个"看起来像"好几个人"。"""
    art = render(_places(), tracks=[Track("夏", [(0, "cafe"), (10, "home")])])
    assert markers_for(["夏"])["夏"] in art
    assert SHARED not in art


def test_markers_are_stable_across_processes():
    """**同一批人画两次必须一样**,否则两张图没法对照。

    用 `hash()` 就会坏:它每个进程都不一样(PYTHONHASHSEED 随机化),于是同一个
    世界今天画和明天画记号会变,而这正是这张图最常见的用法 —— 前后比。
    """
    import subprocess
    import sys

    code = (
        "from anima_world.mapview import markers_for;"
        "print(markers_for(['夏', '柔', '遥']))"
    )
    runs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, check=True).stdout.strip()
        for _ in range(3)
    }
    assert len(runs) == 1, f"记号在不同进程里不一样:{runs}"


def test_markers_never_collide():
    """撞了要挪开 —— 两个人共用一个记号,图例就对不上图。"""
    agents = [f"角色{i}" for i in range(20)]
    given = markers_for(agents)
    assert len(set(given.values())) == len(agents)


def test_markers_are_single_width():
    """记号必须单宽:拿中文首字画线会把格子撑歪(那是第一版的做法)。"""
    for marker in markers_for(["夏", "柔", "遥", "Alice"]).values():
        assert display_width(marker) == 1, f"记号 {marker!r} 是双宽的"


# ── 轨迹是从哪儿来的 ────────────────────────────────────────────────────────


class _Event:
    def __init__(self, ts, type, who, payload):
        self.ts, self.type, self.who, self.payload = ts, type, who, payload


def test_only_arrivals_count_not_departures():
    """**起程不算到达。**

    她可能走到一半被打断(需求紧急带会抢),而画一条没走完的线等于说她到了。
    只认 `state_change/location_join`。
    """
    events = [
        _Event(5, "agent_action", "夏", {"action": "walk", "location": "home"}),
        # **同是 state_change,同样带 location,但不是到达。** 今天引擎里只有
        # `location_join` 带 location,所以按"有没有 location"筛也恰好对 ——
        # 那是巧合,不是判据。这一条把判据钉成"kind 必须是 location_join",
        # 于是哪天有别的 kind 带上了 location,这里当场红而不是悄悄多画一段。
        _Event(7, "state_change", "夏", {"kind": "agent_state", "location": "cafe",
                                         "status": "在路上"}),
        _Event(9, "state_change", "夏", {"kind": "location_join", "location": "home"}),
        _Event(12, "state_change", "夏", {"kind": "mood", "value": "低落"}),
    ]
    tracks = tracks_from_events(events)
    assert [t.points for t in tracks] == [[(9, "home")]], (
        "把起程、或别的带 location 的 state_change 也算成位移了"
    )


def test_filtering_by_agent():
    events = [
        _Event(1, "state_change", "夏", {"kind": "location_join", "location": "home"}),
        _Event(2, "state_change", "柔", {"kind": "location_join", "location": "cafe"}),
    ]
    assert [t.agent for t in tracks_from_events(events, agents=["柔"])] == ["柔"]


# ── 图例不许撒谎 ────────────────────────────────────────────────────────────


def test_someone_who_never_moved_still_shows_up():
    """"她今天没出门"和"没有这个人"是两件事。"""
    lines = legend([], _places(), standing={"home": ["夏"]})
    assert any("夏" in line for line in lines), "没动过的人从图例里消失了"


def test_a_traveller_is_reported_as_on_the_road():
    """**在路上的人不站在任何地方。** 漏了这一层她会在图上凭空消失半段路。"""
    lines = legend(
        [], _places(),
        travelling=[{"agent": "夏", "from": "cafe", "to": "home", "arrive_at": 42}],
    )
    text = "\n".join(lines)
    assert "在路上" in text and "42" in text, f"路上的人没被报出来:{text!r}"


def test_now_view_does_not_claim_nobody_moved():
    """`--now` 只问"此刻谁在哪"。此时说"没动过"是**撒谎** —— 没去看不等于没动。"""
    lines = legend([], _places(), standing={"home": ["夏"]}, show_hops=False)
    assert not any("没动过" in line for line in lines), (
        "只看此刻,却报了一句关于「她动没动过」的话"
    )
    assert any("在 家" in line for line in lines)


# ── 画得出来 ────────────────────────────────────────────────────────────────


def test_a_map_with_no_geometry_does_not_crash():
    """放不下的地点画不出来,但不该让整张图挂掉。"""
    art = render([MapPlace("x", "无处", "point", 0.5, 0.5)], width=20, height=6)
    assert "无处" in art


def test_regions_are_drawn_biggest_first():
    """小的压在大的上面,而不是反过来把大的边框戳出洞。"""
    art = render(_places(), width=60, height=20)
    assert "旧港区" in art and "港街" in art


@pytest.mark.parametrize("size", [(30, 8), (60, 20), (120, 40)])
def test_it_renders_at_any_size(size):
    width, height = size
    art = render(_places(), tracks=[Track("夏", [(0, "cafe"), (9, "home")])],
                 marks={"home": ["夏"]}, width=width, height=height)
    for line in art.split("\n"):
        assert display_width(line) <= width


# ── 数据面:World.map_data ───────────────────────────────────────────────────


def test_geometry_comes_out_absolute_not_parent_relative(tmp_path):
    """**这是最容易画错、又最不会报错的一条。**

    库里存的几何是**相对父级**的(nested-map D2):`w=0.55` 的 region 占的是父级
    宽度的 55%。照原始值画出来的图看上去完全合理 —— 只是每个东西都在错的地方。
    实测内置种子:workshop 原始 `x=0.78`,绝对位置是 `0.482`。

    所以 `map_data` 交出来的必须已经是绝对坐标,而这条拿库里的原始行去比。
    """
    from anima_world.api import World

    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, force_mock_llm=True) as world:
        data = world.map_data()
        raw = {
            str(r["id"]): (r.get("x"), r.get("y"), r.get("w"), r.get("h"), r.get("parent"))
            for r in world.scheduler.location_store.all()
        }
    by_id = {p["id"]: p for p in data["places"]}

    nested = [i for i, row in raw.items() if row[4] is not None and row[0] is not None]
    assert nested, "这个种子里没有嵌套地点 —— 这条测试验不到它想验的"
    differing = [
        i for i in nested
        if i in by_id and abs(by_id[i]["x"] - float(raw[i][0])) > 1e-9
    ]
    assert differing, (
        "每个嵌套地点的 x 都和库里的原始值一模一样 —— 换算没做,"
        "图上每个东西都会在错的地方"
    )
    # 而且换算的结果要和地图自己那套一致(不能各算各的)
    with open_world_at(db_path, force_mock_llm=True) as world:
        store = world.scheduler.location_store
        for place_id, place in by_id.items():
            expected = store.absolute_xy(place_id)
            assert expected is not None
            assert abs(place["x"] - expected[0]) < 1e-9
            assert abs(place["y"] - expected[1]) < 1e-9


def test_someone_on_the_road_is_not_standing_anywhere(tmp_path):
    """**在路上的人不站在任何地方。**

    漏了 `travelling` 这一层,她会在图上凭空消失半段路 —— 而位移正是这张图要看的
    东西,少一个人不会有任何报错。
    """
    from anima_world.api import World

    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        agent = next(iter(world.state()["agents"]))
        here = world.state()["agents"][agent].get("location")
        dest = next(loc["id"] for loc in world.state()["locations"]
                    if loc["id"] != here and loc["kind"] == "point")
        out = world.act(agent, "walk", {"location": dest}, surface="body")
        assert out["ok"], out

        data = world.map_data()
        standing = {a for group in data["standing"].values() for a in group}
        assert agent not in standing, "她在路上,却还被算成站在某个地方"
        trip = next((t for t in data["travelling"] if t["agent"] == agent), None)
        assert trip is not None, "她在路上,而 travelling 里没有她 —— 图上就没这个人了"
        assert trip["to"] == dest and trip["arrive_at"] > 0


def test_the_track_matches_what_the_world_logged(tmp_path):
    """轨迹必须来自事件日志本身,而不是另算一遍。"""
    import json
    import sqlite3

    from anima_world.api import World

    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, force_mock_llm=True) as world:
        world.tick(288)
        data = world.map_data()
        logged = []
        for e in world.scheduler.event_log.replay():
            if e.type == "state_change" and e.payload.get("kind") == "location_join":
                logged.append((int(e.ts), str(e.who), str(e.payload["location"])))

    drawn = sorted(
        (p["tick"], t["agent"], p["place"])
        for t in data["tracks"] for p in t["points"]
    )
    assert drawn == sorted(logged), "画出来的轨迹和日志里记的对不上"


# ── CLI ─────────────────────────────────────────────────────────────────────


def _cli(*argv):
    import os
    import subprocess
    import sys

    return run_cli("map", *argv)


def test_the_cli_prints_a_map(tmp_path):
    """创作台那侧的判据是**有没有 CLI 出口** —— 库里有而 CLI 上没有,对它等于不存在。"""
    from anima_world.api import World

    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, force_mock_llm=True) as world:
        world.tick(288)

    done = _cli("--world-id", "w")
    assert done.returncode == 0, done.stderr
    assert "◉" in done.stdout, "没画出地点"
    assert "世界时钟" in done.stdout


def test_the_cli_json_is_machine_readable(tmp_path):
    """`--json` 是给别的仓库用的契约。**stdout 必须是干净的 JSON** ——
    降级警告那类话混进去,宿主解析就炸。"""
    import json

    from anima_world.api import World

    db_path = str(tmp_path / "w.db")
    with open_world_at(db_path, force_mock_llm=True) as world:
        world.tick(288)

    done = _cli("--world-id", "w", "--json")
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)          # 混进任何一行别的就炸在这儿
    assert data["places"] and "clock" in data
    assert set(data) == {"clock", "places", "standing", "travelling", "tracks"}



def test_day_and_tick_range_are_mutually_exclusive(tmp_path):
    from anima_world.api import World

    db_path = str(tmp_path / "w.db")
    open_world_at(db_path, force_mock_llm=True).close()
    done = _cli("--world-id", "w", "--day", "1", "--from", "0")
    assert done.returncode == 2
    assert "二选一" in done.stderr


def test_a_window_carries_where_she_came_from(tmp_path):
    """**只取窗口内的点,起点在窗口之前的人就只剩一个孤点** —— 画不出线,
    看上去像"她这天没动"。

    而 `--day N` 恰恰是最常用的看法:实测一个世界的第 2 天,三个人里两个的
    起点在第 1 天。锚点标 `before`,图例说"自 X",不假装那是这天的一次位移。
    """
    from anima_world.api import World

    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.tick(288 * 2)
        whole = world.map_data()
        day2 = world.map_data(from_tick=288, to_tick=576)

    moved_before = {
        t["agent"] for t in whole["tracks"]
        if any(p["tick"] < 288 for p in t["points"])
    }
    assert moved_before, "第一天没人动过 —— 这条测试验不到它想验的"

    for track in day2["tracks"]:
        if track["agent"] not in moved_before:
            continue
        first = track["points"][0]
        assert first.get("before"), (
            f"{track['agent']} 在窗口之前动过,而窗口视图没带上她从哪儿来 —— "
            f"她的那条路画不出来"
        )
        assert first["tick"] < 288

    # 锚点是**窗口之前的最后一个**位置,不是随便哪一个
    for track in day2["tracks"]:
        anchor = next((p for p in track["points"] if p.get("before")), None)
        if anchor is None:
            continue
        earlier = [
            p for t in whole["tracks"] if t["agent"] == track["agent"]
            for p in t["points"] if p["tick"] < 288
        ]
        assert anchor["place"] == earlier[-1]["place"]


def test_the_anchor_is_not_reported_as_a_move():
    """图例里那一格必须说"自 X" —— 说成"第 230 tick 到了家"是把前一天的事
    算进了这一天。"""
    places = _places()
    track = Track("夏", [(230, "home"), (510, "cafe")], anchor="home")
    text = "\n".join(legend([track], places))
    assert "自 家" in text, f"锚点没被标成「来处」:{text!r}"
    assert "230" not in text, f"窗口之前的那一步被当成这天的位移报出来了:{text!r}"
    assert "510" in text


# ── 连上一个活着的世界 ──────────────────────────────────────────────────────




def test_the_cli_reads_a_live_world_from_another_connection(tmp_path):
    """**另一条连接读到的必须是「此刻」,不是一份快照。**

    世界活在 World 实例里,CLI 只有一个 Redis 连接 —— 它看到的是同一个世界的
    现在。(真正的跨进程互见由 test_redis_state 的黑板/时钟测试守;这里守的是
    map 这条只读路真的读活状态。)
    """
    import json

    db_path = str(tmp_path / "w.db")
    world = open_world_at(db_path, force_mock_llm=True)
    try:
        world.tick(288)
        clock_here = world.scheduler.clock

        done = run_cli("map", "--world-id", "w", "--json")
        assert done.returncode == 0, done.stderr
        seen = json.loads(done.stdout)
        assert seen["clock"] == clock_here, (
            f"读到的时钟是 {seen['clock']},而世界在 {clock_here} —— 它读的不是这个活世界"
        )
        assert seen["places"], "地图是空的 —— 多半是 world_id 没对上"
        assert seen["standing"] or seen["travelling"], "一个人都没有"

        world.tick(60)
        again = json.loads(run_cli("map", "--world-id", "w", "--json").stdout)
        assert again["clock"] == world.scheduler.clock, "推了时钟,另一条连接没看见"
    finally:
        world.close()


def test_a_wrong_world_id_leaves_no_garbage_behind(tmp_path):
    """拒绝之前一个键都不许建 —— 差点抄错 id 就在 Redis 上创出一个新世界。"""
    from _worldfile import redis_for

    db_path = str(tmp_path / "w.db")
    world = open_world_at(db_path, force_mock_llm=True)
    client = redis_for(db_path)
    bogus = "根本没有这个世界"
    try:
        world.tick(60)
        done = run_cli("map", "--world-id", bogus, "--json")
        assert done.returncode == 2, f"照着抄错的 id 连上去了:\n{done.stdout[:300]}"
        assert bogus in done.stderr
        assert not list(client.scan_iter(match=f"anima:{bogus}:*")), (
            "拒绝之前已经在 Redis 上把那个假世界建出来了"
        )
    finally:
        world.close()


def test_map_refuses_a_world_that_does_not_exist(capsys):
    """抄错 world_id 的样子比报错坏得多(5ce6aed 的教训,新形态):打开一个不存在
    的名字会当场创世 —— 你看到的是一张排版正常、时钟 0 的地图。只读命令必须拒绝。"""
    result = run_cli("map", "--world-id", "no-such-world")
    assert result.returncode == 2
    assert "没有叫" in result.stderr
    assert "start" in result.stderr
