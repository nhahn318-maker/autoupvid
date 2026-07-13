from __future__ import annotations

from dataclasses import dataclass, field

from .base import AgentContext, BaseAgent
from ..scheduler import next_publish_times, to_rfc3339_utc


@dataclass(frozen=True)
class SchedulerInput:
    count: int = 1
    configured_times: list[str] = field(default_factory=list)
    timezone_name: str = "Asia/Ho_Chi_Minh"
    blocked_times: set[str] = field(default_factory=set)


class SchedulerAgent(BaseAgent[SchedulerInput, list[str]]):
    name = "scheduler_agent"

    def execute(self, payload: SchedulerInput, context: AgentContext) -> list[str]:
        times = next_publish_times(
            count=max(1, int(payload.count or 1)),
            configured_times=payload.configured_times or context.settings.get("publish_times") or ["07:30"],
            timezone_name=payload.timezone_name or str(context.settings.get("timezone") or "Asia/Ho_Chi_Minh"),
            blocked_times=set(payload.blocked_times or set()),
        )
        return [to_rfc3339_utc(item) for item in times]
