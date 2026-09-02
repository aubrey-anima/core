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

import copy
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

    ⚠️ 2026-08-27(收件箱 D30)之后这条还钉一件**新的**:这一趟的绿灯不再是
    "什么都没查的绿灯" —— 状态层里开机会编译的那几张表已经查过了。
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
        "收下它可以,但要说出这份包是哪一种 —— 一个什么都没查的绿灯"
        "和一个查过的绿灯长得一样,那是最坏的一种"
    )
    # **而"没查"这件事从今天起必须是机器读得到的一格,不是一句 warning。**
    # 读它的是脚本,脚本读不到那句解释 —— D30 那个洞的全部形状就在这一句里。
    check = json.loads(run_cli("world", "check", str(out), "--json").stdout)
    assert check["checked_layers"] == ["redis"], check
    assert check["unchecked_layers"], (
        "一个跑过的世界导出来带着事件与转录,而这扇门查不动它们 —— "
        "查不动就要说出来,而且要说给机器听"
    )
    assert "kinds" not in check["unchecked_state_tables"], (
        "种类表是开机会编译的,它必须落在'查过'那一边 —— 落在这一格里就是 D30 复发"
    )


# ── 四之二、状态层那一族(收件箱 D30,2026-08-27)────────────────────────────
#
# 这一节和上面那些逐条枚举的用例是**同一件事的另一半**:上面查的是作者层,
# 而一个跑过的世界导出来**一条作者记录都没有**。这扇门从前对那种包什么都没看过,
# 却照答 `loadable: true`。


