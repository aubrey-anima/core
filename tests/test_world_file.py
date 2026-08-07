"""世界文件(`.cyberworld` v3,gzip + JSONL)的线格式。

这一层消掉的是**一层表示**:v3 之前"人写的世界描述"和"导出的 dump"是两种文件格式,
描述同一个世界时长得完全不一样(种子写 `duties`,dump 里是编译好的 `bt_nodes`),
于是要维护两套 schema、两套校验、两个跨仓库镜像。

v3 把它们合成一个文件、两层记录:`author` 装载时编译,`redis`/`event`/`mysql` 直接落。
于是**创世和还原是同一个动作** —— 往一个前缀里装一个世界文件。

这份测试盯的是线格式本身:往返、拒绝什么、以及那几条"错了不报错只会安静地少装一半
世界"的地方。
"""

from __future__ import annotations

import gzip
import json

import pytest

from anima_world.world_file import (
    WORLD_FILE_VERSION,
    WorldFileError,
    WorldFileManifest,
    author_records_to_seed,
    read_world_file,
    seed_to_author_records,
    write_world_file,
)


def _write(tmp_path, records, *, manifest=None, name="w.cyberworld", checksum=True):
    path = tmp_path / name
    write_world_file(path, manifest or WorldFileManifest(world_id="w"), records,
                     checksum=checksum)
    return path


def _lines(path):
    return [l for l in gzip.open(path, "rt", encoding="utf-8").read().splitlines() if l]


def _retype(path, lines):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ── 往返 ──────────────────────────────────────────────────────────────────


def test_写出去再读回来一条不差(tmp_path):
    records = [
        {"kind": "author", "type": "agent", "body": {"id": "夏", "name": "苏晚夏"}},
        {"kind": "redis", "key": "clock", "type": "string", "value": "8640"},
        {"kind": "event", "seq": 1, "ts": 0, "type": "payment", "payload": {"amount": 30}},
        {"kind": "mysql", "table": "memories", "row": {"id": 1, "summary": "她想起"}},
    ]
    path = _write(tmp_path, records)
    manifest, stream = read_world_file(path)
    assert manifest.version == WORLD_FILE_VERSION and manifest.world_id == "w"
    assert list(stream) == records


def test_解压出来就是能读的文本(tmp_path):
    """换成文本格式的全部理由就是这个:能 zcat、能 grep、能 diff。
    `ensure_ascii` 一旦打开,中文变成 \\uXXXX,这三条一起失效。"""
    path = _write(tmp_path, [
        {"kind": "author", "type": "location", "body": {"id": "cafe", "name": "咖啡店"}},
    ])
    text = gzip.open(path, "rt", encoding="utf-8").read()
    assert "咖啡店" in text, "中文被转义了,grep 就废了"
    assert text.splitlines()[0].startswith('{"kind": "manifest"')


def test_每条记录自己一行(tmp_path):
    """一行一条是流式与 diff 的前提。整块 JSON 的话,改一个键 diff 就是整份变。"""
    records = [{"kind": "event", "seq": i, "ts": 0, "type": "x", "payload": {}}
               for i in range(50)]
    path = _write(tmp_path, records)
    assert len(_lines(path)) == 1 + 50 + 1        # manifest + 50 + checksum


# ── 那个真撞过的坑 ────────────────────────────────────────────────────────


def test_载荷里可以有一个叫_kind_的字段(tmp_path):
    """**这条是被一次真碰撞逼出来的。** 种子的 `locations` 条目自己带 `kind`
    (嵌套地图的几何类型 `region`/`point`)。载荷要是平铺进信封,它就把记录类型
    覆盖掉了 —— 而这种覆盖是**静默**的:文件写得出来,读的时候变成另一种记录。

    收进 `body` 让它不可表达,比约定"别用这几个名字"可靠。
    """
    body = {"id": "harbor_st", "name": "海港街", "kind": "region", "type": "outdoor"}
    path = _write(tmp_path, [{"kind": "author", "type": "location", "body": body}])
    _, stream = read_world_file(path)
    (record,) = list(stream)
    assert record["kind"] == "author" and record["type"] == "location"
    assert record["body"] == body, "载荷里的 kind/type 必须原样活下来"


def test_载荷必须收在_body_里(tmp_path):
    """平铺写法要当场报错,而不是猜作者想说什么。"""
    with pytest.raises(WorldFileError) as excinfo:
        author_records_to_seed([{"kind": "author", "type": "agent", "id": "夏"}])
    assert "body" in str(excinfo.value)


