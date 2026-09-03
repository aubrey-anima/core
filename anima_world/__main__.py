"""CLI entrypoint for anima_world."""

from __future__ import annotations

import ast
import argparse
import contextlib
import json
import os
import re
from datetime import datetime, timezone
import logging
import sys
import time
import threading
from pathlib import Path
from typing import Any, Iterable

from anima_world import onboarding
from anima_world.actions import ActionTable
from anima_world.agent import Agent
from anima_world.beats import (
    BeatScript, BeatScriptError, coerce_goals, split_against_stored,
)
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
    LOCATION_IMAGE_GLOSS,
    LOCATION_IMAGE_KEYS,
    LOCATION_IMAGE_MAX_BYTES,
    clip_uri,
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
def _engine_logs_out_of_the_way(verbose: bool = False, *, to_stderr: bool = False):
    """人正在跟她说话的这段时间里,引擎的日志收起来 —— `start` / `chat` / `play`。

    实测的样子:玩家问了一句,屏幕上先冒出来
    `plan step starts outside the day (1440), dropping`,再才是她的回答。那是一句
    英文的、跟玩家无关的、他也无从处理的话,横在对话正中间;而 `run` / `simulate`
    那边同一句是真信号(那里没有人在等一句台词)。

    **不是丢掉。** 收着,散场时报一行"这一场引擎记了几条";`--verbose` 就照原样打,
    什么都不拦。丢掉的话,这个仓库最怕的那种坏法(照跑但给错东西)就少了一个出口。

    此前只有关系判定那一个 logger 被单独按下去(而且只在已降级时),于是别的模块
    照旧插话 —— 一个一个按下去就是给下一个模块留一个洞。

    `to_stderr` 给 `chat --message --json`(3.7.0):那条路上 **stdout 必须只有那一份
    JSON**,而散场这一行是给人看的。⚠️ **仍然要印,只是换一条管道** —— 静音它就把
    "收着不丢"这条纪律换成了"丢掉",而丢掉正是这个上下文管理器不做的那件事。
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
            print(f"  {onboarding.dim(note)}",
                  file=sys.stderr if to_stderr else sys.stdout)


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

    # **这一行要说得出它真查了什么。** 「世界文件」是 world.db 时代的词(世界早已
    # 只住 Redis),而它真正查的那几样里最贵的一样 —— 长过程有没有做完 —— 一个字
    # 都没出现在帮助里:人不会去跑一个自己不知道能回答这个问题的命令。
    doctor = sub.add_parser(
        "doctor",
        help="体检:Redis 持久化、密钥、LLM 连通性、时钟快慢、"
             "自主链、长过程有没有做完",
        description="体检:Redis 会不会把世界忘掉、密钥在不在、LLM 通不通、"
                    "时钟快慢、定时轮次这条链通没通、"
                    "要花时间的长过程有几件真做完了。⚠️ 账要全,判要新:"
                    "屏幕上那几行报这个世界的一生(事件日志只增不减),"
                    "而退出码只看本次开机以来 —— 一条永远红的检查等于没有这条检查。"
                    " 退出码是「总账」:这一趟里需要处理的项数之和 —— "
                    "长过程一件没丢的世界照样可能退 1(比如这台 Redis 没开 AOF)。"
                    "别拿它的退出码当单项判据,读屏幕上那一行。",
    )
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
    chat.add_argument(
        "--message", "-m", action="append", default=None, dest="messages",
        help="说一句就退,不进 REPL(可重复,按顺序一句一轮)—— 给脚本和子进程用",
    )
    chat.add_argument("--json", action="store_true", dest="as_json",
                      help="回执出成 JSON(只在 --message 下有意义)")
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
        help="改看收件箱:谁「当面」叫住过这个玩家(agent_hail),要 --player",
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
    player_host = player_commands.add_parser(
        "host",
        help="主持人的那一屏:一段场景 + 几个选项 + 自由输入(世界永远先开口)",
    )
    _add_world_args(player_host)
    player_host.add_argument("--player", required=True, help="他的 player_id")
    player_host.add_argument(
        "--ask", action="store_true",
        help="他点了「我该干嘛」—— 第四个开口时刻,受 host.ask_cooldown_ticks 管",
    )
    player_host.add_argument(
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
        "--since-seq", type=int, default=None, dest="since_seq",
        help="从这条 seq 之后接着抹(续跑给上一趟回执里的 resume_seq)。"
             "真抹时不许越过已完成的水位 —— 那会在日志里留一个洞",
    )
    player_erase.add_argument(
        "--limit", type=int, default=None,
        help="这一趟最多看多少条事件(数的是条数,不是 seq 跨度)。"
             "⚠️ 它封住的是改写那一遍,收名字那一遍永远要看全量",
    )
    player_erase.add_argument(
        "--resume", action="store_true",
        help="只把上一趟没做完的接着做完;没有未完成的就什么都不做,"
             "绝不顺手开一趟新的",
    )
    player_erase.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出"
    )

    # ── plugin(3.8.0)──────────────────────────────────────────────────────
    plugin_cmd = sub.add_parser(
        "plugin",
        help="这个世界装着哪些插件(list),以及卸掉一个(remove)",
    )
    plugin_commands = plugin_cmd.add_subparsers(dest="plugin_command")
    plugin_list = plugin_commands.add_parser(
        "list",
        help="装着哪几个:id / 版本 / 事实 / 规律 / 触发器 / 种类 / 边 / 动词 / 装载顺序",
    )
    _add_world_args(plugin_list)
    plugin_list.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出(契约)"
    )
    plugin_remove = plugin_commands.add_parser(
        "remove",
        help="卸掉一个插件:删它全部的事实、可见性行与那一行记录(它的规律与触发器"
             "随记录一起消失)。不带 --yes 只数",
    )
    _add_world_args(plugin_remove)
    plugin_remove.add_argument("plugin", help="要卸的插件 id")
    plugin_remove.add_argument(
        "--yes", action="store_true",
        help="真卸。不带它只数要删多少 —— 和 `world drop` / `player erase` 同一个习惯",
    )
    plugin_remove.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出"
    )

    # ── pack(3.10.0,周更链路 2a-①)──────────────────────────────────────────
    pack_cmd = sub.add_parser(
        "pack",
        help="内容包:往一个「跑着的」世界投一份更新(install),看装了哪几周(list)",
    )
    pack_commands = pack_cmd.add_subparsers(dest="pack_command")
    pack_install = pack_commands.add_parser(
        "install",
        help="把一份带 `pack` 段的 `.cyberworld` 装进这个世界 —— 不重建、不停机、"
             "玩家进度不丢。拍的零点是「这个包落地那天」",
    )
    _add_world_args(pack_install)
    pack_install.add_argument("file", help="那份 `.cyberworld`(必须有 `pack` 段)")
    pack_install.add_argument(
        "--force", action="store_true",
        help="明知有几拍会在下一 tick 一起响掉,也照装。"
             "不带它时那种包「当场拒绝并逐条列出」—— `beat_fired` 是历史,烧掉回不来",
    )
    pack_install.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出(回执)"
    )
    pack_disable = pack_commands.add_parser(
        "disable",
        help="停用一份内容包:它的拍不再响、它带来的新人退场、它开的开关回落。"
             "「不是删除」—— 玩家的记忆里有这一周发生过的事",
    )
    _add_world_args(pack_disable)
    pack_disable.add_argument("pack", help="要停用的内容包 id")
    pack_disable.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出(回执)"
    )
    pack_list = pack_commands.add_parser(
        "list", help="这个世界装了哪几份内容包:id / 版本 / 哪天落地 / 带了什么",
    )
    _add_world_args(pack_list)
    pack_list.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出(契约)"
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
        "--portrait-file", default=None, dest="portrait_file", metavar="PATH",
        help="从文件里读那条立绘 URI(`-` = 标准输入)—— 内嵌的 data: URI 长过 "
             "约 128 KiB 就塞不进 argv 了,那是操作系统的上限,不是引擎的",
    )
    set_card.add_argument(
        "--clear", action="store_true",
        help="把整张卡删掉 —— 「作者说他是背景」和「作者什么也没说」是两件事,"
             "所以它单独一格,而且不许和上面几个一起给",
    )
    set_card.add_argument(
        "--dry-run", action="store_true", help="只报要改成什么,不动库"
    )
    set_card.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出"
    )

    location_cmd = sub.add_parser("location", help="地图数据的维护")
    location_commands = location_cmd.add_subparsers(
        dest="location_command", required=True
    )
    set_image = location_commands.add_parser(
        "set-image",
        help="给一个「已经跑着的世界」里的地点换图(一次一个地点;这是覆盖)",
    )
    _add_world_args(set_image)
    set_image.add_argument("--location", required=True, help="改哪儿(地点 id)")
    # **两格从 `LOCATION_IMAGE_KEYS` 长出来,不在这里再抄一遍名字。** 抄一遍的话,
    # 哪天加第三格,这扇门是唯一不会报错、只是安静地少一个开关的地方。
    for _key in LOCATION_IMAGE_KEYS:
        _flag = "--" + _key.replace("_", "-")
        set_image.add_argument(
            _flag, default=None, dest=_key,
            help=f"{LOCATION_IMAGE_GLOSS[_key]} —— 一条 URI"
                 f"(https / http / data;空串 = 抹掉这一格;"
                 f"≤ {LOCATION_IMAGE_MAX_BYTES // 1024} KiB)",
        )
        set_image.add_argument(
            f"{_flag}-file", default=None, dest=f"{_key}_file", metavar="PATH",
            help=f"从文件里读 {_key} 那条 URI(`-` = 标准输入)—— 内嵌的 data: URI "
                 "长过约 128 KiB 就塞不进 argv 了,那是操作系统的上限,不是引擎的",
        )
    set_image.add_argument(
        "--clear", action="store_true",
        help="把这个地点的「两格图都抹掉」(不动名字、描述、几何 —— 那些归作者层)",
    )
    set_image.add_argument(
        "--dry-run", action="store_true", help="只报要改成什么,不动库"
    )
    set_image.add_argument(
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
        help="这份文件要装进一个「已有」的世界(= 一次编辑):不要求它把名册和地图"
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
        help="这份文件要装进一个「已有」的世界(= 一次编辑):不要求它把名册和地图"
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

    world_setting = world_commands.add_parser(
        "setting",
        help="读 / 改一个「已经跑着的世界」的世界观(不给 --set/--clear 就是只读)",
    )
    _add_world_args(world_setting)
    world_setting.add_argument(
        "--set", default=None, dest="set_text", metavar="TEXT",
        help="换成这段世界观(这是覆盖;热改是权威,重启不会被世界文件盖回去)",
    )
    world_setting.add_argument(
        "--set-file", default=None, dest="set_file", metavar="PATH",
        help="从文件里读那段世界观(`-` = 标准输入)—— 世界观动辄几百上千字,"
             "而 argv 有操作系统的上限,那不是引擎的上限",
    )
    world_setting.add_argument(
        "--clear", action="store_true",
        # ⚠️ 强调用「」不用 `**` —— 屏幕上 `**` 就是两个星号
        # (`test_屏幕上不许出现裸markdown星号`,它当场逮住了这一句)。
        help="抹掉这个世界自己那一行,「回落到引擎内置那份」(不是变成空的);"
             "不许和 --set/--set-file 一起给",
    )
    world_setting.add_argument(
        "--dry-run", action="store_true", help="只报要改成什么,不动库"
    )
    world_setting.add_argument(
        "--json", action="store_true", dest="as_json", help="机器可读输出"
    )

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
    - `world_beat_errors` —— 节拍(3.7.0 起它是作者层的第十二个段)。**新增一种
      开机失败,就必须同一轮里补进这扇门** —— 否则这两条命令又变回"比开机松"的
      那种假绿,而上一轮(3.7.0 的 D29)刚刚为同一种病修过三格。

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
    # 🔴 **插件声明的种类要先并进 `kinds`,和开机那条路一模一样**
    # (2026-08-26 验收 B/C 双复现)。少了这一步,一份**开得起来**的世界
    # 被这两扇门答成「引用不到 kind」退 2 —— 而 tool 把退 2 当红灯,
    # 于是**第一个照着 FOR-STUDIO 写插件的作者,先看到的是一盏假红灯**。
    # 本仓那条老纪律的反面:**开机是权威,比它严是假红、比它松是假绿,
    # 两种都比没有校验器更坏。**
    # ⚠️ **并过插件行的那一份只喂给本体那道闸,检查器表拿的是原件**
    # (2026-08-27 分开的)。理由是它们问的是两个问题:本体那道闸问「这些引用
    # 解析得开吗」,少了插件的种类它会对一个**开得起来**的世界报假红;而
    # `world_plugin_errors` 问的是「作者写的这几条插件立不立得住」,拿并过的那份
    # 去问,`compile_kind_rows` 替每一个借来的名字都造了一行,于是
    # 「动词借的种类根本不存在」这道闸永远查不出东西 —— **一盏什么都没查的绿灯**。
    out: list[str] = list(
        _world_seed_errors(_authored_with_plugin_kinds(authored), complete=complete))
    for check in AUTHORED_LAYER_CHECKS:
        out += check(authored)
    return out


def _authored_with_plugin_kinds(authored: dict[str, Any]) -> dict[str, Any]:
    """作者层 + 插件编译出来的那几行 `kinds`。**离线那两扇门用的那一份。**

    ⚠️ **坏插件在这里一声不吭地掉头** —— 它的错由 `world_plugin_errors` 逐条报,
    在这儿再报一遍就是同一件事两个说法(而两个说法迟早分岔)。
    ⚠️ **出厂插件不进这一份**:它们由 `needs.enabled` 之类的**世界配置**决定装不装,
    而离线这一侧手上根本没有那个世界的配置。今天的出厂插件一个 `kinds` / `verbs`
    都没声明,所以这一格是空的;哪天有了,那扇门要么拿到配置、要么明说它没查。
    """
    entries = authored.get("plugins")
    if not entries:
        return authored
    from anima_world.plugins import (
        PluginError, compile_kind_rows, order_plugins, parse_plugins,
    )

    try:
        plugins = order_plugins(parse_plugins(entries))
        rows = compile_kind_rows(plugins)
    # ⚠️ **只吞 `PluginError`**(2026-08-27 验收 A):`(PluginError, Exception)` 里
    # 第一项是多余的,而第二项会连**将来的**真 bug 一起吞掉 —— 而这个函数是
    # 离线两扇门与开机共用的那一段,吞在这儿的下场是「什么都没查的绿灯」。
    # 坏插件那一族照旧由 `world_plugin_errors` 逐条报,不在这儿说第二遍。
    except PluginError:
        return authored
    if not rows:
        return authored
    merged = dict(authored)
    by_id = {str(row.get("id")): dict(row)
             for row in (merged.get("kinds") or []) if isinstance(row, dict)}
    for row in rows:
        have = by_id.get(str(row["id"]))
        if have is None:
            by_id[str(row["id"])] = row
            continue
        have.setdefault("quantities", {}).update(row.get("quantities") or {})
        have.setdefault("affordances", {}).update(row.get("affordances") or {})
    merged["kinds"] = list(by_id.values())
    return merged


#: 作者层的检查器**一张表**(`_world_seed_errors` 另算 —— 它多一个 `complete=`)。
#:
#: 🔴 **这张表存在的理由只有一条纪律**:*新增一种开机失败,就必须同一轮把它补进
#: 离线那两扇门。* 3.7.0 收节拍时第一版就漏了(收了段没补门,于是 `world check`
#: 对一份**开不了机**的文件照答 `loadable: true`),而同一版的上一个 commit 刚为
#: 同一种假绿修过三格。
#:
#: 从前这里是一串写死的 `+`,于是"忘了加一个"只表现为少了一行 —— **没有一处会红**。
#: 收成一张表之后,加检查器变成往这张表里加一行,而
#: `tests/test_validate_matches_boot.py` 那条通用用例盯着它:三扇门都必须走
#: `authored_layer_errors`,而这张表就是它的全部内容。
AUTHORED_LAYER_CHECKS: tuple[Any, ...] = ()


def world_plugin_errors(authored: dict[str, Any] | None) -> list[str]:
    """作者写下的插件立不立得住 —— **和开机同一份判断**(3.8.0)。

    这是那条纪律的第 N 次落点,写在这儿是因为它每一次都被忘掉一半:
    **新增一种开机失败,就必须同一轮把它补进离线那两扇门。** 3.7.0 收节拍时
    第一版只收了段没补门,于是 `world check` 对一份开不了机的文件照答
    `loadable: true` —— 而同一版的上一个 commit 刚为同一种假绿修过三格。

    ⚠️ **和 `complete` 无关**:插件声明**没有跨引用**(它读的是自己的命名空间与
    `reads` 里点名的那几个,而那几个必须由同一份文件里的别的插件提供)——
    所以一次编辑(`--edit`)里它照查。这一格和 `world_beat_errors` 逐字同构。
    """
    if not authored:
        return []
    entries = authored.get("plugins")
    if entries is None:
        return []
    from anima_world.events import SUBSCRIBABLE_EVENTS
    from anima_world.plugins import (
        PluginError, borrowed_kind_errors, order_plugins, parse_plugins,
    )

    try:
        plugins = parse_plugins(entries, subscribable=SUBSCRIBABLE_EVENTS)
        ordered = order_plugins(plugins)
    except PluginError as exc:
        return list(exc.errors)
    # 🔴 **开机那条路上还有第三处拒绝,而它从前不在这扇门上**(2026-08-27):
    # 动词借的那个种类到底存不存在。它判的是插件与**作者写的 `kinds`** 之间的
    # 引用,所以只有拿到整份作者层才判得动 —— 而这个函数正好拿到了。
    # ⚠️ 用的是**没并过插件行**的那一份 `kinds`(见 `authored_layer_errors` 里
    # 那两行的分工):并过之后借来的名字个个都"存在",这道闸就永远是空的。
    return borrowed_kind_errors(
        ordered,
        {str(row.get("id") or "") for row in (authored.get("kinds") or [])
         if isinstance(row, dict)},
    )


#: 一个 pack 的 id 长什么样。**照 `PLUGIN_ID_PATTERN` 那条先例给一格正则**,
#: 别让消费方猜 —— 只是这一层中文优先(章节名会是「第二周」这种),所以是
#: **把不行的那几样排掉**,不是小写字母白名单(和 `KIND_LOCAL_PATTERN` 同一条)。
#: ⚠️ 不许有空白、`.`、`:`、`/`:它进事件载荷、进 `:packs` 的 field、进 CLI 参数,
#: 而这四样在那三处各有各的含义。
PACK_ID_PATTERN = r"^[^\s.:/]{1,64}$"
_PACK_ID = re.compile(PACK_ID_PATTERN)

#: 一个 `pack` 段认哪几个键。闭集,和 `BEAT_KEYS` / `PLUGIN_KEYS` 同一条纪律:
#: 不认识的键**当场说**,而不是照收然后丢掉(那一族这个仓库收了五轮)。
PACK_KEYS = ("id", "version", "note")


# ── `--world-file` 装进一个**已有**世界时,那几段各自会发生什么 ──────────────
#
# 🔴 **2026-09-02 实测出来的一整族**:一份内容全落在 `beat` / `config` /
# 在册者 `personality` / `world_setting` / 在册者 `memory` 这五段上的第 2 周包,
# 走 `--world-file` 装进一个跑着的世界,**世界一个字都不变**,而
# `world check --edit` 答 `loadable: true, errors: []`、`simulate --world-file` 退 **0**
# —— 屏幕上只有关于 `beat` 的那一句 warning。**同一个 `not persisted` 门后面站着
# 五个人,而只有第一个会出声。**
#
# 这和收件箱 D32 治过的那条(`world import` 对纯作者层包 rc 0 → 2)是同一种病:
# **一句写在日志上的真话,和一盏假绿灯是同一件事** —— 机器读的是退出码。
#
# 🔴 **句子写成常量,是为了让「离线那两扇门说同一句」这件事不靠人自觉。**
# 抄第二遍的那天,两边会先给出不同的措辞,再由某个作者按其中一句去改一个没错的地方。
# ⚠️ 强调用「」不用 `**`:这几句会**原样印在终端上**,而屏幕上 `**` 就是两个星号
# (`test_屏幕上不许出现裸markdown星号` 看不见它们 —— 它只扫 `print()` 实参与 `help=`)。
EDIT_PATH_NOTES: dict[str, str] = {
    # ⚠️ **措辞在 3.10.1 收窄过一次,而那次是被一句「太强的真话」逼的**:
    # 这里原先写「`--world-file` 装不进一个已经有剧情的世界」,而 3.10.1 之后
    # 它只对**新增**的拍成立 —— 同一份文件重开机是舰队上的常态,照常开机。
    # 一句过强的话和一句错的一样贵:照它去改的人会以为自己必须重建世界。
    "beats": (
        "「新增」的拍装不进一个已经有剧情的世界:节拍和 `beat_fired` 那份历史"
        "配对,而一份写着 `day: 0..6` 的包装进一个跑了很久的世界,那几拍会在同一 tick 里"
        "全部烧掉。要给一个「跑着的」世界加剧情,用 "
        "`anima-world pack install <文件>` —— 那条路按内容包记账,拍的零点是"
        "「这个包落地那天」。(同一份文件重开机不受影响:同 id 且内容相同的拍"
        "静默跳过)"
    ),
    "config": (
        "这份文件里作者动过的开关「装不进一个已有的世界」:`config` 只在创世那一刻"
        "落地(它之后是运维台/作者调过的运行参数,拿文件里那份写回去等于每次重启"
        "都把人的调整悄悄撤销一次)。改一个跑着的世界的开关有两条路:"
        "`anima-world config set`,或者把它写进一份内容包走 `pack install`"
    ),
    "world_setting": (
        "这份文件里的世界观「装不进一个已有的世界」:它只在首启那一刻落进 `:prompts`。"
        "改一个跑着的世界的世界观用 `anima-world world setting --set`(3.8.0),"
        "或者把它写进一份内容包走 `pack install`"
    ),
    # ⚠️ 措辞**以条件打头**,不以断言打头:离线那两扇门不知道谁在册,而第一版
    # 写成「这份文件给已经在册的人写了 …」之后,一份只带**新人**的包也会读到那句话
    # —— **一句念不通的拒绝语,和一句错的一样贵**(这个仓库记过好几次)。
    "personality": (
        "这份文件写了 `personality`,而其中「已经在册」的那些人一个字都装不进去:"
        "名册的权威是事件日志,作者层只收名册「之外」的新人(新人照旧带得进来)。"
        "改一个在册的人的人设今天还没有出口(周更批 2a-② 会开)"
    ),
    "memories": (
        "这份文件写了 `memory`,而其中「已经在册」的那些人一条都装不进去:"
        "创世记忆只在创世那条路上折,合并那条只给新人(新人照旧带得进来)。"
        "给一个在册的人补记忆今天还没有出口(周更批 2a-② 会开)"
    ),
    "roster_state": (
        "这份文件给「已经在册」的人写了关系 / 目标 / 随身物品 / 钱,而它们装不进去"
        "——「这一格是有意的」:那几样是这个世界「跑出来的现在」,拿文件里的初值"
        "写回去就是把三十天的交情倒带回创世那一刻"
    ),
}


def edit_path_silent_notes(
    authored: dict[str, Any] | None, *, on_roster: Any = None,
) -> list[str]:
    """一份包走 `--world-file` 装进一个**已有**世界时,哪几段会安静地什么都不做。

    `on_roster` 是目标世界此刻的名册。**开机手上有它,离线那两扇门没有** ——
    所以后者传 `None`,那两格的话就说成条件句(「如果他们已经在册」)。
    这正是 `--edit` 那条「查得动查不动」的分界,和 `_edit_ontology_gap_warnings`
    逐字同一个姿势:**答不出来就说答不出来,别猜一个答案**。

    ⚠️ `beats` 那一格不在这儿 —— 它在 3.10.0 起是**当场拒绝**(退出码 2),
    不是一句话;离线那两扇门照旧只说得出一句(目标世界有没有剧情,它们不知道)。
    """
    if not authored:
        return []
    notes: list[str] = []
    config = authored.get("config")
    if isinstance(config, dict) and config:
        notes.append(f"{EDIT_PATH_NOTES['config']}(这份文件里有 {len(config)} 个)")
    setting = authored.get("world_setting")
    if isinstance(setting, str) and setting.strip():
        notes.append(EDIT_PATH_NOTES["world_setting"])

    def _named(section: str, field: str) -> list[str]:
        out: list[str] = []
        for entry in _seed_entry_dicts(authored, section):
            aid = entry.get("agent_id") if section == "memories" else entry.get("id")
            if not isinstance(aid, str) or not aid:
                continue
            if field is not None and section == "agents" and not str(
                    entry.get(field) or "").strip():
                continue
            if on_roster is None or aid in on_roster:
                out.append(aid)
        return sorted(set(out))

    who = _named("agents", "personality")
    if who:
        notes.append(_roster_note("personality", who, on_roster))
    who = _named("memories", None)
    if who:
        notes.append(_roster_note("memories", who, on_roster))
    state_sections = [s for s in ("relations", "items") if authored.get(s)]
    if state_sections or any(
        entry.get(k) is not None
        for entry in _seed_entry_dicts(authored, "agents")
        for k in ("money", "inventory", "goals")
    ):
        notes.append(EDIT_PATH_NOTES["roster_state"])
    return notes


def _roster_note(key: str, who: list[str], on_roster: Any) -> str:
    """名册那两格的话。**开机点得出名字,离线只说得出条件句。**"""
    names = "、".join(who[:8]) + ("…" if len(who) > 8 else "")
    if on_roster is None:
        return (f"{EDIT_PATH_NOTES[key]}(这份文件里写到 {len(who)} 个人:{names};"
                "他们在不在册,离线这一格答不出来 —— 全是新人的话这一句不适用)")
    return f"{EDIT_PATH_NOTES[key]}(在册而被丢掉的是:{names})"


#: 哪几样东西一出现,这份包就只有 3.10.0 以上装得进去。
#: **按"文件里写了什么"判,不按版本号猜** —— 和消费方那条"按段探测"逐字同构。
PACK_ENGINE_MIN = "3.10.0"


def pack_engine_min_errors(authored: dict[str, Any] | None, manifest: Any) -> list[str]:
    """封皮上的 `engine_min` 和这份包**真的需要**的那一版对不对得上(3.10.0)。

    🔴 **2a-① 验收 C 逮的**:一份 `engine_min: "3.9.0"` 而带着 `pack` 段的包,
    `world check --edit` 说可用、`pack install` 退 0 —— 而它在 3.9.0 上是
    **开不了机的硬失败**(不认识的作者层 `type`)。封皮是**下游照它做判断**的那一格,
    而"作者声称要哪个引擎"和"这份包真的要哪个引擎"从前没有一处对过账。

    ⚠️ **只往上查,不往下查**:写 `4.0.0` 的包这一版装不装得进由别处答
    (`world inspect` 的 `runnable`),那是另一个问题。这里答的是
    **"你声称的那一版根本跑不了你自己写的东西"**。
    """
    if not authored:
        return []
    needs: list[str] = []
    if isinstance(authored.get("pack"), dict):
        needs.append("`pack` 段(作者层第十五个段)")
    for beat in _seed_entry_dicts(authored, "beats"):
        at = (beat.get("trigger") or {}).get("at")
        if isinstance(at, dict) and at.get("since") is not None:
            needs.append("`trigger.at.since`")
            break
    for beat in _seed_entry_dicts(authored, "beats"):
        if beat.get("narrate") is not None:
            needs.append("一条拍上的 `narrate`")
            break
    if not needs:
        return []
    from anima_world.plugins import version_tuple

    declared = str(getattr(manifest, "engine_min", "") or "")
    # ⚠️ **「没写」和「写了一个更低的数」是两件事。**
    # 写了更低的数是一句**可以被证伪的假话** —— 作者声称 3.9.0 跑得了,而它跑不了;
    # 没写只是**没说**(`world inspect` 那一格的语义就是"作者声称要哪个引擎"),
    # 而这份格式从一开始就允许不说。所以前者是错误,后者是警告 ——
    # 把没说也判成错,每一份手写的世界文件都会在这扇门上变红。
    if not declared:
        return []
    if version_tuple(declared) >= version_tuple(PACK_ENGINE_MIN):
        return []
    return [
        f"封皮上写着 `engine_min: {declared}`,而这份包里有 "
        f"{'、'.join(sorted(set(needs)))} —— 那几样 {PACK_ENGINE_MIN} 才有,"
        f"更老的引擎见到它们是「开不了机的硬失败」。把 `engine_min` 写成 "
        f"`{PACK_ENGINE_MIN}`"
    ]


def pack_engine_min_warnings(authored: dict[str, Any] | None, manifest: Any) -> list[str]:
    """封皮上**没写** `engine_min`,而这份包里有只有新引擎才有的东西(3.10.0)。

    没说不是说错,所以这是一句警告 —— 但它照旧要说:一份发出去的包,
    下游拿 `engine_min` 决定给它哪一版引擎,而**空着等于「谁都行」**。
    """
    if not authored or not isinstance(getattr(manifest, "engine_min", None), (str, type(None))):
        return []
    if str(getattr(manifest, "engine_min", "") or ""):
        return []
    if pack_engine_min_errors(authored, _AnyEngineMin(PACK_ENGINE_MIN)) != []:
        return []       # 逻辑上到不了,留着让下一个人改坏时当场看见
    needs_new = bool(
        isinstance(authored.get("pack"), dict)
        or any((b.get("trigger") or {}).get("at", {}).get("since") is not None
               or b.get("narrate") is not None
               for b in _seed_entry_dicts(authored, "beats")
               if isinstance((b.get("trigger") or {}).get("at"), dict)
               or b.get("narrate") is not None)
    )
    if not needs_new:
        return []
    return [
        f"封皮上没写 `engine_min`,而这份包里有只有 {PACK_ENGINE_MIN} 才认的东西 —— "
        f"空着等于「谁都行」,而更老的引擎见到它们是「开不了机的硬失败」。"
        f"写上 `engine_min: {PACK_ENGINE_MIN}`"
    ]


class _AnyEngineMin:
    """给 `pack_engine_min_errors` 当"封皮"用的最小替身。"""

    def __init__(self, engine_min: str) -> None:
        self.engine_min = engine_min


def expired_beats(beats: Any, *, day: int, pack_day: int) -> list[str]:
    """这几拍装进今天这个世界,哪几条会在**下一 tick 一起响掉**(3.10.0)。

    只有写了 `since: "world"` 的拍会撞上这件事:它的零点是世界第 0 天,
    而 `trigger.at` 是「不早于」—— 一份 `day: 0..6` 的包装进第 40 天的世界,
    七拍同一 tick 全响,`beat_fired` 是历史、烧掉就再也回不来。

    ⚠️ **`for_each: player` 的拍算不出来,所以不算**:它的零点是
    `max(包落地那天, 他入场那天)`,而"他"是谁在装包这一刻还不知道 ——
    **算不出来就别猜一个答案**(和离线两扇门那条「查得动查不动」逐字同一条);
    这一格由文档那两张 `day` 表说清楚。
    """
    from anima_world.beats import AT_SINCE, is_per_player

    out: list[str] = []
    for beat in (beats or ()):
        if not isinstance(beat, dict) or is_per_player(beat):
            continue
        at = (beat.get("trigger") or {}).get("at")
        if not isinstance(at, dict):
            continue
        if (at.get("since") or AT_SINCE[0]) != "world":
            continue
        try:
            due = int(at.get("day", 0))
        except (TypeError, ValueError):
            continue
        if due <= int(day):
            out.append(str(beat.get("id")))
    return out


def world_pack_errors(authored: dict[str, Any] | None) -> list[str]:
    """作者写下的那个 `pack` 段立不立得住(3.10.0)。

    **一份文件最多一个 pack**(段是"对象型",列表在这一层根本表达不出来),
    所以这道闸只查那一份的形状:id / version 必填,`note` 可选,别的键不认。

    ⚠️ **加一种新的开机失败,就必须同一轮补进离线那两扇门** —— 这条纪律
    3.7.0 收 `beat` 段时漏过一次(收了段没补门,`world check` 对一份开不了机的
    文件照答 `loadable: true`),所以这个函数和它在 `AUTHORED_LAYER_CHECKS`
    里的那一行是**同一个 commit 的两半**。

    ⚠️ **没写 `pack` 的文件这一层整个缺席** —— 和 `beats` / `kinds` / perception
    逐字同构:老包一个字不用改,`install_pack` 那条路才要求它。
    """
    if not authored:
        return []
    body = authored.get("pack")
    if body is None:
        return []
    if not isinstance(body, dict):
        return [f"pack:body 必须是一个对象(收到 {type(body).__name__})"]
    errors: list[str] = []
    unknown = sorted(set(body) - set(PACK_KEYS))
    if unknown:
        # ⚠️ **屏幕上不印 Python 的 list repr**(那串引号和方括号是给机器看的)。
        errors.append(
            f"pack:不认识的键 {'、'.join(unknown)} —— 一个内容包只有 "
            f"{'、'.join(PACK_KEYS)}"
            "(问 `contract --json` 的 `packs.pack_keys`,别照文档记一份清单)"
        )
    pack_id = body.get("id")
    if not isinstance(pack_id, str) or not _PACK_ID.match(pack_id):
        errors.append(
            f"pack.id {pack_id!r} 不合 `{PACK_ID_PATTERN}` —— 1~64 个字符,"
            "不许有空白 / `.` / `:` / `/`(它要同时当事件载荷、Redis 的 field "
            "和一个 CLI 参数用)"
        )
    version = body.get("version")
    if not isinstance(version, str) or not version.strip():
        # ⚠️ **「没写」印成「没写」,不是印一个 `None`** —— 屏幕上的字是给人读的,
        # 而 `pack.version None` 会让人去找一个叫 None 的东西。
        shown = "(没写)" if version is None else repr(version)
        errors.append(
            f"pack.version {shown} 要是一段非空文本(例 `\"1.0.0\"`)—— "
            "没有版本号就说不出「这一周装的是第几版」,而那是升级唯一的判据"
        )
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        errors.append(f"pack.note 要是一段文本(收到 {type(note).__name__})")
    return errors


def world_beat_errors(authored: dict[str, Any] | None) -> list[str]:
    """作者写下的节拍立不立得住 —— **和开机同一份判断,不是第二份**(3.7.0)。

    节拍 3.7.0 起进得了 `.cyberworld`(`AUTHOR_SECTIONS` 的第十二个段),于是它
    **多出一种开机失败**:`build_serve_scheduler` 在第一次写之前调
    `BeatScript.from_data`,坏脚本当场开不了机。这里调的是**同一个函数** ——
    另写一份判断的那天,两边会先给出不同的答案,再由某个人在一个坏掉的世界上发现。

    ⚠️ **这一格和 `complete` 无关:节拍没有跨引用。** 一次编辑(`--edit`)里它照查 ——
    `op` 拼错、`after` 指着一个不存在的拍、id 重复,这几样在包**自己肚子里**就查得动
    (`world check --edit` 的分界是"查得动查不动",不是"这是不是一个完整世界")。

    ⚠️ **没写 `beats` 的世界这一层整个缺席**,和 `kinds` / perception 逐字同构 ——
    所以是 `is None` 而不是 falsy:一份写着 `"beats": []` 的文件是"作者说这个世界
    没有剧情",它和"这份文件没提过节拍"在这道闸上碰巧同一个答案,但别把两者写成
    同一个判断,下一个人会照着推错。
    """
    if not authored:
        return []
    beats = authored.get("beats")
    if beats is None:
        return []
    from anima_world.beats import BeatScript, BeatScriptError

    try:
        BeatScript.from_data({"beats": beats})
    except BeatScriptError as exc:
        return list(exc.errors)
    return []


def _parsed_plugins_or_none(bodies: Any) -> list[Any] | None:
    """这几条插件声明,解析好的。**坏插件在这儿一声不吭地掉头。**

    它的错由 `world_plugin_errors`(离线)/ `_install_plugins`(开机)逐条报 ——
    在这儿再报一遍就是同一件事两个说法,而两个说法迟早分岔
    (`_authored_with_plugin_kinds` 那条逐字同款)。

    ⚠️ **`subscribable=` 要和 `world_plugin_errors` 传的一模一样**:传窄了的话
    一份那扇门收得下的声明会在这儿"解析失败",于是边那道闸**静静地整个跳过** ——
    而跳过的样子和"查过了没问题"在屏幕上是同一个。
    """
    from anima_world.events import SUBSCRIBABLE_EVENTS
    from anima_world.plugins import PluginError, order_plugins, parse_plugins

    try:
        return list(order_plugins(parse_plugins(
            list(bodies or []), subscribable=SUBSCRIBABLE_EVENTS)))
    except PluginError:
        return None


def _edge_layer_verdict(
    authored: dict[str, Any] | None, plugins: list[Any] | None, *,
    complete_namespaces: bool = False,
) -> tuple[list[str], list[str]]:
    """作者层那几条边的判词 `(errors, warnings)` —— **开机与离线共用这一份**。

    唯一的分岔是**手上那份插件名单是从哪儿来的**:离线只有这份文件里的
    `plugin` 记录,开机是「出厂 + 库里 + 文件」三个来源合并之后的那一份
    (`_plugin_bodies`)。⚠️ **名单和它要判的那份数据必须来自同一次合并** ——
    2026-08-28 那条回归(「任何带 `kinds` 的增量编辑都开不了机」)就是喂了全集、
    判了局部,所以这个函数**只收已经合并好的名单**,自己一次都不去合并。

    🔴 **`complete_namespaces` 必须跟着那份名单一起传**(2026-08-31 验收 A 的 P1)。
    名单从哪儿来,和"名单里没有算不算数",是**同一件事的两半** —— 第一版只传了
    前一半,于是开机拿着全集却照着"我可能没查全"的规矩放行:一个插件名打错一个
    字母的完整世界文件**开机成功、边真的落库**,而屏幕上印着的正是那句
    「那种文件真开机时会当场红」。**两半分开传,就是给它们分岔的机会**;
    这儿绑成一个调用点,是让下一个人没法只改一半。
    """
    edges = (authored or {}).get("edges")
    if not edges:
        return [], []
    if plugins is None:      # 坏插件 —— 归 `world_plugin_errors` 报,这儿闭嘴
        return [], []
    from anima_world.plugins import authored_edge_errors

    return authored_edge_errors(
        edges, plugins, factory_ids=FACTORY_PLUGINS,
        namespace_list_is_complete=complete_namespaces)


def world_edge_errors(authored: dict[str, Any] | None) -> list[str]:
    """作者层种下的那几条边立不立得住 —— **和开机同一份判断**(3.8.0,收件箱 D44)。

    边是作者层的**第十四个段**,于是它**多出一种开机失败**,而这个仓库的老规矩是:
    *新增一种开机失败,就必须同一轮把它补进离线那两扇门。* 3.7.0 收节拍时第一版
    只收了段没补门,`world check` 对一份开不了机的文件照答 `loadable: true` ——
    这一行就是那条纪律的第 N 次落点。

    ⚠️ **和 `complete` 无关,而分界由数据自己给**(理由写在
    `plugins.authored_edge_errors` 的 docstring 里):写得对不对永远查得动;
    「这种边这个世界有没有」在**这份文件声明过那个插件**时查得动、否则查不动,
    而查不动的那一格由 `_authored_edge_warnings` **说出来**,不是假装查过了。

    ⚠️ **没写 `edges` 的世界这一层整个缺席** —— 和 `beats` / `kinds` 逐字同构。
    """
    if not authored:
        return []
    return _edge_layer_verdict(
        authored, _parsed_plugins_or_none(authored.get("plugins")))[0]


def _authored_edge_warnings(authored: dict[str, Any] | None) -> list[str]:
    """边那一段里**离线答不了**的那几格。三条路共用(两扇离线门 + 开机)。

    只在真有那一格时才说 —— **误报够多次的警告等于没有警告**
    (和 `_edit_ontology_gap_warnings` 同一条)。
    """
    if not authored:
        return []
    return _edge_layer_verdict(
        authored, _parsed_plugins_or_none(authored.get("plugins")))[1]


def _register_authored_layer_checks() -> None:
    """把那几个检查器填进 `AUTHORED_LAYER_CHECKS`。**加一个就往这儿加一行。**"""
    global AUTHORED_LAYER_CHECKS
    AUTHORED_LAYER_CHECKS = (
        visibility_band_errors,
        world_card_errors,
        world_location_media_errors,
        world_beat_errors,
        world_plugin_errors,
        world_edge_errors,
        world_pack_errors,
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


def _edit_ontology_gap_warnings(authored: dict[str, Any] | None) -> list[str]:
    """一次编辑里,**具体是哪一格离线查不了** —— 只在真有那一格时才说。

    今天只有一条:这份包没重声明 `agent` 种类,而它的能力里读了 `me_*`。她的量表
    在目标世界里,所以"`me_力气` 声明过没有"这一句离线答不了。**说出来而不是
    假装查过了** —— 那正是 `--edit` 上一版那句 warning 的病(见
    `_package_only_ontology_errors`)。没有这一格时一个字都不说:
    **误报够多次的警告等于没有警告**(和 `unreachable_requirements` 同一条)。
    """
    if not authored:
        return []
    rows = [dict(k) for k in (authored.get("kinds") or []) if isinstance(k, dict)]
    if not rows or any(str(row.get("id") or "") == "agent" for row in rows):
        return []
    names = sorted(_me_names_used(rows))
    if not names:
        return []
    return [
        f"这份包没重声明 `agent` 种类,而能力里读了 {['me_' + n for n in names]} ——"
        "「她身上声明过这个量吗」离线答不了(她的量表在目标世界里),这一格跳过了。"
        "要查它,用 `world check <文件> --edit` 连着这份包问;要真装进一个跑着的"
        "世界,用 `pack install`(`--world-file` 是创世 / 离线编辑那条路)"
    ]


def _edit_stock_kind_gap_warnings(authored: dict[str, Any] | None) -> list[str]:
    """一次编辑里,`stocks` 的哪几行**离线查不了量名** —— 照 `me_X` 那条先例说出来。

    `_undeclared_stock_names` 的判据是 owner 所属的**种类**声明过哪些量,而一份编辑包
    完全可以只改种类 A、却顺手给种类 B 的某个实例写个初值 —— B 的声明在目标世界里。
    那时 `declared_quantities` 答空,这一行**跳过**。

    跳过是对的(硬查就是假红),**不说才是错的**:`--edit` 那句总结现在写着"量名两支
    都查了",而这一行恰恰没查。**一句说得比做到的宽的话,和一盏假绿灯是同一件事** ——
    人正是拿它去决定不再自己查的,这一格已经吃过一次亏了。

    只在真有这种行时说,而且只说一句:一句总在响的警告等于没有警告。
    """
    if not authored:
        return []
    rows = [dict(k) for k in (authored.get("kinds") or []) if isinstance(k, dict)]
    declared_kinds = {str(row.get("id") or "") for row in rows}
    if not declared_kinds:
        # 一个 `kinds` 都没写的包,这一层整个缺席("声明本身就是开关"),
        # `_undeclared_stock_names` 本来就一个字不说 —— 这里也不该多嘴。
        return []
    from anima_world.ontology import owner_kind

    unchecked: list[str] = []
    for entry in _seed_entry_dicts(authored, "stocks"):
        owner = str(entry.get("owner") or "").strip()
        if not owner or not isinstance(entry.get("values"), dict):
            continue
        if owner_kind(owner) not in declared_kinds:
            unchecked.append(owner)
    if not unchecked:
        return []
    shown = sorted(set(unchecked))
    return [
        f"这是一次编辑(--edit),而 `stocks` 里有 {len(shown)} 个 owner 的种类"
        f"这份包没声明({'、'.join(shown[:5])}{'…' if len(shown) > 5 else ''})——"
        "「这几个量名它所属的种类声明过吗」离线答不了(那份声明在目标世界里),"
        "这几行的量名跳过了。要查它,用 `world check <文件> --edit`;要真装进一个"
        "跑着的世界,用 `pack install`"
    ]


def _edit_location_media_warnings(authored: dict[str, Any] | None) -> list[str]:
    """一次编辑(`--edit`)里的图**装不进已经存在的地点** —— 离线这两扇门也得说。

    此前这句话只活在 `redis_state._warn_skipped_location_media` 的 `logger.warning`
    里,也就是**只有真开机的人看得到**。而 `--edit` 这条路的用法恰恰是"先离线验一遍,
    再拿去装":作者拿到一个绿灯,装上去图没了,两边日志都干净 —— 一个什么都没说的
    绿灯,正是这两个校验出口存在的理由本身。

    **只在文件里真的写了图的时候说**(说了也只说一句):一句总在响的警告等于没有
    警告,而这条的收件人很具体 —— 拿着一份补了图的编辑包的那个人。

    ⚠️ 它是**警告不是错误**,而且措辞必须带上那个条件("目标世界里**已经有的**那些
    地点"):校验器手上没有目标世界,所以"这个地点在不在那边"它答不出。写成一句
    光秃秃的"图装不进去"是把猜测说成判决 —— 给一个**新**地点配图这条路完全走得通。
    """
    if not authored:
        return []
    entries = authored.get("locations")
    if not isinstance(entries, list):
        return []
    named = [
        str(entry.get("id") or "?")
        for entry in entries
        if isinstance(entry, dict) and any(entry.get(key) for key in LOCATION_IMAGE_KEYS)
    ]
    if not named:
        return []
    return [
        f"这是一次编辑(--edit),而其中 {len(named)} 个地点写了图"
        f"({'、'.join(named[:5])}{'…' if len(named) > 5 else ''})—— "
        "「目标世界里已经有的那些地点,这几格装不进去」:作者层合并按地点 id 整条"
        "跳过已有地点(整行合并会把这个世界跑出来的名字和描述倒带回创世那天)。"
        "只有目标世界里还没有的地点才会带着图落地;给一个已经在册的地点补图走 "
        "`anima-world location set-image --location <id> --map-image <URI>`"
        "(3.4.0 起,`contract --json` 的 seed.location_image_write_command 报得出它)"
    ]


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
        # ⚠️ **开机这一侧也说** —— 三扇门对同一份文件说同一句话,warning 那一半
        # 和 errors 那一半是同一条纪律:只有 `validate world` 看得到的警告,
        # 在托管环境里等于没有(那儿没有人会去跑那条命令)。
        for problem in ((_authored_media_warnings(authored)
                         + _authored_uncreatable_edges(authored)
                         + _authored_edge_warnings(authored))
                        if authored else []):
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
        RedisBeatsStore, RedisOntologyStore, RedisReflectionStore,
        RedisEdgeStore, RedisPluginStore, RedisRulesStore, RedisStockStore,
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
    beats_store = RedisBeatsStore(redis, world_id)
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
    # 🔴 **插件声明的种类与动词,在这里并进作者层的 `kinds`。**
    #
    # 它们从此就是**普普通通的本体种类** —— 于是下面那条已经在跑的路
    # (预检 → 播种 → 装载 → `_apply_ontology`)一个字都不用改,而**出生自检、
    # 「生成必须要代价」、`prompt.budget`、可见性、拒绝语、`resolve` 的跨引用闸**
    # 一件都不用重写。给插件另起一套的下场是那几件要么重写一遍、要么悄悄不生效,
    # 而"悄悄不生效"正是这个仓库最怕的形状。
    #
    # **必须排在预检之前**:预检验的就是这份 `kinds`,而插件的种类要和作者写的
    # 那些一起过同一道闸 —— 晚一步的话,一个引用不到的 `spawn.kind` 会绕过预检,
    # 到 `_load_ontology` 那儿才炸,而那时表已经写过几张了。
    if seed_author_layer:
        world_seed = _merge_plugin_kinds(
            config_store, RedisPluginStore(redis, world_id), world_seed)

    # 🔴 **这一趟到底有哪几个插件命名空间:文件那份 + 库里那份 + 出厂那几个**
    # (2026-08-28 修回归)。喂给本体那一层的 `kinds` 是**库合并之后的全集**
    # (`_union_by_id`),而名单从前只取文件那一份 —— 于是一个建好的插件世界,
    # 之后做**任何**带 `kinds` 的增量编辑(哪怕新种类和插件毫无关系)就开不了机:
    # 报的是作者**没碰过**的那一行,还断言「装着的是(一个都没有)」,
    # 而那个插件明明就在库里。**两份东西必须来自同一次合并**,这正是
    # `_seed_ontology` 自己 docstring 反对的那件事(喂全集、判局部)。
    boot_plugin_bodies = _plugin_bodies(
        config_store, RedisPluginStore(redis, world_id), world_seed)
    boot_namespaces = tuple(
        str(body.get("id") or "")
        for body in boot_plugin_bodies
        if str(body.get("id") or "")
    )

    if seed_author_layer and world_seed and world_seed.get("kinds"):
        _precheck_ontology(world_seed, rules_store, location_store, economy_store,
                           ontology_store if merge_author else None,
                           namespaces=boot_namespaces)

    # ── 边:作者层的第十四个段(3.8.0,收件箱 D44)────────────────────────────
    #
    # **先验,再写** —— 和上面那两条(本体、节拍)逐字同一条。真正种边要等插件
    # 装完(`edge_types` 那张表是 `link` 查约束读的),而那时地图、规律、量表
    # **已经写过好几张了**;一条坏边留下的就是"装了一半的世界"。所以判断提到
    # 这里,验不过一个字都不写。
    #
    # 🔴 **这一趟的插件名单是三个来源合并后的那一份**(`boot_plugin_bodies`),
    # 不是文件里那几行 —— 一次编辑最常见的形状就是"只带几条边",而它要连的那种边
    # 声明在**库里**。名单和它要判的那份数据来自同一次合并,是 2026-08-28 那条
    # 回归换来的规矩。离线两扇门手上只有文件那一份,所以它们对这种包**明说答不了**,
    # 而不是猜一个答案(`_authored_edge_warnings`)。
    if seed_author_layer and world_seed and world_seed.get("edges"):
        edge_errors, edge_warnings = _edge_layer_verdict(
            world_seed, _parsed_plugins_or_none(boot_plugin_bodies),
            # **开机手上是全集,所以它答得出「这个世界里有没有这个插件」。**
            complete_namespaces=True)
        for problem in edge_warnings:
            logger.warning("%s", problem)
        if edge_errors:
            raise WorldSeedError(edge_errors)

    # ── 节拍:作者层的第十二个段(3.7.0,看板 D1)──────────────────────────
    #
    # **先验,再写** —— 和上面那条逐字同一条:`BeatScript.from_data` 是加载期的
    # 严格校验器(坏脚本当场报错,一次列全),而它在这里跑,所以一份坏节拍不会
    # 留下一个装了一半的世界。
    #
    # **`--beats` 赢这一趟,但不写库。** 命令行上指名的那个文件是一次**明示的
    # 覆盖**(试炼、调试都靠它),而库里那份是这个世界自己的剧情。让它写回去的话,
    # 一次试炼就会把作者的剧情换掉,而且不报错。
    if seed_author_layer and world_seed and world_seed.get("beats"):
        # 验一遍再落库(`from_data` 会抛 `BeatScriptError`,和坏 `kinds` 同一条路),
        # 并且**拿它验过的那一份去播**。
        # ⚠️ 这里一度写成 `[dict(b) for b in … if isinstance(b, dict)]` —— 那个
        # `isinstance` 过滤是**在校验之前把不是对象的那几拍安静扔掉**,于是一份
        # 第三拍写成一个字符串的剧本会开机成功、少一拍,日志干净。少装半份剧情
        # 和一拍不响是同一种病,而这一版更难发现(它连"没响"都不算——那一拍从来
        # 就没进过这个世界)。
        authored_script = BeatScript.from_data({"beats": world_seed["beats"]})
        planted = beats_store.seed(authored_script.beats)
        if planted:
            logger.info("装进 %d 拍作者写的节拍", planted)
        else:
            # 🆕 3.10.1:**逐拍比,不是「播下去了没有」。**
            #
            # 🔴 上一版这里的判据是 `planted == 0`,而 `seed()` 的语义是「空的
            # 时候才播」—— 于是**「同一份文件第二次开机」和「一份带着新剧情的包」
            # 在它眼里长得一模一样**。而舰队每次开机都带 `--world-file`:
            # 一个装过剧情的世界**第二次开机起再也起不来了**(2026-09-02 线上
            # 龙族撞上,platform 已回滚)。
            #
            # **拒绝那条本身是对的,错的是它问的问题。** 3.10.0 立它的理由逐字
            # 仍然成立:一句写在日志上的真话,和一盏假绿灯是同一件事,而**机器读
            # 的是退出码**。所以"新增的拍"照旧 rc 2,只是问法换成了逐拍比。
            #
            # ⚠️ **拒绝仍然在这里,因为这里还什么都没写**(地图 / 规律 / 本体都排
            # 在它后面)—— 「坏声明一个字都不写」那条纪律在这一格的落法。
            same, changed, added = split_against_stored(
                authored_script.beats, beats_store.definitions())
            if added:
                raise WorldSeedError([
                    f"这个世界已经有 {len(beats_store)} 拍剧情,而这份文件里有 "
                    f"{len(added)} 拍是新的({'、'.join(added[:5])}"
                    f"{' 等' if len(added) > 5 else ''})。" + EDIT_PATH_NOTES["beats"]
                ])
            if changed:
                # **说一句,但不拒绝开机**:库里那份说了算(`:beats` 那条「之后
                # 这里的行说了算」的契约),而作者需要知道他的改动没生效 ——
                # 一次静默的"改了没生效"正是这一族最贵的错法。
                logger.warning(
                    "这份文件里有 %d 拍和库里同 id 而内容不同(%s):"
                    "「库里那份说了算」,这次开机不改它们。要改已经发出去的那一拍,"
                    "今天还没有出口 —— 剧情和 `beat_fired` 那份历史是按 id 配对的",
                    len(changed), "、".join(changed[:5]) + (" 等" if len(changed) > 5 else ""),
                )
            elif same:
                # 舰队上的常态:同一份文件又开了一次机。**一句话,而且不像出了事。**
                logger.info("这份文件的 %d 拍剧情已经在库里了,这次开机不重复装", len(same))
            # 走到这儿 = 没有新增的拍 → **开机继续**,rc 0。
    if beat_script is None and len(beats_store):
        # **首启自动带** —— 这一条就是 D1 的另一半。没有它,节拍进得了世界文件
        # 却仍然要靠 `--beats` 才响,而舰队上没有任何一条路会去传那个参数:
        # 一拍都不响,零报错。
        #
        # 🔴 **`stored=True`:库里那几拍按宽容判**(3.10.0,2a-① 验收 B)。
        # 3.10.0 给 `trigger` / `trigger.at` 加了闭集,而这一行会把库里存量的拍
        # **重验一遍** —— 于是一个 3.9.0 上跑得好好的世界(那一版这两层一个键都
        # 不查)换上 3.10.0 就 `BOOT FAILED`。**一次收紧不许把已经发出去的世界
        # 锁在门外**;新文件 / 新内容包照旧严格。
        beat_script = BeatScript.from_data(
            {"beats": beats_store.definitions()}, stored=True)
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
    from anima_world.plugins import PluginError

    plugin_store = RedisPluginStore(redis, world_id)
    scheduler.plugin_store = plugin_store
    scheduler.edge_store = RedisEdgeStore(redis, world_id)
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
            ontology_store, world_seed, fresh_world=seed_author_layer,
            merge=merge_author, namespaces=boot_namespaces)
        # `warn=True` 全仓库只有这一处:装载走一次,拿到的又是**这个世界真正在跑
        # 的那份**规律(播种那份可能根本没落库,预检那份还没落库)。
        scheduler.world_rules = _load_world_rules(
            rules_store, warn=True,
            ticks_per_day=max(1, 1440 // max(1, scheduler._minutes_per_tick())))
        # 本体的解析在规律之后:它要拿规律去查引用。声明过种类的世界从此走闸,
        # 没声明的照旧走警告 —— 那条警告只对后者还有意义。
        scheduler.ontology_store = ontology_store
        scheduler.ontology = _load_ontology(
            ontology_store, scheduler.world_rules, location_store, economy_store,
            # ⚠️ **库里那份 + 这次开机手上这份,两个来源都要**(第三波 A3):
            # 插件是在本体之后才装的(`_install_plugins` 在下面),所以**创世那一趟
            # 库里还是空的** —— 只认库里那份的话,一个带插件的新世界第一次开机
            # 就会在本体那一层被判成"没有哪个插件叫 qi",而第二次开机又好了。
            namespaces=boot_namespaces,
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
                            tick=scheduler.clock, redeclare_kinds=redeclared_kinds,
                            rules=scheduler.world_rules)
        # 插件(3.8.0)。**排在本体之后**:插件的事实挂在 bearer 身上,而"这个世界
        # 有哪些实例"是本体答的;而且它要拿 `visibility_store` 写自己那几行镜像,
        # 那张表刚被 `_apply_ontology` 对齐过。
        # `PluginError` 和 `OntologyError` / `WorldSeedError` 同一类:**作者写错了
        # 东西**,而作者看到的该是那几行中文,不是一段 Python 堆栈。
        # (2026-08-26 验收 C:一次被拒的降级,屏幕先甩 `Traceback …` 才轮到中文。)
        # 这里不吞它 —— 换成 `WorldSeedError`,那是开机路上已经有人接的那一种。
        try:
            _install_plugins(
                scheduler, plugin_store, stock_store, visibility_store, location_store,
                world_seed if seed_author_layer else None,
            )
        except PluginError as exc:
            raise WorldSeedError(list(exc.errors)) from None
        # 边(3.8.0,收件箱 D44)。**排在插件之后**:`link` 那一刻查 `exclusive`、
        # 填声明过的事实默认值,读的都是 `_install_plugins` 刚登记的 `edge_types`。
        if seed_author_layer:
            _seed_edges(scheduler, world_seed, merge=merge_author)
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
        if isinstance(world_seed, dict) and world_seed.get("pack"):
            # 🆕 3.10.0:**`--world-file` 不按内容包装。**
            # 它是创世 / 离线编辑那条路;把一份带身份的包按"编辑"装进去,身份就
            # 丢了(以后说不清那几拍是哪一周加的),而**事后补不回来**。
            logger.warning(
                "这份文件带着内容包的身份(pack %r)—— 而 `--world-file` 是"
                "创世 / 离线编辑那条路,**它不按内容包装**:身份不会登记、"
                "拍的零点还是世界第 0 天。要把它当第几周的更新投进这个世界,"
                "用 `anima-world pack install <文件>`(或宿主的 `install_pack`)。",
                str((world_seed.get("pack") or {}).get("id") or "?"),
            )
        # 🆕 3.10.0:**那四段安静地什么都不做,从今天起当场说出来。**
        # 名册手上有,所以这几句点得出名字 —— 离线那两扇门只说得出条件句。
        for note in edit_path_silent_notes(
            world_seed, on_roster=set(scheduler._memory_projection.agents),
        ):
            logger.warning("%s", note)
        logger.info(
            "--world-file %s 装进了一个「已有」的世界 %r(%d 条事件)—— 这是一次编辑:"
            "同名的「声明」(kinds / rules)照文件里这份重写,"
            "而「状态」(名册、量、钱、位置、记忆)只填缺不覆盖 —— "
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
            # 🆕 3.10.0:**创世那一趟的 pack 落在世界第 0 天。**
            #
            # 同一份文件既可能当创世用、也可能当一份包装进一个跑着的世界,而作者
            # 写它的时候不知道是哪一种。让创世也登记一次,`since: "pack"` 于是在
            # 两种用法上都成立(创世那一趟它等于「世界第 0 天」,本来就该是那样)
            # —— **没有特例,就没有一条只在其中一种用法上对的语义。**
            _record_pack_installed(event_log, world_seed, day=0, tick=0)
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
        # 🆕 3.8.0 第 2 期 2b:**投影式事实的物化视图,开机重建一次。**
        #
        # 它必须排在这儿 —— 折叠端(`reset_projection` / `Scheduler.__init__` 的
        # 那一次重放)刚刚把那一串 delta 折完,而量表里那个数只是视图。少了这一趟,
        # "换个进程读到的值"和"日志折出来的值"会各说各话,而两边都不报错:
        # 跑着的世界照旧对(运行期写视图),只有**重开的那一刻**悄悄倒带。
        # ⚠️ 它同时是 `forget_player` 的下半场:折叠端把他那一行折掉了,
        # 视图上那个数得跟着归零,否则"他走了"这件事重开一次就自己撤销。
        if scheduler.projected_facts:
            # ⚠️ **有 `sources` 的话要重折一遍**:`Scheduler.__init__` 那次重放
            # 跑在插件装上**之前**,那时注册表还是空的,于是那几条 `payment`
            # 一条都没被认成 delta。重折是设计 §9.3 写死的代价之一
            # (「projected 事实开机要重放」),这儿只是把它落到实处。
            if getattr(scheduler._memory_projection, "fact_sources", None):
                scheduler.reset_projection(persisted)
            restored = scheduler._materialize_projected_facts()
            if restored:
                logger.debug("投影式事实的物化视图重建了 %d 格", restored)
        # 🆕 2e:邀请那几条边也是**投影的物化视图**,开机重建一趟。
        # 少了它,一个丢了边(或者从别的前缀重放出来)的世界里那几份邀请
        # **永远不会过期** —— 而清单上会一直挂着,「还剩几拍」一直数下去。
        scheduler.rebuild_invitation_edges()
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



def _undeclared_stock_names(
    world_seed: dict[str, Any] | None, ontology: Any,
) -> list[str]:
    """`stocks` 段里那些"它所属的种类没声明过"的量名。**两处共用这一份**。

    两处是 `_precheck_ontology`(写第一张表之前)与 `_seed_stocks`(真播的时候)。
    收成一个函数而不是各写一遍,理由和 `_precheck_ontology` 自己的 docstring 逐字
    相同:**两份判断迟早给出不同答案,而那种不一致会表现成"预检说没问题,开机还是
    失败"** —— 而这一格 2026-08-21 之前正是这个样子的镜像:预检**根本没查**,
    于是 `validate world` 说绿、开机 `OntologyError`(看板 D29 同族)。

    **没声明 `kinds` 的世界这里一个字都不说**(`declared_quantities` 回空):
    owner 和量名在那种世界里都是任意字符串,引擎无从判断 `树髙` 是笔误还是新造的量
    —— 和 `_load_ontology` 的"声明本身就是开关"逐字同构。
    """
    out: list[str] = []
    if world_seed is None or ontology is None:
        return out
    for index, entry in enumerate(_seed_entry_dicts(world_seed, "stocks")):
        owner = str(entry.get("owner") or "").strip()
        values = entry.get("values")
        if not owner or not isinstance(values, dict):
            # 形状读不懂那一档归 `_seed_stocks` / 作者层 schema 说,不在这儿重复。
            continue
        declared = ontology.declared_quantities(owner)
        if not declared:
            continue
        for key in values:
            name = str(key)
            if name not in declared:
                out.append(
                    f"stocks[{index}] 给 {owner} 写了「{name}」,而它所属的种类没有"
                    f"声明过这个量;声明过的是 {sorted(declared)}"
                )
    return out


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
    # **拼错的量名那一档由 `_undeclared_stock_names` 一处说** —— 预检也调它,
    # 所以声明了 `kinds` 的世界走到这儿时它已经答过一遍了(这里再答一遍不会有
    # 第二种说法,因为句子是同一份)。
    problems: list[str] = _undeclared_stock_names(world_seed, ontology)
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
    out: list[str] = []
    entries = (authored or {}).get("rules") if isinstance(authored, dict) else None
    if entries:
        try:
            out += list(drift_warnings(parse_rules(entries)))
        except Exception:  # noqa: BLE001 - 坏规律的错由 world_seed_errors 负责报
            pass
    # 🆕 **插件的规律也要被这句话覆盖**(3.8.0,2026-08-27 第二波 ④)。
    #
    # 调度台那一趟量出来的:`onair.淡忘`(`every: {"days": 1}`,`onair.人气 - 1`)
    # 和晚潮作者层那条 `梅雨` 是**同一种写法**,而引擎对后者说了三遍、对前者
    # 一个字都没有。一条只覆盖一半写法的 lint,比没有这条 lint 更难查:
    # 作者会以为"引擎没说 = 我这条没问题"。
    out += _authored_plugin_drift_warnings(authored)
    return out


def _authored_plugin_drift_warnings(authored: Any) -> list[str]:
    """插件那几条规律的「常数步长」lint —— 和作者层那半**共用同一个函数**。

    ⚠️ **出厂插件不进这一趟**:离线这一侧手上只有作者层,而出厂那几个由世界配置
    决定装不装;它们的写法也不是作者改得动的东西,喊了他也没有一处可修。
    """
    entries = (authored or {}).get("plugins") if isinstance(authored, dict) else None
    if not entries:
        return []
    from anima_world.plugins import PluginError, parse_plugins

    try:
        plugins = parse_plugins(entries)
    except PluginError:
        return []          # 坏插件归 `world_plugin_errors` 报,这儿闭嘴
    rules = [rule for plugin in plugins for rule in plugin.rules]
    return list(drift_warnings(rules)) if rules else []


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

        return list(_check(
            parse_kinds(authored["kinds"],
                        namespaces=_plugin_namespaces(authored)), rules))
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
    merge: bool = False, namespaces: Iterable[str] | None = None,
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
    known = _plugin_namespaces(world_seed) if namespaces is None else tuple(namespaces)
    parse_entities(entities, parse_kinds(kinds, namespaces=known))  # 坏了当场抛
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
    ontology_store: Any = None, *, namespaces: Iterable[str] | None = None,
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
    #
    # 🔴 **两处的合并方向必须一样,而它们曾经是反的**(2026-08-26 验收 C 复现):
    # 这里当初写的是默认的"库里的赢",而真正落库的 `_seed_ontology` 用的是
    # `incoming_wins=True`(**文件里的赢** —— 种类是法不是状态)。于是一份
    # **撤掉了某个量**的编辑包在这里拿旧声明验、当然过;`_seed_ontology` 随后把新
    # 声明写进 `:kinds`;再由 `_load_ontology` 撞上一条还引用着那个量的规律,
    # `OntologyError`。**而 `:kinds` 已经被改掉了** —— 这个世界从此每一次开机都是
    # 同一条报错,不带 `--world-file` 也救不回来。
    #
    # **这正是这个函数当初要治的病的镜像**:不是"验漏了一条",是**验的和写的不是
    # 同一份东西**。所以修法不是在外面包一层回滚(这些表在 Redis 上没有共同的
    # 事务),是把这一处的方向掰过来 —— 判断只有一份,输入也只有一份。
    # ⚠️ `entities` 那一半照旧是"库里的赢",因为**落库那一侧也是**(实例只增)。
    kind_rows = world_seed.get("kinds") or []
    entity_rows = world_seed.get("entities") or []
    if ontology_store is not None and len(ontology_store):
        kind_rows = _union_by_id(ontology_store.kind_definitions(), kind_rows,
                                 incoming_wins=True)
        entity_rows = _union_by_id(ontology_store.entity_definitions(), entity_rows)
    known = _plugin_namespaces(world_seed) if namespaces is None else tuple(namespaces)
    kinds = parse_kinds(kind_rows, namespaces=known)
    entities = parse_entities(entity_rows, kinds)
    ontology = resolve(kinds, entities, rules=rules, locations=sorted(set(locations)),
                       items=sorted({*items, *seed_items}))
    # `stocks` 里的量名也在这儿查(2026-08-21,看板 D29 同族)。它的闸原本只住在
    # **播种**里,而播种在预检之后 —— 两个后果:① `validate world` / `world check`
    # 对一份量名拼错的文件说**绿**,而开机当场 `OntologyError`;② 开机的那次失败
    # 发生在**写过几张表之后**,留下一个装了一半的世界,正是这个函数当初要修的形状。
    problems = _undeclared_stock_names(world_seed, ontology)
    if problems:
        from anima_world.ontology import OntologyError

        raise OntologyError(problems)


def _load_ontology(
    ontology_store: Any, rules: list[Any], location_store: Any, economy_store: Any = None,
    namespaces: Iterable[str] = (),
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
    return ontology_store.load(rules=rules, locations=locations, items=items,
                               namespaces=namespaces)


def _format_dropped_quantities(dropped: dict[str, list[str]]) -> str:
    """`dropped_quantities: {"tree": ["生长速度"]}` —— **一行,而且机器读得到。**

    这个 token 是承重的:三条路(开机的 `logger.warning`、`world check` 的
    `warnings[]`、`validate world`)印的是**同一个函数**的输出,于是下游拿
    `grep -o 'dropped_quantities: .*'` 在哪一条上都取得到同一份 JSON。
    印一句纯人话的下场是每条路各写一遍措辞,而三份措辞里迟早只有一份跟着代码走。
    """
    return "dropped_quantities: " + json.dumps(
        {k: sorted(v) for k, v in sorted(dropped.items())}, ensure_ascii=False)


def _dropped_quantities(
    declared: dict[str, set[str]], *,
    stock_rows: Any = (), visibility_pairs: Any = (), rules: Any = (),
) -> dict[str, list[str]]:
    """**一个种类不再声明、而世界里还留着的那些量。**

    重声明一个种类是**整行替换**(`_seed_ontology` 的合并粒度),所以作者从
    `kinds` 里划掉一个量,这一层就当它不存在了 —— **而存储那一侧一格都不裁剪**:
    量还躺在 `:stocks` 里,可见性行还躺在 `:stock_visibility` 里,而
    `perception` 读的正是这两张表的交集(`perception.py` 不问 `:kinds`)。
    下场是她的提示词里继续出现一个**这个世界已经不承认的量**,顶着旧 label、
    旧分档,规律再也不更新它,**而没有任何一处会报错**。

    同一份文件里写两条同 id 的 `kind` 会当场报错,所以这件事只可能**跨两次开机**
    发生 —— 也就是它必然安静。

    ⚠️ **这一版只吭声,不裁剪。** 裁剪归第 1 期(插件在自己的命名空间里做),
    理由和 `seed.kind_keys` 那一格逐字同一笔账:收严会让**写过额外键的已发布世界
    开不了机**,而那个代价要由一次显式的换钉去付,不是由一次升级安静地付。

    三个来源,而**它们各自安静的程度不同**:

    | 来源 | 今天会怎样 |
    |---|---|
    | `:stocks` 里那个量 | 留着,规律不再更新它;`hidden` 的那些只是占地方 |
    | `:stock_visibility` 里那一行 | 留着,**继续进提示词**,顶着旧 label 与旧分档 —— 这一支是真正会骗人的那一支 |
    | `world_rules` 里引用它的规律 | **非内置种类会当场开不了机**(`resolve` 的 `_check_rule_quantities`),所以这里报得出的只有内置种类(`agent`)与 `action`/`not_action` 选择器那几条 —— 恰恰是那道闸够不着的地方 |
    """
    dropped: dict[str, set[str]] = {}

    def note(kind_id: str, keys: Any) -> None:
        # 带点号的键是**插件命名空间**里的事实(`needs.energy`),不归本体这一层管 ——
        # 它们的撤销由插件自己的裁剪处理并报 `dropped_facts`。不滤掉的话,一个装了
        # 插件的世界开机第一句就是一堆假警报(`agent` 声明里当然没有 `needs.energy`)。
        # **不在 `declared` 里就一个字都不说** —— 那是"这个种类的量另有来路",
        # 不是"这些量被撤掉了"。这道门放在这儿而不是放在每个调用点上:三个来源
        # 各判一次的话,漏掉任何一处的样子都是一条假警报,而假警报没有红字。
        if kind_id not in declared:
            return
        extra = {str(k) for k in keys if "." not in str(k)} - declared[kind_id]
        if extra:
            dropped.setdefault(kind_id, set()).update(extra)

    # ① 量表。
    for kind_id, keys in stock_rows or ():
        note(str(kind_id), keys)

    # ② 可见性表。**这一支是会骗人的那一支** —— 它就是进提示词的那张表。
    for kind_id, key in visibility_pairs or ():
        note(str(kind_id), [key])

    # ③ 规律。`world_` 前缀读的是世界那一份量(另一个 owner),内置日历名不是量。
    from anima_world.rules import BUILTIN_NAMES, WORLD_PREFIX

    for rule in rules or ():
        selector = getattr(rule, "selector_kind", None)
        if selector == "kind":
            kind_id = str(getattr(rule, "selector_value", ""))
        elif selector in ("action", "not_action"):
            kind_id = "agent"
        else:
            continue          # `owner` 指的是一个实例,量表那一支已经数过它了
        if kind_id not in declared:
            continue
        # `reads` 是方法不是属性(表达式里读到的那些名字现算),`outputs` 是
        # 「量名 → 表达式」的表 —— 写的那一半在键上。
        names = set(getattr(rule, "outputs", {}) or {}) | set(rule.reads())
        note(kind_id, {
            n for n in names
            if n not in BUILTIN_NAMES and not n.startswith(WORLD_PREFIX)
        })

    return {k: sorted(v) for k, v in dropped.items()}


def _declared_quantities_by_kind(kinds: Any) -> dict[str, set[str]]:
    """`{种类: {量名}}` —— 这一层管得着的那些种类。

    ⚠️ **声明本身就是开关,而这道门的判据是 `builtin`,不是"有没有量"。**

    | 种类 | 一个量都不声明时,那是什么意思 |
    |---|---|
    | **内置**(`world` / `location` / `agent` / `player`) | **它的量另有来路** —— `world` 的季节与雨势写在作者的 `stock_visibility` / `stocks` 段里,从来不属于任何 `kinds` 声明。算进来的话内置橱窗开机第一句就是三条假警报(实测 `{"world": ["季节","雨势","雨天数"]}`),而**一句会误报的警告等于没有这条警告** |
    | **作者声明的**(`tree` …) | **作者把这个种类的量全撤了** —— 而这恰恰是这条警告要抓的**最大**一次撤销 |

    🔴 **第一版这道门写的是 `if k.quantities`,于是第二行整个落进盲区**
    (2026-08-26 验收 A 实测:一个只改 `affordances`、body 里干脆没写 `quantities`
    的编辑包 —— 整行替换 = 三个量全撤 —— 开机与两扇离线门**都零输出**,而那三行
    照旧进提示词)。我当时在 docstring 里把它写成"不是漏,是同一条开关规则的另一面"
    ——**那句是错的**,而且错得正好盖住了这个功能最该说话的那一次。
    `builtin` 这一格分得开这两件事,所以它才是那道门。
    """
    return {
        kid: set(k.quantities) for kid, k in kinds.items()
        if k.quantities or not k.builtin
    }


def _live_dropped_quantities(
    ontology: Any, stock_store: Any, visibility_store: Any, rules: Any = (),
) -> dict[str, list[str]]:
    """开机那一侧的适配器:把三张真表喂给 `_dropped_quantities`。

    量表**按种类批量取**(一次 pipeline),别逐个 owner 往返 —— 那正是
    `RedisStockStore` 的说明里点名的 72ms/tick。
    """
    declared = _declared_quantities_by_kind(ontology.kinds)
    stock_rows: list[tuple[str, Any]] = []
    for kind_id in declared:
        try:
            rows = stock_store.snapshot_kind(kind_id)
        except Exception:  # noqa: BLE001 - 一句警告不值一次开不了机
            logger.warning("数 %r 的量表失败", kind_id, exc_info=True)
            continue
        stock_rows.extend((kind_id, values) for values in rows.values())
    try:
        pairs = list(visibility_store.rules_map())
    except Exception:  # noqa: BLE001 - 同上
        logger.warning("读可见性表失败", exc_info=True)
        pairs = []
    return _dropped_quantities(
        declared, stock_rows=stock_rows, visibility_pairs=pairs, rules=rules)


def _authored_uncreatable_edges(authored: dict[str, Any] | None) -> list[str]:
    """作者声明了一种边,而这份文件里**没有一个动词或触发器造得出它**。

    **警告,不是错误。** 开机是权威,而开机收得下这种声明(一个还没写完的世界是
    正当的),比它严就是假红。可**没有一处会说话**才是这一格真正的病:那张边表
    永远 0 条,提示词里一个字都不会出现,而作者分不出「它本来就造不出来」和
    「还没有人入门」—— 这正是这个仓库最怕的那种安静。

    ✅ **2026-08-31(收件箱 D44):这一句改口了,而上一版这儿写着它必须改。**
    作者层从今天起种得下边(第十四个段 `edge`),于是「造得出」多了第三条路 ——
    **这份文件里直接种下的那几种**。不跟着改口的下场是:一份写着"青云门的三位
    创派弟子"的世界会收到一句假警报,而作者会去加一个他并不需要的动词。
    ⚠️ 那个"三条路"的判断**不在这儿**,在 `plugins.uncreatable_edges(seeded=)` ——
    这儿只负责把这份文件种了哪几种边交给它。
    """
    if not authored:
        return []
    entries = authored.get("plugins")
    if not entries:
        return []
    from anima_world.plugins import (
        PluginError, order_plugins, parse_plugins, uncreatable_edges,
    )

    try:
        plugins = order_plugins(parse_plugins(entries))
    except PluginError:      # 坏插件归 `world_plugin_errors` 逐条报,这儿闭嘴
        return []
    idle = uncreatable_edges(plugins, seeded=_authored_edge_types(authored))
    if not idle:
        return []
    return [
        f"插件 `{plugin_id}` 声明了这几种边,而这份文件里没有一个动词或触发器"
        f"造得出它们、也没有种下过一条:{'、'.join(names)} —— 那张表会永远是空的,"
        "而「造不出来」和「还没有人连上」在屏幕上长得一模一样。"
        "要么给它一个带 `link` 的动词/触发器,要么在作者层里种几条"
        "(`{\"kind\": \"author\", \"type\": \"edge\"}`),要么先别声明它"
        for plugin_id, names in sorted(idle.items())
    ]


def _authored_edge_types(authored: dict[str, Any] | None) -> tuple[str, ...]:
    """这份作者层**种下**了哪几种边 —— 只取 type,不解析。

    和 `_plugin_namespaces` 逐字同一个安排:这一格只回答"有没有人种过它",
    而"种得对不对"归 `world_edge_errors` 逐条报。解析一遍再报第二遍错,
    就是同一件事两个说法。
    """
    rows = (authored or {}).get("edges")
    return tuple(
        str(row.get("type") or "").strip()
        for row in (rows or ())
        if isinstance(row, dict) and str(row.get("type") or "").strip()
    )


def _authored_dropped_quantities(authored: dict[str, Any] | None) -> list[str]:
    """离线那一侧:**同一份文件里**已经打架的那几个量。

    ⚠️ **它答得比开机那一侧窄,而窄在哪必须说清楚**:开机能拿新声明去比**目标世界
    库里**留着什么,而校验器手上没有那个世界 —— 它只比得了这份文件自己写下的
    `stock_visibility` / `rules` 有没有指着一个同一份文件的 `kinds` 已经不声明的量。
    一次编辑包**通常只带那条重声明的 `kind`**,于是这一支多半一个字都说不出来;
    那一格由 `_edit_dropped_quantity_gap_warnings` 明说"离线答不了",而不是假装查过。

    它仍然值得有,因为它抓得住一种真实写法:作者手改一份**完整**的世界文件,
    把 `kinds` 里的量划掉却忘了删 `stock_visibility` 里那一行 —— 那一行会照旧
    进提示词,而这份文件在今天的两扇门上是全绿的。
    """
    if not authored:
        return []
    from anima_world.ontology import OntologyError, parse_kinds
    from anima_world.rules import RuleError, parse_rules

    try:
        kinds = parse_kinds(namespaces=_plugin_namespaces(authored),
                            entries=[dict(k) for k in (authored.get("kinds") or [])
                             if isinstance(k, dict)])
    except (OntologyError, Exception):  # noqa: BLE001 - 坏声明由别的闸报,这里闭嘴
        return []
    declared = _declared_quantities_by_kind(kinds)
    if not declared:
        return []
    pairs = [
        (str(row.get("kind") or ""), str(row.get("key") or ""))
        for row in _seed_entry_dicts(authored, "stock_visibility")
    ]
    try:
        rules = parse_rules(authored.get("rules"))
    except (RuleError, Exception):  # noqa: BLE001 - 同上
        rules = []
    dropped = _dropped_quantities(declared, visibility_pairs=pairs, rules=rules)
    if not dropped:
        return []
    return [
        "这份文件里有几个量,`kinds` 已经不声明了而别处还引用着 —— 引擎**不裁剪**:"
        "`stock_visibility` 里那一行会照旧进提示词,顶着旧 label 与旧分档,"
        "而规律再也不更新它。" + _format_dropped_quantities(dropped)
    ]


def _edit_dropped_quantity_gap_warnings(authored: dict[str, Any] | None) -> list[str]:
    """一次编辑里,**「你撤掉了哪些量」这一格离线答不了** —— 照 `me_X` 那条先例说出来。

    重声明一个种类是整行替换,所以一次编辑随时可能撤掉量;而**撤掉的量今天不裁剪**,
    它会顶着旧 label 继续进提示词。要知道具体撤了哪几个,得拿新声明去比**目标世界
    库里**留着什么 —— 那份东西不在这个包里。

    **说出来而不是假装查过了**:`--edit` 那句总结逐字列着"包自己肚子里那几件已经
    查过了",而这一格恰恰不在里面。一句说得比做到的宽的话,和一盏假绿灯是同一件事。
    """
    if not authored:
        return []
    rows = [k for k in (authored.get("kinds") or []) if isinstance(k, dict)]
    named = sorted({str(row.get("id") or "?") for row in rows if row.get("quantities")})
    if not named:
        return []
    return [
        f"这是一次编辑(--edit),而其中 {len(named)} 个种类重声明了量"
        f"({'、'.join(named[:5])}{'…' if len(named) > 5 else ''})—— 「重声明是整行替换」,"
        "所以目标世界里那些「这份声明没写」的量会被撤掉;而引擎今天「不裁剪」它们:"
        "值留在 `:stocks` 里、行留在 `:stock_visibility` 里,「顶着旧 label 继续进提示词」。"
        "「具体撤掉了哪几个」离线答不了(要比的是目标世界库里留着什么)。"
        "要查它,用 `world check <文件> --edit`(要真装进一个跑着的世界用 "
        "`pack install`),开机会印一行 "
        "`dropped_quantities:`"
    ]


def _plugin_namespaces(seed: Any) -> tuple[str, ...]:
    """这份作者层里声明了哪几个插件 id —— **给本体那一层认名字用**(第三波 A2/A3)。

    ⚠️ **只取 id,不解析**:插件声明坏了归 `world_plugin_errors` 报,而这一格只是
    在回答「`qi.灵力` 这个名字有没有主」。解析一遍再报第二遍错,就是同一件事
    两个说法。
    ⚠️ **和库里那份是两个来源,而它们服务两条不同的路**:这一份答的是"这份文件
    自己声明了什么"(离线两扇门与创世走它),`RedisOntologyStore._plugin_namespaces`
    答的是"这个世界此刻装着什么"(每次开机重新解析本体时走它)。
    """
    rows = (seed or {}).get("plugins") if isinstance(seed, dict) else None
    return tuple(
        str(row.get("id") or "").strip()
        for row in (rows or ())
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    )


def _plugin_bodies(config_store: Any, plugin_store: Any,
                   world_seed: dict[str, Any] | None) -> list[dict[str, Any]]:
    """这个世界这一趟要装的插件声明,**已经按"谁赢"合并好**。

    三个来源,后面的赢前面的:**出厂**(`_factory_plugins`,由配置开关决定)→
    **库里那几行**(上一次装的,声明原文存在 `body` 里)→ **作者层这一份**
    (`--world-file` 或创世)。
    ⚠️ **出厂那几个 id 上 `_factory_plugins` 是唯一权威** —— 库里那一行是上一次装的
    痕迹,不是"该不该装"的答案(不滤掉的话 `needs.enabled` 关掉之后它会自己活回来)。
    """
    from anima_world.plugins import merge_bodies, stored_bodies

    incoming = [
        dict(row) for row in ((world_seed or {}).get("plugins") or [])
        if isinstance(row, dict)
    ]
    stored = [row for row in stored_bodies(plugin_store)
              if str(row.get("id") or "") not in FACTORY_PLUGINS]
    return merge_bodies(merge_bodies(_factory_plugins(config_store), stored), incoming)


def _merge_plugin_kinds(config_store: Any, plugin_store: Any,
                        world_seed: dict[str, Any] | None,
                        ticks_per_day: int = 288) -> dict[str, Any] | None:
    """把插件声明的种类/动词并进 `world_seed["kinds"]`。**声明坏了当场抛。**

    ⚠️ **同 id 的种类:插件那份赢。** 和 `kinds` 那条"文件里那份赢"逐字同一个理由 ——
    种类是**法不是状态**,身上没有会随时间漂的东西。而动词是**合并**不是替换:
    一个插件给内核的 `agent` 加一个动词,不该把作者写在 `agent` 上的那些顶掉。
    """
    from anima_world.plugins import (
        PluginError, borrowed_kind_errors, compile_kind_rows, order_plugins,
        parse_plugins,
    )

    bodies = _plugin_bodies(config_store, plugin_store, world_seed)
    if not bodies:
        return world_seed
    plugins = order_plugins(parse_plugins(bodies, ticks_per_day=ticks_per_day))
    rows = compile_kind_rows(plugins)
    if not rows:
        return world_seed
    seed = dict(world_seed or {})
    by_id = {str(row.get("id")): dict(row)
             for row in (seed.get("kinds") or []) if isinstance(row, dict)}
    # 🔴 **动词借用的那个种类,真的存在吗**(2026-08-26 验收 C)。
    #
    # 这一格只有在这儿查得动:插件那一层不认识作者写的 `kinds`,而这儿两份都在手上。
    # 从前一个字都没查,于是 `target: "swrd"`(少了个 o)两扇门全绿、开机全绿,
    # 然后**静默长出一个空种类,永远不会有实例** —— 作者看到的是「我的动词点不动」,
    # 而没有一处会告诉他为什么。
    # ⚠️ **这几句话现在住在 `plugins.borrowed_kind_errors` 里,离线那两扇门调的是
    # 同一个函数**(2026-08-27):它从前只在这儿,于是 `target: "swrd"` 离线答绿、
    # 开机退 1 —— §3.28 那一族假绿的又一格。**判断只有一份**是修法的全部内容。
    missing = borrowed_kind_errors(plugins, by_id)
    if missing:
        raise PluginError(missing)
    for row in rows:
        kind_id = str(row["id"])
        have = by_id.get(kind_id)
        if have is None:
            by_id[kind_id] = row
            continue
        # 动词**合并**:插件给一个已有种类加动词,不顶掉作者写在它上面的那些。
        merged = dict(have)
        merged.setdefault("quantities", {}).update(row.get("quantities") or {})
        merged.setdefault("affordances", {}).update(row.get("affordances") or {})
        by_id[kind_id] = merged
    seed["kinds"] = list(by_id.values())
    return seed


def _factory_plugins(config_store: Any) -> list[dict[str, Any]]:
    """出厂内置插件 —— **这一版只有一个:`needs`**(设计稿 §9 的第一个搬家对象)。

    ⚠️ **开关仍然是 `needs.enabled`,而且只有它。** 设计稿提过一个
    `plugins.default` 名单,这一轮**有意没做**:两个开关必须永远说同一句话,
    而"两处判断迟早给出不同答案"是这个仓库反复栽的那一跤。一个世界里
    `needs.enabled=false` 而 `plugins.default` 里有 needs,该听谁的?
    没有第二个开关就没有这个问题。

    ⚠️ **开关照旧是热的**,而这一格是有代价换来的:`needs.enabled` 从前每 tick 现读,
    而"装不装一个插件"是**装载期**的事。两者对不上的话,`config set needs.enabled
    true` 之后世界要重开一次才生效 —— 而 REFERENCE §6 逐字承诺过"全部支持热更新"。
    所以 `World.config_set` 上有一道**通用**的钩子(`FACTORY_SWITCH_KEYS`):
    改到这张表里的键就重装一遍。⚠️ **它是一张表,不是一个 `if needs`** ——
    出厂插件多起来时,那道钩子一个字都不用改。
    """
    store = config_store
    if store is None:
        return []
    from anima_world.economy import factory_plugin as economy_plugin
    from anima_world.needs import factory_plugin as needs_plugin

    # **一个插件一个开关**,而那张表(`FACTORY_PLUGINS`)是唯一的权威 ——
    # 这里按它遍历,加一个出厂插件不必再改这段代码的形状。
    from anima_world.together import factory_plugin as invitation_plugin

    builders = {"needs": needs_plugin, "economy": economy_plugin,
                "invitation": invitation_plugin}
    out: list[dict[str, Any]] = []
    for plugin_id, switch in FACTORY_PLUGINS.items():
        # 空串 = 这个出厂插件没有开关(它搬的那件事今天也没有),永远装。
        if switch and not store.get(switch, default=False):
            continue
        build = builders.get(plugin_id)
        if build is not None:
            out.append(build())
    return out


#: 出厂插件的 id → 决定它装不装的那个配置键。**`World.config_set` 读这张表**,
#: 于是那道热更新的钩子里没有一个具体插件的名字。
FACTORY_PLUGINS: dict[str, str] = {
    "needs": "needs.enabled",
    # 🆕 3.8.0 第 2 期 2e:邀请的**存储与过期规律**(裁决 ③)。
    # 🔴 **空串 = 没有开关,永远装**,而这不是偷懒:**它搬的那件事今天也没有开关**
    # (邀请不受 `social.enabled` 管 —— 那一格管的是八卦与小团体;
    # `_invite` 走的是联合动词那条路,一个开关都不问)。
    # 给它新造一个开关,就是给同一件事第二个答案 —— 而"两个开关必须永远说同一句话"
    # 正是第 1 期有意不做 `plugins.default` 的那条理由。
    "invitation": "",
    # 🆕 3.8.0 第 2 期 2d-①:**只有钱包那一格**(补裁 ⑤)。
    # 开关沿用 `economy.enabled`,而它的语义**一个字没动**(只挡 eat / wages,
    # 不挡 buy / give)—— 这一格是加法,不是把那个开关的含义改宽。
    "economy": "economy.enabled",
}

#: 每个出厂插件**搬了哪几格**。🔴 **不按"搬完几个系统"计数**(补裁 ⑤b:
#: 按系统计数正是把人推向换皮的那把尺子)。消费方读这一格,别照文档记一份清单。
FACTORY_SCOPE: dict[str, str] = {
    "needs": "整条(三个量 + 衰减/恢复那六条规律;值从黑板搬进量表)",
    "invitation": (
        "**只有存储与过期规律**(边 `invitation.invites` + 那条比 `now` 的规律)。"
        "三扇门(`invitations_page` / `answer_invitation` / "
        "`invitation_outcomes_page`)**留在内核,签名一格不变** —— 它们是冻结面。"
        "别把它读成「邀请这条机制变成插件了」"
    ),
    "economy": (
        "**只有钱包一格**(`economy.coins`,projected,认领 `payment`)。"
        "货架仍住 `shop_stock` 那个真 hash、没变成边;`buy`/`eat`/`give` 仍是内核路;"
        "`economy.enabled` 语义一个字没动。别把它读成「economy 已经是插件了」"
    ),
}
#: 那道热更新钩子读的键集。**空串滤掉** —— 没有开关的出厂插件不该让
#: `config_set("")` 这种事变成一次重装。
FACTORY_SWITCH_KEYS = frozenset(k for k in FACTORY_PLUGINS.values() if k)


def refresh_plugins(scheduler: Any) -> None:
    """按此刻的配置把插件重装一遍(**不读作者层**)。

    `World.config_set` 改到 `FACTORY_SWITCH_KEYS` 里的键时走这条。它拿不到世界文件
    (那是开机那一刻的东西),所以只重放**库里那几行 + 出厂那几个** —— 而作者写的
    插件本来就在库里(声明原文存库、编译在读取侧),所以这一趟不会把它们弄丢。
    """
    plugin_store = getattr(scheduler, "plugin_store", None)
    if plugin_store is None or scheduler.stock_store is None:
        return
    _install_plugins(scheduler, plugin_store, scheduler.stock_store,
                     scheduler.visibility_store, scheduler.location_store, None)
    # 🔴 **重装之后要把物化视图也重建一遍**(2026-08-27 复核评审逮的)。
    #
    # `_install_plugins` 换掉的是**声明**(`projected_facts` 与 `fact_sources`
    # 那张注册表),而投影式事实的**值**住在量表里,只有物化那一趟才写得进去。
    # 少了这两句,运行中 `config_set("economy.enabled", True)` 之后,量表那一格
    # 停在 0 而账本早就有数 —— **跑着的世界一个数、重开之后另一个数**,零报错。
    # ⚠️ 那正是 2b 自己防过的形状(「只有重开那一刻才错」那一族),
    # 而这一处是同一个洞的第二个入口:开机那条路补过了,热更新这条没补。
    if getattr(scheduler, "projected_facts", None):
        if getattr(scheduler._memory_projection, "fact_sources", None):
            # 注册表刚换过,而 `Scheduler.__init__` 那趟重放是在旧注册表下折的 ——
            # 和开机那条路逐字同一个理由,所以重折一遍。
            log = getattr(scheduler, "event_log", None)
            if log is not None:
                scheduler.reset_projection(log.replay())
        scheduler._materialize_projected_facts()
    # 邀请那几条边同理:它是投影的物化视图,重装之后也要跟上。
    scheduler.rebuild_invitation_edges()


def _install_plugins(
    scheduler: Any, plugin_store: Any, stock_store: Any, visibility_store: Any,
    location_store: Any, world_seed: dict[str, Any] | None,
) -> None:
    """把插件装进这个世界,并把它的规律与触发器接到调度器上(3.8.0)。

    **两个来源,文件里那份赢**:库里那几行(这个世界上一次装的,和 `:kinds` /
    `:world_rules` 同一类持久声明)与作者层这一份(`--world-file` 或创世)。
    没有作者层的普通重启只走前者 —— 于是一个从 `--world-file` 建起来的世界,
    下一次开机手上没有那份文件,它的插件照旧在跑。

    ⚠️ **没有插件的世界这里是两次判空**:一次 `hgetall` 加一次 `.get`。
    "声明本身就是开关"在这一层的落法 —— 不写 `plugin` 的世界行为逐位不变。
    """
    from anima_world.events import SUBSCRIBABLE_EVENTS
    from anima_world.plugins import (
        install_plugins, order_plugins, parse_plugins, verb_kind_id,
    )

    # **三个来源怎么合并、谁赢,只有一份判断**(`_plugin_bodies`)—— 开机那一处
    # (并 `kinds`)和这一处(真装)喂的必须是同一份名单,不然种类按一份来、
    # 事实按另一份来,而那种不一致不报错。
    bodies = _plugin_bodies(scheduler.config_store, plugin_store, world_seed)
    # 每次重装都从头建 —— 这个函数在开机和 `config set` 两条路上都会被调,
    # 而"接着上一次的名单往上加"会让关掉一个开关之后它的规律还在跑。
    scheduler.plugins = []
    scheduler._triggers_by_event = {}
    scheduler.edge_types = {}
    scheduler.verb_edge_effects = {}
    scheduler.projected_facts = {}
    scheduler.world_rules = [
        rule for rule in scheduler.world_rules
        if rule.id not in scheduler.plugin_rule_ids
    ]
    scheduler.plugin_rule_ids = set()
    if not bodies:
        return
    ticks_per_day = max(1, 1440 // max(1, scheduler._minutes_per_tick()))
    plugins = order_plugins(parse_plugins(
        bodies, ticks_per_day=ticks_per_day, subscribable=SUBSCRIBABLE_EVENTS))

    def owners_of(bearer: str) -> list[str]:
        """这个 bearer 此刻有哪些 owner。**这一层认识世界,插件那一层不认识。**"""
        return _plugin_owners(scheduler, bearer, location_store=location_store,
                              stock_store=stock_store)

    report = install_plugins(
        plugins, store=plugin_store, stock_store=stock_store,
        visibility_store=visibility_store, owners_of=owners_of,
        tick=scheduler.clock, bodies={str(b.get("id")): b for b in bodies},
    )
    scheduler.plugins = list(plugins)
    # 🆕 **常数步长那条 lint 也覆盖插件的规律**(第二波 ④)。开机那一侧的开口
    # **恰好一处**,和 `_load_world_rules(warn=True)` 同一个安排 —— 说好几遍的
    # 警告和没说过一样。⚠️ 出厂那几个滤掉:它们的写法作者改不动。
    from anima_world.rules import drift_warnings as _drift

    for warning in _drift([rule for plugin in plugins
                           if plugin.id not in FACTORY_PLUGINS
                           for rule in plugin.rules]):
        logger.warning("%s", warning)
    # 边类型登记 —— `link` 那一刻查约束读的就是这张表。
    scheduler.edge_types = {
        edge.qualified: edge for plugin in plugins for edge in plugin.edges.values()
    }
    # 动词的边效果**按 (种类, 动词) 登记**,而不是塞进 affordance 里。
    #
    # 理由是 affordance 是**本体那一层**的东西,而边不是:往它身上挂一格
    # `links` 就等于让本体层认识插件的边类型,而它此后每一处遍历 affordance
    # 的代码(校验器、`ontology --json`、离线两扇门)都要各自决定"这一格算不算
    # 声明的一部分" —— 漏一处不报错。挂在调度器上,本体层一个字节都不知道有这回事。
    scheduler.verb_edge_effects = {
        (verb_kind_id(plugin.id, verb.target), verb.name): verb.links
        for plugin in plugins for verb in plugin.verbs.values() if verb.links
    }
    # 投影式事实登记 —— **存储键 → 那条 delta 事件的 type**(第 2 期 2b)。
    # ⚠️ 它是从**这一趟真的装上的**那份声明里建的,不是从库里那几行:
    # 一个刚被关掉的插件留下的键要是还在这张表上,它的量表被别人写一下就会
    # 凭空多出一条没人认领的 delta。
    scheduler.projected_facts = {
        fact.qualified: fact.delta_event
        for plugin in plugins for fact in plugin.facts.values() if fact.projected
    }
    # 🆕 裁决 ④:把**既有的内核事件**认成某个投影式事实的 delta。
    # 折叠端边折边读这张表,所以它必须在**重放之前**就位(见 `reset_projection`)。
    sources: dict[str, list[dict[str, Any]]] = {}
    for plugin in plugins:
        for fact in plugin.facts.values():
            for spec in fact.sources:
                sources.setdefault(str(spec["event"]), []).append(
                    {**spec, "fact": fact.qualified})
    scheduler._memory_projection.fact_sources = {
        event: tuple(rows) for event, rows in sources.items()
    }
    # 规律接进那条已经在跑的路(`stocks.evaluate_due`)—— **不另起一个求值器**:
    # 双缓冲、节流水位、骰子、"两条规律抢同一个量"那句警告,插件一样得吃到。
    for plugin in plugins:
        scheduler.world_rules.extend(plugin.rules)
    scheduler.plugin_rule_ids = {
        rule.id for plugin in plugins for rule in plugin.rules
    }
    by_event: dict[str, list[Any]] = {}
    for plugin in plugins:
        for trigger in plugin.triggers:
            by_event.setdefault(trigger.event, []).append(trigger)
    scheduler._triggers_by_event = by_event
    # 已经在册的人补上插件那几格(后来的人走 `seed_actor_quantities` 那个窄口)。
    for agent_id in list(scheduler.agents):
        scheduler.seed_actor_plugin_facts(agent_id)
    if report.dropped_facts:
        # **删数据的那个要吭声。** 兄弟那条 `dropped_quantities` 是 warning +
        # 两扇离线门各一行,而它**不删任何东西**;这一条真的把值删掉了,却只走
        # `logger.info` —— 吵的那个不删数据,删数据的那个不吭声
        # (2026-08-26 验收 C)。
        logger.warning(
            "插件升级**裁剪**掉了几个事实 —— 它们的值与可见性行**已经删掉,"
            "回不来了**(和本体层那条 `dropped_quantities` 不同:那一条只吭声不删)。"
            "dropped_facts: %s",
            json.dumps(report.dropped_facts, ensure_ascii=False, sort_keys=True),
        )
    if report.installed or report.upgraded or report.dropped_facts:
        logger.info(
            "插件:装了 %s;升级 %s;同版本 %s;种了 %d 个默认值%s",
            report.installed or "—",
            [f"{i}:{a}→{b}" for i, a, b in report.upgraded] or "—",
            report.unchanged or "—", report.seeded,
            f";裁剪 dropped_facts: {report.dropped_facts}" if report.dropped_facts else "",
        )


def _apply_ontology(ontology: Any, stock_store: Any, visibility_store: Any,
                    *, tick: int = 0, redeclare_kinds: Any = (),
                    rules: Any = ()) -> None:
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

    `rules` 是这个世界真正在跑的那份规律,只给 `_dropped_quantities` 用:一条作用在
    `agent` 上的规律引用了一个已经不声明的量,`resolve` 那道闸**够不着**
    (内置种类的量不归本体层声明),所以只有这里说得出。
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

    # **撤掉的量今天不裁剪,但必须吭声。** 见 `_dropped_quantities`:一个种类
    # 不再声明的量照旧躺在 `:stocks` 与 `:stock_visibility` 里,而 `perception`
    # 读的正是这两张表的交集 —— 于是它顶着旧 label 继续进提示词,规律再也不更新它,
    # 一处不报错。同一份文件里两条同 id 的 kind 会当场报错,所以这件事只可能
    # **跨两次开机**发生,也就是它必然安静。
    #
    # ⚠️ **这一句只在真有留下来的量时才响**(和 `_warn_unresolved_rule_names`
    # 同一条纪律):一句总在响的警告等于没有警告。
    dropped = _live_dropped_quantities(ontology, stock_store, visibility_store, rules)
    if dropped:
        logger.warning(
            "有几个量这个世界已经不声明了,而它们还留在库里 —— 这一版引擎**不裁剪**:"
            "`:stocks` 里的值留着(规律不再更新它)、`:stock_visibility` 里的行留着"
            "(**顶着旧 label 继续进提示词**)。要它们消失,把量加回声明再抹,"
            "或者等插件命名空间那一期的裁剪。%s",
            _format_dropped_quantities(dropped),
        )


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
    *, merge: bool = False, builtin_fallback: bool = True
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
    # 🔴 **`builtin_fallback=False` 的意思是"缺席的段 = 不动",不是"回落内置那份"**
    # (3.10.0,2a-① 验收 A 逮的)。
    #
    # 那个回落对**创世**是对的:一个没写 `locations` 的世界总得站得住脚,给它
    # 一份默认地图比开不了机好。对 `install_pack` 它是**错的,而且错得很安静**:
    # 一份只带一拍的第 2 周包装进一个跑着的卡塞尔世界,地图上会凭空多出
    # `cafe` / `home` / `workshop` 三个地点,还跟着三条 `location_join` 进日志
    # —— **事件是只追加的,撤不回来**。带 `locations` 不带 `agents`(第 2 周包
    # 最常见的形状)更阴:`:bt_actions` 会多出 `chat_with_夏/柔/遥`,
    # 指着三个这个世界里根本不存在的人。全程零报错,`world check --edit` 也不说。
    #
    # 这一格和 `merge=` 是两件事:`merge` 说"同名的怎么办",这一格说"没写的怎么办"。
    if world_seed is not None:
        # **只改了一部分的文件不该被当成"作者删掉了其余段"。**
        # 没写 `locations` = 这次不动地图,不是"这个世界没有地点"。
        seed_locs = world_seed.get("locations")
        seed_agents = world_seed.get("agents")
        fallback_locs = [dict(p) for p in DEFAULT_POINTS] if builtin_fallback else []
        fallback_agents = [a["id"] for a in CHARACTER_ROSTER] if builtin_fallback else []
        loc_entries = (
            [_normalize_location_entry(loc, i, len(seed_locs)) for i, loc in enumerate(seed_locs)]
            if seed_locs is not None else fallback_locs
        )
        agent_ids = ([a["id"] for a in seed_agents] if seed_agents is not None
                     else fallback_agents)
    elif builtin_fallback:
        loc_entries = [dict(p) for p in DEFAULT_POINTS]
        agent_ids = [e["id"] for e in CHARACTER_ROSTER]
    else:
        loc_entries, agent_ids = [], []
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


#: 一份包里,每个段各带了哪几个 id —— `pack_installed` 载荷里的 `sections`。
#:
#: 🔴 **`beats` 那一格是承重的**:一条 pack 装进来的拍,它 `trigger.at.day` 的零点
#: 就是这个包落地那天,而"这条拍属于哪个包"唯一的答案就在这儿
#: (`beats.pack_days_from`)。少了它,一份写着 `day: 0..6` 的第 2 周包装进一个
#: 跑到第 40 天的世界,**八拍在同一 tick 全部烧掉**,零报错。
#:
#: 别的段今天只用来**给人看**(`pack list` 那一屏 / 审计),所以取不到 id 的段
#: 就不取 —— 一个猜出来的 id 比没有更坏。
def _pack_sections(world_seed: dict[str, Any] | None) -> dict[str, list[str]]:
    from anima_world import world_file

    if not isinstance(world_seed, dict):
        return {}
    out: dict[str, list[str]] = {}
    for section in world_file.AUTHOR_SECTIONS.values():
        rows = world_seed.get(section)
        if not isinstance(rows, list) or not rows:
            continue
        ids = [str(r["id"]) for r in rows
               if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]]
        if ids:
            out[section] = ids
    config = world_seed.get("config")
    if isinstance(config, dict) and config:
        out["config"] = sorted(str(k) for k in config)
    if isinstance(world_seed.get("world_setting"), str) and world_seed["world_setting"].strip():
        out["world_setting"] = ["world.setting"]
    return out


def _record_pack_installed(
    event_log: EventLog, world_seed: dict[str, Any] | None, *, day: int, tick: int,
    landed: dict[str, list[str]] | None = None,
    wrote: dict[str, dict[str, str]] | None = None,
) -> str:
    """一份内容包落地了 —— 记一条 `pack_installed`,**没有第二张表**。

    「装了哪几周」折自这条事件(`Projection.packs` / `World.packs()`),和余额
    折自 `payment` 逐字同一种。存一份直接写的清单就多出一种和日志对不上的坏法,
    而这一层对不上的样子是**「这一周的拍从哪天起算」答错** —— 没有一处会报错,
    只是那几拍一起在同一 tick 响掉,或者永远不响。

    没写 `pack` 的文件这里什么都不做(**声明本身就是开关**)。返回 pack id,
    没有就返回空串 —— 调用方拿它写日志。
    """
    body = (world_seed or {}).get("pack")
    if not isinstance(body, dict) or not body.get("id"):
        return ""
    pack_id = str(body["id"])
    event_log.append({
        "ts": int(tick),
        "type": "pack_installed",
        "payload": {
            "pack_id": pack_id,
            "version": str(body.get("version") or ""),
            "note": str(body.get("note") or ""),
            "day": int(day),
            "tick": int(tick),
            # 🔴 **`sections` 记的是「真的落地了什么」,不是「文件里写了什么」**
            # (2a-① 验收 C 逮的:同一份包 `pack install --json` 回执 `agents: []`,
            # 而 `pack list --json` 却报 `sections.agents: ["夏"]` —— **两份真相**,
            # 而读的人分不出哪一份是对的)。文件里那份另起一格 `declared`:
            # 两者之差正是「这份包里有几样没装进去」,而那是作者最该看见的一件事。
            "sections": dict(landed if landed is not None else _pack_sections(world_seed)),
            "declared": _pack_sections(world_seed),
            # 🔴 **「我这一版往那几格里写了什么」** —— 下一次升级那把
            # compare-and-set 的尺读的就是它(2a-②)。判据不是"我是同一个 pack",
            # 是"这一格此刻的值还等于我上次写下去的那个值吗"。
            **({"wrote": {k: dict(v) for k, v in wrote.items() if v}}
               if wrote and any(wrote.values()) else {}),
        },
    })
    return pack_id


class PackInstallError(ValueError):
    """一份内容包装不进这个世界;带着每一条理由。

    **和 `WorldSeedError` / `PluginError` 同一类**:作者写错了东西,而作者该看到的
    是那几行中文,不是一段 Python 堆栈。
    """

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("这份内容包装不进去:\n" + "\n".join(f"- {e}" for e in errors))


def disable_authored_pack(scheduler: Any, pack_id: str) -> dict[str, Any]:
    """停用一份内容包 —— **拍不再响、它带来的新人退场、它开的开关回落**(3.10.0,K7)。

    🔴 **停用不是删除,而这一条是硬的**:玩家的记忆里有这一周发生过的事,他的钱包
    里有那 800 块,她记得那天下过雨。删掉那几条事件 = 让历史指向不存在的东西 ——
    而这个引擎里"对账即重放",一段被抹掉的历史会让投影和日志对不上,**且没有任何
    地方会报错**。所以停用**只往日志里追加一条事实**(`pack_disabled`),
    朝前看的那一半跟着变;朝后看的那一半一个字不动。

    ⚠️ **和 `forget_player` 逐字同一个形状**,理由也逐字相同(见那个方法)。

    三件"朝前看的":

    - **拍不再响**:`BeatDirector` 跳过这个包带来的那几拍。已经响过的照旧响过
      (`beat_fired` 是历史)。
    - **新人退场**:走 `agent_leave` 那条已有的路(`Scheduler.unregister` + 一条
      事件),和一条剧情拍的 `agent_leave` op 逐字同一条。**不是删人** ——
      他说过的话、他造成的后果留在世界里。
    - **开关回落**:回到**这个世界原来的样子**(装包前那个值),不是回到引擎默认值。
      ⚠️ **只回落"至今还等于我写下去的那个值"的那几个键** —— 装完之后运维又调过的
      那一格不该被这一趟撤销,而那正是 compare-and-set 在这一层的同一把尺。
    """
    from anima_world.beats import BeatDirector, BeatScript
    from anima_world.redis_state import RedisBeatsStore

    event_log = scheduler.event_log
    if event_log is None:
        raise PackInstallError(["这个世界没有事件日志,停用不了内容包"])
    pack_id = str(pack_id or "").strip()
    with scheduler._lock:
        scheduler.catch_up_projection()
        row = dict(scheduler._memory_projection.packs.get(pack_id) or {})
    if not row:
        raise PackInstallError([
            f"这个世界没有装过 {pack_id!r} 这份内容包。"
            "装了哪几周问 `anima-world pack list`"
        ])
    if row.get("disabled"):
        raise PackInstallError([f"内容包 {pack_id!r} 已经是停用的了。"])

    sections = row.get("sections") or {}
    wrote = row.get("wrote") or {}
    tick = int(scheduler.clock)
    day = int(scheduler.world_time(tick).day)
    receipt: dict[str, Any] = {
        "pack": pack_id, "version": str(row.get("version") or ""),
        "day": day, "tick": tick,
        "beats": sorted(str(b) for b in (sections.get("beats") or ())),
        "agents": [], "config": [], "kept": [],
    }

    with scheduler._lock:
        # ① 它带来的新人退场 —— 走 `agent_leave` 那条已有的路。
        for aid in (sections.get("agents") or ()):
            aid = str(aid)
            if aid not in scheduler.agents:
                continue
            here = scheduler.agents[aid].agent.location
            scheduler.unregister(aid)
            scheduler._transit.pop(aid, None)
            scheduler._plans.pop(aid, None)
            scheduler._current_action.pop(aid, None)
            event_log.append({
                "ts": tick, "who": aid, "loc": here, "type": "agent_leave",
                "payload": {"agent_id": aid, "reason": "pack_disabled",
                            "pack": pack_id},
            })
            receipt["agents"].append(aid)

        # ② 开关回落 —— **只回落至今还等于我写下去的那几个**。
        store = scheduler.config_store
        mine = (wrote.get("config") or {})
        before = (wrote.get("config_before") or {})
        for key, written in sorted(mine.items()):
            if store is None:
                break
            now = repr(store.world_value(key, None))
            if now != written:
                # 装完之后被人调过了。**撤销它等于把那次调整悄悄抹掉**,
                # 而账面上什么都看不出来 —— 留着,并说出来。
                receipt["kept"].append(key)
                continue
            had = before.get(key, "")
            try:
                if had:
                    store.set(key, ast.literal_eval(had))
                else:
                    store.unset(key)
            except Exception as exc:  # noqa: BLE001 - 一个键回落不了不该掀翻整趟
                logger.warning("内容包 %s 的开关 %s 回落不了:%s", pack_id, key, exc)
                continue
            receipt["config"].append(key)

        # ③ 落一条事实,然后重折 —— 和 `forget_player` 逐字同一条路。
        event_log.append({
            "ts": tick, "type": "pack_disabled",
            "payload": {"pack_id": pack_id, "day": day, "tick": tick,
                        "beats": receipt["beats"], "agents": receipt["agents"]},
        })
        persisted = event_log.replay()
        scheduler.reset_projection(persisted)
        scheduler.load_persisted_events(persisted)

        # ④ 导演重建 —— **停用的那几拍从此不再进候选**。
        script_rows = RedisBeatsStore(scheduler.redis, scheduler.world_id).definitions()
        if script_rows:
            fired = {
                (e.payload.get("beat_id"), str(e.payload.get("for") or ""))
                for e in persisted
                if e.type == "beat_fired" and e.payload.get("beat_id")
            }
            scheduler.beat_director = BeatDirector(
                BeatScript.from_data({"beats": script_rows}, stored=True), fired=fired)

    logger.info(
        "内容包 %r 停用了(世界第 %d 天):%d 拍不再响 · %d 个人退场 · %d 个开关回落"
        "%s",
        pack_id, day, len(receipt["beats"]), len(receipt["agents"]),
        len(receipt["config"]),
        f";{len(receipt['kept'])} 个开关装完之后被人调过,留着没动" if receipt["kept"] else "",
    )
    return receipt


def _current_personality(scheduler: Any, agent_id: str) -> str:
    """她此刻的人设是哪一句 —— **问投影,不问黑板**(2a-②)。

    黑板上那份是开机时从投影拼出来的快照,而 `persona_update` 是**事件**:
    投影才是"这一格现在是什么"的权威。问黑板的下场是 CAS 拿一份可能陈的值去比,
    而比错了的样子是安静的:该拒的放行了(把玩家聊出来的三十天抹掉),
    或者该放行的被拒了(作者改不动自己上周写的那一句)。
    """
    projected = scheduler._memory_projection.agents.get(agent_id)
    if projected is not None and "personality" in (getattr(projected, "spec", None) or {}):
        return str(projected.spec["personality"] or "")
    brain = scheduler.agents.get(agent_id)
    if brain is None:
        return ""
    return str(brain.agent.blackboard.read("personality") or "")


def install_authored_pack(scheduler: Any, path: Path | str, *,
                          force: bool = False) -> dict[str, Any]:
    """把一份**内容包**装进一个正在跑的世界(3.10.0,周更链路 2a-①)。

    这是**运行期**那条路;`--world-file` 是创世 / 离线编辑那条。两条路的区别不是
    "装什么",是**谁在装**:

    - `--world-file` 在**开机第一秒**装,装完这个进程才开始跑;
    - 这一条在世界**已经在跑**的时候装,而**装包的那个进程就是在跑的那个进程** ——
      于是"别的进程装的东西这个进程看不见"这个问题整个消失。

    🔴 **有意不做"让别的进程装的包被跑着的进程看见"**:那要给 `:config` / `:kinds` /
    `:world_rules` / `:plugins` / `:beats` / 名册**各加一份版本号 + 一次进程内重装**,
    而进程内重装就是 `build_serve_scheduler` 的下半场 —— **第二条创世路径**,
    正是这个仓库最贵的那条纪律反对的(FOR-STUDIO §3.46:「`--world-file` 那条路
    已经是创世,它不需要孪生兄弟」)。

    落三段是这一条新开的:

    - **`beat`** —— 按 id 合并进 `:beats`(`--world-file` 那条路整份不装,理由是
      "一份写着 `day: 0..6` 的包装进跑到第 40 天的世界,八拍一 tick 全烧"。
      那个理由是对的,而 `trigger.at.since` 把它解决了:一条 pack 装进来的拍,
      零点是**这个包落地那天**)。**id 撞车当场拒绝**,不猜。
    - **`config`(作者动过的那几个键)** —— `--world-file` 那条钉在 `fresh_world` 上,
      于是一个有人在玩的世界改不了自己的开关,而且**一个字都不说**。
    - **`world_setting`** —— 同上,`_seed_world_setting` 由 `not persisted` 把着门。

    其余段**照今天那样只填缺**,走的是开机那条路上**同一批**播种函数
    (`_seed_world_defs` / `_seed_ontology` / `_install_plugins` / …)——
    另写一份的那天,两条路会先给出不同的答案,再由某个人在一个坏掉的世界上发现。

    ⚠️ **2a-① 明确不做**:人设覆盖、给在册的人补记忆、停用一个包。
    那三件各自要一把 compare-and-set 的尺(「这一格还等于我上次写的值吗」),
    而**「同一个 pack 就能覆盖」是错的**:第 1 周的包给她写过一句人设,玩家跟她
    聊了三十天,第 2 周包一升级就把那三十天抹了,账面上什么都看不出来。
    """
    from anima_world.redis_state import (
        RedisBeatsStore, RedisBlackboard, RedisPluginStore, RedisRulesStore, agent_key,
    )
    from anima_world.world_file import author_records_to_seed, read_world_file
    from anima_world.beats import BeatDirector
    from anima_world.ontology import OntologyError
    from anima_world.plugins import PluginError, plugin_version_errors
    from anima_world.world_seed import WorldSeedError

    redis = scheduler.redis
    world_id = scheduler.world_id
    event_log = scheduler.event_log
    if event_log is None:
        raise PackInstallError(["这个世界没有事件日志,装不了内容包"])

    # ── ① 读文件:**只收作者层。**
    #
    # 一份内容包是**作者写下的东西**;带状态记录的是一个跑过的世界的 dump,
    # 把它"装"进另一个世界没有一种正确答案(合并会重号,覆盖会抹掉这期间发生的
    # 一切)。`--world-file` 那条路对这种文件是滤掉 + 说一句,而这一条**当场拒绝**:
    # 那儿滤掉是因为托管环境每次开机都指着同一份文件,而这儿是人按下的一次动作。
    try:
        manifest, records = read_world_file(path)
        rows = list(records)
    except Exception as exc:  # noqa: BLE001 - 读不动就说读不动,别甩堆栈
        raise PackInstallError([f"{path} 读不了:{exc}"]) from None
    stateful = [r for r in rows if r.get("kind") in ("redis", "event", "mysql")]
    if stateful:
        raise PackInstallError([
            f"{path} 里有 {len(stateful)} 条状态记录 —— 内容包只装**作者写下的东西**。"
            "一个跑过的世界导出来的包要还原用 `anima-world world import`(目标必须是空的)"
        ])
    try:
        authored = author_records_to_seed(rows)
    except Exception as exc:  # noqa: BLE001
        # ⚠️ **`WorldFileError` 自己带着一摞中文行,别把它 `str()` 成一句
        # `invalid world file:` 打头的英文**(2a-① 验收 C ⑨)——
        # 作者该看到的是那几行中文,不是一句他改不动的英文抬头。
        raise PackInstallError(
            list(getattr(exc, "errors", None) or [str(exc)])) from None

    body = authored.get("pack")
    if not isinstance(body, dict) or not body.get("id"):
        raise PackInstallError([
            f"{path} 没有 `pack` 段 —— 一份内容包要先有身份:"
            '`{"kind": "author", "type": "pack", "body": {"id": …, "version": …}}`。'
            "没有 id 的话,以后没有一处说得清那几拍是哪一周加的,而**事后补不回来**"
        ])
    pack_id = str(body["id"])

    # ── ② 装不装得进:**和开机、和离线那两扇门同一份判断。**
    errors = list(authored_layer_errors(authored, complete=False))
    errors += pack_engine_min_errors(authored, manifest)
    if errors:
        raise PackInstallError(errors)
    for problem in pack_engine_min_warnings(authored, manifest):
        logger.warning("%s", problem)

    rules_store = RedisRulesStore(redis, world_id)
    plugin_store = RedisPluginStore(redis, world_id)
    location_store = scheduler.location_store
    economy_store = scheduler.economy_store
    ontology_store = scheduler.ontology_store
    config_store = scheduler.config_store
    prompt_store = scheduler.prompt_store
    stock_store = scheduler.stock_store
    visibility_store = scheduler.visibility_store
    bt_store = scheduler.bt_store

    # 插件声明的种类先并进 `kinds` —— **和开机那条路逐字同序**(见
    # `build_serve_scheduler` 里那一段:晚一步的话,引用不到的 `spawn.kind` 会
    # 绕过预检,到 `_load_ontology` 那儿才炸,而那时表已经写过几张了)。
    merged = _merge_plugin_kinds(config_store, plugin_store, authored) or authored
    boot_plugin_bodies = _plugin_bodies(config_store, plugin_store, authored)
    namespaces = tuple(
        str(b.get("id") or "") for b in boot_plugin_bodies if str(b.get("id") or "")
    )
    try:
        if merged.get("kinds"):
            _precheck_ontology(merged, rules_store, location_store, economy_store,
                               ontology_store, namespaces=namespaces)
        if merged.get("edges"):
            edge_errors, edge_warnings = _edge_layer_verdict(
                merged, _parsed_plugins_or_none(boot_plugin_bodies),
                complete_namespaces=True)
            for problem in edge_warnings:
                logger.warning("%s", problem)
            if edge_errors:
                raise WorldSeedError(edge_errors)
    except (WorldSeedError, OntologyError) as exc:
        raise PackInstallError(list(getattr(exc, "errors", None) or [str(exc)])) from None

    # 🔴 **插件那一道也要在第一次写之前问**(2a-① 验收 A 逮的)。
    # 从前它靠 `_install_plugins` 抛,而那一句排在锁里**六次写之后** —— 一份带
    # 新地点 + 插件降级的包被拒,而地点已经进了地图、事件也追加了,`packs()` 却是空的。
    # **半装进去一份包比装不进去坏得多**:作者看到红灯,而世界里已经多了三个地点。
    # 判断**摘出来共用**(`plugins.plugin_version_errors`),不抄第二遍 ——
    # 两份判断迟早给出不同答案,而那种不一致会表现成"预检说没问题,装的时候还是失败"。
    if authored.get("plugins"):
        parsed = _parsed_plugins_or_none(_seed_entry_dicts(authored, "plugins"))
        if parsed is not None:
            problems = plugin_version_errors(parsed, plugin_store)
            if problems:
                raise PackInstallError(problems)

    # 剧情:**先验,再写**,而且 id 撞车当场拒。
    #
    # 🔴 `:beats` 里已经有的 id 不许再来一条:`beat_fired` 那份历史按 id 配对,
    # 两份包各有一条同名的拍,历史就再也分不出是谁响过了 —— 而分不出的样子是
    # "这一拍又响了一次"或者"这一拍再也不响",没有一处会报错。
    new_beats = list(authored.get("beats") or [])
    have_beats = RedisBeatsStore(redis, world_id).definitions()
    if new_beats:
        clash = sorted({str(b.get("id")) for b in new_beats}
                       & {str(b.get("id")) for b in have_beats})
        if clash:
            raise PackInstallError([
                f"这几拍的 id 这个世界里已经有了:{'、'.join(clash)} —— 一份新包不许重用旧 id"
                "(`beat_fired` 那份历史按 id 配对,重了就再也分不出是谁响过)。"
                "改一个名字;要改已经发出去的那一拍,那是另一件事(还没做)"
            ])
        try:
            BeatScript.from_data({"beats": have_beats + new_beats})
        except BeatScriptError as exc:
            raise PackInstallError(list(exc.errors)) from None

    tick = int(scheduler.clock)
    day = int(scheduler.world_time(tick).day)

    # 🔴 **写了 `since: "world"` 的拍,装进一个跑了很久的世界会在下一 tick 一起烧掉**
    # (2a-① 验收 C 逮的)。`trigger.at` 是「不早于」,而 `world` 那个逃生舱的零点
    # 就是世界第 0 天 —— 一份 `day: 0..6` 的包装进第 40 天的世界,七拍同一 tick 全响,
    # rc 0,而屏幕上还印着"`day: 0` 的那一拍下一 tick 就响"。
    # **装的时候手上有这三样(拍表、`since`、今天),所以算得出来** —— 算得出来
    # 就不许让它安静地发生。默认拒绝并逐条列出;`--force` 是"我就是要它们全响"。
    expired = expired_beats(new_beats, day=day, pack_day=day)
    if expired and not force:
        raise PackInstallError([
            f"这几拍装进去会在「下一 tick 一起响掉」(它们写着 `since: \"world\"`,"
            f"零点是世界第 0 天,而今天已经是第 {day} 天):{'、'.join(expired)}。"
            "要按「这份包落地那天」起算就把 `since` 去掉(那是缺省);"
            "真要让它们立刻全响,加 `--force`"
        ])

    # 🔴 **在册的人的 `personality`:一把 compare-and-set 的尺**(2a-②)。
    #
    # 「同一个 pack 就能覆盖」是错的,而它错得不报错:第 1 周的包给她写过一句人设,
    # 玩家跟她聊了三十天、她的人设被 `persona_update` 改过;第 2 周包一升级就把那
    # 三十天抹了,账面上什么都看不出来。**判据不是「我是同一个 pack」,是
    # 「这一格此刻的值,还等于我上一版写下去的那个值吗」。**
    #
    # 三种情形,三种下场:
    #   · 这个包上一版写的,而且**至今没被动过** → 覆盖(那就是"改自己发过的")
    #   · 被动过了 / 根本不是这个包写的(创世写的、别的包写的)→ **拒绝并报告**
    #   · `--force` → 覆盖,并在回执上留一格 `forced`(老板 D53 ④ 批的就是这一条)
    on_roster = set(scheduler.agents) | set(scheduler._memory_projection.agents)
    was_written = dict(
        ((scheduler._memory_projection.packs.get(pack_id) or {}).get("wrote") or {}
         ).get("personality") or {}
    )
    persona_wanted: dict[str, str] = {}
    persona_conflict: list[str] = []
    for entry in _seed_entry_dicts(authored, "agents"):
        aid = entry.get("id")
        said = str(entry.get("personality") or "").strip()
        if not isinstance(aid, str) or aid not in on_roster or not said:
            continue
        now = _current_personality(scheduler, aid)
        if said == now:
            continue                       # 一个字都没变,不必写一条事件
        mine = was_written.get(aid)
        if mine is not None and mine == now:
            persona_wanted[aid] = said     # 我上一版写的,至今没被动过
        else:
            # **冲突照记,即使这一趟带着 `--force`** —— 回执上那一格 `forced`
            # 说的是"这一趟强行覆盖了几件",而它得数得出来。
            persona_conflict.append(aid)
            if force:
                persona_wanted[aid] = said
    if persona_conflict and not force:
        raise PackInstallError([
            f"这几个人的人设**不是这份包上一版写下去的那一句**(或者写下去之后被世界"
            f"改过了):{'、'.join(sorted(persona_conflict))}。"
            "覆盖它等于把这中间发生的事抹掉,而账面上什么都看不出来 —— "
            "确实要覆盖就加 `--force`"
        ])
    skipped_persona = sorted(persona_conflict)
    # 🔴 **在册的人的记忆:只增不改**(2a-②)。记忆是**演化态** —— 改一条既有的
    # 等于伪造历史;而"这一周发生过一件事"是新的一条,加得进去。
    # 按 `(agent_id, summary)` 去重:同一份包装两遍不该让她记得两次。
    memories_wanted = [
        e for e in _seed_entry_dicts(authored, "memories")
        if str(e.get("agent_id") or "") in on_roster
    ]

    receipt: dict[str, Any] = {
        "pack": pack_id,
        "version": str(body.get("version") or ""),
        "note": str(body.get("note") or ""),
        "day": day,
        "tick": tick,
        "beats": [str(b.get("id")) for b in new_beats],
        "config": [],
        "world_setting": False,
        "agents": [],
        "locations": [],
        # **装不进去的那几段也要有一格。** 一张只列"装了什么"的回执,读起来像
        # "别的都装进去了" —— 而那正是这一族最贵的错法。
        "personality": [],
        "memories": 0,
        "skipped": {
            "personality": skipped_persona,
            "memories": 0,
            "reason": ("这几个人的人设不是这份包上一版写下去的那一句(或者写下去"
                       "之后被世界改过了)—— 覆盖它等于把这中间发生的事抹掉。"
                       "`--force` 才写")
            if skipped_persona else "",
        },
        "forced": bool(force and (expired or persona_conflict)),
    }

    with scheduler._lock:
        # ── ③ 其余段:开机那条路上**同一批**播种函数,`merge=True`(只填缺)。
        new_points = _seed_world_defs(location_store, bt_store, authored, merge=True,
                                      builtin_fallback=False)
        _seed_material_layer(economy_store, authored, merge=True)
        _seed_world_rules(rules_store, authored, merge=True)
        _seed_stock_visibility(authored, visibility_store, merge=True)
        _seed_stock_places(authored, visibility_store, merge=True)
        # 🔴 **喂的是 `merged`,不是 `authored`**(2a-① 验收:tool 真装第 2 周包
        # 逮的第 15 条)。`merged` 是"作者写的 kinds + 插件声明的那几行"合并之后
        # 的那一份 —— 创世那条路正是把 `world_seed` 整个换成它再往下走。
        # 上一版这里**判用 `merged`、写用 `authored`**:于是一份带新插件的包
        # `pack install` 退 0、`plugin list` 印得出那个种类和动词,而
        # `ontology --kind <它>` 答「这个世界里没有声明过这一类」—— **机制完全不
        # 生效而回执全是成功**,重开一次也没有;包里若带那个种类的实例则退 1 甩堆栈。
        # **两份东西必须来自同一次合并** —— 这个仓库为这句话红过一次(2026-08-28
        # 那条插件命名空间回归),而这一次是它的镜像:喂全集、判全集,写却喂了局部。
        redeclared = _seed_ontology(ontology_store, merged, fresh_world=True,
                                    merge=True, namespaces=namespaces)
        scheduler.world_rules = _load_world_rules(
            rules_store, warn=True,
            ticks_per_day=max(1, 1440 // max(1, scheduler._minutes_per_tick())))
        scheduler.ontology = _load_ontology(
            ontology_store, scheduler.world_rules, location_store, economy_store,
            namespaces=namespaces)
        _seed_stocks(merged, stock_store, ontology=scheduler.ontology,
                     tick=scheduler.clock, merge=True)
        if scheduler.ontology is not None:
            _apply_ontology(scheduler.ontology, stock_store, visibility_store,
                            tick=scheduler.clock, redeclare_kinds=redeclared,
                            rules=scheduler.world_rules)
        try:
            _install_plugins(scheduler, plugin_store, stock_store, visibility_store,
                             location_store, merged)
        except PluginError as exc:
            raise PackInstallError(list(exc.errors)) from None
        _seed_edges(scheduler, merged, merge=True)

        # ── ④ 新角色:走**中途入场那唯一的窄口**(`Scheduler.register`),
        # 和一条剧情拍的 `agent_join` 逐字同一条路。
        known = set(scheduler.agents) | set(scheduler._memory_projection.agents)
        newcomers: list[dict[str, str]] = []
        for entry in _seed_entry_dicts(authored, "agents"):
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
        for entry in newcomers:
            _seed_agent_tree(bt_store, entry["id"], authored)
            scheduler.register(scheduler._beat_agent_factory(entry))
            brain = scheduler.agents[entry["id"]]
            board = RedisBlackboard(redis, agent_key(world_id, entry["id"]))
            board.seed_missing(brain.agent.blackboard.snapshot())
            brain.agent.blackboard = board

        # ── ⑤ 三段这一条新开的。
        if new_beats:
            RedisBeatsStore(redis, world_id).append(new_beats)
        author_config = authored.get("config")
        wrote_config: dict[str, str] = {}
        config_before: dict[str, str] = {}
        if isinstance(author_config, dict) and config_store is not None:
            for key, value in author_config.items():
                # **先记下「之前是什么」再写** —— 停用那一刻要拿它回落
                # (K7:开关回落,而"回落"是回到**这个世界原来的样子**,
                # 不是回到引擎默认值)。`world_value` 只看世界那一层,
                # 正是"作者动过没有"这个问题。
                had = config_store.world_value(str(key), None)
                try:
                    config_store.set(str(key), value)
                except Exception as exc:  # noqa: BLE001 - 一个坏键不该掀翻整包
                    logger.warning("内容包里的配置 %s 没写进去:%s", key, exc)
                    continue
                receipt["config"].append(str(key))
                wrote_config[str(key)] = repr(config_store.world_value(str(key), None))
                config_before[str(key)] = "" if had is None else repr(had)
        setting = authored.get("world_setting")
        if isinstance(setting, str) and setting.strip() and prompt_store is not None:
            prompt_store.set("world.setting", setting.strip())
            receipt["world_setting"] = True

        # ── ⑤b 在册的人:人设(CAS 过了的那几个)与记忆(只增不改)。2a-②
        #
        # **走 `state_change/persona_update` 这条已有的路**,不新造:名册与人设是
        # `agent_join` / `persona_update` 折出来的投影,直接改黑板的话重开一次就
        # 回去了,而"她今天说话不一样了"这件事在日志里没有任何来路。
        for aid, said in sorted(persona_wanted.items()):
            event_log.append({
                "ts": tick, "who": aid, "type": "state_change",
                "payload": {"kind": "persona_update", "spec": {"personality": said},
                            "pack": pack_id},
            })
            receipt["personality"].append(aid)
        # 记忆**只增不改**,按 `(agent_id, summary)` 去重 —— 同一份包装两遍不该
        # 让她记得两次。已有的一条一个字不动:记忆是演化态,改它就是伪造历史。
        have_summaries = {
            (str(e.payload.get("agent_id") or ""), str(e.payload.get("summary") or ""))
            for e in event_log.replay() if e.type == "memory_seed"
        }
        fresh_memories: list[str] = []
        for mem in memories_wanted:
            aid = str(mem.get("agent_id") or "")
            summary = str(mem.get("summary") or "")
            if not summary or (aid, summary) in have_summaries:
                continue
            have_summaries.add((aid, summary))
            try:
                importance = float(mem.get("importance", 0.5))
            except (TypeError, ValueError):
                importance = 0.5
            event_log.append({
                "ts": tick, "who": aid, "type": "memory_seed",
                "payload": {"agent_id": aid, "kind": str(mem.get("kind", "seed")),
                            "summary": summary, "importance": importance,
                            "anchor": _coerce_bool(mem.get("anchor", False)),
                            "pack": pack_id},
            })
            fresh_memories.append(aid)
        receipt["memories"] = len(fresh_memories)

        # ⚠️ **回执要在落账之前填好** —— `pack_installed` 的 `sections` 记的就是它。
        receipt["agents"] = [e["id"] for e in newcomers]
        receipt["locations"] = list(new_points)

        # ── ⑥ 落账:**事件先记,再折。**
        joined = _join_authored_additions(
            event_log, scheduler, authored, location_store, newcomers, new_points,
        ) if (newcomers or new_points) else set()
        # **落账写的是回执**(真的落地了什么),文件里那份另起 `declared` ——
        # 两份真相那一格就是这么来的(2a-① 验收 C)。
        _record_pack_installed(event_log, authored, day=day, tick=tick,
                               wrote={"personality": dict(persona_wanted),
                                      "config": dict(wrote_config),
                                      "config_before": dict(config_before)}, landed={
            k: v for k, v in (
                ("beats", receipt["beats"]),
                ("agents", receipt["agents"]),
                ("locations", receipt["locations"]),
                ("config", receipt["config"]),
                ("world_setting", ["world.setting"] if receipt["world_setting"] else []),
            ) if v
        })
        persisted = event_log.replay()
        scheduler.reset_projection(persisted)   # 水位跟着挪
        scheduler.load_persisted_events(persisted)
        # **刚追加的那几条 `memory_seed` 自己折一次。** `_rebuild_memories` 见了
        # 非空表就掉头(记忆是持久状态,重放一遍等于把她的一生按今天的触发器重新
        # 裁一遍),所以这条路上得自己折 —— 和新人那一半逐字同一个理由。
        fold_for = set(joined) | {aid for aid in fresh_memories}
        # ⚠️ **这里没有 `count() > 0` 那道条件,而开机那条路上有** —— 那条的理由是
        # `_rebuild_memories` 紧接着会把空表整份折一遍(折两次就是每人两份)。
        # 装包这条路**根本不调 `_rebuild_memories`**,所以不折就是没人折:
        # 日志里有、库里没有,她开口时对刚发生的事一无所知,而回执写着装进去了。
        # 幂等靠 `event_seq`,重复调用是安全的。
        if fold_for and scheduler.memory_store is not None:
            _fold_seeded_memories(scheduler.memory_store, persisted, fold_for)

        # ── ⑦ 剧情:**重建导演,而"响过哪几拍"从日志重放** —— 两份真相里存一份。
        if new_beats:
            script = BeatScript.from_data(
                {"beats": RedisBeatsStore(redis, world_id).definitions()})
            fired = {
                (e.payload.get("beat_id"), str(e.payload.get("for") or ""))
                for e in persisted
                if e.type == "beat_fired" and e.payload.get("beat_id")
            }
            scheduler.beat_director = BeatDirector(script, fired=fired)

    logger.info(
        "内容包 %r(%s)装进了世界 %r:第 %d 天 · %d 拍 · %d 个开关 · %d 个新角色 · %d 个新地点",
        pack_id, receipt["version"], world_id, day, len(receipt["beats"]),
        len(receipt["config"]), len(receipt["agents"]), len(receipt["locations"]),
    )
    return receipt


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


def _seed_edges(scheduler: Any, world_seed: dict[str, Any] | None, *,
                merge: bool) -> None:
    """作者层种下的那几条边(3.8.0,收件箱 D44)。

    **走 `Scheduler.apply_edge_effect`,不自己 `store.link`。** 那一个函数里住着
    三件这条路同样要的东西:`exclusive` / `exclusive_to` 的约束、声明过的事实
    **带命名空间**落地(`f"{plugin}.{key}"`)、`symmetric` 建两条而不是一条。
    自己写一遍 link 的下场是那三件**各漏一件都不报错** —— 一个 `symmetric` 的
    "结拜"只连一个方向,而反着查的那一半永远是空的。

    ## 只填缺,不覆盖 —— 而这一层的粒度是**每一种边**,不是每一条

    - **创世**(`merge=False`):照单全种。
    - **一次编辑**(`merge=True`,`--world-file` 装进一个跑过的世界):只种
      **这个世界里还一条都没有的那几种**;已经有行的那一种**整种跳过**,并
      **点名说出来**。

    🔴 **粒度选每一种而不是每一条,是量出来的,不是推的。** 舰队上的世界容器
      **每次开机都带着 `--world-file`**(2026-08-31 在真进程上看过:
      `world_server.py … --world-file /data/world.cyberworld`),于是"这条边不在
      就补上"会让**每一次重启都把运行期断掉的边接回来** —— 一个退了师门的人,
      容器一重启又是青云门弟子,而屏幕上没有一处会说。边不进事件日志
      (`apply_edge_effect` 一条事件都不发),所以引擎手上**没有"有人断过它"这份
      记录**,分不出"还没连"和"连过又断了"。整种跳过分得出:那一种只要还有一行,
      这个世界就已经在过自己的日子了。
    ⚠️ **代价是"编辑一份文件加一个第四位创派弟子"这件事做不到**,而这一条
      **不无声**(下面那句 warning 逐条报数并告诉你该走哪条路)—— 和节拍那一段
      逐字同一个安排:一份不合并的东西,宁可大声不做,不要安静地做一半。
    ⚠️ **剩下的那个角落,实测过,如实写在这儿**:一种边**每一行都被运行期断掉**
      之后,再拿同一份文件开机,它会**整批重新种下** —— 因为"一行都没有"在这一层
      读作"这个插件在这个世界里还没开张"。它不是漏了,是这个粒度必然带的那一格:
      引擎手上没有"有人断过它"的记录(边不进事件日志),而在"一条都不剩"这一点上,
      "还没连"和"全断了"**在库里是同一个样子**。要它别回来,就别在世界文件里
      种它(走 `link` 动词),或者种完之后把那几条 `edge` 记录从文件里去掉。
    """
    # ⚠️ **`world_seed` 可以是 `None`** —— 一个跑过的世界导出来一条作者记录都没有,
    # 而 `_load_world_file` 对那种包答的就是 `None`("没有种子,不是一个空种子")。
    # 这一行是老闸逮出来的(`test_跑过的世界导出来_两条路都收`:离线说行、开机
    # `'NoneType' object has no attribute 'get'`)—— 而它逮到的正是这一族最典型的
    # 那种漏:**新写的那一段只想着"作者写了东西"的世界。**
    if not world_seed:
        return
    rows = _seed_entry_dicts(world_seed, "edges")
    if not rows:
        return
    store = getattr(scheduler, "edge_store", None)
    if store is None:
        return
    have: dict[str, int] = {}
    if merge:
        for edge_type in {str(r.get("type") or "") for r in rows}:
            have[edge_type] = len(store.all(edge_type)) if edge_type else 0
    planted = 0
    skipped: dict[str, int] = {}
    for row in rows:
        edge_type = str(row.get("type") or "")
        if have.get(edge_type):
            skipped[edge_type] = skipped.get(edge_type, 0) + 1
            continue
        ok = scheduler.apply_edge_effect(
            {"op": "link", "type": edge_type,
             "from": str(row.get("from") or ""), "to": str(row.get("to") or "")},
            {},
        )
        if ok:
            planted += 1
        else:
            # 走到这儿说明那道闸放行了、而内核仍然没连上。**已知的走法不止一种**
            # (上一版这儿写着"唯一已知",不准):`exclusive` / `exclusive_to` 在
            # **库里已有的行**上撞了也会走到这儿 —— 那道闸只查得了这份文件自己
            # 肚子里的冲突,查不了目标世界里已经躺着的那一条。**不吞**:
            # 一条没连上的创世边和一条连上了的,在屏幕上长得一模一样。
            logger.warning(
                "作者层这条边没连上:%s %s → %s —— 它过了校验却没落库,"
                "请把这句话连同世界文件一起报给引擎",
                edge_type, row.get("from"), row.get("to"))
    if planted:
        logger.info("种下 %d 条作者层的边", planted)
    for edge_type, count in sorted(skipped.items()):
        # **不无声。** 一句话不说的样子是"我把这几条种进去了" —— 而拿一份改过的
        # 世界文件去编辑一个跑着的世界的人,会以为第四位弟子已经在里面了。
        logger.warning(
            "这个世界里 `%s` 已经有 %d 条边,文件里那 %d 条「没有种进去」 —— "
            "边只在这一种还一条都没有时才整批种下(理由:边不进事件日志,"
            "引擎分不出「还没连」和「连过又断了」,逐条补等于每次重启都把"
            "运行期断掉的边接回来)。要再连人,走一个带 `link` 的动词/触发器;"
            "要重来一遍,`world drop` 之后重建",
            edge_type, have.get(edge_type, 0), count)


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
    """这个世界住着谁 —— 一个 .cyberworld 报不出自己的名册(#6;3.6.0 仍如此)。

    `world inspect` 读的是 manifest,那上面只有封皮(要哪个引擎、叫什么、多大),
    没有一行说得出住着谁 —— 所以名册只能开着世界问。

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
    # **逐格取,不 `**place`。** `map_data()` 的 `places` 行是**契约**,它会长
    # (地点的两格图就是这么加进去的);而这张字符画是**赠品**,画不出图片。
    # `MapPlace(**place)` 让"契约加一格"变成"`anima-world map` 当场 TypeError" ——
    # 加图那一轮真的这么炸过,而当时橱窗里一格图都没有,所以没有一条测试碰得到它。
    places = [
        MapPlace(
            id=place["id"], name=place["name"], kind=place["kind"],
            x=place["x"], y=place["y"], w=place.get("w"), h=place.get("h"),
        )
        for place in data["places"]
    ]
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
        print("[contact] --inbox 要 --player:收件箱是「某一个人」的",
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
            # 记不记得住。不写的话这一行整个不印 —— 印一句"importance 无"会让读的
            # 人以为有个默认值在那儿,而真相是这一层压根没铺开。
            if a.get("importance") is not None:
                print(f"         ✦ 在场的人会记住这一下(importance "
                      f"{a['importance']:g}),做的人是玩家也一样")
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
    `player_move` 是宿主的可选调用,**写这条命令时(3.2.0)线上根本没人调**,于是
    "异地"是每一次调用的默认值 —— 那道闸打开的当天,`give` 和一起做事全线开始拒绝,
    而回执看上去像是玩家自己站错了地方。

    ⚠️ **那句实况已经过期,而这道命令的价值恰恰因此还在**(2026-08-20 复核):
    站点 2026-08-13 前后接上了 `player_move`(落脚 / 重连 / 世界重启复位三处),
    在场行有 15 分钟 TTL,所以今天的真相是"门只对最近 15 分钟内进过世界的人开" ——
    不是"没人有位置",而是"谁有、谁没有,得当场量一次"。**这道命令就是那把尺子。**

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
    # ⚪ **这一栏印的是裸地点 id**(`cafe`/`home`),和 `roster`/`map` 那两条印人话
    # 地名的命令不一致。**核过,有意不改**(第七轮 2026-08-20 记):这是运维面 ——
    # 用它的人下一步要去 `--json`、去配置、去日志里找同一个 id,把 id 翻成人话反而
    # 要他自己再翻回来。**结论记在原地,免得下一个人再核一遍**;真要改,连
    # `report["agents"]` 那份 JSON 一起想,别只改屏幕。
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
        # 同一条纪律的第二处:**三种来路,别只说宿主那一种**(下面那段说明逐字同理)。
        where = row["location"] or "✗ 世界不知道(他走了 / 在场记录过期 / 宿主没落过 player_move)"
        face = "、".join(row["face_to_face"]) or "没有人"
        print(f"  {row['player_id']:<{pad}}{where}")
        print(f"  {'':<{pad}}此刻面对面:{face}"
              f"{'' if row['present'] else '(而且他不在在场名册上)'}")
    print()
    # ⚠️ **这一句不许省,但 3.2.0 起说的是另一件事。** 从前它警告"位置只活在这个
    # 进程里";现在在场落 Redis 带 TTL,于是要说的是**为什么会没位置** ——
    # 没人调过 `player_move`,或者他很久没动静、TTL 过了。不说的话,读的人会
    # 照着一个假警报去改宿主。
    # ⚠️ **是三种,不是两种**(3.6.0 第六轮 2026-08-20 补上第三种)。漏掉的那种是
    # `player_leave` —— 他自己走了,而少说这一种会让读的人跑去查一个接得好好的宿主。
    # ⚠️ **这儿从前写着「只剩这一处停在两种上」,而那句话当场就是假的**(第七轮
    # 2026-08-20 改):`docs/REFERENCE.md` 那时还有一处停在两种,被验收员当场证伪。
    # **别在注释里写「只剩」「已经全统一了」这种全称断言** —— 夸奖比控诉更危险:
    # 控诉会让人去查,夸奖会让人放心。要知道还剩几处,敲这条(答 0 行才算统一):
    #     git grep -nE '(只剩|只有)两种可能' -- anima_world/ docs/
    # (括号是承重的:写成没括号的那两个词,这行注释自己就会命中自己。)
    print("说明:在场与位置住在 Redis 上(`anima:{world_id}:player:*`),带过期时间 —— "
          "跨进程、扛重启。这里没有位置有三种可能:① 他 player_leave 过,自己走了;"
          "② 他很久没动静,在场记录过了 15 分钟没续上;③ 宿主确实没落过 player_move。"
          "前两种里宿主刚刚才调过。名单是从落库的联系态补齐的。")
    if report["unplaced"]:
        # **不许只报数字。** 这一句是这道命令唯一真正的产出:读的人要知道下一步
        # 该改哪儿,而不是知道有几个玩家没位置。
        tail = (
            "这道闸正在拒绝调用 —— 确认跑世界的那个进程每轮都调 player_move。"
            if report["enforced"] else
            "开 presence.enforce_colocation 之前,先让「跑世界的那个进程」每轮调一次 "
            "player_move,否则一开就是 give / 一起做事全线拒绝。"
        )
        print(f"\n⚠ {report['unplaced']} 个玩家在世界里没有位置。{tail}")
        print("  这里面有一部分只是「很久没动静」(在场带 TTL):真的没接 player_move 的话,"
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
    """和一个角色对话:**REPL,或者一次一问**(#6;`--message` 是 3.7.0 加的)。

    引擎最像人的那件能力,过去只有写 Python 才够得着:`World.chat_reply` →
    `record_chat_turn` 早就齐全,缺的只是一道门。这里就是那道门,没有新的引擎
    能力。

    **时钟不走**:对话发生在世界的此刻,退出时世界还停在原地。要让世界一边活
    一边聊,那是宿主应用的事(`World.open` + `start_clock` + `chat`)——一个
    CLI 不该在你打字时偷偷推进别人的世界。

    转录留在这个进程里,每轮只把最近若干条传进世界(纪律:完整转录归宿主)。
    每说完一轮就 `record_chat_turn`,于是**说完一句话那一刻 db 就是完整的**。

    ## `--message`:一次一问(3.7.0,看板 D5)

    在这之前这条命令**只有 REPL**,而创作台的"一键试玩 / 性格试镜"要的是
    「说一句、拿到回话、退出」—— 它只能去驱动一个交互式 REPL:喂 stdin、
    按提示符切分 stdout。那条路**脆在排版上**:抬头、降级提示、`名字 > ` 这几段
    任何一版换了样子,子进程那一侧就切错,**而它不会报错,只会把半句抬头当成
    她说的话**。库里 `World.chat_reply` 早就够,缺的一直只是一道门。

    - `--message` **可重复**,按顺序一句一轮,共用同一份进程内转录 ——
      多轮的连贯性因此还在(世界只收当轮有限历史,完整转录归宿主,这条没变)。
      给一句就是一句,不落进 REPL、不读 stdin、不要 tty。
    - `--json` 时 **stdout 上只有那一份 JSON**:抬头与提示一律闭嘴。混着印的话,
      调用方就得先剥壳,而剥壳的写法迟早在某一版排版上碎掉 —— 那正是这道门要
      治的病本身。
    - 回执里带 `degraded_reason`:一个跑在 Mock 上的世界照样回得出话,而那几句是
      **模板**。**降级绝不无声** —— 拿模板当"她说的话"去做判断,和拿一份假绿灯
      出包是同一种错。
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
        # `--message --json` 时 stdout 上**只有那一份 JSON** —— 抬头和提示混进去,
        # 子进程那一侧就得先剥壳,而剥壳的写法迟早在某一版的排版上碎掉。
        quiet = bool(getattr(args, "messages", None)) and bool(getattr(args, "as_json", False))
        if not quiet:
            print(onboarding.rule(
                f"{info.get('name', agent_id)} @ {places.get(where, where) or '?'}"
            ))
        if degraded and not quiet:
            print(f"  {onboarding.yellow('这个世界正跑在 Mock 上')}({degraded})——"
                  f"回复会是模板。配一个:anima-world config set llm.api_key sk-…")
        if degraded:
            # 没有 LLM 时,关系判定每一轮都要抱怨一次"读不出 JSON"—— 那是上面
            # 这句话的必然结果,不是新消息,而它会横插在对话中间。真 LLM 下的
            # 同一句话是真信号,所以只在已降级时闭嘴。
            logging.getLogger("anima_world.relationship_judge").setLevel(logging.ERROR)
        scripted = [str(m) for m in (getattr(args, "messages", None) or []) if str(m).strip()]
        scripted_mode = bool(getattr(args, "messages", None))
        if not scripted_mode:
            print(f"  {onboarding.dim('说点什么。空行或 Ctrl-D / Ctrl-C 结束。')}\n")

        history: list[dict[str, str]] = []
        turns = 0
        said_rows: list[dict[str, Any]] = []
        while True:
            if scripted:
                # **一次一问**:给了 `--message` 就不进 REPL —— 说完这几句就退。
                # 见 `_chat_one_shot_note`(这条门为什么存在,以及它为什么可重复)。
                line = scripted.pop(0).strip()
            else:
                if scripted_mode:
                    break            # 脚本模式:说完就走,不落进 REPL
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
            if not (scripted_mode and args.as_json):
                print(f"{info.get('name', agent_id)} > {reply}\n")
            history.append({"role": "assistant", "content": reply})
            # 一轮一记:关系判定在这里发生,世界也在这里落盘。
            world.record_chat_turn(agent_id, args.player_id, history[-2:], meta=meta)
            turns += 1
            said_rows.append({"said": line, "reply": reply, "meta": dict(meta)})

        if scripted_mode and args.as_json:
            print(json.dumps({
                "operation": "chat",
                "world_id": world_id,
                "agent_id": agent_id,
                "agent_name": info.get("name", agent_id),
                "player_id": args.player_id,
                "turns": said_rows,
                # **降级绝不无声**:一个跑在 Mock 上的世界照样回得出话,而那几句
                # 是模板 —— 拿它当"她说的话"去做判断的下场,和拿一份假绿灯出包一样。
                "degraded_reason": degraded or "",
            }, ensure_ascii=False, sort_keys=True))
        elif turns:
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


# 「有人正跑着这个世界」这句提醒后半句说什么,**按出口分岔** —— 因为后果真的不同。
# ⚠️ 这一格 2026-08-27 由 C 视角验收挑出来:那句话原先写死成 config 的语义
# (「那个进程不会重读,要下次重启才生效」),而 `world setting` 挂上来之后它就成了
# **一句假话** —— `:prompts` 没有进程内缓存,`ChatService` 每次拼提示词都现读
# (`chat_service.py` 的 `self._prompt_store.get("world.setting", …)`),所以热改
# **当场就生效**。一句写死的后半句,换一扇门就可能反过来。
_LIVE_EFFECTS = {
    # ⚠️ **这句 3.10.0 翻面了,留着当记号**:上一版写的是「写进去的配置那个进程
    # 不会重读,要下次重启才生效」—— 那是 `ConfigStore` 只在开机 hydrate 一次的
    # 实况,而 3.10.0 给它加了 `:config_rev`。**一句没跟着代码改的提示,
    # 比没有这句提示更坏**:它会让人去重启一个本来不用重启的世界。
    "config": "跑着的那个进程会在下一 tick 上读到新的(3.10.0 起),不用重启。",
    # 提示词是现读的,所以这句对世界观也是真的 —— 只是方向相反。
    "world setting": "世界观是现读的,那个进程下一次拼提示词就会用上新的。",
}


def _warn_if_live(redis: Any, world_id: str, *, outlet: str = "config") -> None:
    """对一个正在跑的世界动手之前,说一声。

    只提示不拒绝:进程崩掉标记就陈旧,拿陈旧标记去拒绝操作,等于在真出事那天把人
    挡在门外。**后半句按 `outlet` 分岔**(`_LIVE_EFFECTS`):同一句"有人在跑",
    对配置和对世界观意味着相反的两件事,而写死其中一句就是对另一扇门撒谎。

    ⚠️ **`owner_pid` 关世界时并不清**(它是提示不是锁),所以这个戳可能是上一次
    命令留下的陈旧值 —— 这是有意的,别把它读成"此刻真有人在跑"。
    """
    owner = _live_owner(redis, world_id)
    if owner is None:
        return
    pid, host = owner
    effect = _LIVE_EFFECTS.get(outlet, _LIVE_EFFECTS["config"])
    print(
        f"  {onboarding.yellow('这个世界正被 pid ' + str(pid) + ' @ ' + str(host) + ' 跑着')}"
        f" —— {effect}",
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
            print(onboarding.dim("  它「不进世界文件」:打包发出去的世界不该带着你的钥匙"))
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

    **一次只改一个人。** 生产上这条路的入口是运维台的一次性容器,argv 由具名参数
    白名单生成;而 argv 是数组传递,中文和标点直接当一个元素传,不过 shell。

    ⚠️ **`--portrait` 够不到契约公布的那个 1 MiB**:Linux 的 `MAX_ARG_STRLEN` 把
    单个 argv 元素封在 128 KiB(实测 122902 字节过、177518 字节炸),再往上
    `execve` 直接 `E2BIG`,壳报「参数列表过长」并给 rc 126 —— **那是操作系统在
    说话,不是引擎**:引擎连被叫起来的机会都没有,于是没有回执、没有退出码 2、
    没有一句能翻译给运维的人的话。一条 `data:` URI 到 1 MiB 是常事(base64 之后
    约是原图的 4/3),所以契约上"能写 1 MiB"和这扇门上"能传 128 KiB"之间那段,
    从前是个只有撞上去才知道的坎。`--portrait-file` 就是补这一段:URI 走文件或
    标准输入进来,不过 argv。**文件里装的是那条 URI 文本,不是图片字节** ——
    引擎不碰字节(嗅 MIME、转 base64 都是创作台的活,见 `media.py`)。

    判断都在 `World.set_card` 的 docstring 里(**覆盖**、部分合并、`--clear` 单独
    一格、幂等)。这里只管三件事:参数互斥、退出码、印给人看。
    **退出码 2 = 「我听懂了,但我不干」**(运维台把它翻译成 409);编一个空回执
    出去的话,运维的人会以为改成功了。
    """
    from anima_world.api import World

    portrait = args.portrait
    source = getattr(args, "portrait_file", None)
    if source is not None:
        if portrait is not None:
            print("[agent] --portrait 和 --portrait-file 不能一起给:"
                  "两句话都在说这一格写成什么 —— 引擎挑哪句都是猜。",
                  file=sys.stderr)
            return 2
        portrait = _uri_from_file(source, cmd="agent", value_flag="--portrait")
        if portrait is None:
            return 2

    given = {
        key: value
        for key, value in (
            ("billing", args.billing), ("tagline", args.tagline), ("portrait", portrait)
        )
        if value is not None
    }
    if args.clear and given:
        print("[agent] --clear 和 --billing/--tagline/--portrait(-file) 不能一起给:"
              "一句是「删掉这张卡」,一句是「这张卡写成这样」—— 引擎挑哪句都是猜。",
              file=sys.stderr)
        return 2
    if not args.clear and not given:
        print("[agent] 什么都没给 —— --billing / --tagline / --portrait / "
              "--portrait-file / --clear 至少给一个。"
              "一次什么也没改的「成功」读起来和改成功了一模一样。",
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


def _uri_from_file(source: str, *, cmd: str, value_flag: str) -> str | None:
    """从一个文件(`-` = 标准输入)里读**那条图的 URI 文本**;读不成返回 `None`。

    存在的理由见 `run_agent_set_card` 的 docstring:Linux 的 `MAX_ARG_STRLEN` 把
    单个 argv 元素封在 128 KiB,而契约公布的上限比它大(立绘 1 MiB、地点每格
    256 KiB)—— 那一段距离只有撞上去才知道,而撞上去时报错的是操作系统。

    **一份,不是两份。** 立绘和地点的两格图走的是同一道闸、同一个理由,这四条
    拒绝也逐字相同;各写一份的话,下一次收紧只会收紧其中一处,而另一处不报错。
    `value_flag` 只进那句"要抹掉这一格请明写 X ''" —— 每扇门的那个开关名不一样。

    **文件里装的是那条 URI 文本,不是图片字节**:引擎一个字节都不碰(嗅 MIME、
    转 base64 是创作台的活)。
    """
    try:
        raw = sys.stdin.read() if source == "-" else Path(source).read_text("utf-8")
    except OSError as exc:
        print(f"[{cmd}] 读不了 {source}:{exc}", file=sys.stderr)
        return None
    except UnicodeDecodeError:
        # 有人会把 PNG 本身喂进来。**说清楚这里要的是哪一样东西** ——
        # 「不是 UTF-8」这句话对着一个刚把图片拖进来的人什么也没解释。
        print(f"[{cmd}] {source} 不是文本 —— 这里要的是那条 URI(比如 "
              "`data:image/png;base64,…` 或一条 https 链接),不是图片字节;"
              "转 base64 是创作台那侧的活。", file=sys.stderr)
        return None
    uri = raw.strip()
    if not uri:
        # 空文件几乎总是「写失败了 / 路径写错了」,而不是「我要抹掉这一格」。
        # 当成后者的话,一次失败的写会安静地删掉线上那张图。
        print(f"[{cmd}] {source} 是空的 —— 要抹掉这一格请明写 {value_flag} ''。",
              file=sys.stderr)
        return None
    if any(ch.isspace() for ch in uri):
        # 一条 URI 里不该有空白。掐掉两头之后还剩空白 = 文件被编辑器折了行,
        # 而闸只看 scheme 和字节数,这种 URI 它照放 —— 出去就是一张断的图。
        print(f"[{cmd}] {source} 里那条 URI 中间有空白(换行?)—— 一条 URI "
              "不该断行,多半是被编辑器折了。整条写成一行再来一次。",
              file=sys.stderr)
        return None
    return uri


def run_location(args: argparse.Namespace) -> int:
    """`anima-world location set-image` —— 地图数据的写口。

    和 `agent` / `memory` 同一个形状:算法在 `World` 上,CLI 只开世界、印出来、
    定退出码。**改之前先 `--dry-run` 看一眼** —— 它动的是作者写的东西。
    """
    command = getattr(args, "location_command", None)
    if command == "set-image":
        return run_location_set_image(args)
    print("[location] 只有 set-image 一个子命令", file=sys.stderr)
    return 2


def run_location_set_image(args: argparse.Namespace) -> int:
    """`anima-world location set-image` —— 给**一个已经跑着的世界**里的地点换图。

    补的是地点两格图那一轮**明着欠下**的一节:作者层是"只填缺、不覆盖",而合并
    的粒度是**整个地点行**,于是拿一份补了图的世界文件去编辑一个已经跑起来的
    世界,那两格一个都装不进去 —— 作者写得进、`validate world` 放行、包也导得出,
    就是到不了玩家眼前。上一轮的处置是**不让它无声**(逐个地点 `logger.warning`
    + `validate world --edit` / `world check --edit` 也说),但一句警告不是一扇门:
    退出码仍然是 0,而事没做。这条命令是那扇门。

    **一次只改一个地点。** 生产上这条路的入口是运维台的一次性容器,argv 由具名
    参数白名单生成;而 argv 是数组传递,中文和标点直接当一个元素传,不过 shell。

    ⚠️ **`--map-image` / `--scene-image` 够不到契约公布的那 256 KiB**,理由和
    `--portrait` 逐字相同(`MAX_ARG_STRLEN`,报错的是操作系统不是引擎)——
    所以两格各配一扇 `--*-file`。**标准输入只有一份**:两格都写 `-` 的话第二格
    会读到空,而空在这条路上的含义是"抹掉这一格" —— 一次手滑会安静地删掉线上
    那张图,所以当场拒绝。

    判断都在 `World.set_location_image` 的 docstring 里(**覆盖**、两格分开合并、
    空串抹一格、`--clear` 抹两格、只写这两格、幂等)。这里只管三件事:参数互斥、
    退出码、印给人看。**退出码 2 = 「我听懂了,但我不干」**;编一个空回执出去的话,
    运维的人会以为改成功了 —— 而那正是这一整轮要修的病。
    """
    from anima_world.api import World

    given: dict[str, str] = {}
    stdin_taken = ""
    for key in LOCATION_IMAGE_KEYS:
        flag = "--" + key.replace("_", "-")
        inline = getattr(args, key, None)
        source = getattr(args, f"{key}_file", None)
        if inline is not None and source is not None:
            print(f"[location] {flag} 和 {flag}-file 不能一起给:"
                  "两句话都在说这一格写成什么 —— 引擎挑哪句都是猜。",
                  file=sys.stderr)
            return 2
        if source is not None:
            if source == "-":
                if stdin_taken:
                    print(f"[location] {flag}-file 和 {stdin_taken}-file 都写着 "
                          "`-`,而标准输入只有一份 —— 第二格会读到空,而空在这里"
                          "的意思是「抹掉这一格」。一次只从标准输入读一格。",
                          file=sys.stderr)
                    return 2
                stdin_taken = flag
            value = _uri_from_file(source, cmd="location", value_flag=flag)
            if value is None:
                return 2
            given[key] = value
        elif inline is not None:
            given[key] = inline

    flags = "/".join("--" + key.replace("_", "-") for key in LOCATION_IMAGE_KEYS)
    if args.clear and given:
        print(f"[location] --clear 和 {flags}(-file) 不能一起给:"
              "一句是「这两格都抹掉」,一句是「这一格写成这样」——引擎挑哪句都是猜。",
              file=sys.stderr)
        return 2
    if not args.clear and not given:
        print(f"[location] 什么都没给 —— {flags} / 它们的 -file / --clear "
              "至少给一个。一次什么也没改的「成功」读起来和改成功了一模一样。",
              file=sys.stderr)
        return 2

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "location"):
        return 2
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[location] {exc}", file=sys.stderr)
        return 2
    try:
        receipt = world.set_location_image(
            args.location, given or None,
            clear=bool(args.clear), dry_run=bool(args.dry_run),
        )
    except KeyError as exc:
        # `KeyError` 的 str() 会加一层引号 —— 拿 args[0] 才是那句人话。
        print(f"[location] {exc.args[0]}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[location] {exc}", file=sys.stderr)
        return 2
    finally:
        world.close()

    if args.as_json:
        print(json.dumps(
            {"operation": "location set-image", "world_id": world_id, **receipt},
            ensure_ascii=False, indent=2))
        return 0

    where = f"{receipt['name']}({receipt['location_id']})"
    if not receipt["changed"]:
        # **说出「没有变化」** —— 一声不吭和改成功了长得一模一样,而这条命令
        # 最常见的用法就是运维照着单子一个一个敲过去。
        print(f"[location] {where} 的图没有变化,一个字都没写。")
        return 0

    before, after = receipt["before"], receipt["after"]
    verb = "要改" if receipt["dry_run"] else "改了"
    print(f"[location] {where} 的图{verb}:")
    for key in LOCATION_IMAGE_KEYS:
        was, now = before.get(key), after.get(key)
        if was == now:
            continue
        # **印出来的 URI 要掐短**:一条 `data:` 图到 256 KiB 是允许的,原样吐出去
        # 会把终端刷没,而人要看的只是"从哪一条换成了哪一条"。
        print(f"    {key:12} {clip_uri(was) if was else '(没写)'}"
              f" → {clip_uri(now) if now else '(删掉)'}")
    if receipt["dry_run"]:
        print("[location] --dry-run:一个字节都没写。")
    else:
        print(f"[location] 看一眼:anima-world map --world-id {world_id} --json")
    return 0


def run_world_setting(args: argparse.Namespace) -> int:
    """`anima-world world setting` —— 读 / 改**一个已经跑着的世界**的世界观。

    补的是收件箱 D4 那一格,而它欠得比角色卡和地点图都久:世界观是作者层的一个段
    (`world_setting`),而它**只在创世那一刻**落进 `:prompts` 的 `world.setting`
    (`_seed_world_setting` 被 `if not persisted` 把着门)。也就是说一个**已经建好、
    有人在玩**的世界改不了自己的世界观 —— 连拿一份改过的 `.cyberworld` 走
    `--world-file` 都不行,那条路对这一段不生效,而且不报错。

    于是创作台唯一的办法是 `world drop` **把整个世界抹掉重建**(实测:
    `anima_studio/infra/workspace.py::_drop_world`)—— 玩家的记忆、关系、事件日志、
    跑了几十个世界日的历史,全为了改一段话陪葬。**这条路是引擎逼出来的。**

    **不给 `--set/--set-file/--clear` 就是只读**,和 `world drop` 不带 `--yes` 只数
    是同一条:一条会改东西的命令,它的"什么都不给"必须是安全的那一边。

    判断都在 `World.set_world_setting` 的 docstring 里(**覆盖**、`--clear` 回落到
    引擎内置那份而不是空、拒绝空白、幂等)。这里只管四件:参数互斥、把文件读进来、
    退出码、印给人看。**退出码 2 = 「我听懂了,但我不干」。**

    ⚠️ **`--set-file` 不是可有可无的**:世界观动辄几百上千字,而 Linux 的
    `MAX_ARG_STRLEN` 把单个 argv 元素封在 128 KiB —— 和 `--portrait-file` 逐字
    同一个理由,撞上去时报错的是操作系统,不是引擎。
    """
    from anima_world.api import World

    inline, source = args.set_text, args.set_file
    if inline is not None and source is not None:
        print("[world setting] --set 和 --set-file 不能一起给:"
              "两句话都在说世界观写成什么 —— 引擎挑哪句都是猜。", file=sys.stderr)
        return 2
    text: str | None = inline
    if source is not None:
        try:
            text = sys.stdin.read() if source == "-" else Path(source).read_text("utf-8")
        except OSError as exc:
            print(f"[world setting] 读不了 {source}:{exc}", file=sys.stderr)
            return 2
        except UnicodeDecodeError:
            print(f"[world setting] {source} 不是 UTF-8 文本 —— "
                  "这里要的是那段世界观本身,一段话。", file=sys.stderr)
            return 2

    if args.clear and text is not None:
        print("[world setting] --clear 和 --set/--set-file 不能一起给:"
              "一句是「回落到引擎内置那份」,一句是「换成这段」——引擎挑哪句都是猜。",
              file=sys.stderr)
        return 2

    redis, world_id, mysql = _world_args(args)
    if not _require_existing_world(redis, world_id, "world setting"):
        return 2

    read_only = text is None and not args.clear
    # ⚠️ **这一句必须在 `World.open()` 之前**,而第一版写在了后面(2026-08-27
    # C 视角验收挑出来的)。`World.open()` 会把**本进程**登记成这个世界的活人,
    # 于是那句"有进程正在跑这个世界"报的是**它自己的 pid** —— 每写一次都出,
    # 而且话是假的:它警告的那个"不会重读配置的进程",就是正在写的这个。
    # **一句每次都出现的警告等于没有这句警告**,和 `doctor` 那条永远退 1 同一族。
    # 兄弟出口 `config set` 一直是先 warn 后开(:5517),照它。
    if not read_only:
        _warn_if_live(redis, world_id, outlet="world setting")
    try:
        world = World.open(world_id, redis=redis, mysql=mysql)
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[world setting] {exc}", file=sys.stderr)
        return 2

    try:
        if read_only:
            receipt = world.world_setting()
        else:
            receipt = world.set_world_setting(
                text, clear=bool(args.clear), dry_run=bool(args.dry_run),
            )
    except ValueError as exc:
        print(f"[world setting] {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"[world setting] {exc}", file=sys.stderr)
        return 2
    finally:
        world.close()

    if args.as_json:
        print(json.dumps(
            {"operation": "world setting", "world_id": world_id,
             "read_only": read_only, **receipt},
            ensure_ascii=False, indent=2))
        return 0

    if read_only:
        print(f"[world setting] {world_id} 的世界观({receipt['length']} 字,"
              f"来源:{receipt['source']}):")
        print()
        print(receipt["text"] or "(空)")
        return 0
    if not receipt["changed"]:
        # **说出「没有变化」** —— 一声不吭和改成功了长得一模一样。
        print(f"[world setting] {world_id} 的世界观没有变化,一个字都没写。")
        return 0
    verb = "要改" if receipt["dry_run"] else "改了"
    what = "回落到引擎内置那份" if receipt["cleared"] else f"{receipt['length']} 字"
    print(f"[world setting] {world_id} 的世界观{verb}({what}):")
    print(f"    原来 {len(receipt['before'])} 字 → 现在 {receipt['length']} 字")
    if receipt["dry_run"]:
        print("[world setting] --dry-run:一个字节都没写。")
    else:
        # **热改是权威** —— 说出来,因为下一个问题必然是"重启会不会被文件盖回去"。
        print("[world setting] 热改是权威:下次开机不会被世界文件里那段旧的盖回去。")
        print(f"[world setting] 看一眼她收到了什么:"
              f"anima-world prompt --world-id {world_id} --agent <谁>")
    return 0


def _print_host_turn(args: argparse.Namespace, turn: dict[str, Any]) -> int:
    """主持人那一屏。**渲染是赠品,`--json` 才是契约**(和 `map` 同一条)。"""
    if getattr(args, "as_json", False):
        print(json.dumps(turn, ensure_ascii=False, indent=2))
        return 0
    # 抬头上的字**全是人话**(3.9.0 验收 C 逮的):从前直出 `〔arrive · cached〕`,
    # 两个英文枚举印在一屏中文上;`place_name` 空时还留一个吊着的 `·`。
    # 枚举是**给机器的**,它的家在 `--json`(那份才是契约)。
    moments = {"arrive": "你到了", "new_day": "新的一天", "beat": "有事发生",
               "ask": "你问了一句"}
    sources = {"llm": "现写的", "mock": "模板", "cached": "还是刚才那一屏"}
    head = f"  第 {turn['day']} 天"
    if turn.get("place_name"):
        head += f" · {turn['place_name']}"
    head += (f"  〔{moments.get(turn['trigger'], turn['trigger'])}"
             f" · {sources.get(turn['scene']['source'], turn['scene']['source'])}〕")
    print()
    print(head)
    print("  " + "─" * 56)
    print(f"  {turn['scene']['text']}")
    print()
    for index, option in enumerate(turn.get("options") or [], 1):
        mark = " " if option.get("available") else "×"
        line = f"  {mark}{index}. {option['label']}"
        if option.get("cost"):
            line += f"({option['cost']})"
        print(line)
        if option.get("hook"):
            print(f"       {option['hook']}")
        if not option.get("available") and (option.get("refusal") or option.get("reason")):
            print(f"       {option.get('refusal') or option.get('reason')}")
    if turn.get("blocked_text"):
        print()
        print(f"  {turn['blocked_text']}")
    print()
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
    if command not in {"forget", "options", "erase", "host"}:
        print("[player] 只有 forget / options / erase / host 四个子命令", file=sys.stderr)
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
    if command == "host":
        try:
            turn = world.host_turn(args.player, ask=bool(getattr(args, "ask", False)))
        finally:
            world.close()
        return _print_host_turn(args, turn)
    if command == "erase":
        try:
            receipt = world.erase_player(
                args.player, reason=args.reason, dry_run=not args.yes,
                since_seq=getattr(args, "since_seq", None),
                limit=getattr(args, "limit", None),
                resume=bool(getattr(args, "resume", False)),
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
        # 量表那一格**零也印**,而**「数不出来」印的是另一句话**:
        # 「他身上没有量」(0)· 「我没查成」(null)· 「这一版引擎不查这个」
        # (这句话整个不出现)—— 三件事,屏幕上必须分得开。
        facts = receipt.get("facts")
        said_facts = (f"他身上的量 {facts} 个" if facts is not None
                      else "他身上的量 数不出来(见日志)")
        print(f"[player] {receipt['player_id']} —— {verb}:"
              f"事件改写 {receipt['events']} 条、"
              f"会话 {receipt['conversations']} 场 {receipt['messages']} 条消息、"
              f"记忆删 {receipt['memories_dropped']} 行改 {receipt['memories_redacted']} 行、"
              f"{said_facts}"
              f"(显示名 {receipt['names']} 个,跳过 {receipt['names_skipped']} 个)。")
        # 分片与续跑那两格。**没做完必须说出来** —— 一趟停在半路而只印一行计数,
        # 读的人会把它当成做完了,而这条路上"以为做完了"是不可逆的那一边。
        from anima_world.api import _ERASE_PHASE_NOT_STARTED, _ERASE_PHASE_PARTIAL

        if receipt.get("resume_seq") is not None:
            print(f"[player] 还没到日志尽头:接着跑 --since-seq {receipt['resume_seq']}"
                  f"(或者直接 --resume)。")
        if receipt.get("phase") == _ERASE_PHASE_PARTIAL and not receipt.get("resume_seq"):
            print("[player] 这个世界里有一趟抹除停在半路 —— --resume 把它做完。")
        if not args.yes:
            print("[player] (没带 --yes:世界一个字节都没动)")
        elif receipt.get("seq") is not None:
            print(f"[player] 已记下 player_erased(seq={receipt['seq']})——"
                  "账本没动;别的进程的内存窗口重启后干净。")
        elif receipt.get("phase") == _ERASE_PHASE_NOT_STARTED:
            print("[player] 没有没做完的抹除 —— 什么都没做"
                  "(--resume 只续,不新开)。")
        else:
            print("[player] 这一片写完了,「审计事件还没写」 —— "
                  "抹除要走到日志尽头才算数。")
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

    from anima_world.redis_state import RedisBeatsStore

    # 分母:声明过的那一串。**只读**,和这条命令其余部分同一条纪律。
    # ⚠️ **`or None` 曾经写在这儿,而它正好把这一层唯一要分开的两件事合成了一件**:
    # 空列表是"这个世界真的一拍都没写",`None` 是"这次调用问不出分母" —— 纯函数
    # 那一侧分得清清楚楚,而这个真出口在最后一步把 `[]` 压成了 `None`。
    # 下场:一个一拍没写的世界,`report --json` 答 `{"declared": null}`,正是
    # FOR-STUDIO §0-② / REFERENCE / CHANGELOG 三处反复警告"不许合成一个"的那一合。
    # 这里读得到库,所以**永远答得出分母**,一格都不该折。
    declared_beats = RedisBeatsStore(redis, world_id).definitions()
    report = build_run_report(events, ticks=clock, minutes_per_tick=mpt,
                              beats=declared_beats)
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
    _print_beat_coverage(report["beats"])
    return 0


def _print_beat_coverage(beats: dict[str, Any]) -> None:
    """屏幕上也要说得出**哪几拍白写了**(3.7.0,看板 D1 的连带)。

    ⚠️ **这一行不是排版,是那条诉求本身。** 创作台要的原话是"一拍都没响 = 这个
    脚本白写,作者必须当场知道" —— 只进 `--json` 的话,敲 `anima-world report` 的
    那个人(**默认的那条路**)屏幕上什么都没有,和"这个世界压根没有剧情"长得一模
    一样。这和 `map` 那条"渲染是赠品"不是一回事:那里赠的是一张图,这里漏的是答案。
    """
    declared = beats.get("declared")
    if declared is None:          # 问不出来 ≠ 一拍都没写:两种都不该冒充对方
        return
    if not declared:
        return
    unfired = beats.get("unfired") or []
    line = f"    节拍:声明 {len(declared)} 拍,响了 {len(beats.get('fired') or [])} 拍"
    if unfired:
        line += onboarding.yellow(
            f"  ← {len(unfired)} 拍一次都没响:{'、'.join(unfired)}")
    print(line)
    stray = beats.get("fired_not_declared") or []
    if stray:
        # 日志里有、今天的脚本里没有 = 剧情被改过。不说的话,上面两个数对不上
        # 而没有一处解释得了为什么。
        print(f"    响过但今天的剧本里没有:{'、'.join(stray)}(剧情改过?)")


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


_ME_NAME_IN_TEXT = re.compile(r"me_([^\s()\[\]{}<>=!+\-*/,'\"]+)")


def _me_names_used(rows: list[dict[str, Any]]) -> set[str]:
    """这份包里提到过哪些 `me_*`(她身上的量)。**只用来免掉一条查不了的检查。**

    见 `_package_only_ontology_errors`:一份编辑包可以完全不重声明 `agent` 种类,
    而她的量表在目标世界里。漏掉一个名字的后果是那条查不了的检查照旧报一次
    **假红** —— 也就是退回今天的样子,不会把一份坏包放行。
    """
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            names.update(_ME_NAME_IN_TEXT.findall(node))
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(key)
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(rows)
    return names


def _package_only_ontology_errors(authored: dict[str, Any]) -> list[str]:
    """一次**编辑**里,这份包**自己肚子里**那几件照查 —— 只豁免跨引用(看板 D29)。

    在这之前 `--edit` 整个跳过本体预检,而 `loadable` 就是 `not errors`,于是一份
    把量名拼错的编辑包拿到一句绿的 `loadable: true`,**开机当场挂**。
    🔬 **它不是"没说",是"说窄了",而说窄了比全不说更难逮**:那一支追加过一句
    warning「引用完整性没查:种类/地点/物品/规律可以来自目标世界」—— 那句话是真的,
    可它只解释得了被跳过的那一摞里的**最后一件**。**人会拿一条真的理由去覆盖整个遗漏。**

    分界是**这件事查得动查不动**,不是"严不严":

    | 查得动(在这份包自己肚子里) | 查不动(可以来自目标世界) |
    |---|---|
    | 量名拼错(`set` 写到没声明过的量) | 规律的 `for_each` 指向哪个种类 / 哪个实例 |
    | 自造动词没给 `label` | 实例的 `location` |
    | `spawn` 声明了却没写代价 | 能力里的 `have_*` / `consumes` 指的物品 |
    | 不认识的字段 · 继承成环 · parent 悬空 | `spawn.kind` 生的是哪个种类 |

    一行 `kinds` 是一份**完整**的声明(合并按 id **整行替换**,不是逐字段打补丁),
    所以左边那一列和目标世界一个字关系都没有。

    ⚠️ **`me_X` 是个例外,而上面那张表原本把它记在左边**(2026-08-21 实测):
    它查的是 `agent` 种类声明过的量,而一份只改某个种类的编辑包完全可以不重声明
    `agent` —— 她的量表在目标世界里。所以这里在包没声明 `agent` 时**补一个只含
    "这份包提到过的那些 `me_*`"的 agent 声明**:名字对得上,这一条就查不出东西来,
    而别的检查一条不少。**这不是放水,是承认这一格离线答不了** —— 答不了的那一格
    由调用方去 `pack install`(它在写第一个字节之前把这几件也问一遍)连着世界问,
    那句话仍然印在 warning 里。
    """
    from anima_world.ontology import Ontology, OntologyError, parse_kinds

    rows = [dict(k) for k in (authored.get("kinds") or []) if isinstance(k, dict)]
    namespaces = _plugin_namespaces(authored)
    parse_rows = rows
    if not any(str(row.get("id") or "") == "agent" for row in rows):
        parse_rows = [{"id": "agent",
                       "quantities": {name: 0.0 for name in sorted(_me_names_used(rows))}}
                      ] + rows
    try:
        kinds = parse_kinds(parse_rows, namespaces=namespaces)
    except OntologyError as exc:
        return list(getattr(exc, "errors", None) or [str(exc)])
    except Exception as exc:  # noqa: BLE001 - 坏声明的形状很多,一律当成一条错报出去
        return [str(exc)]
    # `stocks` 里的量名 —— **同一份判断**(`_undeclared_stock_names`,预检与播种共用
    # 的那一个),只是喂给它一份只含这份包自己声明的本体。
    #
    # ⚠️ **这一格漏过一轮,而漏得极其难看**:上面那句 warning 逐字写着"包自己肚子里
    # 那几件(**量名**、动词 label、`spawn` 代价、不认识的字段)已经查过了" ——
    # 而"量名"其实只查了 `set:` 那一支,`stocks:` 那一支一个字没查。同一份包
    # `world check --edit` 说绿、`validate world` 退 2、真当编辑合并进去当场
    # `OntologyError`。**一句说得比做到的宽的 warning,和一盏假绿灯是同一件事** ——
    # 人正是拿这句话去决定不再自己查的。用例也漏在同一处:只钉了 `set:` 那一支,
    # 所以整套测试全绿而这一格坏着(所以现在两支各一条用例)。
    #
    # ⚠️ **那份合成的 `agent` 声明要摘掉,而且是摘不是重解析**:它是给 `me_X` 那一格
    # 用的替身,身上只有"这份包提到过的 me_ 名字"。留着它去查 `stocks` 的话,一份
    # 给 `agent:甲` 写初值、而没重声明 `agent` 的**完全正常**的编辑包会被判成假红。
    # 而拿原始 `rows` 重跑一次 `parse_kinds` 更糟 —— 那正好是替身在挡的那条假红
    # (`me_体力` 读了而 `agent` 没声明),一句话之内把刚修好的洞又开一遍。
    # 没重声明的种类在这里回落成"查不了"(`declared_quantities` 答空 → 跳过),
    # 而跳过的那一格由 `_edit_stock_kind_gap_warnings` **说出来**。
    own_kinds = kinds
    if parse_rows is not rows:
        own_kinds = {k: v for k, v in kinds.items() if k != "agent"}
    return _undeclared_stock_names(authored, Ontology(kinds=own_kinds))


def _authored_ontology_errors(
    authored: dict[str, Any], *, edit: bool = False,
) -> list[str]:
    """`kinds` / `entities` / 规律那一摞闸,**不建世界地跑一遍**。

    走的是 `_precheck_ontology` —— 开机那条路上**同一个函数**。量名拼错、动词没
    声明、`me_X` 没声明、`spawn` 没写代价、能力里引用不到的物品:这一整摞此前
    `validate world` 一条都报不出来(FOR-STUDIO §3.17 把这个缺口列成了一张表,
    并写着"出包前那一步请是 `simulate --ticks 0`")。那句话本身是诚实的,但它把
    一件引擎该答的事推给了每一个消费方:运维台的判包容器里没有 Redis,创作台的
    体检跑在世界之前 —— 于是"这份文件这一版引擎装得进去吗"离线问不出答案。

    **不另写一份判断**是这里唯一要守的:两份判断迟早给出不同答案,而那种不一致会
    表现成"预检说没问题,开机还是失败"。

    ⚠️ **而"同一个函数"这句话曾经是假的,假在两头**(2026-08-21 实测,创作台在
    3.5.0/3.6.0 双 venv 上量出来的,看板 D29 同族):

    | 写法 | 这条命令 | 开机(`simulate --ticks 0`) |
    |---|---|---|
    | 规律 `for_each.owner: "物件:煤堆"`,**一个 `kinds` 都没写** | 🔴 退 2 | ✅ 退 0,而且规律**真的在跑** |
    | 声明了种类,`stocks` 里量名拼错 | ✅ **说绿** | 🔴 退 1(`OntologyError`) |

    **两个方向都错,而两边都不报错。** 判决:**开机是权威,这两扇门都跟它对齐** ——
    这条命令存在的唯一理由就是离线答出"开机收不收",比开机更严是**假红**(一份跑得
    好好的世界出不了包,而报错指着一个不存在的问题),比开机更松是**假绿**。
    - 第一行:**声明本身就是开关**(CLAUDE.md / README 逐字)。不写 `kinds` 的世界
      这一层整个缺席,`for_each.owner` 是个普通的量 owner。开机那条路一直这么判
      (`if seed_author_layer and world_seed and world_seed.get("kinds")`),
      这里从前无条件跑,于是把那个开关按住了。
    - 第二行:`stocks` 的量名闸住在**播种**里(`_seed_stocks`),而播种在预检之后 ——
      于是它既漏出了这扇门,又让开机在**写过几张表之后**才失败(留下一个装了一半
      的世界,正是 `_precheck_ontology` 当初要修的那个形状)。这一轮把它搬进预检。

    🆕 **3.8.0 第 2 期:插件声明的种类先并进来**(2026-08-26 验收 B/C 双复现)。
    开机那条路在预检之前就把它们并进了 `world_seed["kinds"]`
    (`build_serve_scheduler` 里那句 `_merge_plugin_kinds`),这儿从前没并 ——
    于是同一份世界,开机说好、这扇门说「引用不到 `menpai.sect` 这个 kind」退 2。
    **又是"比开机严"那一侧的假红**,而 tool 把退 2 当红灯:第一个照着 FOR-STUDIO
    写插件的作者,先看到的是一盏指着不存在的问题的红灯。
    """
    from anima_world.ontology import OntologyError
    from anima_world.rules import RuleError

    broken = _plugins_did_not_compile(authored)
    authored = _authored_with_plugin_kinds(authored)

    if not (authored.get("kinds") or []):
        # **声明本身就是开关** —— 和开机那一行逐字同一条判断。
        return []
    if edit:
        return _package_only_ontology_errors(authored)
    empty = _NothingInTheWorldYet()
    try:
        _precheck_ontology(authored, empty, empty, None, None)
    except (OntologyError, RuleError) as exc:
        return _with_plugin_cascade_note(
            list(getattr(exc, "errors", None) or [str(exc)]), broken)
    except Exception as exc:  # noqa: BLE001 - 坏声明的形状很多,一律当成一条错报出去
        return _with_plugin_cascade_note([str(exc)], broken)
    return []


def _plugins_did_not_compile(authored: dict[str, Any] | None) -> bool:
    """这份作者层里有插件,而它们**这一轮一条都没装上**吗?"""
    entries = (authored or {}).get("plugins")
    if not entries:
        return False
    from anima_world.plugins import PluginError, order_plugins, parse_plugins

    try:
        order_plugins(parse_plugins(entries))
    except PluginError:
        return True
    return False


def _with_plugin_cascade_note(errors: list[str], broken: bool) -> list[str]:
    """🔴 **插件没装上时,本体那一摞多半是连带的**(2026-08-27 验收 C 挑出来的)。

    一份插件坏了的文件里,它声明的种类**一行都没编译出来**(`_authored_with_plugin_kinds`
    在坏插件面前一声不吭地掉头,那是有意的:错由 `world_plugin_errors` 逐条报,
    在那儿再报一遍就是同一件事两个说法)。于是实例那条 `entity` 记录会收到一句
    **「引用不到 —— 没有名叫 'menpai.sect' 的 kind」**:那句话字面上是真的,
    可它把作者指向一个**没错的地方** —— 他会去改实例、改种类名,而错在别处。

    **只加一句,不去猜哪几条是连带的**:猜错了比不猜更贵(把一条真错说成连带的,
    作者就再也不会去看它了)。
    """
    if not (errors and broken):
        return errors
    return errors + [
        "⚠️ 上面这几条多半是**连带的**:这份文件里的插件这一轮**一条都没装上**"
        "(它自己的错另有几条,和这一摞印在一起),而插件声明的种类要装上了才存在 ——"
        "所以「引用不到某个 kind」多半不是实例写错了。**先修插件那几条,再回头看这里。**"
    ]


def _state_layer_ontology_errors(state_seed: dict[str, Any]) -> list[str]:
    """**状态层**里那几张开机会编译的表,离线也编译一遍(3.8.0,收件箱 D30)。

    这扇门存在的全部理由是"这一版引擎收不收这份包",而在它加进来的头两年里,它
    看的只有**作者层**。一个跑过的世界导出来一条作者记录都没有 —— 于是这扇门在
    **没看过**的情况下答了"能装",而印出来那句话是「装得进去」。

    实测(FOR-STUDIO §3.30,2026-08-21):3.7.0 导出的世界拿 3.5.0 `world check`
    说绿、`import` 退 0、**真开机退 1**(`OntologyError`,一个 3.5.0 不认识的字段
    随状态层进了包)。**它和 `--edit` 那一格是同一个形状**:不是"没说",是"说窄了"
    —— 而读它的是脚本,脚本读不到那句解释。

    **判断只有一份**:翻成 section 字典之后走的就是 `_authored_ontology_errors`,
    也就是开机第一秒的 `_precheck_ontology`。这一条是这里唯一要守的纪律,理由和
    `_authored_ontology_errors` 的 docstring 逐字相同 —— 两份判断迟早给出不同答案。

    ⚠️ **两条刻意的边界,都是为了不制造假红**(比开机严比比它松更难查:一份跑得
    好好的世界出不了包,而报错指着一个不存在的问题):

    - **规律不管有没有 `kinds` 都编译一遍。** 「声明本身就是开关」说的是本体那一层
      (`_load_ontology` 在 `not len(ontology_store)` 时整个跳过),而规律不是:
      `RedisRulesStore` 定义存原文、编译在读取侧,一个 `kinds` 都没写的世界,
      开机照样要把 `:world_rules` 解析出来。所以这两半在这里是分开的两问。
    - **`stocks` 这一格有意不查。** 状态层的量住 `stock:*` 一族,形状和作者层的
      `stocks` 段不是一回事;硬翻过去就是在写第二份判断。它落进"没查过"那一格
      (`StateScan.unchecked_tables` 数得到),这比猜一句诚实。

    ⚠️ **一种"空真",记在这儿免得下一个人当洞修**(2026-08-27 A 视角验收):
    一份状态层里**只有** `locations` / `item_defs` 的包,这个函数实际上**一条都没
    编译**(没有 `kinds` 就跳过本体、没有 `rules` 就跳过规律),而 `checked_layers`
    照报 `redis`。看上去像"没查却说查了" —— **但它不是假绿**:A 核过**真开机对
    同一份包同样放行**,那两张表本来就只是被引用方,自己没有可编译的声明。
    **判据是"开机会不会因为它开不了机",而答案是不会。** 这一格要是"修"成报
    `unchecked`,反倒会让一份开得起来的包看上去可疑。
    """
    from anima_world.rules import RuleError, parse_rules

    errors: list[str] = []
    rules = [dict(r) for r in (state_seed.get("rules") or []) if isinstance(r, dict)]
    if rules:
        try:
            parse_rules(rules)
        except RuleError as exc:
            errors += list(getattr(exc, "errors", None) or [str(exc)])
        except Exception as exc:  # noqa: BLE001 - 坏声明的形状很多,一律当成一条错报
            errors.append(str(exc))
    if state_seed.get("kinds"):
        # `edit=False`:一个跑过的世界导出来是**完整**的一份(它就是那个世界的
        # 全部),跨引用该查就查 —— 这和 `--edit` 那条豁免不是一回事。
        errors += _authored_ontology_errors(state_seed)
    # 同一句话不说两遍:规律那一摞在 `_precheck_ontology` 里会被再解析一次。
    deduped: list[str] = []
    for line in errors:
        if line not in deduped:
            deduped.append(line)
    return deduped


def _coverage_fields(state: Any, *, authored: bool) -> dict[str, Any]:
    """`loadable` 那句话的**主语**,四格,全是纯增量(3.8.0,收件箱 D30)。

    D30 摆在桌上的三条修法各有舰队级后果:`loadable` 答 `false` 会拦下舰队上
    **每一份正常导出包**(比开机严 = 假红);答 `null` + 退 1 让每份包都拿到一个
    非零退出码(而**一条永远红的检查等于没有这条检查**,人只会把它 `|| true` 掉,
    `doctor` 那一格是同一条教训);只加一格 `checked_layers` 是对的,但它**只把
    那句 warning 翻译成机器读得懂的**,那份 3.7.0 导出包在 3.5.0 上照样答绿。

    所以这一轮做的是**第四条**:先真去查(`_state_layer_ontology_errors`),
    再用这几格说清"查到哪儿为止"。查过之后仍然有查不动的(事件、记忆、转录、
    黑板),那些必须机器读得到 —— 否则下一个 `importance` 又是一次静默。

    ⚠️ **`present` 用的是记录层的真名**(`author` / `redis` / `event` / `mysql`),
    不另造词:消费方已经认识它们(`.cyberworld` 每一行的 `kind` 就是它)。
    """
    from anima_world.world_file import STATE_ONTOLOGY_TABLES

    present = sorted(state.layers)
    checked: list[str] = []
    if authored:
        checked.append("author")
    if "redis" in state.layers and (state.tables & set(STATE_ONTOLOGY_TABLES)):
        # `redis` 这一层算查过的判据是**开机会编译的那几张表我编译了** ——
        # 事件与记忆是数据,读不懂它们不会让世界开不了机(它们安静地不生效,
        # 那是另一种坏法,而这扇门答的是"开不开得了机")。
        checked.append("redis")
    unchecked = [layer for layer in present if layer not in checked]
    return {
        "present_layers": present,
        "checked_layers": sorted(checked),
        "unchecked_layers": unchecked,
        "unchecked_state_tables": state.unchecked_tables(),
    }


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
    path: str, *, scan: Any = None, state: Any = None, manifest_out: list | None = None,
) -> tuple[dict[str, Any], str | None]:
    """读一个世界文件的**作者层**,聚合成 section 字典。读不了就把话说清楚。

    **流式喂进去**(不 `list()` 一份出来):一个跑过的世界导出来是十几万条状态记录,
    而这里只挑 `author` 那几条 —— 攒一份全量列表出来纯属白背,而这条命令正是要被
    拿去问一个真实舰队世界的(灯塔湾那份包)。`author_records_to_seed` 单趟遍历,
    校验和那条 `WorldFileError` 照旧在迭代耗尽时抛出来,一起被下面接住。

    给了 `scan`(一个 `media.MediaScan`)就在**同一趟**上顺手把图数了。为了数图再
    读第二遍等于把上面那条纪律作废,而在同一条流上分叉是免费的。

    🆕 给了 `state`(一个 `world_file.StateScan`)就在**同一趟**上顺手把状态层那几张
    会被编译的表也接下来(3.8.0,收件箱 D30)。理由同上,而且这一格比图更承重:
    一个跑过的世界导出来**只有**状态记录,不接它就等于这扇门对那种包什么都没看过。
    `StateScan` 是有界的 —— 只留层名、表名与登记过的那五张表的行。
    """
    from anima_world.media import tee_media
    from anima_world.world_file import (
        WorldFileError, author_records_to_seed, read_world_file, tee_state,
    )

    try:
        manifest, records = read_world_file(path)
        # 🆕 3.10.0:**封皮跟着同一趟回来。** 「作者声称要哪个引擎」和「这份包真的
        # 要哪个引擎」从前没有一处对过账 —— 而为了对这一笔账再读一遍文件,
        # 等于把这个函数开头那条"流式喂进去、别读第二遍"的纪律作废。
        if manifest_out is not None:
            manifest_out.append(manifest)
        if scan is not None:
            records = tee_media(records, scan)
        if state is not None:
            records = tee_state(records, state)
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
        from anima_world.world_file import StateScan, state_records_to_seed

        edit = bool(getattr(args, "edit", False))
        state = StateScan()
        manifest_box: list = []
        authored, read_error = _load_authored_layer(
            args.path, state=state, manifest_out=manifest_box)
        if read_error is not None:
            return _report_validation("world", args.path, [read_error], [], args.json)
        errors = authored_layer_errors(authored, complete=not edit)
        # 🆕 状态层那几张会被编译的表(收件箱 D30)。**两扇门必须加同一条** ——
        # `tests/test_validate_matches_boot.py` 把它们的 errors 钉成相等,而这两扇
        # 门是同一个判断的两个出口,不是两份判断。
        errors = list(errors) + _state_layer_ontology_errors(
            state_records_to_seed(state.rows)
        )
        warnings: list[str] = []
        if not authored:
            # **作者层为空 = 没有种子,不是一个空种子。** 开机那条路一直这么判;
            # 这里从前不是,于是任何一份导出的世界在校验器嘴里都是非法的。
            # 同上:强调用「」—— `validate world` 的 warnings 也会印在终端上。
            warnings.append(
                "这份文件没有作者层(只有状态记录)—— 它是一个跑过的世界导出来的,"
                "装载时直接落键、不走作者层那几道闸。「状态层里开机会编译的那几张表"
                "(种类 / 实例 / 规律 / 地点 / 物品)这一趟已经查过了」;"
                f"没查过的:{state.unchecked_tables() or '(无)'}"
            )
        else:
            warnings += (
                world_seed_warnings(authored)
                + _authored_drift_warnings(authored)
                + _authored_unreachable_requirements(authored)
                + _authored_media_warnings(authored)
                + _authored_dropped_quantities(authored)
                + _authored_uncreatable_edges(authored)
                + _authored_edge_warnings(authored)
            )
            errors += _authored_ontology_errors(authored, edit=edit)
            if edit:
                # **说出没查的那一半 —— 而且只说没查的那一半。**
                # ⚠️ 这句话 2026-08-21 之前**说窄了**:那时 `--edit` 跳过的是整摞
                # 预检(量名拼错、动词没 label、`spawn` 没写代价一起跳),而这句
                # warning 只解释得了最后一件。**人会拿一条真的理由去覆盖整个遗漏。**
                warnings.append(
                    "这是一次编辑(--edit),「跨引用」没查:规律指向哪个种类 / 哪个"
                    "实例、实例在哪个地点、能力里的物品、`spawn` 生的是哪个种类 ——"
                    "这几样可以来自目标世界,而目标世界不在手上。"
                    "包自己肚子里那几件「已经查过了」:量名两支都查"
                    "(`set:`/`costs:` 里读写的,以及 `stocks:` 里写初值的)、"
                    "动词的 label、`spawn` 有没有代价、不认识的字段、继承成环。"
                    "要连着世界查剩下的:一份内容包用 `pack install`(它在写第一个字节之前"
                    "把这几件也问一遍),别拿 `simulate --world-file` 当编辑用"
                )
                # 🆕 3.10.0:那五段。**句子和开机那条路共用一份常量**
                # (`EDIT_PATH_NOTES`)—— 抄第二遍的那天,两边会先给出不同的措辞,
                # 再由某个作者按其中一句去改一个没错的地方。
                if authored.get("beats"):
                    warnings.append(
                        f"这份文件带着 {len(authored['beats'])} 拍剧情。"
                        + EDIT_PATH_NOTES["beats"]
                        + "(目标世界有哪几拍,离线这一格答不出来 —— 开机是"
                          "「逐拍比」:同 id 且内容相同的静默跳过,同 id 改过的"
                          "说一句而照常开机,「新增」的才当场拒绝、退出码 2)"
                    )
                warnings += edit_path_silent_notes(authored)
                # 🆕 3.10.0(2a-① 验收 C):封皮和内容对不对得上 —— **三扇门同一句**。
                # 一份 `engine_min: "3.9.0"` 而带着 `pack` 段的包,从前这两扇门
                # 说"可用"、`pack install` 退 0 —— 而它在 3.9.0 上是开不了机的硬失败。
                _mf = manifest_box[0] if manifest_box else None
                errors += pack_engine_min_errors(authored, _mf)
                warnings += pack_engine_min_warnings(authored, _mf)
                warnings += _edit_ontology_gap_warnings(authored)
                warnings += _edit_stock_kind_gap_warnings(authored)
                warnings += _edit_dropped_quantity_gap_warnings(authored)
                warnings += _edit_location_media_warnings(authored)
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

    🔴 **这里报的每一段都是「这一版引擎声明过什么」(静态),不是「某个世界现在是
    什么」。** `config` 那一段最容易被读反:世界那一侧是 `config list` 的**合并视图**
    (环境变量 → 机器配置 → 世界 → 引擎默认值,每行带 `source`),而这里是那条链
    **最后一层**的原件。混成一段就是 1.4.0 拆「创世播默认值」时治的病 —— 播下去的
    是**创世那天的快照**,引擎把 `chat.recall_k` 从 3 改成 99,已有的世界一个都吃不到,
    而 `config list` 看上去一模一样。**这条命令连 Redis 都不连**,所以它答的只可能
    是引擎。(这句话此前只写在行内注释与人读输出里,不在这份 docstring 上 ——
    2026-08-26 验收 A 挑的。)
    """
    import anima_world
    from anima_world.api import (
        _ERASE_COUNT_KEYS,
        _ERASE_PHASE_DONE,
        _ERASE_PHASE_NOT_STARTED,
        _ERASE_PHASE_PARTIAL,
        _ERASURE_PROGRESS_TTL_SECONDS,
        _PLAYER_TTL_SECONDS,
    )
    from anima_world import world_file
    from anima_world.beats import (
        AT_KEYS,
        AT_SINCE,
        BEAT_KEYS,
        FOR_EACH_NODES,
        OP_REQUIRED_FIELDS,
        PLAYER_ALLOWED_OP_FIELDS,
        PLAYER_ALLOWED_PREDICATE_FIELDS,
        PLAYER_TOKEN,
        PREDICATE_REQUIRED_FIELDS,
        BEAT_TRIGGER_KEYS,
        VALID_OPS,
        _VALID_PREDICATES,
    )
    from anima_world.config_store import _DEFAULTS as _CONFIG_DEFAULTS
    from anima_world.events import SUBSCRIBABLE_EVENTS
    from anima_world.host import (
        DOOR_METHODS,
        FREE_OPTION_ID,
        HOST_MOMENTS,
        OPTION_KINDS,
        TONES,
    )
    from anima_world.expressions import EDGE_PREFIXES
    from anima_world.rules import RULE_SELECTORS
    from anima_world.world_file import AUTHOR_SECTIONS
    from anima_world.plugins import (
        AUTHORED_EDGE_KEYS,
        BEARER_ALIASES,
        BEARER_FORMS,
        EDGE_ENDS,
        EDGE_END_PREFIXES,
        EDGE_FACT_SHAPES,
        EDGE_NODE_ID_FORMS,
        EDGE_VERB_EFFECTS,
        EDGE_EFFECT_KEYS,
        EDGE_KEYS,
        EMIT_KEY_REQUIRES,
        EMIT_KEYS,
        EMIT_REQUIRED_KEYS,
        FACT_KEYS,
        PLUGIN_KEYS,
        PLUGIN_KIND_KEYS,
        RULE_REQUIRED_KEYS,
        STRICT_LEVELS,
        TRIGGER_EMIT_KEYS,
        KIND_LOCAL_PATTERN,
        RULE_EVERY_KEYS,
        RULE_KEYS,
        TRIGGER_KEYS,
        TRIGGER_REQUIRED_KEYS,
        DEFAULT_TEXT_MAX_CHARS,
        DEFERRED_SHAPES,
        EFFECTS,
        BUILTIN_TARGETS,
        FACT_MODES,
        FACT_SHAPES,
        OWNER_FORMS,
        PROJECTED_SOURCE_KEYS,
        PROJECTED_SHAPES,
        KIND_PREFIXES,
        PLUGIN_ID_PATTERN,
        VERB_KEYS,
        RESERVED_IDS,
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
    from anima_world.media import MEDIA_SCHEMES
    from anima_world.projection import SETTLED_INVITATIONS_KEPT
    from anima_world.together import INVITE_OUTCOMES
    from anima_world.ontology import AFFORDANCE_KEYS, KIND_KEYS
    from anima_world.perception import VISIBILITIES
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
            # 3.5.0 多了 `erasure:{player_id}`:一趟没做完的法务抹除的进度,
            # 而它记着的正是**要被抹掉的那些名字**(见 `RedisErasureProgress`)。
            # 打进包发出去比不抹还糟,所以它和 lock / 在场同一类:带 TTL、不进包。
            # 🆕 3.10.0:`config_rev` —— **配置表改过几次**,`ConfigStore` 拿它
            # 判断"我手里那份还新不新"(见 `RedisConfigBackend`)。
            # 🔴 **它是「进程态」,不是世界内容**(总图那张三态表的定义:协调用的,
            # 不是世界内容 → 不进 `.cyberworld`),和 `lock` 同一类:装回去毫无意义,
            # 而每一份发出去的包都会带着一个没人读得懂的计数器。
            # ⚠️ **这一格动了 `storage`,所以运维台那条 `deepStrictEqual` 会当场红** ——
            # 那是它该有的样子(镜像本该在这种时候喊),同轮认账。
            "volatile_keys": [
                "lock", "players", "player:{player_id}", "erasure:{player_id}",
                "config_rev",
            ],
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
        # 引擎**声明过**哪些配置键(3.8.0)。
        #
        # 🔴 **这一段是「这一版引擎声明过什么」,不是「某个世界现在是什么」。**
        # 后者是 `config list` 的**合并视图**(环境变量 → 机器配置 → 世界 →
        # `_DEFAULTS`,每行带 `source`),而这里报的是那条链**最后一层**的原件。
        # 混成一段就是 1.4.0 拆「创世播默认值」时治的那个病本身:播下去的那份是
        # **创世那天的快照**,引擎把 `chat.recall_k` 从 3 改成 99,已有的世界一个
        # 都吃不到,而 `config list` 看上去一模一样。**这一段永远不碰任何世界** ——
        # 它连 Redis 都不连(`contract` 整条命令都不连),所以它答的是引擎,
        # 不是任何一个世界。
        #
        # ⚠️ **密文键只报元数据,一格值都不报。** `is_secret` 为真的键 `default`
        # **永远是 `null`** —— 不是"这个世界没设",是**这一段根本不报值**。
        # 世界里一个 secret 都没有(`config set llm.api_key` 自动路由进机器配置),
        # 所以这里也不该有一个能放它的位置。
        #
        # 字段名**照 `config list` / `ConfigStore.meta()` 的原话**
        # (`value_type` / `category` / `is_secret` / `description`),不另起一套 ——
        # 同一样东西两个名字,就得有人维护一张翻译表,而翻译表迟早只有一半跟着代码走。
        "config": {
            key: {
                "default": None if is_secret else default,
                "value_type": value_type,
                "category": category,
                "is_secret": is_secret,
                "description": description,
            }
            for key, (default, value_type, category, is_secret, description)
            in _CONFIG_DEFAULTS.items()
        },
        "package": {"format_version": PACKAGE_FORMAT_VERSION},
        "report": {"format_version": REPORT_FORMAT_VERSION, "buckets": list(BUCKETS)},
        "seed": {
            "schema_version": None,  # 无版本号:随主版本走
            "agent_keys": sorted(WORLD_SEED_AGENT_KEYS),
            # **必填之外、引擎认得并且带得过河的那些。** 分开一格是因为
            # `agent_keys` 是必填集(镜像端拿它算"少了什么"),把可选键混进去
            # 等于要求每个世界给每个角色写一张卡。
            "agent_optional_keys": sorted(WORLD_SEED_AGENT_OPTIONAL_KEYS),
            # **一个能力里写得下哪些字段。** 它是校验器判断时用的那一份
            # (`ontology.AFFORDANCE_KEYS`),不是抄给创作台看的副本 —— 抄一份的话,
            # 加一格时总有一次只改了校验器,而这一头照旧答着旧清单,两边都不报错。
            "affordance_keys": sorted(AFFORDANCE_KEYS),
            # 一条 `kinds` 行认哪些字段(3.7.0)。⚠️ **它是"读得到的那几格"的清单,
            # 不是一道闸**:能力级的不认识字段当场开不了机,而种类级的今天被**静默
            # 忽略**。写在契约里是因为 `parent`(单继承 + 加载期 copy-down)从 2.0
            # 起就能用,而 FOR-STUDIO §3.7 到 2026-08-21 都没写过它 —— 创作台因此
            # 不认这一格,对着一份完全合法的声明产出过一条**假红**。
            # **消费方问这一格,别照文档维护一份清单。**
            "kind_keys": sorted(KIND_KEYS),
            # 🆕 2026-08-27:**可见性五档**(tool 修镜像漂移时立的诉求)。
            # 量声明里的 `visibility` 只认这五个词,而**整份契约此前一格都没列过
            # 它们** —— 于是创作台只能照 FOR-STUDIO 手抄一份,和 `kind_keys`
            # 当初那条假红是同一个形状:抄漏一个词,一份完全合法的声明被判红。
            # 照 `plugins.rule_selectors` 的先例给,**和引擎读同一份常量**
            # (`perception.VISIBILITIES`)—— 手写一份平行清单就是把漂移搬个家。
            # ⚠️ 顺序是承重的:`self`/`connected`/`here`/`public`/`hidden` 是**从窄
            # 到宽**排的,消费方拿它画选择器时那个次序就是给作者看的次序。
            # **没声明 = 感知不到**,这一条不在表里 —— 它是缺席的语义,不是第六档。
            "visibilities": list(VISIBILITIES),
            # 本轮新的那一格,单独点名。**创作台按它在不在探测,不比版本号** ——
            # 同一个版本号下有过好几份不同的引擎,而"这支引擎会不会让在场的人
            # 记住这件事"猜错了**不报错**:世界照跑、日志干净,只是屋里的人什么
            # 都没记住,而作者要到读某个人的记忆时才发现。
            "affordance_importance": {
                "range": [0.0, 1.0],
                "read_command": "ontology",
                # **不写 = 这一层整个缺席**,和 perception / kinds 逐字同构:
                # 没有默认值,也没有 `.enabled` 开关。写下一个数才是说
                # "这件事值得被记住",于是同屋的每个人各落一条见证记忆。
                "default": None,
                "gloss": (
                    "`kinds.<种类>.affordances.<动词>.importance`(0~1,可选)—— "
                    "**声明它,同处一地的其他角色就会各记住一条**(记忆 kind "
                    "`witness`,出处进 `entity_interaction` 的载荷);不写的世界"
                    "这一层整个不存在,行为与从前逐位相同。它同时是**玩家动作进"
                    "旁白**的那道闸:玩家做了一件声明过 importance 的事才可能生成"
                    "旁白,而那条路另有一个默认关着的世界开关 "
                    "`narrative.player.enabled`"
                ),
            },
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
            # 读出口与写出口,和 `character_card` 那两格同一个形状、同一个理由。
            # **3.4.0 起写出口真的有了** —— 上一版这里报的是 `None`,而那是当时
            # 唯一诚实的答案(作者层合并按地点 id 整条跳过已有地点,于是给一个
            # 跑着的世界补图只能重建它)。消费方的铁律是"问引擎,不读文档",
            # 所以这一格从 `null` 变成命令名,就是"那个按钮现在可以画出来了"。
            # ⚠️ **按这一格判,别比版本号**:同一个 3.3.0 下有过七份不同的引擎。
            "location_image_read_command": "map",
            "location_image_write_command": "location set-image",
            "location_image_write_gloss": (
                "`anima-world location set-image --location <id> "
                "[--map-image URI] [--scene-image URI] [--clear]` —— 改一个"
                "**已经跑着的**世界里某个地点的图,形状和 `agent set-card` 逐格"
                "相同(明示的编辑所以**覆盖**、两格分开合并、空串抹掉一格、"
                "`--clear` 抹掉两格、逐字相同就一个字都不写、`--dry-run` 只报不写)。"
                "地点不存在退 2 并列出这个世界里有哪些地点。**它只写这两格** —— "
                "名字、描述、几何只有作者层一个合法的写入者。argv 装不下的长 "
                "`data:` URI 走 `--map-image-file` / `--scene-image-file`"
                "(`-` = 标准输入)。作者层那条路不变,仍然是「只填缺、不覆盖」:"
                "拿一份补了图的世界文件去装一个已有的世界,已在册的地点照旧跳过"
                "(并逐个点名),补图请走这扇门"
            ),
            # 🆕 3.8.0(收件箱 D4):**世界观的读写出口。**
            # 和上面那两格逐字同一个理由,而这一格欠得最久 —— `world_setting` 只在
            # **创世那一刻**落进 `:prompts`,于是一个已经在玩的世界改不了自己的
            # 世界观,连 `--world-file` 都不生效(而且不报错),创作台唯一的办法是
            # `world drop` 把整个世界抹掉重建。
            # ⚠️ **按这一格判,别比版本号**(同一个 3.3.0 下有过七份不同的引擎)。
            "world_setting_read_command": "world setting",
            "world_setting_write_command": "world setting --set",
            "world_setting_write_gloss": (
                "`anima-world world setting --world-id <w> "
                "[--set TEXT | --set-file PATH | --clear] [--dry-run] [--json]` —— "
                "改一个**已经跑着的**世界的世界观。**什么都不给 = 只读**"
                "(和 `world drop` 不带 `--yes` 只数同一条)。形状和 "
                "`location set-image` 逐格相同:明示的编辑所以**覆盖**、"
                "逐字相同就一个字都不写、`--dry-run` 只报不写、世界不存在退 2 "
                "且不许当场创世。⚠️ **`--clear` 是回落到引擎内置那份,不是变成空的**;"
                "一段空白 / 一张表**当场拒绝**(退 2)—— 世界观是她提示词里的第一块,"
                "静默抹掉它会让这个世界里每个人下一句话都变,而没有一处报错。"
                "argv 装不下的长文本走 `--set-file`(`-` = 标准输入)。"
                "**热改是权威**:下次开机不会被世界文件里那段旧的盖回去"
            ),
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
        # 法务抹除(《拟人化互动办法》第十六条)。这一段存在的理由和
        # `location_image_write_command` 逐字相同:**消费方按出口在不在探测,
        # 不比版本号** —— 同一个版本号下有过好几份不同的引擎,而"这支引擎的抹除
        # 续不续得上"猜错了不报错,只会在一个大世界上永远排在队列里。
        "erasure": {
            "read_command": "player erase",       # 不带 --yes 就是只数
            "write_command": "player erase --yes",
            # **这一格缺席、或者是 `null`** = 这支引擎的抹除**一趟被杀就回不来**
            # (3.4.0 及更早);命令名 = 可续,而且续跑不会重新推断名字。宿主的作业
            # 调度按这一格决定敢不敢把抹除切成片、敢不敢在部署时把它杀掉。
            # ⚠️ **"缺席"那半句是承重的,而这份契约到 3.7.0 才把它写对**:一支
            # 没有这个能力的老引擎**不会答 `null`,它整段都没有** —— 照
            # `payload["erasure"]["resume_command"]` 那样取的探测器在它身上是
            # `KeyError` 退 1,而**探测器崩掉和"探测出没有"是两件事**。
            # 消费方一律 `.get("erasure", {}).get(...)`。
            #
            # ⚠️ **`--yes` 一个都不能少**,理由和 `write_command` 逐字相同:
            # 这条命令**默认是预演**(和 `world drop` 同款)。少了它,照这一格
            # 原样敲出来的续跑是一次 `dry_run: true` 的空转 —— 退出码 0、回执好看、
            # 水位一格不动,而宿主拿它驱动重试就会**永远** partial 下去。
            # 报一条"照着敲会静默什么都不做"的命令,比不报这一格还坏。
            "resume_command": "player erase --yes --resume",
            "shard_params": ["since_seq", "limit"],
            "phases": [_ERASE_PHASE_NOT_STARTED, _ERASE_PHASE_PARTIAL, _ERASE_PHASE_DONE],
            # 回执上**跨片累加**的那几格。3.8.0 多了第六格 `facts`(他身上那张量表
            # 删了几个量,收件箱 D39)—— **这一格缺席 = 这支引擎的抹除够不着玩家
            # 量表**,而它够不着的时候不报错,只是把一行体力留在世界里。
            # 消费方一律 `.get("erasure", {}).get("receipt_count_keys", [])`,
            # 读回执时一律 `receipt.get("facts", 0)`:老引擎是**缺席**不是 `null`。
            "receipt_count_keys": list(_ERASE_COUNT_KEYS),
            # 🆕 **逐键一句人话**(形状照 `blocked` / `blocked_text` 那一对)。
            # player 那一格明着挂着账:`Account.vue` 逐字写着「欠上游一件:那张表
            # 旁边加一格逐键的 gloss……在它给之前站点一个字不译」——
            # **全链上唯一一处下游明说在等的诉求**,而它等的就是这一格。
            # ⚠️ 缺席 = 这支引擎没有这一格,别拿 `receipt_count_keys` 自己编一份:
            # 编出来的措辞和引擎说的不是同一句话,而两句话都会印在合规记录上。
            #
            # 🔴 **这七句是玩家可见文案,不是开发笔记**(2026-08-26 player 视角 A
            # 逮出来的,第一版写反了)。判据是那对先例:`blocked_text` 就是印在
            # 玩家屏上的那一句,而站点那侧的纪律是**不译、有 gloss 就整排用引擎
            # 原话** —— 于是措辞归我们,**改一次措辞 = 改一次玩家屏**。
            # 第一版摘掉 markdown 之后上屏长这样:「……三档:整格缺席 = 这支引擎
            # 够不着量表 · null = 我没查成 · 0 = 我查过了…」。
            # **三档是给机器读的语义**,它的家在 REFERENCE 与 `receipt_count_keys`
            # 那一行;这七句只回答一件事:**这个数,数的是什么**。
            # 判据 `tests/test_erase_player.py::test_回执那七句是给玩家看的话_不是开发笔记`。
            "receipt_count_gloss": {
                "events": "世界日志里提到你的条目,改写了这么多条:你的名字换成"
                          "「(已注销)」,写到你的那几句抹空。日志本身不会被删掉 —— "
                          "发生过的事仍然发生过,只是里面再也认不出你是谁",
                "conversations": "整场删掉的对话数",
                "messages": "跟着那些对话一起删掉的消息条数",
                "memories_dropped": "因你而起的记忆,删掉了这么多条",
                "memories_redacted": "旁及你的记忆,只把名字换掉了这么多条 —— "
                                     "别人的回忆不该因为你走了就少一角",
                "facts": "你在这个世界里的那张数值表(体力、心情之类),"
                         "抹掉了这么多个",
                "edges": "你和世界里其他人、其他东西之间的那些牵连,"
                         "断掉了这么多条",
            },
            # 进度键的形状。**镜像端要跟的是这一格加 `storage.volatile_keys`**:
            # 打包必须跳过它 —— 它装着正要被抹掉的那些名字。
            "progress_key": "anima:{world_id}:erasure:{player_id}",
            "progress_ttl_seconds": _ERASURE_PROGRESS_TTL_SECONDS,
            "gloss": (
                "`anima-world player erase --player <id> [--yes] [--since-seq N] "
                "[--limit K] [--resume]` —— 不带 `--yes` 只数(和 `world drop` 同款)。"
                "⚠️ **分片分的是改写那一遍,收名字那一遍永远 O(全量事件)**:他的名字"
                "可能出现在任何一条事件的自由文本里,而那种句子不带他的 id。所以"
                "这几个参数**不会让每一发请求变快** —— 它们买到的是「一趟被杀在半路"
                "之后还能续,而且名字不丢」,以及「一次调用的写入量有上限」。"
                "回执的 `phase` 说的是**这个人在这个世界里的抹除处在哪一步**"
                "(not_started / partial / done),不是「他被抹干净了没有」(看计数);"
                "一次**预演**也答得出 partial,宿主据此把「被墙挡在门外」和「抹到"
                "一半」分开。`resume_seq` 是下一趟从哪儿接着看。"
                "真跑时 `--since-seq` 不许越过已完成的水位(会在日志里留一个洞)"
            ),
        },
        # 邀请门(3.6.0 起)。**这一段是给世界壳读的,不是给创作台读的** ——
        # 它是全系统唯一一条"读写成对"的玩家送达面,而它**一条 CLI 出口都没有**:
        # 唯一的消费方是一个 Python 宿主(运维台的世界壳 import 本包)。
        # 报在这里的理由和 `erasure` 逐字相同:**消费方按出口在不在探测,不比版本号**。
        "invitations": {
            "read_command": None,      # 有意为空:这扇门没有 CLI,见上面那段
            "read_api": "World.invitations_page",
            "answer_api": "World.answer_invitation",
            # 🆕 3.7.0(看板 D23):结局那条事件的**带游标**出口。
            # **这一格缺席、或者是 `null`** = 这支引擎只有那两扇有上限的门
            # (3.6.0 及更早),离线久了「她已经走开了」会静默掉出窗外,屏幕上
            # 印成「你错过了」—— **把她做的事记在他头上**,而链路上一处不报错。
            # ⚠️ **3.6.0 上整个 `invitations` 段都不存在**(实测),所以探测要写成
            # `.get("invitations", {}).get("outcomes_api")`。
            "outcomes_api": "World.invitation_outcomes_page",
            "outcomes_event": "invitation_settled",
            "outcomes": list(INVITE_OUTCOMES),
            # 那两处**有上限**的读法,把上限本身报出来:消费方照这两个数就知道
            # "光靠它们够不够",而不必去猜。
            "settled_kept": SETTLED_INVITATIONS_KEPT,
            "gloss": (
                "一份邀请的结局有两条读法,而在 3.7.0 之前**两条都有上限**:"
                "`invitations_page()` 每行那格 `outcome`(只记最近 `settled_kept` 份)"
                "与 `state()['recent_events']` 那扇窗(壳还会再截一次)。"
                "`outcomes_api` 是**带游标**的那一条 —— 离线多久回来都补得齐。"
                "⚠️ 它是**历史**不是清单:问「此刻还有几份等着回话」仍然读 "
                "`invitations_page()` 的 `pending`"
            ),
        },
        # 插件段(3.8.0,第 0 期)。**这一期只有一格** —— 第 1 期才有
        # `type:"plugin"` 记录、命名空间、事实四形状那一摞。
        # 报在这里的理由和 `erasure` / `beats` 逐字相同:**消费方按段/格在不在探测,
        # 不比版本号**;老引擎上**整段缺席**(不是 `null`),一律
        # `d.get("plugins", {}).get("subscribable_events", {})`。
        "plugins": {
            # 插件的触发器订得到哪几种事件。**策展表,不是全集** ——
            # 引擎里在发的 type 有四十来个,一半是内部管道(`subsystem_health` /
            # `memory_seed` / `plan` / `legacy_seq_gap`),它们的载荷为引擎自己的
            # 用途服务,明天就可能因为一次内部重构而变。
            # 🔴 **进了这张表就是一句公开契约,拿不掉**,所以宁少勿多。
            # 每条带 `numbers`(数字格)与 `parties`(当事人格)—— 前者给算术,
            # 后者决定触发器的 `for_each` 对不对得上人。
            # 详见 `anima_world/events.py` 的 `SUBSCRIBABLE_EVENTS`。
            "subscribable_events": {
                name: dict(spec) for name, spec in SUBSCRIBABLE_EVENTS.items()
            },
            # 🆕 3.8.0 第 1 期。**这一格缺席 = 这支引擎不认 `plugin` 记录**
            # (3.7.0 及更早,而它见到那种记录是**开不了机的硬失败** ——
            # 和 `beat` 逐字同一种情形,所以带 `plugin` 记录的包 `engine_min`
            # 必须写 3.8.0)。一律 `d.get("plugins", {}).get("author_type")`。
            "author_type": "plugin",
            "world_file_section": "plugins",
            "storage_key": "anima:{world_id}:plugins",
            "id_pattern": PLUGIN_ID_PATTERN,
            # 🆕 2026-08-27:**六格盲区**(创作台诉求第六条)。
            # 🔴 **引擎本来就全都拒**,契约只是把它已经在做的事说出来 ——
            # 纯增量,一格取值都没改。少了这几格,创作台判不了,
            # 只能眼看作者出包之后被引擎打回。形状照 `id_pattern` 那条先例:
            # **给每种名字一格正则**,别让下游自己写死判断。
            "version_required": True,
            # 🆕 2026-08-27 收尾全扫:**`plugin` 记录每一个层级一格键名单**。
            # 🔴 **一层一层收本身就是那个 bug 的形状** —— 创作台每换一次钉就量出
            # 新的一层,所以这一轮把每层一次过完,并让 `plugins.strict_levels`
            # 报出"哪几层是查的",好让它那条「盲区不许变多」的闸收得到底。
            # **每一格都和引擎读同一份常量**,抄第二遍就是漂移的来路。
            "strict_levels": list(STRICT_LEVELS),
            "plugin_keys": list(PLUGIN_KEYS),
            "fact_keys": list(FACT_KEYS),
            "edge_keys": list(EDGE_KEYS),
            "kind_keys": list(PLUGIN_KIND_KEYS),
            # ⚠️ **和 `emit_keys` 不是同一份,别合成一格**:规律的 `emit` 有
            # `when`/`on`/`importance`(门槛与边沿是规律那一层的概念),
            # 触发器的 `emit` 已经"因一件事而发"了。合了,创作台那边就是假红。
            "trigger_emit_keys": list(TRIGGER_EMIT_KEYS),
            "edge_effect_keys": list(EDGE_EFFECT_KEYS),
            "rule_required_keys": list(RULE_REQUIRED_KEYS),
            "rule_keys": list(RULE_KEYS),
            "rule_every_keys": list(RULE_EVERY_KEYS),
            "emit_keys": list(EMIT_KEYS),
            "emit_required_keys": list(EMIT_REQUIRED_KEYS),
            # 🆕 **"写了 A 就必须写 B"的那几对**(3.8.0,2026-08-27 第二波 ③)。
            # 从前这条耦合只活在代码里,而 `emit_required_keys` 只列 `type`/`when`
            # —— 于是创作台对一份 `{"text": …}` 的 emit **判绿而引擎硬拒**。
            # 「契约说收、引擎不收」比缺一格贵:照契约做出来的东西开不了机。
            # ⚠️ **它只管规律那一层的 `emit`**:触发器的 `emit` 根本没有
            # `importance` 这一格,那儿的 `text` 是载荷里的一句话、不进记忆。
            "emit_key_requires": {k: list(v) for k, v in EMIT_KEY_REQUIRES.items()},
            "trigger_keys": list(TRIGGER_KEYS),
            "trigger_required_keys": list(TRIGGER_REQUIRED_KEYS),
            # 创作台在这一格上是全仓**唯一一处写死的空白判断**,所以它点名要了。
            "kind_local_pattern": KIND_LOCAL_PATTERN,
            "reserved_ids": sorted(RESERVED_IDS),
            # 🔴 **这一版收的事实形状,不是设计稿那张表。** 设计稿说的是这套架构
            # 装得下什么,契约说的是**这一版引擎收不收** —— 差着 `timer` 与 `text`
            # 两种,它们写了**开不了机**并点名(理由各不相同,见 `plugins.py`)。
            "fact_shapes": list(FACT_SHAPES),
            # 🆕 第 2 期 2b:一个事实的**真相住在哪儿**(设计稿 §9.3)。
            # `stored` = 量表里那个数就是真相(默认,和第 1 期逐位相同)·
            # `projected` = 真相是日志里那一串 `<插件>.<事实>.delta`,
            # 量表里那个数只是**物化视图**。
            # 🔴 **值得多这一种模式的理由只有一个**:一个直接写的事实丢掉了
            # 「可重放」—— 而「你为什么只剩三块钱」的唯一答案正是那一串事件,
            # 一个直接写的余额答不出这个问题,**而且它答不出来的时候不报错**。
            "fact_modes": list(FACT_MODES),
            # ⚠️ `projected` 这一版只收 `number`,而这不是"还没做":**delta 是一个
            # 差值**,一个枚举名或一句话身上没有"差"这回事。
            "projected_shapes": list(PROJECTED_SHAPES),
            "projected_delta_event": "<plugin>.<fact>.delta",
            # 载荷四格。`cause` 说的是"哪一条规律/触发器让它变的" —— 没有它,
            # 一串 delta 只是一串数字。
            "projected_delta_payload": ["owner", "fact", "delta", "cause"],
            # ⚠️ **挂在插件自己种类上的事实做不了 `projected`**:那样东西会被
            # `destroy` 抹掉,而一串折向一个不存在的主人的 delta,重放出来是一个
            # 没有主人的数。挂在 `actor` / `world` / `location` 上的可以。
            "projected_bearers": ["actor", "agent", "player", "world", "location"],
            # 🆕 裁决 ④(2026-08-26):把**既有的内核事件**认成自己的 delta。
            #
            # 🔴 **没有它,钱包搬不动**:折叠端只认 `.delta` 后缀,而设计 §9.3 说
            # 「`payment` 事件照旧是 `economy.coins` 的 delta」—— 两句话对不上。
            # 三条路里两条是坏的:改发一条新事件 = **破坏消费方**(`payment` 在
            # `subscribable_events` 上)· 两条都发 = **同一笔钱记两遍账**。
            # 只有"多一格声明"这条不破坏任何人,所以它是纯增量。
            # 🆕 出厂插件那张表:**id → 决定它装不装的那个配置键**。
            # 消费方读这一格,别照文档记一份清单。
            "factory": dict(FACTORY_PLUGINS),
            # 🔴 **每个出厂插件搬了哪几格** —— 不按"搬完几个系统"计数
            # (补裁 ⑤b:按系统计数正是把人推向换皮的那把尺子)。
            # `economy` 这一格说得很明白:只有钱包,货架和动词都没搬。
            "factory_scope": dict(FACTORY_SCOPE),
            "projected_source_keys": list(PROJECTED_SOURCE_KEYS),
            # ⚠️ **只认内核白名单上那几种事件** —— 认别的插件的事件,等于让一张
            # 作者写的表把「谁发的 ≠ 改谁的」那扇门重新打开(只是这次是声明式地开)。
            "projected_source_events": "subscribable_events",
            "projected_owner_forms": list(OWNER_FORMS),
            "projected_source_gloss": (
                "`credit` 加、`debit` 减,两个键名写死 —— **符号不给作者算**:"
                "给一个 `sign` 让他自己填,写反了不报错,而一个反着记的账让"
                "「对账即重放」成了一句空话。⚠️ 一条事件的两头形状不同时"
                "(`payment` 的 `to` 可能是个人而 `from` 是 `__town__`),"
                "**写成两条声明**:一条只给 `credit`、一条只给 `debit`。"
                "让一个 `owner_form` 同时管两头,是让作者在一个格子里说两件事。"
            ),
            # 🆕 第 2 期:**边上多收一个 `text`,而这不是偏心,是存储的形状** ——
            # 节点事实住在量表里(`[float, tick]`),边自己那一行本来就是一份 JSON。
            "edge_fact_shapes": list(EDGE_FACT_SHAPES),
            "edge_ends": list(EDGE_ENDS),
            # 🔴 **上面那一列里,带 `<>` 的两个是「形状」不是「值」**
            # (2026-08-26 复核评审:P1.6 那盏假红灯只治了一半 —— 契约把两个模板
            # 混进了一列字面值,却没有一格说它们是模板,于是照旧做等值比较的 tool
            # **仍然会拒掉 `entity:sword`**)。判的时候:先看是不是这两个前缀之一,
            # 是就只查后面那个种类名;否则再和字面值比。
            "edge_end_prefixes": list(EDGE_END_PREFIXES),
            "edge_ends_gloss": (
                "这一列里 `entity:<kind>` / `group:<kind>` 是**模板**,`<kind>` 是"
                "占位符 —— 真正写进声明的是 `entity:sword` 这种。**别拿整列做等值"
                "比较**:那样一个引擎跑得起来的世界会被校验器拒掉,而作者会去改一个"
                "没错的东西。判据:`any(v.startswith(p) for p in edge_end_prefixes)` "
                "或 `v in edge_ends`。"
            ),
            "edge_storage_key": "anima:{world_id}:edge:{type}",
            # 表达式里边的三个前缀。🔴 **不是设计稿写的 `from`/`to`** ——
            # `from` 是 Python 关键字,而表达式是 `ast.parse` 解析的:
            # `from.x` 连语法都过不去。**声明里那两个键仍然叫 `from`/`to`**
            # (那是 JSON,不受这条限制),两套词只在这一处分岔。
            "edge_expression_prefixes": sorted(EDGE_PREFIXES),
            # 🆕 第 2 期 2a:插件声明的**节点**。`group` 只比 `entity` 多一个
            # `members` 记号(设计稿 §13 ⑥ 自己也说不准这一刀该不该切,而只属于
            # group 的行为这一期一件都没做 —— 现在分家是在猜)。
            # 🔴 **它们编译成普普通通的本体种类**(id 是 `<插件>.<名>`),于是
            # 出生自检、「生成必须要代价」、`prompt.budget`、可见性、拒绝语
            # 一件都不用重写。
            "kind_prefixes": list(KIND_PREFIXES),
            "kind_id_syntax": "<plugin>.<local>",
            # 🆕 **插件种类的实例,作者层里种得下**(2026-08-27,创作台问的第二半)。
            # 它编译成普通本体种类之后,实例就是普普通通的 `entity` 记录 ——
            # 没有新段、没有新语法,只有一条:id 里那个种类名要写**全名**。
            "kind_instance_section": "entities",
            "kind_instance_id_syntax": "<plugin>.<local>:<实例名>",
            # 🆕 **触发器的 `for_each` 到底从事件的哪一格取人**(2026-08-27)。
            # 🔴 这一格治的是一句**写在两处的假话**:白名单那张表的说明与
            # `_fire_trigger` 的 docstring 都写着「`parties` 决定 `for_each` 对不
            # 对得上人」,而取人那条路**一个字都不读 `parties`**。`travel` 是它
            # 最容易骗到人的地方 —— 那一条的 `parties` 只有 `player_id`
            # (玩家那条路才带),照那句话推会得出「角色出发时触发器对不上人」,
            # 而实测两半都对得上(顶层 `who` 两条路都写)。
            # **一格「从哪儿取」比一段「它决定……」值钱得多**:后者要人读、要人记,
            # 而它正好被记错了一轮(`edge_end_prefixes` 那条先例)。
            "trigger_bearer_keys": {
                "agent": "event.who(经 stock_owner_of → `agent:<id>`;"
                         "玩家写成 `player:<id>`,于是 owner 是 `agent:player:<id>`)",
                "world": "(常量 `world`,不看事件)",
                "location": "event.loc",
                "entity:<kind>": "event.payload.target,没有就 event.payload.entity"
                                 "(取到的那个 id 的种类要对得上 `<kind>`)",
            },
            "trigger_bearer_gloss": (
                "🔴 **`subscribable_events` 里那格 `parties` 不是取人的依据** ——"
                "它说的是「这条事件里还写着谁」(对方、目标、收款人),给读的人看。"
                "触发器落在谁头上,只看上面这张表。"
                "⚠️ **取不出人就整条不跑**(`_trigger_bearer` 答 `None`,不猜);"
                "**取出来的人身上一个量都没有时,`agent` 那一支也不跑** —— "
                "那意味着这个插件还没种到他头上。"
            ),
            "kind_instance_gloss": (
                "插件声明的种类编译成**普普通通的本体种类**,所以它的实例走的就是"
                "作者层已有的 `entity` 记录:`{\"id\": \"menpai.sect:青云门\", "
                "\"name\": \"青云门\", \"location\": \"yard\"}`。"
                "⚠️ **种类上声明的事实,量名不带命名空间**(`声望`,不是 "
                "`menpai.声望`)—— 它住在那个实例自己的量表里,不和别人共用一张表;"
                "顶层 `facts` 那一族才带(它们住在**角色/世界/地点**的量表上,"
                "跨插件共用一张,所以必须分得开)。"
                "⚠️ 实例记录**排在哪一行不承重**,但那个种类必须由这份文件里的"
                "某个插件声明(离线两扇门与开机拿的是同一份判断)。"
            ),
            # 🆕 **边的两端写进声明时长什么样。** 这一格是问出来的:创作台要写
            # `{"from": "agent:甲", "to": "menpai.sect:青云门"}`,而此前**整份契约
            # 一格都没说过节点 id 的形状** —— 只能照 FOR-STUDIO 抄,而抄来的镜像
            # 会烂(`visibilities` 那一格刚吃过同一种亏)。
            # 🔴 **`player` 那一行最容易写错**:玩家的节点 id 是 `agent:player:<id>`,
            # 不是 `player:<id>` —— 玩家和角色**同一个量表命名空间**
            # (`stock_owner_of`),而边的两端用的就是那个 owner key。
            # ⚠️ **这一格和 `authored_edge_errors` 读的是同一份常量**
            # (`plugins.EDGE_NODE_ID_FORMS`)—— 印的地方和判的地方各存一份,
            # 就是「契约说 A、闸按 B 判」那种漂移的来路,而两边都不报错。
            "edge_node_id_forms": dict(EDGE_NODE_ID_FORMS),
            # 🆕 **作者层种得下一条边了**(3.8.0,2026-08-31,收件箱 D44)。
            # 探测位和 `beats.author_type` 逐字同构:**这一格缺席 = 这支引擎种不下**
            # (老引擎上是整格缺席,不是 `null`),一律 `d.get("plugins", {}).get(…)`。
            "edge_author_type": "edge",
            "edge_author_section": AUTHOR_SECTIONS["edge"],
            "authored_edge_keys": list(AUTHORED_EDGE_KEYS),
            "edge_author_gloss": (
                "一条边种成 `{\"kind\": \"author\", \"type\": \"edge\", \"body\": "
                "{\"type\": \"menpai.member_of\", \"from\": \"agent:阿岚\", "
                "\"to\": \"menpai.sect:青云门\"}}`。两端的形状问 "
                "`edge_node_id_forms`,别照文档抄。"
                "⚠️ **`facts` 这一格有意不收**:声明过的事实照默认值落地"
                "(和运行期 `link` 同一条路),写它当场被拒 —— 理由是运行期那条路上"
                "「声明的默认值带命名空间、手写的 `facts` 不带」今天就不一致,"
                "收作者层这一格等于把一个已经在打架的语义再复制一份。"
                "🔴 **出厂插件的边一律拒**(`invitation.*` 那一族):它们是内核"
                "**投影的物化视图**,每次开机照事件日志重建,手写一行要么下一秒"
                "被抹掉、要么就是伪造这个世界的历史。"
                "⚠️ **只填缺不覆盖的粒度是「每一种边」,不是每一条**:装进一个"
                "跑过的世界时,只种这个世界里还一条都没有的那几种,已经有行的"
                "那一种整种跳过并点名。理由是边不进事件日志,引擎分不出"
                "「还没连」和「连过又断了」——逐条补会让每次带 `--world-file` 的"
                "重启都把运行期断掉的边接回来。"
            ),
            # 🔴 **这一版收不下的作者层段,连理由一起报** —— 和
            # `deferred_fact_shapes` 逐字同构。一句光秃秃的「不支持」会让作者
            # 以为自己写错了字;而**这一格在不在**就是消费方的探测位。
            # ✅ **2026-08-31:`edge` 从这一格里消失了**(收件箱 D44 已办),
            # 于是今天它是空的 —— 而**空对象不等于缺席**:整格缺席 = 3.8.0 之前
            # 的老引擎(它连这个问题都答不出),空对象 = 这一版作者层一个段都不欠。
            # 别把这一格删掉:删了,消费方那句 `d.get(…, {})` 再也分不出这两件事。
            "deferred_author_sections": {},
            # 🆕 动词。**按 tool-calling 的 JSON schema 声明**(设计 §12.3):
            # NPC 挑动词和玩家点按钮读的是同一份定义。
            "verb_declaration": "tool-calling",
            "verb_keys": list(VERB_KEYS),
            # 动词的 `effects` 这一版只收边那三条 —— 改量写在动词自己的 `set` 里
            # (那是本体那一层,`me_*` / `have_*` 都认)。
            "verb_effects": list(EDGE_VERB_EFFECTS),
            # 🔴 **这一版的动词必须有 `target`。** 「开宗立派」那种不对着任何东西
            # 做的动词今天没有一条调用路(能力调用一律是
            # `act(她, interact, {target, verb})`),写了它**开不了机** ——
            # 装上去让谁也点不动,比开不了机坏。
            "verb_requires_target": True,
            # ⚠️ **target 只认插件自己声明的种类,以及作者写在 `kinds` 里的种类。**
            # 🔴 **`agent` 不是"还不行",是永远不收**(裁决 ①)——「拜某人为师」
            # 「把东西给某人」那一族走**工具路 + 同意门**,不从 affordance 走。
            # 理由见下面 `verb_target_never_why`。
            # ⚠️ 这句话第一版写的是「这一期还写不出来」,和它下面三行正面矛盾
            # (2026-08-26 复核评审逮的)—— **两句打架的话比两句里哪一句都糟**:
            # 读的人会按"以后会支持"去规划,而那件事永远不会来。
            "verb_target_forms": list(KIND_PREFIXES) + ["<作者写的种类 id>"],
            # 🔴 **永远不收的那几个词**(裁决 ①,2026-08-26 老板同意分期收窄)。
            # 它们不是"还没做" —— 对着一个人做的动作要过**同意**那道门,而
            # affordance 这一层没有同意的位置(邀请三扇门才有)。对人的动词走
            # **工具路 + 同意门**,排第 3 期和判定同期。**tool 别把这一族画进界面。**
            "verb_target_never": list(BUILTIN_TARGETS),
            "verb_target_never_why": (
                "对着一个人做的动作要过同意那道门,而能力(affordance)的形状是"
                "「一个人、一样东西、一个瞬间」——把一个人放进 target 格,"
                "`拜师` 就成了单方面把别人变成师父。这个引擎为「他肯不肯」建过"
                "一整套东西(邀请三扇门 + joint_gate + INVITE_OUTCOMES),"
                "**拒绝是一等公民**。对人的动词走工具路 + 同意门(设计 §12.3),第 3 期。"
            ),
            # 🔴 **边上的规律这一版只认 `set`**(2026-08-26 验收 B)。
            # 从前 `effects` 含 `emit`、`rule_selectors` 含 `edge`,而**没有一格说
            # 这个组合不成立** —— 写了 emit 一条事件都不发,开机不拦、零 warning。
            # 现在是加载期拒 + 这一格说出来。**将来收 `emit` 是加法。**
            # 🆕 2e:`emit` 收进来了 —— **纯加法**,而顺序是承重的:
            # 先有使用者(邀请的过期规律「到点发一件事」),再开这个口子。
            # 反过来就是本仓那三道闸挡的「超前于消费方」。
            "edge_rule_effects": ["set", "emit"],
            # `for_each` 认哪几种选择器。第 2 期多了 `edge`。
            "rule_selectors": list(RULE_SELECTORS),
            # 🆕 `bearer` 三个词(2026-08-26 老板自判):`actor` = 角色+玩家
            # (**今天的语义**)· `agent` = 只角色 · `player` = 只玩家。
            # ⚠️ **`agent` 是第 1 期刚公布的词,那时它是"两种人"** —— 装载时
            # 读成 `actor`,消费方按 `bearer_aliases` 判,别自己记一条特例。
            "bearer_aliases": dict(BEARER_ALIASES),
            "deferred_fact_shapes": {k: v for k, v in sorted(DEFERRED_SHAPES.items())},
            "bearer_forms": list(BEARER_FORMS),
            "effects": list(EFFECTS),
            # 表达式里的命名空间语法。**一层,不是属性访问**(见
            # `expressions._validate` 那段:它是安全边界)。`me_` 前缀读的是
            # 施动者身上那一份,和内核的量逐字同构。
            "namespace_syntax": "<plugin>.<fact>",
            # 🆕 **写只写得到自己的命名空间,`costs` 也不例外**(3.8.0,2026-08-27
            # 第二波 ① 裁决)。三条写路(规律的 `set` / 触发器的 `set` / 动词的
            # `costs` 与 `set`)从此给同一个答案。⚠️ 设计稿 §4.2 有一个
            # `costs: {"economy.coins": …}` 的例子,它写在这条边界定下来之前 ——
            # **以这一格为准**。
            "namespaced_write_scope": "self",
            "namespaced_write_gloss": (
                "动词的 `costs` / `set` 里,**带命名空间的键**(`<插件>.<事实>`)"
                "只许是**自己的**顶层 `facts`,而且要挂对身子:`costs` 扣施动者"
                "(bearer `actor` / `player`),`set` 写目标(bearer "
                "`entity:<这个动词的 target>`)。裸名字照旧归本体那一层判"
                "(种类声明过的量)。🔴 **别人的事实读得到、写不了**:直接写有一条"
                "更硬的理由 —— 别人的事实可能是 `projected`(真相是事件流,量表里那个数"
                "只是物化视图),**扣下去重开一次就回来了,而没有一处报错**。"
                "🔴 **`projected` 的事实一律写不得**,自己的也不行。"
                "\n**「读得到」照字面这么写**(2026-08-28 敲过一遍才写下来的):"
                '`"reads": ["mana.魔力"]` + `"requires": ["me_mana.魔力 >= 5"]` —— '
                "⚠️ **`requires` 只准读 `me_*`**,所以那个前缀不能省;"
                "而 `reads` 在这条路上**是承重的**(2026-08-28 起):不写它,"
                "两扇门与开机一起拒。"
                "⚠️ **今天读不到出厂插件的事实**(`economy.coins` 那种):`reads` 要求"
                "依赖是这个世界的**插件声明**里的一个,而出厂那几个不是作者记录 —— "
                "两扇门与开机会一起说「没有装 `economy` 这个插件」。跨到出厂那几个"
                "身上,等它们成为可声明的依赖那一期。"
            ),
            "actor_namespace_syntax": "me_<plugin>.<fact>",
            "state_in_expressions": "ordinal",
            "text_max_chars_default": DEFAULT_TEXT_MAX_CHARS,
            "read_command": "plugin list",
            "remove_command": "plugin remove <id> --yes",
            "gloss": (
                "插件 = 作者层第十三个段(`{\"kind\": \"author\", "
                "\"type\": \"plugin\", \"body\": {…}}`)。事实的存储键是 "
                "`<id>.<key>`,**住在今天的量表里**(`stock:{owner}`),不新造存储。"
                "三条边界:只写自己的命名空间(越界开不了机)· 读别人的要 `reads` "
                "声明(未声明开不了机)· 依赖图定装载顺序(缺依赖、成环当场报)。"
                "⚠️ **`state` 在表达式里是序号**(按 `values` 顺序从 0 起),"
                "不是那个词 —— 和字符串比大小是**加载期错误**,报错里会告诉作者"
                "该写几。⚠️ 触发器**队列在 tick 开头快照、drain 一遍**,"
                "自己 emit 的落进下一 tick:没有同轮递归,代价是滞后一轮"
                "(和规律那一层的双缓冲同一笔账)"
            ),
        },
        # 🆕 3.9.0:主持人 —— 「世界永远先开口」那一屏。
        # **这一格缺席 = 这支引擎没有它**(不是 `null`)。按段探测,别比版本号。
        "host": {
            "method": "host_turn",
            # 🆕 3.10.0(批 1.2):对话那一侧的两扇门。**这一段缺席 = 这支引擎的
            # 「世界先开口」只做进了主持人那一屏,没做进对话里**(玩家点「跟她
            # 说说话」拿到的还是一个空白输入框)。按段探测,不比版本号。
            "opening_method": "chat_open",
            "suggestions_method": "chat_suggestions",
            "cli": "anima-world player host --player <pid> [--json] [--ask]",
            "moments": list(HOST_MOMENTS),
            "option_kinds": list(OPTION_KINDS),
            "door_methods": list(DOOR_METHODS),
            # 🔴 **每种门的 params 键集写在这儿,消费方按段对表、别按名字猜**
            # (2026-09-02 站点量出来的:契约只写了 `player_tool` 一种,另外四种
            # 站点只能按自己的字段名假定 —— 猜对了没人知道,猜错了那一项画得出来
            # 但点不动)。`?` 结尾 = 可选。
            "door_params": {
                "answer_invitation": ["invite_seq", "accept"],
                # 🆕 3.10.0(批 1.2 ①):`opening` —— 真时点下去**她先开口**
                # (走 `World.chat_open`);缺席 / False = 老样子,你先说。
                # **判断在引擎侧**:让宿主自己猜"要不要让她先说",就是让它拿一份
                # 对世界的猜测做决定。
                "chat": ["agent_id", "text?", "opening?"],
                "player_walk": ["location"],
                "player_tool": ["tool_id", "params"],
                "free": [],
            },
            "tones": list(TONES),
            "free_option_id": FREE_OPTION_ID,
            "free_option_always": True,
            "event": "host_scene",
            "max_options_key": "host.max_options",
            "ask_cooldown_key": "host.ask_cooldown_ticks",
            # 🆕 3.10.0(批 1.1 ③):**`arrive` 那一屏之后那一次问不起冷却。**
            # 龙族 `ask_cooldown_ticks=12` × 5 真实分钟 = 一个刚进门的新玩家
            # 「我该干嘛」整整 60 真实分钟按不动,而那正是他最需要按它的一个小时。
            # 冷却防的是"连点十下 = 十次 LLM 调用",而进门第一次问不是那件事。
            "ask_free_after": "arrive",
            # 🆕 3.10.0(2a-②):第五个时刻。两个判据都是减法,**零新状态** ——
            # 他刚才在不在(在场行带 TTL),以及他上一屏之后有没有新的 `pack_installed`。
            "away_key": "host.away_ticks",
            "return_reads": ["presence", "pack_installed"],
            # 冷却那个数**也带在 `host_turn` 的返回里**(`ask_ready_tick` /
            # `ask_ready`):站点对世界只有 `/internal/v1/*`,够不着这扇门 ——
            # 一个到不了消费方的契约格,等于没有这一格。
            # ⚠️ **这张表是下游照着写解析器的那一行,加一格就要同轮跟上** ——
            # 3.9.0 那一轮 `who` 那一格漏在 REFERENCE 上,站点三处同缺一格而没有
            # 一处会红。`ask_ready_text` 是 3.10.0(批 1.1 ③)加的。
            "turn_keys": ["player_id", "tick", "day", "place", "place_name", "trigger",
                          "scene", "options", "ask_ready_tick", "ask_ready",
                          "ask_ready_text", "blocked", "blocked_text"],
            "option_keys": ["id", "kind", "label", "who", "hook", "tone", "available",
                            "reason", "refusal", "cost", "door"],
            "gloss": (
                "一次调用交出一屏:一段场景 + 至多 `host.max_options` 个选项"
                "(**世界里有多少就递多少**,少的时候就少 —— 这不是一句"
                "「保证有 3–5 个」的承诺)+ **永远在最后**的自由输入"
                "(`kind:\"free\"`,不占 `host.max_options` 的名额)。"
                "🔴 **主持人是荐者不是执行者** —— 每一项的 `door` 都指向今天已经存在"
                "的那扇门,引擎这一层一条新的「写世界」的路都不开。"
                "`available` / `reason` / `refusal` / `cost` 是 `player-options` 那四格"
                "**原样透传**,别另算。"
                "**只在五个时刻开口**(进地点 / 新一天 / 指着他的剧情拍响 / 他点"
                "「我该干嘛」/ 「你回来了」),闸在引擎里(时刻钥匙 vs 上一条 "
                "`host_scene` 事件);"
                "三样同时变时报最强的那个,而次序是 `return` > `arrive` > `beat` > "
                "`new_day` —— 一个离线三天的人回来时多半也换了地方,而这两句话里"
                "他更需要读到的是「你不在的时候……」。**这几个名字以 "
                "`contract --json` 的 `host.moments` 为准,别照这句话数**。"
                "没到时刻就原样返回上一屏,`scene.source == \"cached\"`。"
                "🔴 **`card.billing == \"hidden\"` 的角色不进候选,也不进给模型的那份"
                "提示** —— 那三扇结构化的门你们能按行筛,而这一屏是散文,筛一半比不筛"
                "更坏,所以这道闸在引擎侧。"
                "场景那段话走背景槽,一次调用、失败即模板、不重试;**挑哪几项是纯算术**,"
                "同一个世界同一时刻挑两次逐项相同。"
            ),
        },
        "beats": {
            "schema_version": None,
            # 🆕 3.7.0(看板 D1):节拍进得了 `.cyberworld` 了,而且**首启自己带上**。
            # **这一格缺席、或者是 `null`** = 这支引擎的节拍只能靠 `--beats` 单独
            # 喂一个文件(3.6.0 及更早)—— 而舰队上没有任何一条路会去传那个参数,
            # 于是作者写的剧情**一拍都不响,零报错**。
            # **按这一格探测,别比版本号。**
            # ⚠️ **"缺席"是这里真正会发生的那一支,`null` 反而一支引擎都不会答**
            # (2026-08-21 在 3.6.0 那个 venv 上实测):`beats` 段它**有**,而
            # `author_type` 这一格**没有** → `payload["beats"]["author_type"]`
            # 当场 `KeyError` 退 1。**一个崩掉的探测器不是"探测出没有"** ——
            # 它长得像"这台机器坏了",而调用方多半会去查错的地方。
            # 一律 `.get("beats", {}).get("author_type")`。
            "author_type": "beat",
            "world_file_section": "beats",
            "storage_key": "anima:{world_id}:beats",
            # 交接**仍然是一件产物** —— 契约的形状一个字没改(看板 D1 的选项 a)。
            "separate_file_flag": "--beats",
            "report_section": "beats",
            "gloss": (
                "节拍是**作者层**(和人物、地点、关系同属「作者写下的」),3.7.0 起"
                "写成 `{\"kind\": \"author\", \"type\": \"beat\", \"body\": {…一拍…}}` "
                "进世界文件,首启不给 `--beats` 也会带上。"
                "⚠️ `--beats` 仍然认,而且**赢这一趟但不写库**:它是一次明示的覆盖"
                "(试炼、调试靠它),让它写回去就等于一次试炼把作者的剧情换掉。"
                "⚠️ **「哪几拍响过」不在库里**,它从 `beat_fired` 事件重放 —— 两份"
                "真相里存一份,另一份必然有一天对不上,而这一层对不上的样子是"
                "「这一拍又响了一次」。`report` 的 `beats` 段答「声明了几拍、响了几拍、"
                "哪几拍一直没响」"
            ),
            "ops": sorted(VALID_OPS),
            "op_required_fields": {
                op: sorted(fields) for op, fields in sorted(OP_REQUIRED_FIELDS.items())
            },
            "predicates": sorted(_VALID_PREDICATES),
            "predicate_required_fields": {
                pred: sorted(fields)
                for pred, fields in sorted(PREDICATE_REQUIRED_FIELDS.items())
            },
            # 🆕 3.9.0:剧情拍指得到「任何玩家」了。
            # **这一格缺席 = 这支引擎没有它**,而"没有它"的样子是这个仓库最怕的那种:
            # 写了 `for_each` 的包在 3.8.0 上**开得了机**(那一版一条拍的顶层键一个
            # 都不查)、拍按世界时响一次、`mark_fired`、烧掉 —— 静默作废,重启不重放。
            # **发舰队前按这一格探测,不比版本号**(`anima-world:3.8.0` 这个名字下
            # 已经有过两支能力不同的引擎)。
            "player_selector": {
                "for_each": {"node": sorted(FOR_EACH_NODES)},
                "token": PLAYER_TOKEN,
                "binds_to": "player:{player_id}",
                "beat_keys": sorted(BEAT_KEYS),
                # 零点来自哪条事件,以及 `at.day` 相对它怎么算 ——
                # **偏移,不是序数**(`0` = 入场当天)。
                "day_zero": "player_join",
                "day_is_offset_from_join": True,
                "once_scope": "per_player",
                "fired_event_key": "for",
                # 🆕 3.10.0(批 1.1 ⑤):**这一拍响的时候给玩家看的那一句话。**
                # 只写得在指着玩家的拍上(世界级的拍没有「那个人」,写下去谁也到不了
                # —— 当场拒绝,不是静默无效)。它进 `beat_fired.narrate`,
                # 也进那个玩家的叙事流(`narrative`,`source: "template"`)。
                "narrate_key": "narrate",
                "narrate_event_key": "narrate",
                # 🔴 **逐格的收拒表,而不是一句"支持玩家了"。** 一句话的能力声明
                # 挡不住"写下去、开得了机、什么都不发生"那一族 —— 而拒绝正是这一层
                # 唯一便宜的东西。镜像端照这两张表写自己的提醒。
                "op_player_fields": {
                    op: sorted(fields)
                    for op, fields in sorted(PLAYER_ALLOWED_OP_FIELDS.items())
                },
                "predicate_player_fields": {
                    pred: sorted(fields)
                    for pred, fields in sorted(PLAYER_ALLOWED_PREDICATE_FIELDS.items())
                },
                "gloss": (
                    "一条拍顶层写 `\"for_each\": {\"node\": \"player\"}`,之后 "
                    "`payload` / `trigger.when` 里的保留字 `player` 指**这一趟展开的"
                    "那个人**。三件配套语义:`trigger.at.day` 是**从他入场那天起算的"
                    "偏移**(零点是 `player_join` 事件,记在账本上不记在带 TTL 的在场上)"
                    "—— 🔴 **偏移不是序数**:`day: 0` = 他入场当天,`day: 1` = 入场后"
                    "第二天,一封「报到当天该拿到的信」写 `0` 不写 `1`;`once` 按玩家**各算一次**(`beat_fired` 多一格 "
                    "`for`,老事件没有它 = 世界级);**每一格写不写得下 `player` 是"
                    "加载期判的**,拒了就当场说,没有第三种「静默跳过」。"
                    "不写 `for_each` 的老拍逐字不变 —— 声明本身就是开关。"
                ),
            },
            # 🆕 3.10.0(周更链路 2a-①):**拍的零点有第三种了。**
            # **这一格缺席 = 这支引擎按世界时算每一条拍** —— 而那正是"一份写着
            # `day: 0..6` 的第 2 周包装进跑到第 40 天的世界,八拍一 tick 全烧"
            # 的那一支。**按这一格探测,不比版本号。**
            "since": {
                "values": list(AT_SINCE),
                "default": AT_SINCE[0],
                "at_keys": list(AT_KEYS),
                "trigger_keys": list(BEAT_TRIGGER_KEYS),
                "combines_with_player": "max",
                "gloss": (
                    "`trigger.at.since` 说这条拍的 `day` 从哪一天算起。**缺省 "
                    "`\"pack\"` = 从它所属的那份内容包落地那天** —— 一条不属于任何包"
                    "的拍(创世那批)于是照旧从世界第 0 天算,逐字如旧。"
                    "`\"world\"` 是逃生舱:我要的就是世界第 N 天。"
                    "🔴 **和 `for_each: {\"node\": \"player\"}` 同时写时取 `max`** ——"
                    "老玩家从包落地起算(否则第 2 周的剧情对他永远不响),而包落地"
                    "三天后才进来的新玩家从他自己那天起算(否则他一进门就被一堆"
                    "过期的拍砸中)。**只有 `max` 能同时让这两句话成立。**"
                    "⚠️ `trigger` 与 `trigger.at` 这两层的键从 3.10.0 起是**闭集**"
                    "(此前一个都不查,`since` 写下去会被照收然后丢掉)。"
                ),
            },
        },
        # 🆕 3.10.0(周更链路 2a-①):**内容包。作者层第十五个段。**
        # **这一段缺席 = 这支引擎不认内容包** —— 装包的门、`pack list`、
        # `pack_installed` 事件一个都没有,而一份带 `pack` 记录的包在它上面是
        # **开不了机的硬失败**(不认识的作者层 `type`)。
        # **按这一段在不在探测,不比版本号。**
        "packs": {
            "author_type": "pack",
            "world_file_section": "pack",
            "one_pack_per_file": True,
            "pack_keys": list(PACK_KEYS),
            "id_pattern": PACK_ID_PATTERN,
            "event": "pack_installed",
            # 🆕 3.10.0(2a-② K7):停用。**追加一条事实,不删任何东西** ——
            # 玩家的记忆里有这一周发生过的事。再装一次同一个包 = 重新启用。
            "disable_event": "pack_disabled",
            "disable_method": "disable_pack",
            "subscribable": False,
            "method": "install_pack",
            "cli": ("anima-world pack install <file> / anima-world pack list / "
                    "anima-world pack disable <id>"),
            # ⚠️ **两张表都用「编译段名」,不混作者层 `type`**(2a-① 验收:
            # 上一版 `installs_sections` 报的是 `beat`/`config`/`world_setting`
            # (作者层的 `type`),而 `merge_sections` 报的是 `beats`/`kinds`/…
            # (编译管线的段名)—— **同一份契约里两套名字**,而读的人要自己猜
            # 哪一格用哪一套。段名是 `AUTHOR_SECTIONS.values()` 那一套,
            # 作者层 `type` 另有 `author_type` 那一格报。
            "installs_sections": ["beats", "config", "world_setting"],
            "merge_sections": sorted(
                set(world_file.AUTHOR_SECTIONS.values()) - {"beats"}
            ),
            "section_names_are": "编译段名(`AUTHOR_SECTIONS` 的值),不是作者层 `type`",
            "storage": "事件日志(`pack_installed`)—— 没有第二张表",
            "gloss": (
                "一份内容包的身份:`{\"kind\": \"author\", \"type\": \"pack\", "
                "\"body\": {\"id\", \"version\", \"note\"}}`。**一个文件就是一个"
                " pack**,这份包里的作者记录全归属它 —— 段是「对象型」,"
                "「一个文件里两个包」在这一层根本表达不出来。"
                "🔴 **身份不放 manifest**:manifest 的不认识键进 `extra`,老引擎会"
                "**静默忽略身份而照装内容**,包进去了、以后再也说不清那几拍是哪一周"
                "加的。作者层的 `type` 是闭集,老引擎见到它是**开不了机的硬失败** ——"
                "所以带 `pack` 记录的包 `engine_min` 必须写 `3.10.0`。"
                "🔴 **「装了哪几周」没有第二张表**:它折自 `pack_installed` 事件"
                "(`World.packs()`),和余额折自 `payment` 逐字同一种。存一份直接写的"
                "清单就多出一种和日志对不上的坏法,而这一层对不上的样子是"
                "「这一周的拍从哪天起算」答错,没有一处会报错。"
                "⚠️ `pack_installed` **不进** `plugins.subscribable_events`:它的当事人"
                "是世界**外面**的人(作者、运营),订它的插件做不出任何世界里的事。"
            ),
        },
    }


def run_pack(args: argparse.Namespace) -> int:
    """`anima-world pack install / list` —— 往一个**跑着的**世界投一份内容包。

    🔴 **它开的是一扇写门,而它没有 `--dry-run` 的对偶**:`world drop` /
    `player erase` / `plugin remove` 那三条默认预演,理由是**它们删东西**;
    装一份包是加东西,而"加了什么"由回执逐格答得出。要**先**知道装不装得进,
    那条命令已经有了:`anima-world world check <文件> --edit --json`。
    """
    redis, world_id, mysql = _world_args(args)
    command = getattr(args, "pack_command", None)
    if command not in {"install", "list", "disable"}:
        print("[pack] 只有 install / list / disable 三个子命令", file=sys.stderr)
        return 2
    if not _world_exists(redis, world_id):
        # 装包是**给一个已经在跑的世界**加东西 —— 对着一个不存在的名字创世,
        # 拿到的是一个"排版正常、时钟 0"的新世界,而作者以为他更新了线上那个。
        # (`map` 那条老教训 `5ce6aed` 的同一种。)
        print(f"[pack] 还没有 {world_id!r} 这个世界。装包是给一个「已经在跑的」"
              "世界投更新;要新建请用 `anima-world start --world-file`。",
              file=sys.stderr)
        return 2

    from anima_world.api import World

    world = World.open(world_id, redis=redis, mysql=mysql, force_mock_llm=True)
    try:
        if command == "list":
            rows = world.packs()
            if getattr(args, "as_json", False):
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            if not rows:
                print("这个世界还没有装过内容包。")
                return 0
            print(f"{world_id} 装了 {len(rows)} 份内容包:")
            for row in rows:
                sections = row.get("sections") or {}
                what = "、".join(
                    f"{name} {len(ids)}" for name, ids in sorted(sections.items())
                ) or "(空)"
                note = f" —— {row['note']}" if row.get("note") else ""
                print(f"  · {row['id']} v{row.get('version') or '?'}"
                      f"(第 {row.get('day', 0)} 天落地){note}")
                print(f"      带了:{what}")
            return 0

        if command == "disable":
            try:
                receipt = world.disable_pack(args.pack)
            except PackInstallError as exc:
                print(f"[pack] 停用不了 {args.pack!r}:", file=sys.stderr)
                for line in exc.errors:
                    print(f"  ✗ {line}", file=sys.stderr)
                return 2
            if getattr(args, "as_json", False):
                print(json.dumps(receipt, ensure_ascii=False, indent=2))
                return 0
            print(f"内容包 {receipt['pack']} 停用了(世界第 {receipt['day']} 天)。"
                  "「这不是删除」—— 已经发生过的事一件都没动。")
            if receipt["beats"]:
                print(f"  · {len(receipt['beats'])} 拍从此不再响"
                      "(已经响过的照旧响过)")
            if receipt["agents"]:
                print(f"  · {len(receipt['agents'])} 个人退场:"
                      f"{'、'.join(receipt['agents'])}")
            if receipt["config"]:
                print(f"  · {len(receipt['config'])} 个开关回落到装包前那个值:"
                      f"{'、'.join(receipt['config'])}")
            if receipt["kept"]:
                print(f"  · {len(receipt['kept'])} 个开关装完之后被人调过,留着没动:"
                      f"{'、'.join(receipt['kept'])}")
            return 0

        try:
            receipt = world.install_pack(args.file, force=bool(getattr(args, "force", False)))
        except PackInstallError as exc:
            # **作者看到的该是那几行中文,不是一段 Python 堆栈**
            # (2026-08-26 验收 C 那条:一次被拒的降级,屏幕先甩 Traceback)。
            print(f"[pack] 这份包装不进 {world_id!r}:", file=sys.stderr)
            for line in exc.errors:
                print(f"  ✗ {line}", file=sys.stderr)
            return 2
        if getattr(args, "as_json", False):
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
            return 0
        print(f"内容包 {receipt['pack']} v{receipt['version']} 装进了 {world_id}"
              f"(世界第 {receipt['day']} 天)。")
        if receipt["beats"]:
            # ⚠️ **别断言一件这一屏不知道的事**:上一版这里写死着「`day: 0` 的那
            # 一拍下一 tick 就响」,而这份包里可能一条 `day: 0` 都没有 ——
            # 一句念不通的话和一句错的一样贵。说零点,别替它算什么时候响。
            print(f"  · {len(receipt['beats'])} 拍剧情 —— 它们的 `day` 从「今天」"
                  f"(第 {receipt['day']} 天)起算")
        if receipt["config"]:
            print(f"  · {len(receipt['config'])} 个开关:{'、'.join(receipt['config'])}")
        if receipt["world_setting"]:
            print("  · 世界观换了一段")
        if receipt["agents"]:
            print(f"  · {len(receipt['agents'])} 个新角色进了这个世界:"
                  f"{'、'.join(receipt['agents'])}")
        if receipt["locations"]:
            print(f"  · {len(receipt['locations'])} 个新地点上了地图:"
                  f"{'、'.join(receipt['locations'])}")
        return 0
    finally:
        world.close()


def run_plugin(args: argparse.Namespace) -> int:
    """`anima-world plugin list / remove` —— 这个世界装着哪些插件,以及卸掉一个。

    **`remove` 默认是预演**(和 `world drop` / `player erase` 同一个习惯):不带
    `--yes` 只数要删多少,一个字节都不写。⚠️ 任务单里写的是 `--dry-run`,而这个
    CLI 上"危险操作默认预演、`--yes` 才动手"已经是两处的既有习惯 —— **同一把
    CLI 上两种约定,比哪一种都糟**:照另一处的记忆敲下去的人会真的删掉东西。
    """
    from anima_world.plugins import remove_plugin

    redis, world_id, mysql = _world_args(args)
    command = getattr(args, "plugin_command", None)
    if command not in {"list", "remove"}:
        print("[plugin] 只有 list / remove 两个子命令", file=sys.stderr)
        return 2
    if not _world_exists(redis, world_id):
        print(f"[plugin] 还没有 {world_id!r} 这个世界。", file=sys.stderr)
        return 2

    from anima_world.api import World

    world = World.open(world_id, redis=redis, mysql=mysql, force_mock_llm=True)
    try:
        scheduler = world.scheduler
        store = scheduler.plugin_store
        if command == "list":
            rows = [
                {
                    "id": plugin.id, "version": plugin.version, "label": plugin.label,
                    "facts": sorted(plugin.facts), "bearers": sorted(plugin.bearers()),
                    "rules": len(plugin.rules), "triggers": len(plugin.triggers),
                    "reads": sorted(plugin.reads),
                    # 🆕 第 2 期这三样从前**一字不报**(2026-08-26 验收 C):
                    # 一个只声明边和动词的插件,这一行印出来是「挂在 」加一片空白。
                    # ⚠️ **动词报的就是 `Verb.schema()` 那一份**(tool-calling 形状)
                    # —— 给它接上第一个生产路径的消费者:一份没人读的 schema
                    # 和一份没有的 schema,坏起来长得一模一样。
                    "kinds": sorted(k.kind_id for k in plugin.kinds.values()),
                    "edges": sorted(e.qualified for e in plugin.edges.values()),
                    "verbs": [v.schema() for v in
                              sorted(plugin.verbs.values(), key=lambda v: v.name)],
                    # **装载顺序是答案的一部分**:依赖图定的那个顺序决定"我读的那个
                    # 量在我第一次求值时在不在库里",而它不是字母序。
                    "order": index,
                }
                for index, plugin in enumerate(scheduler.plugins)
            ]
            if getattr(args, "as_json", False):
                print(json.dumps({"operation": "plugin list", "world_id": world_id,
                                  "plugins": rows}, ensure_ascii=False, indent=2))
                return 0
            if not rows:
                print("[plugin] 这个世界一个插件都没装。")
                return 0
            for row in rows:
                head = f"{row['id']} {row['version']}"
                print(f"  {head:<24}{row['label'] or '—'}")
                parts = [f"事实 {len(row['facts'])}", f"规律 {row['rules']}",
                         f"触发器 {row['triggers']}"]
                # **只印真有的那几样**:一个只声明边和动词的插件,从前这一行是
                # 「挂在 」加一片空白 —— 一个空着的字段比没有这个字段更难读。
                for label, key in (("种类", "kinds"), ("边", "edges"), ("动词", "verbs")):
                    if row[key]:
                        parts.append(f"{label} {len(row[key])}")
                if row["bearers"]:
                    parts.append(f"挂在 {'、'.join(row['bearers'])}")
                print(f"    {onboarding.dim(' · '.join(parts))}")
                for name in row["kinds"]:
                    print(f"    {onboarding.dim('种类:' + name)}")
                for name in row["edges"]:
                    # 数得出「边 1」而印不出它叫什么,等于让人再去问一次
                    # (2026-08-26 复核评审)。
                    print(f"    {onboarding.dim('边:' + name)}")
                for spec in row["verbs"]:
                    print(f"    {onboarding.dim('动词:' + spec['name'] + ' —— ' + spec['description'])}")
                if row["reads"]:
                    print(f"    {onboarding.dim('读别人的:' + '、'.join(row['reads']))}")
            return 0

        # 🔴 **出厂插件卸不动 —— 卸它的正确动作是关那个开关。**
        #
        # 2026-08-26 验收 C 实测:`plugin remove needs --yes` 答「卸了:事实 3 种、
        # 9 处值」,redis 里也真没了 —— **而下一次开机它装回来,三个值全变回 1.0**。
        # 一次会掉数据的空操作:回执说成功、屏幕不提一个字,而她的精力被悄悄补满。
        # REFERENCE §10.9 自己刚写过这句话(「删掉的话『关一下再开』会把她的精力
        # 悄悄补满」)—— 现在正是那一句。
        #
        # 为什么是**拒**而不是"真卸且下次不装回":后者要记一个"作者卸过"的标记,
        # 而那个标记和 `needs.enabled` 就是同一件事的第二份答案 —— 两处判断迟早
        # 给出不同答案,这个仓库反复栽的那一跤。**开关只有一个。**
        if args.plugin in FACTORY_PLUGINS:
            switch = FACTORY_PLUGINS[args.plugin]
            print(f"[plugin] `{args.plugin}` 是出厂插件,卸不动 —— 它下一次开机"
                  f"会照 `{switch}` 装回来,而这一趟删掉的值一个都回不来"
                  f"(她的精力会被悄悄补满)。\n"
                  f"[plugin] 要关掉它:anima-world config set {switch} false "
                  f"--world-id {world_id}\n"
                  f"[plugin] 关掉不删数据 —— 再打开从原处接着走。",
                  file=sys.stderr)
            world.close()
            return 2
        receipt = remove_plugin(
            args.plugin, store=store, stock_store=scheduler.stock_store,
            visibility_store=scheduler.visibility_store,
            owners_of=lambda bearer: _plugin_owners(scheduler, bearer),
            edge_store=getattr(scheduler, "edge_store", None),
            dry_run=not args.yes,
            emit=scheduler._record_and_deliver,
        )
    finally:
        world.close()

    if getattr(args, "as_json", False):
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0 if receipt["found"] else 2
    if not receipt["found"]:
        print(f"[plugin] 这个世界没装 {args.plugin!r}。", file=sys.stderr)
        return 2
    verb = "卸了" if args.yes else "要卸"
    print(f"[plugin] {receipt['plugin']} —— {verb}:"
          f"事实 {len(receipt['keys'])} 种、{receipt['facts']} 处值、"
          f"边 {len(receipt.get('edge_types') or ())} 种 {receipt.get('edges', 0)} 条。")
    if not args.yes:
        print("[plugin] (没带 --yes:世界一个字节都没动)")
    else:
        print("[plugin] 它的规律与触发器随这一行记录一起消失了;"
              "历史一个字没动 —— 它发过的事件还在日志里。")
    return 0


#: 玩家那一族 owner 的前缀 —— `agent:player:<id>`。它是 `stock_owner_of` 与
#: `Scheduler.PLAYER_PREFIX` 拼出来的,写在一处是为了三个调用点不各拼一遍。
_PLAYER_OWNER_PREFIX = "agent:player:"


def _plugin_owners(scheduler: Any, bearer: str, *, location_store: Any = None,
                   stock_store: Any = None) -> list[str]:
    """这个 bearer 此刻有哪些 owner。**装载、卸载、抹除三处共用这一份判断。**

    ⚠️ **`actor` / `player` 落在同一族 owner 上,靠前缀分**:角色是 `agent:夏`,
    玩家是 `agent:player:p1` —— 两种人**同一个命名空间**(`stock_owner_of` 那条
    「`me_*` 读的是一个人身上的量,而这件事对两种人是同一件」)。分它们的从来不是
    两张表,是那个前缀。
    """
    stocks = stock_store if stock_store is not None else scheduler.stock_store
    if bearer == "world":
        return ["world"]
    if bearer in ("actor", "agent"):
        return list(stocks.owners("agent"))
    if bearer == "player":
        return [o for o in stocks.owners("agent")
                if o.startswith(_PLAYER_OWNER_PREFIX)]
    if bearer == "location":
        store = location_store if location_store is not None else scheduler.location_store
        return [f"location:{row['id']}" for row in ((store.all() if store else []) or ())
                if row.get("id")]
    kind = bearer.split(":", 1)[1] if ":" in bearer else bearer
    ontology = scheduler.ontology
    if ontology is None:
        return []
    return [e.id for e in ontology.entities.values() if e.kind == kind]


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
    write_cmd = payload["seed"]["location_image_write_command"]
    print(f"  地点图出口     读出口 anima-world {payload['seed']['location_image_read_command']}"
          f"   写出口 " + (f"anima-world {write_cmd}" if write_cmd else "没有(只在作者层落地)"))
    print(f"  能力字段       {payload['seed']['affordance_keys']}")
    imp = payload["seed"]["affordance_importance"]
    print(f"  importance     {imp['range'][0]}~{imp['range'][1]},可选;"
          f"不写 = 谁都不记得(这一层整个缺席)   "
          f"读出口 anima-world {imp['read_command']}")
    print(f"  节拍 op        {', '.join(payload['beats']['ops'])}")
    print(f"  节拍谓词       {', '.join(payload['beats']['predicates'])}")
    # 配置键那一段:人这儿只印**数**与分类,逐键那份走 `--json`。
    # 66 行刷屏对人没有用,而"这一版引擎认哪几类配置"是一眼就该看见的。
    groups: dict[str, int] = {}
    for row in payload["config"].values():
        groups[row["category"]] = groups.get(row["category"], 0) + 1
    print(f"  配置键         {len(payload['config'])} 个 —— "
          + "、".join(f"{g}×{n}" for g, n in sorted(groups.items())))
    print(f"  {onboarding.dim('               这是「引擎声明过什么」(静态),'
                              '不是「某个世界现在是什么」—— 后者问 config list')}")
    subscribable = payload["plugins"]["subscribable_events"]
    print(f"  可订事件       {', '.join(sorted(subscribable))}")
    plugins_seg = payload["plugins"]
    print(f"  插件           作者层 type `{plugins_seg['author_type']}`   "
          f"事实形状 {plugins_seg['fact_shapes']}   "
          f"效果 {plugins_seg['effects']}   "
          f"命名空间 {plugins_seg['namespace_syntax']}")
    print(f"  {onboarding.dim('               策展表不是全集 —— 内部事件不在上面;'
                              '进了就拿不掉,所以宁少勿多')}")
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
        print(f"  {onboarding.yellow(onboarding.WARN)} 定时轮次开着,但「一轮都没跑过」 —— "
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


def _run_since_seq(redis: Any, world_id: str) -> int | None:
    """这个世界这一趟是从哪条事件之后开始跑的 —— 读不到 / 没盖过就是 `None`。

    `None` 是**答不上来**,不是 0:0 会被读成"整条日志都算本次开机以来",
    而那恰好把这条命令变回它原来的样子,还多了一句听起来很确定的话。
    """
    from anima_world.redis_state import meta_rows
    from anima_world.scheduler import Scheduler

    try:
        raw = meta_rows(redis, world_id).get(Scheduler.RUN_SINCE_SEQ)
    except Exception:  # noqa: BLE001 - 读不到就是答不上来,别掀翻体检
        return None
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _report_engagements_kept(
    started: int, dropped: list[Any], *, finished: int = 0, gone: int = 0,
    run_since_seq: int | None = None, dropped_this_run: int | None = None,
) -> int:
    """起了头的长过程有几件真做完了 —— 返回"需要处理"的项数。

    为什么在 `doctor` 里,而且**为什么按事件数不按 `subsystem_health`**:
    `World.state()["runtime"]["subsystems"]["engagement_kept"]` 只记**本次开机
    以来**这个进程看见的那几件,而一个跑在容器里的世界每次重启都从零开始 ——
    看板上那盏灯于是永远是刚点亮的绿。事件日志不会重启。

    这一条是这个引擎里"照跑但给错东西"的标本:她起了个头(`duration` 的代价
    **当场付了**),排班在下一 tick 把她带走,于是那件事**永远做不完、代价也不退**。
    世界照跑、日志一行不错,作者要到发现"她那件事一次都没做完"才知道 ——
    而在这之前,一个开着 `anima-world run` 的人**没有任何办法问出这句话**
    (FOR-STUDIO 的判据:库里有而 CLI 上没有,对外面等于不存在)。

    ⚠️ **"做完了几件"从前是减出来的**(`started - len(dropped)`),而那个减法把
    三样东西一起算成了"做完":东西没了收不了尾的(`reason="gone"`,代价一样
    不退)、条件没过的(`finish_affordance` 判否)、以及**此刻还在做的**。
    于是一个刚起了三件长活的世界会被报成"做完了 3 件",一句听起来最像好消息、
    而恰好在自己要度量的那件事情上说反了的话。现在四样各数各的:做完的按
    收尾那条 `entity_interaction`(**只有它的 payload 带 `duration`**,是长过程
    收尾独有的记号),半路被带走的和收不了尾的各按 `entity_disengage` 的
    `reason` 分,剩下的才是在做。起头那一格**不数参与者**
    (`joint_role == "participant"`):一起做的一件事会给每个人各发一条
    `entity_engage`,照人头数的话三个人吃一顿饭会被数成三件事,而收尾只有一条。

    ⚠️ **数报的仍然是这个世界的一生,而退出码 2026-08-21 起按「本次开机以来」**
    (看板 D25,受托拍板)。两者分开是有意的:**账要全,判要新**。
    一个跑了半年的世界身上永远背着头三天那次事故 —— 从前那笔账让这条命令
    **出过一次就再也回不了绿**,而 `CLAUDE.md` 同时写着它能进 CI;
    **一条永远红的 CI 检查等于没有这条检查**,人只会把它 `|| true` 掉,
    于是真出事那天它照样是红的、照样没人看。

    ⚠️ **窗口从哪来:`:meta` 的 `run_since_seq`**(`Scheduler.RUN_SINCE_SEQ`)——
    世界这一趟第一次推动时钟时盖的戳。`doctor` 是另一个进程,进程内存里那盏
    `engagement_kept` 的灯它看不见。
    ⚠️ **戳不在的时候不许悄悄放行**:那意味着这个世界还没在这一版引擎上跑过一 tick
    (老世界、刚导入、只被只读门开过)。这时退出码**照旧按一生算**,并且把
    "我为什么答不出最近那一段"印出来 —— 把"问不出来"过成绿灯,正是这条命令
    最容易长出的下一个谎。

    ⚠️ **加不平的时候把它说出来,别 `max(0, …)` 抹平**(2026-08-20 补):
    四格各数各的之后,"还在做"是**减出来的**,而减出负数只有一个意思 —— 收尾的
    记录比起头的多,引擎自己的账错了。从前那个 `max(0, …)` 把负数压成 0,于是
    屏幕上是一行**绿勾**,说着"起了 1 件,做完 0 件,2 件收不了尾"这种算术上
    不可能的话。**一行绿勾说出不可能的话,比一行红字更贵 —— 看的人会信它。**
    加不平只改脸色和多说一句,**不进退出码**:退出码是"这个世界需要处理几项",
    而算不平是引擎的 bug 不是这个世界的毛病,混进去会让人去改自己的世界。

    ⚠️ **隔壁那一支的绿勾也说过不可能的话**(3.6.0 第六轮 2026-08-20 补):
    `gone > 0` 而 `dropped == 0` 时走的是 `elif not dropped:`,印**绿勾**,而同一行
    里明写着「N 件收不了尾(东西没了,代价一样不退)」。上一轮写下上面那句
    「一行绿勾说出不可能的话,比一行红字更贵」时,只治了 `max(0, …)` 那一支 ——
    **一条只在一个地方被执行的纪律,等于没有这条纪律。**

    **只改脸色,不改数**:`gone` 非零 → 黄 WARN,`return 0` 一个字不动。理由和
    上面那条同构但不同源 —— `gone` 算不算"这个世界需要处理的一项"是产品/运维的
    判断(看板 D25),而**颜色不改会让人信一行假话,退出码改了会让别人的 CI 变色**。

    ⚠️ **黄 `!` 现在有三支,读的人要分得开**(第七轮 2026-08-20 认账):加不平、
    被带走、收不了尾。FOR-STUDIO 上一版给的分法是「看有没有跟着那两行说明」——
    **分不开**,因为"被带走"那一支后面也跟着两行(`最近一件:…` / `做完 N / 被带走 M`)。
    **精确判据是那两行里的第一句**:有「这几个数加不平」= 引擎自己的账错了,请贴回来;
    没有它而有「最近一件:」= 排班在抢她的手(退 1);两样都没有 = 这个世界里真有一件
    收不了尾(退 0)。镜像那一份已照这条改(FOR-STUDIO「这一行还有第五种脸色」那一段)。
    """
    if not started:
        print(f"  {onboarding.dim('这个世界还没有起过要花时间的长过程(duration)')}")
        return 0
    settled = finished + len(dropped) + gone
    balance = started - settled
    in_flight = balance if balance > 0 else 0
    parts = [f"起了 {started} 件", f"做完 {finished} 件"]
    if dropped:
        parts.append(f"{len(dropped)} 件半路被带走(代价不退)")
    if gone:
        parts.append(f"{gone} 件收不了尾(东西没了,代价一样不退)")
    if in_flight:
        parts.append(f"{in_flight} 件还在做")
    line = "要花时间的长过程:" + ",".join(parts)
    if balance < 0:
        print(f"  {onboarding.yellow(onboarding.WARN)} {line}")
        print(f"      {onboarding.dim('这几个数加不平:起了 ' + str(started) + ' 件,收尾的记录却有 ' + str(settled) + ' 条')}")
        print(f"      {onboarding.dim('多出来的那 ' + str(settled - started) + ' 条是引擎自己的账错了,不是这个世界的毛病 —— 请把这一行报回引擎')}")
        if not dropped:
            return 0
    elif not dropped:
        # `gone` 非零时这一行里带着「N 件收不了尾」—— 绿勾配这句话是不可能的组合。
        # **只换脸色,数与退出码一个字不动**(见 docstring 末段)。
        if gone:
            print(f"  {onboarding.yellow(onboarding.WARN)} {line}")
        else:
            print(f"  {onboarding.green(onboarding.OK)} {line}")
        return 0
    else:
        print(f"  {onboarding.yellow(onboarding.WARN)} {line}")
    # **点名最近那一件是谁的哪一件。** 只说"3 件被带走"的话,人下一步无处可去。
    last = (dropped[-1].payload or {})
    who = dropped[-1].who or "?"
    print(f"      {onboarding.dim('最近一件:' + who + ' 的 ' + str(last.get('verb') or '?') + ' ' + str(last.get('target') or '?'))}")
    print(f"      {onboarding.dim('做完 ' + str(finished) + ' / 被带走 ' + str(len(dropped)) + ' —— 比例低就是排班在抢她的手,看 occupies 声明对不对')}")
    # **账要全,判要新。** 上面几行是这个世界的一生,退出码只看这一趟。
    if run_since_seq is None:
        print(f"      {onboarding.dim('这个世界还没在这一版引擎上跑过一 tick(:meta 里没有 run_since_seq)——')}")
        print(f"      {onboarding.dim('答不出「本次开机以来」那一段,所以退出码照一生算。跑起来之后再体检一次')}")
        return 1
    if not dropped_this_run:
        print(f"      {onboarding.dim('本次开机以来(事件 #' + str(run_since_seq) + ' 之后)一件都没被带走 —— 退出码不记这一笔')}")
        return 0
    print(f"      {onboarding.dim('其中 ' + str(dropped_this_run) + ' 件是本次开机以来的(事件 #' + str(run_since_seq) + ' 之后)—— 退出码记的是这一格')}")
    return 1


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
    # **一趟 replay 数完所有要数的东西。** 这条日志十几万条是常态(线上晚潮
    # 16 万),每加一个体检项就多走一遍的话,`doctor` 的代价随项数线性涨。
    joined: set[str] = set()
    engaged = 0
    finished = 0
    gone = 0
    dropped: list[Any] = []
    dropped_this_run = 0
    # **"本次开机以来"这句话只有世界自己答得出。** doctor 是另一个进程,进程内存里
    # 那盏 `engagement_kept` 的灯它看不见 —— 水位是世界这一趟推第一 tick 时盖的戳
    # (`Scheduler.RUN_SINCE_SEQ`)。**戳不在 ≠ 没问题**,见 `_report_engagements_kept`。
    run_since_seq = _run_since_seq(redis, world_id)
    plugin_events: dict[str, int] = {}
    for e in log.replay():
        payload = e.payload or {}
        # 插件发的事件叫 `<插件>.<type>` —— 顺着这趟 replay 数,零额外成本
        # (这条日志十几万条是常态,为它单开一趟就是按项数线性涨的代价)。
        if "." in e.type:
            plugin_events[e.type.split(".", 1)[0]] = \
                plugin_events.get(e.type.split(".", 1)[0], 0) + 1
        if e.type == "agent_join" and e.who:
            joined.add(e.who)
        elif e.type == "entity_engage":
            # 一起做的一件事一人一条,而收尾只有发起人那一条 —— 数人头会让
            # 分子分母不是同一个单位(见 `_report_engagements_kept` 的丑话)。
            if str(payload.get("joint_role") or "") != "participant":
                engaged += 1
        elif e.type == "entity_interaction" and "duration" in payload:
            # **长过程收尾独有的记号。** 一下子做完的那种交互不带 `duration`,
            # 所以这一格数的就是"真做完的长过程",不必再回头对 engage 那条。
            finished += 1
        elif e.type == "entity_disengage":
            reason = str(payload.get("reason") or "")
            if reason == "left":
                dropped.append(e)
                if run_since_seq is not None and int(e.seq) > run_since_seq:
                    dropped_this_run += 1
            else:
                gone += 1
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
                  f"「世界」里 —— 它属于这台机器,不属于这个世界")
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

    _report_plugins(redis, world_id, plugin_events)
    problems += _report_autonomy_chain(redis, world_id, store)
    problems += _report_engagements_kept(
        engaged, dropped, finished=finished, gone=gone,
        run_since_seq=run_since_seq, dropped_this_run=dropped_this_run)

    print()
    if problems:
        print(f"  {onboarding.yellow(str(problems) + ' 项需要处理')}(世界仍然能跑,只是会降级)\n")
        return 1
    where_arg = "" if world_id == _world_id_default() else f" --world-id {world_id}"
    print(f"  {onboarding.green('一切正常。')} anima-world start{where_arg}\n")
    return 0


def _report_plugins(redis: Any, world_id: str,
                    plugin_events: dict[str, int]) -> None:
    """插件这一层跑没跑 —— **二十行体检里从前一行都没有**(3.8.0,第二波 ⑤)。

    🔴 **它答的是"有没有发生过",不是"声明了几条"**:`plugin list` 早就报得出
    声明面的计数,而调度台那一趟撞的病(一条 `when` 恒为假的触发器)在声明面上
    **完全正常** —— 声明得好好的、装载顺序也对,就是一次没响。

    从**外面**看得见的证据只有两样,所以这里只报这两样:

    - **事实被写过没有**:量表那一行的 `updated_tick`。规律与触发器写一个数
      **不发事件**(连续变化不发事件是这一层的老纪律),所以日志里查不到它们 ——
      `updated_tick` 是唯一的凭据(`World.rule_stats()` 的 docstring 早就这么写)。
    - **它发的事件有几条**:`<插件>.<type>`,顺着 doctor 那趟 replay 数的。

    ⚠️ **`when` 为假几次、取不着人几次,从外面看不见** —— 那是进程内的读数
    (`World.trigger_stats()`)。**说出来而不是假装查过了**:这一节最后一行就是
    指路的那句话。
    ⚠️ **这一节不进退出码**:一个刚建好的世界什么都还没发生过,而
    「一条永远红的检查等于没有这条检查」。
    """
    from anima_world.plugins import stored_bodies
    from anima_world.redis_state import RedisPluginStore, RedisStockStore

    try:
        bodies = stored_bodies(RedisPluginStore(redis, world_id))
    except Exception:  # noqa: BLE001 - 体检不该因为一段读不动的声明而中断
        return
    if not bodies:
        return
    stocks = RedisStockStore(redis, world_id)
    # 从外面能看的那几个 owner:世界 + 名册(有界)。**东西身上的那些不扫** ——
    # 一个世界里可以有一万棵树,而体检不该按世界的大小收费。
    owners = ["world", *sorted(stocks.owners("agent"))]
    written: dict[str, int] = {}
    for owner in owners:
        for key, (_value, tick) in (stocks.snapshot(owner) or {}).items():
            if "." not in key or int(tick or 0) <= 0:
                continue
            written[key.split(".", 1)[0]] = written.get(key.split(".", 1)[0], 0) + 1
    for body in bodies:
        pid = str(body.get("id") or "")
        rules = len(body.get("rules") or [])
        triggers = len(body.get("triggers") or [])
        moved = written.get(pid, 0)
        fired = plugin_events.get(pid, 0)
        # ⚠️ **出厂那几个不喊** —— 它们装不装由世界配置决定,写法作者也改不动;
        # 一句他没处修的警告只会教他略过这一段(和 ④ 那条 lint 同一个理由)。
        quiet = (rules or triggers) and not moved and not fired \
            and pid not in FACTORY_PLUGINS
        mark = (onboarding.yellow(onboarding.WARN) if quiet
                else onboarding.green(onboarding.OK))
        print(f"  {mark} 插件 {pid} {body.get('version') or ''}:"
              f"规律 {rules} 条、触发器 {triggers} 条 —— "
              f"写过的事实 {moved} 个(角色与世界身上),发出的事件 {fired} 条")
        if quiet:
            print(f"      {onboarding.dim('声明得好好的,而它一次都没动过世界。')}"
                  f"{onboarding.dim('订的事件发生过吗?`when` 里的名字读得到吗?')}")
    print(f"      {onboarding.dim('「when 为假几次 / 取不着人几次」从外面看不见 ——')}"
          f"{onboarding.dim('那是进程内的读数:World.trigger_stats()')}")


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

    # 🔴 **和 `run` 走同一扇门**(3.9.0,验收 C 逮的)。从前这儿直接
    # `build_serve_scheduler`,而**让世界看得见在场玩家的那根线接在 `World` 里**
    # (`api.py` 的 `scheduler._present_players = self._present_roster`,全仓唯一一处)。
    # 于是 `simulate` 出来的世界 `_present_players is None` —— `co_located` 取不到人、
    # per-player 的剧情拍**一拍都不响**,而 `fast_forward` 照样跑完、rc=0、日志干净。
    # 作者拿 `--ticks 0` 当校验器、拿快进验第一周剧情,量到的是一片"没发生",
    # 而那正是这条命令最常见的两种用法(创作台这一单就撞上了)。
    # **两条路各有一半世界,是这个仓库最怕的那种坏法**,所以合成一条。
    from anima_world.api import World

    try:
        world = World.open(
            world_id,
            redis=redis,
            mysql=mysql,
            agents=args.agents,
            world_file=args.world_file,
            force_mock_llm=(tier == "mock"),
            mock_narrative=(tier == "planner"),
            beats_path=args.beats,
        )
    except (BeatScriptError, WorldSeedError) as exc:
        print(f"[simulate] {exc}", file=sys.stderr)
        return 2
    scheduler = world.scheduler

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
            # 分母是**这一趟真正在跑的那份脚本**(`--beats` 给的就是它给的)。
            # 没有 director = 这个世界这一趟一拍都没有 —— 那是一个**答案**,
            # 所以是 `[]` 不是 `None`;`None` 留给"我问不出来",而 simulate 刚跑完
            # 这一趟,它没有问不出来的道理。
            beats=(scheduler.beat_director.script.beats
                   if scheduler.beat_director is not None else []),
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
            # 白写的那几拍**也进这一行**:一次试炼正是作者会问"我的剧情响了吗"的
            # 那一刻,而报告写进文件之后没人保证他会去打开它。
            unfired = report["beats"].get("unfired") or []
            print(f"[simulate] report → {args.report}"
                  f"  ({report['events']['total']} 事件,"
                  f"{len(report['encounters'])} 对有过相遇"
                  + (f",{len(idle_only)} 人整场无事发生:{'、'.join(idle_only)}" if idle_only else "")
                  + (f",{len(unfired)} 拍一次都没响:{'、'.join(unfired)}" if unfired else "")
                  + ")")
    # `stop()` 上面按"读完日志再停"的次序调过了(幂等);这里收的是 `World` 那一半
    # ——桥循环与聊天服务,它们是 `World.open` 带来的,不收就把线程留给下一个进程。
    world.close()
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
    if external:
        # **把这一段自己的用途说出来。** 只写一句"报这个包指向哪儿"的话,读的人
        # 会当它是一栏统计;而它真正要说的是一句代价:**这些图不在包里** ——
        # 包发出去之后,下面列出的每一台服务器都得继续活着,少了哪一台也不会有
        # 任何一处报错,只是玩家那边少几张图。这句话是准备自建部署的人在装载之前
        # 唯一一次听得到它的机会。
        print(f"  {onboarding.dim('图 —— 这些图不在包里:发出去之后要靠下面这几台还活着')}")
        print(f"  {onboarding.dim('     (不联网探活,只报这个包指向哪儿)')}")
    else:
        print(f"  {onboarding.dim('图 —— 都内嵌在包里,不依赖任何一台服务器')}")
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
    from anima_world.world_file import StateScan, state_records_to_seed
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
        # 🆕 3.8.0(收件箱 D30):**`loadable` 这句话的主语。**
        #
        # 这一格治的不是 `loadable` 的值,是它的**主语从来没写出来**:这扇门答的
        # 是「我查过的那些没问题」,而它印出来的是「装得进去」。中间那段差,读它的
        # 脚本读不到 —— 它只读一个布尔。
        #
        # 三格都是**纯增量**,一格都不夺:
        # · `present_layers` 这份包里有哪些层(`author` / `redis` / `event` / `mysql`)
        # · `checked_layers` 这一趟真编译过哪些层
        # · `unchecked_layers` 两者之差 —— **非空 = `loadable` 没覆盖全**
        #
        # 消费方那条判据只有一行,抄进 FOR-STUDIO §3.45 了:
        #     真绿 = (rc == 0 and loadable is True and not unchecked_layers)
        # 减法由引擎做,不由消费方做:各自算减法就是各持一份对层名的猜测,
        # 和 `world drop` 归引擎是同一条理由。
        "present_layers": [],
        "checked_layers": [],
        "unchecked_layers": [],
        # 状态层里**没被编译过**的那些表(短键名)。上面那三格答"哪一层",
        # 这一格答"那一层里的哪几张表" —— 下一个 `importance` 会出现在其中一张上。
        "unchecked_state_tables": [],
    }
    unopenable = _cannot_even_look(args.package)
    state = StateScan()
    manifest_box: list = []
    authored, read_error = (
        ({}, None) if unopenable
        else _load_authored_layer(args.package, scan=scan, state=state,
                                  manifest_out=manifest_box)
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
        # 🆕 状态层那几张会被编译的表(收件箱 D30)。**放在作者层之外单算**:
        # 一个跑过的世界导出来两者只有一半在,而混装包两半都在。
        errors += _state_layer_ontology_errors(state_records_to_seed(state.rows))
        payload.update(_coverage_fields(state, authored=bool(authored)))
        if not authored:
            # ⚠️ 强调用「」不用 `**`:这句话会**原样印在终端上**
            # (`_print_check_human` 逐条打 warnings),而屏幕上 `**` 就是两个星号。
            # `test_屏幕上不许出现裸markdown星号` 看不见这一条 —— 它只扫 `print()`
            # 的实参与 `help=`,而这句话是先攒进列表再由别处印的,那正是它自己
            # 写明的盲区。**它守不住的地方要靠手,这一条就是。**
            warnings.append(
                "这份文件没有作者层(只有状态记录)—— 它是一个跑过的世界导出来的,"
                "装载时直接落键、不走作者层那几道闸。「状态层里开机会编译的那几张表"
                "(种类 / 实例 / 规律 / 地点 / 物品)这一趟已经查过了」;"
                # ⚠️ **人话面不印 Python 的 list repr,也不指一个这一面没有的 JSON
                # 键名**(2026-08-27 C 视角验收):`['agent', 'bt_actions', …]` 那串
                # 引号与方括号是给机器看的,而"见 unchecked_state_tables"这句话
                # 指向的那一格**只在 `--json` 里存在** —— 让人去查一个他这一屏上
                # 根本没有的字段,等于没告诉他。
                + (f"没查过的还有:{'、'.join(state.unchecked_tables())}"
                   f"(这几张表开机不编译,读不懂它们不会让世界开不了机;"
                   f"完整清单在 --json 的 unchecked_state_tables 一格)"
                   if state.unchecked_tables() else "状态层里没有别的表了")
            )
        else:
            warnings += (
                world_seed_warnings(authored)
                + _authored_drift_warnings(authored)
                + _authored_unreachable_requirements(authored)
                + _authored_media_warnings(authored)
                + _authored_dropped_quantities(authored)
                + _authored_uncreatable_edges(authored)
                + _authored_edge_warnings(authored)
            )
            errors += _authored_ontology_errors(authored, edit=payload["edit"])
            if payload["edit"]:
                warnings.append(
                    "这是一次编辑(--edit),「跨引用」没查:规律指向哪个种类 / 哪个"
                    "实例、实例在哪个地点、能力里的物品、`spawn` 生的是哪个种类 ——"
                    "这几样可以来自目标世界。包自己肚子里那几件「已经查过了」:"
                    "量名两支都查(`set:`/`costs:` 里读写的,以及 `stocks:` 里"
                    "写初值的)、动词的 label、`spawn` 有没有代价、不认识的字段"
                )
                # 🆕 3.10.0:那五段。**句子和开机那条路共用一份常量**
                # (`EDIT_PATH_NOTES`),`validate world` 那一侧也是同一份 ——
                # 三扇门对同一份文件说同一句话,这条纪律在措辞上也成立。
                if authored.get("beats"):
                    warnings.append(
                        f"这份文件带着 {len(authored['beats'])} 拍剧情。"
                        + EDIT_PATH_NOTES["beats"]
                        + "(目标世界有哪几拍,离线这一格答不出来 —— 开机是"
                          "「逐拍比」:同 id 且内容相同的静默跳过,同 id 改过的"
                          "说一句而照常开机,「新增」的才当场拒绝、退出码 2)"
                    )
                warnings += edit_path_silent_notes(authored)
                # 🆕 3.10.0(2a-① 验收 C):封皮和内容对不对得上 —— **三扇门同一句**。
                # 一份 `engine_min: "3.9.0"` 而带着 `pack` 段的包,从前这两扇门
                # 说"可用"、`pack install` 退 0 —— 而它在 3.9.0 上是开不了机的硬失败。
                _mf = manifest_box[0] if manifest_box else None
                errors += pack_engine_min_errors(authored, _mf)
                warnings += pack_engine_min_warnings(authored, _mf)
                warnings += _edit_ontology_gap_warnings(authored)
                warnings += _edit_stock_kind_gap_warnings(authored)
                warnings += _edit_dropped_quantity_gap_warnings(authored)
                warnings += _edit_location_media_warnings(authored)
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
    # `setting` 和 `check` 一样走在下面那个 try/except 之前:它一个字节的 `.cyberworld`
    # 都不碰(读写的是一个**活着的**世界),而那个 except 把一切拒绝报成 2 并印
    # `[world setting] …` 之外的前缀 —— 它的拒绝理由自己已经说得更清楚。
    if args.world_command == "setting":
        return run_world_setting(args)

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
            # 🆕 `receipt` 那一格(收件箱 D32):混装两层的包走这条路会**丢掉作者层
            # 那一半**,而那件事此前只写在 `logger.warning` 上 —— 机器读的是这份
            # JSON 和退出码,读不到日志。纯增量,一格不夺。
            receipt: dict[str, Any] = {}
            manifest = import_world_file(
                args.package, redis=redis, world_id=world_id, mysql=mysql,
                receipt=receipt,
            )
            result = {
                "operation": "import",
                "world_id": world_id,
                "from": manifest.world_id,
                "engine_min": manifest.engine_min,
                **receipt,
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

      anima-world doctor       {onboarding.dim('体检:密钥、LLM、时钟、长过程有没有做完')}
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
        # `chat --message --json` 那条路上 stdout 只许有那一份 JSON,散场那一行
        # 走 stderr(**仍然印**,见 `_engine_logs_out_of_the_way` 的 `to_stderr`)。
        with _engine_logs_out_of_the_way(
            getattr(args, "verbose", False),
            to_stderr=bool(getattr(args, "messages", None))
            and bool(getattr(args, "as_json", False)),
        ):
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
    if args.command == "location":
        return run_location(args)
    if args.command == "player":
        return run_player(args)
    if args.command == "plugin":
        return run_plugin(args)
    if args.command == "pack":
        return run_pack(args)
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


# 这几个检查器都定义完了,把表填上。**放在模块尾**是因为它引用的是函数对象。
_register_authored_layer_checks()

if __name__ == "__main__":
    sys.exit(main())
