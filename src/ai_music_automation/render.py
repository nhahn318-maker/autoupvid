from __future__ import annotations

import re
import subprocess
import math
import tempfile
from pathlib import Path
from typing import Any

from .collection import escape_concat_path
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
    preset = str(render_config.get("encode_preset", "medium"))
    ambient_overlay = overlay_config(render_config.get("ambient_overlay"))
    subscribe_overlay = overlay_config(render_config.get("subscribe_overlay"))
    intro_audio = intro_audio_config(render_config)
    background_ambience = background_ambience_config(render_config)
    low_bed = low_bed_config(render_config)

    if len(track.image_paths) > 1:
        fixed_segment_seconds = float(render_config.get("image_segment_seconds") or 0)
        if fixed_segment_seconds > 0:
            return render_fixed_interval_slideshow_video(
                track=track,
                output_path=output_path,
                render_config=render_config,
                width=width,
                height=height,
                fps=fps,
                duration=duration,
                preset=preset,
                ambient_overlay=ambient_overlay,
                subscribe_overlay=subscribe_overlay,
                intro_audio=intro_audio,
                background_ambience=background_ambience,
                low_bed=low_bed,
                segment_seconds=fixed_segment_seconds,
            )
        return render_slideshow_video(
            track=track,
            output_path=output_path,
            render_config=render_config,
            width=width,
            height=height,
            fps=fps,
            duration=duration,
            preset=preset,
            ambient_overlay=ambient_overlay,
            subscribe_overlay=subscribe_overlay,
            intro_audio=intro_audio,
        )

    if ambient_overlay or subscribe_overlay or intro_audio:
        return render_single_image_video(
            track=track,
            output_path=output_path,
            render_config=render_config,
            width=width,
            height=height,
            fps=fps,
            duration=duration,
            preset=preset,
            ambient_overlay=ambient_overlay,
            subscribe_overlay=subscribe_overlay,
            intro_audio=intro_audio,
        )

    vf = build_video_filter(
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        zoom_effect=bool(render_config.get("zoom_effect", True)),
        render_config=render_config,
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
        preset,
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
    preset: str,
    ambient_overlay: dict[str, Any] | None,
    subscribe_overlay: dict[str, Any] | None,
    intro_audio: dict[str, Any] | None,
) -> Path:
    images = list(track.image_paths)
    segment_duration = duration / len(images)
    intro_duration = float(intro_audio["duration"]) if intro_audio else 0.0
    total_duration = duration + intro_duration
    inputs: list[str] = []
    for index, image_path in enumerate(images):
        image_duration = segment_duration + intro_duration if intro_audio and index == 0 else segment_duration
        inputs.extend([
            "-loop",
            "1",
            "-t",
            f"{image_duration:.3f}",
            "-i",
            str(image_path),
        ])
    next_input_index = len(images)
    intro_audio_index: int | None = None
    if intro_audio:
        intro_audio_index = next_input_index
        inputs.extend([
            "-t",
            f"{float(intro_audio.get('trim_seconds', 5.0)):.3f}",
            "-i",
            str(intro_audio["path"]),
        ])
        next_input_index += 1
    narration_audio_index = next_input_index
    inputs.extend(["-i", str(track.audio_path)])
    overlay_inputs, overlay_indexes = build_overlay_inputs(
        start_index=narration_audio_index + 1,
        ambient_overlay=ambient_overlay,
        subscribe_overlay=subscribe_overlay,
    )
    inputs.extend(overlay_inputs)

    filters = []
    labels = []
    if intro_audio:
        intro_label = "vintro"
        intro_filter = build_video_filter(
            width=width,
            height=height,
            fps=fps,
            duration=intro_duration,
            zoom_effect=bool(render_config.get("zoom_effect", True)),
            render_config=render_config,
        )
        filters.append(
            f"[0:v]{intro_filter},setsar=1,trim=duration={intro_duration:.3f},setpts=PTS-STARTPTS[{intro_label}]"
        )
        labels.append(f"[{intro_label}]")
    for index, _ in enumerate(images):
        label = f"v{index}"
        image_filter = build_video_filter(
            width=width,
            height=height,
            fps=fps,
            duration=segment_duration,
            zoom_effect=bool(render_config.get("zoom_effect", True)),
            render_config=render_config,
        )
        filters.append(
            f"[{index}:v]{image_filter},{segment_transition_filter(segment_duration, render_config)},"
            f"setsar=1,trim=duration={segment_duration:.3f},setpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")
    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[base]")
    filters.extend(
        build_overlay_filter_steps(
            input_label="[base]",
            output_label="[vout]",
            width=width,
            height=height,
            duration=total_duration,
            render_config=render_config,
            track=track,
            ambient_overlay=ambient_overlay,
            subscribe_overlay=subscribe_overlay,
            overlay_indexes=overlay_indexes,
        )
    )
    if intro_audio_index is not None:
        filters.append(f"[{intro_audio_index}:a][{narration_audio_index}:a]concat=n=2:v=0:a=1[aout]")

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
        "[aout]" if intro_audio_index is not None else f"{narration_audio_index}:a",
        "-t",
        f"{total_duration:.3f}",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        preset,
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


