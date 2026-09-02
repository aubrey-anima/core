"""内容包:一个**跑着的**世界收得下一份更新(3.10.0,周更链路 2a-①)。

老板 2026-09-02:「我要的是创作者能控制这个东西,相当于每周都能有更新」。

在这之前,往一个跑着的世界装编辑包**十一段落地、五段静默**,而屏幕上只有一句话、
退出码是 0(裁决 FOR-STUDIO §3.62 (a) 那张表)。这一层要守的东西一条都不在"功能"上:

- **零点跟着来路走。** `trigger.at` 的语义是「不早于」,所以一份写着 `day: 0..6` 的
  第 2 周包装进一个跑到第 40 天的世界,**八拍在同一 tick 全部烧掉**,零报错 ——
  这正是引擎从前拒绝合并节拍的唯一正确理由。
- **`max` 是唯一能同时让两句话成立的写法**:老玩家从包落地起算,而包落地之后才
  进来的新玩家从他自己那天起算。
- **id 撞车当场拒。** `beat_fired` 那份历史按 id 配对,重了就再也分不出是谁响过,
  而分不出的样子是"这一拍又响了一次"或者"再也不响",没有一处会报错。
"""
from __future__ import annotations

import json

import pytest

from _worldfile import open_world_at, redis_for, run_cli, write_seed_file

pytest.importorskip("fakeredis")


BASE = {
    "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}],
    "agents": [{"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"}],
}


def _pack(tmp_path, name, *, pack, engine_min="3.10.0", **sections) -> str:
    """一份内容包。**封皮带 `engine_min: 3.10.0`** —— 那是它真实的形状:
    老引擎见到 `pack` 段是开不了机的硬失败,所以这一格是承重的(2a-① 验收 C)。"""
    from anima_world.world_file import (
        WorldFileManifest, seed_to_author_records, write_world_file,
    )

    seed = {"pack": pack}
    seed.update(sections)
    path = tmp_path / f"{name}.cyberworld"
    write_world_file(
        path, WorldFileManifest(world_id="fixture", engine_min=engine_min),
        seed_to_author_records(seed), compress=False, checksum=False,
    )
    return str(path)


def _beat(bid, day, *, since=None, for_each=None, summary="剧情"):
    at = {"day": day}
    if since is not None:
        at["since"] = since
    beat = {"id": bid, "trigger": {"at": at},
            "payload": [{"op": "memory", "agent_id": "甲", "summary": summary}]}
    if for_each is not None:
        beat["for_each"] = for_each
    return beat


def _world(tmp_path, name="w", **over):
    seed = dict(BASE)
    seed.update(over)
    path = write_seed_file(tmp_path / f"{name}-base.cyberworld", seed)
    return open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                         force_mock_llm=True)


def _fired(world):
    return sorted(
        (e.payload.get("beat_id"), str(e.payload.get("for") or ""))
        for e in world.scheduler.event_log.replay()
        if e.type == "beat_fired"
    )


# ── 一、验收标准 ①:第 2 周包进一个跑了 40 天、有两个老玩家的世界 ───────────

def test_四十天的世界装第二周包_三拍从落地那天起按天响_没有一拍在装的那一tick全烧(tmp_path):
    """🔴 **这一条是这一单的全部理由。**

    没有 `since`(零点跟来路走)的话,`day: 0/1/2` 三拍在装包那一 tick **一起烧掉** ——
    而"烧掉"是不可逆的:`beat_fired` 是历史,重启不重放。
    """
    ticks_per_day = 288
    with _world(tmp_path) as world:
        world.tick(3)
        world.chat_store  # 触一下,确保世界是活的
        # 两个老玩家先进来(第 0 天),再把世界推到第 40 天。
        world._touch_player("老甲")
        world._touch_player("老乙")
        world.tick(ticks_per_day * 40)
        assert world.state()["world_time"]["day"] == 40

        path = _pack(tmp_path, "week2",
                     pack={"id": "第二周", "version": "1.0.0", "note": "社团活动"},
                     beats=[_beat("社团", 0), _beat("夜宵", 1), _beat("实习课", 2)])
        receipt = world.install_pack(path)
        assert receipt["pack"] == "第二周"
        assert receipt["day"] == 40, "落地那天必须是**今天**,不是世界第 0 天"
        assert sorted(receipt["beats"]) == ["夜宵", "实习课", "社团"]

        # 装的那一刻一拍都没响 —— 拍在下一 tick 才判。
        assert _fired(world) == []
        world.tick(1)
        assert [b for b, _ in _fired(world)] == ["社团"], (
            "只有 `day: 0` 那一拍该响 —— 另外两拍还没到"
        )
        world.tick(ticks_per_day)
        assert sorted(b for b, _ in _fired(world)) == ["夜宵", "社团"]
        world.tick(ticks_per_day)
        assert sorted(b for b, _ in _fired(world)) == ["夜宵", "实习课", "社团"]


