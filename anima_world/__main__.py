"""CLI entrypoint for anima_world (M1 recover, M2 story)."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import sys
import time
import threading
from pathlib import Path
from typing import Any

from anima_world.actions import ActionTable
from anima_world.agent import Agent
from anima_world.beats import BeatScript, BeatScriptError, coerce_goals
from anima_world.brain import Brain
from anima_world.bt_nodes import Action, Condition, Selector, Sequence, Status, default_bt
from anima_world.config_store import ConfigStore, load_or_create_key
from anima_world.config_store import seed_defaults as seed_config_defaults
from anima_world.db import open_db
from anima_world.events import EventLog
from anima_world.graph import KnowledgeGraph
from anima_world.locations import DEFAULT_POINTS
from anima_world.memory_store import MemoryDescriptor, MemoryStore
from anima_world.memory_triggers import TriggerEngine
from anima_world.llm_client import MockLLMClient, create_llm_client_from_config
from anima_world.narrative import (
    MockNarrativeProvider,
    create_narrative_provider_from_env,
    generate_capability_catalog,
)
from anima_world.planner import Planner, SyncLLM
from anima_world.projection import project_events
from anima_world.prompt_store import PromptStore
from anima_world.prompt_store import seed_defaults as seed_prompt_defaults
from anima_world.scheduler import Scheduler
from anima_world.types import Event, Projection
from anima_world.world_store import BTStore, LocationStore
from anima_world.world_seed import is_valid_world_seed as _is_valid_world_seed

logger = logging.getLogger(__name__)

LOCAL_DEV_WORLD_SERVICE_TOKEN = "anima-loopback-world-service"
LOCAL_DEV_MEMBERSHIP_CLAIM_SECRET = "anima-loopback-membership-claim"


def _is_loopback_host(host: str) -> bool:
    value = host.strip().lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _resolve_legacy_flag(args: argparse.Namespace) -> bool:
    """Return True iff legacy player routes should be enabled.

    Priority (highest first):
    1. --disable-legacy-player-routes → False (explicit off)
    2. --legacy-player-routes         → True  (explicit on)
    3. loopback host                  → True  (default on for local dev)
    4. non-loopback host              → False (default off for production)
    """
    if args.disable_legacy_player_routes:
        return False
    if args.legacy_player_routes:
        return True
    return _is_loopback_host(args.host)


def _runtime_service_credentials(
    host: str, raw_service_tokens: str, claim_secret: str | None
) -> tuple[tuple[str, ...], str | None]:
    if not raw_service_tokens.strip() and not claim_secret and _is_loopback_host(host):
        return (LOCAL_DEV_WORLD_SERVICE_TOKEN,), LOCAL_DEV_MEMBERSHIP_CLAIM_SECRET
    return (
        tuple(token.strip() for token in raw_service_tokens.split(",") if token.strip()),
        claim_secret,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anima-world",
        description="ANIMA 世界引擎 + 创作工作台:运行世界、创作世界、打包成 .cyberworld",
    )
    sub = parser.add_subparsers(dest="command")

    # -- story (M2) --
    story = sub.add_parser("story", help="Run agent story simulation")
    story.add_argument("--agents", type=int, default=3, help="Number of agents (default 3)")
    story.add_argument("--ticks", type=int, default=50, help="Max ticks (default 50)")
    story.add_argument("--narrative", action="store_true", help="Enable narrative output")

    serve = sub.add_parser("serve", help="Run web chat server")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000, help="Bind port (default 8000)")
    serve.add_argument(
        "--agents", type=int, default=None,
        help="Number of agents (default: full seed roster, or 3 without a seed)",
    )
    serve.add_argument("--tick-rate", type=float, default=1.0, help="Background ticks per second")
    serve.add_argument("--db-path", default="saves/world.db", help="SQLite world DB path")
    serve.add_argument("--seed", default=None, help="World seed JSON path (default: bundled world_seed.json)")
    serve.add_argument(
        "--beats", default=None,
        help="Beat script JSON path (beat-director; invalid script fails startup)",
    )
    serve.add_argument("--instance-id", default="legacy", help="Runtime instance identity")
    serve.add_argument("--world-id", default="legacy", help="World lineage identity")
    serve.add_argument("--world-name", default="", help="World display name for runtime admin")
    serve.add_argument(
        "--world-admin-token-env",
        default="ANIMA_WORLD_ADMIN_TOKEN",
        help="Environment variable containing the online world admin token",
    )
    serve.add_argument(
        "--platform-service-token-env",
        default="ANIMA_WORLD_SERVICE_TOKEN",
        help="Environment variable containing comma-separated platform service credentials",
    )
    serve.add_argument(
        "--membership-claim-secret-env",
        default="ANIMA_MEMBERSHIP_CLAIM_SECRET",
        help="Environment variable containing the membership claim signing secret",
    )
    serve.add_argument(
        "--cors-origin", action="append", default=[],
        help="Allowed independent frontend origin (repeatable; no wildcard)",
    )
    _legacy_group = serve.add_mutually_exclusive_group()
    _legacy_group.add_argument(
        "--disable-legacy-player-routes",
        action="store_true",
        help="Force legacy player identity routes OFF (overrides loopback default)",
    )
    _legacy_group.add_argument(
        "--legacy-player-routes",
        action="store_true",
        help="Force legacy player identity routes ON (overrides non-loopback default of OFF)",
    )

    # -- simulate (novel-benchmark-loop) --
    simulate = sub.add_parser(
        "simulate", help="Fast-forward a world headlessly (no sleep, no web server)"
    )
    simulate.add_argument("--db-path", required=True, help="SQLite world DB path")
    simulate.add_argument("--seed", default=None, help="World seed JSON path (default: bundled world_seed.json)")
    simulate.add_argument(
        "--agents", type=int, default=None,
        help="Number of agents (default: full seed roster, or 3 without a seed)",
    )
    window = simulate.add_mutually_exclusive_group(required=True)
    window.add_argument("--days", type=int, help="World days to fast-forward")
    window.add_argument("--ticks", type=int, help="Ticks to fast-forward")
    simulate.add_argument(
        "--llm", choices=("full", "planner", "mock"), default="full",
        help="LLM tier: full=narrative+planner per config; planner=real planner, "
             "mock narrative (recommended for long runs); mock=everything mock",
    )
    simulate.add_argument(
        "--no-llm", action="store_true",
        help="Alias for --llm mock (wins when both are given)",
    )
    simulate.add_argument(
        "--plan-wait-cap", type=float, default=None,
        help="Max seconds to wait per world day for in-flight plans "
             "(default: 2x planner.timeout)",
    )
    simulate.add_argument(
        "--beats", default=None,
        help="Beat script JSON path (beat-director; invalid script fails startup)",
    )

    world = sub.add_parser("world", help="Export or import portable world data packages")
    world_commands = world.add_subparsers(dest="world_command", required=True)
    world_export = world_commands.add_parser("export", help="Export a .cyberworld package")
    world_export.add_argument("--db-path", default=None, help="SQLite world DB (required for snapshot)")
    world_export.add_argument("--seed", required=True, help="World seed JSON path")
    world_export.add_argument("--beats", default=None, help="Optional beats JSON path")
    world_export.add_argument("--output", required=True, help="Output .cyberworld path")
    world_export.add_argument("--world-id", required=True, help="Stable lowercase world lineage id")
    world_export.add_argument("--name", "--title", required=True, help="World display name")
    world_export.add_argument("--mode", choices=("snapshot", "template"), default="snapshot")
    world_export.add_argument("--summary", default="")
    world_export.add_argument("--genre", default="")
    world_export.add_argument("--setting", default="")
    world_export.add_argument("--theme", default="default")
    world_import = world_commands.add_parser("import", help="Import a .cyberworld package")
    world_import.add_argument("package", help="Package archive path")
    world_import.add_argument(
        "--destination",
        default="saves/operator/instances",
        help="Instances root the imported world lands under (default: the project-local "
        "operator instances dir; pass an absolute path for a backend-managed root)",
    )

    author = sub.add_parser("author", help="World-creator tools (LLM seed generation)")
    author_commands = author.add_subparsers(dest="author_command", required=True)
    author_generate = author_commands.add_parser(
        "generate", help="Generate a validated world seed from a concept via the configured LLM"
    )
    author_generate.add_argument("--concept", required=True, help="One-line world concept (中文即可)")
    author_generate.add_argument("--output", required=True, help="Seed JSON output path")
    author_generate.add_argument("--agents", type=int, default=4, help="Number of characters")
    author_generate.add_argument("--locations", type=int, default=5, help="Number of locations")

    author_serve = author_commands.add_parser("serve", help="Run the author studio web server")
    author_serve.add_argument("--db", default="saves/author.db", help="Author SQLite DB path (default saves/author.db)")
    author_serve.add_argument(
        "--data-dir", default=None,
        help="Directory for novel files (default: <db stem>-data/ next to the db)",
    )
    author_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    author_serve.add_argument("--port", type=int, default=8402, help="Bind port (default 8402)")
    author_serve.add_argument(
        "--db-editor-port", type=int, default=None,
        help="Port of a sibling sqlite-web instance to embed in the studio UI",
    )
    author_serve.add_argument(
        "--db-editor-url", default=None,
        help="Full public URL of that sqlite-web instance (wins over --db-editor-port; "
        "use behind a tunnel/reverse proxy)",
    )

    return parser


CHARACTER_ROSTER: list[dict[str, str]] = [
    {
        "id": "夏",
        "name": "苏晚夏",
        "location": "cafe",
        "personality": "开朗热情，说话直接，是咖啡店里手脚麻利的店员，喜欢主动搭话、把气氛聊热",
    },
    {
        "id": "遥",
        "name": "陆知遥",
        "location": "workshop",
        "personality": "冷静知性，惜字如金，是独立设计工作室的建筑师，习惯理性分析、不轻易表露情绪",
    },
    {
        "id": "柔",
        "name": "沈亦柔",
        "location": "home",
        "personality": "温柔细腻，情感丰富，喜欢窝在家里画画，说话温和、容易共情别人的心事",
    },
]


WORLD_SEED_PATH = Path(__file__).parent / "world_seed.json"

def _load_world_seed(path: Path | str = WORLD_SEED_PATH) -> dict[str, Any] | None:
    """Load + validate the bundled world seed file (D7).

    Returns None (the "fall back to hardcoded defaults" signal) on any
    missing file, unreadable file, or schema mismatch — logging a warning
    rather than raising, so a bad seed file can never take `serve` down at
    boot. Only consulted at first boot against a fresh (empty) database.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("world_seed.json unavailable or invalid (%s); falling back to hardcoded defaults", exc)
        return None
    if not _is_valid_world_seed(data):
        logger.warning("world_seed.json failed schema validation; falling back to hardcoded defaults")
        return None
    return data
