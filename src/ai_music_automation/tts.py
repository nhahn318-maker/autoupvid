from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import edge_tts

from .collection import escape_concat_path
from .media import probe_duration_seconds, slugify


DEFAULT_VOICES = [
    {"id": "vieneu:Ngọc Linh", "label": "VieNeu Local - Ngọc Linh"},
    {"id": "vieneu:Bình An", "label": "VieNeu Local - Bình An"},
    {"id": "vieneu:Ngọc Lan", "label": "VieNeu Local - Ngọc Lan"},
    {"id": "vieneu:Mỹ Duyên", "label": "VieNeu Local - Mỹ Duyên"},
    {"id": "vieneu:Trúc Ly", "label": "VieNeu Local - Trúc Ly"},
    {"id": "vieneu:Gia Bảo", "label": "VieNeu Local - Gia Bảo"},
    {"id": "vieneu:Thái Sơn", "label": "VieNeu Local - Thái Sơn"},
    {"id": "vieneu:Đức Trí", "label": "VieNeu Local - Đức Trí"},
    {"id": "vieneu:Xuân Vĩnh", "label": "VieNeu Local - Xuân Vĩnh"},
    {"id": "vieneu:Trọng Hữu", "label": "VieNeu Local - Trọng Hữu"},
    {"id": "vi-VN-HoaiMyNeural", "label": "Vietnamese - Hoai My"},
    {"id": "vi-VN-NamMinhNeural", "label": "Vietnamese - Nam Minh"},
    {"id": "en-US-AvaMultilingualNeural", "label": "Multilingual - Ava (Vietnamese test)"},
    {"id": "en-US-AndrewMultilingualNeural", "label": "Multilingual - Andrew (Vietnamese test)"},
    {"id": "en-US-EmmaMultilingualNeural", "label": "Multilingual - Emma (Vietnamese test)"},
    {"id": "en-US-BrianMultilingualNeural", "label": "Multilingual - Brian (Vietnamese test)"},
    {"id": "en-US-JennyNeural", "label": "English - Jenny"},
    {"id": "en-US-GuyNeural", "label": "English - Guy"},
    {"id": "en-US-AvaNeural", "label": "English US - Ava"},
    {"id": "en-US-AndrewNeural", "label": "English US - Andrew"},
    {"id": "en-US-EmmaNeural", "label": "English US - Emma"},
    {"id": "en-US-BrianNeural", "label": "English US - Brian"},
    {"id": "en-US-AriaNeural", "label": "English US - Aria"},
    {"id": "en-US-JaneNeural", "label": "English US - Jane"},
    {"id": "en-US-JasonNeural", "label": "English US - Jason"},
    {"id": "en-US-NancyNeural", "label": "English US - Nancy"},
    {"id": "en-US-RogerNeural", "label": "English US - Roger"},
    {"id": "en-US-SaraNeural", "label": "English US - Sara"},
    {"id": "en-US-SteffanNeural", "label": "English US - Steffan"},
    {"id": "en-GB-SoniaNeural", "label": "English UK - Sonia"},
    {"id": "en-GB-RyanNeural", "label": "English UK - Ryan"},
    {"id": "en-GB-LibbyNeural", "label": "English UK - Libby"},
    {"id": "en-GB-ThomasNeural", "label": "English UK - Thomas"},
    {"id": "en-AU-NatashaNeural", "label": "English AU - Natasha"},
    {"id": "en-AU-WilliamNeural", "label": "English AU - William"},
    {"id": "en-CA-ClaraNeural", "label": "English CA - Clara"},
    {"id": "en-CA-LiamNeural", "label": "English CA - Liam"},
]

EDGE_VOICE_FALLBACKS = {
    "en-US-DavisNeural": "en-US-BrianNeural",
}


