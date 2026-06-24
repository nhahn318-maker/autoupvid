from __future__ import annotations
import importlib.machinery
import importlib.util
import inspect
import json
import os
import queue
import random
import shutil
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .accounts import account_state_dir, account_state_dirs, account_token_path, get_accounts, get_active_account_id
from .cli import init_project, render_short, upload_tracks
from .collection import collection_candidates, create_collection
from .config import load_config
from .media import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, discover_tracks, find_matching_images, list_files
from .metadata import build_metadata
from .render import render_video
from .scheduler import next_publish_times, to_rfc3339_utc
from .state import StateStore
from .tts import DEFAULT_VOICES, generate_voice


ROOT = Path.cwd()
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "web_static"
JOBS: dict[str, dict[str, Any]] = {}
JOB_QUEUE: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue()
WORKER_LOCK = threading.Lock()
WORKER_THREAD: threading.Thread | None = None

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
        "mode": "story" if active_account == "account1" else "bolero",
        "fullauto": fullauto_status(config),
        "paths": {key: str(value.relative_to(ROOT)) for key, value in paths.items() if value.is_relative_to(ROOT)},
        "upload_policy": {
            "videos_per_day": int(config.get("schedule", "videos_per_day", default=3)),
            "warning": (
                "Story mode is capped at 1 video per day for safer publishing."
                if active_account == "account1"
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
        "jobs": list(reversed(list(JOBS.values())))[0:8],
        "tts_voices": DEFAULT_VOICES,
    }


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
    config = load_config(ROOT)
    paths = config.paths
    target = paths["audio_dir"] / Path(filename).name
    if not target.exists() or target.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(target)


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
    }
    if action not in action_map:
        raise HTTPException(status_code=404, detail="Unknown Full Auto action")
    job_id = enqueue_job(action_map[action], {"target_account": target_account})
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


@app.post("/api/fullauto/thumbnail")
async def api_fullauto_thumbnail(
    draft_id: str = Form(...),
    thumbnail: UploadFile = File(...),
) -> dict[str, str]:
    recovered = configured_recovered_module(load_config(ROOT))
    return await recovered.api_fullauto_thumbnail(draft_id=draft_id, thumbnail=thumbnail)


def enqueue_job(action: str, payload: dict[str, Any]) -> str:
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "id": job_id,
        "action": action,
        "status": "queued",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "started_at": None,
        "finished_at": None,
        "logs": [f"{datetime.now().strftime('%H:%M:%S')} Queued"],
    }

    JOB_QUEUE.put((job_id, action, payload))
    ensure_worker_running()
    return job_id


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict[str, Any]:
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Unknown job")
    return JOBS[job_id]


@app.post("/api/open-folder/{folder}")
def api_open_folder(folder: str) -> dict[str, str]:
    config = load_config(ROOT)
    paths = config.paths
    fullauto_paths = fullauto_folder_paths(config)
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


def job_worker() -> None:
    while True:
        try:
            job_id, action, payload = JOB_QUEUE.get_nowait()
        except queue.Empty:
            return
        try:
            run_action(job_id, action, payload)
        finally:
            JOB_QUEUE.task_done()


def run_action(job_id: str, action: str, payload: dict[str, Any]) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = datetime.now().isoformat(timespec="seconds")
    log(job, "Started")
    try:
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
            mark_done(job)
            return

        if action == "sync-state":
            removed = state.prune_missing_audio()
            log(job, f"Removed {removed} stale state item(s).")
            mark_done(job)
            return

        if action == "create-collection":
            output = create_collection(
                tracks=all_tracks,
                output_dir=paths["output_dir"],
                state_dir=paths["state_dir"],
                collection_config=config.get("collection", default={}),
            )
            log(job, f"Created collection {output.name}")
            mark_done(job)
            return

        if action in {"fullauto-start", "fullauto-long-start", "fullauto-20min-start"}:
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

            recovered = configured_recovered_module(config)
            if action == "fullauto-start":
                count = recovered.run_fullauto_story_job(job, config, paths, state, upload_config=config)
                log(job, f"Full Auto Story finished with {count} short(s).")
            elif action == "fullauto-long-start":
                output = recovered.run_fullauto_long_job(job, config, paths, state, upload_config=config)
                log(job, f"Full Auto Long finished: {Path(output).name}")
            else:
                output = recovered.run_fullauto_twenty_min_job(job, config, paths, state, upload_config=config)
                log(job, f"Full Auto 20-Min finished: {Path(output).name}")
            mark_done(job)
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
                mark_done(job)
                return
            if track_action == "skip":
                state.mark_processed(track.audio_path)
                log(job, f"Skipped {track.audio_path.name}")
                mark_done(job)
                return
            if track_action in {"render", "rerender"}:
                output = render_video(track, paths["output_dir"], config.get("render"))
                log(job, f"Rendered {output.name}")
                if config.get("shorts", "enabled", default=False):
                    short_output = render_short(track, paths["output_dir"], config)
                    log(job, f"Rendered {short_output.name}")
                mark_done(job)
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
            mark_done(job)
            return

        tracks = [
            track
            for track in all_tracks
            if not uploaded_in_any_state(track.audio_path, other_states)
            if state.needs_work(track.audio_path, shorts_enabled)
        ]
        if not tracks:
            log(job, "No new tracks found.")
            mark_done(job)
            return

        if action == "render":
            for track in tracks:
                output = render_video(track, paths["output_dir"], config.get("render"))
                log(job, f"Rendered {output.name}")
                if config.get("shorts", "enabled", default=False):
                    short_output = render_short(track, paths["output_dir"], config)
                    log(job, f"Rendered {short_output.name}")
            mark_done(job)
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
        mark_done(job)
    except Exception as exc:  # noqa: BLE001 - surfaced to local operator UI.
        job["status"] = "failed"
        job["finished_at"] = datetime.now().isoformat(timespec="seconds")
        log(job, f"Error: {exc}")


