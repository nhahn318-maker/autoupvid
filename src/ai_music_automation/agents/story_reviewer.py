from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from dataclasses import dataclass
from typing import Any

from .base import AgentContext, BaseAgent
from ..automation.artifacts import StoryArtifact
from ..automation.model_client import ModelRequest, OllamaClient


@dataclass(frozen=True)
class StoryReview:
    score: float
    passed: bool
    notes: list[str]
    revised_script: str = ""
    valid_response: bool = True


class StoryReviewerAgent(BaseAgent[StoryArtifact, StoryReview]):
    name = "story_reviewer"

    def __init__(self, model_client: OllamaClient | None = None, max_retries: int = 0) -> None:
        super().__init__(max_retries=max_retries)
        self.model_client = model_client or OllamaClient()

    def execute(self, payload: StoryArtifact, context: AgentContext) -> StoryReview:
        threshold = float(context.settings.get("review_threshold") or 82)
        cache_key = None
        if context.cache:
            cache_key = context.cache.key_for(
                self.name,
                {
                    "niche": context.niche,
                    "title": payload.title,
                    "script": payload.script,
                    "threshold": threshold,
                    "model": context.settings.get("ollama_model") or context.settings.get("model"),
                    "prompt_version": context.settings.get("review_prompt_version") or 4,
                    "multi_judge_review": bool(context.settings.get("multi_judge_review", True)),
                    "multi_judge_min_words": int(context.settings.get("multi_judge_min_words") or 1600),
                    "hard_gate_version": 6,
                },
            )
            cached = context.cache.read_json(cache_key)
            if cached and float(cached.get("score") or 0) > 0 and not any(
                "valid json" in str(note).lower() for note in cached.get("notes", [])
            ):
                cached_review = StoryReview(
                    score=float(cached.get("score") or 0),
                    passed=bool(cached.get("passed")),
                    notes=[str(item) for item in cached.get("notes", [])],
                    revised_script=str(cached.get("revised_script") or ""),
                )
                cached_review = reconcile_anomalous_positive_review(payload, cached_review, threshold)
                cached_review = combine_with_content_gate(payload, cached_review, threshold)
                context.cache.write_json(
                    cache_key,
                    {
                        "score": cached_review.score,
                        "passed": cached_review.passed,
                        "notes": cached_review.notes,
                        "revised_script": cached_review.revised_script,
                    },
                )
                return cached_review

        if bool(context.settings.get("fast_review_if_heuristic_passes", True)):
            heuristic = heuristic_review(payload, threshold)
            fast_threshold = float(context.settings.get("fast_review_threshold") or max(threshold + 6, 90))
            if heuristic.score >= fast_threshold and heuristic.passed:
                review = StoryReview(
                    score=heuristic.score,
                    passed=True,
                    notes=["Fast heuristic review passed; skipped model review.", *heuristic.notes],
                    revised_script="",
                )
                if context.cache and cache_key:
                    context.cache.write_json(
                        cache_key,
                        {
                            "score": review.score,
                            "passed": review.passed,
                            "notes": review.notes,
                            "revised_script": review.revised_script,
                        },
                    )
                return combine_with_content_gate(payload, review, threshold)

        response = self.model_client.generate(
            ModelRequest(
                base_url=str(context.settings.get("ollama_url") or "http://127.0.0.1:11434"),
                model=str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b"),
                prompt=build_review_prompt(payload, threshold),
                temperature=float(context.settings.get("review_temperature") or 0.2),
                top_p=float(context.settings.get("review_top_p") or 0.8),
                timeout_seconds=int(context.settings.get("timeout_seconds") or 240),
                response_format="json",
                context_tokens=int(context.settings.get("review_context_tokens") or 8192),
            )
        )
        reviewer_fallback_used = False
        if not response or not response.strip():
            detail = self.model_client.last_error or "Ollama returned blank text."
            review = fallback_review(payload, threshold, f"Model reviewer unavailable: {detail}")
            reviewer_fallback_used = True
        else:
            review = parse_review_response(response, threshold)
            if not review.valid_response:
                review = fallback_review(payload, threshold, "Model reviewer returned malformed JSON.")
                reviewer_fallback_used = True

        if (
            not reviewer_fallback_used
            and bool(context.settings.get("multi_judge_review", True))
            and len(payload.script.split()) >= int(
                context.settings.get("multi_judge_min_words") or 1600
            )
        ):
            specialist_reviews: list[StoryReview] = []
            for focus in ("structure_retention", "psychology_sleep_visual"):
                specialist_response = self.model_client.generate(
                    ModelRequest(
                        base_url=str(context.settings.get("ollama_url") or "http://127.0.0.1:11434"),
                        model=str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b"),
                        prompt=build_specialist_review_prompt(payload, threshold, focus),
                        temperature=0.15,
                        top_p=0.75,
                        timeout_seconds=int(context.settings.get("timeout_seconds") or 240),
                        response_format="json",
                        context_tokens=int(context.settings.get("review_context_tokens") or 8192),
                    )
                )
                parsed = parse_review_response(specialist_response, threshold)
                if specialist_response and parsed.valid_response and parsed.score > 0:
                    specialist_reviews.append(parsed)
            if not specialist_reviews:
                review = StoryReview(
                    score=review.score,
                    passed=review.passed,
                    notes=[*review.notes, "Specialist reviewers were unavailable; kept the primary validated review."],
                    revised_script=review.revised_script,
                )
            else:
                all_reviews = [review, *specialist_reviews]
                combined_score = round(sum(item.score for item in all_reviews) / len(all_reviews), 1)
                combined_notes: list[str] = []
                for item in all_reviews:
                    combined_notes.extend(note for note in item.notes if note not in combined_notes)
                review = StoryReview(
                    score=combined_score,
                    passed=(
                        combined_score >= threshold
                        and sum(1 for item in all_reviews if item.passed) >= 2
                        and all(item.score >= threshold - 8 for item in all_reviews)
                    ),
                    notes=combined_notes[:16],
                    revised_script=review.revised_script,
                )

        review = reconcile_anomalous_positive_review(payload, review, threshold)
        review = combine_with_content_gate(payload, review, threshold)

        if context.cache and cache_key:
            context.cache.write_json(
                cache_key,
                {
                    "score": review.score,
                    "passed": review.passed,
                    "notes": review.notes,
                    "revised_script": review.revised_script,
                },
            )
        return review


