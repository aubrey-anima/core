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
    MEDIA_SCHEMES,
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
    # 但**「几张图」和「这台图床供着哪几格」是两个问题**,去重只该答第一个。
    # 从前 `fields` 挂在去重之后 return 的下游,于是第二格整个消失:同一张图
    # 既当缩略又当背景(网站的图床是内容寻址的 —— 同一张图传两次拿回同一条 URL,
    # 所以"一张图两处用"是常态不是边角),报出来却成了"这台只供缩略图"。
    assert [row["fields"] for row in payload["external_media"]] == [
        ["map_image", "scene_image"]
    ]


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
    # 闸的另一半:哪些 scheme 算数。**没钉住的契约格子等于没有契约** ——
    # 少了这一行,把 `data:` 从许可名单里拿掉不会让任何一处变红,而创作台照着
    # 一份旧契约生成的世界会在装载时才发现自己写的图引擎不认。
    assert seed["location_image_schemes"] == sorted(MEDIA_SCHEMES)
    assert seed["location_image_max_bytes"] == LOCATION_IMAGE_MAX_BYTES
    assert payload["character_card"]["portrait_max_bytes"] == PORTRAIT_MAX_BYTES
    # 两处上限都在契约里,而且**不相等** —— 按各自的读出口定,见 media.py。
    assert seed["location_image_max_bytes"] != payload["character_card"]["portrait_max_bytes"]


def test_契约说得出写门是哪一条命令(tmp_path):
    """**消费方按这一格决定那个按钮画不画,而不是比版本号。**

    3.3.0 这一格报的是 `None`,而那是当时唯一诚实的答案(补图只能重建世界)。
    3.4.0 写门真的有了,所以它必须变成命令名 —— 一个还报着 `null` 的契约会让
    创作台继续把那个按钮关着,而库里明明有,**库里有而对方看不见等于没有**。

    ⚠️ 断言的是**命令名字符串**,而且顺手钉住它真的存在(下面那条 CLI 测试)——
    报一个拼错的命令名比报 `null` 更坏:前者会被照着敲下去,得到 argparse 的
    退出码 2,而那个码和"引擎听懂了但不干"是同一个。
    """
    payload = json.loads(run_cli("contract", "--json").stdout)
    seed = payload["seed"]
    assert seed["location_image_read_command"] == "map"
    assert seed["location_image_write_command"] == "location set-image"
    gloss = seed["location_image_write_gloss"]
    assert "location set-image" in gloss
    # 作者层那条路**没变**,而契约得把这一点说出来:补图走写门,不是走文件。
    assert "只填缺" in gloss
    # 对照组:角色卡那一段形状一致,答案是另一条命令。
    assert payload["character_card"]["write_command"] == "agent set-card"

    human = run_cli("contract").stdout
    assert "anima-world location set-image" in human
    assert "没有(只在作者层落地)" not in human


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
    said = [r.getMessage() for r in caplog.records if "map_image" in r.getMessage()]
    assert said, "没生效的编辑一声不吭"
    # **一句只说"没装进去"的警告,是把人留在原地。** 3.4.0 起有解法了,这句话
    # 就得指向它 —— 否则读日志的人得到的仍然是"只能重建这个世界"。
    assert any("location set-image" in message for message in said), (
        "警告没说该怎么办 —— 而写门就在隔壁"
    )


def test_离线两扇门也说得出这次编辑的图装不进去(tmp_path):
    """上一条那句话从前**只在真开机时才说得出来**。

    而作者的顺序是先 `validate world --edit`(或运维台的 `world check --edit`),
    绿了才装。于是他拿到一个绿灯、装完、图没了 —— 只有去翻服务器日志才查得到
    原因,而那正是"离线两扇门"存在的全部理由:开机之前就把话说完。

    **两扇门都得说**:`test_validate_matches_boot` 只钉了错误相等,警告可以不同 ——
    也就是说这里少钉一扇,两扇门就会安静地给出不同的判断,而那正是那份文件在防的事。
    """
    path = _write(tmp_path, _seed(map_image=_MAP, scene_image=_SCENE))

    validate = json.loads(run_cli("validate", "world", path, "--edit", "--json").stdout)
    assert validate["valid"] is True, "有图不是错,这条只该是警告"
    check = json.loads(run_cli("world", "check", path, "--edit", "--json").stdout)
    assert check["loadable"] is True

    for label, warnings in (("validate", validate["warnings"]), ("check", check["warnings"])):
        said = [w for w in warnings if "wharf" in w]
        assert said, f"{label} --edit 没说这次编辑的图装不进已有的地点"
        assert any("location set-image" in w for w in said), (
            f"{label} --edit 说了装不进去,却没说补图该走哪扇门"
        )

    # 不是每次 `--edit` 都喊 —— 一句总在响的警告等于没有警告。
    bare = _write(tmp_path, _seed(), "bare.cyberworld")
    quiet = json.loads(run_cli("validate", "world", bare, "--edit", "--json").stdout)
    assert not [w for w in quiet["warnings"] if "wharf" in w]


