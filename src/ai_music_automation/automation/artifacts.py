from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AutomationArtifact:
    """Base artifact produced by an automation stage."""

    id: str
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StoryArtifact:
    title: str
    prompt: str
    script: str
    outline: str = ""
    hook: str = ""
    ending: str = ""
    lesson: str = ""
    score: float | None = None
    review_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SceneArtifact:
    index: int
    label: str
    summary: str
    emotion: str = ""
    image_prompt: str = ""
    source_text: str = ""


@dataclass(frozen=True)
class ImageArtifact:
    scene_index: int
    path: Path
    prompt: str = ""
    score: float | None = None
    reviewer: str = ""


@dataclass(frozen=True)
class VoiceArtifact:
    path: Path
    voice: str
    rate: str = "+0%"
    transcript_path: Path | None = None
    subtitle_path: Path | None = None


@dataclass(frozen=True)
class MetadataArtifact:
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    thumbnail_prompt: str = ""
    thumbnail_path: Path | None = None


@dataclass(frozen=True)
class PipelineArtifacts:
    niche: str
    story: StoryArtifact | None = None
    scenes: list[SceneArtifact] = field(default_factory=list)
    images: list[ImageArtifact] = field(default_factory=list)
    voice: VoiceArtifact | None = None
    metadata: MetadataArtifact | None = None
    video_path: Path | None = None
    draft_json: Path | None = None
    draft_markdown: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)
