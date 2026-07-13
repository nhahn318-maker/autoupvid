from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .metadata import VideoMetadata


NORMAL_THUMBNAIL_SIZE = (1280, 720)
SHORT_THUMBNAIL_SIZE = (1080, 1440)
OUTPUT_DIR = Path("data/output/auto_thumbnails")
TEXT_CACHE_PATH = Path("data/state/thumbnail_text_cache.json")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SHORT_UPLOAD_FRAME_DURATION = 0.7
CHANNEL_SLUGS = {
    "account1": "nhan-tam-phat-phap",
    "account2": "anh-dao-tu-bi",
    "account3": "lang-nghe-phat-phap-dieu-ky",
    "account4": "an-nhien-phat-phap",
}
DEFAULT_SOURCE_DIRS = [
    Path("data/input/buddhist/shared/story-shorts/images"),
    Path("data/input/buddhist/channels/an-nhien-phat-phap/story-shorts/images"),
    Path("data/input/buddhist/channels/lang-nghe-phat-phap-dieu-ky/story-shorts/images"),
    Path("data/input/buddhist/shared/twenty-min/images"),
    Path("data/input/buddhist/channels/story/fullauto-long/images"),
    Path("data/input/buddhist/channels/silent_horizone/fullauto-long/images"),
]


def ensure_auto_thumbnail(video_path: Path, metadata: VideoMetadata) -> VideoMetadata:
    if metadata.thumbnail_path and metadata.thumbnail_path.exists():
        return metadata
    thumbnail_path = auto_thumbnail_path(video_path)
    if not thumbnail_path.exists():
        generate_auto_thumbnail(video_path=video_path, title=metadata.title, output_path=thumbnail_path)
    return VideoMetadata(
        title=metadata.title,
        description=metadata.description,
        tags=metadata.tags,
        category_id=metadata.category_id,
        made_for_kids=metadata.made_for_kids,
        thumbnail_path=thumbnail_path if thumbnail_path.exists() else metadata.thumbnail_path,
    )


def auto_thumbnail_path(video_path: Path) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / f"{video_path.stem}.jpg"


