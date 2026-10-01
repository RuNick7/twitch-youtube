"""Действия над VOD и сегментами, общие для бота, слежения за каналом и очередей."""

from __future__ import annotations

from datetime import timedelta
from html import escape

from sqlalchemy import func, select, update

from .context import App
from .db import Segment, Status, Streamer, Vod, dump_warnings, get_streamer, utcnow
from .segments import (
    build_description,
    build_tags,
    build_title,
    category_warnings,
    fmt_hms,
    normalize_chapters,
    plan_segments,
    skip_reason,
)
from .tools import VodInfo
from .ui import local_time, render_segment, render_vod_header, youtube_link

MONITOR_EVERY = timedelta(hours=6)
PUBLISHABLE = (Status.WAITING, Status.REVIEW, Status.PRIVATE)
LOCKED_REASON = "ролик остался приватным: скорее всего, Google-проект ещё не прошёл аудит YouTube API"

_publishing: set[int] = set()


class IngestError(RuntimeError):
    pass


async def ingest_vod(app: App, info: VodInfo) -> int:
    """Создаёт сегменты VOD, ставит их в очередь загрузки и присылает в Telegram. Возвращает число сегментов."""
    settings = app.settings
    if info.is_live:
        raise IngestError("стрим ещё идёт, VOD не завершён")
    if info.duration <= 0:
        raise IngestError("у VOD нет длительности: он ещё обрабатывается или недоступен")
    if info.uploader_login and info.uploader_login != settings.twitch_channel.lower():
        raise IngestError(f"VOD с канала {info.uploader_login}, а в настройках указан {settings.twitch_channel}")

    planned = plan_segments(normalize_chapters(info.chapters, info.duration), min_sec=settings.min_segment_sec)
    now = utcnow()

    async with app.sessions() as session, session.begin():
        if await session.get(Vod, info.id):
            raise IngestError("этот VOD уже обработан")
        streamer = await get_streamer(session, settings.twitch_channel)
        if info.uploader:
            streamer.display_name = info.uploader
        name = streamer.display_name or streamer.login
        vod = Vod(
            id=info.id,
            streamer_id=streamer.id,
            title=info.title,
            started_at=info.started_at,
            duration=info.duration,
            playlist_url=info.playlist_url,
        )
        # Между моделями нет relationship, и без явного flush SQLAlchemy может вставить
        # сегменты раньше VOD — тогда SQLite отвергнет их по внешнему ключу
        session.add(vod)
        await session.flush()
        segments = []
        for i, p in enumerate(planned, 1):
            reason = skip_reason(
                p.category,
                info.title,
                keywords=settings.keywords,
                title_categories=settings.title_categories,
                categories=settings.skipped_categories,
            )
            segments.append(
                Segment(
                    vod_id=info.id,
                    idx=i,
                    start=p.start,
                    end=p.end,
                    category=p.category,
                    part=p.part,
                    title=build_title(p.category, info.title, name, p.part),
                    status=Status.SKIPPED if reason else Status.QUEUED,
                    reason=reason,
                    warnings=dump_warnings(category_warnings(p.category, settings.warned_categories)),
                    queued_at=None if reason else now,
                )
            )
        session.add_all(segments)

    await app.notify(render_vod_header(vod, streamer, segments, app.tz), silent=True)
    for seg in segments:
        text, markup = render_segment(seg, vod, streamer, app.tz, settings.publish_privacy)
        message = await app.notify(text, markup=markup, silent=True)
        if message:
            await update_segment(app, seg.id, tg_message_id=message.message_id)
    app.wake.set()
    return len(segments)


async def update_segment(app: App, segment_id: int, **values) -> None:
    async with app.sessions() as session, session.begin():
        await session.execute(update(Segment).where(Segment.id == segment_id).values(**values))


