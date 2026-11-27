import asyncio
import unittest

from app.ai import chapters, claude
from app.ai.chapters import VideoChapter
from app.ai.timeline import Moment

HOUR = 3600


def moment(start, end, topic="тема", kind="talk"):
    return Moment(start, end, kind, topic, "high")


def answer(*pairs, topics=()):
    return {"chapters": [{"moment": number, "title": title} for number, title in pairs], "topics": list(topics)}


def chapter(at, title, source=None):
    return VideoChapter(at, at if source is None else source, title)


class PositionTest(unittest.TestCase):
    def test_second_of_a_joined_video(self):
        spans = [(100, 400), (1000, 1600)]
        self.assertEqual([chapters.video_position(spans, t) for t in (100, 250, 400, 1000, 1030, 1600, 50)],
                         [0, 150, 300, 300, 330, 900, 0])

    def test_youtube_time_format(self):
        self.assertEqual([chapters.clock(t) for t in (0, 311.9, 3599, 3600, 3750)], ["00:00", "05:11", "59:59", "1:00:00", "1:02:30"])

    def test_lines_for_description(self):
        self.assertEqual(chapters.render([chapter(0, "Начало"), chapter(3750, "Конец")]), "00:00 Начало\n1:02:30 Конец")


class ParseTest(unittest.TestCase):
    moments = [moment(7000 + i * 300, 7300 + i * 300) for i in range(5)]
    spans = [(6914, 8500)]

    def parse(self, data):
        return chapters.parse_chapters(data, self.moments, self.spans)

    def test_time_comes_from_the_moment_and_first_chapter_starts_the_video(self):
        result = self.parse(answer((3, "Бой с боссом"), (1, "Прокачка")))
        self.assertEqual(result, [VideoChapter(0, 6914, "Прокачка"), VideoChapter(7900 - 6914, 7900, "Бой с боссом")])

    def test_garbage_duplicates_and_profanity_are_dropped(self):
        result = self.parse({"chapters": [
            "строка", {"moment": "1", "title": "а"}, {"moment": True, "title": "б"}, {"moment": 9, "title": "в"},
            {"moment": -1, "title": "г"}, {"moment": 0, "title": " Приветствие. "}, {"moment": 0, "title": "Повтор"},
            {"moment": 1, "title": "Блять, опять лаги"}, {"moment": 2, "title": "  "},
            {"moment": 4, "title": "Очень длинное <b>название</b> главы, которое никак не помещается в строку"},
        ]})
        self.assertEqual([c.title for c in result][0], "Приветствие")
        self.assertEqual(len(result), 2)
        self.assertLessEqual(len(result[1].title), chapters.TITLE_MAX_CHARS)
        self.assertNotIn("<", result[1].title)
        self.assertEqual(self.parse({}), [])
        self.assertEqual(self.parse({"chapters": None}), [])

    def test_chapters_inside_a_cut_out_part_do_not_repeat_a_time(self):
        moments = [moment(0, 300), moment(300, 600), moment(600, 900), moment(2000, 2300)]
        result = chapters.parse_chapters(answer((0, "а"), (2, "б"), (3, "в")), moments, [(0, 600), (2000, 2300)])
        self.assertEqual([(c.at, c.title) for c in result], [(0, "а"), (600, "б")])


class FitTest(unittest.TestCase):
    def fit(self, rows, duration=1000, **kwargs):
        return [(c.at, c.title) for c in chapters.fit([chapter(at, title) for at, title in rows], duration, **kwargs)]

    def test_short_chapter_joins_the_shorter_neighbor_and_takes_its_title(self):
        # «б» длится 40 с; слева 300 с, справа 160 с
        self.assertEqual(self.fit([(0, "а"), (300, "б"), (340, "в"), (500, "г")]), [(0, "а"), (300, "в"), (500, "г")])
        # а теперь короче сосед справа
        self.assertEqual(self.fit([(0, "а"), (100, "б"), (560, "в"), (600, "г")], 800), [(0, "а"), (100, "б"), (560, "г")])

    def test_short_first_and_last_chapters(self):
        self.assertEqual(self.fit([(0, "а"), (30, "б"), (300, "в"), (600, "г")]), [(0, "б"), (300, "в"), (600, "г")])
        self.assertEqual(self.fit([(0, "а"), (300, "б"), (600, "в"), (980, "г")]), [(0, "а"), (300, "б"), (600, "в")])

    def test_chain_of_short_chapters_leaves_none_short(self):
        result = chapters.fit([chapter(i * 40, f"тема {i}") for i in range(20)], 800)
        self.assertTrue(all(length >= chapters.MIN_CHAPTER_SEC for length in chapters.lengths(result, 800)), result)
        self.assertEqual(result[0].at, 0)

    def test_topic_longer_than_a_minute_keeps_its_chapter(self):
        rows = [(0, "значки"), (200, "воскрешение и заточка"), (278, "ежедневные задания"), (377, "экзамен")]
        self.assertEqual(self.fit(rows, 600), rows)

    def test_fewer_than_three_chapters_are_not_shown(self):
        self.assertEqual(self.fit([(0, "а"), (500, "б")]), [])
        self.assertEqual(self.fit([(0, "а"), (500, "б"), (550, "в")]), [])
        self.assertEqual(self.fit([]), [])

    def test_list_fits_into_the_description(self):
        many = [chapter(i * 120, "Длинное название главы про баланс игры") for i in range(200)]
        result = chapters.fit(many, 200 * 120)
        self.assertLessEqual(len(chapters.render(result).encode()), chapters.MAX_BYTES)
        self.assertGreater(len(result), 30)


