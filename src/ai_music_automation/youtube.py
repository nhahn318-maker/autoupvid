from __future__ import annotations

import subprocess
import time as time_module
import socket
import ssl
from collections.abc import Callable
from datetime import date, datetime, time, timezone
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import httplib2
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

import json
import smtplib
from email.message import EmailMessage

from .auto_thumbnail import ensure_auto_thumbnail, thumbnail_kind
from .metadata import VideoMetadata
from .config import repair_mojibake


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
MAX_UPLOAD_RETRIES = 8
RETRYABLE_HTTP_STATUSES = {500, 502, 503, 504}
YOUTUBE_HTTP_TIMEOUT = 120
UploadProgressCallback = Callable[[int, int, str], None]


def safe_console_print(message: object) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        text = str(message).encode("ascii", errors="replace").decode("ascii")
        print(text)


def _account_label_from_token_file(config: dict, token_file_name: str) -> str | None:
    accounts = config.get("accounts")
    if not isinstance(accounts, dict):
        return None
    for account in accounts.values():
        if not isinstance(account, dict):
            continue
        if account.get("token_file") == token_file_name:
            label = account.get("label")
            if isinstance(label, str) and label.strip():
                return label.strip()
    return None


def _load_notification_settings() -> tuple[dict[str, object] | None, dict[str, object] | None]:
    config_path = Path("config.json")
    if not config_path.exists():
        return None, None
    try:
        config = repair_mojibake(json.loads(config_path.read_text(encoding="utf-8-sig")))
        notify = config.get("notifications")
        if not isinstance(notify, dict):
            return config, None
        return config, notify
    except Exception:
        return None, None


def send_email_notification(
    subject: str,
    body: str,
    *,
    notification_type: str = "system",
    force: bool = False,
) -> bool:
    config, notify = _load_notification_settings()
    if not config or not notify or not notify.get("email_enabled", False):
        return False

    if not force:
        if notification_type == "success" and not notify.get("notify_on_success", False):
            return False
        if notification_type == "failure" and not notify.get("notify_on_failure", False):
            return False

    smtp_host = notify.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(notify.get("smtp_port", 587))
    smtp_username = notify.get("smtp_username")
    smtp_password = notify.get("smtp_password")
    to_email = notify.get("to_email")
    from_email = notify.get("from_email") or smtp_username
    if not (smtp_username and smtp_password and to_email):
        return False

    try:
        message = EmailMessage()
        message["From"] = from_email
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
        safe_console_print(f"Da gui email thong bao toi {to_email}: {subject}")
        return True
    except Exception as exc:
        safe_console_print(f"Khong the gui email thong bao '{subject}': {exc}")
        return False