def test_写since_world的那一拍_零点还是世界第0天(tmp_path):
    """逃生舱。**没有它,「世界第 100 天」这种绝对时刻就写不出来了。**

    ⚠️ 这一条同时是上面那条的**反向闸**:上面那三拍要是靠"装包就把 day 加上今天"
    实现的,这一条会红。
    """
    from anima_world.__main__ import PackInstallError

    with _world(tmp_path, name="esc") as world:
        world.tick(288 * 40)
        path = _pack(tmp_path, "abs", pack={"id": "绝对", "version": "1.0.0"},
                     beats=[_beat("世界第三天", 3, since="world")])
        # 🔴 **默认拒绝**(2a-① 验收 C):`since: "world"` 的零点是世界第 0 天,
        # 而今天已经第 40 天 —— 这一拍装进去下一 tick 就烧,而 `beat_fired` 是历史。
        # 装的时候手上有拍表 + `since` + 今天,**算得出来就不许让它安静地发生**。
        with pytest.raises(PackInstallError) as raised:
            world.install_pack(path)
        assert "一起响掉" in str(raised.value) and "世界第三天" in str(raised.value)
        assert world.packs() == [], "拒了还写进去了"

        # `--force` = 我就是要它们立刻全响。
        receipt = world.install_pack(path, force=True)
        assert receipt["forced"] is True
        world.tick(1)
        assert [b for b, _ in _fired(world)] == ["世界第三天"], (
            "`since: \"world\"` 那一拍的零点是世界第 0 天,而世界已经第 40 天了 —— 该响"
        )


def test_老玩家和新玩家_per_player那一拍取max(tmp_path):
    """🔴 **`max` 是唯一能同时让两句话成立的写法。**

    老玩家(第 0 天进来的)从**包落地那天**起算 —— 否则第 2 周的剧情对他永远不响
    (他的第 1 天早在 39 天前过完了)。
    包落地之后才进来的新玩家从**他自己那天**起算 —— 否则他一进门就被一堆过期的
    拍砸中。
    """
    with _world(tmp_path, name="mx") as world:
        world.tick(3)
        world._touch_player("老甲")
        world.tick(288 * 40)
        path = _pack(tmp_path, "perp", pack={"id": "第二周", "version": "1.0.0"},
                     beats=[_beat("第二天那封信", 1, for_each={"node": "player"})])
        world.install_pack(path)

        world.tick(1)
        assert _fired(world) == [], "老玩家的零点是包落地那天,`day: 1` 还没到"
        world.tick(288)
        assert _fired(world) == [("第二天那封信", "player:老甲")]

        # 新玩家在包落地之后第 1 天才进来 —— 他的零点是**他自己进来那天**。
        world._touch_player("新丙")
        world.tick(1)
        assert ("第二天那封信", "player:新丙") not in _fired(world), (
            "新玩家一进门就被一条过期的拍砸中了 —— 零点该取 max"
        )
        world.tick(288)
        assert ("第二天那封信", "player:新丙") in _fired(world)


# ── 二、验收标准 ①的另一半:身份、开关、世界观、新人新地点 ─────────────────

def test_装包落三段_开关和世界观都真的换了_而它们此前一个字都不说(tmp_path):
    """🔴 **`config` 与 `world_setting` 这两段是 2026-09-02 量出来的静默段**:
    `--world-file` 那条路上 `_apply_seed_config_at_genesis` 钉在 `fresh_world` 上、
    `_seed_world_setting` 由 `not persisted` 把门,两段都不装,而且一个字都不说。
    """
    with _world(tmp_path, name="cfg") as world:
        world.tick(3)
        assert world.config_get("narrative.player.enabled") is False
        before = world.world_setting()["text"]

        path = _pack(tmp_path, "sw", pack={"id": "第二周", "version": "1.0.0"},
                     config={"narrative.player.enabled": True},
                     world_setting="第二周的世界观。")
        receipt = world.install_pack(path)
        assert receipt["config"] == ["narrative.player.enabled"]
        assert receipt["world_setting"] is True
        assert world.config_get("narrative.player.enabled") is True
        assert world.world_setting()["text"] == "第二周的世界观。" != before