# --------------------------------------------------- 写门(3.4.0):跑着的世界


def _running_world(tmp_path, **images):
    """一个**已经跑过**的世界:装完文件、走过几 tick、再重开一次(不带文件)。

    "重开一次"不是装饰 —— 写门要修的病恰恰只在**第二次以后的开机**上出现:
    第一次开机手里还捏着世界文件,作者层照常编译,图正常落地。
    """
    path = _write(tmp_path, _seed(**images))
    world = open_world_at(tmp_path / "w.db", world_file=path, force_mock_llm=True)
    world.tick(2)
    world.close()
    return open_world_at(tmp_path / "w.db", force_mock_llm=True)


def test_写门够得着一个已经在册的地点(tmp_path):
    """**这一条是这扇门存在的理由。**

    作者层是"只填缺、不覆盖",而合并粒度是**整个地点行**:一个地点只要已经在
    世界里,拿一份补了图的世界文件装回去那两格一个都装不进去 —— 作者写得进、
    `validate world` 放行、包也导得出,**就是到不了玩家眼前**,而全程零报错。
    角色卡那一次的形状逐字重演。

    钉的是链的出口(`state()`),不是中间那个 store。
    """
    world = _running_world(tmp_path)
    try:
        assert _rows(world)["wharf"]["map_image"] is None      # 先确认它本来没有
        receipt = world.set_location_image("wharf", {"map_image": _MAP})
        assert receipt["changed"] is True
        assert receipt["before"]["map_image"] is None
        assert receipt["after"]["map_image"] == _MAP
        rows = _rows(world)
        assert rows["wharf"]["map_image"] == _MAP
        assert rows["light"]["map_image"] is None              # 别人一个字没被动
    finally:
        world.close()


def test_写门是覆盖_和作者层合并有意相反(tmp_path):
    """两条语义相反是对的,**别把其中一条"修"成另一条**。

    作者层那一条手里捏着一份文件(缺省还是内置橱窗),拿它覆盖等于把这个世界的
    现在倒带回创世那一刻;这一条是一个人指名道姓说"这个地方的图换成这张",
    只填缺的话一张已经写着旧 URL 的图**永远**换不掉 —— 而换图正是这扇门的日常
    用法(图床是内容寻址的,换一张图就是换一条 URL)。
    """
    world = _running_world(tmp_path, map_image=_MAP)
    try:
        assert _rows(world)["wharf"]["map_image"] == _MAP
        world.set_location_image("wharf", {"map_image": "https://img.example.com/t/新.png"})
        assert _rows(world)["wharf"]["map_image"] == "https://img.example.com/t/新.png"
    finally:
        world.close()


def test_两格分开合并_只给一格不许抹掉另一格(tmp_path):
    """只给 `map_image` 不许把作者写了几周的 `scene_image` 顺手抹掉。

    抹掉在这条路上不会报错,只会让玩家走进那个地点时看到一片空白。
    """
    world = _running_world(tmp_path, map_image=_MAP, scene_image=_SCENE)
    try:
        world.set_location_image("wharf", {"map_image": "https://img.example.com/t/新.png"})
        row = _rows(world)["wharf"]
        assert row["map_image"] == "https://img.example.com/t/新.png"
        assert row["scene_image"] == _SCENE, "另一格被顺手抹掉了"
    finally:
        world.close()


