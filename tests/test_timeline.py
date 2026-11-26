import asyncio
import unittest

from app.ai import claude, timeline
from app.ai.timeline import Context, Moment
from app.ai.transcript import Phrase


def moment(start, end, kind="talk", topic="тема", confidence="high"):
    return Moment(start, end, kind, topic, confidence)


class PromptTest(unittest.TestCase):
    def test_game_dialogue_and_donations_are_not_video(self):
        self.assertIn("кат-сцены", timeline.SYSTEM)
        self.assertIn("озвучка донатов", timeline.SYSTEM)
        self.assertIn("если категория Twitch — игра", timeline.SYSTEM)


class ParseTimeTest(unittest.TestCase):
    def test_formats(self):
        cases = {"1:02:30": 3750, "62:30": 3750, "0:05:00": 300, "3750": 3750, " 1:00:00 ": 3600, 90: 90, 12.5: 12.5, "1:00:00.5": 3600.5}
        for value, expected in cases.items():
            self.assertEqual(timeline.parse_time(value), expected, value)

    def test_garbage(self):
        for value in (None, "", "час", "1:2:3:4", "1::30", "-5", True, "1:xx"):
            self.assertIsNone(timeline.parse_time(value), repr(value))


class ParseMomentsTest(unittest.TestCase):
    def item(self, start, end, kind="talk", topic="тема", confidence="high"):
        return {"start": start, "end": end, "kind": kind, "topic": topic, "confidence": confidence}

    def test_clamped_sorted_and_trimmed(self):
        data = {"moments": [self.item("0:30:00", "1:10:00", "game"), self.item("0:00:00", "0:35:00", "talk"),
                            self.item("1:09:00", "1:30:00", "video")]}
        moments = timeline.parse_moments(data, 0, 3600)
        self.assertEqual([(m.start, m.end, m.kind) for m in moments],
                         [(0, 2100, "talk"), (2100, 3600, "game")])

    def test_bad_items_and_values(self):
        data = {"moments": [
            "строка", {"start": "?", "end": "0:10:00", "kind": "talk", "topic": "а"},
            self.item("0:00:00", "0:10:00", topic="   "),
            self.item("0:20:00", "0:10:00"),  # конец раньше начала
            self.item("0:00:00", "0:10:00", kind="кино", confidence="точно", topic="  Обсуждают <b>новый</b>  ПК "),
        ]}
        moments = timeline.parse_moments(data, 0, 3600)
        self.assertEqual(len(moments), 1)
        self.assertEqual((moments[0].kind, moments[0].confidence), ("other", "low"))
        self.assertNotIn("<", moments[0].topic)
        self.assertNotIn(">", moments[0].topic)
        self.assertTrue(moments[0].topic.startswith("Обсуждают") and moments[0].topic.endswith("ПК"))

    def test_missing_moments(self):
        self.assertEqual(timeline.parse_moments({}, 0, 100), [])
        self.assertEqual(timeline.parse_moments({"moments": None}, 0, 100), [])

    def test_long_topic_is_cut(self):
        moments = timeline.parse_moments({"moments": [self.item("0:00:00", "0:10:00", topic="я" * 500)]}, 0, 3600)
        self.assertEqual(len(moments[0].topic), timeline.TOPIC_MAX_CHARS)


