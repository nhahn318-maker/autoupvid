from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_STATE = {"processed_audio": [], "uploads": []}


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return DEFAULT_STATE.copy()
        with self.path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return {**DEFAULT_STATE, **data}

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)

    def is_processed(self, audio_path: Path) -> bool:
        return str(audio_path.resolve()) in self.data["processed_audio"]

    def uploads_for(self, audio_path: Path) -> list[dict[str, Any]]:
        resolved = str(audio_path.resolve())
        return [item for item in self.data["uploads"] if item.get("audio") == resolved]

    def has_upload(self, audio_path: Path, upload_type: str) -> bool:
        return any(item.get("type") == upload_type for item in self.uploads_for(audio_path))

    def mark_processed(self, audio_path: Path) -> None:
        resolved = str(audio_path.resolve())
        if resolved not in self.data["processed_audio"]:
            self.data["processed_audio"].append(resolved)
        self.save()

    def needs_work(self, audio_path: Path, shorts_enabled: bool) -> bool:
        uploads = self.uploads_for(audio_path)
        if self.is_processed(audio_path) and not uploads:
            return False
        if not self.has_upload(audio_path, "normal"):
            return True
        return shorts_enabled and not self.has_upload(audio_path, "short")

    def is_complete(self, audio_path: Path, shorts_enabled: bool) -> bool:
        if self.is_processed(audio_path) and not self.uploads_for(audio_path):
            return True
        if not self.has_upload(audio_path, "normal"):
            return False
        return not shorts_enabled or self.has_upload(audio_path, "short")

    def add_upload(self, item: dict[str, Any]) -> None:
        self.data["uploads"].append(item)
        self.save()

    def used_publish_times(self) -> set[str]:
        return {
            item["publish_at"]
            for item in self.data["uploads"]
            if item.get("publish_at")
        }

    def prune_missing_audio(self) -> int:
        before_processed = len(self.data["processed_audio"])
        before_uploads = len(self.data["uploads"])

        self.data["processed_audio"] = [
            item for item in self.data["processed_audio"] if Path(item).exists()
        ]
        self.data["uploads"] = [
            item
            for item in self.data["uploads"]
            if item.get("audio") and Path(item["audio"]).exists()
        ]
        removed = (before_processed - len(self.data["processed_audio"])) + (
            before_uploads - len(self.data["uploads"])
        )
        if removed:
            self.save()
        return removed
