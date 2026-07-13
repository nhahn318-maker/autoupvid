from __future__ import annotations

import argparse
import random
from collections.abc import Callable
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from .accounts import account_state_dir, account_state_dirs, account_token_path, get_active_account_id
from .auto_images import prepare_auto_images
from .collection import create_collection
from .config import load_config
from .media import IMAGE_EXTENSIONS, discover_tracks, find_matching_images, list_files, track_with_images_from_dir
from .metadata import build_metadata
from .render import render_video
from .scheduler import next_publish_times, parse_time, to_rfc3339_utc
from .state import StateStore
from .youtube import count_videos_on_date, create_token, get_youtube_service, list_existing_video_ids, list_scheduled_publish_times, send_email_notification, upload_video

UploadStatusCallback = Callable[[str, int, str], None]


def notify_cli_event(config, command: str, status: str, details: list[str] | None = None) -> None:
    active_account = get_active_account_id(config)
    account = config.get("accounts", active_account, default={})
    account_label = account.get("label", active_account) if isinstance(account, dict) else active_account
    subject_prefix = "Thanh cong" if status == "success" else "That bai"
    subject = f"{subject_prefix} - CLI {command} - {account_label}"
    body = (
        f"Lenh: {command}\n"
        f"Trang thai: {status}\n"
        f"Tai khoan: {account_label}\n"
        f"Thoi gian: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"Chi tiet:\n" + ("\n".join(details or ["(khong co chi tiet)"]))
    )
    send_email_notification(subject, body, notification_type=status)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI music YouTube automation")
    parser.add_argument("command", choices=["init", "render", "upload", "daily", "login-account", "auto-images"])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--token-file")
    args = parser.parse_args()

    root = Path.cwd()
    if args.command == "init":
        init_project(root)
        return

    config = load_config(root, args.config)
    paths = config.paths

    if args.command == "login-account":
        token_file = root / (args.token_file or "token_account2.json")
        created = create_token(paths["credentials_file"], token_file)
        print(f"Created token: {created}")
        return

    if args.command == "auto-images":
        for result in prepare_auto_images(config, paths, limit=args.limit):
            if result.created:
                print(f"Downloaded {len(result.created)} image file(s) for {result.audio_path.name}")
            else:
                print(f"Skipped {result.audio_path.name}: {result.skipped_reason}")
        return

    state = StateStore(account_state_dir(config))
    active_account = get_active_account_id(config)
    other_states = [
        StateStore(state_dir)
        for account_id, state_dir in account_state_dirs(config).items()
        if account_id != active_account
    ]
    shorts_enabled = bool(config.get("shorts", "enabled", default=False))
    upload_types = scheduled_upload_types(config, shorts_enabled)

    image_results = prepare_auto_images(config, paths, limit=args.limit)
    for result in image_results:
        if result.created:
            print(f"Auto images: {result.audio_path.name} -> {len(result.created)} file(s)")

    tracks = [
        track
        for track in discover_tracks(paths["audio_dir"], paths["image_dir"])
        if not uploaded_in_any_state(track.audio_path, other_states)
        if not state.is_collected(track.audio_path)
        if state.needs_work(track.audio_path, shorts_enabled, upload_types)
    ]
    if args.limit:
        tracks = tracks[: args.limit]

    if not tracks:
        print("No new tracks found. Add MP3 files and images to data/input.")
        if args.command == "daily":
            maybe_create_duration_collection(config, paths, state, dry_run=args.dry_run)
        return

    try:
        if args.command == "render":
            rendered: list[str] = []
            for track in tracks:
                output = render_video(track, paths["output_dir"], config.get("render"))
                print(f"Rendered: {output}")
                rendered.append(output.name)
                if config.get("shorts", "enabled", default=False):
                    short_output = render_short(short_track_for_paths(track, paths), paths["output_dir"], config)
                    print(f"Rendered short: {short_output}")
                    rendered.append(short_output.name)
            notify_cli_event(config, "render", "success", rendered)
            return

        if args.command == "upload":
            upload_tracks(config, paths, state, tracks, schedule=False, dry_run=args.dry_run)
            notify_cli_event(
                config,
                "upload",
                "success",
                [f"So track xu ly: {len(tracks)}", f"Dry run: {args.dry_run}"],
            )
            return

        if args.command == "daily":
            upload_tracks(config, paths, state, tracks, schedule=True, dry_run=args.dry_run, upload_types=upload_types)
            maybe_create_duration_collection(config, paths, state, dry_run=args.dry_run)
            notify_cli_event(
                config,
                "daily",
                "success",
                [f"So track xu ly: {len(tracks)}", f"Dry run: {args.dry_run}", f"Upload types: {sorted(upload_types)}"],
            )
    except Exception as exc:
        notify_cli_event(
            config,
            args.command,
            "failure",
            [f"Loi: {exc}", f"Dry run: {args.dry_run}", f"So track: {len(tracks)}"],
        )
        raise


