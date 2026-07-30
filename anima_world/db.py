"""SQLite WAL persistence layer."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq    INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     INTEGER NOT NULL,
  type   TEXT NOT NULL,
  who    TEXT,
  loc    TEXT,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
"""

MEMORIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id     TEXT NOT NULL,
  tick         INTEGER NOT NULL,
  kind         TEXT NOT NULL,
  summary      TEXT NOT NULL,
  importance   REAL NOT NULL,
  anchor       INTEGER NOT NULL DEFAULT 0,
  event_seq    INTEGER,
  created_at   INTEGER NOT NULL,
  strength     REAL NOT NULL DEFAULT 1.0,
  last_access  INTEGER,
  access_count INTEGER NOT NULL DEFAULT 0,
  source_ids   TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_agent_id ON memories(agent_id);
"""

# memory-2.0: reflection trigger watermark — accumulated importance since the
# agent's last reflection. Current-value data (data-plane), not history: the
# reflections themselves are memory_seed events and replay fine without this.
REFLECTION_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS reflection_state (
  agent_id TEXT PRIMARY KEY,
  accumulated_importance REAL NOT NULL DEFAULT 0,
  last_reflection_tick INTEGER NOT NULL DEFAULT 0
);
"""

EDGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  subject          TEXT NOT NULL,
  predicate        TEXT NOT NULL,
  object           TEXT NOT NULL,
  source_event_seq INTEGER,
  created_at       INTEGER NOT NULL,
  UNIQUE(subject, predicate, object)
);
CREATE INDEX IF NOT EXISTS idx_edges_subject ON edges(subject);
CREATE INDEX IF NOT EXISTS idx_edges_predicate ON edges(predicate);
CREATE INDEX IF NOT EXISTS idx_edges_object ON edges(object);
"""

# M3.5: standalone chat subsystem — conversations + messages live here,
# decoupled from the event log. A closed conversation emits one summary event.
CONVERSATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id         TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'open',
  started_at       INTEGER NOT NULL,
  last_activity_at INTEGER NOT NULL,
  closed_at        INTEGER,
  summary          TEXT,
  message_count    INTEGER NOT NULL DEFAULT 0,
  participants     TEXT,
  location         TEXT,
  player_id        TEXT
);
CREATE INDEX IF NOT EXISTS idx_conversations_agent  ON conversations(agent_id);
CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);
"""

MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL,
  role            TEXT NOT NULL,
  content         TEXT NOT NULL,
  created_at      INTEGER NOT NULL,
  stance          TEXT,
  intent          TEXT,
  intent_confidence REAL,
  tool_calls      TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
"""

# chat-agent(1.3.0):一轮聊天的"此刻"状态。全部是当前值(data-plane),不是历史
# —— 后果(走开、广播、付钱)照旧走事件日志,这些表只回答"她现在对谁是什么姿态、
# 谁被她静音到几点、她拒绝谈什么"。
#
# `agent_stance` 是 (agent, target) 的属性而不是 agent 全局属性:她可以同时对你
# 找茬、对别人讨好。惯性要读上一轮的值,所以它必须落库 —— 进程重启后"她刚才在
# 试探你"这件事不该凭空消失。
AGENT_STANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_stance (
  agent_id     TEXT NOT NULL,
  target_id    TEXT NOT NULL,
  stance       TEXT NOT NULL,
  declared     INTEGER NOT NULL DEFAULT 1,
  updated_tick INTEGER NOT NULL DEFAULT 0,
  updated_at   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent_id, target_id)
);
"""

# 软静音与"等会儿再说"。`kind` 分开两者:mute 是"我不想理你",delay 是"等我一下"
# —— 后者到点会真的回来找你(见 agent_followups),前者不会。
AGENT_MUTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_mutes (
  agent_id   TEXT NOT NULL,
  player_id  TEXT NOT NULL,
  kind       TEXT NOT NULL DEFAULT 'mute' CHECK (kind IN ('mute','delay')),
  expires_at INTEGER NOT NULL,
  reason     TEXT,
  created_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent_id, player_id, kind)
);
"""

AGENT_REFUSED_TOPICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_refused_topics (
  agent_id   TEXT NOT NULL,
  keyword    TEXT NOT NULL,
  expires_at INTEGER,
  created_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent_id, keyword)
);
"""

