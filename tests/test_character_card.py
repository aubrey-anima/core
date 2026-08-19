"""角色卡:作者写给**玩家**看的那一面,以及它到不到得了玩家眼前。

这个文件守的是一条**整链**,不是一个函数:作者写进世界文件 → 加载期严格校验 →
落进事件日志 → 折进投影 → `World.roster()` / `anima-world roster` 读得回来 →
重启还在 → 导出再导入还在 → **一个已经跑着的世界改得动**
(`World.set_card` / `anima-world agent set-card`)。

最后那一节是补上去的,而缺它的时候这一整轮改造对**唯一一个有真人的世界等于没做**:
卡只在 `agent_join` 的 `payload.spec` 上,而作者层合并只给**新来的人**发 join —— 于是
线上那 20 个早就在册的角色一张卡都装不进去。链上有一节只对新世界成立,和链断了是
同一件事。

由来是一次真人试玩:线上那个世界 21 个角色,4 个是作者写了几周的主角、17 个是
背景 NPC,而玩家的通讯录里这 21 个人长得一模一样。作者写得进、校验放行、世界跑
得动、包也导得出 —— **就是到不了玩家眼前,而全程零报错**。链上断一节的样子就是
那个样子:每一节都能单独测绿,而合起来给错东西。所以这里每条断言都钉在**链的
出口**上,不钉在中间那几个函数上。
"""
from __future__ import annotations

import json

import pytest
from _worldfile import bundled_seed, current_client, open_world_at, run_cli

from anima_world.api import World
from anima_world.character_card import (
    CARD_BILLINGS,
    DEFAULT_BILLING,
    TAGLINE_MAX_CHARS,
    billing_of,
    world_card_errors,
    world_card_warnings,
)
from anima_world.world_seed import WORLD_SEED_AGENT_KEYS, WORLD_SEED_AGENT_OPTIONAL_KEYS

# 一份最小的能开机的世界。角色卡以外的东西一律从简 —— 这个文件问的不是世界建得
# 起来吗,而是那三格过不过得了河。
_LOCATIONS = [
    {"id": "town", "name": "小镇", "description": "雨季里的江渡镇",
     "kind": "point", "parent": None, "x": 0.5, "y": 0.5},
]


def _seed(*cards: dict | None) -> dict:
    agents = []
    for index, card in enumerate(cards):
        entry = {
            "id": f"a{index}", "name": f"角色{index}",
            "location": "town", "personality": "话不多。",
        }
        if card is not None:
            entry["card"] = card
        agents.append(entry)
    return {"agents": agents, "locations": list(_LOCATIONS)}


def _write(tmp_path, seed: dict, name: str = "w.cyberworld") -> str:
    from _worldfile import write_seed_file

    return write_seed_file(tmp_path / name, seed)


def _row(world: World, agent_id: str) -> dict:
    rows = {r["agent_id"]: r for r in world.roster()["agents"]}
    return rows[agent_id]


# ---------------------------------------------------------------- 作者写得进


def test_card_is_optional_not_required():
    """**缺席 = 这个世界没做过角色卡,不是错误。**

    `WORLD_SEED_AGENT_KEYS` 是**必填**集(`world_seed_errors` 拿它算 `missing`),
    把 `card` 加进去等于要求每个世界给每个角色写一张卡 —— 每一个已经存在的世界
    当场开不了机。所以它住在 `WORLD_SEED_AGENT_OPTIONAL_KEYS` 里。
    """
    assert "card" not in WORLD_SEED_AGENT_KEYS
    assert "card" in WORLD_SEED_AGENT_OPTIONAL_KEYS
    assert world_card_errors(_seed(None, None)) == []


def test_a_written_card_survives_into_the_roster(tmp_path):
    """链的两头:作者写下的那张卡,玩家那一侧读得到。

    修之前这条是红的 —— `agent_join` 的 `payload.spec` 被写死成
    `{"name", "personality"}`,卡在装载时就没了(而且不报错)。
    """
    seed = _seed({"billing": "lead", "tagline": "她在雨季里等一个人。",
                  "portrait": "https://cdn.example.com/a0.png"})
    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, seed),
                          force_mock_llm=True)
    try:
        row = _row(world, "a0")
        assert row["billing"] == "lead"
        assert row["tagline"] == "她在雨季里等一个人。"
        assert row["portrait"] == "https://cdn.example.com/a0.png"
    finally:
        world.close()


def test_bad_billing_refuses_to_boot_and_lists_everything_at_once(tmp_path):
    """坏声明**当场开不了机,而且一次列全**。

    一次列全是有理由的:作者改一条重试一次,而世界文件只读进空库一次 ——
    试错的代价是重建世界。
    """
    seed = _seed({"billing": "protagonist"}, {"billing": "MAIN"})
    path = _write(tmp_path, seed)
    with pytest.raises(Exception) as exc:
        open_world_at(tmp_path / "w.db", world_file=path, force_mock_llm=True)
    errors = getattr(exc.value, "errors", None) or [str(exc.value)]
    blob = "\n".join(errors)
    # 两条都要在里面 —— 只报第一条的话,作者改完再撞一次。
    assert "protagonist" in blob and "MAIN" in blob
    for billing in CARD_BILLINGS:
        assert billing in blob        # 错误信息要说出合法值是哪几个


