from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


class JobStore:
    """Durable storage for the existing in-process job queue.

    The web worker remains unchanged. SQLite preserves queue payloads and job
    history across backend restarts without introducing another service.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    job_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)"
            )
            connection.commit()

    def save(self, job: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            return
        existing_payload = self.payload_for(job_id) if payload is None else payload
        payload_json = json.dumps(existing_payload or {}, ensure_ascii=False, default=str)
        job_json = json.dumps(job, ensure_ascii=False, default=str)
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO jobs(id, action, payload_json, job_json, status, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    action=excluded.action,
                    payload_json=excluded.payload_json,
                    job_json=excluded.job_json,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    job_id,
                    str(job.get("action") or ""),
                    payload_json,
                    job_json,
                    str(job.get("status") or "unknown"),
                    str(job.get("created_at") or now),
                    now,
                ),
            )
            connection.commit()

    def payload_for(self, job_id: str) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def load_recent(self, limit: int = 100) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT job_json, payload_json FROM jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        output: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for job_json, payload_json in reversed(rows):
            try:
                job = json.loads(job_json)
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(job, dict) and isinstance(payload, dict):
                output.append((job, payload))
        return output

    def mark_interrupted_runs(self) -> None:
        """Never blindly replay an in-flight upload after a crash."""
        for job, payload in self.load_recent(limit=200):
            if job.get("status") != "running":
                continue
            job["status"] = "interrupted"
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            job.setdefault("logs", []).append(
                f"{datetime.now().strftime('%H:%M:%S')} Backend restarted while job was running; "
                "job preserved for inspection to avoid a duplicate upload."
            )
            self.save(job, payload)
