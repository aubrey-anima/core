# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versioning is **not** plain semver: the major version *is* the db format version. A world
file is pinned to the engine version that produced it, and there is no cross-version
migration — hosts install one version and depend on that version.

- **Major** — the database format changed. Existing worlds are not readable.
- **Minor** — new capability, same database format.
- **Patch** — fixes, same database format.

`tests/test_version_contract.py` enforces the major/db-format relationship mechanically,
and `db.py` enforces it again at runtime: mounting an incompatible world file is refused
on the spot rather than silently written to.

## [Unreleased]

## [1.0.2] — 2026-07-23

Same db format (**1**) and package format (**1**) as 1.0.0/1.0.1. Worlds built by either
open unchanged. Theme: **the db is whole the instant a player touches the world** — no
more "close the world first to get a complete file".

### Added

- **`World.export_snapshot()` — live export.** Package a running world into a
  `.cyberworld` snapshot without stopping it: checkpoints are flushed first, the db is
  copied under the world lock via the SQLite backup API (ticks are blocked only for the
  copy itself), and packaging happens outside the lock. Secrets are stripped the moment
  the copy lands. The exported seed resolves explicit `seed_path` → the genesis seed
  recorded in `db_meta` → the bundled seed (with a warning).
- **Genesis-seed provenance.** First boot into an empty database now records the seed it
  was born from in `db_meta` (`world_seed`), so a snapshot always carries its true birth
  certificate. Empty-db-only, like every other seeding step; pre-1.0.2 databases simply
  lack the row. Additive row in an existing table — not a format change.

### Fixed

- **Interaction moments now flush the lazy checkpoints.** `record_chat_turn`,
  `player_action`, `player_buy`, and `close_conversation` write the needs / reflection
  watermark / clock checkpoints on the spot instead of waiting for day rollover or
  shutdown. A crash (or a live export) right after a player interaction no longer loses
  the quiet-tail clock or the day's needs drift for that moment.
- **Orphaned conversations are recovered at open.** A crash between
  `start_conversation` and the close inside `record_chat_turn` used to leave the
  conversation `open` forever (embedded hosts without the idle reaper never closed it,
  and its one `conversation` event was never emitted). `World.open` now sweeps all open
  conversations — messages were already durable, so the summary and event are generated
  late instead of lost.

## [1.0.1] — 2026-07-23

Same db format (**1**) and package format (**1**) as 1.0.0. Worlds built by 1.0.0 open
unchanged.

### Fixed