def test_空串抹一格_clear抹两格(tmp_path):
    """两种"抹掉"是两件事,所以是两个开关(和 `set_card` 的约定逐字相同)。"""
    world = _running_world(tmp_path, map_image=_MAP, scene_image=_SCENE)
    try:
        world.set_location_image("wharf", {"map_image": ""})
        row = _rows(world)["wharf"]
        assert row["map_image"] is None and row["scene_image"] == _SCENE

        receipt = world.set_location_image("wharf", clear=True)
        assert receipt["cleared"] is True
        row = _rows(world)["wharf"]
        assert row["map_image"] is None and row["scene_image"] is None
        # 抹掉的是图,不是这个地点:名字、描述、几何一个字都不许动。
        assert row["name"] == "码头" and row["description"] == "海风很大"
        assert row["x"] == 0.3 and row["y"] == 0.4
    finally:
        world.close()


def test_逐字相同就一个字都不写(tmp_path):
    """一次什么也没改的"成功"读起来和改成功了一模一样。

    而这条命令最常见的用法就是运维照着一张单子一个一个敲过去 —— 敲重了得看得出来。
    """
    world = _running_world(tmp_path, map_image=_MAP)
    try:
        assert world.set_location_image("wharf", {"map_image": _MAP})["changed"] is False
        assert world.set_location_image("wharf", clear=True)["changed"] is True
        assert world.set_location_image("wharf", clear=True)["changed"] is False
    finally:
        world.close()


def test_这扇门只写两格图_别的键当场拒绝(tmp_path):
    """和角色卡"不认识的键原样带过去"**有意不一样**。

    那一格是作者写给玩家看的一张卡,创作台预告过第四样(声线 / 主题色);地点行
    的其余部分(名字、描述、几何)只有一个合法的写入者 —— 作者层。在这里开第二个,
    就是让"这张地图为什么变成这样"多出一个日志之外的答案。
    """
    world = _running_world(tmp_path)
    try:
        with pytest.raises(ValueError) as caught:
            world.set_location_image("wharf", {"name": "换个名字"})
        assert "name" in str(caught.value)
        assert _rows(world)["wharf"]["name"] == "码头", "拒绝了还改了一半"
    finally:
        world.close()


def test_写门不发事件_地图是配置不是历史(tmp_path):
    """`set_card` 发事件而这扇门不发,**这条差别是有意的**。

    角色卡住在 `agent_join.payload.spec` 上 —— 它的家本来就是事件日志;地图不是,
    `locations` 表是它唯一的权威(`projection.py` 为此退役了 `location_desc_update`)。
    在这里再发一条事件,"这个地点的图是什么"就有了两个答案,而分叉的那天不报错。
    """
    world = _running_world(tmp_path)
    try:
        before = len(world.events())
        world.set_location_image("wharf", {"map_image": _MAP, "scene_image": _SCENE})
        assert len(world.events()) == before, "地图是配置,不该往历史里加一条"
        assert _rows(world)["wharf"]["map_image"] == _MAP
    finally:
        world.close()


def test_写门写的图_重启之后还在(tmp_path):
    """写进 Redis 的行才算写进世界 —— 只改进程内存的话下一次开机全没。"""
    world = _running_world(tmp_path)
    try:
        world.set_location_image("wharf", {"scene_image": _SCENE})
    finally:
        world.close()

    again = open_world_at(tmp_path / "w.db", force_mock_llm=True)
    try:
        assert _rows(again)["wharf"]["scene_image"] == _SCENE
    finally:
        again.close()


# --------------------------------------------------------- CLI 出口(真路径)


def test_写门命令走真CLI_从state读得回来(tmp_path):
    """**走真 CLI,再用真读出口读回来。**

    只调 `World.set_location_image` 的测试验不出这一节:角色卡那个 bug 恰恰是
    因为测试都直接调内部函数、没走真路才漏掉的。生产上这条路的入口是运维台的
    一次性容器,argv 由具名参数白名单生成 —— 这里跑的就是它跑的那条。
    """
    _running_world(tmp_path).close()

    done = run_cli("location", "set-image", "--world-id", "w", "--location", "wharf",
                   "--map-image", _MAP, "--scene-image", _SCENE, "--json")
    assert done.returncode == 0, done.stderr
    receipt = json.loads(done.stdout)
    assert receipt["operation"] == "location set-image"
    assert receipt["changed"] is True
    assert receipt["after"] == {"map_image": _MAP, "scene_image": _SCENE}

    # **真的读得出新值** —— 一个进得去出不来的字段和没有这个字段是同一个 bug。
    places = {p["id"]: p for p in json.loads(
        run_cli("map", "--world-id", "w", "--json").stdout)["places"]}
    assert places["wharf"]["map_image"] == _MAP
    assert places["wharf"]["scene_image"] == _SCENE
    # 契约自报的读出口就是 `map`,这里顺手把那句话敲一遍。
    assert json.loads(run_cli("contract", "--json").stdout)["seed"][
        "location_image_read_command"] == "map"


