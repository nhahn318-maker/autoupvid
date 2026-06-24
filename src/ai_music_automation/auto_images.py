from __future__ import annotations

import json
import math
import mimetypes
import re
import shutil
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .media import (
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    find_matching_images,
    list_files,
    probe_duration_seconds,
    track_title,
)


COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_API_URL = "https://api.openverse.org/v1/images/"
DEFAULT_USER_AGENT = "ai-music-automation/1.1 (local football shorts image fetcher)"
MAX_AUTO_IMAGES = 40
WORLD_CUP_2026_FOCUS_TOPICS = [
    "Lionel Messi Argentina",
    "Cristiano Ronaldo Portugal",
    "Kylian Mbappe France",
    "Lamine Yamal Spain",
    "Vinicius Junior Brazil",
    "Jude Bellingham England",
    "Christian Pulisic United States",
    "Alphonso Davies Canada",
    "Santiago Gimenez Mexico",
    "Mohamed Salah Egypt",
    "Erling Haaland Norway",
    "Kevin De Bruyne Belgium",
]
PLAYER_NATIONAL_TEAMS = {
    "cristiano ronaldo": "Portugal",
    "lionel messi": "Argentina",
    "kylian mbappe": "France",
    "lamine yamal": "Spain",
    "vinicius junior": "Brazil",
    "jude bellingham": "England",
    "christian pulisic": "United States",
    "alphonso davies": "Canada",
    "santiago gimenez": "Mexico",
    "mohamed salah": "Egypt",
    "erling haaland": "Norway",
    "kevin de bruyne": "Belgium",
}
KNOWN_FOOTBALL_PEOPLE = [
    "Ronaldo",
    "Cristiano Ronaldo",
    "Diogo Costa",
    "Joao Cancelo",
    "João Cancelo",
    "Nuno Mendes",
    "Ruben Dias",
    "Rúben Dias",
    "Diogo Dalot",
    "Bruno Fernandes",
    "Vitinha",
    "Bernardo Silva",
    "Rafael Leao",
    "Rafael Leão",
    "Pedro Neto",
    "Francisco Conceicao",
    "Francisco Conceição",
    "Messi",
    "Lionel Messi",
    "Kylian Mbappe",
    "Erling Haaland",
    "Neymar",
    "Vinicius Junior",
    "Jude Bellingham",
    "Lamine Yamal",
    "Kevin De Bruyne",
    "Mohamed Salah",
    "Harry Kane",
    "Robert Lewandowski",
    "Bukayo Saka",
    "Phil Foden",
    "Rodri",
    "Luka Modric",
    "Karim Benzema",
    "Christian Pulisic",
    "Alphonso Davies",
    "Santiago Gimenez",
]
KNOWN_NATIONAL_TEAMS = [
    ("Bồ Đào Nha", "Portugal national football team"),
    ("Portugal", "Portugal national football team"),
    ("Argentina", "Argentina national football team"),
    ("Brazil", "Brazil national football team"),
    ("đội tuyển Pháp", "France national football team"),
    ("France", "France national football team"),
    ("đội tuyển Anh", "England national football team"),
    ("England", "England national football team"),
    ("Tây Ban Nha", "Spain national football team"),
    ("Spain", "Spain national football team"),
    ("Mỹ", "United States national football team"),
    ("United States", "United States national football team"),
    ("Canada", "Canada national football team"),
    ("Mexico", "Mexico national football team"),
    ("Colombia", "Colombia national football team"),
    ("Uzbekistan", "Uzbekistan national football team"),
    ("CHDC Congo", "DR Congo national football team"),
    ("DR Congo", "DR Congo national football team"),
]
PLAYER_FOCUS_WORDS = {
    "messi",
    "ronaldo",
    "mbappe",
    "haaland",
    "neymar",
    "vinicius",
    "bellingham",
    "yamal",
    "pulisic",
    "davies",
    "gimenez",
    "salah",
    "kane",
    "lewandowski",
    "saka",
    "foden",
    "rodri",
    "modric",
    "benzema",
    "costa",
    "cancelo",
    "mendes",
    "dias",
    "dalot",
    "fernandes",
    "vitinha",
    "silva",
    "leao",
    "neto",
    "conceicao",
}
GENERIC_VISUAL_TERMS = {
    "stadium",
    "arena",
    "venue",
    "fans",
    "supporters",
    "crowd",
    "spectators",
    "mascot",
    "trophy",
    "ball",
    "poster",
    "host city",
}


