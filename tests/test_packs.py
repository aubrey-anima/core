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


# ── 五之二、舰队每次开机都带 `--world-file`(3.10.1,2026-09-02 线上撞上)──────
#
# 🔴 上面那条 rc 2 的**理由是对的,判据错了**:它问的是 `beats_store.seed()` 有没有
# 播下去,而 `seed()` 的语义是「空的时候才播」—— 于是**「同一份文件第二次开机」和
# 「一份带着新剧情的包」在它眼里长得一模一样**。舰队每次开机都带 `--world-file`,
# 所以一个装过剧情的世界**第二次开机起再也起不来了**(龙族,platform 已回滚)。


def test_同一份文件再开一次机_退0而且拍不重响(tmp_path):
    """**舰队上的常态**:每次开机都带同一份 `--world-file`。

    它一个字都不该说得像出了事,更不该拒绝开机 —— 而且那几拍**不许重复装**
    (`:beats` 是个只 rpush 的 list,重复装一次就是同 id 两行,而
    `beat_fired` 那份历史按 id 配对)。
    """
    redis_for(tmp_path / "again.db")
    path = write_seed_file(tmp_path / "again.cyberworld",
                           dict(BASE, beats=[_beat("第一幕", 0)]))
    for _ in range(3):
        r = run_cli("simulate", "--world-id", "w", "--ticks", "1",
                    "--world-file", path, "--llm", "mock")
        assert r.returncode == 0, f"第二次开机就起不来了:{r.stderr[-800:]}"

    from anima_world.redis_state import RedisBeatsStore
    rows = RedisBeatsStore(redis_for(tmp_path / "again.db"), "w").definitions()
    assert [b["id"] for b in rows] == ["第一幕"], f"拍被重复装了:{rows}"


def test_改过一拍再开机_说一句而且照常开机(tmp_path, caplog):
    """同 id 内容不同 —— **说一句,但不拒绝开机**。

    库里那份说了算(`:beats` 那条「之后这里的行说了算」的契约),而作者需要知道
    他的改动没生效:一次静默的「改了没生效」正是这一族最贵的错法。
    """
    import logging

    client = redis_for(tmp_path / "chg.db")
    first = write_seed_file(tmp_path / "chg-1.cyberworld",
                            dict(BASE, beats=[_beat("第一幕", 0)]))
    from anima_world.api import World
    World.open("w", redis=client, world_file=first, force_mock_llm=True).close()

    second = write_seed_file(tmp_path / "chg-2.cyberworld",
                             dict(BASE, beats=[_beat("第一幕", 5)]))
    with caplog.at_level(logging.WARNING):
        World.open("w", redis=client, world_file=second,
                   force_mock_llm=True).close()
    said = "\n".join(r.getMessage() for r in caplog.records)
    assert "第一幕" in said and "库里那份说了算" in said, said[:400]

    # 而且库里那份没被改掉 —— 说了一句和真的改了是两件事。
    from anima_world.redis_state import RedisBeatsStore
    rows = RedisBeatsStore(client, "w").definitions()
    assert rows[0]["trigger"]["at"]["day"] == 0, rows


def test_加一拍再开机_才是那条rc2(tmp_path):
    """**新增**的拍才是 3.10.0 那条拒绝真正要挡的东西:一份**新**剧情正试图走
    `--world-file` 混进一个跑着的世界,而它的零点会是世界第 0 天。

    ⚠️ 而这一条要和上面两条并排读:三种情形走的是**同一行代码**,
    只差 `split_against_stored` 分出来的那三堆。
    """
    redis_for(tmp_path / "add.db")
    first = write_seed_file(tmp_path / "add-1.cyberworld",
                            dict(BASE, beats=[_beat("第一幕", 0)]))
    r = run_cli("simulate", "--world-id", "w", "--ticks", "1",
                "--world-file", first, "--llm", "mock")
    assert r.returncode == 0, r.stderr

    both = write_seed_file(tmp_path / "add-2.cyberworld",
                           dict(BASE, beats=[_beat("第一幕", 0), _beat("第三幕", 0)]))
    r = run_cli("simulate", "--world-id", "w", "--ticks", "0",
                "--world-file", both, "--llm", "mock")
    assert r.returncode == 2, f"新增的拍混进去了,退出码 {r.returncode}"
    assert "第三幕" in r.stderr, r.stderr[-600:]
    assert "pack install" in r.stderr




