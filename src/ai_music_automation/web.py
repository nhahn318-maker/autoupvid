from __future__ import annotations

"""Compatibility loader for the recovered full web backend.

The working-tree source was accidentally replaced by an old Git version on
2026-06-21. The last complete Python 3.11 bytecode is kept beside this file so
the application remains fully operational while the readable source is being
reconstructed in ``recovery/web-py-20260621``.
"""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


_RECOVERED_NAME = f"{__package__}._web_recovered"
_RECOVERED_PATH = Path(__file__).with_name("_web_recovered.bytecode")


def _load_recovered_module() -> ModuleType:
    existing = sys.modules.get(_RECOVERED_NAME)
    if existing is not None:
        return existing
    if not _RECOVERED_PATH.exists():
        raise RuntimeError(f"Recovered web backend is missing: {_RECOVERED_PATH}")

    loader = importlib.machinery.SourcelessFileLoader(_RECOVERED_NAME, str(_RECOVERED_PATH))
    spec = importlib.util.spec_from_loader(_RECOVERED_NAME, loader)
    if spec is None:
        raise RuntimeError(f"Cannot create module spec for {_RECOVERED_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_RECOVERED_NAME] = module
    loader.exec_module(module)
    return module


_recovered = _load_recovered_module()

# Monkey-patch generate_voice to insert the hook intro for Vietnamese 20min videos
_orig_generate_voice = _recovered.generate_voice

import inspect

def _patched_generate_voice(text, title, voice, output_dir, rate="-5%"):
    caller_frame = inspect.currentframe().f_back
    caller_name = caller_frame.f_code.co_name if caller_frame else ""
    
    if caller_name == "run_fullauto_twenty_min_job" and voice.startswith("vi"):
        try:
            fullauto_config = caller_frame.f_locals.get("fullauto_config", {})
            provider = caller_frame.f_locals.get("provider")
            model = caller_frame.f_locals.get("model")
            api_key = caller_frame.f_locals.get("api_key")
            base_url = caller_frame.f_locals.get("base_url")
            checkpoint_dir = caller_frame.f_locals.get("checkpoint_dir")
            job = caller_frame.f_locals.get("job")
            
            # Generate the hook using AI based on the style
            prompt_hook = (
                "Dựa trên kịch bản video Phật Pháp dưới đây, hãy viết duy nhất 1 câu dẫn nhập (hook intro) ngắn khoảng 1-2 câu để thu hút người nghe ở lại xem video.\n"
                "Câu dẫn nhập này phải viết theo đúng phong cách (style) và giọng điệu của các ví dụ sau:\n"
                "1. Đừng vội lướt qua, vì những phút tiếp theo có thể giúp bạn hiểu vì sao có người càng kiếm tiền càng khổ, còn có người sống giản dị mà phước lành vẫn đến.\n"
                "2. Bạn thân mến, tiền tài không xấu. Nhưng nếu tâm không đủ sáng, chính tiền tài lại có thể trở thành nguồn gốc của lo âu, hơn thua và khổ đau.\n"
                "3. Có bao giờ bạn tự hỏi: vì sao có người làm bao nhiêu cũng không giữ được tiền, còn có người đi đến đâu cũng gặp quý nhân và cơ hội?\n"
                "4. Nghe hết video này, bạn sẽ hiểu rằng tài lộc không chỉ đến từ sự cố gắng, mà còn đến từ phước đức, nhân quả và cái tâm của mỗi người.\n"
                "5. Đức Phật không dạy con người ghét bỏ tiền bạc. Ngài dạy ta cách dùng tiền mà không bị tiền làm chủ.\n\n"
                "Yêu cầu:\n"
                "- Viết bằng tiếng Việt có dấu, giọng ấm áp sâu lắng.\n"
                "- Chỉ trả về duy nhất câu dẫn nhập đó, không có dấu ngoặc kép bên ngoài, không giải thích, không tiêu đề hay lời dẫn nào khác.\n\n"
                f"Kịch bản video:\n{text[:3000]}"
            )
            
            _recovered.log(job, "Đang gọi AI tạo câu hook dẫn nhập theo ngữ cảnh kịch bản...")
            hook = _recovered.call_fullauto_long_model(
                provider=provider,
                model=model,
                prompt=prompt_hook,
                api_key=api_key,
                base_url=base_url
            )
            if hook:
                hook = hook.strip().strip('\"').strip('\'')
                text = hook + "\n\n" + text
                _recovered.log(job, f"Đã thêm câu hook intro tự động: '{hook}'")
                
                # Write updated script to complete-script.txt
                if checkpoint_dir:
                    (checkpoint_dir / "complete-script.txt").write_text(text, encoding="utf-8")
        except Exception as e:
            try:
                _recovered.log(job, f"Lỗi tạo hook tự động: {e}")
            except Exception:
                pass

    return _orig_generate_voice(text, title, voice, output_dir, rate=rate)

_recovered.generate_voice = _patched_generate_voice

# ── FIX: patch job_worker to use blocking get() instead of get_nowait() ──────
# The original bytecode uses get_nowait(), so the worker thread exits immediately
# when the queue is empty. This means only the first job in a batch gets picked up;
# all subsequent jobs stay "Waiting for worker" forever.
# We replace job_worker with a blocking version that stays alive indefinitely.
import queue as _queue_module
import threading as _threading_module

def _patched_job_worker():
    while True:
        try:
            job_id, action, payload = _recovered.JOB_QUEUE.get(timeout=30)
        except _queue_module.Empty:
            # No job for 30s — exit so ensure_worker_running can restart if needed
            return
        try:
            _recovered.run_action(job_id, action, payload)
        except Exception:
            pass
        finally:
            try:
                _recovered.JOB_QUEUE.task_done()
            except Exception:
                pass

_recovered.job_worker = _patched_job_worker

# Also patch ensure_worker_running to always use the patched worker
_orig_ensure_worker = _recovered.ensure_worker_running
def _patched_ensure_worker_running():
    with _recovered.WORKER_LOCK:
        if _recovered.WORKER_THREAD is not None and _recovered.WORKER_THREAD.is_alive():
            return
        t = _threading_module.Thread(target=_patched_job_worker, daemon=True)
        _recovered.WORKER_THREAD = t
        t.start()

_recovered.ensure_worker_running = _patched_ensure_worker_running
# ─────────────────────────────────────────────────────────────────────────────

for _name, _value in vars(_recovered).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


def __getattr__(name: str):
    return getattr(_recovered, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_recovered)))
