"""世界观从世界文件走到她收到的提示词里 —— 整条通道。

**这条路上一整版是断的,而且是静默的。** 线格式把 `world_setting` 归进"对象型"
(body 必须是 dict),播种函数 `__main__._seed_world_setting` 只认字符串:任何
`.cyberworld` 写下的世界观都送不进世界,世界照样建得起来、角色照样说话,只是
每个自定义世界都跑在引擎写死的那份默认世界观下。`demo.cyberworld` 里没有这条记录,
也没有任何测试碰过它 —— **零覆盖正是它能安静烂掉的原因**,所以这份文件盯的是
两头对不对得上,而不是某一头写得对不对。

盯的是**这段话到底有没有进她的提示词**,不是"某个函数被调用了":中间任何一环
改形状,这条都会红。
"""
from __future__ import annotations

import pytest

from anima_world.world_file import WorldFileError, author_records_to_seed

_SETTING = "旧港区,常年下雨。港务局说了算,夜里没人提三号码头。"

_BASE = [
    {"kind": "manifest", "version": 3, "world_id": "harbor", "name": "旧港区"},
    {"kind": "author", "type": "location",
     "body": {"id": "cafe", "name": "咖啡店", "description": "拐角那间"}},
    {"kind": "author", "type": "agent",
     "body": {"id": "夏", "name": "苏晚夏", "location": "cafe", "personality": "话少"}},
]


def _write(path, *extra) -> str:
    """一份手写的世界(裸 JSONL,不 gzip)—— 创作台产出的就是这个形状。"""
    import json

    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [*_BASE, *extra]),
        encoding="utf-8",
    )
    return str(path)


def _world_file(tmp_path, setting=_SETTING, name: str = "harbor.cyberworld") -> str:
    return _write(tmp_path / name,
                  {"kind": "author", "type": "world_setting", "body": setting})


def test_世界文件的世界观进得了她的提示词(open_world, tmp_path):
    """端到端:文件 → `:prompts` 的 `world.setting` → `ChatService.prompt_blocks`。

    最后一段是关键。只验"库里存下了"验不出这条通道有没有接上 —— 而她读不到的
    世界观和没有世界观是同一件事。
    """
    world = open_world(world_file=_world_file(tmp_path))

    assert world.scheduler.prompt_store.get("world.setting") == _SETTING

    seen = world.debug_prompt("夏")
    (block,) = [b for b in seen["blocks"] if b["label"] == "world.setting"]
    assert block["text"] == _SETTING
    assert _SETTING in seen["system"]


def test_没写世界观的世界照旧用内置那份(open_world, tmp_path):
    """**声明本身就是开关**:不写这条记录的世界行为逐位如旧,而不是变成空世界观。"""
    world = open_world(world_file=_write(tmp_path / "bare.cyberworld"))
    default = world.scheduler.prompt_store.get("world.setting")
    assert default and default.strip(), "内置世界观不该因为文件没写而变成空的"


def test_热改留得住重启不拿文件盖回去(open_world, fresh_redis, tmp_path):
    """M5 规矩(和 llm.* 同一条):文件只在**首启**说一次话,之后 `:prompts`
    那一行是运行期权威。反过来的话,一次重启会悄悄改掉一个跑了半年的世界。"""
    path = _world_file(tmp_path)
    world = open_world("w", redis=fresh_redis, world_file=path)
    world.prompt_set("world.setting", "港务局倒了,现在没人说了算。")
    world.close()

    again = open_world("w", redis=fresh_redis, world_file=path)
    assert again.scheduler.prompt_store.get("world.setting") == "港务局倒了,现在没人说了算。"


def test_世界观写成对象的世界当场开不了机(open_world, tmp_path):
    """作者指名的文件坏了**不许降级** —— 世界文件只会被读进空库一次,静默换成
    内置世界观是不可挽回的。而"写成 `{"text": …}`"正是下游最可能写错的那一种。
    """
    path = _world_file(tmp_path, {"text": _SETTING}, name="bad.cyberworld")
    with pytest.raises(WorldFileError) as excinfo:
        open_world(world_file=path)
    assert "world_setting" in str(excinfo.value)