_LOCATION_ENTRY_FIELDS = ("id", "name", "description", "kind", "parent", "x", "y", "w", "h")


def _normalize_location_entry(loc: dict[str, Any], index: int, total: int) -> dict[str, Any]:
    """Coerce one seed-file location entry into the `locations` table's shape.

    An entry from before nested-map (no `kind`/`x`/`y`, possibly an `exits`
    list) is seeded as a top-level point at an evenly spaced fallback position
    and logs a warning — a stale seed file must degrade, never raise.
    """
    entry = {k: v for k, v in loc.items() if k in _LOCATION_ENTRY_FIELDS}
    entry.setdefault("name", entry["id"])
    entry.setdefault("description", "")
    if "kind" not in entry or entry.get("x") is None or entry.get("y") is None:
        logger.warning(
            "world_seed location %r lacks kind/x/y (pre-nested-map format); "
            "seeding it as a top-level point at a fallback position",
            entry["id"],
        )
        spacing = 1.0 / (total + 1)
        entry.setdefault("kind", "point")
        entry.setdefault("parent", None)
        entry["x"] = entry.get("x") if entry.get("x") is not None else spacing * (index + 1)
        entry["y"] = entry.get("y") if entry.get("y") is not None else spacing * (index + 1)
    return entry


def _roster_entry(i: int, locs: list[str], seed: dict[str, Any] | None = None) -> dict[str, str]:
    """Return the i-th character's seed data: seed file agents first (when
    given), else the hardcoded CHARACTER_ROSTER, falling back to a generic
    agent past either roster's length."""
    roster = seed["agents"] if seed is not None else CHARACTER_ROSTER
    if i < len(roster):
        entry = roster[i]
        return {
            "id": entry["id"],
            "name": entry["name"],
            "location": entry["location"],
            "personality": entry["personality"],
        }
    aid = chr(ord("A") + i) if i < 26 else f"agent_{i}"
    return {"id": aid, "name": aid, "location": locs[i % len(locs)], "personality": ""}


