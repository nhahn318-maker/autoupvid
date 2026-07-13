from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NicheProfile:
    id: str
    prompt: str
    settings: dict[str, Any] = field(default_factory=dict)


def sleep_story_profile(settings: dict[str, Any] | None = None) -> NicheProfile:
    merged = dict(settings or {})
    return NicheProfile(
        id="sleep_story",
        prompt=(
            "Write a slow, gentle bedtime story for adults. The story should feel calm, cinematic, "
            "safe, and slightly magical, with soft emotional healing. Avoid horror, conflict, loud "
            "twists, and urgency. End with a peaceful image that helps the listener fall asleep."
        ),
        settings=merged,
    )
