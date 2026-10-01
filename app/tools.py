"""Внешние инструменты: yt-dlp и pip."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

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
    playlist_url: str | None = None  # HLS-плейлист исходного качества


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
    """Метаданные, главы и плейлист VOD без скачивания видео."""
    code, out, err = await run("yt-dlp", "-J", "-f", "best", "--no-warnings", vod_url(vod_id), timeout=180)
    if code != 0:
        raise ToolError(_tail(err) or f"yt-dlp завершился с кодом {code}")
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise ToolError("yt-dlp вернул не JSON") from exc
    timestamp = data.get("timestamp")
    protocol = str(data.get("protocol") or "")
    return VodInfo(
        id=str(data.get("id") or vod_id).lstrip("v"),
        title=data.get("title") or "",
        duration=int(data.get("duration") or 0),
        uploader=data.get("uploader") or data.get("uploader_id") or "",
        uploader_login=(data.get("uploader_id") or "").lower(),
        started_at=datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else None,
        is_live=bool(data.get("is_live")),
        chapters=list(data.get("chapters") or []),
        playlist_url=data.get("url") if protocol.startswith("m3u8") else None,
    )


async def list_channel_vods(channel: str, limit: int = 5) -> list[str]:
    """ID последних записей эфиров канала, новые первыми."""
    code, out, err = await run(
        "yt-dlp", "--no-warnings", "--flat-playlist", "-I", f"1:{limit}", "--print", "id",
        f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time",
        timeout=120,
    )
    if code != 0:
        raise ToolError(_tail(err) or f"yt-dlp завершился с кодом {code}")
    return [line.strip().lstrip("v") for line in out.splitlines() if line.strip()]


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
