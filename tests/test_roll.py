"""骰子那一层的纯函数(3.12.0,批 3b · 裁决 §2.2)。

它是 `expressions.world_dice` 的**有档位版本**,不是第二套随机 ——
所以这里钉的是三件:**同种子同结果** · **第五个坐标真的分得开** · **跨进程一致**。
"""
from __future__ import annotations

from anima_world.roll import DEFAULT_BANDS, FACES, band_of, roll


def _r(**kw):
    base = dict(world_id="w", tick=5, actor="player:p1", verb="confront", nonce=0)
    base.update(kw)
    return roll(**base)


def test_同五坐标两次_逐位相同():
    """引擎的 replay 纪律:同一个世界、同一刻、同一个人、同一件事、同一次 ——
    永远得同一个点数。"""
    assert _r() == _r()
    assert _r(nonce=7) == _r(nonce=7)


def test_同一tick同一动词掷两次_两个数不同():
    """🔴 **这一条是裁决 §2.2 点名要单独写的那一条,而它是底稿漏掉的那一格。**

    底稿把种子写成 `(world, tick, actor, verb)` 四个坐标 —— 而**同一 tick、
    同一个人、同一个动词掷两次会得到同一个数**。那不是边角情形:3a 实测
    「同地同日连点二十次」二十拍**都落在同一批 tick 里**,于是玩家连点两次
    对抗,两次掷出一模一样的点 —— 而屏幕上看起来完全正常。

    ⚠️ **按分布断,不按一对断**:任何两次掷点都有 1/20 撞在一起,
    拿一对去断言的用例会**偶尔红一次**,而这个仓库为「偶尔红的判据」栽过
    (`test_autonomy` 那个 5 秒挂钟)。这里连掷 30 次,要求至少 10 个不同的点 ——
    真漏了第五坐标时它恒等于 1。
    """
    same_tick = {_r(nonce=i)["result"] for i in range(30)}
    assert len(same_tick) >= 10, (
        f"同一 tick 连掷 30 次只摇出 {len(same_tick)} 种点数 —— "
        "第五个坐标多半没接上(漏了它的话这里恒等于 1)")


def test_换掉任何一个坐标都是另一副骰子():
    """五个坐标,**每一个都要真的进种子** —— 少接一个不报错,只是那一维不起作用。"""
    base = _r()
    for kw in ({"world_id": "别的世界"}, {"tick": 6}, {"actor": "player:p2"},
               {"verb": "reveal"}, {"nonce": 1}):
        # 单个坐标变了之后**不保证**点数不同(1/20 会撞),所以比的是 `seed` 那一行:
        # 它是"这一次掷点由哪五个坐标决定"的可读形式,必须逐个不同。
        assert _r(**kw)["seed"] != base["seed"], kw


def test_点数落在1到faces之间_而且取得到两头():
    """`(2**64-1)/2**64` 在双精度里会舍入成 1.0 —— `world_dice` 为此取 53 位。
    这里断的是它的下游:**永远不许摇出 0 或 faces+1**。"""
    seen = {_r(tick=t, nonce=n)["result"]
            for t in range(40) for n in range(10)}
    assert seen and min(seen) >= 1 and max(seen) <= FACES
    # 400 次里两头都该出现过 —— 出不来说明折算把区间掐窄了
    assert 1 in seen and FACES in seen, sorted(seen)[:3] + sorted(seen)[-3:]


def test_档位取最后一个小于等于它的那一档():
    assert band_of(1) == "砸了"
    assert band_of(5) == "砸了"
    assert band_of(6) == "差一点"
    assert band_of(20) == "神了"
    # 作者自己的档位表照收
    assert band_of(3, [[1, "输"], [10, "赢"]]) == "输"
    assert band_of(10, [[1, "输"], [10, "赢"]]) == "赢"


def test_档名是人话_不是裸英文():
    """它**印在玩家屏上** —— 和 `MOVE_LABELS` / `MOMENT_LABELS` 同一条。"""
    for _, name in DEFAULT_BANDS:
        assert name and not name.isascii(), name