def test_relative_portrait_refuses_to_boot(tmp_path):
    """`.cyberworld` 是**分发物**:相对路径发出去就是一张断的图,而且不报错。"""
    seed = _seed({"portrait": "portraits/a0.png"})
    with pytest.raises(Exception) as exc:
        open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, seed),
                      force_mock_llm=True)
    blob = "\n".join(getattr(exc.value, "errors", None) or [str(exc.value)])
    assert "portraits/a0.png" in blob
    assert "data:" in blob            # 要告诉作者怎么让包自足


def test_overlong_tagline_refuses_to_boot(tmp_path):
    """一句话有上限:它进的是通讯录里名字底下那一行。

    没有上限的话,截断在哪儿由界面随手决定,而作者看不见。
    """
    seed = _seed({"tagline": "雨" * (TAGLINE_MAX_CHARS + 1)})
    with pytest.raises(Exception) as exc:
        open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, seed),
                      force_mock_llm=True)
    blob = "\n".join(getattr(exc.value, "errors", None) or [str(exc.value)])
    assert str(TAGLINE_MAX_CHARS) in blob
    # 刚好卡上限的要过。
    assert world_card_errors(_seed({"tagline": "雨" * TAGLINE_MAX_CHARS})) == []


def test_unknown_card_keys_only_warn_and_ride_through(tmp_path):
    """不认识的键**只警告,而且原样带过去**。

    拦下来会让新版创作台配不了老引擎(它已经预告了第四样:声线、主题色、CV);
    一声不吭又会让一个拼错的 `taglien` 静静地什么也不做。
    """
    card = {"tagline": "她在雨季里等一个人。", "taglien": "拼错了", "voice": "低"}
    assert world_card_errors(_seed(card)) == []
    problems = world_card_warnings(_seed(card))
    assert len(problems) == 1
    assert "taglien" in problems[0] and "voice" in problems[0]

    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, _seed(card)),
                          force_mock_llm=True)
    try:
        # **原样带过去** —— 只摊平引擎认得的三格,等于在这道门上把它们悄悄扔了。
        assert _row(world, "a0")["card"]["voice"] == "低"
    finally:
        world.close()


def test_validate_world_says_the_same_things_as_boot(tmp_path):
    """`validate world` 和加载期**同一份判断** —— 另写一份迟早出现
    "validate 说没问题,开机还是失败"。"""
    path = _write(tmp_path, _seed({"billing": "protagonist", "taglien": "拼错了"}))
    result = run_cli("validate", "world", path, "--json")
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    assert any("protagonist" in e for e in payload["errors"])
    assert any("taglien" in w for w in payload["warnings"])
    assert result.returncode != 0


# ------------------------------------------------------------------ 读得出来


def test_roster_gives_all_nine_columns(tmp_path):
    """九栏**一栏都不许缺**:网站照这份画卡片,而 `undefined` 和空串在那一侧是
    两件事 —— 前者是"答不出",后者是"这个世界里没人写过"。

    栏名照运维台 `world_server.py` 的 `_ROSTER_FIELDS` 抄,不发明。
    """
    fields = {"agent_id", "name", "tagline", "portrait", "billing",
              "location", "location_name", "state", "away"}
    seed = _seed({"billing": "lead", "tagline": "一句话。"}, None)
    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, seed),
                          force_mock_llm=True)
    try:
        payload = world.roster()
        assert set(payload) == {"agents"}
        for row in payload["agents"]:
            assert fields <= set(row)
            assert isinstance(row["state"], dict)
            assert isinstance(row["away"], bool)
        # 地名要翻出来 —— 卡片上写"她在 town"和写"她在小镇"是两回事。
        assert _row(world, "a0")["location_name"] == "小镇"
        assert _row(world, "a0")["name"] == "角色0"
    finally:
        world.close()


def test_unmarked_agents_read_as_supporting_not_lead(tmp_path):
    """没标的人是**背景角色**,不是"未知",更不是主角。

    猜错方向的代价不对称:把主角说成配角只是排版难看,把还没出场的人说成主角
    是**剧透**。
    """
    assert DEFAULT_BILLING == "supporting"
    assert billing_of(None) == "supporting"
    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, _seed(None)),
                          force_mock_llm=True)
    try:
        row = _row(world, "a0")
        assert row["billing"] == "supporting"
        # **但卡本身仍然是"作者什么也没说"** —— 凭空造一张出来的话,宿主再也分不出
        # "作者说他是背景"和"作者没写过卡"。
        assert row["card"] is None
        assert row["tagline"] == "" and row["portrait"] == ""
    finally:
        world.close()


