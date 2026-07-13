from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from googleapiclient.errors import HttpError

from .accounts import account_token_path, get_active_account_id
from .youtube import get_youtube_analytics_service
from .youtube_reporting import reporting_windows, sync_youtube_reporting


LogFn = Callable[[str], None]

BASE_METRICS = [
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "subscribersGained",
    "likes",
    "comments",
    "shares",
]
def sync_youtube_analytics(
    *,
    root: Path,
    config,
    days: int = 90,
    limit: int = 120,
    log: LogFn | None = None,
) -> dict[str, str]:
    account_id = get_active_account_id(config)
    videos = uploaded_draft_videos(root, account_id=account_id)
    if not videos:
        raise ValueError(f"No uploaded drafts found for {account_id}.")
    videos = videos[:limit]

    service = get_youtube_analytics_service(config.paths["credentials_file"], account_token_path(config))
    analytics_dir = root / "data" / "research" / "youtube" / "analytics"
    analytics_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).date()
    end_date = today - timedelta(days=1)
    if log:
        log(f"Syncing Analytics for {len(videos)} video(s), account={account_id}, through {end_date.isoformat()}.")

    reporting_index: dict[str, Any] = {}
    reporting_error = ""
    try:
        reporting_index = sync_youtube_reporting(root=root, config=config, log=log)
    except Exception as exc:  # Reporting API may not be enabled yet; targeted analytics must still work.
        reporting_error = str(exc)
        if log:
            log(f"YouTube Reporting sync unavailable: {exc}")

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, video in enumerate(videos, start=1):
        video_id = str(video.get("youtube_id") or "").strip()
        publish_date = parse_date(str(video.get("publish_at") or video.get("created_at") or ""))
        start_date = max(end_date - timedelta(days=max(1, days)), publish_date or date(2005, 1, 1))
        if start_date > end_date:
            start_date = end_date
        try:
            metrics = query_video_metrics(
                service=service,
                video_id=video_id,
                start_date=start_date,
                end_date=end_date,
            )
            rows.append(
                {
                    **video,
                    "analytics": metrics,
                    "reporting_windows": reporting_windows(
                        reporting_index,
                        video_id,
                        str(video.get("publish_at") or video.get("created_at") or ""),
                    ),
                    "analytics_start_date": start_date.isoformat(),
                    "analytics_end_date": end_date.isoformat(),
                    "synced_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            if log and (index == 1 or index % 10 == 0 or index == len(videos)):
                log(f"Analytics synced {index}/{len(videos)}.")
        except HttpError as exc:
            if is_api_disabled_error(exc):
                raise ValueError(
                    "YouTube Analytics API is disabled for this Google Cloud project. "
                    "Open the enable link from the Google error, enable YouTube Analytics API, "
                    "wait a few minutes, then run Sync Analytics again."
                ) from exc
            failures.append({"youtube_id": video_id, "title": str(video.get("title") or ""), "error": str(exc)})
            if log:
                log(f"Analytics skip {video_id}: {exc}")
        except Exception as exc:  # noqa: BLE001 - keep syncing other videos.
            failures.append({"youtube_id": video_id, "title": str(video.get("title") or ""), "error": str(exc)})
            if log:
                log(f"Analytics skip {video_id}: {exc}")

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "account_id": account_id,
        "days": days,
        "requested_count": len(videos),
        "synced_count": len(rows),
        "failed_count": len(failures),
        "failures": failures[:30],
        "reporting_error": reporting_error,
        "reporting_created_jobs": reporting_index.get("created_jobs") or [],
        "reporting_downloaded_reports": reporting_index.get("downloaded_reports") or 0,
        "videos": rows,
    }
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = analytics_dir / f"{stamp}-{account_id}-analytics.json"
    report_path = analytics_dir / f"{stamp}-{account_id}-analytics.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(render_analytics_report(payload), encoding="utf-8")
    return {"json_path": str(json_path), "report_path": str(report_path)}


def uploaded_draft_videos(root: Path, account_id: str) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    for path in (root / "data" / "input").rglob("*.json"):
        if "draft" not in str(path.parent).lower():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        youtube_id = str(data.get("youtube_id") or "").strip()
        if not youtube_id:
            continue
        upload_account = str(data.get("upload_account") or "").strip()
        if upload_account and upload_account != account_id:
            continue
        videos.append(
            {
                "id": data.get("id") or path.stem,
                "youtube_id": youtube_id,
                "youtube_url": data.get("youtube_url") or f"https://www.youtube.com/watch?v={youtube_id}",
                "title": data.get("title") or data.get("prompt") or path.stem,
                "mode": data.get("mode") or infer_mode(path),
                "status": data.get("status") or "",
                "publish_at": data.get("publish_at") or "",
                "created_at": data.get("created_at") or "",
                "path": str(path),
            }
        )
    videos.sort(key=lambda item: str(item.get("publish_at") or item.get("created_at") or ""), reverse=True)
    return videos


def query_video_metrics(service, video_id: str, start_date: date, end_date: date) -> dict[str, Any]:
    return query_metrics(service, video_id, start_date, end_date, BASE_METRICS)


def query_metrics(service, video_id: str, start_date: date, end_date: date, metrics: list[str]) -> dict[str, Any]:
    response = service.reports().query(
        ids="channel==MINE",
        startDate=start_date.isoformat(),
        endDate=end_date.isoformat(),
        metrics=",".join(metrics),
        filters=f"video=={video_id}",
    ).execute()
    headers = [header.get("name") for header in response.get("columnHeaders", [])]
    row = (response.get("rows") or [[None] * len(headers)])[0]
    return {str(name): row[index] if index < len(row) else None for index, name in enumerate(headers)}


def is_api_disabled_error(exc: HttpError) -> bool:
    text = str(exc)
    return "accessNotConfigured" in text or "has not been used" in text or "is disabled" in text


def parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(value[:8], "%Y%m%d").date()
    except ValueError:
        return None


def infer_mode(path: Path) -> str:
    text = str(path).lower()
    if "20min" in text or "twenty" in text:
        return "twenty-min"
    if "long" in text:
        return "long"
    if "short" in text:
        return "short"
    return "draft"


def render_analytics_report(data: dict[str, Any]) -> str:
    lines = [
        "# YouTube Analytics Sync",
        "",
        f"Created: {data.get('created_at', '')}",
        f"Account: {data.get('account_id', '')}",
        f"Synced: {data.get('synced_count', 0)}/{data.get('requested_count', 0)}",
        f"Failed: {data.get('failed_count', 0)}",
        "",
        "## Videos",
    ]
    videos = sorted(
        data.get("videos") or [],
        key=lambda item: int((item.get("analytics") or {}).get("views") or 0),
        reverse=True,
    )
    for item in videos[:60]:
        analytics = item.get("analytics") or {}
        views = analytics.get("views") or 0
        avg_pct = analytics.get("averageViewPercentage")
        avg_duration = analytics.get("averageViewDuration")
        ctr = analytics.get("impressionsClickThroughRate")
        ctr_text = f", CTR {ctr}" if ctr is not None else ""
        lines.append(
            f"- {views} views, avg {avg_duration}s, {avg_pct}%{ctr_text} | "
            f"{item.get('mode')} | {item.get('title')} | {item.get('youtube_url')}"
        )
    if data.get("failures"):
        lines.extend(["", "## Failures"])
        for failure in data.get("failures") or []:
            lines.append(f"- {failure.get('youtube_id')}: {failure.get('error')}")
    lines.append("")
    return "\n".join(lines)
