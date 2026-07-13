from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelRequest:
    prompt: str
    model: str
    base_url: str = "http://127.0.0.1:11434"
    temperature: float = 0.75
    top_p: float = 0.92
    timeout_seconds: int = 240
    response_format: str | None = None
    context_tokens: int | None = None


class OllamaClient:
    """Minimal Ollama generate client used by automation agents."""

    def __init__(self) -> None:
        self.last_error = ""
        self.last_duration_seconds = 0.0

    def generate(self, request: ModelRequest) -> str:
        started = time.monotonic()
        self.last_error = ""
        endpoint = request.base_url.rstrip("/") + "/api/generate"
        request_body: dict[str, Any] = {
            "model": request.model,
            "prompt": request.prompt,
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "top_p": request.top_p,
            },
        }
        if request.response_format:
            request_body["format"] = request.response_format
        if request.context_tokens:
            request_body["options"]["num_ctx"] = max(2048, int(request.context_tokens))
        payload = json.dumps(request_body).encode("utf-8")
        http_request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(http_request, timeout=request.timeout_seconds) as response:
                data: Any = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            self.last_error = f"Ollama HTTP {exc.code}: {detail or exc.reason}"
            self.last_duration_seconds = time.monotonic() - started
            return ""
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            self.last_error = f"Ollama request failed: {type(exc).__name__}: {exc}"
            self.last_duration_seconds = time.monotonic() - started
            return ""
        if not isinstance(data, dict):
            self.last_error = "Ollama returned a non-object response."
            self.last_duration_seconds = time.monotonic() - started
            return ""
        if data.get("error"):
            self.last_error = f"Ollama model error: {data.get('error')}"
            self.last_duration_seconds = time.monotonic() - started
            return ""
        output = str(data.get("response") or "")
        if not output.strip():
            self.last_error = "Ollama returned an empty response."
        self.last_duration_seconds = time.monotonic() - started
        return output

    def unload(
        self,
        base_url: str,
        model: str,
        timeout_seconds: int = 30,
        force_after_seconds: int = 0,
    ) -> bool:
        """Release Ollama model memory before local image/render workloads."""
        endpoint = base_url.rstrip("/") + "/api/generate"
        payload = json.dumps({"model": model, "prompt": "", "stream": False, "keep_alive": 0}).encode("utf-8")
        request = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data: Any = json.loads(response.read().decode("utf-8"))
            accepted = isinstance(data, dict) and not data.get("error")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
            return False
        if not accepted or force_after_seconds <= 0:
            return accepted
        deadline = time.monotonic() + force_after_seconds
        while time.monotonic() < deadline:
            if not self._model_is_loaded(base_url, model):
                return True
            time.sleep(1)
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/IM", "llama-server.exe", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            time.sleep(1)
        return not self._model_is_loaded(base_url, model)

    @staticmethod
    def _model_is_loaded(base_url: str, model: str) -> bool:
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + "/api/ps", timeout=5) as response:
                data: Any = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
            return False
        target = model.split(":", 1)[0].lower()
        return any(
            target in str(item.get("name") or item.get("model") or "").lower()
            for item in (data.get("models") or [])
            if isinstance(item, dict)
        )
