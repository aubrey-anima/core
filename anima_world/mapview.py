"""把地图和轨迹画成一块字符画布。**纯函数,不碰 I/O。**

## 为什么在引擎里,而且只画到终端

本包无 HTTP、无 HTML(网页层 2026-07 整体移除)。但"她今天去了哪儿"这件事,
数据只有引擎手里有,而**看不见的东西没人会去查**:1.3 的四个 bug 有三个在提示词
那一层,正是因为它此前不可见。

分工照旧:这一层只把数据摆成人能一眼看懂的样子;要好看的图,`map --json` 出数据,
创作台 / 网站 / 运维台自己渲染。**渲染在这儿是赠品,数据出口才是契约。**

## 三件差点画错的事

1. **坐标是相对父级的**(nested-map D2):`w=0.55` 的 region 占的是父级宽度的
   55%,不是画布的 55%。照原始 `x/y/w/h` 画出来的图看上去完全合理 —— 只是每个
   东西都在错的地方,而且什么都不会报错(实测 workshop 原始 `x=0.78`,绝对
   `0.482`)。所以这个模块**只收绝对几何**,换算归 `LocationStore`。

2. **中文是双宽字符。** 这个引擎的世界是中文的:地点叫「咖啡店」,角色叫「夏」。
   按字符个数排版,一个中文标签就把整条边框往右推两格 —— 而框线是这张图唯一的
   结构。所以画布按**显示宽度**记账(`east_asian_width`),宽字符占两格。

3. **两个人走同一条路。** 后画的把先画的整条盖掉,于是图上看不出那个人来过 ——
   "照跑但给错东西"的原样。重合处画 `#`,而不是让谁消失。

4. **地点名压掉区域边框。** 一个贴着框边的长名字会把那一行的 `│` 写掉,于是
   「建筑工作室」看上去在港街**外面** —— 而它明明在里面。框线承载的是"谁在谁
   里面"这个**事实**,名字只是名字:名字少几个字,读的人看得出来是少了;边框断
   一格,读的人只会安静地读到一句假话。所以标签给边框让路(`_label_beside`)。
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

# 画布的默认大小。宽按常见终端留一点余量,高按"一屏放得下"。
DEFAULT_WIDTH = 78
DEFAULT_HEIGHT = 26

# 轨迹记号:**单宽**且彼此好认。中文首字是双宽的,拿它画线会把格子撑歪。
_MARKERS = "abcdefghijkmnopqrstuvwxyz"   # 去掉 l(和 1、|、框线太像)
SHARED = "#"                             # 两个人以上走过的那一段

# 框线字符。它们是这张图上唯一**说事实**的东西("谁在谁里面"),所以标签让它们。
_FRAME = frozenset("─│┌┐└┘")
# 截断的记号。和 `◉`、框线一样是"东亚宽度不定"的字符 —— 这个模块一律按一格记账,
# 三者同进同退;要是哪天改成按两格,得一起改,不然框线自己先歪。
_ELLIPSIS = "…"


def display_width(text: str) -> int:
    """这段文本在终端里占几格。宽字符(中日韩)占两格。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


@dataclass
class MapPlace:
    """一个地点,几何已经是**绝对**画布坐标(0~1)。"""

    id: str
    name: str
    kind: str                      # "region" | "point"
    x: float
    y: float
    w: float | None = None
    h: float | None = None


@dataclass
class Track:
    """一个角色走过的路。`points` 是 `(tick, place_id)`,按 tick 升序。

    `anchor` 是**窗口之前**她在哪 —— 看某一天时,起点常常在前一天。它照样连线
    (否则那条路画不出来),但图例里说"自 X",不假装那是这天的一次位移。
    """

    agent: str
    points: list[tuple[int, str]] = field(default_factory=list)
    anchor: str | None = None


