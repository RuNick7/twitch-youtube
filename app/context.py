"""Общий контекст приложения: настройки, база, бот, HTTP-клиент и клиент YouTube."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Coroutine
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings, streamer_settings
from .crypto import Vault
from .db import Segment, Streamer, Vod
from .ui import render_segment
from .youtube import YouTubeClient

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    bot: Bot
    http: httpx.AsyncClient
    youtube: YouTubeClient
    vault: Vault
    tz: ZoneInfo
    wake: asyncio.Event = field(default_factory=asyncio.Event)  # будит очередь загрузки
    sync_wake: asyncio.Event = field(default_factory=asyncio.Event)  # будит раскладку роликов по плейлистам
    shorts_wake: asyncio.Event = field(default_factory=asyncio.Event)  # будит проверку клипов для Shorts
    ytdlp_version: str = "?"
    # Для /status, по ID стримера: что видно на его канале и название идущего стрима, если он идёт
    watch_states: dict[int, str] = field(default_factory=dict)
    live_titles: dict[int, str] = field(default_factory=dict)
    tasks: set[asyncio.Task] = field(default_factory=set)

    @property
    def owner_id(self) -> int | None:
        return self.settings.telegram_owner_id

    def config(self, streamer: Streamer) -> Settings:
        """Настройки стримера: общие из .env с его отличиями."""
        return streamer_settings(self.settings, streamer.overrides)

    def spawn(self, coro: Coroutine) -> asyncio.Task:
        """Фоновая задача; ссылка хранится, чтобы её не собрал сборщик мусора."""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def notify(
        self,
        text: str,
        *,
        markup: InlineKeyboardMarkup | None = None,
        reply_to: int | None = None,
        silent: bool = False,
    ) -> Message | None:
        if self.owner_id is None:
            return None
        reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
        for attempt in range(1, 4):
            try:
                return await self.bot.send_message(
                    self.owner_id, text, reply_markup=markup, reply_parameters=reply, disable_notification=silent
                )
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
            except TelegramNetworkError as exc:
                log.warning("Telegram недоступен (%s), попытка %s", exc, attempt)
                await asyncio.sleep(2**attempt)
            except TelegramBadRequest as exc:
                log.warning("Telegram отклонил сообщение: %s", exc)
                return None
        log.error("Не удалось отправить сообщение в Telegram")
        return None

    async def refresh_segment(self, segment_id: int) -> None:
        """Перерисовывает сообщение сегмента под его текущий статус."""
        async with self.sessions() as session:
            seg = await session.get(Segment, segment_id)
            if seg is None or seg.tg_message_id is None or self.owner_id is None:
                return
            vod = await session.get(Vod, seg.vod_id)
            streamer = await session.get(Streamer, vod.streamer_id)
        text, markup = render_segment(seg, vod, streamer, self.tz, self.config(streamer).publish_privacy)
        try:
            await self.bot.edit_message_text(
                text=text, chat_id=self.owner_id, message_id=seg.tg_message_id, reply_markup=markup
            )
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc):
                log.warning("Не удалось обновить сообщение сегмента %s: %s", segment_id, exc)
        except (TelegramNetworkError, TelegramRetryAfter) as exc:
            log.warning("Не удалось обновить сообщение сегмента %s: %s", segment_id, exc)
