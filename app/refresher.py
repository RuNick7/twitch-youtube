"""Раз в сутки: действует ли доступ к YouTube и не устарели ли сохранённые данные.
Между сверками — раскладка опубликованных роликов по плейлистам.

Правила YouTube API требуют обновлять сохранённые данные не реже раза в 30 дней,
а после отзыва доступа удалять их (в политике конфиденциальности обещано 7 дней).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import httpx
from sqlalchemy import or_, select, update

from .context import App
from .db import Segment, Status, Streamer, Vod, utcnow
from .playlists import sync_playlists, verify_playlists
from .service import DELETED_REASON, PUBLISHABLE, REVOKED_REASON, YOUTUBE_FIELDS, forget_youtube, set_status
from .ui import command_for, streamer_prefix
from .worker import set_paused
from .youtube import AuthError, YouTubeError

log = logging.getLogger(__name__)

CHECK_EVERY = timedelta(days=1)
TICK_SEC = 600
# Ролики, за которыми уже не следит Checker: их достаточно сверять раз в сутки
SETTLED = (Status.REVIEW, Status.PUBLISHED, Status.PRIVATE, Status.LOCKED, Status.REJECTED)
VISIBLE = ("public", "unlisted")


class Refresher:
    def __init__(self, app: App):
        self.app = app

    async def run(self) -> None:
        while True:
            self.app.sync_wake.clear()
            try:
                await self.refresh()
                await sync_playlists(self.app)
            except Exception:
                log.exception("Сбой сверки с YouTube")
            try:
                # Публикация будит раньше: ролик сразу попадает в плейлист
                await asyncio.wait_for(self.app.sync_wake.wait(), timeout=TICK_SEC)
            except asyncio.TimeoutError:
                pass

    async def refresh(self) -> None:
        """Сверяет каналы, которые не сверялись больше суток."""
        async with self.app.sessions() as session:
            streamers = list(
                (
                    await session.scalars(
                        select(Streamer).where(
                            Streamer.youtube_token.is_not(None),
                            or_(
                                Streamer.youtube_checked_at.is_(None),
                                Streamer.youtube_checked_at <= utcnow() - CHECK_EVERY,
                            ),
                        )
                    )
                ).all()
            )
        for streamer in streamers:
            if await self._refresh(streamer):
                async with self.app.sessions() as session, session.begin():
                    await session.execute(
                        update(Streamer).where(Streamer.id == streamer.id).values(youtube_checked_at=utcnow())
                    )

    async def _refresh(self, streamer: Streamer) -> bool:
        """Сверяет канал и его ролики. False — Google не ответил, повторим позже."""
        app = self.app
        token = app.vault.decrypt(streamer.youtube_token)
        async with app.sessions() as session:
            rows = (
                await session.execute(
                    select(Segment.id, Segment.youtube_id, Segment.status)
                    .join(Vod, Vod.id == Segment.vod_id)
                    .where(
                        Vod.streamer_id == streamer.id,
                        Segment.youtube_id.is_not(None),
                        Segment.status.in_(SETTLED),
                        or_(Segment.status != Status.PUBLISHED, Segment.check_at.is_(None)),
                    )
                )
            ).all()
        try:
            channel_id, title = await app.youtube.my_channel(token)
            items = await app.youtube.videos(token, [youtube_id for _, youtube_id, _ in rows]) if rows else {}
            await verify_playlists(app, streamer, token)
        except AuthError as exc:
            if exc.reason != "invalid_grant":
                log.warning("Google не принял ключи приложения: %s", exc)
                return False
            await set_paused(app, True, streamer.id)
            await forget_youtube(app, streamer.id, REVOKED_REASON)
            await app.notify(
                f"🔴 {streamer_prefix(streamer, app.several)}Доступ к YouTube отозван. AutoVOD удалил сохранённые "
                "данные: токен, ID и название канала, ID и состояние роликов. Сами ролики на YouTube не тронуты. "
                f"Подключить канал снова — {command_for('/youtube', streamer, app.several)}"
            )
            return True
        except (YouTubeError, httpx.HTTPError) as exc:
            log.warning("Сверка с YouTube не удалась: %s", exc)
            return False

        async with app.sessions() as session, session.begin():
            current = await session.get(Streamer, streamer.id)
            if current.youtube_token == streamer.youtube_token:  # канал не переподключили, пока шли запросы
                current.youtube_channel_id, current.youtube_channel_title = channel_id, title
        for segment_id, youtube_id, status in rows:
            item = items.get(youtube_id)
            if item is None:
                changed = await set_status(
                    app, segment_id, SETTLED, Status.FORGOTTEN, reason=DELETED_REASON, **YOUTUBE_FIELDS
                )
            else:
                # Доступ к ролику могли поменять вручную в YouTube Studio
                privacy = (item.get("status") or {}).get("privacyStatus")
                if status == Status.PUBLISHED and privacy == "private":
                    changed = await set_status(app, segment_id, (status,), Status.PRIVATE)
                elif status in PUBLISHABLE + (Status.LOCKED,) and privacy in VISIBLE:
                    changed = await set_status(app, segment_id, (status,), Status.PUBLISHED, published_at=utcnow())
                else:
                    changed = False
            if changed:
                await app.refresh_segment(segment_id)
        log.info("Сверка с YouTube: канал «%s», роликов %s, удалено с YouTube %s", title, len(rows), len(rows) - len(items))
        return True
