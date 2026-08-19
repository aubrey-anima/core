"""地点有图:作者写下的那两张图,到不到得了玩家眼前。

和 `test_character_card.py` 守的是同一类东西 —— **一条整链,不是一个函数**。
立绘那一次的病是"作者写得进、校验放行、世界跑得动、包也导得出,就是到不了玩家
眼前,而全程零报错";地点的图比它多两道筛子(`_LOCATION_ENTRY_FIELDS` 与
`_LOCATION_FIELDS`),每一道都是一个字典推导,漏了就**安静地筛掉**。所以断言
一律钉在**链的出口**上(`state()` / `map_data()` / 导出再导入),不钉在中间那几个
推导上:每一节单独测绿而合起来给错东西,正是这条链要防的形状。

两格而不是一格的理由(以及哪个是哪个)写在 `media.LOCATION_IMAGE_GLOSS` 里,
而且**进了 `contract --json`** —— 创作台的铁律是"问引擎,不读文档"。
"""
from __future__ import annotations

import base64
import gzip
import json

import pytest
from _worldfile import current_client, open_world_at, run_cli, write_seed_file

from anima_world.api import World
from anima_world.media import (
    LOCATION_IMAGE_KEYS,
    LOCATION_IMAGE_MAX_BYTES,
    MediaScan,
)
from anima_world.character_card import PORTRAIT_MAX_BYTES
from anima_world.world_seed import (
    WORLD_SEED_LOCATION_KEYS,
    WORLD_SEED_LOCATION_OPTIONAL_KEYS,
)

pytest.importorskip("fakeredis")

_MAP = "https://img.example.com/t/wharf.png"
_SCENE = "https://cdn.example.com/scene/wharf.jpg"


def _seed(**images: str) -> dict:
    """一份最小的能开机的世界:两个地点,图只挂在第一个上。"""
    wharf = {"id": "wharf", "name": "码头", "description": "海风很大",
             "kind": "point", "parent": None, "x": 0.3, "y": 0.4}
    wharf.update(images)
    return {
        "agents": [{"id": "岸", "name": "阿岸", "location": "wharf",
                    "personality": "话不多。"}],
        "locations": [
            wharf,
            {"id": "light", "name": "灯塔", "description": "塔",
             "kind": "point", "parent": None, "x": 0.7, "y": 0.2},
        ],
    }


def _write(tmp_path, seed: dict, name: str = "w.cyberworld") -> str:
    return write_seed_file(tmp_path / name, seed)


def _inline(raw_bytes: int) -> str:
    """一条 `data:` URI,原图 `raw_bytes` 字节(base64 之后约是它的 4/3)。"""
    return "data:image/png;base64," + base64.b64encode(b"\0" * raw_bytes).decode()


def _rows(world: World) -> dict[str, dict]:
    return {row["id"]: row for row in world.state()["locations"]}


# ---------------------------------------------------------------- 可选,不是必填

def test_两格图是可选的_不是必填():
    """**没有图的世界不是坏世界。**

    把它们加进 `WORLD_SEED_LOCATION_KEYS`(必填集)等于要求每一个已经存在的
    世界现在就配齐图 —— 舰队上那两个当场开不了机。所以它们单列一格,和 `card`
    在角色那一侧的安排逐字相同。
    """
    for key in LOCATION_IMAGE_KEYS:
        assert key not in WORLD_SEED_LOCATION_KEYS
        assert key in WORLD_SEED_LOCATION_OPTIONAL_KEYS


# ---------------------------------------------------------------- 链的两头

def test_作者写下的两张图_从state读得回来(tmp_path):
    """**一路上每一道筛子一次走通。**

    作者层 → `_LOCATION_ENTRY_FIELDS` → `_LOCATION_FIELDS` → `upsert` 的缺省行 →
    `api._LOCATION_KEYS` → `state()`。这一条红的时候,前面每一处的单元测试都
    还是绿的 —— 它们各自"正确地"把不认识的键筛掉了。
    """
    seed = _seed(map_image=_MAP, scene_image=_SCENE)
    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, seed),
                          force_mock_llm=True)
    try:
        rows = _rows(world)
        assert rows["wharf"]["map_image"] == _MAP
        assert rows["wharf"]["scene_image"] == _SCENE
        # 没写图的地点这两格是 `None`,不是"没有这个键" —— 宿主拿到的是整行,
        # 少一个键会在某条路上变成 KeyError。
        assert rows["light"]["map_image"] is None
        assert rows["light"]["scene_image"] is None
    finally:
        world.close()


