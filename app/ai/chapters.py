"""Главы для описания YouTube и главные темы для названия: Haiku собирает их из моментов разметки одним запросом.

Время модель не придумывает: она выбирает номер момента, с которого начинается глава.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Sequence

from ..segments import clean_line
from . import claude, transcript
from .timeline import Ask, Moment
from .titles import cut_words, has_profanity

log = logging.getLogger(__name__)

TITLE_MAX_CHARS = 45
MIN_CHAPTER_SEC = 60  # короче — приклеивается к соседней главе; тема на минуту с лишним остаётся отдельной
LONG_CHAPTER_SEC = 20 * 60  # если вышла такая длинная глава, запрос повторяется: похоже на неполный ответ
ATTEMPTS = 2
MIN_CHAPTERS = 3  # меньше трёх YouTube главами не считает
MAX_BYTES = 3500  # всё описание ролика — до 5000 байт, часть из них занимают сведения о стриме
MAX_TOKENS = 16000

SYSTEM = """Ты готовишь описание и название YouTube-ролика с записью стрима по списку моментов. У каждого момента: номер, когда он начинается в ролике, сколько длится, вид, уверенность разметки и тема. Верни главы для описания (chapters) и главные темы для названия (topics).

Главы:
- глава начинается с одного из моментов: укажи его номер; первая глава — с момента 0;
- новая глава начинается, когда меняется тема разговора: разные темы в одну главу не объединяй; соседние моменты об одном и том же объединяй; обычная глава идёт от 2 до 8 минут;
- название главы — по-русски, 2–6 слов, не длиннее 45 символов, без точки; пиши суть («Планы на новый сезон», а не «Разговор с чатом»), без слова «стример» и без повтора названия игры в каждой главе;
- моменты без расшифровки не выделяй в отдельные главы: присоедини к соседним;
- без мата и оскорблений, без кликбейта; только то, что есть в темах.

