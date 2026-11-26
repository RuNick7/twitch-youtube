"""Распознавание речи через Groq (Whisper): API совместим с OpenAI, цена — за секунды звука."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Sequence

import httpx

log = logging.getLogger(__name__)

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3-turbo"
PRICE_PER_HOUR = {"whisper-large-v3-turbo": 0.04, "whisper-large-v3": 0.111}
MIN_BILLED_SEC = 10  # запрос короче 10 секунд оплачивается как 10
RETRIES = 5
TIMEOUT = httpx.Timeout(30.0, read=300.0, write=300.0)
RETRY_AFTER_DEFAULT = 20.0
RETRY_AFTER_MAX = 3600.0
# На бесплатном тарифе есть лимит секунд звука в час: длинную запись приходится ждать, а не бросать
LIMIT_WAIT_MAX = 3 * 3600.0


class TranscribeError(RuntimeError):
    pass


def cost(chunk_seconds: Sequence[float], model: str = MODEL) -> float:
    """Во сколько обойдётся расшифровка кусков звука в долларах."""
    billed = sum(max(seconds, MIN_BILLED_SEC) for seconds in chunk_seconds)
    return billed / 3600 * PRICE_PER_HOUR.get(model, PRICE_PER_HOUR[MODEL])


def _retry_after(resp: httpx.Response) -> float:
    try:
        seconds = float(resp.headers.get("retry-after", ""))
    except ValueError:
        seconds = RETRY_AFTER_DEFAULT
    return min(max(seconds, 1.0), RETRY_AFTER_MAX)


def _problem(resp: httpx.Response) -> str:
    try:
        message = (resp.json().get("error") or {}).get("message")
    except ValueError:
        message = None
    return f"HTTP {resp.status_code}: {message or resp.text[:200]}"


async def transcribe_file(
    http: httpx.AsyncClient,
    api_key: str,
    path: Path,
    *,
    model: str = MODEL,
    language: str = "ru",
    prompt: str | None = None,
) -> dict:
    """Расшифровка одного файла: ответ Groq в формате verbose_json (фразы со временем и оценками уверенности).

    Лимит запросов (429) ждётся по заголовку retry-after, в сумме не больше LIMIT_WAIT_MAX; сбои сети
    и сервера повторяются RETRIES раз.
    """
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
    problem = ""
    attempt = 0
    limit_waited = 0.0
    while attempt < RETRIES:
        wait = 2.0**attempt
        try:
            resp = await http.post(
                URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (path.name, audio, "application/octet-stream")},
                data=form,
                timeout=TIMEOUT,
            )
        except httpx.TransportError as exc:
            problem = str(exc) or type(exc).__name__
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise TranscribeError("Groq вернул не JSON") from exc
            problem = _problem(resp)
            if resp.status_code in (401, 403):
                raise TranscribeError(f"Groq не принял ключ ({problem})")
            if resp.status_code in (400, 404, 413, 422):
                raise TranscribeError(f"Groq отклонил файл {path.name}: {problem}")
            if resp.status_code == 429:
                wait = _retry_after(resp)
                if limit_waited + wait > LIMIT_WAIT_MAX:
                    break
                limit_waited += wait
                log.info("Groq: лимит (%s), жду %.0f с", problem, wait)
                await asyncio.sleep(wait)
                continue  # ожидание лимита не считается неудачной попыткой
        attempt += 1
        if attempt < RETRIES:
            log.info("Groq: %s, повтор через %.0f с", problem, wait)
            await asyncio.sleep(wait)
    raise TranscribeError(f"Groq не ответил на {path.name}: {problem}")
