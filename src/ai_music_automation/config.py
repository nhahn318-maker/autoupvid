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

    return AppConfig(data=data, root=root)