def render_fixed_interval_slideshow_video(
    track: Track,
    output_path: Path,
    render_config: dict[str, Any],
    width: int,
    height: int,
    fps: int,
    duration: float,
    preset: str,
    ambient_overlay: dict[str, Any] | None,
    subscribe_overlay: dict[str, Any] | None,
    intro_audio: dict[str, Any] | None,
    background_ambience: dict[str, Any] | None,
    low_bed: dict[str, Any] | None,
    segment_seconds: float,
) -> Path:
    images = list(track.image_paths)
    intro_duration = float(intro_audio["duration"]) if intro_audio else 0.0
    total_duration = duration + intro_duration
    segment_seconds = max(0.5, segment_seconds)
    transition_seconds = max(0.0, float(render_config.get("image_transition_seconds") or 0))
    if transition_seconds > 0 and len(images) > 1:
        try:
            return render_loop_clip_slideshow_video(
                track=track,
                output_path=output_path,
                render_config=render_config,
                width=width,
                height=height,
                fps=fps,
                duration=duration,
                preset=preset,
                ambient_overlay=ambient_overlay,
                subscribe_overlay=subscribe_overlay,
                intro_audio=intro_audio,
                background_ambience=background_ambience,
                low_bed=low_bed,
                segment_seconds=segment_seconds,
                transition_seconds=min(transition_seconds, segment_seconds / 2),
            )
        except subprocess.CalledProcessError:
            fallback_config = dict(render_config)
            fallback_config["image_transition_seconds"] = 0
            return render_fixed_interval_slideshow_video(
                track=track,
                output_path=output_path,
                render_config=fallback_config,
                width=width,
                height=height,
                fps=fps,
                duration=duration,
                preset=preset,
                ambient_overlay=ambient_overlay,
                subscribe_overlay=subscribe_overlay,
                intro_audio=intro_audio,
                background_ambience=background_ambience,
                low_bed=low_bed,
                segment_seconds=segment_seconds,
            )
    segment_count = max(1, math.ceil(total_duration / segment_seconds))
    sequence = [images[index % len(images)] for index in range(segment_count)]

    with tempfile.NamedTemporaryFile("w", suffix=".ffconcat", delete=False, encoding="utf-8") as manifest:
        manifest_path = Path(manifest.name)
        for image_path in sequence:
            manifest.write(f"file '{escape_concat_path(image_path)}'\n")
            manifest.write(f"duration {segment_seconds:.3f}\n")
        manifest.write(f"file '{escape_concat_path(sequence[-1])}'\n")

    intro_audio_index: int | None = 1 if intro_audio else None
    narration_audio_index = 2 if intro_audio else 1
    overlay_inputs, overlay_indexes = build_overlay_inputs(
        start_index=narration_audio_index + 1,
        ambient_overlay=ambient_overlay,
        subscribe_overlay=subscribe_overlay,
    )
    base_filter = (
        f"[0:v]fps={fps},"
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,"
        f"trim=duration={total_duration:.3f},setpts=PTS-STARTPTS[base]"
    )
    filters = [
        base_filter,
        *build_overlay_filter_steps(
            input_label="[base]",
            output_label="[vout]",
            width=width,
            height=height,
            duration=total_duration,
            render_config=render_config,
            track=track,
            ambient_overlay=ambient_overlay,
            subscribe_overlay=subscribe_overlay,
            overlay_indexes=overlay_indexes,
        ),
    ]
    if intro_audio_index is not None:
        filters.append(f"[{intro_audio_index}:a][{narration_audio_index}:a]concat=n=2:v=0:a=1[narration_full]")
        narration_label = "[narration_full]"
    else:
        narration_label = f"[{narration_audio_index}:a]"
    background_audio_index = narration_audio_index + 1 + len(overlay_indexes) if background_ambience else None
    low_bed_audio_index = narration_audio_index + 1 + len(overlay_indexes) + (1 if background_ambience else 0) if low_bed else None
    # Once an intro is prepended, narration_label points at the concatenated
    # audio stream. Keep the FFmpeg label syntax for later bed mixing.
    audio_map = narration_label
    if background_audio_index is not None:
        filters.extend(build_audio_bed_filters(
            base_audio_label=narration_label,
            narration_label=narration_label,
            bed_index=background_audio_index,
            duration=total_duration,
            config=background_ambience,
            label_prefix="ambience",
            output_label="[ambience_mix]",
        ))
        audio_map = "[ambience_mix]"
    if low_bed_audio_index is not None:
        filters.extend(build_audio_bed_filters(
            base_audio_label=audio_map,
            narration_label=narration_label,
            bed_index=low_bed_audio_index,
            duration=total_duration,
            config=low_bed,
            label_prefix="low_bed",
            output_label="[aout]",
        ))
        audio_map = "[aout]"
    elif intro_audio_index is not None and audio_map == narration_label:
        filters.append(f"{narration_label}anull[aout]")
        audio_map = "[aout]"

    command = [
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
        str(manifest_path),
    ]
    if intro_audio:
        command.extend([
            "-t",
            f"{float(intro_audio.get('trim_seconds', 5.0)):.3f}",
            "-i",
            str(intro_audio["path"]),
        ])
    command.extend([
        "-i",
        str(track.audio_path),
        *overlay_inputs,
        *background_ambience_inputs(background_ambience),
        *background_ambience_inputs(low_bed),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[vout]",
        "-map",
        audio_map,
        "-t",
        f"{total_duration:.3f}",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        preset,
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
    ])
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError:
        if ambient_overlay or subscribe_overlay:
            fallback_config = dict(render_config)
            fallback_config.pop("ambient_overlay", None)
            fallback_config.pop("subscribe_overlay", None)
            return render_fixed_interval_slideshow_video(
                track=track,
                output_path=output_path,
                render_config=fallback_config,
                width=width,
                height=height,
                fps=fps,
                duration=duration,
                preset=preset,
                ambient_overlay=None,
                subscribe_overlay=None,
                intro_audio=intro_audio,
                background_ambience=background_ambience,
                low_bed=low_bed,
                segment_seconds=segment_seconds,
            )
        raise
    finally:
        manifest_path.unlink(missing_ok=True)
    return output_path


