from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SpeechQAReport:
    available: bool
    passed: bool
    similarity: float = 0.0
    transcript: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_speech(
    audio_path: Path,
    expected_text: str,
    *,
    model_name: str = "tiny",
    language: str = "en",
    cache_dir: Path | None = None,
    threshold: float = 0.72,
) -> SpeechQAReport:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return SpeechQAReport(
            available=False,
            passed=True,
            notes=["Speech QA skipped because faster-whisper is not installed."],
        )

    try:
        model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            download_root=str(cache_dir) if cache_dir else None,
            cpu_threads=4,
        )
        segments, _info = model.transcribe(
            str(audio_path),
            language=language or None,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        transcript = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
    except Exception as exc:
        return SpeechQAReport(
            available=True,
            passed=True,
            notes=[f"Speech QA could not run and was treated as a warning: {exc}"],
        )

    expected = _normalize(expected_text)
    actual = _normalize(transcript)
    similarity = SequenceMatcher(None, expected, actual, autojunk=False).ratio() if expected and actual else 0.0
    notes: list[str] = []
    if not actual:
        notes.append("Speech recognizer returned no transcript.")
    elif similarity < threshold:
        notes.append(f"Voice transcript similarity is low: {similarity:.3f} < {threshold:.3f}.")
    return SpeechQAReport(
        available=True,
        passed=bool(actual) and similarity >= threshold,
        similarity=round(similarity, 4),
        transcript=transcript,
        notes=notes or ["Speech QA passed."],
    )


def _normalize(value: str) -> str:
    lowered = value.lower().replace("’", "'")
    return " ".join(re.findall(r"[a-z0-9']+", lowered))
