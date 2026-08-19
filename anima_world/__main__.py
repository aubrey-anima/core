"""CLI entrypoint for anima_world."""

from __future__ import annotations

import argparse
import contextlib
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
from anima_world.perception import parse_bands, visibility_band_errors
from anima_world.planner import Planner, SyncLLM
from anima_world.character_card import (
    CARD_BILLINGS,
    card_of_seed_agent,
    world_card_errors,
    world_card_warnings,
)
from anima_world.media import (
    LOCATION_IMAGE_KEYS,
    world_location_media_errors,
    world_location_media_warnings,
)
from anima_world.projection import project_events
from anima_world.scheduler import Scheduler
from anima_world.types import Event, Projection
from anima_world.rules import drift_warnings, parse_rules
from anima_world.world_seed import WorldSeedError, apply_seed_config
from anima_world.world_seed import world_seed_errors as _world_seed_errors
from anima_world.world_time import DEFAULT_MINUTES_PER_TICK

logger = logging.getLogger(__name__)

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_WORLD_ID = "world"

# 命令行上"你"是谁。**一个,不是每个子命令一个** —— `play` 从前默认 `p1` 而
# `chat`/`prompt` 默认 `cli`,于是玩完 `play` 再照 `--help` 去 `prompt` 看她收到
# 什么,看到的是**另一个人**:名字、关系、玩家教过的规则全记在 `p1` 头上,而调试
# 视图问的是一个从没说过话的 `cli`。它不报错,只是空着 —— "调试视图撒谎比没有调试
# 视图更坏"的一种,而且这一次撒的谎是"她根本不认识你"。
DEFAULT_PLAYER_ID = "cli"


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