def test_内置演示世界仍然不带世界观():
    """橱窗里没有这条记录 —— 那正是这个洞一直没暴露的原因。哪天它进了橱窗,
    这条会红,顺手提醒改它的人:上面几条测试才是通道的闸。"""
    from _worldfile import bundled_seed

    assert "world_setting" not in bundled_seed()


def test_一条空白的世界观是错的不是无声的():
    """空字符串和"没写"在文件里长得几乎一样,而它们的意思完全不同。"""
    with pytest.raises(WorldFileError):
        author_records_to_seed([{"kind": "author", "type": "world_setting", "body": ""}])


# ── 热改的门(3.8.0,收件箱 D4)────────────────────────────────────────────────
#
# 上面那几条钉的是**创世那一刻**这段话进不进得来。这一节钉的是另一半:
# 一个**已经建好、有人在玩**的世界,改不改得了自己的世界观。
#
# 在这扇门之前答案是**改不了** —— `_seed_world_setting` 被 `if not persisted` 把着,
# 连拿一份改过的 `.cyberworld` 走 `--world-file` 都不生效(而且不报错)。于是创作台
# 唯一的办法是 `world drop` 把整个世界抹掉重建,玩家的记忆、关系、事件日志一起陪葬。


def test_一个跑着的世界改得了自己的世界观(open_world, fresh_redis, tmp_path):
    """D4 本身。**判据是她收到的提示词变了**,不是"库里存下了" ——
    她读不到的世界观和没有世界观是同一件事(这条和本文件第一条同一个理由)。"""
    world = open_world("w", redis=fresh_redis, world_file=_world_file(tmp_path))
    assert _SETTING in world.debug_prompt("夏")["system"]

    receipt = world.set_world_setting("港务局倒了。现在三号码头夜里有灯。")
    assert receipt["changed"] is True and receipt["cleared"] is False
    assert receipt["before"] == _SETTING
    assert "三号码头夜里有灯" in world.debug_prompt("夏")["system"], (
        "改完她还读着旧的 —— 那这扇门只是写了一行谁也看不见的字")
    assert _SETTING not in world.debug_prompt("夏")["system"]


def test_改成一模一样的一段话_一个字都不写(open_world, fresh_redis, tmp_path):
    """幂等,和 `set_card` / `set_location_image` 同一条:一次什么也没改的「成功」
    读起来和改成功了一模一样,而这条命令最常见的用法是照着单子敲一遍。"""
    world = open_world("w", redis=fresh_redis, world_file=_world_file(tmp_path))
    assert world.set_world_setting(_SETTING)["changed"] is False


def test_dry_run_一个字节都不写(open_world, fresh_redis, tmp_path):
    world = open_world("w", redis=fresh_redis, world_file=_world_file(tmp_path))
    receipt = world.set_world_setting("换一段", dry_run=True)
    assert receipt["changed"] is True and receipt["dry_run"] is True
    assert world.world_setting()["text"] == _SETTING, "--dry-run 动了库"


@pytest.mark.parametrize("bad", [None, "", "   \n  ", 123, {"text": "x"}])
def test_拒绝把世界观写成空白或一张表(open_world, fresh_redis, tmp_path, bad):
    """🔴 **代价不对称,所以这几种一律拒绝。**

    `None` 在宿主那儿最常见的来源是 `row.get("setting")` 没取到值,一段全空白最
    常见的来源是模板里一个没展开的变量。把它们读成"抹掉"就是一次**静默抹掉整个
    世界观** —— 而世界观是她提示词里的**第一块**,抹掉它这个世界里每个人下一句话
    都会变,回执上却写着"改了"。拒一次的代价只是调用方补一个字。
    """
    world = open_world("w", redis=fresh_redis, world_file=_world_file(tmp_path))
    with pytest.raises(ValueError):
        world.set_world_setting(bad)
    assert world.world_setting()["text"] == _SETTING, "拒绝的那一次不许写下任何东西"


