"""节拍过河:作者写的剧情装得进 `.cyberworld`,而且**首启自己就带上**。

在这之前节拍是这个引擎里唯一一样**作者写下、却进不了世界**的东西 —— 它只能靠
`--beats` 单独喂一个文件。于是这条链在中间断了(看板 D1,创作台 2026-08-08 的诉求):

    工作台   写节拍 → beats.json          （本地试炼五拍全按顺序触发,验过）
             导出   → x.cyberworld        ← 节拍不在里面
    运维台   导入   → 只搬这一个文件进实例目录
    世界镜像 首启   → entrypoint 只在 /data/beats.json 存在时才传 --beats
                                          ← 那个文件从来没人放

结果:**作者写的剧情在本地跑得通,在舰队上一拍都不响,而且没有任何一处报错。**
世界照常启动、居民照常过日子,只是那条故事线不存在了。

判断错在哪:节拍**是作者层**(和人物、地点、关系同属"作者写下的",不是"世界跑出来
的"),而它被留在了"作者层 / 状态层"这个划分之外。收进来之后,交接仍然是
**一件产物** —— 契约的形状一个字没改(选项 (a),`platform` 与 `tool` 都不必动)。
"""
from __future__ import annotations

import json

import pytest

from _worldfile import read_seed_file, run_cli, write_seed_file

pytest.importorskip("fakeredis")


_BEAT = {
    "id": "第一幕",
    "trigger": {"at": {"day": 0, "minute_of_day": 5}},
    "payload": [{"op": "memory", "agent_id": "甲",
                 "summary": "今天早上,她想起了那封没寄出去的信。"}],
}


def _seed(**over):
    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
        "agents": [{"id": "甲", "name": "甲", "location": "cafe",
                    "personality": "安静"}],
    }
    seed.update(over)
    return seed


def test_节拍装得进世界文件_也读得回来(tmp_path):
    """先钉最底下那一层:它真的成了作者层的一个段,来回都不掉。

    ⚠️ **两个方向都要验。** `seed_to_author_records` 那个方向上的静默丢弃更难发现:
    文件写得出来、装得进去,只是少了一段(`world_setting` 是字符串时,那个函数
    曾经一言不发地把整个世界观扔掉)。
    """
    from anima_world.world_file import AUTHOR_SECTIONS

    assert AUTHOR_SECTIONS["beat"] == "beats", "作者层第十二个段"
    path = write_seed_file(tmp_path / "w.cyberworld", _seed(beats=[_BEAT]))
    assert read_seed_file(path)["beats"] == [_BEAT]


def test_首启不给beats也带上_而且真的响(tmp_path, fresh_redis, open_world):
    """**这条就是 D1 的另一半。** 装得进去而首启不带,等于节拍进了包却仍然要靠
    `--beats` 才响 —— 而舰队上没有任何一条路会去传那个参数。

    判据不是"store 里有几行",是**那一拍真的落进了世界的历史**:一条 `beat_fired`
    事件加上她真的多了那条记忆。只数 store 的话,一条装进去却永远不触发的节拍
    照样算过 —— 而那正是这个 bug 的症状本身。
    """
    path = write_seed_file(tmp_path / "w.cyberworld", _seed(beats=[_BEAT]))
    world = open_world("w1", redis=fresh_redis, world_file=path)
    try:
        assert world.scheduler.beat_director is not None, "首启没把节拍带上"
        world.tick(3)
        fired = [e for e in world.history(kind="beat_fired", limit=50)["events"]]
        assert [e["payload"]["beat_id"] for e in fired] == ["第一幕"], fired
        assert any("没寄出去的信" in m["summary"] for m in world.memories("甲"))
    finally:
        world.close()


def test_同一拍不许因为重启再响一次(tmp_path, fresh_redis, open_world):
    """脚本落库了,而**"哪几拍响过"没有落库,也不该落** —— 它是历史,从
    `beat_fired` 事件重放出来。两份真相里存一份,另一份必然有一天对不上,
    而这一层对不上的样子是"这一拍又响了一次"(她会再想起一次那封信)。"""
    path = write_seed_file(tmp_path / "w.cyberworld", _seed(beats=[_BEAT]))
    world = open_world("w2", redis=fresh_redis, world_file=path)
    try:
        world.tick(3)
    finally:
        world.close()
    again = open_world("w2", redis=fresh_redis)     # 重启,**不给世界文件**
    try:
        again.tick(6)
        fired = again.history(kind="beat_fired", limit=50)["events"]
        assert len(fired) == 1, f"重启之后又响了一次:{fired}"
        assert again.scheduler.beat_director is not None, (
            "重启没给 --world-file 也该从库里读得到剧情")
    finally:
        again.close()


