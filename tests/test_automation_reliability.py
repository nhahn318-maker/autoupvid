from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image

from ai_music_automation.agents.qa_agent import near_duplicate_image_pairs
from ai_music_automation.agents.base import AgentContext
from ai_music_automation.automation.artifacts import ImageArtifact
from ai_music_automation.automation.cache import AutomationCache
from ai_music_automation.automation.logging import AutomationLogger
from ai_music_automation.automation.model_client import ModelRequest, OllamaClient
from ai_music_automation.job_store import JobStore
from ai_music_automation.media_qa import inspect_media, inspect_subtitle
from ai_music_automation.metadata import ensure_vietnamese_buddhist_short_hashtags
from ai_music_automation.story_before_sleep import apply_story_visual_bible, build_story_visual_bible, infer_character_gender_label
from ai_music_automation.agents.story_writer import polish_adult_sleep_story_script, repair_complete_sleep_story_script
from ai_music_automation.agents.story_planner import StoryPlan, validate_story_plan
from ai_music_automation.youtube_reporting import aggregate_reporting_csv, reporting_windows
from ai_music_automation.web import (
    analyze_long_script_duplicates,
    build_long_chapter_continuation_prompt,
    long_chapter_overlap_ratio,
    merge_short_state_for_account,
    repair_long_script_duplicate_units,
    sanitize_vi_shorts_response,
    scoped_short_state_for_account,
    _vi_short_hook_replacement,
)
from ai_music_automation.agents.story_reviewer import (
    StoryReviewerAgent,
    StoryReview,
    build_review_prompt,
    content_gate_violations,
    parse_review_response,
    reconcile_anomalous_positive_review,
)
from ai_music_automation.automation.artifacts import StoryArtifact


