"""Тексты и кнопки сообщений в Telegram."""

from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import Segment, Status, Streamer, Vod, as_utc
from .segments import fmt_duration, fmt_hms, twitch_time_param

APPROVE, REJECT, REJECT_YES, REJECT_NO, RETRY = "ap", "rj", "rjy", "rjn", "rt"


class SegmentAction(CallbackData, prefix="seg"):
    action: str
    id: int


def _button(text: str, action: str, segment_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=SegmentAction(action=action, id=segment_id).pack())


def twitch_link(vod_id: str, seconds: int) -> str:
    return f"https://www.twitch.tv/videos/{vod_id}?t={twitch_time_param(seconds)}"


def local_date(value: datetime | None, tz: ZoneInfo, fmt: str = "%d.%m") -> str:
    return as_utc(value).astimezone(tz).strftime(fmt) if value else ""


def segment_keyboard(seg: Segment) -> InlineKeyboardMarkup | None:
    if seg.status == Status.UPLOADED:
        return None
    watch = [InlineKeyboardButton(text="▶️ Смотреть на Twitch", url=twitch_link(seg.vod_id, seg.start))]
    if seg.status == Status.PENDING:
        rows = [[_button("✅ Одобрить", APPROVE, seg.id), _button("❌ Отклонить", REJECT, seg.id)], watch]
    elif seg.status == Status.FAILED:
        rows = [[_button("🔁 Повторить", RETRY, seg.id)], watch]
    else:
        rows = [watch]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_reject_keyboard(segment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button("Да, отклонить", REJECT_YES, segment_id), _button("Отмена", REJECT_NO, segment_id)]]
    )


def status_line(seg: Segment, private: bool) -> str:
    if seg.status == Status.PENDING:
        return "⏳ Ждёт проверки"
    if seg.status == Status.APPROVED:
        return "✅ Одобрен, в очереди на загрузку"
    if seg.status == Status.DOWNLOADING:
        return "⬇️ Скачивается с Twitch…"
    if seg.status == Status.UPLOADING:
        return f"⬆️ Загружается на YouTube: {seg.progress}%"
    if seg.status == Status.UPLOADED:
        return f"✅ Загружен: https://youtu.be/{seg.youtube_id}" + (" (приватно)" if private else "")
    if seg.status == Status.REJECTED:
        return "❌ Отклонён"
    if seg.status == Status.FAILED:
        return f"🔴 Ошибка: {escape((seg.error or '')[:500])}"
    return seg.status


def render_segment(
    seg: Segment, vod: Vod, streamer: Streamer, tz: ZoneInfo, private: bool
) -> tuple[str, InlineKeyboardMarkup | None]:
    part = f" (часть {seg.part})" if seg.part else ""
    date = local_date(vod.started_at, tz)
    stream = f"Стрим {date} «{escape(vod.title)}»" if date else f"Стрим «{escape(vod.title)}»"
    lines = [
        f"🎮 <b>{escape(seg.category)}</b>{part} · {fmt_hms(seg.start)}–{fmt_hms(seg.end)}"
        f" ({fmt_duration(seg.end - seg.start)})",
        stream,
        f"Название: {escape(seg.title)}",
        "",
        status_line(seg, private),
    ]
    return "\n".join(lines), segment_keyboard(seg)


def render_vod_header(vod: Vod, streamer: Streamer, count: int, tz: ZoneInfo) -> str:
    name = escape(streamer.display_name or streamer.login)
    date = local_date(vod.started_at, tz, "%d.%m.%Y")
    when = f" · {date}" if date else ""
    return (
        f"📺 <b>{name}</b>{when} · {fmt_duration(vod.duration)}\n"
        f"«{escape(vod.title)}»\n"
        f"Сегментов: {count}. Проверьте каждый ниже."
    )
