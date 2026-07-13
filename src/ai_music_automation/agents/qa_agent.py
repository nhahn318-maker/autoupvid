from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .base import AgentContext, BaseAgent
from ..automation.artifacts import ImageArtifact, MetadataArtifact, StoryArtifact


@dataclass(frozen=True)
class QAInput:
    story: StoryArtifact
    images: list[ImageArtifact] = field(default_factory=list)
    audio_path: Path | None = None
    metadata: MetadataArtifact | None = None
    target_minutes: int = 3


@dataclass(frozen=True)
class QAResult:
    passed: bool
    score: float
    notes: list[str] = field(default_factory=list)


class QAAgent(BaseAgent[QAInput, QAResult]):
    name = "qa_agent"

    def execute(self, payload: QAInput, context: AgentContext) -> QAResult:
        score = 100.0
        notes: list[str] = []
        from .story_reviewer import content_gate_violations

        content_violations = content_gate_violations(payload.story.script)
        if content_violations:
            score -= min(70, len(content_violations) * 14)
            notes.extend(content_violations)
        if len(payload.story.script.split()) < max(120, payload.target_minutes * 80):
            score -= 18
            notes.append("Story is shorter than expected.")
        narrative_score, narrative_notes = narrative_arc_score(payload.story.script)
        if narrative_score < 4:
            score -= 25
            notes.extend(narrative_notes)
        elif narrative_score < 6:
            score -= 10
            notes.extend(narrative_notes)
        if not payload.images:
            score -= 25
            notes.append("No reviewed images are available.")
        if payload.audio_path and not payload.audio_path.exists():
            score -= 20
            notes.append("Voice file is missing.")
        if not payload.metadata or not payload.metadata.title:
            score -= 12
            notes.append("Metadata is incomplete.")
        for image in payload.images:
            if image.score is not None and image.score < float(context.settings.get("image_review_threshold") or 0.55):
                score -= 8
                notes.append(f"Image score is low for scene {image.scene_index}.")
            prompt_score, prompt_notes = image_prompt_specificity_score(image.prompt)
            if prompt_score < 3:
                score -= 6
                notes.append(f"Image prompt is too generic for scene {image.scene_index}: {'; '.join(prompt_notes)}")
        duplicate_pairs = near_duplicate_image_pairs(payload.images)
        if duplicate_pairs:
            score -= min(20, len(duplicate_pairs) * 5)
            notes.append(
                "Near-duplicate scene images detected: "
                + ", ".join(f"{left}/{right}" for left, right in duplicate_pairs[:5])
                + "."
            )
        threshold = float(context.settings.get("qa_threshold") or 75)
        score = max(0, min(100, score))
        if not notes:
            notes.append("QA passed.")
        return QAResult(passed=score >= threshold and not content_violations, score=score, notes=notes)


def narrative_arc_score(script: str) -> tuple[int, list[str]]:
    text = script or ""
    lowered = text.lower()
    score = 0
    notes: list[str] = []
    checks = [
        (r"\b[A-Z][a-z]{2,}\b", "Story may not have a named character."),
        (r"\b(village|meadow|forest|cabin|garden|room|window|valley|shore|path|hill|home)\b", "Story setting is unclear."),
        (r"\b(wanted|wished|needed|looked for|could not|wondered|missed|hoped|searched|promised|unfinished|lost|missing)\b", "Story lacks a small need or question."),
        (r"\b(found|met|followed|carried|gave|helped|opened|heard|noticed|returned|placed|shared|waited|left|offered|guided)\b", "Story lacks visible action progression."),
        (r"\b(understood|learned|realized|remembered|knew|decided|chose|discovered|saw that|found that)\b", "Story lacks a gentle choice or discovery."),
        (r"\b(kindness|promise|patience|forgive|forgiveness|share|sharing|let go|letting go|listen|listening|belong|belonging|rest is|not broken|larger than)\b", "Story lesson is not specific enough."),
        (r"\b(slept|sleep|dream|rested|safe|home|peaceful|still)\b", "Story resolution does not clearly lead into rest."),
    ]
    for pattern, note in checks:
        if re.search(pattern, text if pattern.startswith(r"\b[A-Z]") else lowered):
            score += 1
        else:
            notes.append(note)
    meditation_markers = len(re.findall(r"\b(breathe|breathing|relax|let go|imagine yourself|feel your)\b", lowered))
    action_markers = len(re.findall(r"\b(walked|found|met|followed|carried|gave|helped|returned|placed|opened|heard)\b", lowered))
    if meditation_markers >= 4 and action_markers < 3:
        score -= 2
        notes.append("Story reads more like guided relaxation than a narrative.")
    return max(0, score), notes


def image_prompt_specificity_score(prompt: str) -> tuple[int, list[str]]:
    text = prompt or ""
    lowered = text.lower()
    score = 0
    notes: list[str] = []
    if len(text.split()) >= 35:
        score += 1
    else:
        notes.append("prompt is short")
    if re.search(r"\b[A-Z][a-z]{2,}\b", text) or re.search(r"\b(fox|girl|boy|keeper|traveler|baker|clockmaker|child|animal)\b", lowered):
        score += 1
    else:
        notes.append("no clear character")
    if re.search(r"\b(holding|carrying|walking|looking|listening|opening|placing|giving|following|resting|repairing|watching|finding|meeting)\b", lowered):
        score += 1
    else:
        notes.append("no visible action")
    if re.search(r"\b(lantern|star|bell|clock|letter|key|book|window|candle|flower|gate|pocket|firefly|boat|map|blanket)\b", lowered):
        score += 1
    else:
        notes.append("no story object")
    if re.search(r"\b(valley|meadow|forest|cabin|cottage|village|garden|shore|lighthouse|bakery|library|workshop|river|moonlit|snowy)\b", lowered):
        score += 1
    else:
        notes.append("no concrete setting")
    generic_markers = len(re.findall(r"\b(dreamy|beautiful|peaceful|soft|calm|storybook|cinematic)\b", lowered))
    if generic_markers > 10:
        score -= 1
        notes.append("too many generic style words")
    return max(0, score), notes


def near_duplicate_image_pairs(images: list[ImageArtifact], max_distance: int = 5) -> list[tuple[int, int]]:
    hashes: list[tuple[int, int]] = []
    try:
        from PIL import Image
    except ImportError:
        return []
    for artifact in images:
        try:
            with Image.open(artifact.path) as image:
                sample = image.convert("L").resize((16, 16))
                pixels = list(sample.getdata())
        except (OSError, ValueError):
            continue
        average = sum(pixels) / max(1, len(pixels))
        value = 0
        for pixel in pixels:
            value = (value << 1) | int(pixel >= average)
        hashes.append((artifact.scene_index, value))
    duplicates: list[tuple[int, int]] = []
    for index, (left_scene, left_hash) in enumerate(hashes):
        for right_scene, right_hash in hashes[index + 1 :]:
            if (left_hash ^ right_hash).bit_count() <= max_distance:
                duplicates.append((left_scene, right_scene))
    return duplicates