def test_clear_是回落到引擎内置那份_不是变成空的(open_world, fresh_redis, tmp_path):
    """「世界里只存作者动过的」那条纪律的对偶:能写下一个意见,就得能收回它。
    而收回之后是**引擎声明的那份**,不是一段空话 —— 判据是"引擎声明过什么",
    不是"表里有没有行"。"""
    from anima_world.prompt_store import resolve

    world = open_world("w", redis=fresh_redis, world_file=_world_file(tmp_path))
    receipt = world.set_world_setting(clear=True)
    assert receipt["cleared"] is True and receipt["changed"] is True
    fallback = resolve("world.setting", None, "")
    assert fallback and receipt["after"] == fallback
    assert world.world_setting()["text"] == fallback
    assert world.world_setting()["source"] == "默认值"
    assert fallback in world.debug_prompt("夏")["system"]


def test_clear_一个和默认值逐字相同的世界观_照样删得掉(open_world, fresh_redis, tmp_path):
    """🔴 **这一条防的是一个只比文本就会漏掉的洞。**

    一个世界完全可能把世界观热改成和引擎默认值**逐字相同**的一段话。那时
    `after == before`,只比文本的话 `--clear` 会报 `changed: false` 然后一个字都
    不写 —— 于是那一行**永远删不掉**,而屏幕上写着"没有变化",看上去完全正常。
    判据必须是「这个世界自己还有没有那一行」。
    """
    from anima_world.prompt_store import resolve

    fallback = resolve("world.setting", None, "")
    world = open_world("w", redis=fresh_redis, world_file=_world_file(tmp_path))
    world.set_world_setting(fallback)
    assert world.world_setting()["source"] == "这个世界"
    assert world.set_world_setting(clear=True)["changed"] is True
    assert world.world_setting()["source"] == "默认值", (
        "文本一样就没删 —— 那一行会永远留在这个世界里")


def test_clear_和一段话不能一起给(open_world, fresh_redis, tmp_path):
    world = open_world("w", redis=fresh_redis, world_file=_world_file(tmp_path))
    with pytest.raises(ValueError):
        world.set_world_setting("换一段", clear=True)


def test_热改过之后重启照旧不被文件盖回去(open_world, fresh_redis, tmp_path):
    """**这扇门没有动那条规矩** —— 它和上面 `test_热改留得住重启不拿文件盖回去`
    是同一条,只是走的是新的写口。两条一起钉:换了门不等于换了语义。"""
    path = _world_file(tmp_path)
    world = open_world("w", redis=fresh_redis, world_file=path)
    world.set_world_setting("港务局倒了,现在没人说了算。")
    world.close()

    again = open_world("w", redis=fresh_redis, world_file=path)
    assert again.world_setting()["text"] == "港务局倒了,现在没人说了算。"


# ── CLI 出口(真路径)────────────────────────────────────────────────────────


def test_world_setting_命令真的注册了_而且默认只读(open_world, fresh_redis, tmp_path):
    """⚠️ **先钉"存在且合法时退 0"** —— argparse 对一个**没注册**的子命令也返回 2,
    只钉拒绝那几条的话,命令根本不存在时测试会一路假绿(`test_character_card.py`
    那条纪律,逐字同一个坑)。

    顺带钉「不给开关就是只读」:一条会改东西的命令,它的"什么都不给"必须是安全
    的那一边(和 `world drop` 不带 `--yes` 只数同一条)。
    """
    from _worldfile import redis_for, run_cli

    client = redis_for(tmp_path / "cli.db")
    open_world("w", redis=client, world_file=_world_file(tmp_path)).close()
    done = run_cli("world", "setting", "--world-id", "w", "--json")
    assert done.returncode == 0, done.stderr
    import json as _json

    payload = _json.loads(done.stdout)
    assert payload["operation"] == "world setting" and payload["read_only"] is True
    assert payload["text"] == _SETTING


