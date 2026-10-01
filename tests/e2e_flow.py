"""Сквозной тест: слежение за каналом, фильтр, потоковая загрузка, проверки, автопубликация.

Настоящие: код приложения (app.__main__.main), Twitch (главы, плейлисты и фрагменты VOD), yt-dlp, SQLite.
Поддельные: Telegram Bot API и Google (OAuth, upload, videos.list, videos.update).
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
)

import app.__main__ as entry  # noqa: E402
import app.checker as checker_mod  # noqa: E402
import app.hls as hls  # noqa: E402
import app.service as service_mod  # noqa: E402
import app.watcher as watcher_mod  # noqa: E402
import app.worker as worker_mod  # noqa: E402
import app.youtube as yt  # noqa: E402
import httpx  # noqa: E402
from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.db import Segment, Status, Vod, get_warnings, make_engine  # noqa: E402
from app.tools import VodInfo, fetch_vod_info  # noqa: E402

yt.DEVICE_CODE_URL = f"{BASE}/device/code"
yt.TOKEN_URL = f"{BASE}/token"
yt.UPLOAD_URL = f"{BASE}/upload/youtube/v3/videos"
yt.API_URL = f"{BASE}/youtube/v3"
# ускоряем таймеры: в жизни это минуты и часы
checker_mod.TICK_SEC = 1
checker_mod.RECHECK = timedelta(seconds=1)
checker_mod.MONITOR_EVERY = service_mod.MONITOR_EVERY = timedelta(seconds=3)
worker_mod.FIRST_CHECK_AFTER = timedelta(seconds=1)

RESULTS: list[tuple[bool, str]] = []


def check(ok, name, detail=""):
    RESULTS.append((bool(ok), name))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok or not detail else f"  <-- {detail}"), flush=True)


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
        self.updates.put_nowait({"update_id": n, "callback_query": {
            "id": f"cb{n}", "from": self._user(uid), "chat_instance": "ci", "data": data,
            "message": {"message_id": message_id, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
                        "text": self.messages.get(message_id, {}).get("text", "")}}})

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

    def app(self):
        app = web.Application(client_max_size=64 * 1024**2)
        app.router.add_post("/device/code", self.device_code)
        app.router.add_post("/token", self.token)
        app.router.add_get("/youtube/v3/channels", self.channels)
        app.router.add_get("/youtube/v3/videos", self.list_videos)
        app.router.add_put("/youtube/v3/videos", self.update_video)
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
            return web.json_response({"access_token": "AT-1", "expires_in": 3599, "refresh_token": "RT-1"})
        return web.json_response({"access_token": "AT-2", "expires_in": 3599})

    def _auth(self, request):
        if not request.headers.get("Authorization", "").startswith("Bearer AT-"):
            self.violations.append(f"без токена: {request.method} {request.path}")

    async def channels(self, request):
        self._auth(request)
        return web.json_response({"items": [{"id": "UCtest", "snippet": {"title": "Тестовый канал"}}]})

    async def create(self, request):
        self._auth(request)
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
        self._auth(request)
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
        self._auth(request)
        if request.query.get("part") != "status,processingDetails,contentDetails":
            self.violations.append(f"videos.list part={request.query.get('part')}")
        ids = [i for i in request.query.get("id", "").split(",") if i]
        return web.json_response({"items": [self._item(i) for i in ids if i in self.videos]})

    async def update_video(self, request):
        self._auth(request)
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


# ---------- подставной список VOD канала для слежения ----------


class FakeChannel:
    def __init__(self, watch_info: VodInfo, old_info: VodInfo):
        self.enabled = False
        self.live = True
        self.watch_info = watch_info
        self.old_info = old_info
        self.list_calls = 0

    async def list_vods(self, channel, limit=5):
        if channel != "zakvielchannel":
            raise AssertionError(channel)
        self.list_calls += 1
        return [VOD_WATCH, VOD_OLD] if self.enabled else []

    async def info(self, vod_id):
        if vod_id == VOD_OLD:
            return self.old_info
        info = VodInfo(**{**self.watch_info.__dict__})
        info.is_live = self.live
        if self.live:
            info.started_at = datetime.now(timezone.utc) - timedelta(seconds=100)
            info.duration = 100
        else:
            info.started_at = datetime.now(timezone.utc) - timedelta(seconds=info.duration + 1)
        return info


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

    # Настоящие данные VOD для слежения, но короткие: 2 главы по 2 минуты
    real = await fetch_vod_info(VOD_WATCH)
    watch_info = VodInfo(id=VOD_WATCH, title="Майнкрафт Лайв в 20:00", duration=240, uploader=real.uploader,
                         uploader_login=real.uploader_login, started_at=None, is_live=True,
                         chapters=[{"start_time": 0, "end_time": 120, "title": "Minecraft"},
                                   {"start_time": 120, "end_time": 240, "title": "Just Chatting"}],
                         playlist_url=real.playlist_url)
    old_info = VodInfo(id=VOD_OLD, title="старый стрим", duration=3600, uploader=real.uploader,
                       uploader_login=real.uploader_login, started_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
                       is_live=False, chapters=[], playlist_url=real.playlist_url)
    channel = FakeChannel(watch_info, old_info)
    watcher_mod.list_channel_vods = channel.list_vods
    watcher_mod.fetch_vod_info = channel.info

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
    task = await start_app()
    tg.push_text(OWNER, "/youtube")
    mid = await wait_for(lambda: tg.find("ABCD-EFGH"), 30, "код")
    await wait_for(lambda: "Подключён канал" in tg.messages[mid]["text"], 30, "канал")
    check(True, "YouTube подключён по коду")
    reply = await say("/pause")
    check("остановлены" in reply[-1][1]["text"], "/pause")

    print("\n== 2. Стрим с сериалом: Just Chatting пропускается, игра идёт в очередь", flush=True)
    since = tg.next_message_id
    tg.push_text(OWNER, f"/process https://www.twitch.tv/videos/{VOD_SERIES}")
    s_rows = await wait_for(lambda: all_sent(VOD_SERIES), 120, "сегменты S")
    check([r.status for r in s_rows] == [Status.SKIPPED, Status.QUEUED, Status.SKIPPED],
          "JC пропущены, Minecraft в очереди", [(r.category, r.status) for r in s_rows])
    header = tg.find("не загружаются: 2", since)
    check(header is not None, "в карточке стрима видно, что 2 сегмента не загружаются")
    s_jc1, s_mc, s_jc2 = s_rows
    m = tg.messages[s_jc1.tg_message_id]
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

    # Короткие отрезки вместо многочасовых, чтобы тест шёл минуты
    ranges = {s_mc.id: (1200, 1290), n_jc.id: (600, 690), n_mc.id: (3000, 3120), s_jc2.id: (6000, 6060)}
    async with db() as s, s.begin():
        for sid, (a, b) in ranges.items():
            await s.execute(update(Segment).where(Segment.id == sid).values(start=a, end=b))
    google.behaviours = {"Minecraft — ВО ВСЕ": "rejected", "Just Chatting — ФРИКЛЕНД": "monitor_block",
                         "Minecraft — ФРИКЛЕНД": "blocked", "Just Chatting (часть 2)": "clean",
                         "Minecraft — Майнкрафт": "locked", "Just Chatting — Майнкрафт": "clean"}
    google.fail_plans = {"Minecraft — ФРИКЛЕНД": {1: "503", 3: "drop_half"}}
    google.crash = {"Minecraft — ФРИКЛЕНД": 5}

    print("\n== 4. «Всё равно загрузить» для пропущенного", flush=True)
    answer = await press(s_jc2.tg_message_id, "⬆️")
    row = await seg(s_jc2.id)
    check(row.status == Status.QUEUED and row.force_review, "сегмент в очереди с пометкой «решать вручную»", answer)

    print("\n== 5. /resume: загрузка потоком, сбои сети, перезапуск посреди ролика", flush=True)
    await say("/resume")
    await asyncio.wait_for(google.crash_event.wait(), 600)
    await crash(task)
    row = await seg(n_mc.id)
    sid_mc = next(k for k, v in google.sessions.items() if v["title"].startswith("Minecraft — ФРИКЛЕНД"))
    got = google.sessions[sid_mc]["received"]
    check(row.status == Status.UPLOADING and row.upload_uri and row.upload_total,
          f"«падение» посреди загрузки: принято {got / 1e6:.0f} из {row.upload_total / 1e6:.0f} МБ, сессия сохранена")
    sessions_before = len(google.sessions)
    task = await start_app()

    print("\n== 6. Итоги загрузок и проверок", flush=True)
    finals = (Status.PUBLISHED, Status.REVIEW, Status.REJECTED, Status.LOCKED, Status.FAILED, Status.PRIVATE)
    row = await wait_for(lambda: status_in(s_mc.id, finals), 300, "S.MC")
    check(row.status == Status.REJECTED and "Content ID" in (row.reason or ""), "отклонённый YouTube ролик → 🔴 и статус «отклонён»", (row.status, row.reason))
    check(tg.find("YouTube отклонил ролик — претензия правообладателя") is not None, "пришло 🔴 с причиной")

    row = await wait_for(lambda: status_in(n_jc.id, finals), 300, "N.JC")
    check(row.status == Status.PUBLISHED, "ролик без предупреждений опубликован сам", (row.status, row.error, get_warnings(row)))
    pub = tg.find(f"✅ Опубликовано: «{n_jc.title[:30]}")
    check(pub is not None and tg.messages[pub]["silent"], "сообщение о публикации без звука")

    row = await wait_for(lambda: status_in(n_mc.id, finals), 600, "N.MC после перезапуска")
    check(row.status == Status.REVIEW and any("заблокирован в 2 странах" in w for w in get_warnings(row)),
          "блокировка в странах → ролик ждёт решения", (row.status, get_warnings(row), row.error))
    mc_sessions = [v for v in google.sessions.values() if v["title"].startswith("Minecraft — ФРИКЛЕНД")]
    check(len(mc_sessions) == 1 and mc_sessions[0]["queries"] >= 3,
          f"после сбоев и перезапуска загрузка продолжена в той же сессии (сессий до перезапуска: {sessions_before})")
    decision = tg.find(f"⚠️ «{n_mc.title[:30]}")
    check(decision is not None and not tg.messages[decision]["silent"], "запрос решения приходит со звуком")
    check(buttons(tg.messages[n_mc.tg_message_id]["markup"])[:2] == ["✅ Опубликовать", "🔒 Оставить приватным"],
          "под сегментом кнопки решения")

    row = await wait_for(lambda: status_in(s_jc2.id, finals), 300, "S.JC2")
    check(row.status == Status.REVIEW and any("вручную" in w for w in get_warnings(row)),
          "загруженный вручную сегмент тоже ждёт решения", get_warnings(row))

    print("\n== 7. Решения кнопками", flush=True)
    answer = await press(n_mc.tg_message_id, "✅")
    row = await wait_for(lambda: status_in(n_mc.id, (Status.PUBLISHED,)), 30, "публикация N.MC")
    check(answer == "Публикую…" and row.status == Status.PUBLISHED, "«Опубликовать» публикует ролик")
    answer = await press(s_jc2.tg_message_id, "🔒")
    row = await seg(s_jc2.id)
    check(row.status == Status.PRIVATE and answer == "Оставлен приватным", "«Оставить приватным»")
    check("✅ Всё-таки опубликовать" in buttons(tg.messages[s_jc2.tg_message_id]["markup"]), "передумать можно")

    print("\n== 8. Наблюдение после публикации", flush=True)
    alert = await wait_for(lambda: tg.find(f"🔴 С опубликованным роликом «{n_jc.title[:30]}"), 60, "тревога после публикации")
    check("заблокирован в 2 странах" in tg.messages[alert]["text"], "YouTube заблокировал ролик после публикации → 🔴")
    await asyncio.sleep(8)
    alerts = [mid for mid, m in tg.owner_messages() if m["text"].startswith(f"🔴 С опубликованным роликом «{n_jc.title[:30]}")]
    check(len(alerts) == 1, "о той же проблеме бот сообщает один раз", len(alerts))

    print("\n== 9. Слежение за каналом", flush=True)
    calls = channel.list_calls
    channel.enabled = True
    await wait_for(lambda: channel.list_calls >= calls + 2, 30, "опрос канала")
    reply = await say("/status")
    check("идёт стрим" in reply[-1][1]["text"], "/status: идёт стрим", reply[-1][1]["text"])
    async with db() as s:
        check(await s.get(Vod, VOD_WATCH) is None and await s.get(Vod, VOD_OLD) is None,
              "пока стрим идёт, VOD не трогается; старый VOD игнорируется")
    channel.live = False
    w_rows = await wait_for(lambda: all_sent(VOD_WATCH), 60, "VOD после конца стрима")
    check(len(w_rows) == 2 and not any(r.status == Status.SKIPPED for r in w_rows),
          "после конца стрима VOD ушёл в обработку сам", [(r.category, r.status) for r in w_rows])
    w_mc, w_jc = w_rows
    row = await wait_for(lambda: status_in(w_mc.id, finals), 300, "W.MC")
    check(row.status == Status.LOCKED, "проект без аудита: YouTube не дал опубликовать → 🔒", (row.status, row.reason))
    check(tg.find("🔒 «Minecraft — Майнкрафт") is not None, "пришло объяснение про аудит")
    row = await wait_for(lambda: status_in(w_jc.id, finals), 300, "W.JC")
    check(row.status == Status.PUBLISHED, "второй сегмент опубликован сам")
    async with db() as s:
        check(await s.get(Vod, VOD_OLD) is None, "VOD, закончившийся до начала слежения, так и не тронут")

    print("\n== 10. Файлы на «YouTube»", flush=True)
    by_title = {v["title"]: v for v in google.sessions.values() if v.get("video")}
    for rid in (s_mc.id, n_jc.id, n_mc.id, s_jc2.id, w_mc.id, w_jc.id):
        row = await seg(rid)
        s = next(v for v in by_title.values() if v["title"] == row.title)
        p = s["probe"]
        ok = (p["streams"] == ["audio", "video"] and abs(p["start"]) < 0.1 and not p["errors"]
              and abs(p["duration"] - row.expected_duration) < 2 and s["received"] == row.upload_total)
        check(ok, f"{row.title[:40]}…: {s['received'] / 1e6:.0f} МБ, {p['duration']:.1f} с, начало {p['start']:.2f} с",
              p)
    # Ролик, загрузка которого пережила сбои и перезапуск, байт в байт совпадает с заново собранным потоком
    row = await seg(n_mc.id)
    async with db() as s:
        vod = await s.get(Vod, VOD_NORMAL)
    async with httpx.AsyncClient(timeout=60) as http:
        plan = await hls.build_plan(http, vod.playlist_url, row.start, row.end)
        digest = hashlib.sha256()
        async for piece in hls.stream(http, plan):
            digest.update(piece)
    check(digest.hexdigest() == google.sessions[sid_mc]["sha256"],
          "ролик после 503, обрыва и перезапуска совпадает с эталоном байт в байт")
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-i", str(google.sessions[sid_mc]["path"]),
                                                "-f", "null", "-", stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    check(not err.strip(), "полное декодирование этого ролика без ошибок", err[:300])

    print("\n== 11. Протоколы и итог", flush=True)
    reply = await say("/status")
    print("   /status:", reply[-1][1]["text"].replace("\n", " | "))
    check(not tg.problems, "все тексты валидны для Telegram", tg.problems[:5])
    check(not google.violations, "запросы к Google соответствуют протоколу", google.violations[:5])
    check(set(tg.calls) <= FakeTelegram.KNOWN, "бот не вызывает неожиданных методов Telegram")
    check(all(u["status"].get("containsSyntheticMedia") is False for u in google.updates), "при публикации status полный")
    await crash(task)
    leftovers = [p.name for p in (WORK / "data").iterdir() if p.suffix not in (".db", ".db-wal", ".db-shm")]
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
