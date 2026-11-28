"""Нейросети в работе бота, режим наблюдения: после нарезки стрима предложить названия и главы роликов.

Ролики не меняются: бот присылает предложения в Telegram ответом на сообщение о сегменте. Звук записи берётся
из Twitch, расшифровывается (OpenRouter или Groq) и разбирается Claude Haiku (app/ai). Расход за сутки ограничен
AI_DAILY_LIMIT_USD: когда лимит выбран, разбор ждёт следующего дня и продолжает с места остановки.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, time, timedelta, timezone
from html import escape
from pathlib import Path

from sqlalchemy import delete, func, select, update

from .ai import audio, chapters, claude, pipeline, timeline, titles, transcript
from .ai.recognizer import Recognizer
from .config import Settings
from .context import App
from .db import KV, AiResult, AiSpend, AiTask, Segment, Status, Streamer, Vod, get_spans, utcnow
from .segments import fmt_hms
from .ui import streamer_prefix, twitch_link

log = logging.getLogger(__name__)

PENDING, DONE, FAILED = "pending", "done", "failed"
TICK_SEC = 60
ATTEMPTS = 3
RETRY = timedelta(minutes=30)
# Оценки расхода до запроса, с запасом: лимит проверяется заранее, а в расход идёт настоящая цена
SPEECH_USD_PER_HOUR = 0.04  # столько стоит запасной поставщик; основной втрое дешевле
TEXT_USD_PER_HOUR = 0.01
TEXT_USD_MIN = 0.003
EFFORT = "low"
MESSAGE_MAX_CHARS = 4000  # у Telegram лимит 4096
LIMIT_NOTICE_KEY = "ai_limit_notice"  # день, за который о лимите уже сообщили


class LimitReached(Exception):
    """Дневной лимит расходов выбран: разбор продолжится завтра."""


def has_keys(settings: Settings) -> bool:
    return bool(settings.anthropic_api_key and (settings.openrouter_api_key or settings.groq_api_key))


def enabled(settings: Settings) -> bool:
    return settings.ai_observe and settings.ai_daily_limit_usd > 0 and has_keys(settings)


def today(app: App) -> str:
    return utcnow().astimezone(app.tz).date().isoformat()


def next_day(app: App) -> datetime:
    """Первая минута завтрашнего дня в часовом поясе владельца."""
    tomorrow = utcnow().astimezone(app.tz).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(0, 1), app.tz).astimezone(timezone.utc)


async def spent_today(app: App) -> float:
    async with app.sessions() as session:
        return float(await session.scalar(select(func.sum(AiSpend.usd)).where(AiSpend.day == today(app))) or 0.0)


async def spend(app: App, vod_id: str, kind: str, usd: float) -> None:
    async with app.sessions() as session, session.begin():
        session.add(AiSpend(day=today(app), vod_id=vod_id, kind=kind, usd=usd))


async def status_text(app: App) -> str:
    """Строка о нейросетях для /status и /ai."""
    settings = app.settings
    if not has_keys(settings):
        return "Нейросети: выключены, нужны ANTHROPIC_API_KEY и OPENROUTER_API_KEY в .env"
    if settings.ai_daily_limit_usd <= 0:
        return "Нейросети: выключены (AI_DAILY_LIMIT_USD=0)"
    async with app.sessions() as session:
        waiting = await session.scalar(select(func.count()).select_from(AiTask).where(AiTask.status == PENDING))
    queue_text = f", записей в очереди {waiting}" if waiting else ""
    return (
        f"Нейросети: наблюдение (ролики не меняются), сегодня ${await spent_today(app):.2f} "
        f"из ${settings.ai_daily_limit_usd:.2f}{queue_text}"
    )


async def queue(app: App, vod_id: str) -> None:
    """Разобрать запись заново (команда /ai): прежние предложения по ней забываются. Расшифровка удачного разбора
    не хранится и оплачивается снова, а от неудачного — используется."""
    segment_ids = select(Segment.id).where(Segment.vod_id == vod_id)
    async with app.sessions() as session, session.begin():
        await session.execute(delete(AiResult).where(AiResult.segment_id.in_(segment_ids)))
        task = await session.get(AiTask, vod_id)
        if task is None:
            session.add(AiTask(vod_id=vod_id))
        else:
            task.status, task.attempts, task.retry_at, task.error = PENDING, 0, None, None
    app.ai_wake.set()


def render_report(
    seg: Segment,
    title: str | None,
    has_topics: bool,
    chapter_lines: list[str],
    videos: list[tuple[float, float]],
    prefix: str = "",
) -> str:
    """Сообщение с предложением для одного ролика."""
    head = [f"🤖 {prefix}<b>Предложение нейросети</b> · ролик не меняется"]
    if title is None:
        return "\n".join([*head, "Речи в ролике не нашлось: предложить нечего."])
    head.append(f"Название: {escape(title)}" + ("" if has_topics else " (тем не нашлось, взято название стрима)"))
    head.append(f"Сейчас: {escape(seg.title)}")
    tail = []
    if videos:
        tail += ["", "⚠️ Похоже, здесь смотрят чужое видео:"]
        tail += [
            f'• <a href="{twitch_link(seg.vod_id, int(start))}">{fmt_hms(start)}–{fmt_hms(end)} записи</a>'
            for start, end in videos[:10]
        ]
    if not chapter_lines:
        return "\n".join([*head, "", "Глав нет: YouTube показывает их, только когда их не меньше трёх.", *tail])
    lines = [escape(line) for line in chapter_lines]
    while True:
        shown = lines if len(lines) == len(chapter_lines) else [*lines, "…"]
        text = "\n".join([*head, "", "Главы для описания:", *shown, *tail])
        if len(text) <= MESSAGE_MAX_CHARS or not lines:
            return text
        lines.pop()


class Analyst:
    def __init__(self, app: App):
        self.app = app

    async def run(self) -> None:
        while True:
            try:
                while await self.tick():
                    pass
            except Exception:
                log.exception("Сбой разбора записей нейросетями")
            try:
                await asyncio.wait_for(self.app.ai_wake.wait(), TICK_SEC)
            except asyncio.TimeoutError:
                pass
            self.app.ai_wake.clear()

    async def tick(self) -> bool:
        """Берёт самую раннюю запись, которую пора разобрать. False — таких нет."""
        if not has_keys(self.app.settings):
            return False
        async with self.app.sessions() as session:
            task = await session.scalar(
                select(AiTask)
                .where(AiTask.status == PENDING, (AiTask.retry_at.is_(None)) | (AiTask.retry_at <= utcnow()))
                .order_by(AiTask.created_at)
                .limit(1)
            )
        if task is None:
            return False
        await self.process(task.vod_id, task.attempts)
        return True

    async def process(self, vod_id: str, attempts: int) -> None:
        try:
            await self._analyze(vod_id)
        except LimitReached:
            await self._update(vod_id, retry_at=next_day(self.app))
            await self._notify_limit()
        except Exception as exc:  # noqa: BLE001 — любой сбой: повторить позже, а не уронить бота
            log.warning("Разбор записи %s не удался: %s", vod_id, exc, exc_info=True)
            attempts += 1
            if attempts < ATTEMPTS:
                await self._update(vod_id, attempts=attempts, retry_at=utcnow() + RETRY, error=str(exc)[:1000])
                return
            await self._update(vod_id, status=FAILED, attempts=attempts, error=str(exc)[:1000])
            await self.app.notify(
                f"🔴 Нейросети не смогли разобрать запись {vod_id}: {escape(str(exc)[:500])}\n"
                f"Повторить: /ai https://www.twitch.tv/videos/{vod_id}"
            )

    async def _update(self, vod_id: str, **values) -> None:
        async with self.app.sessions() as session, session.begin():
            await session.execute(update(AiTask).where(AiTask.vod_id == vod_id).values(**values))

    async def _ensure_budget(self, need: float) -> None:
        if await spent_today(self.app) + need > self.app.settings.ai_daily_limit_usd:
            raise LimitReached

    async def _notify_limit(self) -> None:
        """О выбранном лимите — одно сообщение в день."""
        day = today(self.app)
        async with self.app.sessions() as session, session.begin():
            row = await session.get(KV, LIMIT_NOTICE_KEY)
            if row and row.value == day:
                return
            if row:
                row.value = day
            else:
                session.add(KV(key=LIMIT_NOTICE_KEY, value=day))
        await self.app.notify(
            f"🤖 Дневной лимит нейросетей ${self.app.settings.ai_daily_limit_usd:.2f} выбран "
            f"(сегодня ${await spent_today(self.app):.2f}). Разбор продолжится завтра с места остановки.",
            silent=True,
        )

    async def _analyze(self, vod_id: str) -> None:
        app = self.app
        async with app.sessions() as session:
            vod = await session.get(Vod, vod_id)
            streamer = await session.get(Streamer, vod.streamer_id)
            segments = list(
                (
                    await session.scalars(
                        select(Segment)
                        .where(Segment.vod_id == vod_id, Segment.kind.is_(None), Segment.status != Status.SKIPPED)
                        .order_by(Segment.idx)
                    )
                ).all()
            )
            done = set(
                (
                    await session.scalars(
                        select(AiResult.segment_id).where(AiResult.segment_id.in_([seg.id for seg in segments]))
                    )
                ).all()
            )
        folder = app.settings.data_dir / "ai" / vod_id
        todo = [seg for seg in segments if seg.id not in done]
        if not enabled(app.config(streamer)) or not todo:
            await self._finish(vod_id, folder)
            return

        categories = sorted((start, end, seg.category) for seg in segments for start, end in get_spans(seg))
        recognizer = Recognizer(
            {"openrouter": app.settings.openrouter_api_key, "groq": app.settings.groq_api_key}
        )
        client = claude.make_client(app.settings.anthropic_api_key)
        source: list[str] = []  # адрес звука получается один раз и только если понадобится

        async def audio_url() -> str:
            if not source:
                source.append(await audio.audio_playlist_url(vod_id))
            return source[0]

        async def ask(**request):
            data, usage = await claude.ask_json(client, model=claude.MODEL, **request)
            await spend(app, vod_id, "text", usage.cost_usd)
            return data, usage

        try:
            for seg in todo:
                spans = get_spans(seg)
                phrases: list[transcript.Phrase] = []
                for start, end in spans:
                    phrases += await self._transcribe(vod_id, folder, start, end, recognizer, audio_url)
                found = None
                if any(start <= phrase.start < end for phrase in phrases for start, end in spans):
                    hours = sum(end - start for start, end in spans) / 3600
                    await self._ensure_budget(TEXT_USD_MIN + hours * TEXT_USD_PER_HOUR)
                    context = timeline.Context(streamer.public_name, seg.stream_title or vod.title, categories)
                    found, _ = await pipeline.analyze_segment(
                        ask, context, phrases, category=seg.category, spans=spans, effort=EFFORT
                    )
                await self._report(seg.id, vod, streamer, found)
        finally:
            close = getattr(client, "close", None)
            if close:
                await close()
        await self._finish(vod_id, folder)
        total = await self._vod_cost(vod_id)
        await app.notify(
            f"🤖 {streamer_prefix(streamer, app.several)}Разбор стрима «{escape(vod.title[:200])}» готов: "
            f"роликов {len(segments)}. Расход на запись ${total:.2f}, за сегодня ${await spent_today(app):.2f} "
            f"из ${app.settings.ai_daily_limit_usd:.2f}.",
            silent=True,
        )

    async def _transcribe(
        self, vod_id: str, folder: Path, start: int, end: int, recognizer: Recognizer, audio_url
    ) -> list[transcript.Phrase]:
        """Расшифровка отрезка записи. Сделанное сохраняется после каждого куска и повторно не оплачивается."""
        cache = folder / f"{start}-{end}.json"
        state = json.loads(cache.read_text()) if cache.exists() else {"done": 0, "complete": False, "phrases": "[]"}
        phrases = transcript.load(state["phrases"])
        if state["complete"]:
            return phrases
        await self._ensure_budget(audio.CHUNK_SEC / 3600 * SPEECH_USD_PER_HOUR)  # иначе незачем качать звук
        pieces = folder / f"audio-{start}"
        shutil.rmtree(pieces, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            chunks = await audio.extract_chunks(await audio_url(), pieces, start_sec=start, limit_sec=end - start)
            for number, chunk in enumerate(chunks):
                if number >= state["done"]:
                    await self._ensure_budget(chunk.duration / 3600 * SPEECH_USD_PER_HOUR)
                    result = await recognizer.transcribe(self.app.http, chunk.path, chunk.duration)
                    await spend(self.app, vod_id, "speech", result.cost_usd)
                    phrases += transcript.parse_verbose(result.data, chunk.start)[0]
                    state.update(done=number + 1, phrases=transcript.dump(phrases))
                    cache.write_text(json.dumps(state, ensure_ascii=False))
                chunk.path.unlink(missing_ok=True)  # звук больше не нужен: место на диске ограничено
        finally:
            shutil.rmtree(pieces, ignore_errors=True)
        state["complete"] = True
        cache.write_text(json.dumps(state, ensure_ascii=False))
        return phrases

    async def _report(self, segment_id: int, vod: Vod, streamer: Streamer, found: pipeline.Analysis | None) -> None:
        async with self.app.sessions() as session:
            seg = await session.get(Segment, segment_id)  # свежий: номер сообщения мог появиться только что
        title, chapter_lines, videos, has_topics = None, [], [], False
        if found is not None:
            name = streamer.public_name
            has_topics = bool(titles.pick_topics(found.topics, (seg.category, name)))
            title = titles.compose(found.topics, seg.stream_title or vod.title, seg.category, name, seg.part)
            chapter_lines = [f"{chapters.clock(c.at)} {c.title}" for c in found.chapters]
            videos = pipeline.videos(found.moments)
        await self.app.notify(
            render_report(seg, title, has_topics, chapter_lines, videos, streamer_prefix(streamer, self.app.several)),
            reply_to=seg.tg_message_id,
            silent=True,
        )
        async with self.app.sessions() as session, session.begin():
            session.add(
                AiResult(
                    segment_id=segment_id,
                    title=title,
                    chapters="\n".join(chapter_lines) or None,
                    videos=json.dumps(videos) if videos else None,
                )
            )

    async def _vod_cost(self, vod_id: str) -> float:
        async with self.app.sessions() as session:
            return float(await session.scalar(select(func.sum(AiSpend.usd)).where(AiSpend.vod_id == vod_id)) or 0.0)

    async def _finish(self, vod_id: str, folder: Path) -> None:
        await self._update(vod_id, status=DONE, error=None, retry_at=None)
        shutil.rmtree(folder, ignore_errors=True)
