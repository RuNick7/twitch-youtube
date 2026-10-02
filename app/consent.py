"""Согласие владельца канала с политикой конфиденциальности.

Правила YouTube API: если бот начинает использовать данные так, как политика
не описывала, политику обновляют и снова просят согласия. Версия принятой
политики хранится у стримера, чей канал подключён.
"""

from __future__ import annotations

from sqlalchemy import update

from .context import App
from .db import Streamer, find_streamer
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


def accepted_version(streamer: Streamer) -> int:
    """Версия политики, которую принял владелец канала; 0 — канал подключали до появления версий."""
    return streamer.consent_version or 0


async def accept(app: App, streamer_id: int, version: int = CONSENT_VERSION) -> None:
    """Владелец канала принял версию политики, которую ему показали."""
    async with app.sessions() as session, session.begin():
        streamer = await session.get(Streamer, streamer_id)
        streamer.consent_version = max(accepted_version(streamer), version)
    app.sync_wake.set()
    app.shorts_wake.set()


async def prompt_update(app: App) -> None:
    """Если канал подключён по старой версии политики, один раз просит принять новую."""
    async with app.sessions() as session:
        streamer = await find_streamer(session, app.settings.twitch_channel)
    if not (streamer and streamer.youtube_token) or streamer.consent_prompted == CONSENT_VERSION:
        return
    accepted = accepted_version(streamer)
    if accepted >= CONSENT_VERSION:
        return
    name = streamer.display_name or streamer.login
    changes = [text for version, text in sorted(CHANGES.items()) if version > accepted]
    message = await app.notify(
        render_consent(app.config(streamer), name, changes=changes),
        markup=confirm_keyboard("✅ Принимаю", accept_action(CONSENT_VERSION)),
    )
    if message:
        async with app.sessions() as session, session.begin():
            await session.execute(
                update(Streamer).where(Streamer.id == streamer.id).values(consent_prompted=CONSENT_VERSION)
            )
