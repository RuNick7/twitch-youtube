"""Действия над VOD и сегментами, общие для бота, слежения за каналом и очередей."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from html import escape

from sqlalchemy import delete, func, select, update

from .config import Settings
from .context import App
from .db import (
    Playlist,
    Segment,
    Status,
    Streamer,
    TitleChange,
    Vod,
    as_utc,
    dump_spans,
    dump_warnings,
    find_streamer,
    get_spans,
    utcnow,
)
from .segments import (
    build_description,
    build_tags,
    build_title,
    category_key,
    category_warnings,
    normalize_chapters,
    number_parts,
    plan_segments,
    short_reason,
    skip_reason,
    split_by_titles,
)
from .tools import VodInfo
from .ui import local_time, render_segment, render_vod_header, youtube_link

MONITOR_EVERY = timedelta(hours=6)
PUBLISHABLE = (Status.WAITING, Status.REVIEW, Status.PRIVATE)
LOCKED_REASON = "ролик остался приватным: скорее всего, Google-проект ещё не прошёл аудит YouTube API"

# Сегменты, у которых есть ролик на YouTube
UPLOADED = (
    Status.PROCESSING,
    Status.WAITING,
    Status.REVIEW,
    Status.PUBLISHED,
    Status.PRIVATE,
    Status.LOCKED,
    Status.REJECTED,
)
# Что AutoVOD узнал о ролике от YouTube
YOUTUBE_FIELDS = dict(
    youtube_id=None,
    upload_uri=None,
    warnings=None,
    error=None,
    check_at=None,
    publish_after=None,
    published_at=None,
    monitor_until=None,
    playlist_added_at=None,
)
DISCONNECTED_REASON = "YouTube-канал отключён: данные о ролике удалены"
REVOKED_REASON = "доступ к YouTube отозван: данные о ролике удалены"
DELETED_REASON = "ролика больше нет на YouTube: данные о нём удалены"

_publishing: set[int] = set()
_numbering = asyncio.Lock()


class IngestError(RuntimeError):
    pass


async def ingest_vod(app: App, info: VodInfo, login: str | None = None) -> int:
    """Создаёт сегменты VOD, ставит их в очередь загрузки и присылает в Telegram. Возвращает число сегментов.

    Стример определяется по каналу VOD; login — канал, на котором слежение нашло этот VOD.
    """
    if info.is_live:
        raise IngestError("стрим ещё идёт, VOD не завершён")
    if info.duration <= 0:
        raise IngestError("у VOD нет длительности: он ещё обрабатывается или недоступен")
    channel = (info.uploader_login or login or "").lower()
    if login and channel != login.lower():
        raise IngestError(f"VOD с канала {channel}, а ожидался {login}")
    async with app.sessions() as session:
        streamer = await find_streamer(session, channel) if channel else None
    if streamer is None:
        raise IngestError(
            f"VOD с канала {channel or '(неизвестного)'}, а такого стримера в боте нет"
            + (f". Добавить: /add {channel}" if channel else "")
        )
    if streamer.removed_at is not None:
        raise IngestError(f"стример {channel} убран. Вернуть: /add {channel}")
    settings = app.config(streamer)

    chapters = split_by_titles(normalize_chapters(info.chapters, info.duration), await title_marks(app, info, channel))
    planned = plan_segments(chapters, min_sec=settings.min_segment_sec, join=settings.join_repeated)
    stream_titles = [p.stream_title or info.title for p in planned]
    reasons = [
        skip_reason(
            p.category,
            stream_title,
            keywords=settings.keywords,
            title_categories=settings.title_categories,
            categories=settings.skipped_categories,
        )
        or short_reason(p.duration, settings.skip_shorter_min)
        for p, stream_title in zip(planned, stream_titles)
    ]
    now = utcnow()

    # Номера частей сквозные: два VOD, обработанные одновременно, не должны получить одинаковые
    async with _numbering, app.sessions() as session, session.begin():
        if await session.get(Vod, info.id):
            raise IngestError("этот VOD уже обработан")
        streamer = await session.get(Streamer, streamer.id)
        if info.uploader:
            streamer.display_name = info.uploader
        numbers = number_parts(
            planned,
            [reason is None for reason in reasons],
            settings.unnumbered_categories,
            await upload_counts(session, streamer.id),
        )
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
        # В конце названия — имя, по которому стримера ищут зрители, а не название его канала
        title_name = streamer.public_name
        segments = []
        for i, (p, stream_title, reason, part) in enumerate(zip(planned, stream_titles, reasons, numbers), 1):
            segments.append(
                Segment(
                    vod_id=info.id,
                    idx=i,
                    start=p.start,
                    end=p.end,
                    category=p.category,
                    part=part,
                    title=build_title(p.category, stream_title, title_name, part),
                    stream_title=stream_title,
                    ranges=dump_spans(p.spans),
                    status=Status.SKIPPED if reason else Status.QUEUED,
                    reason=reason,
                    warnings=dump_warnings(category_warnings(p.category, settings.warned_categories)),
                    queued_at=None if reason else now,
                )
            )
        session.add_all(segments)

    await app.notify(render_vod_header(vod, streamer, segments, app.tz, app.several), silent=True)
    for seg in segments:
        text, markup = render_segment(seg, vod, streamer, app.tz, settings.publish_privacy, app.several)
        message = await app.notify(text, markup=markup, silent=True)
        if message:
            await update_segment(app, seg.id, tg_message_id=message.message_id)
    app.wake.set()
    return len(segments)


async def upload_counts(session, streamer_id: int) -> dict[str, int]:
    """Сколько роликов каждой категории (ключ — category_key) бот уже загружал или поставил в очередь."""
    rows = await session.execute(
        select(Segment.category, func.count())
        .join(Vod, Vod.id == Segment.vod_id)
        .where(Vod.streamer_id == streamer_id, Segment.status != Status.SKIPPED, Segment.kind.is_(None))
        .group_by(Segment.category)
    )
    counts: dict[str, int] = {}
    for category, count in rows.all():
        counts[category_key(category)] = counts.get(category_key(category), 0) + count
    return counts


async def queue_skipped(app: App, segment_id: int, review: bool) -> bool:
    """Ставит пропущенный сегмент в очередь (кнопка «Всё равно загрузить»). False — он уже не пропущен.

    Сегмент получает следующий номер части своей категории: номер и очередь меняются
    вместе, чтобы одновременная нарезка нового VOD не взяла тот же номер.
    """
    async with _numbering, app.sessions() as session, session.begin():
        seg = await session.get(Segment, segment_id)
        if seg is None or seg.status != Status.SKIPPED:
            return False
        vod = await session.get(Vod, seg.vod_id)
        streamer = await session.get(Streamer, vod.streamer_id)
        key = category_key(seg.category)
        numbered = key not in {category_key(category) for category in app.config(streamer).unnumbered_categories}
        if seg.kind is None and numbered:  # у Shorts номера части нет
            seg.part = (await upload_counts(session, streamer.id)).get(key, 0) + 1
            seg.title = build_title(seg.category, seg.stream_title or vod.title, streamer.public_name, seg.part)
        seg.status = Status.QUEUED
        seg.queued_at = utcnow()
        seg.force_review = review
    return True


async def title_marks(app: App, info: VodInfo, channel: str) -> list[tuple[float, str]]:
    """Смены названия этого стрима, замеченные во время эфира: (секунда от начала VOD, название).

    Стрим узнаётся по каналу и времени начала: VOD и стрим начинаются одновременно.
    """
    if info.started_at is None:
        return []
    window = timedelta(minutes=15)
    async with app.sessions() as session:
        rows = list(
            (
                await session.scalars(
                    select(TitleChange)
                    .where(
                        TitleChange.channel == channel.lower(),
                        TitleChange.stream_started_at >= info.started_at - window,
                        TitleChange.stream_started_at <= info.started_at + window,
                    )
                    .order_by(TitleChange.at)
                )
            ).all()
        )
    if not rows:
        return []
    closest = min(rows, key=lambda row: abs(as_utc(row.stream_started_at) - info.started_at))
    return [
        ((as_utc(row.at) - info.started_at).total_seconds(), row.title)
        for row in rows
        if row.stream_id == closest.stream_id
    ]


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


async def forget_youtube(app: App, streamer_id: int, reason: str) -> None:
    """Удаляет всё, что AutoVOD получил от YouTube для канала: токен, ID и название канала, ID и состояние
    роликов, ID плейлистов, а заодно и согласие с политикой.

    Правила YouTube API требуют этого после отзыва доступа. Сами ролики и плейлисты остаются на YouTube,
    а сегменты, которые ещё не загружены, ждут в очереди нового подключения.
    """
    vods = select(Vod.id).where(Vod.streamer_id == streamer_id)
    async with app.sessions() as session, session.begin():
        streamer = await session.get(Streamer, streamer_id)
        if streamer.youtube_token:
            app.youtube.forget(app.vault.decrypt(streamer.youtube_token))
        streamer.youtube_token = streamer.youtube_channel_id = streamer.youtube_channel_title = None
        # Согласие с политикой спросим заново при следующем подключении
        streamer.consent_version = streamer.consent_prompted = None
        await session.execute(delete(Playlist).where(Playlist.streamer_id == streamer_id))
        # У этих сегментов в Telegram кнопки, которые больше ничего не сделают
        with_buttons = list(
            (
                await session.scalars(
                    select(Segment.id).where(Segment.vod_id.in_(vods), Segment.status.in_(PUBLISHABLE))
                )
            ).all()
        )
        await session.execute(
            update(Segment)
            .where(Segment.vod_id.in_(vods), Segment.status.in_(UPLOADED))
            .values(status=Status.FORGOTTEN, reason=reason, **YOUTUBE_FIELDS)
        )
        await session.execute(
            update(Segment)
            .where(Segment.vod_id.in_(vods), Segment.status == Status.UPLOADING)
            .values(status=Status.QUEUED)
        )
        await session.execute(
            update(Segment).where(Segment.vod_id.in_(vods), Segment.upload_uri.is_not(None)).values(upload_uri=None)
        )
    for segment_id in with_buttons:
        await app.refresh_segment(segment_id)


async def status_counts(app: App, kind: str | None = None, streamer_id: int | None = None) -> dict[str, int]:
    """Число сегментов (kind=None) или Shorts (kind=SHORT) по статусам: всех или одного стримера."""
    condition = Segment.kind.is_(None) if kind is None else Segment.kind == kind
    query = select(Segment.status, func.count()).where(condition).group_by(Segment.status)
    if streamer_id is not None:
        query = query.join(Vod, Vod.id == Segment.vod_id).where(Vod.streamer_id == streamer_id)
    async with app.sessions() as session:
        rows = await session.execute(query)
    return {status: count for status, count in rows.all()}


def build_metadata(app: App, seg: Segment, vod: Vod, streamer: Streamer) -> dict:
    name = streamer.display_name or streamer.login
    snippet = {
        "title": seg.title,
        "description": build_description(
            seg.stream_title or vod.title,
            name,
            streamer.login,
            local_time(vod.started_at, app.tz, "%d.%m.%Y"),
            seg.category,
            get_spans(seg),
        ),
        "tags": build_tags(seg.category, streamer.title_name, name, streamer.login, "стрим", "twitch"),
    }
    return video_metadata(app.config(streamer), snippet)


def video_metadata(settings: Settings, snippet: dict) -> dict:
    """Метаданные для videos.insert: язык и категория YouTube из настроек стримера, приватный доступ."""
    snippet = {**snippet, "categoryId": settings.youtube_category_id}
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
        settings = app.config(streamer)
        privacy = settings.publish_privacy
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
            monitor_until=now + timedelta(days=settings.monitor_days),
            check_at=now + MONITOR_EVERY,
        )
        app.sync_wake.set()  # в плейлист категории
        await app.refresh_segment(seg.id)
        await app.notify(
            f"✅ Опубликовано: «{escape(seg.title)}» {youtube_link(seg.youtube_id)}",
            reply_to=seg.tg_message_id,
            silent=True,
        )
        return "Опубликовано"
    finally:
        _publishing.discard(segment_id)
