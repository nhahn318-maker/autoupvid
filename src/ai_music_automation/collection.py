from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .media import Track, slugify


def collection_candidates(tracks: list[Track], output_dir: Path, size: int) -> list[Path]:
    videos = []
    for track in tracks:
        video_path = output_dir / f"{track.slug}.mp4"
        if video_path.exists():
            videos.append(video_path)
        if len(videos) == size:
            break
    return videos


def create_collection(
    tracks: list[Track],
    output_dir: Path,
    state_dir: Path,
    collection_config: dict[str, Any],
) -> Path:
    size = int(collection_config.get("size", 5))
    videos = collection_candidates(tracks, output_dir, size)
    if len(videos) < size:
        raise ValueError(f"Need {size} rendered normal videos, found {len(videos)}.")

    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    prefix = slugify(collection_config.get("output_prefix", "bolero-remix-tuyen-tap"))
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = output_dir / f"{prefix}-{timestamp}.mp4"
    concat_file = state_dir / f"collection-{timestamp}.txt"
    concat_file.write_text(
        "\n".join(f"file '{escape_concat_path(video)}'" for video in videos),
        encoding="utf-8",
    )

    copy_command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c",
        "copy",
        str(output_path),
    ]
    fallback_command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_path),
    ]

    try:
        subprocess.run(copy_command, check=True)
    except subprocess.CalledProcessError:
        subprocess.run(fallback_command, check=True)

    return output_path


def escape_concat_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "'\\''")
