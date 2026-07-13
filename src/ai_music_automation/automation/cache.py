from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


class AutomationCache:
    """Small JSON cache keyed by stable payload hashes."""

    def __init__(self, cache_dir: Path, enabled: bool = True) -> None:
        self.cache_dir = cache_dir
        self.enabled = enabled
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def key_for(self, namespace: str, payload: Any) -> str:
        normalized = json.dumps(
            self._jsonable(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return f"{namespace}-{digest[:24]}"

    def read_json(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self._path_for(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def write_json(self, key: str, value: dict[str, Any]) -> Path:
        path = self._path_for(key)
        if self.enabled:
            temporary = path.with_name(
                f"{path.name}.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}.tmp"
            )
            try:
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(value, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                for attempt in range(5):
                    try:
                        os.replace(temporary, path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.1 * (attempt + 1))
            finally:
                temporary.unlink(missing_ok=True)
        return path

    def exists(self, key: str) -> bool:
        return self.enabled and self._path_for(key).exists()

    def _path_for(self, key: str) -> Path:
        safe_key = "".join(char if char.isalnum() or char in "-_." else "-" for char in key)
        return self.cache_dir / f"{safe_key}.json"

    def _jsonable(self, value: Any) -> Any:
        if is_dataclass(value):
            return self._jsonable(asdict(value))
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): self._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._jsonable(item) for item in value]
        return value