def render_loop_clip_slideshow_video(
    track: Track,
    output_path: Path,
    render_config: dict[str, Any],
    width: int,
    height: int,
    fps: int,
    duration: float,
    preset: str,
    ambient_overlay: dict[str, Any] | None,
    subscribe_overlay: dict[str, Any] | None,
    intro_audio: dict[str, Any] | None,
    background_ambience: dict[str, Any] | None,
    low_bed: dict[str, Any] | None,
    segment_seconds: float,
    transition_seconds: float,
) -> Path:
    images = list(track.image_paths)
    intro_duration = float(intro_audio["duration"]) if intro_audio else 0.0
    total_duration = duration + intro_duration
    with tempfile.TemporaryDirectory(prefix="slideshow-loop-") as temp_dir:
        loop_clip = Path(temp_dir) / "loop.mp4"
        inputs: list[str] = []
        filters: list[str] = []
        for index, image_path in enumerate(images):
            inputs.extend(["-loop", "1", "-t", f"{segment_seconds:.3f}", "-i", str(image_path)])
            filters.append(
                f"[{index}:v]fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,trim=duration={segment_seconds:.3f},"
                f"setpts=PTS-STARTPTS[v{index}]"
            )

        current = "v0"
        for index in range(1, len(images)):
            output = f"x{index}"
            offset = max(0.0, index * (segment_seconds - transition_seconds))
            filters.append(
                f"[{current}][v{index}]xfade=transition=fade:duration={transition_seconds:.3f}:"
                f"offset={offset:.3f}[{output}]"
            )
            current = output
        cycle_duration = max(segment_seconds, len(images) * segment_seconds - (len(images) - 1) * transition_seconds)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                *inputs,
                "-filter_complex",
                ";".join(filters),
                "-map",
                f"[{current}]",
                "-t",
                f"{cycle_duration:.3f}",
                "-r",
                str(fps),
                "-c:v",
                "libx264",
                "-preset",
                preset,
                "-b:v",
                str(render_config.get("video_bitrate", "4500k")),
                "-pix_fmt",
                "yuv420p",
                str(loop_clip),
            ],
            check=True,
        )

        intro_audio_index: int | None = 1 if intro_audio else None
        narration_audio_index = 2 if intro_audio else 1
        overlay_inputs, overlay_indexes = build_overlay_inputs(
            start_index=narration_audio_index + 1,
            ambient_overlay=ambient_overlay,
            subscribe_overlay=subscribe_overlay,
        )
        base_filter = (
            f"[0:v]fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,trim=duration={total_duration:.3f},"
            "setpts=PTS-STARTPTS[base]"
        )
        final_filters = [
            base_filter,
            *build_overlay_filter_steps(
                input_label="[base]",
                output_label="[vout]",
                width=width,
                height=height,
                duration=total_duration,
                render_config=render_config,
                track=track,
                ambient_overlay=ambient_overlay,
                subscribe_overlay=subscribe_overlay,
                overlay_indexes=overlay_indexes,
            ),
        ]
        if intro_audio_index is not None:
            final_filters.append(f"[{intro_audio_index}:a][{narration_audio_index}:a]concat=n=2:v=0:a=1[narration_full]")
            narration_label = "[narration_full]"
        else:
            narration_label = f"[{narration_audio_index}:a]"
        background_audio_index = narration_audio_index + 1 + len(overlay_indexes) if background_ambience else None
        low_bed_audio_index = narration_audio_index + 1 + len(overlay_indexes) + (1 if background_ambience else 0) if low_bed else None
        # Once an intro is prepended, narration_label points at the concatenated
        # audio stream. Keep the FFmpeg label syntax for later bed mixing.
        audio_map = narration_label
        if background_audio_index is not None:
            final_filters.extend(build_audio_bed_filters(
                base_audio_label=narration_label,
                narration_label=narration_label,
                bed_index=background_audio_index,
                duration=total_duration,
                config=background_ambience,
                label_prefix="ambience",
                output_label="[ambience_mix]",
            ))
            audio_map = "[ambience_mix]"
        if low_bed_audio_index is not None:
            final_filters.extend(build_audio_bed_filters(
                base_audio_label=audio_map,
                narration_label=narration_label,
                bed_index=low_bed_audio_index,
                duration=total_duration,
                config=low_bed,
                label_prefix="low_bed",
                output_label="[aout]",
            ))
            audio_map = "[aout]"
        elif intro_audio_index is not None and audio_map == narration_label:
            final_filters.append(f"{narration_label}anull[aout]")
            audio_map = "[aout]"

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(loop_clip),
        ]
        if intro_audio:
            command.extend([
                "-t",
                f"{float(intro_audio.get('trim_seconds', 5.0)):.3f}",
                "-i",
                str(intro_audio["path"]),
            ])
        command.extend([
            "-i",
            str(track.audio_path),
            *overlay_inputs,
            *background_ambience_inputs(background_ambience),
            *background_ambience_inputs(low_bed),
            "-filter_complex",
            ";".join(final_filters),
            "-map",
            "[vout]",
            "-map",
            audio_map,
            "-t",
            f"{total_duration:.3f}",
            "-r",
            str(fps),
            "-c:v",
            "libx264",
            "-preset",
            preset,
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
        ])
        subprocess.run(command, check=True)
    return output_path


