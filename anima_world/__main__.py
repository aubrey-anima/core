"""CLI entrypoint for anima_world."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
import logging
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
from anima_world.config_store import ConfigStore
from anima_world import tools as chat_tools
from anima_world.locations import DEFAULT_POINTS
from anima_world.memory_store import MemoryDescriptor
from anima_world.memory_triggers import TriggerEngine
from anima_world.llm_client import MockLLMClient, create_llm_client_from_config
from anima_world.narrative import (
    MockNarrativeProvider,
    create_narrative_provider_from_env,
    generate_capability_catalog,
)
from anima_world.planner import Planner, SyncLLM
from anima_world.projection import project_events
from anima_world.scheduler import Scheduler
from anima_world.types import Event, Projection
from anima_world.rules import parse_rules
from anima_world.world_seed import WorldSeedError, apply_seed_config
from anima_world.world_seed import world_seed_errors as _world_seed_errors
from anima_world.world_time import DEFAULT_MINUTES_PER_TICK

logger = logging.getLogger(__name__)

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_WORLD_ID = "world"


def _redis_url_default() -> str:
    return os.environ.get("ANIMA_REDIS_URL", DEFAULT_REDIS_URL)


def _world_id_default() -> str:
    return os.environ.get("ANIMA_WORLD_ID", DEFAULT_WORLD_ID)


def _add_world_args(p: argparse.ArgumentParser, *, suppress: bool = False,
                    mysql: bool = True) -> None:
    """每个碰世界的子命令共用的三个参数。世界文件退役后,世界 = Redis 上的一个前缀。

    `suppress=True` 给嵌套子命令的叶子用(config set 那组):叶子的解析结果会盖掉
    组一级的 namespace,普通默认值会把组上给的 --redis 抹成默认 —— SUPPRESS 让
    "没写"就是"不出现",组一级的值得以幸存(world.db 时代 --db-path 的同一个坑)。
    """
    default = argparse.SUPPRESS if suppress else None
    p.add_argument("--redis", default=default, metavar="URL",
                   help="世界所在的 Redis(默认 $ANIMA_REDIS_URL 或 redis://127.0.0.1:6379/0)")
    p.add_argument("--world-id", default=default,
                   help="世界的名字,进 Redis 键前缀(默认 $ANIMA_WORLD_ID 或 world)")
    if mysql:
        p.add_argument("--mysql", default=default, metavar="DSN",
                       help="可选:无限增长的历史归 MySQL(mysql://user:pass@host:3306/db)")


def _connect_redis(url: str | None):
    try:
        import redis as redis_mod
    except ImportError:
        print("要连世界得先装 redis:pip install anima-world[redis]", file=sys.stderr)
        raise SystemExit(2) from None
    resolved = url or _redis_url_default()
    client = redis_mod.Redis.from_url(resolved, decode_responses=True)
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001 - 连不上要说人话
        print(f"连不上 Redis({resolved}):{exc}", file=sys.stderr)
        print("世界现在住在 Redis 里 —— 先起一个:docker run -p 6379:6379 redis "
              "--appendonly yes", file=sys.stderr)
        raise SystemExit(2) from None
    return client


def _mysql_factory(dsn: str | None):
    """DSN → 连接工厂。**返回工厂而不是连接**:pymysql threadsafety=1 而引擎有
    线程池,裸连接会让协议帧交叉 —— 引擎按每线程一条包装工厂。"""
    if not dsn:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(dsn)
    if parsed.scheme not in ("mysql", "mysql+pymysql"):
        print(f"--mysql 只认 mysql:// DSN,收到:{dsn}", file=sys.stderr)
        raise SystemExit(2)
    try:
        import pymysql
    except ImportError:
        print("要用 MySQL 得先装 pymysql:pip install pymysql", file=sys.stderr)
        raise SystemExit(2) from None
    return lambda: pymysql.connect(
        host=parsed.hostname or "127.0.0.1", port=parsed.port or 3306,
        user=parsed.username or "root", password=parsed.password or "",
        database=(parsed.path or "/").lstrip("/") or None, charset="utf8mb4",
    )


def _world_args(args: argparse.Namespace) -> tuple[Any, str, Any]:
    """(redis, world_id, mysql工厂)。放一个函数里,免得十个命令各解析各的。"""
    redis = _connect_redis(getattr(args, "redis", None))
    world_id = getattr(args, "world_id", None) or _world_id_default()
    mysql = _mysql_factory(getattr(args, "mysql", None))
    return redis, world_id, mysql


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anima-world",
        description="ANIMA 世界引擎:运行世界、快进、打包成 .cyberworld",
    )
    # For an engine whose headline contract is "the version IS the
    # compatibility promise", self-report is a first-class concern: it is the
    # first thing an external tool managing several engines asks. Both formats
    # ride along, since the version alone does not say which db a build opens.
    from anima_world import __version__
    from anima_world.world_package import PACKAGE_FORMAT_VERSION

    parser.add_argument(
        "--version", action="version",
        version=(
            f"anima-world {__version__}"
            f"  (存储:redis;包格式 {PACKAGE_FORMAT_VERSION})"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    # -- start / config / doctor: the commands a person types --
    start = sub.add_parser(
        "start", help="创建并启动一个世界(引导配置 LLM,前台运行)—— 从这里开始"
    )
    _add_world_args(start)
    start.add_argument("--world-file", dest="world_file", default=None,
                       help="世界文件 .cyberworld(只对新建的世界生效;默认内置演示世界)")
    start.add_argument("--beats", default=None, help="节拍脚本 JSON")
    start.add_argument("--no-input", action="store_true", help="不要交互提问(CI / 脚本)")
    start.add_argument(
        "--real-time", action="store_true",
        help="新世界也用真实时间(5 现实分钟 = 5 世界分钟),不用演示速度",
    )

    config_cmd = sub.add_parser("config", help="读写世界配置(LLM 密钥、时钟快慢…),不用 curl")
    _add_world_args(config_cmd)
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)

    def _config_leaf(name: str, help_text: str) -> argparse.ArgumentParser:
        """A config leaf that also takes the world args.

        Registered on the leaf as well as the group because every other
        command takes `--world-id` trailing (`doctor … --world-id w`), and
        argparse stops honouring the group's copy once a leaf's positionals
        have been consumed. Without this, the habit that works everywhere
        else dies on `config set k v --world-id w` with a bare top-level
        usage error. Both positions now mean the same thing; the leaf wins
        when given twice, which is the one the user typed last.
        """
        leaf = config_sub.add_parser(name, help=help_text)
        # SUPPRESS, not None: a subparser writes its defaults onto the SAME
        # namespace, so a plain default would blank out the group's --world-id
        # whenever the leaf's copy is omitted.
        _add_world_args(leaf, suppress=True)
        return leaf

    config_list = _config_leaf("list", "列出全部配置(密钥自动打码)")
    config_list.add_argument("--category", default=None, help="只看某一类:llm / scheduler / chat …")
    config_get = _config_leaf("get", "读一个配置项")
    config_get.add_argument("key")
    config_set = _config_leaf("set", "改一个配置项(立即生效,无需重启)")
    config_set.add_argument("key")
    config_set.add_argument("value")

    doctor = sub.add_parser("doctor", help="体检:世界文件、密钥、LLM 连通性、时钟快慢")
    _add_world_args(doctor)
    doctor.add_argument("--skip-probe", action="store_true", help="不要真的调用一次 LLM")

    chat = sub.add_parser("chat", help="和世界里的一个角色对话 —— 说完就落进世界的历史")
    _add_world_args(chat)
    chat.add_argument(
        "--agent", default=None,
        help="要找谁说话(角色 id);不给就列出这个世界住着谁",
    )
    chat.add_argument("--player-id", default="cli", help="你的身份 id —— 角色对你的印象记在它头上")
    chat.add_argument("--name", default=None, help="你在角色眼里的称呼(默认「访客」)")
    chat.add_argument("--list", action="store_true", dest="list_only", help="只列出角色名册就退出")

    world_map = sub.add_parser(
        "map", help="把地图画出来 —— 谁在哪、这段时间里谁去了哪儿",
    )
    _add_world_args(world_map)
    world_map.add_argument("--agent", action="append", default=None,
                           help="只看这几个人(可重复);不给就是全部")
    world_map.add_argument("--day", type=int, default=None,
                           help="只看第几个世界日(1 起)。和 --from/--to 二选一")
    world_map.add_argument("--from", dest="from_tick", type=int, default=None, help="起始 tick")
    world_map.add_argument("--to", dest="to_tick", type=int, default=None, help="结束 tick")
    world_map.add_argument("--now", action="store_true",
                           help="只画此刻:谁站在哪、谁在路上,不画轨迹")
    world_map.add_argument("--watch", nargs="?", type=float, const=2.0, default=None,
                           metavar="秒", help="跟着时钟重画(默认每 2 秒);Ctrl-C 停")
    world_map.add_argument("--width", type=int, default=None, help="画布宽(默认按终端)")
    world_map.add_argument("--height", type=int, default=None, help="画布高")
    world_map.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")

    prompt = sub.add_parser(
        "prompt", help="看一眼某个角色此刻收到的提示词 —— 逐块带来源,并说明少了哪块、为什么",
    )
    _add_world_args(prompt)
    prompt.add_argument("--agent", default=None, help="看谁的(角色 id);不给就列出名册")
    prompt.add_argument("--player-id", default="cli", help="以谁的身份跟她说话")
    prompt.add_argument("--name", default=None, help="你在她眼里的称呼")
    prompt.add_argument("--message", default="在吗", help="假设这一刻你说的是哪句话")
    prompt.add_argument(
        "--full", action="store_true",
        help="连每块的正文一起打(默认只打摘要:块名、字数、首行)",
    )
    prompt.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")

    ontology_cmd = sub.add_parser(
        "ontology", help="世界里有哪些种类的东西、身上有哪些量、能对它们做什么",
    )
    _add_world_args(ontology_cmd)
    ontology_cmd.add_argument("--kind", default=None, help="只看这一类(如 tree)")
    ontology_cmd.add_argument("--builtin", action="store_true",
                              help="连内置种类一起列(agent / location / world / player)")
    ontology_cmd.add_argument(
        "--check", action="store_true",
        help="逐个跑一遍出生自检:量落地了吗、能力算得出结论吗、在场吗(有问题时退出码 1)",
    )
    ontology_cmd.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")

    run = sub.add_parser(
        "run", help="Run a world's clock in the foreground until Ctrl-C (no onboarding)"
    )
    _add_world_args(run)
    run.add_argument("--world-file", dest="world_file", default=None,
                     help="世界文件 .cyberworld(默认内置的 demo.cyberworld)")
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
        "simulate", help="Fast-forward a world headlessly (no sleep, no onboarding)"
    )
    _add_world_args(simulate)
    simulate.add_argument("--world-file", dest="world_file", default=None,
                          help="世界文件 .cyberworld(默认内置的 demo.cyberworld)")
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
    simulate.add_argument(
        "--report", default=None, metavar="PATH",
        help="跑完写一份机器可读的运行摘要 JSON:事件密度、相遇统计、"
             "关系曲线、每人时间分配(`-` = 写到 stdout)",
    )

    events_cmd = sub.add_parser(
        "events", help="事件流的格式中立导出(只导出,不重放,不承诺)"
    )
    events_commands = events_cmd.add_subparsers(dest="events_command", required=True)
    events_export = events_commands.add_parser("export", help="导出成 JSONL")
    _add_world_args(events_export)
    events_export.add_argument("--output", required=True, help="写到文件;`-` = stdout")

    report_cmd = sub.add_parser(
        "report", help="对着一个世界出运行摘要 —— 只读,不跑世界"
    )
    _add_world_args(report_cmd)
    report_cmd.add_argument("--json", action="store_true", help="机器可读输出")
    report_cmd.add_argument("--output", help="写到文件(缺省打到 stdout)")

    validate = sub.add_parser(
        "validate", help="不建世界就检查一份世界文件或一份节拍脚本"
    )
    validate_commands = validate.add_subparsers(dest="validate_command", required=True)
    validate_seed = validate_commands.add_parser(
        "world", aliases=["seed"], help="检查一个世界文件(.cyberworld)"
    )
    validate_seed.add_argument("path", help="世界文件")
    validate_seed.add_argument("--json", action="store_true", help="机器可读输出")
    validate_beats = validate_commands.add_parser("beats", help="检查节拍脚本")
    validate_beats.add_argument("path", help="节拍脚本文件")
    validate_beats.add_argument(
        "--world-file", dest="seed",
        help="配套的世界文件 —— 有它才能查角色/地点的引用是否存在"
    )
    validate_beats.add_argument("--json", action="store_true", help="机器可读输出")

    play = sub.add_parser(
        "play", help="在活着的世界里说话 —— 时钟一边走,你一边聊"
    )
    _add_world_args(play)
    play.add_argument("--agent", help="先跟谁说话(缺省是名册里第一个)")
    play.add_argument("--name", help="你在这个世界里叫什么(缺省「访客」)")
    play.add_argument("--player-id", default="p1")
    play.add_argument("--world-file", dest="world_file", default=None,
                      help="世界文件 .cyberworld(只对新建的世界生效)")
    play.add_argument("--beats", help="Beat script")
    play.add_argument("--agents", type=int, help="Roster size for a brand-new world")

    contract = sub.add_parser(
        "contract", help="引擎自报它的线格式版本与 schema 形状 —— 给持有镜像的仓库对齐用"
    )
    contract.add_argument("--json", action="store_true", help="机器可读输出")

    world = sub.add_parser("world", help="Export or import portable world data packages")
    world_commands = world.add_subparsers(dest="world_command", required=True)
    world_export = world_commands.add_parser("export", help="Export a .cyberworld package")
    _add_world_args(world_export)
    world_export.add_argument("--beats", default=None, help="Optional beats JSON path")
    world_export.add_argument("--output", required=True, help="Output .cyberworld path")
    world_export.add_argument("--package-id", required=True,
                              help="包的世系 id(小写;--world-id 是源世界在 Redis 上的名字)")
    world_export.add_argument("--name", "--title", required=True, help="World display name")
    world_export.add_argument("--summary", default="")
    world_export.add_argument("--genre", default="")
    world_export.add_argument("--setting", default="")
    world_export.add_argument("--theme", default="default")
    world_inspect = world_commands.add_parser(
        "inspect", help="读一个 .cyberworld 需要什么引擎 —— 跑不了也照样回答"
    )
    world_inspect.add_argument("package", help="Package archive path")
    world_inspect.add_argument(
        "--json", action="store_true", dest="as_json",
        help="输出一行 JSON(给启动器/工具消费),而不是给人看的清单",
    )
    world_import = world_commands.add_parser("import", help="Import a .cyberworld package")
    world_import.add_argument("package", help="Package archive path")
    _add_world_args(world_import)

    world_migrate = world_commands.add_parser(
        "migrate",
        help="把一个 1.x 的 world.db 迁成 2.0 的世界文件 —— 一次性的桥",
    )
    world_migrate.add_argument("db", help="1.x 的 world.db 路径")
    world_migrate.add_argument("--output", required=True, help="写出的 .cyberworld")
    world_migrate.add_argument("--package-id", required=True, help="迁过去之后世界叫什么")
    world_migrate.add_argument("--name", default="", help="展示名")
    world_migrate.add_argument("--summary", default="")
    world_migrate.add_argument("--json", action="store_true", help="机器可读输出")

    world_drop = world_commands.add_parser(
        "drop", help="把一个世界从 Redis 上整个抹掉(键前缀下的一切)"
    )
    _add_world_args(world_drop)
    world_drop.add_argument(
        "--yes", action="store_true",
        help="不问就删 —— 脚本用;不给这个参数时只报会删多少个键然后退出",
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


WORLD_FILE_PATH = Path(__file__).parent / "demo.cyberworld"


def _load_world_file(
    path: Path | str = WORLD_FILE_PATH,
    *,
    redis: Any,
    world_id: str,
    mysql: Any = None,
    authored: bool = False,
) -> dict[str, Any]:
    """装一个世界文件:状态记录当场落键,作者记录聚合成编译管线吃的 section 字典。

    **创世和还原走这同一条。** 它们本来就是同一个动作 —— 往一个前缀里装一个世界
    文件。区别只在文件里装的是哪一层记录:一份手写的世界只有 `author` 记录(于是
    走编译),一份导出的世界只有 `redis`/`event`/`mysql` 记录(于是直接落键)。

    **内置的那份会降级,作者指名的那份不会。** 内置文件坏了只 warning 然后回落
    硬编码默认值 —— 一个装坏了的包也得能开机。而 `--world-file` 指名的文件坏了
    当场抛:世界文件只会被读进空库一次,静默换成内置演示世界是**不可挽回**的,
    路径打错重跑一次也救不回来。和节拍脚本、显式种子同一条规矩。
    """
    from anima_world.world_file import WorldFileError, read_world_file
    from anima_world.world_package import install_world_records

    try:
        _, records = read_world_file(path)
        authored = install_world_records(
            records, redis=redis, world_id=world_id, mysql=mysql
        )
        # **作者层还是要过那道闸。** 换掉的是容器,不是校验:一份写错了 `agents`
        # 的世界文件照旧不许开机。而**只有带作者层的文件才查** —— 一个跑过的世界
        # 导出来只有状态记录,拿"agents 必须是个列表"去要求它是在问错问题。
        errors = _world_seed_errors(authored) if authored else []
        if errors:
            raise WorldSeedError([f"{path}: {e}" for e in errors])
        return authored
    except Exception as exc:  # noqa: BLE001 — 坏文件的形状很多,分流只看是不是作者指名的
        if authored:
            if isinstance(exc, (WorldFileError, WorldSeedError)):
                raise
            raise WorldFileError([f"装不进这个世界文件 {path}:{exc}"]) from exc
        logger.warning(
            "内置世界文件读不了(%s);回落硬编码默认值 —— 一个装坏了的包也得能开机", exc
        )
        return {}


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


def _roster_from_events(persisted: list[Event], projection: Any) -> list[dict[str, str]]:
    """The cast of a world that already exists, taken from its own event log.

    A non-empty database is the authority on who lives in it. The seed file is
    read once, into an empty database — so consulting it on restart hands the
    world whatever roster happens to be on disk, which for a host that seeded a
    database and shipped it is the BUNDLED demo cast. The world's own agents
    then never tick again while three strangers append events to it, and
    nothing says so.

    Order follows the log, so a roster stays stable across restarts.
    """
    roster: list[dict[str, str]] = []
    for event in persisted:
        if event.type != "agent_join" or not event.who:
            continue
        if any(entry["id"] == event.who for entry in roster):
            continue
        projected = getattr(projection, "agents", {}).get(event.who)
        spec = projected.spec if projected is not None else dict(event.payload.get("spec") or {})
        location = (projected.location if projected is not None else None) or event.payload.get("location")
        roster.append({
            "id": event.who,
            "name": spec.get("name", event.who),
            "location": location or "",
            "personality": spec.get("personality", ""),
        })
    return roster


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


def _behavior_tree_for(agent_id: str, seed: dict[str, Any] | None) -> list[dict[str, Any]]:
    """作者直接写死的行为树节点(`agents[].behavior_tree`)。

    `duties` 只表达得了"时间窗 → 动作";要写条件分支、需求带、嵌套选择器就够不着。
    这个键在场时**取代** `duties`(两者都写等于给同一棵树两个说法),缺席时行为逐
    tick 不变 —— 与种子里每一个可选字段同一条宽容规矩。
    """
    if not seed:
        return []
    for entry in seed.get("agents", []):
        if entry.get("id") == agent_id:
            nodes = entry.get("behavior_tree")
            return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []
    return []


def _seed_agent_tree(bt_store: Any, agent_id: str, seed: dict[str, Any] | None,
                     duties: list[dict[str, Any]] | None = None) -> None:
    """按种子给一个角色播树:先看 `behavior_tree`,没有再退回 `duties`。"""
    if bt_store.seed_tree(agent_id, _behavior_tree_for(agent_id, seed)):
        return
    bt_store.seed_duties(agent_id, list(duties if duties is not None else _duties_for(agent_id, seed)))


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
        situation_provider=lambda aid: _planner_situation(scheduler, aid),
    )


# 别人这会儿在干什么 —— 给 LLM 看的说法,不是内部 kind。
_ACTION_LABELS = {
    "work": "上班", "sleep": "睡觉", "eat": "吃东西", "chat": "跟人说话",
    "walk": "赶路", "idle_wander": "闲着", "idle_social": "闲着",
    None: "闲着",
}


def _planner_situation(scheduler: Any, agent_id: str) -> dict[str, Any]:
    """规划器排一天之前,先看一眼此刻的世界。

    此前它只知道"我是谁、有哪些空窗、能做什么、记得什么" —— 看不见自己站在哪、
    饿不饿、有没有钱、别人这会儿在忙什么。于是它排出来的一天依据比世界实际拥有的
    信息少得多:让一个在家的人"继续在咖啡店待着",让身无分文的人去买东西。

    纯读,拿锁,不碰 LLM。任何一块读不出来就少一行,规划不该死于一次世界读。
    """
    ctx: dict[str, Any] = {}
    with scheduler._lock:
        brain = scheduler.agents.get(agent_id)
        if brain is None:
            return ctx
        loc_id = brain.agent.blackboard.read("loc") or brain.agent.location or ""
        if loc_id and scheduler.location_store is not None:
            row = scheduler.location_store.get(loc_id)
            ctx["location"] = (row or {}).get("name") or loc_id
        elif loc_id:
            ctx["location"] = loc_id

        if scheduler._needs_enabled():
            values = {
                need: brain.agent.blackboard.read(f"need.{need}")
                for need in ("energy", "hunger", "social")
            }
            ctx["needs"] = {k: v for k, v in values.items() if isinstance(v, (int, float))}

        if scheduler.config_store is not None and scheduler.config_store.get(
            "economy.enabled", default=False
        ):
            ctx["money"] = float(scheduler._memory_projection.balances.get(agent_id, 0.0))

        others = []
        for other_id, other in scheduler.agents.items():
            if other_id == agent_id:
                continue
            action = scheduler._current_action.get(other_id)
            where = other.agent.blackboard.read("loc") or other.agent.location or "?"
            label = _ACTION_LABELS.get(action.kind if action else None, "闲着")
            others.append(f"{other.agent.name}在{where}{label}")
        if others:
            ctx["others"] = others
    return ctx


def _warn_if_llm_degraded(config_store: ConfigStore, world_id: str) -> None:
    """Say out loud, at boot, that this world will run on the Mock LLM.

    A world that boots fine, ticks fine, and produces nothing but template
    text is the failure mode `onboarding.probe_llm` exists to stop for
    `simulate`. `run` cannot borrow that check (a world that refuses to
    start is worse than a degraded one), so it warns instead. `start`
    reports the same thing in its banner and turns this off.
    """
    if not (config_store.get("llm.api_key", default="") or ""):
        logger.warning(
            "llm.api_key is not configured — narrative, free-time planner and relationship "
            "judge degrade to Mock (world %s still runs, but its text is templated and its "
            "agents have no plans). Set it with `anima-world config set llm.api_key sk-…`.",
            world_id,
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


def _store_genesis_seed(meta: Any, world_seed: dict[str, Any] | None) -> None:
    """Record the seed that is about to populate a FRESH world (1.0.2).

    调用方已经判过"这是个空世界"(fresh_world);这里再守一遍"已有出生证明就
    不改写" —— an existing world's provenance must never be rewritten to
    whatever today's seed is. Live export reads this back so a snapshot
    always carries its true birth certificate.
    """
    if world_seed is None:
        return
    if meta.get("world_seed") is not None:
        return
    meta.put("world_seed", world_seed)


def _apply_seed_config_at_genesis(
    config_store: Any, world_seed: dict[str, Any] | None
) -> None:
    """种子的 `config` 块 —— **只对空世界生效**(调用方以 fresh_world 把关)。

    已有的世界不认:那些开关此时是**运行数据**(作者可能早就 `config set` 改过),
    拿今天的种子回头覆盖它们,等于让一次重启悄悄改掉一个跑了半年的世界的行为。
    跳过的键逐条 warning 点名 —— 作者以为点亮了、实际没点亮,正是这个仓库最
    在意的那类错。
    """
    if world_seed is None:
        return
    report = apply_seed_config(config_store, world_seed)
    for key, reason in report["skipped"]:
        logger.warning("种子里的 config.%s 没有生效:%s", key, reason)
    if report["applied"]:
        logger.info(
            "种子点亮了 %d 个开关:%s",
            len(report["applied"]),
            ", ".join(f"{k}={v}" for k, v in sorted(report["applied"].items())),
        )


def build_serve_scheduler(
    world_id: str,
    redis: Any,
    mysql: Any = None,
    n_agents: int | None = None,
    world_file: str | Path | None = None,
    force_mock_llm: bool = False,
    mock_narrative: bool = False,
    beats_path: str | Path | None = None,
    llm_warning: bool = True,
) -> Scheduler:
    """Build a world that lives in Redis (+ MySQL for the unbounded four).

    world.db 退役(1.5.0):世界的名字是 `world_id`,家是 `redis`。创世 =
    种子直接写进各 Redis store,而且沿用搬家时代的那条纪律 —— **只填缺,
    不覆盖**(每个 seed 函数都是空 store 才播;接一个在跑的世界不许把她按回
    原点)。给了 `mysql=` 的世界,随时间无限增长的四样(events / memories /
    conversations / messages)归 MySQL,判据照旧是"她带不带得进上下文"。

    world_file: 作者指名的世界文件;装载是**严格**的(坏了当场抛,启动失败)。
    只有**内置**那份会降级成硬编码默认值 —— 装坏了的包也得能开机。
    beats_path: same rule (BeatScriptError). force_mock_llm / mock_narrative:
    unchanged from the world.db era — threaded through construction because a
    post-hoc swap is too late for the capability catalog.
    """
    from anima_world.mysql_state import GROWS_FOREVER
    from anima_world.redis_state import (
        RedisBTStore, RedisChatStore, RedisClock, RedisConfigBackend, RedisDict,
        RedisEconomyStore, RedisEventLog, RedisKnowledgeGraph, RedisLocationStore,
        RedisMemoryStore, RedisNeedsStore, RedisCliqueStore, RedisPromptStore,
        RedisOntologyStore, RedisReflectionStore, RedisRulesStore, RedisStockStore,
        RedisVisibilityStore,
        clock_key, current_action_key, decode_action, encode_action, events_key,
        drop_stale_copies_for_mysql, durability_warning, engagements_key, meta_rows,
        plans_key, transit_key,
    )

    beat_script = BeatScript.load(beats_path) if beats_path is not None else None
    shared_lock = threading.RLock()

    warning = durability_warning(redis)
    if warning:
        logger.warning(warning)

    mysql_conn = None
    mysql_prefix = ""
    if mysql is not None:
        from anima_world.mysql_state import as_connection, ensure_schema

        mysql_conn = as_connection(mysql)
        mysql_prefix = f"{world_id}_"
        ensure_schema(mysql_conn, mysql_prefix)

    # **一道门。** 创世和还原本来就是同一个动作:往一个前缀里装一个世界文件。
    # 状态记录当场落键,作者记录聚合成 section 字典交给下面那条编译管线 ——
    # 而落键在前是承重的:装完状态之后世界就不是空的了,于是"这是不是创世"
    # 那个判断自己会给出正确答案,一份只有状态的文件不会再被当成新世界播一遍种。
    # MySQL 连接必须先建好,不然增长的那四样没地方改道。
    world_seed = _load_world_file(
        world_file if world_file is not None else WORLD_FILE_PATH,
        redis=redis, world_id=world_id, mysql=mysql_conn,
        authored=world_file is not None,
    )
    if n_agents is None:
        n_agents = len(world_seed["agents"]) if world_seed else len(CHARACTER_ROSTER)

    meta = meta_rows(redis, world_id)
    # 事件日志 —— 唯一真相。MySQL 接手时 Redis 里不留拷贝(两份真相里一份不更新,
    # 是这个仓库最怕的坏法)。
    if mysql_conn is not None and "events" in GROWS_FOREVER:
        from anima_world.mysql_state import MySQLEventLog

        event_log = MySQLEventLog(mysql_conn, mysql_prefix)
        drop_stale_copies_for_mysql(redis, world_id)
    else:
        event_log = RedisEventLog(redis, events_key(world_id))

    config_store = ConfigStore(RedisConfigBackend(redis, world_id), lock=shared_lock)
    trigger_engine = TriggerEngine(config_store=config_store)
    prompt_store = RedisPromptStore(redis, world_id)
    if mysql_conn is not None and "memories" in GROWS_FOREVER:
        from anima_world.mysql_state import MySQLMemoryStore

        memory_store = MySQLMemoryStore(mysql_conn, mysql_prefix, config_store)
    else:
        memory_store = RedisMemoryStore(redis, world_id, config_store)
    knowledge_graph = RedisKnowledgeGraph(redis, world_id)
    location_store = RedisLocationStore(redis, world_id)
    bt_store = RedisBTStore(redis, world_id)
    stock_store = RedisStockStore(redis, world_id)
    visibility_store = RedisVisibilityStore(redis, world_id)
    rules_store = RedisRulesStore(redis, world_id)
    ontology_store = RedisOntologyStore(redis, world_id)
    economy_store = RedisEconomyStore(redis, world_id)

    # 创世判定:这个 world_id 下还什么都没有。判据和 world.db 时代逐字同构
    # (没有事件、没有地图 = 空世界),只是问的是 store 而不是表。
    fresh_world = event_log.count() == 0 and not location_store.all()
    if fresh_world:
        _store_genesis_seed(meta, world_seed)  # 出生证明随世界走
        # 种子自己带的开关 —— 现在它是 `:config` 里唯一的来源:
        # 剩下的行就是"这个世界的作者决定了什么"。
        _apply_seed_config_at_genesis(config_store, world_seed)
    if llm_warning and not force_mock_llm:
        _warn_if_llm_degraded(config_store, world_id)
    _seed_world_defs(location_store, bt_store, world_seed)
    # economy-v4: default items + cafe shelf, empty-store-only (authored
    # rows always win). #12: the seed's own material layer goes in FIRST,
    # precisely so the demo items find a non-empty store and stand down.
    with shared_lock:
        _seed_material_layer(economy_store, world_seed)
        economy_store.seed_defaults()
    # llm-relationship-judge: judge only exists with a config store (it needs
    # the live llm.* stack); --no-llm gives it a mock client whose garbage
    # reply degrades every verdict to None — chat then simply produces no
    # relationship data, the designed floor.
    relationship_judge = None
    if config_store is not None:
        from anima_world.relationship_judge import (
            DeterministicRelationshipJudge,
            RelationshipJudge,
        )

        # A Mock LLM cannot produce a parseable verdict, so the LLM-backed judge
        # returned None on EVERY call — and "no relationship data at all" is not
        # a smaller version of the feature, it is the feature missing: no band
        # crossings, no relation_shift memories, no graph edges, no gossip
        # source, nothing for the planner to read. Three-axis relations are
        # documented as always-on, yet a no-key world produced zero relationship
        # events, for players and NPCs alike. Same treatment the reflector below
        # already gets: a deterministic stand-in for the mock tier.
        if force_mock_llm or not (config_store.get("llm.api_key", default="") or ""):
            logger.info(
                "no usable LLM configured: relationships drift on a deterministic "
                "stand-in instead of being judged (configure one with "
                "`anima-world config set llm.api_key …`)"
            )
            relationship_judge = DeterministicRelationshipJudge()
        else:
            relationship_judge = RelationshipJudge(
                llm=SyncLLM(
                    create_llm_client_from_config(config_store),
                    config_store=config_store,
                    timeout_key="judge.timeout",
                ),
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
            MockNarrativeProvider(prompt_store=prompt_store)
            if force_mock_llm or mock_narrative
            else create_narrative_provider_from_env(prompt_store, config_store)
        ),
        relationship_judge=relationship_judge,
        reflector=reflector,
        event_log=event_log,
        world_id=world_id,
        meta_store=meta,
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
    # world-rules:存量的持久层 + 世界的规律。规律从种子播进 `world_rules` 表
    # (空库一次,之后表里的行说了算——和地图/行为树同一条契约),坏规律在这里
    # 就当场抛 RuleError,不流到运行期。
    # World.__init__(转录)与外部动词要用到的连接随调度器走。
    scheduler.redis = redis
    scheduler.mysql_conn = mysql_conn
    scheduler.mysql_prefix = mysql_prefix
    # 时钟第一个接上:后面的 load_persisted_events 会拿事件 ts 和它取 max。
    # setnx 只填缺 —— 重开一个世界不许把时钟拨回去。
    scheduler._clock_store = RedisClock(redis, clock_key(world_id), initial=0)
    # 在途 / 当前动作 / 规划:真状态,全进程可见。
    scheduler._transit = RedisDict(redis, transit_key(world_id))
    scheduler._current_action = RedisDict(
        redis, current_action_key(world_id), encode=encode_action, decode=decode_action,
    )
    scheduler._plans = RedisDict(redis, plans_key(world_id))
    # 在做的长过程:椅子做到一半、孩子怀了六个月。**真状态,不是缓存** ——
    # 内存态等于每次重启都流产一次。
    scheduler._engaged = RedisDict(redis, engagements_key(world_id))
    # 需求 / 小团体 / 反思水位 / 经济。
    scheduler.needs_store = RedisNeedsStore(redis, world_id)
    scheduler.clique_store = RedisCliqueStore(redis, world_id)
    scheduler.reflection_store = RedisReflectionStore(redis, world_id)
    scheduler.economy_store = economy_store
    # 量与规律。
    scheduler.stock_store = stock_store
    scheduler.visibility_store = visibility_store
    with shared_lock:
        # **播种是创世那一刻的事,不是"这张表恰好还空着"的事。**
        #
        # 按空表判断,只在第一次开机时和创世重合。之后每一次开机,手里这份种子
        # (缺省是包自带的橱窗)都会去填当初作者**有意留空**的那几张表 —— 而规律
        # 是这个世界的物理法则:一个作者写了 `kinds` 却没写 `rules` 的世界,重开一次
        # 就会被塞进橱窗那条"树会长高"的规律,而它引用的 `tree` 这个种类在这个世界
        # 里根本不存在。下场不是算错,是**这个世界从此打不开**(`resolve` 当场拒绝)。
        # 创作台的整套流程都是自定义种子,所以这条一撞一个准。
        #
        # `_seed_ontology` 早就是这么判的(`fresh_world=`),这里只是把同一条补齐。
        if fresh_world:
            _seed_world_rules(rules_store, world_seed)
            _seed_stock_visibility(world_seed, visibility_store)
            _seed_stock_places(world_seed, visibility_store)
        _seed_ontology(ontology_store, world_seed, fresh_world=fresh_world)
        scheduler.world_rules = _load_world_rules(rules_store)
        # 本体的解析在规律之后:它要拿规律去查引用。声明过种类的世界从此走闸,
        # 没声明的照旧走警告 —— 那条警告只对后者还有意义。
        scheduler.ontology_store = ontology_store
        scheduler.ontology = _load_ontology(
            ontology_store, scheduler.world_rules, location_store, economy_store
        )
        # 种子的显式值先落(它此刻仍是空库,"空库一次"那条守得住),声明的默认值
        # 后面**只填缺** —— 反过来的话作者写的 3.2 会被声明的 1.0 盖掉。
        # 而它现在拿得到本体了,于是"写了一个没声明过的量"这件事走得了闸。
        _seed_stocks(world_seed, stock_store, ontology=scheduler.ontology)
        if scheduler.ontology is None:
            _warn_unresolved_rule_names(stock_store, scheduler.world_rules)
        else:
            _apply_ontology(scheduler.ontology, stock_store, visibility_store)
    # D3 restart-reversion fix: Scheduler.__init__ already replayed whatever
    # is persisted into scheduler._memory_projection (empty on a fresh DB) —
    # reuse it here for persona resolution BEFORE constructing agents,
    # instead of folding the same event list into a second Projection.
    persisted: list[Event] = event_log.replay() if event_log is not None else []
    if world_file is not None and persisted:
        # The seed file is first-boot-only (M6 D7). Editing one and pointing it
        # at a world that already exists looks like it should work and silently
        # does nothing — the event log and the definition tables are the running
        # world's only source of truth from genesis onward.
        logger.warning(
            "--world-file %s 里的**作者层**没有生效:世界 %r 已经有 %d 条事件了,而作者层"
            "只会被读进一个空世界。它自己的名册与状态来自事件日志,不需要谁再播一遍。"
            "要照着这份文件建世界就换一个空的 --world-id;要改一个活着的世界,"
            "走 World.config_set / World.prompt_set 与各 store。"
            "(文件里的**状态层**不受这条限制,它已经落进去了。)",
            world_file, world_id, len(persisted),
        )
    boot_projection = scheduler._memory_projection
    scheduler.bt_store = bt_store
    # agent-leave-return D4: whoever's last presence event is a leave stays
    # off-stage — skipped in both the roster loop and the mid-run-join scan.
    away = _away_agents(persisted)
    locs = ["cafe", "workshop", "home"]
    # An existing world names its own cast; the seed file only ever furnishes an
    # empty one. `--agents` is a seeding knob and does not apply to a world that
    # already has a roster.
    roster = (
        _roster_from_events(persisted, boot_projection)
        if persisted
        else [_roster_entry(i, locs, world_seed) for i in range(n_agents)]
    )
    for entry in roster:
        if entry["id"] in away:
            continue
        # bt-duties D5/D6: each agent gets its OWN tree (duties first, idle
        # last), seeded from the seed file's `duties` on first boot. An agent
        # with no tree of its own falls back to the shared "default" tree, and
        # a no-DB run to the built-in default_bt() — never brainless.
        if bt_store is not None:
            _seed_agent_tree(bt_store, entry["id"], world_seed)
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
            _seed_mock_narration(prompt_store, world_seed)
            persisted = event_log.replay()
            # Only re-fold here: genesis events were just appended above,
            # so the projection Scheduler.__init__ built (from the
            # then-empty log) is now stale and must be recomputed once.
            # **走 reset_projection,别直接赋值**:水位要跟着一起挪,不然下一次
            # `catch_up_projection` 会把创世事件再折一遍(钱和随身物品当场翻倍)。
            scheduler.reset_projection(persisted)
        scheduler.load_persisted_events(persisted)
        if memory_store is not None and trigger_engine is not None:
            _rebuild_memories(memory_store, trigger_engine, persisted)
    # 黑板最后才搬进 Redis —— 必须晚于事件重放。**只填缺,不覆盖**:手里这份是
    # 创世/重放拼出来的快照,而 Redis 里已有的值是这个世界**更新的现在**;反过来
    # 排序(先接再重放)会让重放把别的进程刚写的位置盖回创世值 —— 第二个
    # World.open 悄悄把她挪回 cafe,两边都不报错,黑板漏的正是这一条。
    from anima_world.redis_state import RedisBlackboard, agent_key

    for aid, brain in scheduler.agents.items():
        board = RedisBlackboard(redis, agent_key(world_id, aid))
        board.seed_missing(brain.agent.blackboard.snapshot())
        brain.agent.blackboard = board
    return scheduler


def _seed_world_setting(prompt_store: Any | None, world_seed: dict[str, Any] | None) -> None:
    """世界文件的 `world_setting`(一条 `author` 记录,body 就是那段话)在**首启**
    时替换掉内置的默认世界观(调用方用 `not persisted` 把着门)。

    之后 `:prompts` 里那一行是运行期权威(和 llm.* 同一条 M5 规矩):`prompt_set`
    的热改留得住,重启不会拿文件回头盖掉它。没有这条通道,每个自定义世界都跑在
    写死的旧港区世界观下 —— Window-1 那些"叙事幻觉"是 LLM 在照它办事。

    ⚠️ 形状归 `world_file.AUTHOR_SCALAR_TYPES` 定,**一段非空文本**。这里曾经和
    线格式对不上(那边归进"对象型"、要求 body 是 dict,这里只认 str),于是任何
    `.cyberworld` 的世界观都进不来,而且一声不吭 —— 所以下面那个 `warning`
    不是装饰:形状不对时必须有人说一句。
    """
    if prompt_store is None or world_seed is None:
        return
    setting = world_seed.get("world_setting")
    if setting is None:
        return
    if not isinstance(setting, str) or not setting.strip():
        logger.warning(
            "world_setting 必须是一段非空文本,收到 %s;这个世界仍用内置世界观",
            type(setting).__name__,
        )
        return
    try:
        prompt_store.set("world.setting", setting.strip())
    except Exception:  # noqa: BLE001 - a bad seed field must not abort a
        # first boot whose genesis events are already committed (the
        # crash would silently lock the world onto the bundled default
        # worldview forever — code review Round 1 #1)
        logger.warning("world_setting from seed could not be stored; keeping default", exc_info=True)


def _seed_mock_narration(prompt_store: Any | None, world_seed: dict[str, Any] | None) -> None:
    """种子的 `mock_narration`:让 Mock 叙事说这个世界的话(#9)。

    没配 key 是**默认状态**,所以 Mock 模板是第一屏。引擎无从知道自己在跑哪个
    世界 —— 那是种子决定的,于是模板也该由种子决定。

    ```jsonc
    "mock_narration": {
      "walk": "{agent} walked to {location}",   // 动作种类 → 模板
      "memory_suffix": "— still thinking about {summary}"
    }
    ```

    与 `world.setting` 同一条规矩:**首启写一次**,之后 db 行是运行期权威
    (`World.prompt_set` 的热改会留住)。坏模板逐条丢弃 —— 创世事件此时已经
    提交,这里崩掉会把世界永远锁在半初始化状态。
    """
    if prompt_store is None or world_seed is None:
        return
    from anima_world.narrative import MOCK_MEMORY_SUFFIX_NAME, MOCK_TEMPLATE_PREFIX

    templates = world_seed.get("mock_narration")
    if not isinstance(templates, dict):
        if templates is not None:
            logger.warning("world_seed mock_narration must be an object, got %s; ignoring",
                           type(templates).__name__)
        return
    for kind, template in templates.items():
        if not isinstance(template, str) or not template.strip():
            logger.warning("world_seed mock_narration[%r] is not a non-empty string; skipping", kind)
            continue
        name = (MOCK_MEMORY_SUFFIX_NAME if kind == "memory_suffix"
                else f"{MOCK_TEMPLATE_PREFIX}{kind}")
        try:
            prompt_store.set(name, template.strip())
        except Exception as exc:  # noqa: BLE001 - 见 docstring:坏模板不许拦住首启
            # 消息里已经点名了是哪个占位符,再甩一整个 traceback 只会淹掉它。
            logger.warning("world_seed mock_narration[%r] was rejected (%s); keeping the default",
                           kind, exc)


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



def _seed_stocks(world_seed: dict[str, Any] | None, store: Any,
                 *, ontology: Any = None) -> None:
    """种子里的初始存量(`"stocks": [{"owner": …, "values": {…}}]`)。空库一次。

    坏条目逐条丢弃 —— 和种子里其它可选字段同一条宽容原则(规律本身不是这样:
    那是世界的物理法则,坏一条就整体拒绝,见 `_seed_world_rules`)。

    **声明过种类的世界例外:量名走闸,拼错当场开不了机。** 这一条和
    `_load_ontology` 的"声明本身就是开关"逐字同构 —— 一个不写 `kinds` 的世界里,
    owner 和量名都是任意字符串,引擎无从判断 `树髙` 是笔误还是作者新造的量;
    一旦他写下 `kinds`,他就是在说"我已经声明了这个世界有什么"。

    不吼一声而放行的样子是这个仓库最怕的那种:`树髙` 安静地建成第二个量,
    `树高` 停在声明的默认值上,规律照跑、日志干净,而作者要到某天发现那棵树
    三个月没长过才知道。
    """
    if world_seed is None:
        return
    if store.owners():
        return
    problems: list[str] = []
    for index, entry in enumerate(_seed_entry_dicts(world_seed, "stocks")):
        owner = str(entry.get("owner") or "").strip()
        values = entry.get("values")
        if not owner or not isinstance(values, dict):
            logger.warning("world_seed stocks[%s] 缺 owner 或 values;跳过", index)
            continue
        declared = ontology.declared_quantities(owner) if ontology is not None else None
        clean: dict[str, float] = {}
        for key, raw in values.items():
            name = str(key)
            if declared and name not in declared:
                problems.append(
                    f"stocks[{index}] 给 {owner} 写了「{name}」,而它所属的种类没有"
                    f"声明过这个量;声明过的是 {sorted(declared)}"
                )
                continue
            try:
                clean[name] = float(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "world_seed stocks[%s] 的 %s 不是数(%r);跳过这一项", index, key, raw
                )
        if clean:
            store.set_many(owner, clean, tick=0)
    if problems:
        from anima_world.ontology import OntologyError

        raise OntologyError(problems)


def _seed_world_rules(rules_store: Any, world_seed: dict[str, Any] | None) -> None:
    """种子里的规律 → `:world_rules`。空的一次,之后那里的行说了算。

    **坏规律整体拒绝**(`RuleError`),不逐条丢弃:规律是这个世界的物理法则,
    少一条不是"少一点内容",是这个世界从此算错。宁可开不了机。
    """
    if world_seed is None:
        return
    if len(rules_store):
        return
    entries = world_seed.get("rules")
    if not entries:
        return
    parse_rules(entries)   # 校验在这里,坏了当场抛
    rules_store.seed(entries, datetime.now(timezone.utc).isoformat())


def _load_world_rules(rules_store: Any) -> list[Any]:
    """从 store 读出规律并编译。被人手改坏了也当场报错,不带着坏规律开机。"""
    definitions = rules_store.definitions()
    if not definitions:
        return []
    return parse_rules(definitions)


def _warn_unresolved_rule_names(stock_store: Any, rules: list[Any]) -> None:
    """规律读了一个它**够不着**的量 —— 开机时点名。

    只警告不拒绝:量可以在世界跑起来之后才被创建(种一棵树),所以这不能是错误。
    但必须说出来 —— 这条的由来是一次真实事故:内置橱窗的规律把 `world_季节` 写成了
    `季节`,于是那条规律每次求值都被静默跳过,树永远不长。

    "够不着"要**按选择器逐条算**,不能把所有 owner 的量名混成一个集合:world 自己
    有个 `季节`,不代表一条作用在树上的规律能裸着读到它(那正是上面那个事故 ——
    第一版 helper 就是这么写的,于是它对那个 bug 视而不见)。
    """
    from anima_world.rules import BUILTIN_NAMES, WORLD_PREFIX, missing_names
    from anima_world.stocks import owner_kind

    by_owner: dict[str, set[str]] = {}
    by_kind: dict[str, set[str]] = {}
    for owner in stock_store.owners():
        for key in stock_store.snapshot(owner):
            by_owner.setdefault(owner, set()).add(key)
            by_kind.setdefault(owner_kind(owner), set()).add(key)

    # 世界的量只能带前缀读到 —— 这正是那个事故的要害。
    globals_ = {f"{WORLD_PREFIX}{key}" for key in by_kind.get("world", set())}
    always = globals_ | set(BUILTIN_NAMES)

    for rule in rules:
        if rule.selector_kind == "kind":
            reachable = by_kind.get(rule.selector_value, set())
        elif rule.selector_kind == "owner":
            reachable = by_owner.get(rule.selector_value, set())
        else:                                   # action:作用在角色身上
            reachable = by_kind.get("agent", set())
        unresolved = missing_names(rule, reachable | always)
        if unresolved:
            logger.warning(
                "规律 %s 读了它够不着的量 %s —— 写错名字了吗?"
                "(世界的量要写成 `%s季节` 这样,带前缀)",
                rule.id, unresolved, WORLD_PREFIX,
            )


def _seed_ontology(
    ontology_store: Any, world_seed: dict[str, Any] | None, *, fresh_world: bool
) -> None:
    """种子里的本体(`"kinds"` / `"entities"`)→ `:kinds` / `:entities`。**只在创世。**

    别的种子段落用的是"表空了就播",这一段不能 —— 一个创世时没有本体的世界,它的
    本体表**永远是空的**,于是下次用默认种子重开就会被硬塞进一个 `tree` 种类,
    连带它的规律闸,而那些东西和这个世界毫无关系(轻则多一棵树,重则开不了机)。
    本体是"这个世界有什么"的定义:它和世界同生,不能事后嫁接。

    **坏声明整体拒绝**(`OntologyError`),和规律同一条理由:少一条不是"少一点
    内容",是这个世界从此有一部分东西静默地不存在。
    """
    from anima_world.ontology import parse_entities, parse_kinds

    if world_seed is None or not fresh_world or len(ontology_store):
        return
    kinds = world_seed.get("kinds") or []
    entities = world_seed.get("entities") or []
    if not kinds and not entities:
        return
    parse_entities(entities, parse_kinds(kinds))   # 校验在这里,坏了当场抛
    ontology_store.seed(kinds, entities, datetime.now(timezone.utc).isoformat())


def _load_ontology(
    ontology_store: Any, rules: list[Any], location_store: Any, economy_store: Any = None
) -> Any:
    """读出本体并解析全部引用。**没声明过种类的世界跳过这一整层。**

    "声明本身就是开关"和认知层逐字同构:一个不写 `kinds` 的世界照旧靠
    `_warn_unresolved_rule_names` 那条**警告**过日子(量可以在世界跑起来之后才被
    创建,所以没有声明时那只能是建议)。一旦作者写下 `kinds`,他就是在说
    "我已经声明了这个世界有什么" —— 于是同一件事从建议升级成闸:
    `for_each: {"kind": "trees"}` 当场开不了机,而不是安静地一条都不跑。
    """
    if not len(ontology_store):
        return None
    locations = [str(row.get("id")) for row in (location_store.all() or ())]
    # 物品定义是**闭集**(只在创世从种子里播,`seed_authored` / `seed_defaults`),
    # 所以能力里的 `have_园艺剪` 查得起来 —— 拼错一个字的后果是那道门永远关着,
    # 而世界照跑、日志干净,正是这一层存在的理由。
    items = [str(row.get("id")) for row in (economy_store.items() if economy_store else ())]
    return ontology_store.load(rules=rules, locations=locations, items=items)


def _apply_ontology(ontology: Any, stock_store: Any, visibility_store: Any) -> None:
    """把本体声明兑现成量、可见性、位置。**三样都只填缺,不覆盖。**

    每次开机都跑,不只创世 —— 它表达的是一条不变量:**一个实体存在,它声明过的量
    就存在**。整份写回则会把长了三十天的树倒带回幼苗(创世那条纪律踩过两次)。
    """
    from anima_world.ontology import seed_quantities, visibility_declarations

    for entity in ontology.entities.values():
        # **逐个量填,不是逐个实体填。** 按实体跳的话,种子里给某棵树写了一个
        # `树高` 就会让它声明过的其余量(`最大树高` / `生长速度`)一个都不落地 ——
        # 于是 `tend` 的条件 `树高 < 最大树高` 求不出值、生长规律算不动,而两件事
        # 都只是安静地不发生。
        have = stock_store.of(entity.id)
        missing = {
            key: value for key, value in seed_quantities(ontology, entity).items()
            if key not in have
        }
        if missing:
            stock_store.set_many(entity.id, missing, tick=0)

    declared = visibility_store.rules_map()
    for kind, key, visibility, label in visibility_declarations(ontology):
        if (kind, key) not in declared:
            visibility_store.declare(kind, key, visibility, label)

    for entity in ontology.entities.values():
        # `here` 档靠它才成立:没有位置的东西永远不在任何地方,于是"在场可见"
        # 等于"永远看不见"。
        if entity.location and visibility_store.place_of(entity.id) is None:
            visibility_store.place(entity.id, entity.location, entity.name)


def _seed_stock_places(world_seed: dict[str, Any] | None, store: Any) -> None:
    """种子里"这个东西在哪"(`"stock_places": [...]`)。空表一次。

    `here` 档的可见性靠它才成立 —— 没有它,一棵树永远不在任何地方,于是"在场可见"
    等于"永远看不见"。
    """
    if world_seed is None:
        return
    if store.labels():
        return
    for index, entry in enumerate(_seed_entry_dicts(world_seed, "stock_places")):
        owner = str(entry.get("owner") or "").strip()
        location = str(entry.get("location") or "").strip()
        if not owner or not location:
            logger.warning("world_seed stock_places[%s] 缺 owner 或 location;跳过", index)
            continue
        store.place(owner, location, entry.get("label"))


def _seed_stock_visibility(world_seed: dict[str, Any] | None, store: Any) -> None:
    """种子里的可见性声明(`"stock_visibility": [...]`)。空表一次。

    **声明本身就是这一层的开关** —— 没声明过任何东西的世界,角色感知不到任何量,
    这一层不进提示词、不花 token。坏条目逐条丢弃并点名(可见性写错的后果是
    "她本该知道却不知道",不该拦住启动)。
    """
    if world_seed is None:
        return
    if store.declarations():
        return
    for index, entry in enumerate(_seed_entry_dicts(world_seed, "stock_visibility")):
        kind = str(entry.get("kind") or "").strip()
        key = str(entry.get("key") or "").strip()
        visibility = str(entry.get("visible") or entry.get("visibility") or "").strip()
        if not kind or not key:
            logger.warning("world_seed stock_visibility[%s] 缺 kind 或 key;跳过", index)
            continue
        try:
            store.declare(kind, key, visibility, entry.get("label"))
        except ValueError as exc:
            logger.warning("world_seed stock_visibility[%s] 没生效:%s", index, exc)


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
    # payment events just fold into an unused ledger. #12: the seed may set a
    # per-agent amount; 0 means "starts broke" and emits nothing, since the
    # projection ignores non-positive payments anyway.
    from anima_world.economy import TOWN

    for aid in scheduler.agents:
        amount = _money_for(aid, world_seed)
        if amount <= 0:
            continue
        event_log.append({
            "ts": 0, "who": aid, "type": "payment",
            "payload": {"from": TOWN, "to": aid, "amount": amount, "reason": "genesis_stipend"},
        })

    if world_seed is not None:
        registered_ids = set(scheduler.agents)
        _seed_relations(event_log, registered_ids, world_seed)
        _seed_goals(event_log, registered_ids, world_seed)
        _seed_memories(event_log, registered_ids, world_seed)
        _seed_inventory(event_log, registered_ids, world_seed)


def _wrap_with_needs_band(bt_root: Any) -> Any:
    """needs-v3: the urgent-needs band sits ABOVE the authored tree — a
    starving agent eats before it opens the shop. Wrapped unconditionally:
    NeedAction is inert (FAILURE) until the scheduler settles `need.*` onto
    the blackboard, which only happens when `needs.enabled` is on, so an
    unlit world behaves tick-for-tick like before."""
    from anima_world.needs import RELEASE, URGENT

    # 触发线 → 释放线:开始恢复就恢复到饱,而不是跨过触发线就收工(见 NeedAction
    # 与 needs.RELEASE —— 单阈值下角色永远卡在触发线上方抖)。
    from anima_world.bt_nodes import IntentAction

    return Selector(children=[
        NeedAction("energy", URGENT, "go_sleep", RELEASE["energy"]),
        NeedAction("hunger", URGENT, "eat", RELEASE["hunger"]),
        NeedAction("social", URGENT, "idle_social", RELEASE["social"]),
        # 她刚决定要做的事:身体之下、排班之上。队列空时 FAILURE,所以没人调用
        # `intend()` 的世界行为逐字不变 —— 纯加法。
        IntentAction(),
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


def _material_entries(entry: dict[str, Any], key: str, where: str) -> list[dict[str, Any]]:
    """A tolerant `[{item, qty, …}]` list off one agent/location entry (#12)."""
    raw = entry.get(key)
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        logger.warning("world_seed %s %r must be a list, got %s; skipping",
                       where, key, type(raw).__name__)
        return []
    good: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("item"), str) or not item["item"]:
            logger.warning("world_seed %s %s[%d] has no string 'item'; skipping", where, key, index)
            continue
        good.append(item)
    return good


def _material_qty(item: dict[str, Any], where: str) -> int | None:
    try:
        qty = int(item.get("qty", 1))
    except (TypeError, ValueError):
        logger.warning("world_seed %s: %r has a non-numeric qty %r; skipping",
                       where, item.get("item"), item.get("qty"))
        return None
    if qty <= 0:
        logger.warning("world_seed %s: %r has qty %d; skipping", where, item.get("item"), qty)
        return None
    return qty


def _seed_material_layer(store: Any, world_seed: dict[str, Any] | None) -> None:
    """物质层的创世入口:物品定义与店铺货架(#12)。

    economy/needs 从首发起就有机制,却是唯一一个没有创世入口的子系统 ——
    "她把父亲的怀表一直带在身上""铺子里囤着过冬的煤"这类物质设定,创作侧
    过去只能丢掉或降级成一句记忆文本。

    三个来源都能引入物品 id:顶层 `items`(完整定义)、`agents[].inventory`、
    `locations[].stock`。只被引用、没有定义的 id 自动补一条定义(名字就是 id、
    durable、0 价),这样 `{"item": "父亲的怀表"}` 直接可用而不必先建表 ——
    要精确控制名称/种类/价格再写 `items`。

    随身物品与钱不在这里:它们是**事件**(item_transfer / payment),走
    `_seed_initial_world` 的创世事件,账本仍然是事件的投影。
    """
    if world_seed is None:
        return
    from anima_world.economy import ITEM_KINDS

    defined: dict[str, tuple[str, str, str, float, dict[str, Any]]] = {}
    for index, entry in enumerate(_seed_entry_dicts(world_seed, "items")):
        item_id = entry.get("id")
        if not isinstance(item_id, str) or not item_id:
            logger.warning("world_seed items[%d] has no string 'id'; skipping", index)
            continue
        kind = entry.get("kind", "durable")
        if kind not in ITEM_KINDS:
            logger.warning("world_seed items[%d] (%r) has kind %r, expected one of %s; skipping",
                           index, item_id, kind, ", ".join(ITEM_KINDS))
            continue
        try:
            base_price = float(entry.get("base_price", 0.0))
        except (TypeError, ValueError):
            logger.warning("world_seed items[%d] (%r) has a non-numeric base_price %r; using 0",
                           index, item_id, entry.get("base_price"))
            base_price = 0.0
        restores = entry.get("restores")
        defined[item_id] = (
            item_id, str(entry.get("name", item_id)), kind, base_price,
            restores if isinstance(restores, dict) else {},
        )

    def _referenced(item_id: str) -> None:
        # 引用即存在:未定义的 id 补一条 durable/0 价的定义,名字就是 id。
        # durable 是安全的默认 —— consumable 会被 cheapest_meal 当饭吃掉。
        defined.setdefault(item_id, (item_id, item_id, "durable", 0.0, {}))

    stock: list[tuple[str, str, int, float | None]] = []
    for entry in _seed_entry_dicts(world_seed, "locations"):
        location_id = entry.get("id")
        if not isinstance(location_id, str) or not location_id:
            continue
        for item in _material_entries(entry, "stock", f"location {location_id!r}"):
            qty = _material_qty(item, f"location {location_id!r}")
            if qty is None:
                continue
            price: float | None = None
            if item.get("price") is not None:
                try:
                    price = float(item["price"])
                except (TypeError, ValueError):
                    logger.warning("world_seed location %r: %r has a non-numeric price %r; "
                                   "falling back to base_price",
                                   location_id, item["item"], item["price"])
            _referenced(item["item"])
            stock.append((location_id, item["item"], qty, price))

    for entry in _seed_entry_dicts(world_seed, "agents"):
        agent_id = entry.get("id")
        for item in _material_entries(entry, "inventory", f"agent {agent_id!r}"):
            _referenced(item["item"])

    if not defined and not stock:
        return  # 种子没碰物质层:内置演示物品照常出场
    store.seed_authored(list(defined.values()), stock)


def _money_for(agent_id: str, world_seed: dict[str, Any] | None) -> float:
    """创世安家费:种子写了就按种子,没写就是默认额度(#12)。"""
    from anima_world.economy import GENESIS_STIPEND

    if not world_seed:
        return GENESIS_STIPEND
    for entry in _seed_entry_dicts(world_seed, "agents"):
        if entry.get("id") != agent_id or "money" not in entry:
            continue
        try:
            return float(entry["money"])
        except (TypeError, ValueError):
            logger.warning("world_seed agent %r has a non-numeric money %r; using the default %.1f",
                           agent_id, entry["money"], GENESIS_STIPEND)
            return GENESIS_STIPEND
    return GENESIS_STIPEND


def _seed_inventory(event_log: EventLog, registered_ids: set[str], world_seed: dict[str, Any]) -> None:
    """随身物品的创世注入(#12):无 `from` 的 item_transfer = 无中生有。

    投影里 `from` 缺省就是纯粹的凭空生成,正是创世该有的语义 —— 若写成从小镇
    金库转出,金库的库存会被记成负数。`note`(「从不离身」这类信物说明)原样
    落在事件载荷里跟着世界走;投影忽略它,也不会自动变成一条记忆 —— 想让角色
    记得这件事,种子的 `memories` 才是那个入口。
    """
    for entry in _seed_entry_dicts(world_seed, "agents"):
        agent_id = entry.get("id")
        if agent_id not in registered_ids:
            continue
        for item in _material_entries(entry, "inventory", f"agent {agent_id!r}"):
            qty = _material_qty(item, f"agent {agent_id!r}")
            if qty is None:
                continue
            payload: dict[str, Any] = {"to": agent_id, "item_id": item["item"], "qty": qty,
                                       "reason": "genesis_inventory"}
            if isinstance(item.get("note"), str) and item["note"].strip():
                payload["note"] = item["note"].strip()
            event_log.append({"ts": 0, "who": agent_id, "type": "item_transfer", "payload": payload})


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
    # chat-agent:chat 里能调的能力(`@tool` 注册表)也写进目录。只在创世时写 ——
    # 往已有世界的日志里补写历史等于替过去撒谎;老世界照样能用这些能力,只是目录里
    # 没有它们(`World.tools()` / `anima-world contract` 报的是注册表,始终是真的)。
    catalog = list(catalog) + chat_tools.capability_payloads()
    for entry in catalog:
        event_log.append({
            "ts": 0,
            "type": "capability_registered",
            "payload": {
                "id": entry.get("id"),
                "kind": entry.get("kind", ""),
                "description": entry.get("description", ""),
                "params_schema": entry.get("params_schema", {}),
                **({"surface": entry["surface"]} if entry.get("surface") else {}),
            },
        })


def _world_exists(redis: Any, world_id: str) -> bool:
    """这个 Redis 上有没有叫这个名字的世界 —— 前缀下有任何键就算有。"""
    from anima_world.redis_state import KEY_PREFIX

    for _ in redis.scan_iter(match=f"{KEY_PREFIX}:{world_id}:*", count=10):
        return True
    return False


def _require_existing_world(redis: Any, world_id: str, cmd: str) -> bool:
    """只读命令不许**创建**世界。

    抄错 world_id 的样子比报错坏得多(commit 5ce6aed 的教训原样成立):打开一个
    不存在的名字会当场创世,你看到的是一张排版正常、时钟 0、所有人各就各位的
    世界 —— 读起来像"还没开始跑",不像"你看错世界了"。顺带还留下一堆垃圾键。
    """
    if _world_exists(redis, world_id):
        return True
    print(f"[{cmd}] 这个 Redis 上没有叫 {world_id!r} 的世界。", file=sys.stderr)
    print(f"      新建世界用:anima-world start --world-id {world_id}", file=sys.stderr)
    return False


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

    redis, world_id, mysql = _world_args(args)
    try:
        world = World.open(
            world_id, redis=redis, mysql=mysql,
            world_file=args.world_file, beats_path=args.beats, agents=args.agents,
        )
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[run] {exc}", file=sys.stderr)
        return 2
    roster = "、".join(brain.agent.name for brain in world.scheduler.agents.values())
    print(f"[run] {world_id}  {len(world.scheduler.agents)} 个角色:{roster}")
    print("[run] 时钟已启动,Ctrl-C 停止(嵌入用法见 anima_world.api.World)")
    _run_world_foreground(world, quiet=args.quiet)
    print("[run] 世界已停下。")
    return 0


def _print_roster(world: Any, world_id: str) -> None:
    """这个世界住着谁 —— 一个 .cyberworld 至今没有办法自报家门(#6)。"""
    state = world.state()
    agents = state.get("agents", {})
    now = state.get("world_time", {})
    print(f"\n  {onboarding.bold(world_id)}  第{now.get('day', 0)}天 "
          f"{now.get('hour', 0):02d}:{now.get('minute', 0):02d}\n")
    if not agents:
        print("  这个世界还没有住人。\n")
        return
    for agent_id, info in agents.items():
        where = info.get("location") or "?"
        doing = info.get("activity") or {}
        transit = doing.get("transit") if isinstance(doing, dict) else None
        if transit:
            doing_text = f"在去 {transit['to']} 的路上"
        else:
            doing_text = (doing.get("kind") or "") if isinstance(doing, dict) else ""
        tail = onboarding.dim(doing_text) if doing_text else ""
        away = onboarding.dim("(不在场)") if info.get("away") else ""
        print(f"    {_pad(agent_id, 12)}{_pad(info.get('name', agent_id), 16)}"
              f"{_pad('@' + str(where), 14)}{tail}{away}")
    print(f"\n  找谁说话:{onboarding.bold(f'anima-world chat --agent {next(iter(agents))}')}"
          f"{'' if world_id == _world_id_default() else f' --world-id {world_id}'}\n")


def _map_frame(world: Any, args: argparse.Namespace) -> str:
    """一帧:地图 + 图例。`--watch` 每次重跑它。

    **和 `--json` 共用 `World.map_data()`** —— 观察窗另写一遍取数就会撒谎
    (`debug_prompt` 与真聊天共用 `prompt_blocks` 是同一条理由)。
    """
    import shutil

    from anima_world.mapview import (
        DEFAULT_HEIGHT, DEFAULT_WIDTH, MapPlace, Track, legend, markers_for, render,
    )

    data = world.map_data(
        from_tick=args.from_tick, to_tick=args.to_tick, agents=args.agent
    )
    places = [MapPlace(**place) for place in data["places"]]
    tracks = [] if args.now else [
        Track(
            agent=t["agent"],
            points=[(p["tick"], p["place"]) for p in t["points"]],
            anchor=next((p["place"] for p in t["points"] if p.get("before")), None),
        )
        for t in data["tracks"]
    ]
    known = {t.agent for t in tracks} | {
        a for group in data["standing"].values() for a in group
    } | {str(t["agent"]) for t in data["travelling"]}
    markers = markers_for(known)

    term = shutil.get_terminal_size((DEFAULT_WIDTH + 6, DEFAULT_HEIGHT + 10))
    width = args.width or max(30, min(DEFAULT_WIDTH, term.columns - 6))
    height = args.height or DEFAULT_HEIGHT

    if not places:
        return "  这个世界还没有地图(locations 表是空的)。"

    lines = [render(places, tracks=tracks, marks=data["standing"],
                    markers=markers, width=width, height=height)]
    lines.append("")
    span = "此刻" if args.now else (
        f"tick {args.from_tick if args.from_tick is not None else 0}"
        f"~{args.to_tick if args.to_tick is not None else data['clock']}"
    )
    lines.append(f"  世界时钟 {data['clock']}  ·  {span}")
    for line in legend(tracks, places, markers=markers, standing=data["standing"],
                       travelling=data["travelling"], show_hops=not args.now):
        lines.append(f"  {line}")
    if not args.now and not any(t.points for t in tracks):
        lines.append("  (这段时间里没人挪过窝)")
    return "\n".join(lines)


def run_map(args: argparse.Namespace) -> int:
    """`anima-world map` —— 把地图画出来,看得见她今天去了哪儿。

    为什么值得一道命令:位移这件事此前只在事件日志里躺着。**看不见的东西没人会
    去查** —— 而"她走了"到底有没有在世界里兑现,是 1.3.0 那批 issue 的病本身。

    渲染是赠品,`--json` 才是契约:创作台 / 网站 / 运维台照那份数据自己画。
    """
    from anima_world.api import World

    if args.day is not None and (args.from_tick is not None or args.to_tick is not None):
        print("[map] --day 和 --from/--to 二选一。", file=sys.stderr)
        return 2
    if args.day is not None:
        if args.day < 1:
            print("[map] --day 从 1 起。", file=sys.stderr)
            return 2
        ticks_per_day = 288
        args.from_tick = (args.day - 1) * ticks_per_day
        args.to_tick = args.day * ticks_per_day

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "map"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[map] {exc}", file=sys.stderr)
        return 2
    try:
        if args.as_json:
            print(json.dumps(
                world.map_data(from_tick=args.from_tick, to_tick=args.to_tick,
                               agents=args.agent),
                ensure_ascii=False, indent=2,
            ))
            return 0
        if args.watch is None:
            print(_map_frame(world, args))
            return 0
        # watch:清屏重画。**不推时钟** —— 这道命令只看,推时钟的是 run/simulate。
        try:
            while True:
                sys.stdout.write("\033[H\033[2J")
                print(_map_frame(world, args))
                print(f"\n  (每 {args.watch:g} 秒重画;Ctrl-C 停。这道命令不推时钟)")
                sys.stdout.flush()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print()
            return 0
    finally:
        world.close()


def run_ontology(args: argparse.Namespace) -> int:
    """`anima-world ontology` —— 这个世界里有哪些东西,以及**能对它们做什么**。

    为什么值得一道命令:量和规律此前在 CLI 上完全没有出口(`docs/FOR-STUDIO.md`
    原话:"没有任何 CLI 能读一个世界当前的量或规律")。而创作台那侧的判据是
    **有没有 CLI 出口** —— 库里有而命令行上没有,对不 import 本包的它等于不存在。

    最要紧的一栏是**能力**:`stocks` 只给得出数字,而数字不告诉你 `tend` 这个词
    存不存在。猜一份动词表出来是这一层最容易犯的错 —— 猜错了不报错,按钮点下去
    才发现世界不认。

    渲染是赠品,`--json` 才是契约(和 `map` 同一条)。
    """
    from anima_world.api import World

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "ontology"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[ontology] {exc}", file=sys.stderr)
        return 2
    try:
        kinds = world.kinds()
        entities = world.entities(args.kind)
        checked = world.check_entity() if getattr(args, "check", False) else None
    finally:
        world.close()

    if checked is not None:
        bad = [row for row in checked if not row["ok"]]
        if args.as_json:
            print(json.dumps({"checked": checked}, ensure_ascii=False, indent=2))
        else:
            for row in bad:
                for problem in row["problems"]:
                    print(f"✗ {problem}")
            print(f"{world_id}:查了 {len(checked)} 个,{len(bad)} 个有问题")
            if not bad:
                # 查过了要说出来。什么也不打印和"没查"长得一模一样。
                print("(量都落地了、能力都算得出结论、该在场的都在场)")
        # 有问题时退出码 1 —— 这条命令于是能进 CI,和 `--ticks 0` 当校验器同一个用法。
        return 1 if bad else 0

    if args.kind is not None:
        kinds = [k for k in kinds if k["id"] == args.kind]
        if not kinds:
            print(f"[ontology] 这个世界里没有声明过 {args.kind!r} 这一类。", file=sys.stderr)
            return 2
    elif not args.builtin:
        # 内置四类(agent/location/world/player)照例没有量也没有能力,列出来只是噪音。
        # **但 `agent` 可以声明量**(她的体力/手艺),而那些量正是 `requires` /
        # `costs` 里 `me_*` 的出处 —— 藏起来的话,读的人看得见"她付 体力 - 10"
        # 却查不到"体力"是什么、默认多少。
        kinds = [k for k in kinds if not k["builtin"] or k["quantities"]]

    if args.as_json:
        print(json.dumps({"kinds": kinds, "entities": entities},
                         ensure_ascii=False, indent=2))
        return 0

    if not kinds:
        print(f"{world_id}:这个世界没有声明过任何种类。")
        print("(在种子的 kinds / entities 两段里声明;见 docs/REFERENCE.md §2.9.6)")
        return 0

    # 量名和实体名基本都是中文,而中文占两格 —— 按字符个数补空格会把每一栏推歪。
    from anima_world.mapview import display_width

    def pad(text: str, width: int) -> str:
        return text + " " * max(1, width - display_width(text))

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for entity in entities:
        by_kind.setdefault(entity["kind"], []).append(entity)

    print(f"{world_id}:{len(kinds)} 类 / {len(entities)} 个\n")
    for kind in kinds:
        hint = "  —— 角色身上的量,能力用 me_ 读它" if kind["builtin"] else ""
        print(f"■ {kind['id']}  {kind['gloss']}{hint}".rstrip())
        for q in kind["quantities"]:
            unit = f" {q['unit']}" if q["unit"] else ""
            print(f"    量   {pad(q['label'], 14)}默认 {q['default']:g}{unit}"
                  f"   她感知得到:{q['visibility']}")
        for a in kind["affordances"]:
            gate = ("  仅当 " + " 且 ".join(a["conditions"])) if a["conditions"] else ""
            deed = "改变世界" if a["changes_world"] else "只是看看"
            # 动词放开之后 id 和人话可以不一样。两个都印:作者调的是 id,
            # 她读到的是人话,而排错时要对得上的正是这两者。
            word = a["verb"] if a.get("label", a["verb"]) == a["verb"] else \
                f"{a['verb']}({a['label']})"
            print(f"    能力 {pad(word, 14)}{deed}{gate}")
            if a.get("duration"):
                # 时长要印出来,而且要连着说占不占用她 —— 一个 8640 tick 的能力
                # 占不占用,决定的是这个世界在这段时间里还有没有她。
                busy = "这期间她腾不出手" if a.get("occupies") else "这期间她照常过日子"
                print(f"         要花 {a['duration']} 个 tick,{busy}")
            for setter in a["sets"]:
                print(f"         → {setter}")
            # 关于她的那一半单独一行,不和上面的效果混在一起 —— 混了的话读的人
            # 看不出"树高 +0.3"和"体力 -10"落在两个不同的东西身上。
            for requirement in a.get("requires") or ():
                print(f"         她得 {requirement}")
            for charge in a.get("costs") or ():
                print(f"         她付 {charge}")
            for item_id, count in sorted((a.get("consumes") or {}).items()):
                # 花掉的东西自带一道"你得有" —— 这里连着说出来,不然读的人会以为
                # 少写了一条 requires,然后照着补一遍。
                print(f"         她花掉 {item_id} × {count}(得先有这么多)")
            # 生与灭要印出来,而且要显眼:一条能力会不会**让世界多一样东西**,是
            # 读一份声明时最该先知道的事,比它把哪个量加了 0.05 要紧得多。
            spawn = a.get("spawn")
            if spawn:
                where = spawn.get("location") or "这件事发生的地方"
                print(f"         ✦ 生出一个 {spawn['kind']} @{where}")
            if a.get("destroys_target"):
                print("         ✦ 做完之后这个东西就没了")
        if not kind["builtin"]:
            # 内置种类没有实例住在本体里(角色在投影里),那条上限对它没有意义。
            print(f"    同一个地方最多带 {kind['budget']} 个进提示词")
        for entity in by_kind.get(kind["id"], []):
            values = "  ".join(f"{k}={v:g}" for k, v in entity["values"].items())
            where = entity["location"] or "不在任何地方"
            print(f"    · {pad(entity['id'], 22)}{entity['name']}  @{where}")
            if values:
                print(f"      {values}")
        print()
    return 0


def run_prompt(args: argparse.Namespace) -> int:
    """`anima-world prompt` —— 她此刻收到的提示词,逐块摊开。

    为什么它值得一道命令:提示词是这套东西**最不可见又最容易出错**的一层。1.3
    开发期四个 bug 有三个在这儿,而当时唯一的诊断办法是写 Python 往私有属性上塞
    一个假 LLM 去偷看 —— 世界作者(改的是 `prompt_templates` 里的模板)一点办法没有。

    **看,但不碰**:不推时钟、不进 LLM、不写玩家状态,静音中的角色也照样交出来。
    """
    from anima_world.api import World

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "prompt"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[prompt] {exc}", file=sys.stderr)
        return 2
    try:
        roster = world.state().get("agents", {})
        if args.agent is None:
            _print_roster(world, world_id)
            return 0
        if args.agent not in roster:
            print(f"[prompt] 这个世界里没有 {args.agent!r}。", file=sys.stderr)
            _print_roster(world, world_id)
            return 2
        seen = world.debug_prompt(
            args.agent,
            player_id=args.player_id,
            display_name=args.name or "访客",
            message=args.message,
        )
    finally:
        world.close()

    if args.as_json:
        print(json.dumps(seen, ensure_ascii=False, indent=2))
        return 0

    name = roster[args.agent].get("name") or args.agent
    print(f"{name} 此刻收到的提示词:{len(seen['blocks'])} 块 / {seen['system_chars']} 字")
    print(f"(假设你说的是「{args.message}」)\n")
    for block in seen["blocks"]:
        head = (block["text"].splitlines() or [""])[0]
        share = block["chars"] * 100 // max(1, seen["system_chars"])
        print(f"  {block['label']:<14} {block['chars']:>5} 字 {share:>3}%  {head[:44]}")
        if args.full:
            for line in block["text"].splitlines():
                print(f"      │ {line}")
            print()
    if seen["absent"]:
        # 缺席比多余难查得多:世界照跑、她照说话,只是从来没提那棵树。
        print("\n没出现的块,以及为什么:")
        for label, why in seen["absent"].items():
            print(f"  {label:<14} {why}")
    return 0


def run_chat(args: argparse.Namespace) -> int:
    """和一个角色对话的 REPL(#6)。

    引擎最像人的那件能力,过去只有写 Python 才够得着:`World.chat_reply` →
    `record_chat_turn` 早就齐全,缺的只是一道门。这里就是那道门,没有新的引擎
    能力。

    **时钟不走**:对话发生在世界的此刻,退出时世界还停在原地。要让世界一边活
    一边聊,那是宿主应用的事(`World.open` + `start_clock` + `chat`)——一个
    CLI 不该在你打字时偷偷推进别人的世界。

    转录留在这个进程里,每轮只把最近若干条传进世界(纪律:完整转录归宿主)。
    每说完一轮就 `record_chat_turn`,于是**说完一句话那一刻 db 就是完整的**。
    """
    from anima_world.api import World

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "chat"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[chat] {exc}", file=sys.stderr)
        return 2

    try:
        roster = world.state().get("agents", {})
        if args.list_only or args.agent is None:
            _print_roster(world, world_id)
            return 0
        if args.agent not in roster:
            print(f"[chat] 这个世界里没有 {args.agent!r}。", file=sys.stderr)
            _print_roster(world, world_id)
            return 2

        agent_id = args.agent
        info = roster[agent_id]
        display_name = args.name or "访客"
        # 走到对方跟前再开口:在场块(时间/地点/同地者)靠玩家所在地才成立,
        # 会话也才知道自己发生在哪儿。地点读不出来就算了,聊天不该因此告吹。
        if info.get("location"):
            try:
                world.player_move(args.player_id, info["location"])
            except (KeyError, ValueError):
                logger.debug("player could not be placed at %r", info.get("location"))

        degraded = world.state().get("runtime", {}).get("llm", {}).get("degraded_reason")
        print(onboarding.rule(f"{info.get('name', agent_id)} @ {info.get('location') or '?'}"))
        if degraded:
            print(f"  {onboarding.yellow('这个世界正跑在 Mock 上')}({degraded})——"
                  f"回复会是模板。配一个:anima-world config set llm.api_key sk-…")
            # 没有 LLM 时,关系判定每一轮都要抱怨一次"读不出 JSON"—— 那是上面
            # 这句话的必然结果,不是新消息,而它会横插在对话中间。真 LLM 下的
            # 同一句话是真信号,所以只在已降级时闭嘴。
            logging.getLogger("anima_world.relationship_judge").setLevel(logging.ERROR)
        print(f"  {onboarding.dim('说点什么。空行或 Ctrl-D / Ctrl-C 结束。')}\n")

        history: list[dict[str, str]] = []
        turns = 0
        while True:
            try:
                line = input(f"{display_name} > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                break
            history.append({"role": "user", "content": line})
            try:
                reply = world.chat_reply(
                    agent_id, history[-20:],
                    player_id=args.player_id, display_name=display_name,
                )
            except (KeyError, ValueError) as exc:
                print(f"[chat] {exc}", file=sys.stderr)
                history.pop()
                continue
            reply = reply.strip() or "……"
            print(f"{info.get('name', agent_id)} > {reply}\n")
            history.append({"role": "assistant", "content": reply})
            # 一轮一记:关系判定在这里发生,世界也在这里落盘。
            world.record_chat_turn(agent_id, args.player_id, history[-2:])
            turns += 1

        if turns:
            print(f"  {onboarding.dim(f'聊了 {turns} 轮,已经记进这个世界。')}\n")
        return 0
    finally:
        world.close()


_PLAY_HELP = """  /who            这会儿谁在哪、在做什么
  /at <角色 id>   换一个人说话
  /quit           离开(世界会停下并存档)
  其它任何一行     说给当前这个人听"""


def _play_burst(
    world: Any, args: argparse.Namespace, agent_id: str,
    turn: list[dict[str, str]], display_name: str, speaker: str,
    meta: dict[str, Any],
) -> str:
    """连续输出模式:逐条打印她说的每一句,工具与让位也显示出来。

    返回拼起来的整段(交回 `record_chat_turn`,世界那边仍然是一轮)。
    """
    said: list[str] = []
    for step in world.chat_burst(
        agent_id, turn[-20:], player_id=args.player_id, display_name=display_name,
    ):
        kind = step.get("kind")
        if kind == "message":
            said.append(step["text"])
            print(f"{speaker} > {step['text']}")
            step_meta = step.get("meta") or {}
            if step_meta.get("stance"):
                meta.setdefault("stance", step_meta["stance"])
            for call in step_meta.get("tool_calls") or []:
                meta.setdefault("tool_calls", []).append(call)
        elif kind == "intent":
            meta.setdefault("intent", step.get("intent"))
            meta.setdefault("intent_confidence", step.get("confidence"))
            if step.get("reason"):
                meta.setdefault("intent_reason", step["reason"])
        elif kind == "tool_call":
            ok = (step.get("result") or {}).get("ok", True)
            label = step["tool"] + ("" if ok else "(没成)")
            print(f"  {onboarding.dim('[' + label + ']')}")
        elif kind == "stop" and step.get("reason") not in ("explicit_yield", "implicit_yield"):
            reason = str(step.get("reason") or "")
            print(f"  {onboarding.dim('(她停下了:' + reason + ')')}")
    return "".join(said).strip() or "……"


def _play_meta_line(meta: dict[str, Any]) -> str:
    """一轮的观测量:她的意图、你这句被判成什么、她调了什么。看不见就等于没有。"""
    bits: list[str] = []
    if meta.get("stance"):
        from anima_world import stance as stance_mod

        suffix = "" if meta.get("stance_declared") else "?"
        bits.append(f"stance={stance_mod.label(meta['stance'])}{suffix}")
    if meta.get("intent"):
        confidence = meta.get("intent_confidence")
        bits.append(
            f"intent={meta['intent']}"
            + (f"({confidence:.2f})" if isinstance(confidence, (int, float)) else "")
        )
    if meta.get("intent_reason"):
        # 退回按对话处理时的原因。降级不许无声 —— 这一行就是那个"说出来"。
        bits.append(str(meta["intent_reason"]))
    for call in meta.get("tool_calls") or []:
        bits.append(call["tool"] + ("" if call.get("ok", True) else "(没成)"))
    for ignored in meta.get("ignored_tool_calls") or []:
        bits.append(f"{ignored}(开关关着,没执行)")
    return f"  {onboarding.dim('· ' + '  '.join(bits))}\n" if bits else ""


def run_play(args: argparse.Namespace) -> int:
    """在**活着**的世界里说话。

    `chat` 说话但时钟不走,`run` 时钟走但说不了话 —— 于是"跟一个正在过日子的角色
    对话"这件事,在命令行上一直做不到。它恰好是这个引擎最想让人看到的那一件事:
    你上一句话说完到下一句之间,她可能已经走去了别的地方。

    与 `chat` 的另一处不同:每说一句之前先把玩家挪到对方所在地,所以判定是面对面
    还是手机私聊会**随她走动而变**。
    """
    from anima_world.api import AgentUnavailable, World

    redis, world_id, mysql = _world_args(args)
    try:
        world = World.open(
            world_id, redis=redis, mysql=mysql,
            world_file=args.world_file, beats_path=args.beats, agents=args.agents,
        )
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[play] {exc}", file=sys.stderr)
        return 2

    try:
        roster = world.state().get("agents", {})
        if not roster:
            print("[play] 这个世界还没有住人。", file=sys.stderr)
            return 2
        agent_id = args.agent or next(iter(roster))
        if agent_id not in roster:
            print(f"[play] 没有 {agent_id} 这个人。这里住着:{', '.join(roster)}",
                  file=sys.stderr)
            return 2

        display_name = args.name or "访客"
        world.start_clock()
        degraded = world.state().get("runtime", {}).get("llm", {}).get("degraded_reason")
        print(onboarding.rule(f"{world_id} —— 时钟在走"))
        if degraded:
            print(f"  {onboarding.yellow('这个世界正跑在 Mock 上')}({degraded})——回复会是模板。")
            logging.getLogger("anima_world.relationship_judge").setLevel(logging.ERROR)
        print(_PLAY_HELP)
        print()

        # 落座:在第一句话之前就把玩家放到她跟前,而不是等第一条消息才注册在场。
        # 不这样做的话,「她自己主动来找你」(issue #13 的闲置搭话 / autonomy 的
        # reach_out)在会话的头几个 tick 里必然啥都等不到——世界还不知道玩家在哪。
        start_here = (world.state().get("agents", {}).get(agent_id) or {}).get("location")
        if start_here:
            try:
                world.player_move(args.player_id, start_here)
            except (KeyError, ValueError):
                logger.debug("player could not be placed at %r", start_here)
        last_hail_seq = 0

        history: dict[str, list[dict[str, str]]] = {}
        turns = 0
        while True:
            here = world.state().get("agents", {}).get(agent_id, {})
            hails = world.inbox(args.player_id, since_seq=last_hail_seq)
            for hail in hails:
                last_hail_seq = max(last_hail_seq, int(hail.get("seq", 0)))
                payload = hail.get("payload") or {}
                who = payload.get("agent_id", "?")
                text = str(payload.get("text") or "").strip()
                # #13 的闲置搭话没带话(text 是空的),autonomy 的 reach_out 带一句。
                note = f"{who} 主动来找你了" + (f":{text}" if text else "(她过来打了个招呼)")
                print(f"  {onboarding.dim('· ' + note)}")
            try:
                line = input(f"{display_name} → {here.get('name', agent_id)} > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in ("/quit", "/q", "/exit"):
                break
            if line == "/who":
                _print_roster(world, world_id)
                continue
            if line.startswith("/at "):
                target = line[4:].strip()
                if target in world.state().get("agents", {}):
                    agent_id = target
                else:
                    print(f"  没有 {target} 这个人。", file=sys.stderr)
                continue
            if line.startswith("/"):
                print(_PLAY_HELP)
                continue

            # 走到她跟前再开口 —— 她这会儿在哪是会变的,所以每轮都问一次。
            where = (world.state().get("agents", {}).get(agent_id) or {}).get("location")
            if where:
                try:
                    world.player_move(args.player_id, where)
                except (KeyError, ValueError):
                    logger.debug("player could not be placed at %r", where)

            turn = history.setdefault(agent_id, [])
            turn.append({"role": "user", "content": line})
            speaker = here.get("name", agent_id)
            meta: dict[str, Any] = {}
            try:
                if world.config_get("chat.loop.enabled", False):
                    # #17:她可以连着说几句、中途做件事,然后自己停下。逐条打印,
                    # 因为"她还在说"和"她说完了"对聊天的人是两种不同的体验。
                    reply = _play_burst(world, args, agent_id, turn, display_name, speaker, meta)
                else:
                    reply = world.chat_reply(
                        agent_id, turn[-20:],
                        player_id=args.player_id, display_name=display_name, meta=meta,
                    )
                    reply = reply.strip() or "……"
                    print(f"{speaker} > {reply}")
                print(_play_meta_line(meta))
            except AgentUnavailable as exc:
                # 软静音:她不理你。这不是报错 —— 是她的选择,所以照她的口气说。
                print(f"  {onboarding.yellow(str(exc))}\n")
                turn.pop()
                continue
            except (KeyError, ValueError) as exc:
                print(f"[play] {exc}", file=sys.stderr)
                turn.pop()
                continue
            turn.append({"role": "assistant", "content": reply})
            world.record_chat_turn(agent_id, args.player_id, turn[-2:], meta=meta)
            turns += 1

        now = world.state().get("world_time", {})
        stamp = f"第 {now.get('day', 0)} 天 {now.get('hour', 0):02d}:{now.get('minute', 0):02d}"
        print(f"  {onboarding.dim(f'聊了 {turns} 轮;世界走到{stamp}。')}")
        return 0
    finally:
        world.close()


def _live_owner(redis: Any, world_id: str) -> tuple[str, str] | None:
    """这个世界的 `:meta` 上有没有"正被谁跑着"的戳。读不出来就当没有 —— 提示不是锁。"""
    from anima_world.redis_state import meta_rows

    try:
        meta = meta_rows(redis, world_id)
        pid = meta.get("owner_pid")
        host = meta.get("owner_host")
    except Exception:  # noqa: BLE001 - 提示读不出来不该拦人
        return None
    return (str(pid), str(host or "?")) if pid else None


def _warn_if_live(redis: Any, world_id: str) -> None:
    """对一个正在跑的世界动手之前,说一声。

    只提示不拒绝:进程崩掉标记就陈旧,拿陈旧标记去拒绝操作,等于在真出事那天把人
    挡在门外。Redis 时代 `config set` 写的行,运行中的进程**同样不会重读**
    (ConfigStore 有内存缓存)—— 要下次重启才生效,这一点和 world.db 时代一样。
    """
    owner = _live_owner(redis, world_id)
    if owner is None:
        return
    pid, host = owner
    print(
        f"  {onboarding.yellow('这个世界正被 pid ' + str(pid) + ' @ ' + str(host) + ' 跑着')}"
        f" —— 写进去的配置那个进程不会重读,要下次重启才生效。",
        file=sys.stderr,
    )


def _open_config_store(redis: Any, world_id: str) -> ConfigStore:
    """Open a world's config without standing up a whole scheduler.

    Shared by `start` / `config` / `doctor`。Redis 时代没有"要不要建文件"的
    问题:`:config` 是个 hash,读写它不会创造世界。
    """
    from anima_world.redis_state import RedisConfigBackend

    return ConfigStore(RedisConfigBackend(redis, world_id), lock=threading.RLock())


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

    redis, world_id, mysql = _world_args(args)
    is_new_world = not _world_exists(redis, world_id)
    # Every pasteable hint below has to point at THIS world; without it the
    # reader's `config set` cheerfully writes into the default-named one.
    where = "" if world_id == _world_id_default() else f" --world-id {world_id}"

    print(onboarding.rule("ANIMA 世界引擎"))

    # ① LLM — first, because it decides what kind of world you get.
    config_store = _open_config_store(redis, world_id)
    status = onboarding.llm_status(config_store, world_id)
    print(f"\n  {onboarding.bold('① LLM')}")
    if status.degraded and not args.no_input and onboarding.can_prompt():
        _print_llm_line(status, indent="     ")
        if onboarding.configure_llm_interactively(config_store):
            status = onboarding.llm_status(config_store, world_id)
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

    # ② The world, and how fast its clock runs.
    print(f"\n  {onboarding.bold('② 世界')}")
    if is_new_world and not args.real_time:
        # The packaged default is real time (1 tick / 5 real minutes), which
        # makes a brand-new world look frozen for the first five minutes
        # somebody watches it. A world they just created gets a visible clock.
        config_store.set("scheduler.tick_rate", 1.0)
    tick_rate = config_store.get("scheduler.tick_rate", default=1.0)
    minutes_per_tick = config_store.get("world.minutes_per_tick", default=DEFAULT_MINUTES_PER_TICK)

    print(f"     {onboarding.green(onboarding.OK)} {'新建' if is_new_world else '沿用'} {world_id}"
          f"{onboarding.dim('(住在 ' + (getattr(args, 'redis', None) or _redis_url_default()) + ')')}")
    print(f"     {onboarding.dim('时钟:' + onboarding.human_tick_rate(tick_rate, int(minutes_per_tick)))}")
    if is_new_world and not args.real_time:
        print(f"     {onboarding.dim('想要真实时间:anima-world config set scheduler.tick_rate 0.00333' + where)}")

    try:
        world = World.open(world_id, redis=redis, mysql=mysql,
                           world_file=args.world_file, beats_path=args.beats)
    except (BeatScriptError, WorldSeedError) as exc:
        what = "节拍脚本" if isinstance(exc, BeatScriptError) else "世界种子"
        print(f"\n  {onboarding.red(onboarding.BAD)} {what}有问题:\n{exc}", file=sys.stderr)
        return 2
    print(f"     {onboarding.green(onboarding.OK)} {len(world.scheduler.agents)} 个角色就位:"
          f" {'、'.join(brain.agent.name for brain in world.scheduler.agents.values())}")

    # ③ Let it live. The engine is a library — this process is the world's
    #    host; anything else (a site, a tool) imports anima_world.api and
    #    holds its own World.
    print(f"\n  {onboarding.bold('③ 运行')}")
    print(f"     {onboarding.dim('世界在本进程里运行,叙事会打印在下面;停止:Ctrl-C')}")
    print(f"     {onboarding.dim('程序里用:from anima_world.api import World; World.open(…)')}")
    print(f"     {onboarding.dim('体检:anima-world doctor   改配置:anima-world config list')}\n")

    _run_world_foreground(world)
    print("\n  世界已停下(状态都在 Redis 里)。下次接着跑:"
          f"anima-world start{where}\n")
    return 0


def run_config(args: argparse.Namespace) -> int:
    """Read/write world settings without curl."""
    from anima_world.config_store import mask_secret

    redis, world_id, _ = _world_args(args)
    if args.config_command != "set" and not _world_exists(redis, world_id):
        print(f"[config] 还没有 {world_id!r} 这个世界。\n"
              f"         先跑一次 anima-world start 创建它。", file=sys.stderr)
        return 2
    if args.config_command == "set":
        _warn_if_live(redis, world_id)
    store = _open_config_store(redis, world_id)
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
                # 来源和值一样要紧:"为什么我改了配置没生效"几乎总是这个问题,而
                # 世界文件里现在只剩作者动过的,所以这一栏第一次答得上来。
                print(f"  {row['key']:<{width}}  {value}  {onboarding.dim(row['source'])}")
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
        # **人不该知道哪个键去哪儿。** `llm.*` 属于这台机器(你用哪家模型、哪把钥匙,
        # 和"这个世界是什么样"无关),自动写进机器配置;别的写进世界。
        from anima_world import machine_config

        shown = mask_secret(str(value)) if meta.get("is_secret") else value
        if machine_config.is_machine_key(args.key):
            path = machine_config.set_value(args.key, value)
            print(f"{onboarding.green(onboarding.OK)} {args.key} = {shown}")
            print(onboarding.dim(f"  写进了 {path}(0600) —— 这台机器上所有世界共用"))
            print(onboarding.dim("  它**不进世界文件**:打包发出去的世界不该带着你的钥匙"))
            env = machine_config.env_value(args.key)
            if env is not None:
                # 写了但不生效是最难查的一种,当场说破。
                print(onboarding.yellow(
                    f"  ⚠️ 但环境变量里也有 {args.key},而环境变量优先 —— 这次改动此刻不生效"
                ))
            return 0
        store.set(args.key, value)
        print(f"{onboarding.green(onboarding.OK)} {args.key} = {shown}")
        return 0
    finally:
        pass


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


def _load_json_file(path: str) -> tuple[Any, str | None]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle), None
    except FileNotFoundError:
        return None, f"文件不存在:{path}"
    except json.JSONDecodeError as exc:
        return None, f"不是合法的 JSON({path}):{exc}"
    except OSError as exc:
        return None, f"读不出来({path}):{exc}"


def _report_validation(
    label: str, path: str, errors: list[str], warnings: list[str], as_json: bool
) -> int:
    """错误 → 退出码 2;只有提醒 → 退出码 0。

    **提醒不算失败**是这两条命令的核心语义:引擎没有"合法值全集"这种东西,把引用
    完整性做成拒绝,会让设计正确的世界在一次小版本升级后开不了机。
    """
    if as_json:
        print(json.dumps({
            "operation": f"validate {label}", "path": path,
            "valid": not errors, "errors": errors, "warnings": warnings,
        }, ensure_ascii=False, indent=2))
        return 2 if errors else 0

    if errors:
        print(onboarding.rule(f"{path} —— 不能用"))
        for line in errors:
            print(f"  ✗ {line}")
    else:
        print(onboarding.rule(f"{path} —— 可以用"))
    for line in warnings:
        print(f"  {onboarding.yellow('!')} {line}")
    if not errors and not warnings:
        print("  没有发现问题。")
    elif not errors and warnings:
        print(f"\n  {onboarding.dim('上面这些是提醒,不阻止世界启动 —— 引擎不假装知道什么是合法值全集。')}")
    return 2 if errors else 0


# 事件流带不走什么。**写进制品的 header,不是写进某份没人会读的文档** —— 一份不说明
# 自己缺什么的导出,比没有导出更危险:拿到的人会以为那就是整个世界。
_EVENTS_NOT_INCLUDED = (
    "图谱边(edges 表)——关系结构不在事件流里,重放不出来",
    "记忆强度与反思水位——派生态,重放后归零(记得什么还在,记得多牢不在)",
    "静默尾部的世界时钟——最后一段没有事件的时间在 db_meta 里,不在事件里",
    "聊天转录——世界侧只有会话摘要,完整对话按设计留在宿主应用",
)


def _open_event_log(redis: Any, world_id: str, mysql: Any):
    """只读命令直开事件日志:mysql 给了就读 MySQL(那才是真相),否则读 Redis。"""
    if mysql is not None:
        from anima_world.mysql_state import MySQLEventLog, as_connection

        return MySQLEventLog(as_connection(mysql), f"{world_id}_")
    from anima_world.redis_state import RedisEventLog, events_key

    return RedisEventLog(redis, events_key(world_id))


def run_events(args: argparse.Namespace) -> int:
    """把事件日志导成 JSONL:一行一个事件,不依赖 db 格式。

    issue #8 的连续性通路,**只做导出这一半**。刻意不做重放端:事件日志今天还不完备
    (见 header 里那四项),在它补齐之前把这份东西固化成第四条跨仓库线格式,等于把
    一个已知缺陷刻进契约。所以 `replayable` 恒为 false —— 不承诺,承诺了就得兑现。

    只读:`mode=ro`,文件不存在退 2,不建库、不生成密钥(同 `report`)。
    """
    import anima_world

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "events"):
        return 2
    log = _open_event_log(redis, world_id, mysql)
    events = log.replay()
    rows = [(e.seq, e.ts, e.type, e.who, e.loc, json.dumps(e.payload, ensure_ascii=False))
            for e in events]

    header = {
        "kind": "anima-events",
        "stream_format_version": 1,
        "engine_version": anima_world.__version__,
        "events": len(rows),
        # 这两条是这份制品最重要的内容。
        "replayable": False,
        "not_included": list(_EVENTS_NOT_INCLUDED),
    }

    def _emit(handle: Any) -> None:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        for seq, ts, type_, who, loc, payload in rows:
            try:
                parsed = json.loads(payload) if payload else {}
            except (TypeError, ValueError):
                parsed = {"_unparseable": payload}
            handle.write(json.dumps({
                "seq": seq, "ts": ts, "type": type_,
                "who": who, "loc": loc, "payload": parsed,
            }, ensure_ascii=False) + "\n")

    if args.output == "-":
        _emit(sys.stdout)
        return 0
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        _emit(handle)
    print(f"[events] {len(rows)} 条事件 → {out}")
    print(f"  {onboarding.dim('这不是一份可重放的世界 —— header 里写着它带不走什么。')}")
    return 0


def run_report(args: argparse.Namespace) -> int:
    """对着一个已经存在的世界出摘要。**只读,不推时钟,不建任何东西。**

    事件日志是唯一真相、`sim_report` 是纯函数 —— 这本来就该是一条只读命令。
    只读纪律的新形态:不存在的 world_id 当场退 2(打开会创世,见
    `_require_existing_world`)。

    ⚠️ 对着一个**正在跑**的世界读到的是某个瞬间的快照,而且尾巴可能缺(叙事与判定
    在线程池上)。摘要里会写明这一点。
    """
    from anima_world.redis_state import RedisClock, clock_key
    from anima_world.sim_report import build_run_report
    from anima_world.world_time import DEFAULT_MINUTES_PER_TICK

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "report"):
        return 2
    log = _open_event_log(redis, world_id, mysql)
    events = log.replay()
    store = _open_config_store(redis, world_id)
    try:
        mpt = int(store.get("world.minutes_per_tick", default=DEFAULT_MINUTES_PER_TICK))
    except (TypeError, ValueError):
        mpt = DEFAULT_MINUTES_PER_TICK
    clock = RedisClock(redis, clock_key(world_id)).get()
    owner = _live_owner(redis, world_id)

    report = build_run_report(events, ticks=clock, minutes_per_tick=mpt)
    if owner is not None:
        report["snapshot_of_a_running_world"] = True

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[report] 已写入 {args.output}")
        return 0
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    world = report["world"]
    print(onboarding.rule(f"{world_id} —— 跑了 {world['days']} 个世界日"))
    if owner is not None:
        print(f"  {onboarding.yellow('注意')}:这个世界正被 pid {owner[0]} 跑着,"
              f"下面是某个瞬间的快照,尾巴可能还没落盘。")
    print(f"  事件 {report['events']['total']} 条"
          f"(其中墙钟 {report['events']['wall_clock_events']} 条)"
          f",{len(world['agents'])} 个角色")
    for agent in report["agents"]:
        share = agent["share_by_activity"]
        busiest = max(share, key=lambda k: share[k]) if share else "?"
        flag = onboarding.yellow("  ← 整场没发生什么") if agent["idle_only"] else ""
        print(f"    {_pad(agent['id'], 10)}{agent['events']:>5} 件事"
              f"   多数时间在{busiest}{flag}")
    for pair in report["encounters"]:
        print(f"    {pair['a']} × {pair['b']}:相遇 {pair['meetings']} 次,"
              f"共 {pair['minutes']} 分钟")
    for curve in report["relationships"]:
        print(f"    {curve['as']} → {curve['target']}:"
              f"{curve['start']} → {curve['end']}({curve['turning_points']} 个拐点)")
    return 0


