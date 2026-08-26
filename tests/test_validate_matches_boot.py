"""同一份 `.cyberworld`,`validate world` / `world check` / 真开机必须给出**同一个答案**。

**为什么值得单独一个文件。** 2026-08-19 舰队上一个世界开不了机:容器 0.57 秒后
退出 1,`stocks[8] 'values' must be an object` —— 一份 2.3.0 时代导入的世界,作者层
是今天的引擎不再接受的形状。运维台想在**离线**问一句"这份文件这一版引擎装得进去
吗",手上的两条路都答错了:`world inspect` 只读封皮(答 `runnable: true`,它读的是
`engine_min`,而这份文件的 `engine_min` 写得没错——它只是老),`validate world` 答得
出坏,但它对**另外两种**文件的答案和开机相反:

- 一个跑过的世界导出来(只有状态记录),开机欢迎它,`validate world` 说
  `'agents' must be a list (missing)`;
- 一份只带 `kinds` 的编辑文件,装进一个已有世界开机是允许的(那就是"编辑"),
  `validate world` 照旧要求它把名册和地图再抄一遍。

三种文件里两种答反了的校验器,比没有校验器更坏:它教会使用者不信它。

**所以这里钉的不是某一条规则,是"几条路的答案相等"这件事本身。** 判断只有一份
(`authored_layer_errors` + `_precheck_ontology`),而"只有一份"这句话必须有人验 ——
在任何一侧多写一条闸、或者少写一条,下面就红。

三条路:`validate world`(作者的门,退出码 = 我的世界过没过)、`world check`
(宿主的门,退出码 = 这句话我答没答上来)、`World.open(world_file=…)`(真开机)。
前两条**只在退出码的含义上不同**,判断是同一份 —— 为什么要两个门,写在
`__main__.run_world_check` 上。

⚠️ 断言写成两半是有意的:先断"两边一致",再断"一致到哪个答案上"。只断前一半的话,
两边**一起**判错还是绿的。
"""
from __future__ import annotations

import gzip
import json

import pytest

from _worldfile import run_cli

pytest.importorskip("fakeredis")


def _write(path, rows) -> str:
    """把几条记录写成一个世界文件(裸 JSONL 也读得动,这里照真格式 gzip)。"""
    with gzip.GzipFile(path, "wb", mtime=0) as fh:
        fh.write(
            ("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n").encode()
        )
    return str(path)


_MANIFEST = {
    "kind": "manifest", "version": 3, "world_id": "t", "name": "对账用",
    "engine_min": "3.3.0",
}
_YARD = {"kind": "author", "type": "location",
         "body": {"id": "yard", "name": "院子", "description": "有一棵树"}}
_JIA = {"kind": "author", "type": "agent",
        "body": {"id": "甲", "name": "甲", "location": "yard", "personality": "温和"}}
_OAK = {"kind": "author", "type": "entity",
        "body": {"id": "tree:oak", "name": "橡树", "location": "yard"}}


def _tree_kind(**overrides) -> dict:
    body = {
        "id": "tree",
        "quantities": {"树高": {"default": 1.0, "visibility": "here"}},
        "affordances": {"照料": {"when": ["树高 < 10"], "set": {"树高": "树高 + 0.1"}}},
    }
    body.update(overrides)
    return {"kind": "author", "type": "kind", "body": body}


# ── 两条路各自的答案 ────────────────────────────────────────────────────────

def _validate_says(path: str, *, edit: bool = False) -> tuple[bool, list[str]]:
    """`anima-world validate world <path> [--edit] --json` 说行不行。"""
    args = ["validate", "world", path, "--json"]
    if edit:
        args.append("--edit")
    done = run_cli(*args)
    payload = json.loads(done.stdout)
    assert done.returncode == (0 if payload["valid"] else 2), (
        f"退出码和 JSON 里的 valid 对不上:{done.returncode} vs {payload['valid']} —— "
        "脚本读退出码、人读 JSON,两个说法不一致等于两种用法各信各的"
    )
    return bool(payload["valid"]), list(payload["errors"])


def _check_says(path: str, *, edit: bool = False) -> tuple[bool, list[str]]:
    """`anima-world world check <path> [--edit] --json` 说装不装得进。

    退出码语义和 `validate world` **有意不同**:0 = 这句话答上来了(`loadable`
    才是答案),非零 = 没答上来。理由写在 `run_world_check` 上。
    """
    args = ["world", "check", path, "--json"]
    if edit:
        args.append("--edit")
    done = run_cli(*args)
    payload = json.loads(done.stdout)
    assert done.returncode == 0, (
        f"一份读得了的文件,`world check` 必须答上来(退出码 0),实得 "
        f"{done.returncode} —— 非零的意思是'我没答上来',把它和'这个世界装不进去'"
        f"报成同一个数,调用方只能去猜"
    )
    return bool(payload["loadable"]), list(payload["errors"])


