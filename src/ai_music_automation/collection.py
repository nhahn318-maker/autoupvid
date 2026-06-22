from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .media import Track, probe_duration_seconds, slugify


def source_filters(collection_config: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    collection_config = collection_config or {}
    raw_include = collection_config.get("source_include") or []
    raw_exclude = collection_config.get("source_exclude") or []
    if isinstance(raw_include, str):
        raw_include = [raw_include]
    if isinstance(raw_exclude, str):
        raw_exclude = [raw_exclude]
    return (
        [str(value).lower() for value in raw_include],
        [str(value).lower() for value in raw_exclude],
    )


def collection_candidates(
    tracks: list[Track],
    output_dir: Path,
    size: int,
    collection_config: dict[str, Any] | None = None,
) -> list[Path]:
    include, exclude = source_filters(collection_config)
    videos = []
    for track in tracks:
        video_path = output_dir / f"{track.slug}.mp4"
        source_key = f"{track.slug} {track.audio_path.name}".lower()
        if video_path.exists() and source_matches(source_key, include, exclude):
            videos.append(video_path)
    return videos


def duration_collection_candidates(
    tracks: list[Track],
    output_dir: Path,
    target_duration_seconds: float,
    collection_config: dict[str, Any] | None = None,
) -> tuple[list[Path], float]:
    include, exclude = source_filters(collection_config)
    videos = []
    total_duration = 0.0
    for track in tracks:
        video_path = output_dir / f"{track.slug}.mp4"
        source_key = f"{track.slug} {track.audio_path.name}".lower()
        if not video_path.exists() or not source_matches(source_key, include, exclude):
            continue
        try:
            total_duration += probe_duration_seconds(video_path)
        except subprocess.CalledProcessError:
            continue
        videos.append(video_path)
        if total_duration >= target_duration_seconds:
            break
    return videos, total_duration


def create_collection(
    tracks: list[Track],
    output_dir: Path,
    state_dir: Path,
    collection_config: dict[str, Any],
    use_duration_target: bool = True,
) -> tuple[Path, list[Path]]:
    target_minutes = float(collection_config.get("target_duration_minutes", 0) or 0)
    if use_duration_target and target_minutes > 0:
        target_seconds = target_minutes * 60
        videos, total_duration = duration_collection_candidates(
            tracks=tracks,
            output_dir=output_dir,
            target_duration_seconds=target_seconds,
            collection_config=collection_config,
        )
        if total_duration < target_seconds:
            found_minutes = round(total_duration / 60, 1)
            raise ValueError(f"Need about {target_minutes:g} minutes of rendered normal videos, found {found_minutes:g}.")
    else:
        size = int(collection_config.get("size", 5))
        videos = collection_candidates(tracks, output_dir, size, collection_config)
        if len(videos) < size:
            raise ValueError(f"Need {size} rendered normal videos, found {len(videos)}.")
    source_audio = [
        track.audio_path
        for track in tracks
        if output_dir / f"{track.slug}.mp4" in videos
    ]

    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    prefix = slugify(collection_config.get("output_prefix", "bolero-remix-tuyen-tap"))
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = output_dir / f"{prefix}-{timestamp}.mp4"
    return concat_videos(videos, output_path, state_dir / f"collection-{timestamp}.txt"), source_audio


def create_mega_collection(
    videos: list[Path],
    output_dir: Path,
    state_dir: Path,
    collection_config: dict[str, Any],
) -> Path:
    if not 2 <= len(videos) <= 4:
        raise ValueError("Choose 2 to 4 collection files.")

    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    prefix = slugify(collection_config.get("mega_output_prefix", "bolero-remix-dai-tuyen-tap"))
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = output_dir / f"{prefix}-{timestamp}.mp4"
    return concat_videos(videos, output_path, state_dir / f"mega-collection-{timestamp}.txt")


def collected_audio_from_manifests(
    tracks: list[Track],
    output_dir: Path,
    state_dir: Path,
    collection_config: dict[str, Any] | None = None,
) -> list[Path]:
    include, exclude = source_filters(collection_config)
    video_to_audio = {}
    for track in tracks:
        video_path = (output_dir / f"{track.slug}.mp4").resolve()
        source_key = f"{track.slug} {track.audio_path.name}".lower()
        if source_matches(source_key, include, exclude):
            video_to_audio[video_path] = track.audio_path

    collected = []
    seen = set()
    for manifest in sorted(state_dir.glob("collection-*.txt")):
        for video_path in parse_concat_manifest(manifest):
            audio_path = video_to_audio.get(video_path.resolve())
            if audio_path and audio_path.resolve() not in seen:
                collected.append(audio_path)
                seen.add(audio_path.resolve())
    return collected


def concat_videos(videos: list[Path], output_path: Path, concat_file: Path) -> Path:
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


def parse_concat_manifest(path: Path) -> list[Path]:
    if not path.exists():
        return []
    videos = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("file "):
            continue
        value = line.removeprefix("file ").strip()
        if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
            value = value[1:-1].replace("'\\''", "'")
        videos.append(Path(value))
    return videos


def source_matches(value: str, include: list[str], exclude: list[str]) -> bool:
    if include and not any(pattern in value for pattern in include):
        return False
    return not any(pattern in value for pattern in exclude)
