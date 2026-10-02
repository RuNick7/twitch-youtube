"""Проверка загруженных роликов: обработка, предупреждения, автопубликация и наблюдение после неё."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape

import httpx
from sqlalchemy import select

from .checks import FAILED, READY, REJECTED, failure_reason, processing_state, youtube_warnings
from .context import App
from .db import SHORT, Segment, Status, Streamer, Vod, as_utc, dump_warnings, get_warnings, utcnow
from .service import MONITOR_EVERY, publish, update_segment
from .ui import PRIVACY_NAMES
from .worker import is_paused, set_paused
from .youtube import AuthError, YouTubeError

log = logging.getLogger(__name__)

TICK_SEC = 60
RECHECK = timedelta(minutes=5)
AUTH_BACKOFF = timedelta(minutes=10)


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


class Checker:
    def __init__(self, app: App):
        self.app = app
        # После отзыва доступа к каналу не дёргать API каждую минуту: ID стримера → когда пробовать снова
        self.skip_until: dict[int, datetime] = {}

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Сбой проверки роликов на YouTube")
            await asyncio.sleep(TICK_SEC)

    async def tick(self) -> None:
        now = utcnow()
        async with self.app.sessions() as session:
            rows = (
                await session.execute(
                    select(Segment, Streamer)
                    .join(Vod, Vod.id == Segment.vod_id)
                    .join(Streamer, Streamer.id == Vod.streamer_id)
                    .where(
                        Segment.status.in_((Status.PROCESSING, Status.WAITING, Status.PUBLISHED)),
                        Segment.check_at.is_not(None),
                        Segment.check_at <= now,
                    )
                    .order_by(Segment.check_at)
                )
            ).all()
        groups: dict[int, tuple[Streamer, list[Segment]]] = {}
        for seg, streamer in rows:
            groups.setdefault(streamer.id, (streamer, []))[1].append(seg)

        for streamer, segments in groups.values():
            if not streamer.youtube_token or now < self.skip_until.get(streamer.id, now):
                continue
            token = self.app.vault.decrypt(streamer.youtube_token)
            try:
                items = await self.app.youtube.videos(token, [seg.youtube_id for seg in segments])
            except AuthError:
                await self._auth_lost(streamer)
                continue
            except (YouTubeError, httpx.HTTPError) as exc:
                log.warning("Не удалось проверить ролики на YouTube: %s", exc)
                continue
            for seg in segments:
                try:
                    await self.check(seg, items.get(seg.youtube_id), streamer)
                except AuthError:
                    await self._auth_lost(streamer)
                    break
                except (YouTubeError, httpx.HTTPError) as exc:
                    log.warning("Не удалось обработать ролик %s: %s", seg.youtube_id, exc)
                    await update_segment(self.app, seg.id, check_at=utcnow() + RECHECK)

    async def _auth_lost(self, streamer: Streamer) -> None:
        """Доступ к каналу пропал: на паузу встаёт только этот стример."""
        self.skip_until[streamer.id] = utcnow() + AUTH_BACKOFF
        if not await is_paused(self.app, streamer.id):
            await set_paused(self.app, True, streamer.id)
            await self.app.notify(
                "🔴 Обработка на паузе: доступ к YouTube отозван или истёк. Выполните /youtube, затем /resume"
            )

    async def check(self, seg: Segment, item: dict | None, streamer: Streamer) -> None:
        app = self.app
        settings = app.config(streamer)
        now = utcnow()
        if item is None:
            await self._reject(seg, "ролик удалён с YouTube")
            return
        state = processing_state(item)
        if state in (REJECTED, FAILED):
            await self._reject(seg, failure_reason(item))
            return

        if seg.status == Status.PROCESSING:
            if state == READY:
                delay = settings.shorts_publish_delay_min if seg.kind == SHORT else settings.publish_delay_min
                publish_after = now + timedelta(minutes=delay)
                await update_segment(app, seg.id, status=Status.WAITING, publish_after=publish_after, check_at=publish_after)
                await app.refresh_segment(seg.id)
            else:
                await update_segment(app, seg.id, check_at=now + RECHECK)
            return

        if seg.status == Status.WAITING:
            warnings = get_warnings(seg) + youtube_warnings(item, seg.expected_duration)
            if seg.force_review:
                warnings.append(f"загружен вручную, хотя фильтр его пропустил: {seg.reason or 'причина не сохранилась'}")
            if not warnings and not settings.auto_publish:
                warnings.append("автопубликация выключена (AUTO_PUBLISH=false)")
            if warnings:
                warnings = _unique(warnings)
                await update_segment(app, seg.id, status=Status.REVIEW, warnings=dump_warnings(warnings), check_at=None)
                await app.refresh_segment(seg.id)
                lines = "\n".join(f"• {escape(w)}" for w in warnings)
                await app.notify(
                    f"⚠️ «{escape(seg.title)}»: нужно решение, публиковать ли ролик.\n{lines}",
                    reply_to=seg.tg_message_id,
                )
                return
            if await is_paused(app, streamer.id):
                await update_segment(app, seg.id, check_at=now + RECHECK)
                return
            await publish(app, seg.id)
            return

        if seg.status == Status.PUBLISHED:
            problems = youtube_warnings(item, None)
            privacy = (item.get("status") or {}).get("privacyStatus")
            if privacy != settings.publish_privacy:
                problems.append(f"ролик стал {PRIVACY_NAMES.get(privacy, privacy)}")
            known = get_warnings(seg)
            new = [problem for problem in problems if problem not in known]
            next_check = now + MONITOR_EVERY
            values: dict = {"check_at": next_check if seg.monitor_until and next_check <= as_utc(seg.monitor_until) else None}
            if new:
                values["warnings"] = dump_warnings(known + new)
            await update_segment(app, seg.id, **values)
            if new:
                await app.refresh_segment(seg.id)
                lines = "\n".join(f"• {escape(p)}" for p in new)
                await app.notify(
                    f"🔴 С опубликованным роликом «{escape(seg.title)}» проблема:\n{lines}",
                    reply_to=seg.tg_message_id,
                )

    async def _reject(self, seg: Segment, reason: str) -> None:
        await update_segment(self.app, seg.id, status=Status.REJECTED, reason=reason, check_at=None)
        await self.app.refresh_segment(seg.id)
        await self.app.notify(
            f"🔴 «{escape(seg.title)}»: YouTube отклонил ролик — {escape(reason)}.",
            reply_to=seg.tg_message_id,
        )