def _boot_says(path: str, redis, world_id: str) -> tuple[bool, list[str]]:
    """真的拿它开一次世界,说行不行。"""
    from anima_world.api import World

    try:
        world = World.open(
            world_id, redis=redis, world_file=path, force_mock_llm=True
        )
    except Exception as exc:  # noqa: BLE001 —— 坏文件的抛法有好几种,这里只问"行不行"
        return False, list(getattr(exc, "errors", None) or [str(exc)])
    try:
        return True, []
    finally:
        world.close()


def _both(path, fresh_redis, world_id="w", *, edit=False) -> tuple[bool, bool, list[str]]:
    """**三条路一起问,答案必须逐个相等。**

    `world check` 也在这儿,而它和 `validate world` 只在退出码的**含义**上不同 ——
    判断是同一份。两个命令答出不同的 `loadable`/`valid`,就是判断长出第二份的那天。
    """
    ok_validate, errors = _validate_says(path, edit=edit)
    ok_check, check_errors = _check_says(path, edit=edit)
    ok_boot, boot_errors = _boot_says(path, fresh_redis, world_id)
    assert ok_validate == ok_boot, (
        f"同一份文件,校验器说 {'行' if ok_validate else '不行'}、开机说 "
        f"{'行' if ok_boot else '不行'} —— 校验器:{errors};开机:{boot_errors}"
    )
    assert ok_check == ok_boot and check_errors == errors, (
        f"`world check` 和另外两条给了不同的答案 —— check:{check_errors};"
        f"validate:{errors};开机:{boot_errors}"
    )
    return ok_validate, ok_boot, errors


# ── 一、写对的世界:两条路都放行 ─────────────────────────────────────────────

def test_写对的世界_校验器和开机都放行(tmp_path, fresh_redis):
    path = _write(tmp_path / "good.cyberworld",
                  [_MANIFEST, _YARD, _JIA, _tree_kind(), _OAK])
    ok, _, errors = _both(path, fresh_redis)
    assert ok, f"一个写对的世界被拦下来了:{errors}"


def test_内置橱窗_校验器和开机都放行(tmp_path, fresh_redis):
    """橱窗是这个包唯一随 wheel 发出去的世界 —— 它先得过自己的闸。"""
    from importlib import resources

    path = str(resources.files("anima_world") / "demo.cyberworld")
    ok, _, errors = _both(path, fresh_redis)
    assert ok, f"内置橱窗自己过不了校验:{errors}"


# ── 二、舰队上那一份:两条路都拦 ─────────────────────────────────────────────

def test_老格式的货架_校验器和开机都拦(tmp_path, fresh_redis):
    """2026-08-19 灯塔湾开不了机的那个形状(`stocks` 条目不是今天的样子)。

    这一条是整个文件的由来:运维台要的就是"**离线**问得出这个答案"。
    """
    stale = {"kind": "author", "type": "stock",
             "body": {"owner": "tree:oak", "key": "树高", "value": 3.0}}
    path = _write(tmp_path / "legacy.cyberworld", [_MANIFEST, _YARD, _JIA, stale])
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, "一份今天的引擎装不进去的作者层,校验器必须当场说不行"
    assert any("stocks" in e for e in errors), errors


def test_乱序的分档_校验器和开机都拦(tmp_path, fresh_redis):
    """阈值不升序 = 前一档是死档:作者以为世界里有三种雨,其实只有两种。"""
    bad = {"kind": "author", "type": "visibility",
           "body": {"kind": "tree", "key": "树高", "visible": "here",
                    "bands": [[0, "小树"], [5, "半人高"], [3, "参天"]]}}
    path = _write(tmp_path / "band.cyberworld",
                  [_MANIFEST, _YARD, _JIA, _tree_kind(), _OAK, bad])
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, "写错的分档必须两条路一起拦 —— 放行的样子是她安静地报出一个不该知道的档"
    assert any("升序" in e for e in errors), errors


def test_坏角色卡_校验器和开机都拦(tmp_path, fresh_redis):
    """卡是分发物里给玩家看的那一面:相对路径的立绘发出去就是一张断图。"""
    carded = dict(_JIA["body"], card={"portrait": "./头像.png"})
    path = _write(tmp_path / "card.cyberworld", [
        _MANIFEST, _YARD, {"kind": "author", "type": "agent", "body": carded},
    ])
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, "一张带相对路径立绘的卡必须两条路一起拦"
    assert any("portrait" in e or "立绘" in e or "头像" in e for e in errors), errors


def test_坏地点图_校验器和开机都拦(tmp_path, fresh_redis):
    """地点的两格图和立绘走同一道闸 —— 所以它也得在这条"三路一致"的账上。

    新加一道闸最容易漏的正是这件事:闸挂在 `authored_layer_errors` 上就三条路
    都有,挂在开机那一侧就只有开机有,而校验器会替一份开不了机的文件发绿灯。
    """
    yard = dict(_YARD["body"], map_image="images/yard.png")
    path = _write(tmp_path / "locimg.cyberworld", [
        _MANIFEST, {"kind": "author", "type": "location", "body": yard}, _JIA,
    ])
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, "一张相对路径的地点图必须三条路一起拦"
    assert any("map_image" in e for e in errors), errors


# ── 三、本体那一摞闸:校验器从前一条都跑不到 ─────────────────────────────────