def test_写门命令对不存在的地点退非零_并说得出这里有哪些地点(tmp_path):
    """**这正是上一单抓到的那个形状:退出码 0 而事没做。**

    ⚠️ 断言要先验证**地点存在时退 0** —— argparse 对一个根本没注册的子命令用的
    也是退出码 2,于是光看码的话,这条测试在"这道命令不存在"的世界里也是绿的,
    而它本该是这一整条链上最先红的一条。
    """
    _running_world(tmp_path).close()

    ok = run_cli("location", "set-image", "--world-id", "w", "--location", "wharf",
                 "--map-image", _MAP, "--json")
    assert ok.returncode == 0, ok.stderr          # 先钉住"这道命令真的存在"

    missing = run_cli("location", "set-image", "--world-id", "w",
                      "--location", "no-such-place", "--map-image", _MAP, "--json")
    assert missing.returncode == 2
    assert "usage:" not in missing.stderr         # argparse 的拒绝不算数
    assert "no-such-place" in missing.stderr
    assert "wharf" in missing.stderr and "light" in missing.stderr
    assert missing.stdout == ""                   # 编一个回执 = 运维以为改成功了


def test_写门命令的坏URI由原来那道闸拦下_而且一个字都不写(tmp_path):
    """闸只有一份(`media.media_uri_errors`)—— 另写一套的话同一条 URI 在开机时
    和这条路上会得到两个答案。"""
    _running_world(tmp_path, map_image=_MAP).close()

    def attempt(*extra):
        return run_cli("location", "set-image", "--world-id", "w",
                       "--location", "wharf", *extra, "--json")

    relative = attempt("--map-image", "images/wharf.png")
    assert relative.returncode == 2 and "usage:" not in relative.stderr
    assert "绝对 URI" in relative.stderr

    too_big = attempt("--scene-image", _inline(LOCATION_IMAGE_MAX_BYTES))
    assert too_big.returncode == 2
    assert str(LOCATION_IMAGE_MAX_BYTES) in too_big.stderr

    # 拒绝了还改了一半,是这一类命令最坏的收场。
    rows = json.loads(run_cli("map", "--world-id", "w", "--json").stdout)["places"]
    wharf = {p["id"]: p for p in rows}["wharf"]
    assert wharf["map_image"] == _MAP
    assert "scene_image" not in wharf


def test_写门命令的几种参数拒绝_每一种都说得出为什么(tmp_path):
    """互相矛盾的两句话,引擎挑哪句都是猜;而"什么都没给"的成功读起来像成功。"""
    _running_world(tmp_path, map_image=_MAP).close()

    def attempt(*extra):
        return run_cli("location", "set-image", "--world-id", "w",
                       "--location", "wharf", *extra, "--json")

    both = attempt("--clear", "--map-image", _MAP)
    assert both.returncode == 2 and "usage:" not in both.stderr and both.stdout == ""

    nothing = attempt()
    assert nothing.returncode == 2 and "usage:" not in nothing.stderr

    # 正对照:同一道命令参数给对时真的退 0(否则上面两条在"命令不存在"时也绿)。
    good = attempt("--scene-image", _SCENE)
    assert good.returncode == 0, good.stderr

    still = {p["id"]: p for p in json.loads(
        run_cli("map", "--world-id", "w", "--json").stdout)["places"]}["wharf"]
    assert still["map_image"] == _MAP