def send_auth_notification(token_file_name: str):
    config, _notify = _load_notification_settings()
    if not config:
        return
    account_label = _account_label_from_token_file(config, token_file_name)
    account_display = f"{account_label} ({token_file_name})" if account_label else token_file_name
    subject = f"Yeu cau cap quyen YouTube - {account_display}"
    body = (
        f"He thong tu dong tao video yeu cau ban cap quyen truy cap YouTube.\n\n"
        f"Kenh: {account_label or 'Khong ro'}\n"
        f"Tep token: {token_file_name}\n"
        f"Vui long kiem tra man hinh may chu/browser de thuc hien dang nhap va cap quyen.\n\n"
        f"Thoi gian yeu cau: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    send_email_notification(subject, body, notification_type="system", force=True)


def _get_google_credentials(credentials_file: Path, token_file: Path):
    credentials = None
    if token_file.exists():
        credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        if not credentials.has_scopes(SCOPES):
            credentials = None

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError:
            credentials = None
            token_file.unlink(missing_ok=True)

    if not credentials or not credentials.valid:
        if not credentials_file.exists():
            raise FileNotFoundError(
                f"Missing {credentials_file}. Download OAuth client JSON from Google Cloud and save it here."
            )
        send_auth_notification(token_file.name)
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    token_file.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def _authorized_http(credentials):
    raw_http = httplib2.Http(timeout=YOUTUBE_HTTP_TIMEOUT)
    # Google uses 308 as the resumable-upload acknowledgement, not a redirect.
    raw_http.redirect_codes = raw_http.redirect_codes - {308}
    return AuthorizedHttp(credentials, http=raw_http)


def get_youtube_service(credentials_file: Path, token_file: Path):
    credentials = _get_google_credentials(credentials_file, token_file)
    http = _authorized_http(credentials)
    return build("youtube", "v3", http=http, cache_discovery=False)


def get_youtube_analytics_service(credentials_file: Path, token_file: Path):
    credentials = _get_google_credentials(credentials_file, token_file)
    http = _authorized_http(credentials)
    return build("youtubeAnalytics", "v2", http=http, cache_discovery=False)


def get_youtube_reporting_service(credentials_file: Path, token_file: Path):
    credentials = _get_google_credentials(credentials_file, token_file)
    http = _authorized_http(credentials)
    return build("youtubereporting", "v1", http=http, cache_discovery=False), credentials


def create_token(credentials_file: Path, token_file: Path) -> Path:
    if not credentials_file.exists():
        raise FileNotFoundError(
            f"Missing {credentials_file}. Download OAuth client JSON from Google Cloud and save it here."
        )
    send_auth_notification(token_file.name)
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    return token_file


def count_videos_on_date(service, local_date: date, timezone_name: str) -> int:
    tz = ZoneInfo(timezone_name)
    start = datetime.combine(local_date, time.min, tzinfo=tz)
    end = datetime.combine(local_date, time.max, tzinfo=tz)
    response = service.search().list(
        part="id",
        forMine=True,
        type="video",
        maxResults=50,
        publishedAfter=start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        publishedBefore=end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    ).execute()
    return int(response.get("pageInfo", {}).get("totalResults", 0))


def _parse_iso8601_duration_seconds(value: str) -> int:
    match = re.fullmatch(
        r"PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
        value or "",
    )
    if not match:
        return 0
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return hours * 3600 + minutes * 60 + seconds


def _scheduled_video_kind(item: dict) -> str:
    title = str(item.get("snippet", {}).get("title") or "")
    if "#shorts" in title.lower():
        return "short"
    duration_seconds = _parse_iso8601_duration_seconds(str(item.get("contentDetails", {}).get("duration") or ""))
    if 0 < duration_seconds <= 70:
        return "short"
    return "normal"


def list_scheduled_publish_times(
    service,
    *,
    now: datetime | None = None,
    max_results: int = 200,
    scheduled_kind: str = "any",
) -> set[str]:
    current = now or datetime.now(timezone.utc)
    scheduled: set[str] = set()

    channels_response = service.channels().list(part="contentDetails", mine=True).execute()
    items = channels_response.get("items") or []
    if not items:
        return scheduled
    uploads_playlist_id = (
        items[0].get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads")
    )
    if not uploads_playlist_id:
        return scheduled

    video_ids: list[str] = []
    page_token: str | None = None
    remaining = max_results
    while remaining > 0:
        response = service.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=min(50, remaining),
            pageToken=page_token,
        ).execute()
        for item in response.get("items", []):
            video_id = item.get("contentDetails", {}).get("videoId")
            if video_id:
                video_ids.append(video_id)
        remaining -= len(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    for start in range(0, len(video_ids), 50):
        chunk = video_ids[start : start + 50]
        if not chunk:
            continue
        response = service.videos().list(part="status,snippet,contentDetails", id=",".join(chunk)).execute()
        for item in response.get("items", []):
            publish_at = item.get("status", {}).get("publishAt")
            if not publish_at:
                continue
            item_kind = _scheduled_video_kind(item)
            if scheduled_kind != "any" and item_kind != scheduled_kind:
                continue
            try:
                publish_dt = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if publish_dt > current:
                scheduled.add(publish_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))

    return scheduled


def list_existing_video_ids(service, video_ids: set[str]) -> set[str]:
    existing: set[str] = set()
    ids = [video_id for video_id in sorted(video_ids) if video_id]
    for start in range(0, len(ids), 50):
        chunk = ids[start : start + 50]
        if not chunk:
            continue
        response = service.videos().list(part="id", id=",".join(chunk)).execute()
        for item in response.get("items", []):
            video_id = item.get("id")
            if video_id:
                existing.add(str(video_id))
    return existing


