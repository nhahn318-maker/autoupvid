from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import AgentContext, BaseAgent
from ..automation.artifacts import ImageArtifact, StoryArtifact, VoiceArtifact
from ..media import Track
from ..render import render_video


@dataclass(frozen=True)
class RenderAgentInput:
    story: StoryArtifact
    voice: VoiceArtifact
    images: list[ImageArtifact]
    output_dir: Path
    render_config: dict
    suffix: str = "-sbs-test"


class RenderAgent(BaseAgent[RenderAgentInput, Path]):
    name = "render_agent"

    def execute(self, payload: RenderAgentInput, context: AgentContext) -> Path:
        track = Track(
            audio_path=payload.voice.path,
            image_paths=tuple(image.path for image in payload.images),
            title=payload.story.title,
        )
        return render_video(track, payload.output_dir, payload.render_config, suffix=payload.suffix)
