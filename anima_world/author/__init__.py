"""World creator (author) surface — the toolkit for authoring a world's content
and packaging it into a self-contained ``.cyberworld`` unit.

Where the *player* surface lets people live in a world and the *operator*
surface (``anima_platform``) deploys and runs one, the *author* surface is what
a world creator uses to build the thing in the first place. It ships inside the
``anima-world`` distribution so one install can both create a world and package
it:

- **Seed authoring** — the roster, locations, relationships and personas that a
  fresh world boots from (``world_seed.json``); validate with
  :func:`is_valid_world_seed`.
- **Beat authoring** — the exogenous story beats / foreshadow-payoffs that get
  fired into a running world (``beats.json``); parse/validate with
  :class:`BeatScript` (raises :class:`BeatScriptError` on a bad script).
- **Packaging** — bundle seed + optional snapshot db + beats into a portable,
  checksummed ``.cyberworld`` archive with :func:`export_world_package`, and
  inspect one without importing it via :func:`inspect_world_package`.
- **Novel Studio** — upload a novel text and derive an opening-state world seed
  through a two-pass pipeline: :func:`split_novel` cuts the text into chapters,
  :func:`run_scan` runs each chapter through an LLM to build a character/location
  dossier in an :class:`AuthorStore`, :func:`run_distill` condenses those dossiers
  into per-agent personalities and memories, and :func:`assemble_seed` packages
  the result as a rich seed JSON ready for ``anima-world simulate --seed``
  or ``anima-world world export``.  The whole workflow is served as a browser UI
  by ``anima-author serve`` (backed by :func:`create_author_app`).

The package *format* itself (export/import, checksums, engine-version gating)
lives in :mod:`anima_world.world_package`; it is shared infrastructure — the
operator surface imports packages a creator produced. This module is the
creator-facing front door onto that toolkit, kept in one place so the authoring
workflow is discoverable as a single surface.

CLI entry points for the same toolkit: ``anima-world world export`` /
``anima-world world import`` (packaging) and ``anima-world simulate --seed ...
--beats ...`` (dry-running an authored world headlessly).
"""

from __future__ import annotations

from anima_world.author.app import create_author_app
from anima_world.author.distill import assemble_seed, run_distill
from anima_world.author.extract import run_scan, split_novel
from anima_world.author.generate import SeedGenerationError, generate_world_seed
from anima_world.author.store import AuthorStore
from anima_world.beats import BeatScript, BeatScriptError
from anima_world.world_package import (
    PACKAGE_FORMAT_VERSION,
    PackageValidationError,
    WorldPackageManifest,
    export_world_package,
    import_world_package,
    inspect_world_package,
)
from anima_world.world_seed import is_valid_world_seed

__all__ = [
    # Studio entry points (novel → seed pipeline)
    "AuthorStore",
    "assemble_seed",
    "create_author_app",
    "run_distill",
    "run_scan",
    "split_novel",
    # Existing seed / beat / packaging helpers
    "BeatScript",
    "BeatScriptError",
    "SeedGenerationError",
    "generate_world_seed",
    "PACKAGE_FORMAT_VERSION",
    "PackageValidationError",
    "WorldPackageManifest",
    "export_world_package",
    "import_world_package",
    "inspect_world_package",
    "is_valid_world_seed",
]