class MergeTest(unittest.TestCase):
    def test_same_kind_and_topic_merge_but_new_topic_stays(self):
        merged = timeline.merge_same([moment(0, 600, "talk", "Новый ПК"), moment(600, 1200, "talk", "новый  пк!"),
                                      moment(1200, 1800, "talk", "Поездка в Японию")])
        self.assertEqual([(m.start, m.end, m.topic) for m in merged], [(0, 1200, "Новый ПК"), (1200, 1800, "Поездка в Японию")])

    def test_same_topic_but_other_kind_stays_apart(self):
        self.assertEqual(len(timeline.merge_same([moment(0, 600, "game", "а"), moment(600, 1200, "talk", "а")])), 2)

    def test_short_moment_joins_previous_or_next(self):
        merged = timeline.merge_short([moment(0, 60, "talk", "а"), moment(60, 900, "game", "б"), moment(900, 960, "talk", "в"),
                                       moment(960, 1800, "video", "г")])
        self.assertEqual([(m.start, m.end, m.kind) for m in merged], [(0, 960, "game"), (960, 1800, "video")])

    def test_only_short_moments_are_all_dropped(self):
        self.assertEqual(timeline.merge_short([moment(0, 60), moment(60, 100)]), [])

    def test_combine_windows_without_repeating_overlap(self):
        first = [moment(0, 1800, "talk", "а"), moment(1800, 3600, "game", "б")]
        second = [moment(3480, 3700, "game", "б"), moment(3700, 5000, "video", "в")]  # окно начинается за 2 минуты до конца первого
        combined = timeline.combine([first, second])
        self.assertEqual([(m.start, m.end, m.kind) for m in combined],
                         [(0, 1800, "talk"), (1800, 3700, "game"), (3700, 5000, "video")])

    def test_combine_skips_what_is_already_covered(self):
        combined = timeline.combine([[moment(0, 3600, "talk", "а")], [moment(3000, 3500, "game", "б"), moment(3500, 4800, "game", "в")]])
        self.assertEqual([(m.start, m.end, m.topic) for m in combined], [(0, 3600, "а"), (3600, 4800, "в")])

    def test_coverage_and_render(self):
        moments = [moment(0, 1800), moment(2400, 3600, "video", "фильм", "low")]
        self.assertAlmostEqual(timeline.coverage(moments, 0, 3600), 0.8333, places=3)
        self.assertEqual(timeline.coverage([], 0, 0), 0.0)
        self.assertEqual(timeline.render(moments).splitlines()[1], "0:40:00–1:00:00 (20 мин) video: фильм [low]")


def phrases(minutes, step=60):
    return [Phrase(t * 1.0, t + 5.0, f"фраза {t}") for t in range(0, minutes * 60, step)]


class AnalyzeTest(unittest.TestCase):
    def run_analyze(self, text, **kwargs):
        calls = []

        async def ask(**kw):
            calls.append(kw)
            # модель отвечает одним моментом на всё окно
            import re

            start, end = re.search(r"Участок для разметки: (\S+)–(\S+)", kw["user"]).groups()
            return ({"moments": [{"start": start, "end": end, "kind": "talk", "topic": "общая тема", "confidence": "medium"}]},
                    claude.Usage(1, 1000, 100, 0.001))

        context = Context("Заквиель", "Стрим про всё", [(0, 3000, "Just Chatting"), (3000, 9000, "Minecraft")])
        moments, usage = asyncio.run(timeline.analyze(text, context, ask, **kwargs))
        return moments, usage, calls

    def test_windows_prompts_and_totals(self):
        moments, usage, calls = self.run_analyze(phrases(150), minutes=60, overlap=2, effort="medium")
        self.assertEqual(len(calls), 3)
        self.assertEqual([c["effort"] for c in calls], ["medium"] * 3)
        self.assertTrue(all(c["schema"] is timeline.SCHEMA and c["system"] is timeline.SYSTEM and c["max_tokens"] == timeline.MAX_TOKENS
                            for c in calls))
        self.assertEqual((usage.requests, usage.input_tokens), (3, 3000))
        self.assertAlmostEqual(usage.cost_usd, 0.003)
        self.assertEqual(len(moments), 1)  # одинаковые вид и тема склеились
        self.assertEqual((moments[0].start, moments[0].end), (0, 149 * 60 + 5))

    def test_user_prompt_has_hints_and_text(self):
        _, _, calls = self.run_analyze(phrases(90))
        first = calls[0]["user"]
        self.assertIn("Стример: Заквиель", first)
        self.assertIn("Название стрима на Twitch: Стрим про всё", first)
        self.assertIn("0:00:00–0:50:00 Just Chatting", first)
        self.assertIn("[0:00:00] фраза 0", first)
        last = calls[-1]["user"]
        self.assertNotIn("Just Chatting", last)  # эта категория закончилась до начала окна
        self.assertIn("Minecraft", last)

    def test_no_phrases_no_requests(self):
        moments, usage, calls = self.run_analyze([])
        self.assertEqual((moments, usage.requests, calls), ([], 0, []))


if __name__ == "__main__":
    unittest.main()
