"""Действия над VOD и сегментами, общие для бота и обработчика очереди."""

from __future__ import annotations

from sqlalchemy import func, select, update

from .context import App
from .db import Segment, Status, Streamer, Vod, get_streamer, utcnow
from .segments import build_description, build_tags, build_title, fmt_hms, normalize_chapters, plan_segments
from .tools import VodInfo
from .ui import local_date, render_segment, render_vod_header


class IngestError(RuntimeError):
    pass


async def ingest_vod(app: App, info: VodInfo) -> int:
    """Создаёт сегменты VOD и присылает их на проверку. Возвращает число сегментов."""
    settings = app.settings
    if info.is_live:
        raise IngestError("стрим ещё идёт, VOD не завершён")
    if info.duration <= 0:
        raise IngestError("у VOD нет длительности: он ещё обрабатывается или недоступен")
    if info.uploader_login and info.uploader_login != settings.twitch_channel.lower():
        raise IngestError(f"VOD с канала {info.uploader_login}, а в настройках указан {settings.twitch_channel}")

    planned = plan_segments(normalize_chapters(info.chapters, info.duration), min_sec=settings.min_segment_sec)

    async with app.sessions() as session, session.begin():
        if await session.get(Vod, info.id):
            raise IngestError("этот VOD уже обработан")
        streamer = await get_streamer(session, settings.twitch_channel)
        if info.uploader:
            streamer.display_name = info.uploader
        name = streamer.display_name or streamer.login
        vod = Vod(
            id=info.id, streamer_id=streamer.id, title=info.title, started_at=info.started_at, duration=info.duration
        )
        segments = [
            Segment(
                vod_id=info.id,
                idx=i,
                start=p.start,
                end=p.end,
                category=p.category,
                part=p.part,
                title=build_title(p.category, info.title, name, p.part),
                status=Status.PENDING,
            )
            for i, p in enumerate(planned, 1)
        ]
        session.add(vod)
        session.add_all(segments)

    await app.notify(render_vod_header(vod, streamer, len(segments), app.tz))
    for seg in segments:
        text, markup = render_segment(seg, vod, streamer, app.tz, app.private_uploads)
        message = await app.notify(text, markup=markup)
        if message:
            async with app.sessions() as session, session.begin():
                await session.execute(
                    update(Segment).where(Segment.id == seg.id).values(tg_message_id=message.message_id)
                )
    return len(segments)


async def set_status(app: App, segment_id: int, allowed_from: tuple[str, ...], to: str) -> bool:
    """Меняет статус, только если сегмент сейчас в одном из allowed_from.

    Так двойное нажатие кнопки ничего не ломает.
    """
    values: dict = {"status": to}
    if to == Status.APPROVED:
        values.update(queued_at=utcnow(), error=None)
    async with app.sessions() as session, session.begin():
        result = await session.execute(
            update(Segment).where(Segment.id == segment_id, Segment.status.in_(allowed_from)).values(**values)
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
            local_date(vod.started_at, app.tz, "%d.%m.%Y"),
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
        # YouTube требует явно указывать аудиторию каждого ролика
        "status": {"privacyStatus": settings.youtube_privacy, "selfDeclaredMadeForKids": False},
    }