def _load_authored_layer(path: str) -> tuple[dict[str, Any], str | None]:
    """读一个世界文件的**作者层**,聚合成 section 字典。读不了就把话说清楚。"""
    from anima_world.world_file import WorldFileError, author_records_to_seed, read_world_file

    try:
        _, records = read_world_file(path)
        return author_records_to_seed(list(records)), None
    except WorldFileError as exc:
        return {}, str(exc)
    except OSError as exc:
        return {}, f"读不了 {path}:{exc}"


def run_validate(args: argparse.Namespace) -> int:
    """不建世界就检查一份种子 / 一份节拍脚本。

    CLAUDE.md 写着"创作台经 CLI 委托校验",而这个入口一直不存在 —— 于是作者唯一
    的检查办法是真开一次世界,而种子只读进空库一次,试错的代价是重建世界。
    """
    from anima_world.beats import beat_script_warnings
    from anima_world.world_seed import world_seed_errors, world_seed_warnings

    if args.validate_command in ("world", "seed"):
        # 世界文件:读出作者层再走既有的那套校验(**换了容器,没换闸**)。
        authored, read_error = _load_authored_layer(args.path)
        if read_error is not None:
            return _report_validation("world", args.path, [read_error], [], args.json)
        return _report_validation(
            "world", args.path,
            world_seed_errors(authored), world_seed_warnings(authored), args.json,
        )

    data, read_error = _load_json_file(args.path)
    if read_error is not None:
        return _report_validation(
            args.validate_command, args.path, [read_error], [], args.json
        )

    # beats:硬错误走既有的严格校验器(与加载期逐字同一份),引用完整性只提醒。
    errors: list[str] = []
    try:
        BeatScript.from_data(data)
    except BeatScriptError as exc:
        errors = list(getattr(exc, "errors", None) or [str(exc)])

    known_agents: list[str] = []
    known_locations: list[str] = []
    if args.seed:
        seed_data, seed_error = _load_authored_layer(args.seed)
        if seed_error is not None:
            errors.append(f"--world-file {seed_error}")
        elif isinstance(seed_data, dict):
            known_agents = [
                str(a.get("id")) for a in seed_data.get("agents") or []
                if isinstance(a, dict) and a.get("id")
            ]
            known_locations = [
                str(loc.get("id")) for loc in seed_data.get("locations") or []
                if isinstance(loc, dict) and loc.get("id")
            ]

    warnings = beat_script_warnings(
        data, known_agents=known_agents, known_locations=known_locations
    )
    if not args.seed:
        warnings.append(
            "没给 --world-file,所以没检查角色/地点是不是真的存在 —— 引用错一个 id,"
            "那个 beat 会静默作废并被永久标记已触发"
        )
    return _report_validation("beats", args.path, errors, warnings, args.json)


def contract_payload() -> dict[str, Any]:
    """这个引擎的对外线格式,一份机器可读的自述。

    本仓库是跨语言契约的权威,别人持有镜像(运维台的 `lib/worldPackage.js` /
    `lib/worldSeed.js`)。镜像端要知道"我对齐的是哪一版",今天只有读 Python 源码
    一条路 —— 于是镜像会悄悄落后,而**落后的镜像不报错,它只是对新格式给出旧答案**。

    种子 schema 与节拍脚本没有版本号(它们随主版本走),所以这里报**形状**:
    镜像可以直接 diff 键集与 op 表。要跑不了世界也能回答,所以不碰 db、不建库。
    """
    import anima_world
    from anima_world.beats import (
        OP_REQUIRED_FIELDS,
        PREDICATE_REQUIRED_FIELDS,
        VALID_OPS,
        _VALID_PREDICATES,
    )
    from anima_world.sim_report import BUCKETS, REPORT_FORMAT_VERSION
    from anima_world.world_package import PACKAGE_FORMAT_VERSION
    from anima_world.world_seed import WORLD_SEED_AGENT_KEYS, WORLD_SEED_LOCATION_KEYS

    return {
        "operation": "contract",
        "engine_version": anima_world.__version__,
        # world.db 退役(2.0):世界住 Redis,键前缀就是"格式"。镜像端此前读
        # `db.*` —— 那一节没有了,读到缺键就该知道要对齐这一版。
        "storage": {
            "backend": "redis",
            "key_prefix": "anima:{world_id}:",
            "mysql_tables": ["events", "memories", "conversations", "messages"],
            "mysql_table_prefix": "{world_id}_",
        },
        # 她能调的能力**全目录**(声明在代码,`@tool` 登记)。宿主要显示"她走开了"
        # 之类的事件,得先知道有哪些能力会产生它们。
        #
        # ⚠️ 字段名是历史包袱:它**不只有 chat 面**。`reach_out` 只在定时轮次里
        # 出现,聊天里永远不会被调用 —— 照着这个名字做一个"聊天能力"列表就会把它
        # 也列进去。所以每条带上 `surfaces`,消费方按它过滤;名字不改是因为运维台
        # 镜像已经在读 `chat_tools`,改名等于跨仓库破坏。
        "chat_tools": [
            {
                "id": spec.id, "kind": spec.kind,
                "params": sorted(spec.params_schema),
                "surfaces": list(spec.surfaces),
            }
            for spec in chat_tools.tools_for("*")
        ],
        "package": {"format_version": PACKAGE_FORMAT_VERSION},
        "report": {"format_version": REPORT_FORMAT_VERSION, "buckets": list(BUCKETS)},
        "seed": {
            "schema_version": None,  # 无版本号:随主版本走
            "agent_keys": sorted(WORLD_SEED_AGENT_KEYS),
            "location_keys": sorted(WORLD_SEED_LOCATION_KEYS),
        },
        "beats": {
            "schema_version": None,
            "ops": sorted(VALID_OPS),
            "op_required_fields": {
                op: sorted(fields) for op, fields in sorted(OP_REQUIRED_FIELDS.items())
            },
            "predicates": sorted(_VALID_PREDICATES),
            "predicate_required_fields": {
                pred: sorted(fields)
                for pred, fields in sorted(PREDICATE_REQUIRED_FIELDS.items())
            },
        },
    }


