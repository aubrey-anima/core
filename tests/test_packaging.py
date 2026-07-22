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
