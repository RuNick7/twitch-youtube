"""Сквозной тест: слежение за каналом, фильтр, потоковая загрузка, проверки, автопубликация,
ежедневная сверка с YouTube, отключение канала и отзыв доступа.

Настоящие: код приложения (app.__main__.main), Twitch (главы, плейлисты и фрагменты VOD), yt-dlp, SQLite.
Поддельные: Telegram Bot API и Google (OAuth и отзыв токена, upload, channels.list, videos.list, videos.update).
Слежение за каналом получает список VOD из подставной функции, но видео берёт с настоящего Twitch.

Запуск описан в README («Тесты»). Twitch удаляет записи через 60 дней: когда VOD ниже
пропадут, замените их ID и названия в google.behaviours на свежие с того же канала.
"""

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import resource
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from cryptography.fernet import Fernet

OWNER, STRANGER = 111111, 222222
PORT = 18080
BASE = f"http://127.0.0.1:{PORT}"
WORK = Path("/work")
VOD_SERIES = "2856928518"  # «ВО ВСЕ ТЯЖКИЕ 5 сезон, 6 серия»: JC, Minecraft, JC
VOD_NORMAL = "2881901989"  # «ФРИКЛЕНД - 75 СТВОЛОВ ЯРОСТИ»: JC, Minecraft
VOD_WATCH = "2884562233"  # через слежение за каналом
VOD_OLD = "2881005366"  # закончился до начала слежения

os.environ.update(
    TELEGRAM_BOT_TOKEN="123456:TEST-fake-token",
    TELEGRAM_OWNER_ID=str(OWNER),
    TWITCH_CHANNEL="zakvielchannel",
    GOOGLE_CLIENT_ID="fake-client.apps.googleusercontent.com",
    GOOGLE_CLIENT_SECRET="fake-secret",
    SECRET_KEY=Fernet.generate_key().decode(),
    DATA_DIR=str(WORK / "data"),
    TZ="Europe/Moscow",
    UPLOAD_CHUNK_MB="8",
    PUBLISH_DELAY_MIN="0",
    WATCH_INTERVAL_SEC="2",
    WATCH_GRACE_MIN="0",
    MONITOR_DAYS="1",
    TITLE_POLL_SEC="1",
    SKIP_SHORTER_MIN="4",
    STREAMER_NAME="Заквиель",
    # Minecraft нумеруется: в шаге 9 его короткий кусок пропущен и не должен превратить единственную часть в «2/2»
    NO_PART_CATEGORIES="Just Chatting",
)

import app.__main__ as entry  # noqa: E402
import app.bot as bot_mod  # noqa: E402
import app.checker as checker_mod  # noqa: E402
import app.hls as hls  # noqa: E402
import app.refresher as refresher_mod  # noqa: E402
import app.service as service_mod  # noqa: E402
import app.shorts as shorts_mod  # noqa: E402
import app.watcher as watcher_mod  # noqa: E402
import app.worker as worker_mod  # noqa: E402
import app.youtube as yt  # noqa: E402
import httpx  # noqa: E402
from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.db import (  # noqa: E402
    KV,
    SHORT,
    Playlist,
    Segment,
    Status,
    Streamer,
    TitleChange,
    Vod,
    as_utc,
    get_spans,
    get_warnings,
    init_db,
    make_engine,
)
from app.tools import VodInfo, fetch_vod_info  # noqa: E402
from app.twitch import LiveState  # noqa: E402

yt.DEVICE_CODE_URL = f"{BASE}/device/code"
yt.TOKEN_URL = f"{BASE}/token"
yt.REVOKE_URL = f"{BASE}/revoke"
yt.UPLOAD_URL = f"{BASE}/upload/youtube/v3/videos"
yt.API_URL = f"{BASE}/youtube/v3"
# ускоряем таймеры: в жизни это минуты и часы. Ежедневная сверка запускается в тесте сбросом отметки в базе
checker_mod.TICK_SEC = 1
refresher_mod.TICK_SEC = 1
checker_mod.RECHECK = timedelta(seconds=1)
checker_mod.MONITOR_EVERY = service_mod.MONITOR_EVERY = timedelta(seconds=3)
worker_mod.FIRST_CHECK_AFTER = timedelta(seconds=1)

# «Популярные клипы» канала для Shorts: подставляются в шаге 10б, сами клипы настоящие
CLIPS: list[dict] = []


async def fake_popular_clips(http, channel, client):
    return list(CLIPS)


async def fake_client_id():
    return "test-client"


shorts_mod.popular_clips = fake_popular_clips
shorts_mod.client_id = fake_client_id


def clip_node(clip_id, slug, title, views, duration, game, vod=None, offset=None):
    return {"id": clip_id, "slug": slug, "title": title, "viewCount": views, "durationSeconds": duration,
            "createdAt": "2026-09-27T17:57:52Z", "curator": {"displayName": "viewer", "login": "viewer"},
            "game": {"name": game}, "video": {"id": vod, "title": "Стрим"} if vod else None,
            "videoOffsetSeconds": offset}


async def ffprobe_dims(path):
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0",
        str(path), stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    width, height = out.decode().strip().split(",")[:2]
    return int(width), int(height)


RESULTS: list[tuple[bool, str]] = []


def check(ok, name, detail=""):
    RESULTS.append((bool(ok), name))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok or not detail else f"  <-- {detail}"), flush=True)


async def eventually(pred, timeout=10.0):
    """Значение pred, когда оно появится, или None: сообщение уходит чуть позже смены статуса в базе."""
    try:
        return await wait_for(pred, timeout)
    except AssertionError:
        return None


async def wait_for(pred, timeout=240.0, what=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(0.3)
    raise AssertionError(f"не дождался: {what}")


# ---------- поддельный Telegram ----------

TAG = re.compile(r"</?(b|strong|i|em|u|ins|s|strike|del|a|code|pre|tg-spoiler|span|blockquote|tg-emoji)(\s[^<>]*)?>")


def html_problems(text):
    problems, stack = [], []
    for m in TAG.finditer(text):
        if m.group(0).startswith("</"):
            if not stack or stack.pop() != m.group(1):
                problems.append(f"unbalanced {m.group(0)}")
        else:
            stack.append(m.group(1))
    if stack:
        problems.append(f"unclosed {stack}")
    rest = TAG.sub("", text)
    if "<" in rest or ">" in rest:
        problems.append("raw < or >")
    if re.search(r"&(?!(lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);)", rest):
        problems.append("raw &")
    if len(text) > 4096:
        problems.append(f"too long: {len(text)}")
    return problems


def markup_json(markup):
    return markup.model_dump_json(exclude_none=True) if markup is not None else None


def buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row] if markup else []


def button_data(markup, prefix):
    for row in markup.inline_keyboard:
        for b in row:
            if b.text.startswith(prefix) and b.callback_data:
                return b.callback_data
    raise AssertionError(f"нет кнопки {prefix!r} в {buttons(markup)}")