# "等会儿再说"的兑现表:到了 due_tick,世界让她回来敲一次门(agent_hail)。
# 没有这张表,delay_reply 就只是一句被记录下来的托辞 —— 声明过,没人读。
AGENT_FOLLOWUPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_followups (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id   TEXT NOT NULL,
  player_id  TEXT NOT NULL,
  due_tick   INTEGER NOT NULL,
  kind       TEXT NOT NULL DEFAULT 'delayed_reply',
  reason     TEXT,
  created_tick INTEGER NOT NULL DEFAULT 0,
  fired_tick INTEGER
);
CREATE INDEX IF NOT EXISTS idx_followups_due ON agent_followups(due_tick, fired_tick);
"""

# 玩家教给角色的对话规则,**按 (角色, 玩家) 永久生效**:一次教会,跨会话跨天
# 不忘。per-conversation 会让"以后叫我霜霜"应一两轮就忘 —— 那正是要修的。
PERSONA_OVERRIDES_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_overrides (
  agent_id   TEXT NOT NULL,
  player_id  TEXT NOT NULL,
  kind       TEXT NOT NULL,
  value      TEXT NOT NULL,
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent_id, player_id, kind)
);
"""

# M5: runtime config + prompt templates — typed scalars and named text
# blocks that used to be hardcoded, now hot-editable via the web API.
CONFIG_SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,
  value_type  TEXT NOT NULL CHECK (value_type IN ('str','int','float','bool')),
  category    TEXT NOT NULL,
  is_secret   INTEGER NOT NULL DEFAULT 0,
  description TEXT,
  updated_at  TEXT NOT NULL
);
"""

PROMPT_TEMPLATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_templates (
  name        TEXT PRIMARY KEY,
  template    TEXT NOT NULL,
  description TEXT,
  updated_at  TEXT NOT NULL
);
"""


# World definition data (data-plane principle: definition/current-value data
# lives in tables, the event log stays the history). `locations` is the single
# source of truth for the whole map — an adjacency tree: `parent` self-refs
# another row, `kind` splits regions (which may nest regions/points) from
# points (which agents stand on). Geometry is relative to the parent region,
# normalized 0~1: points carry x/y, regions x/y (top-left) + w/h. The map is
# configuration, not history, so none of this goes through the event log
# (nested-map D7). bt_actions is the DB-backed action table (node_id →
# kind/params); bt_nodes stores behavior tree shape, one row per node,
# children linked by `parent` and ordered by `sort`.
LOCATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL DEFAULT 'point' CHECK (kind IN ('region','point')),
  parent      TEXT,
  x           REAL,
  y           REAL,
  w           REAL,
  h           REAL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_locations_parent ON locations(parent);
"""

# The flat 6×6 grid the legacy `locations` table was built around.
_LEGACY_GRID_SIZE = 6

# needs-v3: current need levels (data-plane). The curves are pure math over
# ticks — the event log never carries them; this table only checkpoints the
# latest values (day rollover / shutdown) so a restart resumes mid-curve.
AGENT_NEEDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_needs (
  agent_id     TEXT NOT NULL,
  need         TEXT NOT NULL CHECK (need IN ('energy','hunger','social')),
  value        REAL NOT NULL DEFAULT 1.0,
  updated_tick INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent_id, need)
);
"""

# economy-v4: item definitions are authored data; shop_stock is the live
# shelf (data-plane). Balances and inventories are NOT tables — they are
# projections of payment/item_transfer/item_consume events (audit = replay).
ITEM_DEFS_SCHEMA = """
CREATE TABLE IF NOT EXISTS item_defs (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('consumable','durable','artwork')),
  base_price REAL NOT NULL DEFAULT 0,
  restores   TEXT
);
"""

SHOP_STOCK_SCHEMA = """
CREATE TABLE IF NOT EXISTS shop_stock (
  location_id TEXT NOT NULL,
  item_id     TEXT NOT NULL,
  quantity    INTEGER NOT NULL DEFAULT 0,
  price       REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (location_id, item_id)
);
"""