# Normalize a hand-authored goals field to a list — `list("守住店")` would
# silently split it into one goal per CHARACTER (prompt-grounding code review
# #2). One implementation, shared with the beat director (beats.py).
_coerce_goals = coerce_goals


def _goals_for(agent_id: str, seed: dict[str, Any] | None) -> list[Any]:
    """The agent's goals from the seed file (prompt-grounding).

    First-boot fallback source for the blackboard — a restart reads them
    from the projection's spec instead (persona_update genesis wins). Same
    tolerant shape as `_duties_for`: missing key/unknown id ⇒ empty list.
    """
    if not seed:
        return []
    for entry in seed.get("agents", []):
        if entry.get("id") == agent_id:
            return _coerce_goals(entry.get("goals"))
    return []


def _duties_for(agent_id: str, seed: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The agent's fixed duties from the seed file (bt-duties D6).

    An agent with no `duties` (missing key, old seed file, unknown id) gets an
    empty list — `seed_duties` then builds a tree with nothing but the
    idle_wander fallback, i.e. exactly today's behavior. Never raises.
    """
    if not seed:
        return []
    for entry in seed.get("agents", []):
        if entry.get("id") == agent_id:
            duties = entry.get("duties")
            if duties is None:
                logger.warning("agent %r has no duties in world_seed.json — idle-only tree", agent_id)
                return []
            return list(duties)
    return []


def _build_planner(
    scheduler: Scheduler,
    config_store: Any | None,
    prompt_store: Any | None,
    bt_store: BTStore | None,
    location_store: LocationStore | None,
    memory_store: Any | None,
    force_mock_llm: bool = False,
) -> Planner | None:
    """Wire the free-time planner, or None when it can't/shouldn't run.

    Needs the DB-backed stores (the action space and the duty windows both come
    from tables) and the registered agents (chat targets). `planner.enabled=0`
    turns it off and the world simply falls back to `idle_wander` in free time.
    force_mock_llm (novel-benchmark-loop: `simulate --no-llm`): use a mock
    LLM client so a fast-forward run never makes a network call.
    """
    if bt_store is None or location_store is None or config_store is None:
        return None
    if not config_store.get("planner.enabled", default=True):
        logger.info("planner disabled by config; agents will idle in their free time")
        return None

    def persona_provider(agent_id: str) -> dict[str, Any]:
        brain = scheduler.agents.get(agent_id)
        if brain is None:
            return {}
        return {
            "name": brain.agent.name,
            "personality": brain.agent.blackboard.read("personality") or "",
            # prompt-grounding: without this the planner scheduled 米彩's day
            # with no idea she was fighting off a hostile takeover
            "goals": brain.agent.blackboard.read("goals") or [],
        }

    llm_client = MockLLMClient() if force_mock_llm else create_llm_client_from_config(config_store)
    return Planner(
        llm=SyncLLM(llm_client, config_store=config_store),
        bt_store=bt_store,
        location_store=location_store,
        persona_provider=persona_provider,
        agent_ids=lambda: list(scheduler.agents),
        duty_windows=bt_store.duty_windows,
        prompt_store=prompt_store,
        memory_store=memory_store,
    )


def _away_agents(persisted: list[Event]) -> set[str]:
    """Who is off-stage right now: per agent, the LAST presence event wins
    (`agent_join`/`agent_return` = present, `agent_leave` = away). Events
    arrive seq-ordered from replay (agent-leave-return D4)."""
    away: set[str] = set()
    for ev in persisted:
        if not ev.who:
            continue
        if ev.type == "agent_leave":
            away.add(ev.who)
        elif ev.type in ("agent_join", "agent_return"):
            away.discard(ev.who)
    return away


def _make_beat_agent_factory(bt_store: BTStore | None):
    """The Brain-construction callback for beat `agent_join` ops (beat-director).

    Injected into the Scheduler as a closure so `beats.py`/`scheduler.py`
    never import this CLI module (dependency stays downward). Mirrors the
    roster loop in `build_serve_scheduler`: duties seeded into the agent's
    own tree (empty-only, so a restart with an existing tree no-ops), a
    `chat_with_<id>` action row so the new agent matches `seed_defaults`'
    roster shape, personality/goals onto the blackboard.
    """

    def factory(bundle: dict[str, Any]) -> Brain:
        aid = str(bundle["id"])
        location = bundle.get("location")
        if bt_store is not None:
            bt_store.seed_duties(aid, list(bundle.get("duties") or []))
            bt_store.ensure_plan_node(aid)
            if not any(r["node_id"] == f"chat_with_{aid}" for r in bt_store.actions()):
                bt_store.set_action(f"chat_with_{aid}", "chat", {"target": aid})
            bt_root = bt_store.build_tree(aid)
            action_table = bt_store.action_table()
        else:
            bt_root = default_bt()
            action_table = ActionTable.default()
        agent = Agent(id=aid, name=bundle.get("name", aid), bt_root=bt_root, location=location)
        agent.blackboard.write("loc", location)
        agent.blackboard.write("personality", bundle.get("personality", ""))
        agent.blackboard.write("goals", _coerce_goals(bundle.get("goals")))
        return Brain(agent=agent, action_table=action_table)

    return factory


def _bt_for(loc: str) -> Selector:
    """Default BT: check action_id availability and resolve one."""
    return Selector(
        [
            Sequence([Condition("loc", loc), Action(lambda bb: Status.SUCCESS, "go_to_cafe")]),
        ]
    )


def build_serve_scheduler(
    n_agents: int | None = None,
    db_path: str | Path | None = None,
    seed_path: str | Path | None = None,
    force_mock_llm: bool = False,
    mock_narrative: bool = False,
    beats_path: str | Path | None = None,
) -> Scheduler:
    """Build the default M3 web-chat world.

    n_agents=None (novel-benchmark-loop): register the seed file's full
    roster instead of being capped at the old hardcoded default of 3 — a
    benchmark microworld's agent count shouldn't be silently truncated.
    seed_path (novel-benchmark-loop): override which seed file is consulted
    at first boot; only ever read against a fresh (empty) database, same as
    the bundled file (M6 D7) — an existing DB's events remain the sole
    source of truth regardless of what --seed points at.
    force_mock_llm (novel-benchmark-loop): `simulate --no-llm` threads this
    through narrative/planner/capability-catalog construction so a
    fast-forward run makes zero network calls even when a real key is
    configured — a post-hoc swap after construction is too late for the
    capability catalog, which is generated synchronously at first boot.
    mock_narrative (sim-ff-usability): `simulate --llm planner` mocks ONLY
    the narrative provider — hundreds of duty transitions × one real LLM
    call each is what made a 30-world-day run undrainable — while the
    planner and capability catalog stay on the configured stack.
    beats_path (beat-director): authored beat script; loading is STRICT
    (BeatScriptError propagates and fails startup — the opposite of the seed
    file's never-block philosophy, deliberately: --beats is an explicit
    opt-in and an authoring error should surface at load, not mid-run).
    """
    beat_script = BeatScript.load(beats_path) if beats_path is not None else None
    event_log = None
    db_path_str = None
    memory_store = None
    knowledge_graph = None
    trigger_engine = None
    config_store = None
    prompt_store = None
    # M5: created up front so ConfigStore/PromptStore (built before the
    # Scheduler exists) share the same RLock the Scheduler uses to guard the
    # connection, instead of racing it with their own private lock.
    shared_lock = threading.RLock()
    location_store = None
    bt_store = None
    world_seed = _load_world_seed(seed_path) if seed_path is not None else _load_world_seed()
    if n_agents is None:
        n_agents = len(world_seed["agents"]) if world_seed is not None else len(CHARACTER_ROSTER)
    if db_path is not None:
        conn = open_db(db_path)
        event_log = EventLog(conn)
        db_path_str = str(db_path)
        # M5: config/prompts share the same world.db connection; seeding is
        # idempotent (no-op past the first boot).
        config_store = ConfigStore(conn, fernet_key=load_or_create_key(db_path), lock=shared_lock)
        seed_config_defaults(config_store)
        prompt_store = PromptStore(conn, lock=shared_lock)
        seed_prompt_defaults(prompt_store)
        # M4: memory/graph share the same world.db connection.
        # llm-relationship-judge code review #1: every store on this shared
        # connection serializes on shared_lock — MemoryStore was the one
        # exception (private RLock guarding nothing), and the judge pool
        # made web-thread query vs worker-thread append collisions routine.
        memory_store = MemoryStore(conn, config_store=config_store, lock=shared_lock)
        knowledge_graph = KnowledgeGraph(conn)
        trigger_engine = TriggerEngine(config_store=config_store)
        # World definition data (the map, the action table, BT shape) lives in
        # tables; seeded once from world_seed.json, DB rows win thereafter.
        location_store = LocationStore(conn, lock=shared_lock)
        bt_store = BTStore(conn, lock=shared_lock)
        _seed_world_defs(location_store, bt_store, world_seed)
    # llm-relationship-judge: judge only exists with a config store (it needs
    # the live llm.* stack); --no-llm gives it a mock client whose garbage
    # reply degrades every verdict to None — chat then simply produces no
    # relationship data, the designed floor.
    relationship_judge = None
    if config_store is not None:
        from anima_world.relationship_judge import RelationshipJudge

        judge_client = MockLLMClient() if force_mock_llm else create_llm_client_from_config(config_store)
        relationship_judge = RelationshipJudge(
            llm=SyncLLM(judge_client, config_store=config_store, timeout_key="judge.timeout"),
            prompt_store=prompt_store,
        )
    # beat-director: the factory is created unconditionally (not only with
    # --beats) — a DB whose history contains a mid-run agent_join must
    # reconstruct that agent on ANY later boot, beats flag or not.
    beat_agent_factory = _make_beat_agent_factory(bt_store)
    scheduler = Scheduler(
        narrative_provider=(
            MockNarrativeProvider()
            if force_mock_llm or mock_narrative
            else create_narrative_provider_from_env(prompt_store, config_store)
        ),
        relationship_judge=relationship_judge,
        event_log=event_log,
        db_path=db_path_str,
        memory_store=memory_store,
        knowledge_graph=knowledge_graph,
        trigger_engine=trigger_engine,
        config_store=config_store,
        prompt_store=prompt_store,
        location_store=location_store,
        lock=shared_lock,
        beat_script=beat_script,
        beat_agent_factory=beat_agent_factory,
    )
    # D3 restart-reversion fix: Scheduler.__init__ already replayed whatever
    # is persisted into scheduler._memory_projection (empty on a fresh DB) —
    # reuse it here for persona resolution BEFORE constructing agents,
    # instead of folding the same event list into a second Projection.
    persisted: list[Event] = event_log.replay() if event_log is not None else []
    boot_projection = scheduler._memory_projection
    scheduler.bt_store = bt_store
    # agent-leave-return D4: whoever's last presence event is a leave stays
    # off-stage — skipped in both the roster loop and the mid-run-join scan.
    away = _away_agents(persisted)
    locs = ["cafe", "workshop", "home"]
    for i in range(n_agents):
        entry = _roster_entry(i, locs, world_seed)
        if entry["id"] in away:
            continue
        # bt-duties D5/D6: each agent gets its OWN tree (duties first, idle
        # last), seeded from the seed file's `duties` on first boot. An agent
        # with no tree of its own falls back to the shared "default" tree, and
        # a no-DB run to the built-in default_bt() — never brainless.
        if bt_store is not None:
            bt_store.seed_duties(entry["id"], _duties_for(entry["id"], world_seed))
            bt_store.ensure_plan_node(entry["id"])  # trees seeded before the planner existed
            bt_root = bt_store.build_tree(entry["id"])
        else:
            bt_root = default_bt()
        agent = Agent(id=entry["id"], name=entry["name"], bt_root=bt_root, location=entry["location"])
        agent.blackboard.write("loc", entry["location"])
        projected_agent = boot_projection.agents.get(entry["id"])
        personality = entry["personality"]
        if projected_agent is not None and "personality" in projected_agent.spec:
            personality = projected_agent.spec["personality"]
        agent.blackboard.write("personality", personality)
        # prompt-grounding: goals ride the same boot path as personality —
        # projection spec (persona_update genesis) wins, seed entry is the
        # genuinely-fresh-agent fallback.
        goals = _goals_for(entry["id"], world_seed)
        if projected_agent is not None and "goals" in projected_agent.spec:
            goals = _coerce_goals(projected_agent.spec["goals"])  # old events may carry a raw string
        agent.blackboard.write("goals", goals)
        action_table = bt_store.action_table() if bt_store is not None else ActionTable.default()
        brain = Brain(agent=agent, action_table=action_table)
        scheduler.register(brain)
    # beat-director restart path: an agent who joined MID-RUN (a beat's
    # agent_join has ts > 0, unlike genesis) is in the event log but not in
    # the seed roster — without this it would exist in the projection yet
    # never be ticked again after a restart. Persona/goals come from the
    # projection spec (persona_update events win, same rule as the roster
    # loop); duties live in its bt_nodes tree already (seed_duties no-ops on
    # a non-empty tree).
    for ev in persisted:
        if ev.type != "agent_join" or ev.ts <= 0 or not ev.who or ev.who in scheduler.agents:
            continue
        if ev.who in away:
            continue
        projected_agent = boot_projection.agents.get(ev.who)
        spec = projected_agent.spec if projected_agent is not None else dict(ev.payload.get("spec") or {})
        scheduler.register(beat_agent_factory({
            "id": ev.who,
            "name": spec.get("name", ev.who),
            "location": (projected_agent.location if projected_agent is not None else None)
            or ev.payload.get("location"),
            "personality": spec.get("personality", ""),
            "goals": _coerce_goals(spec.get("goals")),
        }))
    # The planner needs the registered roster (chat targets) and the duty trees
    # (free windows), so it is attached only once both exist.
    scheduler.set_planner(
        _build_planner(
            scheduler, config_store, prompt_store, bt_store, location_store, memory_store,
            force_mock_llm,
        )
    )
    if event_log is not None:
        if not persisted:
            _seed_initial_world(event_log, scheduler, world_seed, location_store)
            _seed_capability_catalog(event_log, config_store, force_mock_llm)
            _seed_world_setting(prompt_store, world_seed)
            persisted = event_log.replay()
            # Only re-fold here: genesis events were just appended above,
            # so the projection Scheduler.__init__ built (from the
            # then-empty log) is now stale and must be recomputed once.
            scheduler._memory_projection = project_events(persisted)
        scheduler.load_persisted_events(persisted)
        if memory_store is not None and trigger_engine is not None:
            _rebuild_memories(memory_store, trigger_engine, persisted)
    return scheduler


def _seed_world_setting(prompt_store: Any | None, world_seed: dict[str, Any] | None) -> None:
    """prompt-grounding: a seed file's `world_setting` replaces the bundled
    default worldview at FIRST BOOT only (caller guards with `not persisted`).

    The DB row is the runtime authority thereafter (M5 rule, same as llm.*):
    UI hot-edits stick, restarts never re-apply the seed. Without this
    channel every custom world ran under the hardcoded 旧港区 worldview —
    Window-1's narrative "hallucinations" were the LLM obeying it.
    """
    if prompt_store is None or world_seed is None:
        return
    setting = world_seed.get("world_setting")
    if isinstance(setting, str) and setting.strip():
        try:
            prompt_store.set("world.setting", setting.strip())
        except Exception:  # noqa: BLE001 - a bad seed field must not abort a
            # first boot whose genesis events are already committed (the
            # crash would silently lock the world onto the bundled default
            # worldview forever — code review Round 1 #1)
            logger.warning("world_setting from seed could not be stored; keeping default", exc_info=True)


def _rebuild_memories(
    memory_store: MemoryStore, trigger_engine: TriggerEngine, persisted: list[Event]
) -> None:
    """Lazily replay persisted events through the trigger on a fresh/upgraded DB.

    A no-op if `memories` already has rows (design.md Open Question #3:
    startup-lazy rebuild, only when the table is empty).
    """
    rebuild_projection = Projection()

    def _trigger(event: Event):
        # rich-injection: memory_seed events are already explicit memory
        # declarations, not raw gameplay events needing memory-worthiness
        # detection — bypass TriggerEngine entirely so its contract ("does
        # this real event become a memory") stays clean. This closure is the
        # one path shared by first-boot seeding and any future empty-table
        # rebuild (see _seed_memories), so memory_seed only needs handling
        # here, not a second time at seeding.
        if event.type == "memory_seed":
            payload = event.payload
            agent_id = payload.get("agent_id")
            if not agent_id:
                # memories.agent_id is NOT NULL — an uncaught IntegrityError
                # here would abort the whole rebuild loop, not just this
                # event. _seed_memories never emits a memory_seed without an
                # agent_id, but this closure also runs on replay of whatever
                # is actually in the log, so it degrades instead of trusting
                # the writer (this file's established "malformed data never
                # blocks boot" pattern, e.g. _duties_for/_normalize_location_entry).
                logger.warning("memory_seed event (seq=%s) has no agent_id; skipping", event.seq)
                return None
            return MemoryDescriptor(
                agent_id=agent_id,
                tick=event.ts,
                kind=payload.get("kind", "seed"),
                summary=payload.get("summary", ""),
                importance=payload.get("importance", 0.5),
                anchor=bool(payload.get("anchor", False)),
                event_seq=event.seq,
            )
        ev = {
            "seq": event.seq, "ts": event.ts, "type": event.type,
            "who": event.who, "loc": event.loc, "payload": event.payload,
        }
        descriptor = trigger_engine.process(ev, rebuild_projection)
        project_events([event], base=rebuild_projection)
        return descriptor

    memory_store.rebuild(persisted, trigger=_trigger)


def _seed_world_defs(
    location_store: LocationStore, bt_store: BTStore, world_seed: dict[str, Any] | None
) -> None:
    """Seed the definition tables once (empty-table no-op afterwards).

    Locations come from world_seed.json (fallback: the ids in `GRID`), grid
    coordinates from `locations.GRID`; the action table is generated from the
    live roster (`go_to_<loc>` / `chat_with_<agent>`) so it can't drift into
    ghost references the way the old hardcoded `ActionTable.default()` did.
    """
    if world_seed is not None:
        loc_entries = [_normalize_location_entry(loc, i, len(world_seed["locations"]))
                       for i, loc in enumerate(world_seed["locations"])]
        agent_ids = [a["id"] for a in world_seed["agents"]]
    else:
        loc_entries = [dict(p) for p in DEFAULT_POINTS]
        agent_ids = [e["id"] for e in CHARACTER_ROSTER]
    location_store.seed_defaults(loc_entries)
    point_ids = [e["id"] for e in loc_entries if e.get("kind", "point") == "point"]
    bt_store.seed_defaults(agent_ids=agent_ids, location_ids=point_ids)


def _seed_initial_world(
    event_log: EventLog,
    scheduler: Scheduler,
    world_seed: dict[str, Any] | None = None,
    location_store: LocationStore | None = None,
) -> None:
    """Persist the initial web world once for empty databases.

    Location definitions are read from the `locations` table when a store is
    given (the table is seeded before this runs); the genesis events keep the
    world's history complete, but the table owns the current definition."""
    if location_store is not None:
        entries = location_store.all()
    elif world_seed is not None:
        entries = [_normalize_location_entry(loc, i, len(world_seed["locations"]))
                   for i, loc in enumerate(world_seed["locations"])]
    else:
        entries = [dict(p) for p in DEFAULT_POINTS]
    # Genesis registers the existence of the places an agent can stand in;
    # regions are map structure and carry no geometry into the log (D7).
    for entry in entries:
        if entry.get("kind", "point") != "point":
            continue
        event_log.append({
            "ts": 0,
            "type": "location_join",
            "loc": entry["id"],
            "payload": {
                "id": entry["id"],
                "name": entry.get("name", entry["id"]),
                "description": entry.get("description", ""),
            },
        })
    for aid, brain in scheduler.agents.items():
        agent = brain.agent
        event_log.append({
            "ts": 0,
            "type": "agent_join",
            "who": aid,
            "loc": agent.location,
            "payload": {
                "spec": {
                    "name": agent.name,
                    "personality": agent.blackboard.read("personality") or "",
                },
                "state": {},
                "location": agent.location,
            },
        })

    if world_seed is not None:
        registered_ids = set(scheduler.agents)
        _seed_relations(event_log, registered_ids, world_seed)
        _seed_goals(event_log, registered_ids, world_seed)
        _seed_memories(event_log, registered_ids, world_seed)


def _coerce_bool(value: Any) -> bool:
    """`"false"`/`"False"`/`"0"` parse as Python str, and `bool("false")` is
    True — a hand-authored seed file (JSON, not Python) is exactly the kind
    of input where someone quotes a boolean by mistake."""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no")
    return bool(value)


def _seed_relations(event_log: EventLog, registered_ids: set[str], world_seed: dict[str, Any]) -> None:
    """rich-injection: initial relation values, reusing the existing
    sentiment/r_type state_change genesis semantics — zero new projection
    code. Only emitted for agents that actually got registered this boot.

    Both directions are seeded (relations[(a,b)] AND relations[(b,a)]),
    matching how live `chat` always emits a symmetric pair of sentiment
    events (actions.py `to_event`) — a single one-directional event would
    leave the other agent's view of the relationship at the Relation()
    default, silently, for any seed declaring a mutual relationship."""
    for rel in world_seed.get("relations", []):
        a, b = rel.get("a"), rel.get("b")
        if a not in registered_ids or b not in registered_ids:
            continue
        if "sentiment" in rel:
            for as_id, target_id in ((a, b), (b, a)):
                event_log.append({
                    "ts": 0,
                    "who": as_id,
                    "type": "state_change",
                    "payload": {"kind": "sentiment", "as": as_id, "target": target_id, "sentiment": rel["sentiment"]},
                })
        if "r_type" in rel or "r_type_back" in rel:
            r_type = rel.get("r_type", "acquaintance")
            r_type_back = rel.get("r_type_back", "acquaintance")
            for as_id, target_id, fwd, back in ((a, b, r_type, r_type_back), (b, a, r_type_back, r_type)):
                event_log.append({
                    "ts": 0,
                    "who": as_id,
                    "type": "state_change",
                    "payload": {
                        "kind": "r_type", "as": as_id, "target": target_id,
                        "r_type": fwd, "r_type_back": back,
                    },
                })


def _seed_goals(event_log: EventLog, registered_ids: set[str], world_seed: dict[str, Any]) -> None:
    """rich-injection: per-agent `goals` (same place as `duties`), merged
    into agent.spec via the existing persona_update semantics. Data only —
    BT/planner do not read this field (D9)."""
    for entry in world_seed.get("agents", []):
        aid = entry.get("id")
        goals = _coerce_goals(entry.get("goals"))  # a raw string would persist and char-split forever
        if aid not in registered_ids or not goals:
            continue
        event_log.append({
            "ts": 0,
            "who": aid,
            "type": "state_change",
            "payload": {"kind": "persona_update", "spec": {"goals": goals}},
        })


def _seed_memories(event_log: EventLog, registered_ids: set[str], world_seed: dict[str, Any]) -> None:
    """rich-injection: initial memories as `memory_seed` genesis events —
    event-sourced (D10) so a future memories-table rebuild can't lose them.
    Folded into MemoryStore by `_rebuild_memories`'s trigger closure, not
    here — first-boot seeding and rebuild share that one path."""
    for mem in world_seed.get("memories", []):
        aid = mem.get("agent_id")
        if aid not in registered_ids:
            logger.warning("world_seed memory references unknown agent %r; skipping", aid)
            continue
        event_log.append({
            "ts": 0,
            "who": aid,
            "type": "memory_seed",
            "payload": {
                "agent_id": aid,
                "kind": mem.get("kind", "seed"),
                "summary": mem.get("summary", ""),
                "importance": mem.get("importance", 0.5),
                "anchor": _coerce_bool(mem.get("anchor", False)),
            },
        })


def _seed_capability_catalog(
    event_log: EventLog, config_store: Any | None, force_mock_llm: bool = False
) -> None:
    """Persist the phase-1 capability catalog once for empty databases (D9/D10).

    Data only — `ActionTable`/BT execution (`actions.py`) is not wired to
    read from this catalog in this change.
    """
    catalog = generate_capability_catalog(config_store, force_mock=force_mock_llm)
    for entry in catalog:
        event_log.append({
            "ts": 0,
            "type": "capability_registered",
            "payload": {
                "id": entry.get("id"),
                "kind": entry.get("kind", ""),
                "description": entry.get("description", ""),
                "params_schema": entry.get("params_schema", {}),
            },
        })


def run_story(args: argparse.Namespace) -> int:
    """Run M2 story simulation."""
    n = args.agents
    provider = create_narrative_provider_from_env() if args.narrative else None
    scheduler = Scheduler(narrative_provider=provider)

    world_seed = _load_world_seed()
    locs = ["cafe", "workshop", "home"]
    for i in range(n):
        entry = _roster_entry(i, locs, world_seed)
        bt = Selector([Action(lambda bb: Status.SUCCESS, "go_to_cafe")])
        agent = Agent(id=entry["id"], name=entry["name"], bt_root=bt, location=entry["location"])
        agent.blackboard.write("loc", entry["location"])
        agent.blackboard.write("personality", entry["personality"])
        brain = Brain(agent=agent, action_table=ActionTable.default())
        scheduler.register(brain)

    print(f"[story] starting with {n} agent(s), {args.ticks} ticks\n")
    for _ in range(args.ticks):
        scheduler.tick()
        if provider and scheduler.narrative_history:
            last = scheduler.narrative_history[-1]
            t = scheduler.clock
            print(f"[{t:04d}] {last}")

    print(f"\n[story] done. clock={scheduler.clock}")
    return 0


def run_serve(args: argparse.Namespace) -> int:
    """Run the M3 FastAPI web chat server."""
    import signal

    import uvicorn

    from anima_world.world.app import create_app

    try:
        scheduler = build_serve_scheduler(
            args.agents, db_path=args.db_path, seed_path=args.seed, beats_path=args.beats
        )
    except BeatScriptError as exc:
        print(f"[serve] {exc}", file=sys.stderr)
        return 2
    raw_service_tokens = os.getenv(args.platform_service_token_env, "")
    claim_secret = os.getenv(args.membership_claim_secret_env)
    service_tokens, claim_secret = _runtime_service_credentials(
        args.host, raw_service_tokens, claim_secret
    )
    if bool(service_tokens) != bool(claim_secret):
        print(
            "[serve] platform service token and membership claim secret must be configured together",
            file=sys.stderr,
        )
        return 2
    server_ref: dict[str, Any] = {}

    def request_shutdown() -> None:
        server = server_ref.get("server")
        if server is not None:
            server.should_exit = True

    app = create_app(
        scheduler,
        tick_rate=args.tick_rate,
        config_store=scheduler.config_store,
        prompt_store=scheduler.prompt_store,
        instance_id=args.instance_id,
        world_id=args.world_id,
        world_name=args.world_name,
        admin_token=os.getenv(args.world_admin_token_env),
        shutdown_callback=request_shutdown,
        cors_origins=args.cors_origin,
        platform_service_credentials=service_tokens,
        membership_claim_secret=claim_secret,
        legacy_player_routes=_resolve_legacy_flag(args),
    )
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    server_ref["server"] = server
    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)

    def request_exit(signum, frame) -> None:
        server.should_exit = True

    signal.signal(signal.SIGINT, request_exit)
    signal.signal(signal.SIGTERM, request_exit)
    try:
        server.run()
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
    return 0


