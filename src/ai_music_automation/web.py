from __future__ import annotations
import importlib.machinery
import importlib.util
import inspect
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .accounts import account_state_dir, account_state_dirs, account_token_path, get_accounts, get_active_account_id
from .cli import init_project, render_short, reserve_next_publish_time, sync_missing_youtube_uploads, upload_tracks
from .collection import collection_candidates, concat_videos, create_collection
from .config import AppConfig, load_config
from .media import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, discover_tracks, find_matching_images, list_files
from .metadata import VideoMetadata, build_metadata
from .render import render_video
from .scheduler import next_publish_times, to_rfc3339_utc
from .state import StateStore
from .job_store import JobStore
from .story_before_sleep import (
    run_story_before_sleep_test,
    story_before_sleep_paths,
    story_before_sleep_status,
)
from .tts import DEFAULT_VOICES, generate_voice
from .view_optimizer import generate_view_optimizer_report
from .youtube import get_youtube_service, send_email_notification, upload_video
from .youtube_analytics import sync_youtube_analytics


ROOT = Path.cwd()
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "web_static"
JOBS: dict[str, dict[str, Any]] = {}
JOB_QUEUE: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue()
JOB_STORE = JobStore(ROOT / "data" / "state" / "jobs.sqlite3")
WORKER_LOCK = threading.Lock()
WORKER_THREAD: threading.Thread | None = None
BATCH_NOTIFY_LOCK = threading.Lock()
BATCH_NOTIFY_ACTIVE = False
BATCH_NOTIFY_STARTED_AT = ""

app = FastAPI(title="AI Music YouTube Automation")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_RECOVERED_NAME = f"{__package__}._web_recovered_runtime"
_RECOVERED_PATH = Path(__file__).with_name("_web_recovered.bytecode")


def load_recovered_module():
    existing = sys.modules.get(_RECOVERED_NAME)
    if existing is not None:
        return existing
    loader = importlib.machinery.SourcelessFileLoader(_RECOVERED_NAME, str(_RECOVERED_PATH))
    spec = importlib.util.spec_from_loader(_RECOVERED_NAME, loader)
    if spec is None:
        raise RuntimeError(f"Cannot create module spec for {_RECOVERED_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_RECOVERED_NAME] = module
    loader.exec_module(module)
    return module


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/mobile", response_class=HTMLResponse, include_in_schema=False)
def mobile_index() -> str:
    return (STATIC_DIR / "mobile.html").read_text(encoding="utf-8")


@app.get("/mobile.webmanifest", include_in_schema=False)
def mobile_manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "mobile.webmanifest", media_type="application/manifest+json")


@app.get("/mobile-sw.js", include_in_schema=False)
def mobile_service_worker() -> FileResponse:
    return FileResponse(STATIC_DIR / "mobile-sw.js", media_type="application/javascript")


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    init_project(ROOT)
    config = load_config(ROOT)
    paths = config.paths
    state = StateStore(account_state_dir(config))
    active_account = get_active_account_id(config)
    other_states = [
        StateStore(state_dir)
        for account_id, state_dir in account_state_dirs(config).items()
        if account_id != active_account
    ]
    all_tracks = discover_tracks(paths["audio_dir"], paths["image_dir"])
    tracks = [
        track
        for track in all_tracks
        if not uploaded_in_any_state(track.audio_path, other_states)
    ]
    shorts_enabled = bool(config.get("shorts", "enabled", default=False))
    pending = [track for track in tracks if state.needs_work(track.audio_path, shorts_enabled)]
    collection_config = config.get("collection", default={})
    collection_size = int(collection_config.get("size", 5))
    collection_videos = collection_candidates(tracks, paths["output_dir"], collection_size)

    track_items = []
    for track in tracks:
        output_path = paths["output_dir"] / f"{track.slug}.mp4"
        short_path = paths["output_dir"] / f"{track.slug}-short.mp4"
        metadata = build_metadata(track, config.data, paths["thumbnail_dir"])
        track_items.append(
            {
                "title": metadata.title,
                "description": metadata.description,
                "tags": metadata.tags,
                "category_id": metadata.category_id,
                "thumbnail": metadata.thumbnail_path.name if metadata.thumbnail_path else "",
                "has_thumbnail": bool(metadata.thumbnail_path),
                "thumbnail_url": f"/api/thumbnail/{metadata.thumbnail_path.name}" if metadata.thumbnail_path else "",
                "audio": track.audio_path.name,
                "image": ", ".join(path.name for path in track.image_paths),
                "video": output_path.name,
                "short_video": short_path.name,
                "video_exists": output_path.exists(),
                "short_exists": short_path.exists(),
                "video_url": f"/api/video/{output_path.name}" if output_path.exists() else "",
                "short_video_url": f"/api/video/{short_path.name}" if short_path.exists() else "",
                "processed": state.is_processed(track.audio_path),
                "normal_uploaded": state.has_upload(track.audio_path, "normal"),
                "short_uploaded": state.has_upload(track.audio_path, "short"),
                "upload_needed": state.needs_work(track.audio_path, shorts_enabled),
                "youtube_urls": youtube_urls_for(state.uploads_for(track.audio_path)),
            }
        )

    schedule_preview = [
        to_rfc3339_utc(value)
        for value in next_publish_times(
            count=int(config.get("schedule", "videos_per_day", default=3)),
            configured_times=config.get("schedule", "publish_times"),
            timezone_name=config.get("schedule", "timezone"),
        )
    ]

    return {
        "counts": {
            "audio": len(list_files(paths["audio_dir"], AUDIO_EXTENSIONS)),
            "images": len(list_files(paths["image_dir"], IMAGE_EXTENSIONS)),
            "tracks": len(tracks),
            "pending": len(pending),
            "output": len(list_files(paths["output_dir"], {".mp4"})),
            "uploads": len(state.data["uploads"]),
        },
        "tracks": track_items,
        "files": {
            "audio": [path.name for path in recent_files(paths["audio_dir"], AUDIO_EXTENSIONS)],
            "images": [path.name for path in recent_files(paths["image_dir"], IMAGE_EXTENSIONS)],
            "thumbnails": [path.name for path in recent_files(paths["thumbnail_dir"], IMAGE_EXTENSIONS)],
        },
        "config": config.data,
        "mode": "story" if is_fullauto_supported_account(active_account) else "bolero",
        "fullauto": fullauto_status(config),
        "story_before_sleep": story_before_sleep_status(config.data),
        "paths": {key: str(value.relative_to(ROOT)) for key, value in paths.items() if value.is_relative_to(ROOT)},
        "upload_policy": {
            "videos_per_day": int(config.get("schedule", "videos_per_day", default=3)),
            "warning": (
                "Story mode is capped at 1 video per day for safer publishing."
                if is_fullauto_supported_account(active_account)
                else "Bolero mode uses the shared daily schedule."
            ),
            "upload_limit_warning": upload_limit_warning(),
        },
        "active_account": active_account,
        "accounts": get_accounts(config),
        "collection": {
            "enabled": bool(collection_config.get("enabled", True)),
            "size": collection_size,
            "ready": bool(collection_config.get("enabled", True)) and len(collection_videos) >= collection_size,
            "rendered_count": len(collection_videos),
            "needed_count": max(0, collection_size - len(collection_videos)),
            "videos": [path.name for path in collection_videos],
        },
        "schedule_preview": schedule_preview,
        "credentials_ready": paths["credentials_file"].exists(),
        "token_ready": account_token_path(config).exists(),
        "jobs": status_jobs(config),
        "tts_voices": DEFAULT_VOICES,
    }


def status_jobs(config) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    visible_jobs: list[dict[str, Any]] = []
    for job in reversed(list(JOBS.values())):
        status = str(job.get("status") or "").strip().lower()
        finished_at = parse_iso_timestamp(str(job.get("finished_at") or ""))
        age_seconds = (now - finished_at).total_seconds() if job.get("finished_at") else 0
        # Failed jobs stay briefly so the cause can be read, then leave the
        # active dashboard. Durable history remains in jobs.sqlite3.
        if status == "failed" and age_seconds > 15 * 60:
            continue
        if status == "done" and age_seconds > 2 * 60 * 60:
            continue
        visible_jobs.append(job)
        if len(visible_jobs) >= 8:
            break
    jobs = visible_jobs
    return [enrich_status_job(job, config) for job in jobs]


def enrich_status_job(job: dict[str, Any], config) -> dict[str, Any]:
    accounts = get_accounts(config)
    item = dict(job)
    action = str(item.get("action") or "")
    target_account = str(item.get("target_account") or item.get("account") or notification_account_id(config, action))
    account_label = accounts.get(target_account, {}).get("label", target_account)
    logs = list(item.get("logs") or [])
    latest_log = logs[-1] if logs else ""
    item["label"] = item.get("label") or describe_job_action(action)
    item["account_label"] = account_label
    item["current_step"] = item.get("progress_detail") or item.get("stage") or latest_log or "Waiting"
    item["recent_logs"] = logs[-5:]
    return item


