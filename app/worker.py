"""Очередь загрузки: сегменты по одному идут потоком с Twitch на YouTube, приватно; Shorts — готовым файлом."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from html import escape

from sqlalchemy import or_, select, update

from . import limits
from .context import App
from .db import SHORT, Segment, Status, Streamer, Vod, get_spans, utcnow
from .hls import Plan, SourceError, build_plan, stream
from .service import DISCONNECTED_REASON, build_metadata, update_segment
from .shorts import file_stream, prepare_short, short_metadata
from .tools import fetch_vod_info
from .ui import command_for, local_time, streamer_prefix
from .youtube import LIMIT_REASONS, AuthError, StreamFactory, YouTubeError

log = logging.getLogger(__name__)

FIRST_CHECK_AFTER = timedelta(minutes=2)


class Blocked(Exception):
    """Проблема не в сегменте, а в доступе к YouTube: без владельца канала её не решить.

    Стример встаёт на паузу, а сегмент остаётся первым в его очереди.
    """


class LimitReached(Exception):
    """YouTube временно не принимает загрузки (reason — код ошибки YouTube).

    Загрузки ждут и продолжаются сами, публикация готовых роликов не останавливается.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def is_paused(app: App, streamer_id: int) -> bool:
    async with app.sessions() as session:
        return bool(await session.scalar(select(Streamer.paused).where(Streamer.id == streamer_id)))