def _preflight_llm(config_store: Any | None) -> str | None:
    """Probe the real LLM stack before a fast-forward run; error string or None.

    sim-ff-usability: Window-1 Round 1 ran an entire "real" benchmark round
    on a silently-degraded mock (empty key → MockLLMClient → template
    narrative + planless agents) and the review was worthless. A benchmark
    run must be real or fail loudly — never quietly mock.
    """
    from anima_world.llm_client import create_llm_client_from_config
    from anima_world.planner import SyncLLM

    if config_store is None:
        return "no config store — an in-memory world has no LLM configuration"
    api_key = config_store.get("llm.api_key", default="") or ""
    if not api_key:
        return "config.llm.api_key is empty — configure a key or run with --llm mock / --no-llm"
    try:
        reply = SyncLLM(
            create_llm_client_from_config(config_store), config_store=config_store
        ).complete_sync([{"role": "user", "content": "ping——只回复 pong"}])
    except Exception as exc:  # noqa: BLE001 - any probe failure means "not usable"
        return f"LLM probe call failed: {exc}"
    if not (reply or "").strip():
        return "LLM probe call returned empty output"
    return None


def run_simulate(args: argparse.Namespace) -> int:
    """Fast-forward a world headlessly: no sleep, no web server, no uvicorn.

    Builds the exact same scheduler `serve` would (duties/planner/memory/
    persistence all wired), drives the tick loop synchronously, then drains
    the narrative/planner pools and persists a snapshot before exiting —
    the run is meant to be picked up by `serve --db-path` afterward.
    """
    from anima_world.snapshot import create_snapshot, save_snapshot
    from anima_world.world_time import DEFAULT_MINUTES_PER_TICK, TICKS_PER_DAY

    tier = "mock" if args.no_llm else args.llm  # --no-llm wins (back-compat alias)

    # Preflight BEFORE building the scheduler (code review Round 1 #4): first
    # boot genesis-seeds the capability catalog through the LLM, so aborting
    # after construction would leave a fresh DB permanently seeded with the
    # broken-key fallback catalog. Opening the DB + seeding config here is
    # idempotent with what build_serve_scheduler does right after.
    if tier != "mock" and args.db_path is not None:
        conn = open_db(args.db_path)
        try:
            preflight_store = ConfigStore(conn, fernet_key=load_or_create_key(args.db_path))
            seed_config_defaults(preflight_store)
            error = _preflight_llm(preflight_store)
        finally:
            conn.close()
        if error is not None:
            print(f"[simulate] LLM preflight failed: {error}", file=sys.stderr)
            return 2

    try:
        scheduler = build_serve_scheduler(
            args.agents,
            db_path=args.db_path,
            seed_path=args.seed,
            force_mock_llm=(tier == "mock"),
            mock_narrative=(tier == "planner"),
            beats_path=args.beats,
        )
    except BeatScriptError as exc:
        print(f"[simulate] {exc}", file=sys.stderr)
        return 2

    mpt = DEFAULT_MINUTES_PER_TICK
    if scheduler.config_store is not None:
        mpt = scheduler.config_store.get("world.minutes_per_tick", default=mpt)
    ticks = args.ticks if args.ticks is not None else args.days * TICKS_PER_DAY(mpt)

    # sim-ff-usability: a fast-forward burns thousands of ticks during one
    # LLM call, so a plan requested on day D used to install after day D was
    # long gone (28 plans, all day=0, zero consumed — Window-1 Round 2).
    # Wait for in-flight planning whenever it appears (a mid-day replan after
    # a garbage plan counts too — code review Round 1 #1), bounded by a
    # per-world-day budget; two consecutive exhausted days declare the
    # planner dead and stop all further waiting, so the worst case is
    # 2×wait_cap of dead time, never a hung run.
    wait_cap = args.plan_wait_cap
    if wait_cap is None:
        planner_timeout = 30.0
        if scheduler.config_store is not None:
            planner_timeout = scheduler.config_store.get("planner.timeout", default=planner_timeout)
        # The per-day budget must scale with the roster: N agents drain
        # through a 2-worker pool, so a full day's planning takes up to
        # ceil(N/2) serialized LLM calls — a flat 2× timeout declared a
        # merely-SLOW planner dead on the first 15-agent benchmark run
        # (Round-3 smoke: days 0-1 exhausted, day 2 got zero plans).
        batches = max(2, -(-len(scheduler.agents) // 2))  # ceil(N/2), floor 2
        wait_cap = float(planner_timeout) * batches
    no_wait = wait_cap <= 0  # explicit "never wait" mode, not a dead planner

    print(f"[simulate] fast-forwarding {ticks} tick(s) ...")
    current_day: int | None = None
    day_budget = wait_cap
    day_exhausted = False
    exhausted_streak = 0
    planner_gave_up = False
    for _ in range(ticks):
        scheduler.tick()
        if no_wait or planner_gave_up:
            continue
        day = scheduler.world_time().day
        if day != current_day:
            current_day = day
            if not day_exhausted:
                exhausted_streak = 0
            day_budget = wait_cap
            day_exhausted = False
        if day_budget > 0:
            started = time.monotonic()
            idle = scheduler.wait_planning_idle(timeout=day_budget)
            day_budget -= time.monotonic() - started
            if not idle and not day_exhausted:
                day_exhausted = True
                exhausted_streak += 1
                logger.warning(
                    "[simulate] plan wait budget (%.1fs) exhausted on day %d; continuing planless",
                    wait_cap, day,
                )
                if exhausted_streak >= 2:
                    planner_gave_up = True
                    logger.warning(
                        "[simulate] planner declared dead after %d exhausted days; no further waits",
                        exhausted_streak,
                    )

    # A planner we declared dead must not hold the exit hostage either —
    # its in-flight results were written off when the budget fired.
    scheduler.stop(wait=not planner_gave_up)

    if scheduler.event_log is not None:
        # scheduler._memory_projection is folded on every recorded event
        # (Scheduler._apply_memory_trigger), unconditionally — reusing it
        # here avoids a second full replay of a run that may span many
        # world-days' worth of events (code-review Round 1).
        row = scheduler.event_log.conn.execute("SELECT MAX(seq) FROM events").fetchone()
        seq = int(row[0] or 0)
        snap = create_snapshot(scheduler._memory_projection, seq=seq)
        save_snapshot(scheduler.event_log.conn, snap)

    print(f"[simulate] done. clock={scheduler.clock}")
    return 0


def run_world_package(args: argparse.Namespace) -> int:
    """Run the thin CLI around the portable package service."""
    from anima_world.world_package import (
        PackageValidationError,
        export_world_package,
        import_world_package,
    )

    try:
        if args.world_command == "export":
            manifest = export_world_package(
                db_path=args.db_path,
                seed_path=args.seed,
                beats_path=args.beats,
                output_path=args.output,
                world_id=args.world_id,
                name=args.name,
                mode=args.mode,
                summary=args.summary,
                genre=args.genre,
                setting=args.setting,
                theme=args.theme,
            )
            result = {
                "operation": "export",
                "world_id": manifest.world_id,
                "revision_id": manifest.revision_id,
                "mode": manifest.export_mode,
            }
        else:
            imported = import_world_package(args.package, args.destination)
            result = {
                "operation": "import",
                "world_id": imported.world_id,
                "instance_id": imported.instance_id,
                "path": str(imported.path),
            }
    except (OSError, PackageValidationError):
        print("[world package] operation failed: invalid or inaccessible package data", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _build_author_app(
    db_path: str | Path,
    data_dir: str | Path | None,
    db_editor_port: int | None = None,
    db_editor_url: str | None = None,
):
    """Build and return a FastAPI author studio app (testable without uvicorn).

    Derives data_dir from db_path when not given: <db stem>-data/ next to the db.
    Creates the db parent directory (and data_dir's novels subdirectory on lifespan
    start) so callers need not pre-create anything.
    """
    from anima_world.author.app import create_author_app
    from anima_world.author.store import AuthorStore
    from anima_world.llm_client import create_llm_client_from_env

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if data_dir is None:
        data_dir = db_path.parent / (db_path.stem + "-data")
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    store = AuthorStore(db_path)
    return create_author_app(
        store,
        llm_factory=create_llm_client_from_env,
        data_dir=data_dir,
        db_editor_port=db_editor_port,
        db_editor_url=db_editor_url,
    )


def run_author_serve(args: argparse.Namespace) -> int:
    """Run the author studio FastAPI server."""
    import uvicorn

    app = _build_author_app(
        args.db,
        data_dir=args.data_dir,
        db_editor_port=args.db_editor_port,
        db_editor_url=args.db_editor_url,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def run_author(args: argparse.Namespace) -> int:
    """Run creator-surface tools (currently: LLM world seed generation)."""
    if args.author_command == "serve":
        return run_author_serve(args)

    import asyncio

    from anima_world.author import SeedGenerationError, generate_world_seed
    from anima_world.llm_client import LLMClient
    from anima_world.narrative import resolve_llm_env_settings

    settings = resolve_llm_env_settings()
    if settings is None:
        print(
            "[author] 生成世界种子需要真实 LLM。请配置 ANIMA_LLM_API_KEY /"
            " OPENAI_API_KEY / LONGCAT_API_KEY（及对应 BASE_URL/MODEL）后重试。",
            file=sys.stderr,
        )
        return 2
    api_key, base_url, model, timeout = settings
    # A whole world seed is a long generation — the chat-sized default timeout
    # aborts it mid-thought.
    llm = LLMClient(api_key=api_key, base_url=base_url, model=model,
                    timeout=max(timeout, 120.0))
    try:
        seed = asyncio.run(
            generate_world_seed(
                llm, args.concept, n_agents=args.agents, n_locations=args.locations
            )
        )
    except SeedGenerationError as exc:
        print(f"[author] 种子生成失败：{exc}", file=sys.stderr)
        return 2
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "operation": "generate",
                "output": str(output),
                "agents": [a["id"] for a in seed["agents"]],
                "locations": [loc["id"] for loc in seed["locations"]],
            },
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "story":
        return run_story(args)
    if args.command == "serve":
        return run_serve(args)
    if args.command == "simulate":
        return run_simulate(args)
    if args.command == "world":
        return run_world_package(args)
    if args.command == "author":
        return run_author(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
