"""量的**名字**和它的**意义**:`label` 与 `bands`,她读到的那一行由这两样拼出来。

`label` 只换得了名字:她感知到的仍然是 `江水位 6.5`,不是"水已经上了江岸街"。把一个
浮点数直接塞进提示词有两个后果,都不报错:

- **她把内脏当风景来描述。** 6.5 是引擎的账,不是一个住在江边的人眼里的世界。
- **0.8 的雨算大算小全靠 LLM 猜**,而两个模型猜出两种雨 —— 同一个世界在两台机器上
  下的不是同一场雨,并且没有任何地方看得出来。

所以这个文件守的不是"分档能用",是三条边界:

1. **不写 `bands` 的量,行为逐位不变**(声明本身就是开关,和 perception / ontology
   逐字同构)。
2. **渲染是赠品,数字是契约**:提示词里是词,`perception()` / `--json` 给宿主的
   数字**原样不动** —— 而且她**做决定**读的仍然是真值(行为树 / 能力条件那条路)。
3. **坏声明当场开不了机,一次列全**:跳过一条坏 `bands` 比报错坏得多 —— 作者要到
   三个月后才发现她一直在报数字。

后半个文件守的是同一类病的另一半:**`label` 一直进不了她的提示词。** 作者认认真真
写了 `{"key": "size", "label": "树高"}`,世界照跑、日志干净,而她读到的是 `size 3.2` ——
CLAUDE.md 的愿景里写着"英文世界靠 `label` 机制与英文种子,不靠引擎换语言",而这条路
断着,并且不报错。两样的分工是正交的,合起来正好是那一行:

    label 换的是**名字**,bands 换的是**值** —— `size 3.2` → `树高 齐人高`

而**量名的 label 和实体名的 label 是两张表**:`老橡树` 是那棵树的名字,`树高` 是它
身上那个量的名字,同一行里各就各位,谁也不该盖住谁。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from _worldfile import write_seed_file

from anima_world.perception import band_word, visibility_band_errors
from anima_world.world_seed import WorldSeedError

# 雨:第一档从量的下界写起,这是推荐写法。
RAIN = [[0, "毛毛雨"], [0.4, "大雨"], [0.75, "瓢泼大雨"]]


def _seed(**sections):
    seed = {
        "locations": [{"id": "riverside", "name": "江边", "description": "堤上一条街"}],
        "agents": [
            {"id": "夏", "name": "夏", "location": "riverside", "personality": "安静"},
        ],
    }
    seed.update(sections)
    return seed


def _open(open_world, tmp_path, seed, *, world_id=None, name="w.cyberworld"):
    return open_world(
        world_id, world_file=write_seed_file(tmp_path / name, seed)
    )


def _rain_world(open_world, tmp_path, *, bands=RAIN, value=0.8, **kw):
    row = {"kind": "world", "key": "雨势", "visible": "public"}
    if bands is not None:
        row["bands"] = bands
    return _open(open_world, tmp_path, _seed(
        stocks=[{"owner": "world", "values": {"雨势": value}}],
        stock_visibility=[row],
    ), **kw)


def _system(world) -> str:
    """她这一刻**真会收到**的 system prompt。

    走 `debug_prompt` 是有意的:它和真聊天共用 `ChatService.prompt_blocks`
    (调试视图不许撒谎),所以验它就是在验真路径。
    """
    return world.debug_prompt("夏")["system"]


# ── 开关:不写 bands 的量逐位不变 ────────────────────────────────────────────


def test_a_quantity_without_bands_still_reads_as_a_number(open_world, tmp_path):
    """**这条是这一层的开关。** 没写 `bands` 的量,提示词里逐位还是从前那个数字。

    分档是作者的选择,不是引擎替所有世界做的决定:一个"库存 37 件"的量翻成
    "还剩不少"是在替作者丢掉他要的精度。
    """
    world = _rain_world(open_world, tmp_path, bands=None)
    assert "雨势 0.8" in _system(world)
    perceived = world.perception("夏")
    assert perceived["public"] == {"雨势": 0.8}
    assert perceived["words"]["public"] == {}, "没声明分档却凭空多出一个档词"


# ── 她收到的是词,宿主收到的是数字 ──────────────────────────────────────────


def test_the_band_word_is_what_reaches_her_prompt(open_world, tmp_path):
    """她读到的是"瓢泼大雨",不是 0.8 —— 而这条走的是真提示词那条路。"""
    world = _rain_world(open_world, tmp_path)
    system = _system(world)
    assert "雨势 瓢泼大雨" in system
    assert "0.8" not in system, "分档声明过的量,数字仍然漏进了提示词"


def test_the_number_is_still_the_contract_for_hosts(open_world, tmp_path):
    """**渲染是赠品,数字是契约。** 宿主要自己排版,拿到的必须是原值。

    档词只能**加**一个字段,不能顶替 `public` / `own` / `here` 里的数 —— 顶替掉的话
    宿主要画一根水位曲线就再也画不出来,而且是在升级之后某天突然画不出来。
    """
    world = _rain_world(open_world, tmp_path)
    perceived = world.perception("夏")
    assert perceived["public"] == {"雨势": 0.8}
    assert perceived["words"]["public"] == {"雨势": "瓢泼大雨"}


@pytest.mark.parametrize("value, word", [
    (0, "毛毛雨"),
    (0.39, "毛毛雨"),
    (0.4, "大雨"),        # 阈值含等号:恰好踩在线上归**这一档**
    (0.74, "大雨"),
    (0.75, "瓢泼大雨"),
    (2.0, "瓢泼大雨"),    # 最后一档向上封口
])
def test_the_last_threshold_at_or_below_the_value_wins(open_world, tmp_path, value, word):
    world = _rain_world(open_world, tmp_path, value=1.0)
    world.set_stock("world", "雨势", value)
    assert world.perception("夏")["words"]["public"]["雨势"] == word


def test_below_the_first_threshold_falls_into_the_first_band(open_world, tmp_path):
    """**第一档向下封口。** 作者没从量的下界写起时,它以下的值归第一档。

    另一种做法是"回落成数字",而那会让同一个量时而是词、时而是数 —— 一旦声明了
    `bands`,提示词里就不该再出现这个量的数字,不然作者调试时看到的和上线后
    某个低值时刻看到的不是一回事。
    """
    world = _rain_world(open_world, tmp_path,
                        bands=[[0.4, "大雨"], [0.75, "瓢泼大雨"]], value=0.1)
    assert world.perception("夏")["words"]["public"]["雨势"] == "大雨"
    assert "0.1" not in _system(world), "低于第一档时数字漏了出去"


def test_two_models_read_the_same_rain(open_world, tmp_path):
    """同一个值在两次读取里必须给出同一个词 —— 分档是确定的,不是一次判断。"""
    world = _rain_world(open_world, tmp_path, value=0.4)
    first = world.perception("夏")["words"]["public"]["雨势"]
    assert first == world.perception("夏")["words"]["public"]["雨势"] == "大雨"


# ── 三档都吃分档 ────────────────────────────────────────────────────────────


ACTOR = {"id": "agent", "quantities": {
    "体力": {"default": 100.0, "visibility": "self",
             "bands": [[0, "快撑不住了"], [40, "有点累"], [80, "精神得很"]]},
}}
TREE = {
    "id": "tree",
    "gloss": "一棵树",
    "quantities": {"树高": {"default": 1.0, "visibility": "here", "unit": "米",
                            "bands": [[0, "刚出土的苗"], [3, "齐屋檐高"]]}},
    "affordances": {"tend": {"when": ["树高 < 10"], "set": {"树高": "树高 + 0.3"}}},
}


def _garden(open_world, tmp_path, **kw):
    return _open(open_world, tmp_path, _seed(
        kinds=[ACTOR, TREE],
        entities=[{"id": "tree:oak", "name": "老橡树", "location": "riverside"}],
        stocks=[{"owner": "tree:oak", "values": {"树高": 5.0}},
                {"owner": "agent:夏", "values": {"体力": 20.0}}],
    ), **kw)


def test_a_kind_can_declare_its_bands_where_it_declares_the_quantity(open_world, tmp_path):
    """种类声明**同时就是**可见性声明 —— 分档是量声明的一部分,写在同一处。

    单位跟着一起走:`树高 齐屋檐高米` 是引擎自己印上去的噪音,而她会照着念。
    """
    world = _garden(open_world, tmp_path)
    system = _system(world)
    assert "树高 齐屋檐高" in system
    assert "齐屋檐高米" not in system, "换成词之后单位还跟着"
    assert "5" not in system.split("老橡树")[-1].split("\n")[0], "这一行还带着数字"
    assert world.perception("夏")["here"]["tree:oak"] == {"树高": 5.0}
    assert world.perception("夏")["words"]["here"]["tree:oak"] == {"树高": "齐屋檐高"}


def test_her_own_quantity_gets_a_band_word_too(open_world, tmp_path):
    """`self` 档同理:她自己知道的是"有点累",不是"体力 20"。"""
    world = _garden(open_world, tmp_path)
    system = _system(world)
    assert "体力 快撑不住了" in system
    assert "体力 20" not in system
    assert world.perception("夏")["own"]["体力"] == 20.0


def test_the_words_reach_her_autonomous_decision_too(open_world, tmp_path):
    """感知同时进聊天与**定时轮次的决定上下文**。

    两边各拼一遍的话,她说话时看到的世界和她做决定时看到的不是同一个 —— 而两边
    都能跑、都不报错。这条钉的就是那个分叉。
    """
    world = _rain_world(open_world, tmp_path)
    ctx = world._autonomy_context("夏", SimpleNamespace(day=1, hour=9, minute=0))
    notes = "\n".join(ctx.notes)
    assert "雨势 瓢泼大雨" in notes and "0.8" not in notes


# ── 她做决定读的仍然是真值 ──────────────────────────────────────────────────


def test_the_real_number_is_what_decisions_are_made_on(open_world, tmp_path):
    """分档只影响**她怎么说**,不影响世界怎么算。

    `tend` 的条件是 `树高 < 10`,而她感知到的是"齐屋檐高" —— 那个词进不了表达式,
    也永远不该进。这条一旦坏掉的样子是:世界照跑,只是每次判断都拿一个字符串去
    比大小,而引擎会挑一个谁也没想过的答案。
    """
    world = _garden(open_world, tmp_path)
    result = world.act("夏", "interact", {"target": "tree:oak", "verb": "tend"},
                       surface="body")
    assert result.get("ok"), result
    assert world.stocks("tree:oak")["树高"] == pytest.approx(5.3)


# ── 坏声明:当场开不了机,一次列全 ──────────────────────────────────────────


@pytest.mark.parametrize("bands, fragment", [
    ("大雨", "必须是一个数组"),
    ([[0.4]], "[阈值, 词]"),
    ([[0.4, "大雨", "多的"]], "[阈值, 词]"),
    (["大雨"], "[阈值, 词]"),
    ([["低", "小雨"]], "阈值必须是数字"),
    ([[0, "毛毛雨"], [0.4, 7]], "词必须是字符串"),
    ([[0, "毛毛雨"], [0, "大雨"]], "升序"),
    ([[0.4, "大雨"], [0.2, "小雨"]], "升序"),
    ([[0, "  "]], "档词不能是空的"),
    ([], "空的"),
])
def test_a_bad_band_declaration_stops_the_world(open_world, tmp_path, bands, fragment):
    """坏声明**开不了机**,不是跳过。

    跳过一条比报错坏得多:世界照跑、日志干净,而她一直在报数字 —— 作者要到
    三个月后读日志才知道自己写错了一个方括号。
    """
    with pytest.raises(WorldSeedError) as caught:
        _rain_world(open_world, tmp_path, bands=bands)
    assert fragment in str(caught.value)


def test_every_bad_band_is_listed_at_once(open_world, tmp_path):
    """一次列全 —— 修一条重开一次的话,一份写错三处的文件要开机三次。"""
    with pytest.raises(WorldSeedError) as caught:
        _open(open_world, tmp_path, _seed(stock_visibility=[
            {"kind": "world", "key": "雨势", "visible": "public", "bands": "大雨"},
            {"kind": "world", "key": "江水位", "visible": "public",
             "bands": [[6, "上了滩地"], [3, "水很稳"]]},
            {"kind": "agent", "key": "体力", "visible": "self", "bands": [[0, ""]]},
        ]))
    message = str(caught.value)
    assert "雨势" in message and "江水位" in message and "体力" in message


def test_a_rejected_file_writes_nothing_at_all(open_world, tmp_path, fresh_redis):
    """**坏声明一个字都不写。** 半装进去的世界比装不进去坏:作者改好再来一次,
    上一次留下的半截声明还在,来路不明。"""
    with pytest.raises(WorldSeedError):
        _rain_world(open_world, tmp_path, bands=[[1, "大"], [0, "小"]], world_id="bad")
    assert not fresh_redis.exists("anima:bad:visibility")


def test_the_api_refuses_a_bad_band_declaration_too(open_world, tmp_path):
    """`World.declare_visibility` 是同一道闸的另一个入口 —— 宿主写错照样当场拒。"""
    world = _open(open_world, tmp_path, _seed())
    with pytest.raises(ValueError) as caught:
        world.declare_visibility("world", "雨势", "public", bands=[[0.4, "大雨"], [0.2, "小"]])
    assert "升序" in str(caught.value)
    assert world.visibility_rules() == [], "被拒的声明还是落了库"


def test_the_band_table_is_askable_from_the_cli(tmp_path):
    """**作者把这个量翻成了什么,只有本体这一处问得到** —— 和动词表同一个道理。

    界面上猜一份档词出来的错不报错,只是写着一个这个世界里不存在的说法;而
    `--json` 是契约,那张字符表是赠品。
    """
    import json as _json

    from _worldfile import open_world_at, run_cli

    path = write_seed_file(tmp_path / "w.cyberworld", _seed(
        kinds=[TREE],
        entities=[{"id": "tree:oak", "name": "老橡树", "location": "riverside"}],
    ))
    with open_world_at(tmp_path / "w.db", world_file=path):
        pass
    done = run_cli("ontology", "--world-id", "w")
    assert "分档" in done.stdout and "齐屋檐高" in done.stdout

    done = run_cli("ontology", "--world-id", "w", "--json")
    tree = next(k for k in _json.loads(done.stdout)["kinds"] if k["id"] == "tree")
    quantity = next(q for q in tree["quantities"] if q["key"] == "树高")
    assert quantity["bands"] == [[0, "刚出土的苗"], [3, "齐屋檐高"]]


def test_validating_a_file_reports_bad_bands_without_building_a_world(tmp_path):
    """创作台经 CLI 委托校验 —— `validate world` 得报同一批错,不建世界。"""
    from _worldfile import run_cli

    path = write_seed_file(tmp_path / "bad.cyberworld", _seed(stock_visibility=[
        {"kind": "world", "key": "雨势", "visible": "public", "bands": [[1, "大"], [0, "小"]]},
    ]))
    done = run_cli("validate", "world", path)
    assert done.returncode == 2, "错误的退出码是 2(提醒才是 0)"
    assert "升序" in done.stdout + done.stderr


# ── 落库与重开 ──────────────────────────────────────────────────────────────


def test_bands_survive_a_reopen(open_world, tmp_path):
    """声明住在世界里,不住在这次开机的那份文件里。"""
    _rain_world(open_world, tmp_path, world_id="keep")
    reopened = open_world("keep")
    assert reopened.perception("夏")["words"]["public"]["雨势"] == "瓢泼大雨"
    rows = {r["key"]: r for r in reopened.visibility_rules()}
    assert rows["雨势"]["bands"] == [[0, "毛毛雨"], [0.4, "大雨"], [0.75, "瓢泼大雨"]]


# ── label:量的名字 ─────────────────────────────────────────────────────────


def _oak(open_world, tmp_path, quantity: dict, *, value=8.0, **kw):
    """一棵放在她脚边的树,量的声明由调用方给(走 `kinds` 那条路)。"""
    return _open(open_world, tmp_path, _seed(
        kinds=[{"id": "tree", "gloss": "一棵树", "quantities": {"size": quantity}}],
        entities=[{"id": "tree:oak", "name": "老橡树", "location": "riverside"}],
        stocks=[{"owner": "tree:oak", "values": {"size": value}}],
    ), **kw)


def test_a_quantity_label_reaches_her_prompt(open_world, tmp_path):
    """**这条是断了的那一根。** 作者写了 `label`,她读到的就该是那几个字。

    断着的样子正是这个仓库最怕的:世界照跑、日志干净,而她一直在念引擎的变量名。
    CLAUDE.md 的愿景里"英文世界靠 label 机制"整条路都挂在它上面。
    """
    world = _oak(open_world, tmp_path, {"visibility": "here", "label": "树高"})
    system = _system(world)
    assert "树高 8" in system
    assert "size" not in system, "她还在念引擎的变量名"


def test_a_quantity_without_a_label_reads_exactly_as_before(open_world, tmp_path):
    """没写 `label` 的量逐位不变 —— 和 `bands` 同一条:声明本身就是开关。"""
    world = _oak(open_world, tmp_path, {"visibility": "here"})
    assert "size 8" in _system(world)


def test_the_label_names_it_and_the_band_says_it(open_world, tmp_path):
    """**两样正交:`label` 换名字,`bands` 换值。** 一起写就是一行完整的人话。

    定成"档词顶掉名字"的话,`- 这里的老橡树:齐人高` 读起来像那棵树叫齐人高;
    定成"名字顶掉档词"的话,`bands` 白写。所以两个位置各归各的。
    """
    world = _oak(open_world, tmp_path, {
        "visibility": "here", "label": "树高", "unit": "米",
        "bands": [[0, "刚出土的苗"], [5, "齐人高"]],
    })
    system = _system(world)
    assert "树高 齐人高" in system
    assert "size" not in system and "8" not in system.split("老橡树")[-1].split("\n")[0]
    assert "齐人高米" not in system, "换成词之后单位还跟着"


def test_the_key_is_still_the_contract_for_hosts(open_world, tmp_path):
    """**渲染是赠品,键是契约。** `perception()` 里仍然按量名索引 ——
    换成 label 的话,宿主的代码会在作者改一个字的那天全部读到 KeyError。"""
    world = _oak(open_world, tmp_path, {"visibility": "here", "label": "树高"})
    perceived = world.perception("夏")
    assert perceived["here"] == {"tree:oak": {"size": 8.0}}
    assert perceived["labels"]["here"] == {"tree:oak": {"size": "树高"}}


def test_the_entity_name_and_the_quantity_label_are_two_tables(open_world, tmp_path):
    """`老橡树` 是那棵树的名字,`树高` 是它身上那个量的名字 —— 同一行里各就各位。

    塞进同一个 dict 的下场是它们互相盖:要么每棵树都叫"树高",要么每个量都叫
    "老橡树",而两种都只是提示词里读着别扭,不报错。
    """
    world = _oak(open_world, tmp_path, {"visibility": "here", "label": "树高"})
    assert "这里的老橡树(一棵树):树高 8" in _system(world)


def test_two_kinds_can_label_the_same_key_differently(open_world, tmp_path):
    """同一个量名在两个种类上是两个东西 —— 反查按种类做,不是全世界一张表。

    全局一张表的话,后声明的那个会把先声明的挤掉:一座矿的"储量"会显示成"树高",
    世界照跑、日志干净。(动词的人话反查踩过同一个坑。)
    """
    world = _open(open_world, tmp_path, _seed(
        kinds=[
            {"id": "tree", "gloss": "一棵树", "quantities": {
                "size": {"visibility": "here", "label": "树高"}}},
            {"id": "ore", "gloss": "一处矿脉", "quantities": {
                "size": {"visibility": "here", "label": "储量"}}},
        ],
        entities=[{"id": "tree:oak", "name": "老橡树", "location": "riverside"},
                  {"id": "ore:iron", "name": "铁矿", "location": "riverside"}],
        stocks=[{"owner": "tree:oak", "values": {"size": 8.0}},
                {"owner": "ore:iron", "values": {"size": 42.0}}],
    ))
    system = _system(world)
    assert "老橡树(一棵树):树高 8" in system
    assert "铁矿(一处矿脉):储量 42" in system


def test_her_own_and_the_public_quantities_get_their_labels_too(open_world, tmp_path):
    """三档一视同仁 —— `self` 和 `public` 那两行是另写的拼装,漏一处就少一档。"""
    world = _open(open_world, tmp_path, _seed(
        kinds=[{"id": "agent", "quantities": {
            "kungfu": {"default": 120.0, "visibility": "self", "label": "功力"}}}],
        stocks=[{"owner": "world", "values": {"season": 2}}],
        stock_visibility=[{"kind": "world", "key": "season", "visible": "public",
                           "label": "季节"}],
    ))
    system = _system(world)
    assert "功力 120" in system and "季节 2" in system
    assert "kungfu" not in system and "season" not in system


def test_a_label_declared_in_the_visibility_section_works_too(open_world, tmp_path):
    """两条声明路径同一个待遇:`stock_visibility` 那行的 `label` 也得到她眼前。"""
    world = _open(open_world, tmp_path, _seed(
        stocks=[{"owner": "world", "values": {"rain": 0.8}}],
        stock_visibility=[{"kind": "world", "key": "rain", "visible": "public",
                           "label": "雨势", "bands": RAIN}],
    ))
    assert "雨势 瓢泼大雨" in _system(world)


def test_an_english_world_reads_english(open_world, tmp_path):
    """**英文世界靠 label 机制,不靠引擎换语言。**

    引擎里的量名可以是任何东西(甚至是中文),她读到的由 `label` 决定 —— 这就是
    那条愿景在代码里的落点,而它此前是断的。
    """
    world = _open(open_world, tmp_path, _seed(
        stocks=[{"owner": "world", "values": {"江水位": 6.5}}],
        stock_visibility=[{"kind": "world", "key": "江水位", "visible": "public",
                           "label": "river level",
                           "bands": [[0, "calm"], [5, "over the sandbank"],
                                     [6.2, "up on Riverside Street"]]}],
    ))
    system = _system(world)
    assert "river level up on Riverside Street" in system
    assert "江水位" not in system and "6.5" not in system
    assert world.perception("夏")["public"] == {"江水位": 6.5}, "键仍然是契约"


def test_labels_survive_a_reopen(open_world, tmp_path):
    """声明住在世界里 —— 重开一次不该把她的词退回引擎的变量名。"""
    _oak(open_world, tmp_path, {"visibility": "here", "label": "树高"}, world_id="lbl")
    reopened = open_world("lbl")
    assert "树高 8" in reopened.debug_prompt("夏")["system"]


# ── 纯函数层 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value, word", [
    (-99, "毛毛雨"), (0, "毛毛雨"), (0.4, "大雨"), (0.75, "瓢泼大雨"), (99, "瓢泼大雨"),
])
def test_band_word_picks_the_last_threshold_at_or_below(value, word):
    assert band_word([(0.0, "毛毛雨"), (0.4, "大雨"), (0.75, "瓢泼大雨")], value) == word


def test_band_word_without_bands_is_none():
    """没有分档就是"没有词" —— 调用方照旧渲染数字。"""
    assert band_word(None, 3.0) is None
    assert band_word((), 3.0) is None


def test_a_good_declaration_has_nothing_to_say():
    assert visibility_band_errors({"stock_visibility": [
        {"kind": "world", "key": "雨势", "visible": "public", "bands": RAIN},
        {"kind": "world", "key": "季节", "visible": "public"},
    ]}) == []


def test_a_non_finite_threshold_is_refused():
    """NaN 比不出大小,于是那一档永远选不中 —— 而它看上去写得好好的。"""
    errors = visibility_band_errors({"stock_visibility": [
        {"kind": "world", "key": "雨势", "visible": "public",
         "bands": [[0, "小雨"], [float("nan"), "大雨"]]},
    ]})
    assert errors and "阈值必须是数字" in errors[0]
