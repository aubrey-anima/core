"""The distribution contract: an installed anima-world can run and package.

Exercised through the public import surface only, so they fail if the wheel
ships a broken layout (missing package data, unreachable module, stale import
path). Authoring is NOT here any more — that moved to anima-studio, which
manages engine versions and therefore cannot live inside one.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
from importlib import resources

import pytest

from anima_world.beats import BeatScript, BeatScriptError
from anima_world.world_package import (
    export_world_package,
    import_world_package,
    inspect_world_package,
)
from anima_world.world_seed import is_valid_world_seed, world_seed_errors


def _bundled_seed() -> dict:
    """The world seed shipped inside the package (not a repo-relative path)."""
    return json.loads(
        (resources.files("anima_world") / "world_seed.json").read_text(encoding="utf-8")
    )


def test_bundled_seed_is_shipped_and_valid():
    assert is_valid_world_seed(_bundled_seed())


# 每条都是"这份数据合不合法"的裁决,与运维台 lib/worldSeed.js 的镜像一一对应。
# `world_seed_errors` 是后加的解释层,只准解释、不准改判 —— 改判就是单方面
# 改了跨仓库契约,而契约互验(operator 的 test/contract.test.js)未必当场发现。
SEED_VERDICTS = [
    ({"agents": [], "locations": []}, True),
    ({"agents": [{"id": "a", "name": "A", "location": "l", "personality": "p"}],
      "locations": [{"id": "l", "name": "L", "description": "d"}]}, True),
    (None, False),
    ([], False),
    ({"agents": []}, False),
    ({"locations": []}, False),
    ({"agents": {}, "locations": []}, False),
    ({"agents": [{"id": "a", "name": "A", "location": "l"}], "locations": []}, False),
    ({"agents": ["not-a-dict"], "locations": []}, False),
    ({"agents": [], "locations": [{"id": "l", "name": "L"}]}, False),
    ({"agents": [], "locations": [42]}, False),
]


@pytest.mark.parametrize("data, valid", SEED_VERDICTS)
def test_seed_verdict_is_unchanged_by_the_explanation_layer(data, valid):
    assert is_valid_world_seed(data) is valid
    assert bool(world_seed_errors(data)) is (not valid), (
        "解释层的判定必须与 is_valid_world_seed 完全一致"
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


def test_authoring_is_not_importable_from_the_engine():
    """`anima_world.author` moved out. Leaving a shim would let the studio
    import the engine again, which is exactly what makes version switching a
    lie — the studio drives engines as subprocesses, never as imports."""
    with pytest.raises(ModuleNotFoundError):
        __import__("anima_world.author")


def test_template_export_import_roundtrip(tmp_path):
    seed_path = tmp_path / "world_seed.json"
    seed_path.write_text(json.dumps(_bundled_seed(), ensure_ascii=False), encoding="utf-8")
    package = tmp_path / "demo.cyberworld"

    manifest = export_world_package(
        seed_path=seed_path,
        output_path=package,
        world_id="demo-world",
        name="演示世界",
        mode="template",
        summary="roundtrip fixture",
    )
    assert manifest.world_id == "demo-world"
    assert manifest.export_mode == "template"
    assert package.is_file()

    # Inspecting must not need to unpack into an instance.
    assert inspect_world_package(package).revision_id == manifest.revision_id

    imported = import_world_package(package, tmp_path / "instances")
    assert imported.world_id == "demo-world"
    assert (imported.path / "world_seed.json").is_file()
    # A template carries no database — the world builds one on first boot.
    assert not (imported.path / "world.db").exists()


def test_snapshot_export_carries_the_live_database(tmp_path):
    from anima_world.db import open_db

    db_path = tmp_path / "world.db"
    open_db(db_path).close()
    seed_path = tmp_path / "world_seed.json"
    seed_path.write_text(json.dumps(_bundled_seed(), ensure_ascii=False), encoding="utf-8")
    package = tmp_path / "snap.cyberworld"

    export_world_package(
        seed_path=seed_path,
        db_path=db_path,
        output_path=package,
        world_id="snap-world",
        name="快照世界",
        mode="snapshot",
    )
    imported = import_world_package(package, tmp_path / "instances")
    assert (imported.path / "world.db").is_file()


# ── 包格式:envelope 谁都读得懂,拒绝要说人话 ──────────────────────────────
# 跨语言协作走文件,所以这几条同时是运维台 lib/worldPackage.js 的镜像规格。


def _repack(source: pathlib.Path, target: pathlib.Path, mutate) -> pathlib.Path:
    """Rebuild an archive after `mutate` edits its members, checksums included.

    Checksums are recomputed, so a package built here fails for exactly the
    reason under test and not incidentally as "checksum mismatch".
    """
    import hashlib
    import zipfile

    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    mutate(members)
    members["checksums.json"] = (
        json.dumps(
            {
                "algorithm": "sha256",
                "files": {
                    name: {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
                    for name, raw in members.items()
                    if name != "checksums.json"
                },
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n"
    ).encode()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            archive.writestr(name, members[name])
    return target


def _a_template(tmp_path: pathlib.Path, name: str = "demo.cyberworld") -> pathlib.Path:
    seed_path = tmp_path / "world_seed.json"
    seed_path.write_text(json.dumps(_bundled_seed(), ensure_ascii=False), encoding="utf-8")
    package = tmp_path / name
    export_world_package(
        seed_path=seed_path, output_path=package,
        world_id="demo-world", name="演示世界", mode="template",
    )
    return package


def _needing_engine(package: pathlib.Path, target: pathlib.Path, minimum: str, maximum: str):
    def mutate(members):
        manifest = json.loads(members["manifest.json"])
        manifest["engine_compat"] = {"minimum": minimum, "maximum_exclusive": maximum}
        members["manifest.json"] = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()

    return _repack(package, target, mutate)


def test_manifest_is_readable_by_an_engine_that_cannot_run_it(tmp_path):
    """一个包必须能回答"我需要什么引擎",问的人恰恰是装不了那个引擎的人。

    `.cyberworld` 的全部意义就是在引擎不匹配的机器之间搬运。从前
    `from_dict` 会先跑引擎区间校验再返回,于是区间外的引擎连 world_id 都读
    不到 —— 只能把版本号从异常的**消息文本**里正则抠出来,或者干脆自己解压
    读 manifest,而后者会把包格式的知识复制出这个仓库。
    """
    from anima_world.world_package import read_package_manifest

    future = _needing_engine(_a_template(tmp_path), tmp_path / "v2.cyberworld", "2.0.0", "3.0.0")

    manifest = read_package_manifest(future)
    assert manifest.world_id == "demo-world"
    assert manifest.name == "演示世界"
    assert manifest.export_mode == "template"
    assert manifest.engine_min == "2.0.0"
    assert manifest.runs_on("2.4.1") and not manifest.runs_on("1.0.2")
    assert manifest.compatibility()["runnable"] is False


def test_world_inspect_answers_for_an_incompatible_package(tmp_path):
    """`world inspect` 对跑不了的包必须**回答**,退出码 0 —— 拒绝就没意义了。"""
    import subprocess
    import sys

    future = _needing_engine(_a_template(tmp_path), tmp_path / "v2.cyberworld", "2.0.0", "3.0.0")
    result = subprocess.run(
        [sys.executable, "-m", "anima_world", "world", "inspect", str(future), "--json"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    # 第三方启动器按这组字段编码,少一个就是单方面改契约(REFERENCE §8)。
    for field in (
        "world_id", "name", "export_mode", "engine_min", "engine_max_exclusive",
        "source_engine_version", "package_format_version", "current_engine_version", "runnable",
    ):
        assert field in payload, f"inspect --json 少了字段 {field}"
    assert payload["runnable"] is False
    assert payload["engine_min"] == "2.0.0"


def test_world_inspect_still_refuses_an_unreadable_package(tmp_path):
    """放宽的只是"这个引擎跑不跑得了",不是校验本身。"""
    import subprocess
    import sys

    junk = tmp_path / "junk.cyberworld"
    junk.write_text("not a zip")
    result = subprocess.run(
        [sys.executable, "-m", "anima_world", "world", "inspect", str(junk)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "ZIP" in result.stderr


def test_template_travels_within_a_major_but_a_snapshot_does_not(tmp_path):
    """模板只装 world_seed.json —— 版本中立的作者数据,不该按 db 的规矩钉版。

    快照带着盖了格式戳的 world.db,地板是导出它的那个引擎,天经地义;模板
    盖同一个章,代价就从"存档带不走"变成"作品带不走",而后者从没有人决定过。
    """
    from anima_world.db import open_db
    from anima_world.world_package import _engine_version, _version_tuple

    major = _version_tuple(_engine_version())[0]
    seed_path = tmp_path / "world_seed.json"
    seed_path.write_text(json.dumps(_bundled_seed(), ensure_ascii=False), encoding="utf-8")

    template = export_world_package(
        seed_path=seed_path, output_path=tmp_path / "t.cyberworld",
        world_id="t-world", name="模板", mode="template",
    )
    assert template.engine_min == f"{major}.0.0"
    assert template.runs_on(f"{major}.0.0"), "同大版本的老引擎必须收得下"

    db_path = tmp_path / "world.db"
    open_db(db_path).close()
    snapshot = export_world_package(
        seed_path=seed_path, db_path=db_path, output_path=tmp_path / "s.cyberworld",
        world_id="s-world", name="快照", mode="snapshot",
    )
    assert snapshot.engine_min == _engine_version()


REJECTIONS = [
    ("engine range", "engine >= 2.0.0"),
    ("seed schema", "missing 'personality'"),
    ("checksum", "checksum mismatch"),
    ("not a zip", "ZIP"),
]


@pytest.mark.parametrize("flavour, expected", REJECTIONS)
def test_a_rejected_package_says_which_thing_is_wrong(tmp_path, flavour, expected):
    """拒收要说人话:校验和 / 引擎区间 / 种子 schema / 压缩炸弹防护,四类各说各的。

    从前四类打印同一句 "invalid or inaccessible package data"。运维台只能原样
    转述引擎的话,所以它的 400 报文里也没有原因,作者不知道该"换 core 重导"
    还是"修种子",只能瞎试(平台契约 §7 挂名的已知缺口)。退出码仍是 2。
    """
    import subprocess
    import sys

    package = _a_template(tmp_path)
    if flavour == "engine range":
        target = _needing_engine(package, tmp_path / "bad.cyberworld", "2.0.0", "3.0.0")
    elif flavour == "seed schema":
        def drop_a_key(members):
            seed = json.loads(members["world_seed.json"])
            seed["agents"][0].pop("personality")
            members["world_seed.json"] = (
                json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()

        target = _repack(package, tmp_path / "bad.cyberworld", drop_a_key)
    elif flavour == "checksum":
        # 改字节但**不**重算校验和 —— 这正是"包坏了,重传没用"那一类。
        import zipfile

        target = tmp_path / "bad.cyberworld"
        with zipfile.ZipFile(package) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members["world_seed.json"] = members["world_seed.json"].replace(b"cafe", b"cafF", 1)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(members):
                archive.writestr(name, members[name])
    else:
        target = tmp_path / "bad.cyberworld"
        target.write_text("not a zip at all")

    result = subprocess.run(
        [sys.executable, "-m", "anima_world", "world", "import", str(target),
         "--destination", str(tmp_path / "instances")],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert expected in result.stderr, (
        f"{flavour} 的拒绝理由没有透出来,作者不知道该修哪样:{result.stderr!r}"
    )


def test_beat_script_rejects_a_bad_script():
    """Authoring-time validation is a contract: bad beats must never reach a world."""
    with pytest.raises(BeatScriptError):
        BeatScript.from_data({"beats": [{"nonsense": True}]})


# ── 发布产物只装代码:测试、文档、世界数据都不许搭车 ──────────────────────


def _package_dir() -> pathlib.Path:
    return pathlib.Path(str(resources.files("anima_world")))


def test_package_directory_carries_no_world_data():
    """`anima_world/` 里除了代码只允许有 world_seed.json。

    一个 world.db 是某个人的世界(还带着 Fernet 密钥能解开的 llm.api_key),
    掉进包目录就会被 package-data 或 sdist 扫进发行包。这条测试盯着实际的
    包目录,而不是盯打包声明——声明对了但文件躺在那里同样会出事。
    """
    strays = sorted(
        p.name
        for p in _package_dir().rglob("*")
        if p.is_file()
        and p.suffix not in {".py", ".pyc", ".typed"}
        and p.name != "world_seed.json"
        and "__pycache__" not in p.parts
    )
    assert strays == [], f"包目录里混进了非代码文件:{strays}"


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
    assert any(n.endswith("anima_world/world_seed.json") for n in names), (
        "world_seed.json 是唯一的 package data,漏了会让宿主环境少文件"
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
        "from anima_world.db import DB_FORMAT_VERSION, MIN_SUPPORTED_DB_FORMAT;"
        "print(anima_world.__version__, DB_FORMAT_VERSION, MIN_SUPPORTED_DB_FORMAT)"
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
        f"或 db.py 里加了第三方 import:\n{result.stderr}"
    )
    version, fmt, floor = result.stdout.split()
    assert version and int(fmt) >= 1 and int(floor) >= 1


def test_ticks_zero_initializes_a_world_without_running_it(tmp_path):
    """跨仓库契约:`simulate --ticks 0` = 建库但不跑。

    这是唯一能无头建出世界文件的办法(没有 init/create 子命令),外部工具
    靠它建库再交给宿主。将来给 tick 数加一条"必须为正"的校验 —— 一个看起来
    完全合理的加固 —— 会悄悄拿掉这个能力。
    """
    from anima_world.__main__ import main

    db = tmp_path / "w.db"
    assert main(["simulate", "--db-path", str(db), "--ticks", "0", "--llm", "mock"]) == 0
    assert db.exists()

    conn = sqlite3.connect(db)
    try:
        joins = conn.execute("SELECT count(*) FROM events WHERE type='agent_join'").fetchone()[0]
        ticked = conn.execute("SELECT count(*) FROM events WHERE ts > 0").fetchone()[0]
    finally:
        conn.close()
    assert joins > 0, "建库应该播下创世角色"
    assert ticked == 0, "--ticks 0 不该推进世界"