def test_装包带新角色和新地点_他们真的进了世界而且重启还在(tmp_path):
    """名册、位置、关系、随身、钱**全是事件的投影** —— 只把人注册进调度器的话,
    他这一轮活着,重启之后就没有了,而这中间他说过的话还在日志里指着一个不存在的人。
    """
    client = redis_for(tmp_path / "ppl.db")
    with _world(tmp_path, name="ppl") as world:
        world.tick(3)
        path = _pack(tmp_path, "cast", pack={"id": "第二周", "version": "1.0.0"},
                     locations=[{"id": "yard", "name": "院子", "description": "新地点"}],
                     agents=[{"id": "乙", "name": "乙", "location": "yard",
                              "personality": "新来的"}])
        receipt = world.install_pack(path)
        assert receipt["agents"] == ["乙"] and receipt["locations"] == ["yard"]
        assert "乙" in world.scheduler.agents
        assert "yard" in {loc["id"] for loc in world.scheduler.location_store.all()}

    from anima_world.api import World

    with World.open("w", redis=client, force_mock_llm=True) as again:
        assert "乙" in again.scheduler.agents, "重启之后新角色没了 —— 事件没发出去"
        assert [p["id"] for p in again.packs()] == ["第二周"]


def test_packs那一格折自事件_没有第二张表(tmp_path):
    """**装了哪几周**折自 `pack_installed`,和余额折自 `payment` 逐字同一种。

    🔴 判据是**把投影抹掉再重折**:存了第二份的话这一条照旧绿,而两份真相里
    有一份不更新正是这个仓库最怕的坏法。
    """
    with _world(tmp_path, name="proj") as world:
        world.tick(3)
        world.install_pack(_pack(tmp_path, "p1",
                                 pack={"id": "第二周", "version": "1.0.0", "note": "n"}))
        rows = world.packs()
        assert [r["id"] for r in rows] == ["第二周"] and rows[0]["note"] == "n"

        world.scheduler.reset_projection(world.scheduler.event_log.replay())
        assert [r["id"] for r in world.packs()] == ["第二周"]


def test_同一个包升级_零点不动而版本号跟着换(tmp_path):
    """🔴 **零点是"这一周的内容什么时候进这个世界的",不是"作者最后一次改它"。**

    让它跟着升级走的话,一次错别字修订会把整份第 2 周剧情往后推一周,而没有一处
    会报错:那几拍只是"还没到"。
    """
    with _world(tmp_path, name="up") as world:
        world.tick(3)
        world.install_pack(_pack(tmp_path, "v1", pack={"id": "第二周", "version": "1.0.0"}))
        world.tick(288 * 5)
        world.install_pack(_pack(tmp_path, "v2", pack={"id": "第二周", "version": "1.1.0"}))
        row = world.packs()[0]
        assert row["version"] == "1.1.0"
        assert row["day"] == 0, "零点被升级推走了"


# ── 三、拒绝:每一条都要说得出理由 ──────────────────────────────────────────

def test_没有pack段的包_当场拒(tmp_path):
    from anima_world.__main__ import PackInstallError

    with _world(tmp_path, name="nop") as world:
        world.tick(3)
        path = write_seed_file(tmp_path / "plain.cyberworld", {"beats": [_beat("x", 0)]})
        with pytest.raises(PackInstallError) as raised:
            world.install_pack(path)
        assert "`pack` 段" in str(raised.value)


def test_拍的id撞了车_当场拒_而且一个字节都没写(tmp_path):
    """🔴 **拒绝时一个字节都不写** —— 和能力调用被拒时那条逐字同一。

    半装进去一份包比装不进去坏得多:作者看到红灯,而世界里已经多了三个地点。
    """
    from anima_world.__main__ import PackInstallError

    with _world(tmp_path, name="dup", beats=[_beat("社团", 0)]) as world:
        world.tick(3)
        path = _pack(tmp_path, "dup2", pack={"id": "第二周", "version": "1.0.0"},
                     locations=[{"id": "yard", "name": "院子", "description": "d"}],
                     beats=[_beat("社团", 1)])
        with pytest.raises(PackInstallError) as raised:
            world.install_pack(path)
        assert "已经有了" in str(raised.value)
        assert world.packs() == []
        assert "yard" not in {loc["id"] for loc in world.scheduler.location_store.all()}


def test_带状态记录的包_当场拒(tmp_path):
    """一份跑过的世界导出来的 dump 不是内容包 —— 把它"装"进另一个世界没有一种
    正确答案(合并会重号,覆盖会抹掉这期间发生的一切)。"""
    from anima_world.__main__ import PackInstallError

    with _world(tmp_path, name="dump") as world:
        world.tick(3)
        out = str(tmp_path / "dump.cyberworld")
        world.export_snapshot(out, world_id="dumped", name="导出的")
        with pytest.raises(PackInstallError) as raised:
            world.install_pack(out)
        assert "状态记录" in str(raised.value)


