"""Решение о публикации после проверки ролика: настоящая база, поддельный YouTube, без Telegram."""
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import aiosqlite  # noqa: F401
    import greenlet  # noqa: F401
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.checker import Checker
    from app.config import Settings
    from app.consent import CONSENT_VERSION, RESTRICTED_SINCE
    from app.context import App
    from app.db import Segment, Status, Streamer, Vod, get_warnings, init_db, make_engine, utcnow
    from app.ui import render_consent
except ImportError:  # без зависимостей из requirements.txt: эти тесты идут в тестовом образе
    raise unittest.SkipTest("нужны зависимости бота")


def video(blocked=0, age=False, duration="PT1H", privacy="private"):
    details = {"duration": duration}
    if blocked:
        details["regionRestriction"] = {"blocked": [f"C{i}" for i in range(blocked)]}
    if age:
        details["contentRating"] = {"ytRating": "ytAgeRestricted"}
    return {"status": {"uploadStatus": "processed", "privacyStatus": privacy}, "contentDetails": details}


class FakeYouTube:
    def __init__(self):
        self.item, self.published = None, []

    async def videos(self, token, ids):
        return {video_id: self.item for video_id in ids}

    async def set_privacy(self, token, video_id, privacy, status):
        self.published.append(video_id)
        return {"privacyStatus": privacy}


class Vault:
    def decrypt(self, value):
        return "token"


class RecordingApp(App):
    async def notify(self, text, **kwargs):
        self.sent.append((text, kwargs.get("silent", False)))

    async def refresh_segment(self, segment_id):
        pass


class CheckerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        await init_db(self.engine)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.youtube = FakeYouTube()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def prepare(self, consent=None, overrides=None, status=None, warnings=None, **settings):
        values = dict(_env_file=None, telegram_bot_token="t", twitch_channel="somebody", secret_key="k")
        values.update(settings)
        self.app = RecordingApp(settings=Settings(**values), sessions=self.sessions, bot=None, http=None,
                                youtube=self.youtube, vault=Vault(), tz=ZoneInfo("UTC"))
        self.app.sent = []
        async with self.sessions() as session, session.begin():
            streamer = Streamer(login="somebody", youtube_token="enc",
                                consent_version=CONSENT_VERSION if consent is None else consent,
                                overrides=json.dumps(overrides) if overrides else None)
            session.add(streamer)
            await session.flush()
            session.add(Vod(id="1", streamer_id=streamer.id, title="Стрим", duration=3600))
            await session.flush()
            segment = Segment(vod_id="1", idx=1, start=0, end=3600, category="Minecraft", title="Ролик",
                              youtube_id="yt1", status=status or Status.WAITING, warnings=warnings,
                              expected_duration=3600, monitor_until=utcnow() + timedelta(days=1))
            session.add(segment)
        self.streamer, self.segment_id = streamer, segment.id

    async def check(self, item):
        self.youtube.item = item
        async with self.sessions() as session:
            segment = await session.get(Segment, self.segment_id)
        await Checker(self.app).check(segment, item, self.streamer)
        async with self.sessions() as session:
            return await session.get(Segment, self.segment_id)

    async def test_age_and_few_countries_are_published_and_named(self):
        await self.prepare()
        row = await self.check(video(blocked=10, age=True))
        self.assertEqual((row.status, self.youtube.published), (Status.PUBLISHED, ["yt1"]))
        self.assertEqual(len(get_warnings(row)), 2)
        [(text, silent)] = self.app.sent
        self.assertIn("Опубликовано", text)
        self.assertIn("С ограничениями YouTube", text)
        self.assertIn("возрастное ограничение 18+", text)
        self.assertTrue(silent)

    async def test_clean_video_has_no_restrictions_in_the_message(self):
        await self.prepare()
        row = await self.check(video())
        self.assertEqual((row.status, get_warnings(row)), (Status.PUBLISHED, []))
        self.assertNotIn("ограничениями", self.app.sent[0][0])

    async def test_eleven_countries_wait_for_the_owner(self):
        await self.prepare()
        row = await self.check(video(blocked=11, age=True))
        self.assertEqual((row.status, self.youtube.published), (Status.REVIEW, []))
        self.assertIn("возрастное ограничение 18+", get_warnings(row))  # решая, владелец видит все ограничения
        self.assertIn("в 11 странах", self.app.sent[0][0])
        self.assertFalse(self.app.sent[0][1])

    async def test_old_consent_keeps_the_old_rules(self):
        await self.prepare(consent=RESTRICTED_SINCE - 1)
        row = await self.check(video(age=True))
        self.assertEqual((row.status, self.youtube.published), (Status.REVIEW, []))

    async def test_settings_can_forbid_each_restriction(self):
        await self.prepare(overrides={"auto_publish_age_restricted": False})
        self.assertEqual((await self.check(video(age=True))).status, Status.REVIEW)

    async def test_zero_countries_setting(self):
        await self.prepare(auto_publish_max_blocked=0)
        self.assertEqual((await self.check(video(blocked=1))).status, Status.REVIEW)

    async def test_manual_mode_publishes_nothing_by_itself(self):
        await self.prepare(auto_publish=False)
        row = await self.check(video(age=True))
        self.assertEqual((row.status, self.youtube.published), (Status.REVIEW, []))
        self.assertEqual(get_warnings(row), ["возрастное ограничение 18+"])  # без «автопубликация выключена»

    async def test_wrong_duration_still_waits(self):
        await self.prepare()
        self.assertEqual((await self.check(video(age=True, duration="PT10M"))).status, Status.REVIEW)

    async def test_small_block_after_publication_is_a_quiet_note_once(self):
        await self.prepare(status=Status.PUBLISHED)
        row = await self.check(video(blocked=3, privacy="public"))
        self.assertEqual(len(get_warnings(row)), 1)
        [(text, silent)] = self.app.sent
        self.assertTrue(silent and "появилось ограничение" in text, text)
        await self.check(video(blocked=3, privacy="public"))
        self.assertEqual(len(self.app.sent), 1)

    async def test_wide_block_after_publication_is_an_alert(self):
        await self.prepare(status=Status.PUBLISHED)
        await self.check(video(blocked=40, privacy="public"))
        [(text, silent)] = self.app.sent
        self.assertTrue(text.startswith("🔴") and "в 40 странах" in text and not silent, text)

    def test_consent_screen_says_what_is_published_by_itself(self):
        settings = Settings(_env_file=None, telegram_bot_token="t", twitch_channel="s", secret_key="k")
        text = render_consent(settings, "Стример")
        self.assertIn("ограничение 18+ и блокировка не больше чем в 10 странах публикации не мешают", text)
        off = settings.model_copy(update={"auto_publish_age_restricted": False, "auto_publish_max_blocked": 0})
        self.assertNotIn("не мешают", render_consent(off, "Стример"))


if __name__ == "__main__":
    unittest.main()
