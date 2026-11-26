"""Название ролика по тексту: Haiku придумывает середину «Категория | СЕРЕДИНА | Часть N | Имя».

Модель видит не весь текст, а моменты сегмента с темами (их даёт timeline): так дёшево, и это работает на сегментах
в несколько часов. Без конкретики или с матом в ответе берётся название стрима с Twitch, как сейчас.
"""

from __future__ import annotations

import re
from typing import Sequence

from ..segments import build_title, clean_line
from . import claude, transcript
from .timeline import Ask, Moment

MIDDLE_MIN_CHARS = 8
MIDDLE_MAX_CHARS = 60
MAX_MOMENTS = 40  # сколько самых длинных моментов показывать модели
MAX_TOKENS = 2000

SYSTEM = """Ты придумываешь середину названия для YouTube-ролика с записью части стрима. Система сама добавит категорию, номер части и имя стримера по шаблону «Категория | Середина | Часть N | Имя».

Тебе дают категорию Twitch, название стрима и моменты этой части: время, вид и тему каждого.

Требования к середине:
- по-русски, от 15 до 60 символов, одна фраза без точки в конце;
- конкретно: во что играли, что строили, о чём говорили, что смотрели; категорию не повторяй;
- без кликбейта, капса, эмодзи, вопросов-интриг, без имени стримера и номера части;
- без мата и оскорблений, даже если они есть в темах;
- только то, что следует из списка моментов; ничего не выдумывай;
- если конкретики нет, верни пустую строку: система возьмёт название стрима с Twitch."""

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}

# Грубая страховка на случай, если модель всё же написала мат: слова ищутся с начала слова, чтобы не цеплять
# «употребляя», «хлеб», «потребление» и подобные
_TRAILING_WORDS = {"и", "в", "с", "на", "по", "за", "из", "от", "до", "к", "о", "у", "а", "но", "для", "при", "про"}
_PROFANITY = re.compile(
    r"\b(?:бля\w*|[её]б(?:а|у|и|л|н)\w*|\w*хуй\w*|ху[яеи]\w*|\w*пизд\w*|сук[аиу]\b|сучк\w*|сукин\w*|муд[ао]к\w*"
    r"|залуп\w*|говн\w*|дерьм\w*|жоп[аыуе]\b)",
    re.IGNORECASE,
)


def has_profanity(text: str) -> bool:
    return bool(_PROFANITY.search(text))


def _cut_words(text: str, limit: int) -> str:
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


def build_user_prompt(streamer: str, stream_title: str, category: str, moments: Sequence[Moment]) -> str:
    longest = sorted(moments, key=lambda moment: moment.duration, reverse=True)[:MAX_MOMENTS]
    shown = sorted(longest, key=lambda moment: moment.start)
    lines = [
        f"{transcript.fmt_time(m.start)}–{transcript.fmt_time(m.end)} ({m.duration / 60:.0f} мин) {m.kind}: {m.topic}"
        for m in shown
    ]
    return (
        f"Стример: {streamer}\n"
        f"Категория Twitch: {category}\n"
        f"Название стрима на Twitch: {stream_title}\n\n"
        f"Моменты этой части:\n" + "\n".join(lines)
    )


def parse_title(data: dict) -> tuple[str | None, str | None]:
    """Середина названия и причина, если брать её нельзя (тогда первое значение — None)."""
    middle = clean_line(str(data.get("title") or "")).replace("|", ",").strip(" .,;:-—")
    if not middle:
        return None, "модель не нашла конкретики"
    if has_profanity(middle):
        return None, "в названии мат"
    if len(middle) < MIDDLE_MIN_CHARS:
        return None, "слишком короткое название"
    return _cut_words(middle, MIDDLE_MAX_CHARS), None


def compose(category: str, middle: str | None, stream_title: str, name: str, part: int | None = None) -> str:
    """Полное название ролика; без середины от модели — название стрима с Twitch, как в боте сейчас."""
    return build_title(category, middle or stream_title, name, part)


async def suggest(
    ask: Ask, *, streamer: str, stream_title: str, category: str, moments: Sequence[Moment], effort: str = "low"
) -> tuple[str | None, str | None, claude.Usage]:
    """Середина названия, причина отказа от неё (или None) и расход."""
    if not moments:
        return None, "нет моментов", claude.Usage()
    data, usage = await ask(
        system=SYSTEM,
        user=build_user_prompt(streamer, stream_title, category, moments),
        schema=SCHEMA,
        effort=effort,
        max_tokens=MAX_TOKENS,
    )
    middle, reason = parse_title(data)
    return middle, reason, usage
