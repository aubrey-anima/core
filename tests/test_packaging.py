"""The distribution contract: an installed anima-world can run and package.

Exercised through the public import surface only, so they fail if the wheel
ships a broken layout (missing package data, unreachable module, stale import
path). Authoring is NOT here any more — that moved to anima-studio, which
manages engine versions and therefore cannot live inside one.
"""
from __future__ import annotations

import json
import pathlib
from importlib import resources

import pytest

from _worldfile import bundled_seed

from anima_world.beats import BeatScript, BeatScriptError
from anima_world.world_file import read_world_file
from anima_world.world_package import (
    PackageValidationError,
    import_world_file,
    inspect_world_file,
)



def test_engine_ships_no_ui_at_all():
    """This package is an engine. The player surface belongs to the site, the
    admin console to the operator, and authoring to anima-studio — a wheel that
    grows an HTML file again means one of those boundaries slipped."""
    root = resources.files("anima_world")
    assert not (root / "world" / "static" / "index.html").is_file()
    assert not (root / "author").is_dir()


def test_the_chat_tools_subpackage_ships_and_registers_itself():
    """`anima_world/tools/` 是 1.3.0 新增的**子包**。`packages.find` 是自动发现,
    但一个漏进 wheel 的子目录只会在宿主环境里少文件 —— 而那时候的表现是
    "她一个能力都不会调",不是 ImportError。这条盯的就是那种沉默。"""
    assert (resources.files("anima_world") / "tools" / "social.py").is_file()

    from anima_world import tools

    registered = {spec.id for spec in tools.tools_for("*")}
    assert {"mute", "end_conversation", "delay_reply", "walk_away",
            "refuse_topic", "broadcast", "wait_for_user"} <= registered
    # 声明里必须带 params_schema:没有它,提示词菜单会告诉她一个无参能力。
    assert tools.get("mute").params_schema["minutes"]["required"] is True


@pytest.mark.parametrize("gone", ["anima_world.author", "anima_world.world"])
def test_the_removed_layers_are_not_importable_from_the_engine(gone):
    """`anima_world.author`(创作)和 `anima_world.world`(HTTP)都搬走了。

    留个 shim 就等于给工具开后门:创作台靠子进程驱动多个引擎版本,它一旦
    import 得到引擎,版本切换就是一句谎话。

    ⚠️ `world` 这条是补的,而漏掉它的方式很安静:源码删干净了,**目录还在**
    (只剩 `__pycache__` 和一个空 `static/`),于是 Python 把它当**命名空间包** ——
    `import anima_world.world` 照样成功,返回一个空模块。删源码不等于删模块。
    """
    with pytest.raises(ModuleNotFoundError):
        __import__(gone)



# ── 包格式 v3:一个文件、两层记录 ────────────────────────────────────────────
# 跨语言协作走文件,所以这几条同时是运维台镜像实现的规格。


def _a_world(tmp_path, name="demo.cyberworld", ticks=3):
    """一个真来源的 v3 包:fakeredis 上创世、跑几 tick、导出。"""
    import fakeredis

    from anima_world.api import World

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    with World.open("pkg-src", redis=client, force_mock_llm=True) as world:
        world.tick(ticks)
        package = tmp_path / name
        world.export_snapshot(package, world_id="demo-world", name="演示世界")
    return package


def test_内置演示世界随轮子一起发():
    """它是唯一的 package data。漏了会让宿主环境里少文件,而世界开不起来。"""
    seed = bundled_seed()
    assert len(seed["agents"]) >= 3 and len(seed["locations"]) >= 3
    assert all({"id", "name", "location", "personality"} <= set(a) for a in seed["agents"])


def test_导出再导入是同一个世界(tmp_path):
    import fakeredis

    from anima_world.api import World

    package = _a_world(tmp_path)
    assert package.is_file()

    target = fakeredis.FakeStrictRedis(decode_responses=True)
    manifest = import_world_file(package, redis=target, world_id="demo")
    assert manifest.world_id == "demo-world"   # 世系名;目标世界叫 demo
    with World.open("demo", redis=target, force_mock_llm=True) as world:
        assert world.scheduler.event_log.count() > 0
        assert world.scheduler.clock >= 3