async def set_status(app: App, segment_id: int, allowed_from: tuple[str, ...], to: str, **values) -> bool:
    """Меняет статус, только если сегмент сейчас в одном из allowed_from.

    Так двойное нажатие кнопки ничего не ломает.
    """
    async with app.sessions() as session, session.begin():
        result = await session.execute(
            update(Segment)
            .where(Segment.id == segment_id, Segment.status.in_(allowed_from))
            .values(status=to, **values)
        )
    return result.rowcount == 1


async def status_counts(app: App) -> dict[str, int]:
    async with app.sessions() as session:
        rows = await session.execute(select(Segment.status, func.count()).group_by(Segment.status))
    return {status: count for status, count in rows.all()}


def build_metadata(app: App, seg: Segment, vod: Vod, streamer: Streamer) -> dict:
    settings = app.settings
    name = streamer.display_name or streamer.login
    snippet = {
        "title": seg.title,
        "description": build_description(
            vod.title,
            name,
            streamer.login,
            local_time(vod.started_at, app.tz, "%d.%m.%Y"),
            seg.category,
            fmt_hms(seg.start),
            fmt_hms(seg.end),
        ),
        "tags": build_tags(seg.category, name, streamer.login, "стрим", "twitch"),
        "categoryId": settings.youtube_category_id,
    }
    if settings.youtube_language:
        snippet["defaultLanguage"] = settings.youtube_language
        snippet["defaultAudioLanguage"] = settings.youtube_language
    return {
        "snippet": snippet,
        # Ролик загружается приватным и открывается только после проверок.
        # YouTube требует явно указывать аудиторию и отсутствие сгенерированного контента
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False},
    }


async def publish(app: App, segment_id: int) -> str:
    """Открывает доступ к ролику и возвращает короткий итог.

    Ошибки YouTube (в том числе AuthError) пробрасываются вызывающему.
    """
    if segment_id in _publishing:
        return "Уже публикуется"
    _publishing.add(segment_id)
    try:
        async with app.sessions() as session:
            seg = await session.get(Segment, segment_id)
            vod = await session.get(Vod, seg.vod_id)
            streamer = await session.get(Streamer, vod.streamer_id)
        if seg.status not in PUBLISHABLE or not seg.youtube_id:
            return "Уже обработан"
        if not streamer.youtube_token:
            return "YouTube-канал не подключён: выполните /youtube"
        token = app.vault.decrypt(streamer.youtube_token)
        privacy = app.settings.publish_privacy
        item = (await app.youtube.videos(token, [seg.youtube_id])).get(seg.youtube_id)
        if item is None:
            await update_segment(app, seg.id, status=Status.REJECTED, reason="ролик удалён с YouTube", check_at=None)
            await app.refresh_segment(seg.id)
            return "Ролика больше нет на YouTube"
        status = await app.youtube.set_privacy(token, seg.youtube_id, privacy, item.get("status") or {})
        now = utcnow()
        if status.get("privacyStatus") != privacy:
            await update_segment(app, seg.id, status=Status.LOCKED, reason=LOCKED_REASON, check_at=None)
            await app.refresh_segment(seg.id)
            await app.notify(
                f"🔒 «{escape(seg.title)}»: YouTube не дал опубликовать ролик. Пока Google-проект не прошёл "
                "аудит YouTube API, все загруженные через API ролики остаются приватными.",
                reply_to=seg.tg_message_id,
            )
            return "YouTube не дал опубликовать"
        await update_segment(
            app,
            seg.id,
            status=Status.PUBLISHED,
            published_at=now,
            monitor_until=now + timedelta(days=app.settings.monitor_days),
            check_at=now + MONITOR_EVERY,
        )
        await app.refresh_segment(seg.id)
        await app.notify(
            f"✅ Опубликовано: «{escape(seg.title)}» {youtube_link(seg.youtube_id)}",
            reply_to=seg.tg_message_id,
            silent=True,
        )
        return "Опубликовано"
    finally:
        _publishing.discard(segment_id)
