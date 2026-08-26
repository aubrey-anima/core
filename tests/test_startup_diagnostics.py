"""Boot-time diagnostics for the ways a world degrades without complaining.

Every case here used to look identical to a healthy world from the outside:
the process starts, the clock ticks, events land. What is missing — a real
LLM, an applied seed file — only shows up in the quality of the text hours
later. These tests pin the moment each one becomes visible.
"""
from __future__ import annotations

from _worldfile import write_seed_file, open_world_at

import json
import pathlib
import logging

import pytest

from anima_world.__main__ import build_serve_scheduler


@pytest.fixture
def minimal_seed(tmp_path):
    return pathlib.Path(write_seed_file(tmp_path / "seed.cyberworld", {
        "agents": [{"id": "a", "name": "阿岚", "location": "cafe", "personality": "安静"}],
        "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}],
    }))


def test_seed_is_applied_to_a_fresh_db_without_warning(tmp_path, minimal_seed, caplog):
    with caplog.at_level(logging.WARNING, logger="anima_world.__main__"):
        import fakeredis

        scheduler = build_serve_scheduler(
            "w", fakeredis.FakeStrictRedis(decode_responses=True),
            world_file=minimal_seed, force_mock_llm=True,
        )
    try:
        assert list(scheduler.agents) == ["a"]
        assert not any("--world-file" in r.getMessage() for r in caplog.records)
    finally:
        scheduler.stop()


def test_一份指名的世界文件能改一个已有的世界(tmp_path, minimal_seed, caplog):
    """**作者层本来就是给作者调试用的,所以它得改得动一个活着的世界。**

    这条此前守的是反过来的行为("作者层只进空世界,并且警告你它没生效")。
    那个行为的理由其实是另一件事:**内置的兜底文件**每次开机都在手上,拿它去填
    一个已有世界的空表,就会把橱窗的橡树塞进别人的世界(世界照跑、日志干净,
    只是它凭空多了一棵别人的树)。

    区分不在"世界空不空",在**这份文件是谁给的**:
      · 没给 `--world-file` → 用内置那份 → 只准进空世界
      · 给了 `--world-file`  → 一次**明示的编辑** → 生效

    语义仍然是**只填缺,不覆盖** —— 加得进新东西,不会把这个世界跑出来的现在
    倒带回创世那一刻。下面那条测试钉的就是这后半句。
    """
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    build_serve_scheduler("w", client, world_file=minimal_seed, force_mock_llm=True).stop()

    patch = tmp_path / "patch.cyberworld"
    patch.write_text(
        '{"kind": "manifest", "version": 3, "world_id": "w"}\n'
        '{"kind": "author", "type": "rule", "body": {"id": "r1", "every": {"days": 1},'
        ' "for_each": {"owner": "world"}, "set": {"季节": "季节"}}}\n',
        encoding="utf-8",
    )
    scheduler = build_serve_scheduler("w", client, world_file=patch, force_mock_llm=True)
    try:
        assert client.hexists("anima:w:world_rules", "r1"), (
            "指名的世界文件没能给一个已有的世界补上一条规律 —— 作者层就调不了试"
        )
    finally:
        scheduler.stop()


# ── 作者改得动一个跑着的世界的声明 ──────────────────────────────────────
#
# 「只填缺,不覆盖」那条纪律**说的是状态**:整份写回会把长了三十天的树倒带回
# 幼苗。而**声明不是状态** —— 一个种类、一条规律身上没有任何东西会随时间漂,
# 所以那条理由在它们身上不成立,而照搬的代价是:一个跑着的世界里的声明**永远
# 改不了**。灯塔湾就卡在这儿 —— 一个教英语的世界,每个动词按钮上印的是引擎的
# 中文默认词,而里面住着四个真人,唯一的修法是连他们的进度一起抹掉重建。
#
# 下面四条钉的是同一件事的四个面:改得动、镜像跟着改、实例仍旧只增、
# **而内置兜底那份一个字都改不动**(最后那条是安全的那一半,松了就等于让橱窗
# 每次开机去改写别人世界的物理法则)。