def test_坏包被拒的是中文行_不是堆栈(tmp_path):
    from anima_world.__main__ import PackInstallError

    with _world(tmp_path, name="bad") as world:
        world.tick(3)
        path = _pack(tmp_path, "badpack", pack={"id": "第二周", "version": "1.0.0"},
                     kinds=[{"id": "tree", "quantities": {"树高": {"default": 1.0}},
                             "affordances": {"看": {"set": {"树髙": "树高 + 1"}}}}])
        with pytest.raises(PackInstallError) as raised:
            world.install_pack(path)
        assert raised.value.errors and all(isinstance(e, str) for e in raised.value.errors)


# ── 四、CLI:两条出口都真敲一遍 ─────────────────────────────────────────────

def test_cli装包再列出来(tmp_path):
    """**文档里承诺了一句用户会照着敲的命令,就去敲一遍。**"""
    client = redis_for(tmp_path / "cli.db")
    with _world(tmp_path, name="cli") as world:
        world.tick(3)
    path = _pack(tmp_path, "clipack",
                 pack={"id": "第二周", "version": "1.0.0", "note": "社团活动"},
                 beats=[_beat("社团", 0)])
    r = run_cli("pack", "install", path, "--world-id", "w", "--json")
    assert r.returncode == 0, r.stderr
    receipt = json.loads(r.stdout)
    assert receipt["pack"] == "第二周" and receipt["beats"] == ["社团"]

    r = run_cli("pack", "list", "--world-id", "w", "--json")
    assert r.returncode == 0, r.stderr
    assert [row["id"] for row in json.loads(r.stdout)] == ["第二周"]

    r = run_cli("pack", "list", "--world-id", "w")
    assert "第二周" in r.stdout and "社团活动" in r.stdout, r.stdout


def test_cli对着一个不存在的世界装包_拒绝而不是当场创世(tmp_path):
    """`5ce6aed` 那条老教训的同一种:抄错名字会创世,而作者以为他更新了线上那个。"""
    redis_for(tmp_path / "ghost.db")
    path = _pack(tmp_path, "ghost", pack={"id": "第二周", "version": "1.0.0"})
    r = run_cli("pack", "install", path, "--world-id", "根本没有这个世界")
    assert r.returncode == 2
    assert "还没有" in r.stderr


# ── 五、那五段从今天起「当场说」(2026-09-02 量出来的一整族)────────────────
#
# 一份内容全落在 `beat` / `config` / 在册者 `personality` / `world_setting` /
# 在册者 `memory` 这五段上的第 2 周包,走 `--world-file` 装进一个跑着的世界:
# **世界一个字都不变**,而 `world check --edit` 答 `loadable: true, errors: []`、
# `simulate --world-file` 退 **0**,屏幕上只有关于 `beat` 的那一句 warning。

_SILENT_PACK = {
    "beats": [_beat("第三幕", 0)],
    "config": {"narrative.player.enabled": True},
    "agents": [{"id": "甲", "name": "甲", "location": "cafe",
                "personality": "改过的性格"}],
    "memories": [{"agent_id": "甲", "kind": "seed", "summary": "新剧情给她的记忆"}],
    "world_setting": "第二周的世界观。",
}


def test_带剧情的包走world_file装进一个已经有剧情的世界_退出码2(tmp_path):
    """🔴 **机器读的是退出码。** 上一版这里是一句 `logger.warning` + 退 **0** ——
    一份带着第 2 周剧情的包"装"上去,那几拍一条都没进去,而脚本读到的是"成功"。
    和收件箱 D32 治过的那条逐字同一种病、同一种治法。
    """
    redis_for(tmp_path / "rc.db")
    base = write_seed_file(tmp_path / "rc-base.cyberworld",
                           dict(BASE, beats=[_beat("第一幕", 0)]))
    r = run_cli("simulate", "--world-id", "w", "--ticks", "1",
                "--world-file", base, "--llm", "mock")
    assert r.returncode == 0, r.stderr

    later = write_seed_file(tmp_path / "rc-later.cyberworld",
                            dict(BASE, beats=[_beat("第三幕", 0)]))
    r = run_cli("simulate", "--world-id", "w", "--ticks", "0",
                "--world-file", later, "--llm", "mock")
    assert r.returncode == 2, f"退出码还是 {r.returncode} —— 机器读到的是「成功」"
    assert "pack install" in r.stderr


