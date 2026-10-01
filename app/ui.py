"""Тексты и кнопки сообщений в Telegram."""

from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import Segment, Status, Streamer, Vod, as_utc, get_warnings
from .segments import fmt_duration, fmt_hms, twitch_time_param

PUBLISH, KEEP, RETRY, FORCE = "pub", "keep", "rt", "force"


class SegmentAction(CallbackData, prefix="seg"):
    action: str
    id: int


def _button(text: str, action: str, segment_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=SegmentAction(action=action, id=segment_id).pack())


def twitch_link(vod_id: str, seconds: int) -> str:
    return f"https://www.twitch.tv/videos/{vod_id}?t={twitch_time_param(seconds)}"


def youtube_link(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


def studio_link(video_id: str) -> str:
    return f"https://studio.youtube.com/video/{video_id}/edit"


def local_time(value: datetime | None, tz: ZoneInfo, fmt: str = "%d.%m") -> str:
    return as_utc(value).astimezone(tz).strftime(fmt) if value else ""


def segment_keyboard(seg: Segment) -> InlineKeyboardMarkup | None:
    links = [InlineKeyboardButton(text="▶️ Twitch", url=twitch_link(seg.vod_id, seg.start))]
    if seg.youtube_id:
        links.append(InlineKeyboardButton(text="🛠 YouTube Studio", url=studio_link(seg.youtube_id)))
    if seg.status == Status.SKIPPED:
        rows = [[_button("⬆️ Всё равно загрузить", FORCE, seg.id)], links]
    elif seg.status == Status.WAITING:
        rows = [[_button("🚀 Опубликовать сейчас", PUBLISH, seg.id), _button("🔒 Не публиковать", KEEP, seg.id)], links]
    elif seg.status == Status.REVIEW:
        rows = [[_button("✅ Опубликовать", PUBLISH, seg.id), _button("🔒 Оставить приватным", KEEP, seg.id)], links]
    elif seg.status == Status.PRIVATE:
        rows = [[_button("✅ Всё-таки опубликовать", PUBLISH, seg.id)], links]
    elif seg.status == Status.FAILED:
        rows = [[_button("🔁 Повторить", RETRY, seg.id)], links]
    elif seg.status == Status.PUBLISHED:
        return None
    else:
        rows = [links]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def status_line(seg: Segment, tz: ZoneInfo, privacy: str) -> str:
    link = youtube_link(seg.youtube_id) if seg.youtube_id else ""
    reason = escape(seg.reason or "")
    if seg.status == Status.SKIPPED:
        return f"⏭ Не загружен: {reason}"
    if seg.status == Status.QUEUED:
        return "⏳ В очереди на загрузку"
    if seg.status == Status.UPLOADING:
        return f"⬆️ Загружается на YouTube: {seg.progress}%"
    if seg.status == Status.PROCESSING:
        return f"⚙️ Загружен приватно, YouTube обрабатывает: {link}"
    if seg.status == Status.WAITING:
        return f"🔎 Обработан, ждём проверку Content ID до {local_time(seg.publish_after, tz, '%H:%M')}: {link}"
    if seg.status == Status.REVIEW:
        return f"⚠️ Нужно решение, ролик пока приватный: {link}"
    if seg.status == Status.PUBLISHED:
        return f"✅ Опубликован{' по ссылке' if privacy == 'unlisted' else ''}: {link}"
    if seg.status == Status.PRIVATE:
        return f"🔒 Оставлен приватным: {link}"
    if seg.status == Status.LOCKED:
        return f"🔒 YouTube не дал опубликовать: {reason}. {link}"
    if seg.status == Status.REJECTED:
        return f"🔴 YouTube отклонил ролик: {reason}. {link}"
    if seg.status == Status.FAILED:
        return f"🔴 Ошибка: {escape((seg.error or '')[:500])}"
    return seg.status


def render_segment(
    seg: Segment, vod: Vod, streamer: Streamer, tz: ZoneInfo, privacy: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    part = f" (часть {seg.part})" if seg.part else ""
    date = local_time(vod.started_at, tz)
    stream = f"Стрим {date} «{escape(vod.title)}»" if date else f"Стрим «{escape(vod.title)}»"
    lines = [
        f"🎮 <b>{escape(seg.category)}</b>{part} · {fmt_hms(seg.start)}–{fmt_hms(seg.end)}"
        f" ({fmt_duration(seg.end - seg.start)})",
        stream,
        f"Название: {escape(seg.title)}",
        "",
        status_line(seg, tz, privacy),
    ]
    lines += [f"• {escape(warning)}" for warning in get_warnings(seg)]
    return "\n".join(lines), segment_keyboard(seg)


def render_vod_header(vod: Vod, streamer: Streamer, segments: list[Segment], tz: ZoneInfo) -> str:
    name = escape(streamer.display_name or streamer.login)
    date = local_time(vod.started_at, tz, "%d.%m.%Y")
    when = f" · {date}" if date else ""
    skipped = sum(seg.status == Status.SKIPPED for seg in segments)
    count = f"Сегментов: {len(segments)}" + (f", не загружаются: {skipped}" if skipped else "")
    return f"📺 <b>{name}</b>{when} · {fmt_duration(vod.duration)}\n«{escape(vod.title)}»\n{count}."
