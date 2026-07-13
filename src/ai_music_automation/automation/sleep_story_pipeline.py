from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agents import (
    AgentContext,
    EmotionAnalyzerAgent,
    ImageReviewerAgent,
    ImageReviewInput,
    MetadataGeneratorAgent,
    MetadataGeneratorInput,
    PromptOptimizerAgent,
    PromptOptimizerInput,
    QAAgent,
    QAInput,
    RenderAgent,
    RenderAgentInput,
    ScenePlannerAgent,
    ScenePlannerInput,
    StoryPlannerAgent,
    StoryPlannerInput,
    StoryReviewerAgent,
    StoryWriterAgent,
    StoryWriterInput,
    ThumbnailGeneratorAgent,
    ThumbnailPromptInput,
    TopicGeneratorAgent,
    TopicGeneratorInput,
    VoiceAgent,
    VoiceAgentInput,
)
from .artifacts import ImageArtifact, PipelineArtifacts, SceneArtifact, StoryArtifact, VoiceArtifact
from .cache import AutomationCache
from .logging import AutomationLogger
from .niche import sleep_story_profile
from .model_client import OllamaClient
from ..media import slugify

ROOT_DIR = Path.cwd()
SLEEP_STORY_RESEARCH_DIR = ROOT_DIR / "data" / "input" / "story-before-sleep" / "research"


