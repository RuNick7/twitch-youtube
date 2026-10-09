"""Сквозной тест нескольких стримеров. Интернет и ffmpeg не нужны.

Настоящие: код приложения (app.__main__.main), aiogram, SQLAlchemy, SQLite.
Поддельные: Telegram и Google (из e2e_flow.py), Twitch и само видео — вместо VOD идут байты нужного размера.

Проверяет управление стримерами из бота: /add с отметкой о разрешении, /name, /set, /remove, /streamers,
выбор стримера в /youtube, /disconnect, /pause и /resume, имя и хештег стримера в сообщениях, а ещё то,
что пауза, лимиты YouTube, Shorts и слежение у каждого стримера свои.

Запуск описан в README («Тесты»).
"""

import asyncio
import functools
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for folder in (HERE, HERE.parent):  # e2e_flow лежит рядом, пакет app — рядом или уровнем выше
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import e2e_flow as base  # noqa: E402  (выставляет окружение, подменяет адреса Google и ускоряет таймеры)

WORK = Path(os.environ.get("E2E_WORK") or tempfile.mkdtemp(prefix="e2e-multi-"))
os.environ.update(
    TWITCH_CHANNEL="alpha_channel",
    STREAMER_NAME="Альфа",
    DATA_DIR=str(WORK / "data"),
    UPLOAD_CHUNK_MB="1",
    PUBLISH_DELAY_MIN="0",
    SHORTS_PUBLISH_DELAY_MIN="0",
    WATCH_INTERVAL_SEC="1",
    WATCH_GRACE_MIN="0",
    TITLE_POLL_SEC="1",
    SKIP_SHORTER_MIN="0",
    MONITOR_DAYS="0",
    NO_PART_CATEGORIES="Just Chatting",
    SHORTS_PER_DAY="2",
)

import app.bot as bot_mod  # noqa: E402
import app.shorts as shorts_mod  # noqa: E402
import app.watcher as watcher_mod  # noqa: E402
import app.worker as worker_mod  # noqa: E402
from aiogram import Bot  # noqa: E402
from aiohttp import web  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.db import KV, SHORT, Segment, Status, Streamer, TitleChange, Vod, as_utc, clips_vod_id, make_engine  # noqa: E402
from app.tools import ToolError, VodInfo  # noqa: E402
from app.twitch import LiveState, TwitchError  # noqa: E402

entry = base.entry
check, wait_for, eventually, buttons, button_data = base.check, base.wait_for, base.eventually, base.buttons, base.button_data
OWNER = base.OWNER
SECRET = os.environ["SECRET_KEY"]
OLD_SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)
VIDEO_BYTES = 300 * 1024
ALPHA, BETA = "Альфа #alpha_channel", "Бета #beta_channel"  # имя и хештег стримера в сообщениях


# ---------- поддельные Twitch и видео ----------


class FakeTwitch:
    def __init__(self):
        self.names = {"alpha_channel": "AlphaTV", "beta_channel": "BetaTV"}
        self.vods: dict[str, VodInfo] = {}
        self.lists: dict[str, list[str]] = {}  # канал -> ID его VOD, новые первыми
        self.list_calls: dict[str, int] = {}
        self.broken: set[str] = set()  # каналы, список VOD которых не открывается
        self.live: dict[str, LiveState] = {}
        self.clips: dict[str, list[dict]] = {}

    def add_vod(self, vod_id, login, title, chapters, started_at=None):
        """Законченный эфир. Без started_at он закончился пять секунд назад — уже после начала слежения."""
        duration = chapters[-1][1]
        self.vods[vod_id] = VodInfo(
            id=vod_id,
            title=title,
            duration=duration,
            uploader=self.names.get(login, login),
            uploader_login=login,
            started_at=started_at or datetime.now(timezone.utc) - timedelta(seconds=duration + 5),
            is_live=False,
            chapters=[{"start_time": a, "end_time": b, "title": name} for a, b, name in chapters],
            playlist_url="https://twitch.invalid/vod.m3u8",
        )

    async def display_name(self, http, channel, client):
        if channel not in self.names:
            raise TwitchError(f"канала {channel} нет на Twitch")
        return self.names[channel]

    async def client_id(self):
        return "test-client"

    async def list_vods(self, channel, limit=5):
        self.list_calls[channel] = self.list_calls.get(channel, 0) + 1
        if channel in self.broken:
            raise RuntimeError("Twitch не отвечает")
        return list(self.lists.get(channel, []))

    async def info(self, vod_id):
        if vod_id not in self.vods:
            raise ToolError("Video does not exist")
        return VodInfo(**self.vods[vod_id].__dict__)

    async def live_states(self, http, logins, client):
        return {login: self.live.get(login) for login in logins}

    async def popular_clips(self, http, channel, client):
        return list(self.clips.get(channel, []))


class FakePlan:
    """«Ролик» из байтов: в первых восьми записана длительность, её читает поддельный ffprobe."""

    def __init__(self, spans):
        self.duration = float(sum(end - start for start, end in spans))
        head = f"{round(self.duration):08d}".encode()
        self.data = head + b"v" * (VIDEO_BYTES - len(head))
        self.total = len(self.data)


async def fake_build_plan(http, playlist_url, spans):
    return FakePlan(spans)


async def fake_stream(http, plan, offset=0):
    for start in range(offset, plan.total, 64 * 1024):
        yield plan.data[start : start + 64 * 1024]


async def fake_probe(path):
    return {"duration": float(path.read_bytes()[:8]), "start": 0.0, "format": "mp4",
            "streams": ["audio", "video"], "errors": ""}


async def fake_prepare_short(app, seg):
    path = shorts_mod.shorts_dir(app) / f"{seg.id}.mp4"
    path.write_bytes(f"{seg.expected_duration or 1:08d}".encode() + b"s" * 4096)
    return path


