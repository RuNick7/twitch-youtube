"""Внешние инструменты: yt-dlp, ffprobe, pip."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class ToolError(RuntimeError):
    pass


@dataclass
class VodInfo:
    id: str
    title: str
    duration: int
    uploader: str
    uploader_login: str
    started_at: datetime | None
    is_live: bool
    chapters: list[dict] = field(default_factory=list)


def vod_url(vod_id: str) -> str:
    return f"https://www.twitch.tv/videos/{vod_id}"


def _tail(text: str, lines: int = 5) -> str:
    rows = [row for row in text.strip().splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


async def run(*cmd: str, timeout: float | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"{cmd[0]}: превышено время ожидания") from None
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def fetch_vod_info(vod_id: str) -> VodInfo:
    """Метаданные и главы VOD без скачивания видео."""
    code, out, err = await run("yt-dlp", "-J", "--no-warnings", vod_url(vod_id), timeout=180)
    if code != 0:
        raise ToolError(_tail(err) or f"yt-dlp завершился с кодом {code}")
    data = json.loads(out)
    timestamp = data.get("timestamp")
    return VodInfo(
        id=str(data.get("id") or vod_id).lstrip("v"),
        title=data.get("title") or "",
        duration=int(data.get("duration") or 0),
        uploader=data.get("uploader") or data.get("uploader_id") or "",
        uploader_login=(data.get("uploader_id") or "").lower(),
        started_at=datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else None,
        is_live=bool(data.get("is_live")),
        chapters=list(data.get("chapters") or []),
    )


async def download_section(vod_id: str, start: int, end: int, dest_stem: Path) -> Path:
    """Скачивает кусок VOD [start, end) в исходном качестве, без перекодирования."""
    for leftover in dest_stem.parent.glob(dest_stem.name + ".*"):
        leftover.unlink(missing_ok=True)
    code, out, err = await run(
        "nice", "-n", "10",
        "yt-dlp", "--no-warnings", "--no-progress",
        "-f", "best",
        "--download-sections", f"*{start}-{end}",
        "-o", f"{dest_stem}.%(ext)s",
        "--print", "after_move:filepath", "--no-simulate",
        vod_url(vod_id),
        # даже на медленном канале кусок качается быстрее, чем длится
        timeout=max(1800, end - start),
    )
    if code != 0:
        raise ToolError(_tail(err) or f"yt-dlp завершился с кодом {code}")
    lines = [line for line in out.splitlines() if line.strip()]
    path = Path(lines[-1]) if lines else None
    if path is None or not path.exists():
        raise ToolError("yt-dlp не сообщил, куда сохранил файл")
    return path


async def probe(path: Path) -> dict:
    code, out, err = await run(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
        timeout=300,
    )
    if code != 0:
        raise ToolError(_tail(err) or "ffprobe не смог прочитать файл")
    return json.loads(out)


async def ytdlp_version() -> str:
    try:
        code, out, _ = await run("yt-dlp", "--version", timeout=30)
    except (OSError, ToolError):
        return "не найден"
    return out.strip() if code == 0 else "не найден"


async def update_ytdlp() -> None:
    try:
        code, _, err = await run(
            "pip", "install", "-q", "--no-cache-dir", "-U", "--pre", "yt-dlp[default]", timeout=600
        )
    except (OSError, ToolError) as exc:
        log.warning("Не удалось обновить yt-dlp: %s", exc)
        return
    if code != 0:
        log.warning("Не удалось обновить yt-dlp: %s", _tail(err))