def test_hidden_agents_are_listed_by_the_engine(tmp_path):
    """`hidden` 的人引擎**照出** —— 引擎是"这个世界里有谁"的权威。

    筛掉是宿主那一层的事(运维台的壳已经在 `/internal/v1/roster` 上做了,理由是
    泄露的边界在进程上、不在浏览器里)。引擎先把人藏起来的话,壳连"藏了谁"都问
    不到,而 `world export` 里他还在。
    """
    seed = _seed({"billing": "hidden", "tagline": "还没解锁。"})
    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, seed),
                          force_mock_llm=True)
    try:
        assert _row(world, "a0")["billing"] == "hidden"
    finally:
        world.close()


def test_roster_order_follows_the_world_not_the_alphabet(tmp_path):
    """顺序跟世界自己的名册走(事件日志的顺序),不按字母重排。"""
    seed = {"agents": [
        {"id": "z", "name": "最后写的", "location": "town", "personality": ""},
        {"id": "a", "name": "先写的", "location": "town", "personality": ""},
    ], "locations": list(_LOCATIONS)}
    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, seed),
                          force_mock_llm=True)
    try:
        assert [r["agent_id"] for r in world.roster()["agents"]] == ["z", "a"]
    finally:
        world.close()


def test_roster_and_state_agree_on_where_she_is(tmp_path):
    """`roster()` 与 `state()` 对"她在哪"必须给同一个答案。

    两扇门分头算的话,宿主读哪一扇全凭运气 —— 而位置那条规矩本身是有讲究的
    (在场读活黑板,在途时只有黑板是真的;离场读投影)。
    """
    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, _seed(None, None)),
                          force_mock_llm=True)
    try:
        world.tick(3)
        from_state = {aid: row["location"] for aid, row in world.state()["agents"].items()}
        from_roster = {r["agent_id"]: r["location"] for r in world.roster()["agents"]}
        assert from_state == from_roster
    finally:
        world.close()


# -------------------------------------------------------------------- 存得住


def test_the_card_rides_the_event_log_not_the_blackboard(tmp_path):
    """卡走事件日志,**不上黑板**。

    `tagline` 是写给玩家看的广告词。上了黑板它就会进她的提示词,她开始照着念 ——
    而**那个后果比缺功能坏**。
    """
    seed = _seed({"billing": "lead", "tagline": "她在雨季里等一个人。"})
    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, seed),
                          force_mock_llm=True)
    try:
        joins = [e for e in world.events() if e["type"] == "agent_join"]
        assert joins and joins[0]["payload"]["spec"]["card"]["billing"] == "lead"
        blackboard = world.scheduler.agents["a0"].agent.blackboard
        assert "她在雨季里等一个人。" not in json.dumps(
            blackboard.snapshot(), ensure_ascii=False
        )
    finally:
        world.close()


def test_the_card_is_still_there_after_a_restart(tmp_path):
    """重启后卡还在 —— 名册的权威是事件日志,重放折出来的投影带着它。"""
    path = _write(tmp_path, _seed({"billing": "lead", "tagline": "一句话。"}))
    world = open_world_at(tmp_path / "w.db", world_file=path, force_mock_llm=True)
    world.tick(2)
    world.close()

    # 第二次开机**不给世界文件**:这才是托管环境里真实的重启。
    again = open_world_at(tmp_path / "w.db", force_mock_llm=True)
    try:
        row = _row(again, "a0")
        assert row["billing"] == "lead" and row["tagline"] == "一句话。"
    finally:
        again.close()


def test_the_card_survives_export_and_import(tmp_path):
    """`.cyberworld` 导出再导入,卡还在 —— 包是**分发物**,少了卡等于发出去的
    世界在别人那儿又变回一张等权重的名单。"""
    from anima_world.world_package import import_world_file

    path = _write(tmp_path, _seed({"billing": "lead", "tagline": "一句话。",
                                   "portrait": "https://cdn.example.com/a0.png"}))
    world = open_world_at(tmp_path / "w.db", world_file=path, force_mock_llm=True)
    world.tick(2)
    package = str(tmp_path / "out.cyberworld")
    world.export_snapshot(package, world_id="w2", name="带卡的世界")
    world.close()

    import fakeredis

    target = fakeredis.FakeStrictRedis(decode_responses=True)

    import_world_file(package, redis=target, world_id="w2")
    restored = World.open("w2", redis=target, force_mock_llm=True)
    try:
        row = {r["agent_id"]: r for r in restored.roster()["agents"]}["a0"]
        assert row["billing"] == "lead"
        assert row["tagline"] == "一句话。"
        assert row["portrait"] == "https://cdn.example.com/a0.png"
    finally:
        restored.close()


def test_a_world_with_no_cards_behaves_exactly_as_before(tmp_path):
    """**老世界一个字都不该变。** 一张卡都没写的世界,事件日志里不许多出一个
    `card` 字段 —— 凭空补一张 `supporting` 的话,每个老世界重开一次都会多出
    一整份"作者说他们是背景角色"的声明,而宿主再也分不出两者。"""
    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, _seed(None, None)),
                          force_mock_llm=True)
    try:
        for event in world.events():
            if event["type"] == "agent_join":
                assert set(event["payload"]["spec"]) == {"name", "personality"}
        assert all(r["card"] is None for r in world.roster()["agents"])
    finally:
        world.close()


