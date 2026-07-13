from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .media import IMAGE_EXTENSIONS, Track, list_files, probe_duration_seconds, slugify
from .render import render_video
from .tts import generate_voice


ROOT_DIR = Path.cwd()
BASE_INPUT_DIR = ROOT_DIR / "data" / "input" / "story-before-sleep"
DEFAULT_PROMPTS_DIR = BASE_INPUT_DIR / "prompts"
DEFAULT_REFERENCES_DIR = BASE_INPUT_DIR / "references"
DEFAULT_IMAGES_DIR = BASE_INPUT_DIR / "images"
DEFAULT_GENERATED_DIR = BASE_INPUT_DIR / "generated"
DEFAULT_DRAFTS_DIR = BASE_INPUT_DIR / "drafts"
DEFAULT_RESEARCH_DIR = BASE_INPUT_DIR / "research"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "output"


def story_before_sleep_paths(config: dict[str, Any] | None = None) -> dict[str, Path]:
    settings = dict((config or {}).get("story_before_sleep") or {})
    return {
        "prompts": ROOT_DIR / str(settings.get("prompts_dir") or DEFAULT_PROMPTS_DIR),
        "references": ROOT_DIR / str(settings.get("references_dir") or DEFAULT_REFERENCES_DIR),
        "images": ROOT_DIR / str(settings.get("images_dir") or DEFAULT_IMAGES_DIR),
        "generated": ROOT_DIR / str(settings.get("generated_dir") or DEFAULT_GENERATED_DIR),
        "drafts": ROOT_DIR / str(settings.get("drafts_dir") or DEFAULT_DRAFTS_DIR),
        "output": ROOT_DIR / str(settings.get("output_dir") or DEFAULT_OUTPUT_DIR),
    }


def ensure_story_before_sleep_dirs(config: dict[str, Any] | None = None) -> dict[str, Path]:
    paths = story_before_sleep_paths(config)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def story_before_sleep_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    paths = ensure_story_before_sleep_dirs(config)
    settings = dict((config or {}).get("story_before_sleep") or {})
    generated_images = [
        path for path in paths["generated"].rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]
    reference_images = story_reference_images(paths)
    render_images = story_render_images(paths["images"])
    drafts = []
    for path in sorted(paths["drafts"].glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:12]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        drafts.append(
            {
                "id": data.get("id") or path.stem,
                "title": data.get("title") or path.stem,
                "video": data.get("video_name") or "",
                "audio": data.get("audio_name") or "",
                "markdown": data.get("markdown_name") or "",
                "created_at": data.get("created_at") or "",
            }
        )
    return {
        "enabled": True,
        "prompt_count": len(list_files(paths["prompts"], {".txt", ".md"})),
        "reference_count": len(reference_images),
        "image_count": len(render_images),
        "generated_count": len(generated_images),
        "image_provider": str(settings.get("image_provider") or "sd_webui"),
        "local_image_url": str(settings.get("local_image_url") or "http://127.0.0.1:7860"),
        "paths": {
            key: str(value.relative_to(ROOT_DIR)) if value.is_relative_to(ROOT_DIR) else str(value)
            for key, value in paths.items()
        },
        "drafts": drafts,
    }


def run_story_before_sleep_test(
    job: dict[str, Any],
    config: dict[str, Any],
    title: str = "",
    prompt: str = "",
    target_minutes: int = 3,
    voice: str = "",
    image_count: int | None = None,
    wait_for_images: bool = False,
) -> dict[str, Any]:
    paths = ensure_story_before_sleep_dirs(config)
    settings = dict(config.get("story_before_sleep") or {})
    target_minutes = max(1, min(30, int(target_minutes or settings.get("test_target_minutes") or 10)))
    title = clean_title(title or settings.get("default_title") or "A Gentle Story Before Sleep")
    user_prompt = prompt.strip()
    prompt = user_prompt or sanitize_legacy_sleep_prompt(latest_prompt_text(paths["prompts"]) or default_sleep_prompt())
    voice = voice.strip() or str(settings.get("voice") or "en-US-BrianNeural")
    voice_rate = str(settings.get("voice_rate") or "-8%")
    reference_style = reference_style_hint(paths, settings)

    log(job, "Building Story Before Sleep script.")
    script = generate_sleep_story_script(settings, title, prompt, target_minutes, reference_style)
    apply_story_visual_bible(settings, SimpleNamespace(title=title, script=script, lesson=""))
    scene_prompts = build_scene_prompts(settings, script, reference_style)
    generated_dir = prepare_generated_scene_prompts(paths, title, scene_prompts, reference_style)
    image_count = max(1, min(32, int(image_count or settings.get("image_count") or 8)))
    generate_local_story_images(job, settings, generated_dir, scene_prompts, image_count)
    ensure_fresh_story_images(settings, generated_dir, image_count)
    if wait_for_images:
        timeout_seconds = max(30, min(3600, int(settings.get("wait_for_generated_images_seconds") or 900)))
        wait_for_generated_images(job, generated_dir, image_count, timeout_seconds)
    images = choose_story_images(paths, title, scene_prompts, image_count, generated_dir)

    audio_title = f"story-before-sleep-{title}"
    log(job, f"Generating voice: {voice} {voice_rate}.")
    audio_path = generate_voice(script, audio_title, voice, paths["output"], rate=voice_rate)

    render_config = story_before_sleep_render_config(settings)
    apply_story_image_timing(render_config, settings, audio_path, len(images), job=job)
    ambient_effect = select_story_ambient_effect(
        settings,
        title=title,
        script=script,
        scene_prompts=scene_prompts,
    )
    if ambient_effect:
        render_config["ambient_overlay"] = ambient_effect
        log(job, f"Selected ambient effect: {ambient_effect.get('id') or ambient_effect.get('path')}.")
    background_ambience = select_story_background_ambience(
        settings,
        title=title,
        script=script,
        scene_prompts=scene_prompts,
    )
    if background_ambience:
        render_config.update(background_ambience)
        log(job, f"Selected background ambience: {background_ambience.get('background_ambience_id')}.")
    track = Track(audio_path=audio_path, image_paths=tuple(images), title=title)
    log(job, f"Rendering test video with {len(images)} image(s).")
    video_path = render_video(track, paths["output"], render_config, suffix="-sbs-test")

    draft_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(title, 50)}"
    markdown_path = paths["drafts"] / f"{draft_id}.md"
    json_path = paths["drafts"] / f"{draft_id}.json"
    markdown_path.write_text(
        build_markdown(title, prompt, script, scene_prompts, images, audio_path, video_path),
        encoding="utf-8",
    )
    draft = {
        "id": draft_id,
        "title": title,
        "prompt": prompt,
        "script": script,
        "scene_prompts": scene_prompts,
        "generated_dir": str(generated_dir),
        "images": [str(path) for path in images],
        "audio": str(audio_path),
        "audio_name": audio_path.name,
        "video": str(video_path),
        "video_name": video_path.name,
        "markdown": str(markdown_path),
        "markdown_name": markdown_path.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    json_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return draft


def clean_title(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value[:100] or "A Gentle Story Before Sleep"


def latest_prompt_text(prompts_dir: Path) -> str:
    prompts = sorted(
        list_files(prompts_dir, {".txt", ".md"}),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not prompts:
        return ""
    return prompts[0].read_text(encoding="utf-8-sig").strip()


def sleep_story_benchmark_text(settings: dict[str, Any] | None = None, max_chars: int = 3200) -> str:
    settings = settings or {}
    configured = str(settings.get("benchmark_prompt_path") or "").strip()
    path = ROOT_DIR / configured if configured else DEFAULT_RESEARCH_DIR / "sleepu_benchmark.md"
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    return text[:max_chars].strip()


def sanitize_legacy_sleep_prompt(prompt: str) -> str:
    cleaned = prompt or ""
    replacements = {
        "a small child sitting in a glowing meadow under a huge full moon, watching stars over a quiet valley": (
            "a story-specific main character in a moonlit, dreamy bedtime world"
        ),
        "The child notices fireflies, flowers, clouds, distant warm village lights, and the moon as a calm friend.": (
            "Use gentle moonlit details only when they naturally fit the current story."
        ),
        "soft yellow dress, ": "",
        "yellow dress, ": "",
        "small child": "story-specific main character",
        "The child": "The main character",
        "the child": "the main character",
    }
    for old, new in replacements.items():
        cleaned = cleaned.replace(old, new)
    cleaned += (
        "\n\nImportant visual constraint: do not reuse the old reference character, gender, face, pose, outfit, "
        "yellow dress, or child-under-moon composition. The character identity must come only from the current story."
    )
    return cleaned.strip()


def reference_style_hint(paths: dict[str, Path], settings: dict[str, Any]) -> str:
    style_parts: list[str] = []
    if bool(settings.get("use_reference_style", False)):
        for folder in (paths["references"], paths["images"]):
            style_file = folder / "style_art.txt"
            if style_file.exists():
                text = style_file.read_text(encoding="utf-8-sig").strip()
                if text:
                    style_parts.append(text)
                    break
    configured_style = str(settings.get("art_style") or "").strip()
    if configured_style:
        style_parts.append(configured_style)

    if bool(settings.get("use_reference_image_palette", False)):
        image_pool = story_reference_images(paths)
        image_hint = infer_image_palette_hint(image_pool[:3])
        if image_hint:
            style_parts.append(image_hint)

    if not style_parts:
        return ""
    return " ".join(dict.fromkeys(part.strip() for part in style_parts if part.strip()))


def infer_image_palette_hint(images: list[Path]) -> str:
    if not images:
        return ""
    samples: list[tuple[int, int, int]] = []
    widths: list[int] = []
    heights: list[int] = []
    for path in images:
        try:
            with Image.open(path) as image:
                image = image.convert("RGB")
                widths.append(image.width)
                heights.append(image.height)
                thumb = image.resize((32, 32))
                samples.extend(list(thumb.getdata()))
        except Exception:
            continue
    if not samples:
        return ""
    avg = tuple(sum(pixel[i] for pixel in samples) / len(samples) for i in range(3))
    brightness = sum(avg) / 3
    blue_bias = avg[2] - max(avg[0], avg[1])
    warm_bias = avg[0] + avg[1] - (avg[2] * 1.6)
    mood = "dark moonlit nighttime palette" if brightness < 95 else "soft luminous palette"
    if blue_bias > 15:
        temperature = "deep blue celestial tones"
    elif warm_bias > 45:
        temperature = "warm golden bedtime tones"
    else:
        temperature = "balanced dreamy colors"
    aspect = ""
    if widths and heights:
        ratio = sum(widths) / max(1, sum(heights))
        if ratio > 1.45:
            aspect = "wide cinematic 16:9 compositions"
        elif ratio < 0.85:
            aspect = "vertical storybook compositions"
    return (
        f"Reference image analysis: {mood}, {temperature}, {aspect}, "
        "soft glow, gentle contrast, cohesive image-to-image consistency."
    )


def story_reference_images(paths: dict[str, Path]) -> list[Path]:
    reference_names = ("reference", "style", "art")
    references = list_files(paths["references"], IMAGE_EXTENSIONS)
    image_refs = [
        path for path in list_files(paths["images"], IMAGE_EXTENSIONS)
        if any(token in path.stem.lower() for token in reference_names)
    ]
    return references + image_refs


def story_render_images(image_dir: Path) -> list[Path]:
    blocked_names = ("reference", "style")
    return [
        path for path in list_files(image_dir, IMAGE_EXTENSIONS)
        if not any(token in path.stem.lower() for token in blocked_names)
    ]


def default_sleep_prompt() -> str:
    return (
        "Write a slow, gentle bedtime story for tired adults. The story should feel calm, cinematic, "
        "safe, and slightly magical, with emotional healing and a small mystery. Open with a direct "
        "adult emotional hook, then tell a real story with a named character, a concrete quiet hurt, "
        "one specific memory, a symbolic object, a gentle choice, and a clear lesson shown through action. "
        "Avoid horror, loud conflict, childish framing, and generic atmosphere-only writing. End with a "
        "peaceful image that helps the listener rest."
    )


def generate_sleep_story_script(
    settings: dict[str, Any],
    title: str,
    prompt: str,
    target_minutes: int,
    reference_style: str = "",
) -> str:
    provider = str(settings.get("provider") or "ollama").strip().lower()
    if provider == "ollama":
        response = call_ollama(
            base_url=str(settings.get("ollama_url") or "http://127.0.0.1:11434"),
            model=str(settings.get("ollama_model") or settings.get("model") or "gemma4:e2b"),
            prompt=build_script_prompt(title, prompt, target_minutes, reference_style),
            timeout=int(settings.get("timeout_seconds") or 240),
        )
        script = polish_adult_sleep_story_script(clean_script(response))
        if len(script.split()) >= 80:
            return trim_script_to_target(script, target_minutes)
    return fallback_sleep_story(title, target_minutes)


def build_script_prompt(title: str, prompt: str, target_minutes: int, reference_style: str = "") -> str:
    target_words = story_target_word_count(target_minutes)
    benchmark = sleep_story_benchmark_text()
    return f"""You write calm English bedtime stories for a YouTube channel called Story Before Sleep.

Create one complete narration script.

Title: {title}
Target length: about {target_words} words.

Creative direction:
{prompt}

Sleepu Stories benchmark to follow as reusable standards, not copied content:
{benchmark or "Use adult sleep-story best practices: direct emotional hook, concrete memory, small magical mystery, calm micro-journey, adult sleep ending."}

Reference art style to extract:
{reference_style or "Use a soft, cohesive bedtime-story illustration style."}

Rules:
- Audience: tired adults, not children. Keep the illustrated fairytale feeling, but make the emotional logic mature.
- The first 30 seconds must give the listener a reason to stay.
  Start with one direct emotional promise like "If you have been carrying..." or "Have you ever...".
  Then introduce the named character, the symbolic object, and the small hurt or unfinished promise.
- The opening should quickly imply why the object is special, what the character is carrying,
  and what feeling the listener may receive after the story.
- Use gentle, slow, sensory language.
- Keep the story safe for sleep: no violence, no panic, no loud tension.
- Write an actual story with a clear beginning, middle, turning point, and ending.
- Do not write a guided meditation, an atmosphere piece, or a list of pretty images.
- Include a named main character, a peaceful setting, a specific emotional need or promise, a gentle event,
  a meaningful choice, a consequence of that choice, and a soft resolution.
- Give the emotional need one concrete memory: an unsent letter, a saved cup, a night waiting beside a window,
  an apology never spoken, or a promise that was not kept. Keep it tender, not tragic.
- Include a small magical mystery around the symbolic object or setting. It should create curiosity without urgency.
- Give the story one memorable lesson. The lesson must come from what the character does, not from a lecture.
- Make the character change in a small but visible way by the end.
- Use visible scenes, not only abstract feelings: the room, the garden or street, the object, the character's hands,
  the ritual of tea or light or walking, the small choice, and the final rest.
- Include one quiet moment of curiosity in the first paragraph: something missing, promised, unfinished,
  misunderstood, or softly mysterious.
- Include one gentle helper or obstacle: an old lantern, a tired animal, a closed gate, a forgotten letter,
  a quiet neighbor, a fading star, or another soft story object.
- In the final third, let the character make a kind choice that costs something small:
  time, comfort, pride, certainty, or the wish to keep something for themselves.
- End with a clear emotional takeaway, phrased naturally inside the narration.
- Show emotional healing through action instead of explaining it. For example: "She did not say the word forgive.
  She only loosened her fingers around the memory, and somehow, that was enough."
- Avoid repeating the same mood words too often, especially: quiet, silence, soft, gentle, moonlight,
  stillness, heavy, warm, peaceful. Use concrete sensory details instead.
- Maintain one consistent art direction suitable for dreamy illustrated images.
- Reference images are for art style only: color palette, lighting, brushwork, mood, softness, and visual texture.
- Do not copy the reference image composition, exact character, face, pose, or scene.
- Do not use child-positioning phrases such as "little one", "my dear child", or "sleep now, little one".
- Avoid "children's-book" wording in narration. This channel is for adults.
- Do not include headings, markdown, scene labels, timestamps, or bullet points.
- Start directly with narration.
- End softly for an adult listener, with wording like "You may rest now, safe in the quiet light."
"""


def call_ollama(base_url: str, model: str, prompt: str, timeout: int = 240) -> str:
    endpoint = base_url.rstrip("/") + "/api/generate"
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.75, "top_p": 0.92},
        }
    ).encode("utf-8")
    request = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
            return str(data.get("response") or "")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return ""


