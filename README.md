# anima-world

**A world-simulation engine for LLM-driven agents.** Characters wake up, get hungry,
go to work, gossip about each other, form cliques, spend money, remember what happened
last week, and change how they feel about you. One SQLite file is one world.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/aubrey-anima/core/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://github.com/aubrey-anima/core/blob/main/pyproject.toml)
[![PyPI](https://img.shields.io/pypi/v/anima-world.svg)](https://pypi.org/project/anima-world/)

English | [中文](https://github.com/aubrey-anima/core/blob/main/README.zh-CN.md)

---

## What this is

Most "AI agent" frameworks give you a chatbot with tools. This gives you a **place** —
one that keeps running whether or not anyone is watching, and remembers what happened
while they were away.

A world is a tick-driven simulation. On every tick each character runs a behavior tree
against its own needs and the state of the world, and acts. Those acts become events in
an append-only log, which is the world's only source of truth — balances, relationships,
and locations are all *projections* of that log, never separately stored rows that could
drift out of sync.

The LLM sits beside the simulation, not inside it. It writes the narration, plans what a
character does with free time, and judges how a conversation changed a relationship —
all on background threads. **The clock never blocks on a network call.** Take the LLM
away entirely and the world still runs; it just narrates in templates.

The engine is **a library, not a service.** There is no HTTP server and no port. Your
application imports it, and the world lives inside your process:

```
your app  ──import anima_world.api──▶  world.db
```

## See it run

```console
$ pip install anima-world
$ anima-world start

  ANIMA 世界引擎
  ────────────────────────────────────────────

  ① LLM
     ! LLM 未配置 —— 叙事、空闲计划、关系判定都会降级成模板文本
       修复:anima-world config set llm.api_key sk-…

  ② 世界
     ✓ 新建 saves/world.db
     时钟:1 tick/秒 —— 约 5 分钟走完一个世界日(现实时间的 300 倍速)
     ✓ 3 个角色就位: 苏晚夏、陆知遥、沈亦柔

  ③ 运行
     世界在本进程里运行,叙事会打印在下面;停止:Ctrl-C

  [第0天 00:10] 遥:遥四处走了走
  [第0天 00:10] 夏:夏睡下了
  [第0天 00:35] 遥:遥睡下了
  ^C
  世界已停下,快照已保存。下次接着跑:anima-world start
```

`start` configures the LLM, creates the world, and runs it — in that order, with no
documentation required first. Without an API key everything still works; narration is
templated and characters have no plans. That degradation is deliberate, and it is never
silent (`anima-world doctor` will tell you, and `World.state()` carries the reason).

> **Heads up on language:** the engine speaks Chinese. CLI output, the built-in seed
> world, and the reference docs are all in Chinese. The API itself is English
> (`World.open`, `world.tick`, `world.chat`), and every piece of text a world *says* —
> worldview, prompts, and the no-key narration templates — comes from the seed, not from
> the engine. So an English world is a matter of supplying an English seed.

## Install

```bash
pip install anima-world          # Python 3.11+
```

Three runtime dependencies, chosen to stay out of your way since they land in every host
that embeds a world: `cryptography` (encrypts the API key at rest), `openai` (any
OpenAI-compatible endpoint), `httpx`.

From source:

```bash
git clone https://github.com/aubrey-anima/core.git anima-world
cd anima-world && pip install -e ".[dev]" && python -m pytest
```

## Use it in your app

This is the main interface. `World` is a plain object with a lifetime — open it, drive
it, close it.

```python
from anima_world.api import World

with World.open("saves/world.db") as world:
    world.start_clock()                        # background clock; or drive it yourself
    print(world.state()["world_time"])         # {'day': 0, 'hour': 6, 'minute': 25, ...}

    # Talk to a character on a player's behalf. Streams; your app owns the transcript.
    for chunk in world.chat("夏", [{"role": "user", "content": "你好"}],
                            player_id="p1", display_name="阿宇"):
        print(chunk, end="")

    # Commit a finished exchange: summary + one world event + a relationship verdict
    world.record_chat_turn("夏", "p1", [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ])

    world.player_move("p1", "cafe")
    world.config_set("scheduler.tick_rate", 1.0)
```

For batch work, drive the clock yourself — `world.tick(n)` is synchronous and
deterministic, which is what makes fast-forwarding and testing possible:

```python
with World.open("w.db") as world:
    world.tick(288)                     # one world day
    print(world.memories("夏"))         # what she remembers, ranked
    print(world.needs("夏"))            # {'energy': 0.59, 'hunger': 0.16, ...}
    print(world.cliques())              # who has clustered together
```

## What a world contains

Four subsystems, each an opt-in flag except memory. They are off by default because a
world that only walks around and talks is a legitimate world, and every mechanism you
enable is more LLM spend and more surface to reason about.

| Subsystem | Flag | What it adds |
|---|---|---|
| **Memory** | always on | Retrieval scored on relevance × recency × importance, periodic reflection that writes higher-order memories, and a forgetting curve |
| **Needs** | `needs.enabled` | `energy` / `hunger` / `social` decay per tick and drive an urgency band in the behavior tree — a tired character stops what it is doing and goes to sleep |
| **Economy** | `economy.enabled` | Items, money, shops, wages, and price drift. The ledger is a projection of `payment` events, so balances cannot be forged |
| **Social** | `social.enabled` | Three-axis relationships (always on) plus gossip that propagates second-hand with confidence decay, and emergent cliques |

```bash
anima-world config set needs.enabled true --db-path saves/world.db
```

### Choices of her own (1.3.0, four more flags, all off)

In a 1.2 chat turn a character had exactly one thing she could do: **continue the
sentence**. She had no action to choose (saying "I'm leaving" left her standing there),
no relational intent behind her words, and no way to tell whether your message was
addressed to her or was you directing the scene. These four flags are that missing half.

| Flag | What she gains |
|---|---|
| `chat.stance.enabled` | She picks an explicit **stance** before replying — placate, provoke, probe, avoid, vent, seduce, defer, neutral. Stored per (character, target): she can be prickly with you and sweet to someone else |
| `chat.tools.enabled` | She can **call capabilities** mid-chat: mute you, end the conversation, say "later" (and actually come back), walk away (a real journey, not a line of prose), refuse a topic, broadcast |
| `chat.intent.enabled` | Each of your messages is classified first: `dialogue` / `narrative_direction` (you're directing the scene — it changes the world, not the prompt) / `style_adjust` ("call me 霜霜" — written into her persona for this player, permanently) |
| `chat.loop.enabled` | `world.chat_burst()` keeps generating until **she** decides to stop: explicit yield, a question, budget exhausted, or a tool that ends the turn |

Calls ride as inline markers in the reply stream (`〔tool:mute {"minutes": 5}〕`) rather
than OpenAI's `tools=` field, because the default state has to work: with no key a world
runs on Mock, and function-calling support across local and OpenAI-compatible endpoints is
uneven. Prose still streams a character at a time; not one marker leaks to the player.

```python
with World.open("saves/world.db") as world:
    meta = {}
    reply = world.chat_reply("夏", turn, player_id="p1", display_name="阿宇", meta=meta)
    meta["stance"]        # 'provoke' — and meta['stance_declared'] tells you she chose it
    meta["tool_calls"]    # [{'tool': 'walk_away', 'ok': True, 'detail': {'to': 'home'}}]
    world.record_chat_turn("夏", "p1", turn[-2:], meta=meta)   # observability onto the rows

    for step in world.chat_burst("夏", turn, player_id="p1"):   # she may say three things
        if step["kind"] == "message":
            print(step["text"])
```

`world.chat()` raises `AgentUnavailable` while she is ignoring that player — an empty reply
would be indistinguishable from an LLM outage, and those two deserve different UI.

## How it is built

**One file is one world.** `world.db` holds events, chats, memories, and config.
`world.db.key` sits next to it and encrypts the API key — **it must travel with the
database**, or the key becomes unreadable and the world silently drops to Mock. (Not
actually silently: the engine says so at boot and keeps saying so in `state()`.)

**The event log is the only truth.** There is no `balances` table, because two sources
of truth eventually disagree and you cannot tell which one is right. `world.db` does not
store "夏 has 50 coins" — it stores *why* she has 50 coins. Reconciliation is replay.

**One running world owns its file.** Half the truth lives in memory (clock, projection,
locks, thread pools), so a second process writing the same `world.db` forks it
immediately. Offline work — packaging, fast-forwarding — happens after the world closes.

**Version is contract.** One release freezes (engine code, db format, package format)
together. The major version *is* the db format version — meaning it is **mountability**:
`db.py` enforces it at runtime, so mounting a database from an incompatible format is
refused on the spot rather than quietly writing garbage. Purely additive schema changes
(new tables, new nullable columns) don't affect mountability and ride the minor version
instead, stamped into `db_meta.schema_revision` so a downgrade is visible — an older engine
still opens the file, it just doesn't read the new tables, and `contract` / `doctor` say so.

## Ship a world to someone else

Worlds package into a single `.cyberworld` file — either a template (just the seed, the
world builds itself on first boot) or a snapshot (a database that has already lived).

```bash
anima-world world export --seed seed.json --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode template

anima-world world import my.cyberworld --destination ./instances
```

A package says what it needs without your having to be able to run it — the launcher
managing several engine versions is exactly the caller who cannot yet:

```bash
anima-world world inspect my.cyberworld --json
# {"world_id": "my-world", "engine_min": "2.0.0", …, "current_engine_version": "1.1.0",
#  "runnable": false}          # answers, exit 0 — refusing here would defeat the format
```

## Commands

```bash
anima-world start          # create + run, with guided setup — start here
anima-world chat           # talk to a character; no --agent lists who lives there
anima-world prompt         # see the prompt a character receives, block by block
anima-world doctor         # health check: files, keys, a real LLM call, clock speed
anima-world config         # read/write settings, secrets encrypted and masked
anima-world run            # foreground host, no onboarding (for deployment)
anima-world simulate       # headless fast-forward (--report writes a run summary)
anima-world world          # export / import / inspect .cyberworld packages
```

## Documentation

| | |
|---|---|
| [docs/REFERENCE.md](https://github.com/aubrey-anima/core/blob/main/docs/REFERENCE.md) | Every command, every `World` method, every config key, the beat-script format 🇨🇳 |
| [docs/ARCHITECTURE.md](https://github.com/aubrey-anima/core/blob/main/docs/ARCHITECTURE.md) | Why it is shaped this way: the truth model, the tick frame, threads and locks, invariants, known debt 🇨🇳 |
| [CONTRIBUTING.md](https://github.com/aubrey-anima/core/blob/main/CONTRIBUTING.md) | Development setup, the invariants a patch must not break, how to propose changes |
| [CHANGELOG.md](https://github.com/aubrey-anima/core/blob/main/CHANGELOG.md) | Release history |

## Contributing

Issues and pull requests are welcome. Start with [CONTRIBUTING.md](https://github.com/aubrey-anima/core/blob/main/CONTRIBUTING.md) —
it lists the handful of invariants that a change must not break (there is exactly one
lock in the system, the LLM is never called on the tick thread, and a few file formats
are mirrored by other repositories).

## License

[Apache License 2.0](https://github.com/aubrey-anima/core/blob/main/LICENSE). Use it, modify it, ship it inside closed-source products;
keep the copyright notice and state what you changed. Apache rather than MIT for the
patent grant, and permissive rather than copyleft because an engine that hosts embed
cannot be copyleft without infecting every host that embeds it.
