"""Нейросети в режиме наблюдения: настоящая база и разбор записи; Twitch, распознавание, Claude и Telegram — поддельные."""
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

try:
    import aiosqlite  # noqa: F401
    import greenlet  # noqa: F401
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app import analyst
    from app.ai import audio, claude, recognizer, speech
    from app.bot import ai_command
    from app.config import Settings
    from app.context import App
    from app.db import AiResult, AiSpend, AiTask, Segment, Streamer, as_utc, init_db, make_engine, utcnow
    from app.service import ingest_vod
    from app.tools import VodInfo
except ImportError:  # без зависимостей из requirements.txt: эти тесты идут в тестовом образе
    raise unittest.SkipTest("нужны зависимости бота")

HOUR = 3600
KEYS = {"openrouter_api_key": "or-key", "anthropic_api_key": "claude-key"}


class RecordingApp(App):
    async def notify(self, text, *, markup=None, reply_to=None, silent=False):
        self.sent.append(SimpleNamespace(text=text, reply_to=reply_to, silent=silent))
        return SimpleNamespace(message_id=len(self.sent) + 100)

    async def refresh_segment(self, segment_id):
        pass


class AnalystTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.engine = make_engine(self.data / "app.db")
        await init_db(self.engine)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.heard: list[tuple[int, float]] = []  # какие куски распознавались: (начало отрезка, начало куска)
        self.extracted: list[tuple[int, int]] = []
        self.asked: list[str] = []
        self.speech_cost = 0.002
        self.speech_error = None
        self.silent = False

        async def playlist_url(vod_id):
            return f"https://x/{vod_id}.m3u8"

        async def extract(source, folder, *, chunk_sec=audio.CHUNK_SEC, limit_sec=None, start_sec=0):
            self.extracted.append((start_sec, limit_sec))
            folder.mkdir(parents=True, exist_ok=True)
            chunks = []
            for number in range(-(-limit_sec // chunk_sec)):
                path = folder / f"chunk-{number:03d}.flac"
                path.write_bytes(b"flac")
                start = start_sec + number * chunk_sec
                chunks.append(audio.Chunk(path, start, min(start + chunk_sec, start_sec + limit_sec)))
            return chunks

        async def transcribe(recognizer_self, http, path, seconds, *, language="ru"):
            if self.speech_error:
                raise self.speech_error
            start = int(path.parent.name.split("-")[1])
            offset = int(path.stem.split("-")[1]) * audio.CHUNK_SEC
            self.heard.append((start, start + offset))
            segments = [] if self.silent else [{"start": 1.0, "end": 5.0, "text": f"фраза {offset}"}]
            return recognizer.Result({"segments": segments}, "openrouter", self.speech_cost)

        async def ask_json(client, *, system, user, schema, model, max_tokens, effort):
            self.asked.append(effort)
            if "moments" in schema["properties"]:
                start, end = re.search(r"Участок для разметки: (\S+)–(\S+)", user).groups()
                rows = [(start, "0:10:00", "talk", "приветствие"), ("0:10:00", "0:20:00", "video", "смотрят ролик"),
                        ("0:20:00", "0:40:00", "game", "осада замка"), ("0:40:00", end, "talk", "прощание")]
                data = {"moments": [{"start": a, "end": b, "kind": k, "topic": t, "confidence": "high"} for a, b, k, t in rows]}
            else:
                data = {"chapters": [{"moment": 0, "title": "Приветствие"}, {"moment": 1, "title": "Чужой ролик"},
                                     {"moment": 2, "title": "Осада замка"}, {"moment": 3, "title": "Прощание"}],
                        "topics": ["осада замка", "новый ПК"]}
            return data, claude.Usage(1, 100, 10, 0.001)

        for target, name, value in (
            (audio, "audio_playlist_url", playlist_url), (audio, "extract_chunks", extract),
            (recognizer.Recognizer, "transcribe", transcribe), (claude, "ask_json", ask_json),
            (claude, "make_client", lambda key: object()),
        ):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    def make_app(self, **settings):
        values = dict(_env_file=None, telegram_bot_token="t", telegram_owner_id=1, twitch_channel="somebody",
                      secret_key="k", data_dir=self.data, **KEYS)
        values.update(settings)
        app = RecordingApp(settings=Settings(**values), sessions=self.sessions, bot=None, http=None, youtube=None,
                           vault=None, tz=ZoneInfo("Europe/Moscow"))
        app.sent = []
        return app

    async def ingest(self, app, chapters=None, title="Стрим и фильм", overrides=None, vod_id="1"):
        async with self.sessions() as session, session.begin():
            if not await session.scalar(select(Streamer)):
                session.add(Streamer(login="somebody", title_name="Стример",
                                     overrides=json.dumps(overrides) if overrides else None))
        chapters = chapters or [("Dota 2", 0, HOUR), ("Just Chatting", HOUR, 2 * HOUR)]
        info = VodInfo(id=vod_id, title=title, duration=chapters[-1][2], uploader="Somebody", uploader_login="somebody",
                       started_at=None, is_live=False, playlist_url=None,
                       chapters=[{"title": c, "start_time": a, "end_time": b} for c, a, b in chapters])
        await ingest_vod(app, info)
        app.sent.clear()

    async def rows(self, model):
        async with self.sessions() as session:
            return list((await session.scalars(select(model))).all())

    async def task(self):
        [task] = await self.rows(AiTask)
        return task

    async def test_task_appears_only_when_neural_networks_are_on(self):
        for settings, overrides in (({"anthropic_api_key": ""}, None), ({"openrouter_api_key": ""}, None),
                                    ({"ai_daily_limit_usd": 0}, None), ({"ai_observe": False}, None),
                                    ({}, {"ai_observe": False})):
            async with self.engine.begin() as conn:
                for table in ("ai_tasks", "segments", "vods", "streamers"):
                    await conn.exec_driver_sql(f"DELETE FROM {table}")
            await self.ingest(self.make_app(**settings), overrides=overrides)
            self.assertEqual(await self.rows(AiTask), [], (settings, overrides))
        async with self.engine.begin() as conn:
            for table in ("segments", "vods", "streamers"):
                await conn.exec_driver_sql(f"DELETE FROM {table}")
        app = self.make_app(openrouter_api_key="", groq_api_key="g")
        await self.ingest(app)
        self.assertEqual((await self.task()).status, analyst.PENDING)
        self.assertTrue(app.ai_wake.is_set())

    async def test_nothing_to_upload_means_no_task(self):
        await self.ingest(self.make_app(), chapters=[("Just Chatting", 0, HOUR)])
        self.assertEqual(await self.rows(AiTask), [])

    async def test_proposals_are_sent_and_videos_stay_as_they_are(self):
        app = self.make_app()
        await self.ingest(app)
        async with self.sessions() as session:
            before = [(s.title, s.status, s.tg_message_id) for s in await self.rows(Segment)]
        self.assertTrue(await analyst.Analyst(app).tick())
        report, summary = app.sent
        self.assertEqual((report.reply_to, report.silent, summary.silent), (before[0][2], True, True))
        self.assertIn("ролик не меняется", report.text)
        self.assertIn("Название: Осада замка, новый ПК | Dota 2 #1 | Стример", report.text)
        self.assertIn(f"Сейчас: {before[0][0]}", report.text)
        self.assertIn("00:00 Приветствие\n10:00 Чужой ролик\n20:00 Осада замка\n40:00 Прощание", report.text)
        self.assertIn('videos/1?t=0h10m0s">10:00–20:00 записи</a>', report.text)
        self.assertIn("роликов 1. Расход на запись $0.01", summary.text)  # 6 кусков по $0.002 и два запроса
        self.assertEqual([(s.title, s.status) for s in await self.rows(Segment)], [row[:2] for row in before])
        # пропущенный разговор с фильмом не расшифровывался и не оплачивался
        self.assertEqual(self.extracted, [(0, HOUR)])
        self.assertEqual(len(self.heard), 6)
        self.assertEqual(self.asked, ["low", "low"])
        [result] = await self.rows(AiResult)
        self.assertEqual((result.title, result.videos), ("Осада замка, новый ПК | Dota 2 #1 | Стример", "[[600.0, 1200.0]]"))
        spent = await self.rows(AiSpend)
        self.assertAlmostEqual(sum(row.usd for row in spent), 6 * 0.002 + 2 * 0.001)
        self.assertEqual({row.kind for row in spent}, {"speech", "text"})
        self.assertEqual((await self.task()).status, analyst.DONE)
        self.assertFalse((self.data / "ai" / "1").exists())
        self.assertFalse(await analyst.Analyst(app).tick())

    async def test_category_without_part_numbers(self):
        app = self.make_app()
        await self.ingest(app, chapters=[("Minecraft", 0, HOUR)], title="Стрим")
        await analyst.Analyst(app).tick()
        self.assertIn("| Minecraft | Стример", app.sent[0].text)

    async def test_video_without_speech(self):
        self.silent = True
        app = self.make_app()
        await self.ingest(app)
        await analyst.Analyst(app).tick()
        self.assertIn("Речи в ролике не нашлось", app.sent[0].text)
        self.assertEqual(self.asked, [])
        [result] = await self.rows(AiResult)
        self.assertIsNone(result.title)

    async def test_daily_limit_stops_and_the_next_day_continues_without_paying_twice(self):
        clock = self.enterContext(mock.patch.object(
            analyst, "utcnow", return_value=datetime(2026, 11, 27, 22, tzinfo=timezone.utc),
        ))
        self.speech_cost = 0.02
        app = self.make_app(ai_daily_limit_usd=0.05)
        await self.ingest(app)
        worker = analyst.Analyst(app)
        await worker.tick()
        self.assertEqual(len(self.heard), 3)  # после третьего куска потрачено $0.06
        task = await self.task()
        self.assertEqual((task.status, task.attempts), (analyst.PENDING, 0))
        self.assertEqual(as_utc(task.retry_at), analyst.next_day(app))
        [notice] = app.sent
        self.assertIn("Дневной лимит нейросетей $0.05 выбран", notice.text)
        self.assertFalse(await worker.tick())  # сегодня больше не берётся
        extracted = len(self.extracted)
        await worker._update("1", retry_at=None)
        await worker.tick()  # тот же день: звук не качается, второго сообщения нет
        self.assertEqual((len(self.extracted), len(app.sent)), (extracted, 1))

        clock.return_value += timedelta(days=1)
        await worker.tick()
        self.assertEqual(len(self.heard), 6)  # только оставшиеся три куска
        self.assertEqual(self.asked, [])  # на разбор текста лимита уже не хватило
        self.assertEqual(len(app.sent), 2)  # о лимите — раз в день
        clock.return_value += timedelta(days=1)
        extracted = len(self.extracted)
        await worker.tick()
        self.assertEqual((len(self.heard), len(self.extracted)), (6, extracted))  # расшифровка уже есть
        self.assertEqual((await self.task()).status, analyst.DONE)
        self.assertIn("роликов 1. Расход на запись $0.12", app.sent[-1].text)
        self.assertEqual(sorted(start for _, start in self.heard), [0, 600, 1200, 1800, 2400, 3000])

    async def test_videos_already_reported_are_not_sent_again_after_the_limit(self):
        clock = self.enterContext(mock.patch.object(
            analyst, "utcnow", return_value=datetime(2026, 11, 27, 22, tzinfo=timezone.utc),
        ))
        app = self.make_app(ai_daily_limit_usd=0.03)
        await self.ingest(app, chapters=[("Dota 2", 0, HOUR), ("Hades", HOUR, 2 * HOUR)], title="Стрим")
        worker = analyst.Analyst(app)
        await worker.tick()
        self.assertEqual([("Dota 2 #1" in m.text, "Hades" in m.text) for m in app.sent], [(True, False), (False, False)])
        clock.return_value += timedelta(days=1)
        app.sent.clear()
        await worker.tick()
        report, summary = app.sent
        self.assertIn("Hades #1", report.text)
        self.assertIn("роликов 2", summary.text)
        self.assertEqual(len(await self.rows(AiResult)), 2)

    async def test_failure_is_retried_and_then_reported(self):
        self.speech_error = speech.TranscribeError("OpenRouter не ответил")
        app = self.make_app()
        await self.ingest(app)
        worker = analyst.Analyst(app)
        for attempt in (1, 2):
            await worker.tick()
            task = await self.task()
            self.assertEqual((task.status, task.attempts, app.sent), (analyst.PENDING, attempt, []))
            self.assertGreater(as_utc(task.retry_at), datetime.now(timezone.utc) + timedelta(minutes=25))
            await worker._update("1", retry_at=None)
        await worker.tick()
        task = await self.task()
        self.assertEqual((task.status, task.attempts), (analyst.FAILED, 3))
        [alert] = app.sent
        self.assertIn("OpenRouter не ответил", alert.text)
        self.assertIn("/ai https://www.twitch.tv/videos/1", alert.text)
        self.assertFalse(alert.silent)

    async def test_command_queues_a_processed_recording_again(self):
        app = self.make_app()
        await self.ingest(app)
        await analyst.Analyst(app).tick()
        answers = []

        async def answer(text, **kwargs):
            answers.append(text)

        async def command(args, target=app):
            await ai_command(SimpleNamespace(answer=answer), SimpleNamespace(args=args), target)
            return answers[-1]

        self.assertIn("Нейросети: наблюдение (ролики не меняются), сегодня $0.01 из $0.50", await command(None))
        self.assertIn("Сначала: /process", await command("https://www.twitch.tv/videos/999"))
        self.assertIn("Пришлите ссылку", await command("что-то"))
        self.assertIn("проверьте ключи", await command("https://www.twitch.tv/videos/1", self.make_app(anthropic_api_key="")))
        app.ai_wake.clear()
        self.assertIn("Разберу запись", await command("https://www.twitch.tv/videos/1"))
        self.assertEqual(((await self.task()).status, await self.rows(AiResult), app.ai_wake.is_set()),
                         (analyst.PENDING, [], True))
        app.sent.clear()
        await analyst.Analyst(app).tick()
        self.assertEqual(len(app.sent), 2)

    async def test_switched_off_after_processing(self):
        app = self.make_app()
        await self.ingest(app)
        async with self.sessions() as session, session.begin():
            (await session.scalar(select(Streamer))).overrides = json.dumps({"ai_observe": False})
        await analyst.Analyst(app).tick()
        self.assertEqual(((await self.task()).status, app.sent, self.heard), (analyst.DONE, [], []))

    async def test_status_line(self):
        self.assertIn("нужны ANTHROPIC_API_KEY", await analyst.status_text(self.make_app(anthropic_api_key="")))
        self.assertIn("AI_DAILY_LIMIT_USD=0", await analyst.status_text(self.make_app(ai_daily_limit_usd=0)))
        app = self.make_app()
        await self.ingest(app)
        self.assertIn("сегодня $0.00 из $0.50, записей в очереди 1", await analyst.status_text(app))

    def test_defaults(self):
        settings = Settings(_env_file=None, telegram_bot_token="t", twitch_channel="s", secret_key="k")
        self.assertEqual((settings.ai_observe, settings.ai_daily_limit_usd), (True, 0.5))
        self.assertFalse(analyst.enabled(settings))

    def test_tomorrow_starts_in_the_owner_time_zone(self):
        app = self.make_app()
        value = analyst.next_day(app).astimezone(app.tz)
        self.assertEqual((value.hour, value.minute), (0, 1))
        self.assertEqual(value.date(), utcnow().astimezone(app.tz).date() + timedelta(days=1))

    def test_long_report_fits_into_a_telegram_message(self):
        segment = Segment(vod_id="1", title="Старое <название>")
        lines = [f"{i:02d}:00 Длинное название главы про баланс игры и серверы" for i in range(200)]
        text = analyst.render_report(segment, "Новое <название>", False, lines, [(0, 120)])
        self.assertLessEqual(len(text), analyst.MESSAGE_MAX_CHARS)
        self.assertIn("…", text)
        self.assertIn("Новое &lt;название&gt;", text)
        self.assertIn("Старое &lt;название&gt;", text)
        self.assertIn("тем не нашлось", text)
        self.assertIn("чужое видео", text)  # предупреждение не обрезается вместе с главами


if __name__ == "__main__":
    unittest.main()
