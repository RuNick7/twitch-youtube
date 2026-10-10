"""Загрузка на YouTube, когда токен доступа перестал действовать раньше срока."""

import unittest
from unittest import mock

try:
    import httpx

    from app import youtube as yt
except ImportError:  # без httpx из requirements.txt эти тесты пропускаются
    raise unittest.SkipTest("нужен httpx")

SESSION = "https://upload.test/session"
DATA = b"0123456789"
CHUNK = 4


class Google:
    """Поддельный Google: токены доступа AT-1, AT-2, … и одна сессия загрузки."""

    def __init__(self):
        self.issued = 0
        self.live: str | None = None  # действующий токен доступа
        self.revoked = False  # доступ отозван: нового токена не будет
        self.refused: set[str] = set()  # виды запросов, которые отклоняются и с новым токеном
        # Номер попытки отправить кусок -> что случается перед ней. Число в ответ — статус вместо приёма куска
        self.before_chunk: dict[int, object] = {}
        self.kept = 0  # сколько байт отклонённого куска всё же осталось у YouTube
        self.attempts = 0
        self.data = b""
        self.log: list[str] = []

    def expire(self) -> None:
        """Токен доступа перестаёт действовать, но новый получить можно."""
        self.live = None

    def revoke(self) -> None:
        self.live, self.revoked = None, True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        kind, response = self._serve(request)
        self.log.append(f"{kind} {response.status_code}")
        return response

    def _serve(self, request: httpx.Request) -> tuple[str, httpx.Response]:
        if request.url == yt.TOKEN_URL:
            if self.revoked:
                return "token", httpx.Response(400, json={"error": "invalid_grant"})
            self.issued += 1
            self.live = f"AT-{self.issued}"
            return "token", httpx.Response(200, json={"access_token": self.live, "expires_in": 3600})
        content_range = request.headers.get("Content-Range", "")
        if request.method == "POST":
            kind = what = "create"
        elif content_range.startswith("bytes */"):
            kind = what = "query"
        else:
            what = "chunk"
            kind = "chunk " + content_range.removeprefix("bytes ").split("/")[0]
            self.attempts += 1
            assert self.attempts < 20, "кусок отправляется без конца"
            status = self.before_chunk.pop(self.attempts, lambda: None)()
            if status:
                return kind, httpx.Response(status)
        if what in self.refused or request.headers.get("Authorization") != f"Bearer {self.live}":
            if what == "chunk":
                self.data += request.content[: self.kept]
            error = {"code": 401, "message": "Invalid Credentials", "errors": [{"reason": "authError"}]}
            return kind, httpx.Response(401, json={"error": error})
        if what == "create":
            return kind, httpx.Response(200, headers={"Location": SESSION})
        if what == "chunk":
            if int(kind.split()[1].split("-")[0]) != len(self.data):
                return kind, httpx.Response(400, json={"error": {"code": 400, "message": "bad range"}})
            self.data += request.content
        if len(self.data) == len(DATA):
            return kind, httpx.Response(200, json={"id": "video"})
        return kind, httpx.Response(308, headers={"Range": f"bytes=0-{len(self.data) - 1}"} if self.data else {})


class StaleAccessTokenTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.google = Google()
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.google))
        self.addAsyncCleanup(http.aclose)
        self.client = yt.YouTubeClient("client-id", "client-secret", http)
        self.enterContext(mock.patch.object(yt, "CHUNK_ALIGN", CHUNK))
        self.sleep = self.enterContext(mock.patch.object(yt.asyncio, "sleep", new_callable=mock.AsyncMock))
        await self.client.access_token("RT")  # токен AT-1 получен и лежит в кэше
        self.google.log.clear()

    async def upload(self, session_uri=None):
        async def source(offset):
            for start in range(offset, len(DATA), 3):  # порции источника не совпадают с кусками загрузки
                yield DATA[start : start + 3]

        return await self.client.upload_stream(
            "RT",
            len(DATA),
            source,
            {"snippet": {"title": "ролик"}},
            session_uri=session_uri,
            on_session=mock.AsyncMock(),
            on_progress=mock.AsyncMock(),
            chunk_size=CHUNK,
        )

    async def assert_fails(self, error, log, session_uri=None):
        with self.assertRaises(yt.YouTubeError) as caught:
            await self.upload(session_uri)
        self.assertIs(type(caught.exception), error)
        self.assertEqual(self.google.log, log)
        return caught.exception

    async def test_new_upload_takes_fresh_token(self):
        self.google.expire()
        self.assertEqual(await self.upload(), "video")
        self.assertEqual(self.google.log[:3], ["create 401", "token 200", "create 200"])
        self.assertEqual(self.google.data, DATA)

    async def test_resumed_upload_takes_fresh_token(self):
        self.google.data = DATA[:CHUNK]
        self.google.expire()
        self.assertEqual(await self.upload(SESSION), "video")
        self.assertEqual(self.google.log, ["query 401", "token 200", "query 308", "chunk 4-7 308", "chunk 8-9 200"])
        self.assertEqual(self.google.data, DATA)

    async def test_refused_chunk_is_sent_again_after_asking_youtube_for_offset(self):
        self.google.before_chunk[2] = self.google.expire
        self.assertEqual(await self.upload(), "video")
        self.assertEqual(
            self.google.log[1:],
            ["chunk 0-3 308", "chunk 4-7 401", "token 200", "query 308", "chunk 4-7 308", "chunk 8-9 200"],
        )
        self.assertEqual(self.google.data, DATA)
        self.sleep.assert_not_awaited()  # 401 — не сбой загрузки: ни паузы, ни счёта попыток

    async def test_token_refused_while_connection_dropped_is_renewed_by_offset_query(self):
        def drop():
            self.google.expire()
            raise httpx.ReadError("соединение оборвалось")

        self.google.before_chunk[2] = drop
        with self.assertLogs(yt.log, "WARNING"):
            self.assertEqual(await self.upload(), "video")
        self.assertEqual(
            self.google.log[1:],
            ["chunk 0-3 308", "query 401", "token 200", "query 308", "chunk 4-7 308", "chunk 8-9 200"],
        )
        self.assertEqual(self.google.data, DATA)

    async def test_upload_goes_on_from_offset_youtube_reports(self):
        self.google.before_chunk[2] = self.google.expire
        self.google.kept = 2
        self.assertEqual(await self.upload(), "video")
        self.assertEqual(self.google.log[2:], ["chunk 4-7 401", "token 200", "query 308", "chunk 6-9 200"])
        self.assertEqual(self.google.data, DATA)

    async def test_token_may_expire_again_once_upload_has_moved_on(self):
        self.google.before_chunk[1] = self.google.before_chunk[3] = self.google.expire
        self.assertEqual(await self.upload(), "video")
        self.assertEqual(self.google.data, DATA)
        self.assertEqual(self.google.issued, 3)

    async def test_revoked_access_stops_new_upload(self):
        self.google.revoke()
        await self.assert_fails(yt.AuthError, ["create 401", "token 400"])

    async def test_revoked_access_stops_resumed_upload(self):
        self.google.revoke()
        await self.assert_fails(yt.AuthError, ["query 401", "token 400"], SESSION)

    async def test_revoked_access_stops_upload_midway(self):
        self.google.before_chunk[2] = self.google.revoke
        await self.assert_fails(yt.AuthError, ["create 200", "chunk 0-3 308", "chunk 4-7 401", "token 400"])

    async def test_session_refused_with_fresh_token_is_plain_error(self):
        self.google.refused.add("create")
        error = await self.assert_fails(yt.YouTubeError, ["create 401", "token 200", "create 401"])
        self.assertEqual((error.status, error.reason), (401, "authError"))

    async def test_chunk_refused_with_fresh_token_is_not_retried_forever(self):
        self.google.refused.add("chunk")
        error = await self.assert_fails(
            yt.YouTubeError, ["create 200", "chunk 0-3 401", "token 200", "query 308", "chunk 0-3 401"]
        )
        self.assertEqual((error.status, error.reason), (401, "authError"))

    async def test_server_error_still_waits_before_asking_for_offset(self):
        self.google.before_chunk[2] = lambda: 503
        with self.assertLogs(yt.log, "WARNING"):
            self.assertEqual(await self.upload(), "video")
        self.assertEqual(
            self.google.log[1:], ["chunk 0-3 308", "chunk 4-7 503", "query 308", "chunk 4-7 308", "chunk 8-9 200"]
        )
        self.sleep.assert_awaited_once_with(2)