def markers_for(agents: Iterable[str]) -> dict[str, str]:
    """给每个角色发一个记号。**同一批人画两次必须一样。**

    按名字的稳定散列发(不是 `hash()` —— 那个每个进程都不一样,于是同一个世界
    画两次记号会变,两张图没法对照)。撞了就按名字顺序往后挪,所以给定这批人时
    结果唯一。
    """
    taken: dict[str, str] = {}
    used: set[str] = set()
    for agent in sorted(agents):
        seed = hashlib.md5(agent.encode("utf-8")).digest()[0]
        for offset in range(len(_MARKERS)):
            candidate = _MARKERS[(seed + offset) % len(_MARKERS)]
            if candidate not in used:
                taken[agent] = candidate
                used.add(candidate)
                break
        else:
            taken[agent] = "?"
    return taken


class _Canvas:
    """按**显示宽度**记账的字符画布。宽字符占两格,第二格是占位。"""

    __slots__ = ("_cells", "width", "height")

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self._cells: list[list[str | None]] = [[" "] * width for _ in range(height)]

    def put(self, x: int, y: int, ch: str, *, over: bool = True) -> None:
        if not (0 <= y < self.height and 0 <= x < self.width):
            return
        wide = display_width(ch) == 2
        if wide and x + 1 >= self.width:
            return
        row = self._cells[y]
        if not over and (row[x] != " " or (wide and row[x + 1] != " ")):
            return
        # 盖住一个宽字符的左半边时,把它孤零零的右半边也擦掉,否则排版会错开一格
        for probe in (x, x + 1 if wide else x):
            if row[probe] is None and probe > 0:
                row[probe - 1] = " "
            if probe + 1 < self.width and row[probe + 1] is None:
                row[probe + 1] = " "
        row[x] = ch
        if wide:
            row[x + 1] = None

    def text(self, x: int, y: int, s: str) -> None:
        col = x
        for ch in s:
            self.put(col, y, ch)
            col += display_width(ch)

    def room(self, x: int, y: int, step: int) -> int:
        """从 (x, y) 起沿 step 方向走,**撞上框线之前**还有几格可写。

        为什么问画布而不问几何:一个点旁边可能有好几层框(自己的、父级的、隔壁
        区域的),而"哪一条会被写掉"只取决于路上先遇到谁。照几何算等于把画序又
        推演一遍,推错了不会报错 —— 而画布上摆着的就是答案。
        """
        if not (0 <= y < self.height):
            return 0
        row = self._cells[y]
        free = 0
        col = x
        while 0 <= col < self.width and row[col] not in _FRAME:
            free += 1
            col += step
        return free

    def at(self, x: int, y: int) -> str | None:
        if 0 <= y < self.height and 0 <= x < self.width:
            return self._cells[y][x]
        return None

    def lines(self) -> list[str]:
        return ["".join(c for c in row if c is not None).rstrip() for row in self._cells]


def _to_cell(x: float, y: float, width: int, height: int) -> tuple[int, int]:
    """0~1 的画布坐标 → 字符格。

    不做长宽比校正:字符高约宽的两倍,校正之后地图会缩到角落里。这张图回答的是
    "谁在哪、谁往哪走",不是精确测距 —— 测距有 `LocationStore.distance`。
    """
    col = int(round(x * (width - 1)))
    row = int(round(y * (height - 1)))
    return (max(0, min(width - 1, col)), max(0, min(height - 1, row)))


def _draw_region(canvas: _Canvas, place: MapPlace) -> None:
    if place.w is None or place.h is None:
        return
    x0, y0 = _to_cell(place.x, place.y, canvas.width, canvas.height)
    x1, y1 = _to_cell(place.x + place.w, place.y + place.h, canvas.width, canvas.height)
    if x1 <= x0 or y1 <= y0:
        return
    for x in range(x0 + 1, x1):
        canvas.put(x, y0, "─", over=False)
        canvas.put(x, y1, "─", over=False)
    for y in range(y0 + 1, y1):
        canvas.put(x0, y, "│", over=False)
        canvas.put(x1, y, "│", over=False)
    for cx, cy, ch in ((x0, y0, "┌"), (x1, y0, "┐"), (x0, y1, "└"), (x1, y1, "┘")):
        canvas.put(cx, cy, ch, over=False)
    label = f" {place.name} "
    if x0 + 2 + display_width(label) < x1:
        canvas.text(x0 + 2, y0, label)


