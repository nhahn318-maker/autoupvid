from __future__ import annotations

import re
from dataclasses import dataclass

from .base import AgentContext, BaseAgent
from .json_utils import extract_json_array
from ..automation.artifacts import StoryArtifact
from ..automation.model_client import ModelRequest, OllamaClient


@dataclass(frozen=True)
class EmotionSegment:
    index: int
    emotion: str
    text: str


class EmotionAnalyzerAgent(BaseAgent[StoryArtifact, list[EmotionSegment]]):
    name = "emotion_analyzer"

    def __init__(self, model_client: OllamaClient | None = None, max_retries: int = 0) -> None:
        super().__init__(max_retries=max_retries)
        self.model_client = model_client or OllamaClient()

    def execute(self, payload: StoryArtifact, context: AgentContext) -> list[EmotionSegment]:
        if not bool(context.settings.get("emotion_use_model", False)):
            return heuristic_emotions(payload.script)
        response = self.model_client.generate(
            ModelRequest(
                base_url=str(context.settings.get("ollama_url") or "http://127.0.0.1:11434"),
                model=str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b"),
                prompt=build_emotion_prompt(payload),
                temperature=float(context.settings.get("emotion_temperature") or 0.2),
                top_p=float(context.settings.get("emotion_top_p") or 0.8),
                timeout_seconds=int(context.settings.get("timeout_seconds") or 240),
            )
        )
        parsed = parse_emotions(response)
        return parsed or heuristic_emotions(payload.script)


def build_emotion_prompt(story: StoryArtifact) -> str:
    return f"""Analyze the emotional movement of this bedtime story.

Return only a JSON array:
[
  {{"emotion": "calm|warm|sad|dreamy|safe|wonder|relief", "text": "short matching passage"}}
]

Script:
{story.script}
"""


def parse_emotions(response: str) -> list[EmotionSegment]:
    data = extract_json_array(response)
    if not data:
        return []
    output: list[EmotionSegment] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        emotion = str(item.get("emotion") or "calm").strip().lower()
        if text:
            output.append(EmotionSegment(index=index, emotion=emotion, text=text))
    return output


def heuristic_emotions(script: str) -> list[EmotionSegment]:
    parts = [part.strip() for part in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", script) if part.strip()]
    if not parts:
        return []
    labels = ["calm", "warm", "dreamy", "safe", "relief"]
    return [
        EmotionSegment(index=index, emotion=labels[(index - 1) % len(labels)], text=part[:360])
        for index, part in enumerate(parts[:10], start=1)
    ]