def _future_field_export(tmp_path, open_world, name="future"):
    """造一份"新引擎写下、老引擎读不懂"的导出包 —— D30 那个实测案例的形状。

    真实案例是 3.7.0 的 `importance` 随状态层进包、3.5.0 的解析器不认识它。
    这里没有第二支引擎可用,所以反过来造:往状态层的 `:kinds` 里塞一个**内置种类
    `agent` 不许声明**的字段。两者对这扇门是同一个形状 —— 状态层里躺着一个编译
    不过的声明,而作者层是空的。

    ⚠️ **说准这一格红在哪儿**(2026-08-27 A 视角验收挑出来的):它红的是
    「内置种类 `agent` 只能声明 quantities」**那条专门规则**,不是一条通用的
    "顶层不认识的字段"闸 —— 实测**普通种类**顶层多一个字段,`world check` 与真开机
    **都收**,两扇门并没有分叉。**这条夹具证的是"两扇门对同一份包说同一句话",
    不是"任何多余字段都会被逮住"**;把它读宽了,下一个人会以为有一道并不存在的闸。
    (A 明说:只改措辞,**别为它加闸** —— 加了就是比开机严 = 假红。)
    """
    import gzip

    world = open_world("src")
    world.tick(2)
    clean = tmp_path / f"{name}-clean.cyberworld"
    world.export_snapshot(clean, world_id="src", name="导出的")
    world.close()

    rows = []
    with gzip.open(clean, "rt", encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            if record.get("kind") == "checksum":
                continue           # 改了内容,校验和当然要重算 —— 这里索性不写
            if record.get("kind") == "redis" and record.get("key") == "kinds":
                for field in record["value"]:
                    row = json.loads(record["value"][field])
                    row["definition"]["未来字段"] = 0.6
                    record["value"][field] = json.dumps(row, ensure_ascii=False)
            rows.append(record)
    out = tmp_path / f"{name}.cyberworld"
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        for record in rows:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return clean, out


def test_状态层里读不懂的声明_三扇门说同一句话(tmp_path, fresh_redis, open_world):
    """🔴 **D30 本身**:`world check` 对一份**开不了机**的导出包答过绿灯。

    实测原文(FOR-STUDIO §3.30,2026-08-21,两支真 venv):3.7.0 导出的世界拿
    3.5.0 `world check` **说绿**、`import` **退 0**、**真开机退 1**
    (`OntologyError: 不认识的字段 ['importance']`)。三扇门,三句话,而下游
    (运维台判包、创作台出包前那道闸)读的正是最乐观的那一句。

    病灶不是"说窄了",是**没看**:这扇门当时只读作者层,而这种包一条作者记录都没有。
    所以修法也不是把话说宽 —— 是**真去查**状态层里开机会编译的那几张表,
    用的还是开机第一秒那个函数(`_precheck_ontology`)。

    这条钉的就是那三扇门的答案**相等**,而不是各说各的。
    """
    _, bad = _future_field_export(tmp_path, open_world)

    # 门一:离线那两扇(它们本来就被 `_both` 钉成相等)
    ok_check, check_errors = _check_says(str(bad))
    ok_validate, validate_errors = _validate_says(str(bad))
    # 门三:真开一次机
    ok_boot, boot_errors = _boot_says(str(bad), fresh_redis, "restored")

    assert not ok_boot, (
        "这份夹具本身失效了:它该是一份**开不了机**的包,而它开起来了 —— "
        "夹具不红的话,下面三条断言全是假绿")
    assert not ok_check, (
        f"🔴 D30 复发:一份开不了机的包,`world check` 答了绿灯。开机说:{boot_errors}")
    assert not ok_validate, f"🔴 `validate world` 也漏了同一份包:{validate_errors}"
    assert check_errors == validate_errors, (
        f"两扇离线门给了不同的话 —— check:{check_errors};validate:{validate_errors}")
    assert any("未来字段" in e for e in check_errors), check_errors
    # **同一句话,逐字相同** —— 因为它本来就是同一个函数吐出来的。
    # 两边各写一份判断的话,这一句是第一个会松掉的。
    assert set(check_errors) <= set(boot_errors), (
        f"离线门说的话不在开机说的话里面 —— 那就是第二份判断了。"
        f"离线:{check_errors};开机:{boot_errors}")


def test_状态层查过了这件事_是机器读得到的一格(tmp_path, fresh_redis, open_world):
    """D30 三条候选修法里,只加一格 `checked_layers` 那条**单独做是不够的** ——
    它只把那句 warning 翻译成机器读得懂的,那份包照样答绿。所以这一轮先真去查,
    再用这几格说清"查到哪儿为止"。

    这条钉的是**那几格自己**:纯增量、四格、消费方那条判据只有一行。
    """
    clean, bad = _future_field_export(tmp_path, open_world, name="cov")

    good = json.loads(run_cli("world", "check", str(clean), "--json").stdout)
    assert good["loadable"] is True
    assert good["present_layers"], good
    assert set(good["checked_layers"]) <= set(good["present_layers"]), good
    assert good["unchecked_layers"] == [
        layer for layer in good["present_layers"] if layer not in good["checked_layers"]
    ], "`unchecked` 必须真的是那个减法 —— 让消费方自己减就是让它们各持一份对层名的猜测"

    # 内置橱窗那份是纯作者层,它该是**满覆盖**的那一种:绿灯 + 没有没查过的层。
    from anima_world.__main__ import WORLD_FILE_PATH

    demo = json.loads(run_cli("world", "check", str(WORLD_FILE_PATH), "--json").stdout)
    assert demo["loadable"] is True and demo["unchecked_layers"] == [], demo
    assert demo["checked_layers"] == ["author"], demo

    # 而那份坏的:`loadable` 是 false,可覆盖那几格**照样要填** —— 一个答"不行"
    # 的回执如果不说自己看了哪儿,人只会重问一遍。
    broken = json.loads(run_cli("world", "check", str(bad), "--json").stdout)
    assert broken["loadable"] is False and broken["checked_layers"] == ["redis"], broken


def test_三扇门里的第二扇_import_对一份纯作者层的包不许报成成功(tmp_path, fresh_redis):
    """**收件箱 D32,和 D30 同族**:`world import` 那扇门。

    `world check` 与开机是两扇门,`world import` 是第三扇 —— 而它对一份**只有
    作者层**的包落 0 键、退 0、日志干净:世界仍是空的,首启装的是**内置橱窗**。
    **丢的不是节拍,是整个世界。**

    ⚠️ 这条和 `test_import一份纯作者层的包_是当场拒绝_不是一句没人读的日志`
    是有意的两条:那一条钉 `world import` 自己的出口,这一条钉的是**它和另外
    两扇门站在一起时的一致性** —— 同一份包,check 说"能装"(它作为作者层是合法的)、
    开机说"能开"(`--world-file` 那条路),而 import 必须说"这条路装不了它"。
    三句话方向不同却互不矛盾,**正是这一族要的形状**:每扇门只答自己那个问题,
    而且都不许把"我没做那件事"说成"成了"。
    """
    from _worldfile import redis_for

    path = _write(tmp_path / "authored.cyberworld",
                  [_MANIFEST, _YARD, _JIA, _tree_kind(), _OAK])

    # 门一:作为一份作者层,它是合法的 —— 这扇门不该因为 D32 而变红。
    ok_check, check_errors = _check_says(path)
    assert ok_check, f"D32 的修法误伤了 `world check`:{check_errors}"
    # 门三:`--world-file` 那条路真的开得起来。
    ok_boot, boot_errors = _boot_says(path, fresh_redis, "w")
    assert ok_boot, f"D32 的修法误伤了开机那条路:{boot_errors}"

    # 门二:而 `world import` 这条路装不了它,所以它必须说"不行",不是退 0。
    client = redis_for(tmp_path / "d32.db")
    done = run_cli("world", "import", path, "--world-id", "d32")
    assert done.returncode != 0, (
        "三扇门里最乐观的那一句又出现了:一次完全无效的导入报成了成功。"
        f"stdout={done.stdout}")
    assert list(client.scan_iter("anima:d32:*")) == [], "拒绝不许留下半个世界"


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


# ── 八之二、插件那一族:每一种装载期拒绝,三扇门说同一句话(3.8.0)────────────
#
# 创作台 `docs/引擎接口诉求-插件.md` 「欠的第一条」问的就是这件事:一条越界写、
# 一条没声明的 `reads`,`validate world` 到底答什么?REFERENCE §10.2 承诺三条边界
# 违反了**都是开不了机** —— 而那句承诺对消费方成立,靠的是离线这两扇门也这么说
# (创作台出包前那道闸、运维台判包的一次性容器,读的都是它们)。
#
# ⚠️ **下面第九节那条通用的挡不住这一族。** 它钉的是「`world_plugin_errors` 在不在
# `AUTHORED_LAYER_CHECKS` 上」,而在表上**不等于**它覆盖了开机会拒的每一种:
# 开机那条路上还有一处拒绝根本不在 `parse_plugins` 里(`_merge_plugin_kinds` 的
# 「动词借的那个种类真的存在吗」),而通用那条对它一个字都不会说 ——
# **逐条枚举是唯一能让那种漏出现在屏幕上的写法**,这一节就是那份枚举。


_QI = {
    "id": "qi", "version": "1.0.0", "label": "灵力",
    "facts": {"灵力": {"bearer": "agent", "shape": "number", "default": 10.0,
                       "range": [0, 100], "visibility": "self"}},
    "rules": [{"id": "回气", "every": {"ticks": 1}, "for_each": {"kind": "agent"},
               "set": {"qi.灵力": "clamp(qi.灵力 + 1.0 * dt, 0, 100)"}}],
}
#: 十一层共用的那个"多写的键"。同一个词,免得哪一层的断言其实在验别的东西。
_ODD = "颜色"


def _qi(**over) -> dict:
    """写对的那一份 + 改掉几格。**深拷** —— 一条用例改到的格子不许漏给下一条。"""
    body = copy.deepcopy(_QI)
    body.update(copy.deepcopy(over))
    return body


def _plugin_file(tmp_path, *bodies, name="plug", extra=()) -> str:
    """一个写得完整的世界 + 这几条 `plugin` 记录(+ `extra` 里那几条别的作者记录)。

    `extra` 是 2026-08-31 加的:`authored_edge_keys` 那一层住在**作者层的 `edge`
    记录**上,不在 `plugin` 记录里,而十二层那份枚举必须覆盖得到它
    (`STRICT_LEVELS` 加一层却没有对应用例,下场是安静的)。
    """
    rows = [_MANIFEST, _YARD, _JIA]
    rows += [{"kind": "author", "type": "plugin", "body": body} for body in bodies]
    rows += list(extra)
    return _write(tmp_path / f"{name}.cyberworld", rows)


def _edge_row(**body) -> dict:
    return {"kind": "author", "type": "edge", "body": body}


def test_写对的插件_三条路都放行(tmp_path, fresh_redis):
    """对照组。没有它,下面那一摞对一个"插件一律拦"的实现同样成立。"""
    path = _plugin_file(tmp_path, _QI, name="ok")
    ok, _, errors = _both(path, fresh_redis)
    assert ok, f"一份写对的插件被拦下来了:{errors}"


#: 开机会拒的每一种,一行一条。**`needle` 是报错里必须出现的那几个字** ——
#: 只断"两边一致"的话,两扇门**一起**答错还是绿的(这个文件开头那条纪律)。
_PLUGIN_REJECTIONS: tuple[tuple[str, list, str], ...] = (
    ("越界写",
     [_qi(rules=[{"id": "抢别人的", "for_each": {"kind": "agent"},
                  "set": {"mana.法力": "1"}}])],
     "只写得到自己的命名空间"),
    ("reads没声明",
     [_qi(rules=[{"id": "回气", "for_each": {"kind": "agent"},
                  "set": {"qi.灵力": "qi.灵力 + mana.法力"}}])],
     "`reads` 里没有它"),
    ("依赖缺失",
     [_qi(reads=["mana.法力"])],
     "没有装 `mana`"),
    ("依赖成环",
     [_qi(reads=["mana.法力"]),
      {"id": "mana", "version": "1.0.0", "reads": ["qi.灵力"],
       "facts": {"法力": {"bearer": "agent", "shape": "number", "default": 1.0}}}],
     "成环"),
    ("规律的emit写了裸名",
     [_qi(rules=[{"id": "耗尽", "for_each": {"kind": "agent"},
                  "set": {"qi.灵力": "qi.灵力"},
                  "emit": [{"type": "耗尽了", "when": "qi.灵力 < 1"}]}])],
     "只发得出自己命名空间的事件"),
    ("动词多写了一个键",
     [_qi(kinds={"entity:符": {"gloss": "一张符"}},
          verbs={"贴": {"target": "entity:符", "description": "把符贴上去",
                        _ODD: "朱红"}})],
     "不认识的键"),
    ("projected挂在插件自己的种类上",
     [_qi(kinds={"entity:符": {"gloss": "一张符"}},
          facts={"灵力": {"bearer": "agent", "shape": "number", "default": 10.0},
                 "香火": {"bearer": "entity:qi.符", "shape": "number",
                          "mode": "projected"}})],
     "做不了 `projected`"),
    ("动词的target是人",
     [_qi(verbs={"拜": {"target": "agent", "description": "拜他为师"}})],
     "对着一个人做的动词不从这条路走"),
    ("动词的target指着一个这个世界里没有的种类",
     [_qi(verbs={"贴": {"target": "fu", "description": "把符贴上去"}})],
     "这个世界里没有这个种类"),
    # 🔴 2026-08-27 验收 C 逮的:**只写不读**的那一支从前一路绿到底,
    # 而放行的样子是量表里并排住下两个量,规律更新的是没人读的那一个。
    ("规律写一个没声明过的事实",
     [_qi(rules=[{"id": "涨", "for_each": {"kind": "agent"},
                  "set": {"qi.没这个": "now"}}])],
     "顶层 `facts` 里没有"),
)


@pytest.mark.parametrize(
    "case, bodies, needle", _PLUGIN_REJECTIONS,
    ids=[case for case, _bodies, _needle in _PLUGIN_REJECTIONS],
)
def test_插件的每一种装载期拒绝_三扇门说同一句话(
    tmp_path, fresh_redis, case, bodies, needle,
):
    """**开机是权威**:比它松是假绿(作者信了绿灯再去撞开机),比它严是假红
    (作者去改一个没错的东西)。这一族逐条枚举,是因为通用那条只看得见"表上有没有
    这一格",看不见"这一格覆盖了几种"。"""
    path = _plugin_file(tmp_path, *bodies, name="bad")
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, f"「{case}」开机是拦的,离线那两扇门必须一起拦"
    assert any(needle in e for e in errors), (case, errors)


#: **十一层键名单,一层一条。** 反向闸在下面那条:`STRICT_LEVELS` 加一层而这儿
#: 没跟上,当场红 —— 「一层一层收本身就是这个 bug 的形状」那条纪律的三扇门版。
_ONE_FACT = {"bearer": "agent", "shape": "number", "default": 10.0}
_ONE_RULE = {"id": "回气", "every": {"ticks": 1}, "for_each": {"kind": "agent"},
             "set": {"qi.灵力": "qi.灵力"}}
_ONE_TRIGGER = {"id": "干活耗气", "on": {"event": "entity_interaction"},
                "effects": [{"set": {"qi.灵力": "qi.灵力 - 1"}}]}

_STRICT_LEVEL_CASES: dict[str, dict] = {
    "plugin_keys": _qi(**{_ODD: "朱红"}),
    "fact_keys": _qi(facts={"灵力": {**_ONE_FACT, _ODD: "朱红"}}),
    "edge_keys": _qi(edges={"同门": {"from": "agent", "to": "agent", _ODD: "朱红"}}),
    "kind_keys": _qi(kinds={"entity:符": {"gloss": "一张符", _ODD: "朱红"}}),
    "verb_keys": _qi(kinds={"entity:符": {"gloss": "一张符"}},
                     verbs={"贴": {"target": "entity:符", "description": "贴上去",
                                   _ODD: "朱红"}}),
    "rule_keys": _qi(rules=[{**_ONE_RULE, _ODD: "朱红"}]),
    "emit_keys": _qi(rules=[{**_ONE_RULE,
                             "emit": [{"type": "qi.耗尽", "when": "qi.灵力 < 1",
                                       _ODD: "朱红"}]}]),
    "trigger_keys": _qi(triggers=[{**_ONE_TRIGGER, _ODD: "朱红"}]),
    "trigger_emit_keys": _qi(triggers=[{
        **_ONE_TRIGGER, "effects": [{"emit": {"type": "qi.耗尽", _ODD: "朱红"}}]}]),
    "edge_effect_keys": _qi(
        edges={"同门": {"from": "agent", "to": "agent"}},
        triggers=[{**_ONE_TRIGGER,
                   "effects": [{"link": {"type": "qi.同门", "from": "self",
                                         "to": "agent:甲", _ODD: "朱红"}}]}]),
    "projected_source_keys": _qi(facts={
        "灵力": dict(_ONE_FACT),
        "香火": {"bearer": "actor", "shape": "number", "mode": "projected",
                 "sources": [{"event": "payment", "credit": "amount",
                              _ODD: "朱红"}]}}),
    # 🆕 第十二层住在**作者层的 `edge` 记录**上,不在 `plugin` 记录里
    # (3.8.0,2026-08-31,收件箱 D44)。所以这一格是 `(插件体, 额外的作者记录)`。
    "authored_edge_keys": (
        _qi(edges={"同门": {"from": "agent", "to": "agent"}}),
        [_edge_row(type="qi.同门", **{"from": "agent:甲", "to": "agent:甲",
                                      _ODD: "朱红"})],
    ),
}


def test_十二层键名单_每一层都有一条三扇门用例():
    """🔴 **反向闸**:`STRICT_LEVELS` 是「插件这一族每一个会查不认识键的层级」,
    加一层却没有对应的三扇门用例,下场是安静的 —— 那一层的拒绝可能只在开机那侧
    发生,而离线两扇门照答绿灯,**没有一处会红**。"""
    from anima_world.plugins import STRICT_LEVELS

    assert sorted(_STRICT_LEVEL_CASES) == sorted(STRICT_LEVELS), (
        "这张表和引擎那张层名单对不上 —— 多出来的是过期用例,少掉的是没人验的那一层"
    )


@pytest.mark.parametrize("level", sorted(_STRICT_LEVEL_CASES))
def test_十二层里多写一个键_三扇门都拦(tmp_path, fresh_redis, level):
    """不认识的键**照收然后丢掉**比"不支持"坏得多:作者写下的那一格根本不在,
    而退出码 0、日志干净。这一条钉的是"离线那两扇门也看得见这件事"。"""
    case = _STRICT_LEVEL_CASES[level]
    body, extra = case if isinstance(case, tuple) else (case, ())
    path = _plugin_file(tmp_path, body, name=level, extra=extra)
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, f"`{level}` 这一层多写一个键,开机是拦的,离线两扇门也得拦"
    assert any(_ODD in e for e in errors), (level, errors)
    assert any(f"plugins.{level}" in e for e in errors), (
        f"`{level}` 那条报错没说该去问契约的哪一格 —— **每层的名单都进了契约**,"
        f"而报错不点名的话读的人只能照文档记一份会烂的清单:{errors}"
    )


def test_编辑包里的插件_动词借世界里已有的种类_三扇门说同一句话(tmp_path, fresh_redis):
    """**`--edit` 豁免的是跨引用,而这一格不在豁免里** —— 开机自己也拦。

    形状值得记:一个**跑着的**世界里有 `tree`,作者拿一份只含插件的编辑包去给它加
    一个挂在 `tree` 上的动词 —— **开机拒**(`_merge_plugin_kinds` 手上只有这份文件的
    `kinds`,那是空的)。这不是这一轮改出来的行为,是它一直如此;这一轮改的是
    **离线那两扇门从此说同一句话**(此前它们答绿,而作者要到真开机才知道)。

    替代写法就在这条用例的后半截:**同一份编辑包里把那个 `kind` 再声明一遍**。
    作者层是「只填缺不覆盖」,重声明一个已有的种类不会把世界跑出来的现在倒带,
    它只是让这份文件自己说得清"我这个动词挂在谁身上"。
    ⚠️ **这后半截必须真敲一遍**:FOR-STUDIO 里写给创作台的正是这句话,而
    「文档里承诺了一句用户会照着敲的命令,就去敲一遍」是这个仓库的老账。
    """
    from anima_world.api import World

    base = _write(tmp_path / "base.cyberworld",
                  [_MANIFEST, _YARD, _JIA, _tree_kind(), _OAK])
    World.open("w", redis=fresh_redis, world_file=base, force_mock_llm=True).close()

    verb = {"id": "qi", "version": "1.0.0",
            "facts": {"灵力": {"bearer": "agent", "shape": "number", "default": 1.0}},
            "verbs": {"施法": {"target": "tree", "description": "对着树施法",
                               "set": {"树高": "树高 + 0.1"}}}}
    bare = _write(tmp_path / "edit-bare.cyberworld",
                  [_MANIFEST, {"kind": "author", "type": "plugin", "body": verb}])
    ok_validate, errors = _validate_says(bare, edit=True)
    ok_check, check_errors = _check_says(bare, edit=True)
    ok_boot, boot_errors = _boot_says(bare, fresh_redis, "w")
    assert not ok_boot, "开机居然收了 —— 那这条用例钉的东西整个变了,先去看开机那侧"
    assert ok_validate == ok_boot and ok_check == ok_boot, (
        f"离线两扇门和开机不是同一个答案 —— validate:{errors};check:{check_errors};"
        f"开机:{boot_errors}"
    )
    assert check_errors == errors, "两扇门自己先分叉了"
    # **只断"两边一致"的话,两扇门一起答错还是绿的**(这个文件开头那条纪律)——
    # 所以还要断它一致到哪一句上,而那一句正是这条用例 docstring 讲的那道闸。
    assert any("这个世界里没有这个种类" in e for e in errors), errors

    # 后半截:把那个种类在同一份文件里再声明一遍,三扇门一起放行。
    both = _write(tmp_path / "edit-with-kind.cyberworld",
                  [_MANIFEST, _tree_kind(),
                   {"kind": "author", "type": "plugin", "body": verb}])
    ok_validate, errors = _validate_says(both, edit=True)
    ok_check, check_errors = _check_says(both, edit=True)
    # ⚠️ **仍然装进同一个世界 `w`** —— 换一个空的 world_id 就不是"编辑"了,
    # 那是创世,而创世本来就要一份完整的名册与地图(上面 `--edit` 那条钉着这一半)。
    ok_boot, boot_errors = _boot_says(both, fresh_redis, "w")
    assert ok_validate and ok_check and ok_boot, (
        f"替代写法被拦下来了 —— validate:{errors};check:{check_errors};"
        f"开机:{boot_errors}"
    )


def test_声明了一种边而没人造得出它_两扇门都说这句话(tmp_path, fresh_redis, caplog):
    """**警告那一半也归"三扇门说同一句话"管**(2026-08-27)。

    作者声明了一种边,而这份文件里没有一个动词或触发器造得出它 —— 引擎**收**
    (开机是权威,比它严就是假红),但那张表会永远是空的,而**没有一处会说话**
    正是这个仓库最怕的形状。所以三扇门一起说:两扇离线门进 `warnings`,
    开机进 `logger.warning` —— **只有 `validate world` 看得到的警告,在托管环境里
    等于没有**(那儿没有人会去跑那条命令)。
    """
    import logging

    idle = {"id": "menpai", "version": "1.0.0",
            "edges": {"member_of": {"from": "agent", "to": "group:sect"}},
            "kinds": {"group:sect": {"gloss": "一个门派"}}}
    path = _plugin_file(tmp_path, idle, name="idle-edge")
    ok, _, errors = _both(path, fresh_redis)
    assert ok, f"这是一句警告不是一条错误,世界必须照旧开得起来:{errors}"

    said_validate, said_check = _warnings(path)
    assert any("menpai.member_of" in w for w in said_validate), said_validate
    assert said_validate == said_check, "两扇门的 warnings 分叉了"

    with caplog.at_level(logging.WARNING):
        assert _boot_says(path, fresh_redis, "boot-idle")[0]
    assert any("menpai.member_of" in r.getMessage() for r in caplog.records), (
        "开机那一侧一个字都没说 —— 而托管环境里没有人会去跑 `validate world`"
    )


def test_边有人造得出时_那句话不许再响(tmp_path, fresh_redis):
    """**一句总在响的警告等于没有警告。** 对照组和上一条只差一个动词。"""
    made = {"id": "menpai", "version": "1.0.0",
            "edges": {"member_of": {"from": "agent", "to": "group:sect"}},
            "kinds": {"group:sect": {"gloss": "一个门派"}},
            "verbs": {"入门": {"target": "group:sect", "description": "拜入门派",
                               "effects": [{"link": {"type": "menpai.member_of",
                                                     "from": "self",
                                                     "to": "target"}}]}}}
    path = _plugin_file(tmp_path, made, name="made-edge")
    ok, _, errors = _both(path, fresh_redis)
    assert ok, errors
    said_validate, said_check = _warnings(path)
    assert not any("造得出" in w for w in said_validate), said_validate
    assert said_validate == said_check


# ── 八之三、边那一段:作者层第十四个段,每一种拒绝三扇门说同一句话 ──────────────
#
# 3.8.0 / 2026-08-31,收件箱 D44。**加一个作者层的段就是新开一族开机失败**,
# 而这个仓库为同一件事栽过一次:3.7.0 收节拍时第一版只收了段没补门,`world check`
# 对一份开不了机的文件照答 `loadable: true`。
#
# ⚠️ **第九节那条通用的对这一族一个字都不会说** —— 它只看得见
# `world_edge_errors` 在不在 `AUTHORED_LAYER_CHECKS` 上,而**在表上不等于覆盖了
# 开机会拒的每一种**。下面这份枚举是唯一能让那种漏出现在屏幕上的写法。

_MENPAI = {
    "id": "menpai", "version": "1.0.0",
    "kinds": {"group:sect": {"gloss": "一个门派"}},
    "edges": {"member_of": {"from": "agent", "to": "group:sect",
                            "exclusive": True}},
    "verbs": {"入门": {"target": "group:sect", "description": "拜入门派",
                       "effects": [{"link": {"type": "menpai.member_of",
                                             "from": "self", "to": "target"}}]}},
}
_SECT = {"kind": "author", "type": "entity",
         "body": {"id": "menpai.sect:青云门", "name": "青云门", "location": "yard"}}
_SECT2 = {"kind": "author", "type": "entity",
          "body": {"id": "menpai.sect:天罡门", "name": "天罡门", "location": "yard"}}
_GOOD_EDGE = _edge_row(type="menpai.member_of",
                       **{"from": "agent:甲", "to": "menpai.sect:青云门"})

#: 开机会拒的每一种,一行一条。`needle` 是报错里必须出现的那几个字 ——
#: **只断"两边一致"的话,两扇门一起答错还是绿的**(这个文件开头那条纪律)。
_EDGE_REJECTIONS: tuple[tuple[str, list, str], ...] = (
    ("边名拼错",
     [_edge_row(type="menpai.member_off",
                **{"from": "agent:甲", "to": "menpai.sect:青云门"})],
     "没声明过"),
    ("type没带命名空间",
     [_edge_row(type="member_of", **{"from": "agent:甲", "to": "menpai.sect:青云门"})],
     "`<插件>.<边名>`"),
    ("起点少了前缀",
     [_edge_row(type="menpai.member_of",
                **{"from": "甲", "to": "menpai.sect:青云门"})],
     "`agent:<agent_id>`"),
    ("终点写的是别的种类",
     [_edge_row(type="menpai.member_of", **{"from": "agent:甲", "to": "tree:oak"})],
     "menpai.sect:<实例名>"),
    # 🔴 出厂插件的边是内核**投影的物化视图**,手写一行进去就是伪造演化态。
    ("出厂插件的边",
     [_edge_row(type="invitation.invites",
                **{"from": "agent:甲", "to": "agent:player:p1"})],
     "出厂插件"),
    ("少了一端",
     [_edge_row(type="menpai.member_of", **{"from": "agent:甲"})],
     "少了 ['to']"),
    ("同一条写了两遍",
     [_GOOD_EDGE, dict(_GOOD_EDGE)],
     "写了两遍"),
    # 🔴 **`facts` 那一格得钉具体的键,不能只靠通用的 `_ODD`**(2026-08-31 验收 A):
    # 把 `facts` 加进 `AUTHORED_EDGE_KEYS` 做变异,全量照旧全绿 —— 而放行的后果
    # 正是 `_seed_edges` 把作者写的那几格**静默丢掉**(它只读 type/from/to)。
    # 通用那条用的是一个引擎根本不打算收的词,所以它对"收了一个真键"一个字都不说。
    ("边上写了facts",
     [_edge_row(type="menpai.member_of", facts={"辈分": "内门"},
                **{"from": "agent:甲", "to": "menpai.sect:青云门"})],
     "'facts'"),
    # `exclusive` 在**这一份文件之内**自相矛盾:放行的样子是安静的
    # (`apply_edge_effect` 只 logger.warning 一句然后 return False)。
    ("exclusive自相矛盾",
     [_SECT2, _GOOD_EDGE,
      _edge_row(type="menpai.member_of",
                **{"from": "agent:甲", "to": "menpai.sect:天罡门"})],
     "exclusive"),
)


@pytest.mark.parametrize(
    "case, extra, needle", _EDGE_REJECTIONS,
    ids=[case for case, _extra, _needle in _EDGE_REJECTIONS],
)
def test_边那一段的每一种拒绝_三扇门说同一句话(
    tmp_path, fresh_redis, case, extra, needle,
):
    """**开机是权威**:比它松是假绿(作者信了绿灯再去撞开机),比它严是假红。"""
    path = _plugin_file(tmp_path, _MENPAI, name="bad-edge",
                        extra=[_SECT, *extra])
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, f"「{case}」开机是拦的,离线那两扇门必须一起拦"
    assert any(needle in e for e in errors), (case, errors)


def test_写对的边_三条路都放行_而且那一行真的落进库里(tmp_path, fresh_redis):
    """对照组。**没有它,上面那一摞对一个"有 `edges` 段就一律拦"的实现同样成立。**

    🔴 顺带钉住"真的落库" —— 一条种下去而没连上的边和一条根本没写的边,
    在退出码和日志上长得一模一样。
    """
    path = _plugin_file(tmp_path, _MENPAI, name="ok-edge",
                        extra=[_SECT, _GOOD_EDGE])
    ok, _, errors = _both(path, fresh_redis)
    assert ok, f"一份写对的边被拦下来了:{errors}"

    from anima_world.api import World

    with World.open("planted", redis=fresh_redis, world_file=path,
                    force_mock_llm=True) as world:
        rows = world.scheduler.edge_store.all("menpai.member_of")
    assert rows == [("agent:甲", "menpai.sect:青云门", {})], rows


def test_一份只带边的编辑包_离线明说这一格答不了_而开机答得出(tmp_path, fresh_redis):
    """🔴 **离线那两扇门手上只有这份文件,而边的 type 声明在目标世界的库里。**

    这是 `--edit` 那条"查得动查不动"分界在边这一层的落法,两半都要钉:
    离线**不许猜一个答案**(说出来那一格没查),而开机拿的是三个来源合并后的
    名单,所以它**答得出** —— 一份拼错了边名的编辑包在开机那儿当场红。

    ⚠️ 只钉前一半的话,一个"编辑包里的边一律不查"的实现照样全绿,而作者会拿着
    一份开不了机的包出门。
    """
    from anima_world.api import World

    base = _plugin_file(tmp_path, _MENPAI, name="edit-base", extra=[_SECT])
    with World.open("edit", redis=fresh_redis, world_file=base,
                    force_mock_llm=True) as world:
        assert world.scheduler.edge_store.all("menpai.member_of") == []

    good = _write(tmp_path / "edit-ok.cyberworld", [_MANIFEST, _GOOD_EDGE])
    said_validate, said_check = _warnings(good, edit=True)
    # ⚠️ **只比边那一句,不比整份**:两扇门在 `--edit` 那句**样板话**上本来就不
    # 逐字相同(`validate world` 多一句"要连着世界查剩下的,用 `simulate …`"),
    # 而那是它们各自的出口话术,不是判断。判断那一半才必须一模一样。
    mine_v = [w for w in said_validate if "离线这一格答不了" in w]
    mine_c = [w for w in said_check if "离线这一格答不了" in w]
    assert mine_v == mine_c and mine_v, (
        f"边那一格两扇门分叉了 —— validate:{mine_v};check:{mine_c}")
    assert "menpai" in mine_v[0], (
        f"离线猜了一个答案,而它手上根本没有那份声明:{mine_v}")
    assert _validate_says(good, edit=True)[0], "一份合法的编辑包被拦下来了"
    with World.open("edit", redis=fresh_redis, world_file=good,
                    force_mock_llm=True) as world:
        assert world.scheduler.edge_store.all("menpai.member_of") == [
            ("agent:甲", "menpai.sect:青云门", {})]

    # 后半截:**开机答得出,所以它拦得下** —— 而离线照旧只说"没查"。
    bad = _write(tmp_path / "edit-bad.cyberworld", [
        _MANIFEST,
        _edge_row(type="menpai.member_off",
                  **{"from": "agent:甲", "to": "menpai.sect:青云门"})])
    assert _validate_says(bad, edit=True)[0], "离线手上没有声明,不许猜一个红灯"
    with pytest.raises(Exception) as caught:
        with World.open("edit", redis=fresh_redis, world_file=bad,
                        force_mock_llm=True):
            pass
    said = "\n".join(getattr(caught.value, "errors", None) or [str(caught.value)])
    assert "没声明过" in said, said


def test_开机遇到一个不存在的插件命名空间_当场拒_不许照种(tmp_path, fresh_redis):
    """🔴 **2026-08-31 验收 A 的 P1,而它是这一族最贵的那种错法。**

    离线那两扇门手上只有这份文件,所以「`ghosts` 这个插件到底在不在」它答不了 ——
    **而开机手上是「出厂 + 库里 + 文件」合并后的全集,它答得出**。第一版把这两件事
    写成了同一句(开机原样复用了离线那条 `continue`),下场是:

    - 一份**完整**的世界文件,插件名打错一个字母 → **开机成功**;
    - 边**真的落库**,幻影类型被 `sadd` 进 `edge_types`;
    - 节点 id 那道闸**一次都没跑**(连 `agent:` 前缀都没有的 `甲` 照收);
    - 而屏幕上印着的正是引擎自己那句「那种文件真开机时会当场红」——
      **说那句话的就是开机,而它没红。**

    一句自己证伪自己的诊断,比没有诊断更坏:它让读的人相信这条路已经有人守着。

    ⚠️ 这一条**故意不走 `_both`**:离线绿 + 开机红在这里是**对的**(查得动查不动的
    分界),`_both` 会把它判成分叉。两半分开断,并且**都断**。
    """
    from anima_world.api import World

    path = _plugin_file(tmp_path, _MENPAI, name="ghost", extra=[
        _SECT,
        _edge_row(type="ghosts.member_of", **{"from": "甲", "to": "ghosts.sect:无门"}),
    ])
    # 离线:不猜,只说这一格没查(它手上确实没有 `ghosts` 的声明)。
    assert _validate_says(path)[0], "离线手上没有那份声明,不许猜一个红灯"
    said_validate, said_check = _warnings(path)
    assert any("离线这一格答不了" in w for w in said_validate), said_validate
    assert said_validate == said_check

    # 开机:手上是全集 —— **答得出,所以必须拦**。
    with pytest.raises(Exception) as caught:
        with World.open("ghost", redis=fresh_redis, world_file=path,
                        force_mock_llm=True):
            pass
    said = "\n".join(getattr(caught.value, "errors", None) or [str(caught.value)])
    assert "没有名叫 `ghosts` 的插件" in said, said
    # 🔴 **「先验再写」在这一层也得成立**:拒掉的那一趟不许留下半个世界。
    assert [k for k in fresh_redis.scan_iter("anima:ghost:edge*")] == [], (
        "开机拒了,可幻影边/幻影类型已经落库了 —— 那正是「装了一半的世界」"
    )


def test_边只在这一种还空着时才种_而跳过要说出来(tmp_path, fresh_redis, caplog):
    """**只填缺不覆盖,粒度是「每一种边」。**

    舰队上的世界容器**每次开机都带着 `--world-file`**,所以"这条边不在就补上"
    会让每一次重启都把运行期断掉的边接回来 —— 而边不进事件日志,引擎手上没有
    "有人断过它"这份记录。整种跳过分得出:那一种只要还有一行,这个世界就已经在
    过自己的日子了。

    ⚠️ **跳过必须说出来**:一句话不说的样子是"我把这几条种进去了",而拿一份改过
    的世界文件去编辑一个跑着的世界的人,会以为第二位弟子已经在里面了。
    """
    import logging

    from anima_world.api import World

    one = _plugin_file(tmp_path, _MENPAI, name="grain-1",
                       extra=[_SECT, _SECT2, _GOOD_EDGE])
    with World.open("grain", redis=fresh_redis, world_file=one,
                    force_mock_llm=True) as world:
        assert len(world.scheduler.edge_store.all("menpai.member_of")) == 1

    two = _write(tmp_path / "grain-2.cyberworld", [
        _MANIFEST, {"kind": "author", "type": "plugin", "body": _MENPAI},
        _GOOD_EDGE,
        _edge_row(type="menpai.member_of",
                  **{"from": "agent:乙", "to": "menpai.sect:天罡门"})])
    with caplog.at_level(logging.WARNING):
        with World.open("grain", redis=fresh_redis, world_file=two,
                        force_mock_llm=True) as world:
            rows = world.scheduler.edge_store.all("menpai.member_of")
    assert rows == [("agent:甲", "menpai.sect:青云门", {})], (
        f"这一种已经有行了,文件里那两条一条都不该种进去:{rows}")
    assert any("没有种进去" in r.getMessage() for r in caplog.records), (
        "整种跳过了却一个字不说 —— 作者会以为第二位弟子已经在里面了"
    )


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


def test_状态层那张表里的键_必须真的是这个世界里的键(tmp_path, open_world):
    """🔴 **这一条防的是一种 0 行的假绿**(收件箱 D30 的连带闸,2026-08-27)。

    `STATE_ONTOLOGY_TABLES` 是按 **Redis 键名**匹配的。哪天有人把 `:kinds` 改名、
    或者把种类挪进别的键,这张表就**一条都匹配不上** —— 于是状态层那道新闸
    悄悄回到"什么都没查",而 `errors` 是空的、`loadable` 是 `true`、
    **一条测试都不会红**。这正是这个仓库记过的那种坏法:命令答 0 行,
    而 0 行既可能是"没问题",也可能是"这条命令根本没说话"。

    所以这里拿**一个真的跑过的世界**去对:表里登记的每一个键,都必须真的出现在
    它导出来的状态层里。改了键名 → 这条当场红,而不是那扇门安静地失明。
    """
    from anima_world.world_file import STATE_ONTOLOGY_TABLES, StateScan, read_world_file

    world = open_world("keys")
    world.tick(2)
    out = tmp_path / "keys.cyberworld"
    world.export_snapshot(out, world_id="keys", name="对键名的")
    world.close()

    scan = StateScan()
    _, records = read_world_file(str(out))
    for record in records:
        scan.feed(record)

    missing = sorted(set(STATE_ONTOLOGY_TABLES) - scan.tables)
    assert not missing, (
        f"`STATE_ONTOLOGY_TABLES` 里这几个键在一个真世界里根本不存在:{missing} —— "
        f"这个世界导出来的表是 {sorted(scan.tables)}。"
        "键名对不上时那道闸不会报错,它只是什么都不查"
    )
    # 而"查过的"这一格也必须真的有料:五张表全空的话,上面那条也照样过。
    assert scan.rows, "登记过的那几张表一行都没读到 —— 那道闸此刻是死的"


# ── 内容包(作者层第十五个段,3.10.0)────────────────────────────────────────
#
# 🔴 **加一种新的开机失败,就必须同一轮补进离线那两扇门。** 3.7.0 收 `beat` 段时
# 漏过一次(收了段没补门,于是 `world check` 对一份**开不了机**的文件照答
# `loadable: true`),而 `tests/...::test_每个检查器都在表上` 那条通用用例**不会**
# 替你发现它:在表上 ≠ 覆盖了开机会拒的每一种。下面这份枚举才是。

def _pack_row(**body) -> dict:
    return {"kind": "author", "type": "pack", "body": body}


_GOOD_PACK = _pack_row(id="第二周", version="1.0.0", note="社团活动")

#: 开机会拒的每一种,一行一条。`needle` 是报错里必须出现的那几个字。
_PACK_REJECTIONS: tuple[tuple[str, dict, str], ...] = (
    ("id 里有点号",
     _pack_row(id="第二.周", version="1.0.0"), "pack.id"),
    ("id 里有空格",
     _pack_row(id="第二 周", version="1.0.0"), "pack.id"),
    ("id 是空的",
     _pack_row(id="", version="1.0.0"), "pack.id"),
    ("少了 version",
     _pack_row(id="第二周"), "pack.version"),
    ("version 不是文本",
     _pack_row(id="第二周", version=2), "pack.version"),
    ("note 不是文本",
     _pack_row(id="第二周", version="1.0.0", note=["社团"]), "pack.note"),
    ("多写了一个键",
     _pack_row(id="第二周", version="1.0.0", author="谁"), "不认识的键"),
)


@pytest.mark.parametrize(
    "case, row, needle", _PACK_REJECTIONS,
    ids=[case for case, _row, _needle in _PACK_REJECTIONS],
)
def test_内容包那一段的每一种拒绝_三扇门说同一句话(
    tmp_path, fresh_redis, case, row, needle,
):
    """**开机是权威**:比它松是假绿,比它严是假红。"""
    path = _write(tmp_path / "bad-pack.cyberworld",
                  [_MANIFEST, _YARD, _JIA, row])
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, f"「{case}」开机是拦的,离线那两扇门必须一起拦"
    assert any(needle in e for e in errors), (case, errors)


def test_写对的内容包_三条路都放行_而且它真的落进这个世界的账上(tmp_path, fresh_redis):
    """对照组 + 落地。

    🔴 **没有下半句,上面那一摞对一个「有 `pack` 段就一律拦」的实现同样成立。**
    而"装进去了没有"的判据是 `World.packs()`,它折自 `pack_installed` 事件 ——
    **没有第二张表**,所以这一句同时钉住了"那条事件真的发出去了"。
    """
    path = _write(tmp_path / "ok-pack.cyberworld",
                  [_MANIFEST, _YARD, _JIA, _GOOD_PACK])
    ok, _, errors = _both(path, fresh_redis)
    assert ok, f"一份写对的内容包被拦下来了:{errors}"

    from anima_world.api import World

    with World.open("packed", redis=fresh_redis, world_file=path,
                    force_mock_llm=True) as world:
        packs = world.packs()
    assert [p["id"] for p in packs] == ["第二周"], packs
    # 创世那一趟落地的是**世界第 0 天** —— 于是同一份文件当创世用时,
    # `since: "pack"` 和「世界第 0 天」是同一个答案,没有特例。
    assert packs[0]["day"] == 0, packs
    assert packs[0]["version"] == "1.0.0"


def test_一份没写pack的老包_这一层整个缺席(tmp_path, fresh_redis):
    """**声明本身就是开关** —— 和 `beats` / `kinds` / perception 逐字同构。

    ⚠️ 这一条钉的是"老包一个字不用改":上面那份枚举全绿而这一条红的话,
    就是这一层从"可选"变成了"必填",而**已经发出去的每一个世界都会开不了机**。
    """
    path = _write(tmp_path / "no-pack.cyberworld", [_MANIFEST, _YARD, _JIA])
    ok, _, errors = _both(path, fresh_redis)
    assert ok, errors

    from anima_world.api import World

    with World.open("nopack", redis=fresh_redis, world_file=path,
                    force_mock_llm=True) as world:
        assert world.packs() == []


# ── 拍的 `trigger` / `trigger.at` 两层闭集,与 `since`(3.10.0)────────────────
#
# 🔴 **验收 B ⑦**:这三种新拒绝**零测试**,而通用那条「每个检查器都在表上」
# 对它们一个字都不会说 —— **在表上 ≠ 覆盖了开机会拒的每一种**,这正是逐条枚举
# 存在的全部理由。

def _beat_row(**body) -> dict:
    return {"kind": "author", "type": "beat", "body": body}


_OK_PAYLOAD = [{"op": "memory", "agent_id": "甲", "summary": "一句话"}]

_BEAT_TRIGGER_REJECTIONS: tuple[tuple[str, dict, str], ...] = (
    ("trigger 多一个键",
     _beat_row(id="b1", trigger={"at": {"day": 0}, "tag": "第一幕"},
               payload=_OK_PAYLOAD),
     "trigger 里不认识的字段"),
    ("trigger.at 多一个键",
     _beat_row(id="b2", trigger={"at": {"day": 0, "hour": 9}}, payload=_OK_PAYLOAD),
     "trigger.at 里不认识的字段"),
    ("since 写了个不认识的值",
     _beat_row(id="b3", trigger={"at": {"day": 0, "since": "player"}},
               payload=_OK_PAYLOAD),
     "trigger.at.since"),
    ("世界级的拍写了 narrate",
     _beat_row(id="b4", trigger={"at": {"day": 0}}, payload=_OK_PAYLOAD,
               narrate="一封信躺在你桌上。"),
     "没有「那个人」"),
    ("narrate 是空白",
     _beat_row(id="b5", for_each={"node": "player"}, trigger={"at": {"day": 0}},
               payload=_OK_PAYLOAD, narrate="   "),
     "'narrate' 要是一段非空文本"),
)


@pytest.mark.parametrize(
    "case, row, needle", _BEAT_TRIGGER_REJECTIONS,
    ids=[case for case, _row, _needle in _BEAT_TRIGGER_REJECTIONS],
)
def test_拍的新闭集_每一种拒绝三扇门说同一句话(tmp_path, fresh_redis, case, row, needle):
    """**开机是权威**:比它松是假绿,比它严是假红。"""
    path = _write(tmp_path / "bad-beat.cyberworld", [_MANIFEST, _YARD, _JIA, row])
    ok, _, errors = _both(path, fresh_redis)
    assert not ok, f"「{case}」开机是拦的,离线那两扇门必须一起拦"
    assert any(needle in e for e in errors), (case, errors)


def test_写对的since和narrate_三条路都放行(tmp_path, fresh_redis):
    """对照组。**没有它,上面那一摞对一个「有 `since` 就一律拦」的实现同样成立。**"""
    path = _write(tmp_path / "ok-beat.cyberworld", [
        _MANIFEST, _YARD, _JIA,
        _beat_row(id="b6", trigger={"at": {"day": 0, "since": "world"}},
                  payload=_OK_PAYLOAD),
        _beat_row(id="b7", for_each={"node": "player"}, trigger={"at": {"day": 1}},
                  payload=_OK_PAYLOAD, narrate="一封信躺在你桌上。"),
    ])
    ok, _, errors = _both(path, fresh_redis)
    assert ok, f"写对的拍被拦下来了:{errors}"


def test_封皮说3_9_0而包里有pack段_两扇离线门一起拒(tmp_path, fresh_redis):
    """🟡 **验收 C ⑥**:`engine_min` 一个字不查 —— 而这一格是**下游照它做判断**的。

    ⚠️ 开机那条路不比封皮(它就是那一版引擎,比它没有意义),所以这一条只钉
    离线那两扇门 —— 而这正是「三扇门」在这一格上的真实形状,写清楚比含糊着好。
    """
    old = {"kind": "manifest", "version": 3, "world_id": "t", "engine_min": "3.9.0"}
    path = _write(tmp_path / "old-engine.cyberworld",
                  [old, _YARD, _JIA,
                   {"kind": "author", "type": "pack",
                    "body": {"id": "第二周", "version": "1.0.0"}}])
    ok_validate, errors = _validate_says(path, edit=True)
    ok_check, check_errors = _check_says(path, edit=True)
    assert ok_validate is False and ok_check is False, (errors, check_errors)
    assert errors == check_errors, "两扇门说了不同的话"
    assert any("engine_min" in e for e in errors), errors
