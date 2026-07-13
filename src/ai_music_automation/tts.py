from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import edge_tts

from .collection import escape_concat_path
from .media import probe_duration_seconds, slugify


DEFAULT_VOICES = [
    {"id": "fpt:banmai", "label": "FPT.AI - Ban Mai (nữ Bắc)"},
    {"id": "fpt:lannhi", "label": "FPT.AI - Lan Nhi (nữ Nam)"},
    {"id": "fpt:myan", "label": "FPT.AI - Mỹ An (nữ Trung)"},
    {"id": "fpt:thuminh", "label": "FPT.AI - Thu Minh (nữ Bắc)"},
    {"id": "fpt:linhsan", "label": "FPT.AI - Linh San (nữ Nam)"},
    {"id": "fpt:leminh", "label": "FPT.AI - Lê Minh (nam Bắc)"},
    {"id": "fpt:giahuy", "label": "FPT.AI - Gia Huy (nam Trung)"},
    {"id": "vieneu:Trúc Ly", "label": "VieNeu Local - Trúc Ly"},
    {"id": "vieneu:Phạm Tuyên", "label": "VieNeu Local - Phạm Tuyên"},
    {"id": "vieneu:Thái Sơn", "label": "VieNeu Local - Thái Sơn"},
    {"id": "vieneu:Xuân Vĩnh", "label": "VieNeu Local - Xuân Vĩnh"},
    {"id": "vieneu:Thanh Bình", "label": "VieNeu Local - Thanh Bình"},
    {"id": "vieneu:Minh Đức", "label": "VieNeu Local - Minh Đức"},
    {"id": "vieneu:Ngọc Linh", "label": "VieNeu Local - Ngọc Linh"},
    {"id": "vieneu:Đoan Trang", "label": "VieNeu Local - Đoan Trang"},
    {"id": "vieneu:Mai Anh", "label": "VieNeu Local - Mai Anh"},
    {"id": "vieneu:Thục Đoan", "label": "VieNeu Local - Thục Đoan"},
    {"id": "kokoro-en:bm_lewis", "label": "Kokoro English - Lewis (British male)"},
    {"id": "kokoro:thanh_dat", "label": "Kokoro Vietnamese - Thanh Dat"},
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
    ass_subtitle_path = output_path.with_suffix(".auto.ass")
    clean_text = text.strip()
    transcript_path.write_text(clean_text, encoding="utf-8")
    title_path.write_text(title.strip(), encoding="utf-8")
    ass_subtitle_path.unlink(missing_ok=True)
    voice = EDGE_VOICE_FALLBACKS.get(voice, voice)
    if voice.startswith("vieneu:"):
        generate_vieneu_voice(clean_text, voice.removeprefix("vieneu:"), output_path, subtitle_path, rate)
        return output_path
    if voice.startswith("kokoro-en:"):
        generate_kokoro_english_voice(clean_text, voice.removeprefix("kokoro-en:"), output_path, subtitle_path, rate)
        return output_path
    if voice.startswith("kokoro:"):
        generate_kokoro_voice(clean_text, voice.removeprefix("kokoro:"), output_path, subtitle_path, rate)
        return output_path
    if voice.startswith("fpt:"):
        try:
            generate_fpt_voice(clean_text, voice.removeprefix("fpt:"), output_path, subtitle_path, rate)
        except Exception as exc:
            fallback_voice = fpt_fallback_voice(Path(__file__).resolve().parents[2])
            if not fallback_voice:
                raise
            output_path.unlink(missing_ok=True)
            subtitle_path.unlink(missing_ok=True)
            Path(f"{subtitle_path}.synced").unlink(missing_ok=True)
            generate_tts_segment(clean_text, fallback_voice, output_path, subtitle_path, rate)
            output_path.with_suffix(".fpt-fallback.txt").write_text(
                f"FPT.AI voice {voice} failed, fell back to {fallback_voice}.\nError: {exc}\n",
                encoding="utf-8",
            )
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
        write_ass_from_srt_entries(read_srt_entries(subtitle_path), ass_subtitle_path)
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


def generate_kokoro_voice(
    text: str,
    voice: str,
    output_path: Path,
    subtitle_path: Path,
    rate: str = "+0%",
) -> None:
    root = Path(__file__).resolve().parents[2]
    python_path = root / "tools" / "kokoro-vietnamese" / ".venv" / "Scripts" / "python.exe"
    ffmpeg_path = root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if not python_path.exists():
        raise RuntimeError(f"Kokoro Vietnamese runtime is not installed: {python_path}")

    temp_dir = Path(tempfile.mkdtemp(prefix="kokoro-tts-"))
    try:
        chunks = split_tts_text(text, max_chars=2200)
        wav_paths: list[Path] = []
        for index, chunk in enumerate(chunks, start=1):
            wav_path = temp_dir / f"part-{index:04d}.wav"
            synthesize_kokoro_vietnamese_chunk(
                python_path=python_path,
                root=root,
                temp_dir=temp_dir,
                text=chunk,
                voice=voice,
                wav_path=wav_path,
                index=index,
                total=len(chunks),
            )
            wav_paths.append(wav_path)

        wav_path = temp_dir / "output.wav"
        concat_wav_files(wav_paths, wav_path, ffmpeg_path)

        ffmpeg = str(ffmpeg_path if ffmpeg_path.exists() else "ffmpeg")
        command = [ffmpeg, "-y", "-i", str(wav_path)]
        speed = parse_edge_rate(rate)
        if speed != 1.0:
            command.extend(["-filter:a", f"atempo={speed:.4f}"])
        command.extend(["-codec:a", "libmp3lame", "-q:a", "2", str(output_path)])
        subprocess.run(command, capture_output=True, check=True)

        duration = max(0.1, probe_duration_seconds(output_path))
        write_estimated_srt(text, duration, subtitle_path)
        Path(f"{subtitle_path}.synced").write_text("kokoro-vietnamese-estimated-sentence-timing\n", encoding="utf-8")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def synthesize_kokoro_vietnamese_chunk(
    python_path: Path,
    root: Path,
    temp_dir: Path,
    text: str,
    voice: str,
    wav_path: Path,
    index: int,
    total: int,
) -> None:
    text_path = temp_dir / f"input-{index:04d}.txt"
    text_path.write_text(text, encoding="utf-8")
    script = (
        "from pathlib import Path\n"
        "import soundfile as sf\n"
        "from kokoro_vietnamese import KokoroVietnamese\n"
        f"text = Path({str(text_path)!r}).read_text(encoding='utf-8')\n"
        f"tts = KokoroVietnamese(device='cpu', voice={voice!r})\n"
        "audio, phonemes = tts.synthesize(text, normalize_peak=0.95)\n"
        f"sf.write({str(wav_path)!r}, audio, 24000)\n"
    )
    last_detail = ""
    for attempt in range(1, 3):
        wav_path.unlink(missing_ok=True)
        try:
            result = subprocess.run(
                [str(python_path), "-c", script],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            last_detail = f"timed out after {exc.timeout} seconds"
        else:
            if result.returncode == 0 and wav_path.exists() and wav_path.stat().st_size > 0:
                return
            last_detail = (result.stderr or result.stdout or "unknown Kokoro Vietnamese error").strip()
        if attempt < 2:
            time.sleep(5)
    if len(text) > 700:
        subchunks = split_tts_text(text, max_chars=max(350, len(text) // 2))
        if len(subchunks) > 1:
            sub_wavs: list[Path] = []
            for sub_index, subchunk in enumerate(subchunks, start=1):
                sub_wav = temp_dir / f"{wav_path.stem}-{sub_index:02d}.wav"
                synthesize_kokoro_vietnamese_chunk(
                    python_path=python_path,
                    root=root,
                    temp_dir=temp_dir,
                    text=subchunk,
                    voice=voice,
                    wav_path=sub_wav,
                    index=index,
                    total=total,
                )
                sub_wavs.append(sub_wav)
            concat_wav_files(sub_wavs, wav_path, root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe")
            if wav_path.exists() and wav_path.stat().st_size > 0:
                return
    raise RuntimeError(f"Kokoro Vietnamese chunk {index}/{total} failed: {last_detail[-2000:]}")


def concat_wav_files(wav_paths: list[Path], output_path: Path, ffmpeg_path: Path) -> None:
    if not wav_paths:
        raise RuntimeError("Kokoro Vietnamese returned no audio chunks")
    if len(wav_paths) == 1:
        shutil.copyfile(wav_paths[0], output_path)
        return
    list_path = output_path.with_suffix(".concat.txt")
    list_path.write_text(
        "".join(f"file '{escape_concat_path(path)}'\n" for path in wav_paths),
        encoding="utf-8",
    )
    ffmpeg = str(ffmpeg_path if ffmpeg_path.exists() else "ffmpeg")
    subprocess.run(
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(output_path)],
        capture_output=True,
        check=True,
    )


def generate_kokoro_english_voice(
    text: str,
    voice: str,
    output_path: Path,
    subtitle_path: Path,
    rate: str = "+0%",
) -> None:
    root = Path(__file__).resolve().parents[2]
    python_path = root / "tools" / "kokoro-english" / ".venv" / "Scripts" / "python.exe"
    ffmpeg_path = root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if not python_path.exists():
        raise RuntimeError(f"Kokoro English runtime is not installed: {python_path}")

    temp_dir = Path(tempfile.mkdtemp(prefix="kokoro-en-tts-"))
    try:
        text_path = temp_dir / "input.txt"
        wav_path = temp_dir / "output.wav"
        text_path.write_text(text, encoding="utf-8")
        speed = parse_edge_rate(rate)
        lang_code = voice[0] if voice else "a"
        script = (
            "from pathlib import Path\n"
            "import numpy as np\n"
            "import soundfile as sf\n"
            "from kokoro import KPipeline\n"
            f"text = Path({str(text_path)!r}).read_text(encoding='utf-8')\n"
            f"pipeline = KPipeline(lang_code={lang_code!r}, repo_id='hexgrad/Kokoro-82M', device='cpu')\n"
            "chunks = []\n"
            f"for result in pipeline(text, voice={voice!r}, speed={speed!r}, split_pattern=r'\\n+'):\n"
            "    chunks.append(result.audio)\n"
            "if not chunks:\n"
            "    raise RuntimeError('Kokoro English returned no audio')\n"
            "audio = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)\n"
            f"sf.write({str(wav_path)!r}, audio, 24000)\n"
        )
        result = subprocess.run(
            [str(python_path), "-c", script],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            check=False,
        )
        if result.returncode != 0 or not wav_path.exists():
            detail = (result.stderr or result.stdout or "unknown Kokoro English error").strip()
            raise RuntimeError(f"Kokoro English generation failed: {detail[-2000:]}")

        ffmpeg = str(ffmpeg_path if ffmpeg_path.exists() else "ffmpeg")
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(wav_path),
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(output_path),
        ]
        subprocess.run(command, capture_output=True, check=True)

        duration = max(0.1, probe_duration_seconds(output_path))
        write_estimated_srt(text, duration, subtitle_path)
        Path(f"{subtitle_path}.synced").write_text("kokoro-english-estimated-sentence-timing\n", encoding="utf-8")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def generate_fpt_voice(
    text: str,
    voice: str,
    output_path: Path,
    subtitle_path: Path,
    rate: str = "+0%",
) -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_root_config(root)
    fpt_config = dict(((config.get("tts_providers") or {}).get("fpt") or {}))
    if not bool(fpt_config.get("enabled", False)):
        raise RuntimeError("FPT.AI TTS is not enabled. Set tts_providers.fpt.enabled=true in config.json.")
    api_key = str(
        os.environ.get("FPT_AI_API_KEY")
        or read_secret_value(root, "fpt_ai_api_key")
        or fpt_config.get("api_key")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError("Missing FPT.AI API key. Set FPT_AI_API_KEY or tts_providers.fpt.api_key.")

    max_chars = int(fpt_config.get("monthly_free_chars", 100000) or 100000)
    block_over_quota = bool(fpt_config.get("block_when_over_quota", True))
    charged_chars = len(text)
    quota = read_fpt_quota(root)
    if block_over_quota and quota["used_chars"] + charged_chars > max_chars:
        raise RuntimeError(
            "FPT.AI free-tier quota would be exceeded: "
            f"{quota['used_chars']:,}/{max_chars:,} used, new text needs {charged_chars:,} chars."
        )

    fpt_speed = fpt_speed_from_rate(rate, str(fpt_config.get("speed") or ""))
    chunks = split_oversized_text(text, int(fpt_config.get("max_chars_per_request", 4800) or 4800))
    temp_dir = Path(tempfile.mkdtemp(prefix="fpt-tts-"))
    try:
        segment_paths: list[Path] = []
        subtitle_paths: list[Path] = []
        durations: list[float] = []
        for index, chunk in enumerate(chunks, start=1):
            segment_path = temp_dir / f"{index:04}.mp3"
            segment_srt = temp_dir / f"{index:04}.srt"
            fpt_tts_segment(
                text=chunk,
                voice=voice,
                speed=fpt_speed,
                api_key=api_key,
                output_path=segment_path,
                timeout_seconds=int(fpt_config.get("timeout_seconds", 180) or 180),
            )
            duration = max(0.1, probe_duration_seconds(segment_path))
            write_estimated_srt(chunk, duration, segment_srt)
            segment_paths.append(segment_path)
            subtitle_paths.append(segment_srt)
            durations.append(duration)

        if len(segment_paths) == 1:
            shutil.move(str(segment_paths[0]), output_path)
            shutil.move(str(subtitle_paths[0]), subtitle_path)
        else:
            concat_audio(segment_paths, output_path, temp_dir / "concat.txt")
            merge_segment_srts(subtitle_paths, durations, subtitle_path)
        write_ass_from_srt_entries(read_srt_entries(subtitle_path), output_path.with_suffix(".auto.ass"))
        Path(f"{subtitle_path}.synced").write_text("fpt-tts-estimated-sentence-timing\n", encoding="utf-8")
        quota["used_chars"] += charged_chars
        quota["requests"] += len(chunks)
        write_fpt_quota(root, quota)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def load_root_config(root: Path) -> dict:
    config_path = root / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def fpt_fallback_voice(root: Path) -> str:
    config = load_root_config(root)
    fpt_config = dict(((config.get("tts_providers") or {}).get("fpt") or {}))
    voice = str(fpt_config.get("fallback_voice") or "vi-VN-HoaiMyNeural").strip()
    return voice if voice and not voice.startswith("fpt:") else "vi-VN-HoaiMyNeural"


def read_secret_value(root: Path, key: str) -> str:
    path = root / "data" / "state" / "local_secrets.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get(key) or "").strip()


def fpt_quota_month() -> str:
    return datetime.now().strftime("%Y-%m")


def fpt_quota_path(root: Path) -> Path:
    return root / "data" / "state" / "tts_fpt_quota.json"


def read_fpt_quota(root: Path) -> dict:
    path = fpt_quota_path(root)
    month = fpt_quota_month()
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = {}
    if data.get("month") != month:
        return {"month": month, "used_chars": 0, "requests": 0}
    return {
        "month": month,
        "used_chars": int(data.get("used_chars", 0) or 0),
        "requests": int(data.get("requests", 0) or 0),
    }


def write_fpt_quota(root: Path, quota: dict) -> None:
    path = fpt_quota_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(quota, ensure_ascii=False, indent=2), encoding="utf-8")


def fpt_speed_from_rate(rate: str, configured_speed: str) -> str:
    if configured_speed in {"-3", "-2", "-1", "0", "+1", "+2", "+3"}:
        return configured_speed
    match = re.search(r"([+-]?\d+)", str(rate or ""))
    value = int(match.group(1)) if match else 0
    if value <= -20:
        return "-2"
    if value < 0:
        return "-1"
    if value >= 20:
        return "+2"
    if value > 0:
        return "+1"
    return "0"


def fpt_tts_segment(
    text: str,
    voice: str,
    speed: str,
    api_key: str,
    output_path: Path,
    timeout_seconds: int = 180,
) -> None:
    request = urllib.request.Request(
        "https://api.fpt.ai/hmi/tts/v5",
        data=text.encode("utf-8"),
        headers={
            "api_key": api_key,
            "voice": voice,
            "speed": speed,
            "format": "mp3",
            "Cache-Control": "no-cache",
            "Content-Type": "text/plain; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if int(payload.get("error", 1)) != 0:
        raise RuntimeError(f"FPT.AI TTS request failed: {payload.get('message') or payload}")
    async_url = str(payload.get("async") or "").strip()
    if not async_url:
        raise RuntimeError("FPT.AI TTS response did not include an async audio URL.")
    download_async_audio(async_url, output_path, timeout_seconds)


def download_async_audio(url: str, output_path: Path, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read()
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if len(data) > 1024 and ("audio" in content_type or data[:3] == b"ID3" or data[:2] == b"\xff\xfb"):
                output_path.write_bytes(data)
                return
            last_error = RuntimeError(f"FPT.AI audio is not ready yet: content_type={content_type or 'unknown'}, bytes={len(data)}")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {403, 404, 425, 429, 500, 502, 503, 504}:
                raise
        except Exception as exc:  # noqa: BLE001 - async link may not be ready yet.
            last_error = exc
        time.sleep(5)
    raise RuntimeError(f"Timed out waiting for FPT.AI audio after {timeout_seconds}s: {last_error}")


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


def write_ass_from_srt_entries(
    entries: list[tuple[float, float, str]],
    ass_path: Path,
) -> None:
    if not entries:
        ass_path.unlink(missing_ok=True)
        return

    ass_lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 1",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial Rounded MT Bold,18,&H00FFFFFF,&H0090EEFF,&H88000000,&H00000000,-1,0,0,0,100,100,0,0,1,2.6,0,2,36,36,72,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for start, end, text in entries:
        ass_lines.append(
            f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Default,,0,0,0,,{ass_karaoke_text_from_text(text, start, end)}"
        )

    ass_path.write_text("\n".join(ass_lines), encoding="utf-8")


def ass_karaoke_text_from_text(text: str, start: float, end: float) -> str:
    words = [word for word in re.split(r"\s+", text.replace("\n", " ").strip()) if word]
    if not words:
        return escape_ass_text(text.strip())
    total_duration = max(0.2, end - start)
    base_cs = max(1, int(round(total_duration * 100 / len(words))))
    return " ".join(f"{{\\k{base_cs}}}{escape_ass_text(word)}" for word in words)


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
    write_word_boundary_ass(boundaries, subtitle_path.with_suffix(".ass"))


def write_word_boundary_srt(
    boundaries: list[dict[str, object]],
    subtitle_path: Path,
    words_per_chunk: int = 18,
    max_chars_per_chunk: int = 82,
) -> None:
    words = word_boundary_words(boundaries)
    if not words:
        return

    entries = subtitle_word_entries(words, words_per_chunk, max_chars_per_chunk)

    lines = []
    for index, item in enumerate(entries, start=1):
        lines.append(
            f"{index}\n"
            f"{format_srt_time(float(item['start']))} --> {format_srt_time(float(item['end']))}\n"
            f"{item['text']}\n"
        )

    subtitle_path.write_text("\n".join(lines), encoding="utf-8")
    Path(f"{subtitle_path}.synced").write_text("edge-tts-word-boundary\n", encoding="utf-8")


def write_word_boundary_ass(
    boundaries: list[dict[str, object]],
    ass_path: Path,
    words_per_chunk: int = 18,
    max_chars_per_chunk: int = 82,
) -> None:
    words = word_boundary_words(boundaries)
    if not words:
        ass_path.unlink(missing_ok=True)
        return

    entries = subtitle_word_entries(words, words_per_chunk, max_chars_per_chunk)

    ass_lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 1",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial Rounded MT Bold,18,&H00FFFFFF,&H0090EEFF,&H88000000,&H00000000,-1,0,0,0,100,100,0,0,1,2.6,0,2,36,36,72,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for entry in entries:
        start = float(entry["start"])
        end = float(entry["end"])
        text = ass_karaoke_text(list(entry["words"]))
        ass_lines.append(
            f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Default,,0,0,0,,{text}"
        )

    ass_path.write_text("\n".join(ass_lines), encoding="utf-8")


def word_boundary_words(boundaries: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "text": str(item.get("text", "")).strip(),
            "start": ticks_to_seconds(int(item.get("offset", 0))),
            "duration": max(0.05, ticks_to_seconds(int(item.get("duration", 0)))),
        }
        for item in boundaries
        if str(item.get("text", "")).strip()
    ]


def subtitle_word_entries(
    words: list[dict[str, object]],
    words_per_chunk: int,
    max_chars_per_chunk: int,
) -> list[dict[str, object]]:
    sentence_punctuation = (".", "!", "?", "…", ";", ":", ",")
    strong_breaks = (".", "!", "?", "…")
    entries: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []

    for word in words:
        candidate_words = [*current, word]
        candidate_text = normalize_boundary_text(candidate_words)
        should_flush = False
        if current:
            if len(candidate_words) > words_per_chunk or len(candidate_text) > max_chars_per_chunk:
                should_flush = True
            elif ends_with_boundary(current[-1], sentence_punctuation):
                should_flush = True
            elif strong_pause_between(current[-1], word):
                should_flush = True
        if should_flush:
            entries.append(current)
            current = []
        current.append(word)

    if current:
        entries.append(current)

    normalized_entries: list[dict[str, object]] = []
    for index, entry in enumerate(entries):
        start = float(entry[0]["start"])
        raw_end = float(entry[-1]["start"]) + max(0.25, float(entry[-1]["duration"]))
        if ends_with_boundary(entry[-1], strong_breaks):
            raw_end += 0.22
        elif ends_with_boundary(entry[-1], sentence_punctuation):
            raw_end += 0.14
        else:
            raw_end += 0.08
        next_start = (
            float(entries[index + 1][0]["start"])
            if index + 1 < len(entries)
            else raw_end
        )
        end = min(raw_end, max(start + 0.18, next_start - 0.02))
        normalized_entries.append(
            {
                "start": start,
                "end": end,
                "text": normalize_boundary_text(entry),
                "words": entry,
            }
        )
    return normalized_entries


def normalize_boundary_text(words: list[dict[str, object]]) -> str:
    text = " ".join(str(item["text"]) for item in words).strip()
    text = re.sub(r"\s+([,.;:!?…])", r"\1", text)
    text = re.sub(r"([(\[{])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]}])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def ends_with_boundary(word: dict[str, object], punctuation: tuple[str, ...]) -> bool:
    text = str(word["text"]).strip()
    return text.endswith(punctuation)


def strong_pause_between(left: dict[str, object], right: dict[str, object], threshold_seconds: float = 0.42) -> bool:
    left_end = float(left["start"]) + float(left["duration"])
    right_start = float(right["start"])
    return right_start - left_end >= threshold_seconds


def ass_karaoke_text(words: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for item in words:
        duration_cs = max(1, int(round(float(item["duration"]) * 100)))
        parts.append(f"{{\\k{duration_cs}}}{escape_ass_text(str(item['text']))}")
    return " ".join(parts)


def escape_ass_text(value: str) -> str:
    return value.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def ticks_to_seconds(value: int) -> float:
    return value / 10_000_000


def format_srt_time(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def format_ass_time(seconds: float) -> str:
    centiseconds = int(round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{cs:02}"
