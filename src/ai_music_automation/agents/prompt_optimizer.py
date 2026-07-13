from __future__ import annotations

import re
from dataclasses import dataclass

from .base import AgentContext, BaseAgent
from .json_utils import extract_json_array
from ..automation.artifacts import SceneArtifact
from ..automation.model_client import ModelRequest, OllamaClient


@dataclass(frozen=True)
class PromptOptimizerInput:
    scenes: list[SceneArtifact]
    reference_style: str = ""


class PromptOptimizerAgent(BaseAgent[PromptOptimizerInput, list[SceneArtifact]]):
    name = "prompt_optimizer"

    def __init__(self, model_client: OllamaClient | None = None, max_retries: int = 0) -> None:
        super().__init__(max_retries=max_retries)
        self.model_client = model_client or OllamaClient()

    def execute(self, payload: PromptOptimizerInput, context: AgentContext) -> list[SceneArtifact]:
        response = self.model_client.generate(
            ModelRequest(
                base_url=str(context.settings.get("ollama_url") or "http://127.0.0.1:11434"),
                model=str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b"),
                prompt=build_optimizer_prompt(payload, context.settings),
                temperature=float(context.settings.get("prompt_temperature") or 0.45),
                top_p=float(context.settings.get("prompt_top_p") or 0.9),
                timeout_seconds=int(context.settings.get("timeout_seconds") or 240),
            )
        )
        prompts = parse_prompts(response)
        output: list[SceneArtifact] = []
        for index, scene in enumerate(payload.scenes):
            image_prompt = prompts[index] if index < len(prompts) else fallback_image_prompt(scene, payload.reference_style)
            image_prompt = story_specific_image_prompt(scene, image_prompt, payload.reference_style, context.settings)
            output.append(
                SceneArtifact(
                    index=scene.index,
                    label=scene.label,
                    summary=scene.summary,
                    emotion=scene.emotion,
                    image_prompt=image_prompt,
                    source_text=scene.source_text,
                )
            )
        return output


def build_optimizer_prompt(payload: PromptOptimizerInput, settings: dict | None = None) -> str:
    settings = settings or {}
    scenes = "\n".join(
        f"{scene.index}. {scene.label} | emotion={scene.emotion} | summary={scene.summary} | source={scene.source_text}"
        for scene in payload.scenes
    )
    style_library = str(settings.get("image_style_library") or "").strip()
    character_memory = str(settings.get("character_memory") or "").strip()
    world_memory = str(settings.get("world_memory") or "").strip()
    negative_prompt = str(settings.get("local_image_negative_prompt") or settings.get("comfyui_negative_prompt") or "").strip()
    style_lock = sleep_story_style_lock(settings, payload.reference_style)
    return f"""Rewrite each scene into a high-quality image generation prompt for a cinematic bedtime story video.

Reference style:
{payload.reference_style or "dreamy storybook illustration, soft moonlight, calm bedtime mood"}

Style library:
{style_library or "storybook illustration, cinematic 16:9 composition, soft volumetric moonlight, warm window glow, dreamy blue-gold palette, painterly detail, peaceful atmosphere"}

Required style lock for every prompt:
{style_lock}

Character memory:
{character_memory or "Keep the main character visually consistent across scenes when a character is present: same apparent age, hair, clothing palette, and gentle facial feeling."}

World memory:
{world_memory or "Keep recurring objects and places consistent across scenes: moonlight, lanterns, cabins, paths, gardens, forests, windows, weather, and color palette."}

Negative constraints:
{negative_prompt or "no text, no watermark, no logo, no distorted face, no horror, no harsh contrast, no noisy artifacts"}

Rules:
- Preserve the scene meaning.
- The first 35 words must describe the unique story moment: character, action, setting, and key object.
- Include the exact visible subject from the scene summary. If a character is named, include the name.
- Include a concrete action/pose and a concrete plot object, not just mood.
- Append character memory and world memory when relevant.
- Use specific visual nouns from the scene, not generic atmosphere only.
- Make each prompt visually different from the previous scenes while keeping the same story world.
- Keep texture, palette, light quality, and illustration medium identical across all prompts by appending the required style lock.
- Keep the same character profile across all prompts when the same character appears.
- Use this internal structure in each prompt, but return it as one natural prompt string:
  Scene content: unique character/action/setting/object.
  Character profile: stable age/hair/outfit/carried object.
  World/style: watercolor storybook style lock.
- Use style guidance only; do not copy exact reference composition, character, face, pose, or scene.
- Avoid text, watermark, logo, horror, harsh contrast.
- Return only a JSON array of prompt strings in the same order.

Scenes:
{scenes}
"""


def parse_prompts(response: str) -> list[str]:
    data = extract_json_array(response)
    if not data:
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def fallback_image_prompt(scene: SceneArtifact, reference_style: str = "") -> str:
    style = reference_style.strip() or (
        "dreamy storybook illustration, soft moonlight, warm window glow, painterly texture, "
        "cozy cinematic composition, calm bedtime mood"
    )
    return (
        f"{style}. {scene.label}: {scene.summary}. Emotion: {scene.emotion or 'calm'}. "
        "soft glow, gentle contrast, cinematic 16:9 composition, no text, no watermark, no logo"
    )


def story_specific_image_prompt(
    scene: SceneArtifact,
    image_prompt: str,
    reference_style: str,
    settings: dict | None = None,
) -> str:
    settings = settings or {}
    cleaned = re.sub(r"\s+", " ", image_prompt or "").strip()
    summary = re.sub(r"\s+", " ", scene.summary or "").strip()
    source = re.sub(r"\s+", " ", scene.source_text or "").strip()
    style = str(settings.get("image_style_library") or reference_style or "").strip()
    style_lock = sleep_story_style_lock(settings, reference_style)
    character_memory = str(settings.get("character_memory") or "").strip()
    world_memory = str(settings.get("world_memory") or "").strip()

    if summary and summary.lower() not in cleaned.lower():
        cleaned = f"{summary}. {cleaned}"
    if source:
        cleaned = f"{cleaned}. Story passage anchor: {source[:220]}"
    if style:
        cleaned = f"{cleaned}. Style library: {style}"
    cleaned = f"{cleaned}. Style lock: {style_lock}"
    if character_memory:
        cleaned = f"{cleaned}. Character consistency: {character_memory}"
    if world_memory:
        cleaned = f"{cleaned}. World consistency: {world_memory}"
    cleaned = (
        f"{cleaned}. Scene must match this exact plot moment, with the visible character, action, "
        "location, and key object from the story. Keep the same watercolor texture, muted palette, and warm moonlit glow across all images. cinematic 16:9 composition, no text, no watermark, no logo"
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def sleep_story_style_lock(settings: dict | None = None, reference_style: str = "") -> str:
    settings = settings or {}
    custom = str(settings.get("story_art_style_custom") or "").strip()
    configured = str(settings.get("art_style") or "").strip()
    base = custom or configured or reference_style
    watercolor_lock = (
        "watercolor storybook illustration, soft wet paper texture, delicate transparent washes, "
        "hand-painted fairytale fantasy, soft uneven brush edges, visible watercolor paper grain, "
        "muted pastel greens and misty blues, cream moonlight, warm honey-gold glow, cozy magical bedtime atmosphere, "
        "clear charming silhouettes, layered background depth, detailed but not realistic, illustrated forms, "
        "peaceful poetic composition, gentle fantasy-comic readability"
    )
    if base:
        return f"{watercolor_lock}, {base}, not photorealistic, not 3d, not anime screenshot, no text"
    return f"{watercolor_lock}, not photorealistic, not 3d, not anime screenshot, no text"
