"""Плейлисты по категориям: опубликованный ролик добавляется в плейлист своей категории.

У Shorts свой плейлист «Shorts | <стример>». Плейлист создаётся при первом опубликованном
ролике категории. Если его удалили на YouTube, бот создаёт новый при следующем ролике.
У каналов Twitch одного стримера (db.channel_mates) плейлисты общие.

Если плейлист с нужным названием на канале уже есть, бот берёт его, а не создаёт второй:
так бывает после /disconnect и нового подключения, когда ID плейлистов удалены вместе
с остальными данными канала. Из нескольких плейлистов с одним названием берётся самый старый.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from html import escape

import httpx
from sqlalchemy import delete, select, update

from .consent import PLAYLISTS_SINCE, accepted_version
from .context import App
from .db import SHORT, Playlist, Segment, Status, Streamer, Vod, all_streamers, channel_mates, utcnow
from .segments import build_playlist_title, category_key
from .service import update_segment
from .youtube import LIMIT_REASONS, AuthError, ChannelPlaylist, YouTubeError

log = logging.getLogger(__name__)

SHORTS_PLAYLIST = "Shorts"
_lock = asyncio.Lock()
_verified: set[int] = set()  # стримеры, чьи плейлисты уже сверены с YouTube после запуска


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
                await _verify_once(app, streamer)
                await _sync(app, streamer)


async def _verify_once(app: App, streamer: Streamer) -> None:
    """Первая после запуска сверка плейлистов: не ждёт ежедневной, чтобы дубли исчезли сразу после обновления."""
    if streamer.id in _verified:
        return
    _verified.add(streamer.id)  # при ошибке не повторяем: сверка всё равно идёт раз в сутки
    try:
        await verify_playlists(app, streamer, app.vault.decrypt(streamer.youtube_token))
    except (YouTubeError, httpx.HTTPError) as exc:
        log.warning("Плейлисты канала %s не сверены: %s", streamer.login, exc)


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
    playlists = {category: row.youtube_id for category, row in _own_or_shared(rows, streamer.id).items()}
    for seg in pending:
        category = SHORTS_PLAYLIST if seg.kind == SHORT else seg.category
        key = _category_of(seg)
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


def _own_or_shared(rows, streamer_id: int) -> dict[str, Playlist]:
    """Плейлист каждой категории для стримера: свой, а если его нет — заведённый другим каналом Twitch
    того же стримера. rows — плейлисты всех таких каналов по порядку создания."""
    chosen: dict[str, Playlist] = {}
    for row in sorted(rows, key=lambda row: row.streamer_id != streamer_id):
        chosen.setdefault(row.category, row)
    return chosen


def _title_key(title: str) -> str:
    return " ".join(title.split()).casefold()


def oldest_with_title(existing: dict[str, ChannelPlaylist], title: str) -> str | None:
    """ID самого старого плейлиста канала с таким названием; None — такого нет."""
    key = _title_key(title)
    latest = datetime.max.replace(tzinfo=timezone.utc)  # без времени создания — в конец
    same = [(item.created_at or latest, playlist_id) for playlist_id, item in existing.items() if _title_key(item.title) == key]
    return min(same)[1] if same else None


def _link(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


async def _create(app: App, streamer: Streamer, mates: list[Streamer], token: str, category: str, key: str) -> str:
    """Плейлист категории: уже существующий на канале с тем же названием или новый."""
    settings = app.config(streamer)
    name = streamer.public_name
    title = build_playlist_title(category, name)
    playlist_id = oldest_with_title(await app.youtube.my_playlists(token), title)
    found = playlist_id is not None
    if not found:
        if category == SHORTS_PLAYLIST:
            what = f"Shorts из клипов стримов {name}"
        else:
            what = f"Все ролики категории «{category}» со стримов {name}"
        channels = ", ".join(f"https://www.twitch.tv/{mate.login}" for mate in mates)
        playlist_id = await app.youtube.create_playlist(token, title, f"{what}. Twitch: {channels}", settings.publish_privacy)
    async with app.sessions() as session, session.begin():
        session.add(Playlist(streamer_id=streamer.id, category=key, youtube_id=playlist_id))
    if found:
        log.info("Плейлист «%s» уже есть на канале: беру его", title)
        text = f"📂 Плейлист «{escape(title)}» уже есть на канале, новые ролики идут в него: {_link(playlist_id)}"
    else:
        log.info("Создан плейлист «%s»", title)
        text = f"📂 Создан плейлист «{escape(title)}»: {_link(playlist_id)}"
    await app.notify(text, silent=True)
    return playlist_id


async def _forget(app: App, playlist_id: str) -> None:
    async with app.sessions() as session, session.begin():
        await session.execute(delete(Playlist).where(Playlist.youtube_id == playlist_id))


async def verify_playlists(app: App, streamer: Streamer, token: str) -> None:
    """Сверка с YouTube: забывает плейлисты, удалённые на YouTube, а с плейлиста-дубля переходит на более
    старый плейлист с тем же названием. Ошибки YouTube пробрасываются."""
    async with app.sessions() as session:
        rows = list((await session.scalars(select(Playlist).where(Playlist.streamer_id == streamer.id))).all())
    if not rows:
        return
    _verified.add(streamer.id)
    existing = await app.youtube.my_playlists(token)
    for row in rows:
        current = existing.get(row.youtube_id)
        if current is None:
            await _forget(app, row.youtube_id)
            continue
        original = oldest_with_title(existing, current.title)
        if original != row.youtube_id:
            await _move(app, streamer, token, row, original, current.title)


async def _move(app: App, streamer: Streamer, token: str, row: Playlist, original: str, title: str) -> None:
    """На канале два плейлиста с одним названием, и бот вёл более новый: переходит на старый и заново
    раскладывает ролики, которые успел добавить в дубль. Сам дубль не трогает: удалить его может владелец."""
    present = await app.youtube.playlist_videos(token, original)  # эти ролики второй раз не добавляем
    async with app.sessions() as session, session.begin():
        mates = await channel_mates(session, streamer)
        ids = [mate.id for mate in mates]
        rows = list((await session.scalars(select(Playlist).where(Playlist.streamer_id.in_(ids)).order_by(Playlist.id))).all())
        # Чьи ролики шли в этот плейлист: его хозяина и тех каналов Twitch стримера, у кого своего нет
        users = [mate.id for mate in mates if getattr(_own_or_shared(rows, mate.id).get(row.category), "id", None) == row.id]
        added = (
            await session.scalars(
                select(Segment)
                .join(Vod, Vod.id == Segment.vod_id)
                .where(
                    Vod.streamer_id.in_(users),
                    Segment.status == Status.PUBLISHED,
                    Segment.youtube_id.is_not(None),
                    Segment.playlist_added_at.is_not(None),
                )
            )
        ).all()
        moved = [seg.id for seg in added if _category_of(seg) == row.category and seg.youtube_id not in present]
        await session.execute(update(Playlist).where(Playlist.id == row.id).values(youtube_id=original))
        if moved:
            await session.execute(update(Segment).where(Segment.id.in_(moved)).values(playlist_added_at=None))
    log.info("Плейлист «%s»: перехожу с дубля %s на %s, роликов переложить: %s", title, row.youtube_id, original, len(moved))
    app.sync_wake.set()
    await app.notify(
        f"📂 На канале два плейлиста «{escape(title)}». Ролики теперь идут в более старый: {_link(original)}\n"
        f"Дубль можно удалить в YouTube Studio: {_link(row.youtube_id)}",
        silent=True,
    )


def _category_of(seg: Segment) -> str:
    """Ключ плейлиста, в который идёт ролик."""
    return category_key(SHORTS_PLAYLIST if seg.kind == SHORT else seg.category)
