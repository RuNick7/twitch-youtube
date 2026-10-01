"""Сегмент VOD Twitch как поток байтов известного размера, без записи на диск."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from typing import AsyncIterator, Dict, List, Optional, Tuple

import httpx

from .fmp4 import (
    Part,
    base_time,
    first_times,
    fragment_end_times,
    parse_playlist,
    rebase,
    select_parts,
    shifts,
    timescales_from_init,
)

HEAD_CONCURRENCY = 16
PREFETCH = 2  # фрагментов наперёд: пока отправляется один кусок, следующие уже качаются
RETRIES = 5
TIMEOUT = httpx.Timeout(20.0, read=60.0)


class SourceError(RuntimeError):
    """VOD на Twitch недоступен, не дописан или повреждён."""


@dataclass
class Plan:
    parts: List[Part]
    init: bytes = b""
    # Сдвиг времени для каждого отрезка (Part.group); None — MPEG-TS, время не сдвигается
    shifts: Optional[List[Dict[int, int]]] = field(default=None)

    @property
    def total(self) -> int:
        return len(self.init) + sum(part.size for part in self.parts)

    @property
    def duration(self) -> float:
        return sum(part.duration for part in self.parts)


def _name(url: str) -> str:
    return url.rsplit("/", 1)[-1]


async def _request(http: httpx.AsyncClient, url: str, method: str = "GET") -> httpx.Response:
    problem = ""
    for attempt in range(RETRIES):
        try:
            resp = await http.request(method, url, timeout=TIMEOUT)
        except httpx.TransportError as exc:
            problem = str(exc) or type(exc).__name__
        else:
            if resp.status_code == 200:
                return resp
            if resp.status_code in (403, 404, 410):
                raise SourceError(f"Twitch ответил {resp.status_code} на {_name(url)}: VOD удалён или недоступен")
            problem = f"HTTP {resp.status_code}"
        if attempt + 1 < RETRIES:
            await asyncio.sleep(2**attempt)
    raise SourceError(f"не удалось получить {_name(url)} с Twitch: {problem}")


async def build_plan(http: httpx.AsyncClient, playlist_url: str, ranges: List[Tuple[float, float]]) -> Plan:
    """Фрагменты ролика и их размеры; для fMP4 ещё init и сдвиги времени.

    Ролик может быть склеен из нескольких отрезков VOD: время первого сдвигается к
    нулю, каждого следующего — точно к концу предыдущего.
    """
    playlist = parse_playlist((await _request(http, playlist_url)).text, playlist_url)
    if not playlist.ended:
        raise SourceError("VOD ещё дописывается: стрим не закончился")
    groups = [group for group in (select_parts(playlist.parts, start, end) for start, end in ranges) if group]
    if not groups:
        raise SourceError("в VOD нет фрагментов для этого отрезка")
    parts: List[Part] = []
    for index, group in enumerate(groups):
        for part in group:
            part.group = index
            parts.append(part)

    limit = asyncio.Semaphore(HEAD_CONCURRENCY)

    async def measure(part: Part) -> None:
        async with limit:
            length = (await _request(http, part.url, "HEAD")).headers.get("content-length")
        if not length:
            raise SourceError(f"Twitch не сообщил размер {_name(part.url)}")
        part.size = int(length)

    results = await asyncio.gather(*(measure(part) for part in parts), return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise errors[0]

    plan = Plan(parts)
    if playlist.init_url:
        plan.init = (await _request(http, playlist.init_url)).content
        try:
            timescales = timescales_from_init(plan.init)
            plan.shifts = []
            offset = Fraction(0)  # где в ролике начинается очередной отрезок, секунды
            for index, group in enumerate(groups):
                base = base_time(first_times((await _request(http, group[0].url)).content), timescales)
                plan.shifts.append(shifts(base, timescales, offset))
                if index + 1 < len(groups):
                    ends = fragment_end_times((await _request(http, group[-1].url)).content)
                    offset += max(Fraction(value, timescales[track]) for track, value in ends.items()) - base
        except (ValueError, KeyError) as exc:
            raise SourceError(f"не удалось разобрать MP4 из VOD: {exc}") from exc
    return plan


async def _download(http: httpx.AsyncClient, url: str) -> bytearray:
    """Фрагмент сразу в bytearray: без промежуточной копии в памяти."""
    problem = ""
    for attempt in range(RETRIES):
        data = bytearray()
        try:
            async with http.stream("GET", url, timeout=TIMEOUT) as resp:
                if resp.status_code == 200:
                    async for piece in resp.aiter_bytes(1 << 20):
                        data += piece
                    return data
                if resp.status_code in (403, 404, 410):
                    raise SourceError(f"Twitch ответил {resp.status_code} на {_name(url)}: VOD удалён или недоступен")
                problem = f"HTTP {resp.status_code}"
        except httpx.TransportError as exc:
            problem = str(exc) or type(exc).__name__
        if attempt + 1 < RETRIES:
            await asyncio.sleep(2**attempt)
    raise SourceError(f"не удалось получить {_name(url)} с Twitch: {problem}")


async def _fetch(http: httpx.AsyncClient, plan: Plan, part: Part) -> bytearray:
    data = await _download(http, part.url)
    if len(data) != part.size:
        raise SourceError(f"{_name(part.url)}: {len(data)} байт вместо {part.size}, VOD изменился")
    if plan.shifts is not None:
        try:
            rebase(data, plan.shifts[part.group])
        except ValueError as exc:
            raise SourceError(f"{_name(part.url)} повреждён: {exc}") from exc
    return data


def _forget(task: asyncio.Task) -> None:
    """Отменённая предзагрузка не должна ругаться «exception was never retrieved»."""
    task.cancel()
    task.add_done_callback(lambda t: t.cancelled() or t.exception())


async def stream(http: httpx.AsyncClient, plan: Plan, offset: int = 0) -> AsyncIterator[bytes | bytearray]:
    """Байты ролика начиная с offset; при каждом вызове одни и те же.

    Куски отдаются без копирования: получатель не должен их менять.
    """
    if offset < len(plan.init):
        yield plan.init[offset:]
    todo: List[Tuple[Part, int]] = []
    position = len(plan.init)
    for part in plan.parts:
        if position + part.size > offset:
            todo.append((part, max(0, offset - position)))
        position += part.size

    pending: deque = deque()
    queue = iter(todo)

    def refill() -> None:
        while len(pending) <= PREFETCH:
            item = next(queue, None)
            if item is None:
                return
            part, skip = item
            pending.append((asyncio.create_task(_fetch(http, plan, part)), skip))

    try:
        refill()
        while pending:
            task, skip = pending.popleft()
            data = await task
            refill()
            yield data[skip:] if skip else data
    finally:
        for task, _ in pending:
            _forget(task)