def test_导入只进空世界(tmp_path):
    """半个旧世界叠上半个新世界,跑起来两边都对不上,而且没有地方会报错。"""
    import fakeredis

    package = _a_world(tmp_path)
    target = fakeredis.FakeStrictRedis(decode_responses=True)
    import_world_file(package, redis=target, world_id="demo")
    with pytest.raises(PackageValidationError) as excinfo:
        import_world_file(package, redis=target, world_id="demo")
    assert "空世界" in str(excinfo.value)


def test_包里带的是状态不是作者层(tmp_path):
    """一个跑过的世界导出来是**它此刻的样子**。v2 那份"出生证明"是同一份内容的
    第二种写法,而两份真相里有一份不更新是这个仓库最怕的坏法。"""
    package = _a_world(tmp_path)
    kinds = {r["kind"] for _, stream in [read_world_file(package)] for r in stream}
    assert "redis" in kinds
    assert "author" not in kinds


def test_封皮读得懂而不必跑得动(tmp_path):
    """最需要这个答案的调用方,正是那个还没有对的引擎的启动器 ——
    在这里因为"跑不了"而抛错,就违背了这个格式存在的意义。"""
    package = _a_world(tmp_path)
    payload = inspect_world_file(package)
    assert payload["world_id"] == "demo-world"
    assert payload["format_version"] == 3
    assert payload["runnable"] is True          # 是一个**字段**,不是一个异常
    assert payload["size_bytes"] > 0


def test_封皮把店面栏报全(tmp_path):
    """`inspect` 是运维台读作者填的店面栏的**唯一**来源,v3 起。

    v2 时代运维台从解包出来的 `manifest.json` 里读 name/summary/genre/setting/theme
    (platform `lib/enginePackage.js` 的 `readManifest`);v3 不再解包成目录,它改从
    `world inspect --json` 读同一批字段。少报一栏的下场不是报错 —— 是玩家看到的
    世界卡片上那一栏空着,而两边日志全是干净的(实测:一个 genre 填得好好的包
    导进运维台,目录里 `"genre": ""`)。所以这条钉的是**报全**,不是"报了几个"。
    """
    import fakeredis

    from anima_world.api import World

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    with World.open("pkg-src", redis=client, force_mock_llm=True) as world:
        package = tmp_path / "storefront.cyberworld"
        world.export_snapshot(package, world_id="storefront", name="灯塔湾",
                              summary="靠英语过活的小镇", genre="生活/语言学习",
                              setting="当代海边小镇", theme="seaside")
    payload = inspect_world_file(package)
    assert payload["name"] == "灯塔湾"
    assert payload["summary"] == "靠英语过活的小镇"
    assert payload["genre"] == "生活/语言学习"
    assert payload["setting"] == "当代海边小镇"
    assert payload["theme"] == "seaside"


def test_更新的格式版本读不了但说得清(tmp_path):
    import gzip
    import json as _json

    from anima_world.world_file import WorldFileError

    package = _a_world(tmp_path)
    lines = gzip.open(package, "rt", encoding="utf-8").read().splitlines()
    head = _json.loads(lines[0]); head["version"] = 99
    bad = tmp_path / "future.cyberworld"
    with gzip.open(bad, "wt", encoding="utf-8") as fh:
        fh.write("\n".join([_json.dumps(head, ensure_ascii=False), *lines[1:]]) + "\n")
    with pytest.raises(WorldFileError) as excinfo:
        inspect_world_file(bad)
    assert "99" in str(excinfo.value)