def render_single_image_video(
    track: Track,
    output_path: Path,
    render_config: dict[str, Any],
    width: int,
    height: int,
    fps: int,
    duration: float,
    preset: str,
    ambient_overlay: dict[str, Any] | None,
    subscribe_overlay: dict[str, Any] | None,
    intro_audio: dict[str, Any] | None,
) -> Path:
    intro_duration = float(intro_audio["duration"]) if intro_audio else 0.0
    total_duration = duration + intro_duration
    overlay_inputs, overlay_indexes = build_overlay_inputs(
        start_index=3 if intro_audio else 2,
        ambient_overlay=ambient_overlay,
        subscribe_overlay=subscribe_overlay,
    )
    base_filter = build_video_filter(
        width=width,
        height=height,
        fps=fps,
        duration=total_duration,
        zoom_effect=bool(render_config.get("zoom_effect", True)),
        render_config=render_config,
    )
    filters = [
        f"[0:v]{base_filter}[base]",
        *build_overlay_filter_steps(
            input_label="[base]",
            output_label="[vout]",
            width=width,
            height=height,
            duration=total_duration,
            render_config=render_config,
            track=track,
            ambient_overlay=ambient_overlay,
            subscribe_overlay=subscribe_overlay,
            overlay_indexes=overlay_indexes,
        ),
    ]
    if intro_audio:
        filters.append("[1:a][2:a]concat=n=2:v=0:a=1[aout]")

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
    ]
    if intro_audio:
        command.extend([
            "-t",
            f"{float(intro_audio.get('trim_seconds', 5.0)):.3f}",
            "-i",
            str(intro_audio["path"]),
        ])
    command.extend([
        "-i",
        str(track.audio_path),
        *overlay_inputs,
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[vout]",
        "-map",
        "[aout]" if intro_audio else "1:a",
        "-t",
        f"{total_duration:.3f}",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        preset,
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
    ])
    subprocess.run(command, check=True)
    return output_path


def parse_resolution(value: str) -> tuple[int, int]:
    width, height = value.lower().split("x", maxsplit=1)
    return int(width), int(height)


def overlay_config(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value.get("enabled"):
        return None
    path = Path(str(value.get("path", "")).strip())
    if not path.exists():
        return None
    config = dict(value)
    config["path"] = path
    return config


def intro_audio_config(render_config: dict[str, Any]) -> dict[str, Any] | None:
    path_value = str(render_config.get("intro_audio_path", "")).strip()
    if not path_value:
        return None
    path = Path(path_value)
    if not path.exists():
        return None
    duration = min(5.0, float(render_config.get("intro_audio_duration_seconds") or probe_duration_seconds(path)))
    if duration <= 0:
        return None
    trim_seconds = float(render_config.get("intro_audio_trim_seconds") or min(5.0, duration))
    return {"path": path, "duration": duration, "trim_seconds": max(0.0, trim_seconds)}


def background_ambience_config(render_config: dict[str, Any]) -> dict[str, Any] | None:
    if not bool(render_config.get("background_ambience_enabled", False)):
        return None
    path_value = str(render_config.get("background_ambience_path", "")).strip()
    if not path_value:
        return None
    path = Path(path_value)
    if not path.exists():
        return None
    volume = float(render_config.get("background_ambience_volume") or 0.04)
    duck_ratio = float(render_config.get("background_ambience_duck_ratio") or 8.0)
    duck_threshold = float(render_config.get("background_ambience_duck_threshold") or 0.035)
    return {
        "path": path,
        "volume": max(0.0, min(0.2, volume)),
        "duck_ratio": max(1.0, min(20.0, duck_ratio)),
        "duck_threshold": max(0.001, min(1.0, duck_threshold)),
    }


def low_bed_config(render_config: dict[str, Any]) -> dict[str, Any] | None:
    if not bool(render_config.get("low_bed_enabled", False)):
        return None
    path_value = str(render_config.get("low_bed_path", "")).strip()
    if not path_value:
        return None
    path = Path(path_value)
    if not path.exists():
        return None
    volume = float(render_config.get("low_bed_volume") or 0.018)
    duck_ratio = float(render_config.get("low_bed_duck_ratio") or 12.0)
    duck_threshold = float(render_config.get("low_bed_duck_threshold") or 0.03)
    tone_filter = bool(render_config.get("low_bed_tone_filter", False))
    return {
        "path": path,
        "volume": max(0.0, min(0.25, volume)),
        "duck_ratio": max(1.0, min(30.0, duck_ratio)),
        "duck_threshold": max(0.001, min(1.0, duck_threshold)),
        "tone_filter": tone_filter,
    }


def background_ambience_inputs(config: dict[str, Any] | None) -> list[str]:
    if not config:
        return []
    return ["-stream_loop", "-1", "-i", str(config["path"])]


def build_audio_bed_filters(
    base_audio_label: str,
    narration_label: str,
    bed_index: int,
    duration: float,
    config: dict[str, Any],
    label_prefix: str,
    output_label: str,
) -> list[str]:
    volume = float(config.get("volume") or 0.04)
    ratio = float(config.get("duck_ratio") or 8.0)
    threshold = float(config.get("duck_threshold") or 0.035)
    raw_label = f"[{label_prefix}_raw]"
    ducked_label = f"[{label_prefix}_ducked]"
    tone_chain = ""
    if bool(config.get("tone_filter", False)):
        tone_chain = "highpass=f=35,lowpass=f=6000,equalizer=f=220:t=q:w=1:g=8,equalizer=f=480:t=q:w=1.2:g=4,"
    filters = [
        (
            f"[{bed_index}:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,"
            f"{tone_chain}volume={volume:.4f}{raw_label}"
        )
    ]
    # A ratio of 1 means the bed should remain audible under narration. Avoid
    # creating a sidechain graph in that case; it also keeps the FFmpeg stream
    # labels valid when an intro audio track has been concatenated first.
    if ratio <= 1.0:
        filters.append(f"{base_audio_label}{raw_label}amix=inputs=2:duration=first:dropout_transition=2{output_label}")
        return filters
    filters.extend(
        [
            (
                f"{raw_label}{narration_label}sidechaincompress="
                f"threshold={threshold:.4f}:ratio={ratio:.2f}:attack=120:release=1200:makeup=1{ducked_label}"
            ),
            f"{base_audio_label}{ducked_label}amix=inputs=2:duration=first:dropout_transition=2{output_label}",
        ]
    )
    return filters


def build_background_ambience_filters(
    narration_label: str,
    ambience_index: int,
    duration: float,
    config: dict[str, Any],
) -> list[str]:
    return build_audio_bed_filters(
        base_audio_label=narration_label,
        narration_label=narration_label,
        bed_index=ambience_index,
        duration=duration,
        config=config,
        label_prefix="ambience",
        output_label="[aout]",
    )


def build_video_filter(
    width: int,
    height: int,
    fps: int,
    duration: float,
    zoom_effect: bool,
    render_config: dict[str, Any] | None = None,
) -> str:
    render_config = render_config or {}
    total_frames = max(1, int(duration * fps))
    scale_crop = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}"
    )
    if not zoom_effect:
        return scale_crop

    zoom_max = float(render_config.get("zoom_max") or min(1.18, 1.0 + (duration / 1800.0)))
    zoom_step = float(render_config.get("zoom_step") or 0.00025)
    dynamic_motion = bool(render_config.get("short_dynamic_effects", False))
    portrait_frame = height > width
    if dynamic_motion or portrait_frame:
        # Shorts need visible motion, but keep the frame stable: zoom straight into center.
        zoom_max = max(1.24, min(1.38, zoom_max))
        return (
            f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
            f"crop={width * 2}:{height * 2},"
            f"zoompan="
            f"z='min(1+on*{zoom_step:.6f},{zoom_max:.4f})':"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"d={total_frames}:s={width}x{height}:fps={fps}"
        )

    # Default long-form motion stays softer.
    zoom = min(1.18, max(1.03, zoom_max))
    return (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},"
        f"zoompan=z='min(pzoom+{zoom_step:.6f},{zoom:.4f})':"
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


