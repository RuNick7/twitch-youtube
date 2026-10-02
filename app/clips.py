"""Клипы Twitch для Shorts: разбор ответа Twitch, отбор и метаданные.

Модуль не зависит от сторонних библиотек, чтобы логику можно было проверять
тестами без установки зависимостей.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Tuple

from .segments import (
    _LOGIN,
    DESCRIPTION_MAX_BYTES,
    TITLE_MAX_CHARS,
    TITLE_SEPARATOR,
    _shorten,
    build_title,
    clean_line,
)

_LETTER = re.compile(r"[^\W\d_]")


@dataclass
class Clip:
    id: str
    slug: str
    title: str
    views: int
    duration: float
    created_at: Optional[datetime] = None
    author: Optional[str] = None  # кто сделал клип
    category: Optional[str] = None
    vod_id: Optional[str] = None  # запись эфира, если она сохранилась
    vod_offset: Optional[int] = None  # секунда записи, с которой начинается клип
    vod_title: Optional[str] = None

    @property
    def url(self) -> str:
        return clip_url(self.slug)

    @property
    def span(self) -> Optional[Tuple[int, int]]:
        """Отрезок записи эфира, который показывает клип."""
        if self.vod_id is None or self.vod_offset is None:
            return None
        return self.vod_offset, self.vod_offset + max(1, round(self.duration))


def clip_url(slug: str) -> str:
    return f"https://clips.twitch.tv/{slug}"


def parse_clip(node: dict) -> Optional[Clip]:
    """Клип из ответа GraphQL Twitch или None, если в ответе нет нужных полей."""
    try:
        created = node.get("createdAt")
        video = node.get("video") or {}
        offset = node.get("videoOffsetSeconds")
        return Clip(
            id=str(node["id"]),
            slug=str(node["slug"]),
            title=str(node.get("title") or "").strip(),
            views=int(node.get("viewCount") or 0),
            duration=float(node.get("durationSeconds") or 0),
            created_at=datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None,
            author=(node.get("curator") or {}).get("displayName") or (node.get("curator") or {}).get("login"),
            category=(node.get("game") or {}).get("name"),
            vod_id=str(video["id"]) if video.get("id") else None,
            vod_offset=int(offset) if offset is not None and video.get("id") else None,
            vod_title=video.get("title"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def same_moment(a: Clip, b: Clip) -> bool:
    """Клипы одного момента стрима: одна запись эфира и пересекающееся время."""
    if a.span is None or b.span is None or a.vod_id != b.vod_id:
        return False
    (a_start, a_end), (b_start, b_end) = a.span, b.span
    return a_start < b_end and b_start < a_end


def choose(clips: Iterable[Clip], min_views: int, known: Sequence[Clip] = ()) -> List[Clip]:
    """Новые клипы от min_views просмотров, самые просматриваемые первыми.

    known — клипы, по которым решение уже принято. Повтор уже взятого момента
    (его часто клипают несколько зрителей) не берётся: из одного момента выходит
    один Shorts, из самого просматриваемого клипа.
    """
    known_ids = {clip.id for clip in known}
    chosen: List[Clip] = []
    for clip in sorted(clips, key=lambda item: item.views, reverse=True):
        if clip.views < min_views or clip.id in known_ids:
            continue
        if any(same_moment(clip, other) for other in [*known, *chosen]):
            continue
        chosen.append(clip)
    return chosen


def is_meaningful(title: str) -> bool:
    """Название клипа придумывает зритель: «123123» или «ааа» ролик не описывают."""
    letters = [letter.lower() for letter in _LETTER.findall(title or "")]
    return len(letters) >= 3 and len(set(letters)) >= 2


def build_short_title(
    clip_title: str, streamer: str, category: str, stream_title: str, limit: int = TITLE_MAX_CHARS
) -> str:
    """«{название клипа} | {стример}»; если название ничего не говорит — «{категория} | {название стрима} | {стример}»."""
    title = clean_line(clip_title)
    if not is_meaningful(title):
        return build_title(category, stream_title, streamer, limit=limit)
    name = clean_line(streamer)
    tail = f"{TITLE_SEPARATOR}{name}" if name else ""
    return _shorten(title, limit - len(tail)) + tail


def build_short_description(
    clip_title: str,
    streamer: str,
    streamer_login: str,
    stream_date: str,
    category: str,
    clip_link: str,
    author: Optional[str] = None,
    full_video: Optional[str] = None,
) -> str:
    """Описание Shorts: откуда клип, кто его сделал, ссылка на полный ролик стрима и на Twitch."""
    lines = []
    title = clean_line(clip_title)[:1000]
    if is_meaningful(title):
        lines += [title, ""]
    when = f" от {stream_date}" if stream_date else ""
    lines.append(f"Клип со стрима {clean_line(streamer)}{when}: {clean_line(category)}.")
    if author:
        lines.append(f"Автор клипа: {clean_line(author)}.")
    lines.append(f"Клип на Twitch: {clip_link}")
    if full_video:
        lines.append(f"Полный стрим: {full_video}")
    lines += [
        f"Twitch: https://www.twitch.tv/{_LOGIN.sub('', streamer_login)}",
        "",
        "Опубликовано с разрешения автора.",
        "",
        "#shorts",
    ]
    text = "\n".join(lines)
    data = text.encode("utf-8")
    if len(data) > DESCRIPTION_MAX_BYTES:
        text = data[:DESCRIPTION_MAX_BYTES].decode("utf-8", "ignore")
    return text
