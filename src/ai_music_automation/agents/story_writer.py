from __future__ import annotations

import re
from dataclasses import dataclass

from .base import AgentContext, BaseAgent
from ..automation.artifacts import StoryArtifact
from ..automation.model_client import ModelRequest, OllamaClient


@dataclass(frozen=True)
class StoryWriterInput:
    title: str
    prompt: str
    target_minutes: int
    reference_style: str = ""


class StoryWriterAgent(BaseAgent[StoryWriterInput, StoryArtifact]):
    name = "story_writer"

    def __init__(self, model_client: OllamaClient | None = None, max_retries: int = 0) -> None:
        super().__init__(max_retries=max_retries)
        self.model_client = model_client or OllamaClient()

    def execute(self, payload: StoryWriterInput, context: AgentContext) -> StoryArtifact:
        cache_key = None
        if context.cache:
            cache_key = context.cache.key_for(
                self.name,
                {
                    "niche": context.niche,
                    "title": payload.title,
                    "prompt": payload.prompt,
                    "target_minutes": payload.target_minutes,
                    "reference_style": payload.reference_style,
                    "model": context.settings.get("ollama_model") or context.settings.get("model"),
                    "prompt_version": context.settings.get("story_prompt_version") or 11,
                },
            )
            cached = context.cache.read_json(cache_key)
            if cached and cached.get("script"):
                cached_script = repair_complete_sleep_story_script(
                    polish_adult_sleep_story_script(str(cached.get("script") or "")),
                    str(cached.get("title") or payload.title),
                )
                if cached_script != str(cached.get("script") or ""):
                    context.cache.write_json(
                        cache_key,
                        {
                            **cached,
                            "script": cached_script,
                            "hook": first_sentence(cached_script),
                            "ending": last_sentence(cached_script),
                        },
                    )
                return StoryArtifact(
                    title=str(cached.get("title") or payload.title),
                    prompt=str(cached.get("prompt") or payload.prompt),
                    script=cached_script,
                    hook=first_sentence(cached_script),
                    ending=last_sentence(cached_script),
                    lesson=str(cached.get("lesson") or ""),
                )

        if int(payload.target_minutes or 0) >= 8:
            script = generate_chaptered_bedtime_story(self.model_client, payload, context)
        else:
            response = self.model_client.generate(
                ModelRequest(
                    base_url=str(context.settings.get("ollama_url") or "http://127.0.0.1:11434"),
                    model=str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b"),
                    prompt=build_bedtime_story_prompt(payload),
                    temperature=float(context.settings.get("story_temperature") or 0.75),
                    top_p=float(context.settings.get("story_top_p") or 0.92),
                    timeout_seconds=int(context.settings.get("timeout_seconds") or 240),
                )
            )
            script = clean_script(response)
            target_words = story_target_word_count(payload.target_minutes)
            if len(script.split()) < max(500, int(target_words * 0.75)):
                script = expand_long_story_tail(self.model_client, payload, context, script, target_words)
        script = repair_complete_sleep_story_script(
            polish_adult_sleep_story_script(trim_script_to_target(script, payload.target_minutes)),
            payload.title,
        )
        if len(script.split()) < 80:
            script = fallback_sleep_story(payload.title, payload.target_minutes)

        story = StoryArtifact(
            title=clean_title(payload.title),
            prompt=payload.prompt,
            script=script,
            hook=first_sentence(script),
            ending=last_sentence(script),
        )
        if context.cache and cache_key:
            context.cache.write_json(
                cache_key,
                {
                    "title": story.title,
                    "prompt": story.prompt,
                    "script": story.script,
                    "hook": story.hook,
                    "ending": story.ending,
                    "lesson": story.lesson,
                },
            )
        return story


