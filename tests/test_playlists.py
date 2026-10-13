"""Плейлисты: какой плейлист канала взять, чтобы не создавать второй с тем же названием."""

import unittest
from datetime import datetime, timezone

try:
    from app.db import Playlist
    from app.playlists import _own_or_shared, oldest_with_title
    from app.youtube import ChannelPlaylist, _parse_time
except ImportError:  # без зависимостей из requirements.txt: эти тесты идут в тестовом образе
    raise unittest.SkipTest("нужны зависимости из requirements.txt")


def at(day):
    return datetime(2026, 10, day, tzinfo=timezone.utc)


class OldestWithTitleTest(unittest.TestCase):
    def test_no_such_title(self):
        existing = {"PL1": ChannelPlaylist("Minecraft | Заквиель", at(1))}
        self.assertIsNone(oldest_with_title(existing, "Dota 2 | Заквиель"))
        self.assertIsNone(oldest_with_title({}, "Dota 2 | Заквиель"))

    def test_the_only_one(self):
        existing = {"PL1": ChannelPlaylist("Minecraft | Заквиель", at(1)), "PL2": ChannelPlaylist("Dota 2 | Заквиель", at(2))}
        self.assertEqual(oldest_with_title(existing, "Dota 2 | Заквиель"), "PL2")

    def test_oldest_of_duplicates(self):
        existing = {
            "PL9": ChannelPlaylist("Dota 2 | Заквиель", at(5)),
            "PL3": ChannelPlaylist("Dota 2 | Заквиель", at(2)),
            "PL7": ChannelPlaylist("Dota 2 | Заквиель", at(9)),
        }
        self.assertEqual(oldest_with_title(existing, "Dota 2 | Заквиель"), "PL3")

    def test_case_and_spaces_do_not_matter(self):
        existing = {"PL1": ChannelPlaylist("  dota 2 |  ЗАКВИЕЛЬ ", at(1))}
        self.assertEqual(oldest_with_title(existing, "Dota 2 | Заквиель"), "PL1")

    def test_playlist_without_time_is_the_last_choice(self):
        existing = {"PL1": ChannelPlaylist("Dota 2 | Заквиель", None), "PL2": ChannelPlaylist("Dota 2 | Заквиель", at(3))}
        self.assertEqual(oldest_with_title(existing, "Dota 2 | Заквиель"), "PL2")
        self.assertEqual(oldest_with_title({"PL1": ChannelPlaylist("Dota 2 | Заквиель", None)}, "Dota 2 | Заквиель"), "PL1")


class ParseTimeTest(unittest.TestCase):
    def test_youtube_formats(self):
        self.assertEqual(_parse_time("2026-10-02T00:00:00Z"), at(2))
        self.assertEqual(_parse_time("2026-10-02T00:00:00.000Z"), at(2))
        self.assertEqual(_parse_time("2026-10-02T00:00:00"), at(2))

    def test_missing_or_broken(self):
        self.assertIsNone(_parse_time(None))
        self.assertIsNone(_parse_time("вчера"))


class OwnOrSharedTest(unittest.TestCase):
    def test_own_playlist_first_then_mates(self):
        rows = [
            Playlist(id=1, streamer_id=1, category="dota 2", youtube_id="PL1"),
            Playlist(id=2, streamer_id=1, category="minecraft", youtube_id="PL2"),
            Playlist(id=3, streamer_id=2, category="minecraft", youtube_id="PL3"),
        ]
        chosen = {category: row.youtube_id for category, row in _own_or_shared(rows, 2).items()}
        self.assertEqual(chosen, {"dota 2": "PL1", "minecraft": "PL3"})
        chosen = {category: row.youtube_id for category, row in _own_or_shared(rows, 1).items()}
        self.assertEqual(chosen, {"dota 2": "PL1", "minecraft": "PL2"})


if __name__ == "__main__":
    unittest.main()