def _line(a: tuple[int, int], b: tuple[int, int]) -> Iterable[tuple[int, int]]:
    """两点之间的格子(Bresenham),含两端。"""
    x0, y0 = a
    x1, y1 = b
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        yield (x0, y0)
        if x0 == x1 and y0 == y1:
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _fit(text: str, room: int) -> str:
    """把标签截进 `room` 格,**按显示宽度截**,截了就说出来。

    两条都踩得着:按字符个数截会把一个双宽字劈成半个,那半个字在终端上占一格,
    整行往左错一格 —— 框线又歪了。而截了不留痕更坏:「建筑工作」看上去是一个
    完整的地名,读的人没有任何办法知道自己看的是半个名字。
    """
    if room <= 0:
        return ""
    if display_width(text) <= room:
        return text
    budget = room - display_width(_ELLIPSIS)
    kept, used = "", 0
    for ch in text:
        step = display_width(ch)
        if used + step > budget:
            break
        kept += ch
        used += step
    # 一个字都放不下时也要留个省略号:那是在说"这儿有个名字,只是写不下"
    return kept + _ELLIPSIS


def _label_beside(canvas: _Canvas, col: int, row: int, text: str, *, gap: str = " ") -> None:
    """把标签摆在记号旁边,**绝不写到框线上**。

    右边优先:中文和西文都从左往右读,`◉ 咖啡店` 比 `咖啡店 ◉` 更像"这个点叫
    咖啡店"。右边挤不下就整块挪到左边 —— 换边只是不好看,写穿框线是**说假话**,
    所以宁可换边、再不行宁可截。两边都放不下才截,截在宽的那一边。
    """
    if not text:
        return
    need = display_width(text) + display_width(gap)
    right = canvas.room(col + 1, row, 1)
    left = canvas.room(col - 1, row, -1)
    if right >= need:
        canvas.text(col + 1, row, f"{gap}{text}")
        return
    if left >= need:
        canvas.text(col - need, row, f"{text}{gap}")
        return
    if right >= left:
        shown = _fit(text, right - display_width(gap))
        if shown:
            canvas.text(col + 1, row, f"{gap}{shown}")
    else:
        shown = _fit(text, left - display_width(gap))
        if shown:
            canvas.text(col - display_width(shown) - display_width(gap), row, f"{shown}{gap}")