@pytest.mark.parametrize("flavour,expected", [
    ("checksum", "校验和"),
    ("not a world file", "读不动"),
    ("unknown record", "不认识的记录类型"),
])
def test_拒收要说清是哪一道闸(tmp_path, flavour, expected):
    """从前四类打印同一句"包无效或不可读"。运维台只能原样转述,所以它的 400 报文
    里也没有原因 —— 作者不知道该"换引擎重导"还是"修文件",只能瞎试。"""
    import gzip
    import json as _json

    import fakeredis

    from anima_world.world_file import WorldFileError

    package = _a_world(tmp_path)
    bad = tmp_path / "bad.cyberworld"
    if flavour == "checksum":
        lines = gzip.open(package, "rt", encoding="utf-8").read().splitlines()
        lines[1] = lines[1].replace("cafe", "cafF", 1)
        with gzip.open(bad, "wt", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    elif flavour == "unknown record":
        lines = gzip.open(package, "rt", encoding="utf-8").read().splitlines()
        with gzip.open(bad, "wt", encoding="utf-8") as fh:
            fh.write("\n".join([lines[0], '{"kind":"quantum_flux"}', *lines[1:-1]]) + "\n")
    else:
        # gzip 的魔数对上了,后面是垃圾 —— 最像"包在路上坏掉了"的那一种
        bad.write_bytes(b"\x1f\x8b" + "这不是一个世界文件".encode("utf-8"))

    target = fakeredis.FakeStrictRedis(decode_responses=True)
    with pytest.raises((WorldFileError, PackageValidationError, OSError)) as err:
        import_world_file(bad, redis=target, world_id="rejected")
    assert expected in str(err.value), f"{flavour} 的拒绝理由没透出来:{err.value!r}"


def test_包目录里除了代码只有内置世界():
    """`anima_world/` 里不许躺着别的世界数据 —— 一份跟着轮子发出去的世界,
    必须是有意为之的那一份。"""
    strays = [
        p.name for p in _package_dir().iterdir()
        if p.is_file() and p.suffix in {".json", ".cyberworld", ".db", ".sqlite"}
        and p.name != "demo.cyberworld"
    ]
    assert not strays, f"包目录里混进了世界数据:{strays}"


def test_beat_script_rejects_a_bad_script():
    """Authoring-time validation is a contract: bad beats must never reach a world."""
    with pytest.raises(BeatScriptError):
        BeatScript.from_data({"beats": [{"nonsense": True}]})


# ── 发布产物只装代码:测试、文档、世界数据都不许搭车 ──────────────────────


def _package_dir() -> pathlib.Path:
    return pathlib.Path(str(resources.files("anima_world")))


def test_package_directory_carries_no_tests():
    offenders = sorted(
        p.name for p in _package_dir().rglob("test_*.py") if "__pycache__" not in p.parts
    )
    assert offenders == [], f"测试不该住在发布包里:{offenders}"


def test_sdist_excludes_tests_and_world_data():
    """真构建一次 sdist,看清单——MANIFEST.in 的 prune 规则很容易写错却无声。

    setuptools 默认会把 tests/ 扫进 sdist;wheel 不受影响(它只装
    `[tool.setuptools.packages.find]` 找到的包),所以这里专门盯 sdist。

    ⚠️ **这条测试从前会替打包清单认罪**(2026-08-25 修)。`python -m build` 默认
    起一个**隔离**构建环境,而起那个环境要去 PyPI 下 `setuptools>=77` 与 `wheel` ——
    所以这条测试**从前是联网的**。2026-08-25 全量跑那趟它红了,红的原文是
    `Could not fetch URL https://pypi.org/simple/setuptools/`(SSL EOF + 读超时),
    而屏幕上打出来的那一行是 `FAILED …::test_sdist_excludes_tests_and_world_data`
    —— 读起来是"sdist 里混进了不该带的东西",而真相是**这台机器那一刻上不了网**。
    单独重跑 37 秒绿。和 `tests/test_autonomy.py` 那个 5 秒挂钟是同一族的病:
    **判据脆到会替被测的东西认罪,比没有这条判据更坏。**

    改法是**先严后松,而且分得清是哪一段坏的**:隔离环境起得来就照旧用它;起不来
    (输出里根本没走到 `Building sdist`)就退回 `--no-isolation`,拿这个 venv 里
    已经装好的 setuptools 构建 —— 清单该查的一格都不少。两条路都走不通才 skip,
    而且 skip 的话**明说是这台机器,不给打包的结论**。

    ⚠️ **而中间那一档在这台开发机上是死的,别以为它会走到**(2026-08-25 实测):
    `.venv/bin/python -c "import setuptools"` 是 `ModuleNotFoundError`
    (`pip list` 里只有 `build`),于是 `--no-isolation` 一开口就是
    `BackendUnavailable: Cannot import 'setuptools.build_meta'`,连 `Building sdist`
    都到不了。**这台机器上的真实行为是"联网就 pass、断网就 skip",没有中间档。**
    留着那一档仍然对(换一台 `pip install setuptools` 过的机器它就活了,而 CI 上装了),
    但**它此刻没有被任何一次运行证过** —— 想验它,先在这个 venv 里装一次 setuptools
    再断网跑。把"我写了一条退路"当成"这条退路走得通",是这个仓库最爱犯的那种错。
    """
    import pathlib
    import subprocess
    import sys
    import tarfile
    import tempfile

    build = pytest.importorskip("build", reason="需要 `pip install build` 才能验证 sdist")
    assert build  # 仅用于存在性检查

    root = pathlib.Path(__file__).resolve().parents[1]

    def _build(extra: list[str]) -> tuple[subprocess.CompletedProcess, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--sdist", "--outdir", out, str(root), *extra],
            capture_output=True, text=True,
        )
        return proc, f"{proc.stdout}\n{proc.stderr}"

    with tempfile.TemporaryDirectory() as out:
        proc, log = _build([])
        if proc.returncode != 0 and "Building sdist" not in log:
            # **一个字都没轮到构建** —— 坏在起隔离环境那一步(下 setuptools/wheel),
            # 那是网络不是清单。退回本地已装好的构建后端再问一次。
            proc, log = _build(["--no-isolation"])
            if proc.returncode != 0 and "Building sdist" not in log:
                pytest.skip(
                    "这台机器上 `python -m build` 起不起来:隔离环境下不到 "
                    "setuptools/wheel,--no-isolation 也没走到构建那一步。"
                    "**这不是打包清单的结论** —— 换一台连得上 PyPI 的机器,"
                    f"或先 `pip install -U setuptools wheel` 再看。\n{log}"
                )
        assert proc.returncode == 0, (
            "sdist **真的构建失败了**(已经走到 `Building sdist` 那一步,"
            f"所以这一条不是网络):\n{log}"
        )
        (tarball,) = pathlib.Path(out).glob("*.tar.gz")
        names = tarfile.open(tarball).getnames()

    assert not [n for n in names if "/tests/" in n or n.endswith("/tests")], "sdist 不许带 tests/"
    assert not [n for n in names if n.endswith((".db", ".db.key"))], "sdist 不许带世界数据"
    assert any(n.endswith("anima_world/demo.cyberworld") for n in names), (
        "demo.cyberworld 是唯一的 package data,漏了会让宿主环境少文件、世界开不起来"
    )


# ── 跨仓库契约:版本识别必须在 --no-deps 环境里可用 ──────────────────────────
# anima-studio 给每个引擎版本装一个隔离 venv、全程走子进程。识别一个引擎跑在
# 一次性的 --no-deps venv 里,所以识别一个版本的代价是一次下载而不是一整棵
# 依赖树。这依赖两件事:`__init__.py` 什么都不 import,`db.py` 只 import 标准库。
# 往 `__init__.py` 里加一句 `from anima_world.api import World`(极常见的便利
# 重导出)就会打断它 —— 而全量测试照样全绿,发布照样通过,坏的是别人已经
# 发布的工具。

def test_version_identification_needs_no_third_party_imports():
    """`__version__` 与 db 格式常量必须在没有依赖的环境里读得到。"""
    import subprocess
    import sys

    probe = (
        "import anima_world;"
        "print(anima_world.__version__)"
    )
    # -S 不加载 site-packages,所以第三方包在子进程里根本不存在 —— 这正是
    # --no-deps venv 的形状。开发机上依赖装得好好的,少了这层隔离,测试会
    # 恒绿、什么都守不住。cwd 设成仓库根,让 anima_world 从源码树可见。
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-S", "-c", probe],
        capture_output=True, text=True, cwd=repo_root,
    )
    assert result.returncode == 0, (
        "版本识别探针在无依赖环境里失败了 —— 多半是有人往 anima_world/__init__.py "
        f"里加了第三方 import:\n{result.stderr}"
    )
    assert result.stdout.strip()