def test_坏节拍当场开不了机_而且一个字都不写(tmp_path, fresh_redis, open_world):
    """**坏脚本必须在加载时当场报错,不能流到世界启动**(CLAUDE.md 逐字)。
    而且和坏 `kinds` 同一条:验不过就一个字都不写 —— 一份装了一半的世界比
    开不了机更贵,因为它让重试走的已经不是创世那条路。"""
    bad = dict(_BEAT, payload=[{"op": "没这个 op", "agent_id": "甲"}])
    path = write_seed_file(tmp_path / "bad.cyberworld", _seed(beats=[bad]))
    with pytest.raises(Exception):
        open_world("w3", redis=fresh_redis, world_file=path).close()
    assert [k for k in fresh_redis.scan_iter("anima:w3:beats")] == []


def test_命令行给的beats赢这一趟_但不写库(tmp_path, fresh_redis, open_world):
    """`--beats` 是一次**明示的覆盖**(试炼、调试都靠它),而库里那份是这个世界
    自己的剧情。让它写回去的话,一次试炼就会把作者的剧情换掉,**而且不报错**。"""
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"beats": [dict(_BEAT, id="临时那一拍")]}),
                     encoding="utf-8")
    path = write_seed_file(tmp_path / "w.cyberworld", _seed(beats=[_BEAT]))
    world = open_world("w4", redis=fresh_redis, world_file=path,
                       beats_path=str(other))
    try:
        world.tick(3)
        fired = [e["payload"]["beat_id"]
                 for e in world.history(kind="beat_fired", limit=50)["events"]]
        assert fired == ["临时那一拍"], "命令行那份没赢"
    finally:
        world.close()
    from anima_world.redis_state import RedisBeatsStore

    kept = RedisBeatsStore(fresh_redis, "w4").definitions()
    assert [b["id"] for b in kept] == ["第一幕"], (
        "一次试炼把作者的剧情换掉了", kept)


def test_导出再导入_剧情跟着走(tmp_path, open_world):
    """交接**仍然是一件产物**(契约那条形状没改)。这里走的是真的那条路:
    `world export` → `world import` → 新世界里那一拍照样在。

    ⚠️ 用 `redis_for` 而不是 `fresh_redis`:CLI 在测试里连的是"最近那个世界"
    (`_worldfile.current_client`),而 `fresh_redis` 没在那本册子上登记过 ——
    连错一个 fakeredis 的样子是「这个 Redis 上没有叫 w5 的世界」。
    """
    from _worldfile import redis_for

    fresh_redis = redis_for(tmp_path / "w.db")
    path = write_seed_file(tmp_path / "w.cyberworld", _seed(beats=[_BEAT]))
    world = open_world("w5", redis=fresh_redis, world_file=path)
    try:
        world.tick(1)      # 还没到第 5 分钟,那一拍还没响
    finally:
        world.close()

    out = tmp_path / "out.cyberworld"
    done = run_cli("world", "export", "--world-id", "w5", "--output", str(out),
                   "--package-id", "w5pack", "--name", "过河")
    assert done.returncode == 0, done.stderr
    done = run_cli("world", "import", str(out), "--world-id", "w6")
    assert done.returncode == 0, done.stderr

    from anima_world.redis_state import RedisBeatsStore

    assert [b["id"] for b in RedisBeatsStore(fresh_redis, "w6").definitions()] == [
        "第一幕"], "导出/导入之后剧情没了"
    restored = open_world("w6", redis=fresh_redis)
    try:
        assert restored.scheduler.beat_director is not None
        restored.tick(4)
        fired = [e["payload"]["beat_id"]
                 for e in restored.history(kind="beat_fired", limit=50)["events"]]
        assert fired == ["第一幕"], fired
    finally:
        restored.close()