Темы для названия:
- ровно 5 самых долгих или заметных тем всего ролика, самые интересные зрителю первыми: истории, события и споры важнее рутины;
- каждая тема — 1–2 слова и не длиннее 18 символов, как их ищут зрители («ремонт», «осада замка», «новый телефон»);
- конкретно: предметы, места, события; общие слова («разговоры», «игра», «чат», «крафт», «квесты») не годятся;
- редкое название из игры, в котором не уверен (оно могло распознаться с ошибкой), заменяй простым словом («босс», «ведьма»);
- темы разные, без категории и имени стримера, без мата."""

SCHEMA = {
    "type": "object",
    "properties": {
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"moment": {"type": "integer"}, "title": {"type": "string"}},
                "required": ["moment", "title"],
                "additionalProperties": False,
            },
        },
        "topics": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["chapters", "topics"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class VideoChapter:
    at: float  # секунда ролика, с которой начинается глава
    source: float  # та же точка на записи Twitch
    title: str


def video_position(spans: Sequence[tuple[float, float]], at: float) -> float:
    """Секунда ролика, склеенного из отрезков spans, для секунды at записи."""
    passed = 0.0
    for start, end in spans:
        if at < end:
            return passed + max(at - start, 0.0)
        passed += end - start
    return passed


def clock(seconds: float) -> str:
    """Время так, как YouTube понимает его в описании: 05:11 или 1:02:30."""
    whole = int(seconds)
    hours, minutes, rest = whole // 3600, whole % 3600 // 60, whole % 60
    return f"{hours}:{minutes:02d}:{rest:02d}" if hours else f"{minutes:02d}:{rest:02d}"


def render(chapters: Sequence[VideoChapter]) -> str:
    """Строки для описания ролика."""
    return "\n".join(f"{clock(chapter.at)} {chapter.title}" for chapter in chapters)


def build_user_prompt(category: str, moments: Sequence[Moment], spans: Sequence[tuple[float, float]]) -> str:
    lines = [
        f"{number}. с {transcript.fmt_time(video_position(spans, m.start))}, {m.duration / 60:.1f} мин, "
        f"{m.kind}, {m.confidence}: {m.topic}"
        for number, m in enumerate(moments)
    ]
    length = transcript.fmt_time(sum(end - start for start, end in spans))
    return f"Категория Twitch: {category}\nДлина ролика: {length}\n\nМоменты:\n" + "\n".join(lines)


def parse_chapters(data: dict, moments: Sequence[Moment], spans: Sequence[tuple[float, float]]) -> list[VideoChapter]:
    """Главы из ответа модели по порядку; первая всегда с начала ролика. Мусор и мат отбрасываются."""
    picked: dict[int, str] = {}
    for item in data.get("chapters") or []:
        if not isinstance(item, dict):
            continue
        number = item.get("moment")
        if type(number) is not int or not 0 <= number < len(moments):
            continue
        title = clean_line(str(item.get("title") or "")).strip(" .,;:-—")
        if title and not has_profanity(title):
            picked.setdefault(number, cut_words(title, TITLE_MAX_CHARS))
    result: list[VideoChapter] = []
    for number in sorted(picked):
        source = moments[number].start if result else spans[0][0]
        at = video_position(spans, source) if result else 0.0
        if not result or at > result[-1].at:
            result.append(VideoChapter(at, source, picked[number]))
    return result


def lengths(chapters: Sequence[VideoChapter], duration: float) -> list[float]:
    ends = [chapter.at for chapter in chapters[1:]] + [duration]
    return [end - chapter.at for chapter, end in zip(chapters, ends)]


def fit(
    chapters: Sequence[VideoChapter], duration: float, min_sec: float = MIN_CHAPTER_SEC, max_bytes: int = MAX_BYTES
) -> list[VideoChapter]:
    """Убирает слишком короткие главы и укладывает список в max_bytes.

    Самая короткая глава присоединяется к более короткой из соседних и получает её название (соседняя длиннее).
    Пустой список — глав меньше трёх, показывать их незачем.
    """
    result = list(chapters)
    while len(result) > 1:
        sizes = lengths(result, duration)
        shortest = min(range(len(result)), key=sizes.__getitem__)
        if sizes[shortest] >= min_sec and len(render(result).encode()) <= max_bytes:
            break
        neighbor = min((i for i in (shortest - 1, shortest + 1) if 0 <= i < len(result)), key=sizes.__getitem__)
        first, second = sorted((shortest, neighbor))
        result[first] = replace(result[first], title=result[neighbor].title)
        del result[second]
    return result if len(result) >= MIN_CHAPTERS else []


async def suggest(
    ask: Ask, *, category: str, moments: Sequence[Moment], spans: Sequence[tuple[float, float]], effort: str = "low"
) -> tuple[list[VideoChapter], list, claude.Usage]:
    """Главы ролика, склеенного из отрезков spans записи, темы для его названия (как их вернула модель) и расход."""
    usage = claude.Usage()
    if not moments:
        return [], [], usage
    duration = sum(end - start for start, end in spans)
    best: list[VideoChapter] = []
    topics: list = []
    best_longest = float("inf")
    for attempt in range(1, ATTEMPTS + 1):
        data, used = await ask(
            system=SYSTEM,
            user=build_user_prompt(category, moments, spans),
            schema=SCHEMA,
            effort=effort,
            max_tokens=MAX_TOKENS,
        )
        usage.add(used)
        found = parse_chapters(data, moments, spans)
        longest = max(lengths(found, duration), default=float("inf"))
        if attempt == 1 or longest < best_longest:
            best, best_longest, topics = found, longest, list(data.get("topics") or [])
        # Меньше MIN_CHAPTERS моментов — глав всё равно не будет, и повторять запрос незачем
        if best_longest <= LONG_CHAPTER_SEC or len(moments) < MIN_CHAPTERS:
            break
        log.warning("Главы: самая длинная %.0f мин (попытка %d из %d)", longest / 60, attempt, ATTEMPTS)
    return fit(best, duration), topics, usage
