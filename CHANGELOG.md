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

### Fixed

- **需求带加了迟滞,角色终于能吃饱一顿。** `needs.URGENT` 是单阈值:饿到 `0.15` 吃一
  个 tick 净回 `0.045`,已经高于触发线,立刻回去干活,十来个 tick 后再饿回来。角色
  永远卡在 16% 的饥饿度上(实测 300 tick 内 hunger 只有**两个**取值),而每一次切换
  都发一条 `agent_action` + 一条 `narrative` —— 12 世界日的事件量 **19.7×**、
  `narrative` **32×**、耗时 **7×**。narrative 配了真 key 就是一次 LLM 调用,所以这是
  **32 倍的账单**,换来的不是 32 倍有趣,只是抖得厉害。
  新增 `needs.RELEASE`(energy `0.85` / hunger `0.75` / social `0.50`):开始恢复就恢复
  到饱。同一场景现在是 1.7× 事件量、1.3× 耗时,饥饿度在 600 tick 里走出 528 个取值的
  锯齿。判据是黑板上的派生值 `need._restoring`,不是第二份状态,重启即自愈;
  作者树里没写收工线的 `need_action` 节点行为逐 tick 不变。

## [1.1.1] — 2026-07-28

同 db 格式(**1**)与包格式(**1**),1.0.x 与 1.1.0 建的世界照常打开。
主题:**引擎不再对自己说谎** —— 派生视图曾经
对同一段历史给出两个答案,而世界照跑、没有任何报错。这一批全是那一类:测试看得见
的东西没坏,拿到手的东西是错的。

### Fixed

- **重开世界不再把所有人传送回出生地。** 角色走路发的是 `state_change` +
  `kind=location_join`,而投影拿这条事件去*注册地点*,从不写 `agents[who].location`。
  开机名册读的正是 `projected.location`(那一侧本来就是对的),于是每次重开,世界对
  "谁在哪"的记忆就退回创世那一刻。运行中同样分叉:活黑板说柔在咖啡店、投影说她在家。
  还有一个必要条件容易漏 —— `_record_event` 里的折叠发生在 `_stream_event` **之后**,
  而后者把位移事件改写成 `agent_action{action:"walk"}`,所以折进投影的根本不是那条
  位移。两处一起改,否则"位置对不对"取决于你有没有重启过,比统一错更难查。
  老库重放即自愈,无迁移。
- **玩家动作的内容不再存成一个空对象。** `player_action` 的
  `player_id`/`role`/`action`/`details` 全在事件顶层,而只有 `payload` 落库 —— 玩家在
  世界里做过的每一件事,重放之后一个字都不剩。四个字段**复制**(不是搬走)进 payload,
  顶层形状不变;重放侧同样回填四个键,实时流与重放形状一致。
  ⚠️ 只对此后产生的事件成立,老库里的 `{}` 不补也不迁移。
- **`simulate --report` 不再被一次聊天撑爆。** `events.ts` 跑着两种时基:引擎盖世界
  tick,聊天子系统给 `conversation` 盖墙钟。报表把 Unix 时间戳当 tick 折算成"天",
  于是 `by_day` 按 `range(max_day + 1)` 稠密展开成六百多万行 —— 放不下是 MemoryError,
  放得下是 `days=6198680` 的假答案外加被 horizon 稀释成 `other≈1.0` 的时间分配。
  引擎早就知道这条界线(时钟恢复一直在过闸),只是报表没用上。
- **同处一室不再被当成手机私聊。** `chat_service` 按玩家位置在"面对面交谈"和
  "手机文字私聊,对方不在你当前场景中"两段身份声明里选一段,后者还禁止角色描写看见你
  —— 而 `World.chat` 组 `interlocutor` 时从不传位置,面对面那一支经门面**从来不可达**。
  `anima-world chat` 明明先替你走到对方跟前,角色照样只能演"在手机上收到"。
  现在读 `world.players` 里的位置;宿主没调过 `player_move` 就维持手机私聊,引擎不猜。

### Changed

- **`report_format_version` → 2**(与引擎版本分开,消费方不必升引擎)。
  `events.by_day` 改为**稀疏**(只列真的发生过事情的天),并且**只覆盖世界 tick 上的
  事件**;墙钟事件仍计入 `total` 与 `by_type`,另在新字段 `events.wall_clock_events`
  里单独点名。等式随之变成
  `sum(by_day[*].total) + wall_clock_events == total`。
  把这些事件整个剔掉会得到一份"聊了一整晚但 chat 桶为 0"的干净摘要 —— 比撑爆更坏,
  因为没人看得出自己少读了东西。
- **`World.world_context()` 的 `presence` 新增 `in_transit`**。在途不算在场:黑板的
  `loc` 要落地才改写,只比地点会让一个正在赶路的角色被判成和你面对面,同一段 prompt
  里既说"正在去建筑工作室的路上"又说"我们面对面"。

## [1.1.0] — 2026-07-27

Same db format (**1**) and package format (**1**) as the whole 1.0.x line. Worlds built
by 1.0.0–1.0.2 open unchanged. Theme: **the engine stops swallowing what it knows** —
a package says what it needs, a rejection says which thing is wrong, a fast-forward hands
back numbers, and the front door finally has a way in.

### Added

