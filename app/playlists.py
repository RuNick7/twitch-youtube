"""Плейлисты по категориям: опубликованный ролик добавляется в плейлист своей категории.

У Shorts свой плейлист «Shorts | <стример>». Плейлист создаётся при первом опубликованном
ролике категории. Если его удалили на YouTube, бот создаёт новый при следующем ролике.
У каналов Twitch одного стримера (db.channel_mates) плейлисты общие.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape

import httpx
from sqlalchemy import delete, select

from .consent import PLAYLISTS_SINCE, accepted_version
from .context import App
from .db import SHORT, Playlist, Segment, Status, Streamer, Vod, all_streamers, channel_mates, utcnow
from .segments import build_playlist_title, category_key
from .service import update_segment
from .youtube import LIMIT_REASONS, AuthError, YouTubeError

log = logging.getLogger(__name__)

SHORTS_PLAYLIST = "Shorts"
_lock = asyncio.Lock()


async def sync_playlists(app: App) -> None:
    """Добавляет в плейлисты опубликованные ролики, которых там ещё нет."""
    if _lock.locked():
        return
    async with _lock:
        async with app.sessions() as session:
            streamers = await all_streamers(session, connected=True)
        for streamer in streamers:
            # Пока владелец канала не принял политику, где описаны плейлисты, бот их не трогает
            if app.config(streamer).playlists and accepted_version(streamer) >= PLAYLISTS_SINCE:
                await _sync(app, streamer)


async def _sync(app: App, streamer: Streamer) -> None:
    token = app.vault.decrypt(streamer.youtube_token)
    async with app.sessions() as session:
        pending = list(
            (
                await session.scalars(
                    select(Segment)
                    .join(Vod, Vod.id == Segment.vod_id)
                    .where(
                        Vod.streamer_id == streamer.id,
                        Segment.status == Status.PUBLISHED,
                        Segment.youtube_id.is_not(None),
                        Segment.playlist_added_at.is_(None),
                    )
                    .order_by(Vod.started_at, Vod.id, Segment.idx)
                )
            ).all()
        )
        mates = await channel_mates(session, streamer)
        rows = (
            await session.scalars(
                select(Playlist).where(Playlist.streamer_id.in_([mate.id for mate in mates])).order_by(Playlist.id)
            )
        ).all()
    # Свой плейлист категории, а если его нет — заведённый другим каналом Twitch того же стримера
    playlists: dict[str, str] = {}
    for row in sorted(rows, key=lambda row: row.streamer_id != streamer.id):
        playlists.setdefault(row.category, row.youtube_id)
    for seg in pending:
        category = SHORTS_PLAYLIST if seg.kind == SHORT else seg.category
        key = category_key(category)
        try:
            if key not in playlists:
                playlists[key] = await _create(app, streamer, mates, token, category, key)
            try:
                await app.youtube.add_to_playlist(token, playlists[key], seg.youtube_id)
            except YouTubeError as exc:
                if exc.reason != "playlistNotFound":
                    raise
                # Плейлист удалили на YouTube: создаём новый
                await _forget(app, playlists[key])
                playlists[key] = await _create(app, streamer, mates, token, category, key)
                await app.youtube.add_to_playlist(token, playlists[key], seg.youtube_id)
        except AuthError:
            return  # отзывом доступа занимается ежедневная сверка
        except YouTubeError as exc:
            log.warning("Ролик %s не добавлен в плейлист: %s", seg.youtube_id, exc)
            if exc.reason in LIMIT_REASONS or (exc.status or 0) >= 500:
                return  # повторим при следующем проходе
            continue
        except httpx.HTTPError as exc:
            log.warning("Плейлисты: YouTube недоступен (%s)", exc)
            return
        await update_segment(app, seg.id, playlist_added_at=utcnow())


async def _create(app: App, streamer: Streamer, mates: list[Streamer], token: str, category: str, key: str) -> str:
    settings = app.config(streamer)
    name = streamer.public_name
    if category == SHORTS_PLAYLIST:
        what = f"Shorts из клипов стримов {name}"
    else:
        what = f"Все ролики категории «{category}» со стримов {name}"
    channels = ", ".join(f"https://www.twitch.tv/{mate.login}" for mate in mates)
    playlist_id = await app.youtube.create_playlist(
        token,
        build_playlist_title(category, name),
        f"{what}. Twitch: {channels}",
        settings.publish_privacy,
    )
    async with app.sessions() as session, session.begin():
        session.add(Playlist(streamer_id=streamer.id, category=key, youtube_id=playlist_id))
    title = build_playlist_title(category, name)
    log.info("Создан плейлист «%s»", title)
    await app.notify(
        f"📂 Создан плейлист «{escape(title)}»: https://www.youtube.com/playlist?list={playlist_id}", silent=True
    )
    return playlist_id


async def _forget(app: App, playlist_id: str) -> None:
    async with app.sessions() as session, session.begin():
        await session.execute(delete(Playlist).where(Playlist.youtube_id == playlist_id))


async def verify_playlists(app: App, streamer: Streamer, token: str) -> None:
    """Ежедневная сверка: забывает плейлисты, удалённые на YouTube. Ошибки YouTube пробрасываются."""
    async with app.sessions() as session:
        rows = list((await session.scalars(select(Playlist).where(Playlist.streamer_id == streamer.id))).all())
    if not rows:
        return
    existing = await app.youtube.my_playlists(token)
    for row in rows:
        if row.youtube_id not in existing:
            await _forget(app, row.youtube_id)