def run_contract(args: argparse.Namespace) -> int:
    payload = contract_payload()
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(onboarding.rule(f"anima-world {payload['engine_version']} 的对外契约"))
    print(f"  存储           {payload['storage']['backend']}   "
          f"键前缀 {payload['storage']['key_prefix']}")
    print(f"  MySQL 表       {', '.join(payload['storage']['mysql_tables'])}"
          f"(表前缀 {payload['storage']['mysql_table_prefix']})")
    for surface in ("chat", "autonomy"):
        ids = [t["id"] for t in payload["chat_tools"] if surface in t["surfaces"]]
        print(f"  {surface:<8} 能力  {', '.join(ids)}")
    print(f"  包格式         {payload['package']['format_version']}   .cyberworld")
    print(f"  报表口径       {payload['report']['format_version']}   simulate --report")
    print(f"  种子 schema    agents{payload['seed']['agent_keys']} "
          f"locations{payload['seed']['location_keys']}")
    print(f"  节拍 op        {', '.join(payload['beats']['ops'])}")
    print(f"  节拍谓词       {', '.join(payload['beats']['predicates'])}")
    print(f"\n  {onboarding.dim('持有镜像的仓库用 --json 对齐;种子与节拍没有版本号,随主版本走。')}")
    return 0


def run_doctor(args: argparse.Namespace) -> int:
    """Check the things that fail quietly. Non-zero exit when one of them has."""
    from anima_world.redis_state import durability_warning, events_key, RedisEventLog

    redis, world_id, _ = _world_args(args)
    print(onboarding.rule("体检"))
    problems = 0
    _warn_if_live(redis, world_id)

    if not _world_exists(redis, world_id):
        print(f"  {onboarding.red(onboarding.BAD)} 这个 Redis 上没有叫 {world_id!r} 的世界")
        print(f"      {onboarding.dim('anima-world start 会创建它')}")
        return 1
    print(f"  {onboarding.green(onboarding.OK)} 世界 {world_id} @ "
          f"{getattr(args, 'redis', None) or _redis_url_default()}")

    # **这个 Redis 会不会把世界忘掉** —— 忘掉的样子不是报错,是世界悄悄退回创世。
    warning = durability_warning(redis)
    if warning:
        problems += 1
        print(f"  {onboarding.yellow(onboarding.WARN)} {warning}")

    store = _open_config_store(redis, world_id)
    log = RedisEventLog(redis, events_key(world_id))
    events = log.count()
    joined = {e.who for e in log.replay() if e.type == "agent_join" and e.who}
    print(f"  {onboarding.green(onboarding.OK)} {len(joined)} 个角色,{events} 条事件")

    status = onboarding.llm_status(store, world_id)
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

    # **一个值从哪来,和它是什么一样重要。**
    where = store.provenance("llm.api_key")
    if store.get("llm.api_key", default=""):
        print(f"  {onboarding.green(onboarding.OK)} llm.api_key 来自{where}")
        if where == "世界文件":
            print(f"  {onboarding.yellow(onboarding.WARN)} llm.api_key 还在"
                  f"**世界**里 —— 它属于这台机器,不属于这个世界")
            print(f"      {onboarding.dim('搬出来:anima-world config set llm.api_key sk-…')}")

    # 背景槽:意图分类与自主决策是**便宜活**,空着的背景槽会退回主模型。
    background = str(store.get("llm.background.model", default="") or "").strip()
    cheap_users = [
        (key, label) for key, label in (
            ("chat.intent.enabled", "意图分类(每轮一次,串在回复前面)"),
            ("autonomy.enabled", "定时轮次的决定"),
            ("chat.loop.enabled", "连说的每一步"),
        ) if store.get(key, default=False)
    ]
    main_model = str(store.get("llm.model", default="") or "").strip() or "(未设置)"
    if background:
        print(f"  {onboarding.green(onboarding.OK)} 背景槽用 {background}(便宜活不走主模型)")
    elif cheap_users:
        print(f"  {onboarding.yellow(onboarding.WARN)} 背景槽没配 —— "
              f"这些在用主模型 {main_model}:")
        for _, label in cheap_users:
            print(f"      {onboarding.dim('· ' + label)}")
        print(f"      {onboarding.dim('anima-world config set llm.background.model <一个便宜快的模型>')}")

    rate = store.get("scheduler.tick_rate", default=1.0)
    mpt = int(store.get("world.minutes_per_tick", default=DEFAULT_MINUTES_PER_TICK))
    print(f"  {onboarding.green(onboarding.OK)} 时钟 {onboarding.human_tick_rate(rate, mpt)}")

    print()
    if problems:
        print(f"  {onboarding.yellow(str(problems) + ' 项需要处理')}(世界仍然能跑,只是会降级)\n")
        return 1
    where_arg = "" if world_id == _world_id_default() else f" --world-id {world_id}"
    print(f"  {onboarding.green('一切正常。')} anima-world start{where_arg}\n")
    return 0


