"""内置世界是**橱窗**:装上包、开箱,就该看见这个引擎能干的事。

这一整个文件盯的是一件事:**新特性不许做完就藏起来**。1.3.0 加了 stance / 能力 /
意图分派 / 定时轮次一大堆东西,而内置种子此前连 config 字段都不支持 —— 于是
`anima-world start` 看到的还是 1.0 那个"只会走路说话"的世界,新用户完全看不到
这几版的成果。做了等于没做。

所以这些断言是**产品断言**,不是引擎断言(引擎默认值全关,那部分在 conftest 的
素配种子上验)。橱窗里少一件展品,这里就该红。
"""
from __future__ import annotations

from _worldfile import bundled_seed, open_world_at

import json
from importlib import resources

import pytest

from anima_world.api import World
from anima_world.world_seed import is_valid_world_seed


def _bundled_seed() -> dict:
    return bundled_seed()


@pytest.fixture
def flagship(tmp_path):
    """开箱世界:不指定种子 = 用内置的那一份。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    yield world
    world.close()


def test_the_bundled_seed_is_still_a_valid_seed():
    """富化不能把它写成一份非法种子 —— 那会让开箱直接降级到硬编码默认世界。"""
    assert is_valid_world_seed(_bundled_seed())


def test_out_of_the_box_the_showcase_features_are_lit(flagship):
    """开箱即见:这些开关由种子点亮(引擎默认值仍然是关的)。"""
    for flag in (
        "needs.enabled",         # 会累、会饿、会去睡
        "economy.enabled",       # 有钱、有货架、发工资
        "social.enabled",        # 八卦与小团体
        "chat.stance.enabled",   # 她说话背后有关系性意图
        "chat.tools.enabled",    # 她能走开 / 静音 / 拒谈
        "chat.intent.enabled",   # 导演场景 / 改对话规则
        "autonomy.enabled",      # 没人说话时她也会主动
        "contact.enabled",       # 你不在的时候她也会想起你
        "social.hearsay_reaction.enabled",   # 听到关于你的闲话之后她有反应(吃醋)
    ):
        assert flagship.config_get(flag) is True, f"橱窗里 {flag} 没点亮"


def test_橱窗里熬夜真的有代价(flagship):
    """做了却开箱看不见等于没做。这一条要三样一起在,少一样这个机制就是死的:

    量声明了(而且**她自己感知得到** —— 起床气总得有个来源)、两条规律一攒一还、
    以及那个把债接到 `mood` 上的键。只声明量不接消费方,就是又一个"声明过但没人读"。
    """
    agent = next(k for k in flagship.kinds() if k["id"] == "agent")
    debt = next((q for q in agent["quantities"] if q["key"] == "睡眠债"), None)
    assert debt is not None, "橱窗的 agent 没声明睡眠债"
    assert debt["visibility"] == "self", "她自己感觉不到,那起床气从哪儿来"

    rules = {r["id"]: r for r in flagship.rules()}
    assert rules["熬夜攒睡眠债"]["for_each"] == {"not_action": "sleep"}, (
        "写成 {kind: agent} 的话睡着的人也一起欠觉,语义整个反了"
    )
    assert rules["补觉还债"]["for_each"] == {"action": "sleep"}

    assert flagship.config_get("needs.mood_penalty_stock") == "睡眠债", (
        "债攒得起来却接不到她的心气儿上 —— 这个量就没有消费方"
    )


def test_橱窗里两个人真的能一起做点什么(flagship):
    """做了却开箱看不见等于没做。一起做事要三样一起在,少一样这个机制就是死的:

    能力声明了 `participants`、她身上有那个决定"肯不肯"的量、而且**这个世界里真的
    存在一对答应得下来的人**。第三样最容易漏:一个人人都推掉的橱窗,和这一层没接上
    在产物上完全一样。
    """
    tree = next(k for k in flagship.kinds() if k["id"] == "tree")
    sit = next((a for a in tree["affordances"] if a["verb"] == "树下小坐"), None)
    assert sit is not None, "橱窗里没有任何一件一起做的事"
    assert sit["participants"] == {"min": 1, "max": 2}
    assert sit["duration"] > 0, "一起待着要花时间 —— 时长是关系变化的一个因子"

    agent = next(k for k in flagship.kinds() if k["id"] == "agent")
    assert any(q["key"] == "随和" for q in agent["quantities"]), (
        "没有这个量,没配 key 的世界里「肯不肯」就只剩关系一条 —— 而作者调不动它"
    )
    assert flagship.stocks("agent:遥").get("随和") != flagship.stocks("agent:夏").get("随和"), (
        "橱窗里每个人一样随和的话,「别人凭什么答应」就展示不出来"
    )


def test_橱窗里她们真的坐得下来_而且不是谁都肯(flagship):
    """**开箱就要看得见这一层的两面**:有人答应,有人推掉。刻度是照这个世界对过账的
    (见 `together.DEFAULT_MIN_WILLINGNESS` 的长注释)—— 哪天有人调了权重或者改了
    橱窗的关系,这里会红,而那正是该有人再对一次账的时刻。"""
    # 把人搬到树跟前。**同地不是这条测试要验的东西**(那归 test_joint_activity),
    # 而橱窗里三个人本来住在三个地方 —— 走过去要花世界时间。
    for who in ("柔", "遥"):
        flagship.scheduler.agents[who].agent.blackboard.write("loc", "cafe")

    yes = flagship.act(
        "夏", "interact",
        {"target": "tree:harbor_oak", "verb": "树下小坐", "with": ["柔"]},
        surface="body",
    )
    assert yes["ok"] is True, f"橱窗里没有任何一对坐得下来:{yes.get('error')}"

    no = flagship.act(
        "夏", "interact",
        {"target": "tree:harbor_oak", "verb": "树下小坐", "with": ["遥"]},
        surface="body",
    )
    assert no["ok"] is False and no["detail"]["reason"] == "declined", (
        "谁都肯的话,「别人凭什么答应」这条红线在橱窗里就看不见"
    )


def test_开箱第一屏不是三个人在睡觉(flagship):
    """**橱窗的第一帧也是展品。**

    tick 0 是午夜,而新世界跑在演示速度上(1 tick/现实秒)—— 于是新用户装上包看到
    的头一分半钟,是三个角色躺在各自家里。一个世界引擎最没有说服力的一帧。

    旧港因此声明了自己几点开门。挑 14:30 的理由全在这条断言里:三个人的作息表只有
    10:00–15:00 这一段**同时满足**"都醒着"和"各在各的地方做各的事"(夏 守着咖啡店、
    遥 在木工坊、柔 在家画画),而挑它的尾巴是因为 15:00 柔 就动身去咖啡店找 夏 ——
    静止的一屏之后半分钟就有人在走、有两个人凑到一起。

    这条钉的是产物,不是那个数字:哪天有人改了三个人的作息,这里会红,而那正是
    该有人重新挑一次开门时刻的时刻。
    """
    stamp = flagship.world_time()
    assert (stamp.day, stamp.hour) == (0, 14), "橱窗没有声明自己几点开门"

    flagship.tick(1)                     # 行为树跑一帧,每个人才挑得出自己此刻的活
    where = {}
    for agent_id, view in flagship.state()["agents"].items():
        activity = view["activity"]
        assert activity["source"] == "duty", f"{agent_id} 开箱那一刻没事可做"
        assert activity["kind"] != "sleep", f"{agent_id} 开箱那一刻在睡觉"
        where[agent_id] = view["location"]
    assert len(set(where.values())) == 3, f"三个人挤在一处,世界看上去只有一个场景:{where}"


def test_the_loop_stays_off_in_the_showcase(flagship):
    """连续输出**不**默认开:它把每轮的 LLM 调用乘 2~5 倍,而且不是每个世界都要
    这种节奏。橱窗要摆得满,但不能替用户做一个持续烧钱的决定。"""
    assert flagship.config_get("chat.loop.enabled") is False


def test_the_characters_already_know_each_other(flagship):
    """关系不从零开始 —— 否则社交/八卦/聊天 grounding 开箱全是空的。"""
    relations = flagship.scheduler._memory_projection.relations
    assert len(relations) >= 6, "三个人两两互相认识 = 6 个方向"
    for (a, b), rel in relations.items():
        assert rel.r_type, f"{a}→{b} 没有关系描述"
        assert rel.sentiment != 0, f"{a}→{b} 的好感是 0,等于没播"


def test_everyone_has_a_past_to_recall(flagship):
    """记忆是这个引擎的招牌能力,开箱就得有东西可召回。"""
    for agent_id in flagship.scheduler.agents:
        memories = flagship.memories(agent_id)
        assert memories, f"{agent_id} 一条记忆都没有"
        assert any(m.get("anchor") for m in memories), (
            f"{agent_id} 没有一条锚定记忆 —— 那是「她是谁」的底,不该被遗忘曲线冲掉"
        )


def test_the_material_world_is_furnished(flagship):
    """经济点亮了却没东西可买,等于点了个空开关。"""
    shelf = {row["item_id"]: row for row in flagship.shop("cafe")}
    assert "coffee" in shelf, "咖啡店里没有咖啡"
    assert shelf["coffee"]["quantity"] > 0
    for agent_id in flagship.scheduler.agents:
        assert flagship.balance(agent_id) > 0, f"{agent_id} 身上一分钱都没有"
        assert flagship.inventory(agent_id), f"{agent_id} 什么都没带"


def test_玩家兜里也有钱(flagship):
    """**做了却开箱看不见等于没做。**

    货架摆得再满,一个余额恒为 0 的玩家在这个世界里只能当观众 —— 那正是
    2026-08-12 那次真人试玩的现场:43 行货架,15 个"你手上的 X 不够",
    而 X 就在两条街外卖 1 块 2。橱窗必须自己点亮 `economy.player_allowance`,
    因为引擎默认值是 0(没人说话时的样子),而这是**这个世界的作者的意见**。
    """
    flagship.player_move("看客", "cafe")
    screen = flagship.player_shop("看客")
    assert screen["balance"] > 0, "玩家进门时兜里一分钱都没有"
    assert screen["shelf"], "咖啡店的货架对玩家是空的"
    assert any(row["available"] for row in screen["shelf"]), (
        "一样都买不起 —— 见面礼要够买下橱窗里最便宜的那样"
    )


def test_everyone_wants_something(flagship):
    """目标进 planner 的 prompt —— 没有目标的角色只会闲逛。"""
    for agent_id, brain in flagship.scheduler.agents.items():
        assert brain.agent.blackboard.read("goals"), f"{agent_id} 没有目标"


def test_doing_something_costs_her_something(flagship):
    """**一个总能成功的动作产生不了任何决策。**

    橱窗里那棵老橡树原本是个"谁按谁灵"的按钮:照料一次长 5 厘米,不累、不限次、
    谁来做都一样。于是这个世界里没有任何理由挑先做哪件事、没有理由歇一会儿、
    也没有理由变强 —— 而丰富的决策全是从"做不到"里长出来的。

    这条把那一整条链演一遍:她有体力 → 照料要花体力 → 花光了当场做不了 →
    过一天缓过来。断在任何一环,橱窗就退回那个按钮。
    """
    agent_id = next(iter(flagship.scheduler.agents))
    assert flagship.stocks(f"agent:{agent_id}")["体力"] == 100.0

    tended = 0
    while True:
        result = flagship.scheduler.perform_affordance(agent_id, "tree:harbor_oak", "tend")
        if not result["ok"]:
            break
        tended += 1
        assert tended < 50, "她怎么都累不倒 —— 代价没落到她身上"
    assert tended > 1, "她一次都没干成,橱窗展示的是一堵墙不是一个世界"
    # 而"做不了"和"这会儿不行"必须分得开:她该去歇着,不是去等这棵树。
    assert result["reason"] == "incapable", result

    # 她自己知道这件事 —— 不知道的话,她会在一个"以为自己精神得很"的世界里做决定。
    assert flagship.perception(agent_id)["own"]["体力"] < 100.0

    flagship.tick(288)   # 一个世界日
    assert flagship.stocks(f"agent:{agent_id}")["体力"] == 100.0, "缓不过来 = 只能干一天活"


def test_a_no_key_world_still_boots_clean(flagship):
    """开箱**没有 key** 是默认状态,而橱窗点亮了一堆要 LLM 的开关。

    这条钉的是:那些开关在 Mock 上一律降级成无害的空操作,世界照跑 —— 丰富度不能
    以"没配 key 就开不了机"为代价。
    """
    flagship.tick(5)
    assert flagship.world_time().minute_of_day >= 0
    # 定时轮次问过了、也没做什么(Mock 给不出能力标记),而且没有崩
    stats = flagship.autonomy_stats()
    assert stats["failed"] == 0 or stats["acted"] == 0


def test_一件要花一小时的事橱窗里演得出来(flagship):
    """**做了却开箱看不见,等于没做。**

    时间是这个引擎里唯一封得住"她一天能干多少事"的代价 —— 量能睡回来、材料能买
    回来,而一段时间过不去就是过不去。橱窗里那棵老橡树因此有一件要花一小时的事:
    嫁接(自造的动词,12 tick = 一小时),做完把这棵树的上限抬高两米。

    这条演的是整条链:起头 → 代价当场付而树一点没变 → 这期间她腾不出手 →
    到点了效果才落。断在任何一环,`duration` 这一层在橱窗里就等于不存在。
    """
    agent_id = "夏"                      # 剪子在她手上(遥 没有,那是 §2.9.6.1 的演出)
    tree = "tree:harbor_oak"
    flagship.tick(24)                    # 让树先长过 `when: 树高 >= 2` 那道门
    ceiling = flagship.stocks(tree)["最大树高"]
    stamina = flagship.stocks(f"agent:{agent_id}")["体力"]

    started = flagship.scheduler.perform_affordance(agent_id, tree, "嫁接")
    assert started["ok"] and started["started"] is True, started
    assert started["duration"] == 12
    assert flagship.stocks(f"agent:{agent_id}")["体力"] == stamina - 25, "代价该当场付"
    assert flagship.stocks(tree)["最大树高"] == ceiling, "一小时还没过,树不该已经变了"

    # 这期间她腾不出手 —— 而且理由自成一类(不是"这会儿不行",也不是"她做不了")。
    busy = flagship.scheduler.perform_affordance(agent_id, tree, "tend")
    assert not busy["ok"] and busy["reason"] == "busy", busy

    engaged = flagship.engagements(agent_id)
    assert engaged and engaged[0]["verb"] == "嫁接" and engaged[0]["occupies"]

    flagship.tick(12)
    assert flagship.stocks(tree)["最大树高"] == ceiling + 2, "到点了效果该落"
    assert not flagship.engagements(agent_id)
    assert flagship.scheduler.perform_affordance(agent_id, tree, "tend")["ok"], "手腾出来了"


def test_橱窗里的世界自己长得出新东西也抹得掉(flagship):
    """**一个不能长出新东西的世界是个西洋镜,不是世界。**

    橱窗里那棵老橡树因此能育苗:花两小时(24 tick)、一包肥、二十点体力,长出一株
    树苗——一个创世时**不存在**的实体。而树苗能被拔掉,因为代价只封得住"多久生
    一个",封不住"世界里一共有多少个":会生的东西都得会死,否则每个世界最后都挤爆,
    而且漏得很慢、很安静。

    这条演的是整条链:够不够格 → 代价当场付 → 到点了才生下来 → 生下来的东西真的
    在世界里(有量、在场、能被交互、经得起自检)→ 也真的抹得掉。
    """
    agent_id = "夏"                     # 剪子和肥都在她手上
    tree = "tree:harbor_oak"
    assert flagship.entities("sapling") == [], "创世时世界里还没有树苗"

    started = flagship.scheduler.perform_affordance(agent_id, tree, "育苗")
    assert started["ok"] and started["started"] is True, started
    assert started["consumed"] == {"fertilizer": 1}
    assert flagship.entities("sapling") == [], "两小时还没过,苗不该已经在那儿"

    flagship.tick(24)
    saplings = flagship.entities("sapling")
    assert len(saplings) == 1, "到点了苗该长出来"
    sapling = saplings[0]
    assert sapling["name"] == "新育的树苗"
    assert sapling["location"] == flagship.entities("tree")[0]["location"], \
        "没写 location 就生在这件事发生的地方"
    assert sapling["values"] == {"苗高": 0.2}, "声明过的量要逐个落地"
    assert flagship.check_entity(sapling["id"])[0]["ok"], "生下来就得活得了"

    # 它是世界里一个真的东西:她碰得到,而且能被抹掉。
    assert flagship.scheduler.perform_affordance(agent_id, sapling["id"], "look")["ok"]
    pulled = flagship.scheduler.perform_affordance(agent_id, sapling["id"], "拔掉")
    assert pulled["ok"] and pulled["destroyed"] == sapling["id"]
    assert flagship.entities("sapling") == []
    assert flagship.stocks(sapling["id"]) == {}, "抹掉了量还留着 = 一个不存在的东西还有高度"


def test_橱窗里两个人想起你的频率本来就不一样(flagship):
    """"她想起你"这件事橱窗里演得出来,而且**两个人不是一个频率**。

    性格进这一层走的是**她声明过的量**(`contact.initiative_stock`),不是从人设
    文本里猜关键词。橱窗里因此写死了两个刻度:开朗热情、喜欢主动搭话的苏晚夏是
    1.4,惜字如金的陆知遥是 0.4 —— 一个作者读得懂、调得动、而且解释得清的差别。

    没声明这个量的世界行为逐位不变(默认 1.0),所以这条钉的是"橱窗里展示了它",
    不是"引擎强加了它"。
    """
    assert flagship.stocks("agent:夏")["主动"] == 1.4
    assert flagship.stocks("agent:遥")["主动"] == 0.4
    # 没被作者写过的那个人拿的是声明的默认值 —— 逐个量填,不是逐个人填。
    assert flagship.stocks("agent:柔")["主动"] == 1.0

    # 而且这个量是**她自己感知得到**的(visibility: self),不是引擎手里的暗桩。
    assert "主动" in flagship.perception("夏")["own"]


def test_橱窗里想起你这条链是接通的(flagship):
    """开箱世界里,`contact` 的判定链真的算得动 —— 不是一个点亮了却没接上的开关。

    这里不硬造关系(那是 `test_contact.py` 的活),只钉一件事:**问得出答案**。
    `contact_forecast()` 是这一层唯一能证明"它在算"的窗口,而这一层默认不响,
    静默失效正是它最可能的坏法。
    """
    assert flagship.contact_forecast() == [], "开箱时她跟任何玩家都还没关系"
    assert flagship.contact_stats()["fired"] == 0
    # hook 真的挂在时钟上(而不是"开关点亮了但没人读它")。
    assert flagship.scheduler._contact_hook is not None
    assert flagship.scheduler._contact_interval > 0


def test_橱窗里一个玩家进来就有按钮可点(flagship):
    """**做了却开箱看不见等于没做。**

    玩家那一侧的能力表自带选项(`player_options`)这件事,只有在一个真有东西可点
    的世界里才看得出来。橱窗必须是那样的世界:走进咖啡店,菜单上得有一样带人话
    名字的东西、和至少一个能对它做的动词 —— 而不是一个空表加一句沉默。
    """
    flagship.player_move("u1", "cafe")
    menu = flagship.player_options("u1")
    assert menu["blocked"] == "", menu
    assert menu["targets"], "橱窗里一个玩家站在咖啡店中央,什么也点不动"
    first = menu["targets"][0]
    assert first["name"] and first["name"] != first["id"], (
        "按钮上印 id 等于没画"
    )
    assert first["verbs"], f"{first['id']} 摆在那儿却做不了任何事"
    assert any(v["available"] for v in first["verbs"]), (
        "每一个动词都点不动 —— 那和没有菜单是一样的第一屏"
    )
    # 说明书那一半也得是写给人的(它随 `player_id` 一起长出 options)。
    interact = {row["id"]: row for row in flagship.player_tools("u1")}["interact"]
    assert interact["params_schema"]["target"]["options"], "菜单没自带选项"


def test_橱窗里玩家自己那一排也说人话(flagship):
    """`own` 是这一版新开的一扇窗,而窗后面的东西没人替它排过版。

    东西身上的量作者一个不落地分了档(`发条 满的` / `土 正好`),因为那些一直
    印在屏幕上;**玩家自己身上的量从来没有被显示过**,于是同一个作者、同一份
    声明,`agent` 那几个量一个 `bands` 都没写。新开的窗一装上,第一屏就是
    `主动 0.998 / 睡眠债 0.384 / 心动 0` —— 线上真的这样。

    「库里有 ≠ 玩家点得到」这条的镜像:**我开了一层显示,而它后面的内容不是照着
    "会被人读到"写的**。判据不是"作者写没写 bands",是**渲染出来那一行有没有
    数字的噪音**:有单位的数字是能行动的信息(`体力 100点` 对着 `要 4 点`),
    没单位又没档词的裸浮点数是内脏。
    """
    flagship.player_move("u1", "cafe")
    own = flagship.player_options("u1")["own"]["readouts"]
    assert own, "玩家自己身上一个量都没有"
    naked = [r for r in own if not r["word"] and not r["unit"]]
    assert not naked, (
        f"橱窗第一屏上这几行是裸数字:{[r['text'] for r in naked]}。"
        "分档,或者给个单位 —— 两样都没有的话,屏幕上那行字对玩家不构成任何信息"
    )


def test_橱窗里一段关系说得出人话(flagship):
    """三个人开箱就互相认识(`test_the_characters_already_know_each_other`),
    而那份认识此前只能以四个浮点数的形状拿到手。**给数字等于把一段关系变成
    一根进度条** —— 橱窗第一屏就该演得出那句人话。
    """
    rows = flagship.relationship_summaries()
    assert rows, "橱窗里一段说得出人话的关系都没有"
    for row in rows:
        assert row["band_name"], row
        assert row["summary"].strip()
        # 橱窗里的 id 恰好是名字里的一个字(`夏` / `苏晚夏`),所以"id 没出现"
        # 这条断言在这儿证明不了什么。钉得住的是另一半:**两边都用人话叫**。
        assert row["agent_name"] in row["summary"], row["summary"]
        assert row["other_name"] in row["summary"], row["summary"]
        assert row["agent_name"] != row["agent_id"], "名册里的名字没翻出来"


def test_橱窗里的事件是有人经历过的():
    """规律 emit 出来的事件躺在日志里没人记得,是这一轮修的病本身。

    橱窗里必须有一条**写了 `importance` 和那句话**的门槛事件 —— 否则新用户
    开箱看到的仍是"世界的历史和角色的经历是两个不相交的集合"。
    """
    rules = [r["body"] for r in _bundled_rows() if r.get("type") == "rule"]
    told = [
        emit for rule in rules for emit in (rule.get("emit") or [])
        if emit.get("importance") and emit.get("text")
    ]
    assert told, "橱窗里没有一件她记得住的世界大事"


def test_橱窗里的世界会下不一样的雨():
    """`rand()` 是"可重放的意外"。橱窗不用它的话,新用户看到的世界里没有偶然性 ——
    每一天都可以被预言,而可预言的生活里长不出值得讲述的事。
    """
    rules = [r["body"] for r in _bundled_rows() if r.get("type") == "rule"]
    assert any(
        "rand()" in source
        for rule in rules for source in (rule.get("set") or {}).values()
    ), "橱窗里一颗骰子都没有"


def test_橱窗里的门槛事件有涨也有退():
    """只有 `rise` 的世界里,汛期一生只有一次机会(线上那个世界水位顶死 17 天)。"""
    rules = [r["body"] for r in _bundled_rows() if r.get("type") == "rule"]
    edges = {
        emit.get("on", "rise")
        for rule in rules for emit in (rule.get("emit") or [])
    }
    assert {"rise", "fall"} <= edges, f"橱窗只演了单边沿:{edges}"


def test_橱窗里的量说人话():
    """她该说"瓢泼大雨",不该说 `雨势 0.8` —— 把模拟层的浮点数塞进她的提示词,
    等于让她把内脏当风景来描述。可见性那一路和量声明那一路各要有一处。
    """
    rows = _bundled_rows()
    on_visibility = [
        r["body"] for r in rows
        if r.get("type") == "visibility" and r["body"].get("bands")
    ]
    on_kinds = [
        quantity
        for r in rows if r.get("type") == "kind"
        for quantity in (r["body"].get("quantities") or {}).values()
        if isinstance(quantity, dict) and quantity.get("bands")
    ]
    assert on_visibility, "世界量一个分档都没有"
    assert on_kinds, "种类的量一个分档都没有"


def _bundled_rows() -> list[dict]:
    """橱窗那份文件的作者层逐行 —— 它是纯文本进仓库的,可以直接读。"""
    text = resources.files("anima_world").joinpath("demo.cyberworld").read_text("utf-8")
    return [
        row for row in (json.loads(line) for line in text.splitlines() if line.strip())
        if row.get("kind") == "author"
    ]