@dataclass(frozen=True)
class AutoImageResult:
    audio_path: Path
    created: list[Path]
    skipped_reason: str = ""
    queries: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()


def prepare_auto_images(
    config,
    paths: dict[str, Path],
    limit: int | None = None,
    audio_paths: list[Path] | None = None,
) -> list[AutoImageResult]:
    image_config = config.get("auto_player_images", default={}) or {}
    if not isinstance(image_config, dict) or not bool(image_config.get("enabled", False)):
        return []

    audio_dir = paths["audio_dir"]
    image_dir = paths["image_dir"]
    short_image_dir = paths.get("short_image_dir", image_dir)
    overwrite = bool(image_config.get("overwrite", False))
    query_suffix = str(image_config.get("query_suffix", "football")).strip()
    user_agent = str(image_config.get("user_agent") or DEFAULT_USER_AGENT)
    providers = normalized_providers(image_config.get("providers"))

    image_dir.mkdir(parents=True, exist_ok=True)
    short_image_dir.mkdir(parents=True, exist_ok=True)

    audio_files = (
        [path for path in audio_paths if path.exists() and path.suffix.lower() in AUDIO_EXTENSIONS]
        if audio_paths is not None
        else list_files(audio_dir, AUDIO_EXTENSIONS)
    )
    if limit:
        audio_files = audio_files[:limit]

    results: list[AutoImageResult] = []
    for audio_path in audio_files:
        count = target_image_count(audio_path, config, image_config)
        destinations = unique_paths([image_dir, short_image_dir])
        existing = find_matching_images(audio_path, list_files(image_dir, IMAGE_EXTENSIONS))
        existing_short = find_matching_images(audio_path, list_files(short_image_dir, IMAGE_EXTENSIONS))
        if not overwrite and len(existing) >= count and len(existing_short) >= count:
            write_contextual_visual_timeline(audio_path, short_image_dir)
            results.append(
                AutoImageResult(
                    audio_path=audio_path,
                    created=[],
                    skipped_reason="already has enough images",
                )
            )
            continue

        queries = build_football_image_queries(audio_path, query_suffix)
        created, used_sources = download_multi_source_images(
            queries=queries,
            audio_stem=audio_path.stem,
            destinations=destinations,
            count=count,
            user_agent=user_agent,
            providers=providers,
            start_number=0 if overwrite else len(existing),
        )
        if created:
            write_contextual_visual_timeline(audio_path, short_image_dir)
            results.append(
                AutoImageResult(
                    audio_path=audio_path,
                    created=created,
                    queries=tuple(queries),
                    sources=tuple(sorted(used_sources)),
                )
            )
        else:
            results.append(
                AutoImageResult(
                    audio_path=audio_path,
                    created=[],
                    skipped_reason=f"no images found for {queries!r}",
                    queries=tuple(queries),
                )
            )
    return results


def write_contextual_visual_timeline(audio_path: Path, image_dir: Path) -> Path | None:
    srt_path = audio_path.with_suffix(".auto.srt")
    manifest_path = image_dir / f"{audio_path.stem}.images.json"
    if not srt_path.exists() or not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    entries = parse_srt_entries(srt_path.read_text(encoding="utf-8-sig", errors="ignore"))
    images = [
        item
        for item in manifest
        if isinstance(item, dict) and (image_dir / str(item.get("file") or "")).exists()
    ]
    if not entries or not images:
        return None

    visual_groups: list[tuple[list[str], list[str], str]] = []
    grouped_files: dict[str, list[str]] = {}
    for item in images:
        query = str(item.get("query") or "").strip()
        if query:
            grouped_files.setdefault(query, []).append(str(item["file"]))
    for query, files in grouped_files.items():
        keywords = meaningful_word_list(query)
        aliases = []
        if keywords:
            aliases.append(" ".join(keywords))
            aliases.extend(reversed(keywords))
        aliases = dedupe_text([alias for alias in aliases if len(alias) >= 3])
        if aliases:
            visual_groups.append((aliases, files, query))

    counters: dict[str, int] = {}
    timeline: list[dict[str, Any]] = []
    current_file = str(images[0]["file"])
    cursor = 0.0
    for entry in entries:
        text = str(entry["text"])
        normalized = normalize_text(text)
        entry_start = max(cursor, float(entry["start"]))
        if entry_start > cursor:
            append_visual_segment(timeline, current_file, cursor, entry_start, "subtitle gap")
        entry_end = max(entry_start, float(entry["end"]))
        cursor = entry_end
        mentions: list[tuple[int, str, str]] = []
        for aliases, files, query in visual_groups:
            for alias in aliases:
                index = normalized.find(alias)
                if index >= 0:
                    count = counters.get(query, 0)
                    mentions.append((index, files[count % len(files)], alias))
                    counters[query] = count + 1
                    break
        mentions.sort(key=lambda item: item[0])
        if not mentions:
            append_visual_segment(timeline, current_file, entry_start, entry_end, text)
            continue

        duration = max(0.2, entry_end - entry_start)
        slice_duration = duration / len(mentions)
        for index, (_, filename, label) in enumerate(mentions):
            start = entry_start + (index * slice_duration)
            end = entry_end if index == len(mentions) - 1 else start + slice_duration
            append_visual_segment(timeline, filename, start, end, label)
            current_file = filename

    output_path = audio_path.with_suffix(".visuals.json")
    output_path.write_text(json.dumps({"segments": timeline}, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def append_visual_segment(
    timeline: list[dict[str, Any]],
    filename: str,
    start: float,
    end: float,
    context: str,
) -> None:
    if end <= start:
        return
    if timeline and timeline[-1]["file"] == filename and abs(float(timeline[-1]["end"]) - start) < 0.35:
        timeline[-1]["end"] = round(end, 3)
        timeline[-1]["duration"] = round(float(timeline[-1]["end"]) - float(timeline[-1]["start"]), 3)
        return
    timeline.append(
        {
            "file": filename,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "context": context[:160],
        }
    )


def parse_srt_entries(value: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", value.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_text, end_text = [part.strip() for part in lines[1].split("-->", 1)]
        entries.append(
            {
                "start": parse_srt_seconds(start_text),
                "end": parse_srt_seconds(end_text),
                "text": " ".join(lines[2:]),
            }
        )
    return entries


def parse_srt_seconds(value: str) -> float:
    hours, minutes, rest = value.replace(".", ",").split(":")
    seconds, millis = rest.split(",", 1)
    return (int(hours) * 3600) + (int(minutes) * 60) + int(seconds) + (int(millis[:3]) / 1000)


def target_image_count(audio_path: Path, config, image_config: dict[str, Any]) -> int:
    max_count = max(1, min(MAX_AUTO_IMAGES, int(image_config.get("max_count", 30))))
    mode = str(image_config.get("count_mode", "fixed")).strip().lower()
    if mode == "duration":
        segment_seconds = float(config.get("shorts", "image_segment_seconds", default=2) or 2)
        max_duration = float(config.get("shorts", "max_duration_seconds", default=59) or 59)
        duration = min(probe_duration_seconds(audio_path), max_duration)
        return max(1, min(max_count, math.ceil(duration / max(0.5, segment_seconds))))
    return max(1, min(max_count, int(image_config.get("count", 5))))


def build_player_image_query(audio_path: Path, query_suffix: str) -> str:
    return build_football_image_queries(audio_path, query_suffix)[0]


def build_football_image_queries(audio_path: Path, query_suffix: str) -> list[str]:
    title = track_title(audio_path)
    transcript_path = audio_path.with_suffix(".txt")
    transcript = (
        transcript_path.read_text(encoding="utf-8-sig", errors="ignore")
        if transcript_path.exists()
        else ""
    )
    topics = extract_football_topics(title, transcript)
    queries = []
    for topic in topics:
        query = " ".join(f"{topic} {query_suffix}".split())
        if query.lower() not in {item.lower() for item in queries}:
            queries.append(query)
    return queries[:20] or [" ".join(f"{title} {query_suffix}".split())]


def extract_football_topics(title: str, transcript: str) -> list[str]:
    haystack = f"{title}\n{transcript}"
    normalized = normalize_text(haystack)
    topics = extract_dynamic_football_entities(title, transcript)

    event_patterns = [
        (r"\b(?:fifa\s+)?world\s+cup\s+2026\b", "FIFA World Cup 2026"),
        (r"\bworld\s+cup\b", "FIFA World Cup"),
        (r"\bchampions\s+league\b", "UEFA Champions League"),
        (r"\bpremier\s+league\b", "Premier League football"),
        (r"\bla\s+liga\b", "La Liga football"),
        (r"\bserie\s+a\b", "Serie A football"),
        (r"\beuro\s+2028\b", "UEFA Euro 2028"),
        (r"\beuro\s+2024\b", "UEFA Euro 2024"),
        (r"\bcopa\s+america\b", "Copa America football"),
    ]
    for pattern, label in event_patterns:
        if not topics and re.search(pattern, normalized):
            topics.append(label)

    inferred = infer_player_query(title, transcript)
    if (
        not topics
        and inferred
        and normalize_text(inferred) not in {"football", "bong da"}
    ):
        topics.append(inferred)
    return dedupe_text(topics)[:20]


def extract_dynamic_football_entities(title: str, transcript: str) -> list[str]:
    text = f"{title}\n{transcript}"
    title_normalized = normalize_text(title)
    candidates: list[tuple[int, str]] = []
    leading_stop = {
        "anh", "cung", "day", "dieu", "doi", "doi hinh", "hlv", "khi", "mot",
        "hang", "nam", "nhung", "tam", "theo", "trong", "tren", "tuyen", "va", "voi",
    }
    rejected = {
        "fifa", "world cup", "world cup 2026", "champions league", "premier league",
        "la liga", "serie a", "youtube", "shorts",
    }
    offset = 0
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        tokens = list(re.finditer(r"[^\W\d_]+(?:[’'-][^\W\d_]+)?", sentence, flags=re.UNICODE))
        index = 0
        while index < len(tokens):
            token = tokens[index].group(0)
            if not token[:1].isupper():
                index += 1
                continue
            run = [token]
            end_index = index + 1
            while end_index < len(tokens) and len(run) < 4:
                next_token = tokens[end_index].group(0)
                separator = sentence[tokens[end_index - 1].end() : tokens[end_index].start()]
                if (
                    separator.isspace()
                    and (next_token[:1].isupper() or normalize_text(next_token) in {"da", "de", "do", "dos", "van"})
                ):
                    run.append(next_token)
                    end_index += 1
                else:
                    break
            while len(run) > 1 and normalize_text(run[0]) in leading_stop:
                run.pop(0)
            value = " ".join(run).strip()
            key = normalize_text(value)
            if key and key not in leading_stop and key not in rejected and len(key) >= 4:
                if len(run) > 1 or key in title_normalized:
                    candidates.append((offset + tokens[index].start(), value))
                else:
                    occurrences = len(re.findall(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", normalize_text(text)))
                    if occurrences >= 2:
                        candidates.append((offset + tokens[index].start(), value))
            index = max(index + 1, end_index)
        offset += len(sentence) + 1
    return dedupe_text([value for _, value in sorted(candidates, key=lambda item: item[0])])


def extract_known_mentions(text: str, entities: list[str]) -> list[str]:
    normalized = normalize_text(text)
    matches: list[tuple[int, str]] = []
    seen: set[str] = set()
    for entity in entities:
        key = normalize_text(entity)
        index = normalized.find(key)
        canonical = canonical_person_name(entity)
        canonical_key = normalize_text(canonical)
        if index >= 0 and canonical_key not in seen:
            matches.append((index, canonical))
            seen.add(canonical_key)
    return [entity for _, entity in sorted(matches, key=lambda item: item[0])]


def extract_team_mentions(text: str) -> list[str]:
    normalized = normalize_text(text)
    matches: list[tuple[int, str]] = []
    seen: set[str] = set()
    for alias, canonical in KNOWN_NATIONAL_TEAMS:
        alias_key = normalize_text(alias)
        match = re.search(rf"(?<![a-z0-9]){re.escape(alias_key)}(?![a-z0-9])", normalized)
        index = match.start() if match else -1
        key = normalize_text(canonical)
        if index >= 0 and key not in seen:
            matches.append((index, canonical))
            seen.add(key)
    return [team for _, team in sorted(matches, key=lambda item: item[0])]


def canonical_person_name(value: str) -> str:
    aliases = {
        "ronaldo": "Cristiano Ronaldo",
        "messi": "Lionel Messi",
        "joao cancelo": "João Cancelo",
        "ruben dias": "Rúben Dias",
        "rafael leao": "Rafael Leão",
        "francisco conceicao": "Francisco Conceição",
    }
    return aliases.get(normalize_text(value), value)


def infer_player_query(title: str, transcript: str) -> str:
    haystack = f"{title}\n{transcript}"
    candidates: dict[str, int] = {}
    for match in re.finditer(r"\b[A-Z][A-Za-z']+(?:\s+[A-Z][A-Za-z']+){0,2}\b", haystack):
        value = " ".join(match.group(0).split())
        if is_bad_name_candidate(value):
            continue
        candidates[value] = candidates.get(value, 0) + 1
    if candidates:
        return sorted(candidates.items(), key=lambda item: (item[1], len(item[0])), reverse=True)[0][0]
    if any(term in normalize_text(title) for term in ["world cup", "champions league", "premier league"]):
        return ""
    return title


def is_bad_name_candidate(value: str) -> bool:
    lowered = normalize_text(value)
    stop = {
        "football",
        "shorts",
        "youtube",
        "tiktok",
        "today",
        "world cup",
        "champions league",
        "fifa world cup",
    }
    return lowered in stop or len(value) < 3 or any(char.isdigit() for char in value)


def download_multi_source_images(
    queries: list[str],
    audio_stem: str,
    destinations: list[Path],
    count: int,
    user_agent: str,
    providers: list[str],
    start_number: int = 0,
) -> tuple[list[Path], set[str]]:
    provider_functions: dict[str, Callable[[str, int, str], list[dict[str, Any]]]] = {
        "openverse": openverse_image_candidates,
        "wikimedia": commons_image_candidates,
    }
    candidates: list[dict[str, Any]] = []
    per_query_limit = max(20, math.ceil(count * 3 / max(1, len(queries))))
    searches = [
        (query, provider, provider_functions[provider])
        for query in queries
        for provider in providers
        if provider in provider_functions
    ]
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(searches)))) as executor:
        futures = {
            executor.submit(fetcher, query, per_query_limit, user_agent): (query, provider)
            for query, provider, fetcher in searches
        }
        for future in as_completed(futures):
            try:
                candidates.extend(future.result())
            except (OSError, ValueError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                continue

    created: list[Path] = []
    used_sources: set[str] = set()
    used_urls: set[str] = set()
    used_titles: set[str] = set()
    manifest: list[dict[str, Any]] = []
    for candidate in prioritized_candidates(candidates):
        url = str(candidate.get("download_url") or candidate.get("thumburl") or candidate.get("url") or "")
        identity = str(candidate.get("id") or candidate.get("url") or url)
        title_key = canonical_image_title(str(candidate.get("title") or ""))
        if not url or identity in used_urls or url in used_urls or (title_key and title_key in used_titles):
            continue
        used_urls.update({identity, url})
        if title_key:
            used_titles.add(title_key)
        extension = extension_for_candidate(candidate, url)
        if extension not in IMAGE_EXTENSIONS:
            continue
        image_number = start_number + (len(created) // max(1, len(destinations))) + 1
        if image_number > count:
            break
        primary = destinations[0] / f"{audio_stem}-{image_number:02}{extension}"
        try:
            download_url(url, primary, user_agent=user_agent)
            if primary.stat().st_size < 10_000:
                primary.unlink(missing_ok=True)
                continue
            created.append(primary)
            for destination in destinations[1:]:
                copy_target = destination / primary.name
                shutil.copyfile(primary, copy_target)
                created.append(copy_target)
            source = str(candidate.get("source") or "unknown")
            used_sources.add(source)
            manifest.append(
                {
                    "file": primary.name,
                    "title": candidate.get("title"),
                    "source": source,
                    "creator": candidate.get("creator"),
                    "license": candidate.get("license"),
                    "source_url": candidate.get("source_url") or candidate.get("url"),
                    "query": candidate.get("query"),
                }
            )
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            primary.unlink(missing_ok=True)
            continue

    if manifest:
        manifest_path = destinations[0] / f"{audio_stem}.images.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return created, used_sources


def download_commons_images(
    query: str,
    audio_stem: str,
    destinations: list[Path],
    count: int,
    user_agent: str,
) -> list[Path]:
    created, _ = download_multi_source_images(
        queries=[query],
        audio_stem=audio_stem,
        destinations=destinations,
        count=count,
        user_agent=user_agent,
        providers=["wikimedia"],
    )
    return created


def openverse_image_candidates(query: str, limit: int, user_agent: str) -> list[dict[str, Any]]:
    params = {
        "q": query,
        "page_size": str(min(80, limit)),
        "mature": "false",
        "license_type": "commercial",
    }
    request = urllib.request.Request(
        f"{OPENVERSE_API_URL}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        data = json.loads(response.read().decode("utf-8"))

    candidates = []
    for item in data.get("results", []):
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        url = str(item.get("thumbnail") or item.get("url") or "")
        title = str(item.get("title") or "")
        if (
            not url
            or width < 480
            or height < 480
            or should_skip_title(title)
            or not candidate_matches_focus(title, query)
        ):
            continue
        candidates.append(
            {
                "id": f"openverse:{item.get('id')}",
                "download_url": url,
                "url": item.get("url"),
                "source_url": item.get("foreign_landing_url"),
                "mime": mimetypes.guess_type(urllib.parse.urlparse(url).path)[0] or "image/jpeg",
                "width": width,
                "height": height,
                "title": item.get("title"),
                "creator": item.get("creator"),
                "license": item.get("license"),
                "source": f"openverse:{item.get('source') or 'unknown'}",
                "query": query,
            }
        )
    return sorted(candidates, key=image_score, reverse=True)


def commons_image_candidates(query: str, limit: int, user_agent: str) -> list[dict[str, Any]]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": str(min(50, limit)),
        "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata",
        "iiurlwidth": "1080",
    }
    request = urllib.request.Request(
        f"{COMMONS_API_URL}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": user_agent},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        data = json.loads(response.read().decode("utf-8"))

    candidates: list[dict[str, Any]] = []
    for page in data.get("query", {}).get("pages", {}).values():
        title = str(page.get("title") or "")
        info_list = page.get("imageinfo") or []
        if should_skip_title(title) or not info_list or not candidate_matches_focus(title, query):
            continue
        info = dict(info_list[0])
        if not is_usable_image(info):
            continue
        metadata = info.get("extmetadata") or {}
        candidates.append(
            {
                **info,
                "id": f"wikimedia:{page.get('pageid')}",
                "download_url": info.get("thumburl") or info.get("url"),
                "source_url": info.get("descriptionurl"),
                "title": title,
                "creator": metadata_value(metadata, "Artist"),
                "license": metadata_value(metadata, "LicenseShortName"),
                "source": "wikimedia",
                "query": query,
            }
        )
    return sorted(candidates, key=image_score, reverse=True)


def metadata_value(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key) or {}
    return str(value.get("value") or "") if isinstance(value, dict) else str(value)


def is_usable_image(info: dict[str, Any]) -> bool:
    mime = str(info.get("mime") or "").lower()
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    return mime in {"image/jpeg", "image/png", "image/webp"} and width >= 480 and height >= 480


def image_score(info: dict[str, Any]) -> int:
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    portrait_bonus = 500 if height >= width else 0
    return min(width, 1600) + min(height, 1600) + portrait_bonus


def should_skip_title(title: str) -> bool:
    lowered = title.lower()
    skip_terms = [
        "logo",
        "icon",
        "svg",
        "kit",
        "jersey",
        "shirt",
        "signature",
        "flag",
        "map",
        "diagram",
        "stadium",
        "arena",
        "venue",
        "fans",
        "supporters",
        "crowd",
        "spectators",
        "mascot",
        "trophy",
        "poster",
        "women",
        "women's",
        "female",
        "rugby",
        "olympic",
        "uniform",
        "medal ceremony",
        "impersonator",
        "imitating",
        "imitanta",
        "lookalike",
        "look-alike",
        "wax",
        "statue",
        "estatua",
        "mural",
        "maillot",
        "red carpet",
        "award",
        "potm",
        "trading card",
        "video game",
        "ea sports",
        "museum",
        "museu",
        "television",
        "tv show",
        "talk show",
        "podcast",
        "comedy",
        "gozar com quem trabalha",
    ]
    if any(term in lowered for term in skip_terms):
        return True
    years = [int(year) for year in re.findall(r"\b(19\d{2}|20\d{2})\b", lowered)]
    return bool(years and max(years) < 2020)


def candidate_matches_focus(title: str, query: str) -> bool:
    normalized_title = normalize_text(title)
    normalized_query = normalize_text(query)
    title_words = meaningful_words(title)
    query_word_list = meaningful_word_list(query)
    query_words = set(query_word_list)
    if not title_words or not query_words:
        return False
    required = [
        word
        for word in query_word_list
        if word not in {"football", "soccer", "national", "team", "fifa", "world", "cup", "2026"}
    ]
    if "national" in normalized_query and "team" in normalized_query:
        if len(required) == 1:
            return required[0] in title_words and any(
                term in normalized_title for term in ["national", "football", "soccer"]
            )
        player_words = required[:-1]
        country_word = required[-1]
        return (
            any(word in title_words for word in player_words)
            or (
                country_word in title_words
                and any(term in normalized_title for term in ["national", "football", "soccer"])
            )
        )
    if required:
        return any(word in title_words for word in required)
    return bool(title_words & {"player", "players", "team", "teams", "football", "soccer", "match"})


def extension_for_candidate(candidate: dict[str, Any], url: str) -> str:
    mime = str(candidate.get("mime") or "").lower()
    guessed = mimetypes.guess_extension(mime) or Path(urllib.parse.urlparse(url).path).suffix.lower()
    if guessed == ".jpe":
        return ".jpg"
    return guessed.lower()


def download_url(url: str, target: Path, user_agent: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=35) as response:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if content_type and not content_type.startswith("image/"):
            raise ValueError(f"Unexpected content type: {content_type}")
        target.write_bytes(response.read())


def normalized_providers(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["openverse", "wikimedia"]
    providers = [str(item).strip().lower() for item in value if str(item).strip()]
    return [item for item in providers if item in {"openverse", "wikimedia"}] or ["openverse", "wikimedia"]


def interleave_by_query(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        groups.setdefault(str(candidate.get("query") or ""), []).append(candidate)
    ordered: list[dict[str, Any]] = []
    while groups:
        for query in list(groups):
            if groups[query]:
                ordered.append(groups[query].pop(0))
            if not groups[query]:
                del groups[query]
    return ordered


def prioritized_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    focused = [
        candidate
        for candidate in candidates
        if not any(term in normalize_text(str(candidate.get("title") or "")) for term in GENERIC_VISUAL_TERMS)
    ]
    focused.sort(key=candidate_focus_score, reverse=True)
    return interleave_by_query(focused)


def candidate_focus_score(candidate: dict[str, Any]) -> int:
    title = normalize_text(str(candidate.get("title") or ""))
    query = normalize_text(str(candidate.get("query") or ""))
    score = image_score(candidate)
    for word in meaningful_words(query):
        if word in meaningful_words(title):
            score += 900
    if any(term in title for term in ["player", "team", "match", "footballer", "squad"]):
        score += 700
    years = [int(year) for year in re.findall(r"\b(20\d{2})\b", title)]
    if years:
        newest = max(years)
        if newest >= 2024:
            score += 1800
        elif newest >= 2020:
            score += 500
    if any(term in title for term in GENERIC_VISUAL_TERMS):
        score -= 5000
    return score


def meaningful_words(value: str) -> set[str]:
    return set(meaningful_word_list(value))


def meaningful_word_list(value: str) -> list[str]:
    stop = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "football",
        "soccer",
        "national",
        "team",
        "fifa",
        "world",
        "cup",
        "2026",
    }
    return [
        word
        for word in re.findall(r"[a-z0-9]+", normalize_text(value))
        if len(word) >= 3 and word not in stop
    ]


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", normalized.lower()).strip()


def canonical_image_title(value: str) -> str:
    normalized = normalize_text(value)
    normalized = re.sub(r"^file\s*:\s*", "", normalized)
    normalized = re.sub(r"\(\s*cropped\d*\s*\)", "", normalized)
    normalized = re.sub(r"\b(cropped|crop|thumbnail|thumb)\d*\b", "", normalized)
    normalized = re.sub(r"\b(jpg|jpeg|png|webp)\b$", "", normalized)
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def dedupe_text(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        key = normalize_text(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            unique.append(path)
            seen.add(resolved)
    return unique