# ── 拒绝什么 ──────────────────────────────────────────────────────────────


def test_manifest_必须是第一行(tmp_path):
    """装载器要先知道版本才敢读后面的东西。"""
    path = _write(tmp_path, [{"kind": "redis", "key": "clock", "type": "string", "value": "1"}])
    _retype(path, _lines(path)[1:])
    with pytest.raises(WorldFileError) as excinfo:
        read_world_file(path)
    assert "第一行" in str(excinfo.value)


def test_更新的格式版本当场拒绝(tmp_path):
    """**不猜。** 旧引擎读新格式,读成什么样都不算数 —— 装一个新些的引擎。"""
    path = _write(tmp_path, [])
    head = json.loads(_lines(path)[0])
    head["version"] = WORLD_FILE_VERSION + 1
    _retype(path, [json.dumps(head, ensure_ascii=False), *_lines(path)[1:]])
    with pytest.raises(WorldFileError) as excinfo:
        read_world_file(path)
    assert str(WORLD_FILE_VERSION + 1) in str(excinfo.value)


def test_不认识的记录类型是报错不是跳过(tmp_path):
    """**跳过它等于安静地少装一半世界**,而文件看上去完全正常。
    这正是这个仓库最怕的那一类:照跑,但给错东西。"""
    path = _write(tmp_path, [{"kind": "author", "type": "agent", "body": {"id": "夏"}}])
    _retype(path, [*_lines(path)[:2], '{"kind":"quantum_flux","x":1}', *_lines(path)[2:]])
    _, stream = read_world_file(path)
    with pytest.raises(WorldFileError) as excinfo:
        list(stream)
    assert "quantum_flux" in str(excinfo.value)


def test_不认识的作者类型也是报错(tmp_path):
    with pytest.raises(WorldFileError) as excinfo:
        author_records_to_seed([{"kind": "author", "type": "沙发", "body": {"id": "x"}}])
    assert "沙发" in str(excinfo.value)


def test_manifest_不许出现第二次(tmp_path):
    path = _write(tmp_path, [])
    head = _lines(path)[0]
    _retype(path, [head, head])
    _, stream = read_world_file(path)
    with pytest.raises(WorldFileError):
        list(stream)


def test_坏_json_带行号(tmp_path):
    path = _write(tmp_path, [{"kind": "redis", "key": "a", "type": "string", "value": "1"}])
    _retype(path, [_lines(path)[0], "{这不是 json", *_lines(path)[1:]])
    _, stream = read_world_file(path)
    with pytest.raises(WorldFileError) as excinfo:
        list(stream)
    assert "第 2 行" in str(excinfo.value)


def test_裸文本的世界文件照样读得进来(tmp_path):
    """**压缩与否只看头两个字节,不看扩展名。**

    手写一个世界不该被逼着先 gzip 一下 —— 而这正是这个格式取代种子的前提:
    它得是一份人能直接写出来的东西。内置的演示世界因此以纯文本进仓库,
    可 diff、可 review;一个 review 不了的二进制块不该是新用户看到的第一眼。
    """
    path = tmp_path / "plain.cyberworld"
    path.write_text(
        '{"kind":"manifest","version":3,"world_id":"w"}\n'
        '{"kind":"author","type":"agent","body":{"id":"夏","name":"苏晚夏"}}\n',
        encoding="utf-8",
    )
    manifest, stream = read_world_file(path)
    assert manifest.world_id == "w"
    assert author_records_to_seed(list(stream)) == {"agents": [{"id": "夏", "name": "苏晚夏"}]}


def test_内置演示世界就是纯文本(tmp_path):
    """它同时是格式的说明书 —— 新用户打开看到的第一屏。"""
    import importlib.resources as resources

    path = resources.files("anima_world") / "demo.cyberworld"
    head = path.read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith('{"kind": "manifest"'), "内置演示世界不是纯文本了"
    manifest, stream = read_world_file(str(path))
    seed = author_records_to_seed(list(stream))
    assert manifest.version == WORLD_FILE_VERSION
    assert len(seed["agents"]) == 3 and len(seed["locations"]) == 5


def test_文件不存在(tmp_path):
    with pytest.raises(WorldFileError) as excinfo:
        read_world_file(tmp_path / "没有这个")
    assert "不存在" in str(excinfo.value)


