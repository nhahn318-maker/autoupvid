from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .media import Track


MAX_YOUTUBE_TITLE_LENGTH = 100


@dataclass(frozen=True)
class VideoMetadata:
    title: str
    description: str
    tags: list[str]
    category_id: str
    made_for_kids: bool
    thumbnail_path: Path | None = None


def build_metadata(
    track: Track,
    config: dict[str, Any],
    thumbnail_dir: Path,
    video_type: str = "normal",
) -> VideoMetadata:
    channel = effective_section(config, "channel")
    values = {
        "track_title": humanize_title(track.title),
        "author": channel.get("default_author", "Trần Thiện Nhân"),
        "mood_description": choose_template(
            templates=channel.get("mood_descriptions"),
            fallback="Một ca khúc bolero vinahouse remix buồn, mang màu sắc hoài niệm và nỗi nhớ dành cho một người đã xa.",
            key=f"{track.slug}-description",
        ),
    }
    title_template = choose_template(
        templates=channel.get("title_templates"),
        fallback=channel["default_title_template"],
        key=track.slug,
    )
    description = channel["default_description_template"].format(**values)

    if video_type == "short":
        shorts = effective_section(config, "shorts")
        title_template = choose_template(
            templates=shorts.get("title_templates"),
            fallback=shorts.get("title_template", "{track_title} #Shorts"),
            key=f"{track.slug}-short",
        )
        suffix = shorts.get("description_suffix", "\n\n#Shorts")
        description = f"{description}{suffix}"

    thumbnail_path = find_thumbnail(track, thumbnail_dir)
    title = fit_youtube_title(title_template.format(**values), video_type=video_type)
    tags = list(channel["default_tags"])
    category_id = str(channel["category_id"])
    override = metadata_override(config, track)
    if override:
        title = fit_youtube_title(str(override.get("title") or title), video_type=video_type)
        description = str(override.get("description") or description)
        raw_tags = override.get("tags")
        if isinstance(raw_tags, list):
            tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        category_id = str(override.get("category_id") or category_id)
    return VideoMetadata(
        title=title,
        description=description,
        tags=tags,
        category_id=category_id,
        made_for_kids=bool(channel.get("made_for_kids", False)),
        thumbnail_path=thumbnail_path,
    )


def fit_youtube_title(title: str, video_type: str = "normal") -> str:
    title = " ".join(str(title or "").split())
    if not title:
        title = "Untitled Video"
    if len(title) <= MAX_YOUTUBE_TITLE_LENGTH:
        return title

    suffix = " #Shorts" if video_type == "short" and "#shorts" in title.lower() else ""
    max_base_length = MAX_YOUTUBE_TITLE_LENGTH - len(suffix)
    base = title
    if suffix:
        base = re.sub(r"\s*#shorts\s*$", "", title, flags=re.IGNORECASE).strip()
    return f"{base[:max_base_length].rstrip()}{suffix}"


def effective_section(config: dict[str, Any], section: str) -> dict[str, Any]:
    base = dict(config.get(section, {}))
    active_account = config.get("active_account")
    account_overrides = config.get("account_overrides", {})
    override = account_overrides.get(active_account, {}).get(section, {})
    if isinstance(override, dict):
        base.update(override)
    return base


def metadata_override(config: dict[str, Any], track: Track) -> dict[str, Any]:
    active_account = config.get("active_account")
    overrides = config.get("metadata_overrides", {}).get(active_account, {})
    if not isinstance(overrides, dict):
        return {}
    value = overrides.get(track.audio_path.name) or overrides.get(track.slug) or {}
    return value if isinstance(value, dict) else {}


def humanize_title(value: str) -> str:
    cleaned = value.replace("_", " ").replace("-", " ").strip()
    return " ".join(word.capitalize() for word in cleaned.split())


def choose_template(templates: list[str] | None, fallback: str, key: str) -> str:
    if not templates:
        return fallback
    index = sum(key.encode("utf-8")) % len(templates)
    return templates[index]


def find_thumbnail(track: Track, thumbnail_dir: Path) -> Path | None:
    if not thumbnail_dir.exists():
        return None
    candidates = [
        candidate
        for extension in [".jpg", ".jpeg", ".png", ".webp"]
        if (candidate := thumbnail_dir / f"{track.audio_path.stem}{extension}").exists()
    ]
    if candidates:
        return max(candidates, key=lambda path: path.stat().st_mtime)

    audio_key = thumbnail_match_key(track.audio_path.stem)
    candidates = [
        candidate
        for candidate in thumbnail_dir.iterdir()
        if candidate.is_file()
        and candidate.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        and thumbnail_match_key(candidate.stem) == audio_key
    ]
    if candidates:
        return max(candidates, key=lambda path: path.stat().st_mtime)
    return None


def thumbnail_match_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("đ", "d").replace("Đ", "D").lower()
    value = re.sub(r"\.youtube$", "", value)
    value = re.sub(r"\b(remix|short|shorts|official|video|audio|lyrics?|karaoke)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())