def test_写门命令的dry_run一个字节都不写(tmp_path):
    """`--dry-run` 动的是作者写的东西 —— 先看一眼是这类命令的用法,不是客套。"""
    _running_world(tmp_path).close()

    done = run_cli("location", "set-image", "--world-id", "w", "--location", "wharf",
                   "--map-image", _MAP, "--dry-run", "--json")
    assert done.returncode == 0, done.stderr
    receipt = json.loads(done.stdout)
    assert receipt["dry_run"] is True and receipt["after"]["map_image"] == _MAP

    places = {p["id"]: p for p in json.loads(
        run_cli("map", "--world-id", "w", "--json").stdout)["places"]}
    assert "map_image" not in places["wharf"]


def test_图走文件进来_argv装不下的那一段(tmp_path):
    """契约公布每格 256 KiB,而 `--map-image` 这扇门到 128 KiB 就没了。

    Linux 的 `MAX_ARG_STRLEN` 把单个 argv 元素封在 128 KiB —— 报错的是**操作
    系统**,引擎连被叫起来的机会都没有(壳给 rc 126「参数列表过长」)。和立绘
    那一次逐字相同的坎,而这里更近:上限本身就比 argv 那道坎大一倍。

    这里用 150 KiB 原图(URI 约 200 KiB)—— **刚好在那道坎的另一边、又在 256 KiB
    以内**:写成 1 KiB 的话这条测试永远绿,而它要防的那件事从来没被验到。
    """
    _running_world(tmp_path).close()

    uri = _inline(150_000)
    assert 128 * 1024 < len(uri) <= LOCATION_IMAGE_MAX_BYTES, "没跨过那道坎就什么也没验"
    path = tmp_path / "scene.uri"
    path.write_text(uri + "\n", encoding="utf-8")   # 尾巴上的换行是必然的,得吃掉

    done = run_cli("location", "set-image", "--world-id", "w", "--location", "wharf",
                   "--scene-image-file", str(path), "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["after"]["scene_image"] == uri

    # `-` = 标准输入:运维台那个一次性容器里连一个能写的文件都不一定有。
    piped = run_cli("location", "set-image", "--world-id", "w", "--location", "light",
                    "--map-image-file", "-", "--json", input=_MAP)
    assert piped.returncode == 0, piped.stderr
    assert json.loads(piped.stdout)["after"]["map_image"] == _MAP


def test_两格都从标准输入读要当场拒绝(tmp_path):
    """标准输入只有一份 —— 第二格会读到空,而空在这条路上的意思是"抹掉这一格"。

    也就是说一次手滑会**安静地删掉线上那张图**,而回执上写着"改了"。
    """
    _running_world(tmp_path, map_image=_MAP, scene_image=_SCENE).close()

    clash = run_cli("location", "set-image", "--world-id", "w", "--location", "wharf",
                    "--map-image-file", "-", "--scene-image-file", "-", "--json",
                    input=_MAP)
    assert clash.returncode == 2 and "usage:" not in clash.stderr
    assert clash.stdout == ""

    still = {p["id"]: p for p in json.loads(
        run_cli("map", "--world-id", "w", "--json").stdout)["places"]}["wharf"]
    assert still["map_image"] == _MAP and still["scene_image"] == _SCENE


def test_写命令不许创建世界(tmp_path):
    """抄错 world_id 会当场创世,而回执看上去是成功的(5ce6aed 的教训)。"""
    _running_world(tmp_path).close()
    assert run_cli("location", "set-image", "--world-id", "w", "--location", "wharf",
                   "--map-image", _MAP, "--json").returncode == 0

    missing = run_cli("location", "set-image", "--world-id", "no-such-world",
                      "--location", "wharf", "--map-image", _MAP, "--json")
    assert missing.returncode == 2
    assert "no-such-world" in missing.stderr
    assert "usage:" not in missing.stderr


# ------------------------------------- 「别动」与「抹掉」是两句话(验收挑出来的)


def test_别动这一格和抹掉这一格_四扇门同一个答案(tmp_path):
    """**代价不对称,所以第三类是拒绝而不是"抹掉"。**

    运维台/流水线的模板里一个没展开的变量,走 argv 进来长得就是 `'   '`;
    `None` 在 Python 宿主那儿最常见的来源是 `row.get("img")` 没取到值。把它们
    读成"抹掉"就是一次**静默删图**,而回执上写着"改了"、退出码 0 —— 而拒一次的
    代价只是调用方补一个字。"别动这一格"已经有写法了(**不给这个键**),
    所以 `None` 不必再承担第二种含义。

    头一版这两侧是**不一致**的:argv 那扇门把纯空白掐成空、当成抹掉(rc 0),
    而同样的内容走 `--*-file` 却是 rc 2。同一个输入两个答案,而错的那一半不报错。
    """
    world = _running_world(tmp_path, map_image=_MAP, scene_image=_SCENE)
    try:
        # ① 不给这个键 = 别动这一格(另一格照改)
        world.set_location_image("wharf", {"scene_image": "https://img.example.com/新.jpg"})
        assert _rows(world)["wharf"]["map_image"] == _MAP

        # ② 明写空串 = 抹掉这一格
        world.set_location_image("wharf", {"map_image": ""})
        assert _rows(world)["wharf"]["map_image"] is None

        # ③ None 与纯空白都是拒绝,而且**一个字都不写**
        for bad in (None, "   ", "\t\n"):
            with pytest.raises(ValueError):
                world.set_location_image("wharf", {"scene_image": bad})
            assert _rows(world)["wharf"]["scene_image"] == "https://img.example.com/新.jpg"
    finally:
        world.close()


def test_纯空白走argv和走文件_退出码一样(tmp_path):
    """两扇门给同一个答案 —— 判断在 `World` 上,不在 CLI 上。"""
    _running_world(tmp_path, map_image=_MAP).close()

    blank_file = tmp_path / "blank.uri"
    blank_file.write_text("   \n", encoding="utf-8")

    argv = run_cli("location", "set-image", "--world-id", "w", "--location", "wharf",
                   "--map-image", "   ", "--json")
    from_file = run_cli("location", "set-image", "--world-id", "w", "--location", "wharf",
                        "--map-image-file", str(blank_file), "--json")
    assert argv.returncode == from_file.returncode == 2
    for done in (argv, from_file):
        assert "usage:" not in done.stderr and done.stdout == ""

    # **线上那张图还在** —— 这条测试要防的就是它被安静地删掉。
    still = {p["id"]: p for p in json.loads(
        run_cli("map", "--world-id", "w", "--json").stdout)["places"]}["wharf"]
    assert still["map_image"] == _MAP

    # 正对照:明写空串**才是**抹掉,而且照旧 rc 0。
    wipe = run_cli("location", "set-image", "--world-id", "w", "--location", "wharf",
                   "--map-image", "", "--json")
    assert wipe.returncode == 0, wipe.stderr
    assert json.loads(wipe.stdout)["after"]["map_image"] is None


def test_换图再装回去_也要点名(tmp_path, caplog):
    """**"按语义本该如此"和"他要的事没发生"是两回事。**

    作者把世界文件里的那条 URL 换成新的一条再装回去 —— 两边都有图、值不同,
    上一版这条路**一个字都不说**、退出码 0。而换图恰恰是最常见的那种编辑:
    图床是内容寻址的,换一张图就是换一条 URL。
    """
    first = _write(tmp_path, _seed(map_image=_MAP), "v1.cyberworld")
    open_world_at(tmp_path / "w.db", world_file=first, force_mock_llm=True).close()

    swapped = _write(tmp_path, _seed(map_image="https://img.example.com/t/新.png"),
                     "v2.cyberworld")
    with caplog.at_level("WARNING"):
        world = open_world_at(tmp_path / "w.db", world_file=swapped, force_mock_llm=True)
    try:
        assert _rows(world)["wharf"]["map_image"] == _MAP, "整行合并会把世界倒带"
    finally:
        world.close()

    said = [r.getMessage() for r in caplog.records if "wharf" in r.getMessage()]
    assert said, "换图没生效,却一声不吭"
    assert any("map_image" in m and "location set-image" in m for m in said)


def test_两边一模一样时闭嘴(tmp_path, caplog):
    """一句总在响的警告等于没有警告 —— 装一份和世界逐字相同的文件时不该说话。"""
    path = _write(tmp_path, _seed(map_image=_MAP))
    open_world_at(tmp_path / "w.db", world_file=path, force_mock_llm=True).close()

    with caplog.at_level("WARNING"):
        world = open_world_at(tmp_path / "w.db", world_file=path, force_mock_llm=True)
    world.close()
    assert not [r for r in caplog.records if "map_image" in r.getMessage()]
