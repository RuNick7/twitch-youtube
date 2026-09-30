import unittest

from app.segments import (
    SPLIT_LIMIT_SEC,
    Chapter,
    build_description,
    build_tags,
    build_title,
    fmt_duration,
    fmt_hms,
    normalize_chapters,
    parse_vod_id,
    plan_segments,
    twitch_time_param,
)


def spans(chapters):
    return [(c.start, c.end, c.title) for c in chapters]


def plan(*chapters, **kwargs):
    return [(s.start, s.end, s.category, s.part) for s in plan_segments([Chapter(*c) for c in chapters], **kwargs)]


class NormalizeChaptersTest(unittest.TestCase):
    def test_no_chapters_gives_whole_vod(self):
        self.assertEqual(spans(normalize_chapters([], 3600)), [(0, 3600, "Стрим")])

    def test_no_chapters_and_no_duration(self):
        self.assertEqual(normalize_chapters(None, 0), [])

    def test_single_chapter_without_times(self):
        # так yt-dlp отдаёт стрим, где категория не менялась
        self.assertEqual(spans(normalize_chapters([{"title": "Minecraft"}], 7200)), [(0, 7200, "Minecraft")])

    def test_twitch_moments(self):
        raw = [
            {"start_time": 0, "end_time": 1800, "title": "Just Chatting"},
            {"start_time": 1800, "end_time": 9000, "title": "Minecraft"},
        ]
        self.assertEqual(
            spans(normalize_chapters(raw, 9000)), [(0, 1800, "Just Chatting"), (1800, 9000, "Minecraft")]
        )

    def test_gaps_and_offsets_are_closed(self):
        raw = [{"start_time": 5, "end_time": 100, "title": "A"}, {"start_time": 120, "end_time": 300, "title": "B"}]
        self.assertEqual(spans(normalize_chapters(raw, 400)), [(0, 120, "A"), (120, 400, "B")])

    def test_missing_times_are_inferred(self):
        raw = [{"title": "A"}, {"start_time": 600, "title": "B"}]
        self.assertEqual(spans(normalize_chapters(raw, 1200)), [(0, 600, "A"), (600, 1200, "B")])

    def test_chapter_after_end_is_dropped(self):
        raw = [{"start_time": 0, "title": "A"}, {"start_time": 5000, "title": "B"}]
        self.assertEqual(spans(normalize_chapters(raw, 3600)), [(0, 3600, "A")])

    def test_empty_title_gets_fallback(self):
        self.assertEqual(spans(normalize_chapters([{"start_time": 0, "title": " "}], 60)), [(0, 60, "Стрим")])


class PlanSegmentsTest(unittest.TestCase):
    def test_each_category_change_is_a_segment(self):
        self.assertEqual(
            plan((0, 1800, "Just Chatting"), (1800, 9000, "Minecraft"), (9000, 12600, "Dota 2")),
            [(0, 1800, "Just Chatting", None), (1800, 9000, "Minecraft", None), (9000, 12600, "Dota 2", None)],
        )

    def test_short_middle_segment_joins_previous(self):
        self.assertEqual(
            plan((0, 3600, "A"), (3600, 3660, "B"), (3660, 7200, "C")),
            [(0, 3660, "A", None), (3660, 7200, "C", None)],
        )

    def test_short_first_segment_joins_next(self):
        self.assertEqual(plan((0, 60, "A"), (60, 3600, "B")), [(0, 3600, "B", None)])

    def test_short_last_segment_joins_previous(self):
        self.assertEqual(plan((0, 3600, "A"), (3600, 3650, "B")), [(0, 3650, "A", None)])

    def test_misclick_between_same_game_joins_everything(self):
        self.assertEqual(plan((0, 3600, "A"), (3600, 3630, "B"), (3630, 7200, "A")), [(0, 7200, "A", None)])

    def test_repeated_category_gets_parts(self):
        self.assertEqual(
            plan((0, 3600, "A"), (3600, 7200, "B"), (7200, 10800, "A")),
            [(0, 3600, "A", 1), (3600, 7200, "B", None), (7200, 10800, "A", 2)],
        )

    def test_long_segment_is_split_into_equal_parts(self):
        result = plan((0, 13 * 3600, "A"))
        self.assertEqual(result, [(0, 23400, "A", 1), (23400, 46800, "A", 2)])
        self.assertTrue(all(end - start <= SPLIT_LIMIT_SEC for start, end, _, _ in result))

    def test_single_short_stream_is_kept(self):
        self.assertEqual(plan((0, 60, "A")), [(0, 60, "A", None)])

    def test_min_length_is_configurable(self):
        self.assertEqual(
            plan((0, 3600, "A"), (3600, 3660, "B"), (3660, 7200, "C"), min_sec=30),
            [(0, 3600, "A", None), (3600, 3660, "B", None), (3660, 7200, "C", None)],
        )


class TitleTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(build_title("Minecraft", "Строим замок", "Стример"), "Minecraft — Строим замок | Стример")

    def test_part(self):
        self.assertEqual(
            build_title("Minecraft", "Строим замок", "Стример", part=2),
            "Minecraft (часть 2) — Строим замок | Стример",
        )

    def test_angle_brackets_and_spaces_removed(self):
        self.assertEqual(build_title("<Game>", "a  <b> c", "S"), "Game — a b c | S")

    def test_empty_stream_title(self):
        self.assertEqual(build_title("Minecraft", "", "Стример"), "Minecraft | Стример")

    def test_long_title_is_shortened_keeping_game_and_streamer(self):
        title = build_title("Minecraft", "очень длинное название " * 10, "Стример")
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.startswith("Minecraft — очень"))
        self.assertTrue(title.endswith("… | Стример"))

    def test_extremely_long_category_still_fits(self):
        self.assertLessEqual(len(build_title("x" * 150, "название", "Стример")), 100)


class DescriptionTest(unittest.TestCase):
    def test_credit_link_and_fragment(self):
        text = build_description("Строим замок", "Стример", "streamer", "29.09.2026", "Minecraft", "1:23:45", "3:10:00")
        self.assertIn("Фрагмент стрима Стример от 29.09.2026: Minecraft, 1:23:45–3:10:00.", text)
        self.assertIn("https://www.twitch.tv/streamer", text)
        self.assertIn("Опубликовано с разрешения автора.", text)

    def test_limits_and_forbidden_characters(self):
        text = build_description("я<>" * 3000, "Стример", "bad<login>", "", "Minecraft", "0:00", "1:00:00")
        self.assertLessEqual(len(text.encode("utf-8")), 5000)
        self.assertNotIn("<", text)
        self.assertIn("https://www.twitch.tv/badlogin", text)
        self.assertIn("Опубликовано с разрешения автора.", text)


class TagsTest(unittest.TestCase):
    def test_duplicates_and_empty_values_are_skipped(self):
        self.assertEqual(
            build_tags("Minecraft", None, "", "Стример", "стример", "стрим"), ["Minecraft", "Стример", "стрим"]
        )

    def test_total_length_limit(self):
        tags = build_tags(*[f"тег номер {i}" for i in range(200)])
        cost = sum(len(tag) + 2 for tag in tags) + len(tags) - 1
        self.assertLessEqual(cost, 500)
        self.assertGreater(len(tags), 10)


class FormatTest(unittest.TestCase):
    def test_hms(self):
        self.assertEqual(fmt_hms(5025), "1:23:45")
        self.assertEqual(fmt_hms(59), "0:59")
        self.assertEqual(fmt_hms(0), "0:00")

    def test_duration(self):
        self.assertEqual(fmt_duration(6360), "1 ч 46 мин")
        self.assertEqual(fmt_duration(3600), "1 ч")
        self.assertEqual(fmt_duration(720), "12 мин")
        self.assertEqual(fmt_duration(45), "45 с")

    def test_twitch_time_param(self):
        self.assertEqual(twitch_time_param(5025), "1h23m45s")


class ParseVodIdTest(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(parse_vod_id("https://www.twitch.tv/videos/2567890123"), "2567890123")
        self.assertEqual(parse_vod_id("https://www.twitch.tv/videos/2567890123?t=1h2m3s"), "2567890123")
        self.assertEqual(parse_vod_id("twitch.tv/videos/2567890123"), "2567890123")
        self.assertEqual(parse_vod_id("v2567890123"), "2567890123")
        self.assertEqual(parse_vod_id(" 2567890123 "), "2567890123")

    def test_not_a_vod(self):
        self.assertIsNone(parse_vod_id("https://www.twitch.tv/somechannel"))
        self.assertIsNone(parse_vod_id(""))
        self.assertIsNone(parse_vod_id(None))


if __name__ == "__main__":
    unittest.main()