def init_project(root: Path) -> None:
    config = root / "config.json"
    if not config.exists():
        shutil.copyfile(root / "config.example.json", config)
        print("Created config.json")
    for directory in [
        "data/input/audio",
        "data/input/images",
        "data/input/short_images",
        "data/input/thumbnails",
        "data/output",
        "data/state",
        "logs",
    ]:
        (root / directory).mkdir(parents=True, exist_ok=True)
    print("Project folders are ready.")


def upload_tracks(
    config,
    paths,
    state: StateStore,
    tracks,
    schedule: bool,
    dry_run: bool,
    upload_types: set[str] | None = None,
    progress_callback: UploadStatusCallback | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> None:
    upload_types = upload_types or {"normal", "short"}
    videos_per_day = int(config.get("schedule", "videos_per_day", default=3))
    tracks = tracks[:videos_per_day] if schedule else tracks

    def emit_progress(stage: str, progress: int, detail: str) -> None:
        if progress_callback:
            progress_callback(stage, progress, detail)

    def emit_log(message: str) -> None:
        print(message)
        if log_callback:
            log_callback(message)

    def upload_progress(video_path: Path, label: str):
        last_detail = {"value": ""}

        def _callback(sent_bytes: int, total_bytes: int, state_text: str) -> None:
            total = max(total_bytes, 1)
            percent = max(0, min(100, int(sent_bytes * 100 / total)))
            progress = min(99, 90 + int(percent * 0.09))
            sent_mb = sent_bytes / (1024 * 1024)
            total_mb = total / (1024 * 1024)
            detail = f"{label} {video_path.name}: {sent_mb:.1f}/{total_mb:.1f} MB ({percent}%)"
            if state_text != "uploading":
                detail = f"{detail} | {state_text}"
            if detail != last_detail["value"]:
                last_detail["value"] = detail
                emit_progress("Uploading YouTube", progress, detail)

        return _callback

    service = None
    youtube_date_counts: dict[str, int] = {}
    youtube_blocked_dates: set[str] = set()
    if not dry_run:
        service = get_youtube_service(paths["credentials_file"], account_token_path(config))
        if schedule:
            sync_missing_youtube_uploads(state, service, emit_log)

    blocked_times = state.used_publish_times() if schedule else set()

    for track in tracks:
        if should_render_normal_before_short(config, upload_types):
            video_path = paths["output_dir"] / f"{track.slug}.mp4"
            if not video_path.exists():
                video_path = render_video(track, paths["output_dir"], config.get("render"))
                print(f"Rendered normal for collection: {video_path}")

        publish_time = None
        if schedule and "normal" in upload_types and not state.has_upload(track.audio_path, "normal"):
            publish_time = reserve_next_publish_time(
                config=config,
                blocked_times=blocked_times,
                blocked_dates=youtube_blocked_dates,
                service=service,
                youtube_date_counts=youtube_date_counts,
                slot_kind="normal",
            )
        publish_at = to_rfc3339_utc(publish_time) if publish_time else None
        privacy = config.get("channel", "privacy_status", default="private")
        if not schedule:
            privacy = config.get("schedule", "upload_privacy_when_no_schedule", default=privacy)

        if "normal" in upload_types and not state.has_upload(track.audio_path, "normal"):
            video_path = paths["output_dir"] / f"{track.slug}.mp4"
            if not video_path.exists():
                video_path = render_video(track, paths["output_dir"], config.get("render"))

            metadata = build_metadata(track, config.data, paths["thumbnail_dir"])
            if dry_run:
                emit_log(f"Would upload: {video_path}")
                emit_log(f"Title: {metadata.title}")
                emit_log(f"Privacy: {'private scheduled' if publish_at else privacy}")
                if publish_at:
                    emit_log(f"Publish at: {publish_at}")
            else:
                emit_progress("Uploading YouTube", 90, f"Preparing normal upload: {video_path.name}")
                video_id = upload_video(
                    service=service,
                    video_path=video_path,
                    metadata=metadata,
                    privacy_status=privacy,
                    publish_at=publish_at,
                    progress_callback=upload_progress(video_path, "Normal"),
                )
                state.add_upload(
                    {
                        "audio": str(track.audio_path),
                        "video": str(video_path),
                        "type": "normal",
                        "youtube_id": video_id,
                        "publish_at": publish_at,
                    }
                )
                emit_log(f"Uploaded: https://www.youtube.com/watch?v={video_id}")
        elif "normal" in upload_types:
            emit_log(f"Skip normal already uploaded: {track.title}")

        if (
            "short" in upload_types
            and config.get("shorts", "enabled", default=False)
            and not state.has_upload(track.audio_path, "short")
        ):
            short_track = short_track_for_paths(track, paths)
            short_path = paths["output_dir"] / f"{track.slug}-short.mp4"
            if not short_path.exists():
                short_path = render_short(short_track, paths["output_dir"], config)

            short_metadata = build_metadata(track, config.data, paths["thumbnail_dir"], video_type="short")
            short_publish_at = None
            if publish_time:
                short_time = short_publish_time_for_normal(config, publish_time, blocked_times)
                short_publish_at = to_rfc3339_utc(short_time)
                blocked_times.add(short_publish_at)
            elif schedule:
                short_time = reserve_next_publish_time(
                    config=config,
                    blocked_times=blocked_times,
                    blocked_dates=youtube_blocked_dates,
                    service=service,
                    youtube_date_counts=youtube_date_counts,
                    slot_kind="short",
                )
                short_publish_at = to_rfc3339_utc(short_time)

            if dry_run:
                emit_log(f"Would upload short: {short_path}")
                emit_log(f"Short title: {short_metadata.title}")
                emit_log(f"Short privacy: {'private scheduled' if short_publish_at else privacy}")
                if short_publish_at:
                    emit_log(f"Short publish at: {short_publish_at}")
            else:
                emit_progress("Uploading YouTube", 90, f"Preparing short upload: {short_path.name}")
                short_video_id = upload_video(
                    service=service,
                    video_path=short_path,
                    metadata=short_metadata,
                    privacy_status=privacy,
                    publish_at=short_publish_at,
                    progress_callback=upload_progress(short_path, "Short"),
                )
                state.add_upload(
                    {
                        "audio": str(track.audio_path),
                        "video": str(short_path),
                        "type": "short",
                        "youtube_id": short_video_id,
                        "publish_at": short_publish_at,
                    }
                )
                emit_log(f"Uploaded short: https://www.youtube.com/watch?v={short_video_id}")
        elif "short" in upload_types and state.has_upload(track.audio_path, "short"):
            emit_log(f"Skip short already uploaded: {track.title}")

        if not dry_run and state.is_complete(
            track.audio_path,
            bool(config.get("shorts", "enabled", default=False)),
            upload_types,
        ):
            state.mark_processed(track.audio_path)


def scheduled_upload_types(config, shorts_enabled: bool) -> set[str]:
    configured = config.get("schedule", "upload_types", default=None)
    if configured:
        upload_types = {str(item).strip().lower() for item in configured}
    else:
        upload_types = {"normal", "short"} if shorts_enabled else {"normal"}
    if not shorts_enabled:
        upload_types.discard("short")
    return upload_types & {"normal", "short"}


def sync_missing_youtube_uploads(state: StateStore, service, emit_log: Callable[[str], None] | None = None) -> int:
    video_ids = state.youtube_video_ids()
    if not video_ids:
        return 0
    existing_video_ids = list_existing_video_ids(service, video_ids)
    removed = state.prune_missing_youtube_uploads(existing_video_ids)
    if removed and emit_log:
        emit_log(f"Synced state: removed {removed} deleted YouTube upload record(s).")
    return removed


def short_track_for_paths(track, paths) -> object:
    short_image_dir = paths.get("short_image_dir")
    if short_image_dir:
        image_files = list_files(short_image_dir, IMAGE_EXTENSIONS)
        if image_files:
            count = min(5, len(image_files))
            chosen = random.sample(image_files, k=count)
            if len(chosen) < 5:
                chosen.extend(random.choices(image_files, k=5 - len(chosen)))
            return track.__class__(
                audio_path=track.audio_path,
                image_paths=tuple(chosen),
                title=track.title,
            )
        return track_with_images_from_dir(track, short_image_dir, allow_latest_fallback=False)
    return track


def fullauto_story_short_track(track, config) -> object:
    active_account = str(get_active_account_id(config) or "").strip()
    if active_account not in {"account1", "account2", "account3", "account4"}:
        return track
    image_pool_dirs = config.get("fullauto", "image_pool_dirs", default={}) or {}
    image_pool_dir = ""
    if isinstance(image_pool_dirs, dict):
        image_pool_dir = str(image_pool_dirs.get(active_account) or "").strip()
    if not image_pool_dir:
        image_pool_dir = str(config.get("fullauto", "image_pool_dir", default="") or "").strip()
    if not image_pool_dir:
        return track
    pool_dir = config.root / image_pool_dir
    image_files = list_files(pool_dir, IMAGE_EXTENSIONS)
    if not image_files:
        return track
    chosen = random.sample(image_files, k=min(5, len(image_files)))
    if len(chosen) < 5:
        chosen.extend(random.choices(image_files, k=5 - len(chosen)))
    return track.__class__(
        audio_path=track.audio_path,
        image_paths=tuple(chosen),
        title=track.title,
    )


def short_publish_time_for_normal(config, normal_publish_time: datetime, blocked_times: set[str]) -> datetime:
    configured = config.get("shorts", "publish_times", default=None)
    if isinstance(configured, list) and configured:
        for item in configured:
            candidate = datetime.combine(normal_publish_time.date(), parse_time(str(item)), tzinfo=normal_publish_time.tzinfo)
            candidate_key = to_rfc3339_utc(candidate)
            if candidate > normal_publish_time and candidate_key not in blocked_times:
                return candidate
    offset = int(config.get("shorts", "publish_offset_minutes", default=30))
    return normal_publish_time + timedelta(minutes=offset)


def should_render_normal_before_short(config, upload_types: set[str]) -> bool:
    return (
        "short" in upload_types
        and "normal" not in upload_types
        and bool(config.get("schedule", "render_normal_when_short_only", default=False))
    )


def maybe_create_duration_collection(config, paths, state: StateStore, dry_run: bool) -> None:
    collection_config = config.get("collection", default={})
    target_minutes = float(collection_config.get("target_duration_minutes", 0) or 0)
    if target_minutes <= 0 or not bool(collection_config.get("auto_create", False)):
        return

    tracks = [
        track
        for track in discover_tracks(paths["audio_dir"], paths["image_dir"])
        if not state.is_collected(track.audio_path)
    ]
    if dry_run:
        print(f"Would check long collection target: about {target_minutes:g} minutes.")
        return

    try:
        output, source_audio = create_collection(
            tracks=tracks,
            output_dir=paths["output_dir"],
            state_dir=paths["state_dir"],
            collection_config=collection_config,
        )
    except ValueError as exc:
        print(f"Long collection not ready: {exc}")
        return

    state.mark_collected(source_audio)
    print(f"Created long collection: {output}")
    deleted = cleanup_collection_source_files(
        tracks=tracks,
        source_audio=source_audio,
        paths=paths,
        config=config,
        collection_config=collection_config,
    )
    if deleted:
        print(f"Cleaned {deleted} source media file(s).")


def cleanup_collection_source_files(
    tracks,
    source_audio: list[Path],
    paths,
    config,
    collection_config,
) -> int:
    if not bool(collection_config.get("cleanup_after_create", True)):
        return 0

    source_audio_set = {audio.resolve() for audio in source_audio}
    source_tracks = [track for track in tracks if track.audio_path.resolve() in source_audio_set]
    targets: set[Path] = set()
    if bool(collection_config.get("delete_source_audio", True)):
        targets.update(track.audio_path for track in source_tracks)
    if bool(collection_config.get("delete_source_images", True)):
        image_files = list_files(paths["image_dir"], IMAGE_EXTENSIONS)
        for track in source_tracks:
            targets.update(find_matching_images(track.audio_path, image_files))
    if bool(collection_config.get("delete_source_videos", True)):
        targets.update(paths["output_dir"] / f"{track.slug}.mp4" for track in source_tracks)
    if bool(collection_config.get("delete_source_shorts", True)):
        targets.update(paths["output_dir"] / f"{track.slug}-short.mp4" for track in source_tracks)
    if bool(collection_config.get("delete_source_thumbnails", True)):
        thumbnail_files = list_files(paths["thumbnail_dir"], IMAGE_EXTENSIONS)
        for track in source_tracks:
            targets.update(find_matching_images(track.audio_path, thumbnail_files))
            metadata = build_metadata(track, config.data, paths["thumbnail_dir"])
            if metadata.thumbnail_path:
                targets.add(metadata.thumbnail_path)

    return delete_files_in_allowed_dirs(
        targets=targets,
        allowed_dirs={
            paths["audio_dir"].resolve(),
            paths["image_dir"].resolve(),
            paths["output_dir"].resolve(),
            paths["thumbnail_dir"].resolve(),
        },
    )


def delete_files_in_allowed_dirs(targets: set[Path], allowed_dirs: set[Path]) -> int:
    deleted = 0
    for target in targets:
        resolved = target.resolve()
        if resolved.parent not in allowed_dirs:
            continue
        if resolved.exists() and resolved.is_file():
            resolved.unlink()
            deleted += 1
    return deleted


def reserve_next_publish_time(
    config,
    blocked_times: set[str],
    blocked_dates: set[str] | None = None,
    service=None,
    youtube_date_counts: dict[str, int] | None = None,
    slot_kind: str = "normal",
):
    blocked_times = blocked_times or set()
    blocked_dates = blocked_dates or set()
    youtube_date_counts = youtube_date_counts if youtube_date_counts is not None else {}
    timezone_name = config.get("schedule", "timezone")
    if slot_kind == "short":
        configured_times = config.get("schedule", "publish_times") or config.get("shorts", "publish_times", default=None)
    else:
        configured_times = config.get("schedule", "publish_times")
    allowed_weekdays = config.get("schedule", "allowed_weekdays", default=None)
    day_interval = config.get("schedule", "day_interval", default=None)
    interval_anchor_date = config.get("schedule", "interval_anchor_date", default=None)

    if service is not None:
        try:
            youtube_scheduled = list_scheduled_publish_times(service, scheduled_kind=slot_kind)
            blocked_times.update(youtube_scheduled)
            for value in youtube_scheduled:
                try:
                    scheduled_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    continue
                day_key = scheduled_dt.astimezone(ZoneInfo(timezone_name)).date().isoformat()
                youtube_date_counts[day_key] = youtube_date_counts.get(day_key, 0) + 1
        except Exception as exc:
            print(f"Skip YouTube scheduled-slot check: {exc}")

    start_date_str = config.get("schedule", "start_date", default=None)
    if start_date_str:
        try:
            from datetime import date
            from zoneinfo import ZoneInfo
            start_date = date.fromisoformat(start_date_str)
            tz = ZoneInfo(timezone_name)
            curr_date = datetime.now(tz).date()
            temp_date = curr_date
            while temp_date < start_date:
                blocked_dates.add(temp_date.isoformat())
                temp_date += timedelta(days=1)
        except Exception as exc:
            print(f"Error parsing schedule start_date: {exc}")

    daily_limit = effective_daily_upload_limit(config)

    while True:
        publish_time = next_publish_times(
            count=1,
            configured_times=configured_times,
            timezone_name=timezone_name,
            blocked_times=blocked_times,
            blocked_dates=blocked_dates,
            allowed_weekdays=allowed_weekdays,
            day_interval=day_interval,
            interval_anchor_date=interval_anchor_date,
        )[0]
        day_key = publish_time.date().isoformat()
        if service is not None and day_key not in youtube_date_counts:
            try:
                youtube_date_counts[day_key] = count_videos_on_date(service, publish_time.date(), timezone_name)
            except Exception as exc:  # noqa: BLE001 - readonly scope may not be granted.
                print(f"Skip YouTube day-count check: {exc}")
                youtube_date_counts[day_key] = 0
        if service is not None and youtube_date_counts.get(day_key, 0) >= daily_limit:
            blocked_dates.add(day_key)
            print(f"Skip {day_key}: YouTube already has {youtube_date_counts[day_key]} video(s).")
            continue

        blocked_times.add(to_rfc3339_utc(publish_time))
        if service is not None:
            youtube_date_counts[day_key] = youtube_date_counts.get(day_key, 0) + 1
        return publish_time


def effective_daily_upload_limit(config) -> int:
    configured = config.get("schedule", "daily_upload_limit", default=None)
    if configured:
        return int(configured)
    videos_per_day = int(config.get("schedule", "videos_per_day", default=2))
    upload_types = config.get("schedule", "upload_types", default=None)
    if isinstance(upload_types, list):
        type_count = len({str(item).strip().lower() for item in upload_types if str(item).strip()})
        return max(videos_per_day, videos_per_day * max(1, type_count))
    return videos_per_day


def render_short(track, output_dir: Path, config) -> Path:
    track = fullauto_story_short_track(track, config)
    shorts_config = dict(config.get("render"))
    shorts_config["subscribe_overlay"] = {"enabled": False}
    shorts = config.get("shorts")
    shorts_config["resolution"] = shorts.get("resolution", "1080x1920")
    for key in [
        "subtitle_font_size",
        "subtitle_font_name",
        "subtitle_margin_v",
        "subtitle_margin_h",
        "subtitle_primary_color",
        "subtitle_highlight_color",
        "subtitle_outline_color",
        "subtitle_back_color",
        "subtitle_outline",
        "subtitle_shadow",
        "subtitle_border_style",
        "subtitle_bold",
        "use_synced_subtitles",
        "zoom_max",
        "zoom_step",
        "short_dynamic_effects",
        "image_segment_seconds",
        "contextual_image_timing",
        "background_music",
    ]:
        if key in shorts:
            shorts_config[key] = shorts[key]
    return render_video(
        track=track,
        output_dir=output_dir,
        render_config=shorts_config,
        suffix="-short",
        max_duration_seconds=int(shorts.get("max_duration_seconds", 59)),
    )


def uploaded_in_any_state(audio_path: Path, states: list[StateStore]) -> bool:
    return any(state.has_upload(audio_path, "normal") or state.has_upload(audio_path, "short") for state in states)


if __name__ == "__main__":
    main()