async def set_paused(app: App, paused: bool, streamer_id: int | None = None) -> None:
    """Ставит на паузу или снимает с неё стримера, а без streamer_id — всех стримеров (/pause и /resume)."""
    query = update(Streamer).values(paused=paused)
    if streamer_id is not None:
        query = query.where(Streamer.id == streamer_id)
    async with app.sessions() as session, session.begin():
        await session.execute(query)
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
        """Первый в очереди сегмент стримера, который не на паузе и не ждёт лимита YouTube."""
        async with self.app.sessions() as session:
            return await session.scalar(
                select(Segment.id)
                .join(Vod, Vod.id == Segment.vod_id)
                .join(Streamer, Streamer.id == Vod.streamer_id)
                .where(
                    Segment.status == Status.QUEUED,
                    Streamer.paused.is_not(True),
                    or_(Streamer.uploads_wait_until.is_(None), Streamer.uploads_wait_until <= utcnow()),
                )
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
                raise Blocked(f"YouTube-канал не подключён. {self._reconnect(streamer)}")
            refresh_token = app.vault.decrypt(streamer.youtube_token)
            await update_segment(app, segment_id, status=Status.UPLOADING, error=None)
            await app.refresh_segment(segment_id)
            if seg.kind == SHORT:
                path = await prepare_short(app, seg)
                metadata = await short_metadata(app, seg, streamer)
                video_id = await self._upload(
                    seg,
                    streamer,
                    refresh_token,
                    path.stat().st_size,
                    seg.expected_duration or 0,
                    file_stream(path),
                    metadata,
                )
                path.unlink(missing_ok=True)
            else:
                plan = await self._plan(seg, vod)
                video_id = await self._upload(
                    seg,
                    streamer,
                    refresh_token,
                    plan.total,
                    round(plan.duration),
                    lambda offset: stream(app.http, plan, offset),
                    build_metadata(app, seg, vod, streamer),
                )
        except Blocked as exc:
            await update_segment(app, segment_id, status=Status.QUEUED)
            await set_paused(app, True, streamer.id)
            await app.refresh_segment(segment_id)
            who = streamer_prefix(streamer, app.several)
            await app.notify(f"🔴 {who}Обработка на паузе: {escape(str(exc))}", reply_to=seg.tg_message_id)
            return
        except LimitReached as exc:
            await update_segment(app, segment_id, status=Status.QUEUED)
            await app.refresh_segment(segment_id)
            await self._wait_for_limit(streamer, exc.reason, seg.tg_message_id)
            return
        except Exception as exc:
            if not await self._connected(streamer):
                # Канал отключили во время загрузки: сегмент загрузится заново после нового подключения
                await update_segment(app, segment_id, status=Status.QUEUED, upload_uri=None)
                await app.refresh_segment(segment_id)
                return
            log.exception("Сегмент %s не загружен", segment_id)
            await update_segment(app, segment_id, status=Status.FAILED, error=str(exc)[:1000])
            await app.refresh_segment(segment_id)
            what = "Shorts" if seg.kind == SHORT else "Сегмент"
            await app.notify(
                f"🔴 {what} «{escape(seg.title)}» не загружен: {escape(str(exc)[:500])}",
                reply_to=seg.tg_message_id,
            )
            return

        if not await self._connected(streamer):
            # Ролик загрузился, но канал успели отключить: его ID не сохраняем
            await update_segment(app, segment_id, status=Status.FORGOTTEN, reason=DISCONNECTED_REASON, upload_uri=None)
            await app.refresh_segment(segment_id)
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
        await self._limit_passed(streamer)  # загрузка могла продолжить старую сессию, не открывая новую

    async def _wait_for_limit(self, streamer: Streamer, reason: str, reply_to: int | None) -> None:
        """Загрузки стримера, а если кончилась квота проекта — всех стримеров, ждут и продолжатся сами."""
        app = self.app
        until = limits.retry_at(reason, utcnow())
        query = update(Streamer).values(uploads_wait_until=until, uploads_wait_reason=reason)
        if not limits.affects_everyone(reason):
            query = query.where(Streamer.id == streamer.id)
        async with app.sessions() as session, session.begin():
            await session.execute(query)
        when = local_time(until, app.tz, "%H:%M")
        log.info("YouTube не принимает загрузки на канал %s (%s), следующая попытка в %s", streamer.login, reason, when)
        if streamer.uploads_wait_until is not None:
            return  # о лимите уже сообщили, это очередная попытка
        channel = escape(streamer.youtube_channel_title or streamer.public_name)
        if reason == limits.CHANNEL_LIMIT:
            text = (
                f"⏳ YouTube не принимает новые загрузки на канал «{channel}»: исчерпан дневной лимит канала. "
                f"Готовые ролики публикуются как обычно, а загрузку я продолжу сам, следующая попытка в {when}."
            )
        elif limits.affects_everyone(reason):
            text = f"⏳ Кончилась суточная квота YouTube API. Загрузки продолжатся сами в {when}, когда квота обновится."
        else:
            text = f"⏳ YouTube просит загружать реже ({escape(reason)}). Продолжу загрузку в {when}."
        await app.notify(text, reply_to=reply_to, silent=True)

    async def _limit_passed(self, streamer: Streamer) -> None:
        """YouTube снова принял загрузку: ожидание лимита закончилось."""
        async with self.app.sessions() as session, session.begin():
            result = await session.execute(
                update(Streamer)
                .where(Streamer.id == streamer.id, Streamer.uploads_wait_until.is_not(None))
                .values(uploads_wait_until=None, uploads_wait_reason=None)
            )
        if result.rowcount:
            log.info("YouTube снова принимает загрузки на канал %s", streamer.login)
            channel = escape(streamer.youtube_channel_title or streamer.public_name)
            await self.app.notify(f"▶️ YouTube снова принимает загрузки на канал «{channel}».", silent=True)

    def _reconnect(self, streamer: Streamer) -> str:
        """Что сделать владельцу, чтобы загрузка пошла снова; когда стримеров несколько — команды с логином."""
        several = self.app.several
        return f"Выполните {command_for('/youtube', streamer, several)}, затем {command_for('/resume', streamer, several)}"

    async def _connected(self, streamer: Streamer) -> bool:
        """Подключён ли ещё канал, на который шла загрузка. Повторный вход в тот же канал — не отключение."""
        async with self.app.sessions() as session:
            current = await session.get(Streamer, streamer.id)
        return current.youtube_token is not None and current.youtube_channel_id == streamer.youtube_channel_id

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

    async def _upload(
        self,
        seg: Segment,
        streamer: Streamer,
        refresh_token: str,
        total: int,
        duration: int,
        open_stream: StreamFactory,
        metadata: dict,
    ) -> str:
        """Загружает ролик с докачкой: open_stream(offset) каждый раз отдаёт одни и те же байты."""
        app = self.app
        # Если источник изменился (например, Twitch заглушил фрагменты VOD), старую сессию не продолжить
        session_uri = seg.upload_uri if seg.upload_total == total else None
        await update_segment(
            app,
            seg.id,
            upload_total=total,
            expected_duration=duration,
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
            try:
                await update_segment(app, seg.id, progress=percent)
                await app.refresh_segment(seg.id)
            except Exception:
                # Проценты — только для глаз: из-за них нельзя потерять уже загруженный ролик
                log.warning("Не удалось сохранить прогресс сегмента %s", seg.id, exc_info=True)

        async def on_session(uri: str) -> None:
            await update_segment(app, seg.id, upload_uri=uri)
            # YouTube открыл новую сессию загрузки — значит, лимит больше не мешает
            try:
                await self._limit_passed(streamer)
            except Exception:
                log.warning("Не удалось отметить конец ожидания лимита YouTube", exc_info=True)

        try:
            return await app.youtube.upload_stream(
                refresh_token,
                total,
                open_stream,
                metadata,
                session_uri=session_uri,
                on_session=on_session,
                on_progress=on_progress,
                chunk_size=app.settings.upload_chunk_mb * 1024 * 1024,
                limit_mbit=app.settings.upload_limit_mbit,
            )
        except AuthError as exc:
            raise Blocked(f"доступ к YouTube отозван или истёк. {self._reconnect(streamer)}") from exc
        except YouTubeError as exc:
            if exc.reason in LIMIT_REASONS:
                raise LimitReached(exc.reason) from exc
            if exc.reason == "youtubeSignupRequired":
                connect = command_for("/youtube", streamer, app.several)
                raise Blocked(f"у Google-аккаунта нет YouTube-канала: создайте канал и выполните {connect}") from exc
            raise
