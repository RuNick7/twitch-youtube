"""Два канала Twitch одного стримера на одном YouTube-канале: кто считается вместе и общие номера частей."""

import tempfile
import unittest
from pathlib import Path

try:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db import Segment, Status, Streamer, Vod, channel_mates, init_db, make_engine, utcnow
    from app.service import upload_counts
except ImportError:  # без зависимостей из requirements.txt: эти тесты идут в тестовом образе
    raise unittest.SkipTest("нужны зависимости из requirements.txt")


class MatesCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        await init_db(self.engine)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def add(self, login, name, channel="UC1", **fields):
        """Стример с подключённым YouTube-каналом; channel=None — канал не подключён."""
        async with self.sessions() as session, session.begin():
            streamer = Streamer(
                login=login,
                title_name=name,
                youtube_channel_id=channel,
                youtube_token="token" if channel else None,
                **fields,
            )
            session.add(streamer)
        return streamer

    async def mates(self, streamer):
        async with self.sessions() as session:
            return [mate.login for mate in await channel_mates(session, streamer)]

    async def add_segments(self, streamer, vod_id, *rows):
        """rows — (категория, статус, kind)."""
        async with self.sessions() as session, session.begin():
            session.add(Vod(id=vod_id, streamer_id=streamer.id, title="Стрим", duration=3600))
            await session.flush()
            for idx, (category, status, kind) in enumerate(rows, 1):
                session.add(
                    Segment(vod_id=vod_id, idx=idx, start=0, end=600, category=category, title="x", status=status, kind=kind)
                )


class ChannelMatesTest(MatesCase):
    async def test_same_channel_and_name(self):
        main = await self.add("main", "Заквиель")
        second = await self.add("second", "  заквиель ")
        self.assertEqual(await self.mates(main), ["main", "second"])
        self.assertEqual(await self.mates(second), ["main", "second"])

    async def test_other_name_or_other_channel_is_another_streamer(self):
        main = await self.add("main", "Заквиель")
        await self.add("friend", "Друг")
        await self.add("elsewhere", "Заквиель", channel="UC2")
        self.assertEqual(await self.mates(main), ["main"])

    async def test_name_falls_back_to_twitch_name(self):
        main = await self.add("main", None, display_name="Zakviel")
        await self.add("second", "zakviel")
        self.assertEqual(await self.mates(main), ["main", "second"])

    async def test_without_youtube_channel_streamer_is_alone(self):
        await self.add("main", "Заквиель")
        waiting = await self.add("second", "Заквиель", channel=None)
        self.assertEqual(await self.mates(waiting), ["second"])
        self.assertEqual(await self.mates(await self.add("third", "Заквиель")), ["main", "third"])

    async def test_removed_channel_still_counts(self):
        main = await self.add("main", "Заквиель")
        await self.add("second", "Заквиель", removed_at=utcnow())
        self.assertEqual(await self.mates(main), ["main", "second"])


class SharedNumberingTest(MatesCase):
    async def test_parts_are_counted_across_channels_of_one_streamer(self):
        main = await self.add("main", "Заквиель")
        second = await self.add("second", "Заквиель")
        friend = await self.add("friend", "Друг")
        await self.add_segments(
            main,
            "1",
            ("Dota 2", Status.PUBLISHED, None),
            ("dota 2", Status.FORGOTTEN, None),
            ("Dota 2", Status.SKIPPED, None),  # пропущенный номера не получал
            ("Dota 2", Status.PUBLISHED, "short"),  # у Shorts номеров нет
        )
        await self.add_segments(second, "2", ("Dota 2", Status.QUEUED, None), ("Minecraft", Status.QUEUED, None))
        await self.add_segments(friend, "3", ("Dota 2", Status.PUBLISHED, None))
        async with self.sessions() as session:
            self.assertEqual(await upload_counts(session, main), {"dota 2": 3, "minecraft": 1})
            self.assertEqual(await upload_counts(session, second), {"dota 2": 3, "minecraft": 1})
            self.assertEqual(await upload_counts(session, friend), {"dota 2": 1})


if __name__ == "__main__":
    unittest.main()