class FakeTelegram(BaseSession):
    KNOWN = {"GetMe", "SendMessage", "EditMessageText", "EditMessageReplyMarkup", "DeleteMessage", "AnswerCallbackQuery"}

    def __init__(self):
        super().__init__()
        self.updates = asyncio.Queue()
        self.next_message_id = 100
        self.next_update_id = 1
        self.messages = {}
        self.answers = []
        self.problems = []
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        if False:
            yield b""

    def _user(self, uid):
        return {"id": uid, "is_bot": False, "first_name": "Tester"}

    def push_text(self, uid, text):
        n = self.next_update_id
        self.next_update_id += 1
        self.updates.put_nowait({"update_id": n, "message": {"message_id": 50_000 + n, "date": int(time.time()),
                                 "chat": {"id": uid, "type": "private"}, "from": self._user(uid), "text": text}})

    def push_callback(self, uid, data, message_id):
        n = self.next_update_id
        self.next_update_id += 1
        stored = self.messages.get(message_id, {})
        message = {"message_id": message_id, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
                   "text": stored.get("text", "")}
        if stored.get("markup") is not None:  # как в настоящем Telegram: сообщение приходит вместе с кнопками
            message["reply_markup"] = json.loads(markup_json(stored["markup"]))
        self.updates.put_nowait({"update_id": n, "callback_query": {
            "id": f"cb{n}", "from": self._user(uid), "chat_instance": "ci", "data": data, "message": message}})

    def _check(self, name, method):
        text = getattr(method, "text", None)
        if isinstance(text, str):
            for p in html_problems(text):
                self.problems.append(f"{name}: {p}: {text[:120]!r}")
        for row in getattr(getattr(method, "reply_markup", None), "inline_keyboard", None) or []:
            for b in row:
                if b.callback_data and len(b.callback_data.encode()) > 64:
                    self.problems.append(f"callback_data > 64 байт: {b.callback_data}")

    def _msg(self, chat, mid, text):
        return {"message_id": mid, "date": int(time.time()), "chat": {"id": chat, "type": "private"}, "text": text}

    def _fail(self, bot, method, code, description):
        return self.check_response(bot, method, code, json.dumps({"ok": False, "error_code": code, "description": description}))

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        if name == "GetUpdates":
            try:
                result = [await asyncio.wait_for(self.updates.get(), timeout=1)]
            except asyncio.TimeoutError:
                result = []
            return self.check_response(bot, method, 200, json.dumps({"ok": True, "result": result})).result
        self.calls.append(name)
        self._check(name, method)
        if name not in self.KNOWN:
            self.problems.append(f"неожиданный метод {name}")
        result = True
        if name == "GetMe":
            result = {"id": 123456, "is_bot": True, "first_name": "Test", "username": "test_bot"}
        elif name == "SendMessage":
            self.next_message_id += 1
            mid = self.next_message_id
            self.messages[mid] = {"chat": method.chat_id, "text": method.text, "markup": method.reply_markup,
                                  "silent": bool(method.disable_notification),
                                  "reply_to": method.reply_parameters.message_id if method.reply_parameters else None,
                                  "edits": 0, "deleted": False}
            result = self._msg(method.chat_id, mid, method.text)
        elif name in ("EditMessageText", "EditMessageReplyMarkup"):
            msg = self.messages.get(method.message_id)
            if msg is None:
                return self._fail(bot, method, 400, "Bad Request: message to edit not found")
            text = getattr(method, "text", msg["text"])
            if text == msg["text"] and markup_json(method.reply_markup) == markup_json(msg["markup"]):
                return self._fail(bot, method, 400, "Bad Request: message is not modified")
            msg.update(text=text, markup=method.reply_markup)
            msg["edits"] += 1
            result = self._msg(method.chat_id, method.message_id, text)
        elif name == "DeleteMessage":
            self.messages[method.message_id]["deleted"] = True
        elif name == "AnswerCallbackQuery":
            self.answers.append(method.text)
        return self.check_response(bot, method, 200, json.dumps({"ok": True, "result": result})).result

    def owner_messages(self, since=0):
        return [(mid, m) for mid, m in self.messages.items() if m["chat"] == OWNER and mid > since and not m["deleted"]]

    def find(self, needle, since=0):
        return next((mid for mid, m in self.owner_messages(since) if needle in m["text"]), None)


# ---------- поддельный Google ----------


async def probe_file(path: Path) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    data = json.loads(out or b"{}")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-i", str(path), "-c", "copy", "-f", "null", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, demux_err = await proc.communicate()
    fmt = data.get("format") or {}
    return {
        "duration": float(fmt.get("duration") or 0),
        "start": float(fmt.get("start_time") or 0),
        "format": fmt.get("format_name"),
        "streams": sorted(s.get("codec_type") for s in data.get("streams") or []),
        "errors": (err + demux_err).decode(errors="replace").strip(),
    }