def run_sleep_story_automation(
    config: dict[str, Any],
    title: str = "",
    prompt: str = "",
    target_minutes: int = 3,
    voice: str = "",
    image_count: int | None = None,
    wait_for_images: bool = False,
    emit_log: callable | None = None,
) -> PipelineArtifacts:
    from ..story_before_sleep import (
        choose_story_images,
        ensure_fresh_story_images,
        ensure_story_before_sleep_dirs,
        generate_story_thumbnail,
        generate_local_story_images,
        latest_prompt_text,
        apply_story_visual_bible,
        prepare_generated_scene_prompts,
        reference_style_hint,
        sanitize_legacy_sleep_prompt,
        select_story_ambient_effect,
        select_story_background_ambience,
        apply_story_image_timing,
        story_before_sleep_render_config,
        wait_for_generated_images,
    )

    paths = ensure_story_before_sleep_dirs(config)
    settings = dict(config.get("story_before_sleep") or {})
    profile = sleep_story_profile(settings)
    target_minutes = max(1, min(30, int(target_minutes or settings.get("test_target_minutes") or 10)))
    default_title = clean_title(str(settings.get("default_title") or "A Gentle Story Before Sleep"))
    title_was_default = not str(title or "").strip() or clean_title(title) == default_title
    title = clean_title(title or default_title)
    user_prompt = prompt.strip()
    prompt = user_prompt or sanitize_legacy_sleep_prompt(latest_prompt_text(paths["prompts"]) or profile.prompt)
    voice = voice.strip() or str(settings.get("voice") or "en-US-BrianNeural")
    voice_rate = str(settings.get("voice_rate") or "-8%")
    image_count = max(1, min(32, int(image_count or settings.get("image_count") or 8)))
    reference_style = reference_style_hint(paths, settings)

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(title, 50)}"
    logger = AutomationLogger(paths["drafts"] / "logs", run_id, emit=emit_log)
    cache = AutomationCache(paths["drafts"] / "cache", enabled=bool(settings.get("cache_enabled", True)))
    context = AgentContext(niche=profile.id, settings=settings, logger=logger, cache=cache, run_id=run_id)

    with logger.stage("sleep_story_automation", title=title, target_minutes=target_minutes):
        if title_was_default and bool(settings.get("auto_topic_enabled", True)):
            existing_topics = used_sleep_story_topics(paths["drafts"])
            topics = TopicGeneratorAgent(max_retries=int(settings.get("topic_retries") or 0)).run(
                TopicGeneratorInput(
                    seed_prompt=topic_seed_prompt(prompt, settings),
                    count=int(settings.get("topic_candidate_count") or 12),
                    existing_topics=existing_topics,
                ),
                context,
            ).output
            if topics:
                title = clean_title(topics[0])
                logger.event("sleep_story_automation", "topic_selected", title=title)
        story_direction = prompt
        if title_was_default and bool(settings.get("auto_topic_enabled", True)):
            story_direction = auto_topic_story_direction(prompt, title)
        plan = StoryPlannerAgent(max_retries=int(settings.get("planner_retries") or 0)).run(
            StoryPlannerInput(topic=title, niche_prompt=story_direction, target_minutes=target_minutes),
            context,
        ).output
        if not title_was_default:
            plan = replace(plan, title=title)
        writer_prompt = build_writer_prompt(story_direction, plan)
        story = StoryWriterAgent(max_retries=int(settings.get("story_retries") or 0)).run(
            StoryWriterInput(
                title=plan.title or title,
                prompt=writer_prompt,
                target_minutes=target_minutes,
                reference_style=reference_style,
            ),
            context,
        ).output
        review_retries = max(2, int(settings.get("review_retries") or 0))
        review = StoryReviewerAgent(max_retries=review_retries).run(story, context).output
        rewrite_attempts = max(1, min(3, int(settings.get("content_rewrite_attempts") or 2)))
        for rewrite_attempt in range(1, rewrite_attempts + 1):
            if review.passed or not bool(settings.get("auto_rewrite_on_review_fail", True)):
                break
            logger.event(
                "story_review",
                "rewrite_requested",
                attempt=rewrite_attempt,
                score=review.score,
                notes=review.notes[:6],
            )
            rewrite_prompt = build_review_rewrite_prompt(story_direction, plan, review.notes)
            rewrite_prompt += (
                f"\n\nREWRITE PASS {rewrite_attempt} OF {rewrite_attempts}:\n"
                "Create a genuinely new revision, not a paraphrase of the previous draft. "
                "Resolve every listed hard-gate note through concrete events and causal story beats."
            )
            rewritten_story = StoryWriterAgent(max_retries=int(settings.get("story_retries") or 0)).run(
                StoryWriterInput(
                    title=plan.title or title,
                    prompt=rewrite_prompt,
                    target_minutes=target_minutes,
                    reference_style=reference_style,
                ),
                context,
            ).output
            rewritten_review = StoryReviewerAgent(max_retries=review_retries).run(rewritten_story, context).output
            if rewritten_review.passed:
                story = rewritten_story
                review = rewritten_review
                logger.event(
                    "story_review", "rewrite_used", attempt=rewrite_attempt, score=review.score, passed=True
                )
                break
            logger.event(
                "story_review",
                "rewrite_discarded",
                attempt=rewrite_attempt,
                old_score=review.score,
                new_score=rewritten_review.score,
                new_passed=False,
            )
            if rewritten_review.score >= review.score:
                story = rewritten_story
                review = rewritten_review
        if not review.passed:
            raise RuntimeError(
                f"Sleep Story content review failed at {review.score:.1f}: " + "; ".join(review.notes[:8])
            )
        story = StoryArtifact(
            title=story.title,
            prompt=story.prompt,
            script=story.script,
            outline="\n".join(plan.outline),
            hook=plan.hook or story.hook,
            ending=plan.ending or story.ending,
            lesson=plan.lesson,
            score=review.score,
            review_notes=review.notes,
        )

        visual_bible = apply_story_visual_bible(settings, story)
        context.settings = settings
        logger.event("visual_bible", "created", **visual_bible)

        emotions = EmotionAnalyzerAgent().run(story, context).output
        scenes = ScenePlannerAgent().run(
            ScenePlannerInput(story=story, emotions=emotions, max_scenes=image_count),
            context,
        ).output
        scenes = PromptOptimizerAgent().run(
            PromptOptimizerInput(scenes=scenes, reference_style=reference_style),
            context,
        ).output
        scene_prompts = [scene.image_prompt or scene.summary for scene in scenes]

        with logger.stage("scene_prompt_files", title=story.title, scene_count=len(scene_prompts)):
            generated_dir = prepare_generated_scene_prompts(paths, story.title, scene_prompts, reference_style)
            logger.event("scene_prompt_files", "saved", path=str(generated_dir))

        # Finish every Ollama-dependent text task before loading ComfyUI or TTS.
        thumbnail_prompt = ThumbnailGeneratorAgent().run(
            ThumbnailPromptInput(story=story, niche=profile.id),
            context,
        ).output
        metadata = MetadataGeneratorAgent().run(
            MetadataGeneratorInput(
                story=story,
                scenes=scenes,
                target_minutes=target_minutes,
                thumbnail_prompt=thumbnail_prompt,
            ),
            context,
        ).output
        if bool(settings.get("unload_ollama_before_media", True)):
            unloaded = OllamaClient().unload(
                str(settings.get("ollama_url") or "http://127.0.0.1:11434"),
                str(settings.get("ollama_model") or settings.get("model") or "gemma4:e2b"),
                force_after_seconds=int(settings.get("ollama_unload_wait_seconds") or 15),
            )
            logger.event("resource_manager", "ollama_unloaded" if unloaded else "ollama_unload_skipped")
        image_job = {"_emit_log": emit_log} if emit_log else {}
        parallel_media = bool(settings.get("parallel_media_generation", True))

        def generate_scene_images_task() -> None:
            with logger.stage("image_generation", provider=str(settings.get("image_provider") or "sd_webui"), target_count=image_count):
                image_attempts = max(1, min(3, int(settings.get("image_generation_attempts") or 2)))
                last_error: Exception | None = None
                for image_attempt in range(1, image_attempts + 1):
                    try:
                        generate_local_story_images(image_job, settings, generated_dir, scene_prompts, image_count)
                        ensure_fresh_story_images(settings, generated_dir, image_count)
                        last_error = None
                        break
                    except RuntimeError as exc:
                        last_error = exc
                        if image_attempt >= image_attempts:
                            raise
                        logger.event(
                            "image_generation",
                            "retry_missing_scenes",
                            attempt=image_attempt,
                            error=str(exc),
                        )
                if last_error:
                    raise last_error
                provider = str(settings.get("image_provider") or "sd_webui").strip().lower()
                if provider in {"imagegen_local", "local_diffusers", "diffusers"} and not bool(settings.get("allow_image_placeholders", False)):
                    real_images = [
                        path
                        for path in generated_dir.glob("scene-*.png")
                        if path.is_file() and "placeholder" not in path.stem.lower()
                    ]
                    if len(real_images) < image_count:
                        raise RuntimeError(
                            f"Image generation produced only {len(real_images)}/{image_count} real scene images. "
                            "Render stopped so placeholder images do not appear in the video."
                        )

        def generate_voice_task():
            return VoiceAgent(max_retries=max(1, int(settings.get("voice_retries") or 1))).run(
                VoiceAgentInput(story=story, output_dir=paths["output"], voice=voice, rate=voice_rate),
                context,
            ).output

        voice_artifact = None
        if parallel_media:
            logger.event("parallel_media", "started", tasks=["voice", "scene_images"])
            with ThreadPoolExecutor(max_workers=2) as executor:
                voice_future = executor.submit(generate_voice_task)
                image_future = executor.submit(generate_scene_images_task)
                image_future.result()
                voice_artifact = voice_future.result()
            logger.event("parallel_media", "finished", tasks=["voice", "scene_images"])
        else:
            voice_artifact = generate_voice_task()
            generate_scene_images_task()
        if wait_for_images:
            wait_for_generated_images(image_job, generated_dir, image_count, int(settings.get("wait_for_generated_images_seconds") or 900))
        with logger.stage("choose_images", title=story.title, target_count=image_count):
            chosen_paths = choose_story_images(paths, story.title, scene_prompts, image_count, generated_dir)
            logger.event("choose_images", "selected", count=len(chosen_paths), files=[path.name for path in chosen_paths])
        with logger.stage("review_images", count=len(chosen_paths)):
            image_artifacts = review_images(chosen_paths, scenes, context)
            logger.event(
                "review_images",
                "scored",
                scores=[{"scene": image.scene_index, "file": image.path.name, "score": image.score} for image in image_artifacts],
            )

        def generate_thumbnail_task():
            with logger.stage("thumbnail_image", title=story.title):
                thumbnail = generate_story_thumbnail(
                    job=image_job,
                    settings=settings,
                    paths=paths,
                    story=story,
                    thumbnail_prompt=thumbnail_prompt,
                )
                logger.event("thumbnail_image", "saved", path=str(thumbnail) if thumbnail else "")
                return thumbnail

        thumbnail_path = generate_thumbnail_task()
        if thumbnail_path:
            metadata = replace(metadata, thumbnail_path=thumbnail_path)

        if voice_artifact is None:
            voice_artifact = generate_voice_task()
        speech_qa = None
        if bool(settings.get("speech_qa_enabled", True)):
            from ..speech_qa import inspect_speech

            with logger.stage("speech_qa", audio=voice_artifact.path.name):
                speech_qa = inspect_speech(
                    voice_artifact.path,
                    story.script,
                    model_name=str(settings.get("speech_qa_model") or "tiny.en"),
                    language="en",
                    cache_dir=ROOT_DIR / str(
                        settings.get("speech_qa_model_dir")
                        or "tools/speech-qa-models"
                    ),
                    threshold=float(settings.get("speech_qa_threshold") or 0.72),
                )
                logger.event("speech_qa", "passed" if speech_qa.passed else "failed", **speech_qa.to_dict())
            if speech_qa.available and not speech_qa.passed and not bool(
                settings.get("render_on_speech_qa_fail", False)
            ):
                raise RuntimeError("Sleep Story speech QA failed: " + "; ".join(speech_qa.notes))
        qa = QAAgent().run(
            QAInput(
                story=story,
                images=image_artifacts,
                audio_path=voice_artifact.path,
                metadata=metadata,
                target_minutes=target_minutes,
            ),
            context,
        ).output
        if not qa.passed and not bool(settings.get("render_on_qa_fail", False)):
            raise RuntimeError(f"Sleep Story QA failed: {'; '.join(qa.notes)}")

        render_config = story_before_sleep_render_config(settings)
        apply_story_image_timing(
            render_config,
            settings,
            voice_artifact.path,
            len(image_artifacts),
            job=image_job,
        )
        logger.event(
            "render",
            "image_timing_selected",
            image_count=len(image_artifacts),
            image_segment_seconds=render_config.get("image_segment_seconds"),
        )
        ambient_effect = select_story_ambient_effect(settings, story=story, scenes=scenes)
        if ambient_effect:
            render_config["ambient_overlay"] = ambient_effect
            logger.event(
                "render",
                "ambient_effect_selected",
                id=ambient_effect.get("id"),
                path=ambient_effect.get("path"),
                blend_mode=ambient_effect.get("blend_mode"),
                opacity=ambient_effect.get("opacity"),
            )
        background_ambience = select_story_background_ambience(settings, story=story, scenes=scenes)
        if background_ambience:
            render_config.update(background_ambience)
            logger.event(
                "render",
                "background_ambience_selected",
                id=background_ambience.get("background_ambience_id"),
                path=background_ambience.get("background_ambience_path"),
                volume=background_ambience.get("background_ambience_volume"),
            )
        video_path = RenderAgent().run(
            RenderAgentInput(
                story=story,
                voice=voice_artifact,
                images=image_artifacts,
                output_dir=paths["output"],
                render_config=render_config,
                suffix="-sbs-test",
            ),
            context,
        ).output
        from ..media import probe_duration_seconds
        from ..media_qa import inspect_media, inspect_subtitle

        voice_duration = probe_duration_seconds(voice_artifact.path)
        final_media_qa = inspect_media(
            video_path,
            expected_duration_seconds=voice_duration,
        )
        subtitle_notes = inspect_subtitle(voice_artifact.subtitle_path, voice_duration)
        logger.event("final_media_qa", "passed" if final_media_qa.passed else "failed", **final_media_qa.to_dict())
        if subtitle_notes:
            logger.event("final_media_qa", "subtitle_warning", notes=subtitle_notes)
        if not final_media_qa.passed:
            raise RuntimeError(f"Sleep Story final media QA failed: {'; '.join(final_media_qa.notes)}")

        markdown_path, json_path = write_pipeline_draft(
            paths=paths,
            draft_id=run_id,
            prompt=story_direction,
            story=story,
            scene_prompts=scene_prompts,
            images=image_artifacts,
            audio_path=voice_artifact.path,
            video_path=video_path,
            metadata=metadata,
            qa=qa,
        )
        return PipelineArtifacts(
            niche=profile.id,
            story=story,
            scenes=scenes,
            images=image_artifacts,
            voice=voice_artifact,
            metadata=metadata,
            video_path=video_path,
            draft_json=json_path,
            draft_markdown=markdown_path,
            extra={
                "qa": asdict(qa),
                "final_media_qa": final_media_qa.to_dict(),
                "speech_qa": speech_qa.to_dict() if speech_qa else {},
                "review": asdict(review),
                "plan": asdict(plan),
                "visual_bible": visual_bible,
            },
        )


