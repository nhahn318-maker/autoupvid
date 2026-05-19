from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

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
    target = {
        "audio": paths["audio_dir"],
        "images": paths["image_dir"],
        "thumbnails": paths["thumbnail_dir"],
        "output": paths["output_dir"],
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


def upload_limit_warning() -> str:
    for job in reversed(list(JOBS.values())):
        if job.get("status") != "failed":
            continue
        logs = "\n".join(job.get("logs", []))
        if "uploadLimitExceeded" in logs:
            return "YouTube upload limit hit for this account. Wait before uploading again."
    return ""
