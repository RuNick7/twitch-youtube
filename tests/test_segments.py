import unittest

from app.segments import (
    SPLIT_LIMIT_SEC,
    Chapter,
    PlannedSegment,
    build_description,
    build_tags,
    build_title,
    category_warnings,
    fmt_duration,
    fmt_hms,
    fmt_spans,
    is_short_reason,
    normalize_chapters,
    number_parts,
    parse_vod_id,
    plan_segments,
    short_reason,
    skip_reason,
    split_by_titles,
    split_list,
    twitch_time_param,
)

KEYWORDS = split_list("во все тяжкие,звоните солу,сериал,серия,фильм,кино")


def skip(category, title, categories=("Watch Party",)):
    return skip_reason(category, title, keywords=KEYWORDS, title_categories=["Just Chatting"], categories=categories)


def spans(chapters):
    return [(c.start, c.end, c.title) for c in chapters]


def parts(segments, no_part=()):
    return [part for part, _ in number_parts(segments, no_part=no_part)]


def plan(*chapters, no_part=(), **kwargs):
    segments = plan_segments([Chapter(*c) for c in chapters], **kwargs)
    return [(s.start, s.end, s.category, part) for s, part in zip(segments, parts(segments, no_part))]


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

    def test_long_segment_of_no_part_category_still_gets_numbers(self):
        # иначе у двух роликов было бы одинаковое название
        self.assertEqual(
            plan((0, 13 * 3600, "Minecraft"), no_part=["Minecraft"]),
            [(0, 23400, "Minecraft", 1), (23400, 46800, "Minecraft", 2)],
        )

    def test_single_short_stream_is_kept(self):
        self.assertEqual(plan((0, 60, "A")), [(0, 60, "A", None)])

    def test_min_length_is_configurable(self):
        self.assertEqual(
            plan((0, 3600, "A"), (3600, 3660, "B"), (3660, 7200, "C"), min_sec=30),
            [(0, 3600, "A", None), (3600, 3660, "B", None), (3660, 7200, "C", None)],
        )


class NumberPartsTest(unittest.TestCase):
    SEGMENTS = [
        PlannedSegment(0, 10, "Dota 2", "T1"),
        PlannedSegment(10, 20, "Just Chatting", "T1"),
        PlannedSegment(20, 30, "Dota 2", "T2"),
        PlannedSegment(30, 40, "DOTA 2", "T3"),
    ]

    def test_part_and_total_by_category(self):
        self.assertEqual(number_parts(self.SEGMENTS), [(1, 3), (None, None), (2, 3), (3, 3)])

    def test_skipped_segments_are_not_counted(self):
        self.assertEqual(
            number_parts(self.SEGMENTS, [True, True, False, True]), [(1, 2), (None, None), (None, None), (2, 2)]
        )

    def test_single_uploaded_part_has_no_number(self):
        self.assertEqual(number_parts(self.SEGMENTS, [True, True, False, False]), [(None, None)] * 4)


class TitleTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(build_title("Minecraft", "Строим замок", "Заквиель"), "Minecraft | Строим замок | Заквиель")

    def test_part(self):
        self.assertEqual(
            build_title("Dota 2", "Турнир", "Заквиель", part=2, parts=3), "Dota 2 | Турнир | 2/3 | Заквиель"
        )

    def test_angle_brackets_and_spaces_removed(self):
        self.assertEqual(build_title("<Game>", "a  <b> c", "S"), "Game | a b c | S")

    def test_empty_stream_title(self):
        self.assertEqual(build_title("Dota 2", "", "Заквиель", part=1, parts=2), "Dota 2 | 1/2 | Заквиель")

    def test_long_title_is_shortened_keeping_category_part_and_streamer(self):
        long = "очень длинное название " * 10
        title = build_title("Dota 2", long, "Заквиель", part=1, parts=2)
        self.assertLessEqual(len(title), 100)
        category, shortened, part, streamer = title.split(" | ")
        self.assertEqual((category, part, streamer), ("Dota 2", "1/2", "Заквиель"))
        self.assertTrue(shortened.endswith("…"))
        self.assertTrue(long.startswith(shortened[:-1] + " "), shortened)  # обрезано по границе слова

    def test_extremely_long_category_still_fits(self):
        title = build_title("x" * 150, "название", "Заквиель", part=1, parts=2)
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith(" | 1/2 | Заквиель"))


