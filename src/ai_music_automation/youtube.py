from __future__ import annotations

import subprocess
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .metadata import VideoMetadata


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024


def get_youtube_service(credentials_file: Path, token_file: Path):
    credentials = None
    if token_file.exists():
        credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        if not credentials.has_scopes(SCOPES):
            credentials = None

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        if not credentials_file.exists():
            raise FileNotFoundError(
                f"Missing {credentials_file}. Download OAuth client JSON from Google Cloud and save it here."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        credentials = flow.run_local_server(port=0)

    token_file.write_text(credentials.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=credentials)


def create_token(credentials_file: Path, token_file: Path) -> Path:
    if not credentials_file.exists():
        raise FileNotFoundError(
            f"Missing {credentials_file}. Download OAuth client JSON from Google Cloud and save it here."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
    credentials = flow.run_local_server(port=0)
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

    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)
    request = service.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response["id"]
    if metadata.thumbnail_path:
        set_thumbnail(service, video_id, metadata.thumbnail_path)
    return video_id


def set_thumbnail(service, video_id: str, thumbnail_path: Path) -> None:
    thumbnail_path = prepare_thumbnail(thumbnail_path)
    if thumbnail_path.stat().st_size > MAX_THUMBNAIL_BYTES:
        print(f"Skip thumbnail over 2MB: {thumbnail_path.name}")
        return

    media = MediaFileUpload(str(thumbnail_path))
    try:
        service.thumbnails().set(videoId=video_id, media_body=media).execute()
    except Exception as exc:  # noqa: BLE001 - do not fail an already uploaded video.
        print(f"Thumbnail failed for {video_id}: {exc}")


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