def build_bedtime_story_prompt(payload: StoryWriterInput) -> str:
    target_words = story_target_word_count(payload.target_minutes)
    return f"""You write calm English bedtime stories for a YouTube channel called Story Before Sleep.

Create one complete narration script.

Title: {clean_title(payload.title)}
Target length: about {target_words} words.

Creative direction:
{payload.prompt}

Reference art style to extract:
{payload.reference_style or "Use a soft, cohesive bedtime-story illustration style."}

Rules:
- Audience: tired adults, not children. The story may feel fairytale-like, but it must be emotionally mature.
- Creative lane: gentle fantasy storybook / soft graphic-novel bedtime tale. It should have wonder, visual
  curiosity, and a memorable magical object, but no danger, combat, chase, horror, or loud stakes.
- Long-form retention structure: description -> discovery -> small mystery -> discovery -> memory ->
  new room/place -> new object -> another revelation -> kind choice -> sleep resolution.
- Every 3-5 minutes, something visible should change: a new clue, room, object state, helper, memory, or revelation.
- Each story must feel meaningfully different from previous stories: change the setting, symbolic object,
  helper, emotional wound, and magical rule. Do not keep returning to the same moon meadow, cottage path,
  lantern, teacup, clockmaker, or lost-light structure unless the title specifically asks for it.
- Use a light comic/storybook sense of discovery: a hidden door in a tree root, a map that appears in steam,
  a sleepy bridge that remembers footsteps, a paper boat carrying an old apology, a constellation shop,
  a rain library, a mushroom village, a whispering scarf, a glass garden, a tide bell, or another calm wonder.
- The fantasy element must affect the plot through visible action. It cannot be only decorative atmosphere.
- The first 30 seconds must hook retention with an emotional promise to the listener.
  Start with one direct sentence like "If you have been carrying..." or "Have you ever...".
  Then move into the named character and the story world.
- The opening must quickly answer: why the symbolic object matters, what quiet hurt the character carries,
  and what feeling the listener may receive by staying with the story.
- Use gentle, slow, sensory language.
- Keep the story safe for sleep: no violence, no panic, no loud tension.
- Write an actual story with a clear beginning, middle, turning point, and ending.
- Do not write a guided meditation, an atmosphere piece, or a list of pretty images.
- Include a named main character, a peaceful setting, a specific emotional need or promise, a gentle event,
  a meaningful choice, a consequence of that choice, and a soft resolution.
- Give the main character a simple visual design an illustrator can reuse: apparent age, hair, outfit color,
  and one carried object. Keep it adult or age-neutral, not toddler-like.
- Give the emotional need one concrete, drawable memory when it fits naturally: an unsent letter, a cup saved
  for someone, a night waiting beside a window, an apology never spoken, or a promise that was not kept.
  Keep it tender, not tragic. Strongly prefer an exact moment, place, person/object, and action over vague
  lines like "a memory of waiting".
- Add a small magical mystery around the symbolic object or setting. It should invite curiosity without urgency.
- Include 3-5 visually distinct locations or set pieces that an image model can draw, while keeping the story
  calm: workshop, bridge, shore, library, greenhouse, railway platform, floating market, attic, snow village,
  moon garden, cloud balcony, candlelit room, forest gate, tide pool, or similar.
- Give the story one memorable lesson. The lesson must come from what the character does, not from a lecture.
- Make the character change in a small but visible way by the end.
- Let the character do simple actions: walk, listen, find, carry, give, follow, remember, return, rest.
- The plot should be calm but visible; every paragraph should move the story forward a little.
- Include one quiet moment of curiosity in the first paragraph: something missing, promised, unfinished,
  misunderstood, or softly mysterious.
- Include one gentle helper or obstacle: an old lantern, a tired animal, a closed gate, a forgotten letter,
  a quiet neighbor, a fading star, or another soft story object.
- Use one recurring symbolic object that appears at least three times and changes meaning by the end.
- Include at least three visible actions that an illustrator can draw, not only feelings or thoughts.
- Make the middle of the story contain a small irreversible choice, so the ending feels earned.
- In the final third, let the character make a kind choice that costs something small:
  time, comfort, pride, certainty, or the wish to keep something for themselves.
- End with a clear emotional takeaway, phrased naturally inside the narration.
- Prefer scenes and embodied details over abstract explanation. Do not over-explain forgiveness, peace,
  silence, or healing; show it through hands loosening, a letter being folded, a cup being set down,
  a window being opened, a light being shared, or a character choosing not to clutch a memory.
- Avoid repeating the same mood words too often, especially: quiet, silence, soft, gentle, moonlight,
  stillness, heavy, warm, peaceful. Use concrete sensory details instead.
- Avoid ending the story early. Words such as "fell asleep", "drifted into sleep", "rest now",
  or "you may rest" should be reserved for the final paragraph. Before then, use "settled", "paused",
  "breathed more easily", or "rested in the moment".
- Avoid generic lessons like "be kind" unless the plot makes the lesson specific.
- Avoid relying on second-person guided phrases such as "imagine yourself" for the whole script.
- Do not make the listener the only character; the listener may be invited in, but the story needs its own character.
- Do not use child-positioning phrases such as "little one", "my dear child", or "sleep now, little one".
- Do not write as content for kids. Avoid childish framing even if the art is illustrated.
- Use only mild bedtime stakes: missing home, wondering about a light, looking for a lullaby,
  helping a tired creature, or learning why the moon is gentle.
- Maintain one consistent art direction suitable for dreamy illustrated images.
- Art direction should support watercolor storybook images with a little fantasy-comic readability:
  clear silhouettes, charming props, layered backgrounds, gentle panels of action, warm light sources,
  and readable focal objects.
- Reference images are for art style only: color palette, lighting, brushwork, mood, softness, and visual texture.
- Do not copy the reference image composition, exact character, face, pose, or scene.
- Do not include headings, markdown, scene labels, timestamps, or bullet points.
- Start directly with narration.
- End softly for an adult listener, with wording like "You may rest now, safe in the quiet light."
"""