def log(job: dict[str, Any], message: str) -> None:
    job["logs"].append(f"{datetime.now().strftime('%H:%M:%S')} {message}")


def mark_done(job: dict[str, Any]) -> None:
    job["status"] = "done"
    job["finished_at"] = datetime.now().isoformat(timespec="seconds")


def safe_filename(value: str) -> str:
    keep = []
    for char in Path(value).name:
        if char.isalnum() or char in {" ", ".", "_", "-"}:
            keep.append(char)
    return "".join(keep).strip() or "upload"


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


def fullauto_channel_slug(account_id: str) -> str:
    return {
        "account1": "story",
        "account4": "silent_horizone",
    }.get(account_id, "story")


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
        "short_images": ROOT / str(fullauto_config.get("image_pool_dir") or short_root / "images"),
        "short_drafts": ROOT / str(fullauto_config.get("draft_dir") or short_root / "drafts"),
        "long_prompts": long_root / "prompts",
        "long_images": long_root / "images",
        "long_drafts": long_root / "drafts",
        "twenty_min_prompts": twenty_min_root / "prompts",
        "twenty_min_images": shared_root / "twenty-min" / "images",
        "twenty_min_drafts": twenty_min_root / "drafts",
        "effects": long_assets_root / "ambient",
        "wave": long_assets_root / "wave",
        "stickers": long_assets_root / "stickers",
    }


def configured_recovered_module(config):
    recovered = load_recovered_module()
    folder_paths = fullauto_folder_paths(config)
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

    recovered.fullauto_long_workspace = patched_long_workspace
    recovered.fullauto_twenty_min_workspace = patched_twenty_min_workspace

    original_render_video = recovered.render_video

    def patched_render_video(track, output_dir: Path, render_config: dict[str, Any], suffix: str = "", max_duration_seconds: int | None = None):
        if any(frame.function == "run_fullauto_twenty_min_job" for frame in inspect.stack()):
            image_pool = list_files(folder_paths["twenty_min_images"], IMAGE_EXTENSIONS)
            if image_pool and len(track.image_paths) != 5:
                chosen = random.sample(image_pool, k=min(5, len(image_pool)))
                if len(chosen) < 5:
                    chosen.extend(random.choices(image_pool, k=5 - len(chosen)))
                track = track.__class__(
                    audio_path=track.audio_path,
                    image_paths=tuple(chosen),
                    title=track.title,
                )
        return original_render_video(track, output_dir, render_config, suffix=suffix, max_duration_seconds=max_duration_seconds)

    recovered.render_video = patched_render_video
    recovered.ensure_fullauto_dirs()
    return recovered


def relative_path(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


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
        upload_account: accounts.get(upload_account, {"label": upload_account}),
    }

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
        "twenty_min_prompt_count": len(list_files(folder_paths["twenty_min_prompts"], {".txt", ".md"})),
        "twenty_min_image_count": len(list_files(folder_paths["twenty_min_images"], IMAGE_EXTENSIONS)),
        "twenty_min_required_image_count": int(fullauto_config.get("twenty_min_image_count", 5) or 5),
        "twenty_min_target_minutes": int(fullauto_config.get("twenty_min_target_minutes", 25) or 25),
        "videos_per_run": int(fullauto_config.get("videos_per_run", 1) or 1),
        "long_videos_per_day": int(fullauto_config.get("long_videos_per_day", 1) or 1),
        "twenty_min_videos_per_day": int(fullauto_config.get("twenty_min_videos_per_day", 1) or 1),
        "drafts": configured_recovered_module(config).fullauto_drafts(),
        "auto_merge": {
            "enabled": bool(fullauto_config.get("auto_merge_enabled", False)),
            "stage1_required_count": int(fullauto_config.get("auto_merge_stage1_count", 5) or 5),
            "stage2_required_count": int(fullauto_config.get("auto_merge_stage2_count", 3) or 3),
            "stage1_candidates_count": len(list_files((ROOT / "data" / "output" / "story"), {".mp4"})),
            "stage2_candidates_count": len(list_files((ROOT / "data" / "output" / "story"), {".mp4"})) // max(1, int(fullauto_config.get("auto_merge_stage1_count", 5) or 5)),
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


def upload_limit_warning() -> str:
    for job in reversed(list(JOBS.values())):
        if job.get("status") != "failed":
            continue
        logs = "\n".join(job.get("logs", []))
        if "uploadLimitExceeded" in logs:
            return "YouTube upload limit hit for this account. Wait before uploading again."
    return ""