def resume_sleep_story_automation(
    config: dict[str, Any],
    checkpoint_path: str | Path,
    target_minutes: int,
    voice: str,
    image_count: int,
    emit_log: callable | None = None,
) -> PipelineArtifacts:
    """Resume an interrupted run after story, voice, prompts, and images exist."""
    from ..media import probe_duration_seconds
    from ..media_qa import inspect_media, inspect_subtitle
    from ..story_before_sleep import (
        apply_story_image_timing,
        choose_story_images,
        ensure_story_before_sleep_dirs,
        generate_story_thumbnail,
        select_story_ambient_effect,
        select_story_background_ambience,
        story_before_sleep_render_config,
    )

    manifest_path = Path(checkpoint_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    paths = ensure_story_before_sleep_dirs(config)
    settings = dict(config.get("story_before_sleep") or {})
    profile = sleep_story_profile(settings)
    target_minutes = max(1, min(30, int(target_minutes or 10)))
    image_count = max(1, min(32, int(image_count or 8)))
    voice = str(voice or settings.get("voice") or "kokoro-en:bm_lewis")
    voice_rate = str(settings.get("voice_rate") or "-8%")

    story_data = json.loads(Path(manifest["story_cache"]).read_text(encoding="utf-8-sig"))
    story = StoryArtifact(
        title=str(story_data.get("title") or manifest.get("title") or "Sleep Story"),
        prompt=str(story_data.get("prompt") or ""),
        script=str(story_data.get("script") or ""),
        hook=str(story_data.get("hook") or ""),
        ending=str(story_data.get("ending") or ""),
        lesson=str(story_data.get("lesson") or ""),
        score=float(story_data.get("score") or 0),
    )
    prompts_data = json.loads(Path(manifest["scene_prompts"]).read_text(encoding="utf-8-sig"))
    prompt_values = [str(item) for item in prompts_data.get("scene_prompts", []) if str(item).strip()]
    if not prompt_values:
        raise RuntimeError("Sleep Story resume checkpoint has no scene prompts.")
    scenes = [
        SceneArtifact(index=index, label=f"Scene {index}", summary=prompt, image_prompt=prompt)
        for index, prompt in enumerate(prompt_values, start=1)
    ]
    generated_dir = Path(manifest["generated_dir"])
    audio_path = Path(manifest["audio_path"])
    subtitle_path = Path(manifest["subtitle_path"])
    voice_artifact = VoiceArtifact(
        path=audio_path,
        voice=voice,
        rate=voice_rate,
        transcript_path=audio_path.with_suffix(".txt"),
        subtitle_path=subtitle_path,
    )
    run_id = f"{manifest.get('run_id') or manifest_path.stem}-resume-{datetime.now().strftime('%H%M%S')}"
    logger = AutomationLogger(paths["drafts"] / "logs", run_id, emit=emit_log)
    cache = AutomationCache(paths["drafts"] / "cache", enabled=bool(settings.get("cache_enabled", True)))
    context = AgentContext(niche=profile.id, settings=settings, logger=logger, cache=cache, run_id=run_id)
    image_job = {"_emit_log": emit_log} if emit_log else {}

    with logger.stage("sleep_story_resume", checkpoint=manifest_path.name, title=story.title):
        logger.event("resume", "artifacts_reused", images=image_count, audio=audio_path.name)
        review = StoryReviewerAgent(max_retries=max(2, int(settings.get("review_retries") or 0))).run(
            story, context
        ).output
        if not review.passed:
            raise RuntimeError(
                f"Sleep Story resume content review failed at {review.score:.1f}: "
                + "; ".join(review.notes[:8])
            )
        with logger.stage("choose_images", title=story.title, target_count=image_count):
            chosen_paths = choose_story_images(paths, story.title, prompt_values, image_count, generated_dir)
            logger.event("choose_images", "selected", count=len(chosen_paths), files=[path.name for path in chosen_paths])
        if len(chosen_paths) < image_count:
            raise RuntimeError(f"Sleep Story resume found only {len(chosen_paths)}/{image_count} images.")
        with logger.stage("review_images", count=len(chosen_paths)):
            image_artifacts = review_images(chosen_paths, scenes, context)

        thumbnail_prompt = ThumbnailGeneratorAgent().run(
            ThumbnailPromptInput(story=story, niche=profile.id), context
        ).output
        metadata = MetadataGeneratorAgent().run(
            MetadataGeneratorInput(
                story=story,
                scenes=scenes,
                target_minutes=target_minutes,
                thumbnail_prompt=thumbnail_prompt,
            ),
            context,
        ).output
        if bool(settings.get("unload_ollama_before_media", True)):
            unloaded = OllamaClient().unload(
                str(settings.get("ollama_url") or "http://127.0.0.1:11434"),
                str(settings.get("ollama_model") or settings.get("model") or "gemma4:e2b"),
                force_after_seconds=int(settings.get("ollama_unload_wait_seconds") or 15),
            )
            logger.event("resource_manager", "ollama_unloaded" if unloaded else "ollama_unload_skipped")
        with logger.stage("thumbnail_image", title=story.title):
            thumbnail_path = generate_story_thumbnail(image_job, settings, paths, story, thumbnail_prompt)
        if thumbnail_path:
            metadata = replace(metadata, thumbnail_path=thumbnail_path)

        speech_qa = None
        if bool(settings.get("speech_qa_enabled", True)):
            from ..speech_qa import inspect_speech
            with logger.stage("speech_qa", audio=audio_path.name):
                speech_qa = inspect_speech(
                    audio_path,
                    story.script,
                    model_name=str(settings.get("speech_qa_model") or "tiny.en"),
                    language="en",
                    cache_dir=ROOT_DIR / str(settings.get("speech_qa_model_dir") or "tools/speech-qa-models"),
                    threshold=float(settings.get("speech_qa_threshold") or 0.72),
                )
            if speech_qa.available and not speech_qa.passed and not bool(settings.get("render_on_speech_qa_fail", False)):
                raise RuntimeError("Sleep Story speech QA failed: " + "; ".join(speech_qa.notes))

        qa = QAAgent().run(
            QAInput(story=story, images=image_artifacts, audio_path=audio_path, metadata=metadata, target_minutes=target_minutes),
            context,
        ).output
        if not qa.passed and not bool(settings.get("render_on_qa_fail", False)):
            raise RuntimeError(f"Sleep Story QA failed: {'; '.join(qa.notes)}")

        render_config = story_before_sleep_render_config(settings)
        apply_story_image_timing(render_config, settings, audio_path, len(image_artifacts), job=image_job)
        ambient_effect = select_story_ambient_effect(settings, story=story, scenes=scenes)
        if ambient_effect:
            render_config["ambient_overlay"] = ambient_effect
        background_ambience = select_story_background_ambience(settings, story=story, scenes=scenes)
        if background_ambience:
            render_config.update(background_ambience)
        video_path = RenderAgent().run(
            RenderAgentInput(
                story=story,
                voice=voice_artifact,
                images=image_artifacts,
                output_dir=paths["output"],
                render_config=render_config,
                suffix="-sbs-test",
            ),
            context,
        ).output
        voice_duration = probe_duration_seconds(audio_path)
        final_media_qa = inspect_media(video_path, expected_duration_seconds=voice_duration)
        subtitle_notes = inspect_subtitle(subtitle_path, voice_duration)
        if not final_media_qa.passed:
            raise RuntimeError(f"Sleep Story final media QA failed: {'; '.join(final_media_qa.notes)}")

        markdown_path, json_path = write_pipeline_draft(
            paths=paths,
            draft_id=run_id,
            prompt=story.prompt,
            story=story,
            scene_prompts=prompt_values,
            images=image_artifacts,
            audio_path=audio_path,
            video_path=video_path,
            metadata=metadata,
            qa=qa,
        )
        return PipelineArtifacts(
            niche=profile.id,
            story=story,
            scenes=scenes,
            images=image_artifacts,
            voice=voice_artifact,
            metadata=metadata,
            video_path=video_path,
            draft_json=json_path,
            draft_markdown=markdown_path,
            extra={
                "qa": asdict(qa),
                "final_media_qa": final_media_qa.to_dict(),
                "speech_qa": speech_qa.to_dict() if speech_qa else {},
                "review": asdict(review),
                "subtitle_notes": subtitle_notes,
                "resumed_from": str(manifest_path),
            },
        )


def review_images(paths: list[Path], scenes: list, context: AgentContext) -> list[ImageArtifact]:
    artifacts: list[ImageArtifact] = []
    reviewer = ImageReviewerAgent()
    for index, path in enumerate(paths, start=1):
        scene = scenes[min(index - 1, len(scenes) - 1)] if scenes else None
        if scene is None:
            continue
        reviewed = reviewer.run(ImageReviewInput(scene=scene, candidates=[path]), context).output
        if reviewed is None:
            reviewed = ImageArtifact(scene_index=scene.index, path=path, prompt=scene.image_prompt, score=0.0, reviewer="fallback")
        artifacts.append(reviewed)
    return artifacts


def write_pipeline_draft(
    paths: dict[str, Path],
    draft_id: str,
    prompt: str,
    story: StoryArtifact,
    scene_prompts: list[str],
    images: list[ImageArtifact],
    audio_path: Path,
    video_path: Path,
    metadata,
    qa,
) -> tuple[Path, Path]:
    from ..story_before_sleep import build_markdown

    markdown_path = paths["drafts"] / f"{draft_id}.md"
    json_path = paths["drafts"] / f"{draft_id}.json"
    markdown_path.write_text(
        build_markdown(
            story.title,
            prompt,
            story.script,
            scene_prompts,
            [image.path for image in images],
            audio_path,
            video_path,
        ),
        encoding="utf-8",
    )
    metadata_payload = asdict(metadata)
    if metadata_payload.get("thumbnail_path") is not None:
        metadata_payload["thumbnail_path"] = str(metadata_payload["thumbnail_path"])
    draft = {
        "id": draft_id,
        "title": story.title,
        "prompt": prompt,
        "script": story.script,
        "scene_prompts": scene_prompts,
        "images": [str(image.path) for image in images],
        "audio": str(audio_path),
        "audio_name": audio_path.name,
        "video": str(video_path),
        "video_name": video_path.name,
        "markdown": str(markdown_path),
        "markdown_name": markdown_path.name,
        "metadata": metadata_payload,
        "qa": asdict(qa),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "sleep-story-automation",
    }
    json_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return markdown_path, json_path


def used_sleep_story_topics(drafts_dir: Path) -> list[str]:
    topics: list[str] = []
    for path in sorted(drafts_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:80]:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        title = str(data.get("title") or "").strip()
        generated = str((data.get("metadata") or {}).get("title") or "").strip()
        if title:
            topics.append(title)
        if generated:
            topics.append(generated)
    return topics


def topic_seed_prompt(source_prompt: str, settings: dict[str, Any]) -> str:
    configured = str(settings.get("topic_seed_prompt") or "").strip()
    if configured:
        return configured
    variety = str(settings.get("story_variety_prompt") or "").strip()
    benchmark = sleep_story_benchmark_text(1800)
    return (
        "Create varied English bedtime story topics for Sleepu Stories. "
        "Each topic needs a different gentle setting, character, tiny emotional problem, and lesson. "
        "Use the current visual style only as a soft art direction, not as the repeated story premise.\n"
        f"Variety bank: {variety or 'fairytale, cozy mystery, gentle adventure, magical realism, seaside tale, winter tale, train journey, library tale, bakery tale'}\n\n"
        f"Sleepu benchmark:\n{benchmark}\n\n"
        f"Style/source direction:\n{source_prompt[:1200]}"
    )


def auto_topic_story_direction(source_prompt: str, topic: str) -> str:
    benchmark = sleep_story_benchmark_text()
    return f"""Write a new English bedtime story for Sleepu Stories.

Current topic:
{topic}

The story premise must follow the current topic, not the old reference prompt.
Use the old prompt below only as visual mood/style guidance: dreamy, moonlit, soft, whimsical,
storybook, calm, safe, gentle mist, subtle bloom, peaceful nostalgic mood.

Do not reuse Elara unless the topic explicitly asks for Elara.
Do not reuse the same child-under-the-moon meadow plot.
Do not force fireflies, moon meadow, yellow dress, or distant village lights unless they naturally fit the new topic.

Old visual direction, for style only:
{source_prompt[:1200]}

Sleepu Stories benchmark to follow as reusable standards, not copied content:
{benchmark or "Use adult sleep-story best practices: direct emotional hook, concrete memory, small magical mystery, calm micro-journey, adult sleep ending."}
"""


def build_writer_prompt(source_prompt: str, plan) -> str:
    outline = "\n".join(f"- {item}" for item in plan.outline)
    return f"""{source_prompt}

Story plan:
Hook: {plan.hook}
Outline:
{outline}
Ending: {plan.ending}
Lesson: {plan.lesson}
"""


def build_review_rewrite_prompt(source_prompt: str, plan, review_notes: list[str]) -> str:
    notes = "\n".join(f"- {note}" for note in review_notes[:10]) or "- Improve long-form story quality."
    return f"""{build_writer_prompt(source_prompt, plan)}

Rewrite requirement from quality judges:
{notes}

Before writing, obey this long-form sequence:
1. Description: establish character, setting, object, and adult emotional promise.
2. Discovery: the first visible magical clue appears.
3. Small mystery: create a calm question that keeps the listener curious.
4. Discovery: follow the clue into a new visual place.
5. Memory: reveal one concrete memory behind the burden.
6. New room/place: enter a clearly different drawable location.
7. New object/object change: introduce or transform one symbolic object.
8. Another revelation: reveal a second quiet truth through action.
9. Kind choice: the character gives up control, pride, certainty, or loneliness through a visible gesture.
10. Sleep resolution: one final adult sleep sign-off only at the very end.

Avoid the previous failure modes:
- no dangling hook
- no early sleep ending before the final paragraph
- no guided meditation as the main format
- no long abstract relaxation tail after the story already ended
- reduce repeated mood words by replacing them with concrete actions, objects, and places
"""


def sleep_story_benchmark_text(max_chars: int = 3200) -> str:
    path = SLEEP_STORY_RESEARCH_DIR / "sleepu_benchmark.md"
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    return text[:max_chars].strip()


def clean_title(value: str) -> str:
    return " ".join((value or "").split())[:100] or "A Gentle Story Before Sleep"