def test_另外四段_开机当场说而且点得出名字(tmp_path, caplog):
    """开机手上有名册,所以这几句**点得出名字** —— 离线那两扇门只说得出条件句。"""
    import logging

    redis_for(tmp_path / "say.db")
    base = write_seed_file(tmp_path / "say-base.cyberworld", BASE)
    from anima_world.api import World

    client = redis_for(tmp_path / "say.db")
    World.open("w", redis=client, world_file=base, force_mock_llm=True).close()

    silent = dict(_SILENT_PACK)
    silent.pop("beats")     # 那一格是拒绝,不是一句话
    later = write_seed_file(tmp_path / "say-later.cyberworld", silent)
    with caplog.at_level(logging.WARNING):
        World.open("w", redis=client, world_file=later, force_mock_llm=True).close()
    said = "\n".join(r.getMessage() for r in caplog.records)
    for needle in ("开关", "世界观", "personality", "memory"):
        assert needle in said, f"「{needle}」那一段还是静默的:{said[:400]}"
    assert "甲" in said, "在册的那个人点不出名字"

    # 而且它们确实没装进去 —— 说了一句和真的装了是两件事。
    with World.open("w", redis=client, force_mock_llm=True) as world:
        assert world.config_get("narrative.player.enabled") is False


def test_离线两扇门对同一份包说同一句话(tmp_path):
    """🔴 **句子是同一份常量**,不是两处各写一遍 —— 抄第二遍的那天,两边会先给出
    不同的措辞,再由某个作者按其中一句去改一个没错的地方。

    ⚠️ 离线**答不出**目标世界有没有剧情 / 谁在册,所以那两格是条件句。
    这就是 `--edit` 那条「查得动查不动」的分界。
    """
    from anima_world.__main__ import EDIT_PATH_NOTES

    path = write_seed_file(tmp_path / "off.cyberworld", _SILENT_PACK)
    r = run_cli("world", "check", path, "--edit", "--json")
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    said = "\n".join(payload["warnings"])
    for key in ("beats", "config", "world_setting", "personality", "memories"):
        assert EDIT_PATH_NOTES[key] in said, f"离线没说 {key} 那一句"
    assert "答不出来" in said, "离线得说清哪一格它答不出来,而不是猜一个答案"


def test_那五句话里不许有裸markdown星号(tmp_path):
    """它们会**原样印在人的终端上**(`_print_check_human` 逐条打 warnings),
    而屏幕上 `**` 就是两个星号。

    ⚠️ `test_屏幕上不许出现裸markdown星号` 看不见这一条 —— 它只扫 `print()` 实参
    与 `help=`,而这几句是先攒进列表再由别处印的,那正是它自己写明的盲区。
    """
    from anima_world.__main__ import EDIT_PATH_NOTES

    for key, sentence in EDIT_PATH_NOTES.items():
        assert "**" not in sentence, (key, sentence)

    path = write_seed_file(tmp_path / "stars.cyberworld", _SILENT_PACK)
    r = run_cli("world", "check", path, "--edit")
    assert "**" not in r.stdout, r.stdout

    # 🔴 **别数源码里的星号,去问屏幕**(这条判据 3.7.0 立的)。`pack` 这一族的
    # 真出口逐条敲一遍 —— 那道 AST 闸只扫 `print()` 实参与 `help=`,而这一屏上
    # 有几句是先攒进回执再印的。
    redis_for(tmp_path / "stars.db")
    base = write_seed_file(tmp_path / "stars-base.cyberworld", BASE)
    from anima_world.api import World

    client = redis_for(tmp_path / "stars.db")
    World.open("w", redis=client, world_file=base, force_mock_llm=True).close()
    good = _pack(tmp_path, "stars-ok", pack={"id": "第二周", "version": "1.0.0"},
                 beats=[_beat("社团", 0)],
                 config={"narrative.player.enabled": True},
                 world_setting="第二周。")
    for argv in (("pack", "--help"), ("pack", "install", "--help"),
                 ("pack", "list", "--help"),
                 ("pack", "install", good, "--world-id", "w"),
                 ("pack", "list", "--world-id", "w"),
                 ("pack", "install", good, "--world-id", "w"),      # 第二遍 = 撞车那一屏
                 ("pack", "install", good, "--world-id", "没有这个世界")):
        r = run_cli(*argv)
        assert "**" not in (r.stdout + r.stderr), (argv, r.stdout, r.stderr)


