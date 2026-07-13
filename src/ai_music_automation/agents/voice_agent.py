from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import AgentContext, BaseAgent
from ..automation.artifacts import StoryArtifact, VoiceArtifact


@dataclass(frozen=True)
class VoiceAgentInput:
    story: StoryArtifact
    output_dir: Path
    voice: str
    rate: str = "+0%"


class VoiceAgent(BaseAgent[VoiceAgentInput, VoiceArtifact]):
    name = "voice_agent"

    def execute(self, payload: VoiceAgentInput, context: AgentContext) -> VoiceArtifact:
        from ..tts import generate_voice

        output = generate_voice(payload.story.script, f"story-before-sleep-{payload.story.title}", payload.voice, payload.output_dir, rate=payload.rate)
        return VoiceArtifact(
            path=output,
            voice=payload.voice,
            rate=payload.rate,
            transcript_path=output.with_suffix(".txt"),
            subtitle_path=output.with_suffix(".auto.srt"),
        )
