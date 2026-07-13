from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_STATE = {"processed_audio": [], "uploads": [], "collected_audio": []}


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

    def needs_work(
        self,
        audio_path: Path,
        shorts_enabled: bool,
        required_upload_types: set[str] | None = None,
    ) -> bool:
        uploads = self.uploads_for(audio_path)
        if self.is_processed(audio_path) and not uploads:
            return False
        for upload_type in self._required_upload_types(shorts_enabled, required_upload_types):
            if not self.has_upload(audio_path, upload_type):
                return True
        return False

    def is_complete(
        self,
        audio_path: Path,
        shorts_enabled: bool,
        required_upload_types: set[str] | None = None,
    ) -> bool:
        if self.is_processed(audio_path) and not self.uploads_for(audio_path):
            return True
        return all(
            self.has_upload(audio_path, upload_type)
            for upload_type in self._required_upload_types(shorts_enabled, required_upload_types)
        )

    def _required_upload_types(
        self,
        shorts_enabled: bool,
        required_upload_types: set[str] | None,
    ) -> set[str]:
        if required_upload_types is not None:
            return required_upload_types
        return {"normal", "short"} if shorts_enabled else {"normal"}

    def add_upload(self, item: dict[str, Any]) -> None:
        self.data["uploads"].append(item)
        self.save()

    def is_collected(self, audio_path: Path) -> bool:
        return str(audio_path.resolve()) in self.data["collected_audio"]

    def mark_collected(self, audio_paths: list[Path]) -> None:
        collected = self.data["collected_audio"]
        changed = False
        for audio_path in audio_paths:
            resolved = str(audio_path.resolve())
            if resolved not in collected:
                collected.append(resolved)
                changed = True
        if changed:
            self.save()

    def used_publish_times(self) -> set[str]:
        return {
            item["publish_at"]
            for item in self.data["uploads"]
            if item.get("publish_at")
        }

    def youtube_video_ids(self) -> set[str]:
        return {
            str(item["youtube_id"])
            for item in self.data["uploads"]
            if item.get("youtube_id")
        }

    def prune_missing_youtube_uploads(self, existing_video_ids: set[str]) -> int:
        before_uploads = len(self.data["uploads"])
        self.data["uploads"] = [
            item
            for item in self.data["uploads"]
            if not item.get("youtube_id") or str(item["youtube_id"]) in existing_video_ids
        ]
        removed = before_uploads - len(self.data["uploads"])
        if removed:
            self.save()
        return removed

    def prune_missing_audio(self) -> int:
        before_processed = len(self.data["processed_audio"])
        before_uploads = len(self.data["uploads"])

        self.data["processed_audio"] = [
            item for item in self.data["processed_audio"] if Path(item).exists()
        ]
        before_collected = len(self.data["collected_audio"])
        self.data["collected_audio"] = [
            item for item in self.data["collected_audio"] if Path(item).exists()
        ]
        self.data["uploads"] = [
            item
            for item in self.data["uploads"]
            if item.get("audio") and Path(item["audio"]).exists()
        ]
        removed = (before_processed - len(self.data["processed_audio"])) + (
            before_uploads - len(self.data["uploads"])
        ) + (
            before_collected - len(self.data["collected_audio"])
        )
        if removed:
            self.save()
        return removed