def test_龙族那个形状_开机装包再开机(tmp_path):
    """线上真实形状:`--world-file` 首启 → `pack install` 追加新拍 → 再开机。
    第三步从前 rc 2(文件里的拍在库里了,而库里还多出包带来的几拍)。"""
    from anima_world.world_file import (
        WorldFileManifest, seed_to_author_records, write_world_file,
    )
    base = write_seed_file(tmp_path / "lz.cyberworld",
                           dict(BASE, beats=[_beat("开学", 0)]))
    r = run_cli("simulate", "--world-id", "lz", "--ticks", "1",
                "--world-file", base, "--llm", "mock")
    assert r.returncode == 0, r.stderr

    pack = tmp_path / "week2.cyberworld"
    write_world_file(
        pack, WorldFileManifest(world_id="lz", engine_min="3.10.0"),
        seed_to_author_records({"pack": {"id": "第二周", "version": "1.0.0"},
                                "beats": [_beat("社团", 0)]}),
        compress=False, checksum=False)
    r = run_cli("pack", "install", str(pack), "--world-id", "lz")
    assert r.returncode == 0, r.stderr

    # 舰队重启:同一份 --world-file
    r = run_cli("simulate", "--world-id", "lz", "--ticks", "1",
                "--world-file", base, "--llm", "mock")
    assert r.returncode == 0, f"装过包的世界重启不来了:{r.stderr[-900:]}"

    # 读回来走**同一个 CLI**(`current_client` 就是它连的那个 fakeredis)——
    # 另开一个 handle 读到的是另一个世界,那种"绿"什么都不证明。
    from _worldfile import current_client
    from anima_world.redis_state import RedisBeatsStore
    rows = [b["id"] for b in RedisBeatsStore(current_client(), "lz").definitions()]
    assert rows == ["开学", "社团"], rows


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
    # 🔴 **舰队每次重启走的是 `simulate --world-file`,而上一版这条用例
    # 一次都没敲它**(3.11.0,验收 C ②)——「别数源码里的星号,去问屏幕」
    # 这条判据,只覆盖我记得去敲的那几条出口时,和那道只认得自己语法的 AST 闸
    # 是同一个毛病。那句带 `pack` 段的包走创世路的 warning 就漏在这儿。
    import logging as _logging

    with_pack = _pack(tmp_path, "stars-wf", pack={"id": "第三周", "version": "1.0.0"})
    records: list[str] = []

    class _Grab(_logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Grab()
    _logging.getLogger("anima_world.__main__").addHandler(handler)
    try:
        run_cli("simulate", "--world-id", "w", "--ticks", "0",
                "--world-file", with_pack, "--llm", "mock")
    finally:
        _logging.getLogger("anima_world.__main__").removeHandler(handler)
    for line in records:
        assert "**" not in line, f"`simulate --world-file` 那条路上印了裸星号:{line}"

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
            # 人设一个字不改(改它要过 CAS,那是另一条用例)—— 这一条钉的是
            # 「甲已经在册,他不是这一趟进来的」。
            # 人设写成**和现在一模一样**的那一句(改它要过 CAS,那是另一条用例)
            agents=[{"id": "甲", "name": "甲", "location": "cafe",
                     "personality": "安静"}]))
        assert receipt["agents"] == [], "甲已经在册,他不是这一趟进来的"
        row = world.packs()[0]
        assert "agents" not in row["sections"], row["sections"]
        assert row["declared"]["agents"] == ["甲"], row["declared"]


# ── 七、2a-②:人设按 compare-and-set 覆盖 · 记忆只增不改 ────────────────────
#
# 🔴 **「同一个 pack 就能覆盖」是错的,而它错得不报错**:第 1 周的包给她写过一句
# 人设,玩家跟她聊了三十天、她的人设被 `persona_update` 改过;第 2 周包一升级就把
# 那三十天抹了,账面上什么都看不出来。**判据不是「我是同一个 pack」,是「这一格
# 此刻的值,还等于我上一版写下去的那个值吗」。**(老板 D53 ④:默认拒绝,`--force` 才写。)

