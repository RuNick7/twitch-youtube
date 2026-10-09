"""Запросы к GraphQL Twitch: идёт ли стрим и как он называется, популярные клипы канала.

Запросы лёгкие (десятки миллисекунд), поэтому название можно спрашивать раз в минуту,
пока идёт стрим. ID клиента берётся из установленного yt-dlp: Twitch его иногда меняет,
а yt-dlp обновляется раз в сутки.
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
# Идёт ли стрим и как он называется — сразу у нескольких каналов
USERS_QUERY = (
    "query($logins: [String!]) { users(logins: $logins) { login stream { id createdAt } broadcastSettings { title } } }"
)
USERS_PER_QUERY = 50
NAME_QUERY = "query($login: String!) { user(login: $login) { login displayName } }"
# Клипы за последние 7 дней, самые просматриваемые первыми
CLIPS_QUERY = (
    "query($login: String!) { user(login: $login) { clips(first: 50, criteria: {period: LAST_WEEK, sort: VIEWS_DESC}) "
    "{ edges { node { id slug title viewCount durationSeconds createdAt curator { displayName login } "
    "game { name } video { id title } videoOffsetSeconds } } } } }"
)


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


async def _gql(http: httpx.AsyncClient, query: str, variables: dict, client: str) -> dict:
    """Поле data ответа GraphQL."""
    try:
        resp = await http.post(
            GQL_URL,
            content=json.dumps({"query": query, "variables": variables}),
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
    return data.get("data") or {}


async def _user(http: httpx.AsyncClient, query: str, channel: str, client: str) -> dict:
    """Ответ GraphQL на запрос про канал: объект user."""
    user = (await _gql(http, query, {"login": channel}, client)).get("user")
    if user is None:
        raise TwitchError(f"канала {channel} нет на Twitch")
    return user


async def channel_display_name(http: httpx.AsyncClient, channel: str, client: str) -> str:
    """Название канала, как его видят зрители. TwitchError — такого канала нет или Twitch не ответил."""
    user = await _user(http, NAME_QUERY, channel, client)
    return str(user.get("displayName") or user.get("login") or channel)


async def popular_clips(http: httpx.AsyncClient, channel: str, client: str) -> list[dict]:
    """Клипы канала за последние 7 дней, самые просматриваемые первыми (сырые объекты GraphQL)."""
    user = await _user(http, CLIPS_QUERY, channel, client)
    return [edge["node"] for edge in ((user.get("clips") or {}).get("edges") or []) if edge.get("node")]


async def live_states(http: httpx.AsyncClient, logins: list[str], client: str) -> dict[str, LiveState | None]:
    """Идущие стримы каналов: логин → стрим или None, если стрима нет. Один запрос на 50 каналов."""
    result: dict[str, LiveState | None] = {}
    for i in range(0, len(logins), USERS_PER_QUERY):
        chunk = logins[i : i + USERS_PER_QUERY]
        users = (await _gql(http, USERS_QUERY, {"logins": chunk}, client)).get("users") or []
        # Каналы в ответе идут в порядке запроса, вместо несуществующего — null
        for login, user in zip(chunk, users):
            if user and str(user.get("login") or login).lower() != login.lower():
                raise TwitchError("Twitch ответил про каналы не в том порядке, в каком их спросили")
            result[login] = parse_live(user) if user else None
    return result


def parse_live(user: dict) -> LiveState | None:
    """Идущий стрим из объекта user GraphQL; None, если стрима нет."""
    stream = user.get("stream")
    if not stream:
        return None
    title = ((user.get("broadcastSettings") or {}).get("title") or "").strip()
    try:
        started = datetime.fromisoformat(stream["createdAt"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise TwitchError("Twitch не сообщил время начала стрима") from exc
    return LiveState(stream_id=str(stream.get("id") or ""), started_at=started, title=title)