class DescriptionTest(unittest.TestCase):
    def test_credit_link_and_fragment(self):
        text = build_description("Строим замок", "Стример", "streamer", "29.09.2026", "Minecraft", [(5025, 11400)])
        self.assertIn("Фрагмент стрима Стример от 29.09.2026: Minecraft, 1:23:45–3:10:00.", text)
        self.assertIn("https://www.twitch.tv/streamer", text)
        self.assertIn("Опубликовано с разрешения автора.", text)

    def test_limits_and_forbidden_characters(self):
        text = build_description("я<>" * 3000, "Стример", "bad<login>", "", "Minecraft", [(0, 3600)])
        self.assertLessEqual(len(text.encode("utf-8")), 5000)
        self.assertNotIn("<", text)
        self.assertIn("https://www.twitch.tv/badlogin", text)
        self.assertIn("Опубликовано с разрешения автора.", text)


    def test_joined_segment_lists_all_fragments(self):
        text = build_description("Стрим", "Стример", "streamer", "", "Just Chatting", [(0, 2403), (17917, 27310)])
        self.assertIn("Фрагменты стрима Стример: Just Chatting, 0:00–40:03, 4:58:37–7:35:10.", text)


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


class SplitListTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split_list(" a, b,,c , "), ["a", "b", "c"])
        self.assertEqual(split_list(""), [])
        self.assertEqual(split_list(None), [])


class SkipReasonTest(unittest.TestCase):
    def test_series_in_title_skips_just_chatting(self):
        reason = skip("Just Chatting", "ФРИКЛЕНД - ДАЛЬНОБОЙНАЯ ПУШКА +Во все тяжкие")
        self.assertIn("во все тяжкие", reason)

    def test_game_segment_of_same_stream_is_kept(self):
        self.assertIsNone(skip("Minecraft", "ФРИКЛЕНД - ДАЛЬНОБОЙНАЯ ПУШКА +Во все тяжкие"))

    def test_just_chatting_without_keywords_is_kept(self):
        self.assertIsNone(skip("Just Chatting", "ГЕНИАЛЬНЫЙ ПОДРУБ!"))

    def test_case_and_yo_are_ignored(self):
        self.assertIsNotNone(skip("just chatting", "ФЛ +Лучше ЗВОНИТЕ СОЛУ"))
        self.assertIsNotNone(skip("Just Chatting", "Во всё тяжкие 5 сезон"))
        self.assertIsNotNone(skip("Just Chatting", "ВО ВСЕ ТЯЖКИЕ 5 сезон, 6 серия (смотрит впервые)"))

    def test_category_always_skipped(self):
        self.assertIn("Watch Party", skip("Watch Party", "просто стрим"))

    def test_empty_lists_skip_nothing(self):
        self.assertIsNone(
            skip_reason("Just Chatting", "Во все тяжкие", keywords=[], title_categories=[], categories=[])
        )


class CategoryWarningsTest(unittest.TestCase):
    def test_risky_category(self):
        self.assertEqual(len(category_warnings("slots", ["Slots", "Virtual Casino"])), 1)

    def test_regular_category(self):
        self.assertEqual(category_warnings("Minecraft", ["Slots"]), [])



def titled(chapters, marks, no_part=(), **kwargs):
    segments = plan_segments(split_by_titles([Chapter(*c) for c in chapters], marks), **kwargs)
    return [(s.start, s.end, s.category, s.stream_title, part) for s, part in zip(segments, parts(segments, no_part))]