def render(
    places: list[MapPlace],
    *,
    tracks: list[Track] | None = None,
    marks: dict[str, list[str]] | None = None,
    markers: dict[str, str] | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> str:
    """画布。`marks` 是 `{place_id: [角色名…]}` —— 此刻站在那儿的人。

    画的顺序是**大的先画、小的后画、人最后画**:区域 → 连线 → 地点 → 人。
    倒过来的话一条轨迹会把地点名盖掉,而"她去了哪儿"里的"哪儿"才是重点。
    """
    canvas = _Canvas(width, height)
    by_id = {p.id: p for p in places}
    tracks = tracks or []
    markers = markers or markers_for(t.agent for t in tracks)

    regions = [p for p in places if p.kind == "region"]
    # 大的先画:小的压在上面,而不是反过来把大的边框戳出洞
    regions.sort(key=lambda p: -((p.w or 0) * (p.h or 0)))
    for region in regions:
        _draw_region(canvas, region)

    track_chars = set(_MARKERS) | {SHARED}
    for track in tracks:
        char = markers.get(track.agent, "?")
        cells = [
            _to_cell(by_id[pid].x, by_id[pid].y, width, height)
            for _tick, pid in track.points
            if pid in by_id
        ]
        for start, end in zip(cells, cells[1:]):
            for cx, cy in _line(start, end):
                here = canvas.at(cx, cy)
                # 别人的轨迹已经在这儿 → 标成重合,而不是把那个人抹掉
                canvas.put(cx, cy, SHARED if here in track_chars and here != char else char)

    for place in places:
        if place.kind != "point":
            continue
        col, row = _to_cell(place.x, place.y, width, height)
        canvas.put(col, row, "◉")
        # 挤不下时往左让、再不行截 —— 挡住它的既有画布边缘,也有区域框线:
        # 写穿框线的那一版把「在港街里」画成了「在港街外」。
        _label_beside(canvas, col, row, place.name)

    for place_id, agents in (marks or {}).items():
        place = by_id.get(place_id)
        if place is None:
            continue
        col, row = _to_cell(place.x, place.y, width, height)
        label = "".join(markers.get(a, "?") for a in agents)
        # 站在这儿的人也贴着记号写,同一条规矩:`[n]` 写穿框线和地点名写穿一样骗人。
        # 这一层不留空格 —— `[n]` 本来就紧贴在 `◉` 下面那一格,挪一格会和上一行错开。
        _label_beside(canvas, col, row + 1, f"[{label}]", gap="")

    return "\n".join(canvas.lines())


def legend(
    tracks: list[Track],
    places: list[MapPlace],
    *,
    markers: dict[str, str] | None = None,
    standing: dict[str, list[str]] | None = None,
    travelling: list[dict[str, Any]] | None = None,
    show_hops: bool = True,
) -> list[str]:
    """每个角色一行:记号、名字、这段时间里去过哪儿,以及此刻在哪。

    **一天只有几次位移**(实测一个三人世界 288 tick 里 6 次),所以整行摊开比
    折叠成计数好读 —— 看的人想知道的是顺序,不是次数。
    """
    names = {p.id: p.name for p in places}
    markers = markers or markers_for(t.agent for t in tracks)
    where: dict[str, str] = {}
    for place_id, agents in (standing or {}).items():
        for agent in agents:
            where[agent] = f"在 {names.get(place_id, place_id)}"
    for trip in travelling or []:
        where[str(trip["agent"])] = (
            f"在路上 → {names.get(trip.get('to'), trip.get('to'))}"
            f"(第 {trip.get('arrive_at')} tick 到)"
        )

    lines: list[str] = []
    if not show_hops:
        # `--now` 只问"此刻谁在哪"。此时说"没动过"是**撒谎** —— 没去看不等于没动。
        for agent, place in sorted(where.items()):
            lines.append(f"{markers.get(agent, '?')} {agent}  {place}")
        return lines

    for track in sorted(tracks, key=lambda t: t.agent):
        hops = " → ".join(
            f"{tick} {names.get(pid, pid)}" for tick, pid in track.points
            if pid != track.anchor or (tick, pid) != track.points[0]
        )
        head = f"自 {names.get(track.anchor, track.anchor)}" if track.anchor else ""
        body = " → ".join(x for x in (head, hops) if x) or "(没动过)"
        tail = f"  [{where[track.agent]}]" if track.agent in where else ""
        lines.append(f"{markers.get(track.agent, '?')} {track.agent}  {body}{tail}")
    # 一次都没动过的人也要出现 —— 否则"她今天没出门"看起来像"没有这个人"
    for agent, place in sorted(where.items()):
        if not any(t.agent == agent for t in tracks):
            lines.append(f"{markers.get(agent, '?')} {agent}  (没动过)  [{place}]")
    return lines


def tracks_from_events(
    events: Iterable[Any], *, agents: list[str] | None = None
) -> list[Track]:
    """从事件日志里抠出轨迹。

    只认 `state_change` 里的 `location_join` —— 那是**到达**。`travel` 那种"起程"
    不算:她可能走到一半被打断,而画一条没走完的线等于说她到了。
    """
    per_agent: dict[str, list[tuple[int, str]]] = {}
    for event in events:
        if getattr(event, "type", None) != "state_change":
            continue
        payload = getattr(event, "payload", None) or {}
        if payload.get("kind") != "location_join":
            continue
        who = getattr(event, "who", None)
        place = payload.get("location")
        if not who or not place:
            continue
        if agents is not None and who not in agents:
            continue
        per_agent.setdefault(str(who), []).append((int(getattr(event, "ts", 0)), str(place)))
    return [Track(agent=a, points=p) for a, p in sorted(per_agent.items())]