def run_simulate(args: argparse.Namespace) -> int:
    """Fast-forward a world headlessly: no sleep, no clock thread.

    ⚠️ **定时轮次(autonomy)不在快进里跑。** 它挂在 `World` 上(`_install_autonomy`
    是 `_autonomy_hook` 全仓库唯一的赋值处),而这里直接建 scheduler —— 于是
    `start` / `run` 会问她"此刻想做点什么吗",`simulate` 从来不问。
    这是有意的:快进一年 = 每个角色每 6 世界小时一次 LLM 调用,上千次网络往返,
    而快进的全部意义是不等。但**不许无声** —— 开关开着时下面会打一行说明,
    否则用户看到的是 `autonomy_stats()` 全 0,分不清"她不想做"和"根本没跑"。

    叙事与规划照旧在快进里跑(它们的池子在退出前会被排空),所以别读成
    "快进不打 LLM";漏的只有这一个。

    Builds the same scheduler `run` would (duties/planner/memory/
    persistence all wired), drives the tick loop synchronously, then drains
    the narrative/planner pools before exiting — the run is meant to be
    picked up by `run --world-id` afterward. Nothing extra is written on the
    way out: every tick's events are already in the log, and the log is the
    only truth a reopen needs.
    """
    from anima_world.world_time import TICKS_PER_DAY

    tier = "mock" if args.no_llm else args.llm  # --no-llm wins (back-compat alias)

    # Preflight BEFORE building the scheduler (code review Round 1 #4): first
    # boot genesis-seeds the capability catalog through the LLM, so aborting
    # after construction would leave a fresh DB permanently seeded with the
    # broken-key fallback catalog. Opening the DB here is idempotent with what
    # build_serve_scheduler does right after.
    redis, world_id, mysql = _world_args(args)
    if tier != "mock":
        preflight_store = _open_config_store(redis, world_id)
        error = onboarding.probe_llm(preflight_store)
        if error is not None:
            print(f"[simulate] LLM 预检没过:{error}\n"
                  f"           配置一个:anima-world config set llm.api_key sk-…\n"
                  f"           或改用 --llm mock / --no-llm 空跑。", file=sys.stderr)
            return 2

    try:
        scheduler = build_serve_scheduler(
            world_id,
            redis,
            mysql=mysql,
            n_agents=args.agents,
            world_file=args.world_file,
            force_mock_llm=(tier == "mock"),
            mock_narrative=(tier == "planner"),
            beats_path=args.beats,
        )
    except (BeatScriptError, WorldSeedError) as exc:
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
    # 快进的等规划纪律住在 `Scheduler.fast_forward` —— CLI 与 `World.fast_forward`
    # 共用同一份实现,免得两条快进路径慢慢长出不同的行为。
    # 缺席必须看得见:出厂种子把 autonomy 点亮了,而快进不跑它。不说这一句,
    # 用户看到的是 `autonomy_stats()` 全 0 —— 分不清"她不想做"和"根本没跑起来",
    # 而那个函数存在的唯一理由就是把这两件事分开。
    if scheduler.config_store is not None and scheduler.config_store.get(
        "autonomy.enabled", default=False
    ):
        print("[simulate] 注意:定时轮次(autonomy)不在快进里跑 —— 它每次都要打网络,"
              "而快进的意义是不等。要看她主动做事,用 anima-world run。")

    print(f"[simulate] fast-forwarding {ticks} tick(s) ...")
    outcome = scheduler.fast_forward(ticks, plan_wait_cap=args.plan_wait_cap)
    planner_gave_up = outcome["planner_gave_up"]

    # #11: read the log BEFORE stop() drains the pools, or the last narrative
    # and relationship verdicts of the run are missing from the summary —
    # exactly the tail a three-day trial cares about. Written after stop(),
    # so a failed write cannot leave a half-drained world behind.
    report = None
    if args.report is not None:
        scheduler.stop(wait=not planner_gave_up)
        from anima_world.sim_report import build_run_report

        report = build_run_report(
            scheduler.event_log.replay() if scheduler.event_log is not None else [],
            ticks=ticks,
            minutes_per_tick=mpt,
        )
    else:
        # A planner we declared dead must not hold the exit hostage either —
        # its in-flight results were written off when the budget fired.
        scheduler.stop(wait=not planner_gave_up)

    print(f"[simulate] done. clock={scheduler.clock}")
    if report is not None:
        blob = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
        if args.report == "-":
            print(blob)
        else:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(blob + "\n", encoding="utf-8")
            idle_only = [a["id"] for a in report["agents"] if a["idle_only"]]
            print(f"[simulate] report → {args.report}"
                  f"  ({report['events']['total']} 事件,"
                  f"{len(report['encounters'])} 对有过相遇"
                  + (f",{len(idle_only)} 人整场无事发生:{'、'.join(idle_only)}" if idle_only else "")
                  + ")")
    return 0


