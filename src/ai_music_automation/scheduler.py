from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def next_publish_times(
    count: int,
    configured_times: list[str],
    timezone_name: str,
    now: datetime | None = None,
    blocked_times: set[str] | None = None,
    blocked_dates: set[str] | None = None,
    allowed_weekdays: list[int] | tuple[int, ...] | set[int] | None = None,
    day_interval: int | None = None,
    interval_anchor_date: str | None = None,
) -> list[datetime]:
    tz = ZoneInfo(timezone_name)
    current = now.astimezone(tz) if now else datetime.now(tz)
    if isinstance(configured_times, str):
        configured_times = [configured_times]
    elif not isinstance(configured_times, (list, tuple, set)):
        configured_times = [configured_times] if configured_times else []
    blocked_times = blocked_times or set()
    blocked_dates = blocked_dates or set()
    normalized_weekdays = normalize_allowed_weekdays(allowed_weekdays)
    normalized_interval = normalize_day_interval(day_interval)
    anchor_date = parse_anchor_date(interval_anchor_date)
    results: list[datetime] = []
    day = current.date()

    while len(results) < count:
        if normalized_weekdays and day.weekday() not in normalized_weekdays:
            day += timedelta(days=1)
            continue
        if normalized_interval and anchor_date and ((day - anchor_date).days % normalized_interval != 0):
            day += timedelta(days=1)
            continue
        for item in configured_times:
            candidate = datetime.combine(day, parse_time(item), tzinfo=tz)
            candidate_key = to_rfc3339_utc(candidate)
            day_key = candidate.date().isoformat()
            if candidate > current and candidate_key not in blocked_times and day_key not in blocked_dates:
                results.append(candidate)
                blocked_times.add(candidate_key)
                if len(results) == count:
                    break
        day += timedelta(days=1)

    return results


def parse_time(value: str) -> time:
    hour, minute = value.split(":", maxsplit=1)
    return time(hour=int(hour), minute=int(minute))


def to_rfc3339_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_allowed_weekdays(value) -> set[int]:
    if value is None:
        return set()
    if isinstance(value, int):
        return {value} if 0 <= value <= 6 else set()
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {int(item) for item in value if isinstance(item, int) and 0 <= int(item) <= 6}


def normalize_day_interval(value) -> int | None:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return None
    return interval if interval >= 2 else None


def parse_anchor_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
