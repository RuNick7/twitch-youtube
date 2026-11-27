"""Распознавание речи через OpenRouter (Whisper large-v3-turbo, тот же, что у Groq).

Основной поставщик — DeepInfra ($0.012 за час звука); если он не отвечает, OpenRouter сам отдаёт запрос Groq
($0.04). Других поставщиков не берём: не проверено, возвращают ли они время фраз. Звук уходит в JSON (base64):
так проходит выбор поставщика, а кусок на 10 минут укладывается в лимит 25 МБ.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from .speech import LIMIT_WAIT_MAX, TIMEOUT, TranscribeError, post

URL = "https://openrouter.ai/api/v1/audio/transcriptions"
MODEL = "openai/whisper-large-v3-turbo"
PROVIDERS = ("deepinfra", "groq")  # по порядку: дешёвый первым
PRICE_PER_HOUR = 0.012  # DeepInfra; настоящая цена запроса приходит в ответе


def cost(data: dict, seconds: float) -> float:
    """Цена запроса из ответа, а если её там нет — по тарифу DeepInfra."""
    spent = (data.get("usage") or {}).get("cost")
    return float(spent) if spent is not None else seconds / 3600 * PRICE_PER_HOUR


async def transcribe_file(
    http: httpx.AsyncClient,
    api_key: str,
    path: Path,
    *,
    language: str = "ru",
    prompt: str | None = None,
    limit_wait: float = LIMIT_WAIT_MAX,
) -> dict:
    """Расшифровка одного файла: фразы со временем и оценками уверенности, как у Groq."""
    body = {
        "model": MODEL,
        "input_audio": {"data": base64.b64encode(path.read_bytes()).decode(), "format": path.suffix.lstrip(".").lower()},
        "language": language,
        "response_format": "verbose_json",
        "timestamp_granularities": ["segment"],
        "temperature": 0,
        "provider": {"only": list(PROVIDERS), "order": list(PROVIDERS), "allow_fallbacks": True},
    }
    if prompt:
        body["prompt"] = prompt
    data = await post(
        lambda: http.post(URL, headers={"Authorization": f"Bearer {api_key}"}, json=body, timeout=TIMEOUT),
        service="OpenRouter",
        filename=path.name,
        limit_wait=limit_wait,
    )
    if not isinstance(data.get("segments"), list):
        raise TranscribeError("OpenRouter вернул расшифровку без времени фраз: таймкоды по ней не построить")
    return data