def test_一个字都没写节拍的世界_行为逐位如旧(tmp_path, fresh_redis, open_world):
    """**声明本身就是开关**,这一层和 `kinds` / perception 逐字同构:没写就是
    这一层整个缺席,不是"一个空剧本"。对照组存在的理由是那条期望 0 的判据
    ——`beat_director is None` 对一个整体坏掉的夹具同样成立。"""
    path = write_seed_file(tmp_path / "w.cyberworld", _seed())
    world = open_world("w7", redis=fresh_redis, world_file=path)
    try:
        assert world.scheduler.beat_director is None
        world.tick(3)
        assert world.history(kind="beat_fired", limit=50)["events"] == []
    finally:
        world.close()


def test_export的beats参数不再是哑参数(tmp_path, open_world):
    """**3.7.0 之前它一路传到 `export_snapshot` 然后什么也不做。**

    一个"传了没报错、也没生效"的参数比没有这个参数更坏:节拍那条链本来就断在
    中间(看板 D1),而这一格让人以为自己已经把剧情打进包了。
    """
    from _worldfile import read_seed_file, redis_for

    fresh_redis = redis_for(tmp_path / "e.db")
    script = tmp_path / "beats.json"
    script.write_text(json.dumps({"beats": [_BEAT]}), encoding="utf-8")
    path = write_seed_file(tmp_path / "w.cyberworld", _seed())   # 世界里没有剧情
    world = open_world("e1", redis=fresh_redis, world_file=path)
    try:
        assert world.scheduler.beat_director is None, "前提:这个世界自己没有剧情"
        out = tmp_path / "packed.cyberworld"
        world.export_snapshot(out, world_id="e1pack", name="打包",
                              beats_path=str(script))
    finally:
        world.close()
    assert read_seed_file(out)["beats"] == [_BEAT], "剧情没进包"

    # 对照组(期望非 0 的那一条判据):不给这个参数时包里一条节拍都没有 ——
    # 否则上面那条断言对"任何包都带节拍"这种实现同样成立。
    world = open_world("e2", redis=fresh_redis, world_file=path)
    try:
        bare = tmp_path / "bare.cyberworld"
        world.export_snapshot(bare, world_id="e2pack", name="不带")
    finally:
        world.close()
    assert "beats" not in read_seed_file(bare)


def test_report答得出哪几拍白写了(tmp_path, open_world):
    """**创作台列的五个问题里唯一没答上的那一个**(FOR-STUDIO §0-②)。

    它欠了很久不是因为难,是因为**没有分母**:`beat_fired` 事件一直是现成的,
    而节拍从前只活在一个 `--beats` 文件里,`report` 这条只读日志的路根本看不见它。
    3.7.0 把节拍收进世界之后,这个差集才第一次算得出来。

    **一拍都没响 = 这个脚本白写,而作者必须当场知道** —— 只报 `fired` 的话,
    「响了 0 拍」和「这个世界压根没有剧情」在屏幕上长得一模一样。
    """
    from _worldfile import redis_for

    fresh_redis = redis_for(tmp_path / "r.db")
    late = dict(_BEAT, id="第二幕",
                trigger={"at": {"day": 9, "minute_of_day": 5}})
    path = write_seed_file(tmp_path / "w.cyberworld",
                           _seed(beats=[_BEAT, late]))
    world = open_world("r1", redis=fresh_redis, world_file=path)
    try:
        world.tick(3)
        beats = world.report()["beats"]
    finally:
        world.close()
    assert beats["declared"] == ["第一幕", "第二幕"]
    assert beats["fired"] == ["第一幕"]
    assert beats["unfired"] == ["第二幕"], "白写的那一拍要点名"
    assert beats["fired_not_declared"] == []


def test_问不出来和一拍没写要分得开(tmp_path, open_world):
    """`declared: null` 是**问不出来**(这次调用没给节拍表),`[]` 是"这个世界
    真的一拍都没写"。合成一个的话,一份读不到剧情的报告读起来像一个没有剧情的
    世界 —— 而那两件事作者的下一步完全不同。"""
    from anima_world.sim_report import build_run_report

    unknown = build_run_report([], ticks=1)["beats"]
    assert unknown["declared"] is None and unknown["unfired"] is None

    known = build_run_report([], ticks=1, beats=[])["beats"]
    assert known["declared"] == [] and known["unfired"] == []


