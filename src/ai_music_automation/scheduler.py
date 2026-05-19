from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def next_publish_times(
    count: int,
    configured_times: list[str],
    timezone_name: str,
    now: datetime | None = None,
    blocked_times: set[str] | None = None,
    blocked_dates: set[str] | None = None,
) -> list[datetime]:
    tz = ZoneInfo(timezone_name)
    current = now.astimezone(tz) if now else datetime.now(tz)
    blocked_times = blocked_times or set()
    blocked_dates = blocked_dates or set()
    results: list[datetime] = []
    day = current.date()

    while len(results) < count:
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