def generate_chaptered_bedtime_story(model_client: OllamaClient, payload: StoryWriterInput, context: AgentContext) -> str:
    target_words = story_target_word_count(payload.target_minutes)
    chapter_count = max(4, min(10, round(target_words / 850)))
    words_per_chapter = max(650, min(950, round(target_words / chapter_count)))
    base_url = str(context.settings.get("ollama_url") or "http://127.0.0.1:11434")
    model = str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b")
    timeout = max(300, int(context.settings.get("timeout_seconds") or 240))
    previous_summary = ""
    chapters: list[str] = []
    for index in range(1, chapter_count + 1):
        response = model_client.generate(
            ModelRequest(
                base_url=base_url,
                model=model,
                prompt=build_chapter_prompt(payload, index, chapter_count, words_per_chapter, previous_summary),
                temperature=float(context.settings.get("story_temperature") or 0.75),
                top_p=float(context.settings.get("story_top_p") or 0.92),
                timeout_seconds=timeout,
            )
        )
        chapter = clean_script(response)
        if len(chapter.split()) < max(260, int(words_per_chapter * 0.45)):
            chapter = expand_short_chapter(model_client, payload, context, chapter, index, words_per_chapter)
        chapters.append(clean_script(chapter))
        previous_summary = summarize_for_continuity(chapters)
    script = "\n\n".join(chapter for chapter in chapters if chapter.strip())
    for _ in range(2):
        if len(script.split()) >= max(900, int(target_words * 0.85)):
            break
        script = expand_long_story_tail(model_client, payload, context, script, target_words)
    return script