# ---------------------------------------------------------------- CLI 出口


def test_roster_command_json_is_the_contract(tmp_path):
    """创作台那侧的判据是**有没有 CLI 出口**;而渲染是赠品,`--json` 才是契约。"""
    path = _write(tmp_path, _seed({"billing": "lead", "tagline": "一句话。"}, None))
    world = open_world_at(tmp_path / "w.db", world_file=path, force_mock_llm=True)
    world.close()

    result = run_cli("roster", "--world-id", "w", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["operation"] == "roster"
    assert [r["agent_id"] for r in payload["agents"]] == ["a0", "a1"]
    assert payload["agents"][0]["tagline"] == "一句话。"

    only_leads = json.loads(
        run_cli("roster", "--world-id", "w", "--billing", "lead", "--json").stdout
    )
    assert [r["agent_id"] for r in only_leads["agents"]] == ["a0"]


def test_roster_command_refuses_a_world_that_does_not_exist(tmp_path):
    """只读命令对不存在的 world_id 一律拒绝 —— 抄错名字会当场创世,而你看到的是
    一份"排版正常、一个人都没有"的名册。

    ⚠️ 断言要**认得出拒绝的理由**:光看退出码 2 的话,一个根本没注册的
    `roster` 子命令会让 argparse 用同一个码退出 —— 于是这条测试在"这道命令
    不存在"的世界里也是绿的,而它本该是这一整条链上最先红的一条。
    """
    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, _seed(None)),
                          force_mock_llm=True)
    world.close()
    assert run_cli("roster", "--world-id", "w", "--json").returncode == 0

    missing = run_cli("roster", "--world-id", "no-such-world", "--json")
    assert missing.returncode == 2
    assert "no-such-world" in missing.stderr
    assert "usage:" not in missing.stderr      # argparse 的拒绝不算数
    assert missing.stdout == ""                # 一份空名册也不许交出去


def test_contract_declares_the_card_so_the_studio_can_stop_guessing():
    """创作台此前只能靠版本号猜"这支引擎带不带得动角色卡" —— 而 dev tag
    (`3.2.0-dev.ae4fbd7`)上版本号比较会给出一个安静的错答案。"""
    payload = json.loads(run_cli("contract", "--json").stdout)
    assert payload["seed"]["agent_optional_keys"] == ["card"]
    card = payload["character_card"]
    assert card["billings"] == list(CARD_BILLINGS)
    assert card["default_billing"] == DEFAULT_BILLING
    assert card["tagline_max_chars"] == TAGLINE_MAX_CHARS
    assert card["read_command"] == "roster"


# ------------------------------------------------------------------- 橱窗


def test_the_showcase_world_actually_shows_a_cast(tmp_path):
    """**做了却开箱看不见等于没做。** 内置世界是新用户看到的第一屏。

    至少一个 `lead`(玩家一眼知道该点谁)和一个 `hidden`(第三档不是纸上谈兵),
    而且主角都得有那一句话 —— 没有它,主角和背景在通讯录里还是长得一样。
    """
    seed = bundled_seed()
    cards = {a["id"]: a.get("card") for a in seed["agents"]}
    billings = [billing_of(c) for c in cards.values()]
    assert "lead" in billings
    assert "hidden" in billings
    for agent_id, card in cards.items():
        if billing_of(card) == "lead":
            assert (card or {}).get("tagline"), f"{agent_id} 是主角却没有那一句话"

    world = open_world_at(tmp_path / "w.db", force_mock_llm=True)
    try:
        rows = {r["agent_id"]: r for r in world.roster()["agents"]}
        for agent_id, card in cards.items():
            if card:
                assert rows[agent_id]["billing"] == billing_of(card)
                assert rows[agent_id]["tagline"] == card.get("tagline", "")
    finally:
        world.close()


def test_the_showcase_stays_reviewable_plain_text():
    """`demo.cyberworld` 以**纯文本**进仓库,可 diff 可 review —— 所以立绘不许
    塞一大块 base64。一个 review 不了的二进制块不该是新用户看到的第一眼,
    而它同时是世界文件格式的说明书。"""
    for agent in bundled_seed()["agents"]:
        portrait = (agent.get("card") or {}).get("portrait", "")
        assert not portrait.startswith("data:"), f"{agent['id']} 的立绘是内联 base64"


# --------------------------------------------------- 一个已经跑着的世界改得动


def _cards(world: World) -> dict:
    return {r["agent_id"]: r["card"] for r in world.roster()["agents"]}


def _running_world(tmp_path, *cards):
    """一个**已经跑过**的世界:创世装完文件、走过几 tick、再重开一次。

    "重开一次"不是装饰:线上那个世界的卡要改在**第二次以后的开机**上,而作者层
    合并只对新来的人发 `agent_join` —— 第一次开机手里还捏着世界文件,验不出这个洞。
    """
    path = _write(tmp_path, _seed(*cards))
    world = open_world_at(tmp_path / "w.db", world_file=path, force_mock_llm=True)
    world.tick(2)
    world.close()
    return open_world_at(tmp_path / "w.db", force_mock_llm=True)