def _pad(label: str, width: int = 12) -> str:
    """中文标签按显示宽度对齐 —— 按字符个数补空格会把每一栏推歪。"""
    from anima_world.mapview import display_width

    return label + " " * max(1, width - display_width(label))


def _print_inspect_human(payload: dict[str, Any]) -> None:
    if payload["runnable"]:
        verdict = f"{payload['current_engine_version']} —— {onboarding.green('能跑')}"
    else:
        verdict = (
            f"{payload['current_engine_version']} —— "
            f"{onboarding.yellow('跑不了')}(要 >= {payload['engine_min']})"
        )
    print()
    print(f"  {payload['name'] or payload['world_id']}")
    print()
    rows = [
        ("世界 id", payload["world_id"]),
        ("一句话", payload["summary"] or "—"),
        ("格式版本", f"v{payload['format_version']}(本引擎读到 v{payload['reader_format_version']})"),
        ("要的引擎", f">= {payload['engine_min'] or '不限'}"),
        ("导出自", payload["source_engine_version"] or "—"),
        ("导出于", payload["created_at"] or "—"),
        ("大小", f"{payload['size_bytes'] // 1024} KB"),
        ("当前引擎", verdict),
    ]
    for label, value in rows:
        print(f"    {_pad(label)}{value}")
    print()