def test_量名拼错_校验器和开机都拦(tmp_path, fresh_redis):
    """`树髙` vs `树高`。放行的样子是安静地建成第二个量,而作者三个月后才知道。"""
    typo = _tree_kind(affordances={
        "照料": {"when": ["树髙 < 10"], "set": {"树高": "树高 + 0.1"}}
    })
    path = _write(tmp_path / "typo.cyberworld",
                  [_MANIFEST, _YARD, _JIA, typo, _OAK])
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, "量名拼错开机是拦的,校验器也必须拦 —— 否则作者信了绿灯再去撞开机"
    assert any("树髙" in e for e in errors), errors


def test_生成不写代价_校验器和开机都拦(tmp_path, fresh_redis):
    """`spawn` 没有 `costs`/`consumes`/`duration`:一个免费长东西的世界会挤爆。"""
    free = _tree_kind(affordances={"育苗": {"spawn": {"kind": "tree", "name": "苗"}}})
    path = _write(tmp_path / "spawn.cyberworld",
                  [_MANIFEST, _YARD, _JIA, free, _OAK])
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, "声明了 spawn 就必须要代价 —— 这条闸两条路都要有"
    assert any("代价" in e or "spawn" in e for e in errors), errors


# ── 四、两条从前答反了的路 ──────────────────────────────────────────────────

def test_跑过的世界导出来_两条路都收(tmp_path, fresh_redis, open_world):
    """只有状态记录的文件:开机一直欢迎它,校验器从前说 `'agents' must be a list`。

    **作者层为空 = 没有种子,不是一个空种子。**
    """
    world = open_world("src")
    world.tick(2)
    out = tmp_path / "exported.cyberworld"
    world.export_snapshot(out, world_id="src", name="导出的")
    world.close()

    ok, _, errors = _both(str(out), fresh_redis, world_id="restored")
    assert ok, f"一个跑过的世界导出来的包被校验器判成非法:{errors}"

    _, payload = _validate_json(str(out))
    assert any("没有作者层" in w for w in payload["warnings"]), (
        "收下它可以,但要说出'这里没有可查的东西' —— 一个什么都没查的绿灯"
        "和一个查过的绿灯长得一样,那是最坏的一种"
    )


def test_一次编辑_两条路都收(tmp_path, fresh_redis, open_world):
    """只带 `kinds` 的文件装进一个**已有**的世界 = 一次编辑,不是一个残缺的世界。

    开机按目标世界空不空自己判;校验器手上没有目标世界,所以那一格由调用方说
    (`--edit`)。**不给 `--edit` 就该照旧要求一份完整的世界** —— 那一半也钉在这儿。
    """
    world = open_world("w", redis=fresh_redis)
    world.close()

    # 补一个这个世界**还没有**的种类。改一个它已有的种类是另一回事(合并之后引用
    # 得重新解一遍),而"补一层 kinds"正是这条路当初被开出来的那个用例。
    bench = {"kind": "author", "type": "kind", "body": {
        "id": "bench", "label": "长椅",
        "quantities": {"油漆": {"default": 1.0, "visibility": "here"}},
        "affordances": {"上漆": {"when": ["油漆 < 5"], "set": {"油漆": "油漆 + 1"}}},
    }}
    path = _write(tmp_path / "edit.cyberworld", [_MANIFEST, bench])
    ok_edit, errors = _validate_says(path, edit=True)
    ok_check, check_errors = _check_says(path, edit=True)
    ok_boot, boot_errors = _boot_says(path, fresh_redis, "w")
    assert ok_edit and ok_check and ok_boot, (
        f"一次编辑被拦下来了 —— 校验器:{errors};check:{check_errors};"
        f"开机:{boot_errors}"
    )

    ok_plain, plain_errors = _validate_says(path)
    assert not ok_plain, (
        "不给 --edit 时它是在问'这是不是一个完整的世界',而答案是不是 —— "
        "两种问题给同一个答案的话,--edit 这个参数就没有意义了"
    )
    assert any("agents" in e or "locations" in e for e in plain_errors), plain_errors
    assert _check_says(path)[0] is False, "`world check` 的 --edit 也要真的分两种问法"


@pytest.mark.parametrize("name,rows", [
    ("unknown-kind", [_MANIFEST, {"kind": "沙发", "body": {}}]),
    ("unknown-type", [_MANIFEST, {"kind": "author", "type": "沙发", "body": {"id": "x"}}]),
    ("flat-body", [_MANIFEST, {"kind": "author", "type": "agent", "id": "甲"}]),
])
def test_引擎读不懂的字节_是一个答案_不是一句我没答上来(tmp_path, fresh_redis, name, rows):
    """**打得开、但这个引擎读不懂 ≠ 我没答上来。**

    这三种文件开机**当场失败**(`WorldFileError`),`validate world` 也照实报错误、
    退出码 2。而 `world check` 一度把它们连同"路径打错"一起报成 `loadable: null` +
    退出码 1 —— 而这个出口自己写着"问不出来一律不拦"(运维台的能力探测纪律),
    于是**一份引擎明确拒收的文件会被下游放行**。

    ⚠️ 这正是这条命令要修的那种病的又一个版本:一个答案被当成另一个答案读,
    而两边都不报错。分界不在"读的时候出没出错",在**这个文件被看过没有**
    (`__main__._cannot_even_look`)。
    """
    path = _write(tmp_path / f"{name}.cyberworld", rows)
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, f"{name}:引擎读不懂的字节必须三条路一起拦 —— {errors}"

    done = run_cli("world", "check", path, "--json")
    payload = json.loads(done.stdout)
    assert payload["loadable"] is False and done.returncode == 0, (
        f"{name}:loadable={payload['loadable']} / exit={done.returncode} —— "
        "报成 null+1 等于说'我没答上来',而调用方对'问不出来'的做法是放行"
    )


