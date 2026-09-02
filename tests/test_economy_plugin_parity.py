"""§9.2 那道闸,给 **economy** 的那一份 —— **基线在这儿,搬家还没开始。**

这份文件此刻**验的是旧路对自己**:同一份橱窗世界、同一批 tick、同一串动作,
今天的 `economy.py` + `api.py` 那 23 处 + scheduler 那几处,给出的是
`tests/data/economy_legacy_golden.json` 里那份东西。

🔴 **为什么在搬家之前就把它落盘,而不是搬的时候顺手采一份。**

`tests/data/needs_legacy_golden.json` 那一份是这么来的,而它的价值在旧路被删掉的
那一刻才兑现:**旧路删掉之后,这份文件是"从前那个世界长什么样"唯一的证据。**
反过来,如果等到插件路写好了再采基线,采到的就是**插件路自己**——
一道拿被测对象当标准答案的闸,它永远是绿的,而且它绿的时候什么也没说。

**改这份基线之前先问一句:是行为该变,还是我把它改坏了。**

## 这道闸比什么,以及有意不比什么

和 needs 那一份逐字同一张表(理由写在 `test_needs_plugin_parity.py` 的模块
docstring 里,不在这儿重复一遍 —— 两份说法迟早会分岔):

| | 比吗 |
|---|---|
| `state()` 刨掉 `narrative_log` / `recent_events` / `runtime` | ✅ sha256 逐字节 |
| 三个角色的 `debug_prompt()` | ✅ sha256 逐字节 |
| 每个 owner 的量表 | ✅ 逐位 |
| **钱包与随身库存**(`balance` / `inventory`,事件投影) | ✅ 逐位 |
| **货架**(`shop`,真表 `shop_stock`) | ✅ 逐格 |
| **玩家那一屏**(`player_shop`) | ✅ 逐格 |
| 事件日志(滤掉 `narrative`,按**多重集**比) | ✅ 逐条 |
| `narrative` 那一支 / 次序 | ❌ 线程池,不由这份代码决定 |

## 🔴 有意**不比**什么 —— 而这句话和"比过了"在闸红的那天意思完全不同

**2d-① 只搬了钱包一格**(补裁 ⑤,FOR-STUDIO §3.43)。所以下面这几样
**不是被这道闸验过了,是这一轮一行没动**:

- **`World.player_buy` 那五句手写中文拒绝**(在路上 / 没落脚 / 不在这儿 /
  卖光了 / 钱不够)。它们和 `perform_affordance` 那四类回执**形状与文字都对不上**,
  而玩家那一屏印的就是这些话 —— 动词化 = 换调用路,**换了路的"逐字节相同"
  从原理上不成立**。等第 3 期。
- **`eat` 那条 BT 路**(`scheduler._handle_eat_purchase`,tick 循环里
  `action.kind == "eat"`)。它根本不在 `interact` 那条路上,同上。
- **货架**(`anima:{world_id}:shop_stock` 那个真 hash)。搬它是**换掉一个真键**,
  而钱包那一格是**加法**;老包装进换了键的新引擎会**原样落键、没有一处再读它**。
- **`economy.enabled` 的语义**(只挡 eat / wages,不挡 buy / give)—— 一个字没动。

🔴 **还有一样是"搬不动",不是"没搬"**:`Projection.balances` 是一本按**任意持有者
字符串**记的账(`__town__` / `shop:cafe` 也在里面),而插件的事实住在**有类型的
载体**上(`bearer`)。**镇上的金库和店铺是持有者,不是载体** —— 所以
`balance()` 这一轮照旧读账本,没有改成读那个事实,而那不是偷懒:
把它们塞成 `agent:__town__` 会在世界里凭空建出一个没有人的人。

## 🔴 中途做一件事,不只比末态

第 1 期验收 A 逮到的那条 P1 就死在这儿:parity 只比 288 tick 的末态,而
「吃一碗面」写进黑板后**下一 tick 被派生值盖掉** —— 末态照旧逐位相同,
因为 mock 路上那顿饭要么没发生、要么被淹没。

所以这一份的场景是**三段**:跑一段 → **中途真的动几下钱和东西**
(玩家进城、看一眼货架、买一件、再看一眼)→ 再跑一段。
末态相同**且**这几下的回执逐格相同,才算数。

## 🔴 「给东西」这一下**不在**这道闸里,而这是采基线时才发现的

任务单 §1 的侦察写着「`World.give_item`(`api.py:1079`,只玩家→NPC)」——
**那句话把 owner 说错了一层**。实测(`ast` 数 `api.py` 的类成员):

    _ToolRuntime.give_item  line 1104
    World.balance / player_buy / player_shop   line 7401 / 7471 / 7542

`give_item` 是**聊天工具**那条路上的方法,`World` 门面上根本没有它 ——
照那句话敲 `world.give_item(...)` 拿到的是 `AttributeError`。

这一格的后果不在这道闸上,在**第 2 期 2d** 上:那一期要把 `give` 复刻成插件动词,
而它今天的调用路是「她/他在聊天里用了一个工具」,不是一次公开 API 调用。
**要给它一道 parity 闸,得先能确定性地驱动那条工具路** —— 那是 2d 自己的第一件事,
不该在这里用一个 `AttributeError` 冒充"我验过了"。**所以这一下这里不比,并说出来。**

## 这道闸试过牙,而**第一次试的两下都是假的**

新加的闸也会假绿,所以落盘之前动了真的:

| 动了什么 | 结果 |
|---|---|
| `scheduler.py` 里那句 `wage = 20.0` → `20.01` | 🟢 **全绿 —— 而这是一次假的试验** |
| `economy.drift_price` 的默认参数 `k=0.08` → `0.0801` | 🟢 **同样是假的** |
| `config_store._DEFAULTS["economy.daily_wage"]` `20.0` → `20.01` | 🔴 钱包 + 事件日志两条红,差在 `3.26` vs `3.27` |
| `economy.drift_price` 的**返回值** `+ 0.01` | 🔴 钱包 / 货架 / 中途回执 / 事件日志**四条**红 |

**头两下为什么是假的:改到的都是死掉的默认值。** `wage = 20.0` 那一行下一句就是
`config_store.get("economy.daily_wage", default=wage)` —— 引擎自己的 `_DEFAULTS` 里有
这个键,所以那个字面量永远不被读到;`drift_price` 的 `k` 同理,调用点
(`redis_state.py:2214`)不传它,但传的是别的位置参数,改默认值一样够不着。

**记在这儿是因为这正是"新加的闸也会假绿"的另一半**:我以为我在试这道闸的牙,
其实我在试一段死代码 —— 而屏幕上给出的信号("改了还是绿")和"这道闸没牙"
一模一样。**试牙也要试对地方,而"改一个数看它红不红"不保证你改的那个数在跑。**

⚠️ 顺带一格实况:那四下里 `prompt_sha` / `state_sha` / `stocks` **一次都没红** ——
**这是对的,不是漏了**:钱今天只进规划上下文,不进感知块、不进量表
(任务单 §1 侦察实测)。哪天它们跟着红了,那说明搬家把钱送进了提示词,
而那是行为变更,不是搬家。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import time

from _worldfile import open_world_at, redis_for

GOLDEN_PATH = pathlib.Path(__file__).parent / "data" / "economy_legacy_golden.json"
GOLDEN = json.loads(GOLDEN_PATH.read_text())

#: `state()` 里由**线程池**喂的那三格 —— 对未改动的引擎也不可复现。
# 🔴 **`player_join` 也滤掉,而这不是"金线红了就把它加进白名单"。**
# 这两条金线量的是一件很具体的事:**把 needs / economy 从内核搬成出厂插件,行为
# 一个字节都没变**。基线是从**旧路那棵树**采的,而 3.9.0 给引擎加了一条全新的事件
# (`player_join`:他第一次走进这个世界那一天,`for_each: {"node":"player"}` 的剧情拍
# 拿它当零点)—— 那是一次**独立的、有意的**行为变更,不是搬家搬出来的偏差。
# ⚠️ **不能靠"重采基线"化解**:基线记的是旧路的行为,而旧路的代码已经删了 ——
# 拿 HEAD 重采一遍,这道闸就变成 HEAD 和 HEAD 比,永远绿而且什么都不测。
# 所以滤,而且**只滤这一种**:多一条别的、少一条、payload 差一个字节,照旧当场红。
# 🆕 **`pack_installed` 同理(3.10.0)**:橱窗从这一版起自带一条 `pack` 记录
# (「做了却开箱看不见等于没做」),于是每个从橱窗建起来的世界创世那一趟多一条
# `pack_installed`。它和 `player_join` 逐字同一种情形 —— **一次独立的、有意的
# 行为变更,不是搬家搬出来的偏差**,而基线所在的那棵旧路的树里根本没有这个事件。
# ⚠️ 同样不能靠重采化解,理由和上面那句逐字相同。
_PARITY_IGNORED_EVENTS = ("narrative", "player_join", "pack_installed")

ASYNC_KEYS = ("narrative_log", "recent_events", "runtime")

#: 🔴 **第四样不可复现的东西,而它不是线程池:`players[*].last_seen` 是一个墙钟。**
#:
#: 实测两趟差 2.0027 秒(`1787795653.66` vs `1787795655.66`)—— 它记的是"这个人
#: 最后一次被看见的真实时刻",跟这份代码一点关系都没有。**采基线的第一趟就栽在
#: 它上面**,而屏幕上给出的信号是「`state_sha` 不可复现」,长得和"这个世界本来
#: 就没有确定性"一模一样。
#: ⚠️ **剥它,不是放宽 sha**:放宽的话整个 `players` 段跟着一起豁免掉,而那一段
#: 里还有 `location` / `in_transit` —— 玩家在不在场、在不在路上,恰恰是这道闸最该
#: 盯住的东西之一(`player_shop` 的货架就按它给)。
WALL_CLOCK_KEYS = ("last_seen",)

#: 玩家在这一趟里的名字。**写死** —— 它进事件、进回执、进那一屏。
PLAYER = "parity"

_RUNS = [0]


def _quiesce(world, *, timeout: float = 30.0) -> None:
    """等线程池把手上的活干完再采样。理由与判据见 needs 那一份的 `_quiesce`。"""
    log = world.scheduler.event_log
    deadline = time.monotonic() + timeout
    stable, last = 0, -1
    while time.monotonic() < deadline:
        now = log.max_seq()
        stable = stable + 1 if now == last else 0
        if stable >= 5:
            return
        last = now
        time.sleep(0.05)
    raise AssertionError("等了 30 秒线程池还没静下来 —— 这道闸比的是静止态")


def _sha(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def _comparable_state(state: dict) -> dict:
    """`state()` 里**这份代码决定得了**的那一部分。见 `ASYNC_KEYS` / `WALL_CLOCK_KEYS`。"""
    out = {k: v for k, v in state.items() if k not in ASYNC_KEYS}
    players = out.get("players")
    if isinstance(players, dict):
        out["players"] = {
            pid: {k: v for k, v in (row or {}).items() if k not in WALL_CLOCK_KEYS}
            for pid, row in players.items()
        }
    return out


def run(tmp_path) -> dict:
    """跑一趟,采下可复现的那几样。**采基线和跑闸走的是这同一个函数。**

    ⚠️ **每一趟一个全新的世界**(`redis_for` 是"同一路径 = 同一个 fakeredis")——
    共用路径的话第二趟接着第一趟那个已经跑完的世界跑,而那不是"不可复现",
    是根本没有重跑。
    """
    _RUNS[0] += 1
    db = tmp_path / f"econ-parity-{_RUNS[0]}.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        world.config_set("economy.enabled", True)
        world.config_set("planner.enabled", False)
        world.config_set("autonomy.enabled", False)
        # 🔴 **线程池上的两样都关掉,而这不是"图省事"。**
        #
        # 叙事与关系判定跑在 `ThreadPoolExecutor` 上("时钟永不等网络"),它们的
        # 产物**落在第几 tick 由这台机器此刻忙不忙决定**。needs 那道闸只关了叙事,
        # 于是它在忙的时候会红 —— 而红的那句话指控的是被测的东西
        # (2026-08-26 实测:同一份代码连采两趟,提示词差 12 个字符,
        # 而记忆一条不差)。**一条时好时坏的闸比没有更贵:它红的时候没人知道
        # 该不该信。**
        #
        # ⚠️ **关掉它们不等于放宽这道闸**:economy 那一摞(钱、货架、库存、
        # 那一屏)全跑在 tick 线程上,一处也不经这两个池 —— 关掉之后这道闸比的
        # 仍然是搬家会碰到的每一样东西,只是不再顺带比机器的心情。
        # ⚠️ 反过来说:**这道闸因此验不了"搬家有没有改到关系"** —— economy 不碰
        # 四轴(侦察实测),所以这一格今天是空的;哪天它碰了,得另开一道闸,
        # 而不是把这两个池打开(打开就回到时好时坏)。
        world.scheduler.narrative = None
        world.scheduler.relationship_judge = None

        world.tick(GOLDEN["ticks_before"])

        # ── 中途真的动几下 ────────────────────────────────────────────────
        # 末态相同 ≠ 中途相同(第 1 期验收 A 那条 P1)。这几下的**回执**逐格进闸。
        moves: dict[str, object] = {}
        agents = sorted(world.scheduler.agents)
        here = world.state()["locations"][0]["id"]
        world.player_move(PLAYER, here, display_name="对账的人")
        moves["shop_before"] = world.player_shop(PLAYER)
        shelf = world.shop(here)
        if shelf:
            item = shelf[0]["item_id"]
            try:
                moves["buy"] = world.player_buy(PLAYER, here, item)
            except Exception as exc:            # noqa: BLE001 - 回执也是账
                moves["buy"] = {"error": type(exc).__name__, "detail": str(exc)}
        moves["shop_after"] = world.player_shop(PLAYER)
        world.tick(1)
        moves["shop_next_tick"] = world.player_shop(PLAYER)

        world.tick(GOLDEN["ticks_after"])
        _quiesce(world)

        holders = sorted(
            set(agents) | {f"player:{PLAYER}", "__town__"}
            | set(world.scheduler._memory_projection.balances)
            | set(world.scheduler._memory_projection.inventories)
        )
        return {
            "moves": moves,
            "balances": {h: world.balance(h) for h in holders},
            "inventories": {h: world.inventory(h) for h in holders},
            "shops": {loc["id"]: world.shop(loc["id"])
                      for loc in world.state()["locations"]},
            "player_shop": world.player_shop(PLAYER),
            "stocks": {o: world.stocks(o) for o in sorted(world.stock_owners())},
            "prompt_sha": {a: _sha(world.debug_prompt(a)) for a in agents},
            "state_sha": _sha(_comparable_state(world.state())),
            "events": sorted(
                [e.type, json.dumps(e.payload, ensure_ascii=False, sort_keys=True)]
                for e in world.scheduler.event_log.replay() if e.type not in _PARITY_IGNORED_EVENTS
            ),
        }


def test_钱包与随身库存逐位相同(tmp_path):
    """账本是 `payment` / `item_transfer` 事件的投影 —— **对账即重放**。
    搬成插件之后这句话必须还成立,而"还成立"唯一可验的意思就是这两个数没变。"""
    got = run(tmp_path)
    assert got["balances"] == GOLDEN["balances"], "钱变了"
    assert got["inventories"] == GOLDEN["inventories"], "随身的东西变了"


def test_货架与玩家那一屏逐格相同(tmp_path):
    got = run(tmp_path)
    assert got["shops"] == GOLDEN["shops"], "货架变了"
    assert got["player_shop"] == GOLDEN["player_shop"], "玩家那一屏变了"


def test_中途那几下的回执逐格相同(tmp_path):
    """🔴 **末态相同 ≠ 中途相同。**

    第 1 期验收 A 逮到的那条 P1 正是这个形状:parity 只比末态,而中途那件事被
    下一 tick 的派生值盖掉了 —— 末态照旧逐位相同,因为它压根没留下痕迹。
    """
    got = run(tmp_path)["moves"]
    assert got == GOLDEN["moves"], "买 / 给 / 那一屏,中途某一下的回执变了"


#: 这一轮**搬过去**的那几个键 —— 量表上唯一允许多出来的东西。
#:
#: 🔴 **逐键放行,不许写成「多出来的忽略掉」**(照 needs 那次的写法):
#: 那样一个真的改坏了旧量的改动会从这条缝里溜过去。
#: 每加一格搬家就往这儿加一行,而**加行的时候要问一句:它真的是搬家吗。**
MOVED_KEYS = frozenset({"economy.coins"})


def test_量表是旧的那份_外加搬过来的那一个(tmp_path):
    """**这一条是 2d-① 唯一一处"预期会变",而变的形状要说死。**

    钱包从「只住在事件投影里」变成「量表里也有一格物化视图」,所以量表必然多
    `economy.coins` 一个键 —— 那**就是**这次搬家本身。**除此之外一个字节都不许动。**
    """
    got, want = run(tmp_path)["stocks"], GOLDEN["stocks"]
    assert sorted(got) == sorted(want), (
        "量表的 owner 名单变了 —— ⚠️ 最可能的原因是折出来的账按**任意持有者**记"
        "(`__town__` / `shop:cafe`),而它们被凭空建了一行量表:世界里没有这个人"
    )
    for owner in sorted(want):
        extra = set(got[owner]) - set(want[owner])
        assert extra <= MOVED_KEYS, f"{owner} 上多出了搬家之外的量:{sorted(extra - MOVED_KEYS)}"
        assert not set(want[owner]) - set(got[owner]), f"{owner} 上少了量"
        for key, value in want[owner].items():
            assert got[owner][key] == value, f"{owner}.{key} 变了(旧量不许动)"


def test_钱包那一格和账本逐位相同(tmp_path):
    """🔴 **搬过来那个数,必须**等于**账本折出来的那个数。**

    两处不一致的下场:规划上下文读账本、她的表达式读量表,而两边都不报错 ——
    正是这个仓库最怕的"两份真相里有一份不更新"。
    ⚠️ 进位也得一样:账本 `_apply_payment` **每一步折到两位**,所以那个事实
    声明了 `round: 2`。折到六位的话它们只在小数第三位往后分家。
    """
    got = run(tmp_path)
    for holder, money in GOLDEN["balances"].items():
        owner = f"agent:{holder}"
        if owner not in got["stocks"]:
            # `__town__` / `shop:cafe` 是**持有者**,不是**载体** —— 它们没有量表,
            # 所以这一格搬不过去。见下面文件头那一段。
            continue
        assert got["stocks"][owner].get("economy.coins") == money, (
            f"{holder}:量表 {got['stocks'][owner].get('economy.coins')} "
            f"≠ 账本 {money}"
        )


def test_提示词逐字节相同(tmp_path):
    """钱**只进规划上下文**,不进感知块 —— 所以这一条今天该是"一个字都不变"。
    ⚠️ 搬成插件之后如果它红了,先问的不是"哪儿算错了",而是
    **"我是不是让钱进了提示词"** —— 那是行为变更,不是搬家。"""
    assert run(tmp_path)["prompt_sha"] == GOLDEN["prompt_sha"], "提示词变了"


def test_state刨掉线程池那三格之后逐字节相同(tmp_path):
    assert run(tmp_path)["state_sha"] == GOLDEN["state_sha"], (
        f"`state()` 变了(已刨掉 {list(ASYNC_KEYS)} 与墙钟 {list(WALL_CLOCK_KEYS)})"
    )


def test_非叙事事件逐条相同_按多重集(tmp_path):
    """**逐条精确相等,只排除次序** —— 少一条、多一条、payload 差一个字节都红。
    次序由线程池决定,不由这份代码决定(完整那三段在 needs 那份的 docstring 里)。"""
    got = run(tmp_path)["events"]
    want = [list(row) for row in GOLDEN["events"]]
    assert got == want, "事件日志的多重集变了"


def test_基线自己是可复现的(tmp_path):
    """**这条守的是这道闸本身。** 同一份代码连跑两趟必须给同一个答案 ——
    不然上面几条红起来时,分不出是搬家搬坏了还是这道闸自己在抖。"""
    assert run(tmp_path) == run(tmp_path)


def test_钱包是投影_清掉物化值重放还能回来(tmp_path):
    """🔴 **projected 那道牙,给钱包这一格再验一遍。**

    量表里那个 `economy.coins` 只是**物化视图** —— 真相是日志里那一串 `payment`。
    抹掉它、重开一次,它必须从账本折回来。**一个直接写的余额做不到这件事**,
    而做不到的样子是"抹掉就是抹掉了"。
    """
    from _worldfile import open_world_at, redis_for

    db = tmp_path / "econ-teeth.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        world.config_set("economy.enabled", True)
        world.tick(4)
        owner = next(o for o in world.stock_owners()
                     if o.startswith("agent:") and "economy.coins" in world.stocks(o))
        had = world.stocks(owner)["economy.coins"]
        assert had, "这个世界里没人有钱,这条用例什么也没验到"
        world.scheduler.stock_store.set_many(
            owner, {"economy.coins": 0.0}, tick=int(world.scheduler.clock))
    with open_world_at(str(db), force_mock_llm=True) as world:
        assert world.stocks(owner)["economy.coins"] == had, (
            "抹掉物化视图之后没从账本折回来 —— 那这一格根本不是投影"
        )
        # 而且它**等于账本**:两处不一致的下场是规划上下文读一个、
        # 她的表达式读另一个,而两边都不报错。
        assert world.balance(owner.split(":", 1)[1]) == had
