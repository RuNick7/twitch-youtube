"""Разбор записи на сегменты по настройкам бота: настоящая база, без Telegram, Twitch и YouTube."""
import json
import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import aiosqlite  # noqa: F401
    import greenlet  # noqa: F401
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import Settings
    from app.context import App
    from app.db import Segment, Streamer, init_db, make_engine
    from app.service import ingest_vod
    from app.tools import VodInfo
except ImportError:  # без зависимостей из requirements.txt: эти тесты идут в тестовом образе
    raise unittest.SkipTest("нужны зависимости бота")

HOUR = 3600
CHAPTERS = [
    {"start_time": 0, "end_time": HOUR, "title": "Minecraft"},
    {"start_time": HOUR, "end_time": HOUR + 600, "title": "Just Chatting"},  # 10 минут разговора
    {"start_time": HOUR + 600, "end_time": 2 * HOUR, "title": "Minecraft"},
    {"start_time": 2 * HOUR, "end_time": 4 * HOUR, "title": "Dota 2"},  # два часа другой игры
    {"start_time": 4 * HOUR, "end_time": 5 * HOUR, "title": "Minecraft"},
]


class IngestJoinTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        await init_db(self.engine)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def videos(self, overrides=None):
        async with self.sessions() as session, session.begin():
            session.add(Streamer(login="somebody", overrides=json.dumps(overrides) if overrides else None))
        settings = Settings(_env_file=None, telegram_bot_token="t", twitch_channel="somebody", secret_key="k")
        app = App(settings=settings, sessions=self.sessions, bot=None, http=None, youtube=None, vault=None, tz=ZoneInfo("UTC"))
        info = VodInfo(id="1", title="Стрим", duration=5 * HOUR, uploader="Somebody", uploader_login="somebody",
                       started_at=None, is_live=False, chapters=CHAPTERS, playlist_url=None)
        await ingest_vod(app, info)
        async with self.sessions() as session:
            rows = (await session.scalars(select(Segment).order_by(Segment.idx))).all()
        return [
            (row.category, [tuple(span) for span in json.loads(row.ranges)] if row.ranges else [(row.start, row.end)])
            for row in rows
        ]

    async def test_only_a_short_break_is_joined_by_default(self):
        self.assertEqual(await self.videos(), [
            ("Minecraft", [(0, 3600), (4200, 7200)]),
            ("Just Chatting", [(3600, 4200)]),
            ("Dota 2", [(7200, 14400)]),
            ("Minecraft", [(14400, 18000)]),
        ])

    async def test_streamer_can_set_a_longer_or_no_break(self):
        joined = await self.videos({"join_max_gap_min": 120})
        self.assertEqual(joined[0], ("Minecraft", [(0, 3600), (4200, 7200), (14400, 18000)]))

    async def test_zero_break_keeps_every_return_separate(self):
        self.assertEqual(len(await self.videos({"join_max_gap_min": 0})), 5)


if __name__ == "__main__":
    unittest.main()