def test_读不了的文件_check_说的是没答上来_不是装不进去(tmp_path):
    """**"跑不了"是一个答案,"我没答上来"是另一个。**

    路径打错的调用方不该收到一句关于世界的判决 —— 那个世界没有任何问题,而下游
    (运维台的导入把关、改钉体检)正是照这个答案下判断的。所以 `loadable` 在这里
    是 `null`,退出码非零,和 `loadable: false` + 退出码 0 分得清清楚楚。

    ⚠️ 上面那条钉的是**这一格不许收得太宽**:文件打得开就该给一个答案。两条一起
    才把这条界画完整。
    """
    done = run_cli("world", "check", str(tmp_path / "没有这个文件.cyberworld"), "--json")
    payload = json.loads(done.stdout)
    assert done.returncode != 0, "读不了文件是真正的错误,要用非零"
    assert payload["loadable"] is None, (
        f"loadable={payload['loadable']} —— 报成 false 等于说'这个世界装不进去',"
        "而根本没有一个世界被看过"
    )
    assert payload["errors"], "至少要说出为什么答不上来"


def _validate_json(path: str) -> tuple[int, dict]:
    done = run_cli("validate", "world", path, "--json")
    return done.returncode, json.loads(done.stdout)


# ── 五、两扇门在打架的那两格(看板 D29 同族,2026-08-21)────────────────────
#
# 这一节钉的都是**双向**的不一致:一格假红、一格假绿,而两边都不报错。
# 判据是同一条:**开机是权威**。比开机严 = 一份跑得好好的世界出不了包,而报错指着
# 一个不存在的问题;比开机松 = 绿灯放行,开机当场挂。两种都比"没有校验器"更坏,
# 因为它们教会使用者不信这扇门。


def test_一个kinds都没写的世界_校验器不许比开机更严(tmp_path, fresh_redis):
    """**声明本身就是开关。** 不写 `kinds` 的世界这一层整个缺席,`for_each.owner`
    是个普通的量 owner —— 开机那条路一直这么判
    (`if seed_author_layer and world_seed and world_seed.get("kinds")`)。

    而这扇门从前**无条件**跑本体预检,于是它把那个开关按住了:创作台内置示例作业
    产出的世界 `validate world` 退 2、`simulate --ticks 0` 退 0 **而且规律真的在跑**
    (600 tick 后煤量 100 → 94.24)。看板 D20 的标题「3.x 上一个都开不了机」因此是
    错的 —— 那份世界**开得起来**,坏的是**出不了包**(`export_package` 拿
    `validate world` 当闸)。**「开不了机」和「出不了包」得分开说。**
    """
    rule = {"kind": "author", "type": "rule", "body": {
        "id": "煤堆每日消耗", "every": {"ticks": 1},
        "for_each": {"owner": "物件:煤堆"},
        "set": {"煤量": "煤量 - 0.01"},
    }}
    stock = {"kind": "author", "type": "stock",
             "body": {"owner": "物件:煤堆", "values": {"煤量": 100.0}}}
    path = _write(tmp_path / "nokinds.cyberworld",
                  [_MANIFEST, _YARD, _JIA, stock, rule])
    ok_validate, ok_boot, errors = _both(path, fresh_redis)
    assert ok_boot is True, errors
    assert ok_validate is True, (
        "一个 kinds 都没写的世界被校验器拦下来了 —— 这是假红:开机收它", errors)


