"""YouTube Data API: вход по коду (device flow), загрузка потока с докачкой, состояние и публикация."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncGenerator, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# youtube.upload в device flow недоступен, а youtube покрывает загрузку и изменение видео
SCOPE = "https://www.googleapis.com/auth/youtube"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_URL = "https://www.googleapis.com/youtube/v3"

CHUNK_ALIGN = 256 * 1024  # размер куска должен быть кратен 256 КБ
RETRYABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 8

# Ошибки, при которых дело не в конкретном ролике, а в лимитах канала или проекта
LIMIT_REASONS = {"uploadLimitExceeded", "quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}

ProgressCallback = Callable[[int], Awaitable[None]]
SessionCallback = Callable[[str], Awaitable[None]]
StreamFactory = Callable[[int], AsyncGenerator[bytes, None]]


class YouTubeError(RuntimeError):
    def __init__(self, message: str, reason: str | None = None, status: int | None = None):
        super().__init__(message)
        self.reason = reason
        self.status = status


class AuthError(YouTubeError):
    """Refresh-токен отозван или истёк: нужно заново выполнить /youtube."""


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


def _error(resp: httpx.Response) -> YouTubeError:
    reason = None
    message = resp.text[:300]
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = err.get("message") or message
            details = err.get("errors") or []
            if details and isinstance(details[0], dict):
                reason = details[0].get("reason")
        elif isinstance(err, str):
            reason = err
            message = body.get("error_description") or err
    return YouTubeError(f"HTTP {resp.status_code}: {message}", reason, resp.status_code)


async def _body(buffer: bytearray, size: int, step: int = 1 << 20) -> AsyncGenerator[bytes, None]:
    """Тело куска порциями по 1 МБ: целиком кусок (32 МБ) в памяти не копируется.

    С явным Content-Length httpx отправляет такое тело без chunked-кодирования.
    """
    for start in range(0, size, step):
        with memoryview(buffer) as view:
            piece = bytes(view[start : min(size, start + step)])
        yield piece


def _received_bytes(resp: httpx.Response) -> int:
    """Сколько байт YouTube уже принял: заголовок Range вида bytes=0-12345."""
    value = resp.headers.get("Range", "")
    if "-" not in value:
        return 0
    return int(value.rsplit("-", 1)[1]) + 1


class YouTubeClient:
    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._access: dict[str, tuple[str, float]] = {}

    # --- авторизация ---

    async def start_device_flow(self) -> DeviceCode:
        resp = await self._http.post(DEVICE_CODE_URL, data={"client_id": self._client_id, "scope": SCOPE})
        if resp.status_code != 200:
            raise _error(resp)
        data = resp.json()
        return DeviceCode(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_url=data.get("verification_url") or "https://www.google.com/device",
            expires_in=int(data.get("expires_in", 1800)),
            interval=int(data.get("interval", 5)),
        )

    async def finish_device_flow(self, code: DeviceCode) -> str:
        """Ждёт, пока пользователь введёт код, и возвращает refresh-токен."""
        interval = code.interval
        deadline = time.monotonic() + code.expires_in
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            resp = await self._http.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "device_code": code.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                refresh_token = data.get("refresh_token")
                if not refresh_token:
                    raise YouTubeError("Google не выдал refresh-токен")
                self._remember(refresh_token, data)
                return refresh_token
            error = _error(resp)
            if error.reason == "authorization_pending":
                continue
            if error.reason == "slow_down":
                interval += 5
                continue
            if error.reason == "access_denied":
                raise YouTubeError("доступ не выдан")
            if error.reason == "expired_token":
                break
            raise error
        raise YouTubeError("код истёк, запустите /youtube ещё раз")

    async def access_token(self, refresh_token: str) -> str:
        cached = self._access.get(refresh_token)
        if cached and cached[1] - 60 > time.monotonic():
            return cached[0]
        resp = await self._http.post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code != 200:
            error = _error(resp)
            if error.reason in ("invalid_grant", "unauthorized_client"):
                raise AuthError("доступ к YouTube отозван или истёк", error.reason, error.status)
            raise error
        self._remember(refresh_token, resp.json())
        return self._access[refresh_token][0]

    def _remember(self, refresh_token: str, data: dict) -> None:
        expires_at = time.monotonic() + int(data.get("expires_in", 3600))
        self._access[refresh_token] = (data["access_token"], expires_at)

    async def _auth(self, refresh_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.access_token(refresh_token)}"}

    async def my_channel(self, refresh_token: str) -> tuple[str, str]:
        """ID и название канала, к которому выдан доступ."""
        resp = await self._http.get(
            f"{API_URL}/channels",
            params={"part": "snippet", "mine": "true"},
            headers=await self._auth(refresh_token),
        )
        if resp.status_code != 200:
            raise _error(resp)
        items = resp.json().get("items") or []
        if not items:
            raise YouTubeError("у этого аккаунта нет YouTube-канала")
        return items[0]["id"], items[0]["snippet"]["title"]

    # --- загрузка ---

    async def upload_stream(
        self,
        refresh_token: str,
        total: int,
        open_stream: StreamFactory,
        metadata: dict,
        *,
        session_uri: str | None,
        on_session: SessionCallback,
        on_progress: ProgressCallback,
        chunk_size: int,
        limit_mbit: int = 0,
    ) -> str:
        """Загружает поток по протоколу resumable upload и возвращает ID видео.

        open_stream(offset) должен каждый раз отдавать одни и те же байты
        начиная с offset: так загрузка продолжается с последнего принятого
        байта и после сбоя сети, и после перезапуска (по session_uri).
        """
        chunk_size = max(CHUNK_ALIGN, chunk_size // CHUNK_ALIGN * CHUNK_ALIGN)
        offset = 0
        if session_uri:
            state = await self._query(refresh_token, session_uri, total)
            if isinstance(state, str):
                return state
            if state is None:
                session_uri = None
            else:
                offset = state
        if not session_uri:
            session_uri = await self._create_session(refresh_token, total, metadata)
            await on_session(session_uri)
            offset = 0

        # buffer — прочитанные из источника байты [buffer_start, buffer_start + len(buffer))
        buffer = bytearray()
        buffer_start = offset
        source = open_stream(offset)
        failures = 0
        try:
            while True:
                if not buffer_start <= offset <= buffer_start + len(buffer):
                    await source.aclose()
                    source = open_stream(offset)
                    buffer, buffer_start = bytearray(), offset
                elif offset > buffer_start:
                    # Новый буфер из хвоста, а не del buffer[:n]: иначе bytearray
                    # при дописывании в конец выделяет память с запасом на удалённое начало
                    buffer = buffer[offset - buffer_start :]
                    buffer_start = offset
                while len(buffer) < chunk_size and buffer_start + len(buffer) < total:
                    try:
                        buffer += await anext(source)
                    except StopAsyncIteration:
                        raise YouTubeError("источник закончился раньше заявленного размера") from None
                if buffer_start + len(buffer) > total:
                    raise YouTubeError("источник длиннее заявленного размера")
                size = min(chunk_size, len(buffer))
                started = time.monotonic()
                problem: Exception
                try:
                    resp = await self._http.put(
                        session_uri,
                        content=_body(buffer, size),
                        headers={
                            **await self._auth(refresh_token),
                            "Content-Length": str(size),
                            "Content-Range": f"bytes {offset}-{offset + size - 1}/{total}",
                        },
                        timeout=httpx.Timeout(60.0, read=600.0, write=600.0),
                    )
                except httpx.TransportError as exc:
                    problem = exc
                else:
                    if resp.status_code in (200, 201):
                        await on_progress(100)
                        return resp.json()["id"]
                    if resp.status_code == 308:
                        offset = _received_bytes(resp)
                        failures = 0
                        await on_progress(offset * 100 // total)
                        await self._throttle(size, started, limit_mbit)
                        continue
                    if resp.status_code not in RETRYABLE_STATUS:
                        raise _error(resp)
                    problem = _error(resp)

                failures += 1
                if failures > MAX_RETRIES:
                    raise YouTubeError(f"загрузка прервалась: {problem}")
                log.warning("Сбой загрузки (%s), попытка %s из %s", problem, failures, MAX_RETRIES)
                await asyncio.sleep(min(60, 2**failures))
                try:
                    state = await self._query(refresh_token, session_uri, total)
                except httpx.TransportError:
                    continue  # сеть ещё не вернулась: повторим с того же места
                if isinstance(state, str):
                    return state
                if state is None:
                    raise YouTubeError("сессия загрузки истекла")
                offset = state
        finally:
            await source.aclose()

    # --- состояние и публикация ---

    async def videos(self, refresh_token: str, ids: list[str]) -> dict[str, dict]:
        """Состояние роликов по ID. Удалённых роликов в ответе нет. 1 единица квоты на 50 роликов."""
        result: dict[str, dict] = {}
        for i in range(0, len(ids), 50):
            resp = await self._http.get(
                f"{API_URL}/videos",
                params={"part": "status,processingDetails,contentDetails", "id": ",".join(ids[i : i + 50])},
                headers=await self._auth(refresh_token),
            )
            if resp.status_code != 200:
                raise _error(resp)
            for item in resp.json().get("items") or []:
                result[item["id"]] = item
        return result

    async def set_privacy(self, refresh_token: str, video_id: str, privacy: str, current: dict) -> dict:
        """Меняет доступ к ролику и возвращает новый status. 50 единиц квоты.

        videos.update заменяет status целиком, поэтому изменяемые поля
        переносятся из текущего состояния.
        """
        status = {key: current[key] for key in ("embeddable", "license", "publicStatsViewable") if key in current}
        status.update(privacyStatus=privacy, selfDeclaredMadeForKids=False, containsSyntheticMedia=False)
        resp = await self._http.put(
            f"{API_URL}/videos",
            params={"part": "status"},
            json={"id": video_id, "status": status},
            headers=await self._auth(refresh_token),
        )
        if resp.status_code != 200:
            raise _error(resp)
        return resp.json().get("status") or {}

    async def _create_session(self, refresh_token: str, total: int, metadata: dict) -> str:
        resp = await self._http.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": ",".join(metadata)},
            json=metadata,
            headers={
                **await self._auth(refresh_token),
                "X-Upload-Content-Length": str(total),
                "X-Upload-Content-Type": "video/*",
            },
        )
        if resp.status_code != 200 or "Location" not in resp.headers:
            raise _error(resp)
        return resp.headers["Location"]

    async def _query(self, refresh_token: str, session_uri: str, total: int) -> int | str | None:
        """Сколько байт принято, ID готового видео или None, если сессия истекла."""
        resp = await self._http.put(
            session_uri,
            content=b"",
            headers={**await self._auth(refresh_token), "Content-Range": f"bytes */{total}"},
        )
        if resp.status_code in (200, 201):
            return resp.json()["id"]
        if resp.status_code == 308:
            return _received_bytes(resp)
        if resp.status_code in (404, 410):
            return None
        raise _error(resp)

    @staticmethod
    async def _throttle(sent: int, started: float, limit_mbit: int) -> None:
        """Не занимать весь канал сервера."""
        if limit_mbit <= 0:
            return
        wait = sent * 8 / (limit_mbit * 1_000_000) - (time.monotonic() - started)
        if wait > 0:
            await asyncio.sleep(wait)
