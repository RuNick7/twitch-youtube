"""Проверка скачанного файла по данным ffprobe. Только стандартная библиотека."""

from __future__ import annotations

from typing import Optional


def _seconds(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None  # NaN тоже отсекается: сравнение с ним ложно


def verify_probe(probe: dict, expected: float) -> Optional[str]:
    """Возвращает описание проблемы или None, если файл выглядит целым.

    Кроме общей длительности сравниваем длину видео и звука: известный баг
    yt-dlp (#15825) «сжимает» конец длинных VOD Twitch, и тогда видеодорожка
    заканчивается намного раньше звука.
    """
    duration = _seconds((probe.get("format") or {}).get("duration"))
    if duration is None:
        return "не удалось определить длительность"
    # Нарезка по ключевым кадрам может добавить пару секунд с каждой стороны
    if abs(duration - expected) > max(10.0, expected * 0.005):
        return f"длительность {duration:.0f} с вместо {expected:.0f} с"

    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        return "в файле нет видео"
    video_len = _seconds(video.get("duration"))
    audio_len = _seconds(audio.get("duration")) if audio else None
    if video_len is not None and audio_len is not None:
        if abs(video_len - audio_len) > max(5.0, expected * 0.002):
            return f"видео ({video_len:.0f} с) и звук ({audio_len:.0f} с) разной длины"
    return None