# ── 校验和 ────────────────────────────────────────────────────────────────


def test_路上被改过就读不过去(tmp_path):
    path = _write(tmp_path, [
        {"kind": "redis", "key": "clock", "type": "string", "value": "8640"},
    ])
    lines = _lines(path)
    tampered = lines[1].replace('"8640"', '"999999"')
    _retype(path, [lines[0], tampered, lines[2]])
    _, stream = read_world_file(path)
    with pytest.raises(WorldFileError) as excinfo:
        list(stream)
    assert "校验和" in str(excinfo.value)


def test_手写的文件不必带校验和(tmp_path):
    """作者手写一份世界不该被逼着算 sha256。带了就验,没带就算了。"""
    path = _write(tmp_path, [
        {"kind": "author", "type": "agent", "body": {"id": "夏"}},
    ], checksum=False)
    assert not any('"checksum"' in l for l in _lines(path))
    _, stream = read_world_file(path)
    assert len(list(stream)) == 1


def test_截断的文件读得出是截断的(tmp_path):
    path = _write(tmp_path, [
        {"kind": "event", "seq": i, "ts": 0, "type": "x", "payload": {}} for i in range(5)
    ])
    _retype(path, _lines(path)[:-2])            # 砍掉最后一条事件与校验和
    _, stream = read_world_file(path)
    assert len(list(stream)) == 4, "没有校验和时截断只能读到少的那些"


# ── 上限(gzip 一样做得出解压炸弹) ────────────────────────────────────────


def test_解压后超上限就不读了(tmp_path):
    path = _write(tmp_path, [
        {"kind": "event", "seq": i, "ts": 0, "type": "x", "payload": {"pad": "x" * 200}}
        for i in range(200)
    ])
    _, stream = read_world_file(path, max_uncompressed_bytes=2000)
    with pytest.raises(WorldFileError) as excinfo:
        list(stream)
    assert "解压后超过" in str(excinfo.value)


def test_记录条数有上限(tmp_path):
    path = _write(tmp_path, [
        {"kind": "event", "seq": i, "ts": 0, "type": "x", "payload": {}} for i in range(50)
    ])
    _, stream = read_world_file(path, max_records=10)
    with pytest.raises(WorldFileError):
        list(stream)


def test_单行也有上限(tmp_path):
    path = _write(tmp_path, [
        {"kind": "redis", "key": "big", "type": "string", "value": "x" * 5000},
    ])
    _, stream = read_world_file(path, max_line_bytes=1000)
    with pytest.raises(WorldFileError):
        list(stream)


# ── 流式 ──────────────────────────────────────────────────────────────────


def test_只问_manifest_不必读完整份(tmp_path):
    """`world inspect` 就是那个调用方:它想知道"这个包要哪个引擎",
    不该为此把一个 5 GB 的世界读完。"""
    path = _write(tmp_path, [
        {"kind": "event", "seq": i, "ts": 0, "type": "x", "payload": {}} for i in range(1000)
    ], manifest=WorldFileManifest(world_id="w", engine_min="2.0.0"))
    manifest, stream = read_world_file(path)
    assert manifest.engine_min == "2.0.0"
    first = next(iter(stream))                  # 只取一条就丢掉迭代器
    assert first["seq"] == 0


def test_写也是流式的(tmp_path):
    """`records` 可以是个生成器 —— 一个跑了两年的世界不必先攒进内存。"""
    def stream():
        for i in range(100):
            yield {"kind": "event", "seq": i, "ts": 0, "type": "x", "payload": {}}
    path = _write(tmp_path, stream())
    _, back = read_world_file(path)
    assert len(list(back)) == 100


# ── 作者层 ↔ 编译管线 ────────────────────────────────────────────────────


def test_作者记录聚合回编译管线吃的形状():
    """"换容器不动语义"的落点:聚合回 section 字典,既有的十八个编译步骤一行不改。"""
    seed = author_records_to_seed([
        {"kind": "author", "type": "agent", "body": {"id": "夏"}},
        {"kind": "author", "type": "agent", "body": {"id": "遥"}},
        {"kind": "author", "type": "location", "body": {"id": "cafe"}},
        {"kind": "author", "type": "config", "body": {"needs.enabled": True}},
        {"kind": "redis", "key": "clock", "type": "string", "value": "1"},   # 忽略
    ])
    assert seed == {
        "agents": [{"id": "夏"}, {"id": "遥"}],
        "locations": [{"id": "cafe"}],
        "config": {"needs.enabled": True},
    }