- **Reopening a world registered the bundled demo cast instead of its own agents**
  ([#1]). The roster was built from the seed file on every boot, and the seed file
  defaults to the bundled `world_seed.json` when `--seed` is absent. So a database that a
  host seeded and shipped came back up running 苏晚夏 / 陆知遥 / 沈亦柔 — the world's own
  agents never ticked again, while the three strangers appended `narrative`,
  `state_change`, and `agent_action` events to it permanently. Nothing warned; the output
  looked healthy. This hit the documented workflow (`simulate --seed … --ticks 0`, then
  `run`). A non-empty database is now the authority on its own cast, rebuilt from its
  genesis `agent_join` events.

  The related `--seed was NOT applied` warning was also misleading: passing `--seed` was
  the only way to get the right cast, so the one workaround that worked told you that you
  had done it wrong.

- **A db-format mismatch surfaced as an uncaught traceback** ([#5]). `DBFormatError` is
  the outcome the whole version model exists to produce, and it was the only one of the
  three user-facing precondition failures the CLI did not catch. It now prints one line
  and exits 2, like `BeatScriptError` and `WorldSeedError`. The message also names the
  engine to install (`install a 2.x engine to open this world`) rather than leaving the
  reader to derive it from the version policy.

### Added

- `anima-world --version`, reporting the engine version plus the db and package format
  versions ([#5]). For an engine whose headline contract is "the version *is* the
  compatibility promise", self-report should not have been missing.
- Event `payload` field reference in [docs/REFERENCE.md](docs/REFERENCE.md) §2.1, with a
  stability note ([#7]). Hosts are told to read the `events` table directly for full
  history; until now they had to reverse-engineer the fields.
- Tests pinning three cross-repo contracts that previously held by accident ([#2]):
  `__init__.py` / `db.py` importing only the standard library (version identification
  runs in `--no-deps` virtualenvs), the db-format constants being externally read at
  their import paths, and `simulate --ticks 0` meaning "initialize and stop". All three
  are now documented in [CONTRIBUTING.md](CONTRIBUTING.md).

### Changed

- `Development Status` classifier from Alpha to Beta ([#7]) — it contradicted the
  add-only API promise and the mechanically-enforced version contract.
- [docs/ROADMAP.md](docs/ROADMAP.md) now says up front that its v2.0–v5.0 predictions
  shipped inside 1.0.0 ([#7]). It was written before the release and read as though
  memory 2.0 were still unimplemented.

[#1]: https://github.com/aubrey-anima/core/issues/1
[#2]: https://github.com/aubrey-anima/core/issues/2
[#5]: https://github.com/aubrey-anima/core/issues/5
[#7]: https://github.com/aubrey-anima/core/issues/7

## [1.0.0] — 2026-07-23

First public release. db format **1**, package format **1**.

Everything before this release lives in git history rather than here; the engine went
through several db-format generations during development (memory 2.0, needs, economy,
social each landed as their own format bump) and they were collapsed into a single
format 1 for the first release. Those worlds never left the machines they were built on,
so there is nothing to migrate.

### The engine

- **Event-sourced world core.** An append-only event log is the only source of truth.
  Balances, relationships, locations, and the narrative log are projections of it. There
  is no snapshot table — an earlier one was removed because it wrote back drifted
  balances.
- **Tick-driven scheduler** with the system's single `RLock`, guarding the world clock,
  the projection, and the mailbox.
- **Behavior-tree agents** with an urgency band, a free-time planner, and an action table
  that lives in the database rather than in code.
- **LLM off the tick thread.** Narration, planning, and relationship judging run on
  separate thread pools; the client is injected. A world with no API key runs on
  templates instead of stalling.
- **`World` facade** (`anima_world.api`) — open, drive the clock, read state, chat,
  record turns, move players, hot-edit config. This is the interface host applications
  depend on, and it is add-only from here.
- **Chat subsystem decoupled from the event core.** A whole session emits exactly one
  world event, at close. The world receives only the current turn's bounded history; the
  full transcript stays in the host application.

### Subsystems

- **Memory 2.0** (always on) — retrieval scored on relevance × recency × importance,
  periodic reflection that writes higher-order memories, and a forgetting curve.
- **Needs** (`needs.enabled`, default off) — `energy` / `hunger` / `social` decay per tick
  and drive the behavior tree's urgency band. Checkpointed at day boundaries and on close.
- **Economy** (`economy.enabled`, default off) — items, money, shops, wages, price drift.
  The ledger is a projection of `payment` events.
- **Social** (`social.enabled`, default off) — gossip that propagates second-hand with
  per-hop confidence decay, and emergent cliques. Three-axis relationships are always on.

### Distribution

- **`.cyberworld` packages** — export a world as a template (seed only, builds itself on
  first boot) or a snapshot (a database that has already lived), and import either.
- **CLI**: `start` (guided create + run), `doctor` (health check including a real LLM
  call), `config` (encrypted secrets, masked on read), `run` (foreground host, no
  onboarding), `simulate` (headless fast-forward), `world` (package export/import).
- **Encrypted secrets at rest.** `llm.api_key` is Fernet-encrypted; the key material
  lives in `<db>.key` and must travel with the database.

### Fixed before release

- **The world clock was not persisted.** It was restored as `max(event timestamp)`, so
  every stretch of ticks that produced no event — most of the night — was silently
  discarded on close. A world reported at tick 350 reopened at 320, and the deficit was
  permanent. Now checkpointed to `db_meta` alongside the other data-plane state.
- **An explicitly named seed file that could not be read degraded silently** to the
  built-in demo world. Because a seed is read once into an empty database, a typo in
  `--seed` produced the wrong world permanently. Authored seeds now raise `WorldSeedError`
  with per-field detail; only the bundled seed still falls back.
- **`config --db-path` only worked before the subcommand**, unlike every other command,
  and failed with a bare top-level usage error. Both positions now work.
- **`doctor`'s fix hint omitted `--db-path`**, so a user with a world at a custom path who
  copied the suggested command created a second, empty world at the default path and
  wrote the key into that one.
- Two gossip bugs: a dead branch and a non-reproducible dice roll.

### Removed before release

- **The HTTP layer.** Three REST API groups and membership-claim authentication were
  removed when the engine became a pure library. Network exposure is the host
  application's job. The old protocol is in git history before `e7e3188`.
- **Authoring code.** Authoring moved to a separate desktop application, because a world
  file is pinned to the engine version that produced it and the tool has to hold several
  versions at once.
- The `story` subcommand, an M2-era leftover that no documentation mentioned.

[Unreleased]: https://github.com/aubrey-anima/core/compare/v1.0.2...HEAD
[1.0.2]: https://github.com/aubrey-anima/core/releases/tag/v1.0.2
[1.0.1]: https://github.com/aubrey-anima/core/releases/tag/v1.0.1
[1.0.0]: https://github.com/aubrey-anima/core/releases/tag/v1.0.0
