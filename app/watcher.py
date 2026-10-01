"""Слежение за каналом: после конца стрима VOD сам уходит в обработку.

Ключи Twitch API не нужны: список записей эфиров и признак «идёт сейчас» берутся
через yt-dlp — тот же источник, откуда берутся главы и плейлист. Название стрима
во время эфира — лёгким запросом к GraphQL Twitch (см. twitch.py).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import select

from .context import App
from .db import TitleChange, Vod, as_utc, kv_get, kv_set, utcnow
from .service import IngestError, ingest_vod
from .tools import fetch_vod_info, list_channel_vods
from .twitch import client_id, live_state
from .ui import local_time

log = logging.getLogger(__name__)

SINCE_KEY = "watch_since"
ALERT_AFTER_FAILURES = 3
TITLE_ALERT_AFTER_FAILURES = 10


class Watcher:
    def __init__(self, app: App):
        self.app = app
        self.done: set[str] = set()  # уже обработаны или закончились до начала слежения
        self.failures = 0

    async def run(self) -> None:
        since = await self._since()
        while True:
            try:
                await self.tick(since)
                self.failures = 0
            except Exception as exc:
                self.failures += 1
                log.warning("Не удалось проверить канал: %s", exc)
                self.app.watch_state = f"ошибка проверки: {escape(str(exc)[:200])}"
                if self.failures == ALERT_AFTER_FAILURES:
                    await self.app.notify(
                        f"🔴 Не получается проверить канал Twitch {ALERT_AFTER_FAILURES} раза подряд: "
                        f"{escape(str(exc)[:300])}"
                    )
            await asyncio.sleep(self.app.settings.watch_interval_sec)

    async def _since(self) -> datetime:
        """С какого момента следим. Эфиры, закончившиеся раньше, не трогаем: их можно отдать через /process."""
        async with self.app.sessions() as session, session.begin():
            value = await kv_get(session, SINCE_KEY)
            if value:
                return as_utc(datetime.fromisoformat(value))
            now = utcnow()
            await kv_set(session, SINCE_KEY, now.isoformat())
            return now

    async def tick(self, since: datetime) -> None:
        app = self.app
        grace = timedelta(minutes=app.settings.watch_grace_min)
        live = waiting = None
        for vod_id in reversed(await list_channel_vods(app.settings.twitch_channel)):
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
            log.info("Стрим закончился, обрабатываю VOD %s", vod_id)
            try:
                await ingest_vod(app, info)
            except IngestError as exc:
                log.warning("VOD %s не обработан: %s", vod_id, exc)
            self.done.add(vod_id)

        checked = local_time(utcnow(), app.tz, "%H:%M")
        if live:
            app.watch_state = f"идёт стрим «{escape(live.title)}», проверено в {checked}"
        elif waiting:
            app.watch_state = f"стрим закончился, жду {app.settings.watch_grace_min} мин на случай переподключения"
        else:
            app.watch_state = f"стрима нет, проверено в {checked}"


class TitleTracker:
    """Пока идёт стрим, записывает смены его названия: в данных VOD их нет, а это граница сегмента."""

    def __init__(self, app: App):
        self.app = app
        self.client: str | None = None
        self.last: tuple[str, str] | None = None  # (ID стрима, название), уже записанные в базу
        self.failures = 0

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                self.failures = 0
            except Exception as exc:
                self.failures += 1
                self.client = None  # вдруг Twitch сменил ID клиента: возьмём свежий у yt-dlp
                log.warning("Не удалось узнать название стрима: %s", exc)
                if self.failures == TITLE_ALERT_AFTER_FAILURES:
                    await self.app.notify(
                        f"🟡 Не получается узнать название стрима {TITLE_ALERT_AFTER_FAILURES} раз подряд: "
                        f"{escape(str(exc)[:300])}. Пока это так, нарезка идёт только по категориям."
                    )
            await asyncio.sleep(self.app.settings.title_poll_sec)

    async def tick(self) -> None:
        if self.client is None:
            self.client = await client_id()
        channel = self.app.settings.twitch_channel.lower()
        state = await live_state(self.app.http, channel, self.client)
        if state is None:
            self.last = None
            self.app.live_title = None
            return
        self.app.live_title = state.title
        if self.last == (state.stream_id, state.title):
            return
        async with self.app.sessions() as session, session.begin():
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
                log.info("Название стрима: %s", state.title)
        self.last = (state.stream_id, state.title)