def test_不是这份包写的人设_默认拒绝(tmp_path):
    from anima_world.__main__ import PackInstallError

    with _world(tmp_path, name="cas1") as world:
        world.tick(3)
        pack = _pack(tmp_path, "cas1p", pack={"id": "第二周", "version": "1.0.0"},
                     agents=[{"id": "甲", "name": "甲", "location": "cafe",
                              "personality": "改过的性格"}])
        with pytest.raises(PackInstallError) as raised:
            world.install_pack(pack)
        assert "--force" in str(raised.value) and "甲" in str(raised.value)
        assert world.packs() == [], "拒了还写进去了"
        assert world.scheduler.agents["甲"].agent.blackboard.read("personality") == "安静"

        receipt = world.install_pack(pack, force=True)
        assert receipt["personality"] == ["甲"] and receipt["forced"] is True
        assert world.scheduler.agents["甲"].agent.blackboard.read(
            "personality") == "改过的性格"
        assert world.roster()["agents"][0]["agent_id"]      # 投影读得到


def test_同一份包改自己上一版写下去的那一句_放行(tmp_path):
    """**「改自己发过的」是允许的那一半** —— 而判据是「至今没被动过」。"""
    with _world(tmp_path, name="cas2") as world:
        world.tick(3)
        first = world.install_pack(_pack(
            tmp_path, "cas2a", pack={"id": "第二周", "version": "1.0.0"},
            agents=[{"id": "甲", "name": "甲", "location": "cafe",
                     "personality": "第一版人设"}], force=True) if False else _pack(
            tmp_path, "cas2a", pack={"id": "第二周", "version": "1.0.0"},
            agents=[{"id": "甲", "name": "甲", "location": "cafe",
                     "personality": "第一版人设"}]), force=True)
        assert first["personality"] == ["甲"]
        # 第二版**不用 force**:这一句是我上一版写的,而且至今没人动过。
        second = world.install_pack(_pack(
            tmp_path, "cas2b", pack={"id": "第二周", "version": "1.1.0"},
            agents=[{"id": "甲", "name": "甲", "location": "cafe",
                     "personality": "第二版人设"}]))
        assert second["personality"] == ["甲"] and second["forced"] is False


def test_写下去之后被世界改过了_再升级就拒(tmp_path):
    """🔴 **这一条才是这把尺存在的理由。** 上一版写下去之后玩家跟她聊了几轮、
    她的人设被 `persona_update` 改过 —— 这时候覆盖等于把那几轮抹掉。"""
    from anima_world.__main__ import PackInstallError

    with _world(tmp_path, name="cas3") as world:
        world.tick(3)
        world.install_pack(_pack(
            tmp_path, "cas3a", pack={"id": "第二周", "version": "1.0.0"},
            agents=[{"id": "甲", "name": "甲", "location": "cafe",
                     "personality": "第一版人设"}]), force=True)
        # 世界自己改了她 —— 走的是已有那条路(`state_change/persona_update`)。
        world.scheduler._record_and_deliver({
            "type": "state_change", "who": "甲",
            "payload": {"kind": "persona_update",
                        "spec": {"personality": "聊了三十天之后的她"}},
        })
        with pytest.raises(PackInstallError) as raised:
            world.install_pack(_pack(
                tmp_path, "cas3b", pack={"id": "第二周", "version": "1.1.0"},
                agents=[{"id": "甲", "name": "甲", "location": "cafe",
                         "personality": "第二版人设"}]))
        assert "被世界" in str(raised.value) or "不是这份包" in str(raised.value)


def test_在册的人的记忆只增不改_而且装两遍不记两次(tmp_path):
    """记忆是**演化态** —— 改一条既有的等于伪造历史;而"这一周发生过一件事"
    是新的一条,加得进去。按 `(agent_id, summary)` 去重。"""
    with _world(tmp_path, name="mem") as world:
        world.tick(3)
        pack = _pack(tmp_path, "memp", pack={"id": "第二周", "version": "1.0.0"},
                     memories=[{"agent_id": "甲", "kind": "seed",
                                "summary": "社团招新那天下着雨。"}])
        first = world.install_pack(pack)
        assert first["memories"] == 1, first
        assert any("社团招新" in (m.get("summary") or "") for m in world.memories("甲"))
        again = world.install_pack(_pack(
            tmp_path, "memp2", pack={"id": "第二周", "version": "1.1.0"},
            memories=[{"agent_id": "甲", "kind": "seed",
                       "summary": "社团招新那天下着雨。"}]))
        assert again["memories"] == 0, "同一条记了两次"


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