def test_ticks_zero_initializes_a_world_without_running_it(monkeypatch):
    """跨仓库契约:`simulate --ticks 0` = 创世但不跑。

    这是唯一能无头建出世界的办法(没有 init/create 子命令),外部工具靠它创世
    再交给宿主。将来给 tick 数加一条"必须为正"的校验会悄悄拿掉这个能力。
    """
    import fakeredis

    from anima_world.__main__ import main

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr("anima_world.__main__._connect_redis", lambda url=None: client)

    assert main(["simulate", "--world-id", "init", "--ticks", "0", "--llm", "mock"]) == 0
    events = client.lrange("anima:init:events", 0, -1) or []
    parsed = [json.loads(e) for e in events]
    assert any(e.get("type") == "agent_join" for e in parsed), "创世应该播下角色"
    assert all(int(e.get("ts") or 0) == 0 for e in parsed), "--ticks 0 不该推进世界"

def test_事件在包里是一行一条(tmp_path):
    """**这条是被"演一遍用户故事"逮出来的,而且逮的是我自己宣传的那句话。**

    换成文本格式的全部理由是"能 grep、能 diff、能流式"。而无限增长的那四样一旦按
    它们此刻住在哪个后端来 dump,没接 MySQL 的世界就会把整段历史塞进**一个**
    `redis` list 记录里 —— 一整行几万字节的转义 JSON。那时
    `grep '"type": "entity_spawn"'` 找不到任何东西(它在字符串里是 `\\"type\\"`),
    `diff` 也退化成整块变。**在最常见的那种世界上不成立的卖点,就是空话。**

    ⚠️ **上面那个示范里冒号后的空格是承重的**:记录用 `json.dumps` 的默认分隔符
    写出去,所以 `'"type":"entity_spawn"'`(这一句从前就是这么写的)一条都匹配
    不到 —— 而 grep 找不到时退出码 1、屏幕空白,和"这个世界确实没生过东西"长得
    一模一样。下面那行断言真的照它敲一遍。

    所以四样一律按语义记录导出,不看后端。
    """
    import gzip

    package = _a_world(tmp_path, ticks=10)
    lines = gzip.open(package, "rt", encoding="utf-8").read().splitlines()

    events = [l for l in lines if '"kind": "event"' in l]
    assert len(events) > 5, "事件被塞进一个 redis 记录里了,grep 和 diff 都会废掉"
    assert not any('"key": "events"' in l for l in lines), (
        "还有一条 redis 记录装着整段事件日志"
    )
    # 每条事件自己一行 —— 这才是 grep 找得到的前提
    assert all(l.count('"kind": "event"') == 1 for l in events)
    # **照上面那句示范真敲一遍。** 冒号后那个空格是承重的,而写错了不报错:
    # grep 找不到时退出码 1、屏幕空白,和"这个世界确实没生过东西"一模一样。
    assert [l for l in lines if '"type": "agent_join"' in l], "示范里的 grep 串对不上真文件"
    assert not [l for l in lines if '"type":"agent_join"' in l], "少空格的那种写法不该匹配到"


