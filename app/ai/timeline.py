"""Таймкоды по тексту: Haiku размечает расшифровку на моменты (игра, разговор, чужое видео, перерыв).

Запись режется на окна по часу с нахлёстом, каждое окно — один запрос; потом моменты склеиваются в общий список.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Sequence

from ..segments import clean_line
from . import claude, transcript
from .transcript import Phrase

log = logging.getLogger(__name__)

KINDS = ("game", "talk", "video", "break", "other")
CONFIDENCE = ("high", "medium", "low")
MIN_MOMENT_SEC = 30  # обрывки короче приклеиваются к соседнему; крупные темы собирают уже главы
TOPIC_MAX_CHARS = 120
MAX_TOKENS = 16000  # за час набирается полсотни моментов, и размышления тоже входят в лимит
MIN_COVERAGE = 0.8  # моменты должны покрывать окно хотя бы настолько, иначе запрос повторяется
ATTEMPTS = 2
# «Перерыв» или «другое» не длиннее этого между двумя кусками видео — тоже видео: Twitch глушит музыку фильма,
# в тексте остаётся дыра
VIDEO_GAP_SEC = 600
# Короткий разговор между кусками видео — тоже видео: стример комментирует идущий фильм. Долгий разговор так не
# склеивается: пока видео на паузе, кусок можно оставить в ролике
VIDEO_TALK_GAP_SEC = 180

SYSTEM = """Ты размечаешь по времени расшифровку записи стрима на Twitch: что происходит в каждый отрезок.

О расшифровке. Она автоматическая. Речь стримера, игровых персонажей, чужих видео и озвучки донатов идёт одним текстом без пометок, кто говорит; бывают ошибки распознавания и пропуски там, где Twitch заглушил музыку. В начале строки стоит время «[H:MM:SS]».

Верни моменты: подряд идущие отрезки, которые вместе покрывают весь участок. У каждого момента:
- start и end: время в формате H:MM:SS; следующий момент начинается там, где закончился предыдущий;
- kind: «game» — стример играет и комментирует игру (речь и кат-сцены персонажей игры тоже относятся к game); «talk» — разговор с чатом, истории, новости, обсуждение, вопросы; «video» — стример отвлёкся от игры или разговора и смотрит или слушает постороннее видео, фильм, сериал, клип или чужой стрим (признаки: долгая связная речь, не похожая на разговор стримера: закадровый голос, диалоги сюжета, реклама, чужая манера; сам стример молчит или коротко комментирует). Это video и тогда, когда стример много комментирует то, что идёт на экране: актёров, сюжет, звук. Если же видео ещё не запущено или стоит на паузе и стример несколько минут говорит о другом, это talk; «break» — перерыв, пауза, тишина, музыка; «other» — всё остальное;
- topic: о чём этот отрезок, одной фразой по-русски до 80 символов, конкретно («обсуждают рекламу GTA 6 и PlayStation», а не «разговор»);
- confidence: «high», «medium» или «low».

Правила:
- новый момент начинается, когда меняется занятие или тема разговора: разные темы в один момент не объединяй, даже если тема заняла около минуты; реплики короче 30 секунд присоединяй к соседним;
- если категория Twitch — игра, речь персонажей и кат-сцены игры — это game, а не video; video ставь, только когда по тексту ясно, что стример смотрит постороннее видео;
- в категории Just Chatting «game» ставь, только когда ясно, что играет сам стример: стрельба, взрывы и опасности в диалогах фильма — это video;
- озвучка донатов (синтезированный голос читает сообщение зрителя, часто с суммой) — часть текущего занятия, не video;
- не уверен, что идёт видео, — ставь talk или game и confidence low;
- мат, оскорбления и дословные цитаты из расшифровки не повторяй: называй тему нейтрально;
- ничего не выдумывай: тема должна следовать из текста; категория Twitch и название стрима — только подсказки."""

SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "topic": {"type": "string"},
                    "confidence": {"type": "string", "enum": list(CONFIDENCE)},
                },
                "required": ["start", "end", "kind", "topic", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}

Ask = Callable[..., Awaitable[tuple[dict, claude.Usage]]]


@dataclass(frozen=True)
class Context:
    """Что известно о записи помимо текста: подсказки для модели."""

    streamer: str
    title: str
    chapters: Sequence[tuple[float, float, str]] = ()  # (начало, конец, категория Twitch)


@dataclass(frozen=True)
class Moment:
    start: float
    end: float
    kind: str
    topic: str
    confidence: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def parse_time(value: object) -> float | None:
    """«1:02:30», «62:30», «3750» или число секунд; None, если не разобралось."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value or "").strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", part.strip()) for part in parts):
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def build_user_prompt(context: Context, phrases: Sequence[Phrase]) -> str:
    start, end = phrases[0].start, phrases[-1].end
    chapters = [
        f"{transcript.fmt_time(a)}–{transcript.fmt_time(b)} {name}"
        for a, b, name in context.chapters
        if a < end and b > start
    ]
    return (
        f"Стример: {context.streamer}\n"
        f"Название стрима на Twitch: {context.title}\n"
        f"Категории Twitch на этом участке: {'; '.join(chapters) or 'неизвестны'}\n"
        f"Участок для разметки: {transcript.fmt_time(start)}–{transcript.fmt_time(end)}\n\n"
        f"Расшифровка:\n{transcript.render(phrases)}"
    )