def test_包里带一个新插件_它声明的种类真的进了本体(tmp_path):
    """🔴 **tool 真装第 2 周包逮的第 15 条。**

    上一版 `install_pack` **判用 `merged`(并过插件种类的那一份)、写用 `authored`**
    —— 于是一份带新插件的包 `pack install` 退 0、`plugin list` 印得出那个种类和动词,
    而 `ontology --kind <它>` 答「这个世界里没有声明过这一类」:**机制完全不生效
    而回执全是成功**,重开一次也没有;包里若带那个种类的实例则退 1 甩堆栈。

    **两份东西必须来自同一次合并** —— 这个仓库为这句话红过一次(2026-08-28 那条
    插件命名空间回归),而这一次是它的镜像:喂全集、判全集,写却喂了局部。
    """
    menpai = {
        "id": "menpai", "version": "1.0.0", "label": "门派",
        "facts": {"声望": {"bearer": "agent", "shape": "number", "default": 0.0,
                           "visibility": "here"}},
        "kinds": {"group:sect": {"gloss": "一个门派"}},
        "verbs": {"入门": {"target": "group:sect", "description": "拜入门派"}},
    }
    client = redis_for(tmp_path / "pk.db")
    with _world(tmp_path, name="pk") as world:
        world.tick(2)
        receipt = world.install_pack(_pack(
            tmp_path, "sect", pack={"id": "第二周", "version": "1.0.0"},
            plugins=[menpai],
            entities=[{"id": "menpai.sect:狮心会", "name": "狮心会",
                       "location": "cafe"}]))
        assert receipt["pack"] == "第二周"
        kinds = {k["id"] for k in world.kinds()}
        assert "menpai.sect" in kinds, f"插件声明的种类没进本体:{sorted(kinds)}"
        assert "menpai.sect:狮心会" in {e["id"] for e in world.entities()}
        # 动词真的点得动 —— 「印得出」和「用得上」是两件事。
        world.player_move("p1", "cafe")
        world.tick(1)
        out = world.player_tool("p1", "interact",
                                {"target": "menpai.sect:狮心会", "verb": "入门"})
        assert out["ok"] is True, out

    from anima_world.api import World

    with World.open("w", redis=client, force_mock_llm=True) as again:
        assert "menpai.sect" in {k["id"] for k in again.kinds()}, "重开一次就没了"
        assert "menpai.sect:狮心会" in {e["id"] for e in again.entities()}


# ── 八、2a-② K7:停用(`pack disable`)────────────────────────────────────────
#
# 🔴 **停用不是删除。** 玩家的记忆里有这一周发生过的事,他的钱包里有那 800 块 ——
# 删掉那几条事件 = 让历史指向不存在的东西,而"对账即重放"会让投影和日志对不上,
# **且没有任何地方会报错**(和 `forget_player` 逐字同一个形状)。

def test_停用之后_那几拍不再响_而已经响过的照旧在历史里(tmp_path):
    day = 288
    with _world(tmp_path, name="dis1") as world:
        world.tick(3)
        world.install_pack(_pack(
            tmp_path, "d1", pack={"id": "第二周", "version": "1.0.0"},
            beats=[_beat("社团", 0), _beat("夜宵", 2)]))
        world.tick(1)
        assert [b for b, _ in _fired(world)] == ["社团"]

        receipt = world.disable_pack("第二周")
        assert sorted(receipt["beats"]) == ["夜宵", "社团"]
        world.tick(day * 3)
        assert [b for b, _ in _fired(world)] == ["社团"], (
            "停用之后那一拍还是响了"
        )
        # 🔴 **已经响过的那一条一个字没动** —— 历史是历史。
        assert any(e.type == "beat_fired" and e.payload["beat_id"] == "社团"
                   for e in world.scheduler.event_log.replay())


