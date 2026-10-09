import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db import KV, Streamer, adopt_legacy_state, all_streamers, as_utc, get_streamer, init_db, make_engine
except ImportError:  # без зависимостей из requirements.txt: эти тесты идут в тестовом образе
    raise unittest.SkipTest("нужны SQLAlchemy и aiosqlite")

# Схема базы, пока стример был один: его состояние лежало в общих ключах KV
OLD_SCHEMA = (
    "CREATE TABLE streamers (id INTEGER NOT NULL PRIMARY KEY, login VARCHAR(64) NOT NULL UNIQUE, "
    "display_name VARCHAR(128), youtube_channel_id VARCHAR(64), youtube_channel_title VARCHAR(256), youtube_token TEXT)",
    "CREATE TABLE kv (key VARCHAR(64) NOT NULL PRIMARY KEY, value TEXT NOT NULL)",
    "INSERT INTO streamers (login, display_name) VALUES ('zakvielchannel', 'ZakvielChannel')",
    "INSERT INTO kv (key, value) VALUES ('paused', '1'), ('consent_version', '3'), ('consent_prompted', '3'), "
    "('watch_since', '2026-10-01T13:56:05.464265+00:00'), ('youtube_checked_at', '2026-10-02T10:00:00+00:00'), "
    "('other', 'x')",
)


class LegacyStateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        async with self.engine.begin() as conn:
            for statement in OLD_SCHEMA:
                await conn.exec_driver_sql(statement)
        await init_db(self.engine)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def adopt(self, title_name):
        async with self.sessions() as session, session.begin():
            await adopt_legacy_state(session, await get_streamer(session, "zakvielchannel"), title_name)
        async with self.sessions() as session:
            streamer = await get_streamer(session, "zakvielchannel")
            keys = sorted(row.key for row in (await session.scalars(select(KV))).all())
        return streamer, keys

    async def test_old_database_moves_into_streamer(self):
        streamer, keys = await self.adopt("Заквиель")
        self.assertEqual(
            (streamer.paused, streamer.consent_version, streamer.consent_prompted, streamer.title_name),
            (True, 3, 3, "Заквиель"),
        )
        self.assertEqual(as_utc(streamer.watch_since), datetime(2026, 10, 1, 13, 56, 5, 464265, tzinfo=timezone.utc))
        self.assertEqual(as_utc(streamer.youtube_checked_at), datetime(2026, 10, 2, 10, tzinfo=timezone.utc))
        self.assertEqual(keys, ["other"])

    async def test_second_run_keeps_streamer_values(self):
        await self.adopt("Заквиель")
        async with self.sessions() as session, session.begin():
            session.add(KV(key="paused", value="0"))  # старая версия бота снова записала ключ
        streamer, keys = await self.adopt("Другое имя")
        self.assertEqual((streamer.paused, streamer.title_name), (True, "Заквиель"))
        self.assertEqual(keys, ["other"])

    async def watched(self):
        async with self.sessions() as session:
            return [streamer.login for streamer in await all_streamers(session, watched=True)]

    async def test_rows_left_from_old_settings_are_not_watched(self):
        async with self.engine.begin() as conn:  # строка осталась от прежнего значения TWITCH_CHANNEL
            await conn.exec_driver_sql("INSERT INTO streamers (login) VALUES ('old_channel')")
        await self.adopt("Заквиель")
        self.assertEqual(await self.watched(), ["zakvielchannel"])
        async with self.sessions() as session:
            self.assertEqual(len(await all_streamers(session)), 2)  # строка не удалена, её можно вернуть через /add

    async def test_streamers_added_in_bot_survive_rollback_and_upgrade(self):
        await self.adopt("Заквиель")
        async with self.sessions() as session, session.begin():
            added = await get_streamer(session, "newcomer")
            added.permitted_at = datetime(2026, 10, 9, tzinfo=timezone.utc)  # так отмечает /add
            session.add(KV(key="paused", value="0"))  # откат: старая версия бота снова записала ключ
        await self.adopt("Заквиель")
        self.assertEqual(await self.watched(), ["zakvielchannel", "newcomer"])

    async def test_new_streamer_has_no_legacy_state(self):
        async with self.sessions() as session, session.begin():
            await adopt_legacy_state(session, await get_streamer(session, "zakvielchannel"), "")
            streamer = await get_streamer(session, "another")
            await adopt_legacy_state(session, streamer, "")
        self.assertEqual((streamer.paused, streamer.consent_version, streamer.watch_since), (None, None, None))


class PublicNameTest(unittest.TestCase):
    def test_title_name_then_twitch_name(self):
        self.assertEqual(Streamer(login="zak", display_name="Zak", title_name="Заквиель").public_name, "Заквиель")
        self.assertEqual(Streamer(login="zak", display_name="Zak").public_name, "Zak")
        self.assertEqual(Streamer(login="zak").public_name, "zak")


if __name__ == "__main__":
    unittest.main()
