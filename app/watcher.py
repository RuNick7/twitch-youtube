"""Слежение за каналами стримеров: после конца стрима VOD сам уходит в обработку.

Ключи Twitch API не нужны: список записей эфиров и признак «идёт сейчас» берутся
через yt-dlp — тот же источник, откуда берутся главы и плейлист. Названия идущих
стримов — одним лёгким запросом к GraphQL Twitch на всех стримеров (см. twitch.py).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import select, update

from .context import App
from .db import Streamer, TitleChange, Vod, all_streamers, as_utc, utcnow
from .service import IngestError, ingest_vod
from .tools import fetch_vod_info, list_channel_vods
from .twitch import LiveState, client_id, live_states
from .ui import local_time

log = logging.getLogger(__name__)

ALERT_AFTER_FAILURES = 3
TITLE_ALERT_AFTER_FAILURES = 10


class Watcher:
    """Проверяет каналы всех стримеров по очереди."""

    def __init__(self, app: App):
        self.app = app
        self.done: set[str] = set()  # уже обработаны или закончились до начала слежения
        self.failures: dict[int, int] = {}  # ошибок подряд по ID стримера

    async def run(self) -> None:
        while True:
            async with self.app.sessions() as session:
                streamers = await all_streamers(session)
            for streamer in streamers:
                await self._check(streamer)
            await asyncio.sleep(self.app.settings.watch_interval_sec)

    async def _check(self, streamer: Streamer) -> None:
        """Сбой на одном канале не мешает проверить остальные."""
        try:
            await self.tick(streamer)
            self.failures[streamer.id] = 0
        except Exception as exc:
            failures = self.failures[streamer.id] = self.failures.get(streamer.id, 0) + 1
            log.warning("Не удалось проверить канал %s: %s", streamer.login, exc)
            self.app.watch_states[streamer.id] = f"ошибка проверки: {escape(str(exc)[:200])}"
            if failures == ALERT_AFTER_FAILURES:
                await self.app.notify(
                    f"🔴 Не получается проверить канал Twitch {escape(streamer.login)} {ALERT_AFTER_FAILURES} раза "
                    f"подряд: {escape(str(exc)[:300])}"
                )

    async def _since(self, streamer: Streamer) -> datetime:
        """С какого момента следим за каналом. Эфиры, закончившиеся раньше, не трогаем: их можно отдать через /process."""
        if streamer.watch_since is None:
            streamer.watch_since = utcnow()
            async with self.app.sessions() as session, session.begin():
                await session.execute(
                    update(Streamer).where(Streamer.id == streamer.id).values(watch_since=streamer.watch_since)
                )
        return as_utc(streamer.watch_since)

    async def tick(self, streamer: Streamer) -> None:
        app = self.app
        since = await self._since(streamer)
        grace = timedelta(minutes=app.settings.watch_grace_min)
        live = waiting = None
        for vod_id in reversed(await list_channel_vods(streamer.login)):
            if vod_id in self.done:
                continue
            async with app.sessions() as session:
                if await session.get(Vod, vod_id):
                    self.done.add(vod_id)
                    continue
            info = await fetch_vod_info(vod_id)
            if info.is_live:
                live = info
                continue
            ended = (info.started_at or utcnow()) + timedelta(seconds=info.duration)
            if ended < since:
                self.done.add(vod_id)
                continue
            if utcnow() - ended < grace:
                waiting = ended  # вдруг стример переподключится
                continue
            log.info("Стрим %s закончился, обрабатываю VOD %s", streamer.login, vod_id)
            try:
                await ingest_vod(app, info, streamer.login)
            except IngestError as exc:
                log.warning("VOD %s не обработан: %s", vod_id, exc)
            self.done.add(vod_id)

        checked = local_time(utcnow(), app.tz, "%H:%M")
        if live:
            state = f"идёт стрим «{escape(live.title)}», проверено в {checked}"
        elif waiting:
            state = f"стрим закончился, жду {app.settings.watch_grace_min} мин на случай переподключения"
        else:
            state = f"стрима нет, проверено в {checked}"
        app.watch_states[streamer.id] = state


class TitleTracker:
    """Пока идёт стрим, записывает смены его названия: в данных VOD их нет, а это граница сегмента.

    Про всех стримеров Twitch спрашивается одним запросом.
    """

    def __init__(self, app: App):
        self.app = app
        self.client: str | None = None
        self.last: dict[int, tuple[str, str]] = {}  # ID стримера → (ID стрима, название), уже записанные в базу
        self.failures = 0

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                self.failures = 0
            except Exception as exc:
                self.failures += 1
                self.client = None  # вдруг Twitch сменил ID клиента: возьмём свежий у yt-dlp
                log.warning("Не удалось узнать названия стримов: %s", exc)
                if self.failures == TITLE_ALERT_AFTER_FAILURES:
                    await self.app.notify(
                        f"🟡 Не получается узнать название стрима {TITLE_ALERT_AFTER_FAILURES} раз подряд: "
                        f"{escape(str(exc)[:300])}. Пока это так, нарезка идёт только по категориям."
                    )
            await asyncio.sleep(self.app.settings.title_poll_sec)

    async def tick(self) -> None:
        if self.client is None:
            self.client = await client_id()
        async with self.app.sessions() as session:
            streamers = await all_streamers(session)
        states = await live_states(self.app.http, [streamer.login for streamer in streamers], self.client)
        for streamer in streamers:
            await self._track(streamer, states.get(streamer.login))

    async def _track(self, streamer: Streamer, state: LiveState | None) -> None:
        app = self.app
        if state is None:
            self.last.pop(streamer.id, None)
            app.live_titles.pop(streamer.id, None)
            return
        app.live_titles[streamer.id] = state.title
        if self.last.get(streamer.id) == (state.stream_id, state.title):
            return
        channel = streamer.login
        async with app.sessions() as session, session.begin():
            previous = await session.scalar(
                select(TitleChange)
                .where(TitleChange.channel == channel, TitleChange.stream_id == state.stream_id)
                .order_by(TitleChange.at.desc())
                .limit(1)
            )
            if previous is None or previous.title != state.title:
                session.add(
                    TitleChange(
                        channel=channel,
                        stream_id=state.stream_id,
                        stream_started_at=state.started_at,
                        at=utcnow(),
                        title=state.title,
                    )
                )
                log.info("Название стрима %s: %s", channel, state.title)
        self.last[streamer.id] = (state.stream_id, state.title)
