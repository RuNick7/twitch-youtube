import asyncio
import unittest

from app.ai import claude, titles
from app.ai.timeline import Moment


def moment(start, end, topic="тема", kind="talk"):
    return Moment(start, end, kind, topic, "high")


class ProfanityTest(unittest.TestCase):
    def test_finds_profanity(self):
        for text in ("Блять, ну и стрим", "ебанутая сборка", "Ёбаный майнкрафт", "это пиздец", "какая сука", "полный хуйня", "вот это хуя себе"):
            self.assertTrue(titles.has_profanity(text), text)

    def test_ordinary_words_pass(self):
        for text in ("употребляя кофе", "Хлеб и зрелища", "потребление ресурсов", "себя не жалеем", "лебедь на пруду",
                     "хуже некуда", "хутор в Minecraft", "сукно и шерсть", "жар и небо", "Строим базу и обсуждаем планы"):
            self.assertFalse(titles.has_profanity(text), text)


class ParseTitleTest(unittest.TestCase):
    def test_good_title(self):
        self.assertEqual(titles.parse_title({"title": "  Строим замок и обсуждаем новости. "}), ("Строим замок и обсуждаем новости", None))

    def test_empty_short_and_profane_are_rejected(self):
        self.assertEqual(titles.parse_title({"title": ""}), (None, "модель не нашла конкретики"))
        self.assertEqual(titles.parse_title({}), (None, "модель не нашла конкретики"))
        self.assertEqual(titles.parse_title({"title": "Игра"}), (None, "слишком короткое название"))
        self.assertEqual(titles.parse_title({"title": "Блять, опять сломал сервер"}), (None, "в названии мат"))

    def test_separator_and_brackets_cannot_break_the_template(self):
        middle, _ = titles.parse_title({"title": "Dota 2 | турнир <b>финал</b>"})
        self.assertNotIn("|", middle)
        self.assertNotIn("<", middle)

    def test_cut_never_splits_a_word_or_leaves_a_dangling_preposition(self):
        middle, _ = titles.parse_title({"title": "Сюжетные миссии GTA V: перестрелка, похищение и разборки с бандой"})
        self.assertEqual(middle, "Сюжетные миссии GTA V: перестрелка, похищение и разборки")
        self.assertEqual(titles._cut_words("а" * 80, 60), "а" * 60)  # одно длинное слово режется как есть

    def test_long_title_is_cut(self):
        middle, _ = titles.parse_title({"title": "слово " * 40})
        self.assertLessEqual(len(middle), titles.MIDDLE_MAX_CHARS)
        self.assertFalse(middle.endswith(" "))


class ComposeTest(unittest.TestCase):
    def test_with_and_without_model_middle(self):
        self.assertEqual(titles.compose("Minecraft", "Строим замок", "Стрим про Minecraft", "Заквиель", 12),
                         "Minecraft | Строим замок | Часть 12 | Заквиель")
        self.assertEqual(titles.compose("Minecraft", None, "Стрим про Minecraft", "Заквиель"),
                         "Minecraft | Стрим про Minecraft | Заквиель")

    def test_never_longer_than_youtube_allows(self):
        title = titles.compose("Minecraft", "я" * 60, "х" * 300, "Заквиель", 12)
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith("Часть 12 | Заквиель"))


class PromptTest(unittest.TestCase):
    def test_shows_forty_longest_moments_in_time_order(self):
        moments = [moment(i * 600, i * 600 + 600 + i, f"тема {i}") for i in range(60)]
        prompt = titles.build_user_prompt("Заквиель", "Стрим", "Just Chatting", moments)
        lines = [line for line in prompt.splitlines() if " talk: тема " in line]
        self.assertEqual(len(lines), titles.MAX_MOMENTS)
        self.assertIn("тема 59", prompt)
        self.assertNotIn("тема 0\n", prompt + "\n")
        numbers = [int(line.rsplit("тема ", 1)[1]) for line in lines]
        self.assertEqual(numbers, sorted(numbers))
        self.assertIn("Категория Twitch: Just Chatting", prompt)
        self.assertIn("Название стрима на Twitch: Стрим", prompt)

    def test_prompt_forbids_profanity_and_fabrication(self):
        self.assertIn("без мата", titles.SYSTEM)
        self.assertIn("ничего не выдумывай", titles.SYSTEM)


class SuggestTest(unittest.TestCase):
    def run_suggest(self, answer, moments):
        calls = []

        async def ask(**kw):
            calls.append(kw)
            return answer, claude.Usage(1, 500, 50, 0.0001)

        result = asyncio.run(titles.suggest(ask, streamer="Заквиель", stream_title="Стрим", category="Minecraft",
                                            moments=moments, effort="medium"))
        return result, calls

    def test_good_answer(self):
        (middle, reason, usage), calls = self.run_suggest({"title": "Строим замок на закате"}, [moment(0, 3600, "строят замок")])
        self.assertEqual((middle, reason, usage.requests), ("Строим замок на закате", None, 1))
        self.assertEqual((calls[0]["effort"], calls[0]["schema"], calls[0]["max_tokens"]), ("medium", titles.SCHEMA, titles.MAX_TOKENS))

    def test_no_moments_no_request(self):
        (middle, reason, usage), calls = self.run_suggest({"title": "что-то"}, [])
        self.assertEqual((middle, reason, usage.requests, calls), (None, "нет моментов", 0, []))

    def test_rejected_answer_keeps_reason(self):
        (middle, reason, _), _ = self.run_suggest({"title": ""}, [moment(0, 600)])
        self.assertEqual((middle, reason), (None, "модель не нашла конкретики"))


if __name__ == "__main__":
    unittest.main()
