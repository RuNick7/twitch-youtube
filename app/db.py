"""Модели и доступ к базе SQLite."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import BigInteger, ForeignKey, String, Text, event, select
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
    __tablename__ = "streamers"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    youtube_channel_id: Mapped[str | None] = mapped_column(String(64))
    youtube_channel_title: Mapped[str | None] = mapped_column(String(256))
    youtube_token: Mapped[str | None] = mapped_column(Text)  # refresh-токен, зашифрован


class Vod(Base):
    __tablename__ = "vods"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # ID видео на Twitch
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"))
    title: Mapped[str] = mapped_column(String(512))
    started_at: Mapped[datetime | None]
    duration: Mapped[int]
    playlist_url: Mapped[str | None] = mapped_column(Text)  # HLS-плейлист исходного качества
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    vod_id: Mapped[str] = mapped_column(ForeignKey("vods.id"), index=True)
    idx: Mapped[int]
    start: Mapped[int]
    end: Mapped[int]
    category: Mapped[str] = mapped_column(String(256))
    part: Mapped[int | None]
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
    error: Mapped[str | None] = mapped_column(Text)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)


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
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
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


async def get_streamer(session: AsyncSession, login: str) -> Streamer:
    streamer = await find_streamer(session, login)
    if streamer is None:
        streamer = Streamer(login=login.lower())
        session.add(streamer)
        await session.flush()
    return streamer


async def kv_get(session: AsyncSession, key: str) -> str | None:
    row = await session.get(KV, key)
    return row.value if row else None


async def kv_set(session: AsyncSession, key: str, value: str) -> None:
    await session.merge(KV(key=key, value=value))
