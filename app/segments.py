"""Нарезка VOD на сегменты и метаданные для YouTube.

Модуль не зависит от сторонних библиотек, чтобы логику можно было проверять
тестами без установки зависимостей.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

# YouTube принимает ролики до 12 часов; делим всё, что длиннее 11 ч 50 мин
SPLIT_LIMIT_SEC = 12 * 3600 - 10 * 60
TITLE_MAX_CHARS = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500
FALLBACK_CATEGORY = "Стрим"


@dataclass
class Chapter:
    start: float
    end: float
    title: str


@dataclass
class PlannedSegment:
    start: int
    end: int
    category: str
    part: Optional[int] = None  # номер части, если категория встречается несколько раз

    @property
    def duration(self) -> int:
        return self.end - self.start


def normalize_chapters(
    raw: Optional[Iterable[dict]], duration: float, fallback_title: str = FALLBACK_CATEGORY
) -> List[Chapter]:
    """Превращает главы yt-dlp в непрерывный список от 0 до конца VOD.

    Границами служат начала глав: у Twitch каждая глава длится до начала
    следующей, а так заодно закрываются дыры и перекрытия.
    """
    duration = float(duration)
    items = [item for item in (raw or []) if isinstance(item, dict)]
    chapters: List[Chapter] = []
    for i, item in enumerate(items):
        title = str(item.get("title") or "").strip() or fallback_title
        start = item.get("start_time")
        if start is None:
            start = chapters[-1].end if chapters else 0.0
        end = item.get("end_time")
        if end is None:
            following = items[i + 1].get("start_time") if i + 1 < len(items) else None
            end = following if following is not None else duration
        start = min(max(float(start), 0.0), duration)
        chapters.append(Chapter(start, min(max(float(end), start), duration), title))

    if not chapters:
        return [Chapter(0.0, duration, fallback_title)] if duration > 0 else []

    chapters.sort(key=lambda chapter: chapter.start)
    chapters[0].start = 0.0
    for prev, cur in zip(chapters, chapters[1:]):
        prev.end = cur.start
    chapters[-1].end = duration
    return [chapter for chapter in chapters if chapter.end > chapter.start]


def plan_segments(
    chapters: List[Chapter], min_sec: int = 120, split_limit: int = SPLIT_LIMIT_SEC
) -> List[PlannedSegment]:
    """Каждая смена категории — отдельный сегмент.

    Куски короче min_sec (случайное переключение категории) приклеиваются к
    предыдущему сегменту, а в начале стрима — к следующему. Сегменты длиннее
    split_limit делятся на равные части. Если категория встречается несколько
    раз, сегменты нумеруются: «часть 1», «часть 2».
    """
    segments = _merge_same(
        [
            PlannedSegment(int(round(chapter.start)), int(round(chapter.end)), chapter.title)
            for chapter in chapters
            if round(chapter.end) > round(chapter.start)
        ]
    )
    while len(segments) > 1:
        short = next((i for i, seg in enumerate(segments) if seg.duration < min_sec), None)
        if short is None:
            break
        if short > 0:
            segments[short - 1].end = segments[short].end
        else:
            segments[1].start = segments[0].start
        del segments[short]
        segments = _merge_same(segments)

    result: List[PlannedSegment] = []
    for seg in segments:
        count = math.ceil(seg.duration / split_limit) if seg.duration > split_limit else 1
        bounds = [seg.start + round(seg.duration * k / count) for k in range(count)] + [seg.end]
        result.extend(PlannedSegment(a, b, seg.category) for a, b in zip(bounds, bounds[1:]))

    totals: dict = {}
    for seg in result:
        totals[seg.category] = totals.get(seg.category, 0) + 1
    seen: dict = {}
    for seg in result:
        if totals[seg.category] > 1:
            seen[seg.category] = seen.get(seg.category, 0) + 1
            seg.part = seen[seg.category]
    return result


def _merge_same(segments: List[PlannedSegment]) -> List[PlannedSegment]:
    merged: List[PlannedSegment] = []
    for seg in segments:
        if merged and merged[-1].category == seg.category:
            merged[-1].end = seg.end
        else:
            merged.append(PlannedSegment(seg.start, seg.end, seg.category))
    return merged


# --- что загружать ---


def split_list(value: Optional[str]) -> List[str]:
    """«a, b,,c» → ["a", "b", "c"]: списки в .env пишутся через запятую."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _fold(text: Optional[str]) -> str:
    return (text or "").casefold().replace("ё", "е")