def test_接不接_MySQL_导出来长得一样(tmp_path):
    """一份包能不能被 grep,不该取决于导出它的那台机器接没接 MySQL —— 那和世界无关。

    没接 MySQL 时四样住 Redis,接了住 MySQL;而**文件里它们必须是同一种记录**,
    否则"同一个世界"会有两种包格式,消费方要写两套读法。
    """
    import gzip

    package = _a_world(tmp_path, ticks=5)
    kinds = set()
    for line in gzip.open(package, "rt", encoding="utf-8"):
        import json as _json
        kinds.add(_json.loads(line)["kind"])
    # 这个世界没接 MySQL,但事件照样是 event 记录(而不是 redis list)
    assert "event" in kinds


def test_抹掉一个世界要先说清会抹掉多少(tmp_path):
    """**默认只数不删。** 一个打错的 `--world-id` 在这里的代价是抹掉另一个世界,
    而那不可逆 —— 所以"删"必须是显式的第二步,不是默认行为。"""
    import fakeredis

    from anima_world.api import World
    from anima_world.world_package import drop_world

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    with World.open("doomed", redis=client, force_mock_llm=True) as world:
        world.tick(3)

    counted = drop_world(client, "doomed")
    assert counted > 0
    assert list(client.scan_iter(match="anima:doomed:*")), "只数不该删掉任何东西"

    dropped = drop_world(client, "doomed", confirm=True)
    assert dropped == counted
    assert not list(client.scan_iter(match="anima:doomed:*"))


def test_抹掉一个世界不碰别的世界(tmp_path):
    """键前缀是这个引擎定义的形状,而"抹掉一个世界"必须**恰好**是那个前缀 ——
    一个 Redis 上跑十个世界是常态,多删一个字符就是别人的世界没了。"""
    import fakeredis

    from anima_world.api import World
    from anima_world.world_package import drop_world

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    for name in ("alpha", "alphabet"):      # 前缀是另一个的真前缀 —— 最容易多删的形状
        with World.open(name, redis=client, force_mock_llm=True) as world:
            world.tick(1)

    drop_world(client, "alpha", confirm=True)
    assert not list(client.scan_iter(match="anima:alpha:*"))
    assert list(client.scan_iter(match="anima:alphabet:*")), "把另一个世界一起抹了"


