import asyncio
import json
import unittest
from datetime import datetime, timezone

try:
    from app.config import Settings, streamer_settings
    from app.twitch import TwitchError, live_states
except ImportError:  # без зависимостей из requirements.txt: эти тесты идут в тестовом образе
    raise unittest.SkipTest("нужны pydantic-settings и httpx")


def base_settings():
    return Settings(
        _env_file=None, telegram_bot_token="t", twitch_channel="zakvielchannel", secret_key="k",
        no_part_categories="Just Chatting,Minecraft", shorts_min_views=100,
    )


class StreamerSettingsTest(unittest.TestCase):
    def test_no_overrides_gives_common_settings(self):
        settings = base_settings()
        self.assertIs(streamer_settings(settings, None), settings)

    def test_overrides_apply_on_top_of_env(self):
        settings = base_settings()
        own = streamer_settings(settings, json.dumps({"shorts_min_views": 300, "no_part_categories": "Just Chatting"}))
        self.assertEqual((own.shorts_min_views, own.unnumbered_categories), (300, ["Just Chatting"]))
        self.assertEqual(own.publish_delay_min, settings.publish_delay_min)
        self.assertEqual(settings.shorts_min_views, 100)  # общие настройки не меняются

    def test_unknown_and_invalid_values_are_skipped(self):
        settings = base_settings()
        own = streamer_settings(settings, json.dumps({
            "telegram_bot_token": "чужой",  # общая настройка, стримеру её не задать
            "publish_privacy": "secret",
            "shorts_per_day": "много",
            "publish_delay_min": "15",
        }))
        self.assertEqual(
            (own.telegram_bot_token, own.publish_privacy, own.shorts_per_day, own.publish_delay_min),
            ("t", settings.publish_privacy, settings.shorts_per_day, 15),
        )

    def test_broken_json_is_ignored(self):
        settings = base_settings()
        self.assertEqual(streamer_settings(settings, "{не json").shorts_min_views, 100)


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeHttp:
    def __init__(self, users):
        self.users = users
        self.requests = []

    async def post(self, url, content, headers, timeout):
        self.requests.append(json.loads(content))
        return FakeResponse({"data": {"users": self.users}})


LIVE = {"login": "zakvielchannel", "stream": {"id": "1", "createdAt": "2026-10-02T13:13:00Z"},
        "broadcastSettings": {"title": " Спидран PORTAL "}}


class LiveStatesTest(unittest.TestCase):
    def test_one_request_for_all_channels(self):
        http = FakeHttp([LIVE, {"login": "second", "stream": None, "broadcastSettings": {"title": "x"}}, None])
        states = asyncio.run(live_states(http, ["zakvielchannel", "second", "missing"], "client"))
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(http.requests[0]["variables"], {"logins": ["zakvielchannel", "second", "missing"]})
        live = states["zakvielchannel"]
        self.assertEqual((live.stream_id, live.title), ("1", "Спидран PORTAL"))
        self.assertEqual(live.started_at, datetime(2026, 10, 2, 13, 13, tzinfo=timezone.utc))
        self.assertEqual((states["second"], states["missing"]), (None, None))

    def test_answer_in_other_order_is_an_error(self):
        http = FakeHttp([{**LIVE, "login": "second"}, LIVE])
        with self.assertRaises(TwitchError):
            asyncio.run(live_states(http, ["zakvielchannel", "second"], "client"))


if __name__ == "__main__":
    unittest.main()