def upload_video(
    service,
    video_path: Path,
    metadata: VideoMetadata,
    privacy_status: str,
    publish_at: str | None = None,
    progress_callback: UploadProgressCallback | None = None,
) -> str:
    from .media_qa import validate_media_for_upload

    validate_media_for_upload(video_path)
    is_short = thumbnail_kind(video_path) == "short"
    if not is_short:
        metadata = ensure_auto_thumbnail(video_path, metadata)
    status = {
        "privacyStatus": privacy_status,
        "selfDeclaredMadeForKids": metadata.made_for_kids,
    }
    if publish_at:
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at

    body = {
        "snippet": {
            "title": metadata.title,
            "description": metadata.description,
            "tags": metadata.tags,
            "categoryId": metadata.category_id,
        },
        "status": status,
    }

    media = MediaFileUpload(str(video_path), chunksize=UPLOAD_CHUNK_SIZE, resumable=True)
    request = service.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    retry = 0
    total_bytes = max(video_path.stat().st_size, 1)
    sent_bytes = 0
    if progress_callback:
        progress_callback(0, total_bytes, "starting")
    while response is None:
        try:
            status, response = request.next_chunk()
            retry = 0
            if status and progress_callback:
                sent_bytes = int(getattr(status, "resumable_progress", sent_bytes))
                progress_callback(sent_bytes, total_bytes, "uploading")
        except Exception as exc:
            if not is_retryable_upload_error(exc) or retry >= MAX_UPLOAD_RETRIES:
                raise
            retry += 1
            delay = min(2**retry, 60)
            if progress_callback:
                progress_callback(sent_bytes, total_bytes, f"retry {retry}/{MAX_UPLOAD_RETRIES} in {delay}s")
            safe_console_print(f"Upload interrupted ({exc}); retry {retry}/{MAX_UPLOAD_RETRIES} in {delay}s.")
            time_module.sleep(delay)

    if progress_callback:
        progress_callback(total_bytes, total_bytes, "processing")
    video_id = response["id"]
    if metadata.thumbnail_path and not is_short:
        set_thumbnail(service, video_id, metadata.thumbnail_path)
    return video_id


def set_thumbnail(service, video_id: str, thumbnail_path: Path, raise_errors: bool = False) -> bool:
    thumbnail_path = prepare_thumbnail(thumbnail_path)
    if thumbnail_path.stat().st_size > MAX_THUMBNAIL_BYTES:
        message = f"Thumbnail is still over 2MB after compression: {thumbnail_path.name}"
        if raise_errors:
            raise ValueError(message)
        safe_console_print(message)
        return False

    media = MediaFileUpload(str(thumbnail_path))
    try:
        execute_with_retries(service.thumbnails().set(videoId=video_id, media_body=media).execute)
    except Exception as exc:  # noqa: BLE001 - do not fail an already uploaded video.
        if raise_errors:
            raise
        safe_console_print(f"Thumbnail failed for {video_id}: {exc}")
        return False
    return True


def execute_with_retries(callable_execute):
    retry = 0
    while True:
        try:
            return callable_execute()
        except Exception as exc:
            if not is_retryable_upload_error(exc) or retry >= MAX_UPLOAD_RETRIES:
                raise
            retry += 1
            delay = min(2**retry, 60)
            safe_console_print(f"YouTube request interrupted ({exc}); retry {retry}/{MAX_UPLOAD_RETRIES} in {delay}s.")
            time_module.sleep(delay)


def is_retryable_upload_error(exc: Exception) -> bool:
    if isinstance(exc, HttpError):
        return exc.resp.status in RETRYABLE_HTTP_STATUSES
    return isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            OSError,
            socket.timeout,
            socket.gaierror,
            ssl.SSLError,
            httplib2.ServerNotFoundError,
        ),
    )


def prepare_thumbnail(thumbnail_path: Path) -> Path:
    if thumbnail_path.stat().st_size <= MAX_THUMBNAIL_BYTES:
        return thumbnail_path

    compressed_path = thumbnail_path.with_name(f"{thumbnail_path.stem}.youtube.jpg")
    for quality in [4, 6, 8, 10, 12, 15]:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(thumbnail_path),
            "-vf",
            "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "-frames:v",
            "1",
            "-q:v",
            str(quality),
            str(compressed_path),
        ]
        subprocess.run(command, check=False)
        if compressed_path.exists() and compressed_path.stat().st_size <= MAX_THUMBNAIL_BYTES:
            return compressed_path

    return compressed_path if compressed_path.exists() else thumbnail_path
