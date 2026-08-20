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
    """
    import pathlib
    import subprocess
    import sys
    import tarfile
    import tempfile

    build = pytest.importorskip("build", reason="需要 `pip install build` 才能验证 sdist")
    assert build  # 仅用于存在性检查

    root = pathlib.Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as out:
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--sdist", "--outdir", out, str(root)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"sdist 构建失败:\n{proc.stdout}\n{proc.stderr}"
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