def skip_reason(
    category: str,
    stream_title: str,
    *,
    keywords: Iterable[str],
    title_categories: Iterable[str],
    categories: Iterable[str],
) -> Optional[str]:
    """Почему сегмент не загружается, или None.

    Просмотр сериалов и фильмов Twitch отдельной категорией не отмечает: он
    идёт под Just Chatting, а узнать его можно по названию стрима.
    """
    folded = _fold(category)
    if folded in {_fold(item) for item in categories}:
        return f"категория «{category}» не загружается"
    if folded in {_fold(item) for item in title_categories}:
        title = _fold(stream_title)
        for keyword in keywords:
            if _fold(keyword) and _fold(keyword) in title:
                return f"похоже на просмотр сериала или фильма: в названии стрима «{keyword}»"
    return None


def category_warnings(category: str, warn_categories: Iterable[str]) -> List[str]:
    """Предупреждения, известные ещё до загрузки."""
    if _fold(category) in {_fold(item) for item in warn_categories}:
        return [f"категория «{category}» в списке рискованных"]
    return []


# --- метаданные для YouTube ---

_ANGLE_BRACKETS = re.compile(r"[<>]")
_SPACES = re.compile(r"\s+")
_LOGIN = re.compile(r"[^A-Za-z0-9_]")


def clean_line(text: Optional[str]) -> str:
    """Одна строка без угловых скобок (YouTube их не принимает) и лишних пробелов."""
    return _SPACES.sub(" ", _ANGLE_BRACKETS.sub("", text or "")).strip()


def build_title(
    category: str, stream_title: str, streamer: str, part: Optional[int] = None, limit: int = TITLE_MAX_CHARS
) -> str:
    """«{игра} — {название стрима} | {стример}», не длиннее limit символов."""
    head = clean_line(category) or FALLBACK_CATEGORY
    if part:
        head += f" (часть {part})"
    body = clean_line(stream_title)
    name = clean_line(streamer)
    tail = f" | {name}" if name else ""
    title = f"{head} — {body}{tail}" if body else f"{head}{tail}"
    if len(title) <= limit:
        return title
    room = limit - len(f"{head} — …{tail}")
    if body and room >= 10:
        return f"{head} — {body[:room].rstrip()}…{tail}"
    title = f"{head} — {body}" if body else head
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def build_description(
    stream_title: str,
    streamer: str,
    streamer_login: str,
    stream_date: str,
    category: str,
    start: str,
    end: str,
) -> str:
    """Описание с указанием автора: этого требует разрешение стримера и правила YouTube."""
    lines = []
    title = clean_line(stream_title)[:1000]
    if title:
        lines += [title, ""]
    when = f" от {stream_date}" if stream_date else ""
    lines += [
        f"Фрагмент стрима {clean_line(streamer)}{when}: {clean_line(category)}, {start}–{end}.",
        f"Twitch: https://www.twitch.tv/{_LOGIN.sub('', streamer_login)}",
        "",
        "Опубликовано с разрешения автора.",
    ]
    text = "\n".join(lines)
    data = text.encode("utf-8")
    if len(data) > DESCRIPTION_MAX_BYTES:
        text = data[:DESCRIPTION_MAX_BYTES].decode("utf-8", "ignore")
    return text


def build_tags(*candidates: Optional[str], limit: int = TAGS_MAX_CHARS) -> List[str]:
    """Теги без повторов в пределах лимита YouTube.

    YouTube считает запятые между тегами и кавычки вокруг тегов с пробелами.
    """
    tags: List[str] = []
    used = 0
    for candidate in candidates:
        tag = clean_line((candidate or "").replace(",", " "))
        if not tag or tag.lower() in (existing.lower() for existing in tags):
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if tags else 0)
        if used + cost > limit:
            break
        tags.append(tag)
        used += cost
    return tags


# --- форматирование ---


def fmt_hms(seconds: float) -> str:
    """5025 → «1:23:45», 59 → «0:59»."""
    hours, rest = divmod(int(round(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def fmt_duration(seconds: float) -> str:
    """6360 → «1 ч 46 мин», 720 → «12 мин», 45 → «45 с»."""
    hours, rest = divmod(int(round(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    if minutes:
        return f"{minutes} мин"
    return f"{secs} с"


def twitch_time_param(seconds: float) -> str:
    """Параметр ?t= для ссылки на VOD: 5025 → «1h23m45s»."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes}m{secs}s"


_VOD_URL = re.compile(r"twitch\.tv/(?:[\w-]+/)?videos?/(\d+)")
_VOD_ID = re.compile(r"v?(\d{5,})")


def parse_vod_id(text: Optional[str]) -> Optional[str]:
    """ID VOD из ссылки вида twitch.tv/videos/123 или из самого номера."""
    text = (text or "").strip()
    match = _VOD_URL.search(text)
    if match:
        return match.group(1)
    match = _VOD_ID.fullmatch(text)
    return match.group(1) if match else None