def generate_voice(text: str, title: str, voice: str, output_dir: Path, rate: str = "+0%") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{slugify(title)}.mp3"
    transcript_path = output_path.with_suffix(".txt")
    title_path = output_path.with_suffix(".title.txt")
    subtitle_path = output_path.with_suffix(".auto.srt")
    clean_text = text.strip()
    transcript_path.write_text(clean_text, encoding="utf-8")
    title_path.write_text(title.strip(), encoding="utf-8")
    voice = EDGE_VOICE_FALLBACKS.get(voice, voice)
    if voice.startswith("vieneu:"):
        generate_vieneu_voice(clean_text, voice.removeprefix("vieneu:"), output_path, subtitle_path, rate)
        return output_path

    chunks = split_tts_text(clean_text)
    if len(chunks) == 1:
        generate_tts_segment(clean_text, voice, output_path, subtitle_path, rate)
        return output_path

    temp_dir = Path(tempfile.mkdtemp(prefix="story-tts-"))
    try:
        segment_paths: list[Path] = []
        subtitle_paths: list[Path] = []
        durations: list[float] = []
        for index, chunk in enumerate(chunks, start=1):
            segment_path = temp_dir / f"{index:04}.mp3"
            segment_srt = temp_dir / f"{index:04}.srt"
            generate_tts_segment(chunk, voice, segment_path, segment_srt, rate)
            segment_paths.append(segment_path)
            subtitle_paths.append(segment_srt)
            durations.append(max(0.0, probe_duration_seconds(segment_path)))

        concat_audio(segment_paths, output_path, temp_dir / "concat.txt")
        merge_segment_srts(subtitle_paths, durations, subtitle_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return output_path


def generate_vieneu_voice(
    text: str,
    voice: str,
    output_path: Path,
    subtitle_path: Path,
    rate: str = "+0%",
) -> None:
    root = Path(__file__).resolve().parents[2]
    python_path = root / "tools" / "vieneu" / ".venv" / "Scripts" / "python.exe"
    bridge_path = root / "tools" / "vieneu_bridge.py"
    ffmpeg_path = root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if not python_path.exists():
        raise RuntimeError(f"VieNeu runtime is not installed: {python_path}")
    if not bridge_path.exists():
        raise RuntimeError(f"VieNeu bridge is missing: {bridge_path}")

    temp_dir = Path(tempfile.mkdtemp(prefix="vieneu-tts-"))
    try:
        text_path = temp_dir / "input.txt"
        wav_path = temp_dir / "output.wav"
        text_path.write_text(text, encoding="utf-8")
        result = subprocess.run(
            [
                str(python_path),
                str(bridge_path),
                "--text-file",
                str(text_path),
                "--output",
                str(wav_path),
                "--voice",
                voice,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            check=False,
        )
        if result.returncode != 0 or not wav_path.exists():
            detail = (result.stderr or result.stdout or "unknown VieNeu error").strip()
            raise RuntimeError(f"VieNeu generation failed: {detail[-2000:]}")

        ffmpeg = str(ffmpeg_path if ffmpeg_path.exists() else "ffmpeg")
        command = [ffmpeg, "-y", "-i", str(wav_path)]
        speed = parse_edge_rate(rate)
        if speed != 1.0:
            command.extend(["-filter:a", f"atempo={speed:.4f}"])
        command.extend(["-codec:a", "libmp3lame", "-q:a", "2", str(output_path)])
        subprocess.run(command, capture_output=True, check=True)

        duration = max(0.1, probe_duration_seconds(output_path))
        write_estimated_srt(text, duration, subtitle_path)
        Path(f"{subtitle_path}.synced").write_text("vieneu-estimated-sentence-timing\n", encoding="utf-8")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def parse_edge_rate(rate: str) -> float:
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)%", rate.strip())
    if not match:
        return 1.0
    return max(0.5, min(2.0, 1.0 + float(match.group(1)) / 100.0))


def write_estimated_srt(text: str, duration: float, output_path: Path) -> None:
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?…])\s+|\n+", text)
        if sentence.strip()
    ] or [text.strip()]
    weights = [max(1, len(sentence)) for sentence in sentences]
    total_weight = sum(weights)
    entries: list[str] = []
    start = 0.0
    for index, (sentence, weight) in enumerate(zip(sentences, weights), start=1):
        end = duration if index == len(sentences) else start + duration * weight / total_weight
        entries.append(
            f"{index}\n"
            f"{format_srt_time(start)} --> {format_srt_time(end)}\n"
            f"{sentence}\n"
        )
        start = end
    output_path.write_text("\n".join(entries), encoding="utf-8")


def split_tts_text(text: str, max_chars: int = 2500) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?…])\s+", paragraph)
            if sentence.strip()
        ]
        units.extend(sentences or [paragraph])

    chunks: list[str] = []
    current = ""
    for unit in units:
        for piece in split_oversized_text(unit, max_chars):
            candidate = f"{current} {piece}".strip()
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


def generate_tts_segment(
    text: str,
    voice: str,
    output_path: Path,
    subtitle_path: Path,
    rate: str = "+0%",
    attempts: int = 5,
    timeout_seconds: int = 300,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        output_path.unlink(missing_ok=True)
        subtitle_path.unlink(missing_ok=True)
        Path(f"{subtitle_path}.synced").unlink(missing_ok=True)
        try:
            asyncio.run(
                asyncio.wait_for(
                    _generate(
                        text=text,
                        voice=voice,
                        output_path=output_path,
                        subtitle_path=subtitle_path,
                        rate=rate,
                    ),
                    timeout=timeout_seconds,
                )
            )
            if not output_path.exists() or output_path.stat().st_size == 0:
                raise RuntimeError("TTS returned an empty audio segment")
            return
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 2)
    output_path.unlink(missing_ok=True)
    subtitle_path.unlink(missing_ok=True)
    Path(f"{subtitle_path}.synced").unlink(missing_ok=True)
    raise RuntimeError(f"TTS segment failed after {attempts} attempts: {last_error}") from last_error