def external_resume_jobs() -> list[dict[str, Any]]:
    """Expose a manually resumed long render in the same UI job list."""
    log_path = ROOT / "logs" / "fullauto-long-resume-upload.out.log"
    error_path = ROOT / "logs" / "fullauto-long-resume-upload.err.log"
    output_dir = ROOT / "data" / "output" / "story" / "story"
    outputs = sorted(output_dir.glob("*fullauto-long-resume.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not log_path.exists() and not outputs:
        return []

    try:
        log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        log_lines = []
    try:
        error_lines = error_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        error_lines = []

    output = outputs[0] if outputs else None
    rendered = any(line.startswith("RENDERED=") for line in log_lines)
    uploaded = any(line.startswith("UPLOAD_RECORD=") for line in log_lines)
    failed = any("Traceback" in line for line in error_lines)
    if uploaded:
        status, stage, progress = "done", "Uploaded / scheduled", 100
    elif failed:
        status, stage, progress = "failed", "Resume render failed", 0
    elif rendered:
        status, stage, progress = "running", "Uploading to YouTube", 96
    else:
        size_bytes = output.stat().st_size if output and output.exists() else 0
        expected_bytes = 3_250_000_000
        progress = max(1, min(94, int(size_bytes * 95 / expected_bytes)))
        status, stage = "running", "Rendering 92-minute video"

    recent_logs = log_lines[-8:]
    if output and output.exists() and not uploaded:
        recent_logs.append(f"{datetime.now().strftime('%H:%M:%S')} Output: {output.stat().st_size / 1024 / 1024:.0f} MB")
    if failed:
        recent_logs.extend(error_lines[-3:])
    created_at = datetime.fromtimestamp(log_path.stat().st_mtime).isoformat(timespec="seconds") if log_path.exists() else datetime.now().isoformat(timespec="seconds")
    return [{
        "id": "fullauto-long-resume-current",
        "action": "fullauto-long-resume",
        "status": status,
        "created_at": created_at,
        "started_at": created_at,
        "finished_at": None,
        "stage": stage,
        "progress": progress,
        "progress_detail": "Resume from existing 92-minute Kokoro voice",
        "logs": recent_logs or ["Waiting for resume renderer to start."],
    }]


def external_resume_render_is_active() -> bool:
    """Return true while the manual long-resume FFmpeg render is still active."""
    log_path = ROOT / "logs" / "fullauto-long-resume-upload.out.log"
    if not log_path.exists():
        return False
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    if not any(line == "START_RENDER" for line in lines) or any(line.startswith("RENDERED=") for line in lines):
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return False
    return "ffmpeg.exe" in result.stdout.lower()


def wait_for_external_resume_render(job: dict[str, Any]) -> None:
    announced = False
    last_report = 0.0
    while external_resume_render_is_active():
        now = time.monotonic()
        if not announced or now - last_report >= 60:
            log(job, "Waiting for Fullauto Long Resume to finish before this queued job starts.")
            announced = True
            last_report = now
        job["stage"] = "Waiting for active long render"
        job["progress"] = 0
        job["progress_detail"] = "Queue is paused until Fullauto Long Resume completes."
        time.sleep(5)


@app.get("/api/video/{filename}")
def api_video(filename: str) -> FileResponse:
    config = load_config(ROOT)
    paths = config.paths
    target = paths["output_dir"] / Path(filename).name
    if not target.exists() or target.suffix.lower() != ".mp4":
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(target, media_type="video/mp4")


@app.get("/api/audio/{filename}")
def api_audio(filename: str) -> FileResponse:
    return FileResponse(resolve_audio_file(filename))


@app.head("/api/audio/{filename}")
def api_audio_head(filename: str) -> FileResponse:
    return FileResponse(resolve_audio_file(filename))


def resolve_audio_file(filename: str) -> Path:
    config = load_config(ROOT)
    paths = config.paths
    target = paths["audio_dir"] / Path(filename).name
    if not target.exists() or target.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Audio not found")
    return target


@app.get("/api/thumbnail/{filename}")
def api_thumbnail(filename: str) -> FileResponse:
    config = load_config(ROOT)
    paths = config.paths
    target = paths["thumbnail_dir"] / Path(filename).name
    if not target.exists() or target.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(target)


@app.post("/api/track-action")
def api_track_action(payload: dict[str, str]) -> dict[str, Any]:
    action = (payload.get("action") or "").strip()
    audio_name = (payload.get("audio") or "").strip()
    if action not in {"render", "rerender", "dry-run", "upload", "upload-normal", "upload-short", "skip", "delete"}:
        raise HTTPException(status_code=400, detail="Unknown track action")
    if not audio_name:
        raise HTTPException(status_code=400, detail="Audio is required")
    job_id = enqueue_job(f"track-{action}", {"audio": audio_name})
    return {"job_id": job_id}


@app.post("/api/metadata-override")
def api_metadata_override(payload: dict[str, Any]) -> dict[str, str]:
    audio_name = (payload.get("audio") or "").strip()
    if not audio_name:
        raise HTTPException(status_code=400, detail="Audio is required")
    config = load_config(ROOT)
    active_account = get_active_account_id(config)
    overrides = config.data.setdefault("metadata_overrides", {}).setdefault(active_account, {})
    tags = payload.get("tags", [])
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
    overrides[Path(audio_name).name] = {
        "title": str(payload.get("title") or "").strip(),
        "description": str(payload.get("description") or "").strip(),
        "tags": tags,
        "category_id": str(payload.get("category_id") or "").strip(),
    }
    (ROOT / "config.json").write_text(
        json.dumps(config.data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"status": "saved"}


@app.post("/api/fullauto-provider")
def api_fullauto_provider(payload: dict[str, str]) -> dict[str, str]:
    provider = (payload.get("provider") or "").strip().lower()
    model = (payload.get("model") or "").strip()
    if provider not in {"gemini", "ollama"}:
        raise HTTPException(status_code=400, detail="Unknown provider")
    if not model:
        raise HTTPException(status_code=400, detail="Model is required")

    config = load_config(ROOT)
    fullauto_config = config.data.setdefault("fullauto", {})
    fullauto_config["provider"] = provider
    if provider == "ollama":
        fullauto_config["ollama_model"] = model
    else:
        fullauto_config["gemini_model"] = model

    (ROOT / "config.json").write_text(
        json.dumps(config.data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"status": "saved", "provider": provider, "model": model}


@app.post("/api/story-assets")
async def api_story_assets(
    audio_name: str = Form(...),
    images: list[UploadFile] = File(...),
    thumbnail: UploadFile | None = File(None),
) -> dict[str, Any]:
    config = load_config(ROOT)
    paths = config.paths
    audio_path = paths["audio_dir"] / Path(audio_name).name
    if not audio_path.exists() or audio_path.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unknown MP3 file")
    if not images or len(images) > 5:
        raise HTTPException(status_code=400, detail="Select 1 to 5 images")

    paths["image_dir"].mkdir(parents=True, exist_ok=True)
    paths["thumbnail_dir"].mkdir(parents=True, exist_ok=True)
    stem = audio_path.stem

    saved_images = []
    for index, item in enumerate(images, start=1):
        extension = Path(item.filename or "").suffix.lower()
        if extension not in IMAGE_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Images must be jpg, png, or webp")
        target = paths["image_dir"] / f"{stem}-{index}{extension}"
        with target.open("wb") as handle:
            shutil.copyfileobj(item.file, handle)
        saved_images.append(target.name)

    saved_thumbnail = None
    if thumbnail and thumbnail.filename:
        extension = Path(thumbnail.filename).suffix.lower()
        if extension not in IMAGE_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Thumbnail must be jpg, png, or webp")
        target = paths["thumbnail_dir"] / f"{stem}{extension}"
        with target.open("wb") as handle:
            shutil.copyfileobj(thumbnail.file, handle)
        saved_thumbnail = target.name

    return {
        "audio": audio_path.name,
        "images": saved_images,
        "thumbnail": saved_thumbnail,
    }


@app.post("/api/upload-files")
async def api_upload_files(kind: str = Form(...), files: list[UploadFile] = File(...)) -> dict[str, Any]:
    config = load_config(ROOT)
    paths = config.paths
    destination = {
        "audio": paths["audio_dir"],
        "image": paths["image_dir"],
        "thumbnail": paths["thumbnail_dir"],
    }.get(kind)
    if destination is None:
        raise HTTPException(status_code=400, detail="Unknown upload kind")

    destination.mkdir(parents=True, exist_ok=True)
    saved = []
    for item in files:
        filename = safe_filename(item.filename or "upload")
        target = destination / filename
        with target.open("wb") as handle:
            shutil.copyfileobj(item.file, handle)
        saved.append(filename)
    return {"saved": saved}


@app.post("/api/story-before-sleep/reference")
async def api_story_before_sleep_reference(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    config = load_config(ROOT)
    paths = story_before_sleep_paths(config.data)
    paths["references"].mkdir(parents=True, exist_ok=True)
    saved = []
    for item in files:
        extension = Path(item.filename or "").suffix.lower()
        if extension not in IMAGE_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Reference must be jpg, png, or webp")
        filename = safe_filename(item.filename or "reference.png")
        target = paths["references"] / filename
        with target.open("wb") as handle:
            shutil.copyfileobj(item.file, handle)
        saved.append(filename)
    return {"saved": saved}


@app.post("/api/config")
def api_save_config(payload: dict[str, Any]) -> dict[str, str]:
    (ROOT / "config.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"status": "saved"}


@app.post("/api/tts")
def api_tts(payload: dict[str, str]) -> dict[str, Any]:
    title = (payload.get("title") or "").strip()
    text = (payload.get("text") or "").strip()
    voice = (payload.get("voice") or "vi-VN-HoaiMyNeural").strip()
    if not title or not text:
        raise HTTPException(status_code=400, detail="Title and text are required")
    job_id = enqueue_job("tts", {"title": title, "text": text, "voice": voice})
    return {"job_id": job_id}


@app.post("/api/account/{account_id}")
def api_set_account(account_id: str) -> dict[str, str]:
    config = load_config(ROOT)
    if account_id not in get_accounts(config):
        raise HTTPException(status_code=404, detail="Unknown account")
    config.data["active_account"] = account_id
    config.data.setdefault("fullauto", {})["upload_account"] = account_id
    (ROOT / "config.json").write_text(
        json.dumps(config.data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"status": "saved", "active_account": account_id}


@app.post("/api/action/{action}")
def api_action(action: str) -> dict[str, Any]:
    if action not in {"render", "daily-dry-run", "daily-upload", "create-collection", "sync-state"}:
        raise HTTPException(status_code=404, detail="Unknown action")

    job_id = enqueue_job(action, {})
    return {"job_id": job_id}


@app.post("/api/fullauto-action")
def api_fullauto_action(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip().lower()
    target_account = str(payload.get("target_account") or "").strip()
    action_map = {
        "start": "fullauto-start",
        "start-long": "fullauto-long-start",
        "start-20min": "fullauto-20min-start",
        "merge-1hour": "fullauto-merge-1hour",
        "merge-upload-1hour": "fullauto-merge-upload-1hour",
        "merge-long-selected": "fullauto-merge-long-selected",
        "merge-upload-long-selected": "fullauto-merge-upload-long-selected",
    }
    if action not in action_map:
        raise HTTPException(status_code=404, detail="Unknown Full Auto action")
    validation_error = validate_fullauto_action_assets(action, target_account)
    if validation_error:
        raise HTTPException(status_code=400, detail=validation_error)
    job_id = enqueue_job(
        action_map[action],
        {
            "target_account": target_account,
            "filenames": payload.get("filenames") or [],
        },
    )
    return {"job_id": job_id}


@app.post("/api/fullauto-bulk-action")
def api_fullauto_bulk_action(payload: dict[str, Any]) -> dict[str, Any]:
    short_count = bounded_int(payload.get("short_count"), default=0, minimum=0, maximum=10)
    twenty_min_count = bounded_int(payload.get("twenty_min_count"), default=0, minimum=0, maximum=5)
    long_count = bounded_int(payload.get("long_count"), default=0, minimum=0, maximum=3)
    if short_count + twenty_min_count + long_count < 1:
        raise HTTPException(status_code=400, detail="Choose at least one video to create")
    validation_errors: list[str] = []
    for account_id in ["account1", "account2", "account3"]:
        if short_count:
            error = validate_fullauto_action_assets("start", account_id)
            if error:
                validation_errors.append(f"{account_id} shorts: {error}")
        if twenty_min_count:
            error = validate_fullauto_action_assets("start-20min", account_id)
            if error:
                validation_errors.append(f"{account_id} 20-min: {error}")
        if long_count:
            error = validate_fullauto_action_assets("start-long", account_id)
            if error:
                validation_errors.append(f"{account_id} long: {error}")
    if validation_errors:
        raise HTTPException(status_code=400, detail="; ".join(validation_errors))
    job_id = enqueue_job(
        "fullauto-bulk",
        {
            "accounts": ["account1", "account2", "account3"],
            "short_count": short_count,
            "twenty_min_count": twenty_min_count,
            "long_count": long_count,
        },
    )
    return {"job_id": job_id}


@app.post("/api/youtube-research")
def api_youtube_research(payload: dict[str, Any]) -> dict[str, Any]:
    channel_url = str(payload.get("channel_url") or "").strip()
    if not channel_url:
        raise HTTPException(status_code=400, detail="Channel URL is required")
    limit = bounded_int(payload.get("limit"), default=24, minimum=1, maximum=80)
    tab = str(payload.get("tab") or "shorts").strip().lower()
    if tab not in {"shorts", "videos", "all"}:
        raise HTTPException(status_code=400, detail="tab must be shorts, videos, or all")
    transcript_limit = bounded_int(payload.get("transcript_limit"), default=8, minimum=0, maximum=30)
    job_id = enqueue_job(
        "youtube-research",
        {
            "channel_url": channel_url,
            "limit": limit,
            "tab": tab,
            "transcript_limit": transcript_limit,
        },
    )
    return {"job_id": job_id}


@app.post("/api/view-optimizer")
def api_view_optimizer(payload: dict[str, Any]) -> dict[str, str]:
    limit = bounded_int(payload.get("limit"), default=80, minimum=10, maximum=200)
    job_id = enqueue_job("view-optimizer", {"limit": limit})
    return {"job_id": job_id}


@app.post("/api/youtube-analytics-sync")
def api_youtube_analytics_sync(payload: dict[str, Any]) -> dict[str, str]:
    days = bounded_int(payload.get("days"), default=90, minimum=7, maximum=365)
    limit = bounded_int(payload.get("limit"), default=120, minimum=1, maximum=500)
    job_id = enqueue_job("youtube-analytics-sync", {"days": days, "limit": limit})
    return {"job_id": job_id}


@app.post("/api/fullauto-channel")
def api_fullauto_channel(payload: dict[str, Any]) -> dict[str, str]:
    target_account = str(payload.get("target_account") or "").strip()
    if not target_account:
        raise HTTPException(status_code=400, detail="target_account is required")
    config = load_config(ROOT)
    if target_account not in get_accounts(config):
        raise HTTPException(status_code=404, detail="Unknown account")
    config.data.setdefault("fullauto", {})["upload_account"] = target_account
    (ROOT / "config.json").write_text(
        json.dumps(config.data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"status": "saved", "upload_account": target_account}


@app.get("/api/fullauto/markdown/{filename}")
def api_fullauto_markdown(filename: str) -> FileResponse:
    recovered = configured_recovered_module(load_config(ROOT))
    return recovered.api_fullauto_markdown(filename)


@app.get("/api/story-before-sleep/markdown/{filename}")
def api_story_before_sleep_markdown(filename: str) -> FileResponse:
    config = load_config(ROOT)
    paths = story_before_sleep_paths(config.data)
    target = paths["drafts"] / Path(filename).name
    if not target.exists() or target.suffix.lower() != ".md":
        raise HTTPException(status_code=404, detail="Story Before Sleep markdown not found")
    return FileResponse(target)


@app.post("/api/story-before-sleep-action")
def api_story_before_sleep_action(payload: dict[str, Any]) -> dict[str, str]:
    action = str(payload.get("action") or "test").strip().lower()
    if action == "auto":
        job_id = enqueue_job("story-before-sleep-auto", payload)
    else:
        job_id = enqueue_job("story-before-sleep-test", payload)
    return {"job_id": job_id}


@app.post("/api/fullauto/thumbnail")
async def api_fullauto_thumbnail(
    draft_id: str = Form(...),
    thumbnail: UploadFile = File(...),
) -> dict[str, str]:
    recovered = configured_recovered_module(load_config(ROOT))
    return await recovered.api_fullauto_thumbnail(draft_id=draft_id, thumbnail=thumbnail)


def enqueue_job(action: str, payload: dict[str, Any]) -> str:
    mark_batch_activity()
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "id": job_id,
        "action": action,
        "label": describe_job_action(action),
        "target_account": str(payload.get("target_account") or ""),
        "status": "queued",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "started_at": None,
        "finished_at": None,
        "stage": "Queued",
        "progress_detail": "Waiting in queue",
        "logs": [f"{datetime.now().strftime('%H:%M:%S')} Queued"],
    }

    JOB_STORE.save(JOBS[job_id], payload)

    JOB_QUEUE.put((job_id, action, payload))
    ensure_worker_running()
    return job_id


def mark_batch_activity() -> None:
    global BATCH_NOTIFY_ACTIVE, BATCH_NOTIFY_STARTED_AT
    with BATCH_NOTIFY_LOCK:
        if not BATCH_NOTIFY_ACTIVE:
            BATCH_NOTIFY_STARTED_AT = datetime.now().isoformat(timespec="seconds")
        BATCH_NOTIFY_ACTIVE = True


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict[str, Any]:
    if job_id not in JOBS:
        return {
            "id": job_id,
            "action": "unknown",
            "status": "failed",
            "created_at": "",
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "logs": [
                "Job not found in memory.",
                "Backend may have restarted or worker state was cleared.",
                "Refresh the page and run the action again if needed.",
            ],
        }
    return enrich_status_job(JOBS[job_id], load_config(ROOT))


@app.post("/api/jobs/{job_id}/resume")
def api_resume_job(job_id: str) -> dict[str, str]:
    stored = JOBS.get(job_id)
    if not stored:
        raise HTTPException(status_code=404, detail="Job not found")
    if stored.get("status") != "interrupted":
        raise HTTPException(status_code=400, detail="Only interrupted jobs can be resumed")
    action = str(stored.get("action") or "")
    payload = JOB_STORE.payload_for(job_id)
    if action == "story-before-sleep-auto":
        checkpoint = build_sleep_story_resume_checkpoint(stored, payload)
        if checkpoint is None:
            raise HTTPException(status_code=404, detail="No complete Sleep Story media checkpoint was found")
        payload["_resume_sleep_checkpoint"] = str(checkpoint)
        resumed_id = enqueue_job(action, payload)
        JOBS[resumed_id]["resumed_from"] = job_id
        JOBS[resumed_id]["progress"] = 84
        JOBS[resumed_id]["progress_detail"] = f"Resume media from {checkpoint.name}"
        JOB_STORE.save(JOBS[resumed_id], payload)
        return {"job_id": resumed_id, "checkpoint": str(checkpoint)}
    if action != "fullauto-long-start":
        raise HTTPException(status_code=400, detail="Resume is supported for interrupted long and Sleep Story jobs")
    checkpoint = find_interrupted_long_checkpoint(stored)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="No usable long-video checkpoint was found")
    payload["_resume_checkpoint"] = str(checkpoint)
    payload["target_account"] = str(stored.get("target_account") or payload.get("target_account") or "")
    resumed_id = enqueue_job(action, payload)
    JOBS[resumed_id]["resumed_from"] = job_id
    JOBS[resumed_id]["progress_detail"] = f"Resume from {checkpoint.name}"
    JOB_STORE.save(JOBS[resumed_id], payload)
    return {"job_id": resumed_id, "checkpoint": str(checkpoint)}


def build_sleep_story_resume_checkpoint(job: dict[str, Any], payload: dict[str, Any]) -> Path | None:
    config = load_config(ROOT)
    paths = story_before_sleep_paths(config.data)
    created = parse_iso_timestamp(str(job.get("created_at") or ""))
    finished = parse_iso_timestamp(str(job.get("finished_at") or ""))
    created_naive = created.replace(tzinfo=None)
    finished_naive = finished.replace(tzinfo=None)
    prefix = created_naive.strftime("%Y%m%d-%H%M%S")
    log_files = sorted((paths["drafts"] / "logs").glob(f"{prefix}-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not log_files:
        return None
    run_log = log_files[0]
    generated_dir: Path | None = None
    for line in run_log.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("stage") == "scene_prompt_files" and event.get("status") == "saved":
            candidate = Path(str((event.get("details") or {}).get("path") or ""))
            if candidate.exists():
                generated_dir = candidate
    if generated_dir is None:
        return None
    scene_prompts = generated_dir / "scene-prompts.json"
    if not scene_prompts.exists():
        return None
    try:
        prompt_data = json.loads(scene_prompts.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    story_title = str(prompt_data.get("title") or "").strip()
    image_count = bounded_int(payload.get("image_count"), 8, 1, 32)
    real_images = [path for path in generated_dir.glob("scene-*.png") if "candidate" not in path.stem and "placeholder" not in path.stem]
    if len(real_images) < image_count:
        return None

    cache_candidates: list[Path] = []
    for path in (paths["drafts"] / "cache").glob("story_writer-*.json"):
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        if modified < created_naive or modified > finished_naive:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("title") or "").strip() == story_title and str(data.get("script") or "").strip():
            cache_candidates.append(path)
    if not cache_candidates:
        return None
    story_cache = max(cache_candidates, key=lambda path: path.stat().st_mtime)

    output_dir = paths["output"]
    audio_candidates = []
    for path in output_dir.glob("story-before-sleep-*.mp3"):
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        if created_naive <= modified <= finished_naive and path.stat().st_size > 100_000:
            audio_candidates.append(path)
    if not audio_candidates:
        return None
    audio_path = max(audio_candidates, key=lambda path: path.stat().st_mtime)
    subtitle_path = audio_path.with_suffix(".auto.srt")
    if not subtitle_path.exists() or subtitle_path.stat().st_size < 100:
        return None

    checkpoint = paths["drafts"] / "logs" / f"{run_log.stem}.resume.json"
    checkpoint.write_text(
        json.dumps(
            {
                "run_id": run_log.stem,
                "source_job_id": str(job.get("id") or ""),
                "title": story_title,
                "story_cache": str(story_cache.resolve()),
                "generated_dir": str(generated_dir.resolve()),
                "scene_prompts": str(scene_prompts.resolve()),
                "audio_path": str(audio_path.resolve()),
                "subtitle_path": str(subtitle_path.resolve()),
                "image_count": image_count,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return checkpoint


def find_interrupted_long_checkpoint(job: dict[str, Any]) -> Path | None:
    created_text = str(job.get("created_at") or "")
    try:
        created = datetime.fromisoformat(created_text)
    except ValueError:
        created = datetime.now()
    candidates: list[tuple[float, int, Path]] = []
    channels_root = ROOT / "data" / "input" / "buddhist" / "channels"
    for checkpoint in channels_root.glob("*/fullauto-long/drafts/*"):
        if not checkpoint.is_dir() or not (checkpoint / "outline.json").exists():
            continue
        match = re.match(r"(\d{8}-\d{6})", checkpoint.name)
        if not match:
            continue
        try:
            timestamp = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        distance = abs((timestamp - created.replace(tzinfo=None)).total_seconds())
        chapters = len(list(checkpoint.glob("chapter-*.txt")))
        if distance <= 300 and chapters > 0:
            candidates.append((distance, -chapters, checkpoint))
    return min(candidates, default=(0, 0, None), key=lambda item: (item[0], item[1]))[2]


@app.post("/api/open-folder/{folder}")
def api_open_folder(folder: str) -> dict[str, str]:
    config = load_config(ROOT)
    paths = config.paths
    fullauto_paths = fullauto_folder_paths(config)
    sleep_paths = story_before_sleep_paths(config.data)
    target = {
        "audio": paths["audio_dir"],
        "images": paths["image_dir"],
        "short-images": paths.get("short_image_dir", paths["image_dir"]),
        "thumbnails": paths["thumbnail_dir"],
        "output": paths["output_dir"],
        "fullauto-prompts": fullauto_paths["short_prompts"],
        "fullauto-images": fullauto_paths["short_images"],
        "fullauto-drafts": fullauto_paths["short_drafts"],
        "fullauto-long-prompts": fullauto_paths["long_prompts"],
        "fullauto-long-images": fullauto_paths["long_images"],
        "fullauto-long-drafts": fullauto_paths["long_drafts"],
        "fullauto-long-effects": fullauto_paths["effects"],
        "fullauto-long-wave": fullauto_paths["wave"],
        "fullauto-long-stickers": fullauto_paths["stickers"],
        "fullauto-20min-prompts": fullauto_paths["twenty_min_prompts"],
        "fullauto-20min-images": fullauto_paths["twenty_min_images"],
        "fullauto-20min-drafts": fullauto_paths["twenty_min_drafts"],
        "thumbnail-refs-normal": thumbnail_reference_dir(config, "normal-16x9"),
        "thumbnail-refs-short": thumbnail_reference_dir(config, "short-9x12"),
        "youtube-research": ROOT / "data" / "research" / "youtube",
        "sleep-prompts": sleep_paths["prompts"],
        "sleep-references": sleep_paths["references"],
        "sleep-images": sleep_paths["images"],
        "sleep-generated": sleep_paths["generated"],
        "sleep-drafts": sleep_paths["drafts"],
        "sleep-output": sleep_paths["output"],
    }.get(folder)
    if target is None:
        raise HTTPException(status_code=404, detail="Unknown folder")
    target.mkdir(parents=True, exist_ok=True)
    os.startfile(target)  # type: ignore[attr-defined]
    return {"status": "opened"}


def ensure_worker_running() -> None:
    global WORKER_THREAD
    with WORKER_LOCK:
        if WORKER_THREAD and WORKER_THREAD.is_alive():
            return
        WORKER_THREAD = threading.Thread(target=job_worker, daemon=True)
        WORKER_THREAD.start()


def restore_persisted_jobs() -> None:
    """Restore history and only replay jobs that never started."""
    JOB_STORE.mark_interrupted_runs()
    for stored_job, payload in JOB_STORE.load_recent(limit=100):
        job_id = str(stored_job.get("id") or "").strip()
        action = str(stored_job.get("action") or "").strip()
        if not job_id or not action:
            continue
        JOBS[job_id] = stored_job
        if stored_job.get("status") == "queued":
            stored_job.setdefault("logs", []).append(
                f"{datetime.now().strftime('%H:%M:%S')} Restored queued job after backend restart."
            )
            JOB_STORE.save(stored_job, payload)
            JOB_QUEUE.put((job_id, action, payload))
    if not JOB_QUEUE.empty():
        ensure_worker_running()


@app.on_event("startup")
def restore_jobs_on_startup() -> None:
    restore_persisted_jobs()


def job_worker() -> None:
    while True:
        try:
            job_id, action, payload = JOB_QUEUE.get_nowait()
        except queue.Empty:
            maybe_notify_all_jobs_complete()
            return
        try:
            run_action(job_id, action, payload)
        finally:
            JOB_QUEUE.task_done()
            maybe_notify_all_jobs_complete()


def maybe_notify_all_jobs_complete() -> None:
    global BATCH_NOTIFY_ACTIVE, BATCH_NOTIFY_STARTED_AT
    with BATCH_NOTIFY_LOCK:
        if not BATCH_NOTIFY_ACTIVE:
            return
        if not JOB_QUEUE.empty():
            return
        if any(job.get("status") in {"queued", "running"} for job in JOBS.values()):
            return
        started_at = BATCH_NOTIFY_STARTED_AT
        BATCH_NOTIFY_ACTIVE = False
        BATCH_NOTIFY_STARTED_AT = ""
    notify_all_jobs_complete(started_at)


def notify_all_jobs_complete(started_at: str) -> None:
    try:
        config = load_config(ROOT)
    except Exception:
        config = None
    jobs = list(JOBS.values())
    finished_jobs = [job for job in jobs if job.get("finished_at")]
    failed_jobs = [job for job in finished_jobs if job.get("status") == "failed"]
    done_jobs = [job for job in finished_jobs if job.get("status") == "done"]
    latest = sorted(
        finished_jobs,
        key=lambda item: str(item.get("finished_at") or ""),
        reverse=True,
    )[:8]
    lines = [
        "He thong da hoan thanh xong tat ca cac video.",
        "",
        f"Bat dau dot job: {started_at or '(khong ro)'}",
        f"Ket thuc: {datetime.now().isoformat(timespec='seconds')}",
        f"Tong job da xong: {len(finished_jobs)}",
        f"Thanh cong: {len(done_jobs)}",
        f"That bai: {len(failed_jobs)}",
        "",
        "Job gan nhat:",
    ]
    for job in latest:
        action = str(job.get("action") or "")
        label = describe_job_action(action)
        account = ""
        if config is not None:
            target_account = str(job.get("target_account") or notification_account_id(config, action))
            account = get_accounts(config).get(target_account, {}).get("label", target_account)
        latest_log = (job.get("logs") or [""])[-1]
        lines.append(f"- {label}{f' - {account}' if account else ''}: {job.get('status')} | {latest_log}")
    send_email_notification(
        "He thong da hoan thanh xong tat ca cac video",
        "\n".join(lines),
        notification_type="system",
        force=True,
    )


def run_action(job_id: str, action: str, payload: dict[str, Any]) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = datetime.now().isoformat(timespec="seconds")
    JOB_STORE.save(job, payload)
    log(job, "Started")
    try:
        if action in {"fullauto-start", "fullauto-long-start", "fullauto-20min-start"}:
            wait_for_external_resume_render(job)
        config = load_config(ROOT)
        paths = config.paths
        state = StateStore(account_state_dir(config))
        all_tracks = discover_tracks(paths["audio_dir"], paths["image_dir"])
        shorts_enabled = bool(config.get("shorts", "enabled", default=False))
        active_account = get_active_account_id(config)
        other_states = [
            StateStore(state_dir)
            for account_id, state_dir in account_state_dirs(config).items()
            if account_id != active_account
        ]

        if action == "tts":
            output = generate_voice(
                text=payload["text"],
                title=payload["title"],
                voice=payload["voice"],
                output_dir=paths["audio_dir"],
            )
            log(job, f"Generated voice {output.name}")
            mark_done(job, config, action)
            return

        if action == "sync-state":
            removed = state.prune_missing_audio()
            youtube_removed = 0
            try:
                service = get_youtube_service(paths["credentials_file"], account_token_path(config))
                youtube_removed = sync_missing_youtube_uploads(state, service, lambda message: log(job, message))
            except Exception as exc:
                log(job, f"Skip YouTube upload sync: {exc}")
            log(job, f"Removed {removed} stale local item(s), {youtube_removed} deleted YouTube upload record(s).")
            mark_done(job, config, action)
            return

        if action == "create-collection":
            output = create_collection(
                tracks=all_tracks,
                output_dir=paths["output_dir"],
                state_dir=paths["state_dir"],
                collection_config=config.get("collection", default={}),
            )
            log(job, f"Created collection {output.name}")
            mark_done(job, config, action)
            return

        if action == "fullauto-bulk":
            run_fullauto_bulk_job(job, payload, config)
            config = load_config(ROOT)
            mark_done(job, config, action)
            return

        if action == "youtube-research":
            output = run_youtube_research_job(job, payload)
            log(job, f"YouTube research saved: {output['report_path']}")
            mark_done(job, config, action)
            return

        if action == "view-optimizer":
            output = generate_view_optimizer_report(ROOT, limit=bounded_int(payload.get("limit"), 80, 10, 200))
            log(job, f"View optimizer saved: {output['report_path']}")
            mark_done(job, config, action)
            return

        if action == "youtube-analytics-sync":
            output = sync_youtube_analytics(
                root=ROOT,
                config=config,
                days=bounded_int(payload.get("days"), 90, 7, 365),
                limit=bounded_int(payload.get("limit"), 120, 1, 500),
                log=lambda message: log(job, message),
            )
            log(job, f"YouTube Analytics saved: {output['report_path']}")
            mark_done(job, config, action)
            return

        if action == "story-before-sleep-test":
            output = run_story_before_sleep_test(
                job=job,
                config=config.data,
                title=str(payload.get("title") or ""),
                prompt=str(payload.get("prompt") or ""),
                target_minutes=bounded_int(payload.get("target_minutes"), 10, 1, 30),
                voice=str(payload.get("voice") or ""),
                image_count=bounded_int(payload.get("image_count"), 8, 1, 32),
                wait_for_images=bool(payload.get("wait_for_images", False)),
            )
            job["output_video"] = output.get("video_name") or ""
            job["output_audio"] = output.get("audio_name") or ""
            job["output_markdown"] = output.get("markdown_name") or ""
            job["generated_title"] = output.get("title") or ""
            log(job, f"Story Before Sleep test video finished: {job['output_video']}")
            mark_done(job, config, action)
            return

        if action == "story-before-sleep-auto":
            from .automation.sleep_story_pipeline import resume_sleep_story_automation, run_sleep_story_automation

            sleep_settings = dict(config.data.get("story_before_sleep") or {})
            sleep_account = str(sleep_settings.get("upload_account") or "football").strip() or "football"
            sleep_label = get_accounts(config).get(sleep_account, {}).get("label", sleep_account)
            log(job, f"Sleep Story channel is fixed to {sleep_label} ({sleep_account}).")

            stage_progress = {
                "START sleep_story_automation": 5,
                "START story_planner": 10,
                "END story_planner": 18,
                "START story_writer": 20,
                "END story_writer": 32,
                "START story_reviewer": 34,
                "END story_reviewer": 44,
                "START emotion_analyzer": 46,
                "END emotion_analyzer": 52,
                "START scene_planner": 54,
                "END scene_planner": 60,
                "START prompt_optimizer": 62,
                "END prompt_optimizer": 68,
                "START scene_prompt_files": 69,
                "START image_generation": 70,
                "END image_generation": 84,
                "START choose_images": 85,
                "START review_images": 86,
                "START thumbnail_generator": 87,
                "START metadata_generator": 88,
                "START voice_agent": 90,
                "START speech_qa": 92,
                "START qa_agent": 94,
                "START render_agent": 96,
                "START final_media_qa": 98,
            }

            def sleep_auto_log(message: str) -> None:
                log(job, message)
                for marker, progress in stage_progress.items():
                    if marker in message:
                        job["progress"] = progress
                        job["stage"] = marker
                        break
                image_match = re.search(r"scene\s+(\d+)/(\d+)", message)
                if image_match:
                    current = int(image_match.group(1))
                    total = max(1, int(image_match.group(2)))
                    job["progress"] = max(70, min(84, 70 + int(current * 14 / total)))
                    job["stage"] = f"imagegen_local {current}/{total}"

            sleep_resume_checkpoint = str(payload.get("_resume_sleep_checkpoint") or "").strip()
            if sleep_resume_checkpoint:
                log(job, f"Sleep Story resume requested from media checkpoint: {Path(sleep_resume_checkpoint).name}")
                job["progress"] = 84
                output = resume_sleep_story_automation(
                    config=config.data,
                    checkpoint_path=sleep_resume_checkpoint,
                    target_minutes=bounded_int(payload.get("target_minutes"), 10, 1, 30),
                    voice=str(payload.get("voice") or ""),
                    image_count=bounded_int(payload.get("image_count"), 8, 1, 32),
                    emit_log=sleep_auto_log,
                )
            else:
                output = run_sleep_story_automation(
                    config=config.data,
                    title=str(payload.get("title") or ""),
                    prompt=str(payload.get("prompt") or ""),
                    target_minutes=bounded_int(payload.get("target_minutes"), 10, 1, 30),
                    voice=str(payload.get("voice") or ""),
                    image_count=bounded_int(payload.get("image_count"), 8, 1, 32),
                    wait_for_images=bool(payload.get("wait_for_images", False)),
                    emit_log=sleep_auto_log,
                )
            job["output_video"] = output.video_path.name if output.video_path else ""
            job["output_audio"] = output.voice.path.name if output.voice else ""
            job["output_markdown"] = output.draft_markdown.name if output.draft_markdown else ""
            job["generated_title"] = output.story.title if output.story else ""
            if bool((config.data.get("story_before_sleep") or {}).get("auto_upload", False)):
                upload_info = upload_sleep_story_output(job, config, paths, state, output)
                job["youtube_id"] = upload_info.get("youtube_id", "")
                job["youtube_url"] = upload_info.get("youtube_url", "")
                job["publish_at"] = upload_info.get("publish_at", "")
            log(job, f"Story Before Sleep Auto finished: {job['output_video']}")
            mark_done(job, config, action)
            return

        if action in {
            "fullauto-start",
            "fullauto-long-start",
            "fullauto-20min-start",
            "fullauto-merge-1hour",
            "fullauto-merge-upload-1hour",
            "fullauto-merge-long-selected",
            "fullauto-merge-upload-long-selected",
        }:
            target_account = str(payload.get("target_account") or get_active_account_id(config) or "").strip()
            if target_account and target_account != get_active_account_id(config):
                config.data["active_account"] = target_account
                (ROOT / "config.json").write_text(
                    json.dumps(config.data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                config = load_config(ROOT)
                paths = config.paths
                state = StateStore(account_state_dir(config))

            resume_checkpoint = str(payload.get("_resume_checkpoint") or "").strip()
            if action == "fullauto-long-start" and resume_checkpoint:
                config.data.setdefault("fullauto", {})["_resume_checkpoint"] = resume_checkpoint
                log(job, f"Resume requested from checkpoint: {Path(resume_checkpoint).name}")

            recovered = configured_recovered_module(config)
            account_label = get_accounts(config).get(get_active_account_id(config), {}).get("label", get_active_account_id(config))
            fullauto_section = dict(config.get("fullauto", default={}) or {})
            provider = str(fullauto_section.get("provider") or "")
            model = str(fullauto_section.get("ollama_model") if provider == "ollama" else fullauto_section.get("gemini_model") or "")
            log(job, f"{describe_job_action(action)} started for {account_label}.")
            if provider or model:
                log(job, f"Model: {provider}/{model}".strip("/"))
            if action == "fullauto-start":
                job["stage"] = "Creating shorts"
                job["progress_detail"] = f"Shorts pipeline for {account_label}"
                count = recovered.run_fullauto_story_job(job, config, paths, state, upload_config=config)
                log(job, f"Full Auto Story finished with {count} short(s).")
            elif action == "fullauto-long-start":
                job["stage"] = "Creating long video"
                job["progress_detail"] = f"Long video pipeline for {account_label}"
                output = recovered.run_fullauto_long_job(job, config, paths, state, upload_config=config)
                log(job, f"Full Auto Long finished: {Path(output).name}")
            elif action == "fullauto-merge-1hour":
                job["stage"] = "Merging 1-hour video"
                output = create_fullauto_stage1_merge(job, config, state)
                log(job, f"1-hour merge finished: {Path(output).name}")
            elif action == "fullauto-merge-upload-1hour":
                job["stage"] = "Merging and uploading 1-hour video"
                output, video_id = create_and_upload_fullauto_stage1_merge(job, config, state)
                log(job, f"1-hour merge uploaded: {Path(output).name} -> https://www.youtube.com/watch?v={video_id}")
            elif action == "fullauto-merge-long-selected":
                job["stage"] = "Merging selected long videos"
                output = merge_selected_fullauto_long_videos(job, config, state, payload.get("filenames") or [])
                log(job, f"Selected long merge finished: {Path(output).name}")
            elif action == "fullauto-merge-upload-long-selected":
                job["stage"] = "Merging and uploading selected long videos"
                output, video_id = merge_upload_selected_fullauto_long_videos(
                    job,
                    config,
                    state,
                    payload.get("filenames") or [],
                )
                log(job, f"Selected long merge uploaded: {Path(output).name} -> https://www.youtube.com/watch?v={video_id}")
            else:
                job["stage"] = "Creating 20-minute video"
                job["progress_detail"] = f"20-Min pipeline for {account_label}"
                output = recovered.run_fullauto_twenty_min_job(job, config, paths, state, upload_config=config)
                log(job, f"Full Auto 20-Min finished: {Path(output).name}")
            mark_done(job, config, action)
            return

        if action.startswith("track-"):
            track_action = action.removeprefix("track-")
            track = find_track_by_audio(all_tracks, payload["audio"])
            if track is None:
                raise ValueError(f"Audio not found: {payload['audio']}")
            if track_action == "delete":
                if state.uploads_for(track.audio_path) or uploaded_in_any_state(track.audio_path, other_states):
                    raise ValueError("Cannot delete a track that has already been uploaded.")
                deleted = delete_track_files(track, paths, state, config)
                log(job, f"Deleted {deleted} local file(s) for {track.audio_path.name}")
                mark_done(job, config, action)
                return
            if track_action == "skip":
                state.mark_processed(track.audio_path)
                log(job, f"Skipped {track.audio_path.name}")
                mark_done(job, config, action)
                return
            if track_action in {"render", "rerender"}:
                output = render_video(track, paths["output_dir"], config.get("render"))
                log(job, f"Rendered {output.name}")
                if config.get("shorts", "enabled", default=False):
                    short_output = render_short(track, paths["output_dir"], config)
                    log(job, f"Rendered {short_output.name}")
                mark_done(job, config, action)
                return
            upload_tracks(
                config=config,
                paths=paths,
                state=state,
                tracks=[track],
                schedule=True,
                dry_run=track_action == "dry-run",
                upload_types=track_upload_types(track_action),
            )
            log(job, "Selected dry run finished." if track_action == "dry-run" else "Selected upload finished.")
            mark_done(job, config, action)
            return

        tracks = [
            track
            for track in all_tracks
            if not uploaded_in_any_state(track.audio_path, other_states)
            if state.needs_work(track.audio_path, shorts_enabled)
        ]
        if not tracks:
            log(job, "No new tracks found.")
            mark_done(job, config, action)
            return

        if action == "render":
            for track in tracks:
                output = render_video(track, paths["output_dir"], config.get("render"))
                log(job, f"Rendered {output.name}")
                if config.get("shorts", "enabled", default=False):
                    short_output = render_short(track, paths["output_dir"], config)
                    log(job, f"Rendered {short_output.name}")
            mark_done(job, config, action)
            return

        upload_tracks(
            config=config,
            paths=paths,
            state=state,
            tracks=tracks,
            schedule=True,
            dry_run=action == "daily-dry-run",
        )
        if action == "daily-dry-run":
            log(job, "Dry run finished.")
        else:
            log(job, "Upload job finished.")
        mark_done(job, config, action)
    except Exception as exc:  # noqa: BLE001 - surfaced to local operator UI.
        job["status"] = "failed"
        job["finished_at"] = datetime.now().isoformat(timespec="seconds")
        log(job, f"Error: {exc}")
        JOB_STORE.save(job, payload)
        notify_job_email(config, job, action, "failure")


def run_fullauto_bulk_job(job: dict[str, Any], payload: dict[str, Any], base_config) -> None:
    accounts = [
        account_id
        for account_id in payload.get("accounts", ["account1", "account2", "account3"])
        if str(account_id or "").strip() in {"account1", "account2", "account3"}
    ]
    short_count = bounded_int(payload.get("short_count"), default=0, minimum=0, maximum=10)
    twenty_min_count = bounded_int(payload.get("twenty_min_count"), default=0, minimum=0, maximum=5)
    long_count = bounded_int(payload.get("long_count"), default=0, minimum=0, maximum=3)
    if not accounts:
        raise ValueError("No Buddhist channels selected for Full Auto bulk run.")
    if short_count + twenty_min_count + long_count < 1:
        raise ValueError("Choose at least one video to create.")

    original_active = get_active_account_id(base_config)
    original_upload = str(base_config.get("fullauto", "upload_account", default=original_active) or original_active)
    successes = 0
    failures: list[str] = []

    log(
        job,
        f"Bulk plan: {len(accounts)} channel(s), shorts={short_count}, 20min={twenty_min_count}, long={long_count}.",
    )
    try:
        for account_id in accounts:
            config = load_config(ROOT)
            account_label = get_accounts(config).get(account_id, {}).get("label", account_id)
            log(job, f"Switching to {account_label} ({account_id}).")
            config.data["active_account"] = account_id
            config.data.setdefault("fullauto", {})["upload_account"] = account_id
            write_config_file(config)
            config = load_config(ROOT)
            paths = config.paths
            state = StateStore(account_state_dir(config))

            if short_count:
                try:
                    short_config = load_config(ROOT)
                    short_config.data.setdefault("fullauto", {})["videos_per_run"] = short_count
                    short_config.data.setdefault("fullauto", {})["upload_account"] = account_id
                    recovered = configured_recovered_module(short_config)
                    count = recovered.run_fullauto_story_job(job, short_config, short_config.paths, state, upload_config=short_config)
                    successes += int(count or 0)
                    log(job, f"{account_label}: created {count} Short(s).")
                except Exception as exc:  # noqa: BLE001 - keep other channels moving.
                    message = f"{account_label} Shorts failed: {exc}"
                    failures.append(message)
                    log(job, message)

            for index in range(twenty_min_count):
                try:
                    config = load_config(ROOT)
                    config.data["active_account"] = account_id
                    config.data.setdefault("fullauto", {})["upload_account"] = account_id
                    recovered = configured_recovered_module(config)
                    output = recovered.run_fullauto_twenty_min_job(job, config, config.paths, state, upload_config=config)
                    successes += 1
                    log(job, f"{account_label}: created 20-min video {index + 1}/{twenty_min_count}: {Path(output).name}")
                except Exception as exc:  # noqa: BLE001
                    message = f"{account_label} 20-min {index + 1}/{twenty_min_count} failed: {exc}"
                    failures.append(message)
                    log(job, message)

            for index in range(long_count):
                try:
                    config = load_config(ROOT)
                    config.data["active_account"] = account_id
                    config.data.setdefault("fullauto", {})["upload_account"] = account_id
                    recovered = configured_recovered_module(config)
                    output = recovered.run_fullauto_long_job(job, config, config.paths, state, upload_config=config)
                    successes += 1
                    log(job, f"{account_label}: created long video {index + 1}/{long_count}: {Path(output).name}")
                except Exception as exc:  # noqa: BLE001
                    message = f"{account_label} long {index + 1}/{long_count} failed: {exc}"
                    failures.append(message)
                    log(job, message)
    finally:
        config = load_config(ROOT)
        config.data["active_account"] = original_active
        config.data.setdefault("fullauto", {})["upload_account"] = original_upload
        write_config_file(config)
        log(job, f"Restored active account to {original_active}.")

    if failures:
        log(job, f"Bulk finished with {successes} successful item(s) and {len(failures)} failure(s).")
        raise ValueError("; ".join(failures[:3]))
    log(job, f"Bulk finished successfully with {successes} created item(s).")


def write_config_file(config) -> None:
    (ROOT / "config.json").write_text(
        json.dumps(config.data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def upload_sleep_story_output(job: dict[str, Any], config, paths: dict[str, Path], state: StateStore, output) -> dict[str, str]:
    if not output.video_path or not output.video_path.exists():
        raise RuntimeError("Sleep Story upload skipped: video file is missing.")
    if not output.voice or not output.voice.path.exists():
        raise RuntimeError("Sleep Story upload skipped: audio file is missing.")
    if not output.metadata:
        raise RuntimeError("Sleep Story upload skipped: metadata is missing.")

    sleep_settings = dict(config.data.get("story_before_sleep") or {})
    accounts = get_accounts(config)
    target_account = str(sleep_settings.get("upload_account") or get_active_account_id(config) or "").strip()
    if target_account not in accounts:
        raise RuntimeError(f"Sleep Story upload account is invalid: {target_account!r}.")
    upload_config_data = deepcopy(config.data)
    upload_config_data["active_account"] = target_account
    upload_config = AppConfig(data=upload_config_data, root=config.root)
    upload_paths = upload_config.paths
    upload_state = StateStore(account_state_dir(upload_config))
    account_label = accounts.get(target_account, {}).get("label", target_account)
    schedule_upload = bool(sleep_settings.get("schedule_upload", True))
    log(job, f"Preparing Sleepu Stories YouTube upload for account {target_account} ({account_label}).")
    service = get_youtube_service(upload_paths["credentials_file"], account_token_path(upload_config))

    publish_at = None
    if schedule_upload:
        publish_time = reserve_next_publish_time(
            config=upload_config,
            blocked_times=upload_state.used_publish_times(),
            blocked_dates=set(),
            service=service,
            youtube_date_counts={},
            slot_kind="normal",
        )
        publish_at = to_rfc3339_utc(publish_time) if publish_time else None

    channel = upload_config.get("channel", default={}) or {}
    metadata = VideoMetadata(
        title=output.metadata.title,
        description=output.metadata.description,
        tags=output.metadata.tags,
        category_id=str(channel.get("category_id") or "22"),
        made_for_kids=bool(channel.get("made_for_kids", False)),
        thumbnail_path=output.metadata.thumbnail_path,
    )
    privacy = str(channel.get("privacy_status") or "private")
    if not schedule_upload:
        privacy = str(upload_config.get("schedule", "upload_privacy_when_no_schedule", default=privacy) or privacy)

    def upload_progress(sent_bytes: int, total_bytes: int, state_text: str) -> None:
        total = max(total_bytes, 1)
        percent = max(0, min(100, int(sent_bytes * 100 / total)))
        job["progress"] = max(96, min(99, 96 + int(percent * 0.03)))
        job["stage"] = f"youtube_upload {percent}%"
        if state_text != "uploading":
            log(job, f"YouTube upload: {state_text}")

    video_id = upload_video(
        service=service,
        video_path=output.video_path,
        metadata=metadata,
        privacy_status=privacy,
        publish_at=publish_at,
        progress_callback=upload_progress,
    )
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    upload_state.add_upload(
        {
            "audio": str(output.voice.path.resolve()),
            "video": str(output.video_path.resolve()),
            "type": "normal",
            "youtube_id": video_id,
            "youtube_url": youtube_url,
            "publish_at": publish_at,
            "title": metadata.title,
            "mode": "sleep-story",
            "upload_account": target_account,
        }
    )
    log(job, f"Sleepu Stories uploaded to {account_label}: {youtube_url}")
    if publish_at:
        log(job, f"Scheduled publish at: {publish_at}")
    return {"youtube_id": video_id, "youtube_url": youtube_url, "publish_at": publish_at or ""}


def log(job: dict[str, Any], message: str) -> None:
    job["logs"].append(f"{datetime.now().strftime('%H:%M:%S')} {message}")
    if len(job["logs"]) > 300:
        job["logs"] = job["logs"][-300:]
    JOB_STORE.save(job)


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def describe_job_action(action: str) -> str:
    labels = {
        "render": "Render job",
        "daily-run": "Daily upload job",
        "daily-dry-run": "Daily dry-run job",
        "tts": "Story voice job",
        "sync-state": "Sync state job",
        "create-collection": "Create collection job",
        "fullauto-start": "Shorts job",
        "fullauto-long-start": "Long video job",
        "fullauto-20min-start": "20-Min video job",
        "fullauto-long-resume": "Long resume upload job",
        "fullauto-bulk": "Full Auto bulk job",
        "fullauto-merge-1hour": "Full Auto 1-hour merge job",
        "fullauto-merge-upload-1hour": "Full Auto 1-hour merge upload job",
        "youtube-research": "YouTube research job",
        "view-optimizer": "YouTube view optimizer job",
        "youtube-analytics-sync": "YouTube Analytics sync job",
        "story-before-sleep-test": "Story Before Sleep test job",
        "story-before-sleep-auto": "Sleepu Stories Auto Agent job",
    }
    if action.startswith("track-"):
        return f"Track action {action.removeprefix('track-')}"
    return labels.get(action, action.replace("-", " ").strip().title() or "Automation job")


def notify_job_email(config, job: dict[str, Any], action: str, status: str) -> None:
    active_account = notification_account_id(config, action)
    account_label = get_accounts(config).get(active_account, {}).get("label", active_account)
    subject_prefix = "Thanh cong" if status == "success" else "That bai"
    subject = f"{subject_prefix} - {describe_job_action(action)} - {account_label}"
    recent_logs = "\n".join(job.get("logs", [])[-12:]) or "(khong co log)"
    body = (
        f"Trang thai: {status}\n"
        f"Hanh dong: {action}\n"
        f"Tai khoan: {account_label}\n"
        f"Bat dau: {job.get('created_at', '')}\n"
        f"Ket thuc: {job.get('finished_at', '')}\n\n"
        f"Log gan nhat:\n{recent_logs}"
    )
    send_email_notification(subject, body, notification_type=status)


def notification_account_id(config, action: str) -> str:
    if action.startswith("story-before-sleep"):
        sleep_settings = dict(config.data.get("story_before_sleep") or {})
        account_id = str(sleep_settings.get("upload_account") or "").strip()
        if account_id:
            return account_id
    return get_active_account_id(config)


def mark_done(job: dict[str, Any], config=None, action: str | None = None) -> None:
    job["status"] = "done"
    job["finished_at"] = datetime.now().isoformat(timespec="seconds")
    JOB_STORE.save(job)
    if config is not None and action:
        notify_job_email(config, job, action, "success")


def safe_filename(value: str) -> str:
    keep = []
    for char in Path(value).name:
        if char.isalnum() or char in {" ", ".", "_", "-"}:
            keep.append(char)
    return "".join(keep).strip() or "upload"


def safe_slug(value: str, fallback: str = "item") -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug[:80] or fallback


def run_youtube_research_job(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, str]:
    channel_url = str(payload.get("channel_url") or "").strip()
    tab = str(payload.get("tab") or "shorts").strip().lower()
    limit = bounded_int(payload.get("limit"), default=24, minimum=1, maximum=80)
    transcript_limit = bounded_int(payload.get("transcript_limit"), default=8, minimum=0, maximum=30)
    target_url = research_tab_url(channel_url, tab)
    log(job, f"Crawling {target_url} ({limit} item limit).")
    records = fetch_youtube_records(target_url, limit, job)
    if not records:
        raise ValueError("No public videos found. Check the channel URL or try another tab.")
    log(job, f"Fetched {len(records)} video metadata record(s).")

    for index, record in enumerate(records[:transcript_limit], start=1):
        hook = fetch_transcript_hook(str(record.get("id") or ""), preferred_languages=("vi", "en"))
        if hook:
            record["transcript_hook"] = hook
            log(job, f"Transcript hook found {index}/{min(transcript_limit, len(records))}: {record.get('title', '')[:52]}")

    analysis = analyze_youtube_records(records)
    research_dir = ROOT / "data" / "research" / "youtube"
    research_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    channel_slug = safe_slug(channel_url.split("/")[-1] or "channel", "channel")
    base = f"{stamp}-{channel_slug}-{tab}"
    json_path = research_dir / f"{base}.json"
    report_path = research_dir / f"{base}.md"
    payload_out = {
        "source_url": channel_url,
        "target_url": target_url,
        "tab": tab,
        "limit": limit,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "analysis": analysis,
        "records": records,
    }
    json_path.write_text(json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(render_youtube_research_report(payload_out), encoding="utf-8")
    return {"json_path": str(json_path), "report_path": str(report_path)}


def research_tab_url(channel_url: str, tab: str) -> str:
    url = channel_url.strip().split("?", 1)[0].rstrip("/")
    if tab == "all":
        return url
    if re.search(r"/(videos|shorts|streams|playlists|community)$", url, flags=re.IGNORECASE):
        url = re.sub(r"/(videos|shorts|streams|playlists|community)$", "", url, flags=re.IGNORECASE)
    return f"{url}/{tab}"


def fetch_youtube_records(target_url: str, limit: int, job: dict[str, Any]) -> list[dict[str, Any]]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--ignore-errors",
        "--no-warnings",
        "--dump-json",
        "--playlist-end",
        str(limit),
        target_url,
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if result.returncode != 0 and result.stderr.strip():
        log(job, f"yt-dlp warning: {result.stderr.strip()[-500:]}")
    records: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        records.append(compact_youtube_record(item))
    return records


def compact_youtube_record(item: dict[str, Any]) -> dict[str, Any]:
    thumbnails = item.get("thumbnails") if isinstance(item.get("thumbnails"), list) else []
    thumbnail_url = ""
    if thumbnails:
        best = max(thumbnails, key=lambda thumb: int(thumb.get("width") or 0) * int(thumb.get("height") or 0))
        thumbnail_url = str(best.get("url") or "")
    if not thumbnail_url:
        thumbnail_url = str(item.get("thumbnail") or "")
    timestamp = item.get("timestamp") or item.get("release_timestamp")
    upload_date = str(item.get("upload_date") or "")
    uploaded_at = ""
    if timestamp:
        try:
            uploaded_at = datetime.fromtimestamp(int(timestamp)).isoformat(timespec="seconds")
        except Exception:
            uploaded_at = ""
    return {
        "id": item.get("id"),
        "url": item.get("webpage_url") or item.get("original_url") or item.get("url"),
        "title": item.get("title") or "",
        "description": short_text(item.get("description") or "", 500),
        "duration": item.get("duration"),
        "view_count": item.get("view_count"),
        "like_count": item.get("like_count"),
        "comment_count": item.get("comment_count"),
        "upload_date": upload_date,
        "uploaded_at": uploaded_at,
        "weekday": upload_weekday(upload_date, uploaded_at),
        "upload_hour": upload_hour(uploaded_at),
        "thumbnail_url": thumbnail_url,
        "channel": item.get("channel") or item.get("uploader") or "",
    }


def fetch_transcript_hook(video_id: str, preferred_languages: tuple[str, ...] = ("vi", "en")) -> str:
    if not video_id:
        return ""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        transcript = YouTubeTranscriptApi().fetch(video_id, languages=preferred_languages)
        raw = transcript.to_raw_data() if hasattr(transcript, "to_raw_data") else list(transcript)
    except Exception:
        return ""
    text = " ".join(str(item.get("text") or "").replace("\n", " ") for item in raw[:8] if isinstance(item, dict))
    return short_text(re.sub(r"\s+", " ", text).strip(), 260)


def short_text(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def upload_weekday(upload_date: str, uploaded_at: str) -> str:
    try:
        if upload_date and len(upload_date) == 8:
            return datetime.strptime(upload_date, "%Y%m%d").strftime("%A")
        if uploaded_at:
            return datetime.fromisoformat(uploaded_at).strftime("%A")
    except Exception:
        return ""
    return ""


def upload_hour(uploaded_at: str) -> int | None:
    if not uploaded_at:
        return None
    try:
        return datetime.fromisoformat(uploaded_at).hour
    except Exception:
        return None


def analyze_youtube_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    top_records = sorted(records, key=lambda item: int(item.get("view_count") or 0), reverse=True)[:10]
    title_starters = Counter(first_words(str(item.get("title") or ""), 4) for item in records)
    hook_starters = Counter(first_words(str(item.get("transcript_hook") or item.get("description") or ""), 8) for item in records)
    weekdays = Counter(str(item.get("weekday") or "") for item in records if item.get("weekday"))
    hours = Counter(str(item.get("upload_hour")) for item in records if item.get("upload_hour") is not None)
    title_suffixes = Counter(title_suffix(str(item.get("title") or "")) for item in records)
    return {
        "video_count": len(records),
        "top_titles": [
            {
                "title": item.get("title"),
                "views": item.get("view_count"),
                "upload_date": item.get("upload_date"),
                "url": item.get("url"),
            }
            for item in top_records
        ],
        "common_title_starters": counter_rows(title_starters, 12),
        "common_hook_starters": counter_rows(hook_starters, 12),
        "common_title_suffixes": counter_rows(title_suffixes, 12),
        "upload_weekdays": counter_rows(weekdays, 7),
        "upload_hours": counter_rows(hours, 24),
        "thumbnail_notes": thumbnail_notes(records),
    }


def first_words(value: str, count: int) -> str:
    words = re.findall(r"[\wÀ-ỹ]+", value, flags=re.UNICODE)
    return " ".join(words[:count]).strip()


def title_suffix(value: str) -> str:
    parts = re.split(r"\s[-|]\s", value, maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def counter_rows(counter: Counter, limit: int) -> list[dict[str, Any]]:
    return [{"value": value, "count": count} for value, count in counter.most_common(limit) if value]


def thumbnail_notes(records: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    titles = " ".join(str(item.get("title") or "").lower() for item in records)
    if any(word in titles for word in ("bình an", "an lạc", "chữa lành", "tâm")):
        notes.append("Nội dung thường xoay quanh bình an, an lạc, chữa lành, tâm trí.")
    if any(word in titles for word in ("tài lộc", "may mắn", "duyên lành", "phước")):
        notes.append("Có thể dùng cụm hứa hẹn mềm: duyên lành, phước lành, tài lộc, may mắn.")
    notes.append("Thumbnail nên giữ một hình chính rõ: tượng Phật/người thiền/chùa/hoa sen, chữ lớn 2-5 từ.")
    notes.append("Ưu tiên tương phản chữ vàng-trắng trên nền sáng ấm, tránh che quá nhiều ảnh nền.")
    return notes


def render_youtube_research_report(data: dict[str, Any]) -> str:
    analysis = data.get("analysis", {})
    records = data.get("records", [])
    lines = [
        "# YouTube Channel Research",
        "",
        f"- Source: {data.get('source_url')}",
        f"- Crawled URL: {data.get('target_url')}",
        f"- Tab: {data.get('tab')}",
        f"- Videos: {analysis.get('video_count', len(records))}",
        f"- Created: {data.get('created_at')}",
        "",
        "## Top Ideas/Titles",
    ]
    for item in analysis.get("top_titles", []):
        lines.append(f"- {item.get('title')} ({item.get('views') or 0} views, {item.get('upload_date') or 'no date'})")
    lines += ["", "## Title Starters"]
    lines += [f"- {row['value']} ({row['count']})" for row in analysis.get("common_title_starters", [])] or ["- No pattern found."]
    lines += ["", "## Hook Starters"]
    lines += [f"- {row['value']} ({row['count']})" for row in analysis.get("common_hook_starters", [])] or ["- No transcript/description hook found."]
    lines += ["", "## Title Suffixes"]
    lines += [f"- {row['value']} ({row['count']})" for row in analysis.get("common_title_suffixes", [])] or ["- No suffix pattern found."]
    lines += ["", "## Upload Time"]
    lines.append("Weekdays: " + ", ".join(f"{row['value']}={row['count']}" for row in analysis.get("upload_weekdays", [])) or "No weekday data")
    lines.append("Hours: " + ", ".join(f"{row['value']}:00={row['count']}" for row in analysis.get("upload_hours", [])) or "No hour data")
    lines += ["", "## Thumbnail Direction"]
    lines += [f"- {note}" for note in analysis.get("thumbnail_notes", [])]
    lines += ["", "## Samples"]
    for item in records[:20]:
        lines += [
            "",
            f"### {item.get('title') or 'Untitled'}",
            f"- URL: {item.get('url') or ''}",
            f"- Views: {item.get('view_count') or 0}",
            f"- Upload: {item.get('upload_date') or ''} {item.get('uploaded_at') or ''}",
            f"- Hook: {item.get('transcript_hook') or short_text(item.get('description') or '', 180)}",
        ]
        if item.get("thumbnail_url"):
            lines.append(f"![thumbnail]({item.get('thumbnail_url')})")
    lines.append("")
    return "\n".join(lines)


def recent_files(directory: Path, extensions: set[str]) -> list[Path]:
    return sorted(list_files(directory, extensions), key=lambda path: path.stat().st_mtime, reverse=True)


def uploaded_in_any_state(audio_path: Path, states: list[StateStore]) -> bool:
    return any(state.has_upload(audio_path, "normal") or state.has_upload(audio_path, "short") for state in states)


def find_track_by_audio(tracks, audio_name: str):
    audio_name = Path(audio_name).name
    for track in tracks:
        if track.audio_path.name == audio_name:
            return track
    return None


def track_upload_types(track_action: str) -> set[str]:
    if track_action == "upload-normal":
        return {"normal"}
    if track_action == "upload-short":
        return {"short"}
    return {"normal", "short"}


def delete_track_files(track, paths: dict[str, Path], state: StateStore, config) -> int:
    targets: set[Path] = {
        track.audio_path,
        paths["output_dir"] / f"{track.slug}.mp4",
        paths["output_dir"] / f"{track.slug}-short.mp4",
    }
    for suffix in {".txt", ".title.txt", ".auto.srt", ".srt"}:
        targets.add(track.audio_path.with_suffix(suffix))

    image_files = list_files(paths["image_dir"], IMAGE_EXTENSIONS)
    targets.update(find_matching_images(track.audio_path, image_files))
    thumbnail_files = list_files(paths["thumbnail_dir"], IMAGE_EXTENSIONS)
    targets.update(find_matching_images(track.audio_path, thumbnail_files))

    metadata = build_metadata(track, config.data, paths["thumbnail_dir"])
    if metadata.thumbnail_path:
        targets.add(metadata.thumbnail_path)

    deleted = 0
    allowed_dirs = {
        paths["audio_dir"].resolve(),
        paths["image_dir"].resolve(),
        paths["thumbnail_dir"].resolve(),
        paths["output_dir"].resolve(),
    }
    for target in targets:
        resolved = target.resolve()
        if not any(resolved.parent == allowed_dir for allowed_dir in allowed_dirs):
            continue
        if resolved.exists() and resolved.is_file():
            resolved.unlink()
            deleted += 1

    remove_audio_state(state, track.audio_path)
    remove_metadata_override(config, track.audio_path.name)
    return deleted


def remove_audio_state(state: StateStore, audio_path: Path) -> None:
    resolved = str(audio_path.resolve())
    before_processed = len(state.data["processed_audio"])
    before_uploads = len(state.data["uploads"])
    state.data["processed_audio"] = [item for item in state.data["processed_audio"] if item != resolved]
    state.data["uploads"] = [item for item in state.data["uploads"] if item.get("audio") != resolved]
    if before_processed != len(state.data["processed_audio"]) or before_uploads != len(state.data["uploads"]):
        state.save()


def remove_metadata_override(config, audio_name: str) -> None:
    active_account = get_active_account_id(config)
    overrides = config.data.get("metadata_overrides", {}).get(active_account, {})
    if audio_name not in overrides:
        return
    del overrides[audio_name]
    (ROOT / "config.json").write_text(
        json.dumps(config.data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def youtube_urls_for(uploads: list[dict[str, Any]]) -> dict[str, str]:
    urls = {}
    for item in uploads:
        video_id = item.get("youtube_id")
        upload_type = item.get("type")
        if video_id and upload_type:
            urls[upload_type] = f"https://www.youtube.com/watch?v={video_id}"
    return urls


def is_fullauto_english_account(account_id: str) -> bool:
    return str(account_id or "").strip() == "account4"


def is_fullauto_supported_account(account_id: str) -> bool:
    return str(account_id or "").strip() in {"account1", "account2", "account3", "account4"}


def is_fullauto_vietnamese_account(account_id: str) -> bool:
    return is_fullauto_supported_account(account_id) and not is_fullauto_english_account(account_id)


def fullauto_channel_slug(account_id: str) -> str:
    return {
        "account1": "nhan-tam-phat-phap",
        "account2": "anh-dao-tu-bi",
        "account3": "lang-nghe-phat-phap-dieu-ky",
        "account4": "an-nhien-phat-phap",
    }.get(str(account_id or "").strip(), "nhan-tam-phat-phap")


def thumbnail_reference_channel_slug(account_id: str) -> str:
    return {
        "account1": "nhan-tam-phat-phap",
        "account2": "anh-dao-tu-bi",
        "account3": "lang-nghe-phat-phap-dieu-ky",
        "account4": "an-nhien-phat-phap",
    }.get(str(account_id or "").strip(), "nhan-tam-phat-phap")


def thumbnail_reference_dir(config, ratio_dir: str) -> Path:
    fullauto_config = dict(config.get("fullauto", default={}) or {})
    account_id = str(fullauto_config.get("upload_account") or get_active_account_id(config) or "account1")
    slug = thumbnail_reference_channel_slug(account_id)
    return ROOT / "data" / "input" / "buddhist" / "thumbnail-references" / slug / ratio_dir


def fullauto_short_image_pool_dir(fullauto_config: dict[str, Any], upload_account: str, shared_root: Path) -> Path:
    image_pool_dirs = fullauto_config.get("image_pool_dirs")
    if isinstance(image_pool_dirs, dict):
        account_dir = str(image_pool_dirs.get(upload_account) or "").strip()
        if account_dir:
            return ROOT / account_dir
    default_dir = str(fullauto_config.get("image_pool_dir") or "").strip()
    if default_dir:
        return ROOT / default_dir
    return shared_root / "story-shorts" / "images"


def fullauto_voice_cycle_for_account(fullauto_config: dict[str, Any], account_id: str) -> list[str]:
    voice_cycles = fullauto_config.get("voice_cycles")
    if isinstance(voice_cycles, dict):
        account_cycle = voice_cycles.get(account_id)
        if account_cycle:
            return normalize_voice_cycle_config(account_cycle, ["vi-VN-HoaiMyNeural"])
    return normalize_voice_cycle_config(
        fullauto_config.get("voice_cycle"),
        ["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"],
    )


def fullauto_account_voice_cycle(
    fullauto_config: dict[str, Any],
    map_key: str,
    account_id: str,
    fallback_value: Any,
    fallback: list[str],
) -> list[str]:
    voice_cycles = fullauto_config.get(map_key)
    if isinstance(voice_cycles, dict):
        account_cycle = voice_cycles.get(account_id)
        if account_cycle:
            return normalize_voice_cycle_config(account_cycle, fallback)
    return normalize_voice_cycle_config(fallback_value, fallback)


def fullauto_folder_paths(config) -> dict[str, Path]:
    fullauto_config = dict(config.get("fullauto", default={}) or {})
    upload_account = str(fullauto_config.get("upload_account") or get_active_account_id(config) or "account1")
    channel_root = ROOT / "data" / "input" / "buddhist" / "channels" / fullauto_channel_slug(upload_account)
    shared_root = ROOT / "data" / "input" / "buddhist" / "shared"
    short_root = shared_root / "story-shorts"
    twenty_min_root = channel_root / "fullauto-20min"
    long_root = channel_root / "fullauto-long"
    long_assets_root = shared_root / "long-assets"
    return {
        "short_prompts": ROOT / str(fullauto_config.get("prompts_dir") or short_root / "prompts"),
        "short_images": fullauto_short_image_pool_dir(fullauto_config, upload_account, shared_root),
        "short_drafts": ROOT / str(fullauto_config.get("draft_dir") or short_root / "drafts"),
        "long_prompts": long_root / "prompts",
        "long_images": long_root / "images",
        "long_drafts": long_root / "drafts",
        "twenty_min_prompts": twenty_min_root / "prompts",
        "twenty_min_prompts_en": twenty_min_root / "prompts-en",
        "twenty_min_images": twenty_min_root / "images",
        "twenty_min_drafts": twenty_min_root / "drafts",
        "effects": long_assets_root / "ambient",
        "wave": long_assets_root / "wave",
        "stickers": long_assets_root / "stickers",
        "sounds": long_assets_root / "Sounds",
    }


def fullauto_config_for_account(config, account_id: str):
    account = str(account_id or get_active_account_id(config) or "").strip()
    data = deepcopy(config.data)
    if account:
        data["active_account"] = account
        data.setdefault("fullauto", {})["upload_account"] = account
    return type(config)(data=data, root=config.root)


def validate_fullauto_action_assets(action: str, account_id: str | None = None) -> str:
    config = fullauto_config_for_account(load_config(ROOT), account_id or "")
    account = get_active_account_id(config)
    if not is_fullauto_supported_account(account):
        return "Selected account is not supported by Full Auto."
    fullauto_config = dict(config.get("fullauto", default={}) or {})
    paths = fullauto_folder_paths(config)

    if action == "start-long":
        prompt_count = len(list_files(paths["long_prompts"], {".txt", ".md"}))
        image_count = len(list_files(paths["long_images"], IMAGE_EXTENSIONS))
        required_images = max(1, min(10, int(fullauto_config.get("long_image_count", 10) or 10)))
        if prompt_count < 1:
            return "Long video needs at least 1 prompt."
        if image_count < required_images:
            return f"Long video needs {required_images} images, found {image_count}."
    elif action == "start-20min":
        prompt_dir = paths["twenty_min_prompts_en" if is_fullauto_english_account(account) else "twenty_min_prompts"]
        prompt_count = len(list_files(prompt_dir, {".txt", ".md"}))
        image_count = len(list_files(paths["twenty_min_images"], IMAGE_EXTENSIONS))
        required_images = max(1, int(fullauto_config.get("twenty_min_image_count", 5) or 5))
        if prompt_count < 1:
            return "20-minute video needs at least 1 prompt."
        if image_count < required_images:
            return f"20-minute video needs {required_images} images, found {image_count}."
    elif action == "start":
        prompt_count = len(list_files(paths["short_prompts"], {".txt", ".md"}))
        image_count = len(list_files(paths["short_images"], IMAGE_EXTENSIONS))
        if prompt_count < 1:
            return "Short video needs at least 1 prompt."
        if image_count < 1:
            return "Short video needs at least 1 image."
    return ""


def resolve_temple_bell_audio(sounds_dir: Path) -> Path | None:
    if not sounds_dir.exists():
        return None
    exact_match = next(
        (path for path in sounds_dir.glob("*.mp3") if path.stem.strip().lower() == "templebell_5s"),
        None,
    )
    if exact_match:
        return exact_match
    exact_match = next(
        (path for path in sounds_dir.glob("*.mp3") if path.stem.strip().lower() == "templebell"),
        None,
    )
    if exact_match:
        return exact_match
    candidates = sorted(sounds_dir.glob("*temple*bell*.mp3"))
    if candidates:
        return candidates[0]
    all_mp3 = sorted(sounds_dir.glob("*.mp3"))
    if len(all_mp3) == 1:
        return all_mp3[0]
    return None


def choose_flower_effect(effects_dir: Path) -> Path | None:
    if effects_dir.exists():
        candidates = [
            path
            for path in effects_dir.iterdir()
            if path.is_file()
            if path.suffix.lower() in {".gif", ".mkv", ".mov", ".mp4", ".webm", ".png", ".jpg", ".jpeg", ".webp"}
            if re.search(r"(flower|flowers|falling|hoa)", path.name, flags=re.IGNORECASE)
        ]
        if candidates:
            return sorted(candidates)[0]
    fallback_frame = ROOT / "data" / "output" / "falling-flowers-frame.png"
    return fallback_frame if fallback_frame.exists() else None


def choose_long_wave_asset(wave_dir: Path) -> Path | None:
    if not wave_dir.exists():
        return None
    preferred = wave_dir / "audio-spectrum-alpha.mov"
    if preferred.exists():
        return preferred
    for pattern in ("*spectrum*", "*wave*", "*.mov", "*.mp4", "*.webm", "*.gif"):
        candidates = sorted(path for path in wave_dir.glob(pattern) if path.is_file())
        if candidates:
            return candidates[0]
    return None


def choose_ordered_images_with_cursor(
    image_pool: list[Path],
    image_dir: Path,
    account_id: str,
    state_filename: str,
    count: int,
) -> list[Path]:
    image_pool = [path for path in image_pool if path.exists()]
    if not image_pool:
        return []
    ordered = sorted(image_pool, key=lambda path: path.name.lower())
    state_path = ROOT / "data" / "state" / state_filename
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}

    key = f"{account_id}|{image_dir.resolve()}"
    start = int(state.get(key, 0) or 0) % len(ordered)
    chosen = [ordered[(start + offset) % len(ordered)] for offset in range(max(1, count))]
    state[key] = (start + count) % len(ordered)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return chosen


def repair_missing_render_images(track, fallback_pool: list[Path], required_count: int | None = None):
    valid_pool = [path for path in fallback_pool if path.exists()]
    if not valid_pool:
        return track
    images = [path for path in getattr(track, "image_paths", ()) if Path(path).exists()]
    target_count = required_count or len(images) or min(len(valid_pool), 5)
    target_count = max(1, int(target_count))
    if len(images) >= target_count:
        return track.__class__(audio_path=track.audio_path, image_paths=tuple(images[:target_count]), title=track.title)
    used = {Path(path).resolve() for path in images}
    for candidate in valid_pool:
        resolved = candidate.resolve()
        if resolved in used:
            continue
        images.append(candidate)
        used.add(resolved)
        if len(images) >= target_count:
            break
    while len(images) < target_count and valid_pool:
        images.append(valid_pool[len(images) % len(valid_pool)])
    return track.__class__(audio_path=track.audio_path, image_paths=tuple(images[:target_count]), title=track.title)


def choose_twenty_min_ordered_images(image_pool: list[Path], image_dir: Path, account_id: str, count: int = 5) -> list[Path]:
    return choose_ordered_images_with_cursor(
        image_pool=image_pool,
        image_dir=image_dir,
        account_id=account_id,
        state_filename="twenty_min_image_cursor.json",
        count=count,
    )


def choose_long_ordered_images(image_pool: list[Path], image_dir: Path, account_id: str, count: int = 10) -> list[Path]:
    return choose_ordered_images_with_cursor(
        image_pool=image_pool,
        image_dir=image_dir,
        account_id=account_id,
        state_filename="long_image_cursor.json",
        count=count,
    )


def configured_recovered_module(config):
    recovered = load_recovered_module()
    folder_paths = fullauto_folder_paths(config)
    fullauto_config = dict(config.get("fullauto", default={}) or {})
    upload_account = str(fullauto_config.get("upload_account") or get_active_account_id(config) or "account1")
    fullauto_config["voice_cycle"] = fullauto_voice_cycle_for_account(fullauto_config, upload_account)
    fullauto_config["long_voice_cycle"] = normalize_voice_cycle_config(
        fullauto_config.get("long_voice_cycle"),
        ["en-US-BrianNeural"] if str(fullauto_config.get("long_language") or "").strip().lower().startswith("en") else ["vi-VN-HoaiMyNeural"],
    )
    fullauto_config["twenty_min_voice_cycle"] = normalize_voice_cycle_config(
        fullauto_config.get("twenty_min_voice_cycle"),
        ["en-US-BrianNeural"],
    )
    fullauto_config["twenty_min_vi_voice_cycle"] = fullauto_account_voice_cycle(
        fullauto_config,
        "twenty_min_vi_voice_cycles",
        upload_account,
        fullauto_config.get("twenty_min_vi_voice_cycle"),
        ["vi-VN-NamMinhNeural"],
    )
    config.data.setdefault("fullauto", {}).update(
        {
            "voice_cycle": list(fullauto_config["voice_cycle"]),
            "long_voice_cycle": list(fullauto_config["long_voice_cycle"]),
            "twenty_min_voice_cycle": list(fullauto_config["twenty_min_voice_cycle"]),
            "twenty_min_vi_voice_cycle": list(fullauto_config["twenty_min_vi_voice_cycle"]),
        }
    )
    if not hasattr(recovered, "_codex_original_read_twenty_min_prompt_file"):
        recovered._codex_original_read_twenty_min_prompt_file = recovered._read_twenty_min_prompt_file
    if not hasattr(recovered, "_codex_original_run_twenty_min_job"):
        recovered._codex_original_run_twenty_min_job = recovered.run_fullauto_twenty_min_job
    if not hasattr(recovered, "_codex_original_run_fullauto_story_job"):
        recovered._codex_original_run_fullauto_story_job = recovered.run_fullauto_story_job
    if not hasattr(recovered, "_codex_original_run_fullauto_long_job"):
        recovered._codex_original_run_fullauto_long_job = recovered.run_fullauto_long_job
    if not hasattr(recovered, "_codex_original_call_gemini_story_model"):
        recovered._codex_original_call_gemini_story_model = recovered.call_gemini_story_model
    if not hasattr(recovered, "_codex_original_call_ollama_story_model"):
        recovered._codex_original_call_ollama_story_model = recovered.call_ollama_story_model
    if not hasattr(recovered, "_codex_original_call_fullauto_long_model"):
        recovered._codex_original_call_fullauto_long_model = recovered.call_fullauto_long_model
    if not hasattr(recovered, "_codex_original_build_long_outline_prompt"):
        recovered._codex_original_build_long_outline_prompt = recovered.build_long_outline_prompt
    if not hasattr(recovered, "_codex_original_parse_long_outline"):
        recovered._codex_original_parse_long_outline = recovered.parse_long_outline
    if not hasattr(recovered, "_codex_original_build_long_chapter_prompt"):
        recovered._codex_original_build_long_chapter_prompt = recovered.build_long_chapter_prompt
    if not hasattr(recovered, "_codex_original_render_video"):
        recovered._codex_original_render_video = recovered.render_video
    if not hasattr(recovered, "_codex_original_generate_voice"):
        recovered._codex_original_generate_voice = recovered.generate_voice
    recovered.FULLAUTO_DIR = folder_paths["short_prompts"].parent
    recovered.FULLAUTO_PROMPT_DIR = folder_paths["short_prompts"]
    recovered.FULLAUTO_IMAGE_DIR = folder_paths["short_images"]
    recovered.FULLAUTO_DRAFT_DIR = folder_paths["short_drafts"]
    recovered.FULLAUTO_LONG_DIR = folder_paths["long_prompts"].parent
    recovered.FULLAUTO_LONG_PROMPT_DIR = folder_paths["long_prompts"]
    recovered.FULLAUTO_LONG_IMAGE_DIR = folder_paths["long_images"]
    recovered.FULLAUTO_LONG_DRAFT_DIR = folder_paths["long_drafts"]
    recovered.FULLAUTO_LONG_STICKER_DIR = folder_paths["stickers"]
    recovered.FULLAUTO_TWENTY_MIN_DIR = folder_paths["twenty_min_prompts"].parent
    original_read_twenty_min_prompt_file = recovered._codex_original_read_twenty_min_prompt_file
    original_run_twenty_min_job = recovered._codex_original_run_twenty_min_job
    original_run_fullauto_story_job = recovered._codex_original_run_fullauto_story_job
    original_run_fullauto_long_job = recovered._codex_original_run_fullauto_long_job
    original_call_gemini_story_model = recovered._codex_original_call_gemini_story_model
    original_call_ollama_story_model = recovered._codex_original_call_ollama_story_model
    original_call_fullauto_long_model = recovered._codex_original_call_fullauto_long_model
    original_build_long_outline_prompt = recovered._codex_original_build_long_outline_prompt
    original_parse_long_outline = recovered._codex_original_parse_long_outline
    original_build_long_chapter_prompt = recovered._codex_original_build_long_chapter_prompt
    original_generate_voice = recovered._codex_original_generate_voice

    def patched_long_workspace(account_id: str | None = None) -> dict[str, Path]:
        return {
            "base": folder_paths["long_prompts"].parent,
            "prompts": folder_paths["long_prompts"],
            "images": folder_paths["long_images"],
            "drafts": folder_paths["long_drafts"],
            "stickers": folder_paths["stickers"],
            "effects": folder_paths["effects"],
        }

    def patched_twenty_min_workspace(account_id: str | None = None) -> dict[str, Path]:
        return {
            "base": folder_paths["twenty_min_prompts"].parent,
            "prompts": folder_paths["twenty_min_prompts"],
            "images": folder_paths["twenty_min_images"],
            "drafts": folder_paths["twenty_min_drafts"],
            "stickers": folder_paths["stickers"],
            "effects": folder_paths["effects"],
        }

    def patched_read_twenty_min_prompt_file(workspace: dict[str, Path], name: str) -> str:
        prompt_dir = getattr(recovered, "_current_twenty_min_prompt_dir", None)
        prompt_file = Path(prompt_dir) / f"{name}.txt" if prompt_dir else None
        if prompt_file and prompt_file.exists():
            prompt_text = prompt_file.read_text(encoding="utf-8-sig")
        else:
            prompt_text = original_read_twenty_min_prompt_file(workspace, name)
        variation = getattr(recovered, "_current_twenty_min_vi_variation", None)
        if variation:
            return apply_twenty_min_vi_variation(prompt_text, name, variation)
        variation_en = getattr(recovered, "_current_twenty_min_en_variation", None)
        if variation_en:
            return apply_twenty_min_en_variation(prompt_text, name, variation_en)
        return prompt_text

    def patched_run_twenty_min_job(job: dict[str, Any], config, paths: dict[str, Path], state: StateStore, upload_config):
        active_account = get_active_account_id(upload_config)
        original_schedule_section = deepcopy(dict(config.data.get("schedule", {}) or {}))
        effective_fullauto_section = dict(config.data.get("fullauto", {}) or {})
        target_cluster, cluster_count, cluster_required = preferred_twenty_min_cluster(config, state, active_account)
        if target_cluster:
            remaining = max(0, cluster_required - cluster_count)
            log(
                job,
                "20-Min target cluster: "
                f"{target_cluster} ({cluster_count}/{cluster_required}"
                f"{', complete' if remaining == 0 else f', need {remaining} more'})",
            )
        if is_fullauto_vietnamese_account(active_account):
            recovered._current_twenty_min_prompt_dir = folder_paths["twenty_min_prompts"]
            recovered._current_twenty_min_vi_variation = build_twenty_min_vi_variation(
                fullauto_config,
                preferred_cluster=target_cluster,
            )
            recovered._current_twenty_min_en_variation = None
            log(
                job,
                "20-Min variation: "
                f"{recovered._current_twenty_min_vi_variation['label']} / "
                f"{recovered._current_twenty_min_vi_variation['focus']}",
            )
        else:
            recovered._current_twenty_min_prompt_dir = folder_paths["twenty_min_prompts_en"]
            recovered._current_twenty_min_vi_variation = None
            recovered._current_twenty_min_en_variation = build_twenty_min_en_variation(
                fullauto_config,
                preferred_cluster=target_cluster,
            )
            log(
                job,
                "20-Min EN variation: "
                f"{recovered._current_twenty_min_en_variation['label']} / "
                f"{recovered._current_twenty_min_en_variation['focus']}",
            )
        publish_times = (
            effective_fullauto_section.get("twenty_min_vi_publish_times")
            if is_fullauto_vietnamese_account(active_account)
            else effective_fullauto_section.get("twenty_min_publish_times")
        ) or original_schedule_section.get("publish_times")
        timezone_name = (
            effective_fullauto_section.get("twenty_min_vi_timezone")
            if is_fullauto_vietnamese_account(active_account)
            else effective_fullauto_section.get("twenty_min_timezone")
        ) or original_schedule_section.get("timezone")
        config.data["schedule"] = dict(original_schedule_section)
        config.data["schedule"]["publish_times"] = publish_times
        config.data["schedule"]["timezone"] = timezone_name
        config.data["schedule"]["daily_upload_limit"] = int(effective_fullauto_section.get("twenty_min_daily_upload_limit", 1) or 1)
        if effective_fullauto_section.get("twenty_min_allowed_weekdays") is not None:
            config.data["schedule"]["allowed_weekdays"] = list(effective_fullauto_section.get("twenty_min_allowed_weekdays") or [])
        else:
            config.data["schedule"].pop("allowed_weekdays", None)
        if effective_fullauto_section.get("twenty_min_day_interval") is not None:
            config.data["schedule"]["day_interval"] = int(effective_fullauto_section.get("twenty_min_day_interval") or 0)
            anchor_date = (
                effective_fullauto_section.get("twenty_min_interval_anchor_date")
                or original_schedule_section.get("start_date")
            )
            if anchor_date:
                config.data["schedule"]["interval_anchor_date"] = anchor_date
        else:
            config.data["schedule"].pop("day_interval", None)
            config.data["schedule"].pop("interval_anchor_date", None)
        try:
            return original_run_twenty_min_job(job, config, paths, state, upload_config)
        finally:
            config.data["schedule"] = original_schedule_section
            recovered._current_twenty_min_prompt_dir = None
            recovered._current_twenty_min_vi_variation = None
            recovered._current_twenty_min_en_variation = None

    def patched_build_long_outline_prompt(
        source_prompt: str,
        chapter_count: int,
        target_minutes: int,
        title_templates: list[str] | None = None,
        language: str = "vi",
    ) -> str:
        prompt_text = original_build_long_outline_prompt(
            source_prompt,
            chapter_count,
            target_minutes,
            title_templates=title_templates,
            language=language,
        )
        if str(language or "vi").lower().startswith("en"):
            guard = [
                "",
                "STRICT OUTLINE FORMAT:",
                f"- Return exactly {chapter_count} chapter heading lines.",
                "- Each chapter heading line must start with CHAPTER N: where N is the chapter number.",
                "- Do not write chapter body text in the outline.",
                "- Do not stop after 3 chapters. Continue until the final required chapter number.",
                "- Every chapter title must be unique, specific, and related to the source topic.",
            ]
        else:
            guard = [
                "",
                "DINH DANG DAN Y BAT BUOC:",
                f"- Tra ve dung {chapter_count} dong tieu de chuong.",
                "- Moi dong tieu de chuong bat buoc bat dau bang CHAPTER N: voi N la so chuong.",
                "- Khong viet noi dung chuong trong phan dan y.",
                "- Khong dung lai sau 3 chuong. Phai tiep tuc den dung chuong cuoi cung.",
                "- Moi tieu de chuong phai rieng biet, cu the, va bam sat chu de nguon.",
            ]
        count_override = (
            "\nCONFIGURATION OVERRIDE:\n"
            f"- The required chapter count for this run is exactly {chapter_count}.\n"
            "- If the source material mentions a different chapter count, ignore that number.\n"
            "- This outline must contain only headings for this run; never blend chapter body text into it.\n"
        )
        result = (
            prompt_text.rstrip()
            + "\n"
            + "\n".join(guard)
            + count_override
            + "\n"
            + fullauto_long_outline_diversity_rules(language)
        )
        if getattr(recovered, "_resume_long_checkpoint", None):
            recovered._resume_long_outline_prompt = result
        return result

    def patched_parse_long_outline(text: str, fallback_title: str, chapter_count: int, language: str = "vi"):
        title, description, chapters = original_parse_long_outline(
            text,
            fallback_title,
            chapter_count,
            language=language,
        )
        chapters = meaningful_long_chapters(
            title=title,
            fallback_title=fallback_title,
            parsed_chapters=list(chapters),
            chapter_count=chapter_count,
            language=language,
        )
        return title, description, chapters

    def patched_call_gemini_story_model(prompt_text: str, api_key: str, model: str, attempt: int) -> str:
        active_account = str(getattr(recovered, "_current_fullauto_story_account", "") or "")
        if is_fullauto_vietnamese_account(active_account):
            prompt_text = strengthen_vi_shorts_prompt(prompt_text)
            prompt_text = constrain_shorts_prompt_for_voice(prompt_text, fullauto_config, active_account)
        response_text = original_call_gemini_story_model(prompt_text, api_key, model, attempt)
        if is_fullauto_vietnamese_account(active_account):
            response_text = sanitize_vi_shorts_response(response_text)
        if is_fullauto_vietnamese_account(active_account) and vi_shorts_response_needs_visual_repair(response_text):
            repair_prompt = build_vi_shorts_visual_repair_prompt(prompt_text, response_text)
            repaired = original_call_gemini_story_model(repair_prompt, api_key, model, attempt)
            repaired = sanitize_vi_shorts_response(repaired)
            if not vi_shorts_response_needs_visual_repair(repaired):
                return repaired
        return response_text

    def patched_call_ollama_story_model(prompt_text: str, base_url: str, model: str, attempt: int) -> str:
        active_account = str(getattr(recovered, "_current_fullauto_story_account", "") or "")
        if is_fullauto_vietnamese_account(active_account):
            prompt_text = strengthen_vi_shorts_prompt(prompt_text)
            prompt_text = constrain_shorts_prompt_for_voice(prompt_text, fullauto_config, active_account)
        response_text = original_call_ollama_story_model(prompt_text, base_url, model, attempt)
        if is_fullauto_vietnamese_account(active_account):
            response_text = sanitize_vi_shorts_response(response_text)
        if is_fullauto_vietnamese_account(active_account) and vi_shorts_response_needs_visual_repair(response_text):
            repair_prompt = build_vi_shorts_visual_repair_prompt(prompt_text, response_text)
            repaired = original_call_ollama_story_model(repair_prompt, base_url, model, attempt)
            repaired = sanitize_vi_shorts_response(repaired)
            if not vi_shorts_response_needs_visual_repair(repaired):
                return repaired
        return response_text

    def patched_run_fullauto_story_job(job: dict[str, Any], config, paths: dict[str, Path], state: StateStore, upload_config=None):
        active_account = get_active_account_id(upload_config or config)
        recovered._current_fullauto_story_job = job
        recovered._current_fullauto_story_account = active_account
        if is_fullauto_vietnamese_account(active_account):
            log(
                job,
                "Shorts strategy: uu tien hook ca nhan 0-3 giay de tang Viewed vs Swiped Away.",
            )
        try:
            return original_run_fullauto_story_job(job, config, paths, state, upload_config)
        finally:
            recovered._current_fullauto_story_job = None
            recovered._current_fullauto_story_account = None

    def patched_build_long_chapter_prompt(
        source_prompt: str,
        video_title: str,
        chapters: list[str],
        chapter_index: int,
        words_per_chapter: int,
        continuity: str,
        language: str = "vi",
    ) -> str:
        prompt_text = original_build_long_chapter_prompt(
            source_prompt,
            video_title,
            chapters,
            chapter_index,
            words_per_chapter,
            continuity,
            language=language,
        )
        minimum_words = long_chapter_minimum_words(words_per_chapter)
        if str(language or "vi").lower().startswith("en"):
            extra = (
                "\n\nHARD LENGTH REQUIREMENT:\n"
                f"- The chapter must be at least {minimum_words} words long.\n"
                f"- Ideal range remains about {words_per_chapter}-{words_per_chapter + 150} words.\n"
                "- If you are running short, continue developing the same chapter with more concrete examples, emotional turns, and practical reflection.\n"
                "- Do not stop early, do not summarize, and do not end with a brief closing paragraph."
            )
            if chapter_index == 0:
                extra += (
                    "\n\nSTRONG OPENING HOOK REQUIREMENT:\n"
                    "- Before teaching the main idea, begin with a 20-40 second spoken opening that has the retention strength of a good Short, then softens into the long-form pace.\n"
                    "- Sentence 1 must immediately stop the right viewer: speak to a specific pain, fear, night-time thought, regret, or emotional burden.\n"
                    "- Sentence 2 must make the listener feel seen without sounding dramatic or hopeless.\n"
                    "- Sentence 3 must promise a calm reason to stay, such as relief, clarity, sleep, letting go, or a lighter heart.\n"
                    "- Only after those first 3 hook sentences should you invite the listener to breathe slowly and soften the body.\n"
                    "- Good patterns: 'If your mind will not let you rest tonight...', 'If you keep smiling but feel tired inside...', 'Do not rush away if your heart has been heavy lately...'.\n"
                    "- Avoid weak openings like 'Welcome to...', 'Today we will talk about...', 'In Buddhism...', or generic greetings.\n"
                    "- Do not start immediately with explanation, doctrine, definitions, or abstract analysis.\n"
                    "- Do not use stage directions, timestamps, or labels like Intro. Write it as natural voiceover only.\n"
                    "- After this strong but gentle opening, transition naturally into Chapter 1."
                )
        else:
            extra = (
                "\n\nYEU CAU DO DAI BAT BUOC:\n"
                f"- Chuong nay phai dat toi thieu {minimum_words} tu.\n"
                f"- Muc tieu tot nhat van la khoang {words_per_chapter}-{words_per_chapter + 150} tu.\n"
                "- Neu thay sap ngan, hay tiep tuc dao sau y, them vi du doi thuong, khai mo cam xuc va cach ung dung thuc te.\n"
                "- Khong duoc ket thuc som bang mot doan tong ket ngan."
            )
            if chapter_index == 0:
                extra += (
                    "\n\nYEU CAU HOOK MO DAU MANH BAT BUOC:\n"
                    "- Truoc khi vao bai giang chinh, bat buoc viet mot doan mo dau 20-40 giay co luc giu nguoi nghe nhu Shorts hay, sau do moi ha nhip ve giong long-form cham rai.\n"
                    "- Cau 1 phai giu dung nguoi nghe ngay lap tuc: cham vao mot noi dau, noi lo dem khuya, su met moi, tiec nuoi, cam giac bi don nen, hoac ganh nang trong long.\n"
                    "- Cau 2 phai lam nguoi nghe thay minh duoc thau hieu, nhung khong bi quan, khong kich dong qua da.\n"
                    "- Cau 3 phai cho ho mot ly do de o lai: nhe long hon, bot roi hon, ngu yen hon, hieu minh hon, hoac biet cach buong xuong.\n"
                    "- Chi sau 3 cau hook dau moi moi nguoi nghe tho cham, tha long than tam va di vao bai nghe.\n"
                    "- Co the dung kieu: 'Neu dem nay tam con chua chiu yen...', 'Neu con van cuoi nhung trong long da rat met...', 'Co nhung luc minh can nghe cham lai de thay long bot nang...'.\n"
                    "- Khong dung kieu ep nguoi xem: 'Dung voi luot qua', 'Dung luot qua', 'Dung voi tat video', 'Dung bo qua'.\n"
                    "- Tranh mo dau yeu nhu: 'Chao mung ban den voi...', 'Hom nay chung ta se...', 'Trong Phat phap...', 'Duc Phat tung day...' o cau dau.\n"
                    "- Khong duoc lao ngay vao giai thich, giao ly, dinh nghia, phan tich truu tuong, hay noi 'nhung ganh nang...' ngay cau dau.\n"
                    "- Khong ghi nhan dan san khau, khong ghi 'Intro', khong ghi moc thoi gian. Chi viet loi doc tu nhien.\n"
                    "- Sau do moi chuyen mem vao Chuong 1 bang mot cau noi lien ket nhe."
                )
        is_final_chapter = chapter_index == len(chapters) - 1
        extra += (
            "\n\nCHAPTER BOUNDARY CONTRACT:\n"
            f"- This is chapter {chapter_index + 1} of {len(chapters)}.\n"
            "- Write only this chapter. Start a fresh, self-contained paragraph; never continue a half-finished sentence from another chapter.\n"
            "- Stay inside this chapter title and its assigned role. Do not teach the main lesson of later chapter titles.\n"
            "- End on a complete sentence with a light bridge into the next idea, never a clipped sentence.\n"
            f"- {'This is the final chapter: one short blessing and Nam mo Bon Su Thich Ca Mau Ni Phat are allowed only at the very end.' if is_final_chapter else 'This is not the final chapter: do not summarize the whole video, do not offer a farewell or blessing, and do not say Nam mo Bon Su Thich Ca Mau Ni Phat.'}\n"
            "- If the source prompt contains a different chapter count, this run's chapter count above takes priority.\n"
        )
        extra += fullauto_long_chapter_diversity_rules(chapters, chapter_index, language)
        return prompt_text.rstrip() + extra

    def robust_call_fullauto_long_model(
        provider: str,
        model: str,
        prompt: str,
        api_key: str = "",
        base_url: str = "",
        soft_fail: bool = False,
        soft_context: str = "",
    ) -> str:
        current_job = getattr(recovered, "_current_fullauto_long_job", None)
        resume_checkpoint = getattr(recovered, "_resume_long_checkpoint", None)
        resume_outline_prompt = getattr(recovered, "_resume_long_outline_prompt", None)
        if resume_checkpoint and resume_outline_prompt and prompt == resume_outline_prompt:
            outline_raw = Path(resume_checkpoint) / "outline-raw.txt"
            if outline_raw.exists():
                if current_job:
                    log(current_job, f"Resume: reused existing outline from {Path(resume_checkpoint).name}.")
                return outline_raw.read_text(encoding="utf-8-sig")
        last_error: Exception | None = None
        is_expansion_retry = soft_fail and "mo rong chuong" in soft_context.lower()
        retry_prompts = [
            prompt,
            prompt + "\n\nIMPORTANT: Return the chapter text now. Plain text only. Do not return an empty response.",
            prompt + "\n\nNeu bi dung giua chung, hay tiep tuc va tra ve noi dung chuong ngay bay gio. Khong de trong cau tra loi.",
        ]
        if is_expansion_retry:
            retry_prompts = [
                prompt,
                prompt + "\n\nTra ve phan viet tiep ngay bay gio. Neu khong the viet dai, viet it nhat 250-350 tu noi tiep tu nhien. Khong de trong.",
            ]
        retry_total = len(retry_prompts)
        for attempt, retry_prompt in enumerate(retry_prompts, start=1):
            try:
                result = original_call_fullauto_long_model(
                    provider=provider,
                    model=model,
                    prompt=retry_prompt,
                    api_key=api_key,
                    base_url=base_url,
                )
                if str(result or "").strip():
                    if current_job and attempt > 1:
                        log(current_job, f"Model returned text after retry {attempt}.")
                    return result
                last_error = RuntimeError(f"{provider}/{model} returned empty text")
            except Exception as exc:  # noqa: BLE001 - recover transient empty Ollama responses.
                last_error = exc
                if current_job:
                    log(current_job, f"Model retry {attempt}/{retry_total} after empty/error response: {exc}")
        if soft_fail:
            if current_job:
                context = f" {soft_context}" if soft_context else ""
                log(current_job, f"Soft retry exhausted{context}; giu phan chuong da co va tiep tuc job.")
            return ""
        if last_error:
            raise last_error
        raise RuntimeError(f"{provider}/{model} did not return text")

    def patched_call_fullauto_long_model(provider: str, model: str, prompt: str, api_key: str = "", base_url: str = "") -> str:
        result = robust_call_fullauto_long_model(
            provider=provider,
            model=model,
            prompt=prompt,
            api_key=api_key,
            base_url=base_url,
        )
        target_range = infer_prompt_word_target(prompt)
        if not is_long_chapter_prompt(prompt) or not target_range:
            return result
        minimum_words = long_chapter_minimum_words(target_range[0])
        runtime_minimum_words = max(minimum_words, min(target_range[0], target_range[1]))
        allow_final_closing = prompt_allows_final_long_closing(prompt)
        cleaned = normalize_long_chapter_text(
            result,
            target_range[1],
            allow_final_closing=allow_final_closing,
        )
        current_words = count_text_words(cleaned)
        current_job = getattr(recovered, "_current_fullauto_long_job", None)
        if current_words >= runtime_minimum_words:
            return cleaned
        for expansion_index in range(1, 4):
            if current_job:
                log(
                    current_job,
                    f"Chuong dang ngan ({current_words} tu), tu mo rong lan {expansion_index} de dat it nhat {runtime_minimum_words} tu.",
                )
            continuation_prompt = build_long_chapter_continuation_prompt(
                prompt,
                cleaned,
                minimum_words=runtime_minimum_words,
                target_words=target_range[0],
                expansion_index=expansion_index,
            )
            continuation = robust_call_fullauto_long_model(
                provider=provider,
                model=model,
                prompt=continuation_prompt,
                api_key=api_key,
                base_url=base_url,
                soft_fail=True,
                soft_context=f"khi mo rong chuong lan {expansion_index}",
            )
            continuation_clean = normalize_long_chapter_text(
                continuation,
                target_range[1],
                allow_final_closing=allow_final_closing,
            )
            if not continuation_clean.strip():
                if current_job:
                    log(current_job, f"Bo qua phan mo rong rong lan {expansion_index}; thu cach khac.")
                continue
            cleaned = normalize_long_chapter_text(
                f"{cleaned.rstrip()}\n\n{continuation_clean.lstrip()}",
                target_range[1],
                allow_final_closing=allow_final_closing,
            )
            current_words = count_text_words(cleaned)
            if current_job:
                log(current_job, f"Chuong sau mo rong lan {expansion_index}: {current_words} tu.")
            if current_words >= runtime_minimum_words:
                break
        if current_words < runtime_minimum_words and current_job:
            log(
                current_job,
                f"Chuong van ngan ({current_words}/{runtime_minimum_words} tu); "
                "khong chen van mau lap. De luong chinh thu lai chuong va giu checkpoint neu model van khong dat.",
            )

        cleaned = normalize_long_chapter_text(
            cleaned,
            target_range[1],
            allow_final_closing=allow_final_closing,
        )
        previous_chapters = list(getattr(recovered, "_long_generated_chapters", []) or [])
        if current_words >= runtime_minimum_words and previous_chapters:
            overlap = long_chapter_overlap_ratio(cleaned, previous_chapters)
            opening_reused = long_chapter_opening_reused(cleaned, previous_chapters)
            if overlap > 0.12 or opening_reused:
                if current_job:
                    log(
                        current_job,
                        f"QA trung chuong: overlap={overlap:.1%}, opening_reused={opening_reused}; yeu cau viet lai rieng chuong.",
                    )
                rewrite_prompt = build_long_chapter_dedup_rewrite_prompt(
                    original_prompt=prompt,
                    current_text=cleaned,
                    previous_chapters=previous_chapters,
                    minimum_words=runtime_minimum_words,
                    target_words=target_range[0],
                )
                rewritten = robust_call_fullauto_long_model(
                    provider=provider,
                    model=model,
                    prompt=rewrite_prompt,
                    api_key=api_key,
                    base_url=base_url,
                    soft_fail=True,
                    soft_context="khi viet lai chuong bi trung",
                )
                rewritten_clean = normalize_long_chapter_text(
                    rewritten,
                    target_range[1],
                    allow_final_closing=allow_final_closing,
                )
                rewritten_words = count_text_words(rewritten_clean)
                rewritten_overlap = long_chapter_overlap_ratio(rewritten_clean, previous_chapters)
                if (
                    rewritten_words >= runtime_minimum_words
                    and rewritten_overlap < overlap
                    and not long_chapter_opening_reused(rewritten_clean, previous_chapters)
                ):
                    cleaned = rewritten_clean
                    current_words = rewritten_words
                    if current_job:
                        log(current_job, f"QA viet lai dat: {current_words} tu, overlap={rewritten_overlap:.1%}.")
                elif current_job:
                    log(current_job, "QA viet lai chua dat; khong chap nhan ban viet lai kem hon.")

        if current_words >= runtime_minimum_words:
            recovered._long_generated_chapters = previous_chapters + [cleaned]
        return cleaned

    def patched_run_fullauto_long_job(job: dict[str, Any], config, paths: dict[str, Path], state: StateStore, upload_config):
        active_account = get_active_account_id(upload_config)
        original_fullauto_section = deepcopy(dict(config.data.get("fullauto", {}) or {}))
        effective_long_config = effective_long_fullauto_settings(original_fullauto_section, active_account)
        config.data["fullauto"] = effective_long_config
        recovered._current_fullauto_long_job = job
        recovered._long_generated_chapters = []
        resume_checkpoint_text = str(effective_long_config.get("_resume_checkpoint") or "").strip()
        resume_checkpoint = Path(resume_checkpoint_text) if resume_checkpoint_text else None
        original_datetime = recovered.datetime
        if resume_checkpoint and resume_checkpoint.exists():
            match = re.match(r"(\d{8}-\d{6})", resume_checkpoint.name)
            if not match:
                raise RuntimeError(f"Invalid long resume checkpoint name: {resume_checkpoint.name}")
            fixed_now = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S")

            class ResumeDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    if tz is not None:
                        return fixed_now.replace(tzinfo=timezone.utc).astimezone(tz)
                    return fixed_now

            recovered.datetime = ResumeDateTime
            recovered._resume_long_checkpoint = resume_checkpoint
            recovered._resume_long_outline_prompt = None
            existing_chapters = len(list(resume_checkpoint.glob("chapter-*.txt")))
            recovered._long_generated_chapters = [
                chapter.read_text(encoding="utf-8-sig")
                for chapter in sorted(resume_checkpoint.glob("chapter-*.txt"))
                if chapter.stat().st_size > 0
            ]
            log(
                job,
                f"Resume long checkpoint loaded: {resume_checkpoint.name}; "
                f"reusing {existing_chapters} completed chapter(s).",
            )
        log(
            job,
            "Long config: "
            f"language={effective_long_config.get('long_language', 'vi')}, "
            f"voice={','.join(effective_long_config.get('long_voice_cycle', [])[:2])}",
        )
        if effective_long_config.get("provider") == "ollama" and effective_long_config.get("long_chapter_count") != original_fullauto_section.get("long_chapter_count"):
            log(
                job,
                "Ollama long mode: chia nho thanh "
                f"{effective_long_config.get('long_chapter_count')} chuong de moi lan sinh ngan va on dinh hon.",
            )
        try:
            return original_run_fullauto_long_job(job, config, paths, state, upload_config)
        finally:
            recovered._current_fullauto_long_job = None
            recovered._resume_long_checkpoint = None
            recovered._resume_long_outline_prompt = None
            recovered._long_generated_chapters = []
            recovered.datetime = original_datetime
            config.data["fullauto"] = original_fullauto_section

    recovered.fullauto_long_workspace = patched_long_workspace
    recovered.fullauto_twenty_min_workspace = patched_twenty_min_workspace
    recovered._read_twenty_min_prompt_file = patched_read_twenty_min_prompt_file
    recovered.call_gemini_story_model = patched_call_gemini_story_model
    recovered.call_ollama_story_model = patched_call_ollama_story_model
    recovered.run_fullauto_story_job = patched_run_fullauto_story_job
    recovered.run_fullauto_twenty_min_job = patched_run_twenty_min_job
    recovered.build_long_outline_prompt = patched_build_long_outline_prompt
    recovered.parse_long_outline = patched_parse_long_outline
    recovered.build_long_chapter_prompt = patched_build_long_chapter_prompt
    recovered.call_fullauto_long_model = patched_call_fullauto_long_model
    recovered.run_fullauto_long_job = patched_run_fullauto_long_job

    def patched_generate_voice(text: str, title: str, voice: str, output_dir: Path, rate: str = "+0%"):
        if any(frame.function == "run_fullauto_long_job" for frame in inspect.stack()):
            report = analyze_long_script_duplicates(text)
            current_job = getattr(recovered, "_current_fullauto_long_job", None)
            if current_job:
                log(
                    current_job,
                    "QA kich ban truoc voice: "
                    f"duplicate_ratio={report['duplicate_ratio']:.1%}, "
                    f"max_repeat={report['max_repeat']}, opening_repeat={report['opening_repeat']}.",
                )
            if not report["passed"]:
                raise RuntimeError(
                    "Long script QA failed before voice: "
                    f"duplicate_ratio={report['duplicate_ratio']:.1%}, "
                    f"max_repeat={report['max_repeat']}, opening_repeat={report['opening_repeat']}. "
                    "Draft was kept; voice and render were not started."
                )
        return original_generate_voice(text, title, voice, output_dir, rate)

    recovered.generate_voice = patched_generate_voice

    original_render_video = recovered._codex_original_render_video

    def patched_render_video(track, output_dir: Path, render_config: dict[str, Any], suffix: str = "", max_duration_seconds: int | None = None):
        # Rendering can be reached from either the long or 20-minute Buddhist job.
        # Use that job's selected account when picking its channel-specific assets.
        active_account = get_active_account_id(config)
        local_render_config = dict(render_config or {})
        fullauto_prefix = ""
        if any(frame.function == "run_fullauto_long_job" for frame in inspect.stack()):
            fullauto_prefix = "long_"
        elif any(frame.function == "run_fullauto_twenty_min_job" for frame in inspect.stack()):
            fullauto_prefix = "twenty_min_"
        if fullauto_prefix:
            for source_key, target_key in [
                ("resolution", "resolution"),
                ("fps", "fps"),
                ("encode_preset", "encode_preset"),
                ("zoom_effect", "zoom_effect"),
                ("subtitle_font_name", "subtitle_font_name"),
                ("subtitle_font_size", "subtitle_font_size"),
                ("subtitle_margin_v", "subtitle_margin_v"),
                ("subtitle_margin_h", "subtitle_margin_h"),
                ("subtitle_words_per_chunk", "subtitle_words_per_chunk"),
                ("subtitle_max_chars_per_chunk", "subtitle_max_chars_per_chunk"),
                ("subtitle_alignment", "subtitle_alignment"),
                ("subtitle_outline", "subtitle_outline"),
                ("subtitle_shadow", "subtitle_shadow"),
                ("subtitle_border_style", "subtitle_border_style"),
                ("subtitle_bold", "subtitle_bold"),
            ]:
                prefixed_key = f"{fullauto_prefix}{source_key}"
                if prefixed_key in fullauto_config:
                    local_render_config[target_key] = fullauto_config[prefixed_key]
            if bool(fullauto_config.get(f"{fullauto_prefix}low_bed_enabled", fullauto_config.get("buddhist_low_bed_enabled", False))):
                local_render_config["low_bed_enabled"] = True
                local_render_config["low_bed_path"] = str(
                    fullauto_config.get(f"{fullauto_prefix}low_bed_path")
                    or fullauto_config.get("buddhist_low_bed_path")
                    or ""
                )
                local_render_config["low_bed_volume"] = float(
                    fullauto_config.get(f"{fullauto_prefix}low_bed_volume")
                    or fullauto_config.get("buddhist_low_bed_volume")
                    or 0.03
                )
                local_render_config["low_bed_duck_ratio"] = float(
                    fullauto_config.get(f"{fullauto_prefix}low_bed_duck_ratio")
                    or fullauto_config.get("buddhist_low_bed_duck_ratio")
                    or 18.0
                )
                local_render_config["low_bed_duck_threshold"] = float(
                    fullauto_config.get(f"{fullauto_prefix}low_bed_duck_threshold")
                    or fullauto_config.get("buddhist_low_bed_duck_threshold")
                    or 0.03
                )
                local_render_config["low_bed_tone_filter"] = bool(
                    fullauto_config.get(
                        f"{fullauto_prefix}low_bed_tone_filter",
                        fullauto_config.get("buddhist_low_bed_tone_filter", False),
                    )
                )
        if any(
            frame.function in {"run_fullauto_long_job", "run_fullauto_twenty_min_job"}
            for frame in inspect.stack()
        ):
            bell_audio = resolve_temple_bell_audio(folder_paths["sounds"])
            if bell_audio:
                local_render_config["intro_audio_path"] = str(bell_audio)
                local_render_config["intro_audio_duration_seconds"] = 5.0
                local_render_config["intro_audio_trim_seconds"] = 5.0
        if any(frame.function == "run_fullauto_twenty_min_job" for frame in inspect.stack()):
            image_pool = list_files(folder_paths["twenty_min_images"], IMAGE_EXTENSIONS)
            if image_pool and (len(track.image_paths) != 5 or any(not Path(path).exists() for path in track.image_paths)):
                chosen = choose_twenty_min_ordered_images(
                    image_pool,
                    folder_paths["twenty_min_images"],
                    active_account,
                    count=5,
                )
                track = track.__class__(
                    audio_path=track.audio_path,
                    image_paths=tuple(chosen),
                    title=track.title,
                )
            track = repair_missing_render_images(track, image_pool, required_count=5)
            local_render_config["image_segment_seconds"] = float(fullauto_config.get("twenty_min_image_segment_seconds", 12) or 12)
            local_render_config["image_transition_seconds"] = float(fullauto_config.get("twenty_min_image_transition_seconds", 1.2) or 1.2)
            local_render_config["contextual_image_timing"] = False
            effect_pool = list_files(folder_paths["effects"], {".gif", ".mkv", ".mov", ".mp4", ".webm"})
            if effect_pool:
                chosen_effect = random.choice(effect_pool)
                local_render_config["ambient_overlay"] = {
                    "enabled": True,
                    "path": str(chosen_effect),
                    "opacity": float(fullauto_config.get("twenty_min_mist_opacity", 0.6) or 0.6),
                    "blend_mode": "alpha",
                }
            wave_asset = choose_long_wave_asset(folder_paths["wave"])
            if wave_asset:
                local_render_config["subscribe_overlay"] = {
                    "enabled": True,
                    "path": str(wave_asset),
                    "position": "bottom-left",
                    "width_percent": float(fullauto_config.get("twenty_min_wave_asset_width_percent", 52) or 52),
                    "margin_percent": float(fullauto_config.get("twenty_min_wave_asset_margin_percent", 3) or 3),
                }
        if any(frame.function == "run_fullauto_long_job" for frame in inspect.stack()):
            image_pool = list_files(folder_paths["long_images"], IMAGE_EXTENSIONS)
            required_images = int(fullauto_config.get("long_image_count", 10) or 10)
            required_images = max(1, min(10, required_images))
            if image_pool:
                chosen = choose_long_ordered_images(
                    image_pool,
                    folder_paths["long_images"],
                    active_account,
                    count=required_images,
                )
                track = track.__class__(
                    audio_path=track.audio_path,
                    image_paths=tuple(chosen),
                    title=track.title,
                )
            track = repair_missing_render_images(track, image_pool, required_count=required_images)
            local_render_config["image_segment_seconds"] = float(fullauto_config.get("long_image_segment_seconds", 5) or 5)
            local_render_config["image_transition_seconds"] = float(fullauto_config.get("long_image_transition_seconds", 0.75) or 0.75)
            local_render_config["contextual_image_timing"] = False
            flower_effect = choose_flower_effect(folder_paths["effects"])
            if flower_effect:
                local_render_config["ambient_overlay"] = {
                    "enabled": True,
                    "path": str(flower_effect),
                    "opacity": float(fullauto_config.get("long_flower_effect_opacity", 0.82) or 0.82),
                    "blend_mode": "alpha",
                }
            wave_asset = choose_long_wave_asset(folder_paths["wave"])
            if wave_asset:
                local_render_config["subscribe_overlay"] = {
                    "enabled": True,
                    "path": str(wave_asset),
                    "position": "bottom-left",
                    "width_percent": float(fullauto_config.get("long_wave_asset_width_percent", 52) or 52),
                    "margin_percent": float(fullauto_config.get("long_wave_asset_margin_percent", 3) or 3),
                }
        return original_render_video(
            track,
            output_dir,
            local_render_config,
            suffix=suffix,
            max_duration_seconds=max_duration_seconds,
        )

    recovered.render_video = patched_render_video
    recovered.ensure_fullauto_dirs()
    return recovered


def relative_path(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def fullauto_merge_dir(config) -> Path:
    return config.paths["output_dir"] / "fullauto-merged"


def fullauto_merge_state(state: StateStore) -> dict[str, Any]:
    merge_state = state.data.setdefault("fullauto_merge", {})
    merge_state.setdefault("used_twenty_min_videos", [])
    merge_state.setdefault("stage1_uploads", [])
    merge_state.setdefault("used_stage1_videos", [])
    return merge_state


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def parse_iso_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def normalize_topic_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()


def count_text_words(value: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", str(value or ""), flags=re.UNICODE))


def normalize_voice_cycle_config(value: Any, fallback: list[str]) -> list[str]:
    valid_voice_ids = {str(item.get("id") or "").strip() for item in DEFAULT_VOICES if str(item.get("id") or "").strip()}
    cleaned_fallback = [item for item in fallback if item]
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        candidates = []
    normalized = []
    for item in candidates:
        voice_id = str(item or "").strip()
        if not voice_id:
            continue
        if voice_id in valid_voice_ids or voice_id.startswith(("vieneu:", "kokoro:")):
            normalized.append(voice_id)
    return normalized or cleaned_fallback


def vietnamese_long_title_templates() -> list[str]:
    return [
        "Đêm Khó Ngủ Vì Nghĩ Quá Nhiều? Nghe Lời Phật Dạy Để Tâm Nhẹ Và Ngủ Sâu",
        "Đừng Nghĩ Nhiều Nữa | Nghe Pháp Trước Khi Ngủ Để Bớt Lo Và Tâm An",
        "Nửa Đời Còn Lại Hãy Sống Chậm | Lời Phật Dạy Giúp Tâm Nhẹ Đời An",
        "Người Tốt Thường Gặp Bất Hạnh? Nghe Lời Phật Dạy Để Hiểu Nhân Quả",
        "Chỉ Cần Im Lặng Đúng Lúc | Phật Dạy Cách Giữ Phước Và Bình An",
        "Khổ Vì Tình, Khổ Vì Người? Nghe Phật Dạy Để Buông Nhẹ Lòng",
    ]


def effective_long_fullauto_settings(fullauto_config: dict[str, Any], account_id: str) -> dict[str, Any]:
    effective = dict(fullauto_config or {})
    if is_fullauto_english_account(account_id):
        effective["long_language"] = str(effective.get("long_language") or "en").strip().lower()
        effective["long_voice_cycle"] = normalize_voice_cycle_config(
            effective.get("long_voice_cycle"),
            ["en-US-BrianNeural"],
        )
        return effective

    effective["long_language"] = str(effective.get("long_vi_language") or "vi").strip().lower()
    effective["long_voice_cycle"] = fullauto_account_voice_cycle(
        effective,
        "long_vi_voice_cycles",
        account_id,
        effective.get("long_vi_voice_cycle") or ["vi-VN-NamMinhNeural"],
        ["vi-VN-NamMinhNeural"],
    )
    effective["long_title_templates"] = list(
        effective.get("long_vi_title_templates")
        or effective.get("long_title_templates_vi")
        or vietnamese_long_title_templates()
    )
    vi_hashtags = [
        "#loiphatday",
        "#phatphap",
        "#gieobinhan",
        "#doivodinh",
        "#songtute",
        "#chualanhtamhon",
        "#tinhthuc",
        "#songanlac",
        "#nhanqua",
        "#phatphapmoingay",
        "#buongbo",
        "#annhien",
        "#phuocduc",
        "#loiphatdaymoidem",
        "#demkhongu",
    ]
    vi_hashtag_line = " ".join(vi_hashtags)
    effective["long_description_template"] = str(
        effective.get("long_vi_description_template")
        or f"Bai giang Phat phap duoc trinh bay cham rai, gan gui va de ung dung trong doi song.\n\n{vi_hashtag_line}"
    )
    effective["long_hashtags"] = list(
        effective.get("long_vi_hashtags")
        or vi_hashtags
    )
    provider = str(effective.get("provider") or "").strip().lower()
    target_minutes = int(effective.get("long_target_minutes", 60) or 60)
    configured_chapters = int(effective.get("long_chapter_count", 10) or 10)
    if provider == "ollama" and target_minutes >= 90:
        ollama_chapters = max(configured_chapters, min(24, round(target_minutes * 150 / 1000)))
        effective["long_chapter_count"] = ollama_chapters
    return effective


def meaningful_long_chapters(
    title: str,
    fallback_title: str,
    parsed_chapters: list[str],
    chapter_count: int,
    language: str = "vi",
) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for chapter in parsed_chapters:
        chapter_text = str(chapter or "").strip()
        normalized = normalize_topic_text(chapter_text)
        is_placeholder = (
            re.match(r"^(phan|part)\s+\d+\b", normalized)
            and ("chiem" in normalized or "ung dung" in normalized or "reflection" in normalized)
        )
        if not chapter_text or is_placeholder or normalized in seen:
            continue
        cleaned.append(chapter_text)
        seen.add(normalized)
        if len(cleaned) >= chapter_count:
            return cleaned[:chapter_count]

    topic = normalize_topic_text(f"{title} {fallback_title}")
    if str(language or "vi").lower().startswith("en"):
        bank = [
            "Why the Mind Suffers When It Keeps Holding On",
            "What Letting Go Really Means in Buddhist Practice",
            "Releasing Resentment Without Denying the Pain",
            "Putting Down Expectations That Exhaust the Heart",
            "Seeing Impermanence Clearly in Daily Life",
            "Letting Go of Control and Returning to Peace",
            "Forgiving Yourself for What Cannot Be Changed",
            "Meeting Loss With Awareness Instead of Fear",
            "Softening Attachment in Relationships",
            "Stopping the Habit of Replaying the Past",
            "Finding Freedom in the Present Moment",
            "Practicing Compassion When the Heart Is Tired",
            "Turning Suffering Into Understanding",
            "Living Simply With a Lighter Mind",
            "Trusting the Flow of Cause and Effect",
            "Resting the Mind Before Sleep",
            "Carrying Peace Into Tomorrow",
            "The Quiet Strength of a Heart That Can Release",
        ]
    elif "buong" in topic or "bo" in topic or "chap" in topic:
        bank = [
            "Con Người Khổ Vì Điều Gì Khi Cứ Mãi Chấp Trước",
            "Ý Nghĩa Thật Sự Của Việc Buông Bỏ Trong Phật Pháp",
            "Buông Bỏ Oán Hận Để Tâm Được Thanh Thản",
            "Buông Bỏ Kỳ Vọng Để Lòng Nhẹ Hơn",
            "Nhìn Thấy Vô Thường Để Không Còn Níu Giữ",
            "Buông Bỏ Quá Khứ Mà Không Phủ Nhận Nỗi Đau",
            "Tha Thứ Cho Mình Khi Mọi Chuyện Đã Qua",
            "Dừng Kiểm Soát Để Tâm Trở Về Bình An",
            "Buông Bỏ Trong Các Mối Quan Hệ Nhiều Ràng Buộc",
            "Không Còn So Sánh Để Biết Đủ Với Hiện Tại",
            "Chuyển Hóa Sân Hận Thành Lòng Từ Bi",
            "Tập Thở Và Nhìn Lại Khi Tâm Bị Cuốn Đi",
            "Buông Gánh Nặng Tiền Tài Danh Vọng Và Hơn Thua",
            "Sống Chậm Lại Để Nghe Được Tiếng Nói Nội Tâm",
            "Hiểu Nhân Quả Để Ngừng Trách Đời Trách Người",
            "Bình An Khi Không Còn Đòi Hỏi Mọi Thứ Hoàn Hảo",
            "Giữ Tâm Sáng Giữa Những Thay Đổi Của Cuộc Đời",
            "Sức Mạnh Của Một Trái Tim Biết Buông Xuống",
        ]
    else:
        bank = [
            "Nguồn Gốc Của Khổ Đau Trong Đời Sống Hằng Ngày",
            "Bài Học Đầu Tiên Để Trở Về Với Bình An",
            "Nhìn Lại Tâm Mình Khi Gặp Nghịch Cảnh",
            "Chuyển Hóa Lo Âu Bằng Chánh Niệm",
            "Hiểu Vô Thường Để Sống Nhẹ Lòng Hơn",
            "Nuôi Dưỡng Lòng Từ Bi Với Chính Mình",
            "Buông Bớt Hơn Thua Trong Công Việc Và Gia Đình",
            "Tìm Sự Tĩnh Lặng Giữa Những Biến Động",
            "Học Cách Tha Thứ Để Tâm Không Còn Nặng",
            "Sống Biết Đủ Để Thấy Mình Đang Có Phước",
            "Nhìn Nhân Quả Bằng Một Tâm Không Phán Xét",
            "Tập Dừng Lại Trước Khi Nói Và Hành Động",
            "Chữa Lành Những Vết Thương Cũ Trong Lòng",
            "Giữ Niềm Tin Khi Cuộc Sống Chưa Như Ý",
            "Thực Hành Bình An Trong Từng Việc Nhỏ",
            "Đem Lời Phật Dạy Vào Một Ngày Bình Thường",
            "Đi Qua Mệt Mỏi Với Một Tâm Nhẹ Hơn",
            "Kết Lại Bằng Sự Tỉnh Thức Và Biết Ơn",
        ]

    for chapter in bank:
        normalized = normalize_topic_text(chapter)
        if normalized not in seen:
            cleaned.append(chapter)
            seen.add(normalized)
        if len(cleaned) >= chapter_count:
            break

    while len(cleaned) < chapter_count:
        cleaned.append(f"Chương {len(cleaned) + 1}: Bài học thực tập bình an")
    return cleaned[:chapter_count]


def fullauto_long_outline_diversity_rules(language: str = "vi") -> str:
    if str(language or "vi").lower().startswith("en"):
        return """
LONG-FORM DIVERSITY REQUIREMENT:
- Treat the long video like a guided Buddhist journey, not 18 versions of the same essay.
- Every chapter must have a distinct role: opening pain, daily-life story, Buddhist principle, cause-and-effect reflection, speech/body/mind practice, family/relationship application, money/work/stress application, night-time practice, compassion/forgiveness, common mistake, concrete steps, final settling.
- Adjacent chapters must not repeat the same main concept, example, metaphor, or opening rhythm.
- Avoid stacking many chapters around only "letting go", "impermanence", "peace", or "the mind". Rotate concrete angles.
- Each chapter heading must imply a different listener problem and a different practical takeaway.
"""
    return """
YEU CAU DA DANG CHO VIDEO DAI:
- Hay xem video dai nhu mot hanh trinh Phat phap co nhieu chang, khong phai 18 bien the cua cung mot bai luan.
- Moi chuong phai co vai tro rieng: noi dau mo dau, cau chuyen doi thuong, giao ly cot loi, nhan qua, than-mieng-y, gia dinh/tinh cam, tien bac/cong viec, thuc hanh truoc khi ngu, tu bi/tha thu, sai lam thuong gap, cac buoc thuc hanh, ket lai nhe long.
- Hai chuong lien tiep khong duoc lap cung y chinh, cung vi du, cung an du, hoac cung cach mo dau.
- Khong de qua nhieu chuong chi xoay quanh "buong bo", "vo thuong", "tam", "binh an". Phai luan phien cac goc nhin cu the.
- Moi tieu de chuong phai goi ra mot van de nguoi nghe khac nhau va mot bai thuc hanh khac nhau.
"""


def fullauto_long_chapter_diversity_rules(
    chapters: list[str],
    chapter_index: int,
    language: str = "vi",
) -> str:
    role_bank_vi = [
        "mo dau bang noi dau rat cu the cua nguoi nghe, sau do moi ha nhip",
        "ke mot tinh huong doi thuong co nhan vat/canh ro rang",
        "giai thich mot giao ly Phat phap bang ngon ngu de hieu",
        "soi vao nhan qua va cach gieo lai hat giong thien",
        "dua ve thuc hanh than-mieng-y trong loi noi va hanh dong",
        "ung dung vao gia dinh, tinh cam, nguoi lam ta ton thuong",
        "ung dung vao tien bac, cong viec, danh vong, hon thua",
        "thuc hanh buoi toi/truoc khi ngu de tam diu lai",
        "chuyen hoa san han bang tu bi va tha thu",
        "chi ra mot sai lam pho bien lam tieu phuoc va met tam",
        "dua cac buoc thuc hanh ro rang, nhe, lam duoc ngay",
        "nhin lai qua khu ma khong tu trach va hoc cach tu tha thu",
        "doi dien voi so sanh va long hon thua trong doi song hien dai",
        "thuc hanh biet on va biet du trong viec nho hang ngay",
        "giu phuoc qua cach noi nang, lang nghe va im lang dung luc",
        "dua mot cau chuyen nho ve lua chon thien khi dang kho khan",
        "gom lai bai hoc thanh mot nghi thuc toi ngan de ap dung ngay",
        "ket lai bang su tinh thuc, biet on, cau chuc binh an",
    ]
    role_bank_en = [
        "open with a specific listener pain, then soften the pace",
        "tell a daily-life scene with visible people and setting",
        "explain one Buddhist principle in plain language",
        "reflect on cause and effect and planting kinder seeds",
        "practice body, speech, and mind through ordinary actions",
        "apply the teaching to family, relationships, and hurt",
        "apply the teaching to money, work, status, and comparison",
        "offer a night-time practice before sleep",
        "transform anger through compassion and forgiveness",
        "name one common mistake that drains peace",
        "give clear gentle steps the listener can practice today",
        "look at the past without self-blame and practice self-forgiveness",
        "meet comparison and status anxiety in modern life",
        "practice gratitude and enoughness through small daily moments",
        "protect merit through speech, listening, and timely silence",
        "tell a small story of choosing kindness during difficulty",
        "gather the teaching into a short evening ritual for today",
        "close with awareness, gratitude, and peaceful blessing",
    ]
    role_bank = role_bank_en if str(language or "vi").lower().startswith("en") else role_bank_vi
    role = role_bank[chapter_index % len(role_bank)] if role_bank else ""
    previous_title = chapters[chapter_index - 1] if 0 <= chapter_index - 1 < len(chapters) else ""
    current_title = chapters[chapter_index] if 0 <= chapter_index < len(chapters) else ""
    if str(language or "vi").lower().startswith("en"):
        return f"""
CHAPTER DIVERSITY RULES:
- This chapter's unique role: {role}.
- Current chapter title: {current_title}
- Previous chapter title: {previous_title or "none"}
- Do not open with the same breathing/body-relaxation paragraph used in nearby chapters. Use that only briefly when it truly fits.
- Include one concrete scene, one real-life example, one Buddhist idea, and one practical takeaway.
- Use a fresh metaphor. Do not reuse river, lake, moonlight, heavy burden, or flowing water if they appeared recently.
- If this chapter starts sounding like a general essay about the mind, stop and make it more specific.
"""
    return f"""
QUY TAC DA DANG RIENG CHO CHUONG NAY:
- Vai tro rieng cua chuong nay: {role}.
- Tieu de chuong hien tai: {current_title}
- Tieu de chuong truoc: {previous_title or "khong co"}
- Khong mo dau bang lai doan hit tho/tha long co the da dung o cac chuong gan do. Neu can thi chi dung rat ngan.
- Bat buoc co mot canh doi thuong cu the, mot vi du that gan gui, mot y Phat phap, va mot cach thuc hanh ro rang.
- Dung an du moi. Tranh lap lai song, ho, anh trang, ganh nang, dong chay neu cac chuong gan do da dung.
- Neu chuong nay bat dau giong mot bai luan chung chung ve tam, hay ke cu the hon va dua vao doi song hon.
"""


def strengthen_vi_shorts_prompt(prompt_text: str) -> str:
    recent_titles = recent_vi_short_titles()
    extra = [
        "CHIẾN LƯỢC SHORTS ƯU TIÊN PHÂN PHỐI:",
        "- Mục tiêu số 1 là tăng Viewed vs Swiped Away, không phải cố nhồi giáo lý ngay từ đầu.",
        "- TITLE phải dùng tiếng Việt có dấu đầy đủ, có điểm vào rõ ràng, và không được lặp lại cùng một cấu trúc qua nhiều video.",
        "- TITLE KHÔNG được mở đầu bằng: 'Lời Phật Dạy', 'Đức Phật dạy', 'Hôm nay chúng ta', 'Có 5 điều', 'Bài học hôm nay'.",
        "- KHÔNG dùng title/hook mở đầu bằng kiểu ép người xem: 'Đừng vội lướt qua', 'Đừng lướt qua', 'Đừng vội tắt video', 'Đừng bỏ qua'. Nhóm này đã bị lặp và làm video giống spam.",
        "- Không dùng quá nhiều title mở đầu bằng 'Nếu bạn đang...' hoặc 'Nếu hôm nay bạn...'. Mỗi lần tạo hãy chọn một kiểu hook khác với prompt trước.",
        "- Luân phiên các kiểu nội dung: có duyên gặp được video này, may mắn/phước lành sắp mở lối, người hiền hay chịu thiệt, người biết ơn sẽ có phước, 5 điều giữ phước, tiền tài đi cùng tâm an, bớt kể khổ, buông bỏ để nhẹ lòng, một câu chuyện nhân quả đời thường, một lời cầu chúc bình an.",
        "- Có thể dùng title kiểu trực tiếp như: 'Gặp Được Video Này Là Có Duyên', 'Người Hiền Rồi Sẽ Gặp Phước', 'Ai Đang Khổ Hãy Nghe Lời Này', 'May Mắn Đến Từ Tâm Thiện', 'Giữ Phước Bằng 5 Việc Nhỏ'.",
        "- THUMBNAIL_TEXT phải ngắn, 2-5 từ, đánh vào một ý cụ thể: DUYÊN LÀNH, SẮP BÌNH AN, GIỮ PHƯỚC, TÂM THIỆN, BỚT KHỔ, MAY MẮN.",
        "- 2-3 câu đầu của SCRIPT BẮT BUỘC là hook mạnh, nhưng không nhất thiết luôn là hook đồng cảm cá nhân.",
        "- SCRIPT phải là voiceover ngắn dưới 1 phút: ưu tiên 620-760 ký tự, không viết dài lan man.",
        "- SCRIPT phải có cấu trúc rõ: hook 0-3 giây -> một ý Phật pháp đời thường -> một cách thực hành nhỏ -> câu kết.",
        "- Câu cuối SCRIPT phải kết bằng đúng tinh thần niệm Phật: Nam Mô A Di Đà Phật.",
        "- Hook có thể là lời nhắn duyên lành, câu cầu chúc, nghịch lý nhân quả, câu chuyện rất ngắn, hoặc lời gọi đúng một nhóm người xem.",
        "- Mỗi video chỉ chọn một kiểu hook chính. Luân phiên 8 nhóm hook: duyên lành, câu hỏi nhân quả, tình huống đời thường, lời cầu chúc, tiền tài đúng đạo, gia đình/hiếu đạo, khẩu nghiệp/lời nói, tâm an trước khi ngủ.",
        "- Được dùng hook kiểu 'Nghe được video này', 'Người có duyên mới nghe được', 'Tài lộc muốn đến...', nhưng hãy biến thể câu chữ để không lặp y nguyên giữa các video.",
        "- Với hook về tiền tài/tài lộc/may mắn, không hứa chắc chắn giàu lên; hãy gắn với điều kiện gieo nhân lành, tâm sáng, biết ơn, làm việc thiện, sống đúng đạo.",
        "- Không vào giáo lý ngay ở câu đầu. Không mở bằng 'Đức Phật từng dạy...' ở 1-2 câu đầu.",
        "- Sau hook 2-5 giây mới chuyển mềm sang lời Phật dạy, câu chuyện, hoặc bài học.",
        "- Chủ đề ưu tiên phải xen kẽ: duyên lành, phước báo, nhân quả đời thường, may mắn, tiền tài đúng đạo, chịu thiệt, lòng biết ơn, buông bỏ, bình an, gia đình, cha mẹ, con cái, tha thứ.",
        "- Nếu prompt gốc đã là kiểu cảm xúc cá nhân, hãy biến phần title/script thành một góc nhìn khác như duyên lành, phước báo, câu chuyện nhân quả, hoặc lời cầu chúc.",
        "- Hạn chế title và hook quá chung chung dạng 'Lời Phật dạy về...', 'Bài học cuộc sống...'. Chỉ dùng '5 điều...' khi prompt gốc yêu cầu dạng liệt kê.",
        "- DESCRIPTION chỉ dùng 3-5 hashtag theo đúng chủ đề video, không dùng bộ hashtag cố định. Ưu tiên #phatphap #loiphatday + hashtag nội dung như #nhanqua #buongbo #taman #phuoclanh #duyenlanh #longbieton #khaunghiep + #shorts.",
        "- Câu kết vẫn giữ sâu và ấm, nhưng hook đầu video mới là điểm ưu tiên cao nhất.",
        "",
        "VÍ DỤ HOOK/TITLE ĐỂ XEN KẼ:",
        "- Nghe được video này, có thể tài lộc sẽ tới gần hơn nếu con thật sự làm theo những điều lành.",
        "- Người may mắn nghe được lời này, cầu mong tiền tài, sức khỏe và hạnh phúc dần mở lối.",
        "- Người hay chia sẻ Phật pháp bằng tâm lành, may mắn rồi cũng sẽ tìm đến đúng lúc.",
        "- Người có duyên mới nghe được video này vào đúng khoảnh khắc cần nghe.",
        "- Nghe hết video này, con sẽ hiểu vì sao phước báo quan trọng hơn may mắn.",
        "- Tài lộc muốn đến, tâm con trước hết phải sáng.",
        "- Dừng lại vài phút, biết đâu con sẽ nhận ra điều mình tìm kiếm bấy lâu.",
        "- Gặp được video này cũng là một nhân duyên nhỏ...",
        "- Người hay chịu thiệt không phải đang mất phước...",
        "- Có những may mắn chỉ đến khi tâm mình bớt oán trách...",
        "- Nếu đang mong tiền tài mở lối, hãy giữ tâm mình trước...",
        "- Một việc thiện rất nhỏ cũng có thể đổi hướng một ngày buồn...",
        "- Ai biết ơn trong lúc khó khăn, người đó đang giữ lại phước lành...",
        "- Có câu nói tưởng nhẹ, nhưng lại gieo một nhân rất sâu trong lòng người khác...",
        "- Tối nay, nếu lòng còn nhiều chuyện chưa yên, hãy thử đặt xuống một điều trước đã...",
        "- Người biết giữ miệng trong lúc nóng giận thường giữ được rất nhiều phước.",
        "- Khi cha mẹ còn bên cạnh, có những lời thương nên nói sớm hơn một chút.",
        "- Có khi thứ con cần không phải thêm may mắn, mà là bớt một chút tham cầu.",
        "- Một ngày bình an thường bắt đầu từ một ý nghĩ bớt hơn thua.",
        "- Nếu đang thấy mình chịu thiệt, đừng vội nghĩ trời không thấy lòng con.",
        "- Tâm thiện không làm con giàu ngay, nhưng giữ con khỏi đi sai đường.",
        "- Có những phước lành đến rất khẽ, chỉ người biết lắng lòng mới nhận ra.",
        "- Trước khi ngủ, thử tha thứ cho một chuyện nhỏ để lòng nhẹ hơn.",
        "",
        "MAU HOOK CAN XOAY VONG:",
        "- Duyen lanh: gap duoc loi nay dung luc, nghe mot cau de tam diu lai.",
        "- Cau hoi nhan qua: vi sao nguoi noi loi lanh thuong gap duyen lanh?",
        "- Tinh huong doi thuong: mot cau noi voi cha me, mot luc nong gian, mot viec thien nho.",
        "- Loi cau chuc: cau mong ai nghe duoc loi nay bot kho va them binh an.",
        "- Tien tai dung dao: tai loc gan voi tam sang, biet du, khong tham, khong lua doi.",
        "- Gia dinh/hieu dao: neu con cha me, hay giu mot loi am ap hom nay.",
        "- Khau nghiep: bot phan xet, im lang dung luc, noi mot loi cuu nguoi.",
        "- Truoc khi ngu: dat xuong mot moi lo, niem Phat, va ngu voi long nhe hon.",
        "",
        "NGAN HANG GOC NOI DUNG CAN XOAY VONG, KHONG LAP MAI MOT CUM:",
        "- Nhan qua doi thuong: mot loi noi gay ton thuong, mot hanh dong nho tao qua ve sau.",
        "- Nghiep mieng/khau duc: im lang dung luc, bot phan xet, noi loi lanh.",
        "- Buong bo san han: bot hon thua, bot oam trach, khong chap vao viec cu.",
        "- Hieu dao/gia dinh: cha me, con cai, mot bua com, mot cuoc goi chua kip noi.",
        "- Ngu gioi va doi song: khong noi doi, khong lam hai, khong tham qua, song dung muc.",
        "- Phuoc duc that te: giup nguoi, giu loi hua, tra on, lam viec thien am tham.",
        "- Tam an truoc khi ngu: dem kho ngu, lo au, tam tri khong chiu yen.",
        "- Tien tai dung dao: tai loc gan voi tam thien, ky luat, biet du, khong tham.",
        "- Chua lanh noi tam: tha thu cho minh, chap nhan sai lam, bat dau lai nhe hon.",
        "- Duyen lanh gap Phat phap: nghe dung luc, gap mot loi nhac, thay minh can doi.",
        "",
        "ĐỊNH DẠNG OUTPUT BẮT BUỘC, KHÔNG ĐƯỢC THIẾU MỤC NÀO:",
        "TITLE: ...",
        "THUMBNAIL_TEXT: ...",
        "SCRIPT: ...",
        "IMAGE_PROMPTS:",
        "- ...",
        "- ...",
        "- ...",
        "- ...",
        "- ...",
        "THUMBNAIL_PROMPT: ...",
        "DESCRIPTION: ...",
        "",
        "RÀNG BUỘC CỨNG CHO PHẦN ẢNH:",
        "- IMAGE_PROMPTS bắt buộc phải có đúng 5 dòng gạch đầu dòng.",
        "- Mỗi IMAGE_PROMPT phải cụ thể, khác nhau rõ rệt, bám sát đúng nội dung script.",
        "- Không được bỏ trống IMAGE_PROMPTS.",
        "- Không được bỏ trống THUMBNAIL_PROMPT.",
        "- Nếu thiếu phần ảnh, câu trả lời bị xem là sai định dạng.",
    ]
    if recent_titles:
        extra.extend(
            [
                "",
                "TITLE GẦN ĐÂY ĐÃ DÙNG - KHÔNG ĐƯỢC LẶP Y NGUYÊN:",
                *[f"- {title}" for title in recent_titles[:24]],
            ]
        )
    return str(prompt_text or "").rstrip() + "\n\n" + "\n".join(extra)


def recent_vi_short_titles(limit: int = 80) -> list[str]:
    root = Path("data/input/buddhist/shared/story-shorts/drafts")
    if not root.exists():
        return []
    titles: list[str] = []
    for path in sorted(root.glob("*.txt"), key=lambda item: item.stat().st_mtime, reverse=True):
        if len(titles) >= limit:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = re.search(
            r"(?im)^\s*TITLE:\s*(.+?)(?:\s+THUMBNAIL_TEXT\s*:|$)",
            text,
        )
        if match:
            title = match.group(1).strip()
            if title:
                titles.append(title)
    return titles


def _fold_vi_for_guard(value: str) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("đ", "d").replace("Đ", "D")
    return re.sub(r"\s+", " ", text).strip().lower()


VI_BUDDHIST_SHORT_HASHTAG_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("khau nghiep", "loi noi", "im lang", "noi xau"), ("#khaunghiep", "#imlang")),
    (("nhan qua", "nghiep", "bao ung", "gieo nhan"), ("#nhanqua", "#nghiepbao")),
    (("buong bo", "oan trach", "nong gian", "khong chap", "bot kho"), ("#buongbo", "#botkho")),
    (("tam an", "binh an", "an nhien", "an lac", "nhe long"), ("#taman", "#binhan")),
    (("phuoc", "duyen lanh", "may man", "tai loc", "phuoc bao"), ("#phuoclanh", "#duyenlanh")),
    (("biet on", "tri an", "cam on"), ("#longbieton", "#songthien")),
    (("nhan nhin", "chiu thiet", "thiet thoi", "hien lanh"), ("#nhannhin", "#songthien")),
    (("cha me", "me cha", "gia dinh", "con cai", "hieu thao"), ("#giadinh", "#hieuthao")),
    (("kinh phap cu", "phap cu"), ("#kinhphapcu", "#trituephatday")),
    (("ngu", "dem", "lo au", "met moi"), ("#nghephap", "#ngungon")),
)


def vi_buddhist_short_hashtags_for_text(value: str, limit: int = 5) -> list[str]:
    folded = _fold_vi_for_guard(value)
    selected: list[str] = ["#phatphap", "#loiphatday"]
    for keywords, hashtags in VI_BUDDHIST_SHORT_HASHTAG_RULES:
        if any(keyword in folded for keyword in keywords):
            for hashtag in hashtags[:1]:
                if hashtag not in selected:
                    selected.append(hashtag)
        if len(selected) >= limit - 1:
            break
    for fallback in ("#binhan", "#nhanqua", "#songthien"):
        if len(selected) >= limit - 1:
            break
        if fallback not in selected:
            selected.append(fallback)
    if "#shorts" not in selected:
        selected.append("#shorts")
    return selected[:limit]


def with_dynamic_vi_short_hashtags(description: str, context_text: str = "") -> str:
    body = re.sub(r"#[\wÀ-ỹĐđ]+", "", str(description or ""), flags=re.UNICODE)
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip(" \n\r\t-")
    hashtags = vi_buddhist_short_hashtags_for_text(f"{context_text}\n{body}")
    return f"{body}\n\n{' '.join(hashtags)}".strip() if body else " ".join(hashtags)


def _is_banned_vi_short_hook(value: str) -> bool:
    folded = _fold_vi_for_guard(value)
    banned_prefixes = (
        "dung voi luot",
        "dung luot",
        "dung voi tat",
        "dung tat",
        "dung voi bo qua",
        "dung bo qua",
    )
    return any(folded.startswith(prefix) for prefix in banned_prefixes)


def _vi_short_hook_replacement(seed: str) -> str:
    choices = [
        "Gặp Được Video Này Là Một Duyên Lành",
        "Người Đang Mệt Mỏi Hãy Nghe Lời Này",
        "Một Lời Nhắc Nhỏ Giúp Tâm Bình An",
        "Phước Lành Bắt Đầu Từ Tâm Thiện",
        "Ai Đang Khổ Hãy Giữ Lại Lời Này",
        "Tâm An Rồi May Mắn Sẽ Tự Mở Lối",
        "Một Câu Nói Lành Có Thể Giữ Phước",
        "Tối Nay Hãy Đặt Xuống Một Nỗi Lo",
        "Người Biết Nhẫn Rồi Sẽ Gặp Duyên Lành",
        "Tài Lộc Đến Khi Tâm Không Còn Tham",
        "Có Những Phước Lành Đến Rất Khẽ",
        "Ai Còn Cha Mẹ Hãy Nghe Lời Này",
        "Bớt Một Lời Trách Là Thêm Một Phần An",
        "Một Việc Thiện Nhỏ Cũng Có Quả Lành",
        "Người Chịu Thiệt Chưa Chắc Đã Mất Phước",
        "Trước Khi Ngủ Hãy Niệm Một Lời Bình An",
        "Khi Lòng Bớt Hơn Thua Phước Sẽ Ở Lại",
        "Im Lặng Đúng Lúc Cũng Là Tu Tập",
        "Người Biết Ơn Luôn Có Đường Bình An",
        "Duyên Lành Đôi Khi Đến Từ Một Lời Nhắc",
    ]
    recent = {_fold_vi_for_guard(title) for title in recent_vi_short_titles(120)}
    start = abs(hash(str(seed or ""))) % len(choices)
    for offset in range(len(choices)):
        choice = choices[(start + offset) % len(choices)]
        if _fold_vi_for_guard(choice) not in recent:
            return choice
    return choices[start]


def sanitize_vi_shorts_response(response_text: str) -> str:
    text = str(response_text or "")
    recent_titles = {_fold_vi_for_guard(title) for title in recent_vi_short_titles(120)}
    title_value = ""

    def replace_title(match: re.Match[str]) -> str:
        nonlocal title_value
        prefix, title = match.group(1), match.group(2).strip()
        if _is_banned_vi_short_hook(title) or _fold_vi_for_guard(title) in recent_titles:
            title = _vi_short_hook_replacement(title)
            title_value = title
            return prefix + title
        title_value = title
        return match.group(0)

    text = re.sub(
        r"(?im)^(TITLE:\s*)(.+?)\s*$",
        replace_title,
        text,
        count=1,
    )

    script_match = re.search(
        r"(?is)(SCRIPT:\s*)(.*?)(?=\n\s*IMAGE_PROMPTS\s*:|\n\s*THUMBNAIL_PROMPT\s*:|\n\s*DESCRIPTION\s*:|$)",
        text,
    )
    if script_match:
        script = script_match.group(2).strip()
        first_part = re.split(r"(?<=[.!?。！？])\s+", script, maxsplit=1)
        first_sentence = first_part[0].strip()
        if _is_banned_vi_short_hook(first_sentence):
            replacement = (
                "Nếu hôm nay lòng con còn nặng, hãy nghe chậm lại một chút. "
                "Có những lời nhắc nhỏ đủ giúp tâm mình dịu xuống trước khi bước tiếp."
            )
            rest = first_part[1].strip() if len(first_part) > 1 else ""
            new_script = replacement + ((" " + rest) if rest else "")
            text = text[: script_match.start(2)] + new_script + text[script_match.end(2) :]
            script = new_script
        if "nam mo a di da phat" not in _fold_vi_for_guard(script):
            script = script.rstrip()
            script = re.sub(r"\s*Nam\s+M[ôo]\s+A\s+Di\s+Đ[aà]\s+Ph[aậ]t\.?\s*$", "", script, flags=re.I)
            if script and not re.search(r"[.!?。！？]$", script):
                script += "."
            script = f"{script} Nam Mô A Di Đà Phật.".strip()
            text = text[: script_match.start(2)] + script + text[script_match.end(2) :]
    desc_match = re.search(
        r"(?is)(DESCRIPTION:\s*)(.*?)(?=\n\s*(?:TITLE|THUMBNAIL_TEXT|SCRIPT|IMAGE_PROMPTS|THUMBNAIL_PROMPT)\s*:|$)",
        text,
    )
    if desc_match:
        context_text = "\n".join(
            part
            for part in (
                title_value,
                script_match.group(2).strip() if script_match else "",
            )
            if part
        )
        description = with_dynamic_vi_short_hashtags(desc_match.group(2).strip(), context_text)
        text = text[: desc_match.start(2)] + description + text[desc_match.end(2) :]
    return text


def constrain_shorts_prompt_for_voice(prompt_text: str, fullauto_config: dict[str, Any], account_id: str) -> str:
    voices = fullauto_voice_cycle_for_account(fullauto_config, account_id)
    if not any(str(voice).startswith("fpt:") for voice in voices):
        return prompt_text
    extra = [
        "RÀNG BUỘC RIÊNG CHO GIỌNG FPT BAN MAI:",
        "- Giọng FPT đọc chậm và rõ hơn Edge, nên SCRIPT bắt buộc ngắn hơn prompt gốc.",
        "- SCRIPT chỉ dài 620-700 ký tự, khoảng 38-46 giây đọc tự nhiên.",
        "- Không viết 760-850 ký tự dù prompt gốc yêu cầu như vậy.",
        "- Giữ hook mạnh ở 1-2 câu đầu, sau đó vào thẳng ý chính; bỏ các câu giải thích vòng vo.",
        "- Câu cuối vẫn kết tự nhiên, không kéo dài lời chúc.",
    ]
    return str(prompt_text or "").rstrip() + "\n\n" + "\n".join(extra)


def vi_shorts_response_needs_visual_repair(response_text: str) -> bool:
    text = str(response_text or "")
    if "IMAGE_PROMPTS" not in text or "THUMBNAIL_PROMPT" not in text:
        return True
    image_block = re.search(
        r"IMAGE_PROMPTS\s*:\s*(.*?)(?:THUMBNAIL_PROMPT\s*:|DESCRIPTION\s*:|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not image_block:
        return True
    bullet_count = len(
        re.findall(r"^\s*[-*]\s+.+$", image_block.group(1), flags=re.MULTILINE)
    )
    return bullet_count < 5


def build_vi_shorts_visual_repair_prompt(source_prompt: str, broken_response: str) -> str:
    return "\n".join(
        [
            "Hãy sửa câu trả lời sau thành đúng định dạng bắt buộc.",
            "Giữ nguyên tinh thần title, script, description nếu đã ổn, nhưng BẮT BUỘC bổ sung phần ảnh còn thiếu.",
            "Trả về đầy đủ các mục sau và không được thiếu mục nào:",
            "TITLE: ...",
            "THUMBNAIL_TEXT: ...",
            "SCRIPT: ...",
            "IMAGE_PROMPTS:",
            "- prompt 1",
            "- prompt 2",
            "- prompt 3",
            "- prompt 4",
            "- prompt 5",
            "THUMBNAIL_PROMPT: ...",
            "DESCRIPTION: ...",
            "",
            "Yêu cầu cho ảnh:",
            "- 5 image prompts phải khác nhau rõ rệt.",
            "- Tất cả phải bám sát nội dung script.",
            "- Phong cách dọc 9:16, tranh tâm linh Phật pháp, ánh sáng điện ảnh, dễ dùng làm video Shorts.",
            "- Thumbnail prompt phải có bố cục rõ, dễ chèn chữ lớn, nổi bật trên mobile.",
            "",
            "PROMPT GỐC:",
            str(source_prompt or "").strip(),
            "",
            "CÂU TRẢ LỜI CẦN SỬA:",
            str(broken_response or "").strip(),
        ]
    ).strip()


def infer_prompt_word_target(prompt_text: str) -> tuple[int, int] | None:
    text = str(prompt_text or "")
    patterns = [
        r"(\d{3,5})\s*[-–—]\s*(\d{3,5})\s*(?:t[uừ]|words?)",
        r"(?:do dai|độ dài|khoang|khoảng|about|around|at least|min(?:imum)?)[^.\n]{0,40}?(\d{3,5})\s*(?:t[uừ]|words?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        if len(match.groups()) >= 2 and match.group(2):
            low = int(match.group(1))
            high = int(match.group(2))
            return min(low, high), max(low, high)
        low = int(match.group(1))
        return low, low
    return None


def is_long_chapter_prompt(prompt_text: str) -> bool:
    normalized = normalize_topic_text(prompt_text)
    return "chuong" in normalized or "chapter" in normalized


def long_chapter_minimum_words(target_words: int) -> int:
    target = max(1, int(target_words or 0))
    return max(650, min(900, int(target * 0.72)))


def prompt_allows_final_long_closing(prompt_text: str) -> bool:
    """Read the chapter contract added to every long-form chapter prompt."""
    normalized = normalize_topic_text(prompt_text)
    return "this is the final chapter" in normalized or "day la chuong cuoi" in normalized


def normalize_long_chapter_text(
    text: str,
    maximum_words: int,
    allow_final_closing: bool = False,
) -> str:
    """Keep model output inside one clean, narratable chapter boundary."""
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    cleaned = re.sub(r"(?im)^\s*(chapter|chuong)\s*\d+\s*[:.-]\s*", "", cleaned)
    if not allow_final_closing:
        cleaned = re.sub(
            r"(?iu)\s*nam\s+m[ôo]\s+b[ổo]n\s+s[ưu]\s+th[ií]ch\s+ca\s+m[âa]u\s+ni\s+ph[ậa]t\s*[.!?]*",
            "",
            cleaned,
        )

    # A partial final sentence is jarring in voiceover and was the source of
    # chapter-to-chapter fragments in previous long videos.
    sentences = re.split(r"(?<=[.!?…])\s+", cleaned)
    complete = [sentence.strip() for sentence in sentences if sentence.strip()]
    if len(complete) > 1 and not re.search(r"[.!?…][\"')\]]*\s*$", cleaned):
        complete.pop()

    selected: list[str] = []
    words = 0
    cap = max(1, int(maximum_words or 1))
    for sentence in complete:
        sentence_words = count_text_words(sentence)
        if selected and words + sentence_words > cap:
            break
        selected.append(sentence)
        words += sentence_words
    return "\n\n".join(selected).strip()


def build_long_chapter_continuation_prompt(
    original_prompt: str,
    current_text: str,
    minimum_words: int,
    target_words: int,
    expansion_index: int = 1,
) -> str:
    current_words = count_text_words(current_text)
    missing_words = max(120, target_words - current_words)
    effective_minimum_words = minimum_words
    if expansion_index > 1:
        missing_to_minimum = max(0, minimum_words - current_words)
        missing_words = max(100, min(260, missing_to_minimum or missing_words))
        effective_minimum_words = max(current_words + missing_words, int(minimum_words * 0.9))
        minimum_words = effective_minimum_words
    compact_requirements = compact_long_prompt_excerpt(original_prompt, max_chars=1400)
    compact_chapter = compact_long_chapter_excerpt(current_text, max_chars=3000)
    if re.search(r"[àáảãạăắằẳẵặâấầẩẫậđêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵ]", original_prompt, re.IGNORECASE):
        return (
            "Tiếp tục đúng CHƯƠNG đang viết dưới đây. KHÔNG viết lại từ đầu, KHÔNG lặp lại các ý đã có, "
            "KHÔNG thêm tiêu đề, ghi chú, nhãn chương hay lời bình AI.\n"
            f"Mục tiêu: bổ sung khoảng {missing_words} từ để toàn chương đạt ít nhất {minimum_words} từ, "
            "giữ cùng giọng điệu chậm rãi, ấm áp, có ví dụ đời thường và có tiến triển ý rõ ràng.\n"
            "Chỉ trả về phần nội dung nối tiếp mới.\n\n"
            "YÊU CẦU CHƯƠNG ĐÃ RÚT GỌN:\n"
            f"{compact_requirements}\n\n"
            "TRÍCH ĐOẠN ĐẦU VÀ CUỐI CỦA NỘI DUNG ĐÃ CÓ:\n"
            f"{compact_chapter}"
        )
    return (
        "Continue the SAME chapter below. DO NOT restart, DO NOT repeat existing ideas, and DO NOT add headings, "
        "meta commentary, labels, or AI notes.\n"
        f"Add about {missing_words} more words so the full chapter reaches at least {effective_minimum_words} words, "
        "while keeping the same calm, warm, reflective tone and natural progression.\n"
        "Return only the new continuation paragraphs.\n\n"
        "COMPACT CHAPTER REQUIREMENTS:\n"
        f"{compact_requirements}\n\n"
        "OPENING AND END EXCERPTS OF THE EXISTING CHAPTER:\n"
        f"{compact_chapter}"
    )


def compact_long_prompt_excerpt(prompt_text: str, max_chars: int = 1400) -> str:
    """Keep chapter identity and local rules without overflowing Gemma's context."""
    text = re.sub(r"\s+", " ", str(prompt_text or "")).strip()
    if len(text) <= max_chars:
        return text
    head_size = max(400, int(max_chars * 0.62))
    tail_size = max(250, max_chars - head_size - 20)
    return f"{text[:head_size].rstrip()} ... {text[-tail_size:].lstrip()}"


def compact_long_chapter_excerpt(chapter_text: str, max_chars: int = 3000) -> str:
    """Show enough beginning and ending context for a non-repeating continuation."""
    text = str(chapter_text or "").strip()
    if len(text) <= max_chars:
        return text
    head_size = max(700, int(max_chars * 0.34))
    tail_size = max(1200, max_chars - head_size - 40)
    return f"{text[:head_size].rstrip()}\n\n[... phần giữa đã lược để giữ context ...]\n\n{text[-tail_size:].lstrip()}"


def long_script_units(text: str, minimum_words: int = 8) -> list[str]:
    """Return normalized narratable sentences for cross-chapter duplicate QA."""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    units = re.split(r"(?<=[.!?…])\s+|(?:\n\s*){2,}", raw)
    normalized: list[str] = []
    for unit in units:
        value = re.sub(r"\s+", " ", str(unit or "")).strip()
        if count_text_words(value) < minimum_words:
            continue
        normalized.append(normalize_topic_text(value))
    return [unit for unit in normalized if unit]


def long_chapter_overlap_ratio(current_text: str, previous_chapters: list[str]) -> float:
    current_units = long_script_units(current_text)
    if not current_units:
        return 0.0
    previous_units = {
        unit
        for chapter in previous_chapters
        for unit in long_script_units(chapter)
    }
    repeated = sum(1 for unit in current_units if unit in previous_units)
    return repeated / len(current_units)


def long_chapter_opening_reused(current_text: str, previous_chapters: list[str]) -> bool:
    current_units = long_script_units(current_text)
    if not current_units:
        return False
    opening = current_units[0]
    return any(
        (units := long_script_units(chapter)) and units[0] == opening
        for chapter in previous_chapters
    )


def build_long_chapter_dedup_rewrite_prompt(
    original_prompt: str,
    current_text: str,
    previous_chapters: list[str],
    minimum_words: int,
    target_words: int,
) -> str:
    previous_units = {
        unit
        for chapter in previous_chapters
        for unit in long_script_units(chapter)
    }
    repeated_examples = [
        unit for unit in long_script_units(current_text) if unit in previous_units
    ][:8]
    banned = "\n".join(f"- {item}" for item in repeated_examples) or "- Do not reuse any previous chapter opening."
    return (
        "Viet lai TOAN BO chuong Phat phap duoi day bang tieng Viet tu nhien. "
        "Giu dung chu de va bai hoc cua chuong, nhung thay cach mo dau, canh doi thuong, vi du, an du va cach thuc hanh.\n"
        f"Do dai bat buoc: it nhat {minimum_words} tu, muc tieu khoang {target_words} tu.\n"
        "Khong sao chep cac cau da dung o chuong truoc. Khong chen van mau de keo dai. "
        "Khong lap hook tong cua video. Khong ghi tieu de, nhan chuong, ghi chu hay loi binh AI.\n"
        "Moi phan phai tien trien: mot tinh huong cu the -> soi chieu Phat phap -> chuyen bien nhan thuc -> cach thuc hanh rieng.\n\n"
        "CAC CAU BI CAM LAP LAI:\n"
        f"{banned}\n\n"
        "YEU CAU GOC CUA CHUONG:\n"
        f"{original_prompt.strip()}\n\n"
        "BAN CU CAN VIET LAI:\n"
        f"{current_text.strip()}"
    )


def analyze_long_script_duplicates(text: str) -> dict[str, Any]:
    units = long_script_units(text)
    counts = Counter(units)
    duplicate_excess = sum(count - 1 for count in counts.values() if count > 1)
    duplicate_ratio = duplicate_excess / len(units) if units else 0.0
    max_repeat = max(counts.values(), default=0)
    # A long script assembled from chapters should never repeat one substantial
    # sentence across many chapter openings. max_repeat captures that even when
    # the recovered runtime strips chapter headings before voice generation.
    opening_repeat = max_repeat
    return {
        "passed": duplicate_ratio <= 0.08 and max_repeat <= 3,
        "total_units": len(units),
        "duplicate_excess": duplicate_excess,
        "duplicate_ratio": duplicate_ratio,
        "max_repeat": max_repeat,
        "opening_repeat": opening_repeat,
    }


def build_long_chapter_fallback_continuation(
    original_prompt: str,
    current_text: str,
    minimum_words: int,
    target_words: int,
) -> str:
    current_words = count_text_words(current_text)
    missing_words = max(0, int(minimum_words or 0) - current_words)
    if missing_words <= 0:
        return ""
    normalized = normalize_topic_text(original_prompt)
    is_vietnamese = "chuong" in normalized or "phat" in normalized or "tam" in normalized
    paragraphs_vi = [
        (
            "Vì vậy, điều quan trọng không phải là cố ép mình hết khổ ngay trong một khoảnh khắc, "
            "mà là biết nhìn nỗi khổ bằng một tâm chậm hơn. Khi một người còn đang chịu đựng, họ thường "
            "không cần thêm lời trách móc, mà cần một khoảng lặng để thấy rõ mình đang bám vào điều gì, "
            "đang sợ mất điều gì, và đang mong điều gì phải khác đi."
        ),
        (
            "Trong đời sống hằng ngày, chỉ cần một lời nói không như ý, một ánh nhìn lạnh nhạt, một chuyện "
            "tiền bạc chưa yên, tâm cũng có thể bị kéo đi rất xa. Nhưng nếu biết dừng lại, thở chậm và nhìn "
            "sự việc như một bài học nhân duyên, ta sẽ bớt xem khổ đau là kẻ thù. Nó trở thành tiếng chuông "
            "nhắc mình quay về giữ thân, giữ miệng, giữ ý cho hiền lành hơn."
        ),
        (
            "Khi hiểu như vậy, niềm vui cũng không còn là thứ phải chạy theo thật xa. Niềm vui có thể bắt đầu "
            "từ việc không nói thêm một câu làm tổn thương người khác, không nuôi thêm một ý nghĩ oán giận, "
            "và không tự buộc mình phải gánh hết mọi chuyện trong đêm nay. Chỉ một chút buông xuống như thế "
            "cũng đủ mở ra một khoảng bình an nhỏ trong lòng."
        ),
        (
            "Từ khoảng bình an nhỏ ấy, ta học cách đi tiếp nhẹ hơn. Không phải vì mọi vấn đề đã biến mất, mà "
            "vì tâm đã có thêm trí tuệ để không bị cuốn trôi hoàn toàn. Người biết tu không phải người chưa từng "
            "đau, mà là người sau mỗi lần đau biết trở về, biết sửa mình, biết gieo thêm một hạt lành cho ngày mai."
        ),
        (
            "Nếu nhìn kỹ, ta sẽ thấy mỗi lần khổ đau xuất hiện đều có một cánh cửa nhỏ để học lại cách sống. "
            "Có khi cánh cửa ấy là học bớt hơn thua, có khi là học nói chậm lại, có khi là học ngừng đòi đời phải "
            "đối xử đúng như ý mình. Bài học càng giản dị thì càng dễ thực hành, và chính sự thực hành nhỏ mỗi ngày "
            "mới làm phước đức lớn dần lên."
        ),
        (
            "Bởi vậy, trong giây phút này, ta không cần hứa sẽ thay đổi cả cuộc đời ngay lập tức. Chỉ cần nhận ra "
            "mình đang còn thở, còn có thể chọn một ý nghĩ lành hơn, một lời nói mềm hơn, một hành động tử tế hơn. "
            "Từng lựa chọn nhỏ như vậy âm thầm đổi hướng dòng tâm, giúp lòng bớt tối, bớt nặng, và có thêm chỗ cho "
            "ánh sáng của hiểu biết."
        ),
        (
            "Khi bài học ấy được đặt vào đời sống thật, ta sẽ thấy đạo không ở đâu xa. Đạo ở trong cách ta đối diện "
            "với người làm mình buồn, cách ta tiêu một đồng tiền, cách ta trả lời một tin nhắn, cách ta ngồi yên trước "
            "một đêm nhiều suy nghĩ. Nếu giữ được tâm lành trong những việc rất nhỏ, thì giữa biến động lớn, ta cũng "
            "có một nơi để quay về."
        ),
    ]
    paragraphs_en = [
        (
            "So the point is not to force pain to disappear in a single moment, but to meet it with a slower, "
            "steadier awareness. When the mind is suffering, it often needs less judgment and more room to see "
            "what it is holding, what it is afraid to lose, and what it has been trying too hard to control."
        ),
        (
            "In ordinary life, a small word, a cold silence, or an unfinished worry can pull the heart far away "
            "from peace. Yet when we pause and breathe, the same difficulty can become a gentle bell, reminding us "
            "to return to kindness, patience, and a simpler way of seeing the present moment."
        ),
        (
            "From that small return, calm begins again. The problem may not be fully solved, but the heart is no "
            "longer completely carried by it. This is the quiet practice of wisdom: to suffer, to notice, to soften, "
            "and then to plant one more wholesome seed for tomorrow."
        ),
        (
            "This practice does not ask for perfection. It begins with one honest pause, one kinder thought, one "
            "moment of not adding more harm to what already hurts. In that small space, the mind remembers that it "
            "can choose again."
        ),
        (
            "So even when life remains unfinished, the heart can become a little less tight. It can learn to meet "
            "difficulty without turning every difficulty into an identity. It can rest, breathe, and continue with "
            "a steadier kind of courage."
        ),
    ]
    selected = []
    words = current_words
    paragraphs = paragraphs_vi if is_vietnamese else paragraphs_en
    for paragraph in paragraphs:
        selected.append(paragraph)
        words += count_text_words(paragraph)
        if words >= minimum_words:
            break
    return "\n\n".join(selected).strip()


def infer_twenty_min_cluster(record: dict[str, Any]) -> str:
    haystack = normalize_topic_text(
        " ".join(
            [
                str(record.get("title") or ""),
                str(record.get("prompt") or ""),
                Path(str(record.get("markdown") or "")).stem,
            ]
        )
    )
    cluster_map = {
        "overthinking-anxiety": ["lo au", "suy nghi", "overthinking", "mat ngu", "restless", "anxiety", "stress"],
        "healing-emotions": ["chua lanh", "ton thuong", "co don", "that vong", "grief", "healing", "emotional"],
        "letting-go-relationships": ["buong bo", "quan he", "tha thu", "ky vong", "regret", "forgive", "relationship"],
        "karma-daily-life": ["nhan qua", "phuoc", "nghiep", "daily life", "karma", "habit", "doi song"],
        "mindfulness-inner-peace": ["binh an", "chanh niem", "inner peace", "mindfulness", "tinh lang", "an lac"],
    }
    for cluster, keywords in cluster_map.items():
        if any(keyword in haystack for keyword in keywords):
            return cluster
    return "general-healing"


def choose_stage1_cluster(records: list[dict[str, Any]], required: int) -> tuple[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        cluster = str(item.get("cluster") or infer_twenty_min_cluster(item))
        item["cluster"] = cluster
        grouped.setdefault(cluster, []).append(item)
    ranked = sorted(grouped.items(), key=lambda item: (-len(item[1]), parse_iso_timestamp(str(item[1][0].get("created_at") or ""))))
    if not ranked:
        return "", []
    best_cluster, best_records = ranked[0]
    if len(best_records) < required:
        return best_cluster, best_records
    return best_cluster, best_records[:required]


def stage1_cluster_snapshot(config, state: StateStore, upload_account: str) -> dict[str, Any]:
    fullauto_config = dict(config.get("fullauto", default={}) or {})
    required = int(fullauto_config.get("auto_merge_stage1_count", 5) or 5)
    all_records, available = fullauto_stage1_candidates(config, state, upload_account)
    cluster, chosen = choose_stage1_cluster(available, required)
    return {
        "required": required,
        "cluster": cluster,
        "all_records": all_records,
        "available": available,
        "chosen": chosen,
        "count": len(chosen),
        "ready": len(chosen) >= required,
    }


def preferred_twenty_min_cluster(config, state: StateStore, upload_account: str) -> tuple[str, int, int]:
    snapshot = stage1_cluster_snapshot(config, state, upload_account)
    cluster = str(snapshot.get("cluster") or "")
    count = int(snapshot.get("count") or 0)
    required = int(snapshot.get("required") or 5)
    if not cluster:
        return "", count, required
    if count >= required:
        return cluster, count, required
    return cluster, count, required


def twenty_min_cluster_summary(all_records: list[dict[str, Any]], available_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    available_names = {str(item.get("video_name") or "") for item in available_records}
    grouped: dict[str, dict[str, Any]] = {}
    for item in all_records:
        cluster = str(item.get("cluster") or infer_twenty_min_cluster(item))
        bucket = grouped.setdefault(
            cluster,
            {
                "cluster": cluster,
                "total": 0,
                "available": 0,
                "used": 0,
                "latest": [],
            },
        )
        bucket["total"] += 1
        video_name = str(item.get("video_name") or "")
        if video_name in available_names:
            bucket["available"] += 1
        else:
            bucket["used"] += 1
        if len(bucket["latest"]) < 4:
            bucket["latest"].append(
                {
                    "title": str(item.get("title") or Path(video_name).stem),
                    "video_name": video_name,
                    "created_at": str(item.get("created_at") or ""),
                }
            )
    return sorted(
        grouped.values(),
        key=lambda item: (-int(item["available"]), -int(item["total"]), str(item["cluster"])),
    )


def fullauto_twenty_min_records(config, upload_account: str | None = None) -> list[dict[str, Any]]:
    folder_paths = fullauto_folder_paths(config)
    output_dir = config.paths["output_dir"]
    records: list[dict[str, Any]] = []
    for path in sorted(folder_paths["twenty_min_drafts"].glob("*.json")):
        item = read_json(path)
        if not item or str(item.get("mode") or "") != "twenty-min":
            continue
        account_id = str(item.get("upload_account") or "")
        if upload_account and account_id != upload_account:
            continue
        video_name = Path(str(item.get("normal_video") or "")).name
        if not video_name:
            continue
        video_path = output_dir / video_name
        if not video_path.exists():
            continue
        item["video_name"] = video_name
        item["video_path"] = video_path
        item["draft_path"] = path
        item["cluster"] = infer_twenty_min_cluster(item)
        records.append(item)
    records.sort(key=lambda item: parse_iso_timestamp(str(item.get("created_at") or "")), reverse=True)
    return records


def fullauto_stage1_candidates(config, state: StateStore, upload_account: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    merge_state = fullauto_merge_state(state)
    used = {Path(str(item)).name for item in merge_state.get("used_twenty_min_videos", [])}
    all_records = fullauto_twenty_min_records(config, upload_account=upload_account)
    available = [item for item in all_records if item["video_name"] not in used]
    return all_records, available


def fullauto_stage1_outputs(config, state: StateStore) -> list[dict[str, Any]]:
    merge_dir = fullauto_merge_dir(config)
    merge_dir.mkdir(parents=True, exist_ok=True)
    uploads_by_video = {
        Path(str(item.get("video") or "")).name: item
        for item in fullauto_merge_state(state).get("stage1_uploads", [])
        if item.get("video")
    }
    outputs_by_name: dict[str, dict[str, Any]] = {}
    for path in sorted(merge_dir.glob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True):
        upload_info = uploads_by_video.get(path.name, {})
        outputs_by_name[path.name] = {
            "name": path.name,
            "path": path,
            "uploaded": bool(upload_info),
            "youtube_id": upload_info.get("youtube_id", ""),
            "youtube_url": upload_info.get("youtube_url", ""),
            "publish_at": upload_info.get("publish_at", ""),
            "local_exists": True,
        }
    for name, upload_info in uploads_by_video.items():
        if name in outputs_by_name:
            continue
        outputs_by_name[name] = {
            "name": name,
            "path": merge_dir / name,
            "uploaded": True,
            "youtube_id": upload_info.get("youtube_id", ""),
            "youtube_url": upload_info.get("youtube_url", ""),
            "publish_at": upload_info.get("publish_at", ""),
            "local_exists": False,
        }
    return sorted(
        outputs_by_name.values(),
        key=lambda item: (
            parse_iso_timestamp(str(item.get("publish_at") or "")),
            item["name"],
        ),
        reverse=True,
    )


def build_stage1_merge_name(config, records: list[dict[str, Any]]) -> str:
    target_account = str(config.get("fullauto", "upload_account", default="account1") or "account1")
    prefix = f"{fullauto_channel_slug(target_account)}-{target_account}-1h"
    cluster = str(records[0].get("cluster") or "general-healing").replace("_", "-")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{cluster}-{timestamp}.mp4"


def create_fullauto_stage1_merge(job: dict[str, Any], config, state: StateStore) -> Path:
    target_account = str(config.get("fullauto", "upload_account", default="account1") or "account1")
    snapshot = stage1_cluster_snapshot(config, state, target_account)
    required = int(snapshot["required"])
    cluster = str(snapshot["cluster"] or "")
    chosen = list(snapshot["chosen"])
    if len(chosen) < required:
        raise ValueError(f"Need {required} unused 20-minute videos in the same topic cluster for {target_account}, found {len(chosen)} in '{cluster or 'none'}'.")
    merge_dir = fullauto_merge_dir(config)
    merge_dir.mkdir(parents=True, exist_ok=True)
    output_path = merge_dir / build_stage1_merge_name(config, chosen)
    concat_manifest = config.paths["state_dir"] / f"fullauto-stage1-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"

    log(job, f"Stage 1 cluster: {cluster}")
    for item in chosen:
        log(job, f"Stage 1 source: {item['video_name']}")
    concat_videos([item["video_path"] for item in chosen], output_path, concat_manifest)

    merge_state = fullauto_merge_state(state)
    used_entries = merge_state["used_twenty_min_videos"]
    for item in chosen:
        resolved = str(item["video_path"].resolve())
        if resolved not in used_entries:
            used_entries.append(resolved)
    state.save()
    return output_path


def build_stage1_upload_metadata(config, output_path: Path, source_records: list[dict[str, Any]]) -> VideoMetadata:
    fullauto_config = dict(config.get("fullauto", default={}) or {})
    target_account = str(fullauto_config.get("upload_account") or "account1")
    first_title = str(source_records[0].get("title") or output_path.stem).strip()
    cluster = str(source_records[0].get("cluster") or "general-healing")
    vi_focus = {
        "overthinking-anxiety": "Đừng Nghĩ Nhiều Nữa | Nghe Pháp 1 Giờ Để Tâm Nhẹ Và Ngủ Yên",
        "healing-emotions": "Người Hay Bị Tổn Thương Hãy Nghe Điều Này | Phật Pháp 1 Giờ",
        "letting-go-relationships": "Khổ Vì Tình, Khổ Vì Người? Nghe Phật Dạy 1 Giờ Để Buông Nhẹ Lòng",
        "karma-daily-life": "Nhân Quả Nghiệp Báo Không Tự Nhiên Mà Có | Nghe Pháp 1 Giờ",
        "mindfulness-inner-peace": "Đêm Khó Ngủ Nghe Lời Phật Dạy 1 Giờ | Tâm An Nhẹ Lòng",
        "general-healing": "Nửa Đời Còn Lại Hãy Sống Chậm | Nghe Pháp 1 Giờ Để Đời An",
    }
    en_focus = {
        "overthinking-anxiety": "Anxiety, Overthinking, and Rest",
        "healing-emotions": "Emotional Healing and Self-Compassion",
        "letting-go-relationships": "Letting Go and Relationship Healing",
        "karma-daily-life": "Karma, Habits, and Daily Wisdom",
        "mindfulness-inner-peace": "Mindfulness and Inner Peace",
        "general-healing": "Healing and Inner Peace",
    }
    if is_fullauto_vietnamese_account(target_account):
        title = vi_focus.get(cluster, vi_focus["general-healing"])
        hashtags = " ".join(list(fullauto_config.get("twenty_min_vi_hashtags", [])))
        description = (
            f"Nếu bạn đang nghĩ quá nhiều, khó ngủ, hoặc trong lòng còn nặng vì chuyện đời, bản nghe 1 giờ này giúp bạn chậm lại và giữ tâm an hơn. "
            f"Nội dung mở đầu là: {first_title}.\n\n"
            "Bản tổng hợp này gom 5 bài nghe cùng một cụm chủ đề, đi từ nỗi khổ cụ thể trong đời sống đến lời Phật dạy, nhân quả, buông bỏ, khẩu đức và cách giữ phước. "
            "Hãy nghe chậm, nghe trọn vẹn, và giữ lại điều phù hợp với hoàn cảnh của mình.\n\n"
            f"{hashtags}".strip()
        )
    else:
        title = f"{en_focus.get(cluster, en_focus['general-healing'])} | 1 Hour Buddhist Teaching"
        description = (
            f"This 1-hour Buddhist compilation is built around one clear theme: {en_focus.get(cluster, en_focus['general-healing']).lower()}. "
            f"It opens with {first_title} and continues through five connected reflections designed for calm, healing, and deeper rest.\n\n"
            "Instead of mixing unrelated topics, this video stays with one emotional and spiritual thread so the viewer can settle in, reflect, and stay with the practice."
        )
    channel = dict(config.get("channel", default={}) or {})
    return VideoMetadata(
        title=title,
        description=description,
        tags=[str(tag).strip() for tag in channel.get("default_tags", []) if str(tag).strip()],
        category_id=str(channel.get("category_id") or "22"),
        made_for_kids=bool(channel.get("made_for_kids", False)),
        thumbnail_path=None,
    )


def reserve_stage1_publish_at(config, service) -> str | None:
    fullauto_config = dict(config.get("fullauto", default={}) or {})
    publish_times = fullauto_config.get("long_publish_times") or config.get("schedule", "publish_times")
    timezone_name = (
        fullauto_config.get("twenty_min_vi_timezone")
        if is_fullauto_vietnamese_account(str(fullauto_config.get("upload_account") or ""))
        else fullauto_config.get("twenty_min_timezone")
    ) or config.get("schedule", "timezone")
    daily_limit = int(fullauto_config.get("long_daily_upload_limit", 1) or 1)

    schedule_data = deepcopy(config.data)
    schedule_data.setdefault("schedule", {})
    schedule_data["schedule"]["publish_times"] = publish_times
    schedule_data["schedule"]["timezone"] = timezone_name
    schedule_data["schedule"]["daily_upload_limit"] = daily_limit
    if fullauto_config.get("long_allowed_weekdays") is not None:
        schedule_data["schedule"]["allowed_weekdays"] = list(fullauto_config.get("long_allowed_weekdays") or [])
    else:
        schedule_data["schedule"].pop("allowed_weekdays", None)
    if fullauto_config.get("long_day_interval") is not None:
        schedule_data["schedule"]["day_interval"] = int(fullauto_config.get("long_day_interval") or 0)
        anchor_date = fullauto_config.get("long_interval_anchor_date") or schedule_data["schedule"].get("start_date")
        if anchor_date:
            schedule_data["schedule"]["interval_anchor_date"] = anchor_date
    else:
        schedule_data["schedule"].pop("day_interval", None)
        schedule_data["schedule"].pop("interval_anchor_date", None)
    schedule_config = type(config)(data=schedule_data, root=config.root)
    publish_time = reserve_next_publish_time(
        schedule_config,
        blocked_times=set(),
        blocked_dates=set(),
        service=service,
        youtube_date_counts={},
        slot_kind="normal",
    )
    return to_rfc3339_utc(publish_time) if publish_time else None


def create_and_upload_fullauto_stage1_merge(job: dict[str, Any], config, state: StateStore) -> tuple[Path, str]:
    target_account = str(config.get("fullauto", "upload_account", default="account1") or "account1")
    snapshot = stage1_cluster_snapshot(config, state, target_account)
    required = int(snapshot["required"])
    cluster = str(snapshot["cluster"] or "")
    chosen = list(snapshot["chosen"])
    if len(chosen) < required:
        raise ValueError(f"Need {required} unused 20-minute videos in the same topic cluster for {target_account}, found {len(chosen)} in '{cluster or 'none'}'.")
    output_path = create_fullauto_stage1_merge(job, config, state)
    metadata = build_stage1_upload_metadata(config, output_path, chosen)
    paths = config.paths
    service = get_youtube_service(paths["credentials_file"], account_token_path(config))
    publish_at = reserve_stage1_publish_at(config, service)
    privacy = config.get("channel", "privacy_status", default="private")
    video_id = upload_video(
        service=service,
        video_path=output_path,
        metadata=metadata,
        privacy_status=privacy,
        publish_at=publish_at,
    )

    merge_state = fullauto_merge_state(state)
    merge_state["stage1_uploads"].append(
        {
            "video": str(output_path.resolve()),
            "youtube_id": video_id,
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
            "publish_at": publish_at,
        }
    )
    state.save()
    if bool(config.get("fullauto", "delete_stage1_local_after_upload", default=True)):
        if output_path.exists():
            output_path.unlink()
            log(job, f"Deleted local 1-hour file after upload: {output_path.name}")
    return output_path, video_id


def fullauto_long_merge_candidates(config) -> list[dict[str, Any]]:
    output_dir = config.paths["output_dir"]
    candidates = []
    for path in sorted(output_dir.rglob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.name.lower().endswith("-short.mp4") or path.stat().st_size < 300 * 1024 * 1024:
            continue
        rel_name = path.relative_to(output_dir).as_posix()
        candidates.append(
            {
                "name": rel_name,
                "label": path.name,
                "size_gb": round(path.stat().st_size / 1024 / 1024 / 1024, 2),
                "created_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return candidates


def selected_fullauto_long_paths(config, filenames: list[Any]) -> list[Path]:
    candidates = fullauto_long_merge_candidates(config)
    available = {item["name"]: config.paths["output_dir"] / Path(item["name"]) for item in candidates}
    basename_matches: dict[str, list[Path]] = {}
    for item in candidates:
        basename_matches.setdefault(Path(item["name"]).name, []).append(config.paths["output_dir"] / Path(item["name"]))
    selected = []
    for value in filenames:
        raw_name = str(value or "").replace("\\", "/").strip()
        if not raw_name:
            continue
        path = available.get(raw_name)
        if path is None:
            matches = basename_matches.get(Path(raw_name).name, [])
            path = matches[0] if len(matches) == 1 else None
        if path.exists() and path not in selected:
            selected.append(path)
    if len(selected) < 2:
        raise ValueError("Choose at least 2 rendered long videos to merge.")
    return selected


def merge_selected_fullauto_long_videos(
    job: dict[str, Any],
    config,
    state: StateStore,
    filenames: list[Any],
    *,
    delete_sources: bool = True,
) -> Path:
    sources = selected_fullauto_long_paths(config, filenames)
    merge_dir = fullauto_merge_dir(config)
    merge_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = merge_dir / f"{fullauto_channel_slug(get_active_account_id(config))}-long-merge-{timestamp}.mp4"
    manifest = config.paths["state_dir"] / f"fullauto-long-merge-{timestamp}.txt"
    log(job, f"Merging {len(sources)} selected long video(s) with FFmpeg stream-copy when compatible.")
    output = concat_videos(sources, output_path, manifest)
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("Long merge did not produce an output file.")
    if delete_sources:
        for source in sources:
            source.unlink(missing_ok=True)
            log(job, f"Deleted merged source long video: {source.name}")
    merge_state = fullauto_merge_state(state)
    merge_state.setdefault("long_merges", []).append(
        {
            "output": str(output.resolve()),
            "sources": [str(source.resolve()) for source in sources],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    state.save()
    return output


def selected_long_merge_metadata(job: dict[str, Any], config) -> VideoMetadata:
    fullauto_config = effective_long_fullauto_settings(
        dict(config.data.get("fullauto", {}) or {}),
        get_active_account_id(config),
    )
    hashtags = [str(item).strip() for item in fullauto_config.get("long_hashtags", []) if str(item).strip()]
    fallback_title = "L\u1eddi Ph\u1eadt D\u1ea1y: Bu\u00f4ng B\u1ecf Phi\u1ec1n N\u00e3o \u0110\u1ec3 T\u00e2m An | Nghe Ph\u00e1p \u0110\u00eam Khuya"
    fallback_description = str(fullauto_config.get("long_description_template") or "").strip()
    if not fallback_description:
        fallback_description = "Nghe l\u1eddi Ph\u1eadt d\u1ea1y ch\u1eadm r\u00e3i \u0111\u1ec3 t\u00e2m b\u1edbt n\u1eb7ng, bi\u1ebft bu\u00f4ng b\u1ecf v\u00e0 s\u1ed1ng an l\u00e0nh h\u01a1n m\u1ed7i ng\u00e0y."
    fallback_description = f"{fallback_description}\n\n{' '.join(hashtags)}".strip()
    prompt = """
You create YouTube metadata in Vietnamese for a Buddhist long-form listening video.
Write metadata that feels like an ordinary new video, not a compilation.

The listener benefit must be the focus. Choose one natural angle per response:
- nghe l\u1eddi Ph\u1eadt d\u1ea1y \u0111\u1ec3 th\u1ee9c t\u1ec9nh v\u00e0 nh\u00ecn l\u1ea1i cu\u1ed9c s\u1ed1ng;
- nghe tr\u01b0\u1edbc khi ng\u1ee7 \u0111\u1ec3 t\u00e2m b\u1edbt lo v\u00e0 ng\u1ee7 y\u00ean h\u01a1n;
- nghe Ph\u1eadt ph\u00e1p m\u1ed7i ng\u00e0y \u0111\u1ec3 t\u00e2m an, b\u1edbt h\u01a1n thua v\u00e0 bi\u1ebft bu\u00f4ng b\u1ecf.

Rules:
- Do not say "tuy\u1ec3n t\u1eadp", "\u0111\u1ea1i t\u1eadp", "video g\u1ed9p", or any number of source videos.
- Title: 55-95 Vietnamese characters, human, calm, compelling, no all caps, no fake urgency.
- Description: 2 short paragraphs, warm and useful; do not claim to summarize a specific story.
- Do not include hashtags; the system appends them.
- Return JSON only: {"title":"...", "description":"..."}.
""".strip()
    try:
        provider = str(fullauto_config.get("provider") or "ollama")
        model = str(fullauto_config.get("ollama_model") if provider == "ollama" else fullauto_config.get("gemini_model") or "").strip()
        base_url = str(fullauto_config.get("ollama_url") or "").strip()
        api_key = str(fullauto_config.get("gemini_api_key") or "").strip()
        response = configured_recovered_module(config).call_fullauto_long_model(
            provider=provider,
            model=model,
            prompt=prompt,
            api_key=api_key,
            base_url=base_url,
        )
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response or "").strip(), flags=re.IGNORECASE)
        data = json.loads(cleaned)
        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "").strip()
        if len(title) < 25 or not description:
            raise ValueError("Gemma returned incomplete merge metadata.")
        log(job, f"Gemma generated merged-long metadata: {title}")
        return VideoMetadata(
            title=title[:100],
            description=f"{description}\n\n{' '.join(hashtags)}".strip(),
            tags=[tag.lstrip("#") for tag in hashtags],
            category_id="22",
            made_for_kids=False,
            thumbnail_path=None,
        )
    except Exception as exc:  # noqa: BLE001 - merging must still work when metadata generation is unavailable.
        log(job, f"Gemma merge metadata fallback: {exc}")
        return VideoMetadata(
            title=fallback_title,
            description=fallback_description,
            tags=[tag.lstrip("#") for tag in hashtags],
            category_id="22",
            made_for_kids=False,
            thumbnail_path=None,
        )


def merge_upload_selected_fullauto_long_videos(
    job: dict[str, Any],
    config,
    state: StateStore,
    filenames: list[Any],
) -> tuple[Path, str]:
    sources = selected_fullauto_long_paths(config, filenames)
    output_path = merge_selected_fullauto_long_videos(
        job,
        config,
        state,
        filenames,
        delete_sources=False,
    )
    metadata = selected_long_merge_metadata(job, config)
    service = get_youtube_service(config.paths["credentials_file"], account_token_path(config))
    publish_at = reserve_stage1_publish_at(config, service)
    privacy = config.get("channel", "privacy_status", default="private")
    video_id = upload_video(
        service=service,
        video_path=output_path,
        metadata=metadata,
        privacy_status=privacy,
        publish_at=publish_at,
    )
    merge_state = fullauto_merge_state(state)
    merge_state.setdefault("long_merges", []).append(
        {
            "output": str(output_path.resolve()),
            "youtube_id": video_id,
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
            "publish_at": publish_at,
            "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    state.save()
    for source in sources:
        source.unlink(missing_ok=True)
        log(job, f"Deleted merged source long video after successful upload: {source.name}")
    output_path.unlink(missing_ok=True)
    log(job, "Deleted local merged long video after successful YouTube upload.")
    return output_path, video_id


def build_twenty_min_vi_variation(fullauto_config: dict[str, Any], preferred_cluster: str = "") -> dict[str, str]:
    hooks = [str(item).strip() for item in fullauto_config.get("twenty_min_vi_hook_sentences", []) if str(item).strip()]
    styles = [
        {
            "label": "doi-song-thuc-te",
            "cluster": "overthinking-anxiety",
            "theme": "mot tinh huong doi song rat gan goi va de thay minh trong do",
            "opening": "mo bang mot noi dau cu the trong doi song hang ngay, khong mo qua truu tuong",
            "voice": "chan thanh, gan gui, nhu mot nguoi tung trai dang noi chuyen",
            "focus": "lo au, met moi, roi tri, ap luc co gia dinh va cong viec",
        },
        {
            "label": "nhan-qua-noi-tam",
            "cluster": "karma-daily-life",
            "theme": "goc re cua kho tam duoi anh sang nhan qua va tap khi",
            "opening": "vao thang cau hoi vi sao ta cu lap lai mot kieu dau kho",
            "voice": "sau lang, quan chieu, nhung van de hieu va de nghe",
            "focus": "chap truoc, san han, hoi tiec, tu trach va cach hoa giai",
        },
        {
            "label": "chua-lanh-cam-xuc",
            "cluster": "healing-emotions",
            "theme": "hanh trinh chua lanh cam xuc bi don nen lau ngay",
            "opening": "bat dau bang cam giac co don, ton thuong hoac khong duoc thau hieu",
            "voice": "am ap, nang do, lam nguoi nghe cam thay duoc om lay",
            "focus": "vet thuong cam xuc, mat mat, buon, co don, that vong",
        },
        {
            "label": "thuc-hanh-moi-ngay",
            "cluster": "mindfulness-inner-peace",
            "theme": "mot loi day co the ung dung ngay trong ngay hom nay",
            "opening": "di tu mot van de nho nhung lap lai moi ngay roi mo ra bai hoc lon",
            "voice": "ro rang, de ap dung, it giao dieu, nhieu tinh thuc",
            "focus": "tho quen nghi qua nhieu, mat ngu, mat binh an, song vo thuc",
        },
        {
            "label": "quan-he-va-buong-bo",
            "cluster": "letting-go-relationships",
            "theme": "nhung moi rang buoc trong quan he va nghe thuat buong bo",
            "opening": "dat nguoi nghe vao mot moi quan he khien tim nang va tri nho mai",
            "voice": "tinh te, sau sac, khong phan xet",
            "focus": "duyen no, ky vong, buon vi nguoi khac, buon dung luc",
        },
    ]
    if preferred_cluster:
        matching_styles = [item for item in styles if str(item.get("cluster") or "") == preferred_cluster]
        if matching_styles:
            styles = matching_styles
    chosen = random.choice(styles)
    return {
        "hook": random.choice(hooks) if hooks else "",
        **chosen,
    }


def build_twenty_min_en_variation(fullauto_config: dict[str, Any], preferred_cluster: str = "") -> dict[str, str]:
    styles = [
        {
            "label": "anxiety-overthinking",
            "cluster": "overthinking-anxiety",
            "theme": "anxiety, racing thoughts, and mental overload",
            "opening": "begin with a highly relatable moment of spiraling thoughts or late-night restlessness",
            "voice": "calm, steady, practical, and emotionally safe",
            "focus": "anxiety, overthinking, sleep difficulty, nervous system calm",
        },
        {
            "label": "emotional-healing",
            "cluster": "healing-emotions",
            "theme": "healing emotional pain with Buddhist compassion and clarity",
            "opening": "start with loneliness, grief, disappointment, or feeling unseen",
            "voice": "warm, compassionate, reassuring, and intimate",
            "focus": "grief, heartbreak, self-compassion, emotional healing",
        },
        {
            "label": "letting-go",
            "cluster": "letting-go-relationships",
            "theme": "letting go without emotional numbness or avoidance",
            "opening": "open with a relationship, regret, or attachment the listener cannot stop replaying",
            "voice": "gentle, insightful, and quietly strong",
            "focus": "regret, attachment, forgiveness, relationship pain, release",
        },
        {
            "label": "practical-mindfulness",
            "cluster": "mindfulness-inner-peace",
            "theme": "mindfulness as a daily practice for clarity and peace",
            "opening": "begin with a common daily stress pattern and show a simple path back to presence",
            "voice": "clear, practical, grounded, and beginner-friendly",
            "focus": "mindfulness, inner peace, daily practice, emotional steadiness",
        },
        {
            "label": "karma-daily-wisdom",
            "cluster": "karma-daily-life",
            "theme": "karma and daily wisdom applied to emotional life and choices",
            "opening": "start with a repeating life pattern and ask what keeps recreating it",
            "voice": "reflective, wise, and easy to follow",
            "focus": "karma, habit loops, choices, cause and effect, daily suffering",
        },
    ]
    if preferred_cluster:
        matching_styles = [item for item in styles if str(item.get("cluster") or "") == preferred_cluster]
        if matching_styles:
            styles = matching_styles
    return random.choice(styles)


def fullauto_status(config) -> dict[str, Any]:
    fullauto_config = dict(config.get("fullauto", default={}) or {})
    accounts = get_accounts(config)
    upload_account = str(fullauto_config.get("upload_account") or "account1")
    provider = str(fullauto_config.get("provider") or "gemini").strip().lower()
    model = str(
        fullauto_config.get("ollama_model")
        if provider == "ollama"
        else fullauto_config.get("gemini_model") or "gemini-2.5-flash"
    ).strip()
    folder_paths = fullauto_folder_paths(config)
    ollama_models = discover_ollama_models(fullauto_config, model)
    upload_accounts = {
        account_id: account
        for account_id, account in accounts.items()
        if is_fullauto_supported_account(account_id)
    }
    merge_state = StateStore(account_state_dir(config))
    stage1_snapshot = stage1_cluster_snapshot(config, merge_state, upload_account)
    stage1_outputs = fullauto_stage1_outputs(config, merge_state)
    unuploaded_stage1_outputs = [item for item in stage1_outputs if not item["uploaded"]]

    return {
        "enabled": bool(fullauto_config.get("enabled", False)),
        "provider": provider,
        "model": model,
        "ollama_models": ollama_models,
        "upload_accounts": upload_accounts,
        "prompt_count": len(list_files(folder_paths["short_prompts"], {".txt", ".md"})),
        "image_count": len(list_files(folder_paths["short_images"], IMAGE_EXTENSIONS)),
        "paths": {
            "prompts": relative_path(folder_paths["short_prompts"]),
            "images": relative_path(folder_paths["short_images"]),
            "drafts": relative_path(folder_paths["short_drafts"]),
            "long_prompts": relative_path(folder_paths["long_prompts"]),
            "long_images": relative_path(folder_paths["long_images"]),
            "long_drafts": relative_path(folder_paths["long_drafts"]),
            "long_stickers": relative_path(folder_paths["stickers"]),
            "long_effects": relative_path(folder_paths["effects"]),
            "long_wave": relative_path(folder_paths["wave"]),
            "twenty_min_prompts": relative_path(folder_paths["twenty_min_prompts"]),
            "twenty_min_images": relative_path(folder_paths["twenty_min_images"]),
            "twenty_min_drafts": relative_path(folder_paths["twenty_min_drafts"]),
        },
        "long_prompt_count": len(list_files(folder_paths["long_prompts"], {".txt", ".md"})),
        "long_image_count": len(list_files(folder_paths["long_images"], IMAGE_EXTENSIONS)),
        "long_sticker_count": len(list_files(folder_paths["stickers"], IMAGE_EXTENSIONS | {".gif", ".mkv", ".mov", ".mp4", ".webm"})),
        "long_effect_count": len(list_files(folder_paths["effects"], {".gif", ".mkv", ".mov", ".mp4", ".webm"})),
        "long_wave_count": len(list_files(folder_paths["wave"], {".gif", ".mkv", ".mov", ".mp4", ".webm"})),
        "long_required_image_count": int(fullauto_config.get("long_image_count", 10) or 10),
        "long_target_minutes": int(fullauto_config.get("long_target_minutes", 60) or 60),
        "twenty_min_prompt_count": len(list_files(
            folder_paths["twenty_min_prompts_en" if is_fullauto_english_account(upload_account) else "twenty_min_prompts"],
            {".txt", ".md"},
        )),
        "twenty_min_image_count": len(list_files(folder_paths["twenty_min_images"], IMAGE_EXTENSIONS)),
        "twenty_min_required_image_count": int(fullauto_config.get("twenty_min_image_count", 5) or 5),
        "twenty_min_target_minutes": int(fullauto_config.get("twenty_min_target_minutes", 25) or 25),
        "videos_per_run": int(fullauto_config.get("videos_per_run", 1) or 1),
        "long_videos_per_day": int(fullauto_config.get("long_videos_per_day", 1) or 1),
        "twenty_min_videos_per_day": int(fullauto_config.get("twenty_min_videos_per_day", 1) or 1),
        "long_merge_candidates": fullauto_long_merge_candidates(config),
        "drafts": configured_recovered_module(config).fullauto_drafts(),
        "auto_merge": {
            "enabled": bool(fullauto_config.get("auto_merge_enabled", False)),
            "stage1_required_count": int(fullauto_config.get("auto_merge_stage1_count", 5) or 5),
            "stage2_required_count": int(fullauto_config.get("auto_merge_stage2_count", 3) or 3),
            "stage1_candidates_count": int(stage1_snapshot["count"]),
            "stage1_cluster": str(stage1_snapshot["cluster"] or ""),
            "preferred_cluster": str(stage1_snapshot["cluster"] or ""),
            "stage1_remaining_count": max(
                0,
                int(stage1_snapshot["required"] or 5) - int(stage1_snapshot["count"] or 0),
            ),
            "stage1_total_available": len(stage1_snapshot["available"]),
            "twenty_min_clusters": twenty_min_cluster_summary(
                list(stage1_snapshot["all_records"]),
                list(stage1_snapshot["available"]),
            ),
            "stage2_candidates_count": len(unuploaded_stage1_outputs),
            "latest_stage1_output": stage1_outputs[0]["name"] if stage1_outputs else "",
            "latest_stage1_youtube_url": stage1_outputs[0]["youtube_url"] if stage1_outputs else "",
            "latest_stage1_publish_at": stage1_outputs[0]["publish_at"] if stage1_outputs else "",
        },
    }


def discover_ollama_models(fullauto_config: dict[str, Any], current_model: str) -> list[str]:
    models: list[str] = []
    base_url = str(fullauto_config.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=1) as response:
            data = json.loads(response.read().decode("utf-8"))
        models = [str(item.get("name") or "").strip() for item in data.get("models", []) if str(item.get("name") or "").strip()]
    except (OSError, ValueError, urllib.error.URLError):
        models = []

    if current_model and current_model not in models:
        models.insert(0, current_model)
    return models


def apply_twenty_min_vi_variation(prompt_text: str, prompt_name: str, variation: dict[str, str]) -> str:
    if not prompt_text:
        return prompt_text

    extra = []
    if prompt_name == "topic":
        extra = [
            "BIEN THE CHO LAN CHAY NAY:",
            f"- Huong noi dung: {variation['theme']}.",
            f"- Trong tam cam xuc: {variation['focus']}.",
            f"- Cum chu de can bam sat: {variation.get('cluster', 'general-healing')}.",
            "- Tao chu de cu the, doi song, tranh lap lai mot cach dat van de quen thuoc.",
            "- Uu tien nhung tinh huong nguoi nghe 30-65 tuoi co the gap that.",
        ]
    elif prompt_name == "title":
        extra = [
            "BIEN THE CHO LAN CHAY NAY:",
            f"- Giu giong dieu: {variation['voice']}.",
            f"- Nhan vao huong: {variation['theme']}.",
            "- Title phai danh thang vao mot noi dau/ham muon ro: dem kho ngu, dung nghi nhieu, nua doi con lai, kho vi tinh, nguoi tot gap bat hanh, nhan qua, phuoc duc, khau duc, phat tai.",
            "- Uu tien cong thuc CTR: 'Noi dau/doi tuong cu the + Loi Phat day/Nghe Phap + ket qua mong muon'.",
            "- Co the dung so/moc thoi gian neu hop chu de: '20 phut', '3 dau hieu', '5 loai kho', 'nghe 1 lan', 'nua doi con lai'.",
            "- Dat keyword chinh trong 45 ky tu dau: Loi Phat Day, Phat Day, Nghe Phap, Dem Kho Ngu, Nhan Qua.",
            "- Tao tieu de khac nhau ve goc nhin, tranh lap lai mot khuon 'buong bo de binh an'.",
            "- Khong dat title qua mem/chung chung kieu 'Loi Phat day ve binh an' neu thieu tinh huong doi song cu the.",
        ]
    elif prompt_name == "script":
        extra = [
            "BIEN THE CHO LAN CHAY NAY:",
            f"- Cau mo dau: {variation['opening']}.",
            f"- Giong ke: {variation['voice']}.",
            f"- Trong tam: {variation['focus']}.",
            "- 3 cau dau tien phai co luc giu nguoi nghe nhu Shorts: cau 1 cham dung noi dau, cau 2 lam ho thay duoc thau hieu, cau 3 hua mot ly do de o lai.",
            "- Sau 3 cau hook dau moi ha nhip: moi cham lai, tho deu, va noi ro vi sao bai nghe nay dang can thiet.",
            "- 2-4 cau dau tien BAT BUOC phai dong vai tro opening hook, khong duoc vao giai thich khai niem ngay.",
            "- Opening hook phai cham vao mot noi dau, mot cau hoi day ray rut, hoac mot loi dan rut ruot khien nguoi nghe muon o lai.",
            "- Co the dung kieu: 'Neu dem nay tam con chua chiu yen...', 'Neu con van cuoi nhung trong long da rat met...', 'Co nhung luc minh can nghe cham lai de thay long bot nang...'.",
            "- Khong dung kieu ep nguoi xem: 'Dung voi luot qua', 'Dung luot qua', 'Dung voi tat video', 'Dung bo qua'.",
            "- Tranh mo dau yeu nhu 'Chao mung ban den voi...', 'Hom nay chung ta se...', 'Trong Phat phap...', 'Duc Phat tung day...' o cau dau.",
            "- Khong duoc lao ngay vao giao ly, dinh nghia, phan tich truu tuong, hay liet ke bai hoc o cau dau.",
            "- Sau opening hook/dan nhap moi chuyen mem vao y chinh cua chu de, giu nhip noi tu nhien cho voiceover.",
            "- Nho chen it nhat 1 tinh huong doi song cu the va 1 diem chuyen hoa ro rang.",
            "- Tranh lap lai nguyen van cong thuc cua cac video truoc; moi chuong can co mot y rieng.",
        ]
        if variation.get("hook"):
            extra.append(f"- Hay mo dung tinh than cua template hook nay va bien no cho hop chu de hien tai: {variation['hook']}")
    elif prompt_name in {"seo", "description", "thumbnail", "image_prompt", "qc"}:
        extra = [
            "BIEN THE CHO LAN CHAY NAY:",
            f"- Bam sat huong: {variation['theme']}.",
            f"- Uu tien tu khoa va dien dat lien quan den: {variation['focus']}.",
        ]
        if prompt_name == "description":
            extra.extend(
                [
                    "- 160 ky tu dau phai tom tat ro noi dau + gia tri video.",
                    "- Neu description cu/outline/timeline da co san noi dung cac muc se di qua, giu lai phan do va dat duoi khung 'Trong bai nghe nay...'; khong thay bang danh sach mau chung chung.",
                    "- Ket thuc description bang 3-5 hashtag phu hop voi chu de video. Khong dung mot bo hashtag co dinh cho moi video.",
                    "- Hashtag phai co 2 tag dinh vi niche (#phatphap, #loiphatday), 1-2 tag theo noi dung chinh nhu #nhanqua, #buongbo, #taman, #phuoclanh, #duyenlanh, #khaunghiep, #longbieton, va #shorts.",
                ]
            )
        elif prompt_name == "seo":
            extra.extend(
                [
                    "- De xuat tu khoa theo y dinh tim kiem that: loi phat day, phat day, nghe phap truoc khi ngu, dem kho ngu, dung nghi nhieu, nhan qua, nghiep bao, khau duc, phuoc duc, buong bo.",
                    "- Uu tien cum tu nguoi xem co the go len YouTube, khong viet qua hoc thuat.",
                ]
            )
        elif prompt_name == "thumbnail":
            extra.extend(
                [
                    "- Overlay text chi 3-5 cum rat ngan, moi cum 2-4 tu, doc duoc tren mobile.",
                    "- Hoc pattern doi thu: tu khoa lon nhu 'DEM KHO NGU', 'DUNG NGHI NHIEU', 'TAM NHE LAI', 'NGU NGON HON', 'NHAN QUA', 'KHAU DUC'.",
                    "- Bo cuc: tuong Phat/anh sang vang lam focal point ro, text lon tuong phan manh do/vang/xanh/trang, khong qua 18 tu tren thumbnail.",
                    "- Thumbnail khong can tom tat het video; chi can chot ly do click ro nhat.",
                ]
            )
        elif prompt_name == "image_prompt":
            extra.extend(
                [
                    "- Moi canh chi nen co 1 chu the chinh va 1 cam xuc ro.",
                    "- Tranh khung hinh qua nhieu vat the, giu bo cuc tinh va de nhin.",
                ]
            )
        elif prompt_name == "qc":
            extra.extend(
                [
                    "- Kiem tra title co noi dau/ham muon ro trong 45 ky tu dau va khong chung chung.",
                    "- Kiem tra description mo dau manh, hashtag chi 3-5 cai va phai khop dung chu de, thumbnail text ngan gon va doc duoc tren mobile.",
                ]
            )

    return prompt_text.rstrip() + "\n\n" + "\n".join(extra) if extra else prompt_text


def apply_twenty_min_en_variation(prompt_text: str, prompt_name: str, variation: dict[str, str]) -> str:
    if not prompt_text:
        return prompt_text

    extra: list[str] = []
    if prompt_name == "topic":
        extra = [
            "CURRENT RUN VARIATION:",
            f"- Content direction: {variation['theme']}.",
            f"- Emotional focus: {variation['focus']}.",
            f"- Stay within this topic cluster: {variation.get('cluster', 'general-healing')}.",
            "- Prefer concrete, searchable life problems over abstract spirituality.",
            "- Make each topic feel like a clear viewer entry point, not a vague sermon idea.",
        ]
    elif prompt_name == "title":
        extra = [
            "CURRENT RUN VARIATION:",
            f"- Tone of voice: {variation['voice']}.",
            f"- Lean into: {variation['theme']}.",
            "- Use clear YouTube-style wording with a practical promise or emotional outcome.",
            "- Favor formats like 'How to...', 'Guided Meditation for...', or 'When You Feel...'.",
            "- Keep titles mobile-friendly and easy to understand at a glance.",
        ]
    elif prompt_name == "script":
        extra = [
            "CURRENT RUN VARIATION:",
            f"- Opening move: {variation['opening']}.",
            f"- Voice: {variation['voice']}.",
            f"- Main focus: {variation['focus']}.",
            "- Include at least one vivid real-life situation the listener can recognize immediately.",
            "- Keep the teaching practical, emotionally safe, and easy for non-experts to follow.",
            "- Avoid drifting into abstract doctrine without tying it back to daily life.",
        ]
    elif prompt_name in {"seo", "description", "thumbnail", "image_prompt", "qc"}:
        extra = [
            "CURRENT RUN VARIATION:",
            f"- Stay anchored in: {variation['theme']}.",
            f"- Prioritize wording around: {variation['focus']}.",
        ]
        if prompt_name == "description":
            extra.extend(
                [
                    "- The first 160 characters must clearly state the pain point and what the video helps with.",
                    "- Use only 3-5 relevant hashtags, not a long list.",
                ]
            )
        elif prompt_name == "seo":
            extra.extend(
                [
                    "- Suggest natural search phrases real viewers might type on YouTube.",
                    "- Prioritize intent-rich phrases like anxiety, overthinking, grief, sleep, inner peace, letting go, mindfulness.",
                ]
            )
        elif prompt_name == "thumbnail":
            extra.extend(
                [
                    "- Thumbnail text should be 2-5 words and instantly readable on mobile.",
                    "- Use one focal point and one feeling only.",
                ]
            )
        elif prompt_name == "image_prompt":
            extra.extend(
                [
                    "- Keep scenes visually simple, emotionally coherent, and non-cluttered.",
                    "- Each scene should support one emotional beat, not many competing ideas.",
                ]
            )
        elif prompt_name == "qc":
            extra.extend(
                [
                    "- Check that the title is clear, searchable, and not too generic.",
                    "- Check that the description opens strongly and the thumbnail concept is simple.",
                ]
            )

    return prompt_text.rstrip() + "\n\n" + "\n".join(extra) if extra else prompt_text


def upload_limit_warning() -> str:
    for job in reversed(list(JOBS.values())):
        if job.get("status") != "failed":
            continue
        logs = "\n".join(job.get("logs", []))
        if "uploadLimitExceeded" in logs:
            return "YouTube upload limit hit for this account. Wait before uploading again."
    return ""