def build_chapter_prompt(
    payload: StoryWriterInput,
    chapter_index: int,
    chapter_count: int,
    words_per_chapter: int,
    previous_summary: str,
) -> str:
    position = "opening" if chapter_index == 1 else "ending" if chapter_index == chapter_count else "middle"
    chapter_guidance = chapter_progression_guidance(chapter_index, chapter_count)
    plan_focus = selected_outline_focus(payload.prompt, chapter_index, chapter_count)
    return f"""You are writing chapter {chapter_index}/{chapter_count} of one continuous English bedtime story for tired adults.

Title: {clean_title(payload.title)}
Target for this chapter: around {words_per_chapter} words. Do not exceed {words_per_chapter + 120} words.
Chapter role: {position}

Creative direction and plan:
{payload.prompt}

Reference art style:
{payload.reference_style or "watercolor storybook, gentle fantasy-comic readability"}

Previous continuity summary:
{previous_summary or "This is the opening chapter."}

Chapter-specific job:
{chapter_guidance}

Current plan beat to advance:
{plan_focus}

Rules:
- Continue one coherent story. Do not restart the story in each chapter.
- The previous continuity summary is already written. Continue after it; do not retell it.
- Keep it adult bedtime: calm, emotionally mature, no child-positioning language.
- Write a story about a named adult protagonist. Do not make the listener the protagonist.
- Use third-person narration for the story. Avoid guided-meditation framing like "imagine you are standing" or "you walk".
- Directly address the listener only in the opening hook and final sleep sign-off.
- Keep gentle fantasy storybook/comic wonder: visible magical rule, charming object, distinct set pieces.
- Every chapter must include visible actions and specific objects an illustrator can draw.
- Every chapter must introduce a visibly different set piece, object state, or action from previous chapters.
- Do not spend the whole chapter in one window, one room, one repeated image, or one repeated realization.
- Keep the same main character profile and symbolic object across chapters.
- Avoid danger, villains, chase, combat, horror, panic, or loud tension.
- Avoid headings, markdown, bullet points, chapter labels, and timestamps.
- Avoid repeating generic mood words; use concrete sensory details and action.
- Chapter 1 only must start with a direct adult hook like "If you have been carrying...".
- Do not repeat the opening hook after chapter 1.
- Do not use "You may rest now" or any final sleep sign-off until chapter {chapter_count}.
- Only chapter {chapter_count} may fully resolve the story and end with safety, emotional lesson, and sleep.
- Non-final chapters must end on a soft transition into the next scene, not a conclusion.

Write only the narration text for chapter {chapter_index}. No title, no heading.
"""


def chapter_progression_guidance(chapter_index: int, chapter_count: int) -> str:
    retention_beat = retention_beat_name(chapter_index, chapter_count)
    if chapter_index == 1:
        return (
            f"Retention beat: {retention_beat}. Open with the adult listener hook, introduce the protagonist, the specific emotional wound, "
            "the setting, and one magical clue. Do not explain the lesson yet. Do not solve the problem."
        )
    if chapter_index == chapter_count:
        return (
            f"Retention beat: {retention_beat}. Complete the final gentle set piece, let the protagonist choose release through a concrete action, "
            "show the emotional payoff, and close with an adult sleep ending."
        )
    if chapter_count <= 4:
        return f"Retention beat: {retention_beat}. Continue into a new set piece with one new visual location, one action, and one discovery. Keep the resolution incomplete."
    middle = (chapter_count + 1) / 2
    if chapter_index < middle:
        return (
            f"Retention beat: {retention_beat}. Move into a new magical set piece. Reveal one concrete memory or object connected to the wound. "
            "Keep the protagonist active and curious. Do not resolve the central emotion."
        )
    if chapter_index == round(middle):
        return (
            f"Retention beat: {retention_beat}. Reach the emotional center of the story. Show the clearest memory, promise, or hidden truth through "
            "a drawable fantasy scene. Let the protagonist understand part of the lesson, but not fully release it."
        )
    return (
        f"Retention beat: {retention_beat}. Move toward resolution through a different set piece. Show the protagonist practicing the lesson in action, "
        "with the symbolic object changing form or meaning. Keep the final sleep ending for the last chapter."
    )


def retention_beat_name(chapter_index: int, chapter_count: int) -> str:
    beats = [
        "DESCRIPTION",
        "DISCOVERY",
        "SMALL MYSTERY",
        "DISCOVERY",
        "MEMORY",
        "NEW ROOM",
        "NEW OBJECT",
        "ANOTHER REVELATION",
        "KIND CHOICE",
        "SLEEP RESOLUTION",
    ]
    if chapter_count <= 1:
        return beats[-1]
    index = round((chapter_index - 1) * (len(beats) - 1) / max(1, chapter_count - 1))
    return beats[max(0, min(len(beats) - 1, index))]