def test_不是对象的那一拍_当场报错不许安静扔掉(tmp_path, fresh_redis, open_world):
    """一拍写成了一行字:**当场报错,不跳过**(CLAUDE.md 逐字的那条装载纪律)。

    少装半份剧情和一拍不响是同一种病,而这一版更难发现:那一拍连"没响"都算不上,
    它从来就没进过这个世界。
    ⚠️ 播种那一侧一度在**验之前**先 `isinstance(b, dict)` 过滤了一道 —— 那道
    过滤今天到不了(`author_records_to_seed` 先一步拦住了不是对象的 `body`),
    但它是一道**准备好静默丢弃**的闸,而这一层的失败方式恰恰是安静的,所以拆掉了:
    播的就是 `BeatScript.from_data` 验过的那一份。
    """
    import gzip

    path = tmp_path / "notobj.cyberworld"
    rows = [
        {"kind": "manifest", "version": 3, "world_id": "w8", "name": "一行字"},
        {"kind": "author", "type": "location",
         "body": {"id": "cafe", "name": "咖啡店", "description": "拐角那家"}},
        {"kind": "author", "type": "agent",
         "body": {"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"}},
        {"kind": "author", "type": "beat", "body": _BEAT},
        {"kind": "author", "type": "beat", "body": "这一行不是一拍"},
    ]
    with gzip.GzipFile(path, "wb", mtime=0) as fh:
        fh.write(("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                  + "\n").encode())
    with pytest.raises(Exception):
        open_world("w8", redis=fresh_redis, world_file=str(path)).close()
    assert [k for k in fresh_redis.scan_iter("anima:w8:beats")] == [], (
        "验不过就一个字都不写")
    # 两扇离线门也得给同一个答案 —— 否则又是一格"说绿而开不了机"。
    payload = json.loads(run_cli("world", "check", str(path), "--json").stdout)
    assert payload["loadable"] is False, payload


def test_库里已经有剧情时_不合并要说出来(tmp_path, fresh_redis, open_world, caplog):
    """**不无声。** 库里有剧情时文件里那份不合并(理由见 `RedisBeatsStore.seed`),
    而一句话不说的样子是"我把新剧情装进去了" —— 拿一份改过的世界文件去编辑一个
    跑着的世界的人,会以为第三幕已经在里面了。
    """
    import logging

    path = write_seed_file(tmp_path / "w.cyberworld", _seed(beats=[_BEAT]))
    open_world("w9", redis=fresh_redis, world_file=path).close()

    later = write_seed_file(tmp_path / "later.cyberworld",
                            _seed(beats=[dict(_BEAT, id="第三幕")]))
    with caplog.at_level(logging.WARNING):
        open_world("w9", redis=fresh_redis, world_file=later).close()
    assert any("没有装进去" in r.getMessage() for r in caplog.records), (
        [r.getMessage() for r in caplog.records])
    from anima_world.redis_state import RedisBeatsStore

    assert [b["id"] for b in RedisBeatsStore(fresh_redis, "w9").definitions()] == [
        "第一幕"], "库里那份才说了算"


def test_屏幕上也说得出哪几拍白写了(tmp_path, open_world):
    """**默认那条路**上也得有这句话。创作台那条诉求的原话是"一拍都没响 = 这个脚本
    白写,作者必须当场知道" —— 只进 `--json` 的话,敲 `anima-world report` 的人
    屏幕上什么都没有,和"这个世界压根没有剧情"长得一模一样。
    """
    from _worldfile import redis_for

    client = redis_for(tmp_path / "h.db")
    late = dict(_BEAT, id="第二幕",
                trigger={"at": {"day": 9, "minute_of_day": 5}})
    path = write_seed_file(tmp_path / "w.cyberworld", _seed(beats=[_BEAT, late]))
    world = open_world("h1", redis=client, world_file=path)
    try:
        world.tick(3)
    finally:
        world.close()
    done = run_cli("report", "--world-id", "h1")
    assert done.returncode == 0, done.stderr
    assert "第二幕" in done.stdout and "一次都没响" in done.stdout, done.stdout
    # 对照组:一拍都没写的世界不印这一行(否则上面那条对"永远印一行"同样成立)。
    bare = write_seed_file(tmp_path / "bare.cyberworld", _seed())
    plain = open_world("h2", redis=client, world_file=bare)
    try:
        plain.tick(1)
    finally:
        plain.close()
    assert "节拍:" not in run_cli("report", "--world-id", "h2").stdout
