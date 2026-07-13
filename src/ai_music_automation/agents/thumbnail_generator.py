from __future__ import annotations

from dataclasses import dataclass

from .base import AgentContext, BaseAgent
from .json_utils import extract_json_object
from ..automation.artifacts import StoryArtifact
from ..automation.model_client import ModelRequest, OllamaClient


@dataclass(frozen=True)
class ThumbnailPromptInput:
    story: StoryArtifact
    niche: str = "sleep_story"


class ThumbnailGeneratorAgent(BaseAgent[ThumbnailPromptInput, str]):
    name = "thumbnail_generator"

    def __init__(self, model_client: OllamaClient | None = None, max_retries: int = 0) -> None:
        super().__init__(max_retries=max_retries)
        self.model_client = model_client or OllamaClient()

    def execute(self, payload: ThumbnailPromptInput, context: AgentContext) -> str:
        response = self.model_client.generate(
            ModelRequest(
                base_url=str(context.settings.get("ollama_url") or "http://127.0.0.1:11434"),
                model=str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b"),
                prompt=build_thumbnail_prompt(payload),
                temperature=float(context.settings.get("thumbnail_temperature") or 0.55),
                top_p=float(context.settings.get("thumbnail_top_p") or 0.9),
                timeout_seconds=int(context.settings.get("timeout_seconds") or 240),
            )
        )
        data = extract_json_object(response) or {}
        prompt = str(data.get("prompt") or "").strip()
        return prompt or fallback_thumbnail_prompt(payload.story)


def build_thumbnail_prompt(payload: ThumbnailPromptInput) -> str:
    return f"""Create a thumbnail image prompt for a YouTube video.

Niche: {payload.niche}
Title: {payload.story.title}
Story hook: {payload.story.hook}
Story lesson: {payload.story.lesson}
Story excerpt:
{payload.story.script[:900]}

Rules:
- Do not use the final scene.
- Use one large focal subject from the actual story, not a generic moon/forest.
- Include one strange or symbolic object from the plot near the focal subject.
- Make the character emotion readable at small size: wonder, relief, tenderness, or curiosity.
- Keep the composition simple: 1 subject, 1 object, clear background, strong silhouette.
- Make it simple, emotional, and readable as a YouTube thumbnail on mobile.
- Prioritize curiosity: the viewer should instantly understand that something gentle but unusual is happening.
- Leave strong negative space for 2-4 large overlay words.
- Use higher contrast than the video artwork while staying calm and bedtime-safe.
- No text, no watermark, no logo.
- Keep it safe and calm for bedtime.
- Return only JSON: {{"prompt": "..."}}
"""


def fallback_thumbnail_prompt(story: StoryArtifact) -> str:
    return (
        f"Dreamy bedtime story thumbnail for '{story.title}', one large emotional focal subject from the story "
        "beside the key symbolic object, soft warm moonlit contrast, clear silhouette, gentle curiosity, "
        "cinematic 16:9 composition, no text, no watermark"
    )