def test_停用_它带来的新人退场_而他造成的后果留着(tmp_path):
    with _world(tmp_path, name="dis2") as world:
        world.tick(3)
        world.install_pack(_pack(
            tmp_path, "d2", pack={"id": "第二周", "version": "1.0.0"},
            locations=[{"id": "yard", "name": "院子", "description": "d"}],
            agents=[{"id": "乙", "name": "乙", "location": "yard",
                     "personality": "新来的"}]))
        assert "乙" in world.scheduler.agents
        receipt = world.disable_pack("第二周")
        assert receipt["agents"] == ["乙"]
        assert "乙" not in world.scheduler.agents, "他还在台上"
        # **不是删人**:他 join 那条事件、那个地点都留着。
        assert any(e.type == "agent_join" and e.who == "乙"
                   for e in world.scheduler.event_log.replay())
        assert "yard" in {l["id"] for l in world.scheduler.location_store.all()}


def test_停用_开关回落到装包前那个值_而不是引擎默认值(tmp_path):
    with _world(tmp_path, name="dis3") as world:
        world.tick(3)
        world.config_set("host.max_options", 4)          # 这个世界原来的样子
        world.install_pack(_pack(
            tmp_path, "d3", pack={"id": "第二周", "version": "1.0.0"},
            config={"host.max_options": 2, "narrative.player.enabled": True}))
        assert world.config_get("host.max_options") == 2
        world.disable_pack("第二周")
        assert world.config_get("host.max_options") == 4, "回落到引擎默认值去了"
        # 装包前根本没有的那一格 → 撤掉整行,回落引擎声明的那个值。
        assert world.config_get("narrative.player.enabled") is False
        row = next(r for r in world.config_list()
                   if r["key"] == "narrative.player.enabled")
        assert row["source"] == "默认值", row


def test_停用_装完之后被人调过的那一格_留着没动而且说出来(tmp_path):
    """🔴 **compare-and-set 在这一层的同一把尺**:撤销一次运维的调整等于把它悄悄
    抹掉,而账面上什么都看不出来。"""
    with _world(tmp_path, name="dis4") as world:
        world.tick(3)
        world.install_pack(_pack(
            tmp_path, "d4", pack={"id": "第二周", "version": "1.0.0"},
            config={"host.max_options": 2}))
        world.config_set("host.max_options", 5)          # 运维后来又调过
        receipt = world.disable_pack("第二周")
        assert receipt["config"] == [] and receipt["kept"] == ["host.max_options"]
        assert world.config_get("host.max_options") == 5


def test_再装一次同一个包_不再等于重新启用_而是当场拒(tmp_path):
    """🔴 **这条用例的断言在 3.10.2 反过来了,而反得对。**

    它原先钉的是「再装一次 = 重新启用」,而那句话**对带拍的包从来就是假的**
    (`install` 会因为拍 id 撞车 rc 2),对无拍的包则是「`disabled` 翻回 false
    而它带来的人还站在场外」—— 两种都回不来,只是坏的样子不同。
    ⚠️ 这条用例当年之所以绿,是因为它的第二份包**不带拍**(`d5b` 只有 `pack` 段),
    正好落在那个坏得不报错的那一半上。

    现在只有一扇门:`enable_pack`。
    """
    from anima_world.world_package import PackInstallError

    with _world(tmp_path, name="dis5") as world:
        world.tick(3)
        world.install_pack(_pack(tmp_path, "d5a",
                                 pack={"id": "第二周", "version": "1.0.0"},
                                 beats=[_beat("社团", 1)]))
        world.disable_pack("第二周")
        assert world.packs()[0]["disabled"] is True
        with pytest.raises(PackInstallError) as raised:
            world.install_pack(_pack(tmp_path, "d5b",
                                     pack={"id": "第二周", "version": "1.1.0"}))
        assert "pack enable" in str(raised.value)
        assert world.packs()[0]["disabled"] is True, "拒了却还是把它翻成启用了"

        world.enable_pack("第二周")
        assert world.packs()[0]["disabled"] is False
        world.tick(288 * 2)
        assert [b for b, _ in _fired(world)] == ["社团"], "重新启用之后那一拍没响"