# social-v5: derived cache of friendship connected components. Recomputed at
# every day rollover — the table exists only to avoid recomputing on reads.
CLIQUES_SCHEMA = """
CREATE TABLE IF NOT EXISTS cliques (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  member_ids    TEXT NOT NULL,
  computed_tick INTEGER NOT NULL DEFAULT 0
);
"""

BT_ACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bt_actions (
  node_id    TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  params     TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
"""

# `time_window` joined the node types in bt-duties (a duty like "08:00–18:00"
# is not expressible as an equality Condition). One tree per agent lives here,
# keyed by the long-standing `tree` column.
BT_NODE_TYPES = ("selector", "sequence", "condition", "action", "time_window", "plan", "need_action")

BT_NODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS bt_nodes (
  tree    TEXT NOT NULL,
  node_id TEXT NOT NULL,
  type    TEXT NOT NULL CHECK (type IN ('selector','sequence','condition','action','time_window','plan','need_action')),
  parent  TEXT,
  sort    INTEGER NOT NULL DEFAULT 0,
  params  TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (tree, node_id)
);
CREATE INDEX IF NOT EXISTS idx_bt_nodes_tree ON bt_nodes(tree);
"""


# Live-database format version = **可挂载性**。凡是让老引擎读不了这个文件的改动
# (改列义、拆表、换单位)都得升它;升它就得升主版本号,而主版本号一升,旧世界
# 就打不开了。所以它只在真的读不了时才动。
# 戳跟着 world.db 走(因此也跟着 .cyberworld 包和卷拷贝走)。引擎绝不静默写进
# 一个它读不懂的格式。
DB_FORMAT_VERSION = 1
MIN_SUPPORTED_DB_FORMAT = 1

# 加法修订(1.3.0 起)。**纯加法**的 schema 变化 —— 新表、新的可空列 —— 不改
# 可挂载性:老引擎照样能开这个文件,只是不读那些新表,于是新能力缺席。这种改动
# 跟着**次版本号**走,不再逼一次主版本号跳跃(那会把已有的世界全作废)。
#
#   1 = 1.0.0 ~ 1.2.x
#   2 = 1.3.0:chat-agent 的六张表 + messages 的四个新列
#
# 这个戳存在的唯一理由是**让降级看得见**:一个 1.3 的世界跑在 1.2 引擎上照样跑,
# 但 stance / 静音 / 拒谈话题一概不生效 —— 那正是"照跑但给错东西"。戳让工具能
# 当场说出"这个文件是修订 2 写的,你这个引擎只到 1"。只增不减:低修订的引擎开过
# 高修订的库,不许把戳改小。
SCHEMA_REVISION = 2


class DBFormatError(RuntimeError):
    """The database format is outside this engine's supported range."""


def read_db_format(conn: sqlite3.Connection) -> int | None:
    """Return the db's format stamp, or None for a fresh/pre-stamp database."""
    try:
        row = conn.execute(
            "SELECT value FROM db_meta WHERE key='format_version'"
        ).fetchone()
    except sqlite3.OperationalError:  # no db_meta table yet
        return None
    return int(row[0]) if row else None


def _check_and_stamp_format(conn: sqlite3.Connection) -> None:
    """Refuse unsupported formats BEFORE any schema write, then stamp current.

    Missing stamp = fresh or pre-stamp legacy db: the idempotent migrations in
    init_schema bring either to the current format, so stamping current is
    correct for both.
    """
    stamp = read_db_format(conn)
    if stamp is not None and (stamp > DB_FORMAT_VERSION or stamp < MIN_SUPPORTED_DB_FORMAT):
        # Name the engine that CAN open it. The major version is the db format
        # version, so the stamp already says which one to install — leaving the
        # reader to derive that from the version policy is a step they should
        # never have to take.
        newer = stamp > DB_FORMAT_VERSION
        raise DBFormatError(
            f"database format {stamp} is outside this engine's supported range "
            f"{MIN_SUPPORTED_DB_FORMAT}..{DB_FORMAT_VERSION}"
            + (" (a newer engine wrote it)" if newer else " (an older engine wrote it)")
            + f" — install a {stamp}.x engine to open this world; "
            "worlds do not migrate between majors"
        )


def read_schema_revision(conn: sqlite3.Connection) -> int:
    """这个库的加法修订号。没有戳 = 1.2.x 或更早写的,读作 1。"""
    try:
        row = conn.execute(
            "SELECT value FROM db_meta WHERE key='schema_revision'"
        ).fetchone()
    except sqlite3.OperationalError:  # no db_meta table yet
        return 1
    if not row:
        return 1
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 1


def _stamp_format(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS db_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO db_meta(key, value) VALUES('format_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(DB_FORMAT_VERSION),),
    )
    # 只增不减。一个 1.3 引擎打开 1.4 写的库时,库里那些表本引擎不建也不读 ——
    # 把戳改小等于替未来的引擎撒谎,下一次谁也看不出这库里有它读不懂的东西。
    existing = read_schema_revision(conn)
    if existing < SCHEMA_REVISION:
        conn.execute(
            "INSERT INTO db_meta(key, value) VALUES('schema_revision', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_REVISION),),
        )
    elif existing > SCHEMA_REVISION:
        # 同格式,能挂,所以不拒绝 —— 但不许无声:这个世界身上有本引擎不读的表,
        # 它们代表的能力在这次运行里会整个缺席。
        logger.warning(
            "这个世界是 schema 修订 %s 写的,本引擎只到 %s —— 能挂能跑,"
            "但修订 %s 新增的表本引擎不读,对应的能力这次运行会缺席",
            existing, SCHEMA_REVISION, existing,
        )


def open_db(path: str | Path) -> sqlite3.Connection:
    """Open (or create) a WAL-mode SQLite connection.

    Raises DBFormatError — without touching the database — when the file was
    written by an engine with a newer format than this one supports.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _check_and_stamp_format(conn)
        init_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create events, memories, edges, conversations, messages, config, and prompt_templates tables + indexes if missing."""
    conn.executescript(EVENTS_SCHEMA)
    conn.executescript(MEMORIES_SCHEMA)
    conn.executescript(REFLECTION_STATE_SCHEMA)
    conn.executescript(EDGES_SCHEMA)
    conn.executescript(CONVERSATIONS_SCHEMA)
    conn.executescript(MESSAGES_SCHEMA)
    conn.executescript(CONFIG_SCHEMA)
    conn.executescript(PROMPT_TEMPLATES_SCHEMA)
    _migrate_locations(conn)  # must precede CREATE, which never alters an existing table
    conn.executescript(LOCATIONS_SCHEMA)
    conn.executescript(BT_ACTIONS_SCHEMA)
    _migrate_bt_nodes(conn)  # same reason: CHECK constraints are baked in at CREATE
    conn.executescript(BT_NODES_SCHEMA)
    conn.executescript(AGENT_NEEDS_SCHEMA)
    conn.executescript(ITEM_DEFS_SCHEMA)
    conn.executescript(SHOP_STOCK_SCHEMA)
    conn.executescript(CLIQUES_SCHEMA)
    conn.executescript(AGENT_STANCE_SCHEMA)
    conn.executescript(AGENT_MUTES_SCHEMA)
    conn.executescript(AGENT_REFUSED_TOPICS_SCHEMA)
    conn.executescript(AGENT_FOLLOWUPS_SCHEMA)
    conn.executescript(PERSONA_OVERRIDES_SCHEMA)
    _ensure_columns(conn, "conversations", {"participants": "TEXT", "location": "TEXT", "player_id": "TEXT"})
    # chat-agent:一轮聊天的观测量落在消息行上(而不是每轮发一个事件 ——
    # 聊天子系统与事件核解耦那条不变量还在)。老库的行留 NULL。
    _ensure_columns(conn, "messages", {
        "stance": "TEXT",
        "intent": "TEXT",
        "intent_confidence": "REAL",
        "tool_calls": "TEXT",
    })
    _ensure_columns(conn, "memories", {
        "strength": "REAL NOT NULL DEFAULT 1.0",
        "last_access": "INTEGER",
        "access_count": "INTEGER NOT NULL DEFAULT 0",
        "source_ids": "TEXT",
    })
    _drop_snapshots(conn)
    _stamp_format(conn)
    conn.commit()


def _drop_snapshots(conn: sqlite3.Connection) -> None:
    """Remove the retired `snapshots` table from databases that still carry it.

    The table cached the projection at some seq so a reopen could load it and
    replay only the tail. It never paid for itself: the projection that
    actually drives the world (`Scheduler._memory_projection`) is built by a
    full replay in every case, so the cache saved no work — while the writer
    persisted a half-updated projection stamped with the current MAX(seq),
    quietly losing every event folded in between. Dropping it loses nothing:
    the event log is the only truth, and a full replay rebuilds the projection
    exactly. Safe to run on every boot — a no-op once the table is gone.
    """
    conn.execute("DROP TABLE IF EXISTS snapshots")


def _script_statements(script: str) -> list[str]:
    """Split a schema script into single statements executable inside one
    transaction — executescript() would implicitly COMMIT first, which is
    exactly what table-rebuild migrations must never let happen."""
    return [stmt.strip() for stmt in script.split(";") if stmt.strip()]


def _migrate_locations(conn: sqlite3.Connection) -> None:
    """Rebuild a legacy flat-grid `locations` table into the nested schema.

    Detected by the `grid_x` column. Every legacy row becomes a top-level
    point whose grid cell maps to its center in 0~1 space; `exits` is dropped
    (nested-map D3 — it was never read). A no-op on a table that is already
    nested, so a second boot leaves user data untouched.

    The rename → rebuild → copy → drop dance runs inside ONE transaction: a
    crash mid-migration must roll back to the legacy table so the next boot
    retries, instead of leaving a committed empty table that the `grid_x`
    detection would treat as already migrated (silently orphaning every row).
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(locations)")}
    if not existing or "grid_x" not in existing:
        return
    rows = conn.execute(
        "SELECT id, name, description, grid_x, grid_y, updated_at FROM locations"
    ).fetchall()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE locations RENAME TO locations_legacy")
        for stmt in _script_statements(LOCATIONS_SCHEMA):
            conn.execute(stmt)
        for loc_id, name, description, grid_x, grid_y, updated_at in rows:
            x = (grid_x + 0.5) / _LEGACY_GRID_SIZE if grid_x is not None else None
            y = (grid_y + 0.5) / _LEGACY_GRID_SIZE if grid_y is not None else None
            conn.execute(
                "INSERT INTO locations (id, name, description, kind, parent, x, y, w, h, updated_at) "
                "VALUES (?, ?, ?, 'point', NULL, ?, ?, NULL, NULL, ?)",
                (loc_id, name, description, x, y, updated_at),
            )
        conn.execute("DROP TABLE locations_legacy")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _migrate_bt_nodes(conn: sqlite3.Connection) -> None:
    """Rebuild `bt_nodes` when its CHECK constraint predates a node type.

    SQLite bakes CHECK constraints in at CREATE TABLE, so admitting a new node
    type means rebuilding the table (same dance as `_migrate_locations`). Rows
    are copied verbatim — an existing `default` tree survives as the fallback
    tree. Idempotent: a table whose CHECK already lists `time_window` is left
    untouched.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bt_nodes'"
    ).fetchone()
    if row is None:
        return
    sql = row[0] or ""
    if all(f"'{t}'" in sql for t in BT_NODE_TYPES):
        return  # CHECK already admits every node type we know
    rows = conn.execute("SELECT tree, node_id, type, parent, sort, params FROM bt_nodes").fetchall()
    # One transaction for the same reason as _migrate_locations: a committed
    # half-rebuild passes the CHECK-substring detection and never retries.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP INDEX IF EXISTS idx_bt_nodes_tree")
        conn.execute("ALTER TABLE bt_nodes RENAME TO bt_nodes_legacy")
        for stmt in _script_statements(BT_NODES_SCHEMA):
            conn.execute(stmt)
        conn.executemany(
            "INSERT INTO bt_nodes (tree, node_id, type, parent, sort, params) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute("DROP TABLE bt_nodes_legacy")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Add missing columns to an existing table (idempotent, additive-only).

    CREATE TABLE IF NOT EXISTS never alters an existing table, so DBs created
    before a column was added to the schema need an explicit ALTER TABLE.
    Legacy rows keep NULL in the new columns.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
