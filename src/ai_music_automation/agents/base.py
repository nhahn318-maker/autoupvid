from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

from ..automation.cache import AutomationCache
from ..automation.logging import AutomationLogger


InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class AgentCallable(Protocol[InputT, OutputT]):
    def __call__(self, payload: InputT, context: "AgentContext") -> OutputT:
        ...


@dataclass
class AgentContext:
    niche: str
    settings: dict[str, Any]
    logger: AutomationLogger | None = None
    cache: AutomationCache | None = None
    run_id: str = ""


@dataclass(frozen=True)
class AgentResult(Generic[OutputT]):
    output: OutputT
    score: float | None = None
    notes: list[str] = field(default_factory=list)
    cached: bool = False


class BaseAgent(Generic[InputT, OutputT]):
    """Base class with logging, retry, and optional JSON cache hooks."""

    name = "agent"

    def __init__(self, max_retries: int = 0) -> None:
        self.max_retries = max(0, int(max_retries or 0))

    def run(self, payload: InputT, context: AgentContext) -> AgentResult[OutputT]:
        attempts = self.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                if context.logger:
                    with context.logger.stage(self.name, attempt=attempt):
                        return AgentResult(output=self.execute(payload, context))
                return AgentResult(output=self.execute(payload, context))
            except Exception as exc:
                last_error = exc
                if context.logger:
                    context.logger.event(self.name, "retry", attempt=attempt, error=str(exc))
                if attempt >= attempts:
                    raise
        raise RuntimeError(f"{self.name} failed") from last_error

    def execute(self, payload: InputT, context: AgentContext) -> OutputT:
        raise NotImplementedError