def generate_auto_thumbnail(video_path: Path, title: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    kind = thumbnail_kind(video_path)
    size = thumbnail_size(kind)
    source_image = pick_source_image(title, video_path, kind)
    if source_image:
        image = Image.open(source_image).convert("RGB")
        from_video_frame = False
    else:
        with tempfile.TemporaryDirectory(prefix="auto-thumb-") as temp_name:
            frame_path = Path(temp_name) / "frame.jpg"
            extract_video_frame(video_path, frame_path)
            if frame_path.exists():
                image = Image.open(frame_path).convert("RGB")
            else:
                image = fallback_background()
        from_video_frame = True

    canvas = cover_crop(image, size)
    canvas = stylize_frame_background(canvas) if from_video_frame else stylize_buddhist_background(canvas, kind)
    draw_thumbnail_text(canvas, title, kind)
    save_jpeg_under_limit(canvas, output_path)


def thumbnail_kind(video_path: Path) -> str:
    stem = video_path.stem.lower()
    if stem.endswith("-short") or "short" in stem:
        return "short"
    return "normal"


def thumbnail_size(kind: str) -> tuple[int, int]:
    return SHORT_THUMBNAIL_SIZE if kind == "short" else NORMAL_THUMBNAIL_SIZE


def pick_source_image(title: str, video_path: Path, kind: str | None = None) -> Path | None:
    images = source_images(kind or thumbnail_kind(video_path))
    if not images:
        return None
    key = f"{video_path.stem}|{title}".encode("utf-8", errors="ignore")
    index = int(hashlib.sha1(key).hexdigest(), 16) % len(images)
    return images[index]


def source_images(kind: str = "normal") -> list[Path]:
    candidates: list[Path] = []
    for folder in source_dirs_from_config(kind) + DEFAULT_SOURCE_DIRS:
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                candidates.append(path)
    return sorted(set(candidates), key=lambda path: str(path).lower())


def source_dirs_from_config(kind: str = "normal") -> list[Path]:
    config_path = Path("config.json")
    if not config_path.exists():
        return []
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []

    dirs: list[Path] = []
    ratio_dir = "short-9x12" if kind == "short" else "normal-16x9"
    active_account = str(config.get("active_account") or "")
    fullauto = config.get("fullauto")
    if isinstance(fullauto, dict):
        active_account = str(fullauto.get("upload_account") or active_account)
    account_slug = CHANNEL_SLUGS.get(active_account)
    if account_slug:
        dirs.append(Path("data/input/buddhist/thumbnail-references") / account_slug / ratio_dir)
        dirs.append(Path("data/input/buddhist/channels") / account_slug / "thumbnail-references" / ratio_dir)

    reference_dirs = config.get("thumbnail_reference_dirs")
    if isinstance(reference_dirs, dict):
        account_dirs = reference_dirs.get(active_account)
        if isinstance(account_dirs, dict):
            value = account_dirs.get(ratio_dir) or account_dirs.get(kind)
            if isinstance(value, str) and value.strip():
                dirs.append(Path(value))

    fullauto = config.get("fullauto")
    if not isinstance(fullauto, dict):
        return dirs

    for key in ("image_pool_dir", "long_image_dir", "twenty_min_image_dir"):
        value = fullauto.get(key)
        if isinstance(value, str) and value.strip():
            dirs.append(Path(value))

    image_pool_dirs = fullauto.get("image_pool_dirs")
    if isinstance(image_pool_dirs, dict):
        for value in image_pool_dirs.values():
            if isinstance(value, str) and value.strip():
                dirs.append(Path(value))
    return dirs


def extract_video_frame(video_path: Path, frame_path: Path) -> None:
    command = [
        ffmpeg_binary(),
        "-y",
        "-ss",
        "00:00:01.2",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(frame_path),
    ]
    try:
        subprocess.run(command, capture_output=True, check=False, timeout=60)
    except Exception:
        frame_path.unlink(missing_ok=True)


def ffmpeg_binary() -> str:
    local = Path("tools/ffmpeg/bin/ffmpeg.exe")
    return str(local) if local.exists() else "ffmpeg"


def fallback_background() -> Image.Image:
    width, height = NORMAL_THUMBNAIL_SIZE
    image = Image.new("RGB", NORMAL_THUMBNAIL_SIZE, "#5a3413")
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            glow = int(120 * (1 - abs((x / width) - 0.62)) * (1 - y / height))
            r = min(255, 85 + glow)
            g = min(210, 48 + int(glow * 0.72))
            b = min(120, 22 + int(glow * 0.28))
            pixels[x, y] = (r, g, b)
    return image


def cover_crop(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_w, target_h = size
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    resized = image.resize((math.ceil(src_w * scale), math.ceil(src_h * scale)), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - target_w) // 2)
    top = max(0, (resized.height - target_h) // 2)
    return resized.crop((left, top, left + target_w, top + target_h))


def stylize_frame_background(image: Image.Image) -> Image.Image:
    base = image.filter(ImageFilter.GaussianBlur(radius=15.0))
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    width, height = base.size
    draw.rectangle((0, 0, int(width * 0.7), height), fill=(22, 16, 8, 190))
    draw.rectangle((int(width * 0.38), 0, width, height), fill=(255, 184, 44, 105))
    draw.rectangle((0, int(height * 0.52), width, height), fill=(18, 12, 6, 152))
    draw.rectangle((0, 0, width, height), outline=(255, 207, 82, 18), width=10)
    for radius, alpha in ((540, 120), (360, 86), (220, 58)):
        cx, cy = int(width * 0.68), int(height * 0.44)
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(255, 189, 55, alpha))
    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")


def stylize_buddhist_background(image: Image.Image, kind: str = "normal") -> Image.Image:
    base = image.filter(ImageFilter.UnsharpMask(radius=1.2, percent=115, threshold=3))
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    width, height = base.size
    gradient_width = int(width * (0.42 if kind == "normal" else 0.54))
    max_alpha = 46 if kind == "normal" else 96
    for x in range(gradient_width):
        alpha = max(0, int(max_alpha * (1 - x / max(1, gradient_width))))
        draw.line((x, 0, x, height), fill=(12, 8, 3, alpha))
    draw.rectangle((0, int(height * 0.84), width, height), fill=(28, 14, 3, 18 if kind == "normal" else 30))
    draw.rectangle((0, 0, width, height), outline=(255, 205, 78, 12 if kind == "normal" else 18), width=6 if kind == "normal" else 8)
    if kind != "normal":
        draw.ellipse(
            (int(width * 0.58), -int(height * 0.2), int(width * 1.18), int(height * 1.0)),
            fill=(255, 195, 62, 34),
        )
    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")


def draw_thumbnail_text(image: Image.Image, title: str, kind: str = "normal") -> None:
    draw = ImageDraw.Draw(image)
    text = thumbnail_text(title)
    max_width = int(image.width * (0.58 if kind == "short" else 0.48))
    lines = fit_text_lines(draw, text, max_width=max_width, max_lines=5 if kind == "short" else 3)
    font_size = (126 if len(lines) <= 3 else 104) if kind == "short" else (104 if len(lines) <= 2 else 82)
    font = load_font(font_size)
    while any(draw.textbbox((0, 0), line, font=font, stroke_width=5)[2] > max_width for line in lines) and font_size > 52:
        font_size -= 4
        font = load_font(font_size)

    line_height = int(font_size * 1.03)
    block_h = line_height * len(lines)
    x = int(image.width * 0.052)
    y = max(int(image.height * 0.08), int((image.height - block_h) * (0.26 if kind == "short" else 0.42)))
    for index, line in enumerate(lines):
        fill = "#ffffff" if index == 0 else "#ffd33d"
        draw.text((x + 5, y + 5), line, font=font, fill=(0, 0, 0), stroke_width=8, stroke_fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=fill, stroke_width=5, stroke_fill="#2b1a08")
        y += line_height


def thumbnail_text(title: str) -> str:
    text = re.sub(r"#shorts\b", "", str(title or ""), flags=re.IGNORECASE)
    text = re.split(r"\s[-|]\s", text, maxsplit=1)[0]
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "BINH AN TAM HON"
    ai_text = ai_thumbnail_text(text)
    if ai_text:
        return ai_text
    return smart_thumbnail_text(text)


def ai_thumbnail_text(title: str) -> str:
    cache_key = "v7|" + title.strip().lower()
    cache = read_thumbnail_text_cache()
    cached = cache.get(cache_key)
    if isinstance(cached, str) and is_good_ai_thumbnail_text(cached, title):
        return cached.strip()

    config = read_root_config()
    settings = config.get("thumbnail_text_ai")
    enabled = True if not isinstance(settings, dict) else bool(settings.get("enabled", True))
    if not enabled:
        return ""

    fullauto = config.get("fullauto") if isinstance(config.get("fullauto"), dict) else {}
    base_url = str((settings or {}).get("ollama_url") or fullauto.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    model = str((settings or {}).get("ollama_model") or fullauto.get("ollama_model") or "").strip()
    if not model:
        return ""

    payload = {
        "model": model,
        "prompt": build_thumbnail_text_prompt(title),
        "stream": False,
        "options": {"temperature": 0.18},
    }
    try:
        request = urllib.request.Request(
            f"{base_url}/api/generate",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=75) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return ""

    result = sanitize_thumbnail_text(extract_thumbnail_text_response(str(data.get("response") or "")))
    if is_good_ai_thumbnail_text(result, title):
        cache[cache_key] = result
        write_thumbnail_text_cache(cache)
        return result
    return ""


def build_thumbnail_text_prompt(title: str) -> str:
    return (
        "B\u1ea1n l\u00e0 bi\u00ean t\u1eadp ch\u1eef thumbnail YouTube ti\u1ebfng Vi\u1ec7t cho k\u00eanh Ph\u1eadt ph\u00e1p.\n"
        "Vi\u1ebft l\u1ea1i title th\u00e0nh m\u1ed9t c\u1ee5m ch\u1eef thumbnail ng\u1eafn, \u1ea5m, b\u00e1m \u0111\u00fang c\u1ea3m x\u00fac ch\u00ednh v\u00e0 m\u1edf ra h\u01b0\u1edbng nh\u1eb9 l\u00f2ng.\n\n"
        "Lu\u1eadt b\u1eaft bu\u1ed9c:\n"
        "- Ch\u1ec9 tr\u1ea3 v\u1ec1 1 d\u00f2ng ch\u1eef, kh\u00f4ng gi\u1ea3i th\u00edch.\n"
        "- 4 \u0111\u1ebfn 8 t\u1eeb, ti\u1ebfng Vi\u1ec7t t\u1ef1 nhi\u00ean, c\u00f3 ngh\u0129a tr\u1ecdn v\u1eb9n.\n"
        "- N\u1ebfu c\u00e2u ng\u1eafn qu\u00e1 d\u1ec5 m\u01a1 h\u1ed3, h\u00e3y vi\u1ebft d\u00e0i h\u01a1n m\u1ed9t ch\u00fat cho d\u1ec5 hi\u1ec3u.\n"
        "- C\u00f3 th\u1ec3 vi\u1ebft nh\u01b0 m\u1ed9t c\u00e2u th\u01a1 nh\u1ecf c\u00f3 nh\u1ecbp, nh\u01b0ng v\u1eabn ph\u1ea3i r\u00f5 ngh\u0129a ngay.\n"
        "- Kh\u00f4ng c\u1eaft ngang c\u00e2u. Kh\u00f4ng t\u1ea1o c\u1ee5m c\u1ee5t ngh\u0129a nh\u01b0 '\u0110\u1ec2 AN', 'T\u1ef0 \u0110\u1ebeN' n\u1ebfu thi\u1ebfu ch\u1ee7 th\u1ec3.\n"
        "- Kh\u00f4ng d\u00f9ng c\u1ee5m chung chung nh\u01b0 'B\u00ecnh y\u00ean ngay l\u1eadp t\u1ee9c', 'T\u00e2m b\u00ecnh an', 'Ph\u01b0\u1edbc l\u00e0nh t\u1ef1 \u0111\u1ebfn' tr\u1eeb khi title th\u1eadt s\u1ef1 n\u00f3i v\u1ec1 \u0111i\u1ec1u \u0111\u00f3.\n"
        "- N\u1ebfu title c\u00f3 'm\u1ec7t m\u1ecfi', 'lo l\u1eafng', 'ch\u1ecbu thi\u1ec7t', 'kh\u1ed5 \u0111au', kh\u00f4ng l\u00e0m c\u00e2u bi quan; h\u00e3y chuy\u1ec3n th\u00e0nh l\u1eddi an \u1ee7i ho\u1eb7c m\u1edf l\u1ed1i ra.\n"
        "- Ưu tiên 1 trong 3 kiểu: lời an ủi đúng cảm xúc, lời gợi mở nhẹ nhàng, hoặc mệnh lệnh mềm.\n"
        "- Kh\u00f4ng hashtag, kh\u00f4ng d\u1ea5u c\u00e2u, kh\u00f4ng emoji.\n"
        "- \u01afu ti\u00ean c\u1ee5m d\u1ec5 \u0111\u1ecdc tr\u00ean thumbnail, c\u00f3 c\u1ea3m x\u00fac, kh\u00f4ng sáo rỗng.\n\n"
        "V\u00ed d\u1ee5:\n"
        "Title: N\u1ebfu b\u1ea1n lu\u00f4n c\u1ea3m th\u1ea5y m\u1ec7t m\u1ecfi\n"
        "Thumbnail: M\u1ec6T R\u1ed2I TH\u00cc NGH\u1ec8 M\u1ed8T CH\u00daT\n"
        "Title: N\u1ebfu b\u1ea1n lu\u00f4n l\u00e0 ng\u01b0\u1eddi ch\u1ecbu thi\u1ec7t\n"
        "Thumbnail: THI\u1ec6T TH\u00d2I R\u1ed2I C\u0168NG BU\u00d4NG TH\u00d4I\n"
        "Title: N\u1ebfu b\u1ea1n \u0111ang lo l\u1eafng v\u1ec1 t\u01b0\u01a1ng lai\n"
        "Thumbnail: LO NHI\u1ec0U R\u1ed2I TH\u1ea2 L\u1eceNG \u0110I\n"
        "Title: Bu\u00f4ng B\u1ecf \u0110\u1ec3 H\u1ea1nh Ph\u00fac T\u1ef1 \u0110\u1ebfn\n"
        "Thumbnail: BU\u00d4NG XU\u1ed0NG R\u1ed2I L\u00d2NG S\u1ebc AN\n"
        "Title: L\u1eafng Nghe L\u1eddi Ph\u1eadt D\u1ea1y \u0110\u1ec3 T\u00e2m B\u00ecnh An\n"
        "Thumbnail: NGHE M\u1ed8T CH\u00daT L\u00d2NG S\u1ebC AN\n"
        "Title: Nghe \u0110\u01b0\u1ee3c Video N\u00e0y T\u00e0i L\u1ed9c S\u1ebd T\u1edbi V\u1edbi Con\n"
        "Thumbnail: T\u00c0I L\u1ed8C S\u1ebc \u0110\u1ebeN\n"
        "Title: Ng\u01b0\u1eddi C\u00f3 Duy\u00ean M\u1edbi Nghe \u0110\u01b0\u1ee3c Video N\u00e0y\n"
        "Thumbnail: NG\u01af\u1edcI C\u00d3 DUY\u00caN\n\n"
        f"Title: {title}\n"
        "Thumbnail:"
    )


def is_good_thumbnail_text(value: str) -> bool:
    text = str(value or "").strip()
    if not text or "\ufffd" in text:
        return False
    words = text.split()
    if len(words) < 3 or len(words) > 8:
        return False
    lower = text.lower()
    bad_fragments = {"\u0111\u1ec3 an", "t\u1ef1 \u0111\u1ebfn", "s\u1ebd \u0111\u1ebfn", "v\u1edbi con", "video n\u00e0y"}
    if lower in bad_fragments:
        return False
    generic_fragments = {
        "b\u00ecnh y\u00ean ngay l\u1eadp t\u1ee9c",
        "t\u00e2m b\u00ecnh an",
        "b\u00ecnh an t\u00e2m h\u1ed3n",
        "h\u1ea1nh ph\u00fac t\u1ef1 \u0111\u1ebfn",
        "ph\u01b0\u1edbc l\u00e0nh t\u1ef1 \u0111\u1ebfn",
    }
    if lower in generic_fragments:
        return False
    if any(token in lower for token in ("video", "shorts", "nghe \u0111\u01b0\u1ee3c video")):
        return False
    bad_endings = {"\u0111\u1ec3", "v\u00e0", "c\u1ee7a", "v\u1edbi", "cho", "trong", "n\u00e0y", "m\u1edbi"}
    return words[-1].lower() not in bad_endings


def is_good_ai_thumbnail_text(value: str, title: str) -> bool:
    if not is_good_thumbnail_text(value):
        return False
    title_words = normalize_words(title)
    value_words = normalize_words(value)
    if not title_words or not value_words:
        return False
    if value_words == title_words[: len(value_words)]:
        return False
    lower_title = title.lower()
    lower_value = value.lower()
    intent_groups = [
        (("m\u1ec7t m\u1ecfi", "r\u1ea5t m\u1ec7t", "ki\u1ec7t s\u1ee9c"), ("ngh\u1ec9", "nh\u1eb9", "l\u00f2ng", "an", "t\u00e2m", "m\u1ec7t")),
        (("lo l\u1eafng", "lo \u00e2u", "t\u01b0\u01a1ng lai"), ("b\u1edbt", "lo", "nh\u1eb9", "l\u00f2ng", "an", "t\u00e2m", "sau")),
        (("ch\u1ecbu thi\u1ec7t", "thi\u1ec7t th\u00f2i"), ("bu\u00f4ng", "nh\u1eb9", "l\u00f2ng", "an", "t\u00e2m", "thi\u1ec7t")),
        (("kh\u1ed5 \u0111au", "\u0111au kh\u1ed5", "n\u1ed7i kh\u1ed5"), ("nh\u1eb9", "l\u00f2ng", "l\u00e0nh", "an", "t\u00e2m", "\u0111au")),
        (("bu\u00f4ng b\u1ecf", "bu\u00f4ng x\u1ea3"), ("bu\u00f4ng", "nh\u1eb9", "l\u00f2ng")),
    ]
    for title_needles, value_needles in intent_groups:
        if any(needle in lower_title for needle in title_needles):
            return any(needle in lower_value for needle in value_needles)
    return True


def normalize_words(value: str) -> list[str]:
    return [word.lower() for word in re.findall(r"[\w\u00c0-\u1ef9]+", str(value or ""), flags=re.UNICODE)]


def smart_thumbnail_text(title: str) -> str:
    lower = title.lower()
    if "ng\u1ee7" in lower or "m\u1ea5t ng\u1ee7" in lower:
        return "NG\u1ee6 M\u1ed8T GI\u1ea4C L\u00d2NG S\u1ebC AN"
    if "m\u1ec7t m\u1ecfi" in lower or "r\u1ea5t m\u1ec7t" in lower:
        return "M\u1ec6T R\u1ed2I TH\u00cc NGH\u1ec8 M\u1ed8T CH\u00daT"
    if "ch\u1ecbu thi\u1ec7t" in lower or "thi\u1ec7t th\u00f2i" in lower:
        return "THI\u1ec6T TH\u00d2I R\u1ed2I C\u0168NG BU\u00d4NG TH\u00d4I"
    if "lo l\u1eafng" in lower or "lo \u00e2u" in lower:
        return "LO NHI\u1ec0U R\u1ed2I TH\u1ea2 L\u1eceNG \u0110I"
    if "kh\u1ed5 \u0111au" in lower or "\u0111au kh\u1ed5" in lower or "n\u1ed7i kh\u1ed5" in lower or "ch\u1eefa l\u00e0nh" in lower:
        return "R\u1ed2I M\u1ed8T NG\u00c0Y L\u00d2NG S\u1ebC NH\u1eb8"
    if "bu\u00f4ng b\u1ecf" in lower and "h\u1ea1nh ph\u00fac" in lower:
        return "BU\u00d4NG XU\u1ed0NG R\u1ed2I L\u00d2NG S\u1ebC AN"
    phrase_rules = [
        (("bu\u00f4ng b\u1ecf", "bu\u00f4ng x\u1ea3"), "BU\u00d4NG XU\u1ed0NG R\u1ed2I L\u00d2NG S\u1ebC AN"),
        (("h\u1ea1nh ph\u00fac",), "L\u00d2NG NH\u1eb8 R\u1ed2I H\u1ea0NH PH\u00daC S\u1ebC T\u1edAI"),
        (("b\u00ecnh an", "an l\u1ea1c"), "NGHE M\u1ed8T CH\u00daT L\u00d2NG S\u1ebC AN"),
        (("t\u00e0i l\u1ed9c", "ti\u1ec1n t\u00e0i"), "T\u00c0I L\u1ed8C S\u1ebc \u0110\u1ebeN"),
        (("ph\u01b0\u1edbc", "ph\u01b0\u1edbc l\u00e0nh", "ph\u01b0\u1edbc b\u00e1o"), "GI\u1eee PH\u01af\u1edaC M\u1ed6I NG\u00c0Y"),
        (("duy\u00ean l\u00e0nh",), "\u0110\u1eeaNG L\u1ede DUY\u00caN L\u00c0NH"),
        (("c\u00f3 duy\u00ean", "ng\u01b0\u1eddi c\u00f3 duy\u00ean"), "NG\u01af\u1edcI C\u00d3 DUY\u00caN"),
        (("kh\u1ed5 \u0111au", "\u0111au kh\u1ed5", "n\u1ed7i kh\u1ed5", "ch\u1eefa l\u00e0nh"), "R\u1ed2I M\u1ed8T NG\u00c0Y L\u00d2NG S\u1ebC NH\u1eb8"),
        (("lo l\u1eafng", "lo \u00e2u"), "LO NHI\u1ec0U R\u1ed2I TH\u1ea2 L\u1eceNG \u0110I"),
        (("m\u1ec7t m\u1ecfi",), "M\u1ec6T R\u1ed2I TH\u00cc NGH\u1ec8 M\u1ed8T CH\u00daT"),
        (("l\u1eddi ph\u1eadt", "ph\u1eadt d\u1ea1y"), "NGHE M\u1ed8T CH\u00daT L\u00d2NG S\u1ebC AN"),
    ]
    for needles, replacement in phrase_rules:
        if any(needle in lower for needle in needles):
            return replacement
    return sanitize_thumbnail_text(title) or "T\u00c2M B\u00ccNH AN"


def sanitize_thumbnail_text(value: str) -> str:
    text = str(value or "").splitlines()[0]
    text = re.sub(r"#\w+", " ", text)
    text = re.sub(r"[\*\u201c\u201d\"'`\u00b4:;,.!?()\[\]{}|/\\]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    words = text.split()
    if len(words) > 8:
        words = words[:8]
    return " ".join(words).upper()


def extract_thumbnail_text_response(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    candidates: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^\s*[-*#\d.)]+\s*", "", line)
        line = re.sub(r"^(thumbnail|title thumbnail|dòng chữ thumbnail)\s*[:：]\s*", "", line, flags=re.IGNORECASE)
        line = line.strip()
        if line:
            candidates.append(line)
    candidates.append(text)
    for candidate in candidates:
        cleaned = sanitize_thumbnail_text(candidate)
        if is_good_thumbnail_text(cleaned):
            return cleaned
    return sanitize_thumbnail_text(candidates[0] if candidates else text)


def read_root_config() -> dict:
    config_path = Path("config.json")
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_thumbnail_text_cache() -> dict[str, str]:
    if not TEXT_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(TEXT_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_thumbnail_text_cache(cache: dict[str, str]) -> None:
    TEXT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TEXT_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def fit_text_lines(draw: ImageDraw.ImageDraw, text: str, max_width: int, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    probe_font = load_font(88)
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=probe_font, stroke_width=5)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = re.sub(r"\s+\S+$", "", lines[-1]).strip() or lines[-1]
    return lines or ["BINH AN"]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in (
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/impact.ttf",
        "C:/Windows/Fonts/seguiemj.ttf",
    ):
        font_path = Path(path)
        if font_path.exists():
            return ImageFont.truetype(str(font_path), size=size)
    return ImageFont.load_default(size=size)


def save_jpeg_under_limit(image: Image.Image, output_path: Path, max_bytes: int = 2 * 1024 * 1024) -> None:
    for quality in (92, 86, 80, 74, 68):
        image.save(output_path, format="JPEG", quality=quality, optimize=True, progressive=True)
        if output_path.stat().st_size <= max_bytes:
            return
    image.save(output_path, format="JPEG", quality=64, optimize=True, progressive=True)


def bake_thumbnail_frame_for_short(video_path: Path, thumbnail_path: Path) -> Path:
    if thumbnail_kind(video_path) != "short" or not thumbnail_path.exists():
        return video_path
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"{video_path.stem}.with-thumbnail-frame.mp4"
    source_mtime = max(video_path.stat().st_mtime, thumbnail_path.stat().st_mtime)
    if output_path.exists() and output_path.stat().st_mtime >= source_mtime:
        return output_path

    duration = SHORT_UPLOAD_FRAME_DURATION
    filter_complex = (
        "[0:v]split=2[tbgsrc][tfgsrc];"
        "[tbgsrc]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=24:2[tbg];"
        "[tfgsrc]scale=1080:1920:force_original_aspect_ratio=decrease[tfg];"
        "[tbg][tfg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,format=yuv420p[v0];"
        "[1:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1,fps=30,format=yuv420p[v1];"
        "[2:a]aformat=sample_rates=44100:channel_layouts=stereo[a0];"
        "[1:a]aformat=sample_rates=44100:channel_layouts=stereo[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
    )
    command = [
        ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(thumbnail_path),
        "-i",
        str(video_path),
        "-f",
        "lavfi",
        "-t",
        f"{duration:.3f}",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    subprocess.run(command, check=True)
    return output_path if output_path.exists() else video_path