def _kind_patch(path, kind_body: dict, world_id: str = "w"):
    import json as _json
    path.write_text(
        _json.dumps({"kind": "manifest", "version": 3, "world_id": world_id},
                    ensure_ascii=False) + "\n"
        + _json.dumps({"kind": "author", "type": "kind", "body": kind_body},
                      ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


_ROCK = {
    "id": "rock",
    "gloss": "a rock",
    "quantities": {"moss": {"default": 0.0, "visibility": "here", "label": "moss"}},
    "affordances": {"look": {}},
}


def _world_with_a_rock(tmp_path, client):
    seed = pathlib.Path(write_seed_file(tmp_path / "rocky.cyberworld", {
        "agents": [{"id": "a", "name": "阿岚", "location": "cafe", "personality": "安静"}],
        "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}],
        "kinds": [_ROCK],
        "entities": [{"id": "rock:one", "name": "a rock", "location": "cafe"}],
    }))
    build_serve_scheduler("w", client, world_file=seed, force_mock_llm=True).stop()
    return seed


def test_作者指名的文件改得动一个已有种类的人话(tmp_path):
    """`look` 不写 `label` 就念引擎的中文默认词。一个教英语的世界里那是噪音,
    而从前它改不掉 —— 「同名的一个字都不动」把作者永远锁在创世那天的笔误上。"""
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    _world_with_a_rock(tmp_path, client)

    patch = _kind_patch(tmp_path / "relabel.cyberworld", {
        **_ROCK, "affordances": {"look": {"label": "take a good look"}},
    })
    scheduler = build_serve_scheduler("w", client, world_file=patch, force_mock_llm=True)
    try:
        verb = scheduler.ontology.kinds["rock"].affordances["look"]
        assert verb.label == "take a good look", (
            f"作者改了这个动词的人话,世界还念着旧的:{verb.label!r}"
        )
    finally:
        scheduler.stop()


def test_声明改了_可见性那份镜像跟着改(tmp_path):
    """可见性表是声明的**镜像**,而她读到的是镜像那个。两边不一致的话,同一个量
    在菜单上一个名字、在拒绝语里另一个名字 —— 而没有任何一处会报错。"""
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    _world_with_a_rock(tmp_path, client)

    patch = _kind_patch(tmp_path / "revis.cyberworld", {
        **_ROCK,
        "quantities": {"moss": {"default": 0.0, "visibility": "public", "label": "how mossy"}},
    })
    scheduler = build_serve_scheduler("w", client, world_file=patch, force_mock_llm=True)
    try:
        table = scheduler.visibility_store.labels_map()
        assert table.get(("rock", "moss")) == "how mossy", (
            f"声明改了而镜像没改,同一个量两个名字:{table.get(('rock', 'moss'))!r}"
        )
    finally:
        scheduler.stop()


def test_实例仍旧只增_改不动一个已有实例(tmp_path):
    """种类放开了,实例没有 —— 它有 `location`,而位置另有一份落在可见性表里
    (那一份是只填缺地写的)。覆盖了这边不覆盖那边,同一个东西会有两个"它在哪"。"""
    import fakeredis
    import json as _json

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    _world_with_a_rock(tmp_path, client)

    patch = tmp_path / "moverock.cyberworld"
    patch.write_text(
        '{"kind": "manifest", "version": 3, "world_id": "w"}\n'
        + _json.dumps({"kind": "author", "type": "entity", "body": {
            "id": "rock:one", "name": "somewhere else", "location": "cafe",
        }}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    scheduler = build_serve_scheduler("w", client, world_file=patch, force_mock_llm=True)
    try:
        assert scheduler.ontology.entities["rock:one"].name == "a rock", (
            "实例被文件覆盖了 —— 它的位置在可见性表里还有一份,两边会对不上"
        )
    finally:
        scheduler.stop()


def test_内置兜底那份一个字都改不动这个世界的声明(tmp_path):
    """安全的那一半。放开的是**作者指名的那份文件**,不是"每次开机手里那份"——
    内置橱窗每次开机都在,让它去重写同名的声明,等于每次重启都拿别人世界的物理
    法则盖掉这个世界的。而橱窗里恰好就有一个 `agent` 种类。"""
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    seed = pathlib.Path(write_seed_file(tmp_path / "mine.cyberworld", {
        "agents": [{"id": "a", "name": "阿岚", "location": "cafe", "personality": "安静"}],
        "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}],
        "kinds": [{"id": "agent", "quantities": {"nerve": {"default": 0.5, "label": "nerve"}}}],
    }))
    build_serve_scheduler("w3", client, world_file=seed, force_mock_llm=True).stop()
    mine = json.loads(client.hget("anima:w3:kinds", "agent"))["definition"]

    # 第二次开机**不给** --world-file:走内置兜底那条路
    scheduler = build_serve_scheduler("w3", client, force_mock_llm=True)
    try:
        after = json.loads(client.hget("anima:w3:kinds", "agent"))["definition"]
        assert after == mine, f"橱窗重写了这个世界的 agent 声明:{after}"
    finally:
        scheduler.stop()


def test_没变的声明不重写_不然改动时间永远读不出来(tmp_path):
    """同一份文件再开一次机,`updated_at` 不许被刷成开机时间 —— 刷了的话
    "这条声明上次改是什么时候"这个问题从此没有答案。"""
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    seed = _world_with_a_rock(tmp_path, client)
    stamped = json.loads(client.hget("anima:w:kinds", "rock"))["updated_at"]

    scheduler = build_serve_scheduler("w", client, world_file=seed, force_mock_llm=True)
    try:
        assert json.loads(client.hget("anima:w:kinds", "rock"))["updated_at"] == stamped
    finally:
        scheduler.stop()


def test_作者指名的文件改得动一条已有的规律(tmp_path, minimal_seed):
    """规律是这个世界的物理法则,而一条常数步长不看 `dt` 的规律会漂(线上那个
    世界因此把雨天数多烧了 6 天)。从前修它要连玩家的进度一起抹掉重建。"""
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    build_serve_scheduler("w4", client, world_file=minimal_seed, force_mock_llm=True).stop()

    def _patch(path, expression):
        path.write_text(
            '{"kind": "manifest", "version": 3, "world_id": "w4"}\n'
            + json.dumps({"kind": "author", "type": "rule", "body": {
                "id": "drift", "every": {"days": 1}, "for_each": {"owner": "world"},
                "set": {"雨天数": expression},
            }}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    build_serve_scheduler(
        "w4", client, world_file=_patch(tmp_path / "v1.cyberworld", "雨天数 + 1"),
        force_mock_llm=True).stop()
    scheduler = build_serve_scheduler(
        "w4", client, world_file=_patch(tmp_path / "v2.cyberworld", "雨天数 + dt / 288"),
        force_mock_llm=True)
    try:
        live = {r.id: r for r in scheduler.world_rules}["drift"]
        assert "dt" in live.outputs["雨天数"].names, (
            "作者修好了一条漂着的规律,而这个世界还在跑旧的那条"
        )
    finally:
        scheduler.stop()


def test_内置那份兜底文件不许去填一个已有的世界(tmp_path, minimal_seed):
    """这是上面那条的另一半,而它守的是一个真出过的 bug。

    内置文件每次开机都在手上。按"这张表恰好还空着"判的话,一个从 1.x 迁过来、
    `stocks` 本来就空的世界,开机一次就多出橱窗那棵 `tree:harbor_oak` ——
    **世界照跑、日志干净**,只是它凭空多了一棵别人世界里的树。
    """
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    build_serve_scheduler("w2", client, world_file=minimal_seed, force_mock_llm=True).stop()

    # 第二次开机**不给** --world-file:走内置兜底那条路
    scheduler = build_serve_scheduler("w2", client, force_mock_llm=True)
    try:
        owners = {k.split(":", 2)[-1] for k in client.keys("anima:w2:stock:*")}
        assert not any("harbor_oak" in o for o in owners), (
            f"橱窗的东西漏进了这个世界:{owners}"
        )
    finally:
        scheduler.stop()


def test_state_names_the_reason_the_llm_is_mocked(tmp_path):
    from anima_world.api import World

    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        llm = world.state()["runtime"]["llm"]
    assert llm["mock"] is True
    assert llm["degraded_reason"] == "llm.api_key 还没配"


def test_malformed_rich_seed_sections_degrade_instead_of_stranding_the_world(tmp_path, caplog):
    """relations/memories 不在最小 schema 校验里,而它们的播种跑在创世事件已落盘
    之后——这里崩溃会留下一个半初始化且永不重播种的世界。必须逐条降级。"""
    from anima_world.world_file import WorldFileManifest, write_world_file

    seed = tmp_path / "seed.cyberworld"
    write_world_file(seed, WorldFileManifest(world_id="w"), [
        {"kind": "author", "type": "agent",
         "body": {"id": "a", "name": "阿岚", "location": "cafe", "personality": "安静"}},
        {"kind": "author", "type": "agent",
         "body": {"id": "b", "name": "小北", "location": "cafe", "personality": "外向"}},
        {"kind": "author", "type": "location",
         "body": {"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}},
        # 下面这几条**结构上合法、内容上是坏的** —— 格式挡不住它们(也不该挡:
        # 格式管的是"这是不是一条记录",不是"这条记录说得通吗"),所以它们会一路
        # 走到播种,而播种必须逐条降级。
        {"kind": "author", "type": "relation", "body": {"这不是": "一条关系"}},
        {"kind": "author", "type": "memory", "body": {"agent_id": ["a"], "summary": "id 不可哈希"}},
        {"kind": "author", "type": "memory",
         "body": {"agent_id": "a", "summary": "好记忆", "importance": "很重要"}},
    ], compress=False, checksum=False)
    import fakeredis

    with caplog.at_level(logging.WARNING, logger="anima_world.__main__"):
        scheduler = build_serve_scheduler(
            "w", fakeredis.FakeStrictRedis(decode_responses=True),
            world_file=seed, force_mock_llm=True,
        )
    try:
        assert sorted(scheduler.agents) == ["a", "b"], "坏 relations/memories 不得阻断世界初始化"
        messages = [r.getMessage() for r in caplog.records]
        assert any("relation" in m for m in messages)
        assert any("importance" in m for m in messages)
    finally:
        scheduler.stop()


# ── 重声明撤掉的量:不裁剪,但必须吭声(3.8.0,设计-插件系统 §7)────────────────
#
# 重声明一个种类是**整行替换**,所以作者从 `kinds` 里划掉一个量,本体层就当它不存在
# 了 —— **而存储那一侧一格都不裁剪**:量还躺在 `:stocks` 里,可见性行还躺在
# `:stock_visibility` 里,而 `perception` 读的正是这两张表的交集(它不问 `:kinds`)。
# 下场是那个量顶着旧 label 继续进她的提示词,规律再也不更新它,一处不报错。
#
# 同一份文件里两条同 id 的 `kind` 会当场报错,所以这件事**只可能跨两次开机**发生 ——
# 也就是它必然安静。这一版只吭声不裁剪(裁剪归插件命名空间那一期)。


def _tree_world(tmp_path, quantities: dict) -> pathlib.Path:
    return pathlib.Path(write_seed_file(tmp_path / "tree.cyberworld", {
        "agents": [{"id": "a", "name": "阿岚", "location": "cafe", "personality": "安静"}],
        "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}],
        "kinds": [{"id": "tree", "quantities": quantities}],
        "entities": [{"id": "tree:oak", "name": "老橡树", "location": "cafe"}],
    }))


_THREE = {
    "树高": {"default": 1.0, "visibility": "here", "label": "树高"},
    "湿度": {"default": 0.5, "visibility": "here", "label": "湿度"},
    "生长速度": {"default": 0.05, "visibility": "here", "label": "生长速度"},
}
_TWO = {k: v for k, v in _THREE.items() if k != "生长速度"}


def _redeclare(tmp_path, client, quantities: dict) -> pathlib.Path:
    """一次**明示的编辑**:只带一条重声明的 `kind`,别的段一个字不写。"""
    patch = tmp_path / "patch.cyberworld"
    patch.write_text(
        '{"kind": "manifest", "version": 3, "world_id": "w"}\n'
        + json.dumps({"kind": "author", "type": "kind",
                      "body": {"id": "tree", "quantities": quantities}},
                     ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return patch


def test_重声明撤掉一个量_开机点它的名(tmp_path, caplog):
    """评审 2 实测的那条:`{树高,湿度,生长速度}` → `{树高,湿度}` → 点名 `生长速度`。"""
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    build_serve_scheduler("w", client, world_file=_tree_world(tmp_path, _THREE),
                          force_mock_llm=True).stop()
    patch = _redeclare(tmp_path, client, _TWO)

    with caplog.at_level(logging.WARNING, logger="anima_world.__main__"):
        scheduler = build_serve_scheduler("w", client, world_file=patch,
                                          force_mock_llm=True)
    try:
        lines = [r.getMessage() for r in caplog.records
                 if "dropped_quantities" in r.getMessage()]
        assert lines, "撤掉一个量,开机一个字都没说"
        assert 'dropped_quantities: {"tree": ["生长速度"]}' in lines[0], lines[0]
        # **不裁剪** —— 这一版只吭声。裁掉了就该改这条用例,而不是让它悄悄过。
        assert "生长速度" in scheduler.stock_store.of("tree:oak"), (
            "这一期不该裁剪(裁剪归插件命名空间那一期)"
        )
        assert ("tree", "生长速度") in scheduler.visibility_store.rules_map(), (
            "可见性行也不该被裁掉 —— 它正是那个量还进得了提示词的原因"
        )
    finally:
        scheduler.stop()


def test_没撤过量的世界一个字都不说(tmp_path, caplog):
    """**一句总在响的警告等于没有警告。** 原样重声明一遍,不许出声。"""
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    build_serve_scheduler("w", client, world_file=_tree_world(tmp_path, _THREE),
                          force_mock_llm=True).stop()
    patch = _redeclare(tmp_path, client, _THREE)

    with caplog.at_level(logging.WARNING, logger="anima_world.__main__"):
        build_serve_scheduler("w", client, world_file=patch, force_mock_llm=True).stop()
    assert not [r for r in caplog.records if "dropped_quantities" in r.getMessage()]


def test_内置橱窗开机不许报这条(caplog):
    """**这是那道假警报的判据。** 内置种类(`world` / `location`)的量另有来路 ——
    作者写在 `stock_visibility` / `stocks` 段里,从来不属于任何 `kinds` 声明。
    把它们算进来,橱窗开机第一句就是三条假警报(实测
    `{"world": ["季节","雨势","雨天数"]}`),而**一句会误报的警告等于没有这条警告**。
    """
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    with caplog.at_level(logging.WARNING, logger="anima_world.__main__"):
        scheduler = build_serve_scheduler("w", client, force_mock_llm=True)
        scheduler.stop()
        # 第二次开机(它会把作者层再走一遍)照样不许说。
        build_serve_scheduler("w", client, force_mock_llm=True).stop()
    assert not [r for r in caplog.records if "dropped_quantities" in r.getMessage()]


def test_作用在agent上的规律那一支_resolve够不着而这里说得出(tmp_path, caplog):
    """`resolve` 的量名闸**只查非内置种类**(内置种类的量不归本体层声明),
    所以一条 `for_each: {"kind": "agent"}` 的规律引用了一个已经不声明的量时,
    开机照旧成功 —— 只有这一条警告说得出。"""
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    seed = pathlib.Path(write_seed_file(tmp_path / "agent.cyberworld", {
        "agents": [{"id": "a", "name": "阿岚", "location": "cafe", "personality": "安静"}],
        "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}],
        "kinds": [{"id": "agent", "quantities": {
            "体力": {"default": 100.0, "visibility": "self", "label": "体力"},
            "干劲": {"default": 1.0, "visibility": "self", "label": "干劲"},
        }}],
        "rules": [{"id": "泄气", "every": {"days": 1}, "for_each": {"kind": "agent"},
                   "set": {"干劲": "干劲 - 0.01"}}],
    }))
    build_serve_scheduler("w", client, world_file=seed, force_mock_llm=True).stop()

    patch = tmp_path / "drop.cyberworld"
    patch.write_text(
        '{"kind": "manifest", "version": 3, "world_id": "w"}\n'
        + json.dumps({"kind": "author", "type": "kind", "body": {
            "id": "agent",
            "quantities": {"体力": {"default": 100.0, "visibility": "self",
                                    "label": "体力"}}}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="anima_world.__main__"):
        build_serve_scheduler("w", client, world_file=patch, force_mock_llm=True).stop()
    lines = [r.getMessage() for r in caplog.records if "dropped_quantities" in r.getMessage()]
    assert lines and "干劲" in lines[0], lines
