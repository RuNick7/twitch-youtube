"""Состояние канала Twitch одним запросом к GraphQL: идёт ли стрим и как он называется.

Запрос лёгкий (десятки миллисекунд), поэтому его можно делать раз в минуту, пока идёт
стрим. ID клиента берётся из установленного yt-dlp: Twitch его иногда меняет, а yt-dlp
обновляется раз в сутки.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime

import httpx

from .tools import ToolError, run

GQL_URL = "https://gql.twitch.tv/gql"
# Если yt-dlp не ответил: значение из yt-dlp 2026.09
FALLBACK_CLIENT_ID = "ue6666qo983tsx6so1t0vnawi233wa"
QUERY = "query($login: String!) { user(login: $login) { stream { id createdAt } broadcastSettings { title } } }"


class TwitchError(RuntimeError):
    pass


@dataclass
class LiveState:
    stream_id: str
    started_at: datetime
    title: str


async def client_id() -> str:
    """ID веб-клиента Twitch из yt-dlp (отдельным процессом, чтобы не держать yt-dlp в памяти бота)."""
    code = "from yt_dlp import YoutubeDL; print(YoutubeDL({'quiet': True}).get_info_extractor('TwitchStream')._CLIENT_ID)"
    try:
        status, out, _ = await run(sys.executable, "-c", code, timeout=60)
    except (OSError, ToolError):
        return FALLBACK_CLIENT_ID
    value = out.strip()
    return value if status == 0 and value.isalnum() else FALLBACK_CLIENT_ID


async def live_state(http: httpx.AsyncClient, channel: str, client: str) -> LiveState | None:
    """Текущий стрим канала или None, если стрима нет."""
    try:
        resp = await http.post(
            GQL_URL,
            content=json.dumps({"query": QUERY, "variables": {"login": channel}}),
            headers={"Client-ID": client, "Content-Type": "text/plain;charset=UTF-8"},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise TwitchError(f"Twitch недоступен: {exc}") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise TwitchError(f"Twitch ответил не JSON (HTTP {resp.status_code})") from exc
    if resp.status_code != 200 or not isinstance(data, dict) or "data" not in data:
        message = data.get("message") if isinstance(data, dict) else None
        raise TwitchError(f"Twitch ответил HTTP {resp.status_code}: {message or resp.text[:200]}")
    user = (data.get("data") or {}).get("user")
    if user is None:
        raise TwitchError(f"канала {channel} нет на Twitch")
    stream = user.get("stream")
    if not stream:
        return None
    title = ((user.get("broadcastSettings") or {}).get("title") or "").strip()
    try:
        started = datetime.fromisoformat(stream["createdAt"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise TwitchError("Twitch не сообщил время начала стрима") from exc
    return LiveState(stream_id=str(stream.get("id") or ""), started_at=started, title=title)
