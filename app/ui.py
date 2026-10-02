"""Тексты и кнопки сообщений в Telegram."""

from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .clips import clip_url
from .config import Settings
from .db import SHORT, Segment, Status, Streamer, Vod, as_utc, get_spans, get_warnings
from .segments import fmt_duration, fmt_spans, twitch_time_param

PUBLISH, KEEP, RETRY, FORCE = "pub", "keep", "rt", "force"
CONNECT, ACCEPT, DISCONNECT, CANCEL = "on", "accept", "off", "cancel"

YOUTUBE_TERMS_URL = "https://www.youtube.com/t/terms"
GOOGLE_PRIVACY_URL = "https://www.google.com/policies/privacy"
PRIVACY_NAMES = {"private": "приватным", "unlisted": "доступным только по ссылке", "public": "публичным"}
PLAYLIST_PRIVACY = {"unlisted": "доступные по ссылке", "public": "публичные"}


def accept_action(version: int) -> str:
    """Кнопка «Принимаю» запоминает, какую версию политики показали: accept3."""
    return f"{ACCEPT}{version}"


def accepted_from_action(action: str) -> int:
    """Версия из кнопки; у кнопок до версий политики было просто accept — это версия 2."""
    suffix = action[len(ACCEPT):]
    return int(suffix) if suffix.isdigit() else 2


class SegmentAction(CallbackData, prefix="seg"):
    action: str
    id: int


class YouTubeAction(CallbackData, prefix="yt"):
    action: str


def _button(text: str, action: str, segment_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=SegmentAction(action=action, id=segment_id).pack())


def confirm_keyboard(text: str, action: str) -> InlineKeyboardMarkup:
    """Подтверждение подключения или отключения канала."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=text, callback_data=YouTubeAction(action=action).pack()),
                InlineKeyboardButton(text="Отмена", callback_data=YouTubeAction(action=CANCEL).pack()),
            ]
        ]
    )


def render_consent(settings: Settings, streamer_name: str, changes: list[str] | None = None) -> str:
    """Что AutoVOD будет делать с каналом. Правила YouTube API требуют показать это и получить
    согласие с политикой конфиденциальности до входа, а когда политика меняется — снова.

    changes — что нового в политике, если владелец принимает обновлённую версию.
    """
    privacy = PRIVACY_NAMES[settings.publish_privacy]
    if settings.auto_publish:
        publishing = (
            f"делать ролик {privacy} через {settings.publish_delay_min} мин после обработки, если YouTube "
            "не выдал предупреждений, а ролик с предупреждениями — только по вашей кнопке"
        )
    else:
        publishing = f"делать ролик {privacy} только по вашей кнопке «Опубликовать»"
    if changes:
        header = (
            "📄 <b>Политика конфиденциальности AutoVOD обновлена</b>\n"
            f"Теперь бот {' и '.join(changes)}. Пока вы не примете новую политику, это не включится, "
            "остальное работает как раньше.\n\n"
        )
    else:
        header = "<b>Подключение YouTube-канала</b>\n\n"
    playlists = (
        f"• создавать {PLAYLIST_PRIVACY[settings.publish_privacy]} плейлисты по категориям и добавлять в них "
        "опубликованные ролики;\n"
        if settings.playlists
        else ""
    )
    shorts_publishing = (
        f"публиковать их через {settings.shorts_publish_delay_min} мин после обработки по тем же правилам"
        if settings.auto_publish
        else "публиковать их только по вашей кнопке"
    )
    shorts = (
        f"• делать из клипов канала от {settings.shorts_min_views} просмотров вертикальные ролики Shorts "
        f"и {shorts_publishing};\n"
        if settings.shorts
        else ""
    )
    return (
        f"{header}"
        "С доступом к каналу AutoVOD будет:\n"
        f"• загружать на него сегменты стримов {escape(streamer_name)} приватными роликами;\n"
        f"• {publishing};\n"
        f"{playlists}"
        f"{shorts}"
        "• проверять состояние загруженных им роликов и созданных плейлистов.\n\n"
        "Другие ролики и плейлисты, комментарии и статистику канала AutoVOD не трогает. Он хранит "
        "зашифрованный токен доступа, ID и название канала, ID и состояние своих роликов, ID своих плейлистов "
        "и раз в сутки сверяет их с YouTube. Отключить канал и удалить эти данные — /disconnect.\n\n"
        f'Нажимая «Принимаю», вы соглашаетесь с <a href="{escape(settings.privacy_url)}">политикой '
        f'конфиденциальности</a> и <a href="{escape(settings.terms_url)}">условиями использования</a> AutoVOD '
        f'и с <a href="{YOUTUBE_TERMS_URL}">Условиями использования YouTube</a>. Как Google обращается с данными: '
        f'<a href="{GOOGLE_PRIVACY_URL}">Политика конфиденциальности Google</a>.'
    )


def render_disconnect(channel_title: str) -> str:
    return (
        f"Отключить YouTube-канал «{escape(channel_title)}»?\n\n"
        "AutoVOD отзовёт доступ в Google и удалит из своей базы токен, ID и название канала, ID и состояние "
        "загруженных роликов. Сами ролики останутся на YouTube: удалить их можно в YouTube Studio. "
        "Загрузка и публикация встанут на паузу до нового подключения."
    )


def twitch_link(vod_id: str, seconds: int) -> str:
    return f"https://www.twitch.tv/videos/{vod_id}?t={twitch_time_param(seconds)}"


def youtube_link(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


def studio_link(video_id: str) -> str:
    return f"https://studio.youtube.com/video/{video_id}/edit"


def local_time(value: datetime | None, tz: ZoneInfo, fmt: str = "%d.%m") -> str:
    return as_utc(value).astimezone(tz).strftime(fmt) if value else ""


def segment_keyboard(seg: Segment) -> InlineKeyboardMarkup | None:
    source = clip_url(seg.clip_slug or "") if seg.kind == SHORT else twitch_link(seg.vod_id, seg.start)
    links = [InlineKeyboardButton(text="▶️ Twitch", url=source)]
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
    if seg.status == Status.FORGOTTEN:
        return f"🗑 {reason}"
    return seg.status


def render_short(seg: Segment, tz: ZoneInfo, privacy: str) -> str:
    author = f", автор {escape(seg.clip_author)}" if seg.clip_author else ""
    lines = [
        f"🎬 <b>Shorts</b> · {escape(seg.category)} · {seg.end - seg.start} с",
        f"Клип «{escape(seg.clip_title or '')}» от {local_time(seg.clip_created_at, tz)}{author}, "
        f"просмотров на Twitch: {seg.clip_views or 0}",
        f"Название: {escape(seg.title)}",
        "",
        status_line(seg, tz, privacy),
    ]
    lines += [f"• {escape(warning)}" for warning in get_warnings(seg)]
    return "\n".join(lines)


def render_segment(
    seg: Segment, vod: Vod, streamer: Streamer, tz: ZoneInfo, privacy: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    if seg.kind == SHORT:
        return render_short(seg, tz, privacy), segment_keyboard(seg)
    part = f" (часть {seg.part})" if seg.part else ""
    date = local_time(vod.started_at, tz)
    name = escape(seg.stream_title or vod.title)
    stream = f"Стрим {date} «{name}»" if date else f"Стрим «{name}»"
    spans = get_spans(seg)
    lines = [
        f"🎮 <b>{escape(seg.category)}</b>{part} · {fmt_spans(spans)}"
        f" ({fmt_duration(sum(end - start for start, end in spans))})",
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
