from __future__ import annotations

import subprocess
import time as time_module
import socket
import ssl
from collections.abc import Callable
from datetime import date, datetime, time, timezone
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

from .metadata import VideoMetadata


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
MAX_UPLOAD_RETRIES = 8
RETRYABLE_HTTP_STATUSES = {500, 502, 503, 504}
YOUTUBE_HTTP_TIMEOUT = 120
UploadProgressCallback = Callable[[int, int, str], None]


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


def send_auth_notification(token_file_name: str):
    config_path = Path("config.json")
    if not config_path.exists():
        return
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        notify = config.get("notifications")
        if not notify or not notify.get("email_enabled", False):
            return
        
        smtp_host = notify.get("smtp_host", "smtp.gmail.com")
        smtp_port = int(notify.get("smtp_port", 587))
        smtp_username = notify.get("smtp_username")
        smtp_password = notify.get("smtp_password")
        to_email = notify.get("to_email")
        from_email = notify.get("from_email") or smtp_username

        if not (smtp_username and smtp_password and to_email):
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

        message = EmailMessage()
        message["From"] = from_email
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
        print(f"Da gui email thong bao yeu cau cap quyen toi {to_email} cho {account_display}")
    except Exception as exc:
        print(f"Khong the gui email thong bao yeu cau cap quyen: {exc}")


def get_youtube_service(credentials_file: Path, token_file: Path):
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
    raw_http = httplib2.Http(timeout=YOUTUBE_HTTP_TIMEOUT)
    # Google uses 308 as the resumable-upload acknowledgement, not a redirect.
    raw_http.redirect_codes = raw_http.redirect_codes - {308}
    http = AuthorizedHttp(credentials, http=raw_http)
    return build("youtube", "v3", http=http, cache_discovery=False)


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


def upload_video(
    service,
    video_path: Path,
    metadata: VideoMetadata,
    privacy_status: str,
    publish_at: str | None = None,
    progress_callback: UploadProgressCallback | None = None,
) -> str:
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
            print(f"Upload interrupted ({exc}); retry {retry}/{MAX_UPLOAD_RETRIES} in {delay}s.")
            time_module.sleep(delay)

    if progress_callback:
        progress_callback(total_bytes, total_bytes, "processing")
    video_id = response["id"]
    if metadata.thumbnail_path:
        set_thumbnail(service, video_id, metadata.thumbnail_path)
    return video_id


def set_thumbnail(service, video_id: str, thumbnail_path: Path, raise_errors: bool = False) -> bool:
    thumbnail_path = prepare_thumbnail(thumbnail_path)
    if thumbnail_path.stat().st_size > MAX_THUMBNAIL_BYTES:
        message = f"Thumbnail is still over 2MB after compression: {thumbnail_path.name}"
        if raise_errors:
            raise ValueError(message)
        print(message)
        return False

    media = MediaFileUpload(str(thumbnail_path))
    try:
        execute_with_retries(service.thumbnails().set(videoId=video_id, media_body=media).execute)
    except Exception as exc:  # noqa: BLE001 - do not fail an already uploaded video.
        if raise_errors:
            raise
        print(f"Thumbnail failed for {video_id}: {exc}")
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
            print(f"YouTube request interrupted ({exc}); retry {retry}/{MAX_UPLOAD_RETRIES} in {delay}s.")
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