def parse_moments(data: dict, start: float, end: float) -> list[Moment]:
    """Моменты из ответа модели: времена внутри [start, end], по порядку, без наложений; мусор отбрасывается."""
    moments: list[Moment] = []
    for item in data.get("moments") or []:
        if not isinstance(item, dict):
            continue
        a, b = parse_time(item.get("start")), parse_time(item.get("end"))
        topic = clean_line(str(item.get("topic") or ""))[:TOPIC_MAX_CHARS]
        if a is None or b is None or not topic:
            continue
        a, b = max(a, start), min(b, end)
        kind = item.get("kind") if item.get("kind") in KINDS else "other"
        confidence = item.get("confidence") if item.get("confidence") in CONFIDENCE else "low"
        moments.append(Moment(a, b, kind, topic, confidence))
    moments.sort(key=lambda moment: (moment.start, moment.end))
    result: list[Moment] = []
    for moment in moments:
        if result and moment.start < result[-1].end:
            moment = replace(moment, start=result[-1].end)
        if moment.end > moment.start:
            result.append(moment)
    return result


def _topic_key(topic: str) -> str:
    return re.sub(r"\W+", "", topic.lower())


def merge_same(moments: Sequence[Moment]) -> list[Moment]:
    """Подряд идущие моменты с тем же видом и той же темой — один."""
    result: list[Moment] = []
    for moment in moments:
        last = result[-1] if result else None
        if last and last.kind == moment.kind and _topic_key(last.topic) == _topic_key(moment.topic):
            result[-1] = replace(last, end=moment.end)
        else:
            result.append(moment)
    return result


def merge_short(moments: Sequence[Moment], min_sec: float = MIN_MOMENT_SEC) -> list[Moment]:
    """Обрывки короче min_sec приклеиваются к предыдущему моменту, а в начале — к следующему.

    Несколько обрывков подряд, которые вместе набирают min_sec, — отдельный момент с темой самого длинного из них:
    иначе предыдущий растянулся бы на них, а их темы пропали бы.
    """
    result: list[Moment] = []
    short: list[Moment] = []
    for moment in moments:
        if moment.duration < min_sec:
            short.append(moment)
            if short[-1].end - short[0].start >= min_sec:
                result.append(replace(max(short, key=lambda m: m.duration), start=short[0].start, end=short[-1].end))
                short = []
            continue
        if short:
            if result:
                result[-1] = replace(result[-1], end=short[-1].end)
            else:
                moment = replace(moment, start=short[0].start)
            short = []
        result.append(moment)
    if short and result:
        result[-1] = replace(result[-1], end=short[-1].end)
    return result


def combine(parts: Sequence[Sequence[Moment]], min_sec: float = MIN_MOMENT_SEC) -> list[Moment]:
    """Моменты из нескольких окон в один список: то, что уже покрыто предыдущим окном, не повторяется."""
    result: list[Moment] = []
    for part in parts:
        for moment in part:
            if result and moment.end <= result[-1].end:
                continue
            if result and moment.start < result[-1].end:
                moment = replace(moment, start=result[-1].end)
            result.append(moment)
    return merge_short(merge_same(result), min_sec)


def bridge_video(
    moments: Sequence[Moment], max_gap: float = VIDEO_GAP_SEC, talk_gap: float = VIDEO_TALK_GAP_SEC
) -> list[Moment]:
    """Дыры без речи и короткие реплики внутри просмотра видео помечаются как видео (с низкой уверенностью).

    Игра между кусками видео остаётся игрой.
    """
    result = list(moments)
    previous_video = None
    for i, moment in enumerate(result):
        if moment.kind == "video":
            if previous_video is not None:
                between = result[previous_video + 1 : i]
                gap = moment.start - result[previous_video].end
                silence = all(m.kind in ("break", "other") for m in between) and gap <= max_gap
                remarks = all(m.kind in ("talk", "break", "other") for m in between) and gap <= talk_gap
                if between and (silence or remarks):
                    result[previous_video + 1 : i] = [replace(m, kind="video", confidence="low") for m in between]
            previous_video = i
    return result


def render(moments: Sequence[Moment]) -> str:
    lines = []
    for m in moments:
        lines.append(
            f"{transcript.fmt_time(m.start)}–{transcript.fmt_time(m.end)} ({m.duration / 60:.0f} мин) "
            f"{m.kind}: {m.topic} [{m.confidence}]"
        )
    return "\n".join(lines)


def coverage(moments: Sequence[Moment], start: float, end: float) -> float:
    """Какая доля участка покрыта моментами (пропуски бывают там, где модель ничего не вернула)."""
    return min(sum(m.duration for m in moments) / (end - start), 1.0) if end > start else 0.0


async def analyze(
    phrases: Sequence[Phrase], context: Context, ask: Ask, *, minutes: int = 60, overlap: int = 2, effort: str = "low"
) -> tuple[list[Moment], claude.Usage]:
    """Моменты по всей расшифровке: окна по minutes минут идут одно за другим."""
    usage = claude.Usage()
    parts = []
    for window in transcript.windows(phrases, minutes, overlap):
        start, end = window[0].start, window[-1].end
        best: list[Moment] = []
        for attempt in range(1, ATTEMPTS + 1):
            data, used = await ask(
                system=SYSTEM,
                user=build_user_prompt(context, window),
                schema=SCHEMA,
                effort=effort,
                max_tokens=MAX_TOKENS,
            )
            usage.add(used)
            found = parse_moments(data, start, end)
            if coverage(found, start, end) >= coverage(best, start, end):
                best = found
            if coverage(best, start, end) >= MIN_COVERAGE:
                break
            # Изредка модель возвращает моменты не на весь участок: без проверки кусок записи молча пропадал бы
            log.warning("Участок %s–%s: моменты покрывают %.0f%% (попытка %d из %d)", transcript.fmt_time(start),
                        transcript.fmt_time(end), coverage(found, start, end) * 100, attempt, ATTEMPTS)
        parts.append(best)
    return bridge_video(combine(parts)), usage