def selected_outline_focus(prompt: str, chapter_index: int, chapter_count: int) -> str:
    lines: list[str] = []
    in_outline = False
    for raw in (prompt or "").splitlines():
        line = raw.strip()
        if line.lower().startswith("outline:"):
            in_outline = True
            continue
        if in_outline and line.lower().startswith(("ending:", "lesson:")):
            break
        if in_outline and line.startswith("-"):
            lines.append(line.lstrip("- ").strip())
    if not lines:
        return "Advance the next distinct plot beat with a new visual set piece, visible action, and symbolic object."
    if len(lines) == 1:
        return lines[0]
    index = round((chapter_index - 1) * (len(lines) - 1) / max(1, chapter_count - 1))
    return lines[max(0, min(len(lines) - 1, index))]


def expand_short_chapter(
    model_client: OllamaClient,
    payload: StoryWriterInput,
    context: AgentContext,
    chapter: str,
    chapter_index: int,
    words_per_chapter: int,
) -> str:
    target_words = max(550, int(words_per_chapter * 0.8))
    prompt = f"""Expand this bedtime story chapter to around {target_words} words. Do not exceed {target_words + 120} words.

Keep the same events, same character, same object, and same calm adult bedtime tone.
Add sensory detail, visible action, and gentle fantasy storybook texture.
Do not add danger or a new plot. Do not use headings.

Title: {clean_title(payload.title)}

Chapter {chapter_index} draft:
{chapter}
"""
    response = model_client.generate(
        ModelRequest(
            base_url=str(context.settings.get("ollama_url") or "http://127.0.0.1:11434"),
            model=str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b"),
            prompt=prompt,
            temperature=float(context.settings.get("story_temperature") or 0.75),
            top_p=float(context.settings.get("story_top_p") or 0.92),
            timeout_seconds=max(300, int(context.settings.get("timeout_seconds") or 240)),
        )
    )
    expanded = clean_script(response)
    return expanded if len(expanded.split()) > len(chapter.split()) else chapter


def expand_long_story_tail(
    model_client: OllamaClient,
    payload: StoryWriterInput,
    context: AgentContext,
    script: str,
    target_words: int,
) -> str:
    missing = max(600, min(2200, target_words - len(script.split())))
    prompt = f"""Continue and deepen this adult bedtime fantasy story by adding about {missing} words.

Do not restart. Continue from the existing final paragraph and move toward the same calm ending.
Keep third-person narration about the named adult protagonist. Do not switch into guided meditation.
Add one additional gentle set piece, visible action, and emotional payoff.
No headings, no bullets, no danger.

Existing story:
{script[-5000:]}
"""
    response = model_client.generate(
        ModelRequest(
            base_url=str(context.settings.get("ollama_url") or "http://127.0.0.1:11434"),
            model=str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b"),
            prompt=prompt,
            temperature=float(context.settings.get("story_temperature") or 0.75),
            top_p=float(context.settings.get("story_top_p") or 0.92),
            timeout_seconds=max(300, int(context.settings.get("timeout_seconds") or 240)),
        )
    )
    addition = clean_script(response)
    if not addition:
        return script
    return f"{script}\n\n{addition}"


def summarize_for_continuity(chapters: list[str]) -> str:
    text = " ".join(chapters)
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", text) if item.strip()]
    if len(sentences) <= 8:
        return " ".join(sentences)
    return " ".join(sentences[:3] + sentences[-5:])[-1800:]


def story_target_word_count(target_minutes: int) -> int:
    minutes = max(1, int(target_minutes or 3))
    if minutes <= 5:
        return max(650, min(1100, minutes * 180))
    return max(1400, min(5200, minutes * 130))