def test_config_是合并不是追加():
    """它是一个对象,不是一列条目 —— 两条 config 记录合并成一份。"""
    seed = author_records_to_seed([
        {"kind": "author", "type": "config", "body": {"a": 1}},
        {"kind": "author", "type": "config", "body": {"b": 2}},
    ])
    assert seed["config"] == {"a": 1, "b": 2}


def test_世界观是一段文本不是一张表():
    """**这个洞的形状定义在这里。** 引擎自己的两半曾经对不上:线格式把
    `world_setting` 归进"对象型"(body 必须是 dict),而播种函数只认字符串 ——
    于是任何 `.cyberworld` 的世界观都送不进世界,**而且一声不吭**。

    形状是:`body` 就是那段话本身。段的值 = body,不多包一层。
    """
    seed = author_records_to_seed([
        {"kind": "author", "type": "world_setting", "body": "旧港区,常年下雨。"},
    ])
    assert seed == {"world_setting": "旧港区,常年下雨。"}


def test_世界观写成对象是当场报错不是静默忽略():
    """两边对不上时必须有人说一句 —— 上一版的答案是没有人说话。"""
    with pytest.raises(WorldFileError) as excinfo:
        author_records_to_seed([
            {"kind": "author", "type": "world_setting", "body": {"text": "旧港区"}},
        ])
    assert "world_setting" in str(excinfo.value)

    with pytest.raises(WorldFileError):
        author_records_to_seed([
            {"kind": "author", "type": "world_setting", "body": "   "},
        ])


def test_世界观后一条盖前一条():
    """它是一段而不是一列 —— 记录按文件顺序应用,所以"导出一份、手改一行、
    装回去"照旧成立。"""
    seed = author_records_to_seed([
        {"kind": "author", "type": "world_setting", "body": "第一版"},
        {"kind": "author", "type": "world_setting", "body": "第二版"},
    ])
    assert seed["world_setting"] == "第二版"


def test_段的值类型不对是报错不是静默丢掉():
    """反方向同一条。这个方向更难发现:文件写得出来、装得进去,只是少了一整段
    (`world_setting` 写成字符串时,这里曾经一言不发地把整个世界观扔掉)。"""
    with pytest.raises(WorldFileError) as excinfo:
        seed_to_author_records({"world_setting": {"text": "旧港区"}})
    assert "world_setting" in str(excinfo.value)

    with pytest.raises(WorldFileError) as excinfo:
        seed_to_author_records({"config": "needs.enabled=true"})
    assert "config" in str(excinfo.value)


def test_转换器往返不丢东西():
    """"换容器不动语义"要真成立:section 字典 → author 记录 → section 字典,
    逐段相等。这条守着的是那 26 个按 section 字典写夹具的测试文件 ——
    转换器一旦有损,它们就在一个和产品不一样的世界上跑。"""
    seed = {
        "agents": [{"id": "夏", "name": "苏晚夏", "location": "cafe", "personality": "静",
                    "money": 120, "duties": [{"name": "看店", "start": "08:00", "end": "18:00"}]}],
        # 地点条目自己带 kind —— 正是那个撞过车的字段,往返必须原样活下来
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角", "kind": "point"}],
        "kinds": [{"id": "tree", "quantities": {"树高": {"default": 1.0}}}],
        "entities": [{"id": "tree:a", "name": "那棵"}],
        "rules": [{"id": "grow", "every": {"days": 1}, "for_each": {"kind": "tree"},
                   "set": {"树高": "树高 + 1"}}],
        "items": [{"id": "coffee", "name": "咖啡", "kind": "consumable"}],
        "relations": [{"a": "夏", "b": "遥", "sentiment": 0.3}],
        "memories": [{"agent": "夏", "summary": "她记得"}],
        "stocks": [{"owner": "world", "values": {"季节": 2.0}}],
        "stock_visibility": [{"kind": "tree", "key": "树高", "visible": "here"}],
        "config": {"needs.enabled": True},
        # 三个非列表段全在这里。少一个,那一段就可以在转换器里悄悄丢掉而这条依旧绿
        # —— `world_setting` 缺席了整整一个大版本,正是这么缺的。
        "world_setting": "旧港区,常年下雨,港务局说了算。",
        "mock_narration": {"sleep": "{agent} turned in for the night"},
    }
    assert author_records_to_seed(seed_to_author_records(seed)) == seed
