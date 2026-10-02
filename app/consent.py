"""Согласие владельца канала с политикой конфиденциальности.

Правила YouTube API: если бот начинает использовать данные так, как политика
не описывала, политику обновляют и снова просят согласия. Версия принятой
политики хранится в базе.
"""

from __future__ import annotations

from .context import App
from .db import find_streamer, kv_delete, kv_get, kv_set
from .ui import accept_action, confirm_keyboard, render_consent

# 1 — загрузка и публикация роликов, 2 — плейлисты по категориям, 3 — Shorts из клипов
CONSENT_VERSION = 3
PLAYLISTS_SINCE = 2
SHORTS_SINCE = 3
# Что нового в версии: это бот перечисляет, когда просит принять обновлённую политику
CHANGES = {
    PLAYLISTS_SINCE: "раскладывает опубликованные ролики по плейлистам категорий",
    SHORTS_SINCE: "делает Shorts из самых просматриваемых клипов канала",
}
ACCEPTED_KEY = "consent_version"
PROMPTED_KEY = "consent_prompted"


async def accepted_version(app: App) -> int:
    """Версия политики, которую принял владелец; 0 — канал подключали до появления версий."""
    async with app.sessions() as session:
        value = await kv_get(session, ACCEPTED_KEY)
    return int(value) if value else 0


async def accept(app: App, version: int = CONSENT_VERSION) -> None:
    """Владелец принял версию политики, которую ему показали."""
    current = await accepted_version(app)
    async with app.sessions() as session, session.begin():
        await kv_set(session, ACCEPTED_KEY, str(max(current, version)))
    app.sync_wake.set()
    app.shorts_wake.set()


async def forget(app: App) -> None:
    """Канал отключён: согласие нужно получить заново при следующем подключении."""
    async with app.sessions() as session, session.begin():
        await kv_delete(session, ACCEPTED_KEY)
        await kv_delete(session, PROMPTED_KEY)


async def prompt_update(app: App) -> None:
    """Если канал подключён по старой версии политики, один раз просит принять новую."""
    async with app.sessions() as session:
        streamer = await find_streamer(session, app.settings.twitch_channel)
        prompted = await kv_get(session, PROMPTED_KEY)
    if not (streamer and streamer.youtube_token) or prompted == str(CONSENT_VERSION):
        return
    accepted = await accepted_version(app)
    if accepted >= CONSENT_VERSION:
        return
    name = streamer.display_name or app.settings.twitch_channel
    changes = [text for version, text in sorted(CHANGES.items()) if version > accepted]
    message = await app.notify(
        render_consent(app.settings, name, changes=changes),
        markup=confirm_keyboard("✅ Принимаю", accept_action(CONSENT_VERSION)),
    )
    if message:
        async with app.sessions() as session, session.begin():
            await kv_set(session, PROMPTED_KEY, str(CONSENT_VERSION))