def build_review_prompt(story: StoryArtifact, threshold: float) -> str:
    return f"""You are a strict but gentle editor for a bedtime story YouTube channel.

Review this narration script for:
Act as five independent judges, then combine the score:
1. Story Structure Judge: real narrative arc, continuity, cause/effect, no reset between scenes.
2. Retention Judge: first-30-second hook, curiosity thread, new discovery every few minutes.
3. Psychology Judge: concrete adult wound, memory, emotional payoff, no generic healing lecture.
4. Sleep Quality Judge: calm tone, low stakes, no panic, no harsh wording, adult sleep ending.
5. Visual Variety Judge: enough distinct set pieces, objects, actions, and imageable moments.

Target long-form retention structure:
description -> discovery -> small mystery -> discovery -> memory -> new room/place ->
new object -> another revelation -> kind choice -> sleep resolution.

Important:
- A script that only describes scenery, breathing, safety, moonlight, or relaxation is not enough.
- A guided meditation without a visible character journey must score below {threshold:g}.
- A poetic opening that does not speak to a tired adult listener's feeling must score below {threshold:g}.
- A story with only an abstract hurt, but no concrete memory such as a waiting cup, unsent letter,
  broken promise, or unspoken apology, must score below {threshold:g}.
- A story with no clear lesson, or only a generic lesson like "be peaceful", must score below {threshold:g}.
- A story where the main character does not make a meaningful gentle choice must score below {threshold:g}.
- A story that uses "little one", "my dear child", or reads like content for kids must score below {threshold:g}.
- A story that repeats mood words like quiet, silence, soft, gentle, moonlight, stillness, warm, or peaceful
  so often that it feels AI-written must score below {threshold:g}.
- The story can be very soft, but the listener should understand what happened.
- A story that has fewer than 6 distinct visual set pieces for a 20+ minute script must score below {threshold:g}.
- A story that ends once and then continues with more sleep/relaxation paragraphs must score below {threshold:g}.
- A story with an incomplete first sentence or dangling hook must score below {threshold:g}.
- A story that lacks the discovery -> mystery -> memory -> new object/revelation progression must score below {threshold:g}.

Do not rewrite the script. Diagnose it with short, specific notes. A separate Writer agent will perform any rewrite.

Return only JSON with this shape:
{{
  "score": 0-100,
  "passed": true or false,
  "subscores": {{
    "story_structure": 0-100,
    "retention": 0-100,
    "psychology": 0-100,
    "sleep_quality": 0-100,
    "visual_variety": 0-100
  }},
  "notes": ["short note"],
  "revised_script": ""
}}

Title: {story.title}

Script:
{story.script}
"""