class PromptTest(unittest.TestCase):
    def test_new_topic_means_new_chapter(self):
        self.assertIn("новая глава начинается, когда меняется тема разговора", chapters.SYSTEM)
        self.assertIn("от 2 до 8 минут", chapters.SYSTEM)

    def test_topics_are_short_and_stories_come_before_routine(self):
        self.assertIn("каждая тема — 1–2 слова и не длиннее 18 символов", chapters.SYSTEM)
        self.assertIn("важнее рутины", chapters.SYSTEM)
        self.assertEqual(chapters.SCHEMA["required"], ["chapters", "topics"])

    def test_moments_are_listed_with_video_time(self):
        text = chapters.build_user_prompt("Minecraft", [moment(2500, 2590, "строят базу", "game")], [(2444, 3000)])
        self.assertIn("Категория Twitch: Minecraft", text)
        self.assertIn("Длина ролика: 0:09:16", text)
        self.assertIn("0. с 0:00:56, 1.5 мин, game, high: строят базу", text)


class SuggestTest(unittest.TestCase):
    moments = [moment(i * 300, (i + 1) * 300, f"тема {i}") for i in range(10)]
    spans = [(0, 3000)]

    def run_suggest(self, *answers, moments=None, spans=None):
        queue, calls = list(answers), []

        async def ask(**kw):
            calls.append(kw)
            return queue.pop(0), claude.Usage(1, 100, 10, 0.001)

        found, topics, usage = asyncio.run(chapters.suggest(
            ask, category="Just Chatting", moments=self.moments if moments is None else moments,
            spans=spans or self.spans, effort="medium"))
        self.topics = topics
        return found, usage, calls

    def test_good_answer(self):
        found, usage, calls = self.run_suggest(answer((0, "а"), (3, "б"), (6, "в"), topics=["замок", "сыр"]))
        self.assertEqual([(c.at, c.title) for c in found], [(0, "а"), (900, "б"), (1800, "в")])
        self.assertEqual((usage.requests, self.topics), (1, ["замок", "сыр"]))
        self.assertEqual((calls[0]["schema"], calls[0]["system"], calls[0]["effort"]), (chapters.SCHEMA, chapters.SYSTEM, "medium"))

    def test_answer_that_stops_halfway_is_requested_again_and_the_better_one_wins(self):
        half = answer((0, "а"), (1, "б"), (2, "в"), topics=["начало"])
        full = answer((0, "а"), (3, "б"), (6, "в"), topics=["весь ролик"])
        found, usage, _ = self.run_suggest(half, full)
        self.assertEqual(([c.at for c in found], usage.requests, self.topics), ([0, 900, 1800], 2, ["весь ролик"]))
        found, usage, _ = self.run_suggest(half, answer())
        self.assertEqual(([c.at for c in found], usage.requests, self.topics), ([0, 300, 600], 2, ["начало"]))

    def test_short_video_has_topics_but_no_chapters(self):
        found, usage, _ = self.run_suggest(answer((0, "а"), topics=["замок"]), moments=self.moments[:2], spans=[(0, 600)])
        self.assertEqual((found, self.topics, usage.requests), ([], ["замок"], 1))
        found, usage, _ = self.run_suggest(answer((0, "а"), topics=["замок"]), moments=self.moments[:2])
        self.assertEqual(usage.requests, 1)  # длинная «глава» из двух моментов — не повод повторять

    def test_no_moments_no_request(self):
        found, usage, calls = self.run_suggest(moments=[])
        self.assertEqual((found, self.topics, usage.requests, calls), ([], [], 0, []))


if __name__ == "__main__":
    unittest.main()
