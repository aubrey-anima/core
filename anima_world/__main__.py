"""CLI entrypoint for anima_world (M1 recover, M2 story)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import threading
from pathlib import Path
from typing import Any

from anima_world import onboarding
from anima_world.actions import ActionTable
from anima_world.agent import Agent
from anima_world.beats import BeatScript, BeatScriptError, coerce_goals
from anima_world.brain import Brain
from anima_world.bt_nodes import Action, Condition, NeedAction, Selector, Sequence, Status, default_bt
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
from anima_world.world_time import DEFAULT_MINUTES_PER_TICK

logger = logging.getLogger(__name__)

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anima-world",
        description="ANIMA 世界引擎:运行世界、快进、打包成 .cyberworld",
    )
    sub = parser.add_subparsers(dest="command")

    # -- start / config / doctor: the commands a person types --
    start = sub.add_parser(
        "start", help="创建并启动一个世界(引导配置 LLM,前台运行)—— 从这里开始"
    )
    start.add_argument("--db-path", default="saves/world.db", help="世界文件位置(不存在就新建)")
    start.add_argument("--seed", default=None, help="世界种子 JSON(只对新建的世界生效)")
    start.add_argument("--beats", default=None, help="节拍脚本 JSON")
    start.add_argument("--no-input", action="store_true", help="不要交互提问(CI / 脚本)")
    start.add_argument(
        "--real-time", action="store_true",
        help="新世界也用真实时间(5 现实分钟 = 5 世界分钟),不用演示速度",
    )

    config_cmd = sub.add_parser("config", help="读写世界配置(LLM 密钥、时钟快慢…),不用 curl")
    config_cmd.add_argument("--db-path", default="saves/world.db", help="世界文件位置")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    config_list = config_sub.add_parser("list", help="列出全部配置(密钥自动打码)")
    config_list.add_argument("--category", default=None, help="只看某一类:llm / scheduler / chat …")
    config_get = config_sub.add_parser("get", help="读一个配置项")
    config_get.add_argument("key")
    config_set = config_sub.add_parser("set", help="改一个配置项(立即生效,无需重启)")
    config_set.add_argument("key")
    config_set.add_argument("value")

    doctor = sub.add_parser("doctor", help="体检:世界文件、密钥、LLM 连通性、时钟快慢")
    doctor.add_argument("--db-path", default="saves/world.db", help="世界文件位置")
    doctor.add_argument("--skip-probe", action="store_true", help="不要真的调用一次 LLM")

    # -- story (M2) --
    story = sub.add_parser("story", help="Run agent story simulation")
    story.add_argument("--agents", type=int, default=3, help="Number of agents (default 3)")
    story.add_argument("--ticks", type=int, default=50, help="Max ticks (default 50)")
    story.add_argument("--narrative", action="store_true", help="Enable narrative output")

    run = sub.add_parser(
        "run", help="Run a world's clock in the foreground until Ctrl-C (no onboarding)"
    )
    run.add_argument("--db-path", default="saves/world.db", help="SQLite world DB path")
    run.add_argument("--seed", default=None, help="World seed JSON path (default: bundled world_seed.json)")
    run.add_argument(
        "--beats", default=None,
        help="Beat script JSON path (beat-director; invalid script fails startup)",
    )
    run.add_argument(
        "--agents", type=int, default=None,
        help="Number of agents (default: full seed roster, or 3 without a seed)",
    )
    run.add_argument("--quiet", action="store_true", help="Do not echo narrative events")
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
    # A mock LLM cannot produce a parseable plan — every attempt ends in
    # "produced no usable steps", once per agent per world day, which at demo
    # speed is a wall of warnings about a thing that was never going to work.
    # Not wiring it is the same behavior (idle_wander) without the noise.
    if force_mock_llm or not (config_store.get("llm.api_key", default="") or ""):
        logger.info(
            "no usable LLM configured: free-time planning is off and agents fall back to "
            "idle_wander (configure one with `anima-world config set llm.api_key …`)"
        )
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


def _warn_if_llm_degraded(config_store: ConfigStore, db_path: str | Path) -> None:
    """Say out loud, at boot, that this world will run on the Mock LLM.

    An unset key and an unreadable one both read as "unset" downstream, and the
    result is a world that boots fine, ticks fine, and produces nothing but
    template text — the failure mode `onboarding.probe_llm` exists to stop for
    `simulate`. `serve` cannot borrow that check (a world image that refuses to
    start is worse than a degraded one), so it warns instead. `start` reports
    the same thing in its banner and turns this off.
    """
    if "llm.api_key" in config_store.undecryptable_secrets():
        logger.warning(
            "llm.api_key cannot be decrypted — the keyfile %s.key most likely did not travel "
            "with this database. Narrative, free-time planner and relationship judge all "
            "degrade to Mock. Restore the keyfile, or write a new key with "
            "anima-world config set llm.api_key sk-… .",
            db_path,
        )
    elif not (config_store.get("llm.api_key", default="") or ""):
        logger.warning(
            "llm.api_key is not configured — narrative, free-time planner and relationship "
            "judge degrade to Mock (the world still runs, but its text is templated and its "
            "agents have no plans). Set it with `anima-world config set llm.api_key sk-…`, or seed it from "
            "ANIMA_LLM_API_KEY before the database is first created."
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
            _ensure_need_actions(bt_store)
            bt_root = bt_store.build_tree(aid)
            action_table = bt_store.action_table()
        else:
            bt_root = default_bt()
            action_table = ActionTable.default()
        bt_root = _wrap_with_needs_band(bt_root)
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
    llm_warning: bool = True,
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
        # An explicitly-mocked run is not "degraded", and `start` reports the
        # same thing far better in its own banner (llm_warning=False).
        if llm_warning and not force_mock_llm:
            _warn_if_llm_degraded(config_store, db_path)
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
        # economy-v4: default items + cafe shelf, empty-table-only (authored
        # rows always win). Costless while economy.enabled stays off.
        from anima_world.economy import seed_defaults as seed_economy_defaults

        with shared_lock:
            seed_economy_defaults(conn)
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
    # memory-2.0: the reflector proposes insights; the event log records.
    # Mock tier gets a deterministic one-liner so reflection is testable offline.
    reflector = None
    if config_store is not None:
        if force_mock_llm:
            def reflector(name, personality, summaries):  # noqa: ARG001
                if not summaries:
                    return []
                return [f"{name}把最近的事在心里过了一遍:{summaries[0][:30]}"]
        else:
            reflector_llm = SyncLLM(
                create_llm_client_from_config(config_store),
                config_store=config_store,
                timeout_key="judge.timeout",
            )

            def reflector(name, personality, summaries):
                prompt = (
                    f"你是{name}({personality})。下面是你最近的经历:\n"
                    + "\n".join(f"- {s}" for s in summaries[:10])
                    + "\n\n从这些经历里归纳出 1~2 条你此刻会有的想法或领悟,"
                    "每条一行,第一人称,不超过 40 字,不要编号。"
                )
                raw = reflector_llm.complete_sync([{"role": "user", "content": prompt}])
                return [line.strip("-· ").strip() for line in raw.splitlines() if line.strip()][:2]
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
        reflector=reflector,
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
    if seed_path is not None and persisted:
        # The seed file is first-boot-only (M6 D7). Editing one and pointing it
        # at a world that already exists looks like it should work and silently
        # does nothing — the event log and the definition tables are the running
        # world's only source of truth from genesis onward.
        logger.warning(
            "--seed %s was NOT applied: this database already holds %d event(s), and a seed "
            "file is only ever read into an empty one. Point --db-path at a fresh file to "
            "author against this seed, or edit the live world through World.config_set / "
            "World.prompt_set and the locations/bt_nodes tables.",
            seed_path, len(persisted),
        )
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
            _ensure_need_actions(bt_store)
            bt_root = bt_store.build_tree(entry["id"])
        else:
            bt_root = default_bt()
        bt_root = _wrap_with_needs_band(bt_root)
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

    # economy-v4: genesis stipend (安家费). Costless when economy stays off —
    # payment events just fold into an unused ledger.
    from anima_world.economy import TOWN

    for aid in scheduler.agents:
        event_log.append({
            "ts": 0, "who": aid, "type": "payment",
            "payload": {"from": TOWN, "to": aid, "amount": 30.0, "reason": "genesis_stipend"},
        })

    if world_seed is not None:
        registered_ids = set(scheduler.agents)
        _seed_relations(event_log, registered_ids, world_seed)
        _seed_goals(event_log, registered_ids, world_seed)
        _seed_memories(event_log, registered_ids, world_seed)


def _wrap_with_needs_band(bt_root: Any) -> Any:
    """needs-v3: the urgent-needs band sits ABOVE the authored tree — a
    starving agent eats before it opens the shop. Wrapped unconditionally:
    NeedAction is inert (FAILURE) until the scheduler settles `need.*` onto
    the blackboard, which only happens when `needs.enabled` is on, so an
    unlit world behaves tick-for-tick like before."""
    from anima_world.needs import URGENT

    return Selector(children=[
        NeedAction("energy", URGENT, "go_sleep"),
        NeedAction("hunger", URGENT, "eat"),
        NeedAction("social", URGENT, "idle_social"),
        bt_root,
    ])


def _ensure_need_actions(bt_store: Any) -> None:
    """The need band's leaf ids must resolve in the action table, or the
    lookup falls back to idle_wander and a hungry agent just... wanders."""
    existing = {row["node_id"] for row in bt_store.actions()}
    for node_id, kind in (("eat", "eat"), ("go_sleep", "sleep"), ("idle_social", "idle_social")):
        if node_id not in existing:
            bt_store.set_action(node_id, kind, {})


def _coerce_bool(value: Any) -> bool:
    """`"false"`/`"False"`/`"0"` parse as Python str, and `bool("false")` is
    True — a hand-authored seed file (JSON, not Python) is exactly the kind
    of input where someone quotes a boolean by mistake."""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no")
    return bool(value)


def _seed_entry_dicts(world_seed: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Malformed seed data must never block startup (the module-wide contract
    _duties_for/_normalize_location_entry already honor) — and these seeders
    run AFTER genesis events are committed, so a crash here would strand a
    half-initialized world that the non-empty-db check then skips forever.
    Degrade per entry, loudly."""
    entries = world_seed.get(key, [])
    if entries in (None, []):
        return []
    if not isinstance(entries, list):
        logger.warning(
            "world_seed %r must be a list, got %s; skipping all %s",
            key, type(entries).__name__, key,
        )
        return []
    good: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, dict):
            good.append(entry)
        else:
            logger.warning("world_seed %s[%d] is not an object; skipping", key, index)
    return good