def build_overlay_inputs(
    start_index: int,
    ambient_overlay: dict[str, Any] | None,
    subscribe_overlay: dict[str, Any] | None,
) -> tuple[list[str], dict[str, int]]:
    inputs: list[str] = []
    indexes: dict[str, int] = {}
    next_index = start_index
    for key, config in (("ambient", ambient_overlay), ("subscribe", subscribe_overlay)):
        if not config:
            continue
        path = Path(config["path"])
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            inputs.extend(["-loop", "1", "-i", str(path)])
        else:
            inputs.extend(["-stream_loop", "-1", "-i", str(path)])
        indexes[key] = next_index
        next_index += 1
    return inputs, indexes


def build_overlay_filter_steps(
    input_label: str,
    output_label: str,
    width: int,
    height: int,
    duration: float,
    render_config: dict[str, Any],
    track: Track,
    ambient_overlay: dict[str, Any] | None,
    subscribe_overlay: dict[str, Any] | None,
    overlay_indexes: dict[str, int],
) -> list[str]:
    steps: list[str] = []
    current_label = input_label
    current_name = "video"

    if ambient_overlay and "ambient" in overlay_indexes:
        ambient_input = f"[{overlay_indexes['ambient']}:v]"
        next_label = "[with_ambient]"
        blend_mode = str(ambient_overlay.get("blend_mode") or "alpha").lower()
        if blend_mode == "screen":
            # FFmpeg's screen blend can shift YUV footage toward magenta. Keep both
            # inputs in planar RGB so grayscale mist does not alter the base hue.
            steps.append(f"{current_label}format=gbrp[screen_base]")
            steps.append(f"{ambient_input}scale={width}:{height},format=gbrp[ambient]")
            steps.append(
                f"[screen_base][ambient]blend=all_mode=screen:"
                f"all_opacity={overlay_opacity(ambient_overlay):.3f}:shortest=1{next_label}"
            )
        else:
            steps.append(
                f"{ambient_input}scale={width}:{height},format=rgba,"
                f"colorchannelmixer=aa={overlay_opacity(ambient_overlay):.3f}[ambient]"
            )
            steps.append(f"{current_label}[ambient]overlay=eof_action=repeat:shortest=1{next_label}")
        current_label = next_label
        current_name = "with_ambient"

    decorated_label = f"[{current_name}_decorated]"
    decorated_filter = decorate_video_filter(
        "null",
        track=track,
        title=track.title,
        width=width,
        height=height,
        duration=duration,
        render_config=render_config,
    )
    steps.append(f"{current_label}{decorated_filter}{decorated_label}")
    current_label = decorated_label

    if subscribe_overlay and "subscribe" in overlay_indexes:
        overlay_input = f"[{overlay_indexes['subscribe']}:v]"
        scaled_width = max(1, int(width * float(subscribe_overlay.get("width_percent", 12)) / 100.0))
        margin = max(0, int(width * float(subscribe_overlay.get("margin_percent", 3)) / 100.0))
        x_expr, y_expr = subscribe_overlay_position(
            str(subscribe_overlay.get("position") or "bottom-right").lower(),
            margin,
        )
        enable_expr = subscribe_enable_expr(subscribe_overlay, duration)
        steps.append(
            f"{overlay_input}scale={scaled_width}:-1,format=rgba,"
            f"colorchannelmixer=aa={overlay_opacity(subscribe_overlay):.3f}[subscribe]"
        )
        overlay_step = (
            f"{current_label}[subscribe]overlay=x={x_expr}:y={y_expr}:eof_action=repeat"
        )
        if enable_expr:
            overlay_step += f":enable='{enable_expr}'"
        overlay_step += output_label
        steps.append(overlay_step)
    else:
        steps.append(f"{current_label}null{output_label}")

    return steps


