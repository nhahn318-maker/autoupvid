from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any


class ClipScorerProcess:
    """Small persistent CLIP worker using the existing ComfyUI Python env."""

    def __init__(self, settings: dict[str, Any], root: Path) -> None:
        self.settings = settings
        self.root = root
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> bool:
        if not bool(self.settings.get("clip_review_enabled", False)):
            return False
        python_path = self.root / str(
            self.settings.get("comfyui_python") or "tools/ComfyUI/.venv/Scripts/python.exe"
        )
        if not python_path.exists():
            return False
        cache_dir = self.root / str(
            self.settings.get("clip_model_cache_dir")
            or "tools/ComfyUI/models/clip_vision/huggingface"
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(python_path),
            str(Path(__file__).resolve()),
            "--worker",
            "--model",
            str(self.settings.get("clip_model") or "openai/clip-vit-base-patch32"),
            "--cache-dir",
            str(cache_dir),
        ]
        if not bool(self.settings.get("clip_allow_download", False)):
            command.append("--local-files-only")
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            timeout = max(30, int(self.settings.get("clip_startup_timeout_seconds") or 180))
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.process.stdout.readline) if self.process.stdout else None
                try:
                    ready = future.result(timeout=timeout).strip() if future else ""
                except FutureTimeoutError:
                    self.close()
                    return False
            return ready == '{"status": "ready"}'
        except OSError:
            self.close()
            return False

    def score(self, prompt: str, paths: list[Path]) -> dict[Path, float]:
        if not self.process or self.process.poll() is not None or not self.process.stdin or not self.process.stdout:
            return {}
        request = {"prompt": prompt, "paths": [str(path.resolve()) for path in paths]}
        try:
            self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            response = json.loads(self.process.stdout.readline())
        except (BrokenPipeError, OSError, json.JSONDecodeError):
            return {}
        raw_scores = response.get("scores") if isinstance(response, dict) else None
        if not isinstance(raw_scores, list) or len(raw_scores) != len(paths):
            return {}
        return {path: float(score) for path, score in zip(paths, raw_scores)}

    def close(self) -> None:
        process, self.process = self.process, None
        if not process:
            return
        try:
            if process.stdin:
                process.stdin.write('{"command": "stop"}\n')
                process.stdin.flush()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()

    def __enter__(self) -> "ClipScorerProcess":
        self.start()
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def run_worker(model_name: str, cache_dir: str, local_files_only: bool) -> int:
    try:
        import torch
        from PIL import Image
        from transformers import CLIPModel, CLIPProcessor

        model = CLIPModel.from_pretrained(
            model_name,
            cache_dir=cache_dir,
            local_files_only=local_files_only,
            use_safetensors=True,
        )
        processor = CLIPProcessor.from_pretrained(
            model_name,
            cache_dir=cache_dir,
            local_files_only=local_files_only,
        )
        model.eval()
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), flush=True)
        return 2

    print(json.dumps({"status": "ready"}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("command") == "stop":
                return 0
            paths = [Path(value) for value in request.get("paths", [])]
            images = [Image.open(path).convert("RGB") for path in paths]
            inputs = processor(
                text=[str(request.get("prompt") or "storybook scene")],
                images=images,
                return_tensors="pt",
                padding=True,
                truncation=True,
            )
            with torch.inference_mode():
                outputs = model(**inputs)
                image_features = outputs.image_embeds
                text_features = outputs.text_embeds
                image_features = image_features / image_features.norm(dim=-1, keepdim=True)
                text_features = text_features / text_features.norm(dim=-1, keepdim=True)
                scores = (image_features @ text_features.T).squeeze(-1).tolist()
            for image in images:
                image.close()
            print(json.dumps({"scores": scores}), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc), "scores": []}), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--model", default="openai/clip-vit-base-patch32")
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--local-files-only", action="store_true")
    args = parser.parse_args()
    if args.worker:
        return run_worker(args.model, args.cache_dir, args.local_files_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