def _seed_relations(event_log: EventLog, registered_ids: set[str], world_seed: dict[str, Any]) -> None:
    """rich-injection: initial relation values, reusing the existing
    sentiment/r_type state_change genesis semantics — zero new projection
    code. Only emitted for agents that actually got registered this boot.

    Both directions are seeded (relations[(a,b)] AND relations[(b,a)]),
    matching how live `chat` always emits a symmetric pair of sentiment
    events (actions.py `to_event`) — a single one-directional event would
    leave the other agent's view of the relationship at the Relation()
    default, silently, for any seed declaring a mutual relationship."""
    for rel in _seed_entry_dicts(world_seed, "relations"):
        a, b = rel.get("a"), rel.get("b")
        if not isinstance(a, str) or not isinstance(b, str):
            logger.warning("world_seed relation has non-string agent ids (%r, %r); skipping", a, b)
            continue
        if a not in registered_ids or b not in registered_ids:
            continue
        if "sentiment" in rel and not isinstance(rel["sentiment"], (int, float)):
            logger.warning(
                "world_seed relation (%s, %s) sentiment %r is not numeric; skipping sentiment",
                a, b, rel["sentiment"],
            )
        elif "sentiment" in rel:
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
    for entry in _seed_entry_dicts(world_seed, "agents"):
        aid = entry.get("id")
        goals = _coerce_goals(entry.get("goals"))  # a raw string would persist and char-split forever
        if not isinstance(aid, str) or aid not in registered_ids or not goals:
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
    for mem in _seed_entry_dicts(world_seed, "memories"):
        aid = mem.get("agent_id")
        if not isinstance(aid, str) or aid not in registered_ids:
            logger.warning("world_seed memory references unknown agent %r; skipping", aid)
            continue
        importance = mem.get("importance", 0.5)
        try:
            importance = float(importance)
        except (TypeError, ValueError):
            logger.warning(
                "world_seed memory for %s has non-numeric importance %r; using 0.5",
                aid, importance,
            )
            importance = 0.5
        event_log.append({
            "ts": 0,
            "who": aid,
            "type": "memory_seed",
            "payload": {
                "agent_id": aid,
                "kind": str(mem.get("kind", "seed")),
                "summary": str(mem.get("summary", "")),
                "importance": importance,
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


def _run_world_foreground(world: Any, *, quiet: bool = False) -> None:
    """Drive an opened World in the foreground until SIGINT/SIGTERM.

    The engine is a library — "running a world" just means some process holds
    a World and lets its clock run. This is that process, for anyone who wants
    a living world without writing a host program.
    """
    import queue as queue_module
    import signal

    stop = threading.Event()
    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)

    def request_exit(signum, frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_exit)
    signal.signal(signal.SIGTERM, request_exit)
    events = world.subscribe() if not quiet else None
    world.start_clock()
    try:
        while not stop.is_set():
            if events is None:
                stop.wait(0.5)
                continue
            try:
                batch = events.get(timeout=0.5)
            except queue_module.Empty:
                continue
            for ev in batch.get("events", []):
                if ev.get("type") != "narrative":
                    continue
                text = ev.get("text") or ev.get("payload", {}).get("text", "")
                who = ev.get("who") or "?"
                now = world.world_time()
                print(f"  [第{now.day}天 {now.hour:02d}:{now.minute:02d}] {who}:{text}")
    finally:
        if events is not None:
            world.unsubscribe(events)
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        world.close()


def run_run(args: argparse.Namespace) -> int:
    """Foreground world host: open, let the clock run, Ctrl-C to stop."""
    from anima_world.api import World

    try:
        world = World.open(
            args.db_path, seed_path=args.seed, beats_path=args.beats, agents=args.agents
        )
    except BeatScriptError as exc:
        print(f"[run] {exc}", file=sys.stderr)
        return 2
    roster = "、".join(brain.agent.name for brain in world.scheduler.agents.values())
    print(f"[run] {args.db_path}  {len(world.scheduler.agents)} 个角色:{roster}")
    print("[run] 时钟已启动,Ctrl-C 停止(嵌入用法见 anima_world.api.World)")
    _run_world_foreground(world, quiet=args.quiet)
    print("[run] 世界已停下,快照已保存。")
    return 0


def _open_config_store(db_path: str | Path) -> tuple[Any, ConfigStore]:
    """Open a world's config, creating the database if it isn't there yet.

    Shared by `start` / `config` / `doctor`, all of which need to read or write
    settings without standing up a whole scheduler.

    ConfigStore logs a raw warning per unreadable secret while it hydrates.
    That is right for `serve`, where nothing else would say it, and wrong here:
    these three commands report the same condition properly a moment later, so
    the raw line would just land above their output as noise.
    """
    cfg_logger = logging.getLogger("anima_world.config_store")
    previous_level = cfg_logger.level
    cfg_logger.setLevel(logging.ERROR)
    try:
        conn = open_db(db_path)
        store = ConfigStore(conn, fernet_key=load_or_create_key(db_path), lock=threading.RLock())
        seed_config_defaults(store)
    finally:
        cfg_logger.setLevel(previous_level)
    return conn, store


def _print_llm_line(status: Any, *, indent: str = "  ") -> None:
    mark = onboarding.green(onboarding.OK) if not status.degraded else onboarding.yellow(onboarding.WARN)
    print(f"{indent}{mark} {status.summary}")
    if status.fix:
        print(f"{indent}  {onboarding.dim('修复:' + status.fix)}")


def run_start(args: argparse.Namespace) -> int:
    """The front door: configure, create, run — in that order.

    Everything `run` does, plus the two things a newcomer cannot be expected
    to know: that an unconfigured LLM degrades silently, and that a fresh
    world's clock runs in real time and therefore looks frozen.
    """
    from anima_world.api import World

    db_path = Path(args.db_path)
    is_new_world = not db_path.exists()

    print(onboarding.rule("ANIMA 世界引擎"))

    # ① LLM — first, because it decides what kind of world you get.
    conn, config_store = _open_config_store(db_path)
    status = onboarding.llm_status(config_store, db_path)
    print(f"\n  {onboarding.bold('① LLM')}")
    if status.degraded and not args.no_input and onboarding.can_prompt():
        _print_llm_line(status, indent="     ")
        if onboarding.configure_llm_interactively(config_store, db_path):
            status = onboarding.llm_status(config_store, db_path)
            error = onboarding.probe_llm(config_store)
            if error is None:
                print(f"     {onboarding.green(onboarding.OK)} 连通性测试通过")
            else:
                print(f"     {onboarding.yellow(onboarding.WARN)} 连通性测试没过:{error}")
                print(f"       {onboarding.dim('世界照常启动;改好后用 anima-world doctor 复测')}")
        else:
            print(f"     {onboarding.dim('跳过 —— 这个世界会用 Mock 跑(文本是模板,agent 没有空闲计划)')}")
            print(f"       {onboarding.dim('随时可配:anima-world config set llm.api_key sk-…')}")
    else:
        _print_llm_line(status, indent="     ")

    # ② The world file, and how fast its clock runs.
    print(f"\n  {onboarding.bold('② 世界')}")
    if is_new_world and not args.real_time:
        # The packaged default is real time (1 tick / 5 real minutes), which
        # makes a brand-new world look frozen for the first five minutes
        # somebody watches it. A world they just created gets a visible clock.
        config_store.set("scheduler.tick_rate", 1.0)
    tick_rate = config_store.get("scheduler.tick_rate", default=1.0)
    minutes_per_tick = config_store.get("world.minutes_per_tick", default=DEFAULT_MINUTES_PER_TICK)
    conn.close()

    print(f"     {onboarding.green(onboarding.OK)} {'新建' if is_new_world else '沿用'} {db_path}")
    print(f"     {onboarding.dim('时钟:' + onboarding.human_tick_rate(tick_rate, int(minutes_per_tick)))}")
    if is_new_world and not args.real_time:
        print(f"     {onboarding.dim('想要真实时间:anima-world config set scheduler.tick_rate 0.00333')}")

    try:
        world = World.open(str(db_path), seed_path=args.seed, beats_path=args.beats)
    except BeatScriptError as exc:
        print(f"\n  {onboarding.red(onboarding.BAD)} 节拍脚本有问题:\n{exc}", file=sys.stderr)
        return 2
    print(f"     {onboarding.green(onboarding.OK)} {len(world.scheduler.agents)} 个角色就位:"
          f" {'、'.join(brain.agent.name for brain in world.scheduler.agents.values())}")

    # ③ Let it live. The engine is a library — this process is the world's
    #    host; anything else (a site, a tool) imports anima_world.api and
    #    holds its own World. Authoring lives in the studio, a separate
    #    program, because a world is pinned to one engine version.
    print(f"\n  {onboarding.bold('③ 运行')}")
    print(f"     {onboarding.dim('世界在本进程里运行,叙事会打印在下面;停止:Ctrl-C')}")
    print(f"     {onboarding.dim('程序里用:from anima_world.api import World; World.open(…)')}")
    print(f"     {onboarding.dim('体检:anima-world doctor   改配置:anima-world config list')}\n")

    _run_world_foreground(world)
    print("\n  世界已停下,快照已保存。下次接着跑:"
          f"anima-world start --db-path {db_path}\n")
    return 0


def run_config(args: argparse.Namespace) -> int:
    """Read/write world settings without curl — including the encrypted ones."""
    from anima_world.config_store import mask_secret

    if not Path(args.db_path).exists() and args.config_command != "set":
        print(f"[config] 还没有这个世界:{args.db_path}\n"
              f"         先跑一次 anima-world start 创建它。", file=sys.stderr)
        return 2
    conn, store = _open_config_store(args.db_path)
    try:
        if args.config_command == "list":
            rows = store.list(category=args.category)
            if not rows:
                print(f"[config] 没有 {args.category!r} 这一类。可用类别:"
                      f"{', '.join(sorted({r['category'] for r in store.list()}))}", file=sys.stderr)
                return 2
            width = max(len(r["key"]) for r in rows)
            for row in sorted(rows, key=lambda r: (r["category"], r["key"])):
                value = mask_secret(row["value"] or "") if row["is_secret"] else row["value"]
                if row["is_secret"] and not row["value"]:
                    value = onboarding.dim("(未设置)")
                print(f"  {row['key']:<{width}}  {value}")
                if row["description"]:
                    print(f"  {' ' * width}  {onboarding.dim(row['description'])}")
            return 0

        if not store.has(args.key):
            print(f"[config] 没有 {args.key!r} 这个配置项。"
                  f"用 anima-world config list 看看有哪些。", file=sys.stderr)
            return 2

        if args.config_command == "get":
            meta = store.meta(args.key) or {}
            value = store.get(args.key)
            if meta.get("is_secret"):
                value = mask_secret(value or "") if value else "(未设置)"
            print(value)
            return 0

        # set — coerce to the key's declared type first, or the in-memory cache
        # would hold a string where every reader expects a float/int/bool.
        meta = store.meta(args.key) or {}
        try:
            value = _coerce_config_value(args.value, meta.get("value_type", "str"))
        except ValueError:
            print(f"[config] {args.key} 需要 {meta.get('value_type')} 类型,"
                  f"{args.value!r} 不是。", file=sys.stderr)
            return 2
        store.set(args.key, value)
        shown = mask_secret(str(value)) if meta.get("is_secret") else value
        print(f"{onboarding.green(onboarding.OK)} {args.key} = {shown}")
        if meta.get("is_secret"):
            print(onboarding.dim(f"  已加密写入 {args.db_path};密钥在 {args.db_path}.key —— 搬 db 必须带上"))
        return 0
    finally:
        conn.close()


def _coerce_config_value(raw: str, value_type: str) -> Any:
    if value_type == "int":
        return int(raw)
    if value_type == "float":
        return float(raw)
    if value_type == "bool":
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ValueError(raw)
    return raw


def run_doctor(args: argparse.Namespace) -> int:
    """Check the things that fail quietly. Non-zero exit when one of them has."""
    db_path = Path(args.db_path)
    print(onboarding.rule("体检"))
    problems = 0

    if not db_path.exists():
        print(f"  {onboarding.red(onboarding.BAD)} 没有世界文件:{db_path}")
        print(f"      {onboarding.dim('anima-world start 会创建它')}")
        return 1
    print(f"  {onboarding.green(onboarding.OK)} 世界文件 {db_path}")

    keyfile = Path(f"{db_path}.key")
    if keyfile.exists():
        print(f"  {onboarding.green(onboarding.OK)} 密钥文件 {keyfile.name}(搬 db 时必须一起搬)")
    else:
        print(f"  {onboarding.yellow(onboarding.WARN)} 没有 {keyfile.name} —— 下次启动会新建一把,"
              f"已加密的旧密钥将永久读不出来")

    conn, store = _open_config_store(db_path)
    try:
        from anima_world.db import DB_FORMAT_VERSION, read_db_format

        print(f"  {onboarding.green(onboarding.OK)} db 格式版本 {read_db_format(conn)}"
              f"(本引擎支持到 {DB_FORMAT_VERSION})")

        events, = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        agents, = conn.execute(
            "SELECT COUNT(DISTINCT who) FROM events WHERE type='agent_join'"
        ).fetchone()
        print(f"  {onboarding.green(onboarding.OK)} {agents} 个角色,{events} 条事件")

        status = onboarding.llm_status(store, db_path)
        if status.degraded:
            problems += 1
            print(f"  {onboarding.yellow(onboarding.WARN)} {status.summary}")
            if status.fix:
                print(f"      {onboarding.dim('修复:' + status.fix)}")
        else:
            print(f"  {onboarding.green(onboarding.OK)} {status.summary}"
                  f" {onboarding.dim(status.masked_key or '')}")
            if not args.skip_probe:
                print(f"    {onboarding.dim('正在调用一次 LLM …')}", end="", flush=True)
                error = onboarding.probe_llm(store)
                print("\r" + " " * 30 + "\r", end="")
                if error is None:
                    print(f"  {onboarding.green(onboarding.OK)} LLM 连通性正常")
                else:
                    problems += 1
                    print(f"  {onboarding.red(onboarding.BAD)} LLM 调不通:{error}")
                    print(f"      {onboarding.dim('检查 llm.base_url / llm.model:anima-world config list --category llm')}")

        rate = store.get("scheduler.tick_rate", default=1.0)
        mpt = int(store.get("world.minutes_per_tick", default=DEFAULT_MINUTES_PER_TICK))
        print(f"  {onboarding.green(onboarding.OK)} 时钟 {onboarding.human_tick_rate(rate, mpt)}")
    finally:
        conn.close()

    print()
    if problems:
        print(f"  {onboarding.yellow(str(problems) + ' 项需要处理')}(世界仍然能跑,只是会降级)\n")
        return 1
    print(f"  {onboarding.green('一切正常。')} anima-world start --db-path {db_path}\n")
    return 0


def run_simulate(args: argparse.Namespace) -> int:
    """Fast-forward a world headlessly: no sleep, no clock thread.

    Builds the exact same scheduler `run` would (duties/planner/memory/
    persistence all wired), drives the tick loop synchronously, then drains
    the narrative/planner pools before exiting — the run is meant to be
    picked up by `run --db-path` afterward. Nothing extra is written on the
    way out: every tick's events are already in the log, and the log is the
    only truth a reopen needs.
    """
    from anima_world.world_time import TICKS_PER_DAY

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
            error = onboarding.probe_llm(preflight_store)
        finally:
            conn.close()
        if error is not None:
            print(f"[simulate] LLM 预检没过:{error}\n"
                  f"           配置一个:anima-world config set llm.api_key sk-…\n"
                  f"           或改用 --llm mock / --no-llm 空跑。", file=sys.stderr)
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


def _print_welcome() -> int:
    """What a bare `anima-world` should say: the next command, not a flag dump.

    argparse's help lists eight subcommands with equal weight, which tells a
    newcomer nothing about which one to run first.
    """
    print(onboarding.rule("ANIMA 世界引擎"))
    print(f"""
  第一次用?一条命令就够:

      {onboarding.bold('anima-world start')}      {onboarding.dim('创建并启动一个世界,引导你配 LLM')}

  然后:

      anima-world doctor       {onboarding.dim('体检:密钥、LLM 连通性、时钟快慢')}
      anima-world config list  {onboarding.dim('看/改配置(密钥自动打码)')}

  想造一个自己的世界?那是另一个程序 —— 创作工作台:

      {onboarding.bold('anima-studio')}            {onboarding.dim('桌面程序:管 core 版本 · 小说 → 世界')}
      {onboarding.dim('它把世界钉在某个 core 版本上,所以独立于本引擎单独安装。')}

  前台跑世界 / 快进 / 打包:run / simulate / world export|import
  在程序里嵌入世界:from anima_world.api import World
  完整帮助:anima-world --help
""")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "start":
        return run_start(args)
    if args.command == "config":
        return run_config(args)
    if args.command == "doctor":
        return run_doctor(args)
    if args.command == "story":
        return run_story(args)
    if args.command == "run":
        return run_run(args)
    if args.command == "simulate":
        return run_simulate(args)
    if args.command == "world":
        return run_world_package(args)
    return _print_welcome()


if __name__ == "__main__":
    sys.exit(main())
