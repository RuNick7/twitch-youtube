"""Звук записи стрима для расшифровки: отдельный аудиопоток Twitch (~90 МБ в час) режется ffmpeg на куски.

Видео не скачивается: у каждой записи Twitch есть поток audio_only. Звук сжимается в FLAC 16 кГц моно — так
советует Groq, а в Whisper звук в любом случае приводится к 16 кГц. Кусок получается около 10 МБ на 10 минут,
а лимит Groq — 25 МБ на файл на бесплатном тарифе и 100 МБ на платном.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from ..tools import ToolError, run, vod_url

log = logging.getLogger(__name__)

CHUNK_SEC = 600
EXTRACT_TIMEOUT_SEC = 3600


@dataclass(frozen=True)
class Chunk:
    path: Path
    start: float  # секунд от начала записи
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


async def audio_playlist_url(vod_id: str) -> str:
    """Адрес HLS-плейлиста аудиопотока записи (тот же yt-dlp, что и в остальном боте)."""
    code, out, err = await run(
        "yt-dlp", "--no-warnings", "-f", "audio_only/bestaudio", "-g", vod_url(vod_id), timeout=180
    )
    urls = [line.strip() for line in out.splitlines() if line.strip().startswith("http")]
    if code != 0 or not urls:
        tail = " ".join(err.strip().splitlines()[-2:]) if err.strip() else f"код {code}"
        raise ToolError(f"не получен адрес звука записи {vod_id}: {tail}")
    return urls[0]


def ffmpeg_command(
    ffmpeg: str, source: str, pattern: Path, listing: Path, *, chunk_sec: int = CHUNK_SEC, limit_sec: int | None = None
) -> list[str]:
    """Команда: звук из source одним проходом режется на куски по chunk_sec секунд; границы пишутся в listing (CSV)."""
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if limit_sec:
        command += ["-t", str(limit_sec)]  # до -i: ограничивает чтение входа
    command += [
        "-allowed_extensions", "ALL",  # сегменты Twitch — файлы .mp4
        "-i", source,
        "-vn", "-map", "0:a:0",
        "-ar", "16000", "-ac", "1", "-c:a", "flac",
        "-f", "segment", "-segment_time", str(chunk_sec), "-segment_format", "flac",
        "-reset_timestamps", "1",
        "-segment_list", str(listing), "-segment_list_type", "csv",
        str(pattern),
    ]
    return command


def parse_listing(text: str, folder: Path) -> list[Chunk]:
    """Куски из CSV ffmpeg: имя файла, начало и конец в секундах от начала входа."""
    chunks = []
    for row in csv.reader(text.splitlines()):
        if len(row) < 3:
            continue
        chunks.append(Chunk(folder / row[0], float(row[1]), float(row[2])))
    return sorted(chunks, key=lambda chunk: chunk.start)


async def extract_chunks(
    source: str, folder: Path, *, chunk_sec: int = CHUNK_SEC, limit_sec: int | None = None
) -> list[Chunk]:
    """Скачивает звук и режет на куски. limit_sec — взять только начало записи (для пробных запусков)."""
    import imageio_ffmpeg  # статическая сборка ffmpeg из pip

    folder.mkdir(parents=True, exist_ok=True)
    listing = folder / "chunks.csv"
    listing.unlink(missing_ok=True)
    command = ffmpeg_command(
        imageio_ffmpeg.get_ffmpeg_exe(), source, folder / "chunk-%03d.flac", listing,
        chunk_sec=chunk_sec, limit_sec=limit_sec,
    )
    code, _, err = await run(*command, timeout=EXTRACT_TIMEOUT_SEC)
    if code != 0 or not listing.exists():
        raise ToolError(f"ffmpeg не смог получить звук: {err.strip()[-300:] or code}")
    chunks = parse_listing(listing.read_text(), folder)
    if not chunks:
        raise ToolError("в записи нет звука")
    return chunks
