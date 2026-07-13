from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AppConfig:
    data: dict[str, Any]
    root: Path

    @property
    def paths(self) -> dict[str, Path]:
        raw_paths = dict(self.data["paths"])
        active_account = self.data.get("active_account")
        account_overrides = self.data.get("account_overrides", {})
        path_overrides = account_overrides.get(active_account, {}).get("paths", {})
        if isinstance(path_overrides, dict):
            raw_paths.update(path_overrides)
        return {
            key: self.root / value
            for key, value in raw_paths.items()
            if key.endswith("_dir") or key.endswith("_file")
        }

    def get(self, *keys: str, default: Any = None) -> Any:
        current: Any = self.effective_data()
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
        return current

    def effective_data(self) -> dict[str, Any]:
        data = dict(self.data)
        active_account = data.get("active_account")
        account_overrides = data.get("account_overrides", {})
        overrides = account_overrides.get(active_account, {})
        if not isinstance(overrides, dict):
            return data
        for key, value in overrides.items():
            if key == "paths":
                continue
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                merged = dict(data[key])
                merged.update(value)
                data[key] = merged
            else:
                data[key] = value
        return data


def load_config(root: Path, config_file: str = "config.json") -> AppConfig:
    path = root / config_file
    if not path.exists():
        example = root / "config.example.json"
        raise FileNotFoundError(
            f"Missing {path.name}. Copy {example.name} to {path.name} and edit it."
        )

    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)

    return AppConfig(data=repair_mojibake(data), root=root)


def repair_mojibake(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: repair_mojibake(item) for key, item in value.items()}
    if isinstance(value, list):
        return [repair_mojibake(item) for item in value]
    if isinstance(value, str):
        return repair_mojibake_text_deep(value)
    return value


MOJIBAKE_MARKERS = ("\u00c3", "\u00c2", "\u00c4", "\u00c6", "\u00e2\u20ac", "\u00e1\u00bb", "\u00e1\u00ba", "\ufffd")


def repair_mojibake_text_deep(value: str) -> str:
    if not any(marker in value for marker in MOJIBAKE_MARKERS):
        return value
    repaired = value
    for _ in range(4):
        next_value = repair_mojibake_text_once(repaired)
        if next_value == repaired:
            break
        repaired = next_value
    return repaired


def repair_mojibake_text_once(value: str) -> str:
    repaired = None
    for encoding in ("cp1252", "latin-1"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
            break
        except UnicodeError:
            continue
    if repaired is None:
        try:
            repaired = mixed_mojibake_bytes(value).decode("utf-8")
        except UnicodeError:
            repaired = None
    if repaired is None:
        return value
    return repaired if mojibake_score_deep(repaired) < mojibake_score_deep(value) else value


def mojibake_score_deep(value: str) -> int:
    return sum(value.count(marker) for marker in MOJIBAKE_MARKERS)


def repair_mojibake_text(value: str) -> str:
    if not any(marker in value for marker in ("Ã", "Ä", "Â", "â", "ð")):
        return value
    repaired = None
    for encoding in ("cp1252", "latin-1"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
            break
        except UnicodeError:
            continue
    if repaired is None:
        try:
            repaired = mixed_mojibake_bytes(value).decode("utf-8")
        except UnicodeError:
            repaired = None
    if repaired is None:
        return value
    original_score = mojibake_score(value)
    repaired_score = mojibake_score(repaired)
    return repaired if repaired_score < original_score else value


def mojibake_score(value: str) -> int:
    return sum(value.count(marker) for marker in ("Ã", "Ä", "Â", "â", "ð", "�"))


def mixed_mojibake_bytes(value: str) -> bytes:
    output = bytearray()
    for char in value:
        try:
            output.extend(char.encode("cp1252"))
            continue
        except UnicodeError:
            pass
        codepoint = ord(char)
        if codepoint <= 255:
            output.append(codepoint)
            continue
        raise UnicodeError(f"Cannot map {char!r} to a mojibake byte")
    return bytes(output)