def test_量名拼错_校验器不许比开机更松(tmp_path, fresh_redis):
    """**假绿**,而假绿比假红更贵:一份 `validate world` 说 `valid` 的包,创世当场
    `OntologyError`。

    病根不在判断本身,在**判断跑的时机**:量名那道闸原本只住在**播种**里
    (`_seed_stocks`),而播种在 `_precheck_ontology` **之后** —— 于是它既漏出了这扇
    离线的门,又让开机在**写过几张表之后**才失败,留下一个装了一半的世界
    (正是 `_precheck_ontology` 当初被开出来要修的那个形状)。
    """
    kind = {"kind": "author", "type": "kind", "body": {
        "id": "物件", "gloss": "一样东西",
        "quantities": {"煤量": {"default": 100.0, "visibility": "here"}},
    }}
    entity = {"kind": "author", "type": "entity",
              "body": {"id": "物件:煤堆", "name": "煤堆", "location": "yard"}}
    stock = {"kind": "author", "type": "stock",
             "body": {"owner": "物件:煤堆", "values": {"煤亮": 100.0}}}
    path = _write(tmp_path / "typo.cyberworld",
                  [_MANIFEST, _YARD, _JIA, kind, entity, stock])
    ok_validate, ok_boot, errors = _both(path, fresh_redis)
    assert ok_boot is False and ok_validate is False, errors
    assert any("煤亮" in e and "煤量" in e for e in errors), errors

    # 对照组(期望非 0 的那一条判据的反面):把量名写对,三条路一起放行。
    stock_ok = {"kind": "author", "type": "stock",
                "body": {"owner": "物件:煤堆", "values": {"煤量": 100.0}}}
    good = _write(tmp_path / "typo_ok.cyberworld",
                  [_MANIFEST, _YARD, _JIA, kind, entity, stock_ok])
    assert _both(good, fresh_redis, world_id="w2")[0] is True


def test_量名拼错的世界_一个字都不许先写进去(tmp_path, fresh_redis):
    """上一条的另一半,而这一半只有真开机看得见:**验不过就一个字都不写**。

    从前那道闸在播种里,而播种之前地图、规律、物品已经落库了 —— 一份写错量名的
    文件因此留下一个**装了一半**的世界,而且那次失败让这个前缀不再是空的,
    于是作者改好文件再来一次走的已经不是创世那条路。
    """
    from anima_world.api import World

    kind = {"kind": "author", "type": "kind", "body": {
        "id": "物件", "gloss": "一样东西",
        "quantities": {"煤量": {"default": 100.0, "visibility": "here"}},
    }}
    entity = {"kind": "author", "type": "entity",
              "body": {"id": "物件:煤堆", "name": "煤堆", "location": "yard"}}
    stock = {"kind": "author", "type": "stock",
             "body": {"owner": "物件:煤堆", "values": {"煤亮": 100.0}}}
    path = _write(tmp_path / "half.cyberworld",
                  [_MANIFEST, _YARD, _JIA, kind, entity, stock])
    with pytest.raises(Exception):
        World.open("half", redis=fresh_redis, world_file=path,
                   force_mock_llm=True).close()
    left = [k for k in fresh_redis.scan_iter("anima:half:*")]
    assert left == [], f"失败的创世留下了 {sorted(left)} —— 一个装了一半的世界"


# ── 六、`--edit` 只豁免跨引用(看板 D29 本体)────────────────────────────────


def _edit_kind(**over):
    body = {"id": "物件", "gloss": "一样东西",
            "quantities": {"煤量": {"default": 100.0, "visibility": "here"}}}
    body.update(over)
    return {"kind": "author", "type": "kind", "body": body}


def test_编辑包里量名拼错_必须红(tmp_path):
    """**D29 本体,而"量名"是两支,这一条只钉 `set:` 那一支**(另一支在下一条)。

    `--edit` 从前整个跳过本体预检,而 `loadable` 就是 `not errors` —— 于是一份把
    量名拼错的编辑包拿到一句绿的 `loadable: true`,开机当场挂。

    🔬 **它不是"没说",是"说窄了",而说窄了比全不说更难逮**:那一支追加过一句
    warning「引用完整性没查:种类/地点/物品/规律可以来自目标世界」—— 那句话是真的,
    可它只解释得了被跳过的那一摞里的**最后一件**。
    **人会拿一条真的理由去覆盖整个遗漏。**

    ⚠️ **而这条用例自己也犯过同一个错**:它只钉了 `set:` 那一支,于是修完之后
    `stocks:` 那一支**照旧假绿**,178 个用例全绿而那一格坏着 —— 与此同时那句
    warning 正面写着"量名……已经查过了"。**一条只钉了一支的用例,和一句说得比
    做到的宽的话,合起来正好是一盏看不出来的假绿灯。**
    """
    bad = _edit_kind(affordances={"烧": {"set": {"煤亮": "煤亮 - 1"}}})
    path = _write(tmp_path / "edit_bad.cyberworld", [_MANIFEST, bad])
    ok, errors = _check_says(path, edit=True)
    assert ok is False, "一份开机必挂的编辑包拿到了绿灯"
    assert any("煤亮" in e for e in errors), errors
    assert _validate_says(path, edit=True)[0] is False, "两扇门必须同一个答案"

    # 对照组:同一份包,量名写对 —— 照旧放行(期望非 0 的那一条判据)。
    good = _edit_kind(affordances={"烧": {"set": {"煤量": "煤量 - 1"}}})
    ok2, errors2 = _check_says(_write(tmp_path / "edit_ok.cyberworld",
                                      [_MANIFEST, good]), edit=True)
    assert ok2 is True, errors2


