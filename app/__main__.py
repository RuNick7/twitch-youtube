"""Точка входа: бот, очередь обработки и ежедневное обновление yt-dlp в одном процессе."""

from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import async_sessionmaker

from .bot import router
from .config import Settings
from .context import App
from .crypto import Vault
from .db import get_streamer, init_db, make_engine
from .tools import update_ytdlp, ytdlp_version
from .worker import Worker
from .youtube import YouTubeClient

log = logging.getLogger("app")

YTDLP_UPDATE_INTERVAL = 24 * 3600


async def keep_ytdlp_fresh(app: App) -> None:
    """Twitch периодически ломает скачивание, а исправления приходят со свежими версиями yt-dlp."""
    while True:
        await asyncio.sleep(YTDLP_UPDATE_INTERVAL)
        await update_ytdlp()
        app.ytdlp_version = await ytdlp_version()
        log.info("yt-dlp: %s", app.ytdlp_version)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    settings.work_dir.mkdir(parents=True, exist_ok=True)

    engine = make_engine(settings.db_path)
    await init_db(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await get_streamer(session, settings.twitch_channel)

    bot = Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        app = App(
            settings=settings,
            sessions=sessions,
            bot=bot,
            youtube=YouTubeClient(settings.google_client_id, settings.google_client_secret, http),
            vault=Vault(settings.secret_key),
            tz=ZoneInfo(settings.tz),
        )
        app.ytdlp_version = await ytdlp_version()
        log.info("yt-dlp: %s", app.ytdlp_version)

        dispatcher = Dispatcher()
        dispatcher["app"] = app
        dispatcher.include_router(router)

        app.spawn(Worker(app).run())
        app.spawn(keep_ytdlp_fresh(app))
        if app.owner_id is None:
            log.warning("TELEGRAM_OWNER_ID не задан: напишите боту /start, чтобы узнать свой ID")
        else:
            await app.notify("🟢 Бот запущен", silent=True)
        try:
            await dispatcher.start_polling(bot)
        finally:
            await bot.session.close()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
