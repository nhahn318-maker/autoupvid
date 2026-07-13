from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MediaQAReport:
    passed: bool
    duration_seconds: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    sample_lufs: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_media(
    media_path: Path,
    *,
    expected_duration_seconds: float | None = None,
    min_width: int = 640,
    min_height: int = 360,
    sample_audio: bool = True,
) -> MediaQAReport:
    notes: list[str] = []
    if not media_path.exists() or media_path.stat().st_size < 1024:
        return MediaQAReport(False, notes=["Output video is missing or empty."])

    try:
        probe = _ffprobe(media_path)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        return MediaQAReport(False, notes=[f"FFprobe failed: {exc}"])

    streams = probe.get("streams") if isinstance(probe, dict) else []
    streams = streams if isinstance(streams, list) else []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    duration = _number((probe.get("format") or {}).get("duration"))
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    fps = _parse_rate(str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"))
    has_video = bool(video)
    has_audio = bool(audio)

    if not has_video:
        notes.append("Video stream is missing.")
    if not has_audio:
        notes.append("Audio stream is missing.")
    if duration <= 0:
        notes.append("Video duration is invalid.")
    if has_video and (width < min_width or height < min_height):
        notes.append(f"Resolution is too small: {width}x{height}.")
    if fps <= 0:
        notes.append("Frame rate is invalid.")
    if expected_duration_seconds and expected_duration_seconds > 0:
        delta = abs(duration - expected_duration_seconds)
        tolerance = max(3.0, expected_duration_seconds * 0.025)
        if delta > tolerance:
            notes.append(
                f"Duration differs from expected audio by {delta:.1f}s "
                f"({duration:.1f}s vs {expected_duration_seconds:.1f}s)."
            )

    sample_lufs = _sample_loudness(media_path, duration) if sample_audio and has_audio else None
    if sample_lufs is not None:
        if sample_lufs < -35:
            notes.append(f"Audio is probably too quiet ({sample_lufs:.1f} LUFS sample).")
        elif sample_lufs > -10:
            notes.append(f"Audio is probably too loud ({sample_lufs:.1f} LUFS sample).")

    blocking = [
        note
        for note in notes
        if not note.startswith("Audio is probably")
    ]
    return MediaQAReport(
        passed=not blocking,
        duration_seconds=duration,
        width=width,
        height=height,
        fps=fps,
        has_video=has_video,
        has_audio=has_audio,
        sample_lufs=sample_lufs,
        notes=notes or ["Final media QA passed."],
    )


def validate_media_for_upload(media_path: Path) -> MediaQAReport:
    report = inspect_media(media_path)
    report_path = media_path.with_suffix(media_path.suffix + ".qa.json")
    report_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not report.passed:
        raise RuntimeError("Final media QA failed: " + "; ".join(report.notes))
    return report


def inspect_subtitle(subtitle_path: Path | None, expected_duration_seconds: float) -> list[str]:
    if subtitle_path is None or not subtitle_path.exists():
        return ["Subtitle file is missing."]
    try:
        text = subtitle_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return [f"Subtitle file cannot be read: {exc}"]
    timestamps = re.findall(
        r"(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3}\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3})",
        text,
    )
    if not timestamps:
        return ["Subtitle file has no valid timestamps."]
    last_end = _subtitle_seconds(timestamps[-1])
    if expected_duration_seconds > 0 and abs(last_end - expected_duration_seconds) > max(
        8.0, expected_duration_seconds * 0.08
    ):
        return [
            f"Subtitle ends at {last_end:.1f}s but audio ends at {expected_duration_seconds:.1f}s."
        ]
    return []


def _ffprobe(media_path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(media_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("FFprobe did not return an object")
    return value


def _sample_loudness(media_path: Path, duration: float) -> float | None:
    # A short center sample catches missing or extreme audio without scanning a
    # 90-minute upload. EBU R128 writes its summary to stderr.
    start = max(0.0, duration * 0.45)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}",
                "-t", "20", "-i", str(media_path), "-vn",
                "-filter_complex", "ebur128=peak=true", "-f", "null", "-",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    matches = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s+LUFS", result.stderr)
    return float(matches[-1]) if matches else None


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_rate(value: str) -> float:
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / max(float(denominator), 1.0)
    except (ValueError, ZeroDivisionError):
        return _number(value)


def _subtitle_seconds(value: str) -> float:
    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
    except ValueError:
        return 0.0
    return 0.0
