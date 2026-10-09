"""Управление стримерами из бота: добавить, убрать, переименовать, задать свои настройки."""

from __future__ import annotations

from sqlalchemy import update

from .config import change_override
from .context import App
from .db import Streamer, all_streamers, find_streamer, get_streamer, utcnow


async def watched(app: App) -> list[Streamer]:
    """Стримеры, за которыми бот следит. Заодно обновляет app.several."""
    async with app.sessions() as session:
        streamers = await all_streamers(session, watched=True)
    app.several = len(streamers) > 1
    return streamers


async def first(app: App) -> Streamer:
    """Стример из TWITCH_CHANNEL: к нему относятся кнопки под сообщениями, отправленными, когда он был один."""
    async with app.sessions() as session, session.begin():
        return await get_streamer(session, app.settings.twitch_channel)


async def add(app: App, login: str, display_name: str | None, title_name: str | None) -> tuple[Streamer, str]:
    """Добавляет стримера или возвращает убранного; владелец уже подтвердил, что разрешение стримера есть.

    Второе значение — added, restored или exists.
    """
    async with app.sessions() as session, session.begin():
        streamer = await find_streamer(session, login)
        now = utcnow()
        if streamer is None:
            streamer = await get_streamer(session, login)
            # Эфиры, закончившиеся до этой минуты, бот сам не трогает: их можно отдать через /process
            streamer.watch_since = now
            outcome = "added"
        elif streamer.removed_at is not None:
            streamer.removed_at = None
            streamer.watch_since = now
            outcome = "restored"
        else:
            outcome = "exists"
        streamer.permitted_at = streamer.permitted_at or now
        if display_name:
            streamer.display_name = display_name
        if title_name:
            streamer.title_name = title_name
    await watched(app)
    return streamer, outcome


async def remove(app: App, streamer_id: int) -> None:
    """Бот перестаёт следить за каналом и клипами стримера. Его ролики и подключённый канал остаются."""
    async with app.sessions() as session, session.begin():
        await session.execute(update(Streamer).where(Streamer.id == streamer_id).values(removed_at=utcnow()))
    app.watch_states.pop(streamer_id, None)
    app.live_titles.pop(streamer_id, None)
    await watched(app)


async def rename(app: App, streamer_id: int, title_name: str) -> None:
    async with app.sessions() as session, session.begin():
        await session.execute(update(Streamer).where(Streamer.id == streamer_id).values(title_name=title_name))


async def set_setting(app: App, streamer_id: int, name: str, value: str | None) -> Streamer:
    """Задаёт стримеру свою настройку (value=None — вернуть общую из .env). ValueError — так задать нельзя."""
    async with app.sessions() as session, session.begin():
        streamer = await session.get(Streamer, streamer_id)
        streamer.overrides = change_override(streamer.overrides, name, value)
    app.shorts_wake.set()  # Shorts могли включить или изменить их правила
    app.sync_wake.set()
    return streamer