def test_编辑包里stocks的量名拼错_必须红(tmp_path):
    """**"量名"的另一支:`stocks:` 里写初值的那个名字。**

    它和 `set:` 那一支是同一种错、同一个后果(`树髙` 安静地建成第二个量),判据也
    是同一份(`_undeclared_stock_names`,预检与播种共用)—— 只是修 `--edit` 那一轮
    只接上了 `parse_kinds`,没接上这一个。于是同一份包:
    `world check --edit` 说 `loadable: true`,`validate world` 退 2,真当编辑合并
    进一个跑着的世界 —— **当场 `OntologyError`**。
    """
    typo = {"kind": "author", "type": "stock",
            "body": {"owner": "物件:煤堆", "values": {"煤亮": 3.0}}}
    path = _write(tmp_path / "edit_stock_bad.cyberworld",
                  [_MANIFEST, _edit_kind(), typo])
    ok, errors = _check_says(path, edit=True)
    assert ok is False, "一份 stocks 量名拼错的编辑包拿到了绿灯"
    assert any("煤亮" in e for e in errors), errors
    assert _validate_says(path, edit=True)[0] is False, "两扇门必须同一个答案"

    # 对照组:同一份包,量名写对 —— 照旧放行。
    good = {"kind": "author", "type": "stock",
            "body": {"owner": "物件:煤堆", "values": {"煤量": 3.0}}}
    ok2, errors2 = _check_says(
        _write(tmp_path / "edit_stock_ok.cyberworld", [_MANIFEST, _edit_kind(), good]),
        edit=True)
    assert ok2 is True, errors2


def test_编辑包给一个没声明的种类写初值_不许假红_但要说出这一格没查(tmp_path):
    """和 `me_X` 那一格逐字同构,而且是同一个坑的另一半。

    一份编辑包完全可以只改种类 A、顺手给种类 B 的某个实例写个初值 —— B 的声明在
    目标世界里,硬查就是**假红**。所以这一行跳过,**而且说出来**:上面那条用例刚
    钉完"量名两支都查",要是这一格默默跳过,那句总结就又变成一句说得比做到的宽的话。
    """
    other = {"kind": "author", "type": "stock",
             "body": {"owner": "agent:甲", "values": {"体力": 50}}}
    path = _write(tmp_path / "edit_stock_xkind.cyberworld",
                  [_MANIFEST, _edit_kind(), other])
    ok, errors = _check_says(path, edit=True)
    assert ok is True, errors
    payload = json.loads(run_cli("world", "check", path, "--edit", "--json").stdout)
    assert any("agent:甲" in w and "离线答不了" in w for w in payload["warnings"]), (
        payload["warnings"])


def test_编辑包里spawn没写代价_必须红(tmp_path):
    """四件里的另一件。**代价由作者写,不是引擎发配额** —— 而这一条和目标世界
    一个字关系都没有。"""
    bad = _edit_kind(affordances={"生": {"spawn": {"kind": "物件", "name": "新的"}}})
    path = _write(tmp_path / "edit_spawn.cyberworld", [_MANIFEST, bad])
    ok, errors = _check_says(path, edit=True)
    assert ok is False and any("代价" in e for e in errors), errors


def test_编辑包的跨引用照旧豁免(tmp_path):
    """豁免的是**查不动**的那一摞,不是"严的那一摞":规律指向哪个种类、实例在哪个
    地点、能力里的物品 —— 这几样可以来自目标世界,而目标世界不在手上。"""
    rule = {"kind": "author", "type": "rule", "body": {
        "id": "锈", "every": {"ticks": 1},
        "for_each": {"kind": "目标世界里的种类"},
        "set": {"锈度": "锈度 + 1"},
    }}
    path = _write(tmp_path / "edit_xref.cyberworld", [_MANIFEST, _edit_kind(), rule])
    ok, errors = _check_says(path, edit=True)
    assert ok is True, errors


def test_编辑包用了me_而没重声明agent_不许假红_但要说出这一格没查(tmp_path):
    """⚠️ **`me_X` 那一件原本被记成"在包自己肚子里",而它不是**(2026-08-21 实测)。

    它查的是 `agent` 种类声明过的量,而一份只改某个种类的编辑包完全可以不重声明
    `agent` —— 她的量表在目标世界里。硬查就是**假红**。
    **所以这一格跳过,而且说出来** —— 假装查过了正是 `--edit` 上一版那句 warning
    的病本身。
    """
    bad = _edit_kind(affordances={
        "烧": {"requires": ["me_体力 >= 10"], "set": {"煤量": "煤量 - 1"}}})
    path = _write(tmp_path / "edit_me.cyberworld", [_MANIFEST, bad])
    ok, errors = _check_says(path, edit=True)
    assert ok is True, errors
    payload = json.loads(run_cli("world", "check", path, "--edit", "--json").stdout)
    assert any("me_体力" in w and "离线答不了" in w for w in payload["warnings"]), (
        payload["warnings"])

    # 对照组:包里自带 `agent` 声明时,这一格**真的查** —— 拼错的 me_ 名字要红。
    actor = {"kind": "author", "type": "kind", "body": {
        "id": "agent", "quantities": {"体力": {"default": 100, "visibility": "self"}}}}
    typo = _edit_kind(affordances={
        "烧": {"requires": ["me_精力 >= 10"], "set": {"煤量": "煤量 - 1"}}})
    ok2, errors2 = _check_says(
        _write(tmp_path / "edit_me_typo.cyberworld", [_MANIFEST, actor, typo]),
        edit=True)
    assert ok2 is False and any("me_精力" in e for e in errors2), errors2


