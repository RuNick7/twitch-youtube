import unittest

from app.ai import titles


class ProfanityTest(unittest.TestCase):
    def test_finds_profanity(self):
        for text in ("Блять, ну и стрим", "ебанутая сборка", "Ёбаный майнкрафт", "это пиздец", "какая сука", "полный хуйня", "вот это хуя себе"):
            self.assertTrue(titles.has_profanity(text), text)

    def test_ordinary_words_pass(self):
        for text in ("употребляя кофе", "Хлеб и зрелища", "потребление ресурсов", "себя не жалеем", "лебедь на пруду",
                     "хуже некуда", "хутор в Minecraft", "сукно и шерсть", "жар и небо", "Строим базу и обсуждаем планы"):
            self.assertFalse(titles.has_profanity(text), text)


class PickTopicsTest(unittest.TestCase):
    def test_clean_short_topics_in_the_given_order(self):
        self.assertEqual(titles.pick_topics([" СДЭК. ", "перенос  серверов", "экзамен на права"]),
                         ["СДЭК", "перенос серверов", "экзамен на права"])

    def test_long_profane_empty_and_repeated_are_dropped(self):
        raw = ["", "  ", None, 5, "очень длинная тема из многих слов", "а" * 31, "Блять", "Ёлка", "елка!", "ковка"]
        self.assertEqual(titles.pick_topics(raw), ["Ёлка", "ковка"])
        self.assertEqual(titles.pick_topics(None), [])

    def test_category_and_streamer_are_not_topics(self):
        self.assertEqual(titles.pick_topics(["Minecraft", "заквиель", "Just  Chatting", "шахта"],
                                            ("Minecraft", "Заквиель", "Just Chatting")), ["шахта"])

    def test_separators_inside_a_topic_cannot_break_the_title(self):
        [topic] = titles.pick_topics(["бой | <b>босс</b>,"])
        for symbol in "|,<>":
            self.assertNotIn(symbol, topic)

    def test_no_more_than_five(self):
        self.assertEqual(titles.pick_topics([f"тема {i}" for i in range(8)]), [f"тема {i}" for i in range(5)])


class ComposeTest(unittest.TestCase):
    def test_topics_then_category_then_streamer(self):
        self.assertEqual(titles.compose(["шахта", "боты", "розыгрыш"], "Большой стрим", "Minecraft", "Заквиель"),
                         "Шахта, боты, розыгрыш | Minecraft | Заквиель")

    def test_stream_title_when_there_are_no_topics(self):
        self.assertEqual(titles.compose([], "Большой  стрим", "Minecraft", "Заквиель"), "Большой стрим | Minecraft | Заквиель")
        self.assertEqual(titles.compose(["Minecraft"], "Большой стрим", "Minecraft", "Заквиель"),
                         "Большой стрим | Minecraft | Заквиель")
        self.assertEqual(titles.compose([], "", "", "Заквиель"), "Стрим | Заквиель")

    def test_a_topic_is_never_cut_in_the_middle(self):
        topics = ["ремонт старой квартиры", "покупка нового телефона", "восстановление старой крепости", "сыр"]
        title = titles.compose(topics, "Стрим", "Minecraft", "Заквиель")
        head = title.split(" | ")[0]
        self.assertEqual(head, "Ремонт старой квартиры, покупка нового телефона, сыр")
        self.assertLessEqual(len(head), titles.HEADLINE_MAX_CHARS)

    def test_headline_stays_within_what_youtube_shows(self):
        topics = ["ремонт старой квартиры", "покупка нового телефона", "осада большого замка", "сыр"]
        head = titles.compose(topics, "Стрим", "Minecraft", "Заквиель").split(" | ")[0]
        self.assertEqual(head, "Ремонт старой квартиры, покупка нового телефона, осада большого замка")

    def test_long_category_leaves_less_room(self):
        category = "К" * 60
        title = titles.compose(["покупка телефона", "осада замка", "сыр"], "Стрим", category, "Заквиель")
        self.assertLessEqual(len(title), 100)
        self.assertEqual(title, f"Покупка телефона, сыр | {category} | Заквиель")

    def test_only_stream_title_is_cut_to_what_youtube_allows(self):
        title = titles.compose([], "очень длинное название " * 20, "The Witcher 3: Wild Hunt", "Заквиель")
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith(" | The Witcher 3: Wild Hunt | Заквиель"), title)
        self.assertTrue(title.startswith("очень длинное название"))

    def test_huge_category_leaves_no_room_for_topics(self):
        title = titles.compose(["замок"], "Стрим", "К" * 95, "Заквиель")
        self.assertEqual((len(title), title[:3]), (100, "ККК"))

    def test_cut_never_splits_a_word_or_leaves_a_dangling_preposition(self):
        self.assertEqual(titles.cut_words("Сюжетные миссии GTA V: перестрелка, похищение и разборки с бандой", 60),
                         "Сюжетные миссии GTA V: перестрелка, похищение и разборки")
        self.assertEqual(titles.cut_words("а" * 80, 60), "а" * 60)  # одно длинное слово режется как есть


if __name__ == "__main__":
    unittest.main()