- **`anima-world world inspect <package> [--json]`** — read a `.cyberworld`'s manifest
  without being able to run it (#3). Reading the envelope no longer depends on passing
  the engine-compat gate, so a launcher managing several engine versions can ask
  "which engine does this need?" *before* it has that engine. An incompatible package
  gets an **answer** and exit code 0; only an unreadable one is refused. The JSON field
  set is documented in REFERENCE §8 as a wire contract. New public helpers:
  `read_package_manifest()`, `WorldPackageManifest.validate_structure()` /
  `validate_engine_range()` / `runs_on()` / `compatibility()`.
- **`anima-world chat --db-path <db> --agent <id>`** — talk to a character from the
  command line (#6). Everything it needs was already on the facade (`chat_reply` →
  `record_chat_turn`); what was missing was a door. Omitting `--agent` lists the cast,
  which is also the first way a world file has ever been able to say who lives in it.
  The clock does not advance while you type.
- **`anima-world simulate --report PATH`** — a machine-readable run summary (#11):
  per-world-day event density by bucket, pairwise encounter counts and durations,
  relationship curves with turning points, and per-resident time allocation with an
  explicit `idle_only` flag. Carries its own `report_format_version`, separate from the
  engine version. New module `anima_world.sim_report` (a pure function over an event
  list, so it can be recomputed offline against any `world.db`).
- **The world seed can author the material layer** (#12): top-level `items`,
  `agents[].money`, `agents[].inventory`, and `locations[].stock`. Economy/needs shipped
  with mechanisms but no genesis entry, so an authored keepsake ("she never takes her
  father's pocket watch off") could only be dropped or demoted to a memory string. An
  item id that is only referenced gets an automatic definition, so the short form just
  works. Same tolerance as every other seed field: absent = today's behaviour, bad
  entries dropped one by one, never blocks boot.
- **The world seed can author Mock narration** via `mock_narration` (#9), including
  action kinds this engine has never heard of.

### Changed

- **Template packages now travel within a major** (#4). `engine_min` for a `template`
  export is the floor of the current major instead of the exact exporting version. A
  snapshot carries a format-stamped `world.db` and keeps the exact floor; a template
  carries only `world_seed.json` — version-neutral authored data whose schema is a
  mirrored cross-repo contract precisely so it can travel. Stamping both alike turned
  "you cannot carry your save forward" (the documented, accepted trade-off) into
  "you cannot carry your **content** forward", which nobody decided.
- **Mock narration follows the world's language instead of the engine's** (#9). The
  templates moved from hardcoded English in `narrative.py` into the prompt store
  (`narrative.mock.<kind>`, `narrative.mock_memory_suffix`), read live and authorable
  per world. No API key is the *default* state, so `遥 wandered around——还记着…` —
  English verbs, Chinese name, Chinese memory suffix, all in one line — was the first
  screen, not an edge case. A failing real LLM falls back to the same world-owned
  templates. `eat` gained a template of its own instead of rendering as "did something
  custom".

### Fixed

- **Player conversations now change the world without an API key.** The chain was
  complete on paper — `conversation` event → a 0.8-importance memory → relationship
  verdict → band crossing → `relation_shift` memory + graph edge → gossip source +
  planner context — but it broke at the first link: a Mock LLM cannot produce a
  parseable verdict, so the judge returned `None` on every call. The consequence was not
  "smaller changes", it was **no relationship data at all**, for players and NPCs alike,
  while three-axis relations are documented as always-on. No key is the *default* state,
  so the screen where README promises characters who remember you was exactly the screen
  where talking to them changed nothing — announced only by one `dropping` line on
  stderr while the character replied normally. The mock tier now gets
  `DeterministicRelationshipJudge`, the same treatment the reflector already had:
  `Δ = 0.04 × (1 - |current|)` — no RNG (worlds must stay replayable), asymptotic, never
  saturating, an order below the ±0.2 verdict ceiling. It does not pretend to be
  judgement: always positive, magnitude from headroom alone. `r_type` gets no stand-in
  and keeps its authored text — a number has a sane mechanical substitute, authored prose
  does not. A configured key still gets the real judge.
- **`World.graph(agent_id)` always returned an empty list.** Edges store subjects as
  `agent:<id>` and the parameter takes a bare id, so the lookup never matched — and it
  failed by returning `[]`, which a host reads as "this character has no relationships"
  rather than as a mistake. Bare and prefixed ids are both accepted now.
- **Package rejections name which thing is wrong** (#10). Checksum mismatch, engine
  range, seed schema, and the zip guards each printed the identical
  `invalid or inaccessible package data`. The operator can only relay what the engine
  says, so its 400 carried no reason either and an author could not tell "re-export with
  a matching core" from "fix the seed". Seed problems now carry the per-entry detail
  `world_seed_errors()` was already producing and the package layer was discarding.
  Exit code is unchanged (2).

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

[Unreleased]: https://github.com/aubrey-anima/core/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/aubrey-anima/core/releases/tag/v1.1.1
[1.1.0]: https://github.com/aubrey-anima/core/releases/tag/v1.1.0
[1.0.2]: https://github.com/aubrey-anima/core/releases/tag/v1.0.2
[1.0.1]: https://github.com/aubrey-anima/core/releases/tag/v1.0.1
[1.0.0]: https://github.com/aubrey-anima/core/releases/tag/v1.0.0
