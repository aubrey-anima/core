"""Author studio FastAPI application.

Exposes the novel-import pipeline (scan + distill) behind a REST API.
Worker threads run asyncio.run(run_scan/run_distill) — LLM is never called
from the request thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from anima_world.author.extract import split_novel, run_scan
from anima_world.author.distill import run_distill, assemble_seed
from anima_world.author.store import AuthorStore

logger = logging.getLogger(__name__)


def create_author_app(
    store: AuthorStore,
    llm_factory: Callable[[], Any],
    *,
    data_dir: str | Path,
    db_editor_port: int | None = None,
    db_editor_url: str | None = None,
) -> FastAPI:
    """Create the author studio FastAPI application.

    Args:
        store: AuthorStore instance (caller owns its lifecycle).
        llm_factory: zero-arg callable returning an LLM client.
            Called inside worker threads, never in request handlers.
        data_dir: directory where novel texts are stored as
            ``<data_dir>/novels/<job_id>.txt``.
        db_editor_port: port of a sqlite-web instance on the same host, if
            one runs alongside — the studio frontend embeds it when set.
        db_editor_url: full public URL of that sqlite-web instance; wins
            over the port when the studio is reached through a tunnel or
            reverse proxy where ``host:port`` addressing doesn't apply.
    """
    data_dir = Path(data_dir)
    novels_dir = data_dir / "novels"

    # Shutdown event: set during lifespan teardown so scan workers abort early.
    _stop_event = threading.Event()

    # Per-job worker guard: prevents spawning a duplicate worker for the same job.
    _running: set[str] = set()
    _running_count: dict[str, int] = defaultdict(int)
    _lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        novels_dir.mkdir(parents=True, exist_ok=True)
        yield
        # Signal all scan workers to stop gracefully.
        _stop_event.set()

    app = FastAPI(title="Cyberworld Author Studio", docs_url=None, redoc_url=None, lifespan=lifespan)
    # Expose internals for test assertions.
    app.state._running = _running
    app.state._running_count = _running_count
    app.state._lock = _lock

    # ------------------------------------------------------------------
    # Worker helpers
    # ------------------------------------------------------------------

    def _spawn_scan_worker(job_id: str, novel_text: str) -> bool:
        """Spawn a background scan worker for job_id if one is not already running.

        Returns True if a new worker was spawned, False if one already existed.
        """
        with _lock:
            if job_id in _running:
                return False
            _running.add(job_id)
            _running_count[job_id] += 1

        def _worker() -> None:
            try:
                llm = llm_factory()
                asyncio.run(
                    run_scan(
                        llm,
                        store,
                        job_id,
                        novel_text,
                        should_stop=_stop_event.is_set,
                    )
                )
            except Exception:  # noqa: BLE001
                logger.exception("scan worker failed job=%s", job_id)
                try:
                    store.set_job_status(job_id, "failed", error="scan worker crashed")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                with _lock:
                    _running.discard(job_id)

        t = threading.Thread(target=_worker, daemon=True, name=f"scan-{job_id}")
        t.start()
        return True

    def _spawn_distill_worker(job_id: str) -> bool:
        """Spawn a background distill worker for job_id if one is not already running.

        Returns True if a new worker was spawned, False if one already existed.
        """
        with _lock:
            if job_id in _running:
                return False
            _running.add(job_id)
            _running_count[job_id] += 1

        def _worker() -> None:
            try:
                llm = llm_factory()
                asyncio.run(run_distill(llm, store, job_id))
            except Exception:  # noqa: BLE001
                logger.exception("distill worker failed job=%s", job_id)
                try:
                    store.set_job_status(job_id, "failed", error="distill worker crashed")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                with _lock:
                    _running.discard(job_id)

        t = threading.Thread(target=_worker, daemon=True, name=f"distill-{job_id}")
        t.start()
        return True

    def _novel_text(job_id: str) -> str:
        """Read persisted novel text for a job (raises HTTPException on miss)."""
        p = novels_dir / f"{job_id}.txt"
        try:
            return p.read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(status_code=404, detail="novel file not found on disk") from exc

    def _get_job_or_404(job_id: str) -> dict:
        try:
            return store.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    # Module-level path constant — exposed so tests can monkeypatch it.
    _studio_index_path = Path(__file__).parent / "static" / "index.html"
    app.state._studio_index_path_ref = lambda: _studio_index_path

    @app.get("/")
    async def studio_index():
        index_path = app.state._studio_index_path_ref()
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="studio UI not yet deployed")
        return FileResponse(str(index_path), media_type="text/html")

    @app.get("/author/v1/config")
    async def studio_config():
        return {"db_editor_port": db_editor_port, "db_editor_url": db_editor_url}

    # ---- Jobs ----

    @app.post("/author/v1/jobs", status_code=201)
    async def create_job(body: dict) -> JSONResponse:
        title: str = body.get("title", "")
        text: str = body.get("text", "")
        if not text or not text.strip():
            raise HTTPException(status_code=400, detail="text must not be empty")

        # Generate job id
        job_id = uuid.uuid4().hex[:12]

        # Write novel file to disk
        novels_dir.mkdir(parents=True, exist_ok=True)
        novel_path = novels_dir / f"{job_id}.txt"
        novel_path.write_text(text, encoding="utf-8")

        # Split into chunks
        try:
            chunks = split_novel(text)
        except ValueError as exc:
            novel_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        chunk_tuples = [(c.idx, c.title, c.start, c.end) for c in chunks]

        # Persist job
        job = store.create_job(job_id, title, str(novel_path), chunk_tuples)

        # Spawn scan worker
        _spawn_scan_worker(job_id, text)

        return JSONResponse(job, status_code=201)

    @app.get("/author/v1/jobs")
    async def list_jobs() -> dict:
        return {"jobs": store.list_jobs()}

    @app.get("/author/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        job = _get_job_or_404(job_id)
        chunks = store.list_chunks(job_id)
        failed_idxs = [c["idx"] for c in chunks if c["status"] == "failed"]
        job["chunks"] = chunks
        job["failed_chunks"] = failed_idxs
        return job

    # ---- Chunk retry ----

    @app.post("/author/v1/jobs/{job_id}/chunks/{idx}/retry")
    async def retry_chunk(job_id: str, idx: int) -> dict:
        _get_job_or_404(job_id)
        try:
            store.retry_chunk(job_id, idx)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        text = _novel_text(job_id)
        _spawn_scan_worker(job_id, text)
        return {"ok": True}

    # ---- Resume scanning ----

    @app.post("/author/v1/jobs/{job_id}/resume")
    async def resume_job(job_id: str) -> dict:
        job = _get_job_or_404(job_id)
        if job["status"] != "scanning":
            raise HTTPException(
                status_code=409,
                detail=f"can only resume a scanning job; current status is {job['status']!r}",
            )
        text = _novel_text(job_id)
        _spawn_scan_worker(job_id, text)
        return {"ok": True}

    # ---- Distill ----

    @app.post("/author/v1/jobs/{job_id}/distill")
    async def distill_job(job_id: str) -> dict:
        job = _get_job_or_404(job_id)
        if job["status"] == "scanning":
            raise HTTPException(
                status_code=409,
                detail="cannot distill while scan is in progress",
            )
        _spawn_distill_worker(job_id)
        return {"ok": True}

    # ---- Entities ----

    @app.get("/author/v1/jobs/{job_id}/entities")
    async def get_entities(job_id: str, kind: str | None = None) -> dict:
        _get_job_or_404(job_id)
        return {"entities": store.entities(job_id, kind=kind)}

    @app.get("/author/v1/jobs/{job_id}/entities/{entity_id}/notes")
    async def get_entity_notes(job_id: str, entity_id: str) -> dict:
        _get_job_or_404(job_id)
        return {"notes": store.entity_notes(job_id, entity_id)}

    # ---- Relations ----

    @app.get("/author/v1/jobs/{job_id}/relations")
    async def get_relations(job_id: str) -> dict:
        _get_job_or_404(job_id)
        final = store.final_seed_parts(job_id)
        final_relations = final["relations"] if final else []
        return {
            "facts": store.relation_facts(job_id),
            "final": final_relations,
        }

    # ---- Lore ----

    @app.get("/author/v1/jobs/{job_id}/lore")
    async def get_lore(job_id: str) -> dict:
        _get_job_or_404(job_id)
        final = store.final_seed_parts(job_id)
        world_setting = final["world_setting"] if final else ""
        return {
            "lore": store.lore_notes(job_id),
            "world_setting": world_setting,
        }

    # ---- Materialize ----

    @app.post("/author/v1/jobs/{job_id}/materialize")
    def materialize_job(job_id: str) -> dict:
        """Turn a distilled seed into a world.db via the anima_world simulate CLI.

        Returns {"db_path": str, "seed_path": str} on success.
        Uses a sync def so FastAPI runs it in a thread-pool automatically,
        keeping the event loop free during the subprocess call.
        """
        job = _get_job_or_404(job_id)
        if job["status"] != "done":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"can only materialize a done job; "
                    f"current status is {job['status']!r}"
                ),
            )

        # Assemble seed
        try:
            seed = assemble_seed(store, job_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # Write seed file (overwrite every time — seed is an export artifact)
        seeds_dir = data_dir / "seeds"
        seeds_dir.mkdir(parents=True, exist_ok=True)
        seed_path = seeds_dir / f"{job_id}-seed.json"
        seed_path.write_text(
            json.dumps(seed, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Target world DB — never overwrite
        worlds_dir = data_dir / "worlds"
        worlds_dir.mkdir(parents=True, exist_ok=True)
        db_path = worlds_dir / f"{job_id}.db"
        if db_path.exists():
            raise HTTPException(status_code=409, detail="world db already exists")

        # Build the subprocess command
        cmd = [
            sys.executable,
            "-m", "anima_world",
            "simulate",
            "--db-path", str(db_path),
            "--seed", str(seed_path),
            "--ticks", "0",
            "--llm", "mock",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(
                status_code=502,
                detail="simulate subprocess timed out after 120 seconds",
            )

        if result.returncode != 0:
            stderr_tail = result.stderr[-500:] if result.stderr else "(no stderr)"
            raise HTTPException(
                status_code=502,
                detail=f"simulate subprocess failed (rc={result.returncode}): {stderr_tail}",
            )

        return {"db_path": str(db_path), "seed_path": str(seed_path)}

    # ---- Seed download ----

    @app.get("/author/v1/jobs/{job_id}/seed")
    async def get_seed(job_id: str):
        job = _get_job_or_404(job_id)
        if job["status"] != "done":
            raise HTTPException(
                status_code=409,
                detail=f"seed is only available when job is done; current status is {job['status']!r}",
            )
        try:
            seed = assemble_seed(store, job_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        return JSONResponse(
            content=seed,
            headers={
                "Content-Disposition": f'attachment; filename="{job_id}-seed.json"',
            },
        )

    return app
