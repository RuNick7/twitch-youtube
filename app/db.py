"""Модели и доступ к базе SQLite."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import BigInteger, ForeignKey, String, Text, UniqueConstraint, delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """SQLite хранит время без часового пояса; всё, что мы пишем, — UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Status:
    SKIPPED = "skipped"  # не загружается: похоже на просмотр сериала или фильма
    QUEUED = "queued"  # ждёт загрузки
    UPLOADING = "uploading"
    PROCESSING = "processing"  # загружен приватно, YouTube обрабатывает
    WAITING = "waiting"  # обработан, ждём проверку Content ID перед публикацией
    REVIEW = "review"  # есть предупреждения: публикация только по решению в Telegram
    PUBLISHED = "published"
    PRIVATE = "private"  # оставлен приватным по решению
    LOCKED = "locked"  # YouTube не дал опубликовать
    REJECTED = "rejected"  # YouTube отклонил ролик или не смог его обработать
    FAILED = "failed"  # загрузка не удалась, можно повторить
    FORGOTTEN = "forgotten"  # данные о ролике удалены: канал отключён или ролика больше нет на YouTube


class Base(DeclarativeBase):
    pass


class Streamer(Base):
    """Стример и его YouTube-канал: у каждого стримера свой канал, своя пауза и своё согласие с политикой."""

    __tablename__ = "streamers"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(128))  # название канала на Twitch
    # Имя в конце названий роликов и плейлистов — так стримера ищут зрители («Заквиель»)
    title_name: Mapped[str | None] = mapped_column(String(128))
    # Настройки стримера, которые отличаются от .env: JSON {"shorts_min_views": 300, …}, см. config.STREAMER_FIELDS
    overrides: Mapped[str | None] = mapped_column(Text)
    paused: Mapped[bool | None]  # загрузка и публикация остановлены: /pause или пропал доступ к YouTube
    watch_since: Mapped[datetime | None]  # эфиры, закончившиеся раньше, слежение не трогает
    # Владелец бота подтвердил, что стример разрешил делать нарезки (/add)
    permitted_at: Mapped[datetime | None]
    # Убран командой /remove: бот не следит за каналом и клипами, а ролики, которые уже в работе, доводит до конца
    removed_at: Mapped[datetime | None]
    youtube_channel_id: Mapped[str | None] = mapped_column(String(64))
    youtube_channel_title: Mapped[str | None] = mapped_column(String(256))
    youtube_token: Mapped[str | None] = mapped_column(Text)  # refresh-токен, зашифрован
    youtube_checked_at: Mapped[datetime | None]  # последняя ежедневная сверка с YouTube
    # YouTube не принимает загрузки из-за лимита (причина — код ошибки YouTube): до этого времени
    # загрузки стримера ждут, потом бот пробует снова. Публикация готовых роликов при этом идёт
    uploads_wait_until: Mapped[datetime | None]
    uploads_wait_reason: Mapped[str | None] = mapped_column(String(64))
    consent_version: Mapped[int | None]  # версия политики, которую принял владелец канала (consent.py)
    consent_prompted: Mapped[int | None]  # версия, принять которую бот уже предлагал

    @property
    def public_name(self) -> str:
        """Имя стримера в названиях роликов и плейлистов; без title_name — название канала на Twitch."""
        return self.title_name or self.display_name or self.login


class Vod(Base):
    __tablename__ = "vods"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # ID видео на Twitch
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"))
    title: Mapped[str] = mapped_column(String(512))
    started_at: Mapped[datetime | None]
    duration: Mapped[int]
    playlist_url: Mapped[str | None] = mapped_column(Text)  # HLS-плейлист исходного качества
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


SHORT = "short"  # Segment.kind: вертикальный ролик из клипа Twitch