class SplitByTitlesTest(unittest.TestCase):
    def test_no_marks_keeps_chapters(self):
        chapters = [Chapter(0, 100, "A")]
        self.assertEqual(split_by_titles(chapters, []), chapters)

    def test_title_change_splits_chapter(self):
        result = split_by_titles([Chapter(0, 3600, "Minecraft")], [(0, "Строим"), (1800, "Взрываем")])
        self.assertEqual(
            [(c.start, c.end, c.title, c.stream_title) for c in result],
            [(0, 1800, "Minecraft", "Строим"), (1800, 3600, "Minecraft", "Взрываем")],
        )

    def test_first_title_applies_from_start_even_if_seen_late(self):
        result = split_by_titles([Chapter(0, 600, "A"), Chapter(600, 1200, "B")], [(300, "T")])
        self.assertEqual([(c.start, c.end, c.stream_title) for c in result], [(0, 600, "T"), (600, 1200, "T")])

    def test_repeated_title_and_marks_outside_vod_are_ignored(self):
        result = split_by_titles([Chapter(0, 1000, "A")], [(-30, "T"), (400, "T"), (5000, "U")])
        self.assertEqual([(c.start, c.end, c.stream_title) for c in result], [(0, 1000, "T")])


class PlanWithTitlesTest(unittest.TestCase):
    def test_category_and_title_both_cut(self):
        self.assertEqual(
            titled([(0, 3600, "Minecraft"), (3600, 7200, "Just Chatting")], [(0, "Строим"), (1800, "Взрываем")]),
            [
                (0, 1800, "Minecraft", "Строим", 1),
                (1800, 3600, "Minecraft", "Взрываем", 2),
                (3600, 7200, "Just Chatting", "Взрываем", None),
            ],
        )

    def test_quick_typo_fix_joins_previous(self):
        self.assertEqual(
            titled([(0, 3600, "A")], [(0, "Т1"), (1800, "Опечатка"), (1830, "Т2")]),
            [(0, 1830, "A", "Т1", 1), (1830, 3600, "A", "Т2", 2)],
        )

    def test_same_category_and_title_parts_are_numbered(self):
        chapters = [(0, 1800, "Dota 2"), (1800, 3600, "B"), (3600, 5400, "Dota 2")]
        self.assertEqual(
            [part for *_, part in titled(chapters, [(0, "T")])],
            [1, None, 2],
        )

    def test_no_part_categories(self):
        chapters = [(0, 3600, "Minecraft"), (3600, 7200, "Dota 2")]
        marks = [(0, "T1"), (1800, "T2"), (3600, "T3"), (5400, "T4")]
        self.assertEqual(
            [part for *_, part in titled(chapters, marks, no_part=["just chatting", "minecraft"])],
            [None, None, 1, 2],
        )

    def test_parts_are_counted_by_category_across_titles(self):
        chapters = [(0, 1800, "A"), (1800, 3600, "B"), (3600, 5400, "A")]
        self.assertEqual(
            titled(chapters, [(0, "T1"), (3000, "T2")]),
            [
                (0, 1800, "A", "T1", 1),
                (1800, 3000, "B", "T1", 1),
                (3000, 3600, "B", "T2", 2),
                (3600, 5400, "A", "T2", 2),
            ],
        )


class ShortReasonTest(unittest.TestCase):
    def test_threshold(self):
        self.assertEqual(short_reason(6 * 60 + 59, 7), "короче 7 мин")
        self.assertIsNone(short_reason(7 * 60, 7))
        self.assertIsNone(short_reason(10, 0))

    def test_recognized(self):
        self.assertTrue(is_short_reason(short_reason(60, 7)))
        self.assertFalse(is_short_reason(skip("Just Chatting", "Во все тяжкие")))
        self.assertFalse(is_short_reason(None))


