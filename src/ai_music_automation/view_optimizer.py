from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


STOPWORDS = {
    "and",
    "are",
    "ban",
    "cac",
    "cho",
    "cua",
    "duoc",
    "hay",
    "khi",
    "mot",
    "nguoi",
    "nhung",
    "phat",
    "that",
    "the",
    "this",
    "trong",
    "video",
    "voi",
    "you",
    "your",
}

POWER_WORDS = {
    "binh an",
    "buong bo",
    "chua lanh",
    "dau kho",
    "giac ngo",
    "healing",
    "let go",
    "overthinking",
    "peace",
    "sleep",
    "tam tri",
    "thuc tinh",
}


def generate_view_optimizer_report(root: Path, limit: int = 80) -> dict[str, str]:
    research_dir = root / "data" / "research" / "youtube"
    research_dir.mkdir(parents=True, exist_ok=True)

    research_records = load_research_records(research_dir)
    analytics_by_video = load_analytics_cache(research_dir / "analytics")
    local_drafts = load_local_drafts(root)
    top_records = sorted(research_records, key=lambda item: int(item.get("views") or 0), reverse=True)[:limit]
    keyword_counter = build_keyword_counter(top_records)
    keywords = [{"keyword": key, "count": count} for key, count in keyword_counter.most_common(30)]
    local_scores = [score_local_draft(draft, keyword_counter, analytics_by_video) for draft in local_drafts]
    local_scores.sort(key=lambda item: (item["score"], item["title"]))
    recommendations = build_recommendations(top_records, local_scores, keyword_counter)

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "research_record_count": len(research_records),
        "analytics_video_count": len(analytics_by_video),
        "local_draft_count": len(local_drafts),
        "top_research_videos": top_records[:25],
        "keywords": keywords,
        "local_title_scores": local_scores[:80],
        "recommendations": recommendations,
    }

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = research_dir / f"{stamp}-view-optimizer.json"
    report_path = research_dir / f"{stamp}-view-optimizer.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(render_view_optimizer_report(payload), encoding="utf-8")
    return {"json_path": str(json_path), "report_path": str(report_path)}


