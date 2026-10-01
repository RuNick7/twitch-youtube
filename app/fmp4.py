"""Плейлист HLS и фрагменты MP4 из VOD Twitch. Только стандартная библиотека.

Twitch хранит VOD как init-сегмент и фрагменты fMP4 по 10 секунд. Склейка
init + фрагменты подряд — готовый MP4, но время во фрагментах отсчитывается от
начала эфира. rebase() сдвигает его к нулю, не меняя размеров боксов: поэтому
размер ролика известен до загрузки, а поток байтов одинаков при каждой сборке
и его можно продолжить с любого места.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin

# Служебные боксы с абсолютным временем эфира (ID3 Twitch, индексы) превращаются
# в free того же размера: плееры и YouTube их пропускают
NEUTRALIZE = {b"emsg", b"sidx", b"prft"}


@dataclass
class Part:
    url: str
    start: float  # секунды от начала VOD
    duration: float
    size: int = 0
    group: int = 0  # номер отрезка VOD, если ролик склеен из нескольких


@dataclass
class Playlist:
    parts: List[Part]
    init_url: Optional[str]  # None — фрагменты MPEG-TS, они склеиваются как есть
    ended: bool  # есть EXT-X-ENDLIST: VOD дописан до конца


def parse_playlist(text: str, url: str) -> Playlist:
    init_url: Optional[str] = None
    parts: List[Part] = []
    position = 0.0
    duration: Optional[float] = None
    ended = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MAP:") and 'URI="' in line:
            init_url = urljoin(url, line.split('URI="', 1)[1].split('"', 1)[0])
        elif line.startswith("#EXTINF:"):
            duration = float(line[len("#EXTINF:"):].split(",", 1)[0])
        elif line == "#EXT-X-ENDLIST":
            ended = True
        elif line and not line.startswith("#"):
            length = duration or 0.0
            parts.append(Part(urljoin(url, line), position, length))
            position += length
            duration = None
    return Playlist(parts, init_url, ended)


def select_parts(parts: List[Part], start: float, end: float) -> List[Part]:
    """Фрагменты, середина которых попадает в [start, end): соседние сегменты не пересекаются."""
    return [part for part in parts if start <= part.start + part.duration / 2 < end]


def boxes(data, start: int = 0, end: Optional[int] = None) -> Iterator[Tuple[bytes, int, int, int]]:
    """Боксы одного уровня в data[start:end]: (тип, позиция, длина заголовка, размер)."""
    end = len(data) if end is None else end
    pos = start
    while pos < end:
        if pos + 8 > end:
            raise ValueError(f"обрывок бокса на позиции {pos}")
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise ValueError(f"обрывок бокса на позиции {pos}")
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise ValueError(f"бокс {kind!r} на позиции {pos} выходит за границы")
        yield kind, pos, header, size
        pos += size


def _child(data, parent: Tuple[bytes, int, int, int], kind: bytes) -> Iterator[Tuple[bytes, int, int, int]]:
    _, pos, header, size = parent
    return (box for box in boxes(data, pos + header, pos + size) if box[0] == kind)


def _full_box_field(data, box: Tuple[bytes, int, int, int], offset_v0: int, offset_v1: int, wide_in_v1: bool) -> Tuple[int, str]:
    """Позиция и формат поля полного бокса с учётом версии."""
    _, pos, header, _ = box
    version = data[pos + header]
    if version == 1:
        return pos + header + offset_v1, ">Q" if wide_in_v1 else ">I"
    return pos + header + offset_v0, ">I"


def timescales_from_init(init: bytes) -> Dict[int, int]:
    """track_ID → timescale из moov/trak."""
    result: Dict[int, int] = {}
    for moov in (box for box in boxes(init) if box[0] == b"moov"):
        for trak in _child(init, moov, b"trak"):
            track = timescale = None
            for tkhd in _child(init, trak, b"tkhd"):
                pos, fmt = _full_box_field(init, tkhd, 12, 20, False)
                track = struct.unpack_from(fmt, init, pos)[0]
            for mdia in _child(init, trak, b"mdia"):
                for mdhd in _child(init, mdia, b"mdhd"):
                    pos, fmt = _full_box_field(init, mdhd, 12, 20, False)
                    timescale = struct.unpack_from(fmt, init, pos)[0]
            if track is not None and timescale:
                result[track] = timescale
    if not result:
        raise ValueError("в init-сегменте нет дорожек")
    return result


def _trafs(data) -> Iterator[Tuple[int, Tuple[bytes, int, int, int]]]:
    """(track_ID, tfdt) по всем moof/traf фрагмента."""
    for moof in (box for box in boxes(data) if box[0] == b"moof"):
        for traf in _child(data, moof, b"traf"):
            track = None
            for box in boxes(data, traf[1] + traf[2], traf[1] + traf[3]):
                if box[0] == b"tfhd":
                    track = struct.unpack_from(">I", data, box[1] + box[2] + 4)[0]
                elif box[0] == b"tfdt":
                    if track is None:
                        raise ValueError("tfdt раньше tfhd")
                    yield track, box


def first_times(fragment: bytes) -> Dict[int, int]:
    """baseMediaDecodeTime первого фрагмента каждой дорожки."""
    found: Dict[int, int] = {}
    for track, tfdt in _trafs(fragment):
        if track not in found:
            pos, fmt = _full_box_field(fragment, tfdt, 4, 4, True)
            found[track] = struct.unpack_from(fmt, fragment, pos)[0]
    if not found:
        raise ValueError("во фрагменте нет tfdt")
    return found


def fragment_end_times(fragment: bytes) -> Dict[int, int]:
    """Конец фрагмента по дорожкам: tfdt плюс длительности всех сэмплов.

    Нужен, чтобы приставить следующий отрезок VOD точно встык, без пропуска и наложения.
    """
    ends: Dict[int, int] = {}
    for moof in (box for box in boxes(fragment) if box[0] == b"moof"):
        for traf in _child(fragment, moof, b"traf"):
            track = start = None
            default_duration = None
            total = 0
            for kind, pos, header, size in boxes(fragment, traf[1] + traf[2], traf[1] + traf[3]):
                body = pos + header
                if kind == b"tfhd":
                    flags = int.from_bytes(fragment[body + 1:body + 4], "big")
                    track = struct.unpack_from(">I", fragment, body + 4)[0]
                    field = body + 8 + (8 if flags & 0x1 else 0) + (4 if flags & 0x2 else 0)
                    if flags & 0x8:
                        default_duration = struct.unpack_from(">I", fragment, field)[0]
                elif kind == b"tfdt":
                    position, fmt = _full_box_field(fragment, (kind, pos, header, size), 4, 4, True)
                    start = struct.unpack_from(fmt, fragment, position)[0]
                elif kind == b"trun":
                    flags = int.from_bytes(fragment[body + 1:body + 4], "big")
                    count = struct.unpack_from(">I", fragment, body + 4)[0]
                    field = body + 8 + (4 if flags & 0x1 else 0) + (4 if flags & 0x4 else 0)
                    if flags & 0x100:
                        step = 4 * sum(1 for bit in (0x100, 0x200, 0x400, 0x800) if flags & bit)
                        total += sum(struct.unpack_from(">I", fragment, field + i * step)[0] for i in range(count))
                    elif default_duration is not None:
                        total += count * default_duration
                    else:
                        raise ValueError(f"у дорожки {track} нет длительностей сэмплов")
            if track is None or start is None:
                raise ValueError("traf без tfhd или tfdt")
            ends[track] = max(ends.get(track, 0), start + total)
    if not ends:
        raise ValueError("во фрагменте нет moof")
    return ends


def base_time(first: Dict[int, int], timescales: Dict[int, int]) -> Fraction:
    """Новый ноль в секундах: самое раннее начало среди дорожек, чтобы не сбить синхронизацию."""
    return min(Fraction(value, timescales[track]) for track, value in first.items())


def shifts(base: Fraction, timescales: Dict[int, int], offset: Fraction = Fraction(0)) -> Dict[int, int]:
    """На сколько уменьшить tfdt каждой дорожки, чтобы время base стало временем offset в ролике.

    offset округляется вверх: склеенные отрезки могут разойтись на долю кадра, но не налезть друг на друга.
    """
    return {track: floor(base * timescale) - ceil(offset * timescale) for track, timescale in timescales.items()}


def rebase(fragment: bytearray, shift: Dict[int, int]) -> None:
    """Сдвигает время фрагмента на месте; размеры боксов не меняются."""
    for kind, pos, _, _ in boxes(fragment):
        if kind in NEUTRALIZE:
            fragment[pos + 4:pos + 8] = b"free"
    for track, tfdt in _trafs(fragment):
        if track not in shift:
            raise ValueError(f"дорожки {track} нет в init-сегменте")
        pos, fmt = _full_box_field(fragment, tfdt, 4, 4, True)
        value = struct.unpack_from(fmt, fragment, pos)[0] - shift[track]
        if value < 0:
            raise ValueError(f"время дорожки {track} уходит в минус")
        if fmt == ">I" and value >= 2**32:
            raise ValueError(f"время дорожки {track} не помещается в 32-битный tfdt")
        struct.pack_into(fmt, fragment, pos, value)