def test_停用一个没装过的包_拒得念得通(tmp_path):
    from anima_world.__main__ import PackInstallError

    with _world(tmp_path, name="dis6") as world:
        world.tick(3)
        with pytest.raises(PackInstallError) as raised:
            world.disable_pack("根本没有这一周")
        assert "pack list" in str(raised.value)


def test_cli停用_那一屏念得通(tmp_path):
    client = redis_for(tmp_path / "disc.db")
    with _world(tmp_path, name="disc") as world:
        world.tick(3)
        world.install_pack(_pack(tmp_path, "dc", pack={"id": "第二周", "version": "1.0.0"},
                                 beats=[_beat("社团", 5)]))
    r = run_cli("pack", "disable", "第二周", "--world-id", "w")
    assert r.returncode == 0, r.stderr
    assert "不是删除" in r.stdout and "**" not in r.stdout, r.stdout
    r = run_cli("pack", "disable", "第二周", "--world-id", "w")
    assert r.returncode == 2 and "已经是停用的" in r.stderr


# ── 停用之后回得来吗(3.10.1,验收 A ③)────────────────────────────────────


def test_带拍带人的包_停用之后启用得回来(tmp_path):
    """🔴 **「再装一次 = 重新启用」对带拍的包是假的,而两条规矩各自都对。**

    `disable` 有意**不删** `:beats`(停用不是删除);`install` 有意**拒绝重用
    已有的拍 id**(`beat_fired` 那份历史按 id 配对)。于是一份带拍的包停用之后
    `install` 说「这几拍的 id 已经有了」,而没有第三条路 —— 它永远回不来。
    少的不是一条规矩,是**它们中间的一扇门**。

    这一条两半都带:**带拍**(拍要解封)与**带人**(人要回来 —— 那正是重装
    那条路答不出来的:`known` 含已 `agent_leave` 的人,于是他被当成"已在册"
    跳过,`disabled` 翻回 false 而人还站在场外)。
    """
    from anima_world.api import World
    from anima_world.world_package import PackInstallError
    from anima_world.world_file import (
        WorldFileManifest, seed_to_author_records, write_world_file,
    )

    client = redis_for(tmp_path / "en.db")
    base = write_seed_file(tmp_path / "en-base.cyberworld", BASE)
    World.open("w", redis=client, world_file=base, force_mock_llm=True).close()

    pack = tmp_path / "wk.cyberworld"
    write_world_file(
        pack, WorldFileManifest(world_id="w", engine_min="3.10.0"),
        seed_to_author_records({
            "pack": {"id": "第二周", "version": "1.0.0"},
            "beats": [_beat("社团", 0)],
            "agents": [{"id": "乙", "name": "乙", "location": "cafe",
                        "personality": "新来的"}],
        }),
        compress=False, checksum=False)

    with World.open("w", redis=client, force_mock_llm=True) as world:
        world.install_pack(str(pack))
        assert "乙" in world.scheduler.agents
        world.disable_pack("第二周")
        assert "乙" not in world.scheduler.agents, "停用了,人该退场"
        assert world.packs()[0]["disabled"] is True

        # 「再装一次」这条路走不通 —— 这正是这扇门存在的理由。
        # ⚠️ 3.10.2 起挡它的是**更早**那道闸(「这份包停用着」),而不是拍 id
        # 撞车那一条:两句话指向同一扇门,而早的那句对无拍的包也成立。
        with pytest.raises(PackInstallError) as raised:
            world.install_pack(str(pack))
        assert "pack enable" in str(raised.value)

        receipt = world.enable_pack("第二周")
        assert receipt["beats"] == ["社团"]
        assert receipt["agents"] == ["乙"], f"人没回来:{receipt}"
        assert "乙" in world.scheduler.agents, "启用了,人该回来"
        assert world.packs()[0]["disabled"] is False
        # 那几拍又进候选了
        from anima_world.beats import disabled_beats_from
        assert "社团" not in disabled_beats_from(world.scheduler._memory_projection)


