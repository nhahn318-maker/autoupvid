from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .media import Track, probe_duration_seconds


def render_video(
    track: Track,
    output_dir: Path,
    render_config: dict[str, Any],
    suffix: str = "",
    max_duration_seconds: int | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{track.slug}{suffix}.mp4"
    source_duration = probe_duration_seconds(track.audio_path)
    duration = min(source_duration, max_duration_seconds) if max_duration_seconds else source_duration
    width, height = parse_resolution(render_config.get("resolution", "1920x1080"))
    fps = int(render_config.get("fps", 30))

    vf = build_video_filter(
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        zoom_effect=bool(render_config.get("zoom_effect", True)),
    )
    vf = decorate_video_filter(
        vf,
        track=track,
        title=track.title,
        width=width,
        height=height,
        duration=duration,
        render_config=render_config,
    )

    if len(track.image_paths) > 1:
        return render_slideshow_video(
            track=track,
            output_path=output_path,
            render_config=render_config,
            width=width,
            height=height,
            fps=fps,
            duration=duration,
        )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-i",
        str(track.image_path),
        "-i",
        str(track.audio_path),
        "-vf",
        vf,
        "-t",
        f"{duration:.3f}",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-b:v",
        str(render_config.get("video_bitrate", "4500k")),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        str(render_config.get("audio_bitrate", "192k")),
        "-shortest",
        str(output_path),
    ]
    subprocess.run(command, check=True)
    return output_path


def render_slideshow_video(
    track: Track,
    output_path: Path,
    render_config: dict[str, Any],
    width: int,
    height: int,
    fps: int,
    duration: float,
) -> Path:
    images = list(track.image_paths)
    segment_duration = duration / len(images)
    inputs: list[str] = []
    for image_path in images:
        inputs.extend([
            "-loop",
            "1",
            "-t",
            f"{segment_duration:.3f}",
            "-i",
            str(image_path),
        ])
    audio_input_index = len(images)
    inputs.extend(["-i", str(track.audio_path)])

    filters = []
    labels = []
    for index, _ in enumerate(images):
        label = f"v{index}"
        image_filter = build_video_filter(
            width=width,
            height=height,
            fps=fps,
            duration=segment_duration,
            zoom_effect=bool(render_config.get("zoom_effect", True)),
        )
        filters.append(
            f"[{index}:v]{image_filter},{segment_transition_filter(segment_duration, render_config)},"
            f"setsar=1,trim=duration={segment_duration:.3f},setpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")
    final_filter = decorate_video_filter(
        "format=yuv420p",
        track=track,
        title=track.title,
        width=width,
        height=height,
        duration=duration,
        render_config=render_config,
    )
    filters.append(f"{''.join(labels)}concat=n={len(images)}:v=1:a=0,{final_filter}[vout]")

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[vout]",
        "-map",
        f"{audio_input_index}:a",
        "-t",
        f"{duration:.3f}",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-b:v",
        str(render_config.get("video_bitrate", "4500k")),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        str(render_config.get("audio_bitrate", "192k")),
        "-shortest",
        str(output_path),
    ]
    subprocess.run(command, check=True)
    return output_path


def parse_resolution(value: str) -> tuple[int, int]:
    width, height = value.lower().split("x", maxsplit=1)
    return int(width), int(height)


def build_video_filter(
    width: int,
    height: int,
    fps: int,
    duration: float,
    zoom_effect: bool,
) -> str:
    total_frames = max(1, int(duration * fps))
    scale_crop = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}"
    )
    if not zoom_effect:
        return scale_crop

    # Slow Ken Burns movement. The tiny zoom avoids a static-image feel.
    zoom = min(1.18, 1.0 + (duration / 1800.0))
    return (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},"
        f"zoompan=z='min(zoom+0.00025,{zoom:.4f})':"
        f"d={total_frames}:s={width}x{height}:fps={fps}"
    )


