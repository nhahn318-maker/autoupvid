from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class Track:
    audio_path: Path
    image_paths: tuple[Path, ...]
    title: str

    @property
    def slug(self) -> str:
        return slugify(self.title)

    @property
    def image_path(self) -> Path:
        return self.image_paths[0]


def slugify(value: str, max_length: int = 80) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9\-_\s]+", "", value)
    value = re.sub(r"\s+", "-", value)
    value = value.strip("-") or "track"
    if len(value) <= max_length:
        return value
    return value[:max_length].rstrip("-_") or "track"


def list_files(directory: Path, extensions: set[str]) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in extensions
    )


def discover_tracks(audio_dir: Path, image_dir: Path) -> list[Track]:
    audio_files = list_files(audio_dir, AUDIO_EXTENSIONS)
    image_files = list_files(image_dir, IMAGE_EXTENSIONS)

    tracks: list[Track] = []
    for index, audio in enumerate(audio_files):
        matching_images = find_matching_images(audio, image_files)
        if not matching_images and image_files:
            newest_images = sorted(image_files, key=lambda path: path.stat().st_mtime, reverse=True)
            matching_images = newest_images[:5]
        if not matching_images:
            continue
        tracks.append(Track(audio_path=audio, image_paths=tuple(matching_images[:5]), title=track_title(audio)))

    return tracks


def find_matching_image(audio: Path, images: list[Path]) -> Path | None:
    matches = find_matching_images(audio, images)
    return matches[0] if matches else None


def find_matching_images(audio: Path, images: list[Path]) -> list[Path]:
    audio_slug = slugify(audio.stem)
    matches = [
        image
        for image in images
        if slugify(image.stem) == audio_slug
        or slugify(image.stem).startswith(f"{audio_slug}-")
        or slugify(image.stem).startswith(f"{audio_slug}_")
    ]
    return sorted(matches, key=lambda path: slugify(path.stem))


def track_title(audio: Path) -> str:
    title_path = audio.with_suffix(".title.txt")
    if title_path.exists():
        title = title_path.read_text(encoding="utf-8-sig").strip()
        if title:
            return title
    return audio.stem


def probe_duration_seconds(media_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(media_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])