class Google(base.FakeGoogle):
    """У каждого выданного токена свой канал: RT-1 — UC1 «Канал 1». channel_of сводит два токена на один канал."""

    def __init__(self, root):
        super().__init__(root)
        self.channel_of: dict[str, str] = {}  # номер токена -> номер канала
        self.titles: dict[str, str] = {}  # номер канала -> название
        self.uploaders: dict[str, str] = {}  # название ролика -> refresh-токен, с которым он загружен
        self.upload_limited: set[str] = set()  # токены каналов, упёршихся в дневной лимит загрузок
        self.quota_exceeded = False  # кончилась квота всего Google-проекта

    def _refused(self, reason):
        return web.json_response(
            {"error": {"code": 403, "message": f"refused: {reason}", "errors": [{"reason": reason}]}}, status=403
        )

    async def channels(self, request):
        if (denied := self._auth(request)) is not None:
            return denied
        number = request.headers["Authorization"].removeprefix("Bearer AT-RT-")
        channel = self.channel_of.get(number, number)
        title = self.titles.get(channel, f"Канал {channel}")
        return web.json_response({"items": [{"id": f"UC{channel}", "snippet": {"title": title}}]})

    async def create(self, request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer AT-")
        if self.quota_exceeded:
            return self._refused("quotaExceeded")
        if token in self.upload_limited:
            return self._refused("uploadLimitExceeded")
        response = await super().create(request)
        if response.status == 200:
            self.uploaders[self.sessions[f"s{len(self.sessions)}"]["title"]] = token
        return response


def make_old_database(path: Path) -> None:
    """База, какой её оставила версия с одним стримером: без новых колонок, с общими ключами в kv."""
    path.parent.mkdir(parents=True, exist_ok=True)
    token = Fernet(SECRET.encode()).encrypt(b"RT-0").decode()
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE streamers (
            id INTEGER NOT NULL PRIMARY KEY, login VARCHAR(64) NOT NULL UNIQUE, display_name VARCHAR(128),
            youtube_channel_id VARCHAR(64), youtube_channel_title VARCHAR(256), youtube_token TEXT);
        CREATE TABLE kv (key VARCHAR(64) NOT NULL PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    con.execute(
        "INSERT INTO streamers (login, display_name, youtube_channel_id, youtube_channel_title, youtube_token) "
        "VALUES (?, ?, ?, ?, ?)",
        ("alpha_channel", "AlphaTV", "UC0", "Старый канал", token),
    )
    # Строка осталась от прежнего значения TWITCH_CHANNEL: следить за этим каналом бот не должен
    con.execute("INSERT INTO streamers (login, display_name) VALUES (?, ?)", ("old_test_channel", "OldTest"))
    con.executemany(
        "INSERT INTO kv VALUES (?, ?)",
        [("watch_since", OLD_SINCE.isoformat()), ("consent_version", "3"), ("consent_prompted", "3"), ("paused", "0")],
    )
    con.commit()
    con.close()


async def main():
    (WORK / "google").mkdir(parents=True, exist_ok=True)
    google = Google(WORK / "google")
    google.titles["0"] = "Канал Альфы"
    runner = web.AppRunner(google.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", base.PORT).start()

    twitch = FakeTwitch()
    watcher_mod.list_channel_vods = twitch.list_vods
    watcher_mod.fetch_vod_info = twitch.info
    watcher_mod.live_states = twitch.live_states
    watcher_mod.client_id = twitch.client_id
    bot_mod.fetch_vod_info = twitch.info
    bot_mod.channel_display_name = twitch.display_name
    bot_mod.client_id = twitch.client_id
    shorts_mod.popular_clips = twitch.popular_clips
    shorts_mod.client_id = twitch.client_id
    worker_mod.build_plan = fake_build_plan
    worker_mod.stream = fake_stream
    worker_mod.fetch_vod_info = twitch.info
    worker_mod.prepare_short = fake_prepare_short
    base.probe_file = fake_probe

    tg = base.FakeTelegram()
    entry.Bot = functools.partial(Bot, session=tg)
    db_path = WORK / "data" / "app.db"
    make_old_database(db_path)
    engine = make_engine(db_path)
    db = async_sessionmaker(engine, expire_on_commit=False)
    vault = Fernet(SECRET.encode())

    async def streamer(login):
        async with db() as s:
            return await s.scalar(select(Streamer).where(Streamer.login == login))

    async def segs(vod_id):
        async with db() as s:
            return list((await s.scalars(select(Segment).where(Segment.vod_id == vod_id).order_by(Segment.idx))).all())

    async def all_sent(vod_id):
        rows = await segs(vod_id)
        return rows if rows and all(r.tg_message_id for r in rows) else None

    async def all_in(vod_id, statuses):
        rows = await segs(vod_id)
        return rows if rows and all(r.status in statuses for r in rows) else None

    async def settled(*vod_ids):
        """Наблюдение за опубликованными роликами закончилось: проверка YouTube их больше не трогает."""
        for vod_id in vod_ids:
            if any(r.check_at is not None for r in await segs(vod_id)):
                return False
        return True

    async def waits_over():
        """Время ожидания лимита YouTube вышло: так в жизни проходят часы."""
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        async with db() as s, s.begin():
            await s.execute(update(Streamer).where(Streamer.uploads_wait_until.is_not(None)).values(uploads_wait_until=past))
        app().wake.set()

    def app():
        return base.APPS[-1]

    async def start_app():
        before = sum("Бот запущен" in m["text"] for _, m in tg.owner_messages())
        task = asyncio.create_task(entry.main())
        await wait_for(lambda: sum("Бот запущен" in m["text"] for _, m in tg.owner_messages()) > before or task.done(),
                       60, "запуск")
        if task.done():
            task.result()
        return task

    async def stop_app(task):
        background = list(app().tasks)
        for t in background:
            t.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await base.DISPATCHERS[-1].stop_polling()
        await asyncio.wait_for(task, 30)
        entry.router._parent_router = None

    async def ask(text, needle, timeout=30):
        """Пишет боту и ждёт сообщение с needle: между делом бот шлёт и другие сообщения."""
        since = tg.next_message_id
        tg.push_text(OWNER, text)
        mid = await wait_for(lambda: tg.find(needle, since), timeout, f"«{needle}» в ответ на {text}")
        return mid, tg.messages[mid]["text"]

    async def press(message_id, prefix, timeout=30):
        data = button_data(tg.messages[message_id]["markup"], prefix)
        count = len(tg.answers)
        tg.push_callback(OWNER, data, message_id)
        await wait_for(lambda: len(tg.answers) > count, timeout, f"кнопка {prefix}")
        await asyncio.sleep(0.3)
        return tg.answers[-1]

    async def old_button(data, message_id):
        """Нажатие кнопки, какой она была под сообщениями до появления нескольких стримеров."""
        count = len(tg.answers)
        tg.push_callback(OWNER, data, message_id)
        await wait_for(lambda: len(tg.answers) > count, 30, f"старая кнопка {data}")
        await asyncio.sleep(0.3)
        return tg.answers[-1]

    async def add(command):
        """/add и подтверждение разрешения стримера. Возвращает итоговый текст сообщения."""
        question, _ = await ask(command, "Добавить стримера")
        await press(question, "✅")
        await wait_for(lambda: "Добавить стримера" not in tg.messages[question]["text"], 30, "стример добавлен")
        return tg.messages[question]["text"]

    async def connect(command):
        """/youtube <стример>, согласие и вход. Возвращает текст сообщения о подключении."""
        consent, _ = await ask(command, "Подключение YouTube-канала")
        since = tg.next_message_id
        await press(consent, "✅")
        mid = await wait_for(lambda: tg.find("Подключён канал", since), 30, "канал подключён")
        return tg.messages[mid]["text"]

    def block(text, tag):
        """Часть /status про одного стримера."""
        return next((part for part in text.split("\n\n") if part.startswith(f"<b>{tag}</b>")), "")

    def process(vod_id):
        tg.push_text(OWNER, f"/process https://www.twitch.tv/videos/{vod_id}")

    done = (Status.PUBLISHED,)
    t0 = time.monotonic()

    print("\n== 0. Обновление с версии, где стример был один", flush=True)
    task = await start_app()
    alpha, old = await streamer("alpha_channel"), await streamer("old_test_channel")
    check(alpha.watch_since and as_utc(alpha.watch_since) == OLD_SINCE and alpha.consent_version == 3
          and alpha.consent_prompted == 3 and alpha.paused is False and alpha.removed_at is None,
          "пауза, начало слежения и согласие перенесены из общих ключей в запись стримера",
          (alpha.watch_since, alpha.consent_version, alpha.consent_prompted, alpha.paused))
    check(alpha.title_name == "Альфа", "имя для названий взято из STREAMER_NAME", alpha.title_name)
    check(old.removed_at is not None, "канал, оставшийся от прежних настроек, не попал под слежение")
    check(tg.find("Политика конфиденциальности AutoVOD обновлена") is None and not app().several,
          "согласие действует: заново бот его не просит")
    check(await eventually(lambda: _title_is(streamer, "alpha_channel", "Канал Альфы"), 20),
          "ежедневная сверка работает с перенесённым токеном: название канала обновлено")
    _, text = await ask("/status", "yt-dlp")
    check("<b>" not in text and "Twitch: alpha_channel" in text and "YouTube: «Канал Альфы»" in text
          and "old_test_channel" not in text and "Обработка: ▶️ работает" in text,
          "/status с одним стримером выглядит как раньше", text)
    consent, text = await ask("/youtube", "Подключение YouTube-канала")
    check("сегменты стримов AlphaTV" in text and buttons(tg.messages[consent]["markup"]) == ["✅ Принимаю, подключить", "Отмена"],
          "пока стример один, /youtube не спрашивает, чей канал", text[:200])
    answer = await press(consent, "Отмена")
    check(answer == "Отменено" and not tg.messages[consent]["markup"], "«Отмена» убирает кнопки")

    print("\n== 1. /add с отметкой о разрешении, /name, /streamers", flush=True)
    _, text = await ask("/add", "Укажите канал на Twitch")
    check("/add zakvielchannel Заквиель" in text, "/add без канала объясняет, что написать")
    _, text = await ask("/add nosuch_channel", "Не получилось")
    check("канала nosuch_channel нет на Twitch" in text and await streamer("nosuch_channel") is None,
          "несуществующий канал не добавляется", text)
    question, text = await ask("/add https://www.twitch.tv/Beta_Channel Бета", "Добавить стримера")
    check("BetaTV (twitch.tv/beta_channel)" in text and "разрешение стримера" in text
          and buttons(tg.messages[question]["markup"]) == ["✅ Разрешение есть, добавить", "Отмена"]
          and await streamer("beta_channel") is None,
          "/add сначала спрашивает о разрешении стримера и до ответа никого не добавляет", text)
    answer = await press(question, "Отмена")
    check(answer == "Отменено" and await streamer("beta_channel") is None and not tg.messages[question]["markup"],
          "«Отмена» — стример не добавлен")
    before = datetime.now(timezone.utc)
    text = await add("/add https://www.twitch.tv/Beta_Channel Бета")
    beta = await streamer("beta_channel")
    check("Добавлен стример BetaTV (twitch.tv/beta_channel)" in text and "Разрешение стримера отмечено" in text
          and "«Бета»" in text and "/youtube beta_channel" in text,
          "после подтверждения: имя канала с Twitch, имя для названий и подсказка про YouTube", text)
    check(beta is not None and beta.title_name == "Бета" and beta.display_name == "BetaTV" and beta.youtube_token is None
          and beta.permitted_at is not None and as_utc(beta.watch_since) >= before - timedelta(seconds=5)
          and app().several,
          "стример в базе с отметкой о разрешении, слежение начинается с этой минуты",
          beta and (beta.title_name, beta.permitted_at, beta.watch_since))
    await ask("/add beta_channel", "уже добавлен")
    check(True, "повторный /add ничего не ломает")
    text = await add("/add alpha_channel")
    alpha = await streamer("alpha_channel")
    check("Разрешение стримера AlphaTV (twitch.tv/alpha_channel) отмечено" in text and alpha.permitted_at is not None
          and as_utc(alpha.watch_since) == OLD_SINCE,
          "стримеру из .env разрешение отмечается тем же /add, слежение за ним не сбрасывается", text)
    _, text = await ask("/name beta_channel Бета Тест", "в названиях новых роликов")
    check((await streamer("beta_channel")).title_name == "Бета Тест" and "BetaTV" in text, "/name меняет имя", text)
    await ask("/name beta_channel Бета", "в названиях новых роликов")
    _, text = await ask("/name", "/name &lt;стример&gt;")
    check("Альфа — twitch.tv/alpha_channel" in text and "Бета — twitch.tv/beta_channel" in text,
          "/name без имени показывает стримеров", text)
    _, text = await ask("/streamers", "twitch.tv/beta_channel")
    check(f"<b>{ALPHA}</b>" in text and f"<b>{BETA}</b>" in text and "YouTube: «Канал Альфы»" in text
          and "YouTube: не подключён" in text and text.count("Разрешение стримера отмечено") == 2
          and "old_test_channel" not in text,
          "/streamers: каналы, YouTube и отметки о разрешении", text)

    print("\n== 2. У каждого стримера своя очередь и своя пауза", flush=True)
    twitch.add_vod("9000001", "beta_channel", "Бета играет", [(0, 1800, "Minecraft"), (1800, 3600, "Just Chatting")])
    twitch.add_vod("9000002", "alpha_channel", "Альфа стримит", [(0, 1800, "Minecraft"), (1800, 3600, "Just Chatting")])
    twitch.add_vod("9000099", "gamma_channel", "Чужой стрим", [(0, 1800, "Minecraft")])
    since = tg.next_message_id
    process("9000001")
    b_rows = await wait_for(lambda: all_sent("9000001"), 30, "сегменты Беты")
    check(tg.find("📺 <b>BetaTV #beta_channel</b>", since) is not None, "в карточке стрима хештег стримера")
    check([r.title for r in b_rows] == ["Minecraft | Бета играет | Часть 1 | Бета", "Just Chatting | Бета играет | Бета"],
          "в названиях роликов имя своего стримера", [r.title for r in b_rows])
    check(BETA in tg.messages[b_rows[0].tg_message_id]["text"].splitlines()[1],
          "в сообщении сегмента имя и хештег стримера", tg.messages[b_rows[0].tg_message_id]["text"])
    notice = await wait_for(lambda: tg.find("YouTube-канал не подключён", since), 30, "у Беты нет канала")
    text = tg.messages[notice]["text"]
    check(text.startswith(f"🔴 {BETA} · Обработка на паузе") and "/youtube beta_channel, затем /resume beta_channel" in text
          and not tg.messages[notice]["silent"],
          "нет YouTube-канала: бот называет стримера и команды с его логином", text)
    process("9000002")
    a_rows = await wait_for(lambda: all_in("9000002", done), 60, "ролики Альфы опубликованы")
    check([r.title for r in a_rows] == ["Minecraft | Альфа стримит | Часть 1 | Альфа", "Just Chatting | Альфа стримит | Альфа"],
          "номера частей у каждого стримера свои: у Альфы Minecraft тоже «Часть 1»", [r.title for r in a_rows])
    check(all(google.uploaders.get(r.title) == "RT-0" for r in a_rows), "ролики Альфы загружены с токеном её канала",
          google.uploaders)
    check((await streamer("beta_channel")).paused and not (await streamer("alpha_channel")).paused
          and [r.status for r in await segs("9000001")] == [Status.QUEUED, Status.QUEUED],
          "на паузе только Бета, её ролики ждут; Альфа загружает и публикует")
    _, text = await ask("/process https://www.twitch.tv/videos/9000099", "Не получилось")
    check("такого стримера в боте нет. Добавить: /add gamma_channel" in text, "VOD чужого канала не обрабатывается", text)
    await wait_for(lambda: {"Minecraft | Альфа", "Just Chatting | Альфа"} <= {p["title"] for p in google.playlists.values()},
                   30, "плейлисты Альфы")
    check(True, "плейлисты названы по имени стримера")

    print("\n== 3. /youtube и /resume, когда стримеров несколько", flush=True)
    picker, text = await ask("/youtube", "Для какого стримера")
    check(buttons(tg.messages[picker]["markup"]) == ["Альфа", "Бета"], "бот спрашивает, чей канал подключать",
          buttons(tg.messages[picker]["markup"]))
    since = tg.next_message_id
    await press(picker, "Бета")
    consent = await wait_for(lambda: tg.find("Подключение YouTube-канала", since), 30, "условия для Беты")
    check("сегменты стримов BetaTV" in tg.messages[consent]["text"] and not tg.messages[picker]["markup"],
          "после выбора — условия подключения для этого стримера")
    since = tg.next_message_id
    await press(consent, "✅")
    connected = await wait_for(lambda: tg.find("Подключён канал", since), 30, "канал Беты")
    text = tg.messages[connected]["text"]
    check("«Канал 1» для стримера BetaTV" in text and "Обработка на паузе: /resume beta_channel — продолжить" in text,
          "канал подключён именно Бете, бот напоминает про её паузу", text)
    alpha, beta = await streamer("alpha_channel"), await streamer("beta_channel")
    check(vault.decrypt(beta.youtube_token.encode()) == b"RT-1" and beta.youtube_channel_id == "UC1"
          and beta.consent_version == 3 and vault.decrypt(alpha.youtube_token.encode()) == b"RT-0"
          and alpha.youtube_channel_id == "UC0",
          "токен и согласие записаны Бете, канал Альфы не тронут")
    _, text = await ask("/resume beta_channel", "обработка продолжена")
    b_rows = await wait_for(lambda: all_in("9000001", done), 60, "ролики Беты опубликованы")
    check("BetaTV" in text and all(google.uploaders.get(r.title) == "RT-1" for r in b_rows),
          "/resume стримера снимает его паузу, ролики ушли на его канал", google.uploaders)
    await wait_for(lambda: {"Minecraft | Бета", "Just Chatting | Бета"} <= {p["title"] for p in google.playlists.values()},
                   30, "плейлисты Беты")
    consent, text = await ask("/youtube бета", "Подключение YouTube-канала")
    check("сегменты стримов BetaTV" in text, "стримера можно назвать по имени")
    await press(consent, "Отмена")
    _, text = await ask("/youtube nobody", "Не нашёл такого стримера")
    check("Альфа — twitch.tv/alpha_channel" in text, "неизвестный стример: бот показывает, кто есть", text)

    print("\n== 4. Свои настройки стримера: /set", flush=True)
    _, text = await ask("/set", "Свои настройки стримера")
    check("Бета — twitch.tv/beta_channel" in text, "/set без стримера объясняет, что написать")
    _, text = await ask("/set beta_channel", "Настройки стримера BetaTV")
    check("○ shorts_per_day = 2" in text and "○ auto_publish = да" in text and "●" not in text.split("\n", 1)[1],
          "/set показывает настройки: пока все общие", text)
    _, text = await ask("/set beta_channel shorts_per_day 1", "shorts_per_day = 1")
    check("своя настройка" in text, "/set задаёт стримеру свою настройку", text)
    _, text = await ask("/set beta_channel publish_privacy secret", "Не получилось")
    check("не подходит" in text, "неподходящее значение не принимается", text)
    await ask("/set beta_channel no_part_categories Just Chatting,Minecraft", "no_part_categories")
    twitch.add_vod("9000003", "beta_channel", "Бета снова", [(0, 3600, "Minecraft")])
    process("9000003")
    b3 = await wait_for(lambda: all_in("9000003", done), 60, "ролик Беты со своей настройкой")
    check(b3[0].title == "Minecraft | Бета снова | Бета", "своя настройка действует: у Беты Minecraft без номера части",
          b3[0].title)
    _, text = await ask("/set beta_channel no_part_categories -", "no_part_categories")
    check("общая настройка из .env" in text
          and json.loads((await streamer("beta_channel")).overrides) == {"shorts_per_day": 1}
          and (await streamer("alpha_channel")).overrides is None,
          "«-» возвращает общую настройку; настройки Альфы не тронуты", text)
    _, text = await ask("/set beta_channel", "Настройки стримера BetaTV")
    check("● shorts_per_day = 1" in text, "своя настройка отмечена в списке")

    print("\n== 5. Проблема одного канала не останавливает остальные", flush=True)
    await wait_for(lambda: settled("9000001", "9000003"), 30, "наблюдение за роликами Беты закончилось")
    app().youtube.forget("RT-1")  # как будто токен доступа истёк
    google.revoked.add("RT-1")  # а доступ к каналу Беты отозван в Google
    twitch.add_vod("9000004", "beta_channel", "Бета опять", [(0, 3600, "Dota 2")])
    twitch.add_vod("9000005", "alpha_channel", "Альфа снова", [(0, 3600, "Minecraft")])
    since = tg.next_message_id
    process("9000004")
    notice = await wait_for(lambda: tg.find("доступ к YouTube отозван или истёк", since), 30, "доступ Беты потерян")
    text = tg.messages[notice]["text"]
    check(text.startswith(f"🔴 {BETA} · Обработка на паузе") and "/youtube beta_channel, затем /resume beta_channel" in text
          and (await streamer("beta_channel")).paused,
          "потерян доступ к каналу Беты: на паузе только она, бот называет стримера и команды", text)
    process("9000005")
    a5 = await wait_for(lambda: all_in("9000005", done), 60, "ролик Альфы при паузе Беты")
    check(a5[0].title == "Minecraft | Альфа снова | Часть 2 | Альфа" and not (await streamer("alpha_channel")).paused
          and [r.status for r in await segs("9000004")] == [Status.QUEUED],
          "Альфа продолжает загружать и публиковать, ролик Беты ждёт в очереди")
    alerts = [mid for mid, m in tg.owner_messages(since) if "доступ к YouTube отозван или истёк" in m["text"]]
    check(len(alerts) == 1, "о потере доступа бот сообщает один раз", len(alerts))
    _, text = await ask("/status", "yt-dlp")
    check("⏸ на паузе, /resume beta_channel — продолжить" in block(text, BETA)
          and "Обработка: ▶️ работает" in block(text, ALPHA),
          "/status: блок на каждого стримера, пауза только у Беты", text)
    google.channel_of["2"] = "1"  # Бета снова входит в тот же канал
    text = await connect("/youtube beta_channel")
    check("/resume beta_channel" in text and [r.status for r in await segs("9000004")] == [Status.QUEUED],
          "после нового входа пауза остаётся до /resume", text)
    await ask("/resume beta_channel", "обработка продолжена")
    b4 = await wait_for(lambda: all_in("9000004", done), 60, "ролик Беты после нового подключения")
    check(google.uploaders.get(b4[0].title) == "RT-2", "ролик Беты загружен с новым токеном")

    google.upload_limited.add("RT-0")  # канал Альфы упёрся в дневной лимит загрузок
    twitch.add_vod("9000006", "alpha_channel", "Альфа и лимит", [(0, 3600, "Dota 2")])
    twitch.add_vod("9000007", "beta_channel", "Бета тем временем", [(0, 3600, "Portal")])
    since = tg.next_message_id
    process("9000006")
    notice = await wait_for(lambda: tg.find("исчерпан дневной лимит канала", since), 30, "лимит канала Альфы")
    alpha = await streamer("alpha_channel")
    check("«Канал Альфы»" in tg.messages[notice]["text"] and tg.messages[notice]["silent"] and not alpha.paused
          and alpha.uploads_wait_until is not None and (await streamer("beta_channel")).uploads_wait_until is None,
          "дневной лимит канала: ждёт только Альфа, никто не на паузе", tg.messages[notice]["text"])
    process("9000007")
    await wait_for(lambda: all_in("9000007", done), 60, "ролик Беты, пока Альфа ждёт лимит")
    _, text = await ask("/status", "yt-dlp")
    check("⏳ YouTube не принимает новые ролики — исчерпан дневной лимит канала" in block(text, ALPHA)
          and "⏳" not in block(text, BETA) and [r.status for r in await segs("9000006")] == [Status.QUEUED],
          "Бета загружает и публикует, ожидание видно только в блоке Альфы", text)
    google.upload_limited.clear()
    await waits_over()
    await wait_for(lambda: all_in("9000006", done), 60, "ролик Альфы после лимита")
    check(await eventually(lambda: tg.find("снова принимает загрузки на канал «Канал Альфы»", since))
          and (await streamer("alpha_channel")).uploads_wait_until is None,
          "когда лимит снят, Альфа продолжает сама")

    google.quota_exceeded = True  # а квота Google-проекта общая для всех
    twitch.add_vod("9000008", "beta_channel", "Бета и квота", [(0, 3600, "Portal")])
    since = tg.next_message_id
    process("9000008")
    await wait_for(lambda: tg.find("Кончилась суточная квота YouTube API", since), 30, "квота проекта")
    alpha, beta = await streamer("alpha_channel"), await streamer("beta_channel")
    check(alpha.uploads_wait_until is not None and beta.uploads_wait_until is not None and not alpha.paused and not beta.paused,
          "квота проекта: сброса ждут загрузки всех стримеров, никто не на паузе")
    google.quota_exceeded = False
    await waits_over()
    await wait_for(lambda: all_in("9000008", done), 60, "ролик Беты после сброса квоты")

    _, text = await ask("/pause beta_channel", "остальные стримеры работают")
    check((await streamer("beta_channel")).paused and not (await streamer("alpha_channel")).paused
          and "/resume beta_channel" in text, "/pause стримера останавливает только его", text)
    await ask("/resume beta_channel", "обработка продолжена")
    await ask("/pause", "остановлены")
    check((await streamer("beta_channel")).paused and (await streamer("alpha_channel")).paused,
          "/pause без стримера останавливает всех")
    await ask("/resume", "Обработка продолжена")
    check(not (await streamer("beta_channel")).paused and not (await streamer("alpha_channel")).paused,
          "/resume без стримера продолжает всех")

    twitch.add_vod("9000010", "alpha_channel", "Альфа смотрит", [(0, 1800, "Watch Party")])
    process("9000010")
    skipped = (await wait_for(lambda: all_sent("9000010"), 30, "пропущенный сегмент"))[0]
    check(skipped.status == Status.SKIPPED and skipped.title == "Watch Party | Альфа смотрит | Альфа",
          "сегмент из категории, которая не загружается, пропущен и не получил номер части", (skipped.status, skipped.title))
    answer = await press(skipped.tg_message_id, "⬆️")
    forced = (await segs("9000010"))[0]
    check(forced.title == "Watch Party | Альфа смотрит | Часть 1 | Альфа" and forced.force_review
          and forced.status != Status.SKIPPED and "после вашего решения" in (answer or ""),
          "«Всё равно загрузить» даёт номер части и имя своего стримера", (forced.title, forced.status, answer))

    print("\n== 6. Слежение за несколькими каналами", flush=True)
    twitch.add_vod("9000020", "alpha_channel", "Давний стрим", [(0, 3600, "Minecraft")],
                   started_at=datetime(2026, 9, 20, tzinfo=timezone.utc))
    twitch.add_vod("9000021", "alpha_channel", "Альфа в эфире", [(0, 600, "Portal 2")])
    twitch.add_vod("9000022", "beta_channel", "Бета в эфире", [(0, 600, "Portal 2")])
    twitch.lists = {"alpha_channel": ["9000021", "9000020"], "beta_channel": ["9000022"]}
    a_w = await wait_for(lambda: all_in("9000021", done), 60, "VOD Альфы через слежение")
    b_w = await wait_for(lambda: all_in("9000022", done), 60, "VOD Беты через слежение")
    check(a_w[0].title == "Portal 2 | Альфа в эфире | Часть 1 | Альфа" and b_w[0].title == "Portal 2 | Бета в эфире | Часть 1 | Бета"
          and google.uploaders.get(a_w[0].title) == "RT-0" and google.uploaders.get(b_w[0].title) == "RT-2",
          "бот сам обработал законченные эфиры обоих каналов, каждый на свой YouTube",
          (a_w[0].title, b_w[0].title))
    async with db() as s:
        check(await s.get(Vod, "9000020") is None, "эфир, закончившийся до начала слежения, не тронут")
    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    twitch.live["beta_channel"] = LiveState(stream_id="stream-b", started_at=started, title="Бета прямо сейчас")

    async def title_noted():
        async with db() as s:
            return (await s.scalars(select(TitleChange).where(TitleChange.channel == "beta_channel"))).first()

    await wait_for(title_noted, 20, "название стрима Беты записано")
    _, text = await ask("/status", "Название стрима сейчас")
    check("Название стрима сейчас: «Бета прямо сейчас»" in block(text, BETA)
          and "Название стрима" not in block(text, ALPHA) and "стрима нет, проверено в" in block(text, ALPHA),
          "/status: у каждого стримера своё состояние канала и название стрима", text)
    twitch.live.clear()
    twitch.broken.add("alpha_channel")
    twitch.add_vod("9000023", "beta_channel", "Бета ещё раз", [(0, 600, "Portal 2")])
    twitch.lists["beta_channel"] = ["9000023", "9000022"]
    alert = await wait_for(lambda: tg.find("Не получается проверить канал Twitch alpha_channel 3 раза подряд"), 30,
                           "тревога о канале Альфы")
    await wait_for(lambda: all_in("9000023", done), 60, "VOD Беты, пока канал Альфы не открывается")
    check(not tg.messages[alert]["silent"], "сбой проверки одного канала: тревога о нём, второй канал обрабатывается")
    twitch.broken.clear()

    print("\n== 7. Shorts у каждого канала свои", flush=True)

    async def shorts(login, statuses=None):
        async with db() as s:
            rows = list((await s.scalars(
                select(Segment).where(Segment.kind == SHORT, Segment.vod_id == clips_vod_id(login)).order_by(Segment.idx)
            )).all())
        return rows if statuses is None or (rows and all(r.status in statuses for r in rows)) else None

    # Сначала клипы только у Альфы: она выбирает общий лимит — два Shorts в сутки
    twitch.clips = {
        "alpha_channel": [
            base.clip_node("a1", "AlphaClipOne", "альфа побеждает", 500, 20, "Dota 2"),
            base.clip_node("a2", "AlphaClipTwo", "альфа проигрывает", 400, 20, "Dota 2"),
            base.clip_node("a3", "AlphaClipThree", "альфа молчит", 300, 20, "Dota 2"),
        ]
    }
    app().shorts_wake.set()
    await wait_for(lambda: _count(shorts, "alpha_channel", 2), 30, "Shorts Альфы")
    # Потом клипы появляются у Беты: лимит Альфы на неё не действует, а свой у неё — один в сутки (/set)
    twitch.clips["beta_channel"] = [
        base.clip_node("b1", "BetaClipOne", "бета смеётся", 900, 15, "Dota 2"),
        base.clip_node("b2", "BetaClipTwo", "бета удивляется", 800, 15, "Dota 2"),
    ]
    app().shorts_wake.set()
    await wait_for(lambda: _count(shorts, "beta_channel", 1), 30, "Shorts Беты")
    a_s = await wait_for(lambda: shorts("alpha_channel", done), 60, "Shorts Альфы опубликованы")
    b_s = await wait_for(lambda: shorts("beta_channel", done), 60, "Shorts Беты опубликован")
    app().shorts_wake.set()
    await asyncio.sleep(2)
    a_s, b_s = await shorts("alpha_channel"), await shorts("beta_channel")
    check([r.clip_id for r in a_s] == ["a1", "a2"] and [r.clip_id for r in b_s] == ["b1"],
          "лимит Shorts у каждого канала свой: Альфе два по общей настройке, Бете один по её собственной",
          ([r.clip_id for r in a_s], [r.clip_id for r in b_s]))
    check(a_s[0].title == "альфа побеждает | Альфа" and b_s[0].title == "бета смеётся | Бета"
          and google.uploaders.get(a_s[0].title) == "RT-0" and google.uploaders.get(b_s[0].title) == "RT-2",
          "Shorts названы и загружены по своим стримерам", (a_s[0].title, b_s[0].title))
    check(tg.messages[b_s[0].tg_message_id]["text"].splitlines()[1] == BETA, "в карточке Shorts имя и хештег стримера",
          tg.messages[b_s[0].tg_message_id]["text"])
    await wait_for(lambda: {"Shorts | Альфа", "Shorts | Бета"} <= {p["title"] for p in google.playlists.values()},
                   30, "плейлисты Shorts")
    check(True, "у каждого стримера свой плейлист Shorts")

    print("\n== 8. /disconnect и /remove", flush=True)
    picker, _ = await ask("/disconnect", "Чей YouTube-канал отключить")
    check(buttons(tg.messages[picker]["markup"]) == ["Альфа", "Бета"], "бот спрашивает, чей канал отключать")
    since = tg.next_message_id
    await press(picker, "Альфа")
    confirm = await wait_for(lambda: tg.find("Отключить YouTube-канал", since), 30, "подтверждение отключения")
    check("«Канал Альфы» стримера AlphaTV" in tg.messages[confirm]["text"], "в подтверждении названы канал и стример",
          tg.messages[confirm]["text"])
    await press(confirm, "🔌")
    await wait_for(lambda: "отключён" in tg.messages[confirm]["text"], 30, "отключение Альфы")
    alpha, beta = await streamer("alpha_channel"), await streamer("beta_channel")
    check(alpha.youtube_token is None and alpha.consent_version is None and "RT-0" in google.revoke_calls and alpha.paused
          and beta.youtube_token is not None and "RT-2" not in google.revoke_calls and not beta.paused
          and "/youtube alpha_channel, затем /resume alpha_channel" in tg.messages[confirm]["text"],
          "отключён только канал Альфы: доступ отозван, на паузе только она, Бета работает",
          tg.messages[confirm]["text"])
    check(all(r.status == Status.FORGOTTEN for r in await segs("9000002"))
          and all(r.status == Status.PUBLISHED and r.youtube_id for r in await segs("9000001")),
          "данные о роликах удалены только у Альфы")
    google.channel_of["3"] = "1"  # Альфа входит в тот же YouTube-канал, что и Бета
    text = await connect("/youtube alpha_channel")
    check("Этот же канал подключён для: BetaTV" in text and "/resume alpha_channel" in text,
          "бот предупреждает, что канал общий для двух стримеров", text)
    confirm, _ = await ask("/disconnect alpha_channel", "Отключить YouTube-канал")
    await press(confirm, "🔌")
    await wait_for(lambda: "отключён" in tg.messages[confirm]["text"], 30, "отключение общего канала")
    check("доступ в Google не отозван" in tg.messages[confirm]["text"] and "RT-3" not in google.revoke_calls
          and (await streamer("beta_channel")).youtube_token is not None,
          "общий канал: доступ в Google не отзывается, пока он нужен второму стримеру", tg.messages[confirm]["text"])

    _, text = await ask("/remove", "Кого убрать")
    check("Бета — twitch.tv/beta_channel" in text, "/remove без имени показывает стримеров")
    _, text = await ask("/remove бета", "убран")
    beta = await streamer("beta_channel")
    check(beta.removed_at is not None and "/disconnect beta_channel" in text and "/add beta_channel" in text
          and not app().several, "/remove: слежение снято, канал остаётся подключённым", text)
    await asyncio.sleep(2)
    calls = twitch.list_calls.get("beta_channel", 0)
    await asyncio.sleep(3)
    check(twitch.list_calls.get("beta_channel", 0) == calls and twitch.list_calls["alpha_channel"] > 0,
          "за каналом убранного стримера бот больше не следит")
    _, text = await ask("/status", "yt-dlp")
    check("убран, бот за каналом не следит" in block(text, BETA) and "YouTube: «Канал 1»" in block(text, BETA)
          and block(text, ALPHA),
          "убранный стример виден в /status, пока подключён его канал", text)
    _, text = await ask("/process https://www.twitch.tv/videos/9000003", "уже обработан")
    twitch.add_vod("9000030", "beta_channel", "Бета без слежения", [(0, 600, "Portal 2")])
    _, text = await ask("/process https://www.twitch.tv/videos/9000030", "Не получилось")
    check("стример beta_channel убран. Вернуть: /add beta_channel" in text, "VOD убранного стримера не обрабатывается", text)
    text = await add("/add beta_channel")
    check((await streamer("beta_channel")).removed_at is None and "снова следит" in text and "уже подключён" in text
          and app().several, "/add возвращает убранного стримера", text)

    print("\n== 9. Кнопки под сообщениями, отправленными до обновления", flush=True)
    confirm, _ = await ask("/disconnect beta_channel", "Отключить YouTube-канал")
    answer = await old_button("yt:cancel", confirm)
    check(answer == "Отменено" and not tg.messages[confirm]["markup"]
          and (await streamer("beta_channel")).youtube_token is not None, "старая кнопка «Отмена» работает")
    async with db() as s, s.begin():
        await s.execute(update(Streamer).where(Streamer.login == "alpha_channel").values(consent_version=1))
    answer = await old_button("yt:accept3", confirm)
    check(answer == "Принято" and (await streamer("alpha_channel")).consent_version == 3,
          "старая кнопка согласия относится к стримеру из .env")

    print("\n== 10. Доступ к одному каналу отозван в Google", flush=True)
    watched = (await segs("9000022"))[0]
    async with db() as s, s.begin():
        # У ролика Беты подошло время очередной проверки на YouTube
        await s.execute(update(Segment).where(Segment.id == watched.id).values(check_at=datetime.now(timezone.utc)))
    since = tg.next_message_id
    google.revoked.add("RT-2")
    notice = await wait_for(lambda: tg.find("доступ к YouTube отозван или истёк", since), 30,
                            "проверка роликов заметила отзыв доступа")
    text = tg.messages[notice]["text"]
    check(text.startswith(f"🔴 {BETA} · Обработка на паузе") and "/youtube beta_channel, затем /resume beta_channel" in text
          and (await streamer("beta_channel")).paused,
          "проверка роликов: на паузе только Бета, бот называет стримера и команды", text)
    await asyncio.sleep(3)
    alerts = [mid for mid, m in tg.owner_messages(since) if "доступ к YouTube отозван или истёк" in m["text"]]
    check(len(alerts) == 1, "о потере доступа бот сообщает один раз", len(alerts))
    async with db() as s, s.begin():
        await s.execute(update(Streamer).values(youtube_checked_at=None))  # пора ежедневной сверки
    notice = await wait_for(lambda: tg.find("Доступ к YouTube отозван", since), 30, "ежедневная сверка заметила отзыв")
    text = tg.messages[notice]["text"]
    beta = await streamer("beta_channel")
    check(text.startswith(f"🔴 {BETA} · Доступ к YouTube отозван") and "/youtube beta_channel" in text
          and beta.youtube_token is None and beta.consent_version is None,
          "ежедневная сверка: данные канала Беты удалены, бот называет стримера", text)
    check(all(r.status == Status.FORGOTTEN for r in await segs("9000022")) and "RT-2" not in google.revoke_calls,
          "данные о роликах Беты удалены, а уже отозванный токен повторно не отзывается")

    print("\n== 11. Перезапуск и обновлённая политика", flush=True)
    google.channel_of["4"] = "1"
    await connect("/youtube beta_channel")
    async with db() as s, s.begin():
        # Канал Беты будто подключили до появления Shorts: согласие дано по старой версии политики
        await s.execute(update(Streamer).where(Streamer.login == "beta_channel")
                        .values(consent_version=1, consent_prompted=None))
    await stop_app(task)
    since = tg.next_message_id
    task = await start_app()
    policy = await wait_for(lambda: tg.find("Политика конфиденциальности AutoVOD обновлена", since), 30, "новая политика")
    check("BetaTV" in tg.messages[policy]["text"] and buttons(tg.messages[policy]["markup"]) == ["✅ Принимаю", "Отмена"],
          "после перезапуска бот просит принять новую политику для канала Беты", tg.messages[policy]["text"][:200])
    asked = [mid for mid, m in tg.owner_messages(since) if "Политика конфиденциальности AutoVOD обновлена" in m["text"]]
    check(len(asked) == 1, "про остальных стримеров бот не спрашивает", len(asked))
    answer = await press(policy, "✅")
    beta = await streamer("beta_channel")
    check(answer == "Принято" and beta.consent_version == 3 and beta.consent_prompted == 3, "согласие записано Бете")
    async with db() as s:
        rows = {row.login: row for row in (await s.scalars(select(Streamer))).all()}
        legacy = [row.key for row in (await s.scalars(select(KV))).all() if row.key in ("paused", "watch_since")]
    check(set(rows) == {"alpha_channel", "beta_channel", "old_test_channel"} and rows["old_test_channel"].removed_at
          and rows["alpha_channel"].removed_at is None and rows["beta_channel"].removed_at is None and app().several
          and as_utc(rows["alpha_channel"].watch_since) == OLD_SINCE and not legacy,
          "после перезапуска стримеры на месте, перенос настроек не повторяется")

    print("\n== 12. Протоколы и итог", flush=True)
    check(not tg.problems, "все тексты валидны для Telegram", tg.problems[:5])
    check(not google.violations, "запросы к Google соответствуют протоколу", google.violations[:5])
    check(set(tg.calls) <= base.FakeTelegram.KNOWN, "бот не вызывает неожиданных методов Telegram")
    await stop_app(task)

    await engine.dispose()
    await runner.cleanup()
    failed = [name for ok, name in base.RESULTS if not ok]
    print(f"\nИТОГО: {len(base.RESULTS) - len(failed)} из {len(base.RESULTS)} проверок пройдено за {time.monotonic() - t0:.0f} с")
    for name in failed:
        print("  не пройдено:", name)
    return not failed


async def _title_is(streamer, login, title):
    return (await streamer(login)).youtube_channel_title == title


async def _count(shorts, login, count):
    rows = await shorts(login)
    return rows if len(rows) >= count and all(r.tg_message_id for r in rows) else None


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
