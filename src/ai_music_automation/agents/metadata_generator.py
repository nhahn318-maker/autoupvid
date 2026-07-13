from __future__ import annotations

import re
from dataclasses import dataclass

from .base import AgentContext, BaseAgent
from .json_utils import as_str_list, extract_json_object
from ..automation.artifacts import MetadataArtifact, SceneArtifact, StoryArtifact
from ..automation.model_client import ModelRequest, OllamaClient


@dataclass(frozen=True)
class MetadataGeneratorInput:
    story: StoryArtifact
    scenes: list[SceneArtifact] | None = None
    target_minutes: int = 3
    thumbnail_prompt: str = ""


class MetadataGeneratorAgent(BaseAgent[MetadataGeneratorInput, MetadataArtifact]):
    name = "metadata_generator"

    def __init__(self, model_client: OllamaClient | None = None, max_retries: int = 0) -> None:
        super().__init__(max_retries=max_retries)
        self.model_client = model_client or OllamaClient()

    def execute(self, payload: MetadataGeneratorInput, context: AgentContext) -> MetadataArtifact:
        response = self.model_client.generate(
            ModelRequest(
                base_url=str(context.settings.get("ollama_url") or "http://127.0.0.1:11434"),
                model=str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b"),
                prompt=build_metadata_prompt(payload, context.niche),
                temperature=float(context.settings.get("metadata_temperature") or 0.45),
                top_p=float(context.settings.get("metadata_top_p") or 0.9),
                timeout_seconds=int(context.settings.get("timeout_seconds") or 240),
            )
        )
        data = extract_json_object(response) or {}
        fallback = fallback_metadata(payload)
        title = fit_title(str(data.get("title") or fallback.title))
        title = improve_sleep_story_title(title, payload)
        description = str(data.get("description") or fallback.description).strip()
        if "0:00" not in description:
            description = append_timeline(description, payload)
        hashtags = normalize_hashtags(as_str_list(data.get("hashtags")) or fallback.hashtags)
        description = ensure_hashtag_line(description, hashtags)
        return MetadataArtifact(
            title=title,
            description=description,
            tags=as_str_list(data.get("tags")) or fallback.tags,
            hashtags=hashtags,
            keywords=as_str_list(data.get("seo_keywords")) or fallback.keywords,
            thumbnail_prompt=payload.thumbnail_prompt,
        )


def build_metadata_prompt(payload: MetadataGeneratorInput, niche: str) -> str:
    scene_lines = "\n".join(
        f"{scene.index}. {scene.label}: {scene.summary}"
        for scene in (payload.scenes or [])[:10]
    )
    return f"""Generate YouTube metadata for niche: {niche}.

Channel name: Sleepu Stories
Title: {payload.story.title}
Hook: {payload.story.hook}
Lesson: {payload.story.lesson}
Story summary:
{payload.story.script[:1200]}
Scene beats:
{scene_lines or payload.story.outline or "Use the actual story beats from the script."}

Write metadata for a long-form English bedtime story video.
Requirements:
- Title must match the actual story content, not a generic sleep title.
- Title must be built for YouTube click-through: combine one emotional promise with one concrete story object/setting.
- Good title shape: "Let Go of a Heavy Heart | The Rain Lantern Sleep Story" or
  "Fall Asleep to the Quiet Clockmaker | Bedtime Story for Tired Minds".
- Avoid title-only literary names like "The Lantern That Remembered the Rain" unless paired with a clear sleep/emotional reason to click.
- Do not use fake urgency, shock, all caps, or misleading claims.
- Description must summarize the character, quiet problem, journey, and lesson from this exact story.
- Include a chapter timeline based on the story beats. The first chapter must start at 0:00.
- Include 8-12 relevant tags and 5-8 hashtags.
- Keep the tone calm, warm, and SEO-friendly for sleep story, bedtime story, relaxing narration, and story before sleep.

Return only JSON:
{{
  "title": "under 100 characters",
  "description": "warm SEO description with timeline and hashtags",
  "tags": ["tag"],
  "hashtags": ["#hashtag"],
  "seo_keywords": ["keyword"]
}}
"""


def fallback_metadata(payload: MetadataGeneratorInput) -> MetadataArtifact:
    story = payload.story
    hashtags = [
        "#sleepstory",
        "#bedtimestory",
        "#sleepystories",
        "#storybeforesleep",
        "#relaxingstory",
        "#calmnarration",
        "#sleepustories",
    ]
    tags = [
        "sleep story",
        "bedtime story",
        "story before sleep",
        "relaxing bedtime story",
        "calm narration",
        "sleepy stories",
        "cozy story",
        "adult bedtime story",
        "peaceful sleep",
        "Sleepu Stories",
    ]
    title = improve_sleep_story_title(fit_title(story.title or title_from_script(story.script)), payload)
    description = (
        f"{title}\n\n"
        f"Welcome to Sleepu Stories. In this calming bedtime story, {story_summary(story)}\n\n"
        f"Gentle lesson: {story.lesson or infer_lesson(story.script)}\n\n"
        "Listen slowly, settle in, and let the story carry you toward a quieter night.\n"
    )
    description = append_timeline(description, payload)
    description = ensure_hashtag_line(description, hashtags)
    return MetadataArtifact(
        title=title,
        description=description,
        tags=tags,
        hashtags=hashtags,
        keywords=[
            "sleep story",
            "bedtime story",
            "story before sleep",
            "relaxing story for sleep",
            "calm bedtime narration",
        ],
    )


def fit_title(value: str) -> str:
    value = " ".join((value or "A Gentle Story Before Sleep").split())
    return value[:100].rstrip()


