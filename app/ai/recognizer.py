"""Выбор сервиса распознавания: сначала OpenRouter, а если он не смог — Groq."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import httpx

from . import groq, openrouter
from .speech import LIMIT_WAIT_MAX, TranscribeError

log = logging.getLogger(__name__)

SERVICES = {"openrouter": "OPENROUTER_API_KEY", "groq": "GROQ_API_KEY"}  # по порядку предпочтения
SPARE_LIMIT_WAIT = 600.0  # пока есть запасной сервис, лимита не ждём дольше десяти минут


@dataclass(frozen=True)
class Result:
    data: dict  # ответ сервиса: segments со временем и оценками уверенности
    service: str
    cost_usd: float  # у Groq — по платному тарифу, на бесплатном ничего не списывается


class Recognizer:
    def __init__(self, keys: Mapping[str, str]):
        """keys — ключи сервисов по их названиям; сервисы без ключа не используются."""
        self.keys = {name: keys[name] for name in SERVICES if keys.get(name)}
        self.broken: dict[str, str] = {}  # сервис -> почему больше не пробуем

    @classmethod
    def from_env(cls, environ: Mapping[str, str], only: str | None = None) -> "Recognizer":
        names = [only] if only else list(SERVICES)
        return cls({name: environ.get(SERVICES[name], "").strip() for name in names})

    async def transcribe(self, http: httpx.AsyncClient, path: Path, seconds: float, *, language: str = "ru") -> Result:
        names = [name for name in self.keys if name not in self.broken]
        problems = list(self.broken.values())
        for number, name in enumerate(names, 1):
            limit_wait = LIMIT_WAIT_MAX if number == len(names) else SPARE_LIMIT_WAIT
            module = openrouter if name == "openrouter" else groq
            try:
                data = await module.transcribe_file(
                    http, self.keys[name], path, language=language, limit_wait=limit_wait
                )
            except TranscribeError as exc:
                problems.append(str(exc))
                if exc.account:
                    self.broken[name] = str(exc)  # без ключа или денег следующие куски тоже не пройдут
                log.warning("Распознавание: %s", exc)
                continue
            spent = openrouter.cost(data, seconds) if name == "openrouter" else groq.cost([seconds])
            return Result(data, name, spent)
        raise TranscribeError("; ".join(problems) or "нет ни одного ключа распознавания")