# ── 七、节拍(作者层的第十二个段,3.7.0)──────────────────────────────────────
#
# **新增一种开机失败,就必须同一轮里补进这两扇门。** 节拍 3.7.0 起进得了
# `.cyberworld`,而 `build_serve_scheduler` 在第一次写之前调 `BeatScript.from_data`
# —— 于是坏脚本当场开不了机。这一节钉的就是"三条路答案相等"这件事对新来的这一段
# 同样成立:第一版把段收进去了却没补这扇门,`world check` 对一份开不了机的文件
# 答 `loadable: true`(实测),而那正是上一节刚刚修掉的那种假绿。

_BEAT_BODY = {
    "id": "第一幕",
    "trigger": {"at": {"day": 0, "minute_of_day": 5}},
    "payload": [{"op": "memory", "agent_id": "甲", "summary": "那封没寄出去的信。"}],
}


def _beat(**overrides) -> dict:
    body = dict(_BEAT_BODY)
    body.update(overrides)
    return {"kind": "author", "type": "beat", "body": body}


def test_写对的节拍_三条路都放行(tmp_path, fresh_redis):
    """对照组(期望"绿"的那一条):没有它,下面几条对一个"节拍一律拦"的实现同样成立。"""
    path = _write(tmp_path / "beat_ok.cyberworld", [_MANIFEST, _YARD, _JIA, _beat()])
    ok, _, errors = _both(path, fresh_redis)
    assert ok, f"一份写对的剧情被拦下来了:{errors}"


def test_坏op的节拍_校验器和开机都拦(tmp_path, fresh_redis):
    """`op` 拼错:开机当场 `BeatScriptError`,而这两扇门从前说绿。"""
    path = _write(tmp_path / "beat_op.cyberworld", [
        _MANIFEST, _YARD, _JIA,
        _beat(payload=[{"op": "没这个 op", "agent_id": "甲"}]),
    ])
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, "一份开不了机的剧情,离线两扇门必须当场说不行"
    assert any("op" in e for e in errors), errors


def test_节拍指着一个不存在的拍_校验器和开机都拦(tmp_path, fresh_redis):
    """`after` 指着不存在的 id = 这一拍永远不会响,而且没有一处报错。

    ⚠️ 这一件**在包自己肚子里查得动**(节拍没有跨引用),所以它和量名拼错同一类,
    不属于 `--edit` 豁免的那一摞。
    """
    path = _write(tmp_path / "beat_after.cyberworld", [
        _MANIFEST, _YARD, _JIA,
        _beat(trigger={"after": "没有这一拍", "minutes": 5}),
    ])
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, "指着一个不存在的拍必须两条路一起拦"


def test_编辑包里的坏节拍_照样红(tmp_path):
    """`--edit` 豁免的是**跨引用**,不是"这是一次编辑就什么都不查"。

    节拍**没有**跨引用:`op`/`after`/id 重复全在包自己肚子里。所以这一格照查 ——
    否则一次编辑就能把一份开不了机的剧情送过绿灯。
    """
    path = _write(tmp_path / "beat_edit.cyberworld", [
        _MANIFEST, _beat(payload=[{"op": "没这个 op", "agent_id": "甲"}]),
    ])
    ok, errors = _check_says(path, edit=True)
    assert ok is False and any("op" in e for e in errors), errors

    # 对照组:同一次编辑里剧情写对了就放行 —— 上面那条不是"带节拍的编辑一律红"。
    ok2, errors2 = _check_says(
        _write(tmp_path / "beat_edit_ok.cyberworld", [_MANIFEST, _beat()]), edit=True)
    assert ok2 is True, errors2


# ── 八、撤掉的量:离线这两扇门答得比开机窄,而窄在哪必须说出来(3.8.0)──────────
#
# 开机能拿新声明去比**目标世界库里**留着什么(`dropped_quantities:` 那一行);
# 校验器手上没有那个世界,只比得了这份文件自己写下的东西。**说窄了不要紧,
# 假装查过了才要紧** —— 那正是 `--edit` 上一版那句 warning 的病。


def _warnings(path: str, *, edit: bool = False) -> tuple[list[str], list[str]]:
    """两扇门的 `warnings`,一起取回来 —— 这一族的判据是"两扇门说同一句话"。"""
    args = [path, "--json"] + (["--edit"] if edit else [])
    v = json.loads(run_cli("validate", "world", *args).stdout)
    c = json.loads(run_cli("world", "check", *args).stdout)
    return list(v["warnings"]), list(c["warnings"])


_VIS_STALE = {"kind": "author", "type": "visibility",
              "body": {"kind": "tree", "key": "生长速度", "visible": "here",
                       "label": "生长速度"}}