def test_world_setting_命令改得动_而且_set_file_读得进来(open_world, fresh_redis, tmp_path):
    """`--set-file` 不是可有可无的:世界观动辄上千字,而 Linux 的 `MAX_ARG_STRLEN`
    把单个 argv 元素封在 128 KiB —— 撞上去时报错的是操作系统,不是引擎。"""
    import json as _json

    from _worldfile import redis_for, run_cli

    client = redis_for(tmp_path / "cli.db")
    open_world("w", redis=client, world_file=_world_file(tmp_path)).close()

    long_text = "旧港区的雨下了四十年。" * 200
    src = tmp_path / "setting.txt"
    src.write_text(long_text, encoding="utf-8")
    done = run_cli("world", "setting", "--world-id", "w",
                   "--set-file", str(src), "--json")
    assert done.returncode == 0, done.stderr
    assert _json.loads(done.stdout)["changed"] is True

    again = run_cli("world", "setting", "--world-id", "w", "--json")
    assert _json.loads(again.stdout)["text"] == long_text


def test_world_setting_命令的四种拒绝(open_world, fresh_redis, tmp_path):
    """退出码 2 = 「我听懂了,但我不干」,而且拒绝时一个字都不写。"""
    from _worldfile import redis_for, run_cli

    client = redis_for(tmp_path / "cli.db")
    open_world("w", redis=client, world_file=_world_file(tmp_path)).close()
    base = ["world", "setting", "--world-id", "w"]
    for extra, why in [
        (["--set", "a", "--set-file", "-"], "--set 和 --set-file 一起给"),
        (["--set", "a", "--clear"], "--clear 和 --set 一起给"),
        (["--set", "   "], "一段空白"),
        (["--set-file", str(tmp_path / "没有这个文件")], "读不了那个文件"),
    ]:
        done = run_cli(*base, *extra)
        assert done.returncode == 2, f"{why} 该被拒绝:{done.stdout}"
    # 拒了这么多次,世界观一个字都没动。
    assert open_world("w", redis=client).world_setting()["text"] == _SETTING


def test_world_setting_不许在一个不存在的世界上当场创世(tmp_path):
    """和 `agent set-card` / `location set-image` 同一条:抄错名字不该建出一个新世界
    (`5ce6aed` 的教训)——那时你看到的是一份"排版正常"的默认世界观。"""
    from _worldfile import redis_for, run_cli

    client = redis_for(tmp_path / "empty.db")
    done = run_cli("world", "setting", "--world-id", "根本没有这个世界", "--json")
    assert done.returncode == 2, done.stdout
    assert list(client.scan_iter("anima:根本没有这个世界:*")) == []