def test_地图数据出口也带着图(tmp_path):
    """`map_data()` 是画图的人真正读的那一份(`map --json` 是契约,字符画是赠品)。

    **写了才出现**,和 `w`/`h` 同一个安排:一张没有图的地图的 `--json` 因此和
    从前逐字节相同。
    """
    seed = _seed(map_image=_MAP)
    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, seed),
                          force_mock_llm=True)
    try:
        places = {p["id"]: p for p in world.map_data()["places"]}
        assert places["wharf"]["map_image"] == _MAP
        assert "scene_image" not in places["wharf"]
        assert "map_image" not in places["light"]
    finally:
        world.close()


def test_图跟着包走_导出再导入还在(tmp_path):
    """包是分发物 —— 图在里面而不在里面,是"这个世界发出去还是不是那个世界"。"""
    seed = _seed(map_image=_MAP, scene_image=_SCENE)
    world = open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, seed),
                          force_mock_llm=True)
    out = str(tmp_path / "out.cyberworld")
    try:
        world.export_snapshot(out, world_id="pkg", name="带图的世界")
    finally:
        world.close()

    from anima_world.world_package import import_world_file

    client = current_client()
    import_world_file(out, redis=client, world_id="w2")
    again = World.open("w2", redis=client, force_mock_llm=True)
    try:
        rows = _rows(again)
        assert rows["wharf"]["map_image"] == _MAP
        assert rows["wharf"]["scene_image"] == _SCENE
    finally:
        again.close()


# ---------------------------------------------------------------- 闸

def test_相对路径的图_开不了机而且一次列全(tmp_path):
    """包是分发物:`images/a.png` 发出去就是一张断的图,**而且不报错**。

    一次列全的理由和角色卡那边一样 —— 世界文件只读进空库一次,作者改一条重试
    一次的代价是重建世界。
    """
    seed = _seed(map_image="images/wharf.png", scene_image="../scene.jpg")
    with pytest.raises(Exception) as exc:
        open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, seed),
                      force_mock_llm=True)
    blob = "\n".join(getattr(exc.value, "errors", None) or [str(exc.value)])
    assert "map_image" in blob and "scene_image" in blob, blob