def split_oversized_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    words = text.split()
    pieces: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            pieces.append(current)
            current = word
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def merge_segment_srts(
    subtitle_paths: list[Path],
    durations: list[float],
    output_path: Path,
) -> None:
    entries: list[str] = []
    offset = 0.0
    sequence = 1
    for subtitle_path, duration in zip(subtitle_paths, durations):
        if subtitle_path.exists():
            for start, end, text in read_srt_entries(subtitle_path):
                entries.append(
                    f"{sequence}\n"
                    f"{format_srt_time(start + offset)} --> {format_srt_time(end + offset)}\n"
                    f"{text}\n"
                )
                sequence += 1
        offset += duration
    if entries:
        output_path.write_text("\n".join(entries), encoding="utf-8")
        Path(f"{output_path}.synced").write_text("edge-tts-chunked-word-boundary\n", encoding="utf-8")


def read_srt_entries(path: Path) -> list[tuple[float, float, str]]:
    entries: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_text, end_text = [part.strip() for part in lines[1].split("-->", 1)]
        entries.append(
            (
                parse_srt_time(start_text),
                parse_srt_time(end_text),
                "\n".join(lines[2:]),
            )
        )
    return entries


def parse_srt_time(value: str) -> float:
    hours, minutes, remainder = value.replace(".", ",").split(":")
    seconds, milliseconds = remainder.split(",", 1)
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(milliseconds[:3].ljust(3, "0")) / 1000
    )