def run_world_package(args: argparse.Namespace) -> int:
    """`anima-world world export / import / inspect` —— `.cyberworld` v3。"""
    from anima_world.api import World
    from anima_world.world_file import WorldFileError
    from anima_world.world_package import (
        PackageValidationError,
        drop_world,
        import_world_file,
        inspect_world_file,
    )

    try:
        if args.world_command == "inspect":
            # **答案,不是拒绝。** 最需要这个答案的调用方,正是那个还没有对的引擎
            # 的启动器 —— 在这里因为"跑不了"而退非零,就违背了这个格式的意义。
            payload = inspect_world_file(args.package)
            if args.as_json:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                _print_inspect_human(payload)
            return 0
        if args.world_command == "export":
            redis, world_id, mysql = _world_args(args)
            if not _require_existing_world(redis, world_id, "world export"):
                return 2
            world = World.open(world_id, redis=redis, mysql=mysql, force_mock_llm=True)
            try:
                manifest = world.export_snapshot(
                    args.output, world_id=args.package_id, name=args.name,
                    beats_path=args.beats, summary=args.summary, genre=args.genre,
                    setting=args.setting, theme=args.theme,
                )
            finally:
                world.close()
            result = {
                "operation": "export",
                "world_id": manifest.world_id,
                "output": str(args.output),
                "engine_min": manifest.engine_min,
            }
        elif args.world_command == "migrate":
            # **一次性的桥**:1.x 的世界在 2.0 面前本来没有任何入口
            # (SQLite 退役了,而 v1 的包是 ZIP、v3 的读者只认 gzip JSONL)。
            from anima_world.migrate_v1 import MigrationError, write_migrated_world

            try:
                counts = write_migrated_world(
                    args.db, args.output,
                    world_id=args.package_id, name=args.name, summary=args.summary,
                )
            except MigrationError as exc:
                print(f"迁不了:{exc}", file=sys.stderr)
                return 2
            payload = {
                "operation": "world migrate",
                "source": str(args.db),
                "output": str(args.output),
                "world_id": args.package_id,
                **counts,
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(f"  迁好了 → {args.output}")
                print(f"    状态记录  {counts.get('redis', 0)}")
                print(f"    事件      {counts.get('event', 0)}")
                print(f"    增长的三样 {counts.get('mysql', 0)}")
                gaps = counts.get("seq_gaps_filled", 0)
                if gaps:
                    # 补了必须说 —— 不说的话"这个世界的历史完整吗"没人问得到。
                    print(
                        f"    ⚠ 补了 {gaps} 个空号:1.x 的 AUTOINCREMENT 在事务回滚时"
                        f"消耗掉的号,那些位置从来没有过事件。"
                    )
                print(f"\n  装进一个空世界:anima-world world import {args.output} --world-id …")
            return 0

        elif args.world_command == "drop":
            redis, world_id, mysql = _world_args(args)
            if not _require_existing_world(redis, world_id, "world drop"):
                return 2
            n = drop_world(redis, world_id, confirm=bool(args.yes), mysql=mysql)
            result = {
                "operation": "drop", "world_id": world_id,
                "keys": n, "dropped": bool(args.yes),
            }
            if not args.yes:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                print(f"[world drop] 没有 --yes,什么也没删。加上它会抹掉这 {n} 个键。",
                      file=sys.stderr)
                return 0
        else:
            redis, world_id, mysql = _world_args(args)
            manifest = import_world_file(
                args.package, redis=redis, world_id=world_id, mysql=mysql,
            )
            result = {
                "operation": "import",
                "world_id": world_id,
                "from": manifest.world_id,
                "engine_min": manifest.engine_min,
            }
    except (OSError, WorldFileError, PackageValidationError) as exc:
        # 说清是**哪一道**闸拦下的(校验和 / 格式版本 / 目标非空 / 记录坏了)。
        # 引擎一直知道,而"包无效或不可读"这种笼统话没有指出任何可做的事。
        print(f"[world {args.world_command}] {exc}", file=sys.stderr)
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
    if args.command == "prompt":
        return run_prompt(args)
    if args.command == "map":
        return run_map(args)
    if args.command == "ontology":
        return run_ontology(args)
    if args.command == "chat":
        return run_chat(args)
    if args.command == "run":
        return run_run(args)
    if args.command == "simulate":
        return run_simulate(args)
    if args.command == "events":
        return run_events(args)
    if args.command == "report":
        return run_report(args)
    if args.command == "validate":
        return run_validate(args)
    if args.command == "play":
        return run_play(args)
    if args.command == "contract":
        return run_contract(args)
    if args.command == "world":
        return run_world_package(args)
    return _print_welcome()


if __name__ == "__main__":
    sys.exit(main())
