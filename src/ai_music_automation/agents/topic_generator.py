from __future__ import annotations

import random
from dataclasses import dataclass, field

from .base import AgentContext, BaseAgent
from .json_utils import extract_json_array
from ..automation.model_client import ModelRequest, OllamaClient


@dataclass(frozen=True)
class TopicGeneratorInput:
    seed_prompt: str
    count: int = 10
    existing_topics: list[str] = field(default_factory=list)


class TopicGeneratorAgent(BaseAgent[TopicGeneratorInput, list[str]]):
    name = "topic_generator"

    def __init__(self, model_client: OllamaClient | None = None, max_retries: int = 0) -> None:
        super().__init__(max_retries=max_retries)
        self.model_client = model_client or OllamaClient()

    def execute(self, payload: TopicGeneratorInput, context: AgentContext) -> list[str]:
        count = max(1, min(50, int(payload.count or 10)))
        response = self.model_client.generate(
            ModelRequest(
                base_url=str(context.settings.get("ollama_url") or "http://127.0.0.1:11434"),
                model=str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b"),
                prompt=build_topic_prompt(payload, context.niche, count, context.settings),
                temperature=float(context.settings.get("topic_temperature") or 0.9),
                top_p=float(context.settings.get("topic_top_p") or 0.95),
                timeout_seconds=int(context.settings.get("timeout_seconds") or 240),
            )
        )
        topics = parse_topics(response)
        if not topics:
            topics = fallback_topics(payload.seed_prompt, context.niche)
        return dedupe_topics(topics, payload.existing_topics)[:count]


def build_topic_prompt(payload: TopicGeneratorInput, niche: str, count: int, settings: dict | None = None) -> str:
    settings = settings or {}
    existing = "\n".join(f"- {topic}" for topic in payload.existing_topics[:80]) or "None"
    variety = str(settings.get("story_variety_prompt") or "").strip()
    return f"""Generate {count} fresh YouTube story topics for niche: {niche}.

Seed direction:
{payload.seed_prompt}

Variety direction:
{variety or "Rotate settings, character types, emotional problems, magical objects, and lessons. Do not let two topics feel like the same story with renamed scenery."}

Existing topics to avoid:
{existing}

Rules:
- Return only a JSON array of strings.
- Avoid duplicates and near-duplicates.
- Keep each topic clear, emotionally specific, and suitable for the niche.
- For sleep stories, each topic must imply a different character, setting, emotional problem, and lesson.
- Make the set diverse across subgenres: fairytale, cozy mystery, gentle adventure, magical realism, folktale, seaside tale, winter tale, train journey, library tale, bakery tale.
- Include concrete story objects: key, lantern, recipe, letter, clock, boat, map, teacup, bell, window, seed, scarf, compass.
- Include varied lessons: patience, letting go, keeping a promise, asking for help, resting without guilt, sharing warmth, forgiving quietly, trusting morning.
- Do not keep reusing moon meadow, child under the moon, Elara, fireflies, or glowing flowers.
- Good variety includes library, lighthouse, bakery, train, winter garden, cloud observatory, quiet inn,
  old clockmaker, tea house, snow village, river boat, attic map, seaside lantern, mountain cabin.
"""


def parse_topics(response: str) -> list[str]:
    data = extract_json_array(response)
    if data:
        return [str(item).strip() for item in data if str(item).strip()]
    return [
        line.strip("-*0123456789. \t")
        for line in (response or "").splitlines()
        if line.strip("-*0123456789. \t")
    ]


def dedupe_topics(topics: list[str], existing: list[str]) -> list[str]:
    seen = {normalize_topic(topic) for topic in existing}
    output: list[str] = []
    for topic in topics:
        normalized = normalize_topic(topic)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(topic)
    random.shuffle(output)
    return output


def normalize_topic(value: str) -> str:
    return " ".join(value.lower().replace(":", " ").replace("-", " ").split())


def fallback_topics(seed_prompt: str, niche: str) -> list[str]:
    return [
        "The Little Library That Stayed Open for One Tired Traveler",
        "The Lighthouse Keeper Who Learned to Rest",
        "The Night Train to the Valley of Soft Lanterns",
        "The Baker Who Saved One Warm Loaf for the Lonely Star",
        "The Winter Garden Behind the Blue Door",
        "The Cloud Observatory Where Lost Worries Became Rain",
        "The Old Clockmaker and the Hour That Needed to Sleep",
        "The Quiet Inn at the Edge of the Snow",
        "The River Boat That Carried a Promise Home",
        "The Attic Map to the Gentle Morning",
        "The Tea House Where Every Cup Remembered a Kind Word",
        "The Seamstress Who Stitched a Blanket for the Wind",
        "The Village Bell That Rang Only for Forgiveness",
        "The Tiny Bookshop at the End of the Rain",
        "The Garden Gate That Opened When Someone Let Go",
        "The Sleepy Postman and the Letter He Was Afraid to Deliver",
        "The Moonlit Bakery and the Recipe for Patience",
        "The Compass That Pointed Toward Home Only After Sunset",
        "The Snow Village That Shared Its Last Candle",
        "The Quiet Toymaker and the Doll Who Needed a Name",
        "The Orchard Where Forgotten Promises Became Blossoms",
        "The Harbor Keeper and the Lantern Nobody Claimed",
        "The Blue Umbrella That Waited for the Morning Train",
        "The Little Museum of Things People Finally Released",
        "The Mountain Cabin Where a Tired Teacher Learned to Ask for Help",
        "The Whispering Quilt and the Child Who Could Not Sleep",
        "The Watchmaker's Apprentice and the Minute That Would Not Pass",
        "The Paper Boat That Carried a Worry Across the Lake",
        "The Gentle Keeper of the Lavender Hill",
        "The Last Window Lit in the Rainy Town",
    ]
