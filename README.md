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
your app  ──import anima_world.api──▶  the world on Redis (anima:{world_id}:*)
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
     ✓ 新建 world(住在 redis://127.0.0.1:6379/0)
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

import redis

client = redis.Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
with World.open("world", redis=client) as world:
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
anima-world config set needs.enabled true --world-id world
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
with World.open("world", redis=client) as world:
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

### Things that are not people (2.0)

Until 2.0 a world contained characters, places, and items you could carry. There were
quantities on things — but nothing said what a *tree* was, so nothing could say what you
could *do* to one. Declaring a **kind** does both, once, for every instance of it:

```jsonc
{
  "kinds": [{
    "id": "agent",
    "quantities": {
      "stamina": {"default": 100, "visibility": "self", "unit": "pt"},
      "skill":   {"default": 1.0, "visibility": "here"}
    }
  }, {
    "id": "tree",
    "gloss": "a tree",
    "quantities": {
      "height":     {"default": 1.0, "visibility": "here", "unit": "m"},
      "max_height": 12.0
    },
    "affordances": {
      "look": {},
      "tend": {"when":     ["height < max_height"],
               "set":      {"height": "min(height + 0.3 * me_skill, max_height)"},
               "requires": ["me_stamina >= 15"],
               "costs":    {"stamina": "me_stamina - 15"}}
    },
    "prompt": {"budget": 3}
  }],
  "entities": [{"id": "tree:oak", "name": "the old oak", "location": "yard"}]
}
```

The declaration lives on the kind; an instance carries only its id, name, and where it is.
That split is what buys **boundedness**: her prompt names the kind once instead of
repeating the schema per tree, and `prompt.budget` caps how many of them she can see at
once — a world with 3000 trees and a world with 20 send her the same number of characters.
When the cap truncates, it says so ("there were 2997 other things here you didn't look at
closely"); a silent truncation would have her decide inside a world she believes has three
trees, and she would never find out.

**Declaring is the switch.** A world with no `kinds` behaves exactly as it did before —
this whole layer is absent. Write `kinds` and you have said "I have declared what exists
here", so suggestions become gates: a typo'd quantity name (`heigth`) refuses to start the
world and names the ones you did declare. Accepting it would quietly create a second
quantity while the real one sat at its default, with clean logs, until you noticed months
later that the tree had not grown.

**An affordance is a relation between an actor and a thing, not a property of the thing.**
The same axe affords chopping to someone strong and not to someone weak. So `when` and
`set` (about the tree) have a second half: `requires` and `costs` (about her), reading her
own quantities through the `me_` prefix. Without it every action always succeeds — and *an
action that always succeeds produces no decisions*: no reason to pick what to do first, no
reason to rest, no reason to get better at anything. With it, six turns of tending run her
out of stamina, the seventh comes back `incapable`, and she has to go do something else.

`agent` is the one built-in kind you may extend, and only its `quantities` — she is not
another thing you can `tend`; her verbs live in the behaviour tree and the chat tools.
Her quantities live in the same store as the tree's, under the same visibility rules, so
declaring one `self` means she perceives it and declaring one `here` means anyone standing
next to her does. `requires` may read only `me_*`, on purpose: a failed `requires` must
always mean exactly one thing — *you can't* — and that is the whole reason it exists
separately from `when`. `costs` and `set` read the same pre-action values (double
buffering), and a refusal writes nothing at all.

Three different gates, and they refuse for different reasons. Acting on a thing requires
being **in the same place as it** — `tend` a tree from across town comes back naming both
where the tree is and where she is, because "it's in the yard" reads as a lie when the real
problem is that she is on the road and the engine does not know where she is. Branching a
*duty* on a quantity is gated on **perception** instead: a schedule that reads a value her
`visibility` keeps from her would have her decide on something she cannot know — the same
break of character as blurting out a mine's reserves, except it leaves no trace in the
prompt at all. So that branch is warned about once at startup and reads `None` forever
after, rather than silently using whatever she saw last time she walked past.

**Tools, materials, and time are costs too.** Gibson's own example is an *axe* — something
she carries — and that could not appear in a declaration at all when `requires` could only
read quantities. `have_<item>` reads how many of a thing she is carrying (from the ledger
projection; nothing carried reads as **0**, not as an error — someone who never owned
shears and someone who just put them down are in the same position). `consumes` spends
materials and brings its own *you must have these* gate, so you never write the `requires`
line twice; writing it twice is what lets you write it once, and a world that only wrote
`consumes` would let her finish the job with a bag of fertiliser that does not exist —
stock does not go negative, so the books would look fine.

`duration` is the one cost that cannot be slept off. Quantities come back, materials can be
bought, but a stretch of time simply has to pass — which is why growing a new thing has to
hang off it. Ten months of pregnancy is not a gate because it is expensive; it is a gate
because it is long.

```jsonc
"make": {"duration": 8640,      // ticks; 0 (the default) means instantaneous
         "occupies": false,     // whether she's tied up meanwhile; default true
         "consumes": {"timber": 3}, "costs": {"stamina": "me_stamina - 40"},
         "set": {"quality": "quality + 1"}}
```

The cost is paid **up front** and the effect lands **when the time is up** — paying at the
end would make starting-and-abandoning free, and a promise you can walk away from without
a trace is not a promise. The gates are checked **only at the start**: being refused after
ten months, with the price already paid, is a failure she had no way to prevent, and a
failure you cannot prevent teaches nothing. `occupies` is a property of the *task*, not a
state of hers — building a chair ties her up, carrying a child does not, and both take ten
months. While she is tied up, any affordance call is refused with `reason == "busy"`: a
fourth class, because she should wait for what she is holding rather than try another tree
or go rest. Ask `world.engagements()` what anyone is in the middle of.

**Verbs belong to the author.** They used to be a closed set of ten, justified by "the
engine has to implement the effect" — which stopped being true once `set`/`costs`/`consumes`
made effects data. Declare `酿` or `brew` and it exists; misspell it elsewhere and the world
still refuses to start. An ASCII verb must carry a `label`, because what she reads in her
prompt is those characters and "you may: look at it, brew" is noise she then has to act on.

**The world can grow new things, and lose them.** Until 2.0 `entities` was a closed set
fixed at genesis: a tree could not be planted, a cup could not be broken. A world that
cannot grow anything new is a diorama — and *laying out entities by rule* is what
generating a world actually is.

```jsonc
"raise_seedling": {"duration": 24, "when": ["height >= 3"],
                   "requires": ["me_stamina >= 20"], "consumes": {"fertiliser": 1},
                   "costs": {"stamina": "me_stamina - 20"},
                   "spawn": {"kind": "sapling", "name": "a new seedling"}},
"pull_up": {"costs": {"stamina": "me_stamina - 5"}, "destroys_target": true}
```

**Spawning must cost something, and the author decides what.** Declaring `spawn` or
`destroys_target` without any of `costs` / `consumes` / `duration` refuses to start the
world. The engine does not hand out quotas instead: a quota is the *engine's* ceiling, and
when she hits it the refusal means nothing inside the world — "this world allows at most a
hundred trees" is not something she can understand or act on, and she will never learn it.
A cost is the *world's* reason: she knows why she can't, and what she'd have to replenish
first. But a cost only bounds the *rate*, never the *stock* — stamina refills nightly, so a
hundred days is a hundred children. Real worlds bound themselves by birth and death
together, which is why `destroys_target` shipped in the same round.

**Every birth is checked, and a failed check is not born.** A thing created at runtime does
not go through genesis, so none of the genesis gates apply to it. Without the check a
newborn can look perfectly fine in `entities` while not one of its quantities landed — its
conditions then evaluate against zero, its rules compute nothing, and both simply fail to
happen, quietly, until someone notices months later that the tree never grew. So the engine
dry-runs it: are the quantities there, does every affordance produce a *nameable* outcome,
is it actually present where its visibility claims. Nameable, not successful — "not ripe
yet" and "she can't" are the world talking normally. Anything that fails is rolled back
whole and reported as `entity_stillborn`, and the cost is *not* refunded: she really did
pay, and refunding would erase the author's bug from the books too.

Ask the CLI what a world actually declares, or whether what's in it is alive:

```bash
anima-world ontology --world-id world --json    # kinds, quantities, verbs, live values
anima-world ontology --world-id world --check   # run the birth check over every entity
```

The verb table is only obtainable here. `stocks` gives you numbers, and a number does not
tell you whether the word `tend` exists.

## How it is built

**One key prefix is one world.** The world lives on Redis under `anima:{world_id}:*`
(events, chats, memories, config, the map, her blackboard). Pass `mysql=` and the four
unbounded tables (events / memories / conversations / messages) move to MySQL. The LLM
key lives on the machine (`~/.anima-world/config.json`, 0600) — never inside a world,
so a packaged world carries no secrets by construction.

**The event log is the only truth.** There is no `balances` table, because two sources
of truth eventually disagree and you cannot tell which one is right. The world does not
store "夏 has 50 coins" — it stores *why* she has 50 coins. Reconciliation is replay.

**Many processes, one world.** The clock, her blackboard, and everything she carries
into a prompt live on Redis, so a second process with nothing but a Redis connection
sees — and can change — the same world, under one cross-process lock. Joining a running
world never rewinds it: every write on the way in is fill-if-missing.

**Version is contract.** One release freezes (engine code, storage shape, package
format) together. `anima-world contract --json` reports the storage contract (the Redis
key prefix, the MySQL table set) and the package format version, so a repository holding
a mirror can diff itself against the engine instead of quietly falling behind.

## Ship a world to someone else

Worlds package into a single `.cyberworld` file — a snapshot of a world that has
lived (its full Redis state, plus the MySQL history when there is one), together with
its genesis seed as a birth certificate.

```bash
anima-world world export --world-id world --output my.cyberworld \
    --package-id my-world --name "我的世界"

anima-world world import my.cyberworld --world-id restored   # must be an empty world id
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
anima-world map            # the map, who is where, and where they went (--json)
anima-world ontology       # what kinds of things exist, their quantities and verbs (--json)
anima-world doctor         # health check: Redis durability, keys, a real LLM call, clock speed
anima-world config         # read/write settings; api keys route to this machine, not the world
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
