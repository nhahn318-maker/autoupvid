from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts

from .media import slugify


DEFAULT_VOICES = [
    {"id": "vi-VN-HoaiMyNeural", "label": "Vietnamese - Hoai My"},
    {"id": "vi-VN-NamMinhNeural", "label": "Vietnamese - Nam Minh"},
    {"id": "en-US-AvaMultilingualNeural", "label": "Multilingual - Ava (Vietnamese test)"},
    {"id": "en-US-AndrewMultilingualNeural", "label": "Multilingual - Andrew (Vietnamese test)"},
    {"id": "en-US-EmmaMultilingualNeural", "label": "Multilingual - Emma (Vietnamese test)"},
    {"id": "en-US-BrianMultilingualNeural", "label": "Multilingual - Brian (Vietnamese test)"},
    {"id": "en-US-JennyNeural", "label": "English - Jenny"},
    {"id": "en-US-GuyNeural", "label": "English - Guy"},
]


def generate_voice(text: str, title: str, voice: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{slugify(title)}.mp3"
    transcript_path = output_path.with_suffix(".txt")
    title_path = output_path.with_suffix(".title.txt")
    subtitle_path = output_path.with_suffix(".auto.srt")
    transcript_path.write_text(text.strip(), encoding="utf-8")
    title_path.write_text(title.strip(), encoding="utf-8")
    asyncio.run(_generate(text=text, voice=voice, output_path=output_path, subtitle_path=subtitle_path))
    return output_path


async def _generate(text: str, voice: str, output_path: Path, subtitle_path: Path) -> None:
    communicate = edge_tts.Communicate(text=text, voice=voice, boundary="WordBoundary")
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