def test_内嵌的图有上限_而且量的是URI本身(tmp_path):
    """`data:` 不禁,给一个数 —— 禁掉等于宣布一个 `.cyberworld` 不可能自足。

    上限按**读出口**定:地点的图骑在 `state()` 上,而网站每几秒问一次这道门、
    一次带回全部地点。量的是这条 URI 字符串(base64 之后约是原图的 4/3),
    所以断言也照这个口径写。
    """
    under = _inline(LOCATION_IMAGE_MAX_BYTES // 2)
    over = _inline(LOCATION_IMAGE_MAX_BYTES)      # ×4/3 之后必然越线
    assert len(under.encode()) <= LOCATION_IMAGE_MAX_BYTES < len(over.encode())

    world = open_world_at(tmp_path / "ok.db",
                          world_file=_write(tmp_path, _seed(map_image=under), "ok.cyberworld"),
                          force_mock_llm=True)
    try:
        assert _rows(world)["wharf"]["map_image"] == under
    finally:
        world.close()

    with pytest.raises(Exception) as exc:
        open_world_at(tmp_path / "big.db",
                      world_file=_write(tmp_path, _seed(map_image=over), "big.cyberworld"),
                      force_mock_llm=True)
    blob = "\n".join(getattr(exc.value, "errors", None) or [str(exc.value)])
    assert "map_image" in blob and str(LOCATION_IMAGE_MAX_BYTES) in blob, blob
    # 报错里不许把那条 URI 原样吐回来 —— 一条 300 KB 的 data: 会把终端刷没。
    assert len(blob) < 2000, "错误信息把整条 data: URI 吐回来了"


def test_立绘也有了上限_这一格引擎从前一个字节都没管过(tmp_path):
    """和地点的图同一轮补的那一格。

    此前:一张 8 MB 的图 base64 进世界文件,加载放行、导出带着走、`roster()`
    每次整个吐出来,而没有任何一处会说一句话。上限比地点那两格宽,因为
    `roster()` 是按需拿一次,不是每几秒被轮询一遍 —— **两个数不必相等**。
    """
    assert PORTRAIT_MAX_BYTES > LOCATION_IMAGE_MAX_BYTES
    seed = _seed()
    seed["agents"][0]["card"] = {"portrait": _inline(PORTRAIT_MAX_BYTES)}
    with pytest.raises(Exception) as exc:
        open_world_at(tmp_path / "w.db", world_file=_write(tmp_path, seed),
                      force_mock_llm=True)
    blob = "\n".join(getattr(exc.value, "errors", None) or [str(exc.value)])
    assert "card.portrait" in blob and str(PORTRAIT_MAX_BYTES) in blob, blob


def test_老文档里那个image键_只警告不拦(tmp_path):
    """`FOR-STUDIO §3.22` 公布过单数的 `image`,后来改成了两格。

    公布出去的名字收不回来,而照着它写的人得到的是"装载时安静丢掉":世界照跑、
    日志干净、图一张都不出现。**拦不下**(不认识的键原样带过去是这一层的规矩),
    但必须点名。
    """
    seed = _seed(image="https://img.example.com/t/wharf.png")
    path = _write(tmp_path, seed)
    result = run_cli("world", "check", path, "--json")
    payload = json.loads(result.stdout)
    assert payload["loadable"] is True, payload["errors"]
    assert any("image" in w and "map_image" in w for w in payload["warnings"]), payload

    # 开机也要说 —— 托管环境里没有人会去跑那条命令。
    world = open_world_at(tmp_path / "w.db", world_file=path, force_mock_llm=True)
    world.close()


# ---------------------------------------------------------------- world check 的两段

def _check(path) -> dict:
    result = run_cli("world", "check", str(path), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_外链按host列出来_不联网探活(tmp_path):
    """这一段报的是"这个包挂在谁身上",不是"这些链接还活着吗"。

    图床由网站自己建、世界里只留绝对外链,是定下来的形状 —— 那么这条选择的
    **代价**就该在拿到包的那一刻看得见:包发出去之后,这里列出的每一台都得继续
    活着,而包自己不会因为哪一台没了就报错,它只是少几张图。

    **不联网**是硬要求:一个会去 ping 的校验器在没有网的机器上、在一台正好挂了
    的 CDN 面前,会把"这个世界能不能装"答成"今天网通不通"。所以这里用的是一个
    不存在的域名 —— 真去探活的话这条会超时。
    """
    seed = _seed(map_image="https://img.example.invalid/t/wharf.png",
                 scene_image="https://cdn.example.invalid/s/wharf.jpg")
    seed["agents"][0]["card"] = {"portrait": "https://img.example.invalid/p/an.png"}
    payload = _check(_write(tmp_path, seed))

    by_host = {row["host"]: row for row in payload["external_media"]}
    assert set(by_host) == {"img.example.invalid", "cdn.example.invalid"}
    assert by_host["img.example.invalid"]["count"] == 2
    assert by_host["img.example.invalid"]["fields"] == ["map_image", "portrait"]
    assert payload["inline_media_bytes"] == {"count": 0, "total": 0, "largest": 0}


def test_内嵌的字节数报出来_这是包为自足付的钱(tmp_path):
    seed = _seed(map_image=_inline(1000), scene_image=_inline(2000))
    payload = _check(_write(tmp_path, seed))
    inline = payload["inline_media_bytes"]
    assert inline["count"] == 2
    assert inline["total"] > 3000 and inline["largest"] > inline["total"] // 2
    assert payload["external_media"] == []


def test_同一张图抄在几处只算一张(tmp_path):
    """数的是"这个世界有多少张图",不是"这个字符串出现了几次"。

    后者是导出格式的实现细节:出生证明(`:meta.world_seed`)里抄着一份作者层、
    `agent_join` 的载荷里又是一份 —— 于是一个只有一张立绘的新世界会被报成三张。
    **同一件事被数成三件,和数漏了一样是个错答案。**
    """
    seed = _seed(map_image=_MAP, scene_image=_MAP)
    payload = _check(_write(tmp_path, seed))
    assert [row["count"] for row in payload["external_media"]] == [1]


def test_跑过的世界导出来_图照样数得出(tmp_path):
    """一个跑过的世界只有状态记录,而图就在那几行 Redis 行和事件载荷里。

    只数作者层的话,这种包会得到一句"外链 0 条" —— 一个安静的错答案,正是这一段
    要避免的东西。所以两种文件在这里必须给出**同一个数**。
    """
    seed = _seed(map_image=_MAP, scene_image=_SCENE)
    authored = _write(tmp_path, seed)
    world = open_world_at(tmp_path / "w.db", world_file=authored, force_mock_llm=True)
    out = str(tmp_path / "out.cyberworld")
    try:
        world.export_snapshot(out, world_id="pkg", name="带图的世界")
    finally:
        world.close()

    def counts(path):
        return {row["host"]: row["count"] for row in _check(path)["external_media"]}

    assert counts(out) == counts(authored) == {
        "img.example.com": 1, "cdn.example.com": 1,
    }


def test_扫描器不把配置里的URL当成图():
    """按**键名**认,不按"值看着像不像一条 URL"猜。

    猜的那一版会把 `llm.base_url` 数成一张图 —— 而这一段的用途是让外链的代价
    看得见,把一个 API 端点混进"图挂在哪几台服务器上",这笔账当场就不能看了。
    """
    scan = MediaScan()
    scan.feed({
        "config": {"llm.base_url": "https://api.openai.com/v1",
                   "llm.model": "gpt-4o-mini"},
        "locations": [{"id": "a", "map_image": "https://img.example.com/a.png"}],
    })
    assert [row["host"] for row in scan.external_media()] == ["img.example.com"]


def test_读不完的文件不报半份账(tmp_path):
    """半途抛错的那趟扫描手里是一份残缺的账。

    "12 条外链的包报出 3 条"是一个**安静的错答案**,比一句"没数"坏得多 ——
    和 `loadable: null`(问不出来)是同一个姿势。
    """
    path = tmp_path / "broken.cyberworld"
    rows = [
        {"kind": "manifest", "version": 3, "world_id": "t", "name": "半截的",
         "engine_min": "3.0.0"},
        {"kind": "author", "type": "location",
         "body": {"id": "a", "name": "a", "description": "x",
                  "map_image": "https://img.example.com/a.png"}},
        {"kind": "author", "type": "不认识的段", "body": {"id": "b"}},
    ]
    with gzip.GzipFile(path, "wb", mtime=0) as fh:
        fh.write(("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n").encode())

    result = run_cli("world", "check", str(path), "--json")
    payload = json.loads(result.stdout)
    assert payload["loadable"] is False
    assert payload["external_media"] == []
    assert payload["inline_media_bytes"] == {"count": 0, "total": 0, "largest": 0}


# ---------------------------------------------------------------- 契约

def test_契约自己说得出两格是什么(tmp_path):
    """创作台的铁律是**问引擎,不读文档** —— 所以"哪个是哪个"必须在契约里。

    只报两个键名的契约等于让它去猜哪个铺满屏幕、哪个是地图上那一格,而猜错了
    不报错:两张图各在错的地方,看上去像是设计。
    """
    result = run_cli("contract", "--json")
    payload = json.loads(result.stdout)
    seed = payload["seed"]
    assert seed["location_optional_keys"] == sorted(LOCATION_IMAGE_KEYS)
    assert set(seed["location_image_keys"]) == set(LOCATION_IMAGE_KEYS)
    for gloss in seed["location_image_keys"].values():
        assert gloss.strip(), "每一格都得说清它是干什么用的"
    assert seed["location_image_max_bytes"] == LOCATION_IMAGE_MAX_BYTES
    assert payload["character_card"]["portrait_max_bytes"] == PORTRAIT_MAX_BYTES
    # 两处上限都在契约里,而且**不相等** —— 按各自的读出口定,见 media.py。
    assert seed["location_image_max_bytes"] != payload["character_card"]["portrait_max_bytes"]


def test_必填集没变_老世界一个都不该被拦下(tmp_path):
    """加一格可选字段不许动裁决面。镜像端拿 `location_keys` 算"少了什么"。"""
    payload = json.loads(run_cli("contract", "--json").stdout)
    assert payload["seed"]["location_keys"] == ["description", "id", "name"]


# ---------------------------------------------------------------- 已有的世界

def test_给已有的世界补图_装不进去时得说一句(tmp_path, caplog):
    """**没生效的编辑不许安静。**

    作者往世界文件里补了两格图,再把文件装回一个已经在跑的世界 —— 他要的是
    "图补上了",而作者层的语义是"只填缺、不覆盖",而覆盖粒度是**整行**:整行
    合并会把这个世界跑出来的名字和描述倒带回创世那天。所以这次编辑确实不生效,
    但它必须留下一句话。(真正的修法是一扇 `location set-image` 写门,还没做 ——
    见 REFERENCE §2.14 与 FOR-STUDIO §3.22。)
    """
    first = _write(tmp_path, _seed(), "v1.cyberworld")
    world = open_world_at(tmp_path / "w.db", world_file=first, force_mock_llm=True)
    world.close()

    later = _write(tmp_path, _seed(map_image=_MAP), "v2.cyberworld")
    with caplog.at_level("WARNING"):
        world = open_world_at(tmp_path / "w.db", world_file=later, force_mock_llm=True)
    try:
        assert _rows(world)["wharf"]["map_image"] is None, "整行合并会把世界倒带"
    finally:
        world.close()
    assert any("map_image" in r.getMessage() for r in caplog.records), "没生效的编辑一声不吭"