def story_target_word_count(target_minutes: int) -> int:
    minutes = max(1, int(target_minutes or 3))
    if minutes <= 5:
        return max(650, min(1600, minutes * 220))
    return max(1800, min(8000, minutes * 240))


def clean_script(text: str) -> str:
    text = re.sub(r"```.*?```", "", text or "", flags=re.S)
    text = re.sub(r"(?im)^\s*(title|script|narration|scene)\s*:.*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
    return polished.strip()


def trim_script_to_target(script: str, target_minutes: int) -> str:
    max_words = max(900, min(8500, int(max(1, target_minutes) * 285)))
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


def fallback_sleep_story(title: str, target_minutes: int) -> str:
    base = (
        f"If you have been carrying a small unfinished ache in your heart, {title.lower()} is a story about "
        "letting it loosen before sleep. In a village of rain-dark roofs, a young woman named Mira kept a blue "
        "teacup on the windowsill. It was the cup she had saved for someone who once promised to return for tea, "
        "then never came back and never sent the letter she waited for. One evening, as rain tapped the glass, "
        "the empty cup began to hold a tiny reflection of a road that was not outside her window. Mira wrapped a "
        "shawl around her shoulders and carried the cup through the sleeping lane, following the reflected road "
        "past closed shops, wet stones, and one lamp that kept glowing after every other lamp had gone dark. "
        "At the end of the lane, she found a folded note resting beneath the lamp. It did not explain everything. "
        "It only said, I am sorry I left you waiting. Mira read it once, then placed it inside the cup, not to keep "
        "the hurt alive, but to give it somewhere small and kind to rest. She did not say the word forgive. She only "
        "loosened her fingers around the memory, and somehow, that was enough. "
    )
    repeats = max(1, min(4, target_minutes // 3))
    ending = (
        "When Mira returned home, she set the cup by the window and poured fresh tea into it. The steam rose in "
        "one pale ribbon, then vanished into the room like a breath finally released. The promise had not become "
        "what she once hoped, but it no longer had to press against her chest. She lay down while the rain softened "
        "on the roof, feeling the night make room around her. You may rest now, safe in the quiet light."
    )
    return (base * repeats + ending).strip()


def build_scene_prompts(settings: dict[str, Any], script: str, reference_style: str = "") -> list[str]:
    raw_style = reference_style.strip() or sleep_story_art_style_prompt(settings) or str(settings.get("art_style") or "").strip() or (
        "dreamy storybook illustration, soft moonlight, warm window glow, painterly texture, "
        "cozy cinematic composition, calm bedtime mood, consistent character design"
    )
    style = (
        f"{raw_style}. Use the reference only for style: color palette, lighting, brushwork, "
        "softness, mood, and texture. Do not copy the same composition, exact character, face, pose, or scene."
    )
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", script) if part.strip()]
    selected = paragraphs[:10] if paragraphs else [script[:280]]
    prompts = []
    for index, paragraph in enumerate(selected, start=1):
        short = paragraph[:260].replace("\n", " ")
        prompts.append(f"Scene {index}: {style} Visualize a new scene from the story: {short}")
    return prompts[:10]


def prepare_generated_scene_prompts(
    paths: dict[str, Path],
    title: str,
    scene_prompts: list[str],
    reference_style: str,
) -> Path:
    slug = slugify(title, 48)
    generated_dir = paths["generated"] / slug
    generated_dir.mkdir(parents=True, exist_ok=True)
    fresh_marker = generated_dir / ".fresh_images_started"
    if fresh_marker.exists():
        try:
            fresh_marker.unlink()
        except OSError:
            pass
    payload = {
        "title": title,
        "style_prompt": build_image_style_prompt(reference_style),
        "scene_prompts": scene_prompts,
        "note": (
            "Generate one new image per scene prompt in this folder. "
            "Reference images are style-only inputs; do not copy their exact composition, character, face, or pose."
        ),
    }
    (generated_dir / "scene-prompts.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        f"# {title}",
        "",
        "Use the reference art only for style consistency. Create new images for these scenes.",
        "Do not copy the same composition, exact character, face, pose, or scene from the reference.",
        "",
        "## Style Prompt",
        build_image_style_prompt(reference_style),
        "",
        "## Scene Prompts",
    ]
    lines.extend(f"{index}. {prompt}" for index, prompt in enumerate(scene_prompts, start=1))
    (generated_dir / "scene-prompts.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (generated_dir / "style-prompt.txt").write_text(build_image_style_prompt(reference_style) + "\n", encoding="utf-8")
    return generated_dir


def wait_for_generated_images(job: dict[str, Any], generated_dir: Path, count: int, timeout_seconds: int) -> None:
    existing = list_files(generated_dir, IMAGE_EXTENSIONS)
    if len(existing) >= count:
        return
    log(
        job,
        f"Waiting for {count} generated image(s) in {generated_dir}. "
        f"Current: {len(existing)}. Timeout: {timeout_seconds}s.",
    )
    deadline = time.monotonic() + timeout_seconds
    last_count = len(existing)
    while time.monotonic() < deadline:
        time.sleep(5)
        current = list_files(generated_dir, IMAGE_EXTENSIONS)
        if len(current) != last_count:
            last_count = len(current)
            log(job, f"Generated images detected: {last_count}/{count}.")
        if len(current) >= count:
            return
    raise RuntimeError(
        f"Timed out waiting for {count} generated image(s). "
        f"Open {generated_dir} and save scene images there, then run again."
    )


def generate_local_story_images(
    job: dict[str, Any],
    settings: dict[str, Any],
    generated_dir: Path,
    scene_prompts: list[str],
    count: int,
) -> list[Path]:
    provider = str(settings.get("image_provider") or "sd_webui").strip().lower()
    if bool(settings.get("force_new_story_images", True)) and not generated_dir.joinpath(".fresh_images_started").exists():
        archived = archive_existing_story_images(generated_dir)
        if archived:
            log(job, f"Archived {archived} old scene image(s) so this story gets fresh visuals.")
        generated_dir.joinpath(".fresh_images_started").write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    if provider in {"reference", "reference_fallback", "reference_art"}:
        log(job, "Generating reference-art fallback scene images.")
        return generate_reference_scene_images(
            settings=settings,
            generated_dir=generated_dir,
            scene_prompts=scene_prompts or ["dreamy bedtime storybook illustration"],
            count=count,
            start_index=1,
        )
    if provider in {"none", "manual", "folder"}:
        return []
    existing = sorted(
        [path for path in list_files(generated_dir, IMAGE_EXTENSIONS) if not is_placeholder_image(path)],
        key=lambda item: item.name.lower(),
    )
    if len(existing) >= count:
        log(job, f"Using {len(existing)} existing generated image(s).")
        return existing[:count]
    if provider in {"comfyui", "comfy"}:
        base_url = str(settings.get("comfyui_url") or "http://127.0.0.1:8188").rstrip("/")
        if not comfyui_available(base_url):
            if bool(settings.get("comfyui_auto_start", True)):
                log(job, f"ComfyUI API is not available at {base_url}. Starting local ComfyUI.")
                start_comfyui_server(settings, job)
                wait_seconds = int(settings.get("comfyui_startup_timeout_seconds") or 120)
                if not wait_for_comfyui(base_url, wait_seconds):
                    log(job, f"ComfyUI did not become ready within {wait_seconds}s. Falling back to current image backend.")
                    fallback_settings = dict(settings)
                    fallback_settings["image_provider"] = str(settings.get("comfyui_fallback_provider") or "imagegen_local")
                    return generate_local_story_images(job, fallback_settings, generated_dir, scene_prompts, count)
            else:
                log(job, f"ComfyUI API is not available at {base_url}. Falling back to current image backend.")
                fallback_settings = dict(settings)
                fallback_settings["image_provider"] = str(settings.get("comfyui_fallback_provider") or "imagegen_local")
                return generate_local_story_images(job, fallback_settings, generated_dir, scene_prompts, count)

        needed = count - len(existing)
        candidates = max(1, min(4, int(settings.get("comfyui_candidates_per_scene") or 1)))
        log(job, f"Generating {needed} scene image(s) with ComfyUI at {base_url}.")
        generated: list[Path] = []
        start_index = len(existing) + 1
        prompts = scene_prompts or ["dreamy bedtime storybook illustration"]
        from .semantic_image import ClipScorerProcess

        clip_scorer = ClipScorerProcess(settings, ROOT_DIR)
        clip_ready = clip_scorer.start()
        if clip_ready:
            log(job, "CLIP semantic image reviewer is ready for candidate selection.")
        elif bool(settings.get("clip_review_enabled", False)):
            log(job, "CLIP reviewer unavailable; using technical image scoring for this run.")
        for offset in range(needed):
            index = start_index + offset
            prompt = compact_story_image_prompt(prompts[(index - 1) % len(prompts)], settings)
            output_path = generated_dir / f"scene-{index:02}.png"
            best_path: Path | None = None
            candidate_paths: list[Path] = []
            for candidate in range(candidates):
                candidate_path = output_path if candidates == 1 else generated_dir / f"scene-{index:02}-candidate-{candidate + 1}.png"
                try:
                    log(job, f"START comfyui scene {index}/{start_index + needed - 1} candidate {candidate + 1}/{candidates}.")
                    comfyui_txt2img(base_url, prompt, candidate_path, settings, seed_offset=(index - 1) * 100 + candidate)
                    log(job, f"END comfyui scene {index}/{start_index + needed - 1} candidate {candidate + 1}/{candidates}: {candidate_path.name}")
                except Exception as exc:
                    log(job, f"ComfyUI image generation failed for scene {index} candidate {candidate + 1}: {exc}")
                    fallback_settings = comfyui_fallback_workflow_settings(settings)
                    if fallback_settings:
                        try:
                            log(job, f"RETRY comfyui scene {index}/{start_index + needed - 1} with fallback workflow.")
                            comfyui_txt2img(base_url, prompt, candidate_path, fallback_settings, seed_offset=(index - 1) * 100 + candidate)
                            log(job, f"END comfyui fallback scene {index}/{start_index + needed - 1}: {candidate_path.name}")
                        except Exception as fallback_exc:
                            log(job, f"ComfyUI fallback workflow failed for scene {index}: {fallback_exc}")
                            continue
                    else:
                        continue
                candidate_paths.append(candidate_path)
            if candidate_paths:
                best_path = choose_best_image_candidate(
                    candidate_paths,
                    prompt=prompt,
                    semantic_scorer=clip_scorer if clip_ready else None,
                )
                if candidates > 1:
                    log(job, f"Selected best candidate for scene {index}: {best_path.name}.")
            if best_path is None:
                if bool(settings.get("reference_image_fallback", True)):
                    log(job, "Using reference-art fallback images because ComfyUI did not return images.")
                    fallback = generate_reference_scene_images(
                        settings=settings,
                        generated_dir=generated_dir,
                        scene_prompts=prompts,
                        count=count,
                        start_index=index,
                    )
                    clip_scorer.close()
                    return existing + generated + fallback
                break
            if best_path != output_path:
                output_path.write_bytes(best_path.read_bytes())
            normalize_story_scene_image(output_path, settings)
            generated.append(output_path)
        clip_scorer.close()
        return existing + generated

    if provider in {"imagegen_local", "local_diffusers", "diffusers"}:
        needed = count - len(existing)
        log(job, f"Generating {needed} scene image(s) with local Diffusers imagegen.")
        generated: list[Path] = []
        start_index = len(existing) + 1
        prompts = scene_prompts or ["dreamy bedtime storybook illustration"]
        for offset in range(needed):
            index = start_index + offset
            prompt = compact_stable_diffusion_prompt(prompts[(index - 1) % len(prompts)], settings)
            output_path = generated_dir / f"scene-{index:02}.png"
            try:
                log(job, f"START imagegen_local scene {index}/{start_index + needed - 1}: {output_path.name}")
                imagegen_local_txt2img(prompt, output_path, settings, seed_offset=index - 1)
            except Exception as exc:
                log(job, f"Local Diffusers image generation failed for scene {index}: {exc}")
                try:
                    log(job, f"RETRY imagegen_local scene {index}/{start_index + needed - 1} with stable low settings.")
                    retry_settings = imagegen_retry_settings(settings)
                    imagegen_local_txt2img(prompt, output_path, retry_settings, seed_offset=index - 1)
                except Exception as retry_exc:
                    log(job, f"Local Diffusers retry failed for scene {index}: {retry_exc}")
                    if bool(settings.get("reference_image_fallback", True)):
                        log(job, "Using reference-art fallback images because local imagegen is not available.")
                        fallback = generate_reference_scene_images(
                            settings=settings,
                            generated_dir=generated_dir,
                            scene_prompts=prompts,
                            count=count,
                            start_index=index,
                        )
                        return existing + generated + fallback
                    break
                else:
                    normalize_story_scene_image(output_path, settings)
                    generated.append(output_path)
                    log(job, f"END imagegen_local scene {index}/{start_index + needed - 1}: {output_path.name}")
                    continue
            if output_path.exists():
                normalize_story_scene_image(output_path, settings)
                generated.append(output_path)
                log(job, f"END imagegen_local scene {index}/{start_index + needed - 1}: {output_path.name}")
            else:
                if bool(settings.get("reference_image_fallback", True)):
                    log(job, "Using reference-art fallback images because local imagegen is not available.")
                    fallback = generate_reference_scene_images(
                        settings=settings,
                        generated_dir=generated_dir,
                        scene_prompts=prompts,
                        count=count,
                        start_index=index,
                    )
                    return existing + generated + fallback
                break
        return existing + generated

    if provider not in {"sd_webui", "automatic1111", "a1111", "auto"}:
        log(job, f"Skip local image generation: unsupported provider '{provider}'.")
        return []

    base_url = str(settings.get("local_image_url") or "http://127.0.0.1:7860").rstrip("/")
    if not sd_webui_available(base_url):
        log(job, f"Local image AI not available at {base_url}. Start it with API enabled, then run again.")
        return []

    needed = count - len(existing)
    log(job, f"Generating {needed} scene image(s) with local SD WebUI at {base_url}.")
    generated: list[Path] = []
    start_index = len(existing) + 1
    prompts = scene_prompts or ["dreamy bedtime storybook illustration"]
    for offset in range(needed):
        index = start_index + offset
        prompt = compact_stable_diffusion_prompt(prompts[(index - 1) % len(prompts)], settings)
        output_path = generated_dir / f"scene-{index:02}.png"
        try:
            sd_webui_txt2img(base_url, prompt, output_path, settings)
        except Exception as exc:
            log(job, f"Local image generation failed for scene {index}: {exc}")
            break
        normalize_story_scene_image(output_path, settings)
        generated.append(output_path)
        log(job, f"Generated local image {output_path.name}.")
    return existing + generated


def archive_existing_story_images(generated_dir: Path) -> int:
    if not generated_dir.exists():
        return 0
    candidates = [
        path
        for path in list_files(generated_dir, IMAGE_EXTENSIONS)
        if path.parent == generated_dir and not is_placeholder_image(path)
    ]
    if not candidates:
        return 0
    archive_dir = generated_dir / "_old_images" / datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in candidates:
        target = archive_dir / path.name
        try:
            path.replace(target)
        except Exception:
            continue
        moved += 1
    return moved


def generate_story_thumbnail(
    job: dict[str, Any],
    settings: dict[str, Any],
    paths: dict[str, Path],
    story: Any,
    thumbnail_prompt: str,
) -> Path | None:
    if not bool(settings.get("thumbnail_generate", True)):
        return None
    thumbnails_dir = paths["drafts"] / "thumbnails"
    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    output_path = thumbnails_dir / f"{slugify(str(getattr(story, 'title', '') or 'sleepu-story'), 60)}-thumbnail.jpg"
    if output_path.exists() and output_path.stat().st_size > 10_000:
        log(job, f"Using existing thumbnail: {output_path.name}.")
        return output_path

    prompt = build_story_thumbnail_image_prompt(settings, story, thumbnail_prompt)
    provider = str(settings.get("thumbnail_provider") or settings.get("image_provider") or "comfyui").strip().lower()
    working_png = output_path.with_suffix(".png")
    try:
        if provider in {"comfyui", "comfy"}:
            base_url = str(settings.get("comfyui_url") or "http://127.0.0.1:8188").rstrip("/")
            if not comfyui_available(base_url):
                start_comfyui_server(settings, job)
                wait_for_comfyui(base_url, int(settings.get("comfyui_startup_timeout_seconds") or 120))
            thumb_settings = dict(settings)
            thumb_settings["comfyui_width"] = int(settings.get("thumbnail_width") or 1280)
            thumb_settings["comfyui_height"] = int(settings.get("thumbnail_height") or 720)
            thumb_settings["comfyui_steps"] = int(settings.get("thumbnail_steps") or settings.get("comfyui_steps") or 16)
            thumb_settings["comfyui_cfg"] = float(settings.get("thumbnail_cfg") or settings.get("comfyui_cfg") or 6.0)
            log(job, f"START thumbnail ComfyUI: {output_path.name}")
            comfyui_txt2img(base_url, prompt, working_png, thumb_settings, seed_offset=777)
        else:
            thumb_settings = dict(settings)
            thumb_settings["local_imagegen_width"] = int(settings.get("thumbnail_width") or 1280)
            thumb_settings["local_imagegen_height"] = int(settings.get("thumbnail_height") or 720)
            thumb_settings["local_imagegen_steps"] = int(settings.get("thumbnail_steps") or settings.get("local_imagegen_steps") or 8)
            log(job, f"START thumbnail local imagegen: {output_path.name}")
            imagegen_local_txt2img(prompt, working_png, thumb_settings, seed_offset=777)
    except Exception as exc:
        log(job, f"Thumbnail image generation failed: {exc}")
        fallback = make_story_thumbnail_from_existing_scene(paths, story, output_path, settings)
        if fallback:
            log(job, f"END thumbnail fallback from scene image: {output_path.name}")
        return fallback

    if not working_png.exists():
        fallback = make_story_thumbnail_from_existing_scene(paths, story, output_path, settings)
        if fallback:
            log(job, f"END thumbnail fallback from scene image: {output_path.name}")
        return fallback
    make_sleep_story_thumbnail_card(
        source_path=working_png,
        output_path=output_path,
        title=str(getattr(story, "title", "") or "Sleepu Stories"),
        settings=settings,
    )
    try:
        working_png.unlink(missing_ok=True)
    except Exception:
        pass
    log(job, f"END thumbnail: {output_path.name}")
    return output_path if output_path.exists() else None


def make_story_thumbnail_from_existing_scene(
    paths: dict[str, Path],
    story: Any,
    output_path: Path,
    settings: dict[str, Any],
) -> Path | None:
    """Build a usable thumbnail without loading the image model a second time."""
    title = str(getattr(story, "title", "") or "Sleepu Stories")
    generated_dir = paths.get("generated", Path()) / slugify(title, 48)
    candidates = sorted(
        (
            path
            for path in generated_dir.glob("scene-*.png")
            if path.is_file() and "candidate" not in path.stem.lower() and "placeholder" not in path.stem.lower()
        ),
        key=lambda path: path.name.lower(),
    )
    for source_path in candidates:
        try:
            make_sleep_story_thumbnail_card(
                source_path=source_path,
                output_path=output_path,
                title=title,
                settings=settings,
            )
        except (OSError, ValueError):
            continue
        if output_path.exists() and output_path.stat().st_size > 10_000:
            return output_path
    return None


def build_story_thumbnail_image_prompt(settings: dict[str, Any], story: Any, thumbnail_prompt: str) -> str:
    title = str(getattr(story, "title", "") or "").strip()
    hook = str(getattr(story, "hook", "") or "").strip()
    lesson = str(getattr(story, "lesson", "") or "").strip()
    base = thumbnail_prompt.strip() or (
        f"Dreamy fairytale bedtime story thumbnail for {title}, single strong focal subject, "
        "soft moonlight, warm lantern glow, emotional curiosity"
    )
    art_style = sleep_story_art_style_prompt(settings)
    style = str(settings.get("thumbnail_style") or (
        "premium YouTube thumbnail, storybook fairytale illustration, cinematic 16:9, clear central focal point, "
        "soft blue and gold lighting, cozy magical bedtime mood, clean background space for large title text"
    ))
    return (
        f"{base}. {art_style}. {style}. Title context: {title}. Hook: {hook}. Lesson: {lesson}. "
        "Use one main subject only, strong silhouette, expressive but calm emotion, high contrast for mobile. "
        "Leave clean dark space on the left or lower third for text overlay. No photorealism. "
        "No letters, no words, no watermark, no logo."
    )


def make_sleep_story_thumbnail_card(
    source_path: Path,
    output_path: Path,
    title: str,
    settings: dict[str, Any],
) -> None:
    width = int(settings.get("thumbnail_width") or 1280)
    height = int(settings.get("thumbnail_height") or 720)
    with Image.open(source_path) as image:
        canvas = crop_image_to_ratio(image.convert("RGB"), width / height).resize((width, height), Image.Resampling.LANCZOS)
    canvas = ImageEnhance.Color(canvas).enhance(1.08)
    canvas = ImageEnhance.Contrast(canvas).enhance(1.05)
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    draw.rectangle((0, 0, int(width * 0.58), height), fill=(3, 8, 20, 118))
    draw.rectangle((0, int(height * 0.68), width, height), fill=(3, 8, 20, 82))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)

    if bool(settings.get("thumbnail_text_enabled", True)):
        draw_sleep_thumbnail_text(canvas, thumbnail_text_from_title(title), settings)
    canvas.convert("RGB").save(output_path, quality=int(settings.get("thumbnail_jpeg_quality") or 90), optimize=True)


def draw_sleep_thumbnail_text(image: Image.Image, text: str, settings: dict[str, Any]) -> None:
    draw = ImageDraw.Draw(image)
    width, height = image.size
    font = load_thumbnail_font(int(settings.get("thumbnail_text_size") or 74), bold=True)
    small_font = load_thumbnail_font(int(settings.get("thumbnail_brand_size") or 28), bold=True)
    lines = wrap_thumbnail_text(text, font, int(width * 0.48))
    total_height = sum(draw.textbbox((0, 0), line, font=font)[3] for line in lines) + (len(lines) - 1) * 8
    y = int(height * 0.46 - total_height / 2)
    x = 58
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        draw.text((x + 4, y + 5), line, font=font, fill=(0, 0, 0, 180))
        draw.text((x, y), line, font=font, fill=(255, 246, 218, 255), stroke_width=2, stroke_fill=(28, 18, 9, 210))
        y += (bbox[3] - bbox[1]) + 8
    brand = str(settings.get("channel_name") or "Sleepu Stories")
    draw.text((60, height - 72), brand, font=small_font, fill=(203, 224, 255, 230))


def thumbnail_text_from_title(title: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9' ]+", " ", title or "").strip()
    stopwords = {
        "the", "a", "an", "and", "of", "to", "for", "before", "sleep", "story", "stories",
        "that", "who", "with", "in", "on", "at", "from",
        "behind", "under", "over", "into", "through", "where", "when", "why", "how",
    }
    words = [word for word in cleaned.split() if word.lower() not in stopwords]
    if not words:
        words = cleaned.split()[:4] or ["Sleepu", "Stories"]
    return " ".join(words[:4]).upper()


def wrap_thumbnail_text(text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    probe = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(probe)
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines[:3]


def load_thumbnail_font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/seguibl.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/calibrib.ttf" if bold else "C:/Windows/Fonts/calibri.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def comfyui_available(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/system_stats", timeout=4) as response:
            return response.status < 500
    except Exception:
        return False


def wait_for_comfyui(base_url: str, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + max(5, timeout_seconds)
    while time.monotonic() < deadline:
        if comfyui_available(base_url):
            return True
        time.sleep(2)
    return False


def start_comfyui_server(settings: dict[str, Any], job: dict[str, Any] | None = None) -> None:
    comfy_dir = ROOT_DIR / str(settings.get("comfyui_dir") or "tools/ComfyUI")
    python_path = ROOT_DIR / str(
        settings.get("comfyui_python")
        or comfy_dir / ".venv" / "Scripts" / "python.exe"
    )
    if not comfy_dir.exists() or not python_path.exists():
        log(job or {}, f"Cannot auto-start ComfyUI. Missing {comfy_dir} or {python_path}.")
        return
    host, port = comfyui_host_port(str(settings.get("comfyui_url") or "http://127.0.0.1:8188"))
    logs_dir = ROOT_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = logs_dir / "comfyui.out.log"
    stderr_path = logs_dir / "comfyui.err.log"
    command = [
        str(python_path),
        "main.py",
        "--listen",
        host,
        "--port",
        str(port),
    ]
    if bool(settings.get("comfyui_cpu", False)):
        command.append("--cpu")
    elif bool(settings.get("comfyui_directml", True)):
        command.append("--directml")
    if bool(settings.get("comfyui_lowvram", True)):
        command.append("--lowvram")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        subprocess.Popen(
            command,
            cwd=str(comfy_dir),
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )


def comfyui_host_port(base_url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(base_url)
    return parsed.hostname or "127.0.0.1", int(parsed.port or 8188)


def comfyui_txt2img(
    base_url: str,
    prompt: str,
    output_path: Path,
    settings: dict[str, Any],
    seed_offset: int = 0,
) -> Path:
    workflow = build_comfyui_workflow(prompt, settings, seed_offset)
    client_id = f"sleepu-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
    prompt_id = comfyui_submit_prompt(base_url, workflow, client_id)
    history = comfyui_wait_for_history(
        base_url=base_url,
        prompt_id=prompt_id,
        timeout_seconds=int(settings.get("comfyui_timeout_seconds") or 900),
    )
    image_bytes = comfyui_first_image_bytes(base_url, history)
    if not image_bytes:
        raise RuntimeError("ComfyUI completed but returned no image")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_bytes)
    return output_path


def comfyui_fallback_workflow_settings(settings: dict[str, Any]) -> dict[str, Any] | None:
    fallback = str(settings.get("comfyui_fallback_workflow") or "").strip()
    current = str(settings.get("comfyui_workflow") or "").strip()
    if not fallback or fallback == current:
        return None
    fallback_settings = dict(settings)
    fallback_settings["comfyui_workflow"] = fallback
    fallback_settings["comfyui_steps"] = int(settings.get("comfyui_fallback_steps") or 16)
    fallback_settings["comfyui_cfg"] = float(settings.get("comfyui_fallback_cfg") or 6.0)
    fallback_settings["comfyui_sampler"] = str(settings.get("comfyui_fallback_sampler") or "dpmpp_2m")
    fallback_settings["comfyui_scheduler"] = str(settings.get("comfyui_fallback_scheduler") or "karras")
    return fallback_settings


def build_comfyui_workflow(prompt: str, settings: dict[str, Any], seed_offset: int = 0) -> dict[str, Any]:
    workflow_path = ROOT_DIR / str(
        settings.get("comfyui_workflow")
        or BASE_INPUT_DIR / "workflows" / "comfyui" / "sleep_story_sd15.json"
    )
    if not workflow_path.exists():
        raise FileNotFoundError(
            f"Missing ComfyUI workflow JSON: {workflow_path}. "
            "Export an API-format workflow from ComfyUI and use placeholders like __PROMPT__ and __SEED__."
        )
    validate_comfyui_workflow_assets(workflow_path, settings)
    raw = workflow_path.read_text(encoding="utf-8-sig")
    width = int(settings.get("comfyui_width") or settings.get("local_image_width") or 768)
    height = int(settings.get("comfyui_height") or settings.get("local_image_height") or 432)
    seed = int(settings.get("comfyui_seed") or settings.get("local_image_seed") or 3188)
    seed += int(seed_offset) + image_seed_salt(settings)
    negative_prompt = ", ".join(
        part
        for part in [
            str(settings.get("comfyui_negative_prompt") or settings.get("local_image_negative_prompt") or (
                "low quality, blurry, distorted, deformed, text, watermark, logo, noisy, harsh contrast, horror"
            )),
            sleep_story_art_negative_prompt(settings),
        ]
        if part
    )
    replacements = {
        "__PROMPT__": prompt,
        "__NEGATIVE_PROMPT__": negative_prompt,
        "__WIDTH__": str(width),
        "__HEIGHT__": str(height),
        "__SEED__": str(seed),
        "__STEPS__": str(int(settings.get("comfyui_steps") or settings.get("local_image_steps") or 20)),
        "__CFG__": str(float(settings.get("comfyui_cfg") or settings.get("local_image_cfg_scale") or 6.0)),
        "__SAMPLER__": str(settings.get("comfyui_sampler") or "dpmpp_2m"),
        "__SCHEDULER__": str(settings.get("comfyui_scheduler") or "karras"),
    }
    for key, value in replacements.items():
        raw = raw.replace(key, json.dumps(value)[1:-1])
    try:
        workflow = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid ComfyUI workflow JSON after placeholder replacement: {exc}") from exc
    if not isinstance(workflow, dict):
        raise RuntimeError("ComfyUI workflow must be a JSON object")
    return workflow


def validate_comfyui_workflow_assets(workflow_path: Path, settings: dict[str, Any]) -> None:
    name = workflow_path.name.lower()
    if "dreamshaper_xl" in name or "sdxl" in name:
        checkpoint_name = str(settings.get("comfyui_dreamshaper_xl_checkpoint") or "DreamShaperXL_Lightning-SFW.safetensors")
        checkpoint_path = ROOT_DIR / "tools" / "ComfyUI" / "models" / "checkpoints" / checkpoint_name
        min_gb = float(settings.get("comfyui_dreamshaper_xl_min_checkpoint_gb") or 6.0)
        if not checkpoint_path.exists():
            raise FileNotFoundError(f"Missing DreamShaper XL checkpoint: {checkpoint_path}")
        size_gb = checkpoint_path.stat().st_size / (1024 ** 3)
        if size_gb < min_gb:
            raise RuntimeError(
                f"DreamShaper XL checkpoint is incomplete: {checkpoint_path.name} is {size_gb:.2f}GB, expected at least {min_gb:.1f}GB"
            )
        return
    if "flux" not in name:
        return
    if "gguf" in name:
        required_files = [
            (
                ROOT_DIR / "tools" / "ComfyUI" / "models" / "unet" / str(settings.get("comfyui_flux_gguf_unet") or "flux1-schnell-Q4_K_S.gguf"),
                float(settings.get("comfyui_flux_gguf_min_unet_gb") or 6.0),
            ),
            (
                ROOT_DIR / "tools" / "ComfyUI" / "models" / "clip" / str(settings.get("comfyui_flux_clip_l") or "clip_l.safetensors"),
                0.2,
            ),
            (
                ROOT_DIR / "tools" / "ComfyUI" / "models" / "clip" / str(settings.get("comfyui_flux_t5") or "t5xxl_fp8_e4m3fn.safetensors"),
                4.0,
            ),
            (
                ROOT_DIR / "tools" / "ComfyUI" / "models" / "vae" / str(settings.get("comfyui_flux_vae") or "ae.safetensors"),
                0.25,
            ),
        ]
        for asset_path, min_gb in required_files:
            if not asset_path.exists():
                raise FileNotFoundError(f"Missing FLUX GGUF asset: {asset_path}")
            size_gb = asset_path.stat().st_size / (1024 ** 3)
            if size_gb < min_gb:
                raise RuntimeError(
                    f"FLUX GGUF asset is incomplete: {asset_path.name} is {size_gb:.2f}GB, expected at least {min_gb:.2f}GB"
                )
        return
    checkpoint_name = str(settings.get("comfyui_flux_checkpoint") or "flux1-schnell-fp8.safetensors")
    checkpoint_path = ROOT_DIR / "tools" / "ComfyUI" / "models" / "checkpoints" / checkpoint_name
    min_gb = float(settings.get("comfyui_flux_min_checkpoint_gb") or 15.0)
    if not checkpoint_path.exists():
        raise FileNotFoundError(f"Missing FLUX checkpoint: {checkpoint_path}")
    size_gb = checkpoint_path.stat().st_size / (1024 ** 3)
    if size_gb < min_gb:
        raise RuntimeError(
            f"FLUX checkpoint is incomplete: {checkpoint_path.name} is {size_gb:.2f}GB, expected at least {min_gb:.1f}GB"
        )


def image_seed_salt(settings: dict[str, Any]) -> int:
    value = str(settings.get("image_seed_salt") or "").strip()
    if not value:
        return 0
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 1_000_000


def choose_best_image_candidate(
    candidates: list[Path],
    prompt: str = "",
    semantic_scorer: Any | None = None,
) -> Path:
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return candidates[0]
    try:
        from .agents.image_reviewer import score_image

        semantic_scores = semantic_scorer.score(prompt, existing) if semantic_scorer else {}
        if semantic_scores:
            values = list(semantic_scores.values())
            low, high = min(values), max(values)

            def combined(path: Path) -> float:
                semantic = semantic_scores.get(path, low)
                normalized = 0.5 if high <= low else (semantic - low) / (high - low)
                return 0.7 * normalized + 0.3 * score_image(path)

            return max(existing, key=combined)
        return max(existing, key=score_image)
    except Exception:
        return existing[0]


def comfyui_submit_prompt(base_url: str, workflow: dict[str, Any], client_id: str) -> str:
    data = json.dumps({"prompt": workflow, "client_id": client_id}).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/prompt",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    prompt_id = str(result.get("prompt_id") or "").strip()
    if not prompt_id:
        raise RuntimeError(f"ComfyUI returned no prompt_id: {result}")
    return prompt_id


def comfyui_wait_for_history(base_url: str, prompt_id: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + max(30, timeout_seconds)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + f"/history/{prompt_id}", timeout=10) as response:
                history = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = str(exc)
            time.sleep(2)
            continue
        item = history.get(prompt_id) if isinstance(history, dict) else None
        if isinstance(item, dict):
            status = item.get("status") if isinstance(item.get("status"), dict) else {}
            if status.get("status_str") == "error":
                messages = status.get("messages") or []
                raise RuntimeError(f"ComfyUI workflow failed: {messages}")
            outputs = item.get("outputs")
            if isinstance(outputs, dict) and outputs:
                return item
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for ComfyUI prompt {prompt_id}. {last_error}".strip())


def comfyui_first_image_bytes(base_url: str, history_item: dict[str, Any]) -> bytes:
    outputs = history_item.get("outputs") if isinstance(history_item, dict) else {}
    if not isinstance(outputs, dict):
        return b""
    for node in outputs.values():
        if not isinstance(node, dict):
            continue
        images = node.get("images")
        if not isinstance(images, list):
            continue
        for image in images:
            if not isinstance(image, dict):
                continue
            filename = str(image.get("filename") or "").strip()
            if not filename:
                continue
            query = urllib.parse.urlencode(
                {
                    "filename": filename,
                    "subfolder": str(image.get("subfolder") or ""),
                    "type": str(image.get("type") or "output"),
                }
            )
            with urllib.request.urlopen(base_url.rstrip("/") + "/view?" + query, timeout=60) as response:
                return response.read()
    return b""


def normalize_story_scene_image(path: Path, settings: dict[str, Any]) -> Path:
    if not path.exists():
        return path
    target_width, target_height = parse_resolution(str(settings.get("resolution") or "1920x1080"))
    try:
        with Image.open(path) as image:
            normalized = crop_image_to_ratio(image.convert("RGB"), target_width / max(1, target_height))
            if normalized.size != (target_width, target_height):
                normalized = normalized.resize((target_width, target_height), Image.Resampling.LANCZOS)
            normalized = normalized.filter(ImageFilter.UnsharpMask(radius=0.8, percent=55, threshold=4))
            normalized.save(path, quality=94)
    except Exception:
        return path
    return path


def crop_image_to_ratio(image: Image.Image, target_ratio: float) -> Image.Image:
    width, height = image.size
    current_ratio = width / max(1, height)
    if abs(current_ratio - target_ratio) < 0.01:
        return image
    if current_ratio > target_ratio:
        crop_width = int(height * target_ratio)
        x = max(0, (width - crop_width) // 2)
        return image.crop((x, 0, x + crop_width, height))
    crop_height = int(width / target_ratio)
    y = max(0, (height - crop_height) // 2)
    return image.crop((0, y, width, y + crop_height))


def sd_webui_available(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/sdapi/v1/options", timeout=4) as response:
            return response.status < 500
    except Exception:
        return False


def sd_webui_txt2img(base_url: str, prompt: str, output_path: Path, settings: dict[str, Any]) -> Path:
    width, height = parse_resolution(str(settings.get("resolution") or "1920x1080"))
    width = int(settings.get("local_image_width") or min(1344, max(512, width)))
    height = int(settings.get("local_image_height") or min(768, max(512, height)))
    negative_prompt = ", ".join(
        part
        for part in [
            str(settings.get("local_image_negative_prompt") or (
                "low quality, blurry, deformed, distorted face, extra fingers, text, watermark, logo, "
                "harsh contrast, scary, horror, noisy"
            )),
            sleep_story_art_negative_prompt(settings),
        ]
        if part
    )
    payload = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "steps": int(settings.get("local_image_steps") or 28),
        "cfg_scale": float(settings.get("local_image_cfg_scale") or 6.5),
        "width": width,
        "height": height,
        "sampler_name": str(settings.get("local_image_sampler") or "DPM++ 2M Karras"),
        "batch_size": 1,
        "n_iter": 1,
        "restore_faces": False,
        "send_images": True,
        "save_images": False,
    }
    if settings.get("local_image_seed") is not None:
        try:
            payload["seed"] = int(settings["local_image_seed"])
        except (TypeError, ValueError):
            pass
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/sdapi/v1/txt2img",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    timeout = int(settings.get("local_image_timeout_seconds") or 600)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    images = result.get("images") if isinstance(result, dict) else None
    if not images:
        raise RuntimeError("SD WebUI returned no image")
    encoded = str(images[0]).split(",", 1)[-1]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(base64.b64decode(encoded))
    return output_path


def imagegen_local_txt2img(prompt: str, output_path: Path, settings: dict[str, Any], seed_offset: int = 0) -> Path:
    tool_dir = ROOT_DIR / str(settings.get("local_imagegen_dir") or "tools/imagegen-local")
    script_path = ROOT_DIR / str(settings.get("local_imagegen_script") or tool_dir / "generate_test.py")
    python_path = ROOT_DIR / str(settings.get("local_imagegen_python") or tool_dir / ".venv" / "Scripts" / "python.exe")
    checkpoint = ROOT_DIR / str(
        settings.get("local_imagegen_checkpoint")
        or tool_dir / "models" / "DreamShaper_8_pruned.safetensors"
    )
    if not script_path.exists():
        raise FileNotFoundError(f"Missing imagegen script: {script_path}")
    if not python_path.exists():
        raise FileNotFoundError(f"Missing imagegen Python: {python_path}")

    width = int(settings.get("local_imagegen_width") or settings.get("local_image_width") or 768)
    height = int(settings.get("local_imagegen_height") or settings.get("local_image_height") or 432)
    steps = int(settings.get("local_imagegen_steps") or settings.get("local_image_steps") or 24)
    guidance = float(settings.get("local_imagegen_guidance") or settings.get("local_image_cfg_scale") or 7.0)
    seed = int(settings.get("local_imagegen_seed") or settings.get("local_image_seed") or 3188)
    seed += int(seed_offset) + image_seed_salt(settings)
    timeout = int(settings.get("local_image_timeout_seconds") or 1200)
    negative = ", ".join(
        part
        for part in [
            str(settings.get("local_image_negative_prompt") or (
                "low quality, blurry, distorted, deformed, bad anatomy, text, watermark, logo, noisy, oversaturated"
            )),
            sleep_story_art_negative_prompt(settings),
        ]
        if part
    )
    device = str(settings.get("local_imagegen_device") or "dml").strip().lower()
    if device not in {"dml", "cpu"}:
        device = "dml"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(python_path),
        str(script_path),
        "--prompt",
        prompt,
        "--negative",
        negative,
        "--output",
        str(output_path),
        "--width",
        str(width),
        "--height",
        str(height),
        "--steps",
        str(steps),
        "--guidance",
        str(guidance),
        "--seed",
        str(seed),
        "--device",
        device,
    ]
    if checkpoint.exists():
        command.extend(["--checkpoint", str(checkpoint)])
    else:
        command.extend(["--model", str(settings.get("local_imagegen_model") or "Lykon/dreamshaper-8")])

    result = subprocess.run(
        command,
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail[-1200:] or f"imagegen-local exited with {result.returncode}")
    if not output_path.exists():
        raise RuntimeError("imagegen-local completed but did not create an output image")
    return output_path


def imagegen_retry_settings(settings: dict[str, Any]) -> dict[str, Any]:
    retry = dict(settings)
    retry["local_imagegen_width"] = int(settings.get("local_imagegen_retry_width") or 512)
    retry["local_imagegen_height"] = int(settings.get("local_imagegen_retry_height") or 288)
    retry["local_imagegen_steps"] = int(settings.get("local_imagegen_retry_steps") or 6)
    retry["local_imagegen_guidance"] = float(settings.get("local_imagegen_retry_guidance") or settings.get("local_imagegen_guidance") or 6.0)
    retry["local_image_width"] = retry["local_imagegen_width"]
    retry["local_image_height"] = retry["local_imagegen_height"]
    retry["local_image_steps"] = retry["local_imagegen_steps"]
    retry["local_image_cfg_scale"] = retry["local_imagegen_guidance"]
    retry["local_image_timeout_seconds"] = min(180, int(settings.get("local_image_timeout_seconds") or 240))
    return retry


def generate_reference_scene_images(
    settings: dict[str, Any],
    generated_dir: Path,
    scene_prompts: list[str],
    count: int,
    start_index: int = 1,
) -> list[Path]:
    reference = find_story_reference_image(settings)
    if reference is None:
        return []
    try:
        base = Image.open(reference).convert("RGB")
    except Exception:
        return []

    generated_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    variants = [
        (0.00, 0.00, 1.00, 0.98, 1.04),
        (0.08, 0.00, 1.10, 1.03, 1.06),
        (0.18, 0.02, 1.18, 1.00, 1.08),
        (0.33, 0.04, 1.24, 1.04, 1.10),
        (0.48, 0.02, 1.16, 0.96, 1.05),
        (0.12, 0.08, 1.30, 1.05, 1.08),
        (0.42, 0.10, 1.30, 1.00, 1.06),
        (0.24, 0.00, 1.05, 0.94, 1.03),
    ]
    for index in range(start_index, count + 1):
        output = generated_dir / f"scene-{index:02}.png"
        if output.exists():
            outputs.append(output)
            continue
        variant = variants[(index - 1) % len(variants)]
        image = make_reference_scene_variant(base, index, variant)
        image.save(output, quality=95)
        outputs.append(output)
    return outputs


def find_story_reference_image(settings: dict[str, Any]) -> Path | None:
    candidates: list[Path] = []
    for key, fallback in (
        ("references_dir", DEFAULT_REFERENCES_DIR),
        ("images_dir", DEFAULT_IMAGES_DIR),
    ):
        folder = ROOT_DIR / str(settings.get(key) or fallback)
        if folder.exists():
            candidates.extend(story_reference_images({"references": folder, "images": folder}))
            candidates.extend(list_files(folder, IMAGE_EXTENSIONS))
    for path in candidates:
        if path.exists() and not is_placeholder_image(path):
            return path
    return None


def make_reference_scene_variant(
    base: Image.Image,
    index: int,
    variant: tuple[float, float, float, float, float],
) -> Image.Image:
    x_frac, y_frac, zoom, brightness, color = variant
    image = crop_image_to_16x9(base, x_frac, y_frac, zoom).resize((1920, 1080), Image.Resampling.LANCZOS)
    image = ImageEnhance.Color(image).enhance(color)
    image = ImageEnhance.Brightness(image).enhance(brightness)
    image = ImageEnhance.Contrast(image).enhance(1.03)
    image = add_scene_mist_and_sparks(image, index)
    image = add_soft_vignette(image)
    return image.filter(ImageFilter.UnsharpMask(radius=1.1, percent=75, threshold=3))


def crop_image_to_16x9(image: Image.Image, x_frac: float, y_frac: float, zoom: float) -> Image.Image:
    width, height = image.size
    target_ratio = 16 / 9
    crop_width = int(width / max(1.0, zoom))
    crop_height = int(crop_width / target_ratio)
    if crop_height > height:
        crop_height = int(height / max(1.0, zoom))
        crop_width = int(crop_height * target_ratio)
    max_x = max(0, width - crop_width)
    max_y = max(0, height - crop_height)
    x = int(max_x * max(0.0, min(1.0, x_frac)))
    y = int(max_y * max(0.0, min(1.0, y_frac)))
    return image.crop((x, y, x + crop_width, y + crop_height))


def add_scene_mist_and_sparks(image: Image.Image, index: int) -> Image.Image:
    random.seed(3188 + index)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    for band in range(5):
        y = 580 + band * 75 + random.randint(-20, 20)
        draw.ellipse((-250, y, 2170, y + 260), fill=(190, 220, 255, 10 + (index % 3) * 3))
    for _ in range(24 + index * 2):
        x = random.randint(80, 1840)
        y = random.randint(120, 930)
        radius = random.choice([2, 2, 3, 4])
        alpha = random.randint(60, 135)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(255, 224, 120, alpha))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=1.2))
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def add_soft_vignette(image: Image.Image) -> Image.Image:
    width, height = image.size
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((-260, -190, width + 260, height + 250), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(140))
    dark = Image.new("RGB", (width, height), (2, 10, 24))
    return Image.composite(image, dark, mask)


def compact_stable_diffusion_prompt(prompt: str, settings: dict[str, Any]) -> str:
    max_words = int(settings.get("local_image_prompt_max_words") or 58)
    max_chars = int(settings.get("local_image_prompt_max_chars") or 420)
    cleaned = re.sub(r"\s+", " ", prompt or "").strip()
    cleaned = re.sub(r"\b(ultra detailed|masterpiece|8k|premium concept art)\b,?\s*", "", cleaned, flags=re.I)
    parts = [part.strip(" ,") for part in cleaned.split(",") if part.strip(" ,")]
    kept: list[str] = []
    words = 0
    for part in parts:
        part_words = len(part.split())
        candidate = ", ".join([*kept, part]) if kept else part
        if kept and (words + part_words > max_words or len(candidate) > max_chars):
            break
        kept.append(part)
        words += part_words
    if not kept:
        kept = cleaned.split()[:max_words]
        return " ".join(kept)[:max_chars].strip()
    compact = ", ".join(dict.fromkeys(kept))
    return trim_prompt_text(compact, max_chars, " ,")


def trim_prompt_text(text: str, max_chars: int, strip_chars: str = " .,") -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned.rstrip(strip_chars)
    clipped = cleaned[:max_chars]
    cut = max(clipped.rfind(". "), clipped.rfind(", "), clipped.rfind("; "))
    if cut >= int(max_chars * 0.72):
        clipped = clipped[:cut]
    return clipped.rstrip(strip_chars)


def sleep_story_art_style_prompt(settings: dict[str, Any]) -> str:
    preset = resolve_sleep_story_art_style_preset(settings)
    custom = str(settings.get("story_art_style_custom") or "").strip()
    if custom:
        return custom
    presets = {
        "watercolor_storybook": (
            "(luminous 2D watercolor dream storybook illustration:1.45), soft wet paper texture, transparent washes, "
            "lavender blue moon haze, honey gold lantern glow, delicate loose brushwork, glowing mist and floating light particles, "
            "poetic airy bedtime fairytale mood, simplified elegant illustrated forms, no photographic skin, not realistic, not a photo"
        ),
        "storybook_gouache": (
            "(2D hand-painted gouache adult storybook illustration:1.4), matte paint texture, visible brush strokes, "
            "soft rounded shapes, simplified illustrated adult face, clear silhouettes, cozy moonlit bedtime atmosphere, "
            "warm lantern glow, muted indigo teal and butter yellow palette, not photorealistic, not a photo"
        ),
        "painterly_anime": (
            "(soft painterly anime-inspired bedtime illustration:1.35), hand-painted background art, gentle expressive character design, "
            "dreamy moonlit fantasy atmosphere, soft brushwork, cinematic 16:9 composition, calm adult sleep story mood, "
            "not hyperrealistic, not a photo"
        ),
        "paper_cut_25d": (
            "(layered paper-cut 2.5D storybook illustration:1.35), handmade paper texture, rounded layered shapes, "
            "subtle depth, clean silhouettes, soft moonlit bedtime diorama, warm window and lantern glow, not plastic 3D"
        ),
        "storybook_ink_wash": (
            "(ink and watercolor storybook illustration:1.35), delicate linework, soft wash shading, textured paper, "
            "quiet old-world bedtime tale mood, moonlit blue gray shadows, warm candlelit accents, elegant simple faces, not a photo"
        ),
        "pastel_chalk": (
            "(soft pastel chalk storybook illustration:1.35), velvety texture, gentle blended edges, cozy dreamlike colors, "
            "sleepy moonlit atmosphere, warm lamplight, soft expressive adult character design, not photorealistic"
        ),
        "vintage_fairytale": (
            "(vintage illustrated fairytale book plate:1.35), warm aged paper feel, refined painterly texture, subtle decorative detail, "
            "old village bedtime atmosphere, moonlit windows, lantern glow, muted blue gold palette, not realistic photography"
        ),
        "cozy_cinematic_painting": (
            "(cozy cinematic digital painting for adult bedtime stories:1.35), painterly brush texture, soft filmic moonlight, "
            "warm window glow, clear emotional focal subject, gentle depth, polished storybook look, not a photo"
        ),
        "stained_glass_storybook": (
            "(soft stained-glass inspired storybook illustration:1.25), luminous colored shapes, clean leaded outlines, "
            "gentle moonlit glow, simplified elegant forms, calm magical bedtime atmosphere, not church iconography, not a photo"
        ),
        "woodcut_fairytale": (
            "(gentle woodcut-inspired fairytale illustration:1.25), carved line texture, flat layered colors, readable silhouettes, "
            "quiet moonlit folk tale mood, warm amber highlights, soft edges, not harsh horror, not a photo"
        ),
    }
    if preset in {"watercolor", "storybook_watercolor"}:
        preset = "watercolor_storybook"
    if preset in {"gouache", "fairytale_gouache"}:
        preset = "storybook_gouache"
    if preset in {"anime", "soft_anime"}:
        preset = "painterly_anime"
    if preset in {"papercut", "paper_cut", "paper-cut"}:
        preset = "paper_cut_25d"
    return presets.get(preset, presets["storybook_gouache"])


def resolve_sleep_story_art_style_preset(settings: dict[str, Any]) -> str:
    preset = str(settings.get("story_art_style_preset") or "storybook_gouache").strip().lower()
    if preset not in {"auto", "random", "story_auto"}:
        return preset
    text = " ".join(
        str(settings.get(key) or "")
        for key in (
            "title",
            "prompt",
            "story_topic",
            "story_outline",
            "story_hook",
            "story_lesson",
            "story_text",
        )
    ).lower()
    style_rules = [
        ("stained_glass_storybook", ("window", "glass", "cathedral", "lightkeeper", "lighthouse", "lantern tower")),
        ("woodcut_fairytale", ("forest", "old village", "folktale", "mountain", "wolf", "wood", "winter")),
        ("pastel_chalk", ("cloud", "dream", "meadow", "pillow", "soft rain", "snow")),
        ("storybook_ink_wash", ("letter", "library", "book", "clockmaker", "map", "tea", "teacup")),
        ("vintage_fairytale", ("bakery", "village", "garden", "train", "inn", "cottage")),
        ("paper_cut_25d", ("toy", "shadow", "moon gate", "tiny", "miniature", "paper")),
        ("watercolor_storybook", ("sea", "ocean", "river", "rain", "harbor", "boat")),
        ("cozy_cinematic_painting", ("room", "bedroom", "fireplace", "home", "lamp", "window")),
    ]
    for style_id, keywords in style_rules:
        if any(keyword in text for keyword in keywords):
            return style_id
    options = [
        "storybook_gouache",
        "watercolor_storybook",
        "storybook_ink_wash",
        "pastel_chalk",
        "vintage_fairytale",
        "cozy_cinematic_painting",
        "paper_cut_25d",
    ]
    seed_text = str(settings.get("title") or settings.get("prompt") or settings.get("image_seed_salt") or "")
    digest = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
    return options[int(digest[:8], 16) % len(options)]


def sleep_story_art_negative_prompt(settings: dict[str, Any]) -> str:
    preset = resolve_sleep_story_art_style_preset(settings)
    base = [
        "photorealistic",
        "realistic photo",
        "photo",
        "portrait photo",
        "photography",
        "dslr",
        "camera",
        "lens blur",
        "realistic skin texture",
        "detailed skin pores",
        "realistic face",
        "hyper detailed face",
        "cgi",
        "3d render",
        "plastic skin",
        "hyperreal",
        "uncanny face",
        "harsh contrast",
        "horror",
        "scary",
        "text",
        "watermark",
        "logo",
        "random crown",
        "princess crown",
        "animal ears",
        "cat ears",
        "fairy wings",
        "extra character",
        "duplicate person",
        "childlike character",
    ]
    if preset in {"paper_cut_25d", "papercut", "paper_cut", "paper-cut"}:
        base.extend(["realistic depth of field", "plastic toy", "clay render", "glossy 3d"])
    elif preset in {"stained_glass_storybook"}:
        base.extend(["religious icon", "church altar", "hard black outlines", "busy mosaic"])
    elif preset in {"woodcut_fairytale"}:
        base.extend(["horror engraving", "violent scene", "scratchy harsh shadows"])
    elif preset in {"pastel_chalk"}:
        base.extend(["muddy colors", "overly blurry", "childish crayon drawing"])
    else:
        base.extend(["flat vector", "corporate illustration"])
    return ", ".join(dict.fromkeys(base))


def compact_story_image_prompt(prompt: str, settings: dict[str, Any]) -> str:
    max_chars = int(settings.get("comfyui_prompt_max_chars") or settings.get("local_image_prompt_max_chars") or 720)
    cleaned = re.sub(r"\s+", " ", prompt or "").strip()
    cleaned = re.sub(r"\b(ultra detailed|masterpiece|8k|premium concept art)\b,?\s*", "", cleaned, flags=re.I)

    marker_positions = [
        cleaned.find(marker)
        for marker in ("Story passage anchor:", "Style:", "Character consistency:", "World consistency:", "Scene must")
        if cleaned.find(marker) >= 0
    ]
    first_marker = min(marker_positions) if marker_positions else -1
    pre_marker = cleaned[:first_marker].strip(" .") if first_marker > 0 else ""
    if len(pre_marker) >= 12:
        scene = pre_marker
    else:
        scene_match = re.search(
            r"([A-Z][A-Za-z0-9 ':-]{2,80}:\s*.*?)(?:\s+Emotion:|\s+Story passage anchor:|\s+Style:|\s+Character consistency:|\s+World consistency:|$)",
            cleaned,
        )
        if scene_match:
            scene = scene_match.group(1).strip(" .")
        else:
            marker_positions = [
                cleaned.find(marker)
                for marker in ("Story passage anchor:", "Style:", "Character consistency:", "World consistency:", "Scene must")
                if cleaned.find(marker) >= 0
            ]
            scene_end = min(marker_positions) if marker_positions else min(len(cleaned), 220)
            scene = cleaned[:scene_end].strip(" .")

    anchor = extract_prompt_section(cleaned, "Story passage anchor:", ("Style:", "Character consistency:", "World consistency:", "Scene must"))
    character = extract_prompt_section(cleaned, "Character consistency:", ("World consistency:", "Scene must"))
    world = extract_prompt_section(cleaned, "World consistency:", ("Scene must",))
    identity_lock = str(settings.get("character_identity_lock") or "").strip()
    lock_instruction = character_lock_instruction(" ".join(part for part in [identity_lock, character, scene] if part))

    parts = [
        sleep_story_art_style_prompt(settings),
        scene[:280],
        f"Story moment: {anchor[:160].strip(' .')}" if anchor else "",
        identity_lock[:240] if identity_lock else "",
        f"Consistent character: {character[:150].strip(' .')}" if character and not identity_lock else "",
        lock_instruction[:130] if lock_instruction else "",
        f"Consistent world: {world[:180].strip(' .')}" if world else "",
        (
            "clear focal subject, visible character action, key story object, soft moonlight, "
            "warm gentle glow, cinematic 16:9, no text, no watermark, adult bedtime story mood"
        ),
    ]
    compact = ". ".join(part for part in parts if part).strip()
    return trim_prompt_text(compact, max_chars, " .,")


def character_identity_constraints(text: str) -> tuple[str, list[str]]:
    lowered = (text or "").lower()
    male_score = len(
        re.findall(
            r"\b(boy|male|man|old man|father|son|brother|grandfather|he|his|him)\b",
            lowered,
        )
    )
    female_score = len(
        re.findall(
            r"\b(girl|female|woman|mother|daughter|sister|grandmother|she|her|hers)\b",
            lowered,
        )
    )
    if male_score >= 2 and male_score > female_score:
        return (
            "Character lock: follow this story's current main character exactly as male/a boy; do not draw a girl, woman, dress, or skirt",
            ["girl", "woman", "female", "dress", "skirt", "yellow dress"],
        )
    if female_score >= 2 and female_score > male_score:
        return (
            "Character lock: follow this story's current main character exactly as female/a girl; do not draw a boy or man",
            ["boy", "man", "male"],
        )
    if re.search(r"\b(fox|rabbit|owl|deer|mouse|cat|dog|bear|wolf)\b", lowered) and re.search(
        r"\b(main character|protagonist|visible main character|as the main character)\b",
        lowered,
    ):
        return (
            "Character lock: follow this story's current animal main character exactly; do not turn it into a human child",
            ["human child", "human face", "person"],
        )
    return (
        "Character lock: follow only the current story's described character identity, role, age, outfit, and species; do not reuse any old reference character",
        [],
    )


def character_lock_instruction(text: str) -> str:
    instruction, _ = character_identity_constraints(text)
    return instruction


def extract_prompt_section(text: str, start_marker: str, end_markers: tuple[str, ...]) -> str:
    start = text.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    end = len(text)
    for marker in end_markers:
        index = text.find(marker, start)
        if index >= 0:
            end = min(end, index)
    return text[start:end].strip(" .")


def parse_resolution(value: str) -> tuple[int, int]:
    match = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", value or "")
    if not match:
        return 1920, 1080
    return int(match.group(1)), int(match.group(2))


def build_image_style_prompt(reference_style: str) -> str:
    base = reference_style.strip() or "Soft dreamy bedtime storybook illustration."
    return (
        f"{base} Use this as style guidance only: preserve the color palette, lighting language, "
        "brushwork, softness, mood, atmosphere, and overall illustration quality. Create new imagery "
        "for each scene. Do not copy the reference composition, exact character identity, face, pose, "
        "or background."
    )


def choose_story_images(
    paths: dict[str, Path],
    title: str,
    scene_prompts: list[str],
    count: int,
    generated_dir: Path | None = None,
) -> list[Path]:
    slug_dir = generated_dir or paths["generated"] / slugify(title, 48)
    scene_pool = story_generated_scene_images(slug_dir)
    pool = scene_pool or [
        path
        for path in list_files(slug_dir, IMAGE_EXTENSIONS)
        if not is_placeholder_image(path) and "-candidate-" not in path.stem.lower()
    ]
    if not pool:
        pool = story_render_images(paths["images"])
    if pool:
        ordered = sorted(pool, key=lambda item: item.name.lower()) if generated_dir else sorted(
            pool,
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if len(ordered) >= count:
            return ordered[:count]
        repeated = []
        while len(repeated) < count:
            repeated.extend(ordered)
        return repeated[:count]
    return create_placeholder_images(slug_dir, title, scene_prompts, count)


def story_generated_scene_images(generated_dir: Path) -> list[Path]:
    return sorted(
        [
            path
            for path in list_files(generated_dir, IMAGE_EXTENSIONS)
            if re.match(r"^scene-\d{2}$", path.stem.lower()) and not is_placeholder_image(path)
        ],
        key=lambda item: item.name.lower(),
    )


def ensure_fresh_story_images(settings: dict[str, Any], generated_dir: Path, count: int) -> None:
    if not bool(settings.get("strict_story_images", True)):
        return
    images = story_generated_scene_images(generated_dir)
    if len(images) >= count:
        return
    raise RuntimeError(
        f"Fresh story image generation produced only {len(images)}/{count} scene image(s). "
        "Render stopped so the video does not reuse old or generic fallback visuals."
    )


def is_placeholder_image(path: Path) -> bool:
    return "placeholder" in path.stem.lower()


def create_placeholder_images(image_dir: Path, title: str, scene_prompts: list[str], count: int) -> list[Path]:
    image_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(title, 48)
    palette = [
        ((10, 16, 33), (72, 58, 100), (226, 196, 142)),
        ((12, 23, 38), (49, 84, 112), (204, 180, 145)),
        ((18, 22, 34), (80, 62, 88), (238, 210, 167)),
        ((8, 27, 42), (47, 70, 89), (194, 219, 226)),
    ]
    outputs: list[Path] = []
    for index in range(count):
        bg, mid, glow = palette[index % len(palette)]
        image = Image.new("RGB", (1920, 1080), bg)
        draw = ImageDraw.Draw(image, "RGBA")
        for radius in range(720, 60, -24):
            alpha = max(2, int(90 * (radius / 720) ** 2))
            cx = 1370 + random.randint(-20, 20)
            cy = 250 + random.randint(-18, 18)
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(*glow, alpha))
        for y in range(0, 1080, 6):
            ratio = y / 1080
            color = tuple(int(bg[i] * (1 - ratio) + mid[i] * ratio) for i in range(3))
            draw.line((0, y, 1920, y), fill=color)
        draw.rectangle((0, 710, 1920, 1080), fill=(0, 0, 0, 42))
        for x in range(0, 1920, 160):
            peak = 600 + random.randint(-80, 50)
            draw.polygon([(x - 80, 1080), (x + 120, peak), (x + 330, 1080)], fill=(4, 10, 18, 125))
        for star in range(130):
            x = random.randint(0, 1920)
            y = random.randint(0, 620)
            a = random.randint(45, 150)
            draw.ellipse((x, y, x + 2, y + 2), fill=(255, 245, 220, a))
        scene_line = scene_prompts[index % len(scene_prompts)] if scene_prompts else "quiet bedtime scene"
        draw.text((90, 790), title[:48], fill=(245, 232, 205, 220))
        draw.text((90, 835), scene_line[:130], fill=(230, 224, 212, 120))
        image = image.filter(ImageFilter.GaussianBlur(radius=0.25))
        output = image_dir / f"{slug}-placeholder-{index + 1:02}.jpg"
        image.save(output, quality=92)
        outputs.append(output)
    return outputs


def choose_story_wave_asset(settings: dict[str, Any]) -> Path | None:
    configured = str(settings.get("wave_asset_path") or "").strip()
    if configured:
        path = ROOT_DIR / configured
        if path.exists():
            return path

    configured_dir = str(settings.get("wave_asset_dir") or "").strip()
    wave_dirs = []
    if configured_dir:
        wave_dirs.append(ROOT_DIR / configured_dir)
    wave_dirs.extend(
        [
            BASE_INPUT_DIR / "wave",
            ROOT_DIR / "data" / "input" / "buddhist" / "shared" / "long-assets" / "wave",
        ]
    )
    for wave_dir in wave_dirs:
        if not wave_dir.exists():
            continue
        preferred = wave_dir / "audio-spectrum-alpha.mov"
        if preferred.exists():
            return preferred
        for pattern in ("*spectrum*", "*wave*", "*.mov", "*.mp4", "*.webm", "*.gif"):
            candidates = sorted(path for path in wave_dir.glob(pattern) if path.is_file())
            if candidates:
                return candidates[0]
    return None


def select_story_ambient_effect(
    settings: dict[str, Any],
    *,
    story: Any | None = None,
    scenes: list[Any] | None = None,
    title: str = "",
    script: str = "",
    scene_prompts: list[str] | None = None,
) -> dict[str, Any] | None:
    if not bool(settings.get("effect_overlay_enabled", True)):
        return None
    manifest = load_story_effect_manifest(settings)
    effects = manifest.get("effects") if isinstance(manifest, dict) else None
    if not isinstance(effects, list) or not effects:
        return None

    text_parts = [
        title,
        script[:4000],
        str(getattr(story, "title", "") or ""),
        str(getattr(story, "hook", "") or ""),
        str(getattr(story, "lesson", "") or ""),
        str(getattr(story, "ending", "") or ""),
        str(getattr(story, "script", "") or "")[:4000],
    ]
    for scene in scenes or []:
        text_parts.extend(
            [
                str(getattr(scene, "label", "") or ""),
                str(getattr(scene, "summary", "") or ""),
                str(getattr(scene, "emotion", "") or ""),
                str(getattr(scene, "image_prompt", "") or ""),
            ]
        )
    text_parts.extend(scene_prompts or [])
    haystack = " ".join(part for part in text_parts if part).lower()

    default_id = str(settings.get("effect_default_id") or manifest.get("default_effect_id") or "mist")
    priority_effect = priority_story_effect(effects, haystack)
    if priority_effect:
        return build_story_effect_overlay(settings, priority_effect)
    best_effect = None
    best_score = -1
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        score = story_effect_score(effect, haystack)
        if score > best_score:
            best_score = score
            best_effect = effect
    if best_score <= 0:
        best_effect = next((effect for effect in effects if str(effect.get("id")) == default_id), best_effect)
    return build_story_effect_overlay(settings, best_effect)


def priority_story_effect(effects: list[Any], haystack: str) -> dict[str, Any] | None:
    effect_by_id = {
        str(effect.get("id") or "").lower(): effect
        for effect in effects
        if isinstance(effect, dict)
    }
    priority_rules = [
        ("snow", r"\b(snow|snowy|snowfall|winter|frost|frosted|frozen|ice|icy|blizzard)\b"),
        ("embers", r"\b(ember|embers|fire|fireplace|hearth|candle|candles|lantern|campfire)\b"),
        ("leaves", r"\b(leaf|leaves|autumn|falling leaves|fall season)\b"),
        ("blossoms", r"\b(blossom|blossoms|petal|petals|cherry blossom|spring flowers?)\b"),
        ("mist", r"\b(mist|fog|haze|cloud|dream fog)\b"),
    ]
    for effect_id, pattern in priority_rules:
        if re.search(pattern, haystack) and effect_id in effect_by_id:
            return effect_by_id[effect_id]
    return None


def select_story_background_ambience(
    settings: dict[str, Any],
    *,
    story: Any | None = None,
    scenes: list[Any] | None = None,
    title: str = "",
    script: str = "",
    scene_prompts: list[str] | None = None,
) -> dict[str, Any] | None:
    if not bool(settings.get("background_ambience_enabled", False)):
        return None
    text_parts = [
        title,
        script[:4000],
        str(getattr(story, "title", "") or ""),
        str(getattr(story, "hook", "") or ""),
        str(getattr(story, "lesson", "") or ""),
        str(getattr(story, "ending", "") or ""),
        str(getattr(story, "script", "") or "")[:4000],
    ]
    for scene in scenes or []:
        text_parts.extend(
            [
                str(getattr(scene, "label", "") or ""),
                str(getattr(scene, "summary", "") or ""),
                str(getattr(scene, "source_text", "") or ""),
                str(getattr(scene, "image_prompt", "") or ""),
            ]
        )
    text_parts.extend(scene_prompts or [])
    haystack = " ".join(part for part in text_parts if part).lower()

    ambience_options = [
        (
            "ocean-wind",
            "data/input/story-before-sleep/ambience/pixabay-wind-and-waves.mp3",
            ("ocean", "sea", "shore", "lighthouse", "tide", "wave", "coast", "harbor", "boat"),
            float(settings.get("background_ambience_ocean_volume") or settings.get("background_ambience_volume") or 0.045),
        ),
        (
            "gentle-ocean",
            "data/input/story-before-sleep/ambience/pixabay-gentle-ocean-waves-mix.mp3",
            ("gentle waves", "calm sea", "quiet sea", "beach", "bay", "island", "cove", "moonlit water", "river"),
            float(settings.get("background_ambience_ocean_volume") or settings.get("background_ambience_volume") or 0.045),
        ),
        (
            "rain-room",
            "data/input/story-before-sleep/ambience/pixabay-soft-rain-window-glass.mp3",
            ("rain", "storm", "drizzle", "window rain", "wet roof", "thunder"),
            float(settings.get("background_ambience_rain_volume") or settings.get("background_ambience_volume") or 0.035),
        ),
        (
            "night-forest",
            "data/input/story-before-sleep/ambience/pixabay-night-forest-frogs-crickets.mp3",
            ("forest", "woodland", "garden", "leaf", "leaves", "tree", "meadow", "cottage", "mountain"),
            float(settings.get("background_ambience_forest_volume") or settings.get("background_ambience_volume") or 0.03),
        ),
    ]
    best: tuple[str, str, tuple[str, ...], float] | None = None
    best_score = -1
    for option in ambience_options:
        score = sum(len(re.findall(rf"\b{re.escape(keyword)}s?\b", haystack)) for keyword in option[2])
        if score > best_score:
            best_score = score
            best = option
    if best is None or best_score <= 0:
        default_id = str(settings.get("background_ambience_default_id") or "night-forest")
        best = next((option for option in ambience_options if option[0] == default_id), ambience_options[-1])
    ambience_id, relative_path, _keywords, volume = best
    path = ROOT_DIR / relative_path
    if not path.exists():
        return None
    return {
        "background_ambience_enabled": True,
        "background_ambience_id": ambience_id,
        "background_ambience_path": str(path),
        "background_ambience_volume": max(0.0, min(0.08, volume)),
        "background_ambience_duck_ratio": float(settings.get("background_ambience_duck_ratio") or 1.0),
        "background_ambience_duck_threshold": float(settings.get("background_ambience_duck_threshold") or 0.035),
    }


def build_story_visual_bible(story: Any, settings: dict[str, Any] | None = None) -> dict[str, str]:
    settings = settings or {}
    title = str(getattr(story, "title", "") or "")
    prompt = str(getattr(story, "prompt", "") or "")
    hook = str(getattr(story, "hook", "") or "")
    outline = str(getattr(story, "outline", "") or "")
    script = str(getattr(story, "script", "") or "")
    lesson = str(getattr(story, "lesson", "") or "")
    # Infer character/world continuity only from the actual story, not from
    # reference prompts or benchmark text that may mention unrelated outfits,
    # objects, and locations.
    text = f"{title}\n{hook}\n{outline}\n{script}"
    protagonist = infer_story_protagonist(text)
    character_profile = infer_story_character_profile(text, protagonist)
    settings_hint = infer_story_settings(text)
    objects = infer_story_objects(text)
    palette = infer_story_palette(text)
    style_preset = resolve_sleep_story_art_style_preset(
        {
            **settings,
            "title": title,
            "prompt": prompt,
            "story_hook": hook,
            "story_outline": outline,
            "story_lesson": lesson,
            "story_text": script,
        }
    )

    character_memory = (
        f"Main character bible for this story: {character_profile}. Keep the same person/species, "
        "face identity, apparent age, fur or hair color, clothing/accessory palette, expression, and silhouette in every scene. "
        "Do not change the character identity, gender, outfit palette, or key accessory between images."
    )
    if objects:
        character_memory += f" Recurring character/object details to preserve: {', '.join(objects[:6])}."

    world_memory = (
        f"World bible for this story: {settings_hint}. Keep recurring places, weather, moonlight, "
        "props, scale, and bedtime atmosphere consistent across all scenes."
    )
    if lesson:
        world_memory += f" Visual lesson to support subtly: {lesson[:220]}."

    style = " ".join(
        part
        for part in [
            sleep_story_art_style_prompt({**settings, "story_art_style_preset": style_preset}),
            str(settings.get("image_style_library") or "").strip(),
        ]
        if part
    )
    story_style = (
        f"{style + ', ' if style else ''}{palette}, premium dreamy storybook illustration, "
        "cinematic 16:9 framing, gentle depth, clear focal subject, soft volumetric light, "
        "cozy bedtime mood, polished illustrated texture for adult bedtime stories, not photorealistic"
    )
    return {
        "story_character_memory": character_memory,
        "story_character_identity_lock": character_profile,
        "story_world_memory": world_memory,
        "story_image_style_library": story_style,
        "story_art_style_preset": style_preset,
    }


def apply_story_visual_bible(settings: dict[str, Any], story: Any) -> dict[str, str]:
    bible = build_story_visual_bible(story, settings)
    base_character = str(settings.get("character_memory") or "").strip()
    base_world = str(settings.get("world_memory") or "").strip()
    settings["character_memory"] = " ".join(part for part in [bible["story_character_memory"], base_character] if part)
    settings["character_identity_lock"] = bible["story_character_identity_lock"]
    settings["world_memory"] = " ".join(part for part in [bible["story_world_memory"], base_world] if part)
    settings["story_art_style_preset"] = bible["story_art_style_preset"]
    settings["image_style_library"] = bible["story_image_style_library"]
    dynamic_negative = infer_character_negative_prompt(story)
    if dynamic_negative:
        current_negative = str(settings.get("comfyui_negative_prompt") or settings.get("local_image_negative_prompt") or "").strip()
        settings["comfyui_negative_prompt"] = ", ".join(part for part in [current_negative, dynamic_negative] if part)
    return bible


def infer_story_protagonist(text: str) -> str:
    for pattern in (
        r"\b([A-Z][a-z]{2,})\s+(?:was|is|lived|had|loved|wanted|needed|promised|carried|found)\b",
        r"\b(?:named|called)\s+([A-Z][a-z]{2,})\b",
    ):
        match = re.search(pattern, text or "")
        if match:
            name = match.group(1)
            around_match = re.search(rf".{{0,90}}\b{re.escape(name)}\b.{{0,110}}", text or "")
            around = around_match.group(0).strip() if around_match else "as described in the script"
            return f"{name}, the named main character, {around}"
    candidates = re.findall(r"\b[A-Z][a-z]{2,}\b", text or "")
    blocked = {
        "The", "This", "That", "Then", "When", "And", "But", "One", "As", "In", "At",
        "Story", "Sleep", "YouTube", "Sleepu", "Tonight", "Title", "Target", "Who",
        "What", "Where", "Why", "How",
    }
    for item in candidates:
        if item not in blocked:
            lowered = (text or "").lower()
            around = ""
            match = re.search(rf".{{0,90}}\b{re.escape(item)}\b.{{0,90}}", text or "")
            if match:
                around = match.group(0).strip()
            if re.search(rf"\b{re.escape(item.lower())}\b.*\b(fox|child|girl|boy|keeper|traveler|baker|clockmaker|mouse|rabbit|owl|apprentice|woman|man)\b", lowered):
                return f"{item}, {around or 'the named main character'}"
            return f"{item}, the named main character, {around or 'as described in the script'}"
    lowered = (text or "").lower()
    for noun in ("fox", "child", "girl", "boy", "keeper", "traveler", "baker", "clockmaker", "rabbit", "owl"):
        if noun in lowered:
            return f"a gentle {noun} as the main character, visually consistent across all scenes"
    return "the named main character from the story, visually consistent across all scenes"


def infer_story_character_profile(text: str, protagonist: str) -> str:
    source = re.sub(r"\s+", " ", text or "").strip()
    lowered = clean_character_inference_text(source.lower())
    name = infer_character_name(source)
    gender = infer_character_gender_label(lowered)
    species = infer_character_species_label(lowered)
    age = infer_character_age_label(lowered)
    role = infer_character_role_label(lowered)
    hair = infer_character_hair_label(lowered)
    outfit = infer_character_outfit_label(lowered)
    objects = infer_story_objects(source)

    subject = name or "the main character"
    descriptors = [part for part in [age, gender, species, role] if part]
    if descriptors:
        subject = f"{subject}, {' '.join(descriptors)}"
    elif protagonist:
        subject = f"{subject}, {protagonist[:180]}"

    detail_parts = []
    if hair:
        detail_parts.append(f"same hair/fur detail: {hair}")
    if outfit:
        detail_parts.append(f"same outfit/accessory palette: {outfit}")
    if objects:
        detail_parts.append(f"same recurring key object(s): {', '.join(objects[:5])}")
    detail_text = "; ".join(detail_parts)
    if detail_text:
        detail_text = f" {detail_text}."

    return (
        f"Character identity lock: {subject}. Keep one consistent face, body shape, age impression, "
        f"gender/species, clothing silhouette, and gentle facial feeling in every scene.{detail_text} "
        "Never replace the protagonist with a different person, random child, old reference character, or different outfit."
    )


def clean_character_inference_text(lowered: str) -> str:
    cleaned = lowered or ""
    for phrase in (
        "old reference character",
        "random child",
        "children's-book",
        "children's book",
        "childhood memory",
        "child-under-the-moon",
        "child under the moon",
        "not children",
        "not a child",
        "avoid childish",
    ):
        cleaned = cleaned.replace(phrase, " ")
    return cleaned


def infer_character_name(text: str) -> str:
    for pattern in (
        r"\b([A-Z][a-z]{2,})\s+(?:was|is|lived|had|loved|wanted|needed|promised|carried|found|stood|walked|held|kept)\b",
        r"\b(?:named|called)\s+([A-Z][a-z]{2,})\b",
    ):
        match = re.search(pattern, text or "")
        if match:
            candidate = match.group(1)
            if candidate not in {"The", "This", "That", "Then", "When", "Story", "Sleepu"}:
                return candidate
    return ""


def infer_character_gender_label(lowered: str) -> str:
    male_hits = len(re.findall(r"\b(boy|male|man|young man|old man|father|son|brother|grandfather|he|his|him)\b", lowered))
    female_hits = len(re.findall(r"\b(girl|female|woman|young woman|old woman|mother|daughter|sister|grandmother|she|her|hers)\b", lowered))
    if male_hits >= 2 and male_hits >= female_hits * 1.35:
        return "male"
    if female_hits >= 2 and female_hits >= male_hits * 1.35:
        return "female"
    if re.search(r"\b(boy|young man|old man|father|son|brother|grandfather)\b", lowered):
        return "male"
    if re.search(r"\b(girl|young woman|old woman|mother|daughter|sister|grandmother)\b", lowered):
        return "female"
    return ""


def infer_character_species_label(lowered: str) -> str:
    for species in ("fox", "rabbit", "owl", "deer", "mouse", "cat", "dog", "bear", "wolf"):
        if re.search(rf"\b{species}\b", lowered) and re.search(
            r"\b(main character|protagonist|as the main character|gentle fox|gentle rabbit|gentle owl|gentle deer)\b",
            lowered,
        ):
            return species
    return "human" if re.search(r"\b(boy|girl|man|woman|child|keeper|traveler|baker|clockmaker|apprentice)\b", lowered) else ""


def infer_character_age_label(lowered: str) -> str:
    if re.search(r"\b(adult|woman|man|young woman|young man|keeper|traveler|baker|clockmaker|librarian|shopkeeper)\b", lowered):
        if re.search(r"\b(young woman|young man|apprentice)\b", lowered):
            return "young adult"
        return "adult"
    age_patterns = [
        ("elderly", r"\b(elderly|old|aged|grandfather|grandmother)\b"),
        ("middle-aged", r"\b(middle-aged)\b"),
        ("young", r"\b(young|boy|girl|child)\b"),
        ("little", r"\b(little|small child)\b"),
    ]
    for label, pattern in age_patterns:
        if re.search(pattern, lowered):
            return label
    return ""


def infer_character_role_label(lowered: str) -> str:
    role_phrases = [
        "lantern keeper",
        "clockmaker",
        "apprentice clockmaker",
        "baker",
        "traveler",
        "lighthouse keeper",
        "gardener",
        "librarian",
        "moon keeper",
        "train conductor",
        "shopkeeper",
        "woodcarver",
    ]
    for role in role_phrases:
        if re.search(rf"\b{re.escape(role)}\b", lowered):
            return role
    for role in ("keeper", "apprentice", "traveler"):
        if re.search(rf"\b{role}\b", lowered):
            return role
    if re.search(r"\bchild\b", lowered) and not re.search(r"\b(adult|woman|man|keeper|traveler|baker|clockmaker)\b", lowered):
        return "child"
    return ""


def infer_character_hair_label(lowered: str) -> str:
    match = re.search(
        r"\b((?:warm |soft |dark |light |curly |straight |short |long |silver |gray |grey |white |black |brown |golden |blond |blonde |red ){1,5}hair)\b",
        lowered,
    )
    return match.group(1) if match else ""


def infer_character_outfit_label(lowered: str) -> str:
    outfit_words = [
        "brown cloak",
        "warm brown cloak",
        "blue cloak",
        "worn cloak",
        "coat",
        "robe",
        "scarf",
        "hat",
        "boots",
        "apron",
        "dress",
        "pajamas",
        "lantern",
        "blue lantern",
        "willow leaf map",
    ]
    found = [word for word in outfit_words if re.search(rf"\b{re.escape(word)}\b", lowered)]
    if infer_character_gender_label(lowered) == "male":
        found = [word for word in found if word not in {"dress", "pajamas"}]
    return ", ".join(dict.fromkeys(found[:6]))


def infer_character_negative_prompt(story: Any) -> str:
    text = (
        f"{getattr(story, 'title', '')}\n"
        f"{getattr(story, 'hook', '')}\n{getattr(story, 'outline', '')}\n{getattr(story, 'script', '')}"
    ).lower()
    _, negatives = character_identity_constraints(text)
    negatives.extend(
        [
            "inconsistent character",
            "different protagonist",
            "different face",
            "different outfit",
            "random extra main character",
            "old reference character",
            "yellow dress",
        ]
    )
    return ", ".join(dict.fromkeys(negatives))


def infer_story_settings(text: str) -> str:
    lowered = (text or "").lower()
    setting_words = [
        "valley", "meadow", "forest", "cabin", "cottage", "village", "garden", "shore",
        "lighthouse", "bakery", "library", "train", "mountain", "river", "lake", "workshop",
        "temple", "moon", "snow", "rain", "mist", "star", "lantern",
    ]
    found = [word for word in setting_words if re.search(rf"\b{word}s?\b", lowered)]
    if found:
        return "a consistent " + ", ".join(dict.fromkeys(found[:8])) + " world from this exact story"
    return "a quiet bedtime story world built from the actual script details"


def infer_story_objects(text: str) -> list[str]:
    lowered = (text or "").lower()
    priority_words = [
        "teacup", "letter", "lantern", "bell", "clock", "pocket watch", "key", "candle",
        "book", "map", "gate", "boat", "pearl", "shawl",
    ]
    background_words = ["window", "star", "stardust", "firefly", "flower", "blanket", "moss"]
    found = [word for word in priority_words if re.search(rf"\b{re.escape(word)}s?\b", lowered)]
    for word in background_words:
        if len(re.findall(rf"\b{re.escape(word)}s?\b", lowered)) >= 2:
            found.append(word)
    return list(dict.fromkeys(found[:3]))


def infer_story_palette(text: str) -> str:
    lowered = (text or "").lower()
    if any(word in lowered for word in ("snow", "winter", "frost")):
        return "silver blue winter palette with warm window gold"
    if any(word in lowered for word in ("ember", "fire", "lantern", "candle", "bakery")):
        return "warm amber and deep indigo palette with soft glowing highlights"
    if any(word in lowered for word in ("flower", "blossom", "garden", "spring")):
        return "soft moonlit garden palette with pale pink blossoms, sage green, and warm cream"
    if any(word in lowered for word in ("sea", "shore", "lighthouse", "boat")):
        return "moonlit sea palette with deep teal, pearl silver, and lighthouse gold"
    return "deep indigo, silver moonlight, soft gold highlights, calm bedtime palette"


def load_story_effect_manifest(settings: dict[str, Any]) -> dict[str, Any]:
    configured = str(settings.get("effect_manifest") or "").strip()
    manifest_path = ROOT_DIR / configured if configured else ROOT_DIR / "data" / "input" / "effects" / "sleep-story-stock" / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def story_effect_score(effect: dict[str, Any], haystack: str) -> int:
    score = 0
    effect_id = str(effect.get("id") or "").lower()
    if effect_id and effect_id in haystack:
        score += 4
    generic_keywords = {
        "dream",
        "dreamy",
        "soft",
        "peaceful",
        "quiet",
        "night",
        "moon",
        "garden",
        "path",
        "village",
        "season",
        "white",
        "gold",
    }
    for keyword in effect.get("keywords") or []:
        keyword_text = str(keyword or "").strip().lower()
        if not keyword_text:
            continue
        if keyword_text in generic_keywords:
            continue
        if " " in keyword_text:
            if keyword_text in haystack:
                score += 3
        else:
            score += len(re.findall(rf"\b{re.escape(keyword_text)}\b", haystack))
    return score


def build_story_effect_overlay(settings: dict[str, Any], effect: dict[str, Any] | None) -> dict[str, Any] | None:
    if not effect:
        return None
    path_value = str(effect.get("path") or "").strip()
    if not path_value:
        return None
    path = ROOT_DIR / path_value
    if not path.exists():
        return None
    opacity = float(effect.get("opacity") or settings.get("effect_opacity") or 0.25)
    return {
        "enabled": True,
        "id": str(effect.get("id") or path.stem),
        "path": str(path),
        "blend_mode": str(effect.get("blend_mode") or "screen"),
        "opacity": max(0.0, min(1.0, opacity)),
        "source_url": str(effect.get("source_url") or ""),
    }


def story_before_sleep_render_config(settings: dict[str, Any]) -> dict[str, Any]:
    config = {
        "resolution": str(settings.get("resolution") or "1920x1080"),
        "fps": int(settings.get("fps") or 30),
        "video_bitrate": str(settings.get("video_bitrate") or "4500k"),
        "audio_bitrate": str(settings.get("audio_bitrate") or "192k"),
        "encode_preset": str(settings.get("encode_preset") or "faster"),
        "zoom_effect": bool(settings.get("zoom_effect", True)),
        "transition_effect": True,
        "fade_effect": True,
        "color_grade": True,
        "title_overlay": bool(settings.get("title_overlay", False)),
        "subtitle_overlay": bool(settings.get("subtitle_overlay", False)),
        "use_synced_subtitles": True,
        "image_segment_seconds": float(settings.get("image_segment_seconds") or 8),
        "image_transition_seconds": float(settings.get("image_transition_seconds") or 0.8),
        "background_ambience_enabled": bool(settings.get("background_ambience_enabled", False)),
        "background_ambience_path": str(settings.get("background_ambience_path") or ""),
        "background_ambience_volume": float(settings.get("background_ambience_volume") or 0.04),
        "background_ambience_duck_ratio": float(settings.get("background_ambience_duck_ratio") or 1.0),
        "background_ambience_duck_threshold": float(settings.get("background_ambience_duck_threshold") or 0.035),
        "low_bed_enabled": bool(settings.get("low_bed_enabled", False)),
        "low_bed_path": str(settings.get("low_bed_path") or ""),
        "low_bed_volume": float(settings.get("low_bed_volume") or 0.018),
        "low_bed_duck_ratio": float(settings.get("low_bed_duck_ratio") or 1.0),
        "low_bed_duck_threshold": float(settings.get("low_bed_duck_threshold") or 0.03),
        "low_bed_tone_filter": bool(settings.get("low_bed_tone_filter", False)),
        "subtitle_font_name": str(settings.get("subtitle_font_name") or "Arial Rounded MT Bold"),
        "subtitle_font_size": int(settings.get("subtitle_font_size") or 16),
        "subtitle_margin_v": int(settings.get("subtitle_margin_v") or 115),
        "subtitle_margin_h": int(settings.get("subtitle_margin_h") or 70),
        "subtitle_words_per_chunk": int(settings.get("subtitle_words_per_chunk") or 10),
        "subtitle_max_chars_per_chunk": int(settings.get("subtitle_max_chars_per_chunk") or 58),
        "subtitle_alignment": int(settings.get("subtitle_alignment") or 2),
        "subtitle_outline": float(settings.get("subtitle_outline") or 2.8),
        "subtitle_primary_color": "&H00F5F1E8",
        "subtitle_outline_color": "&HAA080808",
        "subtitle_back_color": "&H00000000",
        "subtitle_shadow": float(settings.get("subtitle_shadow") or 0),
        "subtitle_border_style": int(settings.get("subtitle_border_style") or 1),
        "subtitle_bold": bool(settings.get("subtitle_bold", True)),
    }
    if bool(settings.get("wave_asset_enabled", True)):
        wave_asset = choose_story_wave_asset(settings)
        if wave_asset:
            config["subscribe_overlay"] = {
                "enabled": True,
                "path": str(wave_asset),
                "position": str(settings.get("wave_asset_position") or "bottom-right"),
                "width_percent": float(settings.get("wave_asset_width_percent") or 13),
                "margin_percent": float(settings.get("wave_asset_margin_percent") or 3),
                "opacity": float(settings.get("wave_asset_opacity") or 0.72),
            }
    return config


def apply_story_image_timing(
    render_config: dict[str, Any],
    settings: dict[str, Any],
    audio_path: Path,
    image_count: int,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not bool(settings.get("auto_image_timing", True)):
        return render_config
    image_count = max(1, int(image_count or 1))
    try:
        audio_seconds = max(0.1, probe_duration_seconds(audio_path))
    except Exception:
        return render_config
    min_seconds = float(settings.get("image_segment_min_seconds") or 8)
    max_seconds = float(settings.get("image_segment_max_seconds") or 75)
    transition_seconds = max(0.0, float(render_config.get("image_transition_seconds") or 0))
    segment_seconds = (audio_seconds + max(0, image_count - 1) * transition_seconds) / image_count
    segment_seconds = max(min_seconds, min(max_seconds, segment_seconds))
    render_config["image_segment_seconds"] = round(segment_seconds, 3)
    render_config["contextual_image_timing"] = False
    if job is not None:
        log(
            job,
            f"Auto image timing: audio {audio_seconds:.1f}s / {image_count} image(s) = "
            f"{render_config['image_segment_seconds']:.1f}s per image "
            f"(includes {transition_seconds:.1f}s crossfade math).",
        )
    return render_config


def build_markdown(
    title: str,
    prompt: str,
    script: str,
    scene_prompts: list[str],
    images: list[Path],
    audio_path: Path,
    video_path: Path,
) -> str:
    lines = [
        f"# {title}",
        "",
        "## Source Prompt",
        prompt,
        "",
        "## Scene Prompts",
    ]
    lines.extend(f"{index}. {item}" for index, item in enumerate(scene_prompts, start=1))
    lines.extend(
        [
            "",
            "## Images Used",
            *[f"- {path}" for path in images],
            "",
            "## Audio",
            str(audio_path),
            "",
            "## Video",
            str(video_path),
            "",
            "## Script",
            script,
            "",
        ]
    )
    return "\n".join(lines)


def log(job: dict[str, Any], message: str) -> None:
    logs = job.setdefault("logs", [])
    logs.append(f"{datetime.now().strftime('%H:%M:%S')} {message}")
    emit = job.get("_emit_log")
    if callable(emit):
        emit(message)
