"""AuthorStore: SQLite job store for the novel-import authoring pipeline.

Separate author.db — not world.db. Tables are created idempotently (CREATE TABLE
IF NOT EXISTS). All public methods hold self._lock for thread safety.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_VALID_STATUSES = {"scanning", "scan_done", "distilling", "done", "failed"}

_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    title        TEXT,
    novel_path   TEXT,
    status       TEXT,
    cursor       INTEGER DEFAULT 0,
    total_chunks INTEGER DEFAULT 0,
    params_json  TEXT DEFAULT '{}',
    error        TEXT DEFAULT '',
    created_at   REAL,
    updated_at   REAL
);

CREATE TABLE IF NOT EXISTS chunks (
    job_id    TEXT,
    idx       INTEGER,
    title     TEXT,
    start_off INTEGER,
    end_off   INTEGER,
    status    TEXT DEFAULT 'pending',
    error     TEXT DEFAULT '',
    PRIMARY KEY (job_id, idx)
);

CREATE TABLE IF NOT EXISTS entities (
    job_id      TEXT,
    entity_id   TEXT,
    kind        TEXT,
    name        TEXT,
    aliases_json TEXT DEFAULT '[]',
    brief       TEXT DEFAULT '',
    first_chunk INTEGER,
    PRIMARY KEY (job_id, entity_id)
);

CREATE TABLE IF NOT EXISTS notes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT,
    entity_id TEXT,
    chunk_idx INTEGER,
    text      TEXT
);

CREATE TABLE IF NOT EXISTS appearances (
    job_id    TEXT,
    entity_id TEXT,
    chunk_idx INTEGER,
    PRIMARY KEY (job_id, entity_id, chunk_idx)
);

CREATE TABLE IF NOT EXISTS relation_facts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT,
    a         TEXT,
    b         TEXT,
    chunk_idx INTEGER,
    nature    TEXT,
    origin    TEXT
);

CREATE TABLE IF NOT EXISTS lore_notes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT,
    chunk_idx INTEGER,
    text      TEXT
);

CREATE TABLE IF NOT EXISTS final_agents (
    job_id      TEXT,
    entity_id   TEXT,
    name        TEXT,
    location    TEXT,
    personality TEXT,
    PRIMARY KEY (job_id, entity_id)
);

CREATE TABLE IF NOT EXISTS final_relations (
    job_id     TEXT,
    a          TEXT,
    b          TEXT,
    sentiment  REAL,
    r_type     TEXT,
    r_type_back TEXT,
    PRIMARY KEY (job_id, a, b)
);

CREATE TABLE IF NOT EXISTS final_memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT,
    agent_id   TEXT,
    summary    TEXT,
    importance REAL
);

CREATE TABLE IF NOT EXISTS final_locations (
    job_id      TEXT,
    entity_id   TEXT,
    name        TEXT,
    description TEXT,
    x           REAL,
    y           REAL,
    PRIMARY KEY (job_id, entity_id)
);

CREATE TABLE IF NOT EXISTS final_world_setting (
    job_id TEXT PRIMARY KEY,
    text   TEXT
);
"""


def _row_to_dict(cur: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: val for col, val in zip(cur.description, row)}


