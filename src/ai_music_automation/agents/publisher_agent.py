from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import AgentContext, BaseAgent
from ..automation.artifacts import MetadataArtifact


@dataclass(frozen=True)
class PublishInput:
    video_path: Path
    metadata: MetadataArtifact
    service: object | None = None
    privacy_status: str = "private"
    publish_at: str | None = None
    enabled: bool = False


@dataclass(frozen=True)
class PublishResult:
    uploaded: bool
    youtube_id: str = ""
    youtube_url: str = ""
    skipped_reason: str = ""


class PublisherAgent(BaseAgent[PublishInput, PublishResult]):
    name = "publisher_agent"

    def execute(self, payload: PublishInput, context: AgentContext) -> PublishResult:
        if not payload.enabled:
            return PublishResult(uploaded=False, skipped_reason="Publishing disabled.")
        if payload.service is None:
            return PublishResult(uploaded=False, skipped_reason="YouTube service is missing.")
        from ..youtube import upload_video

        video_id = upload_video(
            service=payload.service,
            video_path=payload.video_path,
            metadata=payload.metadata,
            privacy_status=payload.privacy_status,
            publish_at=payload.publish_at,
        )
        return PublishResult(uploaded=True, youtube_id=video_id, youtube_url=f"https://www.youtube.com/watch?v={video_id}")