def test_启用一份本来就启用着的包_当场说不做(tmp_path):
    """**没有什么要做的**,而这句话要说出来 —— 静默成功会让人以为它做了点什么。"""
    from anima_world.api import World
    from anima_world.world_package import PackInstallError
    from anima_world.world_file import (
        WorldFileManifest, seed_to_author_records, write_world_file,
    )

    client = redis_for(tmp_path / "en2.db")
    base = write_seed_file(tmp_path / "en2-base.cyberworld", BASE)
    World.open("w", redis=client, world_file=base, force_mock_llm=True).close()
    pack = tmp_path / "wk2.cyberworld"
    write_world_file(
        pack, WorldFileManifest(world_id="w", engine_min="3.10.0"),
        seed_to_author_records({"pack": {"id": "周", "version": "1.0.0"},
                                "world_setting": "换一段"}),
        compress=False, checksum=False)
    with World.open("w", redis=client, force_mock_llm=True) as world:
        world.install_pack(str(pack))
        with pytest.raises(PackInstallError) as raised:
            world.enable_pack("周")
        assert "本来就是启用的" in str(raised.value)
        with pytest.raises(PackInstallError) as raised:
            world.enable_pack("没装过的")
        assert "没有装过" in str(raised.value)


def test_无拍的包停用之后_重装当场拒并指向enable(tmp_path):
    """🔴 **「再装一次 = 重新启用」这条路必须整个关掉,不是只对带拍的包关掉**
    (3.10.2,验收 A ①)。

    3.10.1 给带拍的包加了 `pack enable`,而**无拍那一半漏了**:重装一份已停用的
    无拍包 rc 0、`pack list` 的「(已停用)」消失,而它带来的人**还站在场外**;
    之后 `pack enable` 答「本来就是启用的」rc 2 —— **人永远回不来**,
    而屏幕上那份包看起来好好的。
    """
    from anima_world.api import World
    from anima_world.world_package import PackInstallError
    from anima_world.world_file import (
        WorldFileManifest, seed_to_author_records, write_world_file,
    )

    client = redis_for(tmp_path / "nb.db")
    base = write_seed_file(tmp_path / "nb-base.cyberworld", BASE)
    World.open("w", redis=client, world_file=base, force_mock_llm=True).close()

    pack = tmp_path / "nb.cyberworld"
    write_world_file(
        pack, WorldFileManifest(world_id="w", engine_min="3.10.0"),
        seed_to_author_records({
            "pack": {"id": "无拍周", "version": "1.0.0"},
            "agents": [{"id": "丙", "name": "丙", "location": "cafe",
                        "personality": "路过的"}],
        }), compress=False, checksum=False)

    with World.open("w", redis=client, force_mock_llm=True) as world:
        world.install_pack(str(pack))
        assert "丙" in world.scheduler.agents
        world.disable_pack("无拍周")
        assert "丙" not in world.scheduler.agents

        # 重装:**当场拒**,而且那句话要指向 `pack enable`
        with pytest.raises(PackInstallError) as raised:
            world.install_pack(str(pack))
        said = str(raised.value)
        assert "pack enable" in said and "停用着" in said, said
        # 拒了就一个字节都没写:那份包还停用着
        assert world.packs()[0]["disabled"] is True

        # 而那扇门真的把人带回来
        receipt = world.enable_pack("无拍周")
        assert receipt["agents"] == ["丙"], receipt
        assert "丙" in world.scheduler.agents
        assert world.packs()[0]["disabled"] is False