class ReliabilityTests(unittest.TestCase):
    def test_job_store_preserves_payload_and_marks_running_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder) / "jobs.sqlite3")
            job = {
                "id": "job-1",
                "action": "story-before-sleep-auto",
                "status": "running",
                "created_at": "2026-01-01T00:00:00",
                "logs": [],
            }
            store.save(job, {"title": "Test"})
            store.mark_interrupted_runs()
            restored, payload = store.load_recent()[0]
            self.assertEqual(restored["status"], "interrupted")
            self.assertEqual(payload["title"], "Test")

    def test_cache_writes_valid_json_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = AutomationCache(Path(folder))
            path = cache.write_json("sample", {"title": "Binh an", "score": 91})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["score"], 91)
            self.assertFalse(path.with_suffix(path.suffix + ".tmp").exists())

    def test_cache_parallel_writes_do_not_share_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = AutomationCache(Path(folder))
            threads = [
                threading.Thread(target=cache.write_json, args=("shared", {"writer": index}))
                for index in range(6)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            result = cache.read_json("shared")
            self.assertIsInstance(result, dict)
            self.assertIn("writer", result)
            self.assertFalse(list(Path(folder).glob("*.tmp")))

    def test_buddhist_short_description_uses_dynamic_five_hashtags(self) -> None:
        description = (
            "Hay hoc cach buong bo oan trach de tam an hon.\n\n"
            "#loiphatday #phatphap #gieobinhan #doivodinh #songtute #chualanhtamhon #nhanqua"
        )
        cleaned = ensure_vietnamese_buddhist_short_hashtags(description, "Buong Bo De Tam An")
        hashtags = re.findall(r"#[\wÀ-ỹĐđ]+", cleaned)
        self.assertEqual(len(hashtags), 5)
        self.assertIn("#buongbo", hashtags)
        self.assertIn("#taman", hashtags)
        self.assertIn("#shorts", hashtags)

    def test_gender_inference_uses_dominant_character_signals(self) -> None:
        male_story = "Elias was a young man. He kept his clock. A woman waved once, then he returned home."
        female_story = "Elara was a young woman. She carried her book. A man opened the gate for her."
        self.assertEqual(infer_character_gender_label(male_story.lower()), "male")
        self.assertEqual(infer_character_gender_label(female_story.lower()), "female")

    def test_sleep_story_visual_bible_does_not_append_old_reference_memory_by_default(self) -> None:
        story = StoryArtifact(
            title="A Lighthouse Keeper's Wait",
            prompt="Old visual direction mentions yellow dress, forest, cottage, letter, and clock.",
            hook="If waiting has felt heavy, this lighthouse story may help.",
            outline=["Silas follows a lantern through a lighthouse chamber."],
            script=(
                "Silas was an adult male lighthouse keeper with dark hair and a blue coat. "
                "He held the lantern beside the stone window and walked down to the sea chamber. "
                "The lantern showed him how to set down the need to hurry dawn."
            ),
            lesson="Rest can come from trusting the patient lantern.",
        )
        settings = {
            "character_memory": "old reference character in a yellow dress with a clock",
            "world_memory": "old meadow, forest, cottage, village world",
        }
        bible = apply_story_visual_bible(settings, story)
        combined = f"{settings['character_memory']} {settings['world_memory']} {bible['story_character_identity_lock']}".lower()
        self.assertIn("lighthouse", settings["world_memory"].lower())
        self.assertIn("lantern", settings["character_memory"].lower())
        self.assertNotIn("yellow dress", combined)
        self.assertNotIn("cottage", settings["world_memory"].lower())
        self.assertNotIn("same outfit/accessory palette: lantern", combined)

    def test_duplicate_images_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "first.png"
            second = root / "second.png"
            Image.new("RGB", (640, 360), (40, 60, 90)).save(first)
            Image.new("RGB", (640, 360), (40, 60, 90)).save(second)
            artifacts = [
                ImageArtifact(1, first),
                ImageArtifact(2, second),
            ]
            self.assertEqual(near_duplicate_image_pairs(artifacts), [(1, 2)])

    def test_subtitle_end_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "sample.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:05,000\nHello\n", encoding="utf-8")
            self.assertTrue(inspect_subtitle(subtitle, 60))
            self.assertFalse(inspect_subtitle(subtitle, 5))

    def test_media_qa_accepts_small_valid_av_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sample.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=navy:s=640x360:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=220:duration=1",
                    "-shortest", "-c:v", "libx264", "-c:a", "aac", str(output),
                ],
                check=True,
            )
            report = inspect_media(output, expected_duration_seconds=1, sample_audio=False)
            self.assertTrue(report.passed, report.notes)
            self.assertTrue(report.has_audio)
            self.assertTrue(report.has_video)

    def test_long_script_duplicate_qa_rejects_reused_padding(self) -> None:
        repeated = "Khi tâm biết dừng lại, ta có thể nhìn nỗi khổ bằng sự hiểu biết và lòng từ bi."
        script = "\n\n".join(
            [
                repeated,
                "Một người mẹ ngồi bên cửa sổ và nhận ra mình đã nói quá nhanh trong lúc nóng giận.",
                repeated,
                "Sáng hôm sau, bà chọn xin lỗi con bằng một lời chân thành và một hành động tử tế.",
                repeated,
                "Từ việc nhỏ ấy, gia đình bắt đầu học cách lắng nghe nhau trước khi phán xét.",
                repeated,
            ]
        )
        report = analyze_long_script_duplicates(script)
        self.assertFalse(report["passed"])
        self.assertEqual(report["max_repeat"], 4)

    def test_long_script_duplicate_qa_warns_on_moderate_reused_opening(self) -> None:
        repeated = "Con co bao gio tu hoi vi sao cuoc doi con nhu vay?"
        paragraphs = []
        for index in range(5):
            paragraphs.append(
                repeated
                + f" Cau chuyen rieng thu {index} ke ve mot nguoi trong gia dinh, mot loi noi, "
                + "mot lua chon thien lanh va mot cach nhin nhan qua khac nhau trong doi song."
            )
        paragraphs.extend(
            f"Doan rieng {index} noi ve mot canh sinh hoat, mot bai hoc va mot cach thuc hanh khong trung lap."
            for index in range(80)
        )
        report = analyze_long_script_duplicates("\n\n".join(paragraphs))
        self.assertTrue(report["passed"])
        self.assertTrue(report["warning"])
        self.assertEqual(report["max_repeat"], 5)

    def test_long_script_duplicate_repair_rewrites_repeated_openings(self) -> None:
        repeated = "Con co bao gio tu hoi vi sao cuoc doi con nhu vay?"
        text = "\n\n".join(
            repeated + f" Phan rieng {index} co mot canh doi song va mot bai hoc Phat phap khac nhau."
            for index in range(9)
        )
        before = analyze_long_script_duplicates(text)
        repaired, changed = repair_long_script_duplicate_units(text, max_repeat=3)
        after = analyze_long_script_duplicates(repaired)
        self.assertGreater(changed, 0)
        self.assertLess(after["max_repeat"], before["max_repeat"])
        self.assertLessEqual(after["max_repeat"], 3)

    def test_long_chapter_overlap_detects_cross_chapter_copy(self) -> None:
        shared = "Trong đời sống hằng ngày, một lời nói thiếu tỉnh thức có thể làm người thân tổn thương rất lâu."
        previous = [
            f"{shared}\n\nTa có thể bắt đầu sửa đổi bằng cách lắng nghe và nhận lỗi một cách chân thành."
        ]
        current = (
            f"{shared}\n\n"
            "Một buổi chiều, Minh dừng cuộc tranh luận và rót cho cha một chén trà nóng.\n\n"
            "Hành động nhỏ đó giúp anh hiểu rằng im lặng đúng lúc cũng là một cách giữ gìn khẩu nghiệp."
        )
        self.assertGreater(long_chapter_overlap_ratio(current, previous), 0.30)

    def test_sleep_story_review_preserves_component_scores(self) -> None:
        response = json.dumps(
            {
                "score": 84,
                "passed": True,
                "subscores": {
                    "story_structure": 88,
                    "retention": 72,
                    "psychology": 90,
                    "emotional_specificity": 66,
                    "sleep_quality": 94,
                    "visual_variety": 80,
                    "ai_repetition": 68,
                },
                "notes": ["Good but could use stronger curiosity beats."],
                "revised_script": "",
            }
        )
        review = parse_review_response(response, 82)
        self.assertTrue(review.passed)
        self.assertEqual(review.subscores["retention"], 72)
        self.assertIn("Weakest: emotional_specificity=66, ai_repetition=68", review.notes[0])

    def test_sleep_story_plan_validator_adds_concrete_premise_lock(self) -> None:
        plan = StoryPlan(
            title="A Lighthouse Keeper's Wait",
            hook="If you have been carrying the ache of waiting, this story may help you rest.",
            outline=["beat 1 DESCRIPTION: lighthouse window and lantern."],
            ending="The keeper rests by the lantern.",
            lesson="Waiting can become tender when it is shared with memory.",
        )
        repaired = validate_story_plan(plan, "lighthouse")
        self.assertIn("PREMISE LOCK:", repaired.outline[0])
        self.assertTrue(repaired.concrete_memory_object)
        self.assertTrue(repaired.choice_action)

    def test_sleep_story_gate_catches_deep_sleep_before_final_section(self) -> None:
        early = (
            "If you have been carrying an old promise, this story may help you rest. "
            "Mara kept a brass key beside the window and remembered the cup her friend had saved. "
            "She drifted into a deep and restorative sleep. "
        )
        tail = " ".join(["The lantern showed another room and she followed the path."] * 80)
        final = " At last, Mara set down the key. You may rest now, safe in the quiet light."
        violations = content_gate_violations(early + tail + final)
        self.assertTrue(any("sleep ending appears before" in item for item in violations))

    def test_sleep_story_polish_replaces_abstract_filler_phrases(self) -> None:
        script = (
            "Mara felt deep peace and profound stillness. "
            "The slow unfolding of time gave her quiet acceptance."
        )
        polished = polish_adult_sleep_story_script(script)
        lowered = polished.lower()
        self.assertNotIn("deep peace", lowered)
        self.assertNotIn("profound stillness", lowered)
        self.assertNotIn("quiet acceptance", lowered)
        self.assertIn("hands loosening", lowered)

    def test_long_continuation_prompt_is_compact_enough_for_local_model(self) -> None:
        original = "YÊU CẦU CHƯƠNG " + ("nội dung yêu cầu " * 900)
        current = "Mở đầu chương. " + ("Một ví dụ đời thường giúp người nghe hiểu rõ hơn. " * 350)
        prompt = build_long_chapter_continuation_prompt(
            original,
            current,
            minimum_words=1000,
            target_words=1100,
        )
        self.assertLess(len(prompt), 6000)
        self.assertIn("phần giữa đã lược", prompt)
        self.assertIn("Chỉ trả về phần nội dung nối tiếp mới", prompt)

    def test_reporting_reach_uses_impression_weighted_ctr(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            report = base / "channel_reach_basic_a1" / "reach.csv"
            report.parent.mkdir(parents=True)
            report.write_text(
                "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
                "2026-07-01,c1,v1,100,0.04\n"
                "2026-07-02,c1,v1,300,0.08\n",
                encoding="utf-8",
            )
            index = {"daily": aggregate_reporting_csv(base)}
            windows = reporting_windows(index, "v1", "2026-07-01")
            self.assertEqual(windows["metrics_72h"]["video_thumbnail_impressions"], 400)
            self.assertAlmostEqual(windows["metrics_72h"]["video_thumbnail_impressions_ctr"], 0.07)

    def test_sleep_story_reviewer_rejects_invalid_json(self) -> None:
        review = parse_review_response("This is not JSON", 86)
        self.assertFalse(review.valid_response)
        self.assertFalse(review.passed)

    def test_sleep_story_reviewer_falls_back_when_model_json_is_invalid(self) -> None:
        class InvalidJsonModel:
            last_error = ""

            def generate(self, _request):
                return "The story is good, but this response is not JSON."

        story = StoryArtifact(
            title="The Fox Who Collected Falling Stars",
            prompt="",
            script=(
                "If your mind has been full tonight, this story may help you rest. "
                "Mara walked through the forest to a cottage window and remembered "
                "the letter she had never sent. She opened the door, carried the "
                "lantern into the garden, placed the letter beneath a tree, and "
                "returned home. You may rest now. "
            )
            * 25,
        )
        review = StoryReviewerAgent(model_client=InvalidJsonModel()).execute(
            story,
            AgentContext(
                niche="sleep-story",
                settings={"review_threshold": 86, "multi_judge_review": False},
            ),
        )
        self.assertTrue(review.valid_response)
        self.assertTrue(any("malformed JSON" in note for note in review.notes))

    def test_sleep_story_reviewer_does_not_request_full_rewrite(self) -> None:
        story = StoryArtifact(title="Test", prompt="", script="A calm story.")
        prompt = build_review_prompt(story, 86)
        self.assertIn('"revised_script": ""', prompt)
        self.assertIn("Do not rewrite the script", prompt)
        self.assertNotIn("only include a full revised script", prompt)

    def test_sleep_story_reviewer_reconciles_positive_low_score(self) -> None:
        script = (
            "If you have been carrying a tired hurt in your heart tonight, this story may help you rest. "
            "There lived a keeper named Faelan beside an old forest path, and he wanted to collect every falling star "
            "because he was afraid that anything beautiful would leave him. One evening he noticed a strange lantern "
            "glowed by itself beside the river door, and he wondered why its glass showed stars moving like water. "
            "He walked through the forest, opened the door, crossed a bridge, entered a library, and carried the lantern "
            "into a chamber where a small map waited. Years ago, Faelan remembered his father and the letter he never sent; "
            "he had waited by the window, wrote three careful lines, left the page inside an empty chair, and kept the apology "
            "instead of giving it. In the balcony garden, the lantern changed when he placed the letter beside a seed, and he "
            "realized the light had not asked him to possess the stars but to release them. He offered the map to the river, "
            "gave the seed back to the earth, returned through the threshold, and chose to let the last bright thing travel on. "
            "Only near the end, when the lake became dark and kind, Faelan rested, safe to rest, and drifted into sleep."
        )
        story = StoryArtifact(title="Faelan and the Falling Stars", prompt="", script=script)
        self.assertFalse(content_gate_violations(script))
        review = StoryReview(
            score=64,
            passed=False,
            notes=[
                "Excellent progression: Description -> Discovery -> Small Mystery -> Discovery -> Memory -> New Room/Place -> New Object -> Another Revelation -> Kind Choice -> Sleep Resolution.",
                "The psychological arc is strong and concrete.",
                "Sleep quality is excellent due to the calm tone.",
                "Visual variety is high with distinct set pieces.",
                "Excellent causal continuity and visible emotional payoff.",
            ],
        )
        reconciled = reconcile_anomalous_positive_review(story, review, threshold=86)
        self.assertTrue(reconciled.passed)
        self.assertGreaterEqual(reconciled.score, 86)

    def test_sleep_story_reviewer_reconciles_cached_positive_low_score(self) -> None:
        class UnusedModel:
            last_error = ""

            def generate(self, _request):
                raise AssertionError("cached review should avoid model call")

        script = (
            "If you have been carrying a tired hurt in your heart tonight, this story may help you rest. "
            "There lived a keeper named Faelan beside an old forest path, and he wanted to collect every falling star "
            "because he was afraid that anything beautiful would leave him. One evening he noticed a strange lantern "
            "glowed by itself beside the river door, and he wondered why its glass showed stars moving like water. "
            "He walked through the forest, opened the door, crossed a bridge, entered a library, and carried the lantern "
            "into a chamber where a small map waited. Years ago, Faelan remembered his father and the letter he never sent; "
            "he had waited by the window, wrote three careful lines, left the page inside an empty chair, and kept the apology "
            "instead of giving it. In the balcony garden, the lantern changed when he placed the letter beside a seed, and he "
            "realized the light had not asked him to possess the stars but to release them. He offered the map to the river, "
            "gave the seed back to the earth, returned through the threshold, and chose to let the last bright thing travel on. "
            "Only near the end, when the lake became dark and kind, Faelan rested, safe to rest, and drifted into sleep."
        )
        story = StoryArtifact(title="Faelan and the Falling Stars", prompt="", script=script)
        with tempfile.TemporaryDirectory() as folder:
            cache = AutomationCache(Path(folder))
            context = AgentContext(
                niche="sleep-story",
                settings={
                    "review_threshold": 86,
                    "multi_judge_review": False,
                    "fast_review_if_heuristic_passes": False,
                    "ollama_model": "gemma4:e2b",
                },
                cache=cache,
            )
            cache_key = cache.key_for(
                "story_reviewer",
                {
                    "niche": context.niche,
                    "title": story.title,
                    "script": story.script,
                    "threshold": 86.0,
                    "model": "gemma4:e2b",
                    "prompt_version": 6,
                    "multi_judge_review": False,
                    "multi_judge_min_words": 1600,
                    "hard_gate_version": 6,
                },
            )
            cache.write_json(
                cache_key,
                {
                    "score": 64,
                    "passed": False,
                    "notes": [
                        "Excellent progression with strong causal continuity.",
                        "Psychological depth is strong and concrete.",
                        "Sleep quality is exceptional.",
                        "Visual variety is high with distinct set pieces.",
                    ],
                    "revised_script": "",
                },
            )
            review = StoryReviewerAgent(model_client=UnusedModel()).execute(story, context)
        self.assertTrue(review.passed)
        self.assertGreaterEqual(review.score, 86)

    def test_sleep_story_reviewer_reconciles_positive_passed_false_without_false_negative_words(self) -> None:
        script = (
            "If tomorrow has felt too uncertain tonight, this story may help your mind rest. "
            "Silas lived beside a valley library and carried a heavy atlas because he wanted every path to be certain. "
            "He opened the atlas, noticed a glowing page, walked through a moonlit arch, entered an archive, "
            "crossed a bridge, and found a brass scroll beside a quiet balcony. "
            "Standing there, he recalled one clear night when he had watched the stars turn slowly and understood that "
            "he had once trusted the sky without owning it. The scroll changed into a river-map when he placed it down, "
            "and Silas realized tomorrow was not a locked answer but a path that could unfold. "
            "He touched the scroll, chose to release the need to control each road, returned through the archive, "
            "rested beside the warm lamp, safe to rest, and drifted into sleep."
        )
        story = StoryArtifact(title="Silas and Tomorrow's Map", prompt="", script=script)
        review = StoryReview(
            score=88,
            passed=False,
            notes=[
                "Component scores: ai_repetition=91, retention=78. Weakest: retention=78.",
                "Excellent progression with strong causal continuity.",
                "The psychological core is strong and concrete.",
                "The tone succeeds without relying on repetitive mood words.",
                "Visual variety is high with distinct set pieces.",
                "The story delivers a clear emotional payoff.",
            ],
        )
        with patch(
            "ai_music_automation.agents.story_reviewer.heuristic_review",
            return_value=StoryReview(score=83, passed=False, notes=["Heuristic sanity check only."]),
        ):
            reconciled = reconcile_anomalous_positive_review(story, review, 86)
        self.assertTrue(reconciled.passed)
        self.assertGreaterEqual(reconciled.score, 86)

    def test_sleep_story_writer_repairs_truncated_clockmaker_ending(self) -> None:
        script = (
            "If you have been carrying the heavy weight of hurried expectations, allow yourself to settle into a mountain morning. "
            "Elias was a man who lived high in the mountains, where he repaired clocks in a stone inn. "
            "One morning, the great clock hesitated and the brass mechanism unfolded a strange silver light. "
            "Elias opened the clock case, carried a small tool from the workshop, and followed the mist from the main hall "
            "to a hidden clearing beside a stone bench. "
            "He held a worn brass weight and remembered a specific moment from his past: years ago, he had been working on "
            "a particularly intricate mechanism, racing against the demands of an order until exhaustion blurred his vision. "
            "The clock changed again when he returned to the inn, and Elias realized the mechanism was not asking for control, "
            "but patience. He chose to stop forcing it. Elias placed the brass weight down and let the "
            "relentless pressure of measurement dissolve into the"
        )
        repaired = repair_complete_sleep_story_script(script, "The Clockmaker's Secret")
        self.assertRegex(repaired, r"[.!?]$")
        self.assertIn("You may rest now", repaired)
        self.assertFalse(content_gate_violations(repaired))

    def test_sleep_story_polish_softens_mid_story_sleep_closure(self) -> None:
        script = (
            "If you have been waiting tonight, this story may help you rest. "
            "Silas opened the lighthouse door and followed the lantern down the stairs. "
            "He closed his eyes, breathing in the salt air, drifting into a deep and restorative sleep. "
            "Then the lantern revealed a hidden room where he placed a shell beside the window. "
            "At the end, Silas sat beside the warm light. You may rest now, safe in the quiet light."
        )
        polished = polish_adult_sleep_story_script(script)
        early = polished[: int(len(polished) * 0.85)].lower()
        self.assertNotIn("drifting into a deep and restorative sleep", early)
        self.assertIn("settled into deep stillness", early)

    def test_sleep_story_visual_bible_uses_script_not_reference_prompt(self) -> None:
        story = StoryArtifact(
            title="A Lighthouse Keeper's Wait",
            prompt="Old visual reference: yellow dress, moon meadow, letter, clock, cottage, forest path.",
            script=(
                "Silas was an adult male lighthouse keeper in a blue coat. "
                "He carried a brass lantern through the stone lighthouse and watched the misty shore. "
                "He placed a smooth piece of driftwood beside the window and chose to wait with patience."
            ),
        )
        bible = build_story_visual_bible(story, {})
        combined = " ".join(bible.values()).lower()
        self.assertIn("silas", combined)
        self.assertIn("lighthouse", combined)
        self.assertIn("lantern", combined)
        self.assertNotIn("dress", combined)
        self.assertNotIn("clock", combined)
        self.assertNotIn("cottage", combined)

    def test_sleep_story_hard_gate_accepts_role_based_new_symbolic_object(self) -> None:
        script = (
            "If you have been carrying an old worry in your heart tonight, this story may help you set it down before sleep. "
            "Mira lived beside a rain-bright station where travelers left small keepsakes on a cedar shelf. "
            "She wanted to understand why a folded blue scarf on that shelf whispered only when the platform lamps went dim. "
            "Mira opened the station door, carried the scarf through the glass arcade, crossed a footbridge, and entered a quiet greenhouse. "
            "Years ago, Mira had once folded a blue scarf for a friend at the station, waited beside the last train, and kept the goodbye inside her pocket. "
            "In the greenhouse, the scarf changed when she placed it beside a bowl of rainwater, showing tiny threads of lantern light. "
            "Mira realized the scarf was not asking to be kept, but to be returned to the shelf where other tired travelers could find comfort. "
            "She chose to give it back, opened her hands, released the old goodbye, and walked home through the soft rain. "
            "At the end of the night, Mira rested by the window, safe to rest, while the station lamps settled into sleep."
        )
        self.assertFalse(content_gate_violations(script))

    def test_automation_logger_state_lock_does_not_fail_event(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            logger = AutomationLogger(Path(folder), "run")
            with patch("ai_music_automation.automation.logging.os.replace", side_effect=PermissionError("locked")):
                logger.event("stage", "start")
            self.assertTrue((Path(folder) / "run.jsonl").exists())

    def test_ollama_json_mode_is_sent_to_api(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"response":"{\\"score\\":90}"}'

        captured: dict = {}

        def fake_urlopen(request, timeout):
            captured.update(json.loads(request.data.decode("utf-8")))
            self.assertEqual(timeout, 30)
            return Response()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            output = OllamaClient().generate(
                ModelRequest(
                    prompt="review",
                    model="gemma4:e2b",
                    timeout_seconds=30,
                    response_format="json",
                    context_tokens=8192,
                )
            )

        self.assertEqual(captured["format"], "json")
        self.assertEqual(captured["options"]["num_ctx"], 8192)
        self.assertIn("score", output)

    def test_ollama_unload_sends_keep_alive_zero(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"done":true}'

        captured: dict = {}

        def fake_urlopen(request, timeout):
            captured.update(json.loads(request.data.decode("utf-8")))
            return Response()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertTrue(OllamaClient().unload("http://127.0.0.1:11434", "gemma4:e2b"))
        self.assertEqual(captured["keep_alive"], 0)
        self.assertEqual(captured["prompt"], "")

    def test_vi_shorts_sanitizer_replaces_swipe_away_hook(self) -> None:
        response = (
            "TITLE: Đừng Vội Lướt Qua Video Này\n"
            "THUMBNAIL_TEXT: DUYÊN LÀNH\n"
            "SCRIPT: Đừng vội lướt qua, đây là lời nhắc cho con hôm nay. "
            "Nếu lòng còn nặng, hãy thở chậm lại.\n"
            "IMAGE_PROMPTS:\n"
            "- scene 1\n- scene 2\n- scene 3\n- scene 4\n- scene 5\n"
            "THUMBNAIL_PROMPT: warm Buddhist scene\n"
            "DESCRIPTION: short description\n"
        )
        sanitized = sanitize_vi_shorts_response(response)
        self.assertNotIn("Đừng Vội Lướt", sanitized)
        self.assertNotIn("Đừng vội lướt", sanitized)
        self.assertIn("TITLE:", sanitized)
        self.assertIn("SCRIPT:", sanitized)

    def test_vi_shorts_sanitizer_preserves_short_format_blessing_and_dynamic_hashtags(self) -> None:
        response = (
            "TITLE: Giu Phuoc Bang Loi Noi Lanh\n"
            "THUMBNAIL_TEXT: GIU PHUOC\n"
            "SCRIPT: Nghe duoc loi nay la mot duyen lanh. Hom nay, neu con bot mot cau trach moc, "
            "giu lai mot loi hien hoa, thi tam minh da gieo mot hat phuoc nho. Hay tap noi cham hon, "
            "nghi thien hon, va de mot ngay di qua nhe hon\n"
            "IMAGE_PROMPTS:\n"
            "- scene 1\n- scene 2\n- scene 3\n- scene 4\n- scene 5\n"
            "THUMBNAIL_PROMPT: warm Buddhist scene\n"
            "DESCRIPTION: Mot loi nhac ve loi noi, khau nghiep va phuoc lanh.\n"
        )
        sanitized = sanitize_vi_shorts_response(response)
        script_match = re.search(r"(?is)^SCRIPT:\s*(.*?)(?=\nIMAGE_PROMPTS:)", sanitized, re.M)
        self.assertIsNotNone(script_match)
        script = script_match.group(1).strip()
        hashtags = re.findall(r"#[\wÀ-ỹĐđ]+", sanitized)
        self.assertTrue(script.endswith("Nam Mô A Di Đà Phật."))
        self.assertLessEqual(len(script), 760)
        self.assertEqual(len(hashtags), 5)
        self.assertIn("#khaunghiep", hashtags)
        self.assertIn("#shorts", hashtags)

    def test_vi_short_hook_replacement_has_broader_variety(self) -> None:
        hooks = {_vi_short_hook_replacement(f"seed-{index}") for index in range(40)}
        self.assertGreaterEqual(len(hooks), 10)
        folded = " ".join(hooks).lower()
        self.assertIn("duyên", folded)
        self.assertTrue(any(keyword in folded for keyword in ("tài lộc", "phước", "bình an")))

    def test_bulk_short_prompt_state_is_scoped_per_account(self) -> None:
        global_state = {
            "used_prompts": ["legacy"],
            "next_prompt_index": 9,
            "accounts": {
                "account1": {"used_prompts": ["a"], "next_prompt_index": 2, "image_index": 5, "voice_index": 1},
                "account2": {"used_prompts": ["b"], "next_prompt_index": 4, "image_index": 10, "voice_index": 0},
            },
        }
        scoped = scoped_short_state_for_account(global_state, "account1")
        self.assertEqual(scoped["next_prompt_index"], 2)
        scoped["next_prompt_index"] = 4
        scoped["used_prompts"].append("new")
        merged = merge_short_state_for_account(global_state, scoped, "account1")
        self.assertEqual(merged["accounts"]["account1"]["next_prompt_index"], 4)
        self.assertEqual(merged["accounts"]["account2"]["next_prompt_index"], 4)
        self.assertIn("new", merged["accounts"]["account1"]["used_prompts"])
        self.assertEqual(merged["next_prompt_index"], 9)

    def test_sleep_story_hard_gate_blocks_early_ending_but_not_creative_object_roles(self) -> None:
        script = (
            "If you have been tired tonight, this story may help your heart rest. "
            "Elias found a strange lantern beside the railway and wondered why it glowed. "
            "He drifted toward sleep before deciding what the lantern meant. "
            "Years ago he remembered a letter, but no one had written or sent it. "
            "He walked through the valley, opened a gate, crossed a bridge, and placed a stone on a table. "
            "The story ended without returning to the lantern."
        )
        violations = content_gate_violations(script)
        self.assertTrue(any("sleep ending appears" in item for item in violations))
        self.assertFalse(any("payoff" in item for item in violations))


if __name__ == "__main__":
    unittest.main()