def test_跨进程一致_不许用内置hash():
    """`hash()` 对 str 加了每进程一份的盐(PYTHONHASHSEED),于是同一个世界在
    两个进程里摇出两副骰子 —— 而这个引擎的世界本来就是很多进程同时在操作的。

    判据是**在另一个解释器里再摇一遍**,不是读源码。
    """
    import json
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from anima_world.roll import roll\n"
        "import json; print(json.dumps(roll(world_id='w', tick=5,"
        " actor='player:p1', verb='confront', nonce=0)))"
        % str(__import__("pathlib").Path(__file__).resolve().parents[1])
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env={"PYTHONHASHSEED": "12345", "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr[-500:]
    assert json.loads(out.stdout) == _r(), "换一个进程就摇出另一副骰子"


def test_契约那一段和纯模块逐项相等():
    """🔴 **分档表只有一份**(和编剧那三张人话表逐字同一条):各译一遍的话,
    同一个点会在引擎屏 / 创作台 / 站点上说三种话,而没有一处会报错。"""
    from anima_world import roll as R
    from anima_world.__main__ import contract_payload

    seg = contract_payload()["roll"]
    assert seg["faces"] == R.FACES
    assert [tuple(b) for b in seg["bands"]] == [tuple(b) for b in R.DEFAULT_BANDS]


def test_契约报的五个坐标_就是roll真吃的那五个():
    """少报一格的下场是下游自己补一个 —— 而补错了不报错,只是骰子重了。"""
    import inspect

    from anima_world import roll as R
    from anima_world.__main__ import contract_payload

    named = set(contract_payload()["roll"]["seed_coordinates"])
    takes = {n for n, p in inspect.signature(R.roll).parameters.items()
             if p.default is inspect.Parameter.empty}
    assert named == takes, f"契约说 {sorted(named)},而函数吃 {sorted(takes)}"


def test_契约报的载荷格_一格不少地真出现在事件里(tmp_path):
    """**别数源码,去问事件**:契约里那张 `payload_keys` 是宿主照着解析的。"""
    from _worldfile import open_world_at
    from anima_world.__main__ import contract_payload

    want = set(contract_payload()["roll"]["payload_keys"])
    with open_world_at(tmp_path / "rl.db") as world:
        agent = next(iter(world.scheduler.agents))
        world.player_move("p1", "cafe")
        world.tick(2)
        world._director_apply(
            "p1", {"move": "confront", "who": agent, "line": "摊牌了",
                   "why": "", "promise": "", "stake": None, "source": "mock"},
            tension_before=0.5, phase="climax", tick=int(world.scheduler.clock),
            place="cafe", thread=None, pin_ticks=12, due_ticks=0, capped=False,
            forbidden_ops=set(), recap=[], place_name="咖啡店", moves_made=1)
        rolls = [e["payload"] for e in world.history(kind="roll")["events"]]
    assert rolls, "摊牌了而一个点都没掷"
    assert want <= set(rolls[-1]), f"契约说有 {sorted(want)},事件里是 {sorted(rolls[-1])}"


def test_roll不许混进表达式那张名表():
    """🟡 **验收 A ⑥ 的反向闸**:`roll` 是**判定原语**,不是作者写得进表达式的
    一个函数。混进 `expressions` 那套(那是**算术**,而且是安全边界)的下场:
    作者在一条规律里写 `roll(...)`,而**同一份日志重放两遍会得到两副骰子** ——
    「对账即重放」当场破。
    """
    from anima_world import expressions

    names = set()
    for attr in ("FUNCTIONS", "ALLOWED_FUNCTIONS", "SAFE_FUNCTIONS", "FUNCS"):
        table = getattr(expressions, attr, None)
        if isinstance(table, dict):
            names |= set(table)
        elif isinstance(table, (set, tuple, list)):
            names |= set(table)
    assert "roll" not in names, f"`roll` 混进了表达式名表:{sorted(names)}"