class SkipBySegmentTitleTest(unittest.TestCase):
    def test_only_the_series_part_of_the_stream_is_skipped(self):
        segments = titled(
            [(0, 3600, "Just Chatting"), (3600, 7200, "Minecraft"), (7200, 10800, "Just Chatting")],
            [(0, "ФРИКЛЕНД - строим"), (7200, "Смотрим Во все тяжкие")],
            no_part=["Just Chatting", "Minecraft"],
        )
        reasons = [skip(category, title) for _, _, category, title, _ in segments]
        self.assertEqual([reason is None for reason in reasons], [True, True, False])


def joined(*chapters, **kwargs):
    segments = plan_segments([Chapter(*c) for c in chapters], join=True, **kwargs)
    return [(s.category, s.spans, s.duration, part) for s, part in zip(segments, parts(segments))]


class JoinRepeatedTest(unittest.TestCase):
    def test_same_category_of_one_stream_becomes_one_video(self):
        self.assertEqual(
            joined((0, 2400, "Just Chatting"), (2400, 18000, "Minecraft"), (18000, 27000, "Just Chatting")),
            [
                ("Just Chatting", [(0, 2400), (18000, 27000)], 11400, None),
                ("Minecraft", [(2400, 18000)], 15600, None),
            ],
        )

    def test_different_titles_are_not_joined(self):
        segments = plan_segments(
            split_by_titles(
                [Chapter(0, 2400, "Just Chatting"), Chapter(2400, 18000, "Minecraft"), Chapter(18000, 27000, "Just Chatting")],
                [(0, "Стрим"), (17000, "Смотрим сериал")],
            ),
            join=True,
        )
        self.assertEqual(
            [(s.category, s.stream_title, s.spans) for s in segments],
            [
                ("Just Chatting", "Стрим", [(0, 2400)]),
                ("Minecraft", "Стрим", [(2400, 17000)]),
                ("Minecraft", "Смотрим сериал", [(17000, 18000)]),
                ("Just Chatting", "Смотрим сериал", [(18000, 27000)]),
            ],
        )

    def test_misclick_is_still_absorbed_before_joining(self):
        self.assertEqual(
            joined((0, 3600, "A"), (3600, 3630, "B"), (3630, 7200, "C"), (7200, 9000, "B")),
            [("A", [(0, 3630)], 3630, None), ("C", [(3630, 7200)], 3570, None), ("B", [(7200, 9000)], 1800, None)],
        )

    def test_too_long_joined_video_is_split_across_fragments(self):
        hours = 3600
        result = joined((0, 8 * hours, "A"), (8 * hours, 9 * hours, "B"), (9 * hours, 17 * hours, "A"))
        self.assertEqual(result[0][:3], ("A", [(0, 8 * hours)], 8 * hours))
        self.assertEqual(result[1][:3], ("A", [(9 * hours, 17 * hours)], 8 * hours))
        self.assertEqual((result[0][3], result[1][3]), (1, 2))
        self.assertEqual(result[2][:3], ("B", [(8 * hours, 9 * hours)], hours))

    def test_split_point_inside_second_fragment(self):
        hours = 3600
        result = joined((0, 2 * hours, "A"), (2 * hours, 3 * hours, "B"), (3 * hours, 13 * hours, "A"))
        # 12 часов одной категории — два ролика по 6 часов: 2 + 4 и ещё 6
        self.assertEqual(result[0][1], [(0, 2 * hours), (3 * hours, 7 * hours)])
        self.assertEqual(result[1][1], [(7 * hours, 13 * hours)])
        self.assertTrue(all(duration <= SPLIT_LIMIT_SEC for _, _, duration, _ in result))

    def test_without_join_repeats_stay_separate(self):
        self.assertEqual(
            plan((0, 3600, "A"), (3600, 7200, "B"), (7200, 10800, "A")),
            [(0, 3600, "A", 1), (3600, 7200, "B", None), (7200, 10800, "A", 2)],
        )

    def test_fmt_spans(self):
        self.assertEqual(fmt_spans([(0, 2403), (17917, 27310)]), "0:00–40:03, 4:58:37–7:35:10")

if __name__ == "__main__":
    unittest.main()