def test_set_card_reaches_an_agent_who_was_already_in_the_world(tmp_path):
    """**这一条是这一整轮改造的理由。**

    卡只在 `agent_join.payload.spec` 上,而已经在册的角色不会重新 join —— 于是
    线上那个真有玩家的世界(20 个角色、4 个主角)一张卡都装不进去,而作者写得进、
    校验放行、测试全绿、包也导得出。**照跑但给错东西**的标本。

    钉的是链的出口(`roster()`),不是中间那个函数。
    """
    world = _running_world(tmp_path, None, None)
    try:
        assert _cards(world)["a0"] is None          # 先确认这个世界原本没有卡
        world.set_card("a0", {"billing": "lead", "tagline": "她在雨季里等一个人。"})
        row = _row(world, "a0")
        assert row["billing"] == "lead"
        assert row["tagline"] == "她在雨季里等一个人。"
        assert _cards(world)["a1"] is None          # 别人一个字都没被动过
    finally:
        world.close()


def test_set_card_overwrites_on_purpose_unlike_the_author_layer(tmp_path):
    """**一次明示的编辑就是要覆盖** —— 和作者层合并的"只填缺不覆盖"有意相反。

    只填缺的话,一个已经写着 `supporting` 的角色**永远**改不成 `lead`,而这正是
    线上那 20 个人此刻的处境。两条路语义相反是对的;把其中一条"修"成另一条不是。
    """
    world = _running_world(tmp_path, {"billing": "supporting", "tagline": "旧的一句话。"})
    try:
        world.set_card("a0", {"billing": "lead", "tagline": "新的一句话。"})
        row = _row(world, "a0")
        assert row["billing"] == "lead"
        assert row["tagline"] == "新的一句话。"
    finally:
        world.close()


def test_set_card_merges_into_the_existing_card(tmp_path):
    """部分更新是**合并进现有的卡**,不是替换整张卡。

    只改一句话不许把作者写了几周的 `billing` 和立绘顺手抹掉 —— 而"抹掉"在这条
    路上不会报错,只会让那个人下一次刷新时从通讯录第一屏掉下去。
    """
    world = _running_world(tmp_path, {"billing": "lead",
                                      "portrait": "https://cdn.example.com/a0.png",
                                      "voice": "低"})
    try:
        world.set_card("a0", {"tagline": "补一句话。"})
        card = _cards(world)["a0"]
        assert card["billing"] == "lead"
        assert card["portrait"] == "https://cdn.example.com/a0.png"
        assert card["tagline"] == "补一句话。"
        # 引擎不认识的键也一样活着 —— 它本来就只是原样带过去的。
        assert card["voice"] == "低"
    finally:
        world.close()


def test_set_card_clear_is_its_own_slot(tmp_path):
    """`--clear` 单独一格:**"作者说他是背景"和"作者什么也没说"是两件事**。

    没有这一格的话,唯一的"取消"写法是把 billing 设回 `supporting` —— 而那是一句
    声明,不是收回声明。收回之后 `billing` 照旧读作 `supporting`(缺省在读的一侧补),
    但 `card` 必须回到 `None`。
    """
    world = _running_world(tmp_path, {"billing": "lead", "tagline": "一句话。"})
    try:
        receipt = world.set_card("a0", clear=True)
        assert receipt["changed"] is True
        row = _row(world, "a0")
        assert row["card"] is None
        assert row["billing"] == DEFAULT_BILLING
        assert row["tagline"] == "" and row["portrait"] == ""
        # 幂等:已经没有卡了,再清一次一个字都不写。
        assert world.set_card("a0", clear=True)["changed"] is False
    finally:
        world.close()


def test_set_card_refuses_clear_together_with_a_value(tmp_path):
    """`--clear` 和给值同时来 = 两句互相矛盾的话。引擎挑哪一句都是猜。"""
    world = _running_world(tmp_path, None)
    try:
        with pytest.raises(ValueError):
            world.set_card("a0", {"billing": "lead"}, clear=True)
        assert _cards(world)["a0"] is None      # 拒绝时一个字都不写
    finally:
        world.close()


def test_set_card_is_idempotent_and_says_so(tmp_path):
    """合并后逐字相同就**一个字都不写**。

    事件溯源里追加一条毫无差别的 `persona_update` 只是给历史添噪音 —— 而历史是
    这个世界唯一的真相,噪音进去了就再也分不出"这一天作者真的改了主意"。
    """
    world = _running_world(tmp_path, {"billing": "lead", "tagline": "一句话。"})
    try:
        before = len([e for e in world.events() if e["type"] == "state_change"])
        first = world.set_card("a0", {"billing": "lead", "tagline": "一句话。"})
        assert first["changed"] is False
        assert len([e for e in world.events() if e["type"] == "state_change"]) == before

        second = world.set_card("a0", {"tagline": "换一句。"})
        assert second["changed"] is True
        assert len([e for e in world.events() if e["type"] == "state_change"]) == before + 1
        # 再来一次同样的:又回到"没有变化"。
        assert world.set_card("a0", {"tagline": "换一句。"})["changed"] is False
    finally:
        world.close()