def generate_conversation_voice(
    script: str,
    title: str,
    speaker1_label: str,
    speaker1_voice: str,
    speaker2_label: str,
    speaker2_voice: str,
    output_dir: Path,
    speaker3_label: str = "",
    speaker3_voice: str = "",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{slugify(title)}.mp3"
    transcript_path = output_path.with_suffix(".txt")
    title_path = output_path.with_suffix(".title.txt")
    subtitle_path = output_path.with_suffix(".auto.srt")
    metadata_path = output_path.with_suffix(".conversation.json")

    lines = parse_conversation_script(
        script=script,
        speaker1_label=speaker1_label,
        speaker2_label=speaker2_label,
        speaker1_voice=speaker1_voice,
        speaker2_voice=speaker2_voice,
        speaker3_label=speaker3_label,
        speaker3_voice=speaker3_voice,
    )
    if not lines:
        raise ValueError("Conversation script needs at least one line like A: Hello.")

    transcript = "\n".join(f"{line['label']}: {line['text']}" for line in lines)
    transcript_path.write_text(transcript, encoding="utf-8")
    title_path.write_text(title.strip(), encoding="utf-8")
    metadata_path.write_text(json.dumps({"lines": lines}, ensure_ascii=False, indent=2), encoding="utf-8")
    for stale_path in [subtitle_path, Path(f"{subtitle_path}.synced")]:
        if stale_path.exists():
            stale_path.unlink()

    temp_dir = Path(tempfile.mkdtemp(prefix="conversation-tts-"))
    try:
        segment_paths = []
        starts_and_durations = []
        current_start = 0.0
        for index, line in enumerate(lines, start=1):
            segment_path = temp_dir / f"{index:03}.mp3"
            segment_srt = temp_dir / f"{index:03}.srt"
            asyncio.run(_generate(text=str(line["text"]), voice=str(line["voice"]), output_path=segment_path, subtitle_path=segment_srt))
            duration = max(0.25, probe_duration_seconds(segment_path))
            segment_paths.append(segment_path)
            starts_and_durations.append((current_start, duration))
            current_start += duration

        concat_audio(segment_paths, output_path, temp_dir / "concat.txt")
        write_conversation_srt(lines, starts_and_durations, subtitle_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return output_path


def parse_conversation_script(
    script: str,
    speaker1_label: str,
    speaker2_label: str,
    speaker1_voice: str,
    speaker2_voice: str,
    speaker3_label: str = "",
    speaker3_voice: str = "",
) -> list[dict[str, str]]:
    speaker1_label = speaker1_label.strip() or "A"
    speaker2_label = speaker2_label.strip() or "B"
    speaker3_label = speaker3_label.strip()
    speaker_map = {
        normalize_speaker_key("A"): (speaker1_label, speaker1_voice),
        normalize_speaker_key("1"): (speaker1_label, speaker1_voice),
        normalize_speaker_key("Voice 1"): (speaker1_label, speaker1_voice),
        normalize_speaker_key(speaker1_label): (speaker1_label, speaker1_voice),
        normalize_speaker_key("B"): (speaker2_label, speaker2_voice),
        normalize_speaker_key("2"): (speaker2_label, speaker2_voice),
        normalize_speaker_key("Voice 2"): (speaker2_label, speaker2_voice),
        normalize_speaker_key(speaker2_label): (speaker2_label, speaker2_voice),
    }
    if speaker3_label and speaker3_voice:
        speaker_map.update(
            {
                normalize_speaker_key("C"): (speaker3_label, speaker3_voice),
                normalize_speaker_key("3"): (speaker3_label, speaker3_voice),
                normalize_speaker_key("Voice 3"): (speaker3_label, speaker3_voice),
                normalize_speaker_key(speaker3_label): (speaker3_label, speaker3_voice),
            }
        )
    parsed: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^\[?([^\]:\-]+)\]?\s*[:\-]\s*(.+)$", line)
        if match:
            key = normalize_speaker_key(match.group(1))
            if key not in speaker_map:
                raise ValueError(f"Unknown speaker label: {match.group(1).strip()}")
            label, voice = speaker_map[key]
            current = {"speaker": key, "label": label, "voice": voice, "text": match.group(2).strip()}
            parsed.append(current)
        elif current:
            current["text"] = f"{current['text']} {line}".strip()
        else:
            raise ValueError("Every conversation line must start with A:, B:, C:, or your speaker label.")
    return [item for item in parsed if item["text"]]


def normalize_speaker_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def concat_audio(segment_paths: list[Path], output_path: Path, concat_file: Path) -> None:
    concat_file.write_text(
        "\n".join(f"file '{escape_concat_path(path)}'" for path in segment_paths),
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
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        str(output_path),
    ]
    try:
        subprocess.run(copy_command, check=True)
    except subprocess.CalledProcessError:
        subprocess.run(fallback_command, check=True)


def write_conversation_srt(
    lines: list[dict[str, str]],
    starts_and_durations: list[tuple[float, float]],
    subtitle_path: Path,
) -> None:
    entries = []
    for index, (line, timing) in enumerate(zip(lines, starts_and_durations), start=1):
        start, duration = timing
        end = start + duration
        text = f"{line['label']}: {line['text']}"
        entries.append(f"{index}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}\n")
    subtitle_path.write_text("\n".join(entries), encoding="utf-8")
    Path(f"{subtitle_path}.synced").write_text("edge-tts-conversation-segments\n", encoding="utf-8")


async def _generate(text: str, voice: str, output_path: Path, subtitle_path: Path, rate: str = "+0%") -> None:
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, boundary="WordBoundary")
    boundaries: list[dict[str, object]] = []
    with output_path.open("wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                boundaries.append(chunk)
    write_word_boundary_srt(boundaries, subtitle_path)


def write_word_boundary_srt(
    boundaries: list[dict[str, object]],
    subtitle_path: Path,
    words_per_chunk: int = 18,
    max_chars_per_chunk: int = 82,
) -> None:
    words = [
        {
            "text": str(item.get("text", "")).strip(),
            "start": ticks_to_seconds(int(item.get("offset", 0))),
            "duration": ticks_to_seconds(int(item.get("duration", 0))),
        }
        for item in boundaries
        if str(item.get("text", "")).strip()
    ]
    if not words:
        return

    entries = []
    current: list[dict[str, object]] = []
    for word in words:
        candidate = " ".join([*(str(item["text"]) for item in current), str(word["text"])])
        if current and (len(current) >= words_per_chunk or len(candidate) > max_chars_per_chunk):
            entries.append(current)
            current = []
        current.append(word)
    if current:
        entries.append(current)

    lines = []
    for index, entry in enumerate(entries, start=1):
        start = float(entry[0]["start"])
        last = entry[-1]
        end = float(last["start"]) + max(0.25, float(last["duration"]))
        text = " ".join(str(item["text"]) for item in entry)
        lines.append(f"{index}\n{format_srt_time(start)} --> {format_srt_time(end + 0.15)}\n{text}\n")

    subtitle_path.write_text("\n".join(lines), encoding="utf-8")
    Path(f"{subtitle_path}.synced").write_text("edge-tts-word-boundary\n", encoding="utf-8")


def ticks_to_seconds(value: int) -> float:
    return value / 10_000_000


def format_srt_time(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"
