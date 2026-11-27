"""Распознавание речи через Groq (Whisper): API совместим с OpenAI, цена — за секунды звука.

Запасной сервис: бесплатный тариф ограничен 7200 секундами звука в час, а платный сейчас не купить.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import httpx

from .speech import LIMIT_WAIT_MAX, RETRIES, RETRY_AFTER_MAX, TIMEOUT, TranscribeError, post

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3-turbo"
PRICE_PER_HOUR = {"whisper-large-v3-turbo": 0.04, "whisper-large-v3": 0.111}
MIN_BILLED_SEC = 10  # запрос короче 10 секунд оплачивается как 10


def cost(chunk_seconds: Sequence[float], model: str = MODEL) -> float:
    """Во сколько обошлась бы расшифровка кусков звука по платному тарифу, в долларах."""
    billed = sum(max(seconds, MIN_BILLED_SEC) for seconds in chunk_seconds)
    return billed / 3600 * PRICE_PER_HOUR.get(model, PRICE_PER_HOUR[MODEL])


async def transcribe_file(
    http: httpx.AsyncClient,
    api_key: str,
    path: Path,
    *,
    model: str = MODEL,
    language: str = "ru",
    prompt: str | None = None,
    limit_wait: float = LIMIT_WAIT_MAX,
) -> dict:
    """Расшифровка одного файла: ответ Groq в формате verbose_json (фразы со временем и оценками уверенности)."""
    audio = path.read_bytes()
    form = {
        "model": model,
        "language": language,
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
        "temperature": "0",
    }
    if prompt:
        form["prompt"] = prompt
    return await post(
        lambda: http.post(
            URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (path.name, audio, "application/octet-stream")},
            data=form,
            timeout=TIMEOUT,
        ),
        service="Groq",
        filename=path.name,
        limit_wait=limit_wait,
    )