def improve_sleep_story_title(candidate: str, payload: MetadataGeneratorInput) -> str:
    """Make the upload title carry a click reason without losing the story object."""
    candidate = fit_title(candidate)
    if strong_sleep_title(candidate):
        return candidate
    motif = story_motif(payload.story.title or candidate or title_from_script(payload.story.script))
    promise = emotional_promise(payload)
    options = [
        f"{promise} | {motif} Sleep Story",
        f"Fall Asleep to {motif} | Bedtime Story for Tired Minds",
        f"{motif} for a Tired Heart | Sleep Story",
    ]
    for option in options:
        if len(option) <= 96:
            return option
    return fit_title(f"{motif} | Sleep Story")


def strong_sleep_title(value: str) -> bool:
    lowered = value.lower()
    has_sleep_intent = any(token in lowered for token in ("sleep story", "bedtime story", "fall asleep", "deep sleep"))
    has_emotion = any(
        token in lowered
        for token in (
            "tired",
            "heart",
            "overthinking",
            "anxiety",
            "let go",
            "healing",
            "lonely",
            "rest",
            "peace",
            "calm",
        )
    )
    return has_sleep_intent and has_emotion and len(value) <= 100


def story_motif(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9' ]+", " ", value or "").strip()
    cleaned = re.sub(r"(?i)^(a|an|the)\s+", "", cleaned).strip()
    cleaned = re.sub(r"(?i)\s+(sleep|bedtime)\s+stor(?:y|ies)\s*$", "", cleaned).strip()
    if 8 <= len(cleaned) <= 50:
        return cleaned
    stopwords = {
        "the",
        "a",
        "an",
        "and",
        "of",
        "that",
        "who",
        "for",
        "to",
        "before",
        "sleep",
        "story",
        "stories",
    }
    words = [word for word in cleaned.split() if word.lower() not in stopwords]
    motif = " ".join(words[:5]).strip()
    return motif or "A Gentle Night"


def emotional_promise(payload: MetadataGeneratorInput) -> str:
    haystack = " ".join(
        [
            payload.story.title or "",
            payload.story.hook or "",
            payload.story.lesson or "",
            payload.story.script[:1600] or "",
        ]
    ).lower()
    if any(word in haystack for word in ("overthinking", "racing thoughts", "anxious", "anxiety", "worry", "worried")):
        return "Calm Your Thoughts Tonight"
    if any(word in haystack for word in ("promise", "forgive", "forgiveness", "apology", "letter", "remembered")):
        return "Let Go of a Heavy Heart"
    if any(word in haystack for word in ("lonely", "alone", "missing", "home", "traveler")):
        return "Feel Less Alone Tonight"
    if any(word in haystack for word in ("tired", "rest", "keeper", "work", "clock", "hour")):
        return "Rest Without Guilt Tonight"
    return "A Peaceful Story Before Sleep"


def normalize_hashtags(values: list[str]) -> list[str]:
    output = []
    for value in values:
        item = value.strip()
        if not item:
            continue
        output.append(item if item.startswith("#") else f"#{item.replace(' ', '')}")
    return output


def append_timeline(description: str, payload: MetadataGeneratorInput) -> str:
    timeline = build_timeline(payload)
    if not timeline:
        return description.strip()
    return f"{description.strip()}\n\nTimeline:\n{timeline}".strip()


def build_timeline(payload: MetadataGeneratorInput) -> str:
    scenes = list(payload.scenes or [])
    if scenes:
        labels = [timeline_label(scene.label or scene.summary) for scene in scenes[:8]]
    else:
        labels = [
            "Opening the quiet story",
            "The small mystery begins",
            "A gentle choice is made",
            "The lesson becomes clear",
            "A peaceful ending for sleep",
        ]
    total_seconds = max(90, int(payload.target_minutes or 3) * 60)
    step = max(20, total_seconds // max(1, len(labels)))
    lines = []
    for index, label in enumerate(labels):
        lines.append(f"{format_timestamp(index * step)} {label}")
    return "\n".join(lines)


def format_timestamp(seconds: int) -> str:
    minutes, secs = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{secs:02}"


def timeline_label(value: str) -> str:
    cleaned = " ".join((value or "").replace("\n", " ").split())
    cleaned = cleaned.strip(" .:-")
    if not cleaned:
        return "A quiet story beat"
    return cleaned[:70].rstrip()


def ensure_hashtag_line(description: str, hashtags: list[str]) -> str:
    clean = description.strip()
    if not hashtags:
        return clean
    hashtag_line = " ".join(hashtags[:8])
    lines = [line for line in clean.splitlines() if not line.strip().startswith("#")]
    body = "\n".join(lines).strip()
    return f"{body}\n\n{hashtag_line}".strip()


def title_from_script(script: str) -> str:
    text = " ".join((script or "").split())
    return text[:60].rstrip(" .,;:") or "A Gentle Story Before Sleep"


def story_summary(story: StoryArtifact) -> str:
    hook = (story.hook or "").strip()
    if hook:
        return hook[:220].rstrip(" .") + "."
    script = " ".join((story.script or "").split())
    return script[:260].rstrip(" .") + "."


def infer_lesson(script: str) -> str:
    lowered = (script or "").lower()
    if "promise" in lowered:
        return "a promise can become even kinder when it makes room for another heart."
    if "home" in lowered:
        return "home can be found again through patience, kindness, and a slower step."
    if "listen" in lowered or "heard" in lowered:
        return "quiet listening can reveal what hurry often misses."
    return "even a small gentle choice can make the night feel safer and softer."