def clips_vod_id(login: str) -> str:
    """Shorts живут в той же таблице, что сегменты, и с тем же жизненным циклом на YouTube.

    Их «VOD» — служебная запись clips-<логин>: у клипа запись эфира может быть не обработана
    или удалена, а настоящий ID VOD нельзя занимать, иначе бот решит, что эфир уже нарезан.
    """
    return f"clips-{login.lower()}"


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    vod_id: Mapped[str] = mapped_column(ForeignKey("vods.id"), index=True)
    kind: Mapped[str | None] = mapped_column(String(16))  # None — сегмент VOD, SHORT — Shorts из клипа
    # Только у Shorts: клип Twitch, из которого сделан ролик
    clip_id: Mapped[str | None] = mapped_column(String(64), index=True)
    clip_slug: Mapped[str | None] = mapped_column(String(128))
    clip_title: Mapped[str | None] = mapped_column(Text)
    clip_views: Mapped[int | None]
    clip_author: Mapped[str | None] = mapped_column(String(128))
    clip_vod_id: Mapped[str | None] = mapped_column(String(32))  # запись эфира, если она есть
    clip_created_at: Mapped[datetime | None]
    idx: Mapped[int]
    start: Mapped[int]
    end: Mapped[int]
    category: Mapped[str] = mapped_column(String(256))
    part: Mapped[int | None]  # «Часть N»: номер сквозной по категории, от стрима к стриму
    title: Mapped[str] = mapped_column(String(256))
    stream_title: Mapped[str | None] = mapped_column(Text)  # название стрима на этом отрезке
    ranges: Mapped[str | None] = mapped_column(Text)  # JSON [[start, end], …], если ролик склеен из отрезков
    status: Mapped[str] = mapped_column(String(16), default=Status.QUEUED, index=True)
    reason: Mapped[str | None] = mapped_column(Text)  # почему пропущен или отклонён
    warnings: Mapped[str | None] = mapped_column(Text)  # JSON-список предупреждений
    force_review: Mapped[bool] = mapped_column(default=False)  # загружен вопреки фильтру
    progress: Mapped[int] = mapped_column(default=0)
    queued_at: Mapped[datetime | None]  # очередь загрузки идёт по этому времени
    upload_uri: Mapped[str | None] = mapped_column(Text)  # сессия resumable upload для докачки
    upload_total: Mapped[int | None] = mapped_column(BigInteger)  # размер ролика в байтах
    expected_duration: Mapped[int | None]  # секунд, для сверки с YouTube
    youtube_id: Mapped[str | None] = mapped_column(String(32))
    check_at: Mapped[datetime | None] = mapped_column(index=True)  # когда проверить на YouTube
    publish_after: Mapped[datetime | None]
    published_at: Mapped[datetime | None]
    monitor_until: Mapped[datetime | None]
    playlist_added_at: Mapped[datetime | None]  # когда ролик добавлен в плейлист своей категории
    error: Mapped[str | None] = mapped_column(Text)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)


class Playlist(Base):
    """Плейлист категории на YouTube-канале: создаётся при первом опубликованном ролике категории."""

    __tablename__ = "playlists"
    __table_args__ = (UniqueConstraint("streamer_id", "category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"))
    category: Mapped[str] = mapped_column(String(256))  # category_key: без учёта регистра
    youtube_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TitleChange(Base):
    """Название стрима, замеченное во время эфира. В данных VOD истории названий нет."""

    __tablename__ = "title_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(64), index=True)
    stream_id: Mapped[str] = mapped_column(String(32))
    stream_started_at: Mapped[datetime]
    at: Mapped[datetime]  # когда бот увидел это название
    title: Mapped[str] = mapped_column(Text)


class KV(Base):
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


def get_spans(seg: Segment) -> list[tuple[int, int]]:
    """Отрезки VOD, из которых состоит ролик сегмента."""
    return [tuple(span) for span in json.loads(seg.ranges)] if seg.ranges else [(seg.start, seg.end)]


def dump_spans(spans: list[tuple[int, int]]) -> str | None:
    return json.dumps([list(span) for span in spans]) if len(spans) > 1 else None


def get_warnings(seg: Segment) -> list[str]:
    return json.loads(seg.warnings) if seg.warnings else []


def dump_warnings(warnings: list[str]) -> str | None:
    return json.dumps(warnings, ensure_ascii=False) if warnings else None


def make_engine(path: Path) -> AsyncEngine:
    # Коммит может ждать диск, занятый другими процессами сервера: ждём блокировку до 30 секунд, а не 5
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 30})

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        # В режиме WAL база не портится и без fsync на каждый коммит; при сбое питания
        # могут пропасть только последние секунды изменений
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


