from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator


class AutomationLogger:
    """Structured logger for automation stages."""

    def __init__(
        self,
        log_dir: Path,
        run_id: str,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        self.log_dir = log_dir
        self.run_id = run_id
        self.emit = emit
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / f"{run_id}.jsonl"
        self.state_path = self.log_dir / f"{run_id}.state.json"
        self._write_lock = threading.RLock()

    def event(self, stage: str, status: str, **details: Any) -> None:
        payload = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "stage": stage,
            "status": status,
            "details": details,
        }
        with self._write_lock:
            line = json.dumps(payload, ensure_ascii=False)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
            state = {
                "run_id": self.run_id,
                "updated_at": payload["time"],
                "current_stage": stage,
                "status": status,
                "details": details,
            }
            temporary = self.state_path.with_name(
                f"{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}.tmp"
            )
            try:
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(state, handle, ensure_ascii=False, indent=2, default=str)
                    handle.flush()
                    os.fsync(handle.fileno())
                for attempt in range(5):
                    try:
                        os.replace(temporary, self.state_path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.1 * (attempt + 1))
            finally:
                if temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
        if self.emit:
            message = f"{status.upper()} {stage}"
            if "duration_seconds" in details:
                message = f"{message} ({details['duration_seconds']}s)"
            if "message" in details and details["message"]:
                message = f"{message}: {details['message']}"
            self.emit(message)

    @contextmanager
    def stage(self, name: str, **details: Any) -> Iterator[None]:
        started = time.monotonic()
        self.event(name, "start", **details)
        try:
            yield
        except Exception as exc:
            self.event(name, "error", duration_seconds=round(time.monotonic() - started, 3), error=str(exc))
            raise
        self.event(name, "end", duration_seconds=round(time.monotonic() - started, 3))
