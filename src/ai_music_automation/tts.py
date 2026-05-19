from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts

from .media import slugify


DEFAULT_VOICES = [
    {"id": "vi-VN-HoaiMyNeural", "label": "Vietnamese - Hoai My"},
    {"id": "vi-VN-NamMinhNeural", "label": "Vietnamese - Nam Minh"},
    {"id": "en-US-JennyNeural", "label": "English - Jenny"},
    {"id": "en-US-GuyNeural", "label": "English - Guy"},
]


def generate_voice(text: str, title: str, voice: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{slugify(title)}.mp3"
    transcript_path = output_path.with_suffix(".txt")
    title_path = output_path.with_suffix(".title.txt")
    transcript_path.write_text(text.strip(), encoding="utf-8")
    title_path.write_text(title.strip(), encoding="utf-8")
    asyncio.run(_generate(text=text, voice=voice, output_path=output_path))
    return output_path


async def _generate(text: str, voice: str, output_path: Path) -> None:
    communicate = edge_tts.Communicate(text=text, voice=voice)
    await communicate.save(str(output_path))