def clean_title(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value[:100] or "A Gentle Story Before Sleep"


def clean_script(text: str) -> str:
    text = re.sub(r"```.*?```", "", text or "", flags=re.S)
    text = re.sub(r"(?im)^\s*(title|script|narration|scene)\s*:.*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def trim_script_to_target(script: str, target_minutes: int) -> str:
    target_words = story_target_word_count(target_minutes)
    max_words = max(900, int(target_words * 1.12))
    words = script.split()
    if len(words) <= max_words:
        return script
    sentences = re.split(r"(?<=[.!?])\s+", script.strip())
    kept: list[str] = []
    count = 0
    for sentence in sentences:
        sentence_words = sentence.split()
        if kept and count + len(sentence_words) > max_words:
            break
        kept.append(sentence)
        count += len(sentence_words)
    trimmed = " ".join(kept).strip()
    if not trimmed:
        trimmed = " ".join(words[:max_words]).strip()
    if not re.search(r"[.!?]$", trimmed):
        trimmed += "."
    return trimmed


def polish_adult_sleep_story_script(script: str) -> str:
    replacements = {
        r"\b[Ss]leep now,\s*little one\b": "Rest now, and let the quiet hold you gently",
        r"\b[Rr]est now,\s*little one\b": "Rest now, and let the quiet hold you gently",
        r"\b[Mm]y dear child\b": "dear listener",
        r"\b[Ll]ittle one\b": "dear listener",
        r"\bchildren's-book texture\b": "illustrated storybook texture",
        r"\bchildren's book texture\b": "illustrated storybook texture",
    }
    polished = script
    for pattern, replacement in replacements.items():
        polished = re.sub(pattern, replacement, polished)
    polished = soften_nonfinal_sleep_closures(polished)
    return keep_only_final_sleep_signoff(polished).strip()


def soften_nonfinal_sleep_closures(script: str) -> str:
    text = str(script or "")
    if not text.strip():
        return text
    cutoff = int(len(text) * 0.85)
    early = text[:cutoff]
    late = text[cutoff:]
    replacements = [
        (r"\bdrift(?:ed|ing)?\s+into\s+(?:a\s+)?(?:deep\s+and\s+restorative\s+)?sleep\b", "settled into deep stillness"),
        (r"\bdrift(?:ed|ing)?\s+toward\s+sleep\b", "drifted toward stillness"),
        (r"\bfell\s+asleep\b", "grew deeply restful"),
        (r"\bsank\s+into\s+sleep\b", "sank into stillness"),
    ]
    for pattern, replacement in replacements:
        early = re.sub(pattern, replacement, early, flags=re.I)
    return early + late


def repair_complete_sleep_story_script(script: str, title: str = "") -> str:
    """Patch local-model truncation without changing the story's core plot.

    Gemma occasionally stops mid-clause near the ending. A bedtime story should
    never reach review without a complete final sentence and one adult sleep
    resolution, so this trims dangling fragments and appends a short
    story-specific landing when needed.
    """
    repaired = trim_dangling_paragraphs(script)
    if final_section_has_sleep_resolution(repaired):
        return repaired.strip()
    name = infer_main_character_name(repaired) or "the traveler"
    object_name = infer_symbolic_object(repaired)
    object_phrase = f"the {object_name}" if object_name else "the small object"
    ending = (
        f"At last, {name} placed {object_phrase} where the first patient light could find it, "
        "and let both hands rest open in their lap. The lesson did not arrive as a lecture, "
        "but as a simple ease in the body: not every precious moment needed to be measured, "
        "kept, or hurried into shape. The world could breathe at its own pace, and so could "
        f"{name}. You may rest now, safe in the quiet light, while the story settles softly into sleep."
    )
    return f"{repaired.rstrip()}\n\n{ending}".strip()


def trim_dangling_paragraphs(script: str) -> str:
    paragraphs: list[str] = []
    for raw in re.split(r"\n\s*\n", (script or "").strip()):
        paragraph = raw.strip()
        if not paragraph:
            continue
        if re.search(r"[.!?][\"')\]]?$", paragraph):
            paragraphs.append(paragraph)
            continue
        matches = list(re.finditer(r"[.!?][\"')\]]?", paragraph))
        if matches:
            paragraph = paragraph[: matches[-1].end()].strip()
            if paragraph:
                paragraphs.append(paragraph)
    return "\n\n".join(paragraphs).strip()


def final_section_has_sleep_resolution(script: str) -> bool:
    lowered = (script or "").lower()
    final_section = lowered[int(len(lowered) * 0.85) :]
    return bool(
        re.search(
            r"\b(rest now|you may rest|safe to rest|settles? .*? into sleep|"
            r"drift(?:ed|ing)? .*? sleep|fell asleep|slept)\b",
            final_section,
        )
    )


def infer_main_character_name(script: str) -> str:
    for pattern in (
        r"\b(?:named|called)\s+([A-Z][a-z]{2,})\b",
        r"\bthere lived\s+(?:a\s+\w+\s+)?(?:named\s+)?([A-Z][a-z]{2,})\b",
        r"\b([A-Z][a-z]{2,})\s+(?:was|lived|sat|stood|walked|held|carried)\b",
    ):
        match = re.search(pattern, script or "")
        if match:
            return match.group(1)
    return ""


def infer_symbolic_object(script: str) -> str:
    candidates = [
        "brass weight",
        "clock",
        "mechanism",
        "lantern",
        "letter",
        "book",
        "key",
        "seed",
        "cup",
        "bell",
        "map",
        "watch",
        "pendulum",
    ]
    lowered = (script or "").lower()
    scored = [(lowered.count(item), item) for item in candidates]
    scored = [item for item in scored if item[0] > 0]
    if not scored:
        return ""
    return sorted(scored, reverse=True)[0][1]


def keep_only_final_sleep_signoff(script: str) -> str:
    pattern = re.compile(r"\b(?:Rest now|You may rest now)[^.!?]*[.!?]", flags=re.I)
    matches = list(pattern.finditer(script or ""))
    if len(matches) <= 1:
        return script
    final = matches[-1].group(0).strip()
    pieces: list[str] = []
    last_end = 0
    for match in matches[:-1]:
        pieces.append(script[last_end : match.start()])
        if match.start() < 260:
            pieces.append("this story will guide you toward a quieter breath.")
        last_end = match.end()
    pieces.append(script[last_end:])
    without = "".join(pieces).strip()
    without = re.sub(r"\n{3,}", "\n\n", without)
    without = re.sub(r",\s*\n\n", ".\n\n", without, count=1)
    if re.search(re.escape(final), without, flags=re.I):
        return without
    return f"{without}\n\n{final}".strip()


def first_sentence(script: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", script.strip())
    return sentences[0].strip() if sentences else ""


def last_sentence(script: str) -> str:
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", script.strip()) if item.strip()]
    return sentences[-1] if sentences else ""


def fallback_sleep_story(title: str, target_minutes: int) -> str:
    base = (
        f"Tonight's story is called {clean_title(title)}. In a quiet village at the edge of a moonlit meadow, "
        "there lived a young woman named Mira who had promised her grandmother she would hang a small silver bell by the window "
        "before the first spring moon. But when bedtime came, the bell was gone. Mira did not feel frightened, only unfinished, "
        "as if the day had left one soft thread untied. "
        "So Mira wrapped herself in a warm shawl and followed the pale glow across the grass. "
        "Along the path, she met a sleepy firefly carrying a spark that was almost too heavy for its tiny wings. "
        "Mira wanted to hurry past, because the bell mattered to her promise, yet the little light trembled in the wind. "
        "She cupped her hands around the firefly and walked slowly, giving up speed so the spark could stay alive. "
        "Together they crossed the meadow, past folded flowers and clouds that moved like slow boats. "
        "At last they found the silver bell resting beneath a moonflower. It had not been stolen or forgotten. "
        "It had been ringing softly to guide the tired firefly home. Mira could have carried it back at once, "
        "but she chose to hang it on a low branch until morning, where its gentle sound could comfort every small creature in the grass. "
    )
    repeats = max(1, min(3, target_minutes // 3))
    ending = (
        "When Mira returned to her window, the bell chimed once from far across the meadow, quiet and clear. "
        "She understood that a promise is not broken when kindness makes it larger. Some things are found "
        "only when we stop clutching them for ourselves. She lay down beneath the moonlight, breathing slowly, while the meadow grew "
        "still around her. The bell rang softer and softer, until even its silver note became part of a dream."
    )
    return (base * repeats + ending).strip()