def test_set_card_validates_the_merged_card_before_writing(tmp_path):
    """校验用的是 `character_card` **那一份**判断,而且在写之前。

    另写一套的下场是同一张卡在装载期和这条路上得到两个答案 —— 于是
    `validate world` 说没问题的包,里面躺着一张开不了机的卡。
    """
    world = _running_world(tmp_path, {"billing": "lead"})
    try:
        # 每条都要**认得出拒绝的理由** —— 只验"抛了 ValueError"的话,一句
        # "什么都没给"也能让这条测试绿。
        for bad, expected in (({"billing": "protagonist"}, "protagonist"),
                              ({"tagline": "雨" * (TAGLINE_MAX_CHARS + 1)},
                               str(TAGLINE_MAX_CHARS)),
                              ({"portrait": "portraits/a0.png"}, "portraits/a0.png")):
            with pytest.raises(ValueError) as exc:
                world.set_card("a0", bad)
            assert expected in str(exc.value)
        # 一个字都没写进去:那张好卡原样还在。
        assert _cards(world)["a0"] == {"billing": "lead"}
    finally:
        world.close()


def test_set_card_refuses_an_unknown_agent_and_says_who_is_here(tmp_path):
    """不认识的人一律拒绝,**并且把这个世界里有谁说出来**。

    编一个空结果出去的话,运维的人会以为改成功了 —— 而那正是这一整轮要修的病。
    """
    world = _running_world(tmp_path, None, None)
    try:
        with pytest.raises(KeyError) as exc:
            world.set_card("a9", {"billing": "lead"})
        blob = str(exc.value)
        assert "a9" in blob
        assert "a0" in blob and "a1" in blob      # 这个世界里有谁
    finally:
        world.close()


def test_set_card_does_not_rewrite_the_genesis_event(tmp_path):
    """新的写点不许把"投影和事件共用一个可变 dict"那个洞重新打开。

    共用的话,一次 `set_card` 会顺着 `agent.spec.update(...)` **就地改写那条创世
    `agent_join` 在内存里的样子** —— 于是"那条事件说了什么"有两个答案,而日志
    才是对的(`projection.py` 的注释为此警告过)。
    """
    world = _running_world(tmp_path, {"billing": "supporting"})
    try:
        world.set_card("a0", {"billing": "lead", "tagline": "一句话。"})
        joins = [e for e in world.events() if e["type"] == "agent_join"]
        for join in joins:
            # 创世那条 join 说的仍然是创世那天的话。
            assert join["payload"]["spec"].get("card") == {"billing": "supporting"}
    finally:
        world.close()


def test_set_card_still_keeps_the_tagline_off_the_blackboard(tmp_path):
    """这条路也不许把广告词写上黑板 —— 上了黑板她就会照着念。"""
    world = _running_world(tmp_path, None)
    try:
        world.set_card("a0", {"tagline": "全城最好喝的手冲!"})
        blackboard = world.scheduler.agents["a0"].agent.blackboard
        assert "全城最好喝的手冲!" not in json.dumps(
            blackboard.snapshot(), ensure_ascii=False
        )
    finally:
        world.close()


def test_set_card_survives_a_restart(tmp_path):
    """改完重启还在 —— 走的是 `persona_update` 那条现成的路,不是就地改历史。"""
    world = _running_world(tmp_path, None)
    try:
        world.set_card("a0", {"billing": "lead", "tagline": "一句话。"})
    finally:
        world.close()
    again = open_world_at(tmp_path / "w.db", force_mock_llm=True)
    try:
        row = _row(again, "a0")
        assert row["billing"] == "lead" and row["tagline"] == "一句话。"
    finally:
        again.close()


# --------------------------------------------------------- CLI 出口(真路径)


def test_set_card_command_reaches_the_roster(tmp_path):
    """**走真 CLI,再用真 `roster` 读回来。**

    只调 `World.set_card` 的测试验不出这一节:上一轮那个 bug 恰恰是因为测试都直接
    调内部函数、没走真路才漏掉的。运维台那侧的入口是一次性容器,argv 由具名参数
    白名单生成 —— 这里跑的就是它跑的那条。
    """
    world = _running_world(tmp_path, None)
    world.close()

    done = run_cli("agent", "set-card", "--world-id", "w", "--agent", "a0",
                   "--billing", "lead", "--tagline", "她在雨季里等一个人。",
                   "--portrait", "https://cdn.example.com/a0.png", "--json")
    assert done.returncode == 0, done.stderr
    receipt = json.loads(done.stdout)
    assert receipt["changed"] is True
    assert receipt["after"]["billing"] == "lead"

    # **真的读得出新值** —— 一个进得去出不来的字段和没有这个字段是同一个 bug。
    roster = json.loads(run_cli("roster", "--world-id", "w", "--json").stdout)
    row = {r["agent_id"]: r for r in roster["agents"]}["a0"]
    assert row["billing"] == "lead"
    assert row["tagline"] == "她在雨季里等一个人。"
    assert row["portrait"] == "https://cdn.example.com/a0.png"


