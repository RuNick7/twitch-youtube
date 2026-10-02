"""Shorts из популярных клипов канала.

Раз в shorts_check_min минут бот берёт клипы канала за последние 7 дней. Клип от
shorts_min_views просмотров становится вертикальным роликом 1080×1920 (клип по центру
на размытом фоне) и дальше идёт тем же путём, что сегменты: приватная загрузка,
проверки YouTube, публикация, плейлист «Shorts | <стример>». Не больше shorts_per_day
Shorts за сутки; из одного момента стрима выходит один Shorts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy import func, select

from .clips import Clip, build_short_description, build_short_title, choose, clip_url, parse_clip
from .config import Settings
from .consent import SHORTS_SINCE, accepted_version
from .context import App
from .db import (
    SHORT,
    Segment,
    Status,
    Streamer,
    TitleChange,
    all_streamers,
    clips_vod_id,
    dump_warnings,
    get_clips_vod,
    get_spans,
    utcnow,
)
from .segments import FALLBACK_CATEGORY, build_tags, category_warnings, is_short_reason, skip_reason
from .service import update_segment, video_metadata
from .tools import ToolError, run
from .twitch import client_id, popular_clips
from .ui import local_time, render_segment, youtube_link
from .youtube import StreamFactory

log = logging.getLogger(__name__)

RENDER_TIMEOUT_SEC = 900
# Клип по центру на размытом фоне. Фон размывается в уменьшенном виде, поэтому почти ничего не стоит
VERTICAL_FILTER = (
    "[0:v]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:1,scale=1080:1920,setsar=1[bg];"
    "[0:v]scale=1080:-2,setsar=1[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p[v]"
)
# Пока Shorts ждёт загрузки, его готовый файл лежит здесь: после перезапуска загрузка продолжится с того же файла
KEEP_FILES = (Status.QUEUED, Status.UPLOADING, Status.FAILED)


def shorts_dir(app: App) -> Path:
    path = app.settings.data_dir / "shorts"
    path.mkdir(parents=True, exist_ok=True)
    return path


class ShortsScout:
    """Следит за клипами каналов всех стримеров и ставит в очередь новые Shorts по их настройкам."""

    def __init__(self, app: App):
        self.app = app
        self.client: str | None = None

    async def run(self) -> None:
        while True:
            self.app.shorts_wake.clear()
            try:
                await self.scan()
            except Exception:
                self.client = None  # вдруг Twitch сменил ID клиента: возьмём свежий у yt-dlp
                log.exception("Не удалось проверить клипы канала")
            try:
                # Согласие с политикой будит раньше
                await asyncio.wait_for(self.app.shorts_wake.wait(), timeout=self.app.settings.shorts_check_min * 60)
            except asyncio.TimeoutError:
                pass

    async def scan(self) -> int:
        """Ставит в очередь новые клипы всех стримеров и возвращает, сколько Shorts добавлено (вместе с пропущенными)."""
        app = self.app
        async with app.sessions() as session:
            streamers = await all_streamers(session, connected=True)
            stored = list((await session.scalars(select(Segment).where(Segment.kind == SHORT))).all())
        self._clean_files(stored)
        added = 0
        for streamer in streamers:
            settings = app.config(streamer)
            # Пока владелец канала не принял политику, где описаны Shorts, бот их не делает
            if not settings.shorts or accepted_version(streamer) < SHORTS_SINCE:
                continue
            try:
                added += await self._scan(streamer, settings)
            except Exception:
                self.client = None  # вдруг Twitch сменил ID клиента: возьмём свежий у yt-dlp
                log.exception("Не удалось проверить клипы канала %s", streamer.login)
        if added:
            app.wake.set()
        return added

    async def _scan(self, streamer: Streamer, settings: Settings) -> int:
        app = self.app
        clips_vod = clips_vod_id(streamer.login)
        async with app.sessions() as session:
            stored = list((await session.scalars(select(Segment).where(Segment.vod_id == clips_vod))).all())
            recent = await session.scalar(
                select(func.count())
                .select_from(Segment)
                .where(
                    Segment.vod_id == clips_vod,
                    Segment.status != Status.SKIPPED,
                    Segment.queued_at >= utcnow() - timedelta(days=1),
                )
            )
        if self.client is None:
            self.client = await client_id()
        clips = [clip for clip in map(parse_clip, await popular_clips(app.http, streamer.login, self.client)) if clip]
        room = settings.shorts_per_day - (recent or 0)
        added = 0
        for clip in choose(clips, settings.shorts_min_views, [_stored_clip(row) for row in stored]):
            stream_title, covering = await self._context(streamer, clip)
            reason = self._skip_reason(settings, clip, stream_title, covering)
            if reason is None:
                if room <= 0:
                    continue  # лимит на сутки: клип дождётся следующей проверки
                room -= 1
            await self._add(streamer, settings, clip, stream_title, reason)
            added += 1
        return added

    async def _context(self, streamer: Streamer, clip: Clip) -> tuple[str | None, Segment | None]:
        """Название стрима в момент клипа и сегмент VOD, в который клип попадает, если VOD нарезан."""
        async with self.app.sessions() as session:
            covering = None
            if clip.vod_id and clip.vod_offset is not None:
                segments = (
                    await session.scalars(select(Segment).where(Segment.vod_id == clip.vod_id, Segment.kind.is_(None)))
                ).all()
                covering = next(
                    (s for s in segments if any(start <= clip.vod_offset < end for start, end in get_spans(s))), None
                )
            change = None
            if clip.created_at is not None:
                change = await session.scalar(
                    select(TitleChange)
                    .where(
                        TitleChange.channel == streamer.login,
                        TitleChange.at <= clip.created_at,
                        TitleChange.at >= clip.created_at - timedelta(days=1),
                    )
                    .order_by(TitleChange.at.desc())
                    .limit(1)
                )
        title = (covering.stream_title if covering else None) or (change.title if change else None) or clip.vod_title
        return title, covering

    def _skip_reason(
        self, settings: Settings, clip: Clip, stream_title: str | None, covering: Segment | None
    ) -> str | None:
        """Те же фильтры, что для сегментов: клипы из сериалов и фильмов не загружаются."""
        if covering is not None and covering.status == Status.SKIPPED and not is_short_reason(covering.reason):
            return f"клип из части стрима, которая не загружается: {covering.reason}"
        return skip_reason(
            clip.category or FALLBACK_CATEGORY,
            " ".join(text for text in (stream_title, clip.vod_title, clip.title) if text),
            keywords=settings.keywords,
            title_categories=settings.title_categories,
            categories=settings.skipped_categories,
        )

    async def _add(
        self, streamer: Streamer, settings: Settings, clip: Clip, stream_title: str | None, reason: str | None
    ) -> None:
        app = self.app
        name = streamer.public_name
        category = clip.category or FALLBACK_CATEGORY
        start, end = clip.span or (0, max(1, round(clip.duration)))
        async with app.sessions() as session, session.begin():
            owner = await session.get(Streamer, streamer.id)
            vod = await get_clips_vod(session, owner)
            last = await session.scalar(select(func.max(Segment.idx)).where(Segment.vod_id == vod.id))
            seg = Segment(
                vod_id=vod.id,
                idx=(last or 0) + 1,
                start=start,
                end=end,
                category=category,
                title=build_short_title(clip.title, name, category, stream_title or ""),
                stream_title=stream_title,
                status=Status.SKIPPED if reason else Status.QUEUED,
                reason=reason,
                warnings=dump_warnings(category_warnings(category, settings.warned_categories)),
                queued_at=None if reason else utcnow(),
                expected_duration=max(1, round(clip.duration)),
                kind=SHORT,
                clip_id=clip.id,
                clip_slug=clip.slug,
                clip_title=clip.title,
                clip_views=clip.views,
                clip_author=clip.author,
                clip_vod_id=clip.vod_id,
                clip_created_at=clip.created_at,
            )
            session.add(seg)
        log.info("Shorts из клипа %s (%s просмотров): %s", clip.slug, clip.views, reason or "в очереди")
        text, markup = render_segment(seg, vod, streamer, app.tz, settings.publish_privacy)
        message = await app.notify(text, markup=markup, silent=True)
        if message:
            await update_segment(app, seg.id, tg_message_id=message.message_id)

    def _clean_files(self, stored: list[Segment]) -> None:
        """Готовые файлы нужны, только пока Shorts ждёт загрузки."""
        keep = {str(row.id) for row in stored if row.status in KEEP_FILES}
        for path in shorts_dir(self.app).glob("*.mp4"):
            # 7.mp4, а пока идёт отрисовка — 7.source.mp4 и 7.draft.mp4
            if path.name.split(".", 1)[0] not in keep:
                path.unlink(missing_ok=True)


def _stored_clip(row: Segment) -> Clip:
    """Клип уже созданного Shorts — чтобы не взять его или тот же момент ещё раз."""
    return Clip(
        id=row.clip_id or "",
        slug=row.clip_slug or "",
        title=row.clip_title or "",
        views=row.clip_views or 0,
        duration=row.end - row.start,
        vod_id=row.clip_vod_id,
        vod_offset=row.start if row.clip_vod_id else None,
    )


async def render_short(link: str, out: Path) -> None:
    """Скачивает клип и делает из него вертикальный ролик 1080×1920. Готовый файл появляется целиком."""
    import imageio_ffmpeg  # статическая сборка ffmpeg из pip

    source = out.with_name(f"{out.stem}.source.mp4")
    draft = out.with_name(f"{out.stem}.draft.mp4")
    try:
        code, _, err = await run(
            "yt-dlp", "--no-warnings", "-q", "--no-part", "--force-overwrites",
            "-f", "best[height<=1080]/best", "-o", str(source), link,
            timeout=300,
        )
        if code != 0 or not source.exists():
            raise ToolError(f"клип не скачался: {err.strip()[-300:] or code}")
        code, _, err = await run(
            imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-filter_complex", VERTICAL_FILTER, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(draft),
            timeout=RENDER_TIMEOUT_SEC,
        )
        if code != 0:
            raise ToolError(f"ffmpeg: {err.strip()[-300:] or code}")
        draft.replace(out)
    finally:
        source.unlink(missing_ok=True)
        draft.unlink(missing_ok=True)


def file_stream(path: Path) -> StreamFactory:
    """Байты файла начиная с offset — для загрузки с докачкой."""

    async def open_stream(offset: int) -> AsyncGenerator[bytes, None]:
        with path.open("rb") as file:
            file.seek(offset)
            while piece := file.read(1 << 20):
                yield piece

    return open_stream


async def short_metadata(app: App, seg: Segment, streamer: Streamer) -> dict:
    """Метаданные Shorts. Если ролик стрима с этим моментом уже опубликован, в описании ссылка на него."""
    full_video = None
    if seg.clip_vod_id:
        async with app.sessions() as session:
            published = (
                await session.scalars(
                    select(Segment).where(
                        Segment.vod_id == seg.clip_vod_id, Segment.kind.is_(None), Segment.status == Status.PUBLISHED
                    )
                )
            ).all()
        covering = next((s for s in published if any(a <= seg.start < b for a, b in get_spans(s))), None)
        if covering and covering.youtube_id:
            full_video = youtube_link(covering.youtube_id)
    name = streamer.display_name or streamer.login
    snippet = {
        "title": seg.title,
        "description": build_short_description(
            seg.clip_title or "",
            name,
            streamer.login,
            local_time(seg.clip_created_at, app.tz, "%d.%m.%Y"),
            seg.category,
            clip_url(seg.clip_slug or ""),
            seg.clip_author,
            full_video,
        ),
        "tags": build_tags(seg.category, streamer.title_name, name, streamer.login, "shorts", "клип", "twitch"),
    }
    return video_metadata(app.config(streamer), snippet)


async def prepare_short(app: App, seg: Segment) -> Path:
    """Готовый вертикальный файл Shorts: уже отрисованный после перезапуска или новый."""
    path = shorts_dir(app) / f"{seg.id}.mp4"
    if not path.exists():
        # Сессия загрузки, если была, принимала другой файл
        await update_segment(app, seg.id, upload_uri=None, upload_total=None)
        seg.upload_uri = seg.upload_total = None
        await render_short(clip_url(seg.clip_slug or ""), path)
    return path
