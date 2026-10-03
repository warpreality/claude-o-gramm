"""Расшифровка голосовых локально через faster-whisper.

Модель грузится при первом голосовом (и при самом первом запуске скачивается в ~/.cache/huggingface).
Расшифровываем по одному голосовому за раз, чтобы не съесть весь процессор и память.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()
_run_lock: asyncio.Lock | None = None


def _load():
    global _model
    with _model_lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise RuntimeError("расшифровка голосовых не установлена (пакет faster-whisper)") from e
            name = os.environ.get("WHISPER_MODEL", "").strip() or "small"
            log.info("загружаю модель Whisper %r (первый раз скачивается, это долго)", name)
            _model = WhisperModel(name, device="cpu", compute_type="int8")
        return _model


def _transcribe_sync(path: Path) -> str:
    model = _load()
    language = os.environ.get("WHISPER_LANGUAGE", "").strip() or None
    segments, _ = model.transcribe(str(path), language=language, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe(path: Path) -> str:
    global _run_lock
    if _run_lock is None:
        _run_lock = asyncio.Lock()
    async with _run_lock:
        return await asyncio.to_thread(_transcribe_sync, path)