def test_set_card_command_refuses_an_unknown_agent(tmp_path):
    """不认识的 agent 退出码 2,并说得出这个世界里有谁。

    ⚠️ 断言要先验证**世界存在且 agent 存在时退 0** —— argparse 对一个根本没注册
    的子命令用的是同一个退出码 2,于是光看码的话,这条测试在"这道命令不存在"的
    世界里也是绿的,而它本该是这一整条链上最先红的一条。
    """
    world = _running_world(tmp_path, None, None)
    world.close()

    ok = run_cli("agent", "set-card", "--world-id", "w", "--agent", "a0",
                 "--billing", "lead", "--json")
    assert ok.returncode == 0, ok.stderr        # 先钉住"这道命令真的存在"

    missing = run_cli("agent", "set-card", "--world-id", "w", "--agent", "a9",
                      "--billing", "lead", "--json")
    assert missing.returncode == 2
    assert "usage:" not in missing.stderr       # argparse 的拒绝不算数
    assert "a9" in missing.stderr
    assert "a0" in missing.stderr and "a1" in missing.stderr
    assert missing.stdout == ""                 # 编一个回执出去 = 运维以为改成功了


def test_set_card_command_dry_run_writes_nothing(tmp_path):
    """`--dry-run` 一个字节都不写 —— 它动的是作者写的东西。"""
    world = _running_world(tmp_path, None)
    world.close()

    done = run_cli("agent", "set-card", "--world-id", "w", "--agent", "a0",
                   "--tagline", "只是看看。", "--dry-run", "--json")
    assert done.returncode == 0, done.stderr
    receipt = json.loads(done.stdout)
    assert receipt["dry_run"] is True
    assert receipt["after"]["tagline"] == "只是看看。"

    roster = json.loads(run_cli("roster", "--world-id", "w", "--json").stdout)
    assert {r["agent_id"]: r for r in roster["agents"]}["a0"]["tagline"] == ""


def test_set_card_command_refuses_clear_with_values(tmp_path):
    """`--clear` 和另外三个同时给要报错 —— 两句互相矛盾的话,挑哪句都是猜。

    ⚠️ **这条测试写第一版时是假绿的**:argparse 对一个根本没注册的子命令用的也是
    退出码 2,于是"拒绝"这件事在这道命令不存在时照样成立。所以两道额外的钉子:
    stderr 里不许有 `usage:`(那是 argparse 在拒绝,不是引擎),末尾放一个**正
    对照** —— 同一道命令参数给对时真的退 0。
    """
    world = _running_world(tmp_path, {"billing": "lead"})
    world.close()

    done = run_cli("agent", "set-card", "--world-id", "w", "--agent", "a0",
                   "--clear", "--billing", "hidden", "--json")
    assert done.returncode == 2
    assert "usage:" not in done.stderr
    assert done.stdout == ""

    # 什么都不给也不行:一次什么也没改的"成功"读起来像改成功了。
    empty = run_cli("agent", "set-card", "--world-id", "w", "--agent", "a0", "--json")
    assert empty.returncode == 2
    assert "usage:" not in empty.stderr

    still = json.loads(run_cli("roster", "--world-id", "w", "--json").stdout)
    assert {r["agent_id"]: r for r in still["agents"]}["a0"]["billing"] == "lead"

    good = run_cli("agent", "set-card", "--world-id", "w", "--agent", "a0",
                   "--clear", "--json")
    assert good.returncode == 0, good.stderr


def test_set_card_command_refuses_a_bad_card_before_writing(tmp_path):
    """坏值在**写之前**被挡住,退出码 2,而且说得出错在哪。"""
    world = _running_world(tmp_path, {"billing": "lead"})
    world.close()

    done = run_cli("agent", "set-card", "--world-id", "w", "--agent", "a0",
                   "--portrait", "portraits/a0.png", "--json")
    assert done.returncode == 2
    assert "portraits/a0.png" in done.stderr
    assert done.stdout == ""

    roster = json.loads(run_cli("roster", "--world-id", "w", "--json").stdout)
    assert {r["agent_id"]: r for r in roster["agents"]}["a0"]["billing"] == "lead"