def test_验收标准第一条原样_四十天两个老玩家_三拍per_player_新玩家也响(tmp_path):
    """**任务单 §3.1 验收标准 ① 逐字照抄的那一条。**

    一份第 2 周包(`pack` 段 + 三拍 `for_each: player` + 一个 config 键 + 世界观一段)
    装进一个已跑 40 世界日、有两个老玩家的世界:
    `:packs`(= `World.packs()`)有它 · `pack_installed` 在 · 三拍对两个老玩家
    **从落地那天起**各响一次 · 之后进来的第三个玩家也响 · **没有一拍在装的那一 tick 全烧**。
    """
    day = 288
    with _world(tmp_path, name="acc") as world:
        world.tick(3)
        world._touch_player("老甲")
        world._touch_player("老乙")
        world.tick(day * 40)
        assert world.state()["world_time"]["day"] == 40

        path = _pack(
            tmp_path, "acc-week2",
            pack={"id": "第二周", "version": "1.0.0", "note": "社团活动 / 夜宵 / 实习课"},
            beats=[_beat("社团", 0, for_each={"node": "player"}),
                   _beat("夜宵", 1, for_each={"node": "player"}),
                   _beat("实习课", 2, for_each={"node": "player"})],
            config={"narrative.player.enabled": True},
            world_setting="第二周的旧港。")
        receipt = world.install_pack(path)
        assert receipt["day"] == 40

        # `:packs` 有它,`pack_installed` 在事件流里。
        assert [r["id"] for r in world.packs()] == ["第二周"]
        assert any(e.type == "pack_installed"
                   for e in world.scheduler.event_log.replay())
        # 开关与世界观当场生效。
        assert world.config_get("narrative.player.enabled") is True
        assert world.world_setting()["text"] == "第二周的旧港。"

        # 🔴 装的那一 tick 一拍都没烧。
        assert _fired(world) == []

        world.tick(1)
        assert sorted(_fired(world)) == [("社团", "player:老乙"), ("社团", "player:老甲")]
        world.tick(day)
        assert ("夜宵", "player:老甲") in _fired(world)
        world.tick(day)
        assert ("实习课", "player:老乙") in _fired(world)
        # 每人各一次,不多不少。
        assert len(_fired(world)) == 6, sorted(_fired(world))

        # 第三个玩家在包落地之后才进来 —— 从**他自己**进来那天起算,一样响三次。
        world._touch_player("新丙")
        world.tick(1)
        assert ("社团", "player:新丙") in _fired(world)
        world.tick(day * 2)
        mine = sorted(b for b, who in _fired(world) if who == "player:新丙")
        assert mine == ["夜宵", "实习课", "社团"], mine


# ── 六、2a-① 验收退回那一轮的判据(3.10.0,验收 A/C 真敲出来的)──────────────

