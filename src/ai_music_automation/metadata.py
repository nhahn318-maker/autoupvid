from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .media import Track


MAX_YOUTUBE_TITLE_LENGTH = 100


VIETNAMESE_BUDDHIST_ACCOUNTS = {"account1", "account2", "account3"}


def vietnamese_buddhist_short_templates() -> list[str]:
    return [
        "{track_title} - Câu Chuyện Nhân Quả #Shorts",
        "{track_title} - Bài Học Cuộc Sống #Shorts",
        "{track_title} - Truyện Ngắn Ý Nghĩa #Shorts",
        "{track_title} - Lời Phật Dạy #Shorts",
    ]


def vietnamese_buddhist_emotional_short_templates() -> list[str]:
    return [
        "Nếu Hôm Nay Bạn Đang Rất Mệt... | {track_title} #Shorts",
        "Nếu Bạn Luôn Là Người Chịu Thiệt... | {track_title} #Shorts",
        "Nếu Gần Đây Mọi Chuyện Đều Không Thuận... | {track_title} #Shorts",
        "Nếu Bạn Đang Lo Lắng Về Tương Lai... | {track_title} #Shorts",
        "Người Luôn Bình An Thường Làm Được Điều Này | {track_title} #Shorts",
        "Nếu Bạn Cảm Thấy Cô Đơn Trong Lòng... | {track_title} #Shorts",
    ]


def vietnamese_buddhist_short_description(title: str) -> str:
    clean_title = str(title or "").strip() or "Lời nhắc Phật pháp hôm nay"
    return (
        f"{clean_title}\n\n"
        "Câu chuyện được kể bằng giọng đọc AI, mang màu sắc Phật pháp đời sống, "
        "nhân quả, phước lành và những bài học giúp tâm nhẹ hơn.\n\n"
        "Mong bạn nghe chậm lại, gieo thêm một ý nghĩ thiện lành và tìm thấy bình an "
        "trong khoảnh khắc này."
    )


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
    active_account = str(config.get("active_account") or "").strip()
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
        short_templates = shorts.get("title_templates")
        content_driven_short_title = content_driven_short_title_for(track, config)
        content_driven_short_description = content_driven_short_description_for(track, config)
        if active_account in VIETNAMESE_BUDDHIST_ACCOUNTS:
            short_templates = vietnamese_buddhist_short_templates()
        if content_driven_short_title:
            values["track_title"] = content_driven_short_title
            title_template = choose_template(
                templates=short_templates,
                fallback=shorts.get("title_template", "{track_title} #Shorts"),
                key=f"{track.slug}-short",
            )
        else:
            if active_account in VIETNAMESE_BUDDHIST_ACCOUNTS:
                short_templates = vietnamese_buddhist_emotional_short_templates()
            title_template = choose_template(
                templates=short_templates,
                fallback=shorts.get("title_template", "{track_title} #Shorts"),
                key=f"{track.slug}-short",
            )
        suffix = shorts.get("description_suffix", "\n\n#Shorts")
        if active_account in VIETNAMESE_BUDDHIST_ACCOUNTS:
            description = content_driven_short_description or vietnamese_buddhist_short_description(values["track_title"])
        else:
            description = content_driven_short_description or description
        description = f"{content_driven_short_description or description}{suffix}"

    thumbnail_path = find_thumbnail(track, thumbnail_dir)
    title = fit_youtube_title(title_template.format(**values), video_type=video_type)
    tags = list(channel["default_tags"])
    category_id = str(channel["category_id"])
    if video_type == "short" and active_account in VIETNAMESE_BUDDHIST_ACCOUNTS:
        tags = ["phat phap", "loi phat day", "nhan qua", "bai hoc cuoc song", "binh an"]
        category_id = "22"
    override = metadata_override(config, track)
    if override:
        title = fit_youtube_title(str(override.get("title") or title), video_type=video_type)
        description = str(override.get("description") or description)
        raw_tags = override.get("tags")
        if isinstance(raw_tags, list):
            tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        category_id = str(override.get("category_id") or category_id)
    if video_type == "short" and active_account in VIETNAMESE_BUDDHIST_ACCOUNTS and looks_like_bolero_metadata(description):
        description = f"{vietnamese_buddhist_short_description(values['track_title'])}{suffix}"
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


def content_driven_short_title_for(track: Track, config: dict[str, Any]) -> str:
    active_account = str(config.get("active_account") or "").strip()
    if active_account not in VIETNAMESE_BUDDHIST_ACCOUNTS:
        return ""
    title_path = track.audio_path.with_suffix(".title.txt")
    if not title_path.exists():
        return ""
    title = str(track.title or "").strip()
    if not title:
        return ""
    normalized_stem = normalize_title_source(track.audio_path.stem)
    normalized_title = normalize_title_source(title)
    if normalized_title == normalized_stem:
        return ""
    return title


def looks_like_bolero_metadata(value: str) -> bool:
    normalized = normalize_title_source(value)
    return any(token in normalized for token in ("bolero", "nhac vang", "nhac tru tinh", "ca khuc"))


def content_driven_short_description_for(track: Track, config: dict[str, Any]) -> str:
    active_account = str(config.get("active_account") or "").strip()
    if active_account not in VIETNAMESE_BUDDHIST_ACCOUNTS:
        return ""
    markdown_path = fullauto_story_markdown_path_for(track, config)
    if not markdown_path or not markdown_path.exists():
        return ""
    try:
        markdown = markdown_path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    match = re.search(
        r"^## Description\s*(.*?)\s*(?=^## |\Z)",
        markdown,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if not match:
        return ""
    description = " ".join(line.strip() for line in match.group(1).splitlines() if line.strip())
    return description.strip()


def fullauto_story_markdown_path_for(track: Track, config: dict[str, Any]) -> Path | None:
    fullauto = config.get("fullauto", {}) or {}
    if not isinstance(fullauto, dict):
        return None
    draft_dir_value = str(fullauto.get("draft_dir") or "").strip()
    if not draft_dir_value:
        return None
    draft_dir = Path(draft_dir_value)
    if not draft_dir.exists():
        return None
    for draft_path in sorted(draft_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(draft_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("audio") or "").strip() != track.audio_path.name:
            continue
        markdown_name = str(data.get("markdown") or "").strip()
        if markdown_name:
            markdown_path = draft_dir / Path(markdown_name).name
            if markdown_path.exists():
                return markdown_path
        fallback_markdown = draft_path.with_suffix(".md")
        if fallback_markdown.exists():
            return fallback_markdown
    return None


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


def normalize_title_source(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.replace("đ", "d").replace("Đ", "D").lower()
    normalized = re.sub(r"\d{8,}", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


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