def segment_transition_filter(duration: float, render_config: dict[str, Any]) -> str:
    if not bool(render_config.get("transition_effect", True)) or duration <= 2.0:
        return "format=yuv420p"
    fade_duration = min(0.7, duration / 5)
    return (
        "format=yuv420p,"
        f"fade=t=in:st=0:d={fade_duration:.3f},"
        f"fade=t=out:st={max(0.0, duration - fade_duration):.3f}:d={fade_duration:.3f}"
    )


def overlay_opacity(config: dict[str, Any]) -> float:
    return max(0.0, min(1.0, float(config.get("opacity", 1.0))))


def subscribe_overlay_position(position: str, margin: int) -> tuple[str, str]:
    if position == "bottom-center":
        return "(W-w)/2", f"H-h-{margin}"
    if position == "top-right":
        return f"W-w-{margin}", str(margin)
    if position == "top-left":
        return str(margin), str(margin)
    if position == "top-center":
        return "(W-w)/2", str(margin)
    if position == "bottom-left":
        return str(margin), f"H-h-{margin}"
    return f"W-w-{margin}", f"H-h-{margin}"


def subscribe_enable_expr(config: dict[str, Any], duration: float) -> str:
    start_seconds = max(0.0, float(config.get("start_seconds", 0.0) or 0.0))
    display_seconds = float(config.get("display_seconds", 0.0) or 0.0)
    interval_seconds = float(config.get("interval_seconds", 0.0) or 0.0)
    if start_seconds >= duration:
        return "0"
    if display_seconds > 0 and interval_seconds > 0:
        return (
            f"gte(t,{start_seconds:.3f})*"
            f"lt(mod(t-{start_seconds:.3f},{interval_seconds:.3f}),{display_seconds:.3f})"
        )
    if display_seconds > 0:
        return f"between(t,{start_seconds:.3f},{min(duration, start_seconds + display_seconds):.3f})"
    if start_seconds > 0:
        return f"gte(t,{start_seconds:.3f})"
    return ""