class _CollectedLogs(logging.Handler):
    """引擎的日志记在这儿,而不是横插进人和角色的对话里。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(record.getMessage())
        except Exception:  # noqa: BLE001 - 日志自己出错不该掀翻会话
            self.records.append(str(record.msg))


@contextlib.contextmanager
def _engine_logs_out_of_the_way(verbose: bool = False):
    """人正在跟她说话的这段时间里,引擎的日志收起来 —— `start` / `chat` / `play`。

    实测的样子:玩家问了一句,屏幕上先冒出来
    `plan step starts outside the day (1440), dropping`,再才是她的回答。那是一句
    英文的、跟玩家无关的、他也无从处理的话,横在对话正中间;而 `run` / `simulate`
    那边同一句是真信号(那里没有人在等一句台词)。

    **不是丢掉。** 收着,散场时报一行"这一场引擎记了几条";`--verbose` 就照原样打,
    什么都不拦。丢掉的话,这个仓库最怕的那种坏法(照跑但给错东西)就少了一个出口。

    此前只有关系判定那一个 logger 被单独按下去(而且只在已降级时),于是别的模块
    照旧插话 —— 一个一个按下去就是给下一个模块留一个洞。
    """
    if verbose:
        yield None
        return
    engine = logging.getLogger("anima_world")
    sink = _CollectedLogs()
    # 挂在 `anima_world` 上就够了:records 在这里找到 handler,`logging.lastResort`
    # (没配 handler 时那个直接往 stderr 打 WARNING 的兜底)便不再触发。
    engine.addHandler(sink)
    try:
        yield sink
    finally:
        engine.removeHandler(sink)
        if sink.records:
            note = (f"这一场里引擎记了 {len(sink.records)} 条警告;"
                    f"最后一条:{sink.records[-1][:60]}。加 --verbose 看全部。")
            print(f"  {onboarding.dim(note)}")


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
        "start",
        help="人的门:引导配 LLM → 创世 → 前台运行(新世界用演示速度)—— 从这里开始",
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
    start.add_argument("--verbose", action="store_true",
                      help="引擎的日志照原样打出来(默认收着,散场报一行)")

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
    chat.add_argument("--player-id", default=DEFAULT_PLAYER_ID,
                      help="你的身份 id —— 角色对你的印象记在它头上(chat/play/prompt 共用一个)")
    chat.add_argument("--name", default=None,
                  help="你的名字;不给就是「他还没告诉你名字」,她会称你「访客」但不会当成名字")
    chat.add_argument("--list", action="store_true", dest="list_only", help="只列出角色名册就退出")
    chat.add_argument("--verbose", action="store_true",
                      help="引擎的日志照原样打出来(默认收着,散场报一行)")

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
    # **这一个默认成 None,不跟 chat/play 一样默认 `cli`。** 那两条会把玩家挪进世界
    # (`player_move`),于是 `cli` 当场变成一个真人;`prompt` 是「看,但不碰」,永远
    # 不会 —— 默认值在这条命令上必然是个世界不认得的幽灵,而拿幽灵算出来的提示词
    # 和真的那一份差着三块。不给就去世界里找一个真站在她跟前的人(见 `run_prompt`)。
    prompt.add_argument("--player-id", default=None,
                        help="以谁的身份跟她说话;不给就挑一个真站在她跟前的玩家")
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

    roster_cmd = sub.add_parser(
        "roster", help="这个世界里有谁:名字、一句话、立绘、主次、此刻在哪",
    )
    _add_world_args(roster_cmd)
    roster_cmd.add_argument(
        "--billing", default=None,
        help=f"只看这一档({'/'.join(CARD_BILLINGS)})",
    )
    roster_cmd.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")

    contact_cmd = sub.add_parser(
        "contact", help="谁想起过玩家、由头是什么(contact;--why 连没触发的一起解释)",
    )
    _add_world_args(contact_cmd)
    contact_cmd.add_argument("--player", default=None, help="只看想起这个玩家的")
    contact_cmd.add_argument("--since-seq", type=int, default=0,
                             help="增量拉取:上次拿到的最后一条 seq")
    contact_cmd.add_argument("--limit", type=int, default=50, help="最多几条")
    contact_cmd.add_argument(
        "--why", action="store_true",
        help="不看已发生的,改问「此刻每个人对每个玩家算出来是多少」—— 调阈值用",
    )
    contact_cmd.add_argument(
        "--inbox", action="store_true",
        help="改看收件箱:谁**当面**叫住过这个玩家(agent_hail),要 --player",
    )
    contact_cmd.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")

    relationship_cmd = sub.add_parser(
        "relationship",
        help="一段关系的人话:她把这个人当什么、粗到什么档、上一次是什么改变了它",
    )
    _add_world_args(relationship_cmd)
    relationship_cmd.add_argument("--agent", default=None, help="谁的视角(不给就是所有人)")
    relationship_cmd.add_argument("--with", dest="other", default=None,
                                  help="跟谁(角色 id 或 player_id;不给就是所有对方)")
    relationship_cmd.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出(契约)"
    )

    player_cmd = sub.add_parser("player", help="玩家在这个世界里的那一份数据的维护")
    player_commands = player_cmd.add_subparsers(dest="player_command", required=True)
    player_forget = player_commands.add_parser(
        "forget",
        help="让世界跟一个走掉的人告别:清掉关系/联系态/姿态,历史一个字不改",
    )
    _add_world_args(player_forget)
    player_forget.add_argument("--player", required=True, help="他的 player_id")
    player_forget.add_argument("--reason", default="", help="为什么(会写进那条事件)")
    player_forget.add_argument(
        "--dry-run", action="store_true", help="只报要清什么,不动世界"
    )
    player_forget.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出"
    )
    player_options = player_commands.add_parser(
        "options",
        help="这个人此时此地点得动什么:有哪些东西、能被怎么做、点不动是为什么",
    )
    _add_world_args(player_options)
    player_options.add_argument("--player", required=True, help="他的 player_id")
    player_options.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出(契约)"
    )
    player_erase = player_commands.add_parser(
        "erase",
        help="法务抹除:删他的转录与记忆、事件里抹名抹原文(用户行使删除权时用;"
             "先 forget 再抹历史)",
    )
    _add_world_args(player_erase)
    player_erase.add_argument("--player", required=True, help="他的 player_id")
    player_erase.add_argument("--reason", default="", help="为什么(会写进审计事件)")
    player_erase.add_argument(
        "--yes", action="store_true",
        help="真抹。不带它只数要动多少(和 world drop 同一个习惯)",
    )
    player_erase.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出"
    )

    drift_cmd = sub.add_parser(
        "drift",
        help="她还是不是她:人设漂移的尺子(纯计数,不调模型;含迎合度这一格)",
    )
    _add_world_args(drift_cmd)
    drift_cmd.add_argument("--agent", required=True, help="看谁")
    drift_cmd.add_argument("--player", default=None,
                           help="只看跟这个人的对话(不同的人会把她带向不同的样子)")
    drift_cmd.add_argument("--baseline", type=int, default=None,
                           help="基线取她最早的几条(默认 6)")
    drift_cmd.add_argument("--json", action="store_true", dest="as_json",
                           help="机器可读输出(契约)")

    engagement_cmd = sub.add_parser(
        "engagement",
        help="他跟这个世界处得有多深:会话/消息/跨几个角色/关系/世界主动想起他几次",
    )
    _add_world_args(engagement_cmd)
    engagement_cmd.add_argument("--player", required=True, help="看谁")
    engagement_cmd.add_argument("--json", action="store_true", dest="as_json",
                                help="机器可读输出(契约)")

    presence_cmd = sub.add_parser(
        "presence",
        help="谁在谁跟前(presence.enforce_colocation 的迁移体检:宿主调过 player_move 吗)",
    )
    _add_world_args(presence_cmd)
    presence_cmd.add_argument("--player-id", default=None, help="只看这一个玩家")
    presence_cmd.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")

    run = sub.add_parser(
        "run",
        help="程序的门:只把时钟跑在前台,Ctrl-C 停 —— 不引导、不问、不改时钟(部署与脚本用)",
    )
    _add_world_args(run)
    run.add_argument("--world-file", dest="world_file", default=None,
                     help="世界文件 .cyberworld(默认内置的 demo.cyberworld)")
    run.add_argument(
        "--beats", default=None,
        help="节拍脚本 JSON(导演节拍);脚本有错当场拒绝启动,不会流进世界",
    )
    run.add_argument(
        "--agents", type=int, default=None,
        help="创世时上场几个人(只对新建的世界生效;默认:世界文件里的整份名册,没有就 3 个)",
    )
    run.add_argument("--quiet", action="store_true", help="不回显叙事事件")
    # -- simulate (novel-benchmark-loop) --
    simulate = sub.add_parser(
        "simulate",
        help="无头快进:不等真实时间、不引导 —— 攒历史 / 跑基准;--ticks 0 = 只创世,当校验器用",
    )
    _add_world_args(simulate)
    simulate.add_argument("--world-file", dest="world_file", default=None,
                          help="世界文件 .cyberworld(默认内置的 demo.cyberworld)")
    simulate.add_argument(
        "--agents", type=int, default=None,
        help="创世时上场几个人(只对新建的世界生效;默认:世界文件里的整份名册,没有就 3 个)",
    )
    window = simulate.add_mutually_exclusive_group(required=True)
    window.add_argument("--days", type=int, help="快进几个世界日")
    window.add_argument("--ticks", type=int, help="快进几个 tick(给 0 就是只创世,不往前走)")
    simulate.add_argument(
        "--llm", choices=("full", "planner", "mock"), default="full",
        help="真调到哪一档:full=叙事和规划都照配置来;planner=规划真调、叙事用模板"
             "(长跑推荐);mock=全都用模板,一次也不联网",
    )
    simulate.add_argument(
        "--no-llm", action="store_true",
        help="等于 --llm mock(两个都给时以它为准)",
    )
    simulate.add_argument(
        "--plan-wait-cap", type=float, default=None,
        help="每个世界日最多等在途的规划几秒,免得快进被网络拖住(默认 planner.timeout 的两倍)",
    )
    simulate.add_argument(
        "--beats", default=None,
        help="节拍脚本 JSON(导演节拍);脚本有错当场拒绝启动,不会流进世界",
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

    memory_cmd = sub.add_parser(
        "memory", help="记忆层的维护(迁移/体检)"
    )
    memory_commands = memory_cmd.add_subparsers(dest="memory_command", required=True)
    memory_repair = memory_commands.add_parser(
        "repair-ticks",
        help="把老世界里盖了墙钟的记忆 tick 折回世界时钟(2.0 之前的 conversation 事件)",
    )
    _add_world_args(memory_repair)
    memory_repair.add_argument(
        "--dry-run", action="store_true", help="只报要改哪些,不动库"
    )
    memory_repair.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出"
    )

    agent_cmd = sub.add_parser("agent", help="角色数据的维护")
    agent_commands = agent_cmd.add_subparsers(dest="agent_command", required=True)
    goals_repair = agent_commands.add_parser(
        "repair-goals",
        help="把被按字拆开的 goals 拼回来(创作台老版本生成的世界)",
    )
    _add_world_args(goals_repair)
    goals_repair.add_argument(
        "--dry-run", action="store_true", help="只报要改什么,不动库"
    )
    goals_repair.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出"
    )
    set_card = agent_commands.add_parser(
        "set-card",
        help="改一个角色的卡:主次 / 一句话 / 立绘(一次一个人;这是覆盖)",
    )
    _add_world_args(set_card)
    set_card.add_argument("--agent", required=True, help="改谁(角色 id)")
    set_card.add_argument(
        "--billing", default=None,
        help=f"主次({'/'.join(CARD_BILLINGS)})",
    )
    set_card.add_argument(
        "--tagline", default=None,
        help="通讯录里名字底下那一行(不许换行;空串 = 抹掉这一格)",
    )
    set_card.add_argument(
        "--portrait", default=None,
        help="立绘 URI(https / http / data;空串 = 抹掉这一格)",
    )
    set_card.add_argument(
        "--clear", action="store_true",
        help="把整张卡删掉 —— 「作者说他是背景」和「作者什么也没说」是两件事,"
             "所以它单独一格,而且不许和上面三个一起给",
    )
    set_card.add_argument(
        "--dry-run", action="store_true", help="只报要改成什么,不动库"
    )
    set_card.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出"
    )

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
    validate_seed.add_argument(
        "--edit", action="store_true",
        help="这份文件要装进一个**已有**的世界(= 一次编辑):不要求它把名册和地图"
             "再抄一遍,引用完整性也不查(它们可以来自目标世界)",
    )
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
    play.add_argument("--name",
                  help="你在这个世界里叫什么;不给就是「还没说过」,她称你「访客」而已")
    play.add_argument("--player-id", default=DEFAULT_PLAYER_ID,
                      help="你的身份 id —— 角色对你的印象记在它头上(chat/play/prompt 共用一个)")
    play.add_argument("--world-file", dest="world_file", default=None,
                      help="世界文件 .cyberworld(只对新建的世界生效)")
    play.add_argument("--beats", help="节拍脚本 JSON(只对新建的世界生效)")
    play.add_argument("--agents", type=int, help="创世时上场几个人(只对新建的世界生效)")
    play.add_argument("--verbose", action="store_true",
                      help="引擎的日志照原样打出来(默认收着,散场报一行)")

    contract = sub.add_parser(
        "contract", help="引擎自报它的线格式版本与 schema 形状 —— 给持有镜像的仓库对齐用"
    )
    contract.add_argument("--json", action="store_true", help="机器可读输出")

    world = sub.add_parser(
        "world", help="世界的进出:导出 / 装回 / 看一眼 / 从 1.x 迁过来 / 整个抹掉",
    )
    world_commands = world.add_subparsers(dest="world_command", required=True)
    world_export = world_commands.add_parser(
        "export", help="把这个世界导出成一个 .cyberworld 文件 —— 可以发给别人",
    )
    _add_world_args(world_export)
    world_export.add_argument("--beats", default=None,
                              help="可选:把配套的节拍脚本一起打进包里")
    world_export.add_argument("--output", required=True, help="写到哪个 .cyberworld 文件")
    world_export.add_argument("--package-id", required=True,
                              help="包的世系 id(小写;--world-id 是源世界在 Redis 上的名字)")
    world_export.add_argument("--name", "--title", required=True,
                              help="世界的展示名 —— 别人看到的那个名字")
    world_export.add_argument("--summary", default="", help="一句话简介(写进清单,inspect 看得到)")
    world_export.add_argument("--genre", default="", help="题材,给挑世界的人看")
    world_export.add_argument("--setting", default="", help="背景设定,给挑世界的人看")
    world_export.add_argument("--theme", default="default", help="展示主题,交给宿主界面用")
    world_inspect = world_commands.add_parser(
        "inspect", help="读一个 .cyberworld 需要什么引擎 —— 跑不了也照样回答"
    )
    world_inspect.add_argument("package", help="要看的 .cyberworld 文件")
    world_inspect.add_argument(
        "--json", action="store_true", dest="as_json",
        help="输出一行 JSON(给启动器/工具消费),而不是给人看的清单",
    )
    world_check = world_commands.add_parser(
        "check",
        help="这一版引擎装得进这份 .cyberworld 吗 —— 真跑校验器,不读封皮,不建世界",
    )
    world_check.add_argument("package", help="要查的 .cyberworld 文件")
    world_check.add_argument(
        "--edit", action="store_true",
        help="这份文件要装进一个**已有**的世界(= 一次编辑):不要求它把名册和地图"
             "再抄一遍,引用完整性也不查(它们可以来自目标世界)",
    )
    world_check.add_argument(
        "--json", action="store_true", dest="as_json",
        help="输出一行 JSON(给启动器/运维台消费),而不是给人看的清单",
    )
    world_import = world_commands.add_parser(
        "import", help="把一个 .cyberworld 装进 --world-id 那个世界(目标必须是空的)",
    )
    world_import.add_argument("package", help="要装的 .cyberworld 文件")
    _add_world_args(world_import)

    world_migrate = world_commands.add_parser(
        "migrate",
        help="把一个 1.x 的 world.db 迁成 2.0 的世界文件 —— 一次性的桥",
    )
    world_migrate.add_argument("db", help="1.x 的 world.db 路径")
    world_migrate.add_argument("--output", required=True, help="写出的 .cyberworld")
    world_migrate.add_argument("--package-id", required=True, help="迁过去之后世界叫什么")
    world_migrate.add_argument("--name", default="", help="展示名")
    world_migrate.add_argument("--summary", default="", help="一句话简介(写进新包的清单)")
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


def _authored_only(records, path):
    """滤掉状态记录,只放作者记录过去;跳过了多少条要说出来。

    **报数而不是只报"跳过了"**:一份 12000 条事件的文件被静默跳过,和一份空文件
    在日志里长得一模一样。
    """
    skipped = 0

    def gen():
        nonlocal skipped
        for record in records:
            if record.get("kind") == "author":
                yield record
            else:
                skipped += 1
        if skipped:
            logger.info(
                "%s 里有 %d 条状态记录没有装:这个世界已经存在了,状态层只在创世那一次装。"
                "要还原一个世界用 `anima-world world import`(它要求目标是空的)",
                path, skipped,
            )

    return gen()


def authored_layer_errors(
    authored: dict[str, Any] | None, *, complete: bool = True
) -> list[str]:
    """一份作者层装不装得进世界 —— **开机与 `validate world` 共用的那一份判断**。

    三道闸,一次列全:

    - `world_seed_errors` —— 必填键与引擎读得懂的形状(写错的 `stocks` 会被装载器
      整条丢掉,那是"安静地少装一半世界")。
    - `visibility_band_errors` —— 分档声明。放行的样子是这个仓库最怕的那种:世界
      照跑、日志干净,而她一直在报数字,作者要到三个月后才发现写错了一个方括号。
    - `world_card_errors` —— 角色卡。它多一条理由:卡是**分发物里给玩家看的那一面**,
      相对路径的立绘发出去就是一张断的图,而作者自己看不见。
    - `world_location_media_errors` —— 地点的两格图。和上一条同一道闸、
      同一个理由,只是上限的数按各自的读出口定(`media.py` 的模块 docstring)。

    两条纪律都在这个函数的**存在**上,不在它的实现里:

    **① 只有带作者层的文件才查。** 一个跑过的世界导出来只有状态记录,拿
    "agents 必须是个列表"去要求它是在**问错问题** —— 开机那条路一直是这么判的
    (`authored or None`),而 `validate world` 从前不是:它对每一份 `.cyberworld`
    都要求一份完整的名册和地图,于是**任何一份导出的世界在它嘴里都是非法的**
    (`'agents' must be a list (missing)`,退出码 2)。同一份文件,开机说好、
    校验器说坏。

    **② `complete=False` 是"这是一次编辑,不是一个世界"。** 把一份只含 `author`
    记录的文件装进一个**已有**的世界就是一次编辑,那时要求它把名册和地图再抄一遍
    等于逼作者维护一份迟早会不一致的抄件。开机按目标世界空不空自己判;校验器手上
    没有目标世界,所以那一格由调用方说(`validate world --edit`)。

    ⚠️ **这两条从前只写在开机那一侧**,于是"创作台经 CLI 委托校验"这句话在最要紧
    的两种文件上不成立 —— 而 2026-08-19 舰队上那次开不了机(`stocks[8] 'values'
    must be an object`,一份 2.3.0 时代导入的世界撞上收紧了的作者层)正是运维台
    离线问不出答案的那一种:`world inspect` 只读封皮,答 `runnable: true`;
    `validate world` 答得出,但它对同一份文件的答案和开机不是同一个。
    **判断只有一份**,是这条修法的全部内容。
    """
    if not authored:
        return []
    return (
        _world_seed_errors(authored, complete=complete)
        + visibility_band_errors(authored)
        + world_card_errors(authored)
        + world_location_media_errors(authored)
    )


def _authored_media_warnings(authored: dict[str, Any] | None) -> list[str]:
    """拼错 / 过时的图字段,一行一条。**开机、`validate world`、`world check` 共用。**

    `world_card_warnings` 管卡上那些引擎不认识的键(`taglien`),
    `world_location_media_warnings` 管地点上那个我自己公布过又改掉的 `image` ——
    两条都是"写了但什么也不会发生",而这类问题只有作者能改,所以三条路都得说。
    """
    if not authored:
        return []
    return world_card_warnings(authored) + world_location_media_warnings(authored)


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
        # ⚠️ **状态记录只装一次,而"开机"会发生很多次。**
        #
        # 一份带状态层的世界文件(跑过的世界导出来的、或者从 1.x 迁过来的)装进一个
        # **已经不空**的世界,没有一种正确答案:合并会重号,覆盖会抹掉这期间发生的
        # 一切。此前是当场抛错 —— 而托管环境里 entrypoint 每次启动都指着同一份文件,
        # 于是**第一次开得起来、第二次再也起不来**,而错误信息说的是"seq 对不上",
        # 没人会想到它在讲"这个世界已经装过了"。
        #
        # 作者层不同:它本来就是"只填缺,不覆盖",装进已有世界 = 一次编辑。
        # 所以这里只滤掉状态记录,并且**说一句**——安静地跳过和安静地装错一样坏。
        if _world_exists(redis, world_id):
            records = _authored_only(records, path)
        authored = install_world_records(
            records, redis=redis, world_id=world_id, mysql=mysql
        )
        # **作者层还是要过那道闸。** 换掉的是容器,不是校验:一份写错了 `agents`
        # 的世界文件照旧不许开机。三道闸合在**一个函数**里
        # (`authored_layer_errors`),`validate world` 调的就是它 —— 理由写在那个
        # 函数上,一句话是:两条路对同一份文件必须给同一个答案。
        errors = authored_layer_errors(
            authored, complete=not _world_exists(redis, world_id)
        )
        if errors:
            raise WorldSeedError([f"{path}: {e}" for e in errors])
        # 拼错的键只警告(`taglien` 什么也不做,但拦下来会让新版创作台配不了老引擎)。
        # **开机也要说** —— 这条警告此前只有 `validate world` 才看得到,而托管环境里
        # 没有人会去跑那条命令。
        for problem in (_authored_media_warnings(authored) if authored else []):
            logger.warning("%s: %s", path, problem)
        # **一个作者层为空的文件 = 没有种子,不是一个空种子。**
        # 跑过的世界导出来、或者从 1.x 迁过来的,里面一条 author 记录都没有 ——
        # 交一个 `{}` 下去的话,下游会拿它当"作者写了一个没有地点的世界"去索引
        # `world_seed["locations"]` 然后 KeyError。而 `None` 是它一直认得的
        # "这次开机没有种子"。
        return authored or None
    except Exception as exc:  # noqa: BLE001 — 坏文件的形状很多,分流只看是不是作者指名的
        if authored:
            if isinstance(exc, (WorldFileError, WorldSeedError)):
                raise
            raise WorldFileError([f"装不进这个世界文件 {path}:{exc}"]) from exc
        logger.warning(
            "内置世界文件读不了(%s);回落硬编码默认值 —— 一个装坏了的包也得能开机", exc
        )
        return {}


#: 作者写的一个地点条目里,引擎收得下的那些键。**这是一路上的第一道筛子** ——
#: 一格图从文件走到玩家眼前,每一道都得放它过:这里 →
#: `world_store._LOCATION_FIELDS` → `RedisLocationStore.upsert` 的缺省行 →
#: `api._LOCATION_KEYS` → `map_data()`。
#: 漏掉任何一处的下场都一样:作者写了、引擎收了、玩家看不见,而且零报错。
_LOCATION_ENTRY_FIELDS = (
    "id", "name", "description", "kind", "parent", "x", "y", "w", "h",
    *LOCATION_IMAGE_KEYS,
)


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
        if loc_id:
            ctx["location"] = scheduler.place_name(loc_id)

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
            where = other.agent.blackboard.read("loc") or other.agent.location or ""
            # 上面那行名字翻了、这行地点没翻,于是提示词里是「林迟在studio上班」——
            # 半句人话半句键名。她照着这句话排一天的事,而 `walk` 的清单本来就是
            # id,两种写法混在一份提示词里只会让她更分不清哪个是地名。
            label = _ACTION_LABELS.get(action.kind if action else None, "闲着")
            others.append(f"{other.agent.name}在{scheduler.place_name(where) or '?'}{label}")
        if others:
            ctx["others"] = others
    return ctx


def _warn_if_llm_degraded(world: Any) -> None:
    """开机说一句:这个世界会跑在 Mock 上。**只有 `run` 需要它。**

    一个开得起来、跑得动、而说出来的全是模板的世界,正是 `onboarding.probe_llm`
    替 `simulate` 挡掉的那种失败。`run` 借不到那道闸(一个开不了机的世界比一个
    降级的更糟),所以它改成说一声。

    从前这句话挂在 `build_serve_scheduler` 里 —— 于是**每一条**开世界的路都要
    听一遍,包括 `start` / `chat` / `play` 这三个自己已经用中文把同一件事说得更
    清楚的。实测的样子是引导流程里横插进来一句英文,而它下面两行就是同一句话的
    中文版。挂在这里,是因为"没有别的办法说"正是它存在的全部理由。
    """
    degraded = (world.state().get("runtime", {}) or {}).get("llm", {}).get("degraded_reason")
    if not degraded:
        return
    print(f"[run] 这个世界跑在 Mock 上({degraded})——叙事、空闲规划、关系判定"
          f"都会退成模板,世界照跑。配一个:anima-world config set llm.api_key sk-…",
          file=sys.stderr)


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
            if f"chat_with_{aid}" not in bt_store.shared_action_ids():
                bt_store.set_action(f"chat_with_{aid}", "chat", {"target": aid})
            _ensure_need_actions(bt_store)
            bt_root = bt_store.build_tree(aid)
            action_table = bt_store.action_table(aid)
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


def _genesis_tick(config_store: Any) -> int:
    """创世那一刻钟面上是几点 —— `world.start_time`(HH:MM)换算成 tick 数。

    这一层存在的理由是开箱那一分钟。tick 0 是午夜,而新世界跑在演示速度上,
    于是"装上包看到的第一屏"是三个人在各自家里睡觉 —— 一个世界引擎最没有说服力
    的一帧。而**几点开门是这个世界作者的意见**(它和作息表是同一件事),所以修法
    是把它交出去、引擎的默认值仍旧是午夜,不是把午夜改成九点。

    两个值都从配置里读:一天有多少 tick 取决于这个世界的 `world.minutes_per_tick`,
    把 288 抄进换算就是把一个配置值写死回引擎里。时刻解析走 `world_time.parse_hhmm`
    ——世界日历只能有一个来源,另写一份 "HH:MM" 解析迟早和它给出不同答案。

    写错了当场抛:这里的降级只有一种样子(悄悄退回午夜),而那正是作者永远不会
    发现的那种坏 —— 他只会觉得"这个世界怎么老是从半夜开始"。
    """
    from anima_world.world_time import DEFAULT_MINUTES_PER_TICK, parse_hhmm

    raw = str(config_store.get("world.start_time") or "00:00")
    try:
        minute_of_day = parse_hhmm(raw)
    except ValueError as exc:
        raise ValueError(
            f"world.start_time 不是一个 HH:MM 时刻:{raw!r}(世界的起始时刻)"
        ) from exc
    minutes_per_tick = max(
        1, int(config_store.get("world.minutes_per_tick") or DEFAULT_MINUTES_PER_TICK)
    )
    return minute_of_day // minutes_per_tick


def build_serve_scheduler(
    world_id: str,
    redis: Any,
    mysql: Any = None,
    n_agents: int | None = None,
    world_file: str | Path | None = None,
    force_mock_llm: bool = False,
    mock_narrative: bool = False,
    beats_path: str | Path | None = None,
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
        RedisBTStore, RedisChatStore, RedisClock, RedisConfigBackend, RedisContactStore,
        RedisDict,
        RedisEconomyStore, RedisEventLog, RedisKnowledgeGraph, RedisLocationStore,
        RedisMemoryStore, RedisNeedsStore, RedisCliqueStore, RedisPromptStore,
        RedisOntologyStore, RedisReflectionStore, RedisRulesStore, RedisStockStore,
        RedisVisibilityStore,
        clock_key, current_action_key, decode_action, decode_plan, encode_action,
        encode_plan, events_key,
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
        # 一次编辑可以只带 kinds、不带名册 —— 那时人数以世界自己的为准。
        n_agents = len(world_seed.get("agents") or CHARACTER_ROSTER) if world_seed else len(CHARACTER_ROSTER)

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
    # **作者层什么时候生效:空世界,或者作者指名了一份文件。**
    #
    # 这两件事此前合成了一个判断("世界空不空"),而它们性质完全不同:
    #
    #   内置的兜底那份(没给 `--world-file`)—— **只准进空世界**。它每次开机都在手上,
    #     拿它去填一个已有世界的空表,就是把橱窗的橡树塞进别人的世界:世界照跑、
    #     日志干净,只是它凭空多了一棵别人的树。这个 bug 今天刚修过。
    #   作者指名的那份 —— 是一次**明示的编辑**。作者层本来就是给作者调试用的,
    #     "改不了一个活着的世界"说不通:他想给世界补一层 `kinds`、改一条规律,
    #     就该改得动。
    #
    # 引擎里本来就有这个区分(`_load_world_file` 的 `authored=`),只是没用在播种上。
    # 语义仍然是**只填缺,不覆盖**(每个 seed 函数自己守着空表那条)——
    # 编辑加得进新东西,但不会把这个世界跑出来的现在倒带回创世那一刻。
    authored_file = world_file is not None
    seed_author_layer = fresh_world or authored_file
    # **合并 = 把作者指名的那份文件装进一个已经跑过的世界。** 创世那条路上每张表
    # 本来就是空的,`merge` 传不传逐位等价;分开写是为了让"降粒度"这件事只发生在
    # 它该发生的那一条路上 —— 内置兜底那份永远走不到这里(`authored_file` 是 False),
    # 于是橱窗的橡树没有任何一条缝可以钻进别人的世界。
    merge_author = authored_file and not fresh_world

    # ⚠️ **先验,再写 —— 因为这些表不是一起写的。**
    #
    # 从前的顺序是"播地图 → 播规律 → 播可见性 → 编译本体",而**会失败的是最后那一步**。
    # 于是一份写错了 `kinds` 的文件会留下一个装了一半的世界:地图和规律都进去了,
    # `kinds` 是空的,而开机是失败的 —— 作者改好文件再来一次,那条半截的规律还在
    # 那儿,来路不明。更坏的是那个失败让这个前缀**不再是空的**,于是重试走的已经
    # 不是创世那条路了。
    #
    # 修法不是包一层事务(这些表在 Redis 上没有共同的事务),是**把判断提前到第一次
    # 写之前**:本体声明本来就验得动,它依赖的规律/地点/物品三样,库里那份读得到、
    # 文件里那份就在手上。验不过就一个字都不写 —— 和能力调用被拒时"一个字都不写"
    # 是同一条。
    if seed_author_layer and world_seed and world_seed.get("kinds"):
        _precheck_ontology(world_seed, rules_store, location_store, economy_store,
                           ontology_store if merge_author else None)
    if fresh_world:
        _store_genesis_seed(meta, world_seed)  # 出生证明随世界走
        # 种子自己带的开关 —— 现在它是 `:config` 里唯一的来源:
        # 剩下的行就是"这个世界的作者决定了什么"。
        #
        # ⚠️ **这一条钉在 `fresh_world` 上,不跟着 `merge_author` 走。** `:config`
        # 里的行是运维台/作者后来调过的运行参数(tick_rate、enforce_colocation),
        # 拿文件里那份写回去等于每次重启都把人的调整悄悄撤销一次。
        _apply_seed_config_at_genesis(config_store, world_seed)
    new_points = _seed_world_defs(location_store, bt_store, world_seed, merge=merge_author)
    # economy-v4: default items + cafe shelf, empty-store-only (authored
    # rows always win). #12: the seed's own material layer goes in FIRST,
    # precisely so the demo items find a non-empty store and stand down.
    with shared_lock:
        _seed_material_layer(economy_store, world_seed, merge=merge_author)
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
    # 降级不许无声(三轴):真模型经常只吐 headline,判定器会照 headline 自己派生
    # 一份 trust/affection/respect —— 那是回落不是判断,所以要和 planner /
    # narrative 一样在 `World.state()["runtime"]["subsystems"]` 里看得见。
    # 判定器建在 scheduler 之前(它不认识存储层),所以这一笔在这儿接。
    if getattr(relationship_judge, "health", "missing") is None:
        relationship_judge.health = scheduler.note_subsystem
    # 时钟第一个接上:后面的 load_persisted_events 会拿事件 ts 和它取 max。
    # setnx 只填缺 —— 重开一个世界不许把时钟拨回去。
    #
    # 起始时刻**只在这个前缀还没有钟的时候**算一次。setnx 才是那道真闸(算出来的
    # 初值本来就进不去一个已有的世界),这里先问一句是为了另一半:一个跑了三天的
    # 世界不该因为 `world.start_time` 写错了一个字而开不了机 —— 那个值对它已经
    # 没有任何意义了,而在创世那一刻它是承重的,所以严格只留在那一刻。
    initial_tick = 0 if redis.exists(clock_key(world_id)) else _genesis_tick(config_store)
    scheduler._clock_store = RedisClock(redis, clock_key(world_id), initial=initial_tick)
    # 在途 / 当前动作 / 规划:真状态,全进程可见。
    scheduler._transit = RedisDict(redis, transit_key(world_id))
    scheduler._current_action = RedisDict(
        redis, current_action_key(world_id), encode=encode_action, decode=decode_action,
    )
    scheduler._plans = RedisDict(
        redis, plans_key(world_id), encode=encode_plan, decode=decode_plan,
    )
    # 在做的长过程:椅子做到一半、孩子怀了六个月。**真状态,不是缓存** ——
    # 内存态等于每次重启都流产一次。
    scheduler._engaged = RedisDict(redis, engagements_key(world_id))
    # 需求 / 小团体 / 反思水位 / 经济。
    scheduler.needs_store = RedisNeedsStore(redis, world_id)
    scheduler.clique_store = RedisCliqueStore(redis, world_id)
    scheduler.reflection_store = RedisReflectionStore(redis, world_id)
    scheduler.economy_store = economy_store
    # 她想起某个玩家这件事的冷却与水位(contact)。**和 `_engaged` 同一个理由落库**:
    # 内存态的冷却等于每次换镜像重启都把所有人的冷却清零,而玩家看到的是"一发版
    # 就四个人同时来找我"。
    scheduler.contact_store = RedisContactStore(redis, world_id)
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
        #
        if seed_author_layer:
            _seed_world_rules(rules_store, world_seed, merge=merge_author)
            _seed_stock_visibility(world_seed, visibility_store, merge=merge_author)
            _seed_stock_places(world_seed, visibility_store, merge=merge_author)
        # 返回的是作者这次真的改动过的种类 —— 可见性那张表是声明的镜像,得跟着改。
        redeclared_kinds = _seed_ontology(
            ontology_store, world_seed, fresh_world=seed_author_layer, merge=merge_author)
        # `warn=True` 全仓库只有这一处:装载走一次,拿到的又是**这个世界真正在跑
        # 的那份**规律(播种那份可能根本没落库,预检那份还没落库)。
        scheduler.world_rules = _load_world_rules(
            rules_store, warn=True,
            ticks_per_day=max(1, 1440 // max(1, scheduler._minutes_per_tick())))
        # 本体的解析在规律之后:它要拿规律去查引用。声明过种类的世界从此走闸,
        # 没声明的照旧走警告 —— 那条警告只对后者还有意义。
        scheduler.ontology_store = ontology_store
        scheduler.ontology = _load_ontology(
            ontology_store, scheduler.world_rules, location_store, economy_store
        )
        _warn_unreachable_requirements(scheduler.ontology, scheduler.world_rules)
        # 种子的显式值先落(它此刻仍是空库,"空库一次"那条守得住),声明的默认值
        # 后面**只填缺** —— 反过来的话作者写的 3.2 会被声明的 1.0 盖掉。
        # 而它现在拿得到本体了,于是"写了一个没声明过的量"这件事走得了闸。
        #
        # ⚠️ **同样要 `fresh_world`,这是上面那三个的第四个** —— 漏了它的下场是
        # 迁移时撞出来的:一个 1.x 迁过来的世界(`stocks` 表本来就是空的)开机一次,
        # 就被塞进橱窗那棵 `tree:harbor_oak` 和两个世界量。世界照跑、日志干净,
        # 只是它凭空多了一棵别人世界里的橡树。
        if seed_author_layer:
            _seed_stocks(world_seed, stock_store, ontology=scheduler.ontology,
                         tick=scheduler.clock, merge=merge_author)
        if scheduler.ontology is None:
            _warn_unresolved_rule_names(stock_store, scheduler.world_rules)
        else:
            _apply_ontology(scheduler.ontology, stock_store, visibility_store,
                            tick=scheduler.clock, redeclare_kinds=redeclared_kinds)
    # D3 restart-reversion fix: Scheduler.__init__ already replayed whatever
    # is persisted into scheduler._memory_projection (empty on a fresh DB) —
    # reuse it here for persona resolution BEFORE constructing agents,
    # instead of folding the same event list into a second Projection.
    persisted: list[Event] = event_log.replay() if event_log is not None else []
    if world_file is not None and persisted:
        # **这是一次编辑,不是一次创世** —— 说清楚它能做什么、做不了什么。
        #
        # 从前这里是一句"你的编辑没有生效"的警告,因为作者层只进空世界。现在它进得去了
        # (作者层本来就是给作者调试用的),而分界**不是**"新的还是旧的",是
        # **声明还是状态**:
        #
        #   - 声明(`kinds` / `rules`)身上没有任何东西会随时间漂,所以文件里那条赢。
        #     照搬"不覆盖"的代价是作者永远改不了自己世界的物理法则。
        #   - 状态(名册、量、钱、位置、记忆)只填缺 —— 整份写回等于拿创世快照倒带她。
        #
        # 这两句话必须一起说。只说"生效了",作者会以为文件能把这个世界改回创世那天;
        # 只说"不覆盖",他又会以为改一条写错的规律得把世界抹掉重建(而那要连玩家
        # 的进度一起抹)。
        logger.info(
            "--world-file %s 装进了一个**已有**的世界 %r(%d 条事件)—— 这是一次编辑:"
            "同名的**声明**(kinds / rules)照文件里这份重写,"
            "而**状态**(名册、量、钱、位置、记忆)只填缺不覆盖 —— "
            "这个世界跑出来的现在不会被倒带回创世那一刻。",
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
    # **一次编辑加进来的人。** 名册的权威是事件日志(所以上面那一支不看文件),
    # 而作者指名一份文件恰恰是在说"这个世界从今天起还有这些人"。少了这一段,
    # 新写的十六个角色只是文件里的十六段文字:地图上没有、提示词里没有、
    # 谁也遇不到 —— 而开机日志一行不错。
    newcomers: list[dict[str, str]] = []
    joined: set[str] = set()
    if merge_author and persisted and world_seed:
        known = {e["id"] for e in roster} | set(boot_projection.agents) | away
        for entry in _seed_entry_dicts(world_seed, "agents"):
            aid = entry.get("id")
            if not isinstance(aid, str) or aid in known:
                continue
            known.add(aid)
            newcomers.append({
                "id": aid,
                "name": str(entry.get("name", aid)),
                "location": str(entry.get("location") or ""),
                "personality": str(entry.get("personality", "")),
            })
        roster = roster + newcomers
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
        action_table = (
            bt_store.action_table(entry["id"]) if bt_store is not None else ActionTable.default()
        )
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
        elif newcomers or new_points:
            joined = _join_authored_additions(
                event_log, scheduler, world_seed, location_store, newcomers, new_points,
            )
            persisted = event_log.replay()
            scheduler.reset_projection(persisted)   # 同一条:水位跟着挪
        scheduler.load_persisted_events(persisted)
        if memory_store is not None and trigger_engine is not None:
            # ⚠️ 顺序要紧:`rebuild` 见了非空表就掉头(记忆是持久状态,重放一遍等于
            # 把她的一生按今天的触发器重新裁一遍)。所以合并进来的新人的创世记忆
            # 得**在这之前**自己折一次,而且只在表已经非空时折 —— 表是空的时候
            # `rebuild` 会连他们一起折,两边都折就是每人两份。
            if joined and memory_store.count() > 0:
                _fold_seeded_memories(memory_store, persisted, joined)
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
                 *, ontology: Any = None, tick: int = 0, merge: bool = False) -> None:
    """种子里的初始存量(`"stocks": [{"owner": …, "values": {…}}]`)。空库一次。

    **读不懂的条目当场开不了机,不丢弃。** 这里从前是一条 `logger.warning` 加
    `continue`,理由记的是"和其它可选字段同一条宽容原则" —— 那条类比是错的。
    宽容原则说的是"这个引擎版本还不认识这个键"(种子会比引擎活得久,少点亮一项
    好过整个世界打不开);而形状读不懂说的是"你写的这一行没有人读",在任何引擎
    版本上都一样。灯塔湾就是这么丢的:11 行逐条写成 `{"owner","key","value"}`,
    世界照常开机、日志干净,而五个角色的 `initiative` / `agreeableness` 全停在
    声明的默认值上 —— 引用它们的条件照跑,算出来的全是同一个数。

    **声明过种类的世界例外:量名走闸,拼错当场开不了机。** 这一条和
    `_load_ontology` 的"声明本身就是开关"逐字同构 —— 一个不写 `kinds` 的世界里,
    owner 和量名都是任意字符串,引擎无从判断 `树髙` 是笔误还是作者新造的量;
    一旦他写下 `kinds`,他就是在说"我已经声明了这个世界有什么"。

    不吼一声而放行的样子是这个仓库最怕的那种:`树髙` 安静地建成第二个量,
    `树高` 停在声明的默认值上,规律照跑、日志干净,而作者要到某天发现那棵树
    三个月没长过才知道。

    ⚠️ **`merge=True` 的粒度是每个 (owner, 量名),不是每个 owner。** 这一层最容易
    分错:世界量全挂在同一个 owner(`world`)下,按 owner 判的话,一个已经有
    `雨势` 的世界永远补不进 `气温` —— 而它不报错,只是那个量从此恒为 0,引用它的
    规律照跑、算出来的全是同一个数。判据仍是**只增不改**:量的当前值是这个世界
    跑出来的现在,拿今天文件里的初值写回去就是把江水位倒带回创世那一寸。
    """
    if world_seed is None:
        return
    have_owners = set(store.owners())
    if have_owners and not merge:
        return
    problems: list[str] = []
    for index, entry in enumerate(_seed_entry_dicts(world_seed, "stocks")):
        owner = str(entry.get("owner") or "").strip()
        values = entry.get("values")
        if not owner or not isinstance(values, dict):
            problems.append(
                f"stocks[{index}] 读不懂:要 {{\"owner\": …, \"values\": {{量名: 数}}}},"
                f"收到的键是 {sorted(entry)} —— 逐条的 key/value 写法引擎不认,"
                "整条会读不出任何一个量"
            )
            continue
        declared = ontology.declared_quantities(owner) if ontology is not None else None
        # 拼错的量名照样当场开不了机(闸在跳过之前),但**已经有值的量一个都不动**。
        existing = set(store.of(owner)) if merge else set()
        clean: dict[str, float] = {}
        for key, raw in values.items():
            name = str(key)
            if declared and name not in declared:
                problems.append(
                    f"stocks[{index}] 给 {owner} 写了「{name}」,而它所属的种类没有"
                    f"声明过这个量;声明过的是 {sorted(declared)}"
                )
                continue
            if name in existing:
                continue
            try:
                clean[name] = float(raw)
            except (TypeError, ValueError):
                # 同上:跳过一项 = 这个量停在声明的默认值上,而作者以为他给过初值了。
                problems.append(
                    f"stocks[{index}] 给 {owner} 的「{name}」写了 {raw!r},不是一个数"
                )
        if clean:
            # 创世那一刻,不是 tick 0 —— 一个从下午两点半开门的世界里,写死的 0
            # 会让每个量在第一帧就补上一整个上午的变化(理由同 `_apply_ontology`)。
            store.set_many(owner, clean, tick=int(tick))
    if problems:
        from anima_world.ontology import OntologyError

        raise OntologyError(problems)


def _seed_world_rules(rules_store: Any, world_seed: dict[str, Any] | None,
                      *, merge: bool = False) -> None:
    """种子里的规律 → `:world_rules`。空的一次,之后那里的行说了算。

    **坏规律整体拒绝**(`RuleError`),不逐条丢弃:规律是这个世界的物理法则,
    少一条不是"少一点内容",是这个世界从此算错。宁可开不了机。

    `merge=True` 把粒度降到**每条规律的 id**(作者指名了世界文件那条路),而且
    **同名的照文件里那条重写** —— 规律是法不是状态,改它不会倒带任何人的现在,而
    从前那条"同名的不动"意味着一条写漂了的规律在跑着的世界里永远修不掉。合并之后的
    **全集**在 `_load_world_rules` 那一遍再解析一次 —— 所以"新规律引用了库里那条
    规律"这种跨来源的引用查得到。
    """
    if world_seed is None:
        return
    if len(rules_store) and not merge:
        return
    entries = world_seed.get("rules")
    if not entries:
        return
    parse_rules(entries)   # 校验在这里,坏了当场抛
    rules_store.seed(entries, datetime.now(timezone.utc).isoformat(), merge=merge)


def _load_world_rules(rules_store: Any, *, warn: bool = False,
                      ticks_per_day: int | None = None) -> list[Any]:
    """从 store 读出规律并编译。被人手改坏了也当场报错,不带着坏规律开机。

    **常数步长那条 lint 由调用方开口(`warn=True`),而全仓库只有一处开。**
    一次开机要解析好几遍规律(播种 / 本体预检 / 装载),在 `parse_rules` 里打
    就是同一句话说好几遍 —— 而说好几遍的警告和没说过一样,人会开始略过它。
    默认关的理由是**将来新加的调用点默认闭嘴**:开口要显式写出来,才看得见。

    ⚠️ 这里原本无条件打,并且注释里写着"每次开机恰好走一次"。那句是假的 ——
    `_precheck_ontology` 也走这个函数,于是线上那次重启把同一条警告说了两遍。
    单测没逮到,是因为它直接调这个函数、只调一次:**测的是函数,不是那条真路**。
    """
    definitions = rules_store.definitions()
    if not definitions:
        return []
    # `every: {"days": 1}` 折成多少 tick 是**这个世界自己的配置** —— 写死 288 的话,
    # 一个把 `minutes_per_tick` 调成 10 的世界里它一天跑两遍,而作者按"一天一次"
    # 写的常数因此翻倍:世界照跑、日志干净,只有数是错的。
    rules = parse_rules(definitions, **({} if ticks_per_day is None
                                        else {"ticks_per_day": ticks_per_day}))
    if warn:
        for warning in drift_warnings(rules):
            logger.warning("%s", warning)
    return rules


def _authored_drift_warnings(authored: Any) -> list[str]:
    """`validate world` 也要报常数步长那条 lint —— **作者会看的是这里**。

    开机那条警告落在服务器日志里,而作者手上只有这个命令。两处共用
    `rules.drift_warnings` 同一个函数:另写一份判断,迟早出现"validate 说没问题、
    开机却在报"。规律本身写坏了不归这儿管(`world_seed_errors` 已经列过),
    所以解析不动就安静退场,不重复报一遍错。
    """
    entries = (authored or {}).get("rules") if isinstance(authored, dict) else None
    if not entries:
        return []
    try:
        return list(drift_warnings(parse_rules(entries)))
    except Exception:  # noqa: BLE001 - 坏规律的错由 world_seed_errors 负责报
        return []


def _warn_unreachable_requirements(ontology: Any, rules: list[Any]) -> None:
    """开机点名「永远开不了的那道门」。**恰好一处开口**,和常数步长那条同一个安排。

    这一处走的是装载那条路(`build_serve_scheduler`,每次开机一次),不是
    `_precheck_ontology` —— 上一次就是因为预检和装载共用一个函数,同一句警告在
    线上说了两遍。作者手上那一份归 `validate world`。
    """
    if ontology is None:
        return
    from anima_world.ontology import unreachable_requirements

    for warning in unreachable_requirements(ontology.kinds, rules):
        logger.warning("%s", warning)


def _authored_unreachable_requirements(authored: Any) -> list[str]:
    """`validate world` 那一份。开机那条落在服务器日志里,而作者手上只有这个命令。

    和开机共用 `ontology.unreachable_requirements` 同一个函数 —— 另写一份判断
    迟早出现"validate 说没问题、开机却在报"。声明本身写坏了不归这儿管
    (`world_seed_errors` 已经列过),所以解析不动就安静退场。
    """
    if not isinstance(authored, dict) or not authored.get("kinds"):
        return []
    try:
        rules = parse_rules(authored.get("rules") or [])
    except Exception:  # noqa: BLE001 - 坏规律的错由 world_seed_errors 负责报
        rules = []
    try:
        from anima_world.ontology import parse_kinds, unreachable_requirements as _check

        return list(_check(parse_kinds(authored["kinds"]), rules))
    except Exception:  # noqa: BLE001 - 坏声明的错同上
        return []


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
    ontology_store: Any, world_seed: dict[str, Any] | None, *, fresh_world: bool,
    merge: bool = False
) -> list[str]:
    """种子里的本体(`"kinds"` / `"entities"`)→ `:kinds` / `:entities`。**只在创世。**

    别的种子段落用的是"表空了就播",这一段不能 —— 一个创世时没有本体的世界,它的
    本体表**永远是空的**,于是下次用默认种子重开就会被硬塞进一个 `tree` 种类,
    连带它的规律闸,而那些东西和这个世界毫无关系(轻则多一棵树,重则开不了机)。
    本体是"这个世界有什么"的定义:它和世界同生,不能事后嫁接。

    **坏声明整体拒绝**(`OntologyError`),和规律同一条理由:少一条不是"少一点
    内容",是这个世界从此有一部分东西静默地不存在。

    `merge=True`(作者指名了 `--world-file`)是上一段唯一的例外:那不是"下次重开
    被硬塞",是作者的一次明示编辑。校验喂的是**合并后的全集**而不是文件里那一份 ——
    一个新实例挂在库里已有的种类上是完全正常的写法,只看文件会把它判成"引用了不存在
    的种类"。

    ⚠️ **种类与实例在这里的规矩不同,而这个不对称是有理由的。**

    从前两边都是"同名的一个字都不动",于是**一个跑着的世界里的声明永远改不了** ——
    作者写错一个 `label`、给的代价调得不对、一条规律漂了,唯一的修法是把世界抹掉
    重建,而那要连同每个玩家的进度一起抹。灯塔湾就卡在这儿:一个教英语的世界,
    每个动词按钮上印的是引擎的中文默认词(`look` → 「端详」),四个真人在里面。

    只填缺不覆盖那条纪律**说的是状态**:整份写回会把长了三十天的树倒带回幼苗。
    而**种类是法,不是状态** —— 它身上没有任何东西会随时间漂,所以那条理由在这儿
    不成立。于是:

    - **`kinds`:文件里那条赢。** 作者指名了这份文件,就是在说"照它来"。
    - **`entities`:仍旧只增。** 实例有 `location`,而位置另有一份落在可见性表里
      (`_apply_ontology` 只填缺地写);覆盖了这边不覆盖那边,同一个东西会有两个
      "它在哪"。运行期 `spawn` 出来的实例根本不在文件里,不受影响。

    返回**真的写下去的那些种类 id**(新的 + 改了的)。调用方要拿它去把可见性那份
    镜像跟着改 —— 声明变了而镜像没变,是这个仓库最怕的那种不一致。
    """
    from anima_world.ontology import parse_entities, parse_kinds

    if world_seed is None or not fresh_world:
        return []
    if len(ontology_store) and not merge:
        return []
    kinds = world_seed.get("kinds") or []
    entities = world_seed.get("entities") or []
    if not kinds and not entities:
        return []
    if merge:
        kinds = _union_by_id(ontology_store.kind_definitions(), kinds, incoming_wins=True)
        entities = _union_by_id(ontology_store.entity_definitions(), entities)
    parse_entities(entities, parse_kinds(kinds))   # 校验在这里,坏了当场抛
    written, _ = ontology_store.seed(
        kinds, entities, datetime.now(timezone.utc).isoformat(),
        merge=merge, replace_kinds=merge)
    return written


def _union_by_id(
    existing: list[dict], incoming: list[dict], *, incoming_wins: bool = False
) -> list[dict]:
    """库里那份 + 文件里那份,按 `id` 去重。默认**库里的赢**(只增不改)。

    `incoming_wins=True` 用在声明上(种类、规律):它们是法不是状态,身上没有会随
    时间漂的东西,所以"文件里那条赢"不会倒带任何人的现在 —— 而反过来会让一个跑着
    的世界永远改不了自己的声明。顺序也跟着换:文件里那份排在前面,库里剩下的跟上。
    """
    if incoming_wins:
        out = [dict(row) for row in incoming]
        have = {str(row.get("id")) for row in out}
        out += [row for row in existing if str(row.get("id")) not in have]
        return out
    out = [dict(row) for row in existing]
    have = {str(row.get("id")) for row in out}
    out += [row for row in incoming if str(row.get("id")) not in have]
    return out


def _precheck_ontology(
    world_seed: dict[str, Any], rules_store: Any, location_store: Any, economy_store: Any,
    ontology_store: Any = None
) -> None:
    """在动任何一张表之前,先把作者写的 `kinds` / `entities` 验一遍。

    坏声明**整体拒绝世界**是对的(一次列全,所以 `--ticks 0` 能当校验器用)。
    但那次拒绝原先发生在**写完几张表之后** —— 于是失败留下一个装了一半的世界。
    这个函数把同一份判断搬到写之前,验的是同一套规则(它和 `_load_ontology` 走
    `Ontology.parse` 同一条路),所以两边不可能给出不同的答案。

    引用要查的三样此刻都在:规律读得到、地点读得到、物品读得到。种子里带的规律
    还没落库,所以这里把**文件里那一份**和**库里那一份**并起来查 —— 不然一条只在
    文件里的规律会被判成"引用了不存在的规律"。
    """
    # **走的是 `RedisOntologyStore.load` 同一条路**(parse_kinds → parse_entities →
    # resolve),只是喂给它文件里的声明而不是库里的。另写一份判断的话,两边迟早
    # 给出不同的答案 —— 而那种不一致会表现成"预检说没问题,开机还是失败"。
    from anima_world.ontology import parse_entities, parse_kinds, resolve

    seeded_rules = [dict(r) for r in (world_seed.get("rules") or []) if isinstance(r, dict)]
    # 地点同理:此刻库里那份可能还没播,所以把文件里的并进来 —— 不然一个只写在
    # 文件里的地点会被判成"实体挂在一个不存在的地方"。
    locations = [str(row.get("id")) for row in (location_store.all() or ())]
    locations += [str(l.get("id")) for l in (world_seed.get("locations") or []) if isinstance(l, dict)]
    # 物品同理,而且**来源不止 `items` 那一段**:`_seed_material_layer` 走的是
    # "引用即存在" —— 角色的 `inventory` 和地点的 `stock` 里提到的 id 也会被自动
    # 补一条定义。预检只看 `items` 的话,一个只写在随身物品里的 `garden_shears`
    # 会被判成"引用不到",而真实创世里它明明会存在。
    #
    # 这一处是被 `test_broken_material_entries_are_dropped_one_by_one_not_fatally`
    # 逮住的:那条测试把种子的 `items` 整个换掉,而橱窗的能力靠随身物品里的
    # `garden_shears` 才立得住 —— **预检和播种必须看同一批来源**,不然预检会拒掉
    # 一个真实开机完全正常的世界。
    items = [str(row.get("id")) for row in (economy_store.items() if economy_store else ())]
    seed_items = [str(i.get("id")) for i in (world_seed.get("items") or []) if isinstance(i, dict)]
    for section, field in (("agents", "inventory"), ("locations", "stock")):
        for entry in (world_seed.get(section) or []):
            if not isinstance(entry, dict):
                continue
            for row in (entry.get(field) or []):
                if isinstance(row, dict) and row.get("item"):
                    seed_items.append(str(row["item"]))

    # 种子里的规律还没落库,所以把**文件里那份**和**库里那份**并起来查 ——
    # 不然一条只在文件里的规律会被判成"引用了不存在的规律"。
    rules = list(_load_world_rules(rules_store))
    if seeded_rules:
        from anima_world.rules import parse_rules

        rules = rules + list(parse_rules(seeded_rules))

    # 本体同理:合并那条路上,库里已经声明过的种类此刻读得到,而文件里的新实例
    # 完全可以挂在它们身上。只验文件那一份会把这种写法判成"引用了不存在的种类"。
    kind_rows = world_seed.get("kinds") or []
    entity_rows = world_seed.get("entities") or []
    if ontology_store is not None and len(ontology_store):
        kind_rows = _union_by_id(ontology_store.kind_definitions(), kind_rows)
        entity_rows = _union_by_id(ontology_store.entity_definitions(), entity_rows)
    kinds = parse_kinds(kind_rows)
    entities = parse_entities(entity_rows, kinds)
    resolve(kinds, entities, rules=rules, locations=sorted(set(locations)),
            items=sorted({*items, *seed_items}))


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


def _apply_ontology(ontology: Any, stock_store: Any, visibility_store: Any,
                    *, tick: int = 0, redeclare_kinds: Any = ()) -> None:
    """把本体声明兑现成量、可见性、位置。**三样都只填缺,不覆盖。**

    每次开机都跑,不只创世 —— 它表达的是一条不变量:**一个实体存在,它声明过的量
    就存在**。整份写回则会把长了三十天的树倒带回幼苗(创世那条纪律踩过两次)。

    `tick` 是**此刻**,不是 0 —— 一个量的 `updated_tick` 决定下一次规律求值的 `dt`,
    所以钉错它的下场不是"少写一个字段",是这个量当场跳一大截:世界的钟在 174 上
    而量记的是 0,第一帧就补上 175 tick 的生长。它以前恒等于 0 只是因为世界都从
    午夜开始 —— 而"往一个跑着的世界里补一层声明"这条路上,它一直就是错的
    (`api.py` 的 `stock_set` 早就写着"写进来的 updated_tick 是此刻")。

    `redeclare_kinds` 是作者这次真的改动过的那些种类(`_seed_ontology` 的返回值)。
    可见性那张表是**声明的镜像**,不是状态:量的 `visibility` / `label` / `bands`
    都写在种类声明里。声明改了而镜像不改,同一个量会有两个答案 —— 而她读到的是
    镜像那个。所以这几个种类的可见性行照新声明重写,其余的照旧只填缺(作者显式写
    在 `stock_visibility` 段里的那些因此仍然赢)。
    """
    from anima_world.ontology import seed_quantities, visibility_declarations

    redeclare = {str(k) for k in (redeclare_kinds or ())}

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
            stock_store.set_many(entity.id, missing, tick=int(tick))

    declared = visibility_store.rules_map()
    for kind, key, visibility, label in visibility_declarations(ontology):
        if (kind, key) not in declared or kind in redeclare:
            # 分档写在量声明里(`{"visibility": "here", "bands": [...]}`)——
            # 可见性是量声明的一部分,分档同理,不必再去 `stock_visibility` 写
            # 第二行。写了第二行的话它先落库,这里的 `not in declared` 让它赢。
            quantity = ontology.kinds[kind].quantities.get(key)
            visibility_store.declare(kind, key, visibility, label,
                                     bands=quantity.bands if quantity else ())

    for entity in ontology.entities.values():
        # `here` 档靠它才成立:没有位置的东西永远不在任何地方,于是"在场可见"
        # 等于"永远看不见"。
        if entity.location and visibility_store.place_of(entity.id) is None:
            visibility_store.place(entity.id, entity.location, entity.name)


def _seed_stock_places(world_seed: dict[str, Any] | None, store: Any,
                       *, merge: bool = False) -> None:
    """种子里"这个东西在哪"(`"stock_places": [...]`)。空表一次。

    `here` 档的可见性靠它才成立 —— 没有它,一棵树永远不在任何地方,于是"在场可见"
    等于"永远看不见"。

    `merge=True` 把粒度降到**每个 owner**:已经站在某处的东西一个都不挪。位置是
    这个世界跑出来的现在(一条船会被撑到对岸),按今天文件里的写法放回去就是把它
    挪回码头,而世界照跑、日志干净。
    """
    if world_seed is None:
        return
    have = set(store.labels())
    if have and not merge:
        return
    for index, entry in enumerate(_seed_entry_dicts(world_seed, "stock_places")):
        owner = str(entry.get("owner") or "").strip()
        location = str(entry.get("location") or "").strip()
        if not owner or not location:
            logger.warning("world_seed stock_places[%s] 缺 owner 或 location;跳过", index)
            continue
        if owner in have:
            continue
        store.place(owner, location, entry.get("label"))


def _seed_stock_visibility(world_seed: dict[str, Any] | None, store: Any,
                           *, merge: bool = False) -> None:
    """种子里的可见性声明(`"stock_visibility": [...]`)。空表一次。

    `merge=True` 把粒度降到**每个 (种类, 量名)**。这一层和 `_seed_stocks` 是同一个
    坑的两半:声明全挂在 `world` 这一个 kind 下,按整张表判的话,一个已经声明过
    `雨势` 的世界永远补不进 `气温` 的 label 和 bands —— 她于是一直在报
    "气温 0.62" 这种数字,而没有任何一处报错。

    **声明本身就是这一层的开关** —— 没声明过任何东西的世界,角色感知不到任何量,
    这一层不进提示词、不花 token。坏条目逐条丢弃并点名(可见性写错的后果是
    "她本该知道却不知道",不该拦住启动)。

    ⚠️ **`bands` 是例外:坏分档拦住启动。** 理由不一样 —— 写错一档可见性的后果
    是"她少知道一件事",而写错 `bands` 的后果是"她一直在报数字",两者都不报错,
    但后者作者永远不会去查(提示词看上去正常,只是数字没被翻译)。这里再验一遍
    是**为了不半装**:闸在 `_load_world_file`(第一次写之前),这一遍守的是
    "世界不是从文件来的"那些入口。判断只有一份(`visibility_band_errors`)。
    """
    if world_seed is None:
        return
    have = {(str(r.get("kind") or ""), str(r.get("key") or "")) for r in store.declarations()}
    if have and not merge:
        return
    problems = visibility_band_errors(world_seed)
    if problems:
        raise WorldSeedError(problems)
    for index, entry in enumerate(_seed_entry_dicts(world_seed, "stock_visibility")):
        kind = str(entry.get("kind") or "").strip()
        key = str(entry.get("key") or "").strip()
        visibility = str(entry.get("visible") or entry.get("visibility") or "").strip()
        if not kind or not key:
            logger.warning("world_seed stock_visibility[%s] 缺 kind 或 key;跳过", index)
            continue
        if merge and (kind, key) in have:
            continue
        try:
            store.declare(kind, key, visibility, entry.get("label"),
                          bands=parse_bands(entry.get("bands")))
        except ValueError as exc:
            logger.warning("world_seed stock_visibility[%s] 没生效:%s", index, exc)


def _seed_world_defs(
    location_store: LocationStore, bt_store: BTStore, world_seed: dict[str, Any] | None,
    *, merge: bool = False
) -> list[str]:
    """Seed the definition tables once (empty-table no-op afterwards).

    Locations come from world_seed.json (fallback: the ids in `GRID`), grid
    coordinates from `locations.GRID`; the action table is generated from the
    live roster (`go_to_<loc>` / `chat_with_<agent>`) so it can't drift into
    ghost references the way the old hardcoded `ActionTable.default()` did.

    `merge=True` 把两张表的粒度都降到每一行,并把**真的新开出来的那些地点 id**
    交回给调用方 —— 它要拿这个去补 `location_join` 事件,不然新地点只存在于
    地图表里,而投影(以及任何一次重放)里没有它。
    """
    if world_seed is not None:
        # **只改了一部分的文件不该被当成"作者删掉了其余段"。**
        # 没写 `locations` = 这次不动地图,不是"这个世界没有地点"。
        seed_locs = world_seed.get("locations")
        seed_agents = world_seed.get("agents")
        loc_entries = (
            [_normalize_location_entry(loc, i, len(seed_locs)) for i, loc in enumerate(seed_locs)]
            if seed_locs is not None else [dict(p) for p in DEFAULT_POINTS]
        )
        agent_ids = [a["id"] for a in seed_agents] if seed_agents is not None else [
            a["id"] for a in CHARACTER_ROSTER
        ]
    else:
        loc_entries = [dict(p) for p in DEFAULT_POINTS]
        agent_ids = [e["id"] for e in CHARACTER_ROSTER]
    written = location_store.seed_defaults(loc_entries, merge=merge) or []
    point_ids = [e["id"] for e in loc_entries if e.get("kind", "point") == "point"]
    bt_store.seed_defaults(agent_ids=agent_ids, location_ids=point_ids, merge=merge)
    point_set = set(point_ids)
    return [loc_id for loc_id in written if loc_id in point_set]


def _join_spec(aid: str, agent: Any, world_seed: dict[str, Any] | None) -> dict[str, Any]:
    """一条 `agent_join` 的 `payload.spec` —— **创世那条路和一次编辑共用这一份**。

    `card` 在这里进,而不是从黑板上读:角色卡是**写给玩家看的**,
    `tagline` 尤其绝不许进提示词(混进人设她就会照着念,而那句是广告词)。
    黑板是她的上下文的来源,所以卡不上黑板 —— 它只走事件日志,读回来走投影
    (`character_card` 模块 docstring 第三条)。

    **没写卡就一个字都不写**:凭空补一张 `{"billing": "supporting"}` 的话,每个
    老世界重开一次都会多出一整份"作者说他们是背景角色"的声明,而宿主再也分不出
    "作者说他是背景"和"作者什么也没说"。缺省在**读**的那一侧补(`billing_of`)。

    ⚠️ **这条路只对「这一次新 join 的人」有效,而且语义是只填缺不覆盖** ——
    已经在册的角色不会重新 join,所以拿一份世界文件是改不动他们的卡的。改一个
    已经跑着的世界走 `World.set_card` / `anima-world agent set-card`,那条路
    **有意是覆盖**(理由写在它的 docstring 里:一个人指名道姓地说"这个人是主角",
    只填缺的话他永远说不动)。**两条语义相反是对的,别把其中一条"修"成另一条。**
    """
    spec: dict[str, Any] = {
        "name": agent.name,
        "personality": agent.blackboard.read("personality") or "",
    }
    card = card_of_seed_agent(aid, world_seed)
    if card:
        spec["card"] = card
    return spec


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
        seed_locs = world_seed.get("locations")
        if seed_locs is None:
            return
        entries = [_normalize_location_entry(loc, i, len(seed_locs))
                   for i, loc in enumerate(seed_locs)]
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
                "spec": _join_spec(aid, agent, world_seed),
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


def _join_authored_additions(
    event_log: EventLog,
    scheduler: Scheduler,
    world_seed: dict[str, Any] | None,
    location_store: LocationStore,
    newcomers: list[dict[str, str]],
    new_points: list[str],
) -> set[str]:
    """把一次编辑加进来的人和地方写进事件日志 —— 创世那条路的合并版。

    为什么非得发事件而不只是写表:名册、位置、关系、随身物品、钱**全是事件的投影**。
    只把人注册进调度器的话,他这一轮活着,重启之后就没有了 —— 而这中间他说过的话、
    走过的路都还在日志里,指着一个不存在的人。

    ⚠️ **`ts` 必须大于 0。** 重启时捡回"中途加入的人"那一段(`agent_join` 且
    `ev.ts > 0`)靠的就是这个:钉成 0 的话他们会被当成创世名册的一部分,而创世
    名册在有事件的世界里根本不被读 —— 于是这些人只在装文件的那一次开机里存在,
    下次重启就整批消失。

    关系只发**至少有一头是新人**的那些:两个老角色之间的关系是这个世界跑出来的现在,
    拿文件里的初值写回去就是把三十天的交情倒带回创世那一刻。
    """
    from anima_world.economy import TOWN

    ts = max(1, int(scheduler.clock))
    for loc_id in new_points:
        row = location_store.get(loc_id) or {}
        event_log.append({
            "ts": ts,
            "type": "location_join",
            "loc": loc_id,
            "payload": {
                "id": loc_id,
                "name": row.get("name", loc_id),
                "description": row.get("description", ""),
            },
        })
    joined = [e["id"] for e in newcomers if e["id"] in scheduler.agents]
    for aid in joined:
        agent = scheduler.agents[aid].agent
        event_log.append({
            "ts": ts,
            "type": "agent_join",
            "who": aid,
            "loc": agent.location,
            "payload": {
                "spec": _join_spec(aid, agent, world_seed),
                "state": {},
                "location": agent.location,
            },
        })
        amount = _money_for(aid, world_seed)
        if amount > 0:
            event_log.append({
                "ts": ts, "who": aid, "type": "payment",
                "payload": {"from": TOWN, "to": aid, "amount": amount,
                            "reason": "genesis_stipend"},
            })
    if world_seed is None or not joined:
        return set()
    registered = set(scheduler.agents)
    _seed_relations(event_log, registered, world_seed, require_new=set(joined))
    _seed_goals(event_log, set(joined), world_seed)
    _seed_memories(event_log, registered, world_seed, only=set(joined))
    _seed_inventory(event_log, set(joined), world_seed)
    logger.info(
        "作者层合并:%d 个新角色进了这个世界(%s),%d 个新地点上了地图",
        len(joined), "、".join(joined[:8]) + ("…" if len(joined) > 8 else ""),
        len(new_points),
    )
    return set(joined)


def _fold_seeded_memories(
    memory_store: MemoryStore, persisted: list[Event], only: set[str]
) -> None:
    """把刚写进日志的 `memory_seed` 折进记忆库 —— 只给这几个人,且只折一次。

    创世那条路上这一步是 `_rebuild_memories` 顺手做的,而它见了非空表就掉头,
    所以合并进来的人在那条路上永远拿不到自己的创世记忆:日志里有、库里没有,
    她开口时对自己的过去一无所知,而开机日志一行不错。

    幂等靠 `event_seq`:同一份文件连开两次,第二次这些事件的 seq 都已经在库里了。
    """
    have = {row.get("event_seq") for aid in only for row in memory_store.query(aid)}
    for event in persisted:
        if event.type != "memory_seed" or event.seq in have:
            continue
        payload = event.payload
        aid = payload.get("agent_id")
        if not aid or aid not in only:
            continue
        memory_store.add(
            agent_id=aid, tick=event.ts, kind=payload.get("kind", "seed"),
            summary=payload.get("summary", ""), importance=payload.get("importance", 0.5),
            anchor=bool(payload.get("anchor", False)), event_seq=event.seq,
        )


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
    existing = bt_store.shared_action_ids()
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


def _seed_material_layer(store: Any, world_seed: dict[str, Any] | None,
                         *, merge: bool = False) -> None:
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
    # `merge=True` 的粒度是每件物品 / 每格货架。货架上的数量尤其不能整份写回 ——
    # 那是这个世界卖了三十天之后的现在,按今天的文件放回去就是把卖掉的东西变回来。
    store.seed_authored(list(defined.values()), stock, merge=merge)


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


def _seed_relations(event_log: EventLog, registered_ids: set[str], world_seed: dict[str, Any],
                    *, require_new: set[str] | None = None) -> None:
    """rich-injection: initial relation values, reusing the existing
    sentiment/r_type state_change genesis semantics — zero new projection
    code. Only emitted for agents that actually got registered this boot.

    Both directions are seeded (relations[(a,b)] AND relations[(b,a)]),
    matching how live `chat` always emits a symmetric pair of sentiment
    events (actions.py `to_event`) — a single one-directional event would
    leave the other agent's view of the relationship at the Relation()
    default, silently, for any seed declaring a mutual relationship.

    `require_new`(作者层合并那条路)再加一道:**至少有一头是这次新来的人。**
    两个老角色之间的关系是这个世界跑出来的现在,拿文件里的初值发一遍就是把
    三十天的交情倒带回创世那一刻 —— 而 `state_change` 是覆盖写,不报错。"""
    for rel in _seed_entry_dicts(world_seed, "relations"):
        a, b = rel.get("a"), rel.get("b")
        if not isinstance(a, str) or not isinstance(b, str):
            logger.warning("world_seed relation has non-string agent ids (%r, %r); skipping", a, b)
            continue
        if a not in registered_ids or b not in registered_ids:
            continue
        if require_new is not None and a not in require_new and b not in require_new:
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


def _seed_memories(
    event_log: EventLog,
    registered_ids: set[str],
    world_seed: dict[str, Any],
    *,
    only: set[str] | None = None,
) -> None:
    """rich-injection: initial memories as `memory_seed` genesis events —
    event-sourced (D10) so a future memories-table rebuild can't lose them.
    Folded into MemoryStore by `_rebuild_memories`'s trigger closure, not
    here — first-boot seeding and rebuild share that one path.

    `only` 是合并那条路上的"只给新人播":「这个人不是新来的」和「这个人根本不在
    这个世界里」得分开报,合成一条的话每次合并都会为每个老角色喊一句"unknown
    agent" —— 喊的是假话,而人一旦学会忽略它,真的那句也一起被忽略了。
    """
    for mem in _seed_entry_dicts(world_seed, "memories"):
        aid = mem.get("agent_id")
        if not isinstance(aid, str) or aid not in registered_ids:
            logger.warning("world_seed memory references unknown agent %r; skipping", aid)
            continue
        if only is not None and aid not in only:
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
    _warn_if_llm_degraded(world)
    roster = "、".join(brain.agent.name for brain in world.scheduler.agents.values())
    print(f"[run] {world_id}  {len(world.scheduler.agents)} 个角色:{roster}")
    print("[run] 时钟已启动,Ctrl-C 停止(嵌入用法见 anima_world.api.World)")
    _run_world_foreground(world, quiet=args.quiet)
    print("[run] 世界已停下。")
    return 0


def _place_names(state: dict[str, Any]) -> dict[str, str]:
    """地点 id → 人话名。**印给人看的一律走它。**

    和提示词那侧的 `ChatService._place_name` 同一条纪律,只是这边的来源是
    `world.state()`:名册里已经这么翻了,而 `chat` 的抬头漏了,于是同一个命令
    先印「苏晚夏 @ home」,底下的地图和提示词都写「家」。不报错,只是这个世界
    对着用户说了半句英文 —— 而演示与模板一律中文是这个仓库的明文纪律。
    """
    return {
        str(row.get("id")): str(row.get("name") or row.get("id"))
        for row in state.get("locations", [])
    }


def _print_roster(world: Any, world_id: str, *, in_play: bool = False) -> None:
    """这个世界住着谁 —— 一个 .cyberworld 至今没有办法自报家门(#6)。

    `in_play=True` 是 `play` 里的 `/who`。**末尾那句"找谁说话"必须跟着变**:
    在 play 里换人是 `/at 夏`,而这张表照旧教人去开一个新进程跑
    `anima-world chat --agent 夏` —— 照做的下场是两个进程操作同一个世界,
    时钟那边还在走。它不报错,只是把人支到了另一条路上。
    """
    state = world.state()
    agents = state.get("agents", {})
    now = state.get("world_time", {})
    places = _place_names(state)
    print(f"\n  {onboarding.bold(world_id)}  第{now.get('day', 0)}天 "
          f"{now.get('hour', 0):02d}:{now.get('minute', 0):02d}\n")
    if not agents:
        print("  这个世界还没有住人。\n")
        return
    for agent_id, info in agents.items():
        where = places.get(info.get("location")) or info.get("location") or "?"
        doing = info.get("activity") or {}
        transit = doing.get("transit") if isinstance(doing, dict) else None
        if transit:
            doing_text = f"在去 {places.get(transit.get('to')) or transit.get('to')} 的路上"
        else:
            kind = (doing.get("kind") if isinstance(doing, dict) else None)
            doing_text = _ACTION_LABELS.get(kind, kind or "")
        tail = onboarding.dim(doing_text) if doing_text else ""
        away = onboarding.dim("(不在场)") if info.get("away") else ""
        print(f"    {_pad(agent_id, 12)}{_pad(info.get('name', agent_id), 16)}"
              f"{_pad('@' + str(where), 14)}{tail}{away}")
    first = next(iter(agents))
    if in_play:
        print(f"\n  换个人说话:{onboarding.bold(f'/at {first}')}\n")
    else:
        print(f"\n  找谁说话:{onboarding.bold(f'anima-world chat --agent {first}')}"
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


def run_contact(args: argparse.Namespace) -> int:
    """`anima-world contact` —— 谁想起过玩家、由头是什么(contact)。

    两张脸,故意分开:

    - 默认打**已经发生的**(`agent_wants_contact` 事件)。这是上层要消费的那份。
    - `--why` 打**此刻算出来是多少**,包括没触发的。调 `contact.threshold` 的人
      要的是这一份 —— 只看已发生的话,一个永远不触发的配置和一个刚好差一点的
      配置长得一模一样,而这一层默认关着、默认不响,静默失效是它最可能的坏法。
    - `--inbox` 打**收件箱**(`agent_hail`)。它和上面那份是互补的一对:一条是
      "你不在跟前时她想起了你",一条是"她当面叫住了你"。挂在同一条命令下面是
      有意的 —— 分成两条命令,运维得先知道有两条命令,而这一层此前在 CLI 上
      一个出口都没有(只能 `redis-cli HGETALL anima:<world>:contact`)。

    渲染是赠品,`--json` 才是契约(和 `map` / `ontology` 同一条)。
    """
    from anima_world.api import World

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "contact"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[contact] {exc}", file=sys.stderr)
        return 2
    inbox_face = bool(getattr(args, "inbox", False))
    if inbox_face and not args.player:
        world.close()
        print("[contact] --inbox 要 --player:收件箱是**某一个人**的",
              file=sys.stderr)
        return 2
    # 游标那三格(cursor / next_seq / scanned)只有分页的两张脸有;`--why` 是
    # 此刻算出来的一份快照,没有 seq 可言。
    cursor: dict[str, Any] | None = None
    try:
        if inbox_face:
            page = world.inbox_page(
                args.player, since_seq=args.since_seq, limit=args.limit,
            )
            rows, cursor = page["events"], page
            stats = world.contact_stats()
        elif getattr(args, "why", False):
            rows = world.contact_forecast()
            if args.player:
                rows = [r for r in rows if r["player_id"] == args.player]
            stats = world.contact_stats()
        else:
            page = world.contact_requests_page(
                args.player, since_seq=args.since_seq, limit=args.limit,
            )
            rows, cursor = page["events"], page
            stats = world.contact_stats()
        enabled = bool(world.config_get("contact.enabled", False))
    finally:
        world.close()

    if args.as_json:
        face = "inbox" if inbox_face else (
            "forecast" if getattr(args, "why", False) else "requests"
        )
        payload: dict[str, Any] = {"enabled": enabled, "stats": stats, face: rows}
        if cursor is not None:
            # **空页也要带着游标。** 照着 `rows[-1].seq` 推游标的脚本会在一个热闹的
            # 世界里饿死:一整窗都是别人的事件时它拿到空表,游标一步都推不动,
            # 而那个人自己那条永远排在窗外。`--json` 是契约,所以这一格必须在。
            payload.update({
                "cursor": cursor["cursor"],
                "next_seq": cursor["next_seq"],
                "scanned": cursor["scanned"],
                "total": cursor["total"],
            })
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    if inbox_face:
        # 收件箱和 contact.enabled 无关 —— `agent_hail` 走的是另一条路
        # (她当面叫住你),那句"这一层没在跑"在这张脸上是句谎。
        if not rows:
            print(f"没有人叫住过 {args.player}。")
            return 0
        for event in rows:
            payload = event.get("payload") or {}
            where = payload.get("location_name") or payload.get("location") or "?"
            who = payload.get("agent_name") or payload.get("agent_id")
            print(f"#{event.get('seq')} {who} 在{where}叫住了 "
                  f"{payload.get('player_name') or args.player}"
                  f"({payload.get('reason') or 'hail'})")
            if payload.get("text"):
                print(f"    「{payload['text']}」")
        return 0

    if not enabled:
        # 关着的时候先说出来。空表加上一个关着的开关,读起来像"没人想你",
        # 而真相是这一层压根没跑。
        print("(contact.enabled 是关的 —— 这一层没在跑;开:"
              f"anima-world config set contact.enabled true --world-id {world_id})")

    if getattr(args, "why", False):
        if not rows:
            print("没有一对 (角色, 玩家) 可算 —— 她们跟任何玩家都还没有关系。")
            return 0
        for row in sorted(rows, key=lambda r: -r["components"]["score"]):
            mark = "→" if row["would_fire"] else " "
            print(
                f"{mark} {row['agent_name']}({row['agent_id']}) → "
                f"{row['player_name']}({row['player_id']}):"
                f"{row['components']['score']:.2f} / 门槛 {row['components']['threshold']:.2f}"
            )
            print(
                f"    近 {row['components']['closeness']:.2f} × "
                f"由头 {row['components']['urge']:.2f} × "
                f"状态 {row['components']['readiness']:.2f}    {row['explain']}"
            )
            for reason in sorted(row["reasons"], key=lambda r: -r["weight"]):
                print(f"    · {reason['label']}({reason['weight']:.2f}):{reason['note']}")
        return 0

    if not rows:
        print("还没有人想起过谁。" if enabled else "")
        return 0
    for event in rows:
        payload = event.get("payload") or {}
        print(
            f"#{event.get('seq')} 第{payload.get('day')}天 {payload.get('at')} "
            f"{payload.get('agent_name') or payload.get('agent_id')} 想起了 "
            f"{payload.get('player_name')}"
        )
        print(f"    「{payload.get('topic')}」({payload.get('topic_source')})")
        for reason in payload.get("reasons") or []:
            print(f"    · {reason.get('label')}({reason.get('weight')}):{reason.get('note')}")
    return 0


def run_relationship(args: argparse.Namespace) -> int:
    """`anima-world relationship` —— 一段关系此刻的人话。

    此前这一层在 CLI 上一个出口都没有:要看一段关系只能去 `state --json` 里翻
    `relations`,而那儿躺着的是四个 -1~1 的浮点数。**给数字等于把一段关系变成
    一根进度条**,而刷分是恋爱陪伴产品最不该长出来的东西。

    所以人看的那张脸**一个浮点数都不印**:一句话、一个档、和上一次改变它的
    那件事。要数字的去拿 `--json`(`axes` 那一格) —— 渲染是赠品,`--json`
    才是契约,和 `map` / `ontology` / `contact` 同一条。
    """
    from anima_world.api import World

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "relationship"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[relationship] {exc}", file=sys.stderr)
        return 2
    agent, other = getattr(args, "agent", None), getattr(args, "other", None)
    try:
        if agent and other:
            payload: Any = world.relationship_summary(agent, other)
            rows = [payload]
        else:
            rows = world.relationship_summaries(agent_id=agent or "", other_id=other or "")
            payload = {"relationships": rows}
    finally:
        world.close()

    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    if not rows:
        print("这个世界里还没有任何一段关系。" if not (agent or other)
              else "没有查到这一段关系 —— 两个人之间还没有来往。")
        return 0
    for row in rows:
        # 判定还没落地的那些行没有档 —— `band` 是 sentiment 0.0 折出来的,印成
        # 「还谈不上什么交情」等于把"还不知道"说成一个结论。
        head = f"[{row['band_name']}] " if row.get("exists") else ""
        print(f"{head}{row['summary']}")
        change = row.get("last_change")
        if change:
            where = (f"第 {change['conversation_id']} 号对话"
                     if change.get("conversation_id") is not None else "查不到是哪一场对话")
            print(f"    上一次改变它的是 #{change['seq']}(tick {change['tick']},{where})")
    return 0


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
            # **两个都印,和下面那几行能力同一条理由** —— 而这一行此前只印人话。
            # 后果是这张表自己跟自己对不上:`agent` 的量表里写着「手上的活儿」,
            # 底下的能力写着「她得 me_手艺 >= 1.0」,于是作者(和我)读出来的结论是
            # "手艺 这个量没声明过,这几条能力永远做不成" —— 而它声明得好好的,
            # 只是 label 换了个说法。查一个量到底叫什么,只有这里问得到。
            key = q["key"] if q.get("label", q["key"]) == q["key"] else \
                f"{q['key']}({q['label']})"
            print(f"    量   {pad(key, 14)}默认 {q['default']:g}{unit}"
                  f"   她感知得到:{q['visibility']}")
            if q.get("bands"):
                # **她读到的是这几个词,不是数字** —— 而"作者把这个量翻成了什么"
                # 和动词表同一个道理:只有这里问得到。猜一份档词出来的错和猜动词
                # 一样不报错,只是界面上写着一个世界里不存在的说法。
                print("         分档 " + " / ".join(
                    f"{float(t):g}↑ {w}" for t, w in q["bands"]))
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
            # 得有人一起做的事。和生灭一样要显眼:它决定这条能力**一个人调不动**,
            # 而一个人调不动这件事,只有这里问得到 —— 猜不出来,试出来的代价是
            # 一次拒绝。
            party = a.get("participants")
            if party:
                want = (
                    f"{party['min']} 个"
                    if party["min"] == party["max"]
                    else f"{party['min']}~{party['max']} 个"
                )
                print(f"         ✦ 得有人一起:除发起的人之外还要 {want}"
                      f",而且每个人都要点头(拒得掉)")
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


def run_roster(args: argparse.Namespace) -> int:
    """`anima-world roster` —— **这个世界里有谁**,连人带卡一次给出。

    补的是两个洞,都是真人试玩撞出来的:

    - **显示名此前没有读出口**(已经在线上咬人):`map --json` 里地点有 `name`,
      人只有 id,于是网站旁白印的是 `mai:`、`yu:`。剩下唯一的路是重放整份事件
      日志去捞 `agent_join` —— 而那是历史,不是现在。
    - **角色卡到不了玩家眼前**:作者写的主次 / 一句话 / 立绘进得去出不来,
      而**一个进得去出不来的字段和没有这个字段是同一个 bug,只是发作得更晚**。

    创作台那侧的判据是**有没有 CLI 出口** —— 库里有而命令行上没有,对不 import
    本包的它等于不存在。渲染是赠品,`--json` 才是契约(和 `map` / `ontology` 同一条)。

    `hidden` 的人**照出**:引擎是"这个世界里有谁"的权威。要不要给玩家看是宿主
    那一层的事(运维台的壳在 `/internal/v1/roster` 上筛掉它们)。
    """
    from anima_world.api import World

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "roster"):
        return 2
    if args.billing is not None and args.billing not in CARD_BILLINGS:
        print(f"[roster] --billing 只认 {', '.join(CARD_BILLINGS)},收到 {args.billing!r}",
              file=sys.stderr)
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[roster] {exc}", file=sys.stderr)
        return 2
    try:
        payload = world.roster()
    finally:
        world.close()

    agents = payload["agents"]
    if args.billing is not None:
        agents = [a for a in agents if a["billing"] == args.billing]

    if args.as_json:
        # **筛过的也照 `roster()` 的形状给** —— 消费方按同一个 key 读。
        print(json.dumps({"operation": "roster", "world_id": world_id, "agents": agents},
                         ensure_ascii=False, indent=2))
        return 0

    from anima_world.mapview import display_width

    def pad(text: str, width: int) -> str:
        return text + " " * max(1, width - display_width(text))

    if not agents:
        print(f"{world_id}:一个人都没有。")
        return 0

    # 主次用记号而不是英文单词:通讯录那一屏本来就是靠一眼看出主次的。
    mark = {"lead": "★", "supporting": " ", "hidden": "·"}
    carded = sum(1 for a in payload["agents"] if a["card"])
    print(f"{world_id}:{len(agents)} 人")
    for row in agents:
        where = row["location_name"] or "不在任何地方"
        away = "  (离场)" if row["away"] else ""
        print(f"{mark.get(row['billing'], ' ')} {pad(row['agent_id'], 14)}"
              f"{pad(row['name'], 14)}@{where}{away}")
        if row["tagline"]:
            print(f"    {row['tagline']}")
        if row["portrait"]:
            print(f"    立绘 {row['portrait'][:72]}")
    if carded == 0:
        # 一张卡都没有要说出来 —— 什么也不说和"这个世界写了卡但没读出来"长得一样。
        print("\n(这个世界没做过角色卡:通讯录上这些人一样重、没有一句话、没有立绘。"
              "在世界文件的 agents[].card 里写。)")
    else:
        # 记号不解释就是一道谜:两个符号看得见,而"它们各是什么意思"看不见。
        print("\n(★ 主角  · 还没出场(hidden,引擎照出,筛不筛是宿主的事)  "
              "无记号 背景)")
    return 0


def run_drift(args: argparse.Namespace) -> int:
    """`anima-world drift` —— 人设漂移的尺子(R2)。**只读。**

    渲染是赠品,`--json` 才是契约(和 `map` / `ontology` 同一条)。
    退出码有意义:**漂了退 1**,所以它能进 CI —— 一个"人设一致性"的回归闸
    和 `ontology --check` 是同一个用法。样本不够时退 0 并说出为什么
    (不够就是不够,那不是失败)。
    """
    from anima_world.api import World

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "drift"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[drift] {exc}", file=sys.stderr)
        return 2
    try:
        report = world.persona_drift(
            args.agent, baseline_n=args.baseline, player_id=args.player,
        )
    except KeyError as exc:
        print(f"[drift] {exc}", file=sys.stderr)
        return 2
    finally:
        world.close()

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 1 if report.get("drifted") else 0

    who = report.get("agent_name") or report["agent_id"]
    if not report["ok"]:
        print(f"[drift] {who}:{report['reason']}")
        return 0
    print(f"[drift] {who} —— 她说过 {report['messages']} 条,"
          f"前 {report['baseline_n']} 条当基线;阈值 {report['threshold']}")
    for row in report["features"]:
        mark = "漂" if row["drifted"] else "  "
        # 标签按**显示宽度**补,不按字符个数(`_pad`)—— 中文是双宽字符,
        # `:<6` 会让「用字丰富度」那一行比别的行宽出五格,整张表推歪。
        # 这是这个仓库在地图那一层踩过、写进 CLAUDE.md 的同一条。
        print(f"  {mark} {_pad(row['label'], 11)}基线 {row['baseline']:>8.3f}"
              f"  最近 {row['recent']:>8.3f}  累积 {row['cusum']:>6.2f} {row['direction']}")
    print(f"[drift] {report['verdict']}")
    if (report.get("sycophancy") or {}).get("rising"):
        print("[drift] ⚠️ 迎合度在持续上升 —— 《拟人化互动办法》第八条(五)"
              "禁止过度迎合用户、诱导情感依赖")
    return 1 if report["drifted"] else 0


def run_engagement(args: argparse.Namespace) -> int:
    """`anima-world engagement` —— 他跟这个世界处得有多深(E2)。**只读。**

    给数不给结论:依赖预警的阈值与干预是宿主的判断(引擎不触达用户),
    这一层只把散在三处的账拢到一起。理由见 `World.player_engagement`。
    """
    from anima_world.api import World

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "engagement"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[engagement] {exc}", file=sys.stderr)
        return 2
    try:
        report = world.player_engagement(args.player)
    except ValueError as exc:
        print(f"[engagement] {exc}", file=sys.stderr)
        return 2
    finally:
        world.close()

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"[engagement] {report['player_id']} —— "
          f"{report['conversations']} 场对话、{report['messages']} 条消息、"
          f"跨 {report['agents']} 个角色、{report['span_days']} 天;"
          f"世界主动想起他 {report['contacts']} 次")
    for row in report["relationships"]:
        print(f"  {row['other_name'] or row['other_id']} ← {row['agent_name']}:"
              f"{row['summary']}")
    if not report["relationships"]:
        print("  —— 还没有任何一个角色和他有来往")
    return 0


def run_presence(args: argparse.Namespace) -> int:
    """`anima-world presence` —— 开 `presence.enforce_colocation` 之前的体检。

    **这道命令是为迁移写的。** 引擎侧收紧位置语义会当场打断线上世界:
    `player_move` 是宿主的可选调用,今天线上根本没人调,于是"异地"是每一次调用的
    默认值 —— 那道闸打开的当天,`give` 和一起做事全线开始拒绝,而回执看上去像是
    玩家自己站错了地方。

    所以它先答"这个世界里有没有人在维护玩家的位置",再给一句**能照着做的**结论。
    """
    from anima_world.api import World

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "presence"):
        return 2
    world = World.open(world_id, redis=redis, mysql=mysql)
    try:
        report = world.presence(args.player_id)
    finally:
        world.close()

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    names = report["agents"]
    print(f"{world_id}:同地这道闸 "
          f"{'开着' if report['enforced'] else '关着(默认)'}\n")
    print("角色在哪:")
    for aid, here in sorted(names.items()):
        print(f"  {aid:<20}{here or '在路上 / 不知道'}")
    print("\n玩家在哪:")
    if not report["players"]:
        # **筛过之后的空,和世界本身的空,是两件事。** 一句"这个世界跟谁都还没打过
        # 交道"会让人跑去查宿主接没接上,而真相只是这一个 id 抄错了。
        print(f"  (这个世界不认得 {args.player_id} —— 既没有在场记录,"
              "也没有打过交道的记录。id 抄错了?)" if args.player_id else
              "  (这个世界跟谁都还没打过交道 —— 没人跟她说过话)")
    # 真部署里 player_id 是 membership 的 uuid(36 字),写死 20 会让 id 和地点
    # 糊成一坨 —— 只有 `p1`/`cli` 那种短名字才看着是对的。
    pad = max([20] + [len(r["player_id"]) + 2 for r in report["players"]])
    for row in report["players"]:
        where = row["location"] or "✗ 世界不知道(没调过 player_move,或者他已经不在场)"
        face = "、".join(row["face_to_face"]) or "没有人"
        print(f"  {row['player_id']:<{pad}}{where}")
        print(f"  {'':<{pad}}此刻面对面:{face}"
              f"{'' if row['present'] else '(而且他不在在场名册上)'}")
    print()
    # ⚠️ **这一句不许省,但 3.2.0 起说的是另一件事。** 从前它警告"位置只活在这个
    # 进程里";现在在场落 Redis 带 TTL,于是要说的是**为什么会没位置** ——
    # 没人调过 `player_move`,或者他很久没动静、TTL 过了。不说的话,读的人会
    # 照着一个假警报去改宿主。
    print("说明:在场与位置住在 Redis 上(`anima:{world_id}:player:*`),带过期时间 ——"
          "跨进程、扛重启。这里没有位置只有两种可能:宿主没调过 player_move,"
          "或者他很久没动静已经过期。名单是从落库的联系态补齐的。")
    if report["unplaced"]:
        # **不许只报数字。** 这一句是这道命令唯一真正的产出:读的人要知道下一步
        # 该改哪儿,而不是知道有几个玩家没位置。
        tail = (
            "这道闸正在拒绝调用 —— 确认跑世界的那个进程每轮都调 player_move。"
            if report["enforced"] else
            "开 presence.enforce_colocation 之前,先让**跑世界的那个进程**每轮调一次 "
            "player_move,否则一开就是 give / 一起做事全线拒绝。"
        )
        print(f"\n⚠ {report['unplaced']} 个玩家在世界里没有位置。{tail}")
        print("  这里面有一部分只是**很久没动静**(在场带 TTL):真的没接 player_move 的话,"
              "刚聊过的人也会没有位置。")
        return 1
    if report["players"]:
        print("✓ 每个玩家都有位置 —— 这道闸开得起来。")
    return 0


def _pick_asker(world: Any, agent_id: str) -> str:
    """没指定玩家时,替 `prompt` 挑一个**世界真认得**的人。

    优先站在她跟前的那个 —— 调提示词的人想看的就是那一轮。挑不出来就退回
    `DEFAULT_PLAYER_ID`,那时抬头上会明说这是个陌生人。
    """
    try:
        report = world.presence(None)
    except Exception:  # noqa: BLE001 —— 挑不出人不该让这条只读命令告吹
        return DEFAULT_PLAYER_ID
    rows = [r for r in (report.get("players") or []) if r.get("known")]
    if not rows:
        return DEFAULT_PLAYER_ID
    # 排序而不是取第一个:名单顺序会随 Redis 变,而同一条命令两次给出不同的
    # 提示词,比给错还难查。
    facing = sorted(r["player_id"] for r in rows if agent_id in (r.get("face_to_face") or ()))
    return facing[0] if facing else sorted(r["player_id"] for r in rows)[0]


def run_prompt(args: argparse.Namespace) -> int:
    """`anima-world prompt` —— 她此刻收到的提示词,逐块摊开。

    为什么它值得一道命令:提示词是这套东西**最不可见又最容易出错**的一层。1.3
    开发期四个 bug 有三个在这儿,而当时唯一的诊断办法是写 Python 往私有属性上塞
    一个假 LLM 去偷看 —— 世界作者(改的是 `prompt_templates` 里的模板)一点办法没有。

    **看,但不碰**:不推时钟、不进 LLM、不写玩家状态,静音中的角色也照样交出来。

    ⚠️ **「不碰」有个代价:默认那个玩家是个幽灵。** `chat`/`play` 默认的 `cli` 会被
    `player_move` 挪进世界,当场变成真人;这一条不写玩家状态,所以它永远不会。世界
    不认得的人身上,身份/在场/关系三块整个换一套算法 —— 她被告知对方没报过名字、
    不在她跟前、这是手机私聊,而真玩家被列成"同场角色,不是正在和你说话的人"。
    线上实测:幽灵 8 块,真玩家 10 块,重合的那几块里有三块是反的。而它渲染得毫无
    破绽 —— 一个来调提示词的人会照着它去改一个不存在的问题。
    所以不给 `--player-id` 时**去世界里找一个真的**(优先站在她跟前的那个),
    并且**永远把"这一份是拿谁算的"印在抬头上** —— 挑了谁不说,是另一种撒谎。
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
        player_id = args.player_id or _pick_asker(world, args.agent)
        seen = world.debug_prompt(
            args.agent,
            player_id=player_id,
            display_name=args.name or "",
            message=args.message,
        )
    finally:
        world.close()

    if args.as_json:
        print(json.dumps(seen, ensure_ascii=False, indent=2))
        return 0

    name = roster[args.agent].get("name") or args.agent
    asker = seen["asker"]
    who = asker["display_name"] or asker["player_id"]
    print(f"{name} 此刻收到的提示词:{len(seen['blocks'])} 块 / {seen['system_chars']} 字")
    print(f"(假设 {who} 说的是「{args.message}」)")
    if not asker["known"]:
        # **这不是提醒,这是这一次输出的成色。** 下面那几块是拿一个世界不认得的人
        # 算的,和任何一轮真对话都不一样 —— 不说的话,读的人会照着它去改提示词。
        print(onboarding.yellow(
            f"⚠ 这个世界不认得「{asker['player_id']}」"
            + ("(你没给 --player-id,而这个世界里一个玩家都没有)"
               if args.player_id is None else "(id 抄错了?)")
        ))
        print("  她眼里这是个没报过名字、也不在她跟前的陌生人:身份/在场/关系三块"
              "因此和真的那一轮不一样。")
        print("  想看真的那一份:anima-world prompt --agent … --player-id <真玩家>"
              "(用 `anima-world presence` 查 id)。")
    print()
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
        state = world.state()
        roster = state.get("agents", {})
        places = _place_names(state)
        if args.list_only or args.agent is None:
            _print_roster(world, world_id)
            return 0
        if args.agent not in roster:
            print(f"[chat] 这个世界里没有 {args.agent!r}。", file=sys.stderr)
            _print_roster(world, world_id)
            return 2

        agent_id = args.agent
        info = roster[agent_id]
        display_name = args.name or ""
        # 走到对方跟前再开口:在场块(时间/地点/同地者)靠玩家所在地才成立,
        # 会话也才知道自己发生在哪儿。地点读不出来就算了,聊天不该因此告吹。
        if info.get("location"):
            try:
                world.player_move(args.player_id, info["location"])
            except (KeyError, ValueError):
                logger.debug("player could not be placed at %r", info.get("location"))

        degraded = world.state().get("runtime", {}).get("llm", {}).get("degraded_reason")
        where = info.get("location") or ""
        print(onboarding.rule(
            f"{info.get('name', agent_id)} @ {places.get(where, where) or '?'}"
        ))
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
            # `meta` 是出参:她这一轮摆的姿态、这句话被判成什么意图、调了哪个
            # 能力,都从这里回来。**必须一路带到 `record_chat_turn`** ——
            # 不带的话那四列("她这一轮赌了气")永远是 null,而消息本身好好地
            # 落了库:运维台上气泡照常显示,只是永远没有 tag,没有一处会报错。
            # `play` 那条路一直是带的,这条路忘了 —— 两条门,一条把观测量丢了。
            meta: dict[str, Any] = {}
            try:
                reply = world.chat_reply(
                    agent_id, history[-20:],
                    player_id=args.player_id, display_name=display_name, meta=meta,
                )
            except (KeyError, ValueError) as exc:
                print(f"[chat] {exc}", file=sys.stderr)
                history.pop()
                continue
            reply = reply.strip() or "……"
            print(f"{info.get('name', agent_id)} > {reply}\n")
            history.append({"role": "assistant", "content": reply})
            # 一轮一记:关系判定在这里发生,世界也在这里落盘。
            world.record_chat_turn(agent_id, args.player_id, history[-2:], meta=meta)
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

        display_name = args.name or ""
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
                _print_roster(world, world_id, in_play=True)
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


def run_memory(args: argparse.Namespace) -> int:
    """`anima-world memory repair-ticks` —— 迁移那批墙钟 tick。

    算法一个字都不在这儿:它在 `World.repair_memory_ticks`,和
    `TriggerEngine._tick_of` 同一条折法。CLI 只负责开世界、印出来、定退出码。
    """
    from anima_world.api import World

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "memory"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[memory] {exc}", file=sys.stderr)
        return 2
    try:
        result = world.repair_memory_ticks(dry_run=bool(args.dry_run))
    finally:
        world.close()

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        verb = "要改" if args.dry_run else "改了"
        print(f"[memory] 扫到 {result['scanned']} 条盖着墙钟的记忆,{verb} {result['repaired']} 条。")
        for row in result["rows"]:
            if row["repaired_to"] is None:
                print(f"  #{row['id']} {row['agent_id']} {row['kind']} "
                      f"tick={row['tick']} —— 查不到出处(event_seq={row['event_seq']}),没动")
            else:
                print(f"  #{row['id']} {row['agent_id']} {row['kind']} "
                      f"tick={row['tick']} → {row['repaired_to']}")
        if result["unresolved"]:
            # 降级不许无声:剩下的那些还会继续占着召回列表的前排。
            print(f"[memory] 有 {result['unresolved']} 条查不到出处,一律没动 —— "
                  f"编一个 tick 出来比留着更坏,因为它从此看不出来了。", file=sys.stderr)
    return 1 if result["unresolved"] else 0


def run_agent(args: argparse.Namespace) -> int:
    """`anima-world agent repair-goals` / `agent set-card` —— 角色数据的两个写口。

    和 `memory repair-ticks` 同一个形状:算法在 `World` 上,CLI 只开世界、印出来、
    定退出码。**改之前先 `--dry-run` 看一眼**是这类命令的用法,不是客套 ——
    它们动的是作者写的东西。
    """
    from anima_world.api import World

    command = getattr(args, "agent_command", None)
    if command == "set-card":
        return run_agent_set_card(args)
    if command != "repair-goals":
        print("[agent] 只有 repair-goals / set-card 两个子命令", file=sys.stderr)
        return 2

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "agent"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[agent] {exc}", file=sys.stderr)
        return 2
    try:
        result = world.repair_agent_goals(dry_run=bool(args.dry_run))
    finally:
        world.close()

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    verb = "要改" if args.dry_run else "改了"
    print(f"[agent] 扫了 {result['scanned']} 个角色,{verb} {result['repaired']} 个。")
    for row in result["rows"]:
        print(f"  {row['name']}({row['agent_id']}):")
        print(f"    现在 {len(row['before'])} 条:{json.dumps(row['before'], ensure_ascii=False)}")
        print(f"    改成 {len(row['after'])} 条:{json.dumps(row['after'], ensure_ascii=False)}")
    if not result["repaired"]:
        print("[agent] 没有要修的 —— 这个世界的 goals 是好的。")
    return 0


def run_agent_set_card(args: argparse.Namespace) -> int:
    """`anima-world agent set-card` —— 给**一个已经跑着的世界**改一张角色卡。

    补的是角色卡那一整轮改造漏掉的一节:卡只在 `agent_join` 的 `payload.spec` 上,
    而已经在册的角色永远不会再 join —— 于是那一轮**对唯一一个有真人的世界等于
    没做**(线上 20 个角色一张卡都装不进去,而作者写得进、校验放行、包也导得出)。

    **一次只改一个人,不收文件。** 生产上这条路的入口是运维台的一次性容器,argv
    由具名参数白名单生成 —— 容器里没有作者的文件;而 argv 是数组传递,中文和标点
    直接当一个元素传,不过 shell。

    判断都在 `World.set_card` 的 docstring 里(**覆盖**、部分合并、`--clear` 单独
    一格、幂等)。这里只管三件事:参数互斥、退出码、印给人看。
    **退出码 2 = 「我听懂了,但我不干」**(运维台把它翻译成 409);编一个空回执
    出去的话,运维的人会以为改成功了。
    """
    from anima_world.api import World

    given = {
        key: getattr(args, key)
        for key in ("billing", "tagline", "portrait")
        if getattr(args, key, None) is not None
    }
    if args.clear and given:
        print("[agent] --clear 和 --billing/--tagline/--portrait 不能一起给:"
              "一句是「删掉这张卡」,一句是「这张卡写成这样」—— 引擎挑哪句都是猜。",
              file=sys.stderr)
        return 2
    if not args.clear and not given:
        print("[agent] 什么都没给 —— --billing / --tagline / --portrait / --clear "
              "至少给一个。一次什么也没改的「成功」读起来和改成功了一模一样。",
              file=sys.stderr)
        return 2

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "agent"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[agent] {exc}", file=sys.stderr)
        return 2
    try:
        receipt = world.set_card(
            args.agent, given or None,
            clear=bool(args.clear), dry_run=bool(args.dry_run),
        )
    except KeyError as exc:
        # `KeyError` 的 str() 会加一层引号 —— 拿 args[0] 才是那句人话。
        print(f"[agent] {exc.args[0]}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[agent] {exc}", file=sys.stderr)
        return 2
    finally:
        world.close()

    if args.as_json:
        print(json.dumps({"operation": "agent set-card", "world_id": world_id, **receipt},
                         ensure_ascii=False, indent=2))
        return 0

    who = f"{receipt['name']}({receipt['agent_id']})"
    if not receipt["changed"]:
        # **说出「没有变化」** —— 一声不吭和改成功了长得一模一样,而这条命令
        # 最常见的用法就是运维照着单子一个一个敲过去。
        print(f"[agent] {who} 的卡没有变化,一个字都没写。")
        return 0

    before, after = receipt["before"] or {}, receipt["after"] or {}
    verb = "要改" if receipt["dry_run"] else "改了"
    print(f"[agent] {who} 的卡{verb}:")
    for key in sorted(set(before) | set(after)):
        was, now = before.get(key), after.get(key)
        if was == now:
            continue
        print(f"    {key:10} {was if was is not None else '(没写)'}"
              f" → {now if now is not None else '(删掉)'}")
    for warning in receipt["warnings"]:
        print(f"[agent] {warning}", file=sys.stderr)
    if receipt["dry_run"]:
        print("[agent] --dry-run:一个字节都没写。")
    else:
        print(f"[agent] 看一眼:anima-world roster --world-id {world_id}")
    return 0


def run_player(args: argparse.Namespace) -> int:
    """`player forget` / `player options` / `player erase` —— 玩家数据的三个出口。

    **forget 不是一次删除,是往日志里追加一条事实**(`player_departed`);理由写在
    `World.forget_player` 的 docstring 里,一句话:关系是投影,手删投影下一次重放
    自己长回来。所以这条命令改的是世界的历史**加了一条**,而不是少了一条。

    **erase 是法务抹除**(用户行使删除权,《拟人化互动办法》第十六条):先 forget,
    再删转录与记忆、把事件里他的名字与原文改写掉 —— 设计与边界在
    `World.erase_player` 的 docstring 里。不带 `--yes` 只数,和 `world drop` 同款。

    **options 是只读的**:这个人此时此地点得动什么。它存在的理由是宿主那侧 ——
    `player_tools()` 说得出"有 interact 这个按钮",说不出"这会儿有什么可以 interact"。

    和 `agent repair-goals` 同一个形状:算法在 `World` 上,CLI 只开世界、印出来、
    定退出码。**先 `--dry-run` 看一眼**是写命令的用法。
    """
    from anima_world.api import World

    command = getattr(args, "player_command", None)
    if command not in {"forget", "options", "erase"}:
        print("[player] 只有 forget / options / erase 三个子命令", file=sys.stderr)
        return 2

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "player"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[player] {exc}", file=sys.stderr)
        return 2
    if command == "options":
        try:
            menu = world.player_options(args.player)
        finally:
            world.close()
        return _print_player_options(args, menu)
    if command == "erase":
        try:
            receipt = world.erase_player(
                args.player, reason=args.reason, dry_run=not args.yes,
            )
        except ValueError as exc:
            print(f"[player] {exc}", file=sys.stderr)
            return 2
        finally:
            world.close()
        if args.as_json:
            print(json.dumps(receipt, ensure_ascii=False, indent=2, default=str))
            return 0
        verb = "抹了" if args.yes else "要抹"
        print(f"[player] {receipt['player_id']} —— {verb}:"
              f"事件改写 {receipt['events']} 条、"
              f"会话 {receipt['conversations']} 场 {receipt['messages']} 条消息、"
              f"记忆删 {receipt['memories_dropped']} 行改 {receipt['memories_redacted']} 行"
              f"(显示名 {receipt['names']} 个,跳过 {receipt['names_skipped']} 个)。")
        if not args.yes:
            print("[player] (没带 --yes:世界一个字节都没动)")
        else:
            print(f"[player] 已记下 player_erased(seq={receipt['seq']})——"
                  "账本没动;别的进程的内存窗口重启后干净。")
        return 0
    try:
        receipt = world.forget_player(
            args.player, reason=args.reason, dry_run=bool(args.dry_run),
        )
    except ValueError as exc:
        print(f"[player] {exc}", file=sys.stderr)
        return 2
    finally:
        world.close()

    if args.as_json:
        print(json.dumps(receipt, ensure_ascii=False, indent=2, default=str))
        return 0
    verb = "要清" if args.dry_run else "清了"
    chat_state = receipt["chat_state"]
    print(f"[player] {receipt['player_id']} —— {verb}:"
          f"关系 {receipt['relations']} 段、关系图 {receipt['edges']} 条边、"
          f"联系态 {receipt['contact']} 行、"
          f"聊天当前值 {'?' if chat_state is None else chat_state} 行。")
    if args.dry_run:
        print("[player] (--dry-run:世界一个字节都没动)")
    else:
        print(f"[player] 已记下 player_departed(seq={receipt['seq']})——"
              "历史、记忆、转录、账本一个字没改。")
    return 0


_BLOCKED_SAYS = {
    # **空菜单的原因要说出来。** 空表加一句沉默读起来像"这儿什么也没有",
    # 而这三件事一件也不是那个意思(和 `presence` 的 `known` 那一格同一课)。
    # ⚠️ **这一句 3.2.0 之前说的是另一件事。** 从前它警告"位置是进程内的,所以这条
    # 命令对一个跑在别处的世界总是这一句" —— 而在场早就搬进了 Redis(`presence` 那条
    # 已经改过口,这条漏了)。留着它更坏:它教人把一个**真**信号当成 CLI 的已知毛病
    # 挥挥手过去,于是没接 player_move 的宿主永远查不出来。
    "unknown_player_location": "世界不知道他这会儿在哪。三种可能:这个世界压根不"
                               "认得这个 id(抄错了?)、宿主从没调过 player_move、"
                               "或者他很久没动静、在场已经过期"
                               "(`anima-world presence` 分得开这三种)",
    "in_transit": "他这会儿在路上 —— 在途不算站在任何地方",
    "no_ontology": "这个世界没有声明过任何东西(没有 kinds),没什么可交互的",
}


def _print_player_options(args: argparse.Namespace, menu: dict[str, Any]) -> int:
    """`player options` 的两张脸。**`--json` 才是契约**,渲染是赠品。"""
    if args.as_json:
        print(json.dumps(menu, ensure_ascii=False, indent=2, default=str))
        return 0
    where = menu["location_name"] or menu["location"] or "?"
    print(f"[player] {menu['player_id']} 在 {where}:")
    if menu["blocked"]:
        print(f"  —— {_BLOCKED_SAYS.get(menu['blocked'], menu['blocked'])}")
        return 0
    if not menu["targets"]:
        print("  —— 这儿没有能做点什么的东西")
    for row in menu["targets"]:
        gloss = f"({row['gloss']})" if row["gloss"] else ""
        print(f"  [{row['id']}] {row['name']}{gloss}")
        for verb in row["verbs"]:
            mark = "可以" if verb["available"] else f"不行/{verb['reason']}"
            tail = f" —— {verb['refusal']}" if verb["refusal"] else ""
            print(f"      {verb['label']}:{mark}{tail}")
    if menu["overflow"]:
        print(f"  (还有 {menu['overflow']} 样没带进来 —— 你没细看)")
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


class _NothingInTheWorldYet:
    """一个"库里什么都还没有"的替身,给**不建世界**的那条校验路用。

    `_precheck_ontology` 要三样东西来解引用:库里的规律、地点、物品。创世那一刻
    它们**本来就是空的**(种子还没落库,所以那个函数把文件里那一份并进来查)——
    所以拿一个空替身喂它,验的正好是"这份文件自己立不立得住"。

    ⚠️ 空替身只对**完整世界文件**成立。一次编辑的引用可以落在目标世界里,那时
    空替身会把一份完全正常的编辑判成"引用了不存在的种类" —— 所以 `--edit` 那一支
    不跑这道闸,并且**把没查的那一半说出来**。
    """

    def definitions(self) -> list[Any]:
        return []

    def all(self) -> list[Any]:
        return []


def _authored_ontology_errors(authored: dict[str, Any]) -> list[str]:
    """`kinds` / `entities` / 规律那一摞闸,**不建世界地跑一遍**。

    走的是 `_precheck_ontology` —— 开机那条路上**同一个函数**。量名拼错、动词没
    声明、`me_X` 没声明、`spawn` 没写代价、能力里引用不到的物品:这一整摞此前
    `validate world` 一条都报不出来(FOR-STUDIO §3.17 把这个缺口列成了一张表,
    并写着"出包前那一步请是 `simulate --ticks 0`")。那句话本身是诚实的,但它把
    一件引擎该答的事推给了每一个消费方:运维台的判包容器里没有 Redis,创作台的
    体检跑在世界之前 —— 于是"这份文件这一版引擎装得进去吗"离线问不出答案。

    **不另写一份判断**是这里唯一要守的:两份判断迟早给出不同答案,而那种不一致会
    表现成"预检说没问题,开机还是失败"。
    """
    from anima_world.ontology import OntologyError
    from anima_world.rules import RuleError

    empty = _NothingInTheWorldYet()
    try:
        _precheck_ontology(authored, empty, empty, None, None)
    except (OntologyError, RuleError) as exc:
        return list(getattr(exc, "errors", None) or [str(exc)])
    except Exception as exc:  # noqa: BLE001 - 坏声明的形状很多,一律当成一条错报出去
        return [str(exc)]
    return []


def _cannot_even_look(path: str) -> str | None:
    """**这个文件被看过没有** —— 分的是"我没答上来"和"这个引擎收不了它"。

    `world check` 的退出码答的是前者,而这两件事从读文件那一侧看长得很像:
    两条路都是"读的时候出错了"。分界不在出错没出错,在**有没有一份内容被判断过**:
    路径打错、没权限、指着一个目录 —— 世界根本没被看过,那时说一句"装不进去"是在
    替一个没人看过的文件下判决。而文件打得开、里头的字节这个引擎读不懂(不认识的
    记录类型、格式版本比它新、校验和对不上),**那是一个答案**,开机也会照样拒绝。
    """
    try:
        with open(path, "rb") as probe:
            probe.read(1)
    except OSError as exc:
        return f"读不了 {path}:{exc}"
    return None


def _load_authored_layer(
    path: str, *, scan: Any = None
) -> tuple[dict[str, Any], str | None]:
    """读一个世界文件的**作者层**,聚合成 section 字典。读不了就把话说清楚。

    **流式喂进去**(不 `list()` 一份出来):一个跑过的世界导出来是十几万条状态记录,
    而这里只挑 `author` 那几条 —— 攒一份全量列表出来纯属白背,而这条命令正是要被
    拿去问一个真实舰队世界的(灯塔湾那份包)。`author_records_to_seed` 单趟遍历,
    校验和那条 `WorldFileError` 照旧在迭代耗尽时抛出来,一起被下面接住。

    给了 `scan`(一个 `media.MediaScan`)就在**同一趟**上顺手把图数了。为了数图再
    读第二遍等于把上面那条纪律作废,而在同一条流上分叉是免费的。
    """
    from anima_world.media import tee_media
    from anima_world.world_file import WorldFileError, author_records_to_seed, read_world_file

    try:
        _, records = read_world_file(path)
        if scan is not None:
            records = tee_media(records, scan)
        return author_records_to_seed(records), None
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
    from anima_world.world_seed import world_seed_warnings

    if args.validate_command in ("world", "seed"):
        # 世界文件:读出作者层再走**开机那条路上同一批闸**(`authored_layer_errors`
        # 与 `_precheck_ontology`)。另写一份判断的话,迟早出现"validate 说没问题,
        # 开机还是失败" —— 而 2026-08-19 舰队上撞到的是它的反面:开机说没问题
        # (一份纯状态层的导出包),validate 却答"'agents' must be a list"。
        edit = bool(getattr(args, "edit", False))
        authored, read_error = _load_authored_layer(args.path)
        if read_error is not None:
            return _report_validation("world", args.path, [read_error], [], args.json)
        errors = authored_layer_errors(authored, complete=not edit)
        warnings: list[str] = []
        if not authored:
            # **作者层为空 = 没有种子,不是一个空种子。** 开机那条路一直这么判;
            # 这里从前不是,于是任何一份导出的世界在校验器嘴里都是非法的。
            warnings.append(
                "这份文件没有作者层(只有状态记录)—— 它是一个跑过的世界导出来的,"
                "装载时直接落键、不走作者层那几道闸,所以这里没有可查的东西"
            )
        else:
            warnings += (
                world_seed_warnings(authored)
                + _authored_drift_warnings(authored)
                + _authored_unreachable_requirements(authored)
                + _authored_media_warnings(authored)
            )
            if edit:
                # **说出没查的那一半。** 一次编辑的引用可以落在目标世界里(它的名册
                # 和地图在它自己的库里),而目标世界不在手上 —— 编一个绿灯出去,
                # 正是这条命令要修的病本身。
                warnings.append(
                    "这是一次编辑(--edit),引用完整性没查:种类 / 地点 / 物品 / 规律"
                    "可以来自目标世界,而目标世界不在手上 —— 要连着世界查,"
                    "用 `simulate --ticks 0 --world-file …`"
                )
            else:
                errors += _authored_ontology_errors(authored)
        return _report_validation("world", args.path, errors, warnings, args.json)

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
    from anima_world.api import _PLAYER_TTL_SECONDS
    from anima_world.beats import (
        OP_REQUIRED_FIELDS,
        PREDICATE_REQUIRED_FIELDS,
        VALID_OPS,
        _VALID_PREDICATES,
    )
    from anima_world.sim_report import BUCKETS, REPORT_FORMAT_VERSION
    from anima_world.world_package import PACKAGE_FORMAT_VERSION
    from anima_world.character_card import (
        CARD_BILLINGS as _BILLINGS,
        CARD_KEYS,
        DEFAULT_BILLING,
        PORTRAIT_MAX_BYTES,
        PORTRAIT_SCHEMES,
        TAGLINE_MAX_CHARS,
    )
    from anima_world.media import (
        LOCATION_IMAGE_GLOSS,
        LOCATION_IMAGE_MAX_BYTES,
        MEDIA_SCHEMES,
    )
    from anima_world.world_seed import (
        WORLD_SEED_AGENT_KEYS,
        WORLD_SEED_AGENT_OPTIONAL_KEYS,
        WORLD_SEED_LOCATION_KEYS,
        WORLD_SEED_LOCATION_OPTIONAL_KEYS,
    )

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
            # 3.2.0:在场玩家从进程内存搬进 Redis(重启不该让世界忘了人坐在她对面)。
            # **带 TTL,而且不进 `.cyberworld`** —— JSON 存不了 TTL,装回去就是一份
            # 永不过期的假在场;而包是分发物,不该带着别人的玩家此刻在哪儿。
            # 镜像端(运维台 `lib/worldPackage.js`)照这一格对齐:打包时跳过这两类键。
            "volatile_keys": ["lock", "players", "player:{player_id}"],
            "presence": {
                "index_key": "anima:{world_id}:players",
                "row_key": "anima:{world_id}:player:{player_id}",
                "ttl_seconds": _PLAYER_TTL_SECONDS,
                "in_package": False,
            },
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
                # 这两格是**画按钮要先知道的**:一个要玩家真在她跟前,一个要手边
                # 有一样能动的东西。FOR-STUDIO 早就照这条写着"从 contract --json
                # 读 requires_colocation",而这里一直没给 —— 文档比代码走得快的那种
                # 不一致,正是这个仓库最怕的一种。
                "requires_colocation": spec.requires_colocation,
                "requires_target_entity": spec.requires_target_entity,
            }
            for spec in chat_tools.tools_for("*")
        ],
        "package": {"format_version": PACKAGE_FORMAT_VERSION},
        "report": {"format_version": REPORT_FORMAT_VERSION, "buckets": list(BUCKETS)},
        "seed": {
            "schema_version": None,  # 无版本号:随主版本走
            "agent_keys": sorted(WORLD_SEED_AGENT_KEYS),
            # **必填之外、引擎认得并且带得过河的那些。** 分开一格是因为
            # `agent_keys` 是必填集(镜像端拿它算"少了什么"),把可选键混进去
            # 等于要求每个世界给每个角色写一张卡。
            "agent_optional_keys": sorted(WORLD_SEED_AGENT_OPTIONAL_KEYS),
            "location_keys": sorted(WORLD_SEED_LOCATION_KEYS),
            # 地点的两格图。和 `agent_optional_keys` 同一格、同一个理由:
            # **可选**,而且创作台要一个问得到的答案而不是按版本号猜。
            "location_optional_keys": sorted(WORLD_SEED_LOCATION_OPTIONAL_KEYS),
            # **哪个是哪个也在契约里。** 创作台的铁律是"问引擎,不读文档",所以
            # 一份只报了两个键名的契约等于让它去猜哪个铺满屏幕、哪个是地图上那一格 ——
            # 猜错了不报错,只是两张图各在错的地方。
            "location_image_keys": dict(LOCATION_IMAGE_GLOSS),
            "location_image_schemes": sorted(MEDIA_SCHEMES),
            # 上限量的是**这条 URI 字符串本身**(不是原图字节;base64 之后约 4/3),
            # 数按读出口定 —— 地点的图骑在 `state()` 上,而那道门每几秒被问一次、
            # 一次带回全部地点,所以它比立绘那一格贵得多,数也就不一样。
            "location_image_max_bytes": LOCATION_IMAGE_MAX_BYTES,
        },
        # 角色卡:作者写给**玩家**看的那一面。创作台照这一段决定填什么、怎么校验,
        # 而它此前只能靠版本号猜"这支引擎带不带得动" —— 猜错不报错。
        "character_card": {
            "keys": sorted(CARD_KEYS),
            "billings": list(_BILLINGS),
            "default_billing": DEFAULT_BILLING,
            "tagline_max_chars": TAGLINE_MAX_CHARS,
            "portrait_schemes": sorted(PORTRAIT_SCHEMES),
            # 这一轮补上的一格。**此前引擎一个字节都没管过** —— 一张 8 MB 的图
            # base64 进世界文件,加载放行、导出带着走、`roster()` 每次整个吐出来,
            # 而没有任何一处会说一句话。量的是这条 URI 字符串,不是原图字节。
            "portrait_max_bytes": PORTRAIT_MAX_BYTES,
            # 读出口。**没有出口的字段等于没有这个字段**,所以它和形状写在一起。
            "read_command": "roster",
            # 写出口。只报读出口的时候,"作者层写得进"和"一个跑着的世界改得动"
            # 长得一模一样 —— 而那两件事差着这个仓库最怕的那一类 bug:线上那 20 个
            # 早就在册的角色一张卡都装不进去,全程零报错。
            "write_command": "agent set-card",
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
    print(f"  可选           agents{payload['seed']['agent_optional_keys']} "
          f"locations{payload['seed']['location_optional_keys']}")
    print(f"  角色卡         {payload['character_card']['keys']}   "
          f"主次 {'/'.join(payload['character_card']['billings'])}"
          f"(缺省 {payload['character_card']['default_billing']})   "
          f"读出口 anima-world {payload['character_card']['read_command']}   "
          f"写出口 anima-world {payload['character_card']['write_command']}")
    for key, gloss in payload["seed"]["location_image_keys"].items():
        print(f"  地点图         {key:<12} {gloss}")
    print(f"  图的闸         scheme {'/'.join(payload['seed']['location_image_schemes'])}   "
          f"立绘 ≤ {payload['character_card']['portrait_max_bytes'] // 1024} KiB   "
          f"地点每格 ≤ {payload['seed']['location_image_max_bytes'] // 1024} KiB"
          f"(量的是 URI 本身;引擎不存字节)")
    print(f"  节拍 op        {', '.join(payload['beats']['ops'])}")
    print(f"  节拍谓词       {', '.join(payload['beats']['predicates'])}")
    print(f"\n  {onboarding.dim('持有镜像的仓库用 --json 对齐;种子与节拍没有版本号,随主版本走。')}")
    return 0


def _autonomy_is_overdue(redis: Any, world_id: str, store: Any, row: dict) -> str:
    """上一轮过去太久了吗 —— 是就返回该说的那句话,否则空串。

    宽限给两个间隔:一个间隔是正常节奏,卡在第二个间隔上多半是这个世界压根没人
    在跑(`run` 停了 / 只被 `simulate` 快进过)。留一个间隔的余量,免得每次正常
    的轮次间隙都报一次警 —— 一句总在响的警告等于没有警告。
    """
    try:
        last_tick = int(row.get("last_tick"))
        now = int(redis.get(f"anima:{world_id}:clock") or 0)
    except (TypeError, ValueError):
        return ""   # 老世界发布的行没有这一格,那就只按四个数判
    interval = max(1, int(store.get("autonomy.interval_ticks", default=72) or 72))
    behind = now - last_tick
    if behind <= interval * 2:
        return ""
    return (f"上一轮在第 {last_tick} tick,现在第 {now} tick —— 隔了 {behind} 个 tick"
            f"(间隔本该是 {interval});这个世界多半没人在跑,或者 hook 掉了")


def _report_autonomy_chain(redis: Any, world_id: str, store: Any) -> int:
    """定时轮次这条链跑没跑 —— 返回"需要处理"的项数。

    为什么在 `doctor` 里:这一层是这个引擎里**最容易静默地不工作**的一条。开关
    点亮了、时钟在走、日志一行不错,而她一次也没主动过 —— 分不清是"她确实没什么
    想做的"、hook 没挂上、LLM 一直失败,还是根本没人在跑这个世界。而在这之前
    `World.autonomy_stats()` 只有 Python 出口:一个开着 `anima-world run` 的人
    **没有任何办法问出这句话**(FOR-STUDIO 的判据:库里有而 CLI 上没有,
    对外面等于不存在)。
    """
    from anima_world.redis_state import meta_rows

    if not store.get("autonomy.enabled", default=False):
        print(f"  {onboarding.dim('定时轮次关着(autonomy.enabled)')}")
        return 0
    row = meta_rows(redis, world_id).get("autonomy_stats")
    if not isinstance(row, dict):
        print(f"  {onboarding.yellow(onboarding.WARN)} 定时轮次开着,但**一轮都没跑过** —— "
              f"她不会自己做任何事")
        print(f"      {onboarding.dim('快进(simulate)不跑它;要它跑得用 anima-world run 或 start')}")
        return 1
    asked = int(row.get("asked") or 0)
    acted = int(row.get("acted") or 0)
    quiet = int(row.get("quiet") or 0)
    failed = int(row.get("failed") or 0)
    last = str(row.get("last") or "")
    tail = f",最近一次:{last}" if last else ""
    line = (f"定时轮次(本次开机以来):问过 {asked} 次,做了 {acted} 次,"
            f"歇了 {quiet} 次,没成 {failed} 次{tail}")

    # **判据是"离上一轮过去多久了",不是那四个数。** 数只说本次开机以来,而重启
    # 之后库里躺着的还是上一次开机那一行 —— 光看数的话,"刚重启"和"这条链死了"
    # 长得一模一样,那正是这一节要分开的两件事。
    stale = _autonomy_is_overdue(redis, world_id, store, row)
    if stale:
        print(f"  {onboarding.yellow(onboarding.WARN)} {line}")
        print(f"      {onboarding.dim(stale)}")
        return 1
    if failed and failed >= acted:
        print(f"  {onboarding.yellow(onboarding.WARN)} {line}")
        # 报出**那一次是什么**。只说"没成 1 次"的话,人下一步无处可去 ——
        # `last` 每轮都被改写,失败的理由早被后面的沉默盖掉了。
        why = str(row.get("last_failure") or "").strip()
        if why:
            print(f"      {onboarding.dim('最近一次没成:' + why)}")
        print(f"      {onboarding.dim('做的没有没成的多 —— 多半是动词的前提不满足,或者 LLM 在编参数')}")
        return 1
    if asked and not acted and not quiet:
        # 问了却既没做也没"想了想算了" —— 那是每一轮都半路掉了(渲染失败 / LLM 报错)。
        print(f"  {onboarding.yellow(onboarding.WARN)} {line}")
        print(f"      {onboarding.dim('问过却一次都没走完 —— 看 last 那句话')}")
        return 1
    print(f"  {onboarding.green(onboarding.OK)} {line}")
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

    problems += _report_autonomy_chain(redis, world_id, store)

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
        # 店面栏:作者填的,直通玩家的世界卡片。渲染上看得见,是"这几栏到底有没有
        # 跟着包走过来"最省事的一次自查 —— JSON 那份是契约,这份是给人看的回执。
        ("题材", payload.get("genre") or "—"),
        ("背景", payload.get("setting") or "—"),
        ("主题", payload.get("theme") or "default"),
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


def _print_check_human(payload: dict[str, Any]) -> None:
    path = payload["path"]
    if payload["loadable"] is None:
        print(onboarding.rule(f"{path} —— 问不出来"))
    elif payload["loadable"]:
        print(onboarding.rule(f"{path} —— {payload['engine_version']} 装得进去"))
    else:
        print(onboarding.rule(f"{path} —— {payload['engine_version']} 装不进去"))
    for line in payload["errors"]:
        print(f"  ✗ {line}")
    for line in payload["warnings"]:
        print(f"  {onboarding.yellow('!')} {line}")
    _print_check_media(payload)
    if payload["loadable"] and not payload["warnings"]:
        print("  没有发现问题。")


def _print_check_media(payload: dict[str, Any]) -> None:
    """图这一段。**不联网探活** —— 这里报的是"这个包挂在谁身上",不是"它们还活着吗"。

    外链不是错(图床由网站自己建,世界文件里只留一条绝对链接是定下来的形状),
    但它是一笔**看不见的依赖**:包发出去之后,这几台服务器每一台都得继续活着。
    按 host 列出来,是让这笔账在拿到包的那一刻就能被看见。
    """
    external = payload.get("external_media") or []
    inline = payload.get("inline_media_bytes") or {}
    if not external and not inline.get("count"):
        return
    print(f"  {onboarding.dim('图 —— 不联网探活,只报这个包指向哪儿')}")
    for row in external:
        print(
            f"    {row['count']:>4} 张  {row['scheme']}://{row['host']}"
            f"  ({', '.join(row['fields'])})"
        )
    if inline.get("count"):
        print(
            f"    {inline['count']:>4} 张  内嵌 data:  共 "
            f"{inline['total'] // 1024} KiB,最大一张 {inline['largest'] // 1024} KiB"
        )


def run_world_check(args: argparse.Namespace) -> int:
    """**这一版引擎装得进这份 `.cyberworld` 吗** —— 真跑校验器,不读封皮。

    ⚠️ **它和 `validate world` 是同一个判断,不是第二份。** 两条都调
    `authored_layer_errors` + `_precheck_ontology`,也就是开机第一秒调的那两个。
    `tests/test_validate_matches_boot.py` 把三条路的答案钉成相等 —— 判断有两份的
    那天,它们会先给出不同的答案,再由某个人在一个坏掉的世界上发现。

    **那为什么还要第二个命令?两件事,都不在"判断"上:**

    ① **退出码问的是不同的问题。** `validate world` 是**作者的门**:退出码 = 我的
    世界过没过(错误 2、提醒 0),CI 里直接当断言用。`world check` 是**宿主的门**:
    退出码 = **这句话我答没答上来**(0 = 答上来了,`loadable` 才是答案;1 = 没答上来,
    文件根本打不开)。运维台要的正是后者 —— "跑不了"是一个答案,不是一个异常;
    一个把"世界坏了"和"命令挂了"报成同一个数的出口,调用方只能去猜。
    (`start` 是人的门、`run` 是程序的门,是同一条分法。)

    ⚠️ 而这条分法有一个**踩过的坑**:退出码 1 那一格一度收得太宽 —— 凡是读文件
    出错就报"没答上来",于是**不认识的记录类型 / 平铺的 `body` / 不认识的 `author`
    type / 格式版本比这个引擎新 / 校验和对不上**这一整摞全落进了 1。它们每一条都让
    **开机当场失败**,`validate world` 也照实报错误 —— 而这个出口自己写着"问不出来
    一律不拦",于是一份引擎明确拒收的文件会被下游**放行**。分界不在"读的时候出没出
    错",在 `_cannot_even_look`:**这个文件被看过没有。** 打不开(路径错 / 没权限 /
    指着目录)= 没被看过 = `null` + 1;打得开而这个引擎读不懂 = `false` + 0。

    ② **能力探测按子命令在不在做,不比版本号。** 这条是运维台的纪律,而它决定了
    这里必须是一个**新名字**:`validate world` 在老引擎上**也存在**,只是那时它对
    一份纯状态层的导出包答"'agents' must be a list"——存在但答错,正是能力探测最
    骗人的形态。而同一个 `3.2.0` 下有过七份不同的引擎,所以版本号比不出来。
    `world check` 在老引擎上是 argparse 的 usage + 退出码 2,调用方据此回落到
    "问不出来",而**问不出来一律不拦**。

    背景:2026-08-19 舰队上灯塔湾 `815b3ae9` 在 3.3.0 上 0.57 秒退出 1
    (`stocks[8] 'values' must be an object` ×11)—— Redis 数据完好,卷上那份
    2.3.0 时代的 `world.cyberworld` 过不了今天的作者层。而 `world inspect` 读封皮,
    对同一份文件答 `runnable: true`。**封皮说的是作者声称要哪个引擎,这里说的是
    这一版引擎真的收不收它** —— 两个问题,`inspect` 那一格没有被动过。
    """
    from anima_world.media import MediaScan
    from anima_world.world_package import _engine_version
    from anima_world.world_seed import world_seed_warnings

    scan = MediaScan()
    payload: dict[str, Any] = {
        "operation": "world check",
        "path": str(args.package),
        "engine_version": _engine_version(),
        "edit": bool(getattr(args, "edit", False)),
        "loadable": None,
        "authored": None,
        "errors": [],
        "warnings": [],
        # 图这两段。**不联网探活** —— 要报的是"这个包挂在哪几台服务器
        # 身上、自己又带了多少字节",不是"这些链接还活着吗"(那是运维的活,而且
        # 答案每分钟都在变)。图床由网站自己建、世界里只存绝对外链是定下来的形状,
        # 那么这条选择的**代价**就该在拿到包的那一刻看得见:发出去之后,这里列出的
        # 每一台都得继续活着,而包自己不会因为哪一台没了就报错 —— 它只是少几张图。
        #
        # 作者层和状态层**都数**:一个跑过的世界导出来只有状态记录,而立绘就在
        # `agent_join` 的载荷里。只数作者层的话,那种包会得到一句"外链 0 条"。
        "external_media": [],
        "inline_media_bytes": {"count": 0, "total": 0, "largest": 0},
    }
    unopenable = _cannot_even_look(args.package)
    authored, read_error = (
        ({}, None) if unopenable else _load_authored_layer(args.package, scan=scan)
    )
    if unopenable is None and read_error is None:
        # **只有整份读完了才报这两段。** 半途抛错的那趟扫描手里是一份残缺的账
        # (读到第几条算第几条),而"12 条外链的包报出 3 条"是一个**安静的错答案**,
        # 比一句"没数"坏得多。读不完的时候这两段留空,和上面 `loadable` 那条
        # "问不出来"是同一个姿势。
        payload["external_media"] = scan.external_media()
        payload["inline_media_bytes"] = scan.inline_media_bytes()
    if unopenable is not None:
        # **文件打不开不是"装不进去",是"没答上来"。** 报成 `loadable: false` 的话,
        # 一个路径打错的调用方会得到一句关于世界的判决 —— 而世界没有任何问题。
        payload["errors"] = [unopenable]
        code = 1
    elif read_error is not None:
        # ⚠️ **而"打得开、但这个引擎读不懂"是一个答案,不是一句"我没答上来"。**
        # 不认识的记录类型、平铺的 `body`、不认识的 `author` type、格式版本比这个
        # 引擎新、校验和对不上 —— 这一摞全都让**开机当场失败**,`validate world`
        # 也照实报错误(退出码 2)。它们一度被这条命令报成 `loadable: null` + 退出
        # 码 1,而这个出口自己写着"问不出来一律不拦" —— 于是一份引擎明确拒收的文件
        # 会被下游**放行**。这正是这条命令要修的那种病:一个答案被当成另一个答案读。
        # 分界因此不在"读文件出没出错",而在**这个文件被看过没有**。
        payload["errors"] = [read_error]
        payload["loadable"] = False
        code = 0
    else:
        errors = list(authored_layer_errors(authored, complete=not payload["edit"]))
        warnings: list[str] = []
        payload["authored"] = bool(authored)
        if not authored:
            warnings.append(
                "这份文件没有作者层(只有状态记录)—— 它是一个跑过的世界导出来的,"
                "装载时直接落键、不走作者层那几道闸,所以这里没有可查的东西"
            )
        else:
            warnings += (
                world_seed_warnings(authored)
                + _authored_drift_warnings(authored)
                + _authored_unreachable_requirements(authored)
                + _authored_media_warnings(authored)
            )
            if payload["edit"]:
                warnings.append(
                    "这是一次编辑(--edit),引用完整性没查:种类 / 地点 / 物品 / 规律"
                    "可以来自目标世界,而目标世界不在手上"
                )
            else:
                errors += _authored_ontology_errors(authored)
        payload["errors"] = errors
        payload["warnings"] = warnings
        payload["loadable"] = not errors
        code = 0

    if getattr(args, "as_json", False):
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        _print_check_human(payload)
    return code


def run_world_package(args: argparse.Namespace) -> int:
    """`anima-world world export / import / inspect / check` —— `.cyberworld` v3。"""
    from anima_world.api import World
    from anima_world.world_file import WorldFileError
    from anima_world.world_package import (
        PackageValidationError,
        drop_world,
        import_world_file,
        inspect_world_file,
    )

    if args.world_command == "check":
        return run_world_check(args)

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
                **{k: v for k, v in counts.items() if k != "dropped"},
                "dropped": counts.get("dropped") or {},
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(f"  迁好了 → {args.output}")
                print(f"    状态记录  {counts.get('redis', 0)}")
                print(f"    事件      {counts.get('event', 0)}")
                print(f"    增长的三样 {counts.get('mysql', 0)}")
                gaps = counts.get("seq_gaps_filled", 0)
                for table, rows in (counts.get("dropped") or {}).items():
                    from anima_world.migrate_v1 import DROPPED_TABLES

                    # 有意丢弃也要报 —— 这是使用者唯一有机会说"等等那个我要"的时刻。
                    print(f"    · 丢掉 {table}({rows} 行):{DROPPED_TABLES[table]}")
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
    # 人在屏幕前等一句台词的那三个命令,引擎的日志收起来(见
    # `_engine_logs_out_of_the_way`)。挂在这儿而不是三个 `run_*` 里面,是因为
    # 最吵的那几条发生在 `World.open` 里面 —— 函数体开头才拦就已经晚了。
    if args.command in ("start", "chat", "play"):
        with _engine_logs_out_of_the_way(getattr(args, "verbose", False)):
            return _dispatch(args)
    return _dispatch(args)


def _dispatch(args: argparse.Namespace) -> int:
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
    if args.command == "roster":
        return run_roster(args)
    if args.command == "contact":
        return run_contact(args)
    if args.command == "relationship":
        return run_relationship(args)
    if args.command == "presence":
        return run_presence(args)
    if args.command == "drift":
        return run_drift(args)
    if args.command == "engagement":
        return run_engagement(args)
    if args.command == "chat":
        return run_chat(args)
    if args.command == "run":
        return run_run(args)
    if args.command == "simulate":
        return run_simulate(args)
    if args.command == "events":
        return run_events(args)
    if args.command == "memory":
        return run_memory(args)
    if args.command == "agent":
        return run_agent(args)
    if args.command == "player":
        return run_player(args)
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
