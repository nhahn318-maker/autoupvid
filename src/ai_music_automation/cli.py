from __future__ import annotations

import argparse
import shutil
from datetime import timedelta
from pathlib import Path

from .accounts import account_state_dir, account_state_dirs, account_token_path, get_active_account_id
from .config import load_config
from .media import discover_tracks
from .metadata import build_metadata
from .render import render_video
from .scheduler import next_publish_times, to_rfc3339_utc
from .state import StateStore
from .youtube import count_videos_on_date, create_token, get_youtube_service, upload_video


def main() -> None:
    parser = argparse.ArgumentParser(description="AI music YouTube automation")
    parser.add_argument("command", choices=["init", "render", "upload", "daily", "login-account"])
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

    state = StateStore(account_state_dir(config))
    active_account = get_active_account_id(config)
    other_states = [
        StateStore(state_dir)
        for account_id, state_dir in account_state_dirs(config).items()
        if account_id != active_account
    ]
    shorts_enabled = bool(config.get("shorts", "enabled", default=False))

    tracks = [
        track
        for track in discover_tracks(paths["audio_dir"], paths["image_dir"])
        if not uploaded_in_any_state(track.audio_path, other_states)
        if state.needs_work(track.audio_path, shorts_enabled)
    ]
    if args.limit:
        tracks = tracks[: args.limit]

    if not tracks:
        print("No new tracks found. Add MP3 files and images to data/input.")
        return

    if args.command == "render":
        for track in tracks:
            output = render_video(track, paths["output_dir"], config.get("render"))
            print(f"Rendered: {output}")
            if config.get("shorts", "enabled", default=False):
                short_output = render_short(track, paths["output_dir"], config)
                print(f"Rendered short: {short_output}")
        return

    if args.command == "upload":
        upload_tracks(config, paths, state, tracks, schedule=False, dry_run=args.dry_run)
        return

    if args.command == "daily":
        upload_tracks(config, paths, state, tracks, schedule=True, dry_run=args.dry_run)


def init_project(root: Path) -> None:
    config = root / "config.json"
    if not config.exists():
        shutil.copyfile(root / "config.example.json", config)
        print("Created config.json")
    for directory in [
        "data/input/audio",
        "data/input/images",
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
) -> None:
    upload_types = upload_types or {"normal", "short"}
    videos_per_day = int(config.get("schedule", "videos_per_day", default=3))
    tracks = tracks[:videos_per_day] if schedule else tracks

    blocked_times = state.used_publish_times() if schedule else set()

    service = None
    youtube_date_counts: dict[str, int] = {}
    youtube_blocked_dates: set[str] = set()
    if not dry_run:
        service = get_youtube_service(paths["credentials_file"], account_token_path(config))

    for track in tracks:
        publish_time = None
        if schedule and "normal" in upload_types and not state.has_upload(track.audio_path, "normal"):
            publish_time = reserve_next_publish_time(
                config=config,
                blocked_times=blocked_times,
                blocked_dates=youtube_blocked_dates,
                service=service,
                youtube_date_counts=youtube_date_counts,
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
                print(f"Would upload: {video_path}")
                print(f"Title: {metadata.title}")
                print(f"Privacy: {'private scheduled' if publish_at else privacy}")
                if publish_at:
                    print(f"Publish at: {publish_at}")
            else:
                video_id = upload_video(
                    service=service,
                    video_path=video_path,
                    metadata=metadata,
                    privacy_status=privacy,
                    publish_at=publish_at,
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
                print(f"Uploaded: https://www.youtube.com/watch?v={video_id}")
        elif "normal" in upload_types:
            print(f"Skip normal already uploaded: {track.title}")

        if (
            "short" in upload_types
            and config.get("shorts", "enabled", default=False)
            and not state.has_upload(track.audio_path, "short")
        ):
            short_path = paths["output_dir"] / f"{track.slug}-short.mp4"
            if not short_path.exists():
                short_path = render_short(track, paths["output_dir"], config)

            short_metadata = build_metadata(track, config.data, paths["thumbnail_dir"], video_type="short")
            short_publish_at = None
            if publish_time:
                offset = int(config.get("shorts", "publish_offset_minutes", default=30))
                short_time = publish_time + timedelta(minutes=offset)
                short_publish_at = to_rfc3339_utc(short_time)
                blocked_times.add(short_publish_at)
            elif schedule:
                short_time = reserve_next_publish_time(
                    config=config,
                    blocked_times=blocked_times,
                    blocked_dates=youtube_blocked_dates,
                    service=service,
                    youtube_date_counts=youtube_date_counts,
                )
                short_publish_at = to_rfc3339_utc(short_time)

            if dry_run:
                print(f"Would upload short: {short_path}")
                print(f"Short title: {short_metadata.title}")
                print(f"Short privacy: {'private scheduled' if short_publish_at else privacy}")
                if short_publish_at:
                    print(f"Short publish at: {short_publish_at}")
            else:
                short_video_id = upload_video(
                    service=service,
                    video_path=short_path,
                    metadata=short_metadata,
                    privacy_status=privacy,
                    publish_at=short_publish_at,
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
                print(f"Uploaded short: https://www.youtube.com/watch?v={short_video_id}")
        elif "short" in upload_types and state.has_upload(track.audio_path, "short"):
            print(f"Skip short already uploaded: {track.title}")

        if not dry_run and state.is_complete(track.audio_path, bool(config.get("shorts", "enabled", default=False))):
            state.mark_processed(track.audio_path)


def reserve_next_publish_time(
    config,
    blocked_times: set[str],
    blocked_dates: set[str] | None = None,
    service=None,
    youtube_date_counts: dict[str, int] | None = None,
):
    blocked_dates = blocked_dates or set()
    youtube_date_counts = youtube_date_counts if youtube_date_counts is not None else {}
    timezone_name = config.get("schedule", "timezone")
    daily_limit = int(config.get("schedule", "videos_per_day", default=2))

    while True:
        publish_time = next_publish_times(
            count=1,
            configured_times=config.get("schedule", "publish_times"),
            timezone_name=timezone_name,
            blocked_times=blocked_times,
            blocked_dates=blocked_dates,
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


def render_short(track, output_dir: Path, config) -> Path:
    shorts_config = dict(config.get("render"))
    shorts = config.get("shorts")
    shorts_config["resolution"] = shorts.get("resolution", "1080x1920")
    for key in [
        "subtitle_font_size",
        "subtitle_margin_v",
        "subtitle_words_per_chunk",
        "subtitle_max_chars_per_chunk",
        "use_synced_subtitles",
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
