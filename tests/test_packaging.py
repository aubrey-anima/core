"""The distribution contract: an installed anima-world can run and package.

Exercised through the public import surface only, so they fail if the wheel
ships a broken layout (missing package data, unreachable module, stale import
path). Authoring is NOT here any more — that moved to anima-studio, which
manages engine versions and therefore cannot live inside one.
"""
from __future__ import annotations

import json
from importlib import resources

import pytest

from anima_world.beats import BeatScript, BeatScriptError
from anima_world.world_package import (
    export_world_package,
    import_world_package,
    inspect_world_package,
)
from anima_world.world_seed import is_valid_world_seed


def _bundled_seed() -> dict:
    """The world seed shipped inside the package (not a repo-relative path)."""
    return json.loads(
        (resources.files("anima_world") / "world_seed.json").read_text(encoding="utf-8")
    )


def test_bundled_seed_is_shipped_and_valid():
    assert is_valid_world_seed(_bundled_seed())


def test_engine_ships_no_ui_at_all():
    """This package is an engine. The player surface belongs to the site, the
    admin console to the operator, and authoring to anima-studio — a wheel that
    grows an HTML file again means one of those boundaries slipped."""
    root = resources.files("anima_world")
    assert not (root / "world" / "static" / "index.html").is_file()
    assert not (root / "author").is_dir()


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


def test_beat_script_rejects_a_bad_script():
    """Authoring-time validation is a contract: bad beats must never reach a world."""
    with pytest.raises(BeatScriptError):
        BeatScript.from_data({"beats": [{"nonsense": True}]})


# ── 发布产物只装代码:测试、文档、世界数据都不许搭车 ──────────────────────


def _package_dir() -> "pathlib.Path":
    import pathlib

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