def test_同一份文件里的陈旧可见性行_两扇门都点名(tmp_path):
    """`kinds` 里划掉了 `生长速度`,而 `stock_visibility` 里那一行还留着 ——
    **那一行会照旧进提示词**,而这份文件在今天的两扇门上是全绿的。"""
    path = _write(tmp_path / "stale.cyberworld",
                  [_MANIFEST, _YARD, _JIA, _tree_kind(), _OAK, _VIS_STALE])
    assert _validate_says(path)[0] and _check_says(path)[0], "它不该是错误,只是警告"
    for label, said in zip(("validate world", "world check"), _warnings(path)):
        hit = [w for w in said if "dropped_quantities" in w]
        assert hit, f"{label} 对一行陈旧的可见性声明一个字都没说"
        assert 'dropped_quantities: {"tree": ["生长速度"]}' in hit[0], hit[0]


def test_没有陈旧行的文件_两扇门都不说这句(tmp_path):
    """**一句总在响的警告等于没有警告。**"""
    path = _write(tmp_path / "clean.cyberworld",
                  [_MANIFEST, _YARD, _JIA, _tree_kind(), _OAK])
    for label, said in zip(("validate world", "world check"), _warnings(path)):
        assert not [w for w in said if "dropped_quantities" in w], label


def test_编辑包里那一格离线答不了_两扇门明说而不是装作查过(tmp_path):
    """一次编辑包**通常只带那条重声明的 `kind`** —— 目标世界里被撤掉的是哪几个量,
    离线没有任何办法知道。`--edit` 那句总结逐字列着"包自己肚子里那几件已经查过了",
    而这一格不在里面:**一句说得比做到的宽的话,和一盏假绿灯是同一件事。**"""
    path = _write(tmp_path / "edit.cyberworld", [_MANIFEST, _tree_kind()])
    for label, said in zip(("validate world", "world check"),
                           _warnings(path, edit=True)):
        hit = [w for w in said if "重声明是整行替换" in w]
        assert hit, f"{label} --edit 没说「撤掉了哪些量」这一格离线答不了"
        assert "不裁剪" in hit[0] and "dropped_quantities" in hit[0], hit[0]
        assert "tree" in hit[0], f"{label} 说了有这一格,却没说是哪个种类"


def test_不带量的编辑包不说这句(tmp_path):
    """只改地点、只改规律的编辑包不该被喊 —— 它一个量都没重声明。"""
    path = _write(tmp_path / "loc.cyberworld", [_MANIFEST, _YARD])
    for label, said in zip(("validate world", "world check"),
                           _warnings(path, edit=True)):
        assert not [w for w in said if "重声明是整行替换" in w], label


# ── 九、通用的那一条:**新开一种开机失败,三扇门必须一起认**(3.8.0 第 1 期)────


def test_三扇门都走同一份判断_而检查器是一张看得见的表():
    """🔴 **这一条是通用的,上面那些是逐条枚举的。**

    这个仓库反复栽的那一跤:**新增一种开机失败,忘了把它接进离线那两扇门。**
    3.7.0 收节拍时第一版就这样(收了段没补门,于是 `world check` 对一份开不了机的
    文件照答 `loadable: true`),而同一版的上一个 commit 刚为同一种假绿修过三格。
    ⚠️ **上面那张逐条枚举的夹具不会替你发现这件事** —— 它是一条一条写的,
    你开一种新的开机失败,它一条都不红。

    所以这条**不枚举案例,枚举结构**,钉两件:

    1. 检查器是一张**看得见的表**(`AUTHORED_LAYER_CHECKS`),而不是一串写死的
       `+` —— 从前是后者,于是"忘了加一个"只表现为少了一行,没有一处会红。
    2. **三扇门都走 `authored_layer_errors`**:开机(`build_serve_scheduler`)、
       `validate world`、`world check`。只要这一条成立,往那张表里加一行就
       **自动**同时进三扇门 —— 那正是这条纪律想要的东西。
    """
    import ast
    import inspect

    from anima_world import __main__ as main_mod

    checks = [f.__name__ for f in main_mod.AUTHORED_LAYER_CHECKS]
    assert len(checks) >= 4, f"这张表自己空了:{checks}"
    assert "world_plugin_errors" in checks, "第 1 期新开的那种开机失败没进表"

    # `authored_layer_errors` 真的**遍历那张表**,而不是又写了一串 `+`。
    body = inspect.getsource(main_mod.authored_layer_errors)
    assert "AUTHORED_LAYER_CHECKS" in body, (
        "`authored_layer_errors` 没读那张表 —— 那张表就成了摆设,"
        "而摆设正是这条纪律要防的东西"
    )

    # 三扇门都走它。**按源码里出现过判**:这三个函数是那三扇门的入口。
    # ⚠️ 开机那扇门的入口不是 `build_serve_scheduler` 本身,是它调的
    # `_load_world_file` —— 那是"作者层第一次被读进来"的地方,闸就在那儿。
    for door in ("_load_world_file", "run_validate", "run_world_check"):
        source = inspect.getsource(getattr(main_mod, door))
        assert "authored_layer_errors" in source, (
            f"`{door}` 没走 `authored_layer_errors` —— 它会和另外两扇门分叉,"
            "而分叉那天两边都不报错"
        )
