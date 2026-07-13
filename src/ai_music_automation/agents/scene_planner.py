from __future__ import annotations

import re
from dataclasses import dataclass, field

from .base import AgentContext, BaseAgent
from .emotion_analyzer import EmotionSegment
from .json_utils import extract_json_array
from ..automation.artifacts import SceneArtifact, StoryArtifact
from ..automation.model_client import ModelRequest, OllamaClient


@dataclass(frozen=True)
class ScenePlannerInput:
    story: StoryArtifact
    emotions: list[EmotionSegment] = field(default_factory=list)
    max_scenes: int = 10


class ScenePlannerAgent(BaseAgent[ScenePlannerInput, list[SceneArtifact]]):
    name = "scene_planner"

    def __init__(self, model_client: OllamaClient | None = None, max_retries: int = 0) -> None:
        super().__init__(max_retries=max_retries)
        self.model_client = model_client or OllamaClient()

    def execute(self, payload: ScenePlannerInput, context: AgentContext) -> list[SceneArtifact]:
        max_scenes = max(1, min(32, int(payload.max_scenes or 10)))
        response = self.model_client.generate(
            ModelRequest(
                base_url=str(context.settings.get("ollama_url") or "http://127.0.0.1:11434"),
                model=str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b"),
                prompt=build_scene_prompt(payload, max_scenes),
                temperature=float(context.settings.get("scene_temperature") or 0.35),
                top_p=float(context.settings.get("scene_top_p") or 0.88),
                timeout_seconds=int(context.settings.get("timeout_seconds") or 240),
            )
        )
        scenes = parse_scenes(response)
        if not scenes:
            scenes = heuristic_scenes(payload.story, payload.emotions)
        return scenes[:max_scenes]


def build_scene_prompt(payload: ScenePlannerInput, max_scenes: int) -> str:
    emotion_hint = "\n".join(f"- {item.emotion}: {item.text}" for item in payload.emotions[:max_scenes])
    return f"""Divide this story into visual scenes by content, not by sentence count.

Use meaningful scene labels that come from this exact story, such as Clockmaker Workshop,
Moonlit Bridge, Lost Lantern, Bakery Window, Garden Gate, Morning Shore.
Maximum scenes: {max_scenes}

Each scene must be story-specific. The summary must include:
- the named character or subject visible in the image
- a stable visual character profile when the same character appears: apparent age, hair, outfit colors, carried object
- the character's action or emotional moment
- the exact setting/location
- one important object from the plot
- the time of day, weather, or lighting
- one distinct set piece or background feature that makes this scene visually different from the others

Do not return generic bedtime scenery like only "moon", "forest", "cabin", "dream".
Do not invent a different plot. Use only visual moments that actually happen in the story.
Keep the visual sequence varied: avoid using the same location for every scene unless the story truly stays there.
When the story has a fantasy rule/object, make it visible in the scene summaries through concrete props or actions.

Emotion hints:
{emotion_hint or "None"}

Return only a JSON array:
[
  {{"label": "Rain Library Door", "summary": "named character with stable profile doing a specific action in the exact setting with the key object, distinct set piece, and lighting", "emotion": "dreamy", "source_text": "1-3 matching story sentences"}}
]

Story:
{payload.story.script}
"""


def parse_scenes(response: str) -> list[SceneArtifact]:
    data = extract_json_array(response)
    if not data:
        return []
    output: list[SceneArtifact] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or f"Scene {index}").strip()
        summary = str(item.get("summary") or item.get("visual") or "").strip()
        if not summary:
            continue
        output.append(
            SceneArtifact(
                index=index,
                label=label,
                summary=summary,
                emotion=str(item.get("emotion") or "calm").strip().lower(),
                source_text=str(item.get("source_text") or "").strip(),
            )
        )
    return output


def heuristic_scenes(story: StoryArtifact, emotions: list[EmotionSegment]) -> list[SceneArtifact]:
    source_parts = [item.text for item in emotions] or [
        part.strip()
        for part in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", story.script)
        if part.strip()
    ]
    labels = ["Opening", "Path", "Moon", "Meadow", "Window", "Dream", "Rest"]
    output: list[SceneArtifact] = []
    for index, text in enumerate(source_parts[:10], start=1):
        emotion = emotions[index - 1].emotion if index <= len(emotions) else "calm"
        output.append(
            SceneArtifact(
                index=index,
                label=labels[(index - 1) % len(labels)],
                summary=text[:240],
                emotion=emotion,
                source_text=text,
            )
        )
    return output