# ── 许可:装进分发物里的那句话 ────────────────────────────────────────────────
# 这两条不建包、不联网、不碰 Redis —— 纯文本比对。理由见下面各自的 docstring:
# 它们要挡的那个 bug 已经真的发生过一次,而当时全量 1839 项一条都没红。


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def test_装进包的许可声明和印在_PyPI_上的说同一版():
    """`NOTICE` 与 `README.md` 必须报同一个「Apache 到哪一版为止」。

    **这条闸挡的是一个真发生过、而且发生了两次的错。** 2026-08-19 查出四份文档把
    Apache 的截止版本写少了一版(写 1.3.0,而 `v1.4.0` 也是 Apache 并且真的上了
    PyPI),于是跑遍了 README / CLAUDE.md / FOR-STUDIO / 2.0.0 那节 CHANGELOG ——
    **`NOTICE` 不在那四份里**,它一直写着 1.3.0 直到 2026-08-26 发版前。

    为什么单挑这两份来对:**它们是仅有的两份会离开这个仓库的许可文本。**
    `NOTICE` 随 wheel 与 sdist 装进分发物(`license-files`),`README.md` 是
    `Description-Content-Type: text/markdown` 的正文 —— 也就是 PyPI 项目页上那段字。
    其余几份写错了只是仓库里的话,这两份写错了是**发出去的话**。

    ⚠️ 这条闸认的是那两个句式。改写句子会让它红 —— **那是有意的**:一句对外的
    许可声明被重写时,值得有人再读一遍它说的是不是真的。
    """
    import re

    root = _repo_root()
    notice = (root / "NOTICE").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    m_notice = re.search(
        r"Releases up to and including (\d+\.\d+\.\d+) were published under the Apache",
        notice,
    )
    m_readme = re.search(
        r"直到 (\d+\.\d+\.\d+),PyPI 上发出的每一个版本都是 Apache-2\.0",
        readme,
    )
    assert m_notice, (
        "`NOTICE` 里那句 Apache 声明不见了或被改写了。它是**装进 wheel 的**那份许可"
        "声明 —— 改写之前请确认新句子说的仍然是真的,然后把这条闸的句式一起改。"
    )
    assert m_readme, (
        "`README.md` 里那句 Apache 声明不见了或被改写了。README 就是 PyPI 项目页"
        "正文 —— 同上。"
    )
    assert m_notice.group(1) == m_readme.group(1), (
        f"两份**会发出去**的许可声明说的不是同一版:"
        f"NOTICE 说到 {m_notice.group(1)} 为止,README 说到 {m_readme.group(1)} 为止。"
        f"许可这种事不能靠约等于,尤其当差的那一版恰好是用户 `pip install` 装到的那一版。"
    )


def test_许可这件事三个地方说的是同一件事():
    """`pyproject.toml` 的 SPDX 串、`license-files` 清单、`LICENSE` 正文,三者对得上。

    元数据里写 `license = "AGPL-3.0-or-later"` 而 `LICENSE` 文件里躺着别的许可,
    **今天没有任何一处会报错** —— `twine check` 不读 `LICENSE` 的正文,PyPI 也不读。
    页面上会照 SPDX 串印一个许可名,而随包发出去的是另一份文本。

    `license-files` 那一格同样承重:漏掉一个名字,那个文件就**不进 wheel 的
    `dist-info/licenses/`**,而包照样构建成功、`twine check` 照样 PASSED。
    """
    import tomllib

    root = _repo_root()
    with (root / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)

    spdx = cfg["project"]["license"]
    assert spdx == "AGPL-3.0-or-later", f"pyproject 的许可串变成了 {spdx!r}"

    declared = cfg["project"]["license-files"]
    assert sorted(declared) == ["LICENSE", "NOTICE"], (
        f"`license-files` 是 {declared!r} —— 少一个名字,那个文件就不进 "
        f"wheel 的 dist-info/licenses/,而构建与 `twine check` 都不会说一个字。"
    )
    for name in declared:
        assert (root / name).is_file(), f"`license-files` 里的 {name} 不存在"

    head = (root / "LICENSE").read_text(encoding="utf-8")[:400].upper()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in head and "VERSION 3" in head, (
        "`pyproject.toml` 说这个包是 AGPL-3.0-or-later,而 `LICENSE` 的正文不是 "
        "AGPL v3 —— 元数据上那个名字和随包发出去的那份文本是两样东西,"
        "没有任何工具会替你对这一格。"
    )
