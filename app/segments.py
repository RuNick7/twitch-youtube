"""Нарезка VOD на сегменты и метаданные для YouTube.

Модуль не зависит от сторонних библиотек, чтобы логику можно было проверять
тестами без установки зависимостей.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

# YouTube принимает ролики до 12 часов; делим всё, что длиннее 11 ч 50 мин
SPLIT_LIMIT_SEC = 12 * 3600 - 10 * 60
TITLE_MAX_CHARS = 100
PLAYLIST_TITLE_MAX_CHARS = 150
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500
FALLBACK_CATEGORY = "Стрим"


@dataclass
class Chapter:
    start: float
    end: float
    title: str  # категория: так глава называется у Twitch
    stream_title: Optional[str] = None  # название стрима на этом отрезке, если известно


@dataclass
class PlannedSegment:
    start: int  # начало первого отрезка
    end: int  # конец последнего отрезка
    category: str
    stream_title: Optional[str] = None
    ranges: List[Tuple[int, int]] = field(default_factory=list)  # пусто — один отрезок [start, end)

    @property
    def spans(self) -> List[Tuple[int, int]]:
        """Отрезки VOD, из которых склеен ролик."""
        return self.ranges or [(self.start, self.end)]

    @property
    def duration(self) -> int:
        return sum(end - start for start, end in self.spans)

    @property
    def key(self) -> Tuple[str, Optional[str]]:
        return self.category, self.stream_title


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


def split_by_titles(chapters: List[Chapter], marks: Iterable[Tuple[float, str]]) -> List[Chapter]:
    """Режет главы ещё и по сменам названия стрима.

    marks — пары (секунда от начала VOD, название), замеченные во время эфира.
    Первое название действует с начала VOD, даже если бот увидел его позже.
    """
    points = sorted((float(at), str(title).strip()) for at, title in marks if str(title or "").strip())
    if not points:
        return chapters
    changes = [(0.0, points[0][1])]
    for at, title in points[1:]:
        if title != changes[-1][1]:
            changes.append((max(at, 0.0), title))
    result: List[Chapter] = []
    for chapter in chapters:
        cuts = [chapter.start] + [at for at, _ in changes if chapter.start < at < chapter.end] + [chapter.end]
        for a, b in zip(cuts, cuts[1:]):
            title = next(title for at, title in reversed(changes) if at <= a)
            result.append(Chapter(a, b, chapter.title, title))
    return result


def plan_segments(
    chapters: List[Chapter],
    min_sec: int = 120,
    split_limit: int = SPLIT_LIMIT_SEC,
    join: bool = False,
) -> List[PlannedSegment]:
    """Каждая смена категории или названия стрима — отдельный сегмент.

    Куски короче min_sec (случайное переключение категории, исправленная
    опечатка в названии) приклеиваются к предыдущему сегменту, а в начале
    стрима — к следующему. С join повторы с той же категорией и тем же
    названием склеиваются в один ролик из нескольких отрезков. Сегменты
    длиннее split_limit делятся на равные части. Номера частей — number_parts.
    """
    segments = _merge_same(
        [
            PlannedSegment(
                int(round(chapter.start)), int(round(chapter.end)), chapter.title, stream_title=chapter.stream_title
            )
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

    if join:
        segments = _join_repeated(segments)

    result: List[PlannedSegment] = []
    for seg in segments:
        count = math.ceil(seg.duration / split_limit) if seg.duration > split_limit else 1
        cuts = [round(seg.duration * k / count) for k in range(1, count)]
        for spans in _cut_spans(seg.spans, cuts):
            result.append(
                PlannedSegment(
                    spans[0][0],
                    spans[-1][1],
                    seg.category,
                    stream_title=seg.stream_title,
                    ranges=spans if len(spans) > 1 else [],
                )
            )
    return result


def category_key(category: Optional[str]) -> str:
    """Категория без учёта регистра и «ё»: по ней считаются части и выбирается плейлист."""
    return _fold(clean_line(category) or FALLBACK_CATEGORY)


def number_parts(
    segments: List[PlannedSegment],
    uploaded: Optional[List[bool]] = None,
    no_part: Iterable[str] = (),
    counts: Optional[dict] = None,
) -> List[Optional[int]]:
    """Номер части каждого сегмента: «Часть 7».

    Нумерация сквозная по категории, от стрима к стриму: counts — сколько роликов
    каждой категории (ключ — category_key) загружалось раньше. Пропущенные сегменты
    номера не получают и счёт не двигают. Категории из no_part не нумеруются, кроме
    отрезка длиннее 12 часов: его куски с одинаковым названием получают «Часть 1», «Часть 2».
    """
    flags = uploaded if uploaded is not None else [True] * len(segments)
    unnumbered = {category_key(category) for category in no_part}
    same: dict = {}
    for seg, flag in zip(segments, flags):
        if flag and category_key(seg.category) in unnumbered:
            same[seg.key] = same.get(seg.key, 0) + 1
    last = dict(counts or {})
    seen: dict = {}
    result: List[Optional[int]] = []
    for seg, flag in zip(segments, flags):
        key = category_key(seg.category)
        if not flag:
            result.append(None)
        elif key in unnumbered:
            if same[seg.key] > 1:
                seen[seg.key] = seen.get(seg.key, 0) + 1
                result.append(seen[seg.key])
            else:
                result.append(None)
        else:
            last[key] = last.get(key, 0) + 1
            result.append(last[key])
    return result


def _merge_same(segments: List[PlannedSegment]) -> List[PlannedSegment]:
    merged: List[PlannedSegment] = []
    for seg in segments:
        if merged and merged[-1].key == seg.key:
            merged[-1].end = seg.end
        else:
            merged.append(PlannedSegment(seg.start, seg.end, seg.category, stream_title=seg.stream_title))
    return merged


def _join_repeated(segments: List[PlannedSegment]) -> List[PlannedSegment]:
    """Сегменты с той же категорией и тем же названием — в один ролик; порядок по первому появлению."""
    joined: dict = {}
    for seg in segments:
        if seg.key in joined:
            target = joined[seg.key]
            target.ranges = target.spans + [(seg.start, seg.end)]
            target.end = seg.end
        else:
            joined[seg.key] = PlannedSegment(seg.start, seg.end, seg.category, stream_title=seg.stream_title)
    return list(joined.values())


def _cut_spans(spans: List[Tuple[int, int]], cuts: List[int]) -> List[List[Tuple[int, int]]]:
    """Делит склеенные отрезки по позициям внутри ролика (секунды от его начала)."""
    pieces: List[List[Tuple[int, int]]] = [[]]
    position = 0
    targets = iter(cuts)
    cut = next(targets, None)
    for start, end in spans:
        while cut is not None and position < cut < position + (end - start):
            middle = start + (cut - position)
            pieces[-1].append((start, middle))
            pieces.append([])
            position, start = cut, middle
            cut = next(targets, None)
        pieces[-1].append((start, end))
        position += end - start
        if cut is not None and position == cut:
            pieces.append([])
            cut = next(targets, None)
    return [piece for piece in pieces if piece]


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


SHORT_PREFIX = "короче "


def short_reason(duration_sec: float, min_minutes: int) -> Optional[str]:
    """Сегменты короче min_minutes не загружаются: это заставки, перерывы и переходы."""
    if min_minutes > 0 and duration_sec < min_minutes * 60:
        return f"{SHORT_PREFIX}{min_minutes} мин"
    return None


def is_short_reason(reason: Optional[str]) -> bool:
    return bool(reason) and reason.startswith(SHORT_PREFIX)


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


TITLE_SEPARATOR = " | "


def build_title(
    category: str, stream_title: str, streamer: str, part: Optional[int] = None, limit: int = TITLE_MAX_CHARS
) -> str:
    """«{категория} | {название стрима} | Часть {N} | {стример}», не длиннее limit символов.

    Категория, часть и стример обязательны: если всё не помещается, сокращается название стрима.
    """
    head = clean_line(category) or FALLBACK_CATEGORY
    tail = [item for item in (f"Часть {part}" if part else "", clean_line(streamer)) if item]
    body = clean_line(stream_title)
    title = TITLE_SEPARATOR.join([head, body, *tail] if body else [head, *tail])
    if len(title) <= limit:
        return title
    fixed = TITLE_SEPARATOR.join([head, *tail])
    room = limit - len(fixed) - len(TITLE_SEPARATOR)
    if body and room >= 10:
        return TITLE_SEPARATOR.join([head, _shorten(body, room), *tail])
    if len(fixed) <= limit:
        return fixed
    # Не помещается даже без названия стрима — значит, слишком длинная категория
    rest = "".join(TITLE_SEPARATOR + item for item in tail)
    return _shorten(head, limit - len(rest)) + rest


def build_playlist_title(category: str, streamer: str, limit: int = PLAYLIST_TITLE_MAX_CHARS) -> str:
    """«{категория} | {стример}» — плейлист со всеми роликами категории."""
    name = clean_line(streamer)
    tail = f"{TITLE_SEPARATOR}{name}" if name else ""
    return _shorten(clean_line(category) or FALLBACK_CATEGORY, limit - len(tail)) + tail


def _shorten(text: str, room: int) -> str:
    """Обрезает текст до room символов вместе с «…», по возможности на границе слова."""
    if len(text) <= room:
        return text
    cut = text[: max(room - 1, 0)]
    space = cut.rfind(" ")
    if space >= room // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:!?-—|") + "…"


def build_description(
    stream_title: str,
    streamer: str,
    streamer_login: str,
    stream_date: str,
    category: str,
    spans: Iterable[Tuple[float, float]],
) -> str:
    """Описание с указанием автора: этого требует разрешение стримера и правила YouTube.

    spans — отрезки стрима в секундах; у склеенного ролика их несколько.
    """
    lines = []
    title = clean_line(stream_title)[:1000]
    if title:
        lines += [title, ""]
    when = f" от {stream_date}" if stream_date else ""
    spans = list(spans)
    kind = "Фрагменты" if len(spans) > 1 else "Фрагмент"
    lines += [
        f"{kind} стрима {clean_line(streamer)}{when}: {clean_line(category)}, {fmt_spans(spans)}.",
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


def fmt_spans(spans: Iterable[Tuple[float, float]]) -> str:
    """[(0, 2403), (17917, 27310)] → «0:00–40:03, 4:58:37–7:35:10»."""
    return ", ".join(f"{fmt_hms(start)}–{fmt_hms(end)}" for start, end in spans)


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


# --- стримеры ---

_CHANNEL_URL = re.compile(r"(?<![\w.-])(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]+)", re.IGNORECASE)
_CHANNEL_LOGIN = re.compile(r"@?([A-Za-z0-9_]+)")
# Разделы сайта Twitch, которые стоят в ссылке на месте логина
_NOT_CHANNELS = {"videos", "video", "directory", "settings", "search", "downloads", "subscriptions", "inventory", "drops"}


def parse_channel(text: Optional[str]) -> Optional[str]:
    """Логин канала из ссылки twitch.tv/<логин> или из самого логина (можно с @). None — это не канал."""
    text = (text or "").strip()
    match = _CHANNEL_URL.search(text) or _CHANNEL_LOGIN.fullmatch(text)
    if not match:
        return None
    login = match.group(1).lower()
    # На Twitch логин — от 3 до 25 латинских букв, цифр и подчёркиваний
    if login in _NOT_CHANNELS or not 3 <= len(login) <= 25:
        return None
    return login


def match_streamer(text: Optional[str], streamers: Sequence):
    """Стример, которого назвал владелец: по логину, ссылке на канал или имени.

    У стримеров нужны поля login, display_name и title_name. None — никто не подошёл
    или под имя подходят несколько.
    """
    query = _fold(clean_line(text))
    if not query:
        return None
    login = parse_channel(text)
    for streamer in streamers:
        if login and streamer.login == login:
            return streamer
    named = [s for s in streamers if query in (_fold(s.title_name), _fold(s.display_name))]
    return named[0] if len(named) == 1 else None
