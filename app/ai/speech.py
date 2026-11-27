"""Общие правила запросов к сервисам распознавания речи: повторы, ожидание лимита и понятные ошибки."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

RETRIES = 5
TIMEOUT = httpx.Timeout(30.0, read=300.0, write=300.0)
RETRY_AFTER_DEFAULT = 20.0
RETRY_AFTER_MAX = 3600.0
# На бесплатном тарифе Groq есть лимит секунд звука в час: длинную запись приходится ждать, а не бросать
LIMIT_WAIT_MAX = 3 * 3600.0
ACCOUNT_STATUSES = (401, 402, 403)  # ключ или баланс: следующие куски этим сервисом тоже не пройдут
REJECTED_STATUSES = (400, 404, 413, 422)  # повторять тот же запрос бесполезно


class TranscribeError(RuntimeError):
    def __init__(self, message: str, *, account: bool = False):
        super().__init__(message)
        self.account = account


def _retry_after(resp: httpx.Response) -> float:
    try:
        seconds = float(resp.headers.get("retry-after", ""))
    except ValueError:
        seconds = RETRY_AFTER_DEFAULT
    return min(max(seconds, 1.0), RETRY_AFTER_MAX)


def _problem(resp: httpx.Response) -> str:
    try:
        message = (resp.json().get("error") or {}).get("message")
    except (ValueError, AttributeError):
        message = None
    return f"HTTP {resp.status_code}: {message or resp.text[:200]}"


async def post(
    send: Callable[[], Awaitable[httpx.Response]], *, service: str, filename: str, limit_wait: float = LIMIT_WAIT_MAX
) -> dict:
    """Выполняет запрос расшифровки и возвращает ответ сервиса (JSON).

    Лимит запросов (429) ждётся по заголовку retry-after, в сумме не больше limit_wait; сбои сети и сервера
    повторяются RETRIES раз.
    """
    problem = ""
    attempt = 0
    waited = 0.0
    while attempt < RETRIES:
        wait = 2.0**attempt
        try:
            resp = await send()
        except httpx.TransportError as exc:
            problem = str(exc) or type(exc).__name__
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise TranscribeError(f"{service} вернул не JSON") from exc
            problem = _problem(resp)
            if resp.status_code in ACCOUNT_STATUSES:
                raise TranscribeError(f"{service} не принял ключ или на балансе нет денег ({problem})", account=True)
            if resp.status_code in REJECTED_STATUSES:
                raise TranscribeError(f"{service} отклонил файл {filename}: {problem}")
            if resp.status_code == 429:
                wait = _retry_after(resp)
                if waited + wait > limit_wait:
                    break
                waited += wait
                log.info("%s: лимит (%s), жду %.0f с", service, problem, wait)
                await asyncio.sleep(wait)
                continue  # ожидание лимита не считается неудачной попыткой
        attempt += 1
        if attempt < RETRIES:
            log.info("%s: %s, повтор через %.0f с", service, problem, wait)
            await asyncio.sleep(wait)
    raise TranscribeError(f"{service} не ответил на {filename}: {problem}")
