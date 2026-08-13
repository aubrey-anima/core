"""玩家的钱有几个答案。

经济的第一条设计是"**账本是投影**":余额和库存没有表,是 `payment` / `item_transfer`
事件折叠出来的,对账 = 重放。玩家钱包却站在这条规矩外面:

- `player_topup` 只改内存里的 `players[pid]["wallet"]`,**不发任何事件**;
- `player_buy` 拿这个内存数做门禁,却把花费**发成 payment 事件**。

于是同一个玩家有两个余额:内存里是"充值 − 花费",账本投影里是"**负的花费**"。
`World.balance()` 读投影,所以一个刚充过钱的玩家在那里显示为负数。两个数谁也不知道
对方存在,而且都不报错。

收敛的方向只能是账本:内存那份重启即失效,而账本是这个引擎对"钱"的定义。
"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world.api import World


@pytest.fixture
def world(tmp_path):
    """账本这几条**从零起**。

    内置世界是**橱窗**,而橱窗自己点亮了 `economy.player_allowance`(60)——
    拿它去验引擎的账本算术,验的是橱窗的布置,而且这几条的数字会跟着橱窗改价而红。
    见面礼那件事由 `test_flagship_seed.py::test_玩家兜里也有钱` 单独守着。
    **必须在他第一次露面之前设**:见面礼落在 `_touch_player` 的第一次那一支上。
    """
    w = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    w.config_set("economy.enabled", "true")
    w.config_set("economy.player_allowance", "0")
    yield w
    w.close()


def _holder(player_id: str) -> str:
    return f"player:{player_id}"


def test_topping_up_lands_in_the_ledger(world):
    world.player_move("p1", "cafe")
    world.player_topup("p1", 50)
    assert world.balance(_holder("p1")) == 50.0, "充值必须走账本,不然它就不存在"


def test_the_wallet_and_the_ledger_agree_after_a_purchase(world):
    world.player_move("p1", "cafe")
    world.player_topup("p1", 50)

    stock = world.shop("cafe")
    if not stock:
        pytest.skip("演示世界的咖啡店没有货架")
    item = stock[0]
    result = world.player_buy("p1", "cafe", item["item_id"])

    assert result["wallet"] == world.balance(_holder("p1")), (
        "买完之后钱包和账本必须是同一个数"
    )
    assert result["wallet"] == pytest.approx(50.0 - item["price"])


def test_a_purchase_you_cannot_afford_is_refused_by_the_ledger(world):
    world.player_move("p1", "cafe")
    stock = world.shop("cafe")
    if not stock:
        pytest.skip("演示世界的咖啡店没有货架")
    with pytest.raises(ValueError):
        world.player_buy("p1", "cafe", stock[0]["item_id"])
    assert world.balance(_holder("p1")) == 0.0, "被拒的交易不该在账本上留下痕迹"


def test_the_balance_survives_a_restart(tmp_path):
    """内存那份重启即失效 —— 而钱是世界的一部分,不是会话的一部分。"""
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        world.config_set("economy.player_allowance", "0")
        world.player_topup("p1", 30)

    with open_world_at(db, force_mock_llm=True) as reopened:
        assert reopened.balance(_holder("p1")) == 30.0


def test_topup_returns_the_same_number_the_ledger_holds(world):
    assert world.player_topup("p1", 10) == world.balance(_holder("p1"))
    assert world.player_topup("p1", 5) == 15.0


# ── 兜里的第一笔钱 ────────────────────────────────────────────────────────────


def test_玩家进门时兜里有世界给的那笔钱(tmp_path):
    """线上现场:晚潮的货架 43 行、15 样能力要的东西一样不缺,而玩家余额恒为 0。

    `player_topup` 是**宿主**的门(充值),不该挂在玩家点得到的地方 —— 玩家自己
    印钱,经济这一层就没有了。缺的是另一件事:他第一次出现在这个世界里时,兜里
    该有多少。那是**世界的作者**的意见,不是引擎的配额,所以它是一个配置项,
    默认 0(= 这个世界不给),声明本身就是开关。
    """
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        world.config_set("economy.player_allowance", "12.5")
        world.player_move("p1", "cafe")
        assert world.balance(_holder("p1")) == 12.5


def test_那笔钱只给一次(tmp_path):
    """花完就是花完了 —— 每次露面补满等于没有代价。"""
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        world.config_set("economy.player_allowance", "12.5")
        world.player_move("p1", "cafe")
        stock = world.shop("cafe")
        if stock:
            world.player_buy("p1", "cafe", stock[0]["item_id"])
        spent = world.balance(_holder("p1"))
        world.player_move("p1", "cafe")          # 再露一次面
        world.player_action("p1", "look", {})
        assert world.balance(_holder("p1")) == spent, "补满了的话,他永远不必掂量"


def test_挂机一刻钟回来不该再领一次(tmp_path):
    """"只给一次"的那个**一次**,从前挂在"在场那一行原先不存在"上。

    3.2.0 把在场从进程内存搬进了带 TTL 的 Redis 键(重启不该让世界忘了人坐在她
    对面)—— 那次搬家没人重新读一遍这句话,而它的含义就此从"他这辈子头一回露面"
    悄悄变成了"他这**一刻钟**里头一回露面"。挂机十五分钟再回来就是又一笔,
    没有上限。线上真的这样:晚潮的账本里 `dogfood-2e7fbb4` 领了**四次** 60 块,
    而账本、日志、屏幕上没有一处说它不对 —— 补满了的钱包不构成代价,货架又成了
    摆设,只是换了个方向(和上面那条 `只给一次` 是同一句话的两半)。

    所以判据要挂在一件**不会过期**的事上:账本自己。见面礼是账本上的一笔,而
    账本是全量重放折出来的投影 —— 折过一次就永远记得。

    ⚠️ 上面那条 `test_那笔钱只给一次` 单独跑是绿的:它连着露两次面,而那两次
    之间在场那一行一直在。**分不开的正是这一格** —— 差别只有"中间过没过期"。
    """
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        world.config_set("economy.player_allowance", "12.5")
        world.player_move("p1", "cafe")
        assert world.balance(_holder("p1")) == 12.5

        world.presence_store.expire_now("p1")   # 一刻钟过去了,他被忘在门外
        world.player_move("p1", "cafe")         # 他回来了
        assert world.balance(_holder("p1")) == 12.5, "挂机就能再领一次"

        # 再来一轮 —— 一次巧合和一个漏斗要分得开。
        world.presence_store.expire_now("p1")
        world.player_action("p1", "look", {})
        assert world.balance(_holder("p1")) == 12.5


def test_默认不给钱(tmp_path, bare_seed):
    """引擎默认值 = 没人说话时的样子。不写 `economy.player_allowance` 的世界
    行为与从前逐位相同 —— 兜里空的。

    **必须用毛坯**:内置世界是橱窗,它自己点亮了这一格,拿它验默认值等于在验
    橱窗的布置(而且改一次橱窗的数就红一次)。
    """
    with open_world_at(str(tmp_path / "w.db"), world_file=bare_seed,
                       force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        world.player_move("p1", "cafe")
        assert world.balance(_holder("p1")) == 0.0


# ── 买东西也得人在场 ──────────────────────────────────────────────────────────


def test_隔着半个镇子买不到东西(world):
    """和能力调用上那道 `absent` 闸是同一条:**一次交易是一个人、一个地方、
    一个瞬间**。放开的话玩家站在渡口就能刷光全镇的货架,而这个世界里"走过去"
    本身就是一段代价。

    拒绝**两头都说** —— 只说"它在铁匠巷"会读成一句谎:真正的原因可能是他压根
    还没告诉过世界他在哪。
    """
    world.player_move("p1", "cafe")
    world.player_topup("p1", 50.0)
    stock = world.shop("cafe")
    if not stock:
        pytest.skip("演示世界的咖啡店没有货架")
    world.player_move("p1", "workshop")
    with pytest.raises(LookupError) as caught:
        world.player_buy("p1", "cafe", stock[0]["item_id"])
    said = str(caught.value)
    assert "cafe" not in said, f"地点 id 漏给玩家了:{said!r}"
    assert world.balance(_holder("p1")) == 50.0, "被拒的交易不该在账本上留下痕迹"


# ── 一屏:他脚下卖什么、他有多少钱、他带着什么 ────────────────────────────────


def test_货架这一屏说得出每一样买不买得起(world):
    """`shop()` 给的是货架,`balance()` 给的是余额 —— 两个都问到了,拼出"这个
    按钮为什么是灰的"仍然是宿主的活,而那正是上一轮把整套经济做丢的那根线。

    判据和 `player_options` 逐字同构:`available` + 分得开的 `reason` + 一句
    引擎已经写成人话的 `refusal`。合成一个"现在不能",玩家会挨样点过去。
    """
    world.player_move("p1", "cafe")
    screen = world.player_shop("p1")
    assert screen["balance"] == 0.0
    assert screen["location"] and screen["location_name"], "地名要给人话"
    if not screen["shelf"]:
        pytest.skip("演示世界的咖啡店没有货架")
    row = screen["shelf"][0]
    assert row["name"], "东西要有名字 —— 印 id 是上一轮修掉的那类 bug"
    assert row["available"] is False and row["reason"] == "broke"
    assert "还差" in row["refusal"], f"要说出还差多少:{row['refusal']!r}"

    world.player_topup("p1", 500.0)
    row = world.player_shop("p1")["shelf"][0]
    assert row["available"] is True and row["reason"] == "" and row["refusal"] == ""


def test_随身物品这一屏也说人话(world):
    world.player_move("p1", "cafe")
    world.player_topup("p1", 50.0)
    stock = world.shop("cafe")
    if not stock:
        pytest.skip("演示世界的咖啡店没有货架")
    world.player_buy("p1", "cafe", stock[0]["item_id"])
    carried = world.player_shop("p1")["carrying"]
    assert carried and carried[0]["quantity"] == 1
    assert carried[0]["name"] and carried[0]["name"] != carried[0]["item_id"]


def test_人不在任何地方时这一屏不撒谎(world):
    """他还没告诉过世界他在哪 —— 那就没有货架,而不是一个空店铺。"""
    world.player_action("p1", "look", {})       # 露过面,但没走过路
    screen = world.player_shop("p1")
    assert screen["location"] == "" and screen["shelf"] == []


# ── 路上的人没有货架 ─────────────────────────────────────────────────────────
#
# `_where_is` 早就把这个答案定死了:「在路上就是"不知道"(空串)—— 而不是"还在
# 出发地"」,而它那句话下面紧跟着一行警告:两处各写一遍,迟早一处认为在路上还
# 算在原地。买卖这两扇门就是那"另一处" —— 它们问的是 `player_location()`
# (那一条**有意**答"还算在出发地",`player_walk` 要拿它算路费),于是同一个人在
# 同一时刻,能力那条路上"不在",货架这条路上"在"。


def test_路上的人看不见他刚走开的那个货架(world):
    """他一边在路上,一边看着他已经离开的那家店的货 —— 而那一屏还同时诚实地
    写着 `in_transit: true`。一屏之内自相矛盾,玩家信哪个都是错的。
    """
    world.player_move("p1", "cafe")
    world.player_topup("p1", 500.0)
    if not world.shop("cafe"):
        pytest.skip("演示世界的咖啡店没有货架")
    world.player_walk("p1", "workshop")
    screen = world.player_shop("p1")
    assert screen["in_transit"] is True, "先得真的在路上,不然这条什么也没验"
    assert screen["location"] == "" and screen["location_name"] == ""
    assert screen["shelf"] == [], "路上的人不该看见任何一个货架"
    assert screen["balance"] == 500.0, "钱包和随身物品照常 —— 那几样跟着他走"


def test_路上的人买不到他刚走开的那个货架上的东西(world):
    """和 `test_隔着半个镇子买不到东西` 是同一条闸的另一半:那一条验的是他站在
    别处,这一条验的是他谁那儿都不站着。走开这个动作要真的把货架关上,否则
    "走过去"那段路费白付了 —— 他大可以起步之后原地把全镇刷完。
    """
    world.player_move("p1", "cafe")
    world.player_topup("p1", 500.0)
    stock = world.shop("cafe")
    if not stock:
        pytest.skip("演示世界的咖啡店没有货架")
    world.player_walk("p1", "workshop")
    with pytest.raises(LookupError) as caught:
        world.player_buy("p1", "cafe", stock[0]["item_id"])
    said = str(caught.value)
    assert "cafe" not in said and "workshop" not in said, f"地点 id 漏给玩家了:{said!r}"
    assert "路上" in said, f"得说出他为什么买不了:{said!r}"
    assert world.balance(_holder("p1")) == 500.0, "被拒的交易不该在账本上留痕"


def test_到了地方货架就回来了(world):
    """闸落在"在路上",不是落在"走过路" —— 分不开的话他这辈子只买得成第一次。"""
    world.player_move("p1", "cafe")
    world.player_topup("p1", 500.0)
    if not world.shop("cafe"):
        pytest.skip("演示世界的咖啡店没有货架")
    world.player_walk("p1", "workshop")
    assert world.player_shop("p1")["shelf"] == []
    while world.player_in_transit("p1"):
        world.scheduler.tick()
    screen = world.player_shop("p1")
    assert screen["in_transit"] is False and screen["location"] == "workshop"


# ── 空货架说得出为什么空 ─────────────────────────────────────────────────────
#
# 三件事在数据上长得一模一样(`shelf: []`),而玩家的下一步完全不同。这句话从前
# 没有主人:站点那块面板自己写了三句,而姊妹面板(能力)一句都没写,于是同一个
# 赶路的人一边读到「你还在路上」、一边读到 `in_transit`。`blocked_text` 那一半
# 已经收进引擎了,这是另一半。


def test_空货架的三个理由分得开而且各配一句话(world):
    world.player_action("p1", "look", {})       # 露过面,但没走过路
    screen = world.player_shop("p1")
    assert screen["empty"] == "unknown_player_location"
    assert "站过去" in screen["note"], f"没说出他下一步该干什么:{screen['note']!r}"

    world.player_move("p1", "cafe")
    if not world.shop("cafe"):
        pytest.skip("演示世界的咖啡店没有货架")
    world.player_walk("p1", "workshop")
    screen = world.player_shop("p1")
    assert screen["empty"] == "in_transit"
    assert "路上" in screen["note"], f"没说出他为什么看不到货:{screen['note']!r}"

    while world.player_in_transit("p1"):
        world.scheduler.tick()
    screen = world.player_shop("p1")
    if screen["shelf"]:
        pytest.skip("演示世界的工坊也摆着货 —— 这一格验不到")
    assert screen["empty"] == "no_shop_here", "「这儿不卖东西」和「你在路上」是两件事"
    assert screen["note"]


def test_货架上有货的时候不挂一句话(world):
    """有东西可买还写着"这儿没有卖东西的",比不说更坏。"""
    world.player_move("p1", "cafe")
    if not world.shop("cafe"):
        pytest.skip("演示世界的咖啡店没有货架")
    screen = world.player_shop("p1")
    assert screen["shelf"] and screen["empty"] == "" and screen["note"] == ""


def test_货架空着的每个理由都配了一句话():
    """新加一个 `empty` 枚举而忘了配话,玩家面前就是一块什么也没写的空板子 ——
    而那一天没有任何一处会报错。和 `_BLOCKED_WORDS` 那条逐字同构。
    """
    from anima_world.api import World

    for reason, said in World._SHOP_WORDS.items():
        assert said and reason not in said, f"{reason} 这一格还是引擎的话:{said!r}"


# ── 钱有最小单位 ─────────────────────────────────────────────────────────────


def test_余额永远是整分(world):
    """线上现场:一个玩家买了六样东西之后,接口回给他的余额是
    `0.3799999999999921`。

    前端 `toFixed(2)` 把它盖住了,所以**屏幕上看不出来** —— 而账本是这个引擎对
    "钱"的定义,不是屏幕上那一行。二进制浮点存不下 0.1,于是每一笔减法都掉一点
    渣;账面上"余额 < 价钱"这道门禁最终会在一个正好够的数上给出错误答案,
    而那一次不报错、不留痕。

    钱有最小单位,所以**折账的时候就进位**,不是显示的时候。两边一起进 ——
    单边进的话镇上那个户头和玩家兜里的钱加起来不再守恒。

    ⚠️ 内置世界的价钱全是整数,拿它买三样东西验不出这件事(线上那些价钱是
    价格漂移出来的 `5.23` / `1.16`)。所以这儿直接往账本上折带分的数 ——
    验的是折账那一步,不是货架。
    """
    world.player_topup("p1", 0.1)
    world.player_topup("p1", 0.1)
    world.player_topup("p1", 0.1)
    left = world.balance(_holder("p1"))
    assert left == 0.3, f"余额掉渣了:{left!r}"


def test_一分钱都没蒸发(world):
    """账本是投影,所以它必须**守恒**:玩家少的正是镇上多的。进位只进一边的话
    这条当场破 —— 而破了之后没有任何一处会报错。"""
    from anima_world import economy
    world.player_move("p1", "cafe")
    world.player_topup("p1", 60)
    stock = world.shop("cafe")
    if not stock:
        pytest.skip("演示世界的咖啡店没有货架")
    before = world.balance(economy.TOWN)
    world.player_buy("p1", "cafe", stock[0]["item_id"])
    spent = round(60.0 - world.balance(_holder("p1")), 2)
    assert round(world.balance(economy.TOWN) - before, 2) == spent


# ── 拒绝的措辞归引擎,而它必须原样到得了玩家眼前 ──────────────────────────────


def test_卖完了那句话不带引号(world):
    """`KeyError.__str__` 是 `repr(args[0])` —— 全 Python 独此一家。于是引擎写好的
    人话到了玩家屏幕上变成 `'小念咖啡车没有一杯拿铁了'`,**带着一对单引号**。

    线上真的这样:世界壳照规矩 `str(exc)` 原样传,一个字没改,而它就是错的。
    所以规矩要再紧一格:**玩家读得到的拒绝,不许坐 `KeyError` 这班车。**
    "这儿没有" 是 `LookupError`(和"你不在这儿"同一类:都是"这条路走不通"),
    "钱不够" 是 `ValueError`。
    """
    world.player_move("p1", "cafe")
    world.player_topup("p1", 500)
    stock = world.shop("cafe")
    if not stock:
        pytest.skip("演示世界的咖啡店没有货架")
    item = stock[0]
    for _ in range(int(item["quantity"])):
        world.player_buy("p1", "cafe", item["item_id"])
    with pytest.raises(LookupError) as caught:
        world.player_buy("p1", "cafe", item["item_id"])
    said = str(caught.value)
    assert not said.startswith("'"), f"引号漏给玩家了:{said!r}"
    assert item["name"] in said and item["item_id"] not in said, said


def test_世界不认识的人收到的也是人话(world):
    """他有一张会员卡,但一次都没在世界里落过脚。从前这里是
    `KeyError("player p9 not present")` —— 英文、带 uuid、带引号,三样全中。"""
    with pytest.raises(LookupError) as caught:
        world.player_buy("p9", "cafe", "coffee")
    said = str(caught.value)
    assert not said.startswith("'") and "p9" not in said, said