def decorate_video_filter(
    base_filter: str,
    track: Track,
    title: str,
    width: int,
    height: int,
    duration: float,
    render_config: dict[str, Any],
) -> str:
    filters = [base_filter]
    if bool(render_config.get("color_grade", True)):
        filters.append("eq=contrast=1.04:saturation=1.08")
        filters.append("vignette=PI/5")
    if bool(render_config.get("fade_effect", True)) and duration > 2.0:
        filters.append("fade=t=in:st=0:d=0.6")
        filters.append(f"fade=t=out:st={max(0.0, duration - 0.8):.3f}:d=0.8")
    if bool(render_config.get("title_overlay", True)):
        font_size = int(render_config.get("title_font_size") or max(22, height // 44))
        y_offset = max(54, height // 10)
        text = escape_drawtext(title)
        fontfile = drawtext_fontfile()
        filters.append(
            "drawtext="
            f"{fontfile}"
            f"text='{text}':"
            f"fontsize={font_size}:"
            "fontcolor=white@0.94:"
            "box=1:"
            "boxcolor=black@0.42:"
            "boxborderw=12:"
            "x=(w-text_w)/2:"
            f"y=h-text_h-{y_offset}:"
            "enable='between(t,0,7)'"
        )
    if bool(render_config.get("subtitle_overlay", True)):
        subtitle_filter = build_subtitle_filter(track, height, duration, render_config)
        if subtitle_filter:
            filters.append(subtitle_filter)
    return ",".join(filters)


def segment_transition_filter(duration: float, render_config: dict[str, Any]) -> str:
    if not bool(render_config.get("transition_effect", True)) or duration <= 2.0:
        return "format=yuv420p"
    fade_duration = min(0.7, duration / 5)
    return (
        "format=yuv420p,"
        f"fade=t=in:st=0:d={fade_duration:.3f},"
        f"fade=t=out:st={max(0.0, duration - fade_duration):.3f}:d={fade_duration:.3f}"
    )


def escape_drawtext(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("%", "\\%")
    )


def drawtext_fontfile() -> str:
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    if not font_path.exists():
        return ""
    return "fontfile='C\\:/Windows/Fonts/arial.ttf':"


def build_subtitle_filter(
    track: Track,
    height: int,
    duration: float,
    render_config: dict[str, Any],
) -> str:
    srt_path = track.audio_path.with_suffix(".auto.srt")
    if (
        bool(render_config.get("use_synced_subtitles", True))
        and srt_path.exists()
        and Path(f"{srt_path}.synced").exists()
    ):
        return subtitle_style_filter(srt_path, height, render_config)

    transcript_path = track.audio_path.with_suffix(".txt")
    if not transcript_path.exists() or duration <= 1:
        return ""
    text = transcript_path.read_text(encoding="utf-8-sig").strip()
    chunks = transcript_chunks(
        text,
        int(render_config.get("subtitle_words_per_chunk", 18)),
        int(render_config.get("subtitle_max_chars_per_chunk", 82)),
    )
    if not chunks:
        return ""

    usable_duration = max(1.0, duration - 1.0)
    chunk_duration = max(1.8, usable_duration / len(chunks))
    entries = []
    for index, chunk in enumerate(chunks[: int(duration / 1.2) + 2]):
        start = min(duration - 0.3, 0.5 + (index * chunk_duration))
        end = min(duration - 0.1, start + chunk_duration + 0.25)
        if end <= start:
            continue
        entries.append(f"{len(entries) + 1}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{chunk}\n")
    if not entries:
        return ""
    srt_path.write_text("\n".join(entries), encoding="utf-8")
    return subtitle_style_filter(srt_path, height, render_config)


def subtitle_style_filter(srt_path: Path, height: int, render_config: dict[str, Any]) -> str:
    font_size = int(render_config.get("subtitle_font_size") or max(18, height // 66))
    margin_v = int(render_config.get("subtitle_margin_v") or max(30, height // 16))
    style = (
        "FontName=Arial,"
        f"FontSize={font_size},"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&HAA000000,"
        "BackColour=&H99000000,"
        "BorderStyle=3,"
        "Outline=1,"
        "Shadow=0,"
        "Alignment=2,"
        f"MarginV={margin_v}"
    )
    return f"subtitles=filename='{escape_filter_path(srt_path)}':force_style='{style}'"


def transcript_chunks(text: str, words_per_chunk: int, max_chars_per_chunk: int = 82) -> list[str]:
    words_per_chunk = max(4, words_per_chunk)
    max_chars_per_chunk = max(24, max_chars_per_chunk)
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    sentences = re.split(r"(?<=[.!?。！？])\s+", cleaned)
    chunks: list[str] = []
    for sentence in sentences:
        words = sentence.split()
        current: list[str] = []
        for word in words:
            candidate = " ".join([*current, word])
            if current and (len(current) >= words_per_chunk or len(candidate) > max_chars_per_chunk):
                chunks.append(" ".join(current))
                current = []
            current.append(word)
        if current:
            chunks.append(" ".join(current))
    return chunks


def format_srt_time(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def escape_filter_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    value = value.replace(":", "\\:")
    value = value.replace("'", "\\'")
    return value