def _dicts(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    if cur.description is None:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


class AuthorStore:
    """SQLite-backed store for novel-import jobs and their derived data."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_DDL)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _entity_exists(self, job_id: str, entity_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM entities WHERE job_id = ? AND entity_id = ?",
            (job_id, entity_id),
        )
        return cur.fetchone() is not None

    def create_job(
        self,
        job_id: str,
        title: str,
        novel_path: str,
        chunks: list[tuple[int, str, int, int]],
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (job_id, title, novel_path, status, cursor, total_chunks, "
                "params_json, error, created_at, updated_at) VALUES (?, ?, ?, 'scanning', 0, ?, '{}', ?, ?, ?)",
                (job_id, title, novel_path, len(chunks), '', now, now),
            )
            for idx, ctitle, start_off, end_off in chunks:
                self._conn.execute(
                    "INSERT INTO chunks (job_id, idx, title, start_off, end_off, status, error) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', '')",
                    (job_id, idx, ctitle, start_off, end_off),
                )
            self._conn.commit()
            return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            rows = _dicts(cur)
            if not rows:
                raise KeyError(job_id)
            return rows[0]

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM jobs ORDER BY created_at DESC")
            return _dicts(cur)

    def set_job_status(self, job_id: str, status: str, *, error: str = '') -> None:
        if status not in _VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}; must be one of {sorted(_VALID_STATUSES)}")
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE job_id = ?",
                (status, error, now, job_id),
            )
            self._conn.commit()

    def pending_chunks(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM chunks WHERE job_id = ? AND status = 'pending' ORDER BY idx",
                (job_id,),
            )
            return _dicts(cur)

    def list_chunks(self, job_id: str) -> list[dict[str, Any]]:
        """Return ALL chunks for a job ordered by idx, regardless of status."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM chunks WHERE job_id = ? ORDER BY idx",
                (job_id,),
            )
            return _dicts(cur)

    def mark_chunk_failed(self, job_id: str, idx: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE chunks SET status = 'failed', error = ? WHERE job_id = ? AND idx = ?",
                (error, job_id, idx),
            )
            self._conn.commit()

    def retry_chunk(self, job_id: str, idx: int) -> None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT status FROM chunks WHERE job_id = ? AND idx = ?",
                (job_id, idx),
            )
            row = cur.fetchone()
            if row is None or row[0] != "failed":
                current = row[0] if row else "missing"
                raise ValueError(f"cannot retry chunk {idx} in status {current!r}; must be 'failed'")
            self._conn.execute(
                "UPDATE chunks SET status = 'pending', error = '' WHERE job_id = ? AND idx = ?",
                (job_id, idx),
            )
            self._conn.commit()

    def merge_chunk_result(self, job_id: str, chunk_idx: int, result: dict) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                if "new_entities" in result:
                    for ent in result["new_entities"]:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO entities "
                            "(job_id, entity_id, kind, name, aliases_json, brief, first_chunk) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                job_id,
                                ent["id"],
                                ent["kind"],
                                ent["name"],
                                json.dumps(ent.get("aliases", []), ensure_ascii=False),
                                ent.get("brief", ""),
                                chunk_idx,
                            ),
                        )

                if "alias_additions" in result:
                    for entity_id, new_aliases in result["alias_additions"].items():
                        if not self._entity_exists(job_id, entity_id):
                            continue
                        cur = self._conn.execute(
                            "SELECT aliases_json FROM entities WHERE job_id = ? AND entity_id = ?",
                            (job_id, entity_id),
                        )
                        row = cur.fetchone()
                        existing: list[str] = json.loads(row[0]) if row else []
                        merged = existing + [a for a in new_aliases if a not in existing]
                        self._conn.execute(
                            "UPDATE entities SET aliases_json = ? WHERE job_id = ? AND entity_id = ?",
                            (json.dumps(merged, ensure_ascii=False), job_id, entity_id),
                        )

                if "appeared" in result:
                    for entity_id in result["appeared"]:
                        if not self._entity_exists(job_id, entity_id):
                            continue
                        self._conn.execute(
                            "INSERT OR IGNORE INTO appearances (job_id, entity_id, chunk_idx) VALUES (?, ?, ?)",
                            (job_id, entity_id, chunk_idx),
                        )

                if "notes" in result:
                    for note in result["notes"]:
                        entity_id = note["entity_id"]
                        if not self._entity_exists(job_id, entity_id):
                            continue
                        self._conn.execute(
                            "INSERT INTO notes (job_id, entity_id, chunk_idx, text) VALUES (?, ?, ?, ?)",
                            (job_id, entity_id, chunk_idx, note["text"]),
                        )

                if "relation_facts" in result:
                    for fact in result["relation_facts"]:
                        a, b = fact["a"], fact["b"]
                        if not self._entity_exists(job_id, a) or not self._entity_exists(job_id, b):
                            continue
                        self._conn.execute(
                            "INSERT INTO relation_facts (job_id, a, b, chunk_idx, nature, origin) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (job_id, a, b, chunk_idx, fact["nature"], fact["origin"]),
                        )

                if "lore" in result:
                    for text in result["lore"]:
                        self._conn.execute(
                            "INSERT INTO lore_notes (job_id, chunk_idx, text) VALUES (?, ?, ?)",
                            (job_id, chunk_idx, text),
                        )

                now = time.time()
                self._conn.execute(
                    "UPDATE chunks SET status = 'done' WHERE job_id = ? AND idx = ?",
                    (job_id, chunk_idx),
                )
                self._conn.execute(
                    "UPDATE jobs SET cursor = cursor + 1, updated_at = ? WHERE job_id = ?",
                    (now, job_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def entities(self, job_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if kind is not None:
                cur = self._conn.execute(
                    "SELECT * FROM entities WHERE job_id = ? AND kind = ?",
                    (job_id, kind),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM entities WHERE job_id = ?",
                    (job_id,),
                )
            rows = _dicts(cur)
            for row in rows:
                raw = row.get("aliases_json", "[]")
                row["aliases"] = json.loads(raw) if raw else []
                del row["aliases_json"]

                app_cur = self._conn.execute(
                    "SELECT chunk_idx FROM appearances WHERE job_id = ? AND entity_id = ?",
                    (job_id, row["entity_id"]),
                )
                row["appearance_chunks"] = [r[0] for r in app_cur.fetchall()]

                count_cur = self._conn.execute(
                    "SELECT COUNT(*) FROM notes WHERE job_id = ? AND entity_id = ?",
                    (job_id, row["entity_id"]),
                )
                row["note_count"] = count_cur.fetchone()[0]
            return rows

    def entity_notes(self, job_id: str, entity_id: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM notes WHERE job_id = ? AND entity_id = ? ORDER BY id",
                (job_id, entity_id),
            )
            return _dicts(cur)

    def relation_facts(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM relation_facts WHERE job_id = ? ORDER BY id",
                (job_id,),
            )
            return _dicts(cur)

    def lore_notes(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM lore_notes WHERE job_id = ? ORDER BY id",
                (job_id,),
            )
            return _dicts(cur)

    def replace_final(
        self,
        job_id: str,
        *,
        agents: list[dict],
        relations: list[dict],
        memories: list[dict],
        locations: list[dict],
        world_setting: str,
    ) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for tbl in (
                    "final_agents",
                    "final_relations",
                    "final_memories",
                    "final_locations",
                    "final_world_setting",
                ):
                    self._conn.execute(f"DELETE FROM {tbl} WHERE job_id = ?", (job_id,))

                for agent in agents:
                    self._conn.execute(
                        "INSERT INTO final_agents (job_id, entity_id, name, location, personality) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (job_id, agent["entity_id"], agent["name"], agent["location"], agent["personality"]),
                    )

                for rel in relations:
                    self._conn.execute(
                        "INSERT INTO final_relations (job_id, a, b, sentiment, r_type, r_type_back) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (job_id, rel["a"], rel["b"], rel["sentiment"], rel["r_type"], rel["r_type_back"]),
                    )

                for mem in memories:
                    self._conn.execute(
                        "INSERT INTO final_memories (job_id, agent_id, summary, importance) "
                        "VALUES (?, ?, ?, ?)",
                        (job_id, mem["agent_id"], mem["summary"], mem["importance"]),
                    )

                for loc in locations:
                    self._conn.execute(
                        "INSERT INTO final_locations (job_id, entity_id, name, description, x, y) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (job_id, loc["entity_id"], loc["name"], loc["description"], loc["x"], loc["y"]),
                    )

                self._conn.execute(
                    "INSERT INTO final_world_setting (job_id, text) VALUES (?, ?)",
                    (job_id, world_setting),
                )

                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def final_seed_parts(self, job_id: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM final_agents WHERE job_id = ?", (job_id,)
            )
            agents = _dicts(cur)
            if not agents:
                return None

            cur = self._conn.execute(
                "SELECT * FROM final_relations WHERE job_id = ?", (job_id,)
            )
            relations = _dicts(cur)

            cur = self._conn.execute(
                "SELECT * FROM final_memories WHERE job_id = ?", (job_id,)
            )
            memories = _dicts(cur)

            cur = self._conn.execute(
                "SELECT * FROM final_locations WHERE job_id = ?", (job_id,)
            )
            locations = _dicts(cur)

            cur = self._conn.execute(
                "SELECT text FROM final_world_setting WHERE job_id = ?", (job_id,)
            )
            row = cur.fetchone()
            world_setting = row[0] if row else ""

            return {
                "agents": agents,
                "relations": relations,
                "memories": memories,
                "locations": locations,
                "world_setting": world_setting,
            }
