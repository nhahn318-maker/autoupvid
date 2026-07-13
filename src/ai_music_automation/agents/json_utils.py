from __future__ import annotations

import json
import re
from typing import Any


def extract_json_object(value: str) -> dict[str, Any] | None:
    text = strip_code_fence(value)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def extract_json_array(value: str) -> list[Any] | None:
    text = strip_code_fence(value)
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def strip_code_fence(value: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", (value or "").strip(), flags=re.I | re.S).strip()


def as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []
