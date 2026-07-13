# AI YouTube Automation Studio

An end-to-end local automation system for creating, rendering, scheduling, and improving faceless YouTube videos with LLMs, TTS, image generation, FFmpeg, and YouTube APIs.

This project was built as a practical AI orchestration pipeline: it plans content, generates scripts, creates voices and visuals, renders videos, uploads to YouTube, tracks jobs, supports resume after interruption, and collects analytics for future optimization.

## Highlights

- Multi-channel YouTube automation for Shorts, 20-minute videos, long videos, and sleep stories.
- Local LLM orchestration with Ollama/Gemma for topic planning, scripts, metadata, hooks, and review.
- Text-to-speech pipeline with subtitle generation and speech/media QA.
- ComfyUI image generation support for story scenes, thumbnails, and watercolor-style sleep story visuals.
- FFmpeg rendering with subtitles, ambience, low-bed audio, effects, stickers, and format-specific layouts.
- Persistent job queue with progress logs, failure handling, checkpoint/resume, and email notifications.
- Mobile/PWA control panel for starting jobs remotely through a local server or private tunnel.
- YouTube Analytics and Reporting API integration for CTR, impressions, watch time, views, retention signals, and optimization reports.

## Main Pipelines

### 1. Standard Music/Audio Video

```text
Audio + image assets
-> metadata generation
-> horizontal video render
-> optional Shorts render
-> schedule validation
-> YouTube upload
```

Used for MP3-based videos where local audio and image assets are already available.

### 2. Buddhist Shorts Automation

```text
Prompt pool
-> LLM title/script/image prompts
-> format and hook validation
-> TTS voice
-> vertical image selection
-> Shorts render
-> metadata/thumbnail
-> scheduled upload
```

The Shorts pipeline includes guardrails to avoid repeated low-quality hooks, duplicated titles, missing image prompts, and weak first seconds.

### 3. Buddhist 20-Minute Videos

```text
Topic prompt
-> chapter outline
-> chapter scripts
-> duplicate and length QA
-> TTS
-> subtitles
-> ambience/effects/stickers
-> 1080p render
-> metadata + timeline
-> upload
```

Designed for repeatable long-form spiritual content with stronger hooks, chapter variety, ambient audio, and YouTube-friendly metadata.

### 4. Buddhist Long Videos

```text
Channel-specific topic
-> 18-chapter outline
-> chapter-by-chapter writing
-> checkpoint after each chapter
-> script QA
-> long TTS
-> render
-> upload
```

The long-video flow saves checkpoints so interrupted jobs can resume instead of restarting from zero.

### 5. Sleep Story Auto Agent

```text
Auto topic or custom title
-> story planner
-> story writer
-> multi-stage reviewer
-> rewrite if needed
-> visual bible
-> scene planner
-> optimized image prompts
-> voice generation
-> ComfyUI scene images
-> thumbnail
-> speech/media QA
-> 1080p render
-> upload
```

This flow targets adult bedtime stories with emotional hooks, a small mystery, character consistency, scene-level image prompts, watercolor art direction, ambient sound, and sleep-safe pacing.

## Architecture

```text
src/ai_music_automation/
  agents/               LLM agents for story planning, review, image QA, metadata, etc.
  automation/           Shared artifacts, cache, logging, model clients, and pipeline helpers.
  web.py                FastAPI backend and job orchestration.
  web_static/           Desktop and mobile web UI.
  story_before_sleep.py Sleep story rendering and asset pipeline.
  render.py             FFmpeg video rendering.
  tts.py                Voice and subtitle generation.
  youtube.py            YouTube upload and OAuth helpers.
  youtube_analytics.py  Analytics API collector.
  youtube_reporting.py  Reporting API collector.
tests/
  test_automation_reliability.py
```

## Job System

All heavy actions run as jobs:

- `queued`: waiting for the current job to finish.
- `running`: actively generating, rendering, or uploading.
- `done`: completed successfully.
- `failed`: failed with a visible error log.
- `interrupted`: backend stopped while the job was running; resume may be available.

The job store is persisted locally with SQLite so the UI can show recent history after a backend restart.

## Quality Controls

- Script length and duplicate-content checks.
- Story structure and retention review for sleep stories.
- JSON fallback handling when local models return malformed reviewer output.
- Scene/image prompt validation.
- Subtitle end-time checks.
- Media validation before upload.
- Thumbnail fallback from existing scene images when image generation fails.
- Shorts hook/title sanitizer to avoid repeated spam-like openings.

## Analytics Feedback Loop

The analytics module can sync:

- Views.
- Watch time.
- Average view duration.
- Average view percentage.
- Likes/comments/subscribers gained.
- Thumbnail impressions.
- Thumbnail CTR.

These metrics are used by the View Optimizer to compare topic, title, hook, thumbnail style, and content format performance over time.

## Local Setup

Requirements:

- Windows
- Python 3.11+
- FFmpeg
- Optional: Ollama for local LLM generation
- Optional: ComfyUI for local image generation
- YouTube Data API credentials for upload
- YouTube Analytics/Reporting API credentials for analytics sync

Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Copy and edit config:

```powershell
Copy-Item config.example.json config.json
```

Run the desktop web UI:

```powershell
.\run_gui.ps1
```

Open:

```text
http://127.0.0.1:8000
```

Run the mobile gateway:

```powershell
.\run_mobile_gateway.ps1
```

## CLI Examples

Initialize folders/config:

```powershell
.\run.ps1 init
```

Render one local audio/video item:

```powershell
.\run.ps1 render -Limit 1
```

Dry-run upload scheduling:

```powershell
.\run.ps1 daily -DryRun
```

Run real upload scheduling:

```powershell
.\run.ps1 daily
```

## Data and Secrets

This repository is intended to keep code and templates in Git while generated assets stay local.

Ignored local data includes:

- OAuth tokens and client secrets.
- `config.json`.
- generated videos/audio/images.
- local model/tool folders.
- story drafts, analytics exports, research notes, and logs.

Use `config.example.json` as a template and keep production credentials outside Git.

## Tests

Run reliability tests:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

## Portfolio Notes

This project demonstrates:

- LLM workflow orchestration.
- AI agent-style planning, reviewing, rewriting, and QA.
- Local model integration with resource-aware execution.
- Media generation pipelines using TTS, image generation, and FFmpeg.
- Background job processing, resume/checkpoint design, and operational logging.
- YouTube API integration for upload, scheduling, and analytics feedback.
