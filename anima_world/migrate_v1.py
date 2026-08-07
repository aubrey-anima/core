"""把 1.x 的 `world.db` 迁成 2.0 的世界文件(v3)。

## 为什么需要它,以及为什么它长这样

2.0 把 SQLite 整个退役了:`anima_world.db` 没有了,引擎再也读不了一个 `world.db`。
而 1.x 的导出包是 ZIP,2.0 的读者只认 gzip JSONL —— 拿一个 v1 包喂它,第一行就
不是合法 JSON。**于是一个跑了两周的 1.x 世界,在 2.0 面前没有任何入口。**

这个模块是那道桥,而且**只是一道桥**:

- **它不 import 任何引擎的存储层。** 用裸 `sqlite3` 读那 26 张表 —— 1.x 的 schema
  已经冻死了(那一代不会再有新表),照着它写一份读法比留一个 SQLite 依赖干净。
  留依赖的话,"引擎不碰 SQLite 了"这句话就是假的,而下一个人会顺手用它。
- **它不写 Redis。** 产物是一份普通的 `.cyberworld`,走的是和别的世界文件**同一条
  装载路径**。写第二条落库路径就是给自己造第二种"装了一半"的坏法,而那一条不会
  有任何测试覆盖 —— 迁移天然是一次性的,一次性的代码最不该自己造机制。

## 一条硬纪律:不认识的表要报错

1.x 有 26 张表。少迁一张的下场不是报错,是**这个世界安静地少了一层** —— 比如
`agent_stance` 丢了,她对每个人的关系性意图归零,而世界照跑、日志干净。所以这里
的表清单是**闭集**:遇到清单外的表就抛错,让人去看一眼那是什么,而不是跳过。

## 角色的黑板不在这里迁

1.x 没有黑板表 —— 角色是从事件日志里的 `agent_join` 重放出来的,黑板在开机时
按名册填。2.0 的开机路径同样如此(`build_serve_scheduler` 尾部,**只填缺不覆盖**),
所以迁移只要把事件带过去,首启时黑板自己会长出来。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from anima_world.world_file import WorldFileManifest, write_world_file

__all__ = ["migrate_world_db", "MigrationError", "LEGACY_TABLES", "DROPPED_TABLES"]

NUL = "\x00"


class MigrationError(RuntimeError):
    """这个 `world.db` 迁不了 —— 缺表、多表、或者根本不是一个世界库。"""


def _rows(db: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]
    except sqlite3.Error:
        return []


def _json(value: Any, default: Any = None) -> Any:
    """1.x 把结构化字段存成 TEXT。取出来要还原,不然它在 2.0 里是一个字符串。"""
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ── 表 → 键 的映射 ──────────────────────────────────────────────────────────
#
# 每一项:1.x 表名 → (2.0 的键, 怎么把一行变成 hash 的 field/value)。
# **这是一个闭集**,见文件头。

def _config_row(row: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """配置行 —— **`is_secret` 的一律不迁**,返回 None 表示丢掉这一行。

    ⚠️ 这道闸我先漏了,而漏的方式很典型:引擎自己的导出路径有
    `world_package._strip_secrets`,而迁移**直读 SQLite,绕过了它**。于是 1.x 那行
    Fernet 密文 `llm.api_key` 原样进了 `.cyberworld` —— 而那个文件是**分发物**。
    功能上看不出来(2.0 手里没有 Fernet 钥匙,读的时候会点名跳过),所以它能一直躺着。

    两条理由,任何一条都够:
      - 包是分发物,带着作者的钥匙发出去**不可挽回**;
      - 那段密文对 2.0 毫无用处 —— Fernet 随 SQLite 一起退役了,钥匙在
        `world.db.key` 里,而世界文件里不该有钥匙这种东西。

    钥匙的去处是**机器配置**(`~/.anima-world/config.json`,0600),迁移的调用方
    自己搬 —— 那一步要解密,而解密要 keyfile,那是一次**人在场**的操作。
    """
    if row.get("is_secret"):
        return None
    return (str(row["key"]), row)


def _hash_by(field: str):
    """field 取一列,value 是整行。绝大多数表是这个形状。"""
    def build(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return (str(row[field]), row)
    return build


def _hash_by_pair(a: str, b: str):
    """复合主键的表:2.0 用 NUL 拼成一个 field(和引擎侧逐字相同)。"""
    def build(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return (f"{row[a]}{NUL}{row[b]}", row)
    return build


def _world_rules(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    # `definition` 在 1.x 是 TEXT,2.0 里是一个对象 —— 不还原的话规律层
    # 拿到一个字符串,整个世界开不了机(坏规律是整体拒绝)。
    return (str(row["id"]), {**row, "definition": _json(row.get("definition"), {})})


def _bt_node(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return (f"{row['tree']}{NUL}{row['node_id']}", {**row, "params": _json(row.get("params"), {})})


def _bt_action(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return (str(row["node_id"]), {**row, "params": _json(row.get("params"), {})})


def _needs(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """1.x 一个 (agent, need) 一行;2.0 一个角色一行,三个需求在同一个对象里。"""
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        agent = str(row["agent_id"])
        slot = merged.setdefault(agent, {})
        slot[str(row["need"])] = float(row["value"])
        slot["updated_tick"] = max(int(row.get("updated_tick") or 0), int(slot.get("updated_tick") or 0))
    return merged


def _reflection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        str(row["agent_id"]): {
            "accumulated": float(row.get("accumulated_importance") or 0.0),
            "last_tick": int(row.get("last_reflection_tick") or 0),
        }
        for row in rows
    }


def _stocks(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """量按 owner 分成一个个 `stock:{owner}` hash,值是 `[值, tick]`。

    顺带攒出 `stock_owners` 那个 set —— 少了它,"这个世界里有量的东西有哪些"
    问不出来,而每个量本身还在:规律照跑,只是没有任何一处枚举得到它们。
    """
    by_owner: dict[str, dict[str, Any]] = {}
    for row in rows:
        owner = str(row["owner"])
        by_owner.setdefault(owner, {})[str(row["key"])] = [
            float(row.get("value") or 0.0),
            int(row.get("updated_tick") or 0),
        ]
    return by_owner, sorted(by_owner)


# 表名 → (键, 行→(field, value));None 表示这张表另有去处或有意丢弃。
LEGACY_TABLES: dict[str, Any] = {
    # —— 直接映射成一个 hash ——
    "locations": ("locations", _hash_by("id")),
    "item_defs": ("item_defs", _hash_by("id")),
    "stock_places": ("stock_places", _hash_by("owner")),
    "prompt_templates": ("prompts", _hash_by("name")),
    "config": ("config", _config_row),
    "world_rules": ("world_rules", _world_rules),
    "bt_nodes": ("bt_nodes", _bt_node),
    "bt_actions": ("bt_actions", _bt_action),
    "shop_stock": ("shop_stock", _hash_by_pair("location_id", "item_id")),
    "stock_visibility": ("visibility", _hash_by_pair("owner_kind", "key")),
    "agent_stance": ("stance", _hash_by_pair("agent_id", "target_id")),
    "agent_mutes": ("mutes", _hash_by_pair("agent_id", "player_id")),
    "agent_refused_topics": ("refused_topics", _hash_by_pair("agent_id", "keyword")),
    "persona_overrides": ("overrides", _hash_by_pair("agent_id", "player_id")),
    "agent_followups": ("followups", _hash_by("id")),
    "edges": ("edges", _hash_by("id")),
    "cliques": ("cliques", _hash_by("id")),
    # —— 要合并/重整的 ——
    "agent_needs": ("needs", None),
    "reflection_state": ("reflection", None),
    "stocks": ("stock:*", None),
    # —— 不进 redis 记录的 ——
    "events": (None, None),          # → event 记录
    "memories": (None, None),        # → mysql 记录(装载时按后端路由)
    "conversations": (None, None),   # 同上
    "messages": (None, None),        # 同上
    # —— 另有去处 ——
    # `db_meta` 不是整张丢:**时钟的权威在它里面**(见下面那段)。
    # 丢掉的只有 `format_version` / `schema_revision` —— db 格式联锁随 world.db
    # 一起退役了,2.0 的"格式"是键前缀。
    "db_meta": (None, None),
    "sqlite_sequence": (None, None),
}

# **有意丢弃的表,以及为什么。**
#
# 和"没登记"分开放,因为那是两件事:没登记要**报错**(可能是漏了一层世界),
# 登记了不要要**报出来给人看一眼**(可能那正是他要的东西)。一次迁移里
# "补了什么"和"扔了什么"是使用者唯一有机会说"等等那个我要"的时刻 ——
# 过了这一刻,就再也没人会去问了。
DROPPED_TABLES: dict[str, str] = {
    "world_command_receipts":
        "运维台旧版服务壳的投递回执 —— 平台的账,不是世界的内容。"
        "世界的历史只记世界里发生的事;新版的壳写自己的 internal-receipts.db",
    "world_chat_evolution_receipts":
        "同上:聊天演化的投递回执,归平台",
    "config.secret":
        "1.x 的加密配置行(Fernet 密文)。包是**分发物**,带着作者的钥匙发出去不可挽回;"
        "而那段密文对 2.0 也毫无用处 —— Fernet 随 SQLite 一起退役了。"
        "钥匙的去处是机器配置(~/.anima-world/config.json),那一步要 keyfile,人得在场",
}


def migrate_world_db(
    db_path: str | Path,
    *,
    world_id: str,
    name: str = "",
    summary: str = "",
    engine_min: str = "",
    gaps: list[int] | None = None,
    dropped: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """读一个 1.x 的 `world.db`,产出 v3 记录流。**纯读,不碰引擎的存储层。**

    `gaps` / `dropped` 给容器进来的话,补掉的空号与有意丢掉的表会写进去 ——
    两样都要报给人看:一次迁移里"补了什么"和"扔了什么"是使用者唯一有机会
    质疑的地方,而过了这一刻就再也没人会去问了。
    """
    self_gaps = gaps if gaps is not None else []
    self_dropped = dropped if dropped is not None else {}
    path = Path(db_path)
    if not path.exists():
        raise MigrationError(f"没有这个文件:{path}")

    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if "events" not in present or "locations" not in present:
            raise MigrationError(
                f"{path} 看上去不是一个 1.x 的世界库(没有 events / locations 表)"
            )
        unknown = present - set(LEGACY_TABLES) - set(DROPPED_TABLES)
        if unknown:
            # 跳过等于让这个世界安静地少一层。
            raise MigrationError(
                f"不认识的表 {sorted(unknown)} —— 迁移的表清单是闭集,"
                f"加一张新表要同时在 LEGACY_TABLES 里登记,不然它会静默消失"
            )

        for table in sorted(present & set(DROPPED_TABLES)):
            count = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count:
                self_dropped[table] = int(count)

        # ── 状态层:一个个 hash ────────────────────────────────────────────
        for table, (key, build) in LEGACY_TABLES.items():
            if key in (None, "stock:*") or build is None or table not in present:
                continue
            rows = _rows(db, table)
            if not rows:
                continue
            value: dict[str, str] = {}
            for row in rows:
                built = build(row)
                if built is None:      # 有意丢掉这一行(目前只有 is_secret)
                    self_dropped[f"{table}.secret"] = self_dropped.get(f"{table}.secret", 0) + 1
                    continue
                field, body = built
                value[field] = json.dumps(body, ensure_ascii=False)
            yield {"kind": "redis", "key": key, "type": "hash", "value": value}

        # 需求:一个角色一行,不是一个 (角色, 需求) 一行
        needs = _needs(_rows(db, "agent_needs"))
        if needs:
            yield {
                "kind": "redis", "key": "needs", "type": "hash",
                "value": {k: json.dumps(v, ensure_ascii=False) for k, v in needs.items()},
            }

        reflection = _reflection(_rows(db, "reflection_state"))
        if reflection:
            yield {
                "kind": "redis", "key": "reflection", "type": "hash",
                "value": {k: json.dumps(v, ensure_ascii=False) for k, v in reflection.items()},
            }

        # 量:按 owner 拆成一个个 `stock:{owner}`,外加 `stock_owners` 那个 set
        by_owner, owners = _stocks(_rows(db, "stocks"))
        for owner, values in by_owner.items():
            yield {
                "kind": "redis", "key": f"stock:{owner}", "type": "hash",
                "value": {k: json.dumps(v, ensure_ascii=False) for k, v in values.items()},
            }
        if owners:
            yield {"kind": "redis", "key": "stock_owners", "type": "set", "value": owners}

        # 时钟:**权威在 `db_meta.clock`**,不是 `MAX(events.ts)`。
        #
        # ⚠️ 这一条我先写错过,而错法很典型:拿 `MAX(ts)` 当时钟,世界照样开得起来 ——
        # 只是她以为现在是**第 6,200,765 天**。原因是 1.x 的事件里混着一行
        # `ts=1785820326`(一个 unix 时间戳,那一代自己的脏数据),而 tick 的正常范围
        # 是 0~3853。一个坏行就能把整个世界的时间带飞,而没有任何地方会报错。
        #
        # 不带时钟的话世界从 0 开始跑,她的记忆里全是"还没发生"的事 —— 所以这一栏
        # 缺了要**当场拒**,不是默默从 0 开始。
        meta = {row["key"]: row["value"] for row in _rows(db, "db_meta")}
        clock = meta.get("clock")
        if clock is None:
            raise MigrationError(
                "db_meta 里没有 clock —— 迁过去的世界会从第 0 天开始跑,"
                "而她的记忆里全是还没发生的事"
            )
        yield {"kind": "redis", "key": "clock", "type": "string", "value": str(int(clock))}

        # ── 事件层 ────────────────────────────────────────────────────────
        #
        # ⚠️ **1.x 的 seq 会有洞,而 2.0 不允许。** 1.x 用 AUTOINCREMENT,事务回滚
        # 会把号消耗掉 —— 那些号**从来没有对应过任何事件**。而 2.0 的 seq 是
        # "1 起的连续整数",Redis 列表的下标就是它,投影、分页、`since_seq` 全建立
        # 在这条上,所以洞在结构上不允许。
        #
        # 两条路,选了填洞:
        #   - **重新编号**会静默改掉 `memories.event_seq` 与 `edges.source_event_seq`
        #     的指向(实测一个真世界里有 356 条记忆引用它)—— 那条记忆从此指向
        #     另一件事,而没有任何地方会报错。
        #   - **填一个占位事件**保住原有编号。它的类型不在投影的分派表里(那是一条
        #     没有 else 的 if/elif 链),所以重放时是惰性的;而它**留在日志里说明
        #     这里曾经有个空号**,不是假装无事发生。
        #
        # 填了几个必须报出去(`_gap_count`),不能安静地补完就算。
        rows = _rows(db, "events")
        expected = 1
        for row in rows:
            seq = int(row["seq"])
            while expected < seq:
                self_gaps.append(expected)
                yield {
                    "kind": "event", "seq": expected, "ts": int(row["ts"]),
                    "type": "legacy_seq_gap", "who": None, "loc": None,
                    "payload": {
                        "note": "1.x 的 AUTOINCREMENT 空号(事务回滚),这里从来没有过事件",
                        "migrated_from": "world.db",
                    },
                }
                expected += 1
            yield {
                "kind": "event",
                "seq": seq,
                "ts": int(row["ts"]),
                "type": str(row["type"]),
                "who": row.get("who"),
                "loc": row.get("loc"),
                "payload": _json(row.get("payload"), {}),
            }
            expected = seq + 1

        # ── 会无限增长的那三样 ────────────────────────────────────────────
        # 发成 `mysql` 记录**不看后端**:装载时引擎按有没有 MySQL 自己路由。
        # 这也是它们在 2.0 里被 dump 出来的样子,所以迁移和导出走的是同一条路。
        for table, jsonish in (
            ("memories", ("source_ids",)),
            ("conversations", ("participants",)),
            ("messages", ("tool_calls",)),
        ):
            for row in _rows(db, table):
                body = dict(row)
                for column in jsonish:
                    if column in body:
                        body[column] = _json(body[column])
                yield {"kind": "mysql", "table": table, "row": body}
    finally:
        db.close()


def write_migrated_world(
    db_path: str | Path,
    output: str | Path,
    *,
    world_id: str,
    name: str = "",
    summary: str = "",
    engine_min: str = "",
) -> dict[str, int]:
    """迁一个 `world.db` 到 `output`,回一份逐类计数(给人对账用)。"""
    import anima_world

    counts: dict[str, int] = {}
    gaps: list[int] = []
    dropped: dict[str, int] = {}

    def counted() -> Iterator[dict[str, Any]]:
        for record in migrate_world_db(
            db_path, world_id=world_id, name=name, summary=summary,
            gaps=gaps, dropped=dropped,
        ):
            counts[record["kind"]] = counts.get(record["kind"], 0) + 1
            yield record

    manifest = WorldFileManifest(
        world_id=world_id,
        name=name or world_id,
        summary=summary,
        engine_min=engine_min or anima_world.__version__,
        source_engine_version=anima_world.__version__,
    )
    write_world_file(output, manifest, counted())
    # 补了几个空号要报出去 —— 补完不吭声的话,"这个世界的历史完整吗"就没人问得到了。
    counts["seq_gaps_filled"] = len(gaps)
    counts["dropped"] = dropped
    return counts