def test_立绘走文件进来_argv装不下的那一段(tmp_path):
    """契约公布 1 MiB,而 `--portrait` 这扇门到 128 KiB 就没了。

    Linux 的 `MAX_ARG_STRLEN` 把单个 argv 元素封在 128 KiB,再往上 `execve` 直接
    `E2BIG` —— 报错的是**操作系统**,引擎连被叫起来的机会都没有:没有回执、没有
    退出码 2、没有一句能翻译给运维的人的话(壳给的是 rc 126「参数列表过长」)。
    一条 `data:` 立绘到 1 MiB 是常事(base64 之后约是原图的 4/3),所以"契约说
    能写 1 MiB"和"这扇门能传 128 KiB"之间那一段,从前只有撞上去才知道。

    `--portrait-file` 补的就是这一段:URI 走文件或标准输入,不过 argv。
    这里用 200 KiB —— **刚好在那道坎的另一边**,写成 1 KiB 的话这条测试永远绿,
    而它要防的那件事从来没被验到。
    """
    import base64

    world = _running_world(tmp_path, None, None)
    world.close()

    uri = "data:image/png;base64," + base64.b64encode(b"\0" * 150_000).decode()
    assert len(uri) > 128 * 1024, "小于那道坎的话这条测试什么也没验"
    path = tmp_path / "portrait.txt"
    path.write_text(uri + "\n", encoding="utf-8")     # 尾巴上的换行是必然的,得吃掉

    done = run_cli("agent", "set-card", "--world-id", "w", "--agent", "a0",
                   "--portrait-file", str(path), "--json")
    assert done.returncode == 0, done.stderr
    roster = json.loads(run_cli("roster", "--world-id", "w", "--json").stdout)
    assert {r["agent_id"]: r for r in roster["agents"]}["a0"]["portrait"] == uri

    # `-` = 标准输入:运维台那个一次性容器里连一个能写的文件都不一定有。
    piped = run_cli("agent", "set-card", "--world-id", "w", "--agent", "a1",
                    "--portrait-file", "-", "--json",
                    input="https://cdn.example.com/a1.png")
    assert piped.returncode == 0, piped.stderr
    roster = json.loads(run_cli("roster", "--world-id", "w", "--json").stdout)
    assert {r["agent_id"]: r for r in roster["agents"]}["a1"]["portrait"] \
        == "https://cdn.example.com/a1.png"


def test_立绘文件那几种拒绝_每一种都说得出为什么(tmp_path):
    """这扇门新开的口子上,几种拒绝各有各的理由,而**猜错了都是安静的**。"""
    world = _running_world(tmp_path, {"portrait": "https://cdn.example.com/old.png"})
    world.close()

    def attempt(*extra):
        return run_cli("agent", "set-card", "--world-id", "w", "--agent", "a0",
                       *extra, "--json")

    # 1. 两句都在说这一格写成什么 —— 挑哪句都是猜。
    both = attempt("--portrait", "https://cdn.example.com/x.png",
                   "--portrait-file", str(tmp_path / "any.txt"))
    assert both.returncode == 2 and "usage:" not in both.stderr

    # 2. 读不了。**别把它读成"作者要抹掉这一格"** —— 一次失败的写会安静地
    #    删掉线上那张立绘。
    gone = attempt("--portrait-file", str(tmp_path / "nope.txt"))
    assert gone.returncode == 2 and "nope.txt" in gone.stderr

    # 3. 空文件同理:几乎总是"写失败了 / 路径写错了",不是"抹掉它"。
    empty = tmp_path / "empty.txt"
    empty.write_text("\n", encoding="utf-8")
    blank = attempt("--portrait-file", str(empty))
    assert blank.returncode == 2 and "--portrait ''" in blank.stderr

    # 4. 中间断了行:闸只看 scheme 和字节数,这种 URI 它照放 —— 出去是一张断的图。
    folded = tmp_path / "folded.txt"
    folded.write_text("data:image/png;base64,AAAA\nBBBB\n", encoding="utf-8")
    wrapped = attempt("--portrait-file", str(folded))
    assert wrapped.returncode == 2 and "空白" in wrapped.stderr

    # 而文件里写了一条**坏 URI** 时,拒绝它的仍然是原来那道闸(这扇门不另判一次)。
    bad = tmp_path / "bad.txt"
    bad.write_text("portraits/a0.png\n", encoding="utf-8")
    assert attempt("--portrait-file", str(bad)).returncode == 2

    # 每一次都没写进去 —— 拒绝了还改了一半是这一类命令最坏的收场。
    roster = json.loads(run_cli("roster", "--world-id", "w", "--json").stdout)
    assert {r["agent_id"]: r for r in roster["agents"]}["a0"]["portrait"] \
        == "https://cdn.example.com/old.png"


def test_set_card_command_refuses_a_world_that_does_not_exist(tmp_path):
    """写命令同样不许**创建**世界:抄错 world_id 会当场创世,而回执看上去是成功的。"""
    world = _running_world(tmp_path, None)
    world.close()
    assert run_cli("agent", "set-card", "--world-id", "w", "--agent", "a0",
                   "--billing", "lead", "--json").returncode == 0

    missing = run_cli("agent", "set-card", "--world-id", "no-such-world",
                      "--agent", "a0", "--billing", "lead", "--json")
    assert missing.returncode == 2
    assert "no-such-world" in missing.stderr
    assert "usage:" not in missing.stderr


def test_contract_declares_the_write_command_too():
    """`character_card` 段要说得出**写**出口 —— 只报 `read_command` 的话,创作台
    与运维台只能靠版本号猜"这支引擎改不改得动一个跑着的世界",而猜错不报错。"""
    payload = json.loads(run_cli("contract", "--json").stdout)
    assert payload["character_card"]["write_command"] == "agent set-card"