async def init_db(engine: AsyncEngine) -> None:
    """Создаёт недостающие таблицы и добавляет в существующие недостающие колонки.

    Пока схема только растёт, этого достаточно; для переименований и удалений
    понадобятся миграции Alembic.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn) -> None:
    for table in Base.metadata.sorted_tables:
        existing = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable:
                raise RuntimeError(f"нельзя автоматически добавить обязательную колонку {table.name}.{column.name}")
            kind = column.type.compile(dialect=conn.dialect)
            conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {kind}')


async def find_streamer(session: AsyncSession, login: str) -> Streamer | None:
    return await session.scalar(select(Streamer).where(Streamer.login == login.lower()))


async def all_streamers(session: AsyncSession, connected: bool = False, watched: bool = False) -> list[Streamer]:
    """Все стримеры в порядке добавления; connected — только с подключённым YouTube-каналом,
    watched — только те, за чьим каналом бот следит (не убраны командой /remove)."""
    query = select(Streamer).order_by(Streamer.id)
    if connected:
        query = query.where(Streamer.youtube_token.is_not(None))
    if watched:
        query = query.where(Streamer.removed_at.is_(None))
    return list((await session.scalars(query)).all())


async def get_streamer(session: AsyncSession, login: str) -> Streamer:
    streamer = await find_streamer(session, login)
    if streamer is None:
        streamer = Streamer(login=login.lower())
        session.add(streamer)
        await session.flush()
    return streamer


async def get_clips_vod(session: AsyncSession, streamer: Streamer) -> Vod:
    """Служебная запись, к которой привязаны Shorts стримера (см. clips_vod_id)."""
    vod = await session.get(Vod, clips_vod_id(streamer.login))
    if vod is None:
        vod = Vod(id=clips_vod_id(streamer.login), streamer_id=streamer.id, title="Клипы канала", duration=0)
        session.add(vod)
        await session.flush()
    return vod


# Пока стример был один, его состояние хранилось в общих ключах KV
LEGACY_KEYS = ("paused", "consent_version", "consent_prompted", "watch_since", "youtube_checked_at")


async def adopt_legacy_state(session: AsyncSession, streamer: Streamer, title_name: str = "") -> None:
    """Переносит в запись стримера то, что хранилось общим, пока стример был один: паузу, согласие
    с политикой, начало слежения и время сверки с YouTube из KV, имя для названий из STREAMER_NAME.

    Перенесённые ключи удаляются, уже заполненные поля не перезаписываются: повторный вызов ничего не меняет.
    Строки других каналов, оставшиеся от прежних значений TWITCH_CHANNEL, при переносе помечаются убранными.
    """
    legacy = {row.key: row.value for row in (await session.scalars(select(KV).where(KV.key.in_(LEGACY_KEYS)))).all()}
    if "paused" in legacy and streamer.paused is None:
        streamer.paused = legacy["paused"] == "1"
    for key in ("consent_version", "consent_prompted"):
        if getattr(streamer, key) is None and legacy.get(key, "").isdigit():
            setattr(streamer, key, int(legacy[key]))
    for key in ("watch_since", "youtube_checked_at"):
        if getattr(streamer, key) is None and key in legacy:
            setattr(streamer, key, _parse_time(legacy[key]))
    if legacy:
        await session.execute(delete(KV).where(KV.key.in_(LEGACY_KEYS)))
        # Раньше бот следил только за каналом из TWITCH_CHANNEL. Строки других каналов остались от прежних
        # значений этой настройки, и следить за ними бот не должен; вернуть такой канал можно командой /add.
        # Стримеров, добавленных через /add (у них отмечено разрешение), это не касается
        await session.execute(
            update(Streamer)
            .where(Streamer.id != streamer.id, Streamer.removed_at.is_(None), Streamer.permitted_at.is_(None))
            .values(removed_at=utcnow())
        )
    if title_name and not streamer.title_name:
        streamer.title_name = title_name


def _parse_time(value: str) -> datetime | None:
    try:
        return as_utc(datetime.fromisoformat(value)).astimezone(timezone.utc)
    except ValueError:
        return None