def test_只带一拍的包_不许把引擎内置的地图和名册灌进来(tmp_path):
    """🔴 **验收 A ⑪。** `_seed_world_defs` 的「没写 = 回落内置那份」对**创世**是对的
    (一个没写 `locations` 的世界总得站得住脚),对 `install_pack` 是**错的,而且
    错得很安静**:一份只带一拍的第 2 周包装进一个跑着的卡塞尔世界,地图上凭空多出
    `cafe`/`home`/`workshop` 三个地点,还跟着三条 `location_join` 进日志 ——
    **事件是只追加的,撤不回来**。全程零报错。
    """
    base = {
        "locations": [{"id": "卡塞尔", "name": "卡塞尔学院", "description": "d"}],
        "agents": [{"id": "路明非", "name": "路明非", "location": "卡塞尔",
                    "personality": "p"}],
    }
    path = write_seed_file(tmp_path / "kassel.cyberworld", base)
    with open_world_at(str(tmp_path / "k.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.tick(2)
        before_actions = set(world.scheduler.bt_store.shared_action_ids())
        world.install_pack(_pack(
            tmp_path, "onebeat", pack={"id": "第二周", "version": "1.0.0"},
            beats=[_beat("社团", 0, summary="社团招新")]))
        assert sorted(l["id"] for l in world.scheduler.location_store.all()) == ["卡塞尔"]
        assert set(world.scheduler.bt_store.shared_action_ids()) == before_actions, (
            "`:bt_actions` 多出了指向不存在的人的行"
        )
        joined = [e.payload["id"] for e in world.scheduler.event_log.replay()
                  if e.type == "location_join"]
        assert joined == ["卡塞尔"], joined


def test_带地点不带角色的包_也不许灌名册(tmp_path):
    """⚠️ 这是**龙族第 2 周包的形状**(加地点、不加人),而它比上一条更阴:
    地图看着没问题,而 `:bt_actions` 里多出 `chat_with_夏/柔/遥` 指着三个不存在的人。"""
    base = {
        "locations": [{"id": "卡塞尔", "name": "卡塞尔学院", "description": "d"}],
        "agents": [{"id": "路明非", "name": "路明非", "location": "卡塞尔",
                    "personality": "p"}],
    }
    path = write_seed_file(tmp_path / "k2.cyberworld", base)
    with open_world_at(str(tmp_path / "k2.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.tick(2)
        before = set(world.scheduler.bt_store.shared_action_ids())
        world.install_pack(_pack(
            tmp_path, "locsonly", pack={"id": "第二周", "version": "1.0.0"},
            locations=[{"id": "训练场", "name": "训练场", "description": "d"}]))
        extra = set(world.scheduler.bt_store.shared_action_ids()) - before
        assert not any(a.startswith("chat_with_") for a in extra), extra
        assert sorted(l["id"] for l in world.scheduler.location_store.all()) == [
            "卡塞尔", "训练场"]


def test_同一个包升级_上一版那几拍的零点不动(tmp_path):
    """🔴 **验收 A ⑩。** `sections` 从前是整片替换 —— 第 40 天装 v1.0.0(两拍),
    第 41 天装 v1.1.0 带**别的**拍,那两拍就从 `pack_days_from` 上消失,零点读作 0,
    下一 tick 一起烧掉。而那条升级用例装的两份**都不带 beats**,所以它看不见。
    """
    day = 288
    with _world(tmp_path, name="up2") as world:
        world.tick(day * 40)
        world.install_pack(_pack(
            tmp_path, "w2v1", pack={"id": "第二周", "version": "1.0.0"},
            beats=[_beat("社团", 5), _beat("夜宵", 6)]))
        world.tick(day)                                   # 第 41 天
        world.install_pack(_pack(
            tmp_path, "w2v2", pack={"id": "第二周", "version": "1.1.0"},
            beats=[_beat("实习课", 1)]))
        assert _fired(world) == [], "升级那一刻把上一版两拍烧了"
        world.tick(1)
        assert _fired(world) == [], "零点被打回世界第 0 天了"
        row = world.packs()[0]
        assert sorted(row["sections"]["beats"]) == ["夜宵", "实习课", "社团"], row
        assert row["beat_days"]["社团"] == 40 and row["beat_days"]["实习课"] == 41, row
        # 社团 day5 从第 40 天起算 → 第 45 天响;实习课 day1 从第 41 天起 → 第 42 天。
        world.tick(day)                                   # 第 42 天
        assert [b for b, _ in _fired(world)] == ["实习课"], _fired(world)


def test_装了什么和文件里写了什么_是两格(tmp_path):
    """🔴 **验收 C ②:两份真相。** 同一份包回执 `agents: []`(对),而
    `pack list` 却报 `sections.agents: ["夏"]`(抄的是文件)—— 读的人分不出哪份对。
    """
    with _world(tmp_path, name="two") as world:
        world.tick(3)
        receipt = world.install_pack(_pack(
            tmp_path, "already", pack={"id": "第二周", "version": "1.0.0"},
            agents=[{"id": "甲", "name": "甲", "location": "cafe",
                     "personality": "改过的"}]))
        assert receipt["agents"] == [], "甲已经在册,他不是这一趟进来的"
        row = world.packs()[0]
        assert "agents" not in row["sections"], row["sections"]
        assert row["declared"]["agents"] == ["甲"], row["declared"]


def test_在册的人的人设和记忆装不进去_而这件事说得出来(tmp_path, caplog):
    """🔴 **验收 C ①**:引擎一个字不说、rc 0,而离线门反而说了两句漂亮的。"""
    import logging

    with _world(tmp_path, name="skip") as world:
        world.tick(3)
        with caplog.at_level(logging.WARNING):
            receipt = world.install_pack(_pack(
                tmp_path, "skips", pack={"id": "第二周", "version": "1.0.0"},
                agents=[{"id": "甲", "name": "甲", "location": "cafe",
                         "personality": "改过的性格"}],
                memories=[{"agent_id": "甲", "kind": "seed", "summary": "新的一条"}]))
        assert receipt["skipped"]["personality"] == ["甲"], receipt["skipped"]
        assert receipt["skipped"]["memories"] == 1
        assert "还没做" in receipt["skipped"]["reason"]
        assert any("没有装进去" in r.getMessage() for r in caplog.records), (
            [r.getMessage() for r in caplog.records])


def test_插件降级被拒时_世界一个字节都没变(tmp_path):
    """🟡 **验收 A ⑫**:`except PluginError` 排在锁里六次写之后 —— 一份带新地点 +
    插件降级的包被拒,而地点已经进了地图、`packs()` 却是空的。
    **半装进去一份包比装不进去坏得多。**"""
    from anima_world.__main__ import PackInstallError

    qi = {"id": "qi", "version": "2.0.0", "label": "灵力",
          "facts": {"灵力": {"bearer": "agent", "shape": "number", "default": 10.0,
                             "visibility": "self"}}}
    base = dict(BASE, plugins=[qi])
    path = write_seed_file(tmp_path / "pl.cyberworld", base)
    with open_world_at(str(tmp_path / "pl.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.tick(2)
        bad = _pack(tmp_path, "downgrade", pack={"id": "第二周", "version": "1.0.0"},
                    plugins=[{**qi, "version": "1.0.0"}],
                    locations=[{"id": "院子", "name": "院子", "description": "d"}])
        with pytest.raises(PackInstallError) as raised:
            world.install_pack(bad)
        assert "不降级" in str(raised.value)
        assert sorted(l["id"] for l in world.scheduler.location_store.all()) == ["cafe"]
        assert world.packs() == []


def test_封皮说3_9_0而包里有pack段_三扇门同一句拒(tmp_path):
    """🟡 **验收 C ⑥**:`engine_min: "3.9.0"` 而带 `pack` 段的包,从前
    `world check --edit` 说可用、`pack install` 退 0 —— 它在 3.9.0 上开不了机。"""
    from anima_world.__main__ import PackInstallError

    old = _pack(tmp_path, "old-engine", engine_min="3.9.0",
                pack={"id": "第二周", "version": "1.0.0"})
    r = run_cli("world", "check", old, "--edit", "--json")
    payload = json.loads(r.stdout)
    assert payload["loadable"] is False, payload
    assert any("engine_min" in e for e in payload["errors"]), payload["errors"]

    r = run_cli("validate", "world", old, "--edit")
    assert r.returncode == 2, r.stdout

    with _world(tmp_path, name="eng") as world:
        world.tick(2)
        with pytest.raises(PackInstallError) as raised:
            world.install_pack(old)
        assert "engine_min" in str(raised.value)


def test_封皮没写engine_min_是一句警告不是一条错(tmp_path):
    """⚠️ **「没写」和「写了一个更低的数」是两件事。** 后者是一句可以被证伪的假话,
    前者只是没说 —— 把没说也判成错,每一份手写的世界文件都会在这扇门上变红。"""
    silent = _pack(tmp_path, "no-engine", engine_min="",
                   pack={"id": "第二周", "version": "1.0.0"})
    r = run_cli("world", "check", silent, "--edit", "--json")
    payload = json.loads(r.stdout)
    assert payload["loadable"] is True, payload["errors"]
    assert any("没写 `engine_min`" in w for w in payload["warnings"]), payload["warnings"]


def test_库里一条3_9_0留下的拍_3_10_0开得了机而且说一句(tmp_path, caplog):
    """🔴 **验收 B ③:一次收紧不许把已经发出去的世界锁在门外。**

    3.10.0 给 `trigger` / `trigger.at` 加了闭集,而开机会把库里存量的拍**重验一遍**
    —— 一个 3.9.0 上跑得好好的世界(那一版这两层一个键都不查,写错一个字母是照收
    然后丢掉)换上 3.10.0 就 `BOOT FAILED`。

    ⚠️ 判据是**直接往库里塞**,不是走文件:文件那条路照旧严格,而这一条要验的
    正是"它已经在库里了"这件事。
    """
    import logging

    from anima_world.redis_state import RedisBeatsStore

    client = redis_for(tmp_path / "legacy.db")
    with _world(tmp_path, name="legacy") as world:
        world.tick(2)
    # 3.9.0 收得下、3.10.0 的闭集不认的那种拍(`tag` 是它照收然后丢掉的一格)。
    RedisBeatsStore(client, "w").append([{
        "id": "老拍", "trigger": {"at": {"day": 0}, "tag": "第一幕"},
        "payload": [{"op": "memory", "agent_id": "甲", "summary": "老世界的剧情"}],
    }])

    from anima_world.api import World

    with caplog.at_level(logging.WARNING):
        with World.open("w", redis=client, force_mock_llm=True) as again:
            assert again.scheduler.beat_director is not None, "世界开不了机了"
            again.tick(2)
            assert [e.payload["beat_id"] for e in again.scheduler.event_log.replay()
                    if e.type == "beat_fired"] == ["老拍"], "存量那一拍不响了"
    assert any("只警告不拦" in r.getMessage() for r in caplog.records), (
        [r.getMessage() for r in caplog.records])


def test_同一种写法从文件里进来_照旧当场拒(tmp_path):
    """**分界是「这几拍是从哪儿来的」** —— 宽容只给库里那份,新文件照旧严格。
    不然这条收紧等于没做。"""
    from anima_world.beats import BeatScript, BeatScriptError

    with pytest.raises(BeatScriptError) as raised:
        BeatScript.from_data({"beats": [{
            "id": "新拍", "trigger": {"at": {"day": 0}, "tag": "第一幕"},
            "payload": [{"op": "memory", "agent_id": "甲", "summary": "s"}]}]})
    assert "trigger 里不认识的字段" in str(raised.value)
