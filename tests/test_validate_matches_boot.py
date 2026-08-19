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