class FakeGoogle:
    """Behaviour выбирается по началу названия ролика: clean, blocked, rejected, locked, monitor_block."""

    def __init__(self, root: Path):
        self.root = root
        self.device_polls = 0
        self.behaviours: dict[str, str] = {}
        self.fail_plans: dict[str, dict] = {}  # подстрока названия -> {номер PUT: "503" | "drop_half"}
        self.crash: dict[str, int] = {}  # подстрока названия -> после какого PUT «уронить» приложение
        self.crash_event = asyncio.Event()
        self.sessions: dict[str, dict] = {}
        self.videos: dict[str, dict] = {}
        self.updates: list[dict] = []
        self.violations: list[str] = []
        self.issued = 0  # выдано refresh-токенов: RT-1, RT-2, …
        self.revoked: set[str] = set()
        self.revoke_calls: list[str] = []
        self.channel_title = "Тестовый канал"
        self.playlists: dict[str, dict] = {}
        self.playlist_seq = 0
        self.upload_limit = False  # канал исчерпал дневной лимит загрузок
        self.limit_refusals = 0

    def app(self):
        app = web.Application(client_max_size=64 * 1024**2)
        app.router.add_post("/device/code", self.device_code)
        app.router.add_post("/token", self.token)
        app.router.add_post("/revoke", self.revoke)
        app.router.add_get("/youtube/v3/channels", self.channels)
        app.router.add_get("/youtube/v3/videos", self.list_videos)
        app.router.add_put("/youtube/v3/videos", self.update_video)
        app.router.add_get("/youtube/v3/playlists", self.list_playlists)
        app.router.add_post("/youtube/v3/playlists", self.create_playlist)
        app.router.add_post("/youtube/v3/playlistItems", self.add_playlist_item)
        app.router.add_post("/upload/youtube/v3/videos", self.create)
        app.router.add_put("/upload/session/{sid}", self.put)
        return app

    def _pick(self, table: dict, title: str, default=None):
        return next((value for key, value in table.items() if title.startswith(key)), default)

    async def device_code(self, request):
        return web.json_response({"device_code": "DC", "user_code": "ABCD-EFGH",
                                  "verification_url": "https://www.google.com/device", "expires_in": 1800, "interval": 1})

    async def token(self, request):
        form = await request.post()
        if form.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code":
            self.device_polls += 1
            if self.device_polls < 2:
                return web.json_response({"error": "authorization_pending"}, status=428)
            self.issued += 1
            rt = f"RT-{self.issued}"
            return web.json_response({"access_token": f"AT-{rt}", "expires_in": 3599, "refresh_token": rt})
        rt = form.get("refresh_token")
        if rt in self.revoked:
            return web.json_response({"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
                                     status=400)
        return web.json_response({"access_token": f"AT-{rt}", "expires_in": 3599})

    async def revoke(self, request):
        token = (await request.post()).get("token")
        self.revoke_calls.append(token)
        if token in self.revoked:
            return web.json_response({"error": "invalid_token", "error_description": "Token expired or revoked"}, status=400)
        self.revoked.add(token)
        return web.Response(status=200)

    def _auth(self, request):
        """Ответ 401, если токен доступа выдан по отозванному refresh-токену, иначе None."""
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer AT-"):
            self.violations.append(f"без токена: {request.method} {request.path}")
            return None
        if header.removeprefix("Bearer AT-") in self.revoked:
            return web.json_response({"error": {"code": 401, "message": "Invalid Credentials",
                                                "errors": [{"reason": "authError"}]}}, status=401)
        return None

    async def channels(self, request):
        if (denied := self._auth(request)) is not None:
            return denied
        return web.json_response({"items": [{"id": "UCtest", "snippet": {"title": self.channel_title}}]})

    async def create(self, request):
        if (denied := self._auth(request)) is not None:
            return denied
        if self.upload_limit:
            self.limit_refusals += 1
            return web.json_response({"error": {"code": 400, "message": "The user has exceeded the number of videos "
                                                "they may upload.", "errors": [{"reason": "uploadLimitExceeded"}]}},
                                     status=400)
        total = int(request.headers["X-Upload-Content-Length"])
        meta = await request.json()
        st = meta.get("status", {})
        if st != {"privacyStatus": "private", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False}:
            self.violations.append(f"status при загрузке: {st}")
        title = meta["snippet"]["title"]
        sid = f"s{len(self.sessions) + 1}"
        path = self.root / f"{sid}.mp4"
        path.write_bytes(b"")
        self.sessions[sid] = {"total": total, "received": 0, "path": path, "meta": meta, "title": title,
                              "video": None, "puts": 0, "queries": 0,
                              "fail_plan": dict(self._pick(self.fail_plans, title, {})),
                              "crash_at": self._pick(self.crash, title)}
        return web.Response(status=200, headers={"Location": f"{BASE}/upload/session/{sid}"})

    def _range(self, s):
        return {"Range": f"bytes=0-{s['received'] - 1}"} if s["received"] else {}

    async def put(self, request):
        sid = request.match_info["sid"]
        s = self.sessions.get(sid)
        if s is None:
            return web.Response(status=404)
        if (denied := self._auth(request)) is not None:
            return denied
        content_range = request.headers.get("Content-Range", "")
        if not content_range.startswith("bytes */") and (
            "chunked" in request.headers.get("Transfer-Encoding", "").lower() or not request.headers.get("Content-Length")
        ):
            self.violations.append(f"кусок без Content-Length: {dict(request.headers)}")
        body = await request.read()
        if content_range.startswith("bytes */"):
            s["queries"] += 1
            if s["video"]:
                return web.json_response({"id": s["video"]})
            return web.Response(status=308, headers=self._range(s))
        m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
        first, last, total = map(int, m.groups())
        if total != s["total"] or last - first + 1 != len(body) or first != s["received"]:
            self.violations.append(f"{sid}: кусок {first}-{last}/{total}, принято {s['received']}, тело {len(body)}")
            return web.json_response({"error": {"code": 400, "message": "bad range", "errors": [{"reason": "badRange"}]}}, status=400)
        s["puts"] += 1
        action = s["fail_plan"].get(s["puts"])
        if action == "503":
            return web.json_response({"error": {"code": 503, "message": "Backend Error", "errors": [{"reason": "backendError"}]}}, status=503)
        keep = len(body) // 2 // 1024 * 1024 if action == "drop_half" else len(body)
        with s["path"].open("ab") as f:
            f.write(body[:keep])
        s["received"] += keep
        if action == "drop_half":
            request.transport.close()
            return web.Response(status=500)
        if s["crash_at"] and s["puts"] >= s["crash_at"]:
            s["crash_at"] = None
            self.crash_event.set()
        if s["received"] < s["total"]:
            return web.Response(status=308, headers=self._range(s))
        vid = f"vid{sid}"
        s["video"] = vid
        s["sha256"] = hashlib.sha256(s["path"].read_bytes()).hexdigest()
        s["probe"] = await probe_file(s["path"])
        self.videos[vid] = {"title": s["title"], "polls": 0, "privacy": "private", "published": False,
                            "behaviour": self._pick(self.behaviours, s["title"], "clean"),
                            "duration": s["probe"]["duration"]}
        return web.json_response({"id": vid, "status": {"uploadStatus": "uploaded", "privacyStatus": "private"}})

    def _item(self, vid):
        v = self.videos[vid]
        v["polls"] += 1
        processed = v["polls"] > 1
        status = {"uploadStatus": "processed" if processed else "uploaded", "privacyStatus": v["privacy"],
                  "embeddable": True, "license": "youtube", "publicStatsViewable": True}
        details = {"duration": f"PT{round(v['duration'])}S"}
        b = v["behaviour"]
        if processed and b == "rejected":
            status.update(uploadStatus="rejected", rejectionReason="claim")
        if processed and (b == "blocked" or (b == "monitor_block" and v["published"])):
            details["regionRestriction"] = {"blocked": ["DE", "US"]}
        return {"id": vid, "status": status, "contentDetails": details,
                "processingDetails": {"processingStatus": "succeeded" if processed else "processing"}}

    async def list_videos(self, request):
        if (denied := self._auth(request)) is not None:
            return denied
        if request.query.get("part") != "status,processingDetails,contentDetails":
            self.violations.append(f"videos.list part={request.query.get('part')}")
        ids = [i for i in request.query.get("id", "").split(",") if i]
        return web.json_response({"items": [self._item(i) for i in ids if i in self.videos]})

    async def update_video(self, request):
        if (denied := self._auth(request)) is not None:
            return denied
        body = await request.json()
        self.updates.append(body)
        v = self.videos.get(body.get("id"))
        st = body.get("status") or {}
        if request.query.get("part") != "status" or v is None or st.get("selfDeclaredMadeForKids") is not False:
            self.violations.append(f"videos.update {request.query} {body}")
            return web.json_response({"error": {"code": 400, "message": "bad update"}}, status=400)
        if v["behaviour"] != "locked":
            v["privacy"] = st["privacyStatus"]
            v["published"] = st["privacyStatus"] == "public"
        return web.json_response({"id": body["id"], "status": {**st, "privacyStatus": v["privacy"]}})

    async def list_playlists(self, request):
        if (denied := self._auth(request)) is not None:
            return denied
        if request.query.get("mine") != "true":
            self.violations.append(f"playlists.list {dict(request.query)}")
        return web.json_response({"items": [{"id": pid} for pid in self.playlists]})

    async def create_playlist(self, request):
        if (denied := self._auth(request)) is not None:
            return denied
        body = await request.json()
        snippet, status = body.get("snippet") or {}, body.get("status") or {}
        if request.query.get("part") != "snippet,status" or not snippet.get("title") or "<" in snippet["title"]:
            self.violations.append(f"playlists.insert {dict(request.query)} {body}")
        self.playlist_seq += 1
        pid = f"PL{self.playlist_seq}"
        self.playlists[pid] = {"title": snippet.get("title"), "privacy": status.get("privacyStatus"), "items": []}
        return web.json_response({"id": pid})

    async def add_playlist_item(self, request):
        if (denied := self._auth(request)) is not None:
            return denied
        snippet = (await request.json()).get("snippet") or {}
        playlist = self.playlists.get(snippet.get("playlistId"))
        video = (snippet.get("resourceId") or {}).get("videoId")
        if playlist is None:
            return web.json_response({"error": {"code": 404, "message": "Playlist not found",
                                                "errors": [{"reason": "playlistNotFound"}]}}, status=404)
        if video not in self.videos:
            return web.json_response({"error": {"code": 404, "message": "Video not found",
                                                "errors": [{"reason": "videoNotFound"}]}}, status=404)
        if video in playlist["items"]:
            self.violations.append(f"ролик {video} дважды добавлен в плейлист")
        playlist["items"].append(video)
        return web.json_response({"id": f"PLI-{video}"})

    def playlist(self, prefix):
        return next(((pid, p) for pid, p in self.playlists.items() if p["title"].startswith(prefix)), (None, None))


# ---------- подставной список VOD канала для слежения ----------


class FakeChannel:
    """Подставной Twitch для слежения: список VOD, данные VOD и название идущего стрима."""

    def __init__(self, watch_info: VodInfo, old_info: VodInfo):
        self.enabled = False
        self.live = True
        self.watch_info = watch_info
        self.old_info = old_info
        self.list_calls = 0
        self.title_calls = 0
        self.title = watch_info.title
        self.started = datetime.now(timezone.utc) - timedelta(seconds=60)

    async def list_vods(self, channel, limit=5):
        if channel != "zakvielchannel":
            return []  # у второго стримера эфиров нет
        self.list_calls += 1
        return [VOD_WATCH, VOD_OLD] if self.enabled else []

    async def info(self, vod_id):
        if vod_id == VOD_OLD:
            return self.old_info
        info = VodInfo(**{**self.watch_info.__dict__})
        info.is_live = self.live
        info.started_at = self.started
        if self.live:
            info.duration = int((datetime.now(timezone.utc) - self.started).total_seconds())
        return info

    async def client_id(self):
        return "test-client"

    async def live_states(self, http, logins, client):
        if "zakvielchannel" not in logins or client != "test-client":
            raise AssertionError((logins, client))
        self.title_calls += 1
        live = self.enabled and self.live
        state = LiveState(stream_id="stream-w", started_at=self.started, title=self.title) if live else None
        return {login: state if login == "zakvielchannel" else None for login in logins}


# ---------- приложение ----------

APPS = []
DISPATCHERS = []


class RecordingApp(entry.App):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        APPS.append(self)


class RecordingDispatcher(entry.Dispatcher):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        DISPATCHERS.append(self)


entry.App = RecordingApp
entry.Dispatcher = RecordingDispatcher


async def main():
    logging.basicConfig(filename=str(WORK / "app.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    (WORK / "google").mkdir(parents=True, exist_ok=True)
    google = FakeGoogle(WORK / "google")
    runner = web.AppRunner(google.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    # Настоящие видео VOD для слежения, но короткий эфир: Minecraft 8 минут и Just Chatting 7 минут
    real = await fetch_vod_info(VOD_WATCH)
    watch_info = VodInfo(id=VOD_WATCH, title="Майнкрафт Лайв в 20:00", duration=900, uploader=real.uploader,
                         uploader_login=real.uploader_login, started_at=None, is_live=True,
                         chapters=[{"start_time": 0, "end_time": 480, "title": "Minecraft"},
                                   {"start_time": 480, "end_time": 900, "title": "Just Chatting"}],
                         playlist_url=real.playlist_url)
    old_info = VodInfo(id=VOD_OLD, title="старый стрим", duration=3600, uploader=real.uploader,
                       uploader_login=real.uploader_login, started_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
                       is_live=False, chapters=[], playlist_url=real.playlist_url)
    channel = FakeChannel(watch_info, old_info)
    watcher_mod.list_channel_vods = channel.list_vods
    watcher_mod.fetch_vod_info = channel.info
    watcher_mod.live_states = channel.live_states
    watcher_mod.client_id = channel.client_id

    tg = FakeTelegram()
    entry.Bot = functools.partial(Bot, session=tg)
    engine = make_engine(WORK / "data" / "app.db")
    db = async_sessionmaker(engine, expire_on_commit=False)

    async def segs(vod_id):
        async with db() as s:
            return list((await s.scalars(select(Segment).where(Segment.vod_id == vod_id).order_by(Segment.idx))).all())

    async def seg(sid):
        async with db() as s:
            return await s.get(Segment, sid)

    async def all_sent(vod_id):
        rows = await segs(vod_id)
        return rows if rows and all(r.tg_message_id for r in rows) else None

    async def status_in(sid, statuses):
        row = await seg(sid)
        return row if row and row.status in statuses else None

    async def streamer_row(login="zakvielchannel"):
        async with db() as s:
            return await s.scalar(select(Streamer).where(Streamer.login == login))

    async def start_app():
        before = sum("Бот запущен" in m["text"] for _, m in tg.owner_messages())
        task = asyncio.create_task(entry.main())
        await wait_for(lambda: sum("Бот запущен" in m["text"] for _, m in tg.owner_messages()) > before or task.done(), 60, "запуск")
        if task.done():
            task.result()
        return task

    async def crash(task):
        """Загрузка и проверки обрываются на месте, как при падении; опрос Telegram останавливается штатно.

        Отмена задачи не останавливает внутренний цикл опроса aiogram, а в настоящем
        перезапуске процесс новый, поэтому бот останавливается через stop_polling.
        """
        background = list(APPS[-1].tasks)
        for t in background:
            t.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await DISPATCHERS[-1].stop_polling()
        await asyncio.wait_for(task, 30)
        entry.router._parent_router = None

    async def say(text, timeout=60):
        since = tg.next_message_id
        tg.push_text(OWNER, text)
        return await wait_for(lambda: tg.owner_messages(since), timeout, f"ответ на {text}")

    async def press(message_id, prefix, timeout=60):
        data = button_data(tg.messages[message_id]["markup"], prefix)
        count = len(tg.answers)
        tg.push_callback(OWNER, data, message_id)
        await wait_for(lambda: len(tg.answers) > count, timeout, f"кнопка {prefix}")
        await asyncio.sleep(0.5)
        return tg.answers[-1]

    t0 = time.monotonic()
    print("\n== 1. Запуск, YouTube, пауза", flush=True)
    # База от версии с одним стримером: пауза и начало слежения лежат в общих ключах
    (WORK / "data").mkdir(parents=True, exist_ok=True)
    await init_db(engine)
    legacy_since = datetime.now(timezone.utc)
    # Shorts этого стримера ждут после обработки минуту, а сегменты — ноль (PUBLISH_DELAY_MIN)
    overrides = json.dumps({"shorts_publish_delay_min": 1})
    async with db() as s, s.begin():
        s.add(Streamer(login="zakvielchannel", overrides=overrides))
        s.add_all([KV(key="watch_since", value=legacy_since.isoformat()), KV(key="paused", value="0")])
    task = await start_app()
    row = await streamer_row()
    async with db() as s:
        legacy = (await s.scalars(select(KV))).all()
    check(row.title_name == "Заквиель" and row.paused is False and as_utc(row.watch_since) == legacy_since
          and not legacy and row.overrides == overrides,
          "пауза, начало слежения и имя из STREAMER_NAME перенесены в запись стримера",
          (row.title_name, row.paused, row.watch_since, [r.key for r in legacy], row.overrides))
    reply = await say("/youtube")
    consent = reply[-1][0]
    text = tg.messages[consent]["text"]
    check("приватными роликами" in text and "плейлисты по категориям" in text and "Shorts" in text and "политикой" in text
          and "Условиями использования YouTube" in text and "/disconnect" in text and tg.find("ABCD-EFGH") is None,
          "до входа бот объясняет, что будет делать с каналом, и просит принять политику", text)
    await press(consent, "✅")
    mid = await wait_for(lambda: tg.find("ABCD-EFGH"), 30, "код")
    check(not tg.messages[consent]["markup"], "после согласия кнопки под условиями убраны")
    await wait_for(lambda: "Подключён канал" in tg.messages[mid]["text"], 30, "канал")
    check(True, "YouTube подключён по коду")
    # Канал будто подключили до появления плейлистов: согласие дано по старой версии политики
    async with db() as s, s.begin():
        await s.execute(update(Streamer).values(consent_version=1))
    reply = await say("/pause")
    check("остановлены" in reply[-1][1]["text"], "/pause")

    print("\n== 2. Стрим с сериалом: оба Just Chatting склеены в один ролик и пропущены, игра в очереди", flush=True)
    since = tg.next_message_id
    tg.push_text(OWNER, f"/process https://www.twitch.tv/videos/{VOD_SERIES}")
    s_rows = await wait_for(lambda: all_sent(VOD_SERIES), 120, "сегменты S")
    check([(r.category, r.status) for r in s_rows] == [("Just Chatting", Status.SKIPPED), ("Minecraft", Status.QUEUED)],
          "два Just Chatting стали одним сегментом и пропущены, Minecraft в очереди", [(r.category, r.status) for r in s_rows])
    header = tg.find("не загружаются: 1", since)
    check(header is not None, "в карточке стрима видно, что сегмент не загружается")
    s_jc, s_mc = s_rows
    check(len(get_spans(s_jc)) == 2 and s_jc.part is None, "склеенный сегмент состоит из двух отрезков, без «часть N»",
          get_spans(s_jc))
    m = tg.messages[s_jc.tg_message_id]
    check(", " in m["text"].splitlines()[0], "в сообщении видны оба отрезка", m["text"].splitlines()[0])
    check("⏭ Не загружен: похоже на просмотр сериала" in m["text"] and "⬆️ Всё равно загрузить" in buttons(m["markup"]),
          "пропущенный сегмент: причина и кнопка «Всё равно загрузить»", m["text"][-120:])
    check(all(tg.messages[r.tg_message_id]["silent"] for r in s_rows) and tg.messages[header]["silent"],
          "сообщения о новом стриме приходят без звука")

    print("\n== 3. Обычный стрим: оба сегмента в очередь", flush=True)
    tg.push_text(OWNER, f"/process https://www.twitch.tv/videos/{VOD_NORMAL}")
    n_rows = await wait_for(lambda: all_sent(VOD_NORMAL), 120, "сегменты N")
    check([r.status for r in n_rows] == [Status.QUEUED, Status.QUEUED], "JC и Minecraft в очереди")
    n_jc, n_mc = n_rows
    vod_n = None
    async with db() as s:
        vod_n = await s.get(Vod, VOD_NORMAL)
    check(vod_n.playlist_url and vod_n.playlist_url.endswith("index-dvr.m3u8"), "плейлист VOD сохранён")
    check(s_mc.title.endswith(" | Часть 1 | Заквиель") and n_mc.title.endswith(" | Часть 2 | Заквиель")
          and n_jc.title.endswith(" | Заквиель") and n_jc.part is None,
          "Minecraft нумеруется от стрима к стриму, Just Chatting — без номера", (s_mc.title, n_mc.title, n_jc.title))

    # Короткие отрезки вместо многочасовых, чтобы тест шёл минуты
    ranges = {s_mc.id: (1200, 1290), n_jc.id: (600, 690), n_mc.id: (3000, 3120)}
    async with db() as s, s.begin():
        for sid, (a, b) in ranges.items():
            await s.execute(update(Segment).where(Segment.id == sid).values(start=a, end=b))
        # склеенный ролик: по 30 секунд из начала стрима и из пятого часа
        await s.execute(update(Segment).where(Segment.id == s_jc.id).values(start=100, end=18030, ranges="[[100, 130], [18000, 18030]]"))
    google.behaviours = {"Minecraft | ВО ВСЕ": "rejected", "Just Chatting | ФРИКЛЕНД": "monitor_block",
                         "Minecraft | ФРИКЛЕНД": "blocked", "Just Chatting | ВО ВСЕ": "clean",
                         "Minecraft | Майнкрафт: строим": "locked", "Just Chatting | Майнкрафт: строим": "clean"}
    google.fail_plans = {"Minecraft | ФРИКЛЕНД": {1: "503", 3: "drop_half"}}
    google.crash = {"Minecraft | ФРИКЛЕНД": 5}

    print("\n== 4. «Всё равно загрузить» для пропущенного", flush=True)
    answer = await press(s_jc.tg_message_id, "⬆️")
    row = await seg(s_jc.id)
    check(row.status == Status.QUEUED and row.force_review, "сегмент в очереди с пометкой «решать вручную»", answer)

    print("\n== 5. /resume: загрузка потоком, сбои сети, перезапуск посреди ролика", flush=True)
    await say("/resume")
    await asyncio.wait_for(google.crash_event.wait(), 600)
    await crash(task)
    row = await seg(n_mc.id)
    sid_mc = next(k for k, v in google.sessions.items() if v["title"].startswith("Minecraft | ФРИКЛЕНД"))
    got = google.sessions[sid_mc]["received"]
    check(row.status == Status.UPLOADING and row.upload_uri and row.upload_total,
          f"«падение» посреди загрузки: принято {got / 1e6:.0f} из {row.upload_total / 1e6:.0f} МБ, сессия сохранена")
    sessions_before = len(google.sessions)
    # Согласие дано по старой версии политики: после перезапуска бот просит принять новую
    since = tg.next_message_id
    task = await start_app()
    policy = await wait_for(lambda: tg.find("Политика конфиденциальности AutoVOD обновлена", since), 30, "новая политика")
    check("плейлисты по категориям" in tg.messages[policy]["text"] and "Shorts" in tg.messages[policy]["text"]
          and buttons(tg.messages[policy]["markup"])[0] == "✅ Принимаю",
          "после обновления политики бот просит принять её заново", tg.messages[policy]["text"][:200])

    print("\n== 6. Итоги загрузок и проверок", flush=True)
    finals = (Status.PUBLISHED, Status.REVIEW, Status.REJECTED, Status.LOCKED, Status.FAILED, Status.PRIVATE)
    row = await wait_for(lambda: status_in(s_mc.id, finals), 300, "S.MC")
    check(row.status == Status.REJECTED and "Content ID" in (row.reason or ""), "отклонённый YouTube ролик → 🔴 и статус «отклонён»", (row.status, row.reason))
    check(await eventually(lambda: tg.find("YouTube отклонил ролик — претензия правообладателя")), "пришло 🔴 с причиной")

    row = await wait_for(lambda: status_in(n_jc.id, finals), 300, "N.JC")
    check(row.status == Status.PUBLISHED, "ролик без предупреждений опубликован сам", (row.status, row.error, get_warnings(row)))
    pub = tg.find(f"✅ Опубликовано: «{n_jc.title[:30]}")
    check(pub is not None and tg.messages[pub]["silent"], "сообщение о публикации без звука")
    await asyncio.sleep(2)
    check(not google.playlists, "пока новая политика не принята, плейлисты не создаются", google.playlists)
    await press(policy, "✅")
    n_jc_video = (await seg(n_jc.id)).youtube_id
    await wait_for(lambda: (google.playlist("Just Chatting")[1] or {}).get("items") == [n_jc_video], 30, "плейлист JC")
    _, jc_list = google.playlist("Just Chatting")
    check(jc_list["title"] == "Just Chatting | Заквиель" and jc_list["privacy"] == "public",
          "после согласия опубликованный ролик попал в новый плейлист своей категории", jc_list)

    row = await wait_for(lambda: status_in(n_mc.id, finals), 600, "N.MC после перезапуска")
    check(row.status == Status.REVIEW and any("заблокирован в 2 странах" in w for w in get_warnings(row)),
          "блокировка в странах → ролик ждёт решения", (row.status, get_warnings(row), row.error))
    mc_sessions = [v for v in google.sessions.values() if v["title"].startswith("Minecraft | ФРИКЛЕНД")]
    check(len(mc_sessions) == 1 and mc_sessions[0]["queries"] >= 3,
          f"после сбоев и перезапуска загрузка продолжена в той же сессии (сессий до перезапуска: {sessions_before})")
    decision = await eventually(lambda: tg.find(f"⚠️ «{n_mc.title[:30]}"))
    check(decision is not None and not tg.messages[decision]["silent"], "запрос решения приходит со звуком")
    check(buttons(tg.messages[n_mc.tg_message_id]["markup"])[:2] == ["✅ Опубликовать", "🔒 Оставить приватным"],
          "под сегментом кнопки решения")

    row = await wait_for(lambda: status_in(s_jc.id, finals), 300, "S.JC")
    check(row.status == Status.REVIEW and any("вручную" in w for w in get_warnings(row)),
          "загруженный вручную сегмент тоже ждёт решения", get_warnings(row))

    print("\n== 7. Решения кнопками", flush=True)
    answer = await press(n_mc.tg_message_id, "✅")
    row = await wait_for(lambda: status_in(n_mc.id, (Status.PUBLISHED,)), 30, "публикация N.MC")
    check(answer == "Публикую…" and row.status == Status.PUBLISHED, "«Опубликовать» публикует ролик")
    answer = await press(s_jc.tg_message_id, "🔒")
    row = await seg(s_jc.id)
    check(row.status == Status.PRIVATE and answer == "Оставлен приватным", "«Оставить приватным»")
    check("✅ Всё-таки опубликовать" in buttons(tg.messages[s_jc.tg_message_id]["markup"]), "передумать можно")

    print("\n== 8. Наблюдение после публикации", flush=True)
    alert = await wait_for(lambda: tg.find(f"🔴 С опубликованным роликом «{n_jc.title[:30]}"), 60, "тревога после публикации")
    check("заблокирован в 2 странах" in tg.messages[alert]["text"], "YouTube заблокировал ролик после публикации → 🔴")
    await asyncio.sleep(8)
    alerts = [mid for mid, m in tg.owner_messages() if m["text"].startswith(f"🔴 С опубликованным роликом «{n_jc.title[:30]}")]
    check(len(alerts) == 1, "о той же проблеме бот сообщает один раз", len(alerts))

    print("\n== 9. Слежение за каналом и сменами названия", flush=True)
    await say("/pause")
    calls, title_calls = channel.list_calls, channel.title_calls
    channel.enabled = True
    await wait_for(lambda: channel.list_calls >= calls + 2, 30, "опрос канала")
    await wait_for(lambda: channel.title_calls >= title_calls + 2, 30, "опрос названия")
    titles = ["Майнкрафт Лайв в 20:00", "Майнкрафт: строим базу", "Смотрим Во все тяжкие"]
    for title in titles[1:]:
        before = channel.title_calls
        channel.title = title
        await wait_for(lambda: channel.title_calls >= before + 3, 30, f"название «{title}» замечено")
    async with db() as s:
        changes = list((await s.scalars(select(TitleChange).order_by(TitleChange.at))).all())
    check([c.title for c in changes] == titles, "каждая смена названия записана один раз", [c.title for c in changes])
    reply = await say("/status")
    text = reply[-1][1]["text"]
    check("идёт стрим" in text and "Название стрима сейчас: «Смотрим Во все тяжкие»" in text,
          "/status: идёт стрим и его текущее название", text)
    async with db() as s:
        check(await s.get(Vod, VOD_WATCH) is None and await s.get(Vod, VOD_OLD) is None,
              "пока стрим идёт, VOD не трогается; старый VOD игнорируется")
    # В настоящем эфире названия сменились бы на 3:20 и 11:00: переносим записанные времена на эту шкалу
    channel.started = datetime.now(timezone.utc) - timedelta(seconds=901)
    async with db() as s, s.begin():
        for change, offset in zip(changes, (20, 200, 660)):
            await s.execute(update(TitleChange).where(TitleChange.id == change.id).values(
                at=channel.started + timedelta(seconds=offset), stream_started_at=channel.started))
    channel.live = False
    w_rows = await wait_for(lambda: all_sent(VOD_WATCH), 60, "VOD после конца стрима")
    got = [(r.category, r.stream_title, r.start, r.end, r.status) for r in w_rows]
    check(got == [
        ("Minecraft", titles[0], 0, 200, Status.SKIPPED),
        ("Minecraft", titles[1], 200, 480, Status.QUEUED),
        ("Just Chatting", titles[1], 480, 660, Status.SKIPPED),
        ("Just Chatting", titles[2], 660, 900, Status.SKIPPED),
    ], "VOD разрезан и по категориям, и по сменам названия", got)
    reasons = [r.reason or "" for r in w_rows]
    check(reasons[0] == reasons[2] == "короче 4 мин" and "во все тяжкие" in reasons[3],
          "короткие куски и сериал не загружаются, каждый со своей причиной", reasons)
    check([r.title for r in w_rows] == [
        "Minecraft | Майнкрафт Лайв в 20:00 | Заквиель",
        "Minecraft | Майнкрафт: строим базу | Часть 3 | Заквиель",
        "Just Chatting | Майнкрафт: строим базу | Заквиель",
        "Just Chatting | Смотрим Во все тяжкие | Заквиель",
    ], "третий загружаемый ролик Minecraft — «Часть 3»; пропущенный кусок и Just Chatting без номера",
          [r.title for r in w_rows])
    w_mc0, w_mc, w_jc = w_rows[0], w_rows[1], w_rows[2]
    await press(w_mc0.tg_message_id, "⬆️")
    row = await seg(w_mc0.id)
    check(row.status == Status.QUEUED and row.title == "Minecraft | Майнкрафт Лайв в 20:00 | Часть 4 | Заквиель",
          "пропущенный кусок, загруженный по кнопке, получает следующий номер части", (row.status, row.title))
    answer = await press(w_jc.tg_message_id, "⬆️")
    row = await seg(w_jc.id)
    check(row.status == Status.QUEUED and not row.force_review and answer == "Загружу",
          "короткий кусок по кнопке загружается без лишнего решения", (row.status, row.force_review, answer))
    async with db() as s, s.begin():
        await s.execute(update(Segment).where(Segment.id == w_mc.id).values(end=w_mc.start + 60))
        await s.execute(update(Segment).where(Segment.id == w_jc.id).values(end=w_jc.start + 60))
        await s.execute(update(Segment).where(Segment.id == w_mc0.id).values(end=w_mc0.start + 60))
    await say("/resume")
    row = await wait_for(lambda: status_in(w_mc.id, finals), 300, "W.MC")
    check(row.status == Status.LOCKED, "проект без аудита: YouTube не дал опубликовать → 🔒", (row.status, row.reason))
    check(await eventually(lambda: tg.find("🔒 «Minecraft | Майнкрафт: строим")), "пришло объяснение про аудит")
    row = await wait_for(lambda: status_in(w_jc.id, finals), 300, "W.JC")
    check(row.status == Status.PUBLISHED, "короткий кусок, загруженный по кнопке, опубликован сам", (row.status, get_warnings(row)))
    meta = next(v["meta"] for v in google.sessions.values() if v["title"] == row.title)
    check(meta["snippet"]["description"].startswith("Майнкрафт: строим базу"), "в описании название этого отрезка")
    row = await wait_for(lambda: status_in(w_mc0.id, finals), 300, "W.MC0")
    check(row.status == Status.PUBLISHED, "ролик «Часть 4» опубликован", (row.status, get_warnings(row)))
    async with db() as s:
        check(await s.get(Vod, VOD_OLD) is None, "VOD, закончившийся до начала слежения, так и не тронут")
    video = {sid: (await seg(sid)).youtube_id for sid in (n_jc.id, n_mc.id, w_jc.id, w_mc0.id, w_mc.id)}
    await wait_for(lambda: (google.playlist("Minecraft")[1] or {}).get("items") == [video[n_mc.id], video[w_mc0.id]]
                   and (google.playlist("Just Chatting")[1] or {}).get("items") == [video[n_jc.id], video[w_jc.id]],
                   30, "плейлисты")
    check(sorted((p["title"], p["privacy"]) for p in google.playlists.values())
          == [("Just Chatting | Заквиель", "public"), ("Minecraft | Заквиель", "public")],
          "по плейлисту на категорию; опубликованные ролики идут по порядку, отклонённые и закрытые в них не попали",
          google.playlists)

    print("\n== 10. Файлы на «YouTube»", flush=True)
    by_title = {v["title"]: v for v in google.sessions.values() if v.get("video")}
    for rid in (s_mc.id, n_jc.id, n_mc.id, s_jc.id, w_mc.id, w_jc.id):
        row = await seg(rid)
        s = next(v for v in by_title.values() if v["title"] == row.title)
        p = s["probe"]
        ok = (p["streams"] == ["audio", "video"] and abs(p["start"]) < 0.1 and not p["errors"]
              and abs(p["duration"] - row.expected_duration) < 2 and s["received"] == row.upload_total)
        check(ok, f"{row.title[:40]}…: {s['received'] / 1e6:.0f} МБ, {p['duration']:.1f} с, начало {p['start']:.2f} с",
              p)
    joined_title = (await seg(s_jc.id)).title
    joined = next(v for v in google.sessions.values() if v["title"] == joined_title)
    dts = []
    for sel in ("v:0", "a:0"):
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", sel, "-show_entries", "packet=dts_time", "-of", "csv=p=0",
            str(joined["path"]), stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        dts.append([float(x) for x in out.split()])
    steps = [b - a for track in dts for a, b in zip(track, track[1:])]
    check(abs(joined["probe"]["duration"] - 60) < 1 and min(steps) > 0 and max(steps) < 0.05,
          f"склеенный из двух отрезков ролик идёт без скачков времени ({joined['probe']['duration']:.2f} с, "
          f"наибольший шаг {max(steps) * 1000:.0f} мс)", (min(steps), max(steps)))
    # Ролик, загрузка которого пережила сбои и перезапуск, байт в байт совпадает с заново собранным потоком
    row = await seg(n_mc.id)
    async with db() as s:
        vod = await s.get(Vod, VOD_NORMAL)
    async with httpx.AsyncClient(timeout=60) as http:
        plan = await hls.build_plan(http, vod.playlist_url, get_spans(row))
        digest = hashlib.sha256()
        async for piece in hls.stream(http, plan):
            digest.update(piece)
    check(digest.hexdigest() == google.sessions[sid_mc]["sha256"],
          "ролик после 503, обрыва и перезапуска совпадает с эталоном байт в байт")
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-i", str(google.sessions[sid_mc]["path"]),
                                                "-f", "null", "-", stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    check(not err.strip(), "полное декодирование этого ролика без ошибок", err[:300])

    print("\n== 10б. Shorts из популярных клипов", flush=True)

    async def shorts():
        async with db() as s:
            return list((await s.scalars(select(Segment).where(Segment.kind == SHORT).order_by(Segment.idx))).all())

    async def shorts_sent(count):
        rows = await shorts()
        return rows if len(rows) >= count and all(r.tg_message_id for r in rows) else None

    CLIPS[:] = [
        clip_node("160495036", "MildObeseFlyKappaWealth-izDh89-5XKM_Na2S", "ААААААА ЖЕНЩИИНАА", 2037, 7,
                  "Baldur's Gate 3", "2886356064", 5051),
        clip_node("dup", "SameMomentClip", "тот же момент", 300, 10, "Baldur's Gate 3", "2886356064", 5053),
        clip_node("series", "SeriesClip", "смотрим вместе", 500, 20, "Just Chatting", VOD_SERIES, 110),
        clip_node("349032338", "AuspiciousPhilanthropicDragonflyThisIsSparta-mC4F6DJoEO3koY8K", "123123", 400, 15,
                  "Portal 2"),
        clip_node("141133624", "WealthyCooperativeTofuYee-5YHUOP3GWT0BPnk1", "куб компаньён", 153, 14, "Portal 2",
                  "2885498731", 14305),
        clip_node("low", "LowViewsClip", "мало просмотров", 50, 10, "Portal 2", "2885498731", 100),
    ]
    google.upload_limit = True  # канал исчерпал дневной лимит загрузок: Shorts сначала упрутся в него
    since = tg.next_message_id
    APPS[-1].shorts_wake.set()
    rows = await wait_for(lambda: shorts_sent(3), 60, "Shorts из клипов")
    # Первый Shorts очередь может взять в загрузку раньше, чем тест прочитает статус
    check([r.clip_id for r in rows] == ["160495036", "series", "349032338"]
          and [r.status == Status.SKIPPED for r in rows] == [False, True, False],
          "клипы от 100 просмотров по убыванию; повтор момента, сериал, слабый клип и клип сверх 2 в сутки не берутся",
          [(r.clip_id, r.status, r.reason) for r in rows])
    short_a, short_series, short_f = rows
    check(short_a.title == "ААААААА ЖЕНЩИИНАА | Заквиель" and short_f.title == "Portal 2 | Заквиель",
          "название Shorts — название клипа, а бессмысленное заменено категорией", (short_a.title, short_f.title))
    check("во все тяжкие" in (short_series.reason or "")
          and "⬆️ Всё равно загрузить" in buttons(tg.messages[short_series.tg_message_id]["markup"]),
          "клип из просмотра сериала пропущен, загрузить его можно кнопкой", short_series.reason)
    m = tg.messages[short_a.tg_message_id]
    check("🎬 <b>Shorts</b>" in m["text"] and "просмотров на Twitch: 2037" in m["text"] and m["silent"]
          and buttons(m["markup"])[0] == "▶️ Twitch", "в Telegram карточка Shorts без звука", m["text"])

    notice = await wait_for(lambda: tg.find("дневной лимит канала", since), 120, "лимит канала")
    await asyncio.sleep(3)  # бот не должен повторять попытки, пока ждёт
    streamer = await streamer_row()
    queued = [(await seg(short_a.id)).status, (await seg(short_f.id)).status]
    check(tg.messages[notice]["silent"] and google.limit_refusals == 1 and not streamer.paused
          and streamer.uploads_wait_until is not None and queued == [Status.QUEUED, Status.QUEUED],
          "лимит канала: Shorts ждут в очереди, бот не на паузе, пишет без звука и не повторяет попытку сразу",
          (google.limit_refusals, streamer.paused, streamer.uploads_wait_until, queued))
    reply = await say("/status")
    check("⏳ YouTube не принимает новые ролики — исчерпан дневной лимит канала" in reply[-1][1]["text"],
          "в /status видно, что загрузки ждут лимита", reply[-1][1]["text"])
    google.upload_limit = False
    async with db() as s, s.begin():  # время ожидания вышло
        await s.execute(update(Streamer).values(uploads_wait_until=datetime.now(timezone.utc)))
    APPS[-1].wake.set()
    resumed = await wait_for(lambda: tg.find("снова принимает загрузки", since), 120, "загрузки после лимита")
    check(tg.messages[resumed]["silent"] and (await streamer_row()).uploads_wait_until is None,
          "когда время ожидания вышло, загрузка продолжилась сама, без /resume")

    waiting = await wait_for(lambda: status_in(short_a.id, (Status.WAITING,)), 300, "Shorts A обработан")
    left = (as_utc(waiting.publish_after) - datetime.now(timezone.utc)).total_seconds()
    check(0 < left <= 60, f"Shorts ждёт публикации свою минуту (настройка стримера), осталось {left:.0f} с", left)
    row = await wait_for(lambda: status_in(short_a.id, finals), 300, "Shorts A")
    row_f = await wait_for(lambda: status_in(short_f.id, finals), 300, "Shorts F")
    check(row.status == Status.PUBLISHED and row_f.status == Status.PUBLISHED, "Shorts опубликованы сами",
          (row.status, row.error, row_f.status, row_f.error))
    for r in (row, row_f):
        session = next(v for v in google.sessions.values() if v["title"] == r.title)
        dims, probe = await ffprobe_dims(session["path"]), session["probe"]
        check(dims == (1080, 1920) and probe["streams"] == ["audio", "video"] and not probe["errors"]
              and abs(probe["duration"] - r.expected_duration) < 1.5,
              f"{r.title}: вертикальный ролик {dims[0]}×{dims[1]}, {probe['duration']:.1f} с", (dims, probe))
    meta = next(v["meta"] for v in google.sessions.values() if v["title"] == row.title)
    description = meta["snippet"]["description"]
    check("Клип на Twitch: https://clips.twitch.tv/MildObeseFlyKappaWealth-izDh89-5XKM_Na2S" in description
          and "#shorts" in description and "shorts" in meta["snippet"]["tags"],
          "в описании ссылка на клип и #shorts", meta["snippet"])
    await wait_for(lambda: (google.playlist("Shorts")[1] or {}).get("items") == [row.youtube_id, row_f.youtube_id],
                   30, "плейлист Shorts")
    check(google.playlist("Shorts")[1]["title"] == "Shorts | Заквиель", "Shorts попали в свой плейлист «Shorts | Заквиель»")
    APPS[-1].shorts_wake.set()
    await asyncio.sleep(3)
    check(len(await shorts()) == 3, "повторная проверка не берёт те же клипы и соблюдает лимит на сутки")
    check(not list((WORK / "data" / "shorts").glob("*.mp4")), "после загрузки файлы Shorts удалены")

    print("\n== 10в. Второй стример: своё имя, свои настройки и своя пауза", flush=True)
    second_vod = "9990000001"
    async with db() as s, s.begin():
        s.add(Streamer(login="secondstreamer", display_name="SecondStreamer", title_name="Второй",
                       overrides=json.dumps({"no_part_categories": "Just Chatting,Minecraft"})))
    real_fetch = bot_mod.fetch_vod_info

    async def fetch_second(vod_id):
        if vod_id != second_vod:
            return await real_fetch(vod_id)
        return VodInfo(id=second_vod, title="Второй стрим", duration=3600, uploader="SecondStreamer",
                       uploader_login="secondstreamer", started_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
                       is_live=False, chapters=[{"start_time": 0, "end_time": 1800, "title": "Minecraft"},
                                                {"start_time": 1800, "end_time": 3600, "title": "Dota 2"}])

    bot_mod.fetch_vod_info = fetch_second
    since = tg.next_message_id
    tg.push_text(OWNER, f"/process https://www.twitch.tv/videos/{second_vod}")
    rows = await wait_for(lambda: all_sent(second_vod), 60, "сегменты второго стримера")
    check([r.title for r in rows] == ["Minecraft | Второй стрим | Второй", "Dota 2 | Второй стрим | Часть 1 | Второй"],
          "у второго стримера своё имя, свои категории без номера и своя нумерация частей", [r.title for r in rows])
    notice = await wait_for(lambda: tg.find("YouTube-канал не подключён", since), 60, "второй стример без канала")
    second, first = await streamer_row("secondstreamer"), await streamer_row()
    check(second.paused and not first.paused and [r.status for r in await segs(second_vod)] == [Status.QUEUED] * 2
          and tg.messages[notice]["reply_to"] == rows[0].tg_message_id,
          "у второго стримера нет YouTube-канала: на паузе только он, Заквиель работает дальше",
          (second.paused, first.paused))
    check(await eventually(lambda: "стрима нет" in APPS[-1].watch_states.get(second.id, ""))
          and (await streamer_row("secondstreamer")).watch_since is not None,
          "за каналом второго стримера тоже следят, со своего момента", APPS[-1].watch_states)

    print("\n== 11. Ежедневная сверка, отключение канала и отзыв доступа", flush=True)

    async def run_daily_check():
        async with db() as s, s.begin():
            await s.execute(update(Streamer).values(youtube_checked_at=None))

    uploaded = [s_mc.id, n_jc.id, n_mc.id, s_jc.id, w_mc.id, w_jc.id]
    google.videos.pop((await seg(s_mc.id)).youtube_id)  # отклонённый ролик удалили в YouTube Studio
    google.videos[(await seg(w_mc.id)).youtube_id]["privacy"] = "public"  # а заблокированный открыли там вручную
    google.channel_title = "Канал после переименования"
    jc_pid, _ = google.playlist("Just Chatting")
    google.playlists.pop(jc_pid)  # владелец удалил плейлист в Studio
    await run_daily_check()
    row = await wait_for(lambda: status_in(s_mc.id, (Status.FORGOTTEN,)), 30, "сверка роликов")
    check(row.youtube_id is None and "больше нет на YouTube" in (row.reason or ""),
          "ролик, удалённый с YouTube, забыт при ежедневной сверке", (row.youtube_id, row.reason))
    check("🗑" in tg.messages[s_mc.tg_message_id]["text"], "в сообщении сегмента видно, что данные удалены")
    row = await wait_for(lambda: status_in(w_mc.id, (Status.PUBLISHED,)), 30, "доступ, изменённый в Studio")
    check(row.youtube_id, "ролик, открытый вручную в YouTube Studio, отмечен опубликованным")
    check(all([(await seg(i)).youtube_id for i in uploaded if i != s_mc.id]), "остальные ролики на месте")
    check((await streamer_row()).youtube_channel_title == "Канал после переименования", "название канала обновлено")
    async with db() as s:
        stored = sorted(row.category for row in (await s.scalars(select(Playlist))).all())
    check(stored == ["minecraft", "shorts"], "плейлист, удалённый на YouTube, забыт при сверке", stored)
    await wait_for(lambda: (google.playlist("Minecraft")[1] or {}).get("items", [])[-1:] == [video[w_mc.id]], 30,
                   "W.MC в плейлисте")
    check(True, "ролик, открытый в Studio, тоже попал в плейлист своей категории")

    reply = await say("/disconnect")
    confirm = reply[-1][0]
    await press(confirm, "Отмена")
    check(not tg.messages[confirm]["markup"] and (await streamer_row()).youtube_token, "«Отмена» ничего не отключает")
    reply = await say("/disconnect")
    confirm = reply[-1][0]
    check("останутся на YouTube" in tg.messages[confirm]["text"], "/disconnect предупреждает, что ролики на YouTube останутся")
    await press(confirm, "🔌")
    await wait_for(lambda: "отключён" in tg.messages[confirm]["text"], 30, "отключение")
    streamer = await streamer_row()
    rows = [await seg(i) for i in uploaded]
    check(google.revoke_calls == ["RT-1"], "доступ отозван в Google", google.revoke_calls)
    check(streamer.youtube_token is None and streamer.youtube_channel_id is None and streamer.youtube_channel_title is None,
          "токен и данные канала удалены")
    async with db() as s:
        leftovers = (await s.scalars(select(Segment).where(
            (Segment.youtube_id.is_not(None)) | (Segment.upload_uri.is_not(None))))).all()
    check(not leftovers and all(r.status == Status.FORGOTTEN for r in rows),
          "ID роликов и сессий загрузки удалены", [(r.status, r.youtube_id) for r in rows])
    check(streamer.paused, "после отключения обработка на паузе")
    async with db() as s:
        left = (await s.scalars(select(Playlist))).all()
    check(not left and streamer.consent_version is None and streamer.consent_prompted is None,
          "ID плейлистов и согласие тоже удалены", (left, streamer.consent_version, streamer.consent_prompted))
    check(buttons(tg.messages[s_jc.tg_message_id]["markup"]) == ["▶️ Twitch"],
          "под роликом, ждавшим решения, остались только ссылки", buttons(tg.messages[s_jc.tg_message_id]["markup"]))

    since = tg.next_message_id
    reply = await say("/youtube")
    await press(reply[-1][0], "✅")
    mid = await wait_for(lambda: tg.find("Подключён канал", since), 30, "повторное подключение")
    check("/resume" in tg.messages[mid]["text"], "после подключения бот напоминает, что обработка на паузе")
    google.revoked.add("RT-2")  # владелец отозвал доступ на странице настроек Google
    await run_daily_check()
    alert = await wait_for(lambda: tg.find("Доступ к YouTube отозван", since), 30, "отзыв доступа замечен")
    check(not tg.messages[alert]["silent"] and (await streamer_row()).youtube_token is None,
          "отзыв доступа в Google замечен при сверке: данные удалены, пришло 🔴")
    check(google.revoke_calls == ["RT-1"], "отозванный в Google токен бот повторно не отзывает", google.revoke_calls)

    print("\n== 12. Протоколы и итог", flush=True)
    reply = await say("/status")
    print("   /status:", reply[-1][1]["text"].replace("\n", " | "))
    check(not tg.problems, "все тексты валидны для Telegram", tg.problems[:5])
    check(not google.violations, "запросы к Google соответствуют протоколу", google.violations[:5])
    check(set(tg.calls) <= FakeTelegram.KNOWN, "бот не вызывает неожиданных методов Telegram")
    check(all(u["status"].get("containsSyntheticMedia") is False for u in google.updates), "при публикации status полный")
    await crash(task)
    leftovers = [p.name for p in (WORK / "data").iterdir()
                 if p.suffix not in (".db", ".db-wal", ".db-shm") and not (p.is_dir() and not any(p.iterdir()))]
    check(not leftovers, "на диске ничего, кроме базы", leftovers)
    print(f"   пиковая память процесса: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} МБ")

    print("\n== Что увидел бы владелец (итоговое состояние):", flush=True)
    for mid, m in tg.owner_messages():
        print(f"  [{mid}]{' 🔕' if m['silent'] else ''} " + m["text"].replace("\n", "\n        "))
        if m["markup"]:
            print("        кнопки: " + " | ".join(buttons(m["markup"])))

    await engine.dispose()
    await runner.cleanup()
    for path in (WORK / "google").glob("*.mp4"):
        path.unlink()
    failed = [name for ok, name in RESULTS if not ok]
    print(f"\nИТОГО: {len(RESULTS) - len(failed)} из {len(RESULTS)} проверок пройдено за {time.monotonic() - t0:.0f} с")
    for name in failed:
        print("  не пройдено:", name)


if __name__ == "__main__":
    asyncio.run(main())
