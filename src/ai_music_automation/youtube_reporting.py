from __future__ import annotations

import csv
import json
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .accounts import account_token_path, get_active_account_id
from .youtube import get_youtube_reporting_service


REPORT_TYPES = {
    "reach": "channel_reach_basic_a1",
    "activity": "channel_basic_a3",
}


def sync_youtube_reporting(*, root: Path, config, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    account_id = get_active_account_id(config)
    service, credentials = get_youtube_reporting_service(
        config.paths["credentials_file"], account_token_path(config)
    )
    base = root / "data" / "research" / "youtube" / "reporting" / account_id
    base.mkdir(parents=True, exist_ok=True)
    state_path = base / "state.json"
    state = _read_json(state_path)
    processed = set(state.get("processed_report_ids") or [])

    available = {
        item.get("id") for item in service.reportTypes().list().execute().get("reportTypes", [])
    }
    jobs = {
        item.get("reportTypeId"): item
        for item in service.jobs().list().execute().get("jobs", [])
    }
    created: list[str] = []
    for label, report_type in REPORT_TYPES.items():
        if report_type not in available:
            continue
        if report_type not in jobs:
            job = service.jobs().create(
                body={"reportTypeId": report_type, "name": f"automation-{label}"}
            ).execute()
            jobs[report_type] = job
            created.append(report_type)
            if log:
                log(f"Created YouTube Reporting job: {report_type}; first data may take up to 48 hours.")

    downloaded = 0
    for report_type, job in jobs.items():
        if report_type not in REPORT_TYPES.values():
            continue
        response = service.jobs().reports().list(jobId=job["id"]).execute()
        for report in response.get("reports", []):
            report_id = str(report.get("id") or "")
            if not report_id or report_id in processed or not report.get("downloadUrl"):
                continue
            destination = base / report_type / f"{report_id}.csv"
            destination.parent.mkdir(parents=True, exist_ok=True)
            request = urllib.request.Request(
                report["downloadUrl"], headers={"Authorization": f"Bearer {credentials.token}"}
            )
            with urllib.request.urlopen(request, timeout=180) as response_stream:
                destination.write_bytes(response_stream.read())
            processed.add(report_id)
            downloaded += 1

    daily = aggregate_reporting_csv(base)
    index = {
        "account_id": account_id,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "created_jobs": created,
        "downloaded_reports": downloaded,
        "daily": daily,
    }
    (base / "daily-index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    state.update(
        {
            "account_id": account_id,
            "processed_report_ids": sorted(processed),
            "updated_at": index["updated_at"],
        }
    )
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return index


def aggregate_reporting_csv(base: Path) -> dict[str, dict[str, dict[str, float]]]:
    rows: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for path in base.rglob("*.csv"):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for item in csv.DictReader(handle):
                video_id = str(item.get("video_id") or "").strip()
                day = str(item.get("date") or "").strip()
                if not video_id or not day:
                    continue
                target = rows[(video_id, day)]
                impressions = _number(item.get("video_thumbnail_impressions"))
                ctr = _number(item.get("video_thumbnail_impressions_ctr"))
                if impressions:
                    target["video_thumbnail_impressions"] += impressions
                    target["_ctr_weighted"] += impressions * ctr
                for source, output in (
                    ("views", "views"),
                    ("likes", "likes"),
                    ("comments", "comments"),
                    ("shares", "shares"),
                    ("watch_time_minutes", "watch_time_minutes"),
                    ("subscribers_gained", "subscribers_gained"),
                ):
                    target[output] += _number(item.get(source))
    output: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    for (video_id, day), metrics in rows.items():
        impressions = metrics.get("video_thumbnail_impressions", 0)
        if impressions:
            metrics["video_thumbnail_impressions_ctr"] = metrics.pop("_ctr_weighted", 0) / impressions
        views = metrics.get("views", 0)
        if views and metrics.get("watch_time_minutes"):
            metrics["average_view_duration_seconds"] = metrics["watch_time_minutes"] * 60 / views
        output[video_id][day] = {key: round(value, 4) for key, value in metrics.items() if not key.startswith("_")}
    return dict(output)


def reporting_windows(index: dict[str, Any], video_id: str, publish_date: str) -> dict[str, dict[str, float]]:
    daily = (index.get("daily") or {}).get(video_id) or {}
    days = sorted(day for day in daily if not publish_date or day >= publish_date[:10])
    return {
        "metrics_24h": _sum_days(daily, days[:1]),
        "metrics_72h": _sum_days(daily, days[:3]),
        "metrics_7d": _sum_days(daily, days[:7]),
    }


def _sum_days(daily: dict[str, dict[str, float]], days: list[str]) -> dict[str, float]:
    total: dict[str, float] = defaultdict(float)
    weighted_ctr = 0.0
    for day in days:
        metrics = daily.get(day) or {}
        impressions = _number(metrics.get("video_thumbnail_impressions"))
        weighted_ctr += impressions * _number(metrics.get("video_thumbnail_impressions_ctr"))
        for key, value in metrics.items():
            if key not in {"video_thumbnail_impressions_ctr", "average_view_duration_seconds"}:
                total[key] += _number(value)
    impressions = total.get("video_thumbnail_impressions", 0)
    if impressions:
        total["video_thumbnail_impressions_ctr"] = weighted_ctr / impressions
    views = total.get("views", 0)
    if views and total.get("watch_time_minutes"):
        total["average_view_duration_seconds"] = total["watch_time_minutes"] * 60 / views
    return {key: round(value, 4) for key, value in total.items()}


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