def build_specialist_review_prompt(story: StoryArtifact, threshold: float, focus: str) -> str:
    if focus == "structure_retention":
        rubric = """Judge only narrative structure and listener retention.
Check causal continuity, a concrete goal or burden, discoveries every few minutes,
distinct locations and objects, no reset between scenes, no early ending, and a visible emotional payoff."""
    else:
        rubric = """Judge only adult psychology, sleep suitability, and visual variety.
Check for a concrete memory rather than generic healing language, an earned lesson shown through action,
calm low stakes, no child positioning, limited repeated mood words, and at least six drawable set pieces."""
    return f"""You are an independent specialist judge for an adult bedtime-story channel.

{rubric}

Be strict and do not rewrite the story. Return only JSON:
{{"score": 0-100, "passed": true or false, "notes": ["specific short evidence"], "revised_script": ""}}
Passing threshold: {threshold:g}

Title: {story.title}

Script:
{story.script}
"""


def parse_review_response(response: str, threshold: float) -> StoryReview:
    data = extract_json_object(response)
    if not data:
        return StoryReview(score=0, passed=False, notes=["Reviewer did not return valid JSON."], valid_response=False)
    score = bounded_score(data.get("score"))
    notes = data.get("notes")
    if not isinstance(notes, list):
        notes = [str(notes)] if notes else []
    revised_script = str(data.get("revised_script") or "").strip()
    passed = bool(data.get("passed")) and score >= threshold
    return StoryReview(
        score=score,
        passed=passed,
        notes=[str(item).strip() for item in notes if str(item).strip()],
        revised_script=revised_script,
        valid_response=True,
    )


