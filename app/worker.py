"""Очередь: одобренные сегменты по одному скачиваются с Twitch и загружаются на YouTube."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from html import escape
from pathlib import Path

from sqlalchemy import select, update

from .checks import verify_probe
from .context import App
from .db import Segment, Status, Streamer, Vod, kv_get, kv_set
from .service import build_metadata
from .tools import ToolError, download_section, probe
from .youtube import LIMIT_REASONS, AuthError, YouTubeError

log = logging.getLogger(__name__)

# Оценка размера сверху: 1080p60 на Twitch — до ~10 Мбит/с
BYTES_PER_SECOND = 1_250_000
PAUSED_KEY = "paused"


class Blocked(Exception):
    """Проблема не в сегменте (нет доступа, места, упёрлись в лимит).

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
                log.exception("Сбой в цикле обработки")
            try:
                await asyncio.wait_for(self.app.wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def _recover(self) -> None:
        """После перезапуска прерванные задачи возвращаются в очередь.

        Скачанный и проверенный файл повторно не качается, а загрузка на YouTube
        продолжается с последнего принятого куска.
        """
        async with self.app.sessions() as session, session.begin():
            await session.execute(
                update(Segment)
                .where(Segment.status.in_((Status.DOWNLOADING, Status.UPLOADING)))
                .values(status=Status.APPROVED)
            )

    async def _next(self) -> int | None:
        async with self.app.sessions() as session:
            return await session.scalar(
                select(Segment.id)
                .where(Segment.status == Status.APPROVED)
                .order_by(Segment.queued_at, Segment.id)
                .limit(1)
            )

    async def _update(self, segment_id: int, **values) -> None:
        async with self.app.sessions() as session, session.begin():
            await session.execute(update(Segment).where(Segment.id == segment_id).values(**values))

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
            path = await self._download(seg)
            video_id = await self._upload(seg, vod, streamer, refresh_token, path)
        except Blocked as exc:
            await self._update(segment_id, status=Status.APPROVED)
            await set_paused(app, True)
            await app.refresh_segment(segment_id)
            await app.notify(f"🔴 Обработка на паузе: {escape(str(exc))}", reply_to=seg.tg_message_id)
            return
        except Exception as exc:
            log.exception("Сегмент %s не загружен", segment_id)
            await self._update(segment_id, status=Status.FAILED, error=str(exc)[:1000])
            await app.refresh_segment(segment_id)
            await app.notify(
                f"🔴 Сегмент «{escape(seg.title)}» не загружен: {escape(str(exc)[:500])}",
                reply_to=seg.tg_message_id,
            )
            return

        path.unlink(missing_ok=True)
        await self._update(
            segment_id,
            status=Status.UPLOADED,
            youtube_id=video_id,
            local_path=None,
            upload_uri=None,
            progress=100,
            error=None,
        )
        await app.refresh_segment(segment_id)
        await app.notify(f"✅ Загружено: https://youtu.be/{video_id}", reply_to=seg.tg_message_id)

    async def _download(self, seg: Segment) -> Path:
        if seg.local_path and Path(seg.local_path).exists():
            return Path(seg.local_path)  # скачан и проверен до перезапуска
        settings = self.app.settings
        need = (seg.end - seg.start) * BYTES_PER_SECOND + settings.disk_reserve_gb * 1024**3
        free = shutil.disk_usage(settings.work_dir).free
        if free < need:
            raise Blocked(f"мало места на диске: нужно ~{need / 1024**3:.0f} ГБ, свободно {free / 1024**3:.0f} ГБ")

        await self._update(seg.id, status=Status.DOWNLOADING, progress=0, error=None)
        await self.app.refresh_segment(seg.id)
        path = await download_section(seg.vod_id, seg.start, seg.end, settings.work_dir / f"seg{seg.id}")
        problem = verify_probe(await probe(path), expected=seg.end - seg.start)
        if problem:
            path.unlink(missing_ok=True)
            raise ToolError(f"скачанный файл повреждён: {problem}")
        await self._update(seg.id, local_path=str(path))
        return path

    async def _upload(self, seg: Segment, vod: Vod, streamer: Streamer, refresh_token: str, path: Path) -> str:
        app = self.app
        await self._update(seg.id, status=Status.UPLOADING, progress=0)
        await app.refresh_segment(seg.id)
        shown = {"percent": 0, "at": time.monotonic()}

        async def on_progress(percent: int) -> None:
            # Не чаще раза в 5 секунд и шагами по 10%, чтобы не упираться в лимиты Telegram
            now = time.monotonic()
            if percent < 100 and (percent - shown["percent"] < 10 or now - shown["at"] < 5):
                return
            shown.update(percent=percent, at=now)
            await self._update(seg.id, progress=percent)
            await app.refresh_segment(seg.id)

        async def on_session(uri: str) -> None:
            await self._update(seg.id, upload_uri=uri)

        try:
            return await app.youtube.upload(
                refresh_token,
                path,
                build_metadata(app, seg, vod, streamer),
                session_uri=seg.upload_uri,
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
