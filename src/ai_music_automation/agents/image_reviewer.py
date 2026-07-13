from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import AgentContext, BaseAgent
from ..automation.artifacts import ImageArtifact, SceneArtifact


@dataclass(frozen=True)
class ImageReviewInput:
    scene: SceneArtifact
    candidates: list[Path]
    min_score: float = 0.55


class ImageReviewerAgent(BaseAgent[ImageReviewInput, ImageArtifact | None]):
    name = "image_reviewer"

    def execute(self, payload: ImageReviewInput, context: AgentContext) -> ImageArtifact | None:
        scored = [
            ImageArtifact(
                scene_index=payload.scene.index,
                path=path,
                prompt=payload.scene.image_prompt,
                score=score_image(path),
                reviewer="heuristic",
            )
            for path in payload.candidates
            if path.exists()
        ]
        if not scored:
            return None
        best = max(scored, key=lambda item: item.score or 0)
        min_score = float(context.settings.get("image_review_threshold") or payload.min_score or 0.55)
        return best if (best.score or 0) >= min_score else None


def score_image(path: Path) -> float:
    try:
        from PIL import Image, ImageFilter, ImageStat

        with Image.open(path) as image:
            image = image.convert("RGB")
            width, height = image.size
            sample = image.resize((128, 128))
            stat = ImageStat.Stat(sample)
            gray = sample.convert("L")
            edge_stat = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES))
            histogram = gray.histogram()
    except ImportError:
        return 0
    except Exception:
        return 0
    # Accept 16:9 generated frames such as 768x432. The old height-only
    # cutoff rejected valid landscape frames before brightness/aspect scoring.
    if width < 512 or height < 288 or width * height < 512 * 288:
        return 0.2
    brightness = sum(stat.mean) / (3 * 255)
    contrast = sum(stat.stddev) / (3 * 128)
    sharpness = min(1.0, float(edge_stat.mean[0]) / 28.0)
    total_pixels = max(1, sum(histogram))
    clipped = (sum(histogram[:3]) + sum(histogram[-3:])) / total_pixels
    clipping_score = 1.0 - min(1.0, clipped / 0.35)
    aspect = width / max(1, height)
    aspect_score = 1.0 - min(1.0, abs(aspect - 16 / 9) / 1.8)
    # Bedtime artwork is intentionally dark. Reward a broad usable range
    # instead of forcing every scene toward mid-gray exposure.
    if 0.18 <= brightness <= 0.72:
        exposure_score = 1.0
    else:
        exposure_score = 1.0 - min(1.0, min(abs(brightness - 0.18), abs(brightness - 0.72)) / 0.28)
    contrast_score = min(1.0, contrast)
    return round(
        max(
            0.0,
            min(
                1.0,
                0.25 * aspect_score
                + 0.25 * exposure_score
                + 0.20 * contrast_score
                + 0.18 * sharpness
                + 0.12 * clipping_score,
            ),
        ),
        3,
    )
