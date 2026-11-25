"""Расшифровка речи: фразы со временем, очистка от «галлюцинаций» Whisper и разбиение для языковой модели."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

# Пороги из самого Whisper: на тишине и музыке он выдумывает текст, а на шуме зацикливается
NO_SPEECH_ABOVE = 0.6
LOGPROB_BELOW = -1.0
COMPRESSION_ABOVE = 2.4
LOOP_RUN = 4  # столько одинаковых фраз подряд — зацикливание

# «Субтитры сделал DimaTorzok», «Редактор субтитров А.Семкин»: Whisper дописывает их из обучающих данных
_CREDITS = re.compile(r"субтитры\s+(?:сделал|создавал|подогнал|делал)|редактор\s+субтитров|корректор\s+[а-я]\.", re.I)
# Эти фразы бывают и настоящими, поэтому отбрасываются, только если Whisper сомневался, что в звуке была речь
_FILLERS = re.compile(r"^(?:продолжение следует|спасибо за просмотр|подписывайтесь на (?:наш )?канал)\W*$", re.I)
FILLER_NO_SPEECH_ABOVE = 0.3


@dataclass(frozen=True)
class Phrase:
    start: float  # секунд от начала записи
    end: float
    text: str


def _key(text: str) -> str:
    return re.sub(r"\W+", "", text.lower())


def collapse_loops(phrases: Sequence[Phrase], run: int = LOOP_RUN) -> tuple[list[Phrase], int]:
    """Серия из run и более одинаковых фраз подряд заменяется одной. Второе значение — сколько фраз убрано."""
    result: list[Phrase] = []
    removed = 0
    i = 0
    while i < len(phrases):
        j = i
        while j + 1 < len(phrases) and _key(phrases[j + 1].text) == _key(phrases[i].text):
            j += 1
        count = j - i + 1
        if count >= run:
            result.append(Phrase(phrases[i].start, phrases[j].end, phrases[i].text))
            removed += count - 1
        else:
            result.extend(phrases[i : j + 1])
        i = j + 1
    return result, removed


def parse_verbose(data: dict, offset: float = 0.0) -> tuple[list[Phrase], Counter]:
    """Фразы из ответа Groq (verbose_json) со сдвигом offset секунд и счётчик отброшенных по причинам."""
    dropped: Counter = Counter()
    phrases: list[Phrase] = []
    for item in data.get("segments") or []:
        text = " ".join(str(item.get("text") or "").split())
        no_speech = float(item.get("no_speech_prob") or 0)
        logprob = float(item.get("avg_logprob") or 0)
        ratio = float(item.get("compression_ratio") or 0)
        if not text:
            dropped["пусто"] += 1
        elif no_speech > NO_SPEECH_ABOVE and logprob < LOGPROB_BELOW:
            dropped["тишина или музыка"] += 1
        elif ratio > COMPRESSION_ABOVE:
            dropped["зацикливание"] += 1
        elif _CREDITS.search(text):
            dropped["чужие субтитры"] += 1
        elif _FILLERS.match(text) and no_speech > FILLER_NO_SPEECH_ABOVE:
            dropped["фраза из тишины"] += 1
        else:
            start = float(item.get("start") or 0)
            phrases.append(Phrase(offset + start, offset + max(float(item.get("end") or 0), start), text))
    phrases, removed = collapse_loops(phrases)
    if removed:
        dropped["повтор подряд"] += removed
    return phrases, dropped


def fmt_time(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def render(phrases: Sequence[Phrase], line_sec: float = 30) -> str:
    """Текст для языковой модели: строка на каждые line_sec секунд, в начале время «[1:02:30]»."""
    lines: list[str] = []
    words: list[str] = []
    line_start = 0.0
    for phrase in phrases:
        if words and phrase.start - line_start >= line_sec:
            lines.append(f"[{fmt_time(line_start)}] {' '.join(words)}")
            words = []
        if not words:
            line_start = phrase.start
        words.append(phrase.text)
    if words:
        lines.append(f"[{fmt_time(line_start)}] {' '.join(words)}")
    return "\n".join(lines)


def windows(phrases: Sequence[Phrase], minutes: int = 60, overlap_minutes: int = 2) -> list[list[Phrase]]:
    """Куски расшифровки по minutes минут с нахлёстом: у Haiku 5.5 запрос длиннее 100 тыс. токенов в 5 раз дороже."""
    if overlap_minutes >= minutes:
        raise ValueError("нахлёст должен быть меньше окна")
    if not phrases:
        return []
    span, step, end = minutes * 60, (minutes - overlap_minutes) * 60, phrases[-1].end
    result: list[list[Phrase]] = []
    start = 0.0
    while True:
        part = [phrase for phrase in phrases if start <= phrase.start < start + span]
        if part:
            result.append(part)
        if start + span >= end:
            return result
        start += step


def dump(phrases: Sequence[Phrase]) -> str:
    return json.dumps([[round(p.start, 2), round(p.end, 2), p.text] for p in phrases], ensure_ascii=False)


def load(text: str) -> list[Phrase]:
    return [Phrase(float(start), float(end), str(words)) for start, end, words in json.loads(text)]
