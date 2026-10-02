import unittest
from datetime import datetime, timezone

from app.clips import (
    Clip,
    build_short_description,
    build_short_title,
    choose,
    is_meaningful,
    parse_clip,
    same_moment,
)

NODE = {
    "id": "160495036",
    "slug": "MildObeseFlyKappaWealth",
    "title": "ААААААА ЖЕНЩИИНАА",
    "viewCount": 2037,
    "durationSeconds": 7,
    "createdAt": "2026-09-28T15:31:22Z",
    "curator": {"displayName": "IMr_Deni", "login": "imr_deni"},
    "game": {"name": "Baldur's Gate 3"},
    "video": {"id": "2886356064", "title": "Стрим"},
    "videoOffsetSeconds": 5051,
}


def clip(clip_id, views, vod="v1", offset=100, duration=20):
    return Clip(id=clip_id, slug=f"s-{clip_id}", title="Момент", views=views, duration=duration, vod_id=vod,
                vod_offset=offset)


class ParseClipTest(unittest.TestCase):
    def test_graphql_node(self):
        c = parse_clip(NODE)
        self.assertEqual(
            (c.id, c.views, c.duration, c.category, c.vod_id, c.vod_offset, c.author),
            ("160495036", 2037, 7.0, "Baldur's Gate 3", "2886356064", 5051, "IMr_Deni"),
        )
        self.assertEqual(c.created_at, datetime(2026, 9, 28, 15, 31, 22, tzinfo=timezone.utc))
        self.assertEqual(c.span, (5051, 5058))
        self.assertEqual(c.url, "https://clips.twitch.tv/MildObeseFlyKappaWealth")

    def test_clip_without_vod(self):
        c = parse_clip({**NODE, "video": None, "videoOffsetSeconds": None})
        self.assertIsNone(c.vod_id)
        self.assertIsNone(c.span)

    def test_broken_node(self):
        self.assertIsNone(parse_clip({"title": "без id"}))


class ChooseTest(unittest.TestCase):
    def test_threshold_and_most_viewed_first(self):
        clips = [clip("a", 150, offset=0), clip("b", 2000, offset=500), clip("c", 99, offset=900)]
        self.assertEqual([c.id for c in choose(clips, 100)], ["b", "a"])

    def test_one_moment_gives_one_short_from_the_most_viewed_clip(self):
        clips = [clip("a", 300, offset=100), clip("b", 900, offset=110), clip("c", 200, offset=500)]
        self.assertEqual([c.id for c in choose(clips, 100)], ["b", "c"])

    def test_known_clips_and_their_moments_are_skipped(self):
        known = [clip("old", 0, offset=100)]
        clips = [clip("old", 5000, offset=100), clip("dup", 800, offset=105), clip("new", 300, offset=900)]
        self.assertEqual([c.id for c in choose(clips, 100, known)], ["new"])

    def test_different_records_or_unknown_time_are_different_moments(self):
        self.assertFalse(same_moment(clip("a", 1, vod="v1"), clip("b", 1, vod="v2")))
        self.assertFalse(same_moment(clip("a", 1, vod=None), clip("b", 1, vod=None)))
        self.assertTrue(same_moment(clip("a", 1, offset=100, duration=20), clip("b", 1, offset=119, duration=5)))
        self.assertFalse(same_moment(clip("a", 1, offset=100, duration=20), clip("b", 1, offset=120, duration=5)))


class ShortTitleTest(unittest.TestCase):
    def test_clip_title_and_streamer(self):
        self.assertEqual(
            build_short_title("ААААААА ЖЕНЩИИНАА", "Заквиель", "Baldur's Gate 3", "Стрим"),
            "ААААААА ЖЕНЩИИНАА | Заквиель",
        )

    def test_meaningless_title_falls_back_to_category_and_stream(self):
        self.assertFalse(is_meaningful("123123"))
        self.assertFalse(is_meaningful("ааа"))
        self.assertTrue(is_meaningful("ЛОР"))
        self.assertEqual(
            build_short_title("123123", "Заквиель", "Portal 2", "Проходим портал"),
            "Portal 2 | Проходим портал | Заквиель",
        )

    def test_long_title_keeps_streamer(self):
        title = build_short_title("очень длинное название клипа " * 10, "Заквиель", "Portal 2", "")
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith("… | Заквиель"))


class ShortDescriptionTest(unittest.TestCase):
    def test_credits_and_links(self):
        text = build_short_description(
            "ЛОР", "ZakvielChannel", "zakvielchannel", "30.09.2026", "Portal 2",
            "https://clips.twitch.tv/x", "NIKITA3618", "https://youtu.be/abc",
        )
        for line in (
            "ЛОР",
            "Клип со стрима ZakvielChannel от 30.09.2026: Portal 2.",
            "Автор клипа: NIKITA3618.",
            "Клип на Twitch: https://clips.twitch.tv/x",
            "Полный стрим: https://youtu.be/abc",
            "Twitch: https://www.twitch.tv/zakvielchannel",
            "Опубликовано с разрешения автора.",
            "#shorts",
        ):
            self.assertIn(line, text)

    def test_meaningless_title_and_missing_video(self):
        text = build_short_description("123123", "S", "s", "", "Portal 2", "https://clips.twitch.tv/x")
        self.assertTrue(text.startswith("Клип со стрима S: Portal 2."))
        self.assertNotIn("Полный стрим", text)
        self.assertNotIn("Автор клипа", text)


if __name__ == "__main__":
    unittest.main()
