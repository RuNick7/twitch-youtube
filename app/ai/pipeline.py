"""Разбор одного ролика по расшифровке: моменты, главы, темы для названия и чужие видео внутри него.

Общий для проверочной команды (python -m app.ai analyze) и для бота (app/analyst.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from . import chapters, claude, timeline
from .timeline import Ask, Context, Moment
from .transcript import Phrase

VIDEO_MIN_SEC = 60  # короче — мимолётный клип или ошибка разметки: о таком не сообщаем


@dataclass(frozen=True)
class Analysis:
    moments: list[Moment]
    chapters: list[chapters.VideoChapter]
    topics: list  # темы для названия, как их вернула модель
    transcribed: float  # сколько секунд записи покрыто расшифровкой


async def analyze_segment(
    ask: Ask,
    context: Context,
    phrases: Sequence[Phrase],
    *,
    category: str,
    spans: Sequence[tuple[float, float]],
    window: int = 60,
    effort: str = "low",
) -> tuple[Analysis, claude.Usage]:
    """phrases — расшифровка записи (лишнее вне отрезков spans не используется).

    У склеенного ролика отрезки разнесены по записи: каждый размечается отдельно, иначе момент растянулся бы
    через то, что стояло между ними.
    """
    usage = claude.Usage()
    moments: list[Moment] = []
    transcribed = 0.0
    for start, end in spans:
        piece = [phrase for phrase in phrases if start <= phrase.start < end]
        if not piece:
            continue
        found, used = await timeline.analyze(piece, context, ask, minutes=window, effort=effort)
        usage.add(used)
        moments += found
        transcribed += piece[-1].end - piece[0].start
    parts, topics, used = await chapters.suggest(ask, category=category, moments=moments, spans=spans, effort=effort)
    usage.add(used)
    return Analysis(moments, parts, topics, transcribed), usage


def videos(moments: Sequence[Moment], min_sec: float = VIDEO_MIN_SEC) -> list[tuple[float, float]]:
    """Отрезки записи, где стример смотрит чужое видео: соседние моменты слиты, мимолётные отброшены."""
    result: list[tuple[float, float]] = []
    for moment in moments:
        if moment.kind != "video":
            continue
        if result and moment.start - result[-1][1] <= 1:
            result[-1] = (result[-1][0], moment.end)
        else:
            result.append((moment.start, moment.end))
    return [(start, end) for start, end in result if end - start >= min_sec]