def extract_json_object(value: str) -> dict[str, Any] | None:
    text = (value or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def bounded_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def combine_with_content_gate(story: StoryArtifact, review: StoryReview, threshold: float) -> StoryReview:
    violations = content_gate_violations(story.script)
    if not violations:
        return review
    deterministic_score = max(0.0, 100.0 - min(70.0, len(violations) * 12.0))
    combined_score = min(review.score, deterministic_score)
    notes = list(review.notes)
    for violation in violations:
        if violation not in notes:
            notes.append(violation)
    return StoryReview(
        score=combined_score,
        passed=False,
        notes=notes[:20],
        revised_script=review.revised_script,
        valid_response=review.valid_response,
    )


def reconcile_anomalous_positive_review(
    story: StoryArtifact,
    review: StoryReview,
    threshold: float,
) -> StoryReview:
    """Correct self-contradictory model reviews without lowering hard gates.

    Local LLM judges sometimes return a low numeric score while every written
    note says the story is excellent. This only upgrades strongly positive
    reviews when deterministic gates and heuristic checks also agree the story
    is solid.
    """
    if review.passed:
        return review
    if content_gate_violations(story.script):
        return review

    heuristic = heuristic_review(story, threshold)
    minimum_sanity_score = min(float(threshold), max(74.0, float(threshold) - 12.0))
    if heuristic.score < minimum_sanity_score:
        return review

    notes_text = " ".join(review.notes).lower()
    if has_negative_review_marker(notes_text):
        return review

    positive_markers = (
        "excellent", "strong", "successful", "smooth", "high", "clear",
        "distinct", "payoff", "causal", "progression", "variety", "resolved",
    )
    positive_hits = sum(1 for marker in positive_markers if marker in notes_text)
    if positive_hits < 4 or len(review.notes) < 4:
        return review

    adjusted_score = max(float(threshold), review.score)
    note = (
        "Adjusted anomalous low numeric score: written review notes were strongly positive "
        "and deterministic story gates passed."
    )
    notes = list(review.notes)
    if note not in notes:
        notes.append(note)
    return StoryReview(
        score=adjusted_score,
        passed=True,
        notes=notes,
        revised_script=review.revised_script,
        valid_response=review.valid_response,
    )


def has_negative_review_marker(notes_text: str) -> bool:
    text = str(notes_text or "").lower()
    text = re.sub(
        r"\b(?:successfully\s+)?avoid(?:s|ing)?\s+(?:\w+\s+){0,5}repetitive\b",
        "",
        text,
    )
    text = re.sub(
        r"\b(?:without|not|no|less|limited)\s+(?:\w+\s+){0,5}repetitive\b",
        "",
        text,
    )
    text = re.sub(
        r"\brepetition\s+of\s+(?:\w+\s+){0,5}(?:managed|handled)\s+well\b",
        "",
        text,
    )
    negative_markers = (
        "lack", "lacks", "missing", "weak", "thin", "repetitive", "too generic",
        "not enough", "no clear", "fails", "failed", "below", "insufficient",
        "problem", "issue", "needs", "should", "must improve", "does not",
    )
    return any(marker in text for marker in negative_markers)


def content_gate_violations(script: str) -> list[str]:
    """Deterministic hard gates that prompts and LLM judges cannot waive."""
    text = str(script or "").strip()
    lowered = text.lower()
    words = re.findall(r"[a-z']+", lowered)
    violations: list[str] = []
    if not words:
        return ["Hard gate: script is empty."]

    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", text) if item.strip()]
    normalized_sentences = [re.sub(r"[^a-z0-9 ]+", "", item.lower()).strip() for item in sentences]
    similar_pairs = 0
    for index, left in enumerate(normalized_sentences):
        if len(left.split()) < 10:
            continue
        for right in normalized_sentences[index + 1 :]:
            if len(right.split()) < 10:
                continue
            if SequenceMatcher(None, left, right).ratio() >= 0.82:
                similar_pairs += 1
                if similar_pairs >= 2:
                    break
        if similar_pairs >= 2:
            break
    if similar_pairs:
        violations.append(f"Hard gate: {similar_pairs} repeated or near-duplicate sentence pair(s) detected.")

    sleep_markers = list(
        re.finditer(
            r"\b(?:drift(?:ed|ing)?\s+(?:toward|into)\s+sleep|fell asleep|"
            r"sink(?:ing)?\s+into\s+(?:rest|sleep)|rest now|you may rest now|carried .*? toward sleep)\b",
            lowered,
        )
    )
    if any(match.start() < len(lowered) * 0.85 for match in sleep_markers):
        violations.append("Hard gate: a sleep ending appears before the final 15% of the story.")
    final_section = lowered[int(len(lowered) * 0.85) :]
    if not re.search(r"\b(rest now|you may rest|fell asleep|drifted into sleep|slept|safe to rest)\b", final_section):
        violations.append("Hard gate: the final 15% lacks one clear adult sleep resolution.")

    return violations


MEMORY_CUE_RE = re.compile(
    r"\b(remembered|memory|recollection|recalled|years ago|had once|used to|"
    r"that night|that day|that evening|childhood|once|long ago|earlier)\b",
    flags=re.I,
)
MEMORY_EVENT_RE = re.compile(
    r"\b(waited|said|wrote|sent|left|returned|saved|promised|apologized|called|"
    r"visited|shared|gave|received|heard|kept|worked|working|racing|pushed|watched|watching|stood|standing|"
    r"adjusted|forced|held|carried|lost|found|opened|closed|placed|offered|folded|"
    r"refused|forgot|remembered)\b",
    flags=re.I,
)
MYSTERY_CUE_RE = re.compile(
    r"\b(mystery|secret|strange|unfamiliar|wondered|question|why|appeared|"
    r"glowed|hesitated|yielding|unfolded|revealed|shimmered|pulsed|changed|"
    r"moved by itself|opened by itself|whispered|hummed|vanished)\b",
    flags=re.I,
)
PAYOFF_CUE_RE = re.compile(
    r"\b(realized|understood|revealed|discovered|learned|opened|changed|"
    r"returned|chose|decided|placed|offered|released|set down|gave|shared|"
    r"accepted|let go|no longer|became|transformed)\b",
    flags=re.I,
)
CONCRETE_STOPWORDS = {
    "about", "again", "against", "almost", "around", "because", "before", "behind",
    "being", "between", "beyond", "breath", "carried", "could", "deep", "every",
    "felt", "first", "from", "gentle", "heavy", "herself", "himself", "into",
    "itself", "light", "little", "moment", "morning", "night", "only", "other",
    "patient", "peace", "peaceful", "quiet", "really", "seemed", "silence",
    "slow", "soft", "still", "story", "their", "there", "these", "thing",
    "through", "toward", "under", "until", "voice", "warm", "where", "while",
    "world", "would", "years",
}


def has_concrete_memory_role(sentences: list[str]) -> bool:
    for sentence in sentences:
        if not MEMORY_CUE_RE.search(sentence):
            continue
        if not MEMORY_EVENT_RE.search(sentence):
            continue
        if len(salient_story_terms(sentence)) >= 2:
            return True
    return False


def magical_mystery_payoff_report(sentences: list[str]) -> dict[str, bool]:
    for index, sentence in enumerate(sentences):
        if not MYSTERY_CUE_RE.search(sentence):
            continue
        mystery_terms = salient_story_terms(sentence)
        later = sentences[index + 1 :]
        if not later:
            return {"has_mystery": True, "has_payoff": False}
        if mystery_terms:
            for term in mystery_terms:
                mentions = [
                    item for item in later
                    if re.search(rf"\b{re.escape(term)}s?\b", item, flags=re.I)
                ]
                if len(mentions) >= 1 and any(PAYOFF_CUE_RE.search(item) for item in mentions):
                    return {"has_mystery": True, "has_payoff": True}
        # Some stories use a setting-based mystery where the exact noun changes
        # from clue to payoff. Accept it when a later sentence clearly reveals a
        # discovery and repeats at least one concrete story term.
        later_with_payoff = [item for item in later if PAYOFF_CUE_RE.search(item)]
        if later_with_payoff and any(
            set(mystery_terms) & set(salient_story_terms(item))
            for item in later_with_payoff
        ):
            return {"has_mystery": True, "has_payoff": True}
        return {"has_mystery": True, "has_payoff": False}
    return {"has_mystery": False, "has_payoff": False}


def salient_story_terms(sentence: str) -> list[str]:
    words = [
        item.lower()
        for item in re.findall(r"\b[a-z][a-z'-]{3,}\b", sentence or "", flags=re.I)
    ]
    terms: list[str] = []
    for word in words:
        normalized = word.strip("'").rstrip(".,;:!?")
        if normalized.endswith("'s"):
            normalized = normalized[:-2]
        if normalized in CONCRETE_STOPWORDS:
            continue
        if normalized.endswith("ly") or normalized.endswith("ing") and normalized not in {"spring"}:
            continue
        if normalized not in terms:
            terms.append(normalized)
    return terms[:8]


def heuristic_review(story: StoryArtifact, threshold: float) -> StoryReview:
    words = story.script.split()
    notes: list[str] = []
    score = 88.0
    if len(words) < 120:
        score -= 20
        notes.append("Script is likely too short for a calming bedtime arc.")
    if len(words) < 1600:
        score -= 10
        notes.append("Script may be too short for a main YouTube sleep story.")
    if not strong_adult_hook(story.script):
        score -= 14
        notes.append("Opening hook is not direct enough for a tired adult listener.")
    if has_dangling_opening(story.script):
        score -= 18
        notes.append("Opening hook appears incomplete or dangling.")
    if not has_concrete_memory(story.script):
        score -= 12
        notes.append("Story lacks one concrete emotional memory behind the burden.")
    if re.search(r"\b(little one|my dear child|sleep now,\s*little one|children's content)\b", story.script, flags=re.I):
        score -= 20
        notes.append("Script contains child-positioning language.")
    repetition_penalty = repeated_mood_word_penalty(story.script)
    if repetition_penalty:
        score -= repetition_penalty
        notes.append("Mood words may be overused, making the story feel too AI-like.")
    if re.search(r"\b(kill|blood|scream|panic|terrified|horror|danger)\b", story.script, flags=re.I):
        score -= 25
        notes.append("Script contains words that may be too intense for bedtime.")
    narrative_score, narrative_notes = narrative_arc_score(story.script)
    if narrative_score < 4:
        score -= 28
        notes.extend(narrative_notes)
    elif narrative_score < 5:
        score -= 12
        notes.extend(narrative_notes)
    repeated = repeated_sentence_count(story.script)
    if repeated > 1:
        score -= min(18, repeated * 4)
        notes.append("Script may contain repeated sentences.")
    progression_score, progression_notes = longform_progression_score(story.script)
    if len(words) >= 2200 and progression_score < 7:
        score -= min(24, (7 - progression_score) * 5)
        notes.extend(progression_notes)
    if early_sleep_signoff_count(story.script) > 0:
        score -= 16
        notes.append("Script appears to close with a sleep sign-off before the final ending.")
    if not notes:
        notes.append("Heuristic review passed because model review was unavailable.")
    score = max(0, min(100, score))
    return StoryReview(score=score, passed=score >= threshold, notes=notes)


def fallback_review(story: StoryArtifact, threshold: float, reason: str) -> StoryReview:
    """Keep the pipeline moving when the local model breaks JSON formatting.

    The deterministic reviewer still enforces narrative and content gates. A low
    score therefore triggers the normal rewrite path instead of silently passing.
    """
    heuristic = heuristic_review(story, threshold)
    notes = [reason, "Used deterministic story-quality review instead.", *heuristic.notes]
    return StoryReview(
        score=heuristic.score,
        passed=heuristic.passed,
        notes=notes,
        revised_script="",
        valid_response=True,
    )


def repeated_sentence_count(script: str) -> int:
    sentences = [item.strip().lower() for item in re.split(r"(?<=[.!?])\s+", script) if item.strip()]
    return len(sentences) - len(set(sentences))


def narrative_arc_score(script: str) -> tuple[int, list[str]]:
    text = script or ""
    lowered = text.lower()
    score = 0
    notes: list[str] = []

    if re.search(r"\b[A-Z][a-z]{2,}\b", text):
        score += 1
    else:
        notes.append("Story may not have a named main character.")

    if re.search(r"\b(village|meadow|forest|cabin|garden|room|window|valley|shore|path|hill|home)\b", lowered):
        score += 1
    else:
        notes.append("Story setting is not clear enough.")

    if re.search(r"\b(wanted|wished|needed|looked for|could not|wondered|missed|hoped|searched|promised|unfinished|lost|missing)\b", lowered):
        score += 1
    else:
        notes.append("Story lacks a small wish, question, or emotional need.")

    if re.search(r"\b(found|met|followed|carried|gave|helped|opened|heard|noticed|returned|placed|shared|waited|left|offered|guided)\b", lowered):
        score += 1
    else:
        notes.append("Story lacks visible gentle action or event progression.")

    if re.search(r"\b(understood|learned|realized|remembered|knew|decided|chose|discovered|saw that|found that)\b", lowered):
        score += 1
    else:
        notes.append("Story lacks a gentle discovery, choice, or lesson.")

    if re.search(r"\b(kindness|promise|patience|forgive|forgiveness|share|sharing|let go|letting go|listen|listening|home|belong|belonging|rest is|not broken|larger than)\b", lowered):
        score += 1
    else:
        notes.append("Story lesson is not specific enough.")

    if re.search(r"\b(slept|sleep|dream|rested|safe|home|peaceful|still)\b", lowered):
        score += 1
    else:
        notes.append("Story resolution does not clearly lead into rest.")

    meditation_markers = len(re.findall(r"\b(breathe|breathing|relax|let go|imagine yourself|feel your)\b", lowered))
    action_markers = len(re.findall(r"\b(walked|found|met|followed|carried|gave|helped|returned|placed|opened|heard)\b", lowered))
    if meditation_markers >= 4 and action_markers < 3:
        score -= 2
        notes.append("Script reads more like guided relaxation than a story.")

    return max(0, score), notes


def has_dangling_opening(script: str) -> bool:
    first_paragraph = re.split(r"\n\s*\n", (script or "").strip(), maxsplit=1)[0].strip()
    if first_paragraph.endswith(","):
        return True
    first = first_sentence_text(script)
    if not first:
        return True
    if first.endswith(","):
        return True
    return bool(re.search(r"\b(and if|if you|because|while|when)\s*$", first, flags=re.I))


def first_sentence_text(script: str) -> str:
    match = re.search(r"(.{1,400}?[.!?])(?:\s|$)", (script or "").strip(), flags=re.S)
    return match.group(1).strip() if match else (script or "").strip()[:250]


def early_sleep_signoff_count(script: str) -> int:
    matches = list(re.finditer(r"\b(?:Rest now|You may rest now)[^.!?]*[.!?]", script or "", flags=re.I))
    if len(matches) <= 1:
        return 0
    return len(matches) - 1


def longform_progression_score(script: str) -> tuple[int, list[str]]:
    lowered = (script or "").lower()
    score = 0
    notes: list[str] = []
    checks = [
        ("description/setup", r"\b(lived|worked|kept|tended|sat within|stood in|there was|there lived)\b"),
        ("discovery", r"\b(discovered|noticed|found|heard|saw|appeared|glowed|opened)\b"),
        ("small mystery", r"\b(mystery|question|strange|unfamiliar|unknown|why|curiosity|wondered|secret|hesitated|yielding|unfolded|revealed)\b"),
        ("concrete memory", r"\b(memory|remembered|promise|letter|apology|window|chair|cup|conversation|not kept|mechanism|gears|clock|watch|weight|workshop|order|exhaustion)\b"),
        ("new room/place", r"\b(room|chamber|hall|garden|library|balcony|bridge|market|shore|greenhouse|railway|archive|valley|door|threshold|inn|clearing|mountain|workshop|path|bench)\b"),
        ("new object/object change", r"\b(book|key|letter|lantern|map|seed|bell|cup|compass|page|door|object|changed|transformed|folded|opened|clock|mechanism|gear|gears|weight|watch|pendulum)\b"),
        ("another revelation", r"\b(realized|understood|revealed|learned|recognized|saw that|discovered that)\b"),
        ("kind choice", r"\b(chose|decided|offered|gave|placed|released|folded|opened|shared|set down|let go)\b"),
        ("sleep resolution", r"\b(rest now|you may rest|sleep|slept|rested|safe|dream)\b"),
    ]
    for label, pattern in checks:
        if re.search(pattern, lowered):
            score += 1
        else:
            notes.append(f"Long-form progression is missing or weak: {label}.")
    set_piece_count = distinct_set_piece_count(lowered)
    if set_piece_count >= 6:
        score += 1
    else:
        notes.append(f"Only {set_piece_count} distinct set-piece keyword(s) detected; long videos need at least 6 visual changes.")
    return score, notes


def distinct_set_piece_count(lowered: str) -> int:
    places = {
        "room",
        "chamber",
        "hall",
        "garden",
        "library",
        "balcony",
        "bridge",
        "market",
        "shore",
        "greenhouse",
        "railway",
        "archive",
        "valley",
        "door",
        "threshold",
        "forest",
        "cabin",
        "tower",
        "window",
        "river",
        "sea",
        "path",
        "inn",
        "clearing",
        "mountain",
        "workshop",
        "bench",
        "hall",
    }
    return sum(1 for place in places if re.search(rf"\b{re.escape(place)}\b", lowered))


def strong_adult_hook(script: str) -> bool:
    opening = " ".join((script or "").split()[:70]).lower()
    return bool(
        re.search(r"\b(if you have|if you've|have you ever|if you are|if you have been carrying)\b", opening)
        and re.search(r"\b(heart|hurt|tired|carrying|rest|sleep|forgive|memory|worry|lonely|unspoken)\b", opening)
    )


def has_concrete_memory(script: str) -> bool:
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", script or "") if item.strip()]
    return has_concrete_memory_role(sentences)


def repeated_mood_word_penalty(script: str) -> int:
    words = re.findall(r"[a-z']+", (script or "").lower())
    if not words:
        return 0
    watched = {"quiet", "silence", "soft", "gentle", "moonlight", "stillness", "heavy", "warm", "peaceful"}
    total = sum(1 for word in words if word in watched)
    ratio = total / max(1, len(words))
    if total >= 55 or ratio > 0.045:
        return 10
    if total >= 35 or ratio > 0.035:
        return 5
    return 0