def test_文档里承诺的每一条命令_真敲一遍(open_world, tmp_path):
    """**凡是文档里承诺了一句用户会照着敲的命令,就去敲一遍**(CLAUDE.md 那条)。

    REFERENCE §4.7.1 与 FOR-STUDIO §3.47 各印了一块 bash,里面五条命令。
    这条把那五条**逐条**敲过去 —— 一份 832 项全绿的测试套件曾经和一句
    `grep` 什么都找不到并存过,而坏的正是**可用性**。
    """
    import json as _json

    from _worldfile import redis_for, run_cli

    client = redis_for(tmp_path / "doc.db")
    open_world("w", redis=client, world_file=_world_file(tmp_path)).close()
    src = tmp_path / "setting.txt"
    src.write_text("港务局倒了。三号码头夜里有灯。", encoding="utf-8")

    def _say(*args):
        done = run_cli(*args)
        assert done.returncode == 0, f"{' '.join(args)} → {done.stderr}"
        assert done.stdout.strip(), f"{' '.join(args)} 什么都没印 —— 人敲完看不到结果"
        # 🔴 **stderr 也要干净**(2026-08-27 C 视角验收补上的)。
        # 上一版这条只看 rc 与 stdout,于是它**拦不住那个真 bug**:`world setting`
        # 把 `_warn_if_live` 写在了 `World.open()` **之后**,而 open 会把本进程登记
        # 成活人 —— 每写一次都印一句"有进程正在跑这个世界",报的是**它自己的 pid**,
        # 而那句话的内容("那个进程不会重读")说的也正是它自己。
        # **一条可用性判据不看 stderr,就看不见用户屏幕的一半。**
        return done.stdout

    base = ["world", "setting", "--world-id", "w"]
    assert _SETTING in _say(*base), "只读那一趟没把现在这段印出来"

    # 🔴 **头一次写,不许警告它自己**(2026-08-27 C 视角验收逮出来的真 bug)。
    # 病是 `_warn_if_live` 被写在了 `World.open()` **之后**,而 open 会把本进程
    # 登记成这个世界的活人 —— 于是那句"这个世界正被 pid N 跑着"报的是**自己的
    # pid**,而它警告的那个"不会重读配置的进程"也正是自己。兄弟出口 `config set`
    # 一直是先 warn 后开,照它修的。
    #
    # ⚠️ **判据只钉"第一次"**,而这一格的边界值得写清楚:`owner_pid` 写在 `:meta`
    # 上、**关世界时并不清**(`_warn_if_live` 的 docstring 自己就说"进程崩掉标记
    # 就陈旧,提示不是锁"),所以**后续**几次敲本来就会看见上一次留下的戳 ——
    # 那是这一族命令共有的旧行为,不是这一单引入的。把后面几次也钉上,钉住的
    # 会是一条**和这个 bug 无关**的旧账,而下一个人会拿它去改一个没坏的地方。
    #
    # ⚠️ **而"试牙要试对地方"在这一格上又咬了我一次**:这条断言第一版写的是
    # `"正在跑" not in stderr`,而那句话真正的字样是「这个世界正被 pid N @ host
    # 跑着」——"正在跑"三个字**根本不存在**,于是把 bug 放回去它照样绿。
    first_write = run_cli(*base, "--set", "旧港区,常年下雨。港务局说了算。")
    assert first_write.returncode == 0, first_write.stderr
    assert "正被 pid" not in first_write.stderr, (
        f"头一次写就在警告它自己 —— stderr:{first_write.stderr}")
    _say(*base, "--set-file", str(src))
    assert open_world("w", redis=client).world_setting()["text"] == src.read_text("utf-8")

    # ⚠️ **`--dry-run` 那一条要在 `--clear` 之前查**,否则这句断言是白写的:
    # 清完之后世界观本来就不含那段话,不管 --dry-run 有没有写过。
    _say(*base, "--set", "换一段", "--dry-run", "--json")
    assert open_world("w", redis=client).world_setting()["text"] == src.read_text("utf-8"), (
        "--dry-run 动了库")

    _say(*base, "--clear")
    assert open_world("w", redis=client).world_setting()["source"] == "默认值"


def test_contract_报得出这扇门_消费方按出口探测不比版本号(open_world, tmp_path):
    """`contract --json` 的 `seed` 段三格 —— 创作台照它决定"这支引擎带不带得动",
    而**同一个 3.3.0 下有过七份不同的引擎**,版本号比不出来。"""
    import json as _json

    from _worldfile import redis_for, run_cli

    redis_for(tmp_path / "c.db")
    seed = _json.loads(run_cli("contract", "--json").stdout)["seed"]
    assert seed["world_setting_read_command"] == "world setting"
    assert seed["world_setting_write_command"] == "world setting --set"
    assert "--clear" in seed["world_setting_write_gloss"], (
        "gloss 要说清 --clear 是回落不是清空 —— 读它的人正是拿它决定怎么调")
