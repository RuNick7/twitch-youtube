"""Очередь загрузки: сегменты по одному идут потоком с Twitch на YouTube, приватно."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from html import escape

from sqlalchemy import select, update

from .context import App
from .db import Segment, Status, Streamer, Vod, get_spans, kv_get, kv_set, utcnow
from .hls import Plan, SourceError, build_plan, stream
from .service import build_metadata, update_segment
from .tools import fetch_vod_info
from .youtube import LIMIT_REASONS, AuthError, YouTubeError

log = logging.getLogger(__name__)

PAUSED_KEY = "paused"
FIRST_CHECK_AFTER = timedelta(minutes=2)


class Blocked(Exception):
    """Проблема не в сегменте (нет доступа, упёрлись в лимит).

    Обработка встаёт на паузу, а сегмент остаётся первым в очереди.
    """


async def is_paused(app: App) -> bool:
    async with app.sessions() as session:
        return await kv_get(session, PAUSED_KEY) == "1"


async def set_paused(app: App, paused: bool) -> None:
    async with app.sessions() as session, session.begin():
        await kv_set(session, PAUSED_KEY, "1" if paused else "0")
    if not paused:
        app.wake.set()


class Worker:
    def __init__(self, app: App):
        self.app = app

    async def run(self) -> None:
        await self._recover()
        while True:
            self.app.wake.clear()
            try:
                if not await is_paused(self.app):
                    segment_id = await self._next()
                    if segment_id is not None:
                        await self._process(segment_id)
                        continue
            except Exception:
                log.exception("Сбой в очереди загрузки")
            try:
                await asyncio.wait_for(self.app.wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def _recover(self) -> None:
        """После перезапуска прерванная загрузка возвращается в очередь.

        Сессия загрузки сохранена, поэтому YouTube продолжит с последнего принятого байта.
        """
        async with self.app.sessions() as session, session.begin():
            await session.execute(
                update(Segment).where(Segment.status == Status.UPLOADING).values(status=Status.QUEUED)
            )

    async def _next(self) -> int | None:
        async with self.app.sessions() as session:
            return await session.scalar(
                select(Segment.id)
                .where(Segment.status == Status.QUEUED)
                .order_by(Segment.queued_at, Segment.id)
                .limit(1)
            )

    async def _process(self, segment_id: int) -> None:
        app = self.app
        async with app.sessions() as session:
            seg = await session.get(Segment, segment_id)
            vod = await session.get(Vod, seg.vod_id)
            streamer = await session.get(Streamer, vod.streamer_id)
        try:
            if not streamer.youtube_token:
                raise Blocked("YouTube-канал не подключён. Выполните /youtube, затем /resume")
            refresh_token = app.vault.decrypt(streamer.youtube_token)
            await update_segment(app, segment_id, status=Status.UPLOADING, error=None)
            await app.refresh_segment(segment_id)
            plan = await self._plan(seg, vod)
            video_id = await self._upload(seg, vod, streamer, refresh_token, plan)
        except Blocked as exc:
            await update_segment(app, segment_id, status=Status.QUEUED)
            await set_paused(app, True)
            await app.refresh_segment(segment_id)
            await app.notify(f"🔴 Обработка на паузе: {escape(str(exc))}", reply_to=seg.tg_message_id)
            return
        except Exception as exc:
            log.exception("Сегмент %s не загружен", segment_id)
            await update_segment(app, segment_id, status=Status.FAILED, error=str(exc)[:1000])
            await app.refresh_segment(segment_id)
            await app.notify(
                f"🔴 Сегмент «{escape(seg.title)}» не загружен: {escape(str(exc)[:500])}",
                reply_to=seg.tg_message_id,
            )
            return

        await update_segment(
            app,
            segment_id,
            status=Status.PROCESSING,
            youtube_id=video_id,
            upload_uri=None,
            progress=100,
            error=None,
            check_at=utcnow() + FIRST_CHECK_AFTER,
        )
        await app.refresh_segment(segment_id)

    async def _plan(self, seg: Segment, vod: Vod) -> Plan:
        if vod.playlist_url:
            try:
                return await build_plan(self.app.http, vod.playlist_url, get_spans(seg))
            except SourceError as exc:
                log.info("Плейлист VOD %s не открылся (%s), беру свежую ссылку", vod.id, exc)
        info = await fetch_vod_info(vod.id)
        if not info.playlist_url:
            raise SourceError("yt-dlp не нашёл плейлист VOD")
        async with self.app.sessions() as session, session.begin():
            await session.execute(update(Vod).where(Vod.id == vod.id).values(playlist_url=info.playlist_url))
        return await build_plan(self.app.http, info.playlist_url, get_spans(seg))

    async def _upload(self, seg: Segment, vod: Vod, streamer: Streamer, refresh_token: str, plan: Plan) -> str:
        app = self.app
        # Если VOD на Twitch изменился (например, Twitch заглушил фрагменты), старую сессию не продолжить
        session_uri = seg.upload_uri if seg.upload_total == plan.total else None
        await update_segment(
            app,
            seg.id,
            upload_total=plan.total,
            expected_duration=round(plan.duration),
            upload_uri=session_uri,
            progress=0,
        )
        shown = {"percent": 0, "at": time.monotonic()}

        async def on_progress(percent: int) -> None:
            # Не чаще раза в 5 секунд и шагами по 10%, чтобы не упираться в лимиты Telegram
            now = time.monotonic()
            if percent < 100 and (percent - shown["percent"] < 10 or now - shown["at"] < 5):
                return
            shown.update(percent=percent, at=now)
            await update_segment(app, seg.id, progress=percent)
            await app.refresh_segment(seg.id)

        async def on_session(uri: str) -> None:
            await update_segment(app, seg.id, upload_uri=uri)

        try:
            return await app.youtube.upload_stream(
                refresh_token,
                plan.total,
                lambda offset: stream(app.http, plan, offset),
                build_metadata(app, seg, vod, streamer),
                session_uri=session_uri,
                on_session=on_session,
                on_progress=on_progress,
                chunk_size=app.settings.upload_chunk_mb * 1024 * 1024,
                limit_mbit=app.settings.upload_limit_mbit,
            )
        except AuthError as exc:
            raise Blocked("доступ к YouTube отозван или истёк. Выполните /youtube, затем /resume") from exc
        except YouTubeError as exc:
            if exc.reason in LIMIT_REASONS:
                raise Blocked(f"YouTube временно не принимает загрузки ({exc.reason}). Выполните /resume позже") from exc
            if exc.reason == "youtubeSignupRequired":
                raise Blocked("у Google-аккаунта нет YouTube-канала: создайте канал и выполните /youtube") from exc
            raise
