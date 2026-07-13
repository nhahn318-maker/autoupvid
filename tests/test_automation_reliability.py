from __future__ import annotations

import json
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
from ai_music_automation.automation.model_client import ModelRequest, OllamaClient
from ai_music_automation.job_store import JobStore
from ai_music_automation.media_qa import inspect_media, inspect_subtitle
from ai_music_automation.story_before_sleep import infer_character_gender_label
from ai_music_automation.youtube_reporting import aggregate_reporting_csv, reporting_windows
from ai_music_automation.web import (
    analyze_long_script_duplicates,
    build_long_chapter_continuation_prompt,
    long_chapter_overlap_ratio,
    sanitize_vi_shorts_response,
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

    def test_gender_inference_uses_dominant_character_signals(self) -> None:
        male_story = "Elias was a young man. He kept his clock. A woman waved once, then he returned home."
        female_story = "Elara was a young woman. She carried her book. A man opened the gate for her."
        self.assertEqual(infer_character_gender_label(male_story.lower()), "male")
        self.assertEqual(infer_character_gender_label(female_story.lower()), "female")

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
            score=76,
            passed=False,
            notes=[
                "Excellent progression: Description -> Discovery -> Small Mystery -> Discovery -> Memory -> New Room/Place -> New Object -> Another Revelation -> Kind Choice -> Sleep Resolution.",
                "The psychological arc is strong and concrete.",
                "Sleep quality is excellent due to the calm tone.",
                "Visual variety is high with distinct set pieces.",
                "Excellent causal continuity and visible emotional payoff.",
            ],
        )
        reconciled = reconcile_anomalous_positive_review(story, review, threshold=78)
        self.assertTrue(reconciled.passed)
        self.assertGreaterEqual(reconciled.score, 78)

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

    def test_sleep_story_hard_gate_blocks_early_ending_and_missing_payoff(self) -> None:
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
        self.assertTrue(any("without a clear later payoff" in item for item in violations))


if __name__ == "__main__":
    unittest.main()