def load_research_records(research_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(research_dir.glob("*.json"), reverse=True):
        if path.name.endswith("-view-optimizer.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        for record in data.get("records") or []:
            title = repair_text(str(record.get("title") or "")).strip()
            if not title:
                continue
            records.append(
                {
                    "title": title,
                    "views": int(record.get("view_count") or record.get("views") or 0),
                    "duration": record.get("duration") or 0,
                    "upload_date": record.get("upload_date") or "",
                    "url": record.get("webpage_url") or record.get("url") or "",
                    "hook": repair_text(str(record.get("transcript_hook") or "")).strip(),
                }
            )
    return dedupe_records(records)


def load_local_drafts(root: Path) -> list[dict[str, Any]]:
    drafts: list[dict[str, Any]] = []
    input_root = root / "data" / "input"
    if not input_root.exists():
        return drafts
    for path in input_root.rglob("*.json"):
        if "draft" not in str(path.parent).lower():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        title = repair_text(str(data.get("title") or data.get("prompt") or "")).strip()
        if not title:
            continue
        drafts.append(
            {
                "id": data.get("id") or path.stem,
                "title": title,
                "mode": data.get("mode") or infer_mode(path),
                "status": data.get("status") or "",
                "youtube_id": data.get("youtube_id") or "",
                "youtube_url": data.get("youtube_url") or "",
                "publish_at": data.get("publish_at") or "",
                "path": str(path),
            }
        )
    return drafts


def load_analytics_cache(analytics_dir: Path) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    if not analytics_dir.exists():
        return output
    for path in sorted(analytics_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in data.get("videos") or []:
            video_id = str(item.get("youtube_id") or "").strip()
            analytics = item.get("analytics")
            if video_id and isinstance(analytics, dict):
                windows = item.get("reporting_windows") or {}
                reach = windows.get("metrics_7d") or windows.get("metrics_72h") or windows.get("metrics_24h") or {}
                output[video_id] = {
                    **analytics,
                    **reach,
                    "reporting_windows": windows,
                    "synced_at": item.get("synced_at") or data.get("created_at") or "",
                    "analytics_start_date": item.get("analytics_start_date") or "",
                    "analytics_end_date": item.get("analytics_end_date") or "",
                }
    return output


def repair_text(value: str) -> str:
    if not value:
        return ""
    markers = ("Ã", "Ä", "áº", "á»", "â€", "ðŸ")
    text = value
    for _ in range(2):
        if not any(marker in text for marker in markers):
            break
        repaired = ""
        for encoding in ("cp1252", "latin1"):
            try:
                repaired = text.encode(encoding, errors="ignore").decode("utf-8", errors="ignore")
            except UnicodeError:
                continue
            if repaired and repaired != text:
                break
        if not repaired or repaired == text:
            break
        text = repaired
    return text


def dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for record in records:
        key = str(record.get("url") or record.get("title") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(record)
    return output


def infer_mode(path: Path) -> str:
    text = str(path).lower()
    if "20min" in text or "twenty" in text:
        return "twenty-min"
    if "long" in text:
        return "long"
    if "short" in text:
        return "short"
    return "draft"


def build_keyword_counter(records: list[dict[str, Any]]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for record in records:
        weight = 1 + min(10, int(record.get("views") or 0) // 1000)
        for token in tokenize(str(record.get("title") or "")):
            if token not in STOPWORDS:
                counter[token] += weight
    return counter


def tokenize(value: str) -> list[str]:
    normalized = value.lower()
    normalized = re.sub(r"https?://\S+", " ", normalized)
    return [token for token in re.findall(r"[\w]+", normalized, flags=re.UNICODE) if len(token) >= 3]


def score_local_draft(
    draft: dict[str, Any],
    keyword_counter: Counter[str],
    analytics_by_video: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    title = str(draft.get("title") or "")
    lower = title.lower()
    score = 0
    notes: list[str] = []
    title_len = len(title)

    if 45 <= title_len <= 95:
        score += 25
    elif title_len < 30:
        notes.append("Title is too short; add a clear emotional promise.")
    elif title_len > 105:
        notes.append("Title is long; trim the slow part before upload.")
    else:
        score += 12

    matched_keywords = [token for token in tokenize(title) if keyword_counter.get(token, 0) > 0]
    score += min(25, len(set(matched_keywords)) * 5)
    if not matched_keywords:
        notes.append("No overlap with researched winning keywords yet.")

    if any(word in lower for word in POWER_WORDS):
        score += 20
    else:
        notes.append("Add one strong viewer intent: binh an, buong bo, healing, sleep, overthinking.")

    if "?" in title or re.search(r"\b\d+\b", title):
        score += 10
    else:
        notes.append("Try a question or concrete number when natural.")

    if any(word in lower for word in ("khi ", "vi sao", "ban ", "before sleep", "let go", "stop ")):
        score += 15
    else:
        notes.append("Opening phrase could be sharper in the first 4-6 words.")

    analytics = (analytics_by_video or {}).get(str(draft.get("youtube_id") or ""))
    if analytics:
        avg_pct = as_float(analytics.get("averageViewPercentage"))
        ctr = as_float(
            analytics.get("video_thumbnail_impressions_ctr")
            if analytics.get("video_thumbnail_impressions_ctr") is not None
            else analytics.get("impressionsClickThroughRate")
        )
        if ctr is not None and 0 <= ctr <= 1:
            ctr *= 100
        views = as_float(analytics.get("views"))
        if avg_pct is not None:
            if avg_pct >= 35:
                score += 10
            elif avg_pct < 18:
                notes.append("Retention is weak; improve first minute pacing and promise delivery.")
        if ctr is not None:
            if ctr >= 6:
                score += 10
            elif ctr < 3:
                notes.append("CTR is weak; rewrite title/thumbnail pair.")
        if views is not None and views < 100:
            notes.append("Low views; compare topic and packaging with winning keywords.")

    score = max(0, min(100, score))
    return {
        **draft,
        "score": score,
        "analytics": analytics or {},
        "matched_keywords": sorted(set(matched_keywords))[:8],
        "notes": notes[:4],
    }


def as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_recommendations(
    top_records: list[dict[str, Any]],
    local_scores: list[dict[str, Any]],
    keyword_counter: Counter[str],
) -> list[str]:
    recommendations = [
        "Use the research report as input before generating fullauto prompts; the strongest titles should influence the next prompt variations.",
        "Keep thumbnails simple: 2-4 big words, one clear face/statue/object, high contrast, no tiny paragraph text.",
        "For 20-min and long videos, prioritize titles that name the viewer problem in the first 4-6 words.",
    ]
    if keyword_counter:
        top_terms = ", ".join(key for key, _ in keyword_counter.most_common(8))
        recommendations.append(f"Current winning keyword cluster: {top_terms}.")
    weak_count = sum(1 for item in local_scores if int(item.get("score") or 0) < 55)
    if weak_count:
        recommendations.append(f"{weak_count} local draft title(s) score below 55 and should be rewritten before reuse.")
    if not top_records:
        recommendations.append("Run Channel Research on 3-5 competitor channels first; optimizer will become more accurate.")
    return recommendations


def render_view_optimizer_report(data: dict[str, Any]) -> str:
    lines = [
        "# YouTube View Optimizer",
        "",
        f"Created: {data.get('created_at', '')}",
        f"Research records: {data.get('research_record_count', 0)}",
        f"Analytics videos: {data.get('analytics_video_count', 0)}",
        f"Local drafts: {data.get('local_draft_count', 0)}",
        "",
        "## Recommendations",
    ]
    for item in data.get("recommendations") or []:
        lines.append(f"- {item}")

    lines.extend(["", "## Winning Keywords"])
    for item in (data.get("keywords") or [])[:20]:
        lines.append(f"- {item.get('keyword')}: {item.get('count')}")

    lines.extend(["", "## Top Research Videos"])
    for record in (data.get("top_research_videos") or [])[:15]:
        title = record.get("title") or ""
        views = record.get("views") or 0
        url = record.get("url") or ""
        lines.append(f"- {views} views | {title} | {url}")
        if record.get("hook"):
            lines.append(f"  Hook: {str(record.get('hook'))[:220]}")

    lines.extend(["", "## Local Draft Title Scores"])
    for item in (data.get("local_title_scores") or [])[:40]:
        analytics = item.get("analytics") or {}
        metrics = ""
        if analytics:
            metrics = (
                f" | views {analytics.get('views', 0)}, "
                f"avg% {analytics.get('averageViewPercentage', '')}, "
                f"CTR {analytics.get('video_thumbnail_impressions_ctr', analytics.get('impressionsClickThroughRate', ''))}"
            )
        lines.append(f"- {item.get('score')}/100 | {item.get('mode')}{metrics} | {item.get('title')}")
        notes = item.get("notes") or []
        if notes:
            lines.append(f"  Fix: {'; '.join(notes)}")
    lines.append("")
    return "\n".join(lines)
