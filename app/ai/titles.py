"""Название ролика «Тема, тема, тема | Категория #N | Стример» из главных тем, которые Haiku возвращает вместе
с главами.

Несколько тем по одному-два слова говорят о ролике больше, чем одна длинная фраза. Без подходящих тем берётся
название стрима с Twitch, как сейчас у бота.
"""

from __future__ import annotations

import re
from typing import Sequence

from ..segments import FALLBACK_CATEGORY, TITLE_MAX_CHARS, TITLE_SEPARATOR, clean_line

MAX_TOPICS = 5
TOPIC_MAX_WORDS = 3  # «экзамен на права» — ещё тема, целая фраза — уже нет
TOPIC_MAX_CHARS = 30
HEADLINE_MAX_CHARS = 70  # дальше YouTube в списках название обрезает
MIN_ROOM = 8  # меньше — для названия стрима места нет

_TRAILING_WORDS = {"и", "в", "с", "на", "по", "за", "из", "от", "до", "к", "о", "у", "а", "но", "для", "при", "про"}
# Грубая страховка на случай, если модель всё же написала мат: слова ищутся с начала слова, чтобы не цеплять
# «употребляя», «хлеб», «потребление» и подобные
_PROFANITY = re.compile(
    r"\b(?:бля\w*|[её]б(?:а|у|и|л|н)\w*|\w*хуй\w*|ху[яеи]\w*|\w*пизд\w*|сук[аиу]\b|сучк\w*|сукин\w*|муд[ао]к\w*"
    r"|залуп\w*|говн\w*|дерьм\w*|жоп[аыуе]\b)",
    re.IGNORECASE,
)


def has_profanity(text: str) -> bool:
    return bool(_PROFANITY.search(text))


def cut_words(text: str, limit: int) -> str:
    """Обрезка по границе слова, без повисшего предлога в конце."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut and text[limit] != " ":
        cut = cut[: cut.rfind(" ")]
    words = cut.rstrip(" ,;:-—").split(" ")
    while len(words) > 1 and words[-1].lower() in _TRAILING_WORDS:
        words.pop()
    return " ".join(words).rstrip(" ,;:-—")


def _key(text: str) -> str:
    return re.sub(r"\W+", "", text.lower().replace("ё", "е"))


def pick_topics(raw: Sequence[object], banned: Sequence[str] = ()) -> list[str]:
    """Годные темы из ответа модели по порядку: короткие, без мата и повторов; banned — категория и имя стримера."""
    seen = {_key(word) for word in banned}
    result: list[str] = []
    for item in raw or ():
        if not isinstance(item, str):
            continue
        topic = " ".join(clean_line(item).replace("|", " ").replace(",", " ").split()).strip(" .;:-—!?")
        key = _key(topic)
        if not key or key in seen or has_profanity(topic):
            continue
        if len(topic) <= TOPIC_MAX_CHARS and len(topic.split()) <= TOPIC_MAX_WORDS:
            seen.add(key)
            result.append(topic)
    return result[:MAX_TOPICS]


def headline(topics: Sequence[str], room: int) -> str:
    """Темы через запятую, сколько помещается в room символов; тема не обрезается посередине."""
    chosen: list[str] = []
    for topic in topics:
        if len(", ".join([*chosen, topic])) <= room:
            chosen.append(topic)
    text = ", ".join(chosen)
    return text[:1].upper() + text[1:]


def compose(
    topics: Sequence[object],
    stream_title: str,
    category: str,
    name: str,
    part: int | None = None,
    limit: int = TITLE_MAX_CHARS,
) -> str:
    """«Темы | Категория #N | Стример» не длиннее limit; part — номер части, если он есть у этой категории.

    Категория, номер и стример обязательны.
    """
    section = clean_line(category) or FALLBACK_CATEGORY
    if part:
        section = f"{section} #{part}"
    tail = TITLE_SEPARATOR.join(filter(None, (section, clean_line(name))))
    room = limit - len(tail) - len(TITLE_SEPARATOR)
    head = headline(pick_topics(topics, (category, name)), min(room, HEADLINE_MAX_CHARS))
    if not head and room >= MIN_ROOM:
        head = cut_words(clean_line(stream_title), room)
    return head + TITLE_SEPARATOR + tail if head else tail[:limit]