def test_装包回执里_种类和实例真的记了(tmp_path):
    """🔴 **这一格此前零覆盖,而它整个是坏的**(3.10.2,验收 A ②)。

    `kind_definitions()` 给的是 `list[dict]`,而 3.10.1 那两行拿 `for k, _ in`
    去解包 —— **两个键的 dict 会静默解出键名**,别的行数当场 `ValueError`
    被 except 吞成 WARNING **并把 Traceback 印在一屏「装成功了」上面**。
    下场:`kinds` / `entities` 永远进不了 `landed`,而 FOR-STUDIO §3.62(m)
    教消费方读的正是 `declared - sections` —— **两样装得好好的东西被指着说
    没装进去**,那正是这一格要治的病本身。
    """
    from anima_world.api import World
    from anima_world.world_file import (
        WorldFileManifest, seed_to_author_records, write_world_file,
    )

    client = redis_for(tmp_path / "ki.db")
    base = write_seed_file(tmp_path / "ki-base.cyberworld", BASE)
    World.open("w", redis=client, world_file=base, force_mock_llm=True).close()

    pack = tmp_path / "ki.cyberworld"
    write_world_file(
        pack, WorldFileManifest(world_id="w", engine_min="3.10.0"),
        seed_to_author_records({
            "pack": {"id": "带实例的周", "version": "1.0.0"},
            "kinds": [{"id": "灯", "quantities": {"亮度": {"default": 1.0, "visibility": "here"}}}],
            "entities": [{"id": "灯:门口", "name": "门口那盏灯",
                          "location": "cafe"}],
        }), compress=False, checksum=False)

    with World.open("w", redis=client, force_mock_llm=True) as world:
        receipt = world.install_pack(str(pack))
        assert "灯" in (receipt.get("kinds") or []), receipt
        assert "灯:门口" in (receipt.get("entities") or []), receipt
        # 而且它进了 `pack list` 的 `sections` —— 消费方读的就是这一格
        sections = world.packs()[0]["sections"]
        assert "灯" in (sections.get("kinds") or []), sections
        assert "灯:门口" in (sections.get("entities") or []), sections
        # `declared - sections` 不该再把它们算成"没装进去"
        declared = world.packs()[0].get("declared") or {}
        for name in ("kinds", "entities"):
            missing = set(declared.get(name) or ()) - set(sections.get(name) or ())
            assert not missing, f"{name} 里这几样被指着说没装进去,而它们装进去了:{missing}"


@pytest.mark.parametrize("with_beats", [True, False])
def test_同一份文件连开两次_那五段一句都不说(tmp_path, caplog, with_beats):
    """🔴 **判据是「同一份文件又开了一次机」,不是「它带不带拍」**
    (3.11.0,验收 A 逮的)。

    3.10.2 那一版把这个开关写在 `if world_seed.get("beats")` 分支里,于是一份
    **没有 `beats` 段**的文件(demo、晚潮、灯塔湾……)二开照旧吼五段
    「装不进去」。⚠️ **这和「只给带拍的包补门」是同一种漏法,而且隔了一个
    commit 又犯了一次** —— 病根都是**拿一个恰好在手边的条件当判据**。

    所以这条用例**带拍与不带拍各跑一遍**:少了后者,那个洞照样测不出来。
    """
    import logging
    from anima_world.api import World

    client = redis_for(tmp_path / f"twice{int(with_beats)}.db")
    seed = dict(BASE)
    if with_beats:
        seed["beats"] = [_beat("第一幕", 0)]
    path = write_seed_file(tmp_path / f"twice{int(with_beats)}.cyberworld", seed)

    World.open("w", redis=client, world_file=path, force_mock_llm=True).close()
    with caplog.at_level(logging.WARNING):
        World.open("w", redis=client, world_file=path, force_mock_llm=True).close()
    said = "\n".join(r.getMessage() for r in caplog.records)
    for needle in ("装不进去", "没有装进去", "装不进一个"):
        assert needle not in said, (
            f"同一份文件二开还在吼那五段(带拍={with_beats}):{said[:400]}")


def test_文件改过之后二开_那五段照说(tmp_path, caplog):
    """**闭嘴只对「一模一样的那一份」成立** —— 作者真改了一格,那句话还得说,
    否则这个开关就从"别吵"变成了"永远不说"。"""
    import logging
    from anima_world.api import World

    client = redis_for(tmp_path / "chg2.db")
    first = write_seed_file(tmp_path / "c1.cyberworld", BASE)
    World.open("w", redis=client, world_file=first, force_mock_llm=True).close()
    second = write_seed_file(tmp_path / "c2.cyberworld",
                             dict(BASE, world_setting="换了一段世界观。"))
    with caplog.at_level(logging.WARNING):
        World.open("w", redis=client, world_file=second, force_mock_llm=True).close()
    said = "\n".join(r.getMessage() for r in caplog.records)
    assert "世界观" in said, f"作者改了世界观,而引擎一个字没说:{said[:400]}"