def escape_drawtext(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "'\\''")
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
    subtitle_offset = max(
        0.0,
        float(render_config.get("subtitle_start_offset_seconds") or render_config.get("intro_audio_duration_seconds") or 0.0),
    )
    srt_path = track.audio_path.with_suffix(".auto.srt")
    ass_path = track.audio_path.with_suffix(".auto.ass")
    if (
        bool(render_config.get("use_synced_subtitles", True))
        and srt_path.exists()
        and Path(f"{srt_path}.synced").exists()
    ):
        if ass_path.exists():
            if subtitle_offset > 0:
                ass_path = shifted_ass_path(ass_path, subtitle_offset)
            ass_path = styled_ass_path(ass_path, height, render_config)
            return ass_subtitle_style_filter(ass_path, height, render_config)
        if subtitle_offset > 0:
            srt_path = shifted_srt_path(srt_path, subtitle_offset)
        return subtitle_style_filter(srt_path, height, render_config)

    transcript_path = track.audio_path.with_suffix(".txt")
    if not transcript_path.exists() or duration <= 1:
        return ""
    text = transcript_path.read_text(encoding="utf-8-sig").strip()
    chunks = subtitle_chunks(
        text,
        int(render_config.get("subtitle_words_per_chunk", 18)),
        int(render_config.get("subtitle_max_chars_per_chunk", 82)),
    )
    if not chunks:
        return ""

    usable_duration = max(1.0, duration - subtitle_offset - 1.0)
    chunk_duration = max(1.8, usable_duration / len(chunks))
    entries = []
    for index, chunk in enumerate(chunks[: int(duration / 1.2) + 2]):
        start = min(duration - 0.3, subtitle_offset + 0.5 + (index * chunk_duration))
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
    margin_h = int(render_config.get("subtitle_margin_h") or max(24, height // 24))
    font_name = str(render_config.get("subtitle_font_name") or "Arial").strip() or "Arial"
    alignment = int(render_config.get("subtitle_alignment") or 2)
    outline = float(render_config.get("subtitle_outline") or 2.6)
    shadow = float(render_config.get("subtitle_shadow") or 0.0)
    border_style = int(render_config.get("subtitle_border_style") or 1)
    bold = -1 if bool(render_config.get("subtitle_bold", True)) else 0
    primary_colour = str(render_config.get("subtitle_primary_color") or "&H00FFFFFF")
    outline_colour = str(render_config.get("subtitle_outline_color") or "&HAA000000")
    back_colour = str(render_config.get("subtitle_back_color") or "&H00000000")
    style = (
        f"FontName={font_name},"
        f"FontSize={font_size},"
        f"PrimaryColour={primary_colour},"
        f"OutlineColour={outline_colour},"
        f"BackColour={back_colour},"
        f"BorderStyle={border_style},"
        f"Outline={outline},"
        f"Shadow={shadow},"
        f"Bold={bold},"
        f"Alignment={alignment},"
        "WrapStyle=1,"
        f"MarginL={margin_h},"
        f"MarginR={margin_h},"
        f"MarginV={margin_v}"
    )
    return f"subtitles=filename='{escape_filter_path(srt_path)}':force_style='{style}'"


def ass_subtitle_style_filter(ass_path: Path, height: int, render_config: dict[str, Any]) -> str:
    return f"ass=filename='{escape_filter_path(ass_path)}'"


def styled_ass_path(source_path: Path, height: int, render_config: dict[str, Any]) -> Path:
    styled_path = source_path.with_name(f"{source_path.stem}.styled.ass")
    style_line = build_ass_style_line(height, render_config)
    lines: list[str] = []
    replaced = False
    for line in source_path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("Style: Default,"):
            lines.append(style_line)
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(style_line)
    styled_path.write_text("\n".join(lines), encoding="utf-8")
    return styled_path


def build_ass_style_line(height: int, render_config: dict[str, Any]) -> str:
    font_size = int(render_config.get("subtitle_font_size") or max(18, height // 66))
    margin_v = int(render_config.get("subtitle_margin_v") or max(30, height // 16))
    margin_h = int(render_config.get("subtitle_margin_h") or max(24, height // 24))
    font_name = str(render_config.get("subtitle_font_name") or "Arial").strip() or "Arial"
    alignment = int(render_config.get("subtitle_alignment") or 2)
    outline = float(render_config.get("subtitle_outline") or 2.6)
    shadow = float(render_config.get("subtitle_shadow") or 0.0)
    border_style = int(render_config.get("subtitle_border_style") or 1)
    bold = -1 if bool(render_config.get("subtitle_bold", True)) else 0
    primary_colour = str(render_config.get("subtitle_primary_color") or "&H00FFFFFF")
    outline_colour = str(render_config.get("subtitle_outline_color") or "&HAA000000")
    back_colour = str(render_config.get("subtitle_back_color") or "&H00000000")
    highlight_colour = str(render_config.get("subtitle_highlight_color") or "&H0000C8FF")
    return (
        "Style: Default,"
        f"{font_name},"
        f"{font_size},"
        f"{primary_colour},"
        f"{highlight_colour},"
        f"{outline_colour},"
        f"{back_colour},"
        f"{bold},0,0,0,100,100,0,0,"
        f"{border_style},"
        f"{outline},"
        f"{shadow},"
        f"{alignment},"
        f"{margin_h},"
        f"{margin_h},"
        f"{margin_v},1"
    )


def transcript_chunks(text: str, words_per_chunk: int, max_chars_per_chunk: int = 82) -> list[str]:
    words_per_chunk = max(4, words_per_chunk)
    max_chars_per_chunk = max(24, max_chars_per_chunk)
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    sentences = re.split(r"(?<=[.!?ã€‚ï¼ï¼Ÿ])\s+", cleaned)
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


def subtitle_chunks(text: str, words_per_chunk: int, max_chars_per_chunk: int = 82) -> list[str]:
    chunks = transcript_chunks(text, words_per_chunk, max_chars_per_chunk)
    if not chunks:
        return []
    max_lines_per_chunk = 2
    max_chars_per_line = max(18, min(max_chars_per_chunk - 8, max_chars_per_chunk // max(1, max_lines_per_chunk)))
    styled_chunks: list[str] = []
    for chunk in chunks:
        for phrase in phrase_chunks(chunk, words_per_chunk, max_chars_per_chunk):
            styled_chunks.append(balance_subtitle_lines(phrase, max_lines_per_chunk, max_chars_per_line))
    return styled_chunks


def phrase_chunks(text: str, words_per_chunk: int, max_chars_per_chunk: int) -> list[str]:
    phrase_limit = max(3, min(words_per_chunk, max_chars_per_chunk // 6))
    raw_parts = re.split(r"(?<=[,;:])\s+|\s+-\s+", text)
    parts = [re.sub(r"\s+", " ", part).strip(" ,;:-") for part in raw_parts if part and part.strip(" ,;:-")]
    if not parts:
        return [re.sub(r"\s+", " ", text).strip()]

    chunks: list[str] = []
    current = ""
    current_words = 0
    for part in parts:
        phrase_words = len(part.split())
        candidate = part if not current else f"{current} {part}"
        if current and (
            current_words + phrase_words > words_per_chunk
            or len(candidate) > max_chars_per_chunk
        ):
            chunks.append(current)
            current = part
            current_words = phrase_words
            continue
        current = candidate
        current_words += phrase_words
    if current:
        chunks.append(current)
    return chunks


def balance_subtitle_lines(text: str, max_lines: int, max_chars_per_line: int) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned or max_lines <= 1 or len(cleaned) <= max_chars_per_line:
        return cleaned

    words = cleaned.split()
    if len(words) < 3:
        return cleaned

    best_split_index: int | None = None
    best_score: float | None = None
    for index in range(1, len(words)):
        left = " ".join(words[:index]).strip()
        right = " ".join(words[index:]).strip()
        if not left or not right:
            continue
        if len(left) > max_chars_per_line + 8 or len(right) > max_chars_per_line + 8:
            continue
        score = abs(len(left) - len(right))
        if left.endswith((",", ";", ":")):
            score -= 3
        if len(left.split()) < 2 or len(right.split()) < 2:
            score += 5
        if best_score is None or score < best_score:
            best_score = score
            best_split_index = index

    if best_split_index is None:
        return cleaned
    return "\n".join(
        [
            " ".join(words[:best_split_index]).strip(),
            " ".join(words[best_split_index:]).strip(),
        ]
    )


def format_srt_time(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def parse_srt_time(value: str) -> float:
    hours, minutes, seconds_millis = value.split(":")
    seconds, millis = seconds_millis.split(",")
    return (
        (int(hours) * 3600)
        + (int(minutes) * 60)
        + int(seconds)
        + (int(millis) / 1000.0)
    )


def shifted_srt_path(source_path: Path, offset_seconds: float) -> Path:
    shifted_path = source_path.with_name(f"{source_path.stem}.offset{int(offset_seconds * 1000)}.srt")
    lines = source_path.read_text(encoding="utf-8-sig").splitlines()
    shifted_lines: list[str] = []
    for line in lines:
        if " --> " not in line:
            shifted_lines.append(line)
            continue
        start_text, end_text = line.split(" --> ", maxsplit=1)
        shifted_start = format_srt_time(parse_srt_time(start_text) + offset_seconds)
        shifted_end = format_srt_time(parse_srt_time(end_text) + offset_seconds)
        shifted_lines.append(f"{shifted_start} --> {shifted_end}")
    shifted_path.write_text("\n".join(shifted_lines), encoding="utf-8")
    return shifted_path


def shifted_ass_path(source_path: Path, offset_seconds: float) -> Path:
    shifted_path = source_path.with_name(f"{source_path.stem}.offset{int(offset_seconds * 1000)}.ass")
    shifted_lines: list[str] = []
    for line in source_path.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("Dialogue:"):
            shifted_lines.append(line)
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            shifted_lines.append(line)
            continue
        parts[1] = format_ass_time(parse_ass_time(parts[1]) + offset_seconds)
        parts[2] = format_ass_time(parse_ass_time(parts[2]) + offset_seconds)
        shifted_lines.append(",".join(parts))
    shifted_path.write_text("\n".join(shifted_lines), encoding="utf-8")
    return shifted_path


def parse_ass_time(value: str) -> float:
    hours, minutes, seconds_cs = value.split(":")
    seconds, centiseconds = seconds_cs.split(".")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(centiseconds) / 100.0
    )


def format_ass_time(seconds: float) -> str:
    centiseconds = int(round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{cs:02}"


def escape_filter_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    value = value.replace(":", "\\:")
    value = value.replace("'", "\\'")
    return value


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+|(?<=:)\s+(?=[A-ZÀ-Ỹ0-9])", text)
    normalized = [finalize_subtitle_text(part) for part in parts if finalize_subtitle_text(part)]
    return normalized or [finalize_subtitle_text(text)]


def normalize_subtitle_phrase(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s+([,;:.!?…])", r"\1", cleaned)
    cleaned = re.sub(r"([(\[{])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([)\]}])", r"\1", cleaned)
    return cleaned


def finalize_subtitle_text(text: str) -> str:
    cleaned = normalize_subtitle_phrase(text)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def transcript_chunks(text: str, words_per_chunk: int, max_chars_per_chunk: int = 82) -> list[str]:
    words_per_chunk = max(4, words_per_chunk)
    max_chars_per_chunk = max(24, max_chars_per_chunk)
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    sentences = split_sentences(cleaned)
    chunks: list[str] = []
    for sentence in sentences:
        words = sentence.split()
        current: list[str] = []
        for word in words:
            candidate = " ".join([*current, word])
            if current and (len(current) >= words_per_chunk or len(candidate) > max_chars_per_chunk):
                chunks.append(finalize_subtitle_text(" ".join(current)))
                current = []
            current.append(word)
        if current:
            chunks.append(finalize_subtitle_text(" ".join(current)))
    return chunks


def subtitle_chunks(text: str, words_per_chunk: int, max_chars_per_chunk: int = 82) -> list[str]:
    chunks = transcript_chunks(text, words_per_chunk, max_chars_per_chunk)
    if not chunks:
        return []
    max_lines_per_chunk = 2
    max_chars_per_line = max(18, min(max_chars_per_chunk - 8, max_chars_per_chunk // max(1, max_lines_per_chunk)))
    styled_chunks: list[str] = []
    for chunk in chunks:
        for phrase in phrase_chunks(chunk, words_per_chunk, max_chars_per_chunk):
            styled_chunks.append(balance_subtitle_lines(phrase, max_lines_per_chunk, max_chars_per_line))
    return styled_chunks


def phrase_chunks(text: str, words_per_chunk: int, max_chars_per_chunk: int) -> list[str]:
    raw_parts = re.split(r"(?<=[,;:.!?…])\s+|\s+-\s+", text)
    parts = [normalize_subtitle_phrase(part) for part in raw_parts if normalize_subtitle_phrase(part)]
    if not parts:
        normalized = normalize_subtitle_phrase(text)
        return [normalized] if normalized else []

    chunks: list[str] = []
    current = ""
    current_words = 0
    for part in parts:
        phrase_words = len(part.split())
        candidate = part if not current else f"{current} {part}"
        if current and (
            current_words + phrase_words > words_per_chunk
            or len(candidate) > max_chars_per_chunk
        ):
            chunks.append(finalize_subtitle_text(current))
            current = part
            current_words = phrase_words
            continue
        current = candidate
        current_words += phrase_words
    if current:
        chunks.append(finalize_subtitle_text(current))
    return chunks


def balance_subtitle_lines(text: str, max_lines: int, max_chars_per_line: int) -> str:
    cleaned = finalize_subtitle_text(text)
    if not cleaned or max_lines <= 1 or len(cleaned) <= max_chars_per_line:
        return cleaned

    words = cleaned.split()
    if len(words) < 3:
        return cleaned

    best_split_index: int | None = None
    best_score: float | None = None
    for index in range(1, len(words)):
        left = " ".join(words[:index]).strip()
        right = " ".join(words[index:]).strip()
        if not left or not right:
            continue
        if len(left) > max_chars_per_line + 8 or len(right) > max_chars_per_line + 8:
            continue
        score = abs(len(left) - len(right))
        if left.endswith((",", ";", ":")):
            score -= 3
        if left.endswith((".", "?", "!", "…")):
            score -= 5
        if right[:1] in {",", ";", ":", ".", "?", "!", "…"}:
            score += 8
        if len(left.split()) < 2 or len(right.split()) < 2:
            score += 5
        if best_score is None or score < best_score:
            best_score = score
            best_split_index = index

    if best_split_index is None:
        return cleaned
    return "\n".join(
        [
            finalize_subtitle_text(" ".join(words[:best_split_index])),
            finalize_subtitle_text(" ".join(words[best_split_index:])),
        ]
    )
