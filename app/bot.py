"""Команды и кнопки бота. Всё, кроме /start, доступно только владельцу."""

from __future__ import annotations

import logging
import shutil
from html import escape

import httpx
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from . import consent, limits, streamers
from .config import describe_settings
from .context import App
from .db import SHORT, Playlist, Segment, Status, Streamer, Vod, all_streamers, as_utc, find_streamer, utcnow
from .segments import clean_line, is_short_reason, match_streamer, parse_channel, parse_vod_id
from .service import (
    DISCONNECTED_REASON,
    PUBLISHABLE,
    IngestError,
    forget_youtube,
    ingest_vod,
    publish,
    queue_skipped,
    set_status,
    status_counts,
)
from .tools import ToolError, fetch_vod_info
from .twitch import TwitchError, channel_display_name, client_id
from .ui import (
    ACCEPT,
    ASK_CONNECT,
    ASK_DISCONNECT,
    CANCEL,
    CONNECT,
    DISCONNECT,
    FORCE,
    KEEP,
    PUBLISH,
    RETRY,
    AddAction,
    SegmentAction,
    StreamerAction,
    YouTubeAction,
    accepted_from_action,
    command_for,
    confirm_keyboard,
    local_time,
    permission_keyboard,
    render_consent,
    render_disconnect,
    streamer_keyboard,
    streamer_tag,
)
from .worker import is_paused, set_paused
from .youtube import AuthError, DeviceCode, YouTubeError

log = logging.getLogger(__name__)

HELP = (
    "Бот сам следит за каналами стримеров. После конца стрима сегменты загружаются на YouTube приватно, "
    "ролики без предупреждений публикуются, по остальным бот спросит. Самые просматриваемые "
    "клипы канала становятся Shorts.\n\n"
    "/process &lt;ссылка на VOD&gt; — обработать VOD вручную (старый или пропущенный)\n"
    "/streamers — стримеры и их YouTube-каналы\n"
    "/add &lt;канал на Twitch&gt; [имя] — добавить стримера; имя пишется в конце названий его роликов\n"
    "/name &lt;стример&gt; &lt;имя&gt; — сменить это имя\n"
    "/set &lt;стример&gt; [настройка значение] — свои настройки стримера\n"
    "/remove &lt;стример&gt; — перестать следить за стримером\n"
    "/youtube [стример] — подключить YouTube-канал\n"
    "/disconnect [стример] — отключить YouTube-канал и удалить сохранённые о нём данные\n"
    "/status — состояние\n"
    "/pause [стример], /resume [стример] — остановить и продолжить загрузку и публикацию\n\n"
    "Стримера можно назвать логином, ссылкой на канал или именем. Пока стример один, его можно не указывать."
)
PUBLISH_PLACES = {"public": "публично", "unlisted": "по ссылке"}
NAME_MAX_CHARS = 40  # имя стоит в названии каждого ролика, а названию отведено 100 символов
MESSAGE_ROOM = 3800  # сообщение Telegram вмещает 4096 символов
RESET_WORDS = ("-", "default", "общее", "общая")  # /set <стример> <настройка> - возвращает общую настройку

STATUS_LABELS = (
    (Status.QUEUED, "в очереди"),
    (Status.UPLOADING, "загружаются"),
    (Status.PROCESSING, "обрабатываются"),
    (Status.WAITING, "ждут проверки"),
    (Status.REVIEW, "ждут решения"),
    (Status.PUBLISHED, "опубликованы"),
    (Status.PRIVATE, "оставлены приватными"),
    (Status.LOCKED, "заблокированы YouTube"),
    (Status.REJECTED, "отклонены"),
    (Status.FAILED, "с ошибкой"),
    (Status.SKIPPED, "пропущены"),
    (Status.FORGOTTEN, "данные удалены"),
)


class IsOwner(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, app: App) -> bool:
        user = event.from_user
        return app.owner_id is not None and user is not None and user.id == app.owner_id


router = Router(name="root")
owner = Router(name="owner")
owner.message.filter(IsOwner())
owner.callback_query.filter(IsOwner())
router.include_router(owner)


@router.message(CommandStart())
async def start(message: Message, app: App) -> None:
    user_id = message.from_user.id if message.from_user else None
    if app.owner_id is None:
        await message.answer(
            f"Ваш Telegram ID: <code>{user_id}</code>\n"
            "Впишите его в TELEGRAM_OWNER_ID в файле .env и перезапустите контейнер."
        )
    elif user_id == app.owner_id:
        await message.answer(HELP)
    else:
        log.info("Сообщение от постороннего пользователя %s", user_id)


@owner.message(Command("help"))
async def show_help(message: Message) -> None:
    await message.answer(HELP)


@owner.message(Command("process"))
async def process(message: Message, command: CommandObject, app: App) -> None:
    vod_id = parse_vod_id(command.args)
    if not vod_id:
        await message.answer("Пришлите ссылку на VOD: /process https://www.twitch.tv/videos/…")
        return
    async with app.sessions() as session:
        if await session.get(Vod, vod_id):
            await message.answer("Этот VOD уже обработан.")
            return
    progress = await message.answer("⏳ Получаю данные VOD…")
    try:
        await ingest_vod(app, await fetch_vod_info(vod_id))
    except (ToolError, IngestError) as exc:
        await progress.edit_text(f"🔴 Не получилось: {escape(str(exc))}")
        return
    except Exception as exc:
        log.exception("VOD %s не обработан", vod_id)
        await progress.edit_text(f"🔴 Внутренняя ошибка: {escape(str(exc)[:300])}")
        return
    await progress.delete()


# --- стримеры ---


def _name(streamer: Streamer) -> str:
    """Название канала на Twitch: для сообщений владельцу."""
    return streamer.display_name or streamer.login


def _streamer_list(rows: list[Streamer]) -> str:
    if not rows:
        return "Стримеров пока нет. Добавить: /add &lt;канал на Twitch&gt;"
    return "\n".join(f"• {escape(row.public_name)} — twitch.tv/{escape(row.login)}" for row in rows)


async def _pick(message: Message, args: str | None, candidates: list[Streamer], ask: str, question: str) -> Streamer | None:
    """Стример, к которому относится команда. Если его не назвали, а стримеров несколько, бот спрашивает
    кнопками и возвращает None: команда продолжится по нажатию."""
    if (args or "").strip():
        target = match_streamer(args, candidates)
        if target is None:
            await message.answer(f"Не нашёл такого стримера. Есть:\n{_streamer_list(candidates)}")
        return target
    if len(candidates) == 1:
        return candidates[0]
    await message.answer(question, reply_markup=streamer_keyboard(candidates, ask))
    return None


def _split_target(words: list[str], rows: list[Streamer]) -> tuple[Streamer | None, list[str]]:
    """Стример из первого слова команды и остальные слова. Единственного стримера можно не называть."""
    if words and (target := match_streamer(words[0], rows)) is not None:
        return target, words[1:]
    if len(rows) == 1:
        return rows[0], words
    return None, words


@owner.message(Command("streamers"))
async def list_streamers(message: Message, app: App) -> None:
    await streamers.watched(app)
    async with app.sessions() as session:
        rows = await all_streamers(session)
    # Убранный стример виден, пока подключён его канал: ролики на нём бот ещё ведёт
    shown = [row for row in rows if row.removed_at is None or row.youtube_token]
    if not shown:
        await message.answer(_streamer_list([]))
        return
    blocks = []
    for row in shown:
        youtube = f"«{escape(row.youtube_channel_title or '')}»" if row.youtube_token else "не подключён"
        permission = (
            f"разрешение стримера отмечено {local_time(row.permitted_at, app.tz, '%d.%m.%Y')}"
            if row.permitted_at
            else f"разрешение стримера не отмечено (/add {escape(row.login)})"
        )
        own = sum(1 for _, _, is_own in describe_settings(app.settings, row.overrides) if is_own)
        state = [
            *(["убран, бот за каналом не следит"] if row.removed_at else []),
            *(["⏸ на паузе"] if row.paused else []),
        ]
        blocks.append(
            "\n".join(
                [
                    f"<b>{streamer_tag(row)}</b> — twitch.tv/{escape(row.login)}",
                    f"YouTube: {youtube}",
                    permission[0].upper() + permission[1:],
                    f"Своих настроек: {own} (/set {escape(row.login)})",
                    *state,
                ]
            )
        )
    for text in _pack(blocks):
        await message.answer(text)


@owner.message(Command("add"))
async def add_streamer(message: Message, command: CommandObject, app: App) -> None:
    channel, _, rest = (command.args or "").strip().partition(" ")
    login = parse_channel(channel)
    if not login:
        await message.answer(
            "Укажите канал на Twitch: /add twitch.tv/логин или /add логин.\n"
            "После канала можно написать имя для названий роликов: /add zakvielchannel Заквиель"
        )
        return
    title_name = clean_line(rest)[:NAME_MAX_CHARS] or None
    async with app.sessions() as session:
        existing = await find_streamer(session, login)
    if existing and existing.removed_at is None and existing.permitted_at:
        if title_name:
            await streamers.rename(app, existing.id, title_name)
            existing.title_name = title_name
        await message.answer(
            f"Стример {escape(_name(existing))} (twitch.tv/{escape(login)}) уже добавлен. "
            f"В конце названий его роликов — «{escape(existing.public_name)}»."
        )
        return
    try:
        display_name = await channel_display_name(app.http, login, await client_id())
    except TwitchError as exc:
        await message.answer(f"🔴 Не получилось: {escape(str(exc))}")
        return
    app.pending_adds[login] = (display_name, title_name)
    # Нарезки делаются только с разрешения стримера: бот спрашивает о нём и запоминает ответ
    await message.answer(
        f"Добавить стримера {escape(display_name)} (twitch.tv/{escape(login)})?\n"
        "Бот будет нарезать его стримы и публиковать их на YouTube-канале, который вы подключите. "
        "Для этого нужно разрешение стримера на нарезки. Оно у вас есть?",
        reply_markup=permission_keyboard(login),
    )


@owner.callback_query(AddAction.filter())
async def confirm_add(query: CallbackQuery, callback_data: AddAction, app: App) -> None:
    pending = app.pending_adds.pop(callback_data.login, None)
    if not callback_data.yes:
        await query.answer("Отменено")
        await _drop_buttons(query)
        return
    if pending is None:
        await query.answer("Запрос устарел: повторите /add")
        await _drop_buttons(query)
        return
    await query.answer("Добавляю…")
    streamer, outcome = await streamers.add(app, callback_data.login, *pending)
    who = f"{escape(_name(streamer))} (twitch.tv/{escape(streamer.login)})"
    lines = [
        {
            "added": f"✅ Добавлен стример {who}. Разрешение стримера отмечено.",
            "restored": f"✅ Бот снова следит за стримером {who}.",
            "exists": f"✅ Разрешение стримера {who} отмечено.",
        }[outcome],
        f"В конце названий его роликов будет «{escape(streamer.public_name)}». "
        f"Сменить: /name {escape(streamer.login)} &lt;имя&gt;",
    ]
    if outcome != "exists":
        lines.append(
            "Эфиры, которые закончатся после этой минуты, бот обработает сам; более ранние можно отдать вручную: "
            "/process &lt;ссылка на VOD&gt;"
        )
    if streamer.youtube_token:
        lines.append(f"YouTube-канал «{escape(streamer.youtube_channel_title or '')}» уже подключён.")
    else:
        lines.append(f"Осталось подключить его YouTube-канал: /youtube {escape(streamer.login)}")
    text = "\n".join(lines)
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text)
            return
        except TelegramBadRequest as exc:
            log.info("Сообщение о добавлении стримера не обновлено: %s", exc)
    await app.notify(text)


@owner.message(Command("remove"))
async def remove_streamer(message: Message, command: CommandObject, app: App) -> None:
    rows = await streamers.watched(app)
    target = match_streamer(command.args, rows)
    if target is None:
        await message.answer(f"Кого убрать? /remove &lt;стример&gt;\n{_streamer_list(rows)}")
        return
    await streamers.remove(app, target.id)
    text = (
        f"Стример {escape(_name(target))} убран: бот больше не следит за его каналом и клипами. "
        f"Ролики, которые уже в работе, он доведёт до конца. Вернуть: /add {escape(target.login)}"
    )
    if target.youtube_token:
        text += (
            f"\nYouTube-канал «{escape(target.youtube_channel_title or '')}» остаётся подключённым. "
            f"Отключить его и удалить данные: /disconnect {escape(target.login)}"
        )
    await message.answer(text)


@owner.message(Command("name"))
async def rename_streamer(message: Message, command: CommandObject, app: App) -> None:
    args = clean_line(command.args)
    rows = await streamers.watched(app)
    first, _, name = args.partition(" ")
    if len(rows) == 1:
        # Стример один: его можно не называть, и тогда всё написанное — имя
        target = rows[0]
        if parse_channel(first) != target.login or not name:
            name = args
    else:
        target = match_streamer(first, rows)
    name = name.strip()[:NAME_MAX_CHARS]
    if target is None or not name:
        await message.answer(
            "Имя стоит в конце названий роликов и плейлистов: /name &lt;стример&gt; &lt;имя&gt;\n"
            f"{_streamer_list(rows)}"
        )
        return
    await streamers.rename(app, target.id, name)
    await message.answer(
        f"✅ {escape(_name(target))}: в названиях новых роликов и плейлистов теперь «{escape(name)}». "
        "Уже нарезанные ролики и созданные плейлисты названий не меняют."
    )


def _shown(value: object) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


@owner.message(Command("set"))
async def set_setting(message: Message, command: CommandObject, app: App) -> None:
    rows = await streamers.watched(app)
    target, words = _split_target((command.args or "").split(), rows)
    if target is None:
        await message.answer(
            "Свои настройки стримера: /set &lt;стример&gt; — посмотреть, "
            f"/set &lt;стример&gt; &lt;настройка&gt; &lt;значение&gt; — задать.\n{_streamer_list(rows)}"
        )
        return
    login = escape(target.login)
    if not words:
        lines = [
            f"{'●' if own else '○'} {name} = {escape(_shown(value))}"
            for name, value, own in describe_settings(app.settings, target.overrides)
        ]
        await message.answer(
            f"Настройки стримера {escape(_name(target))}. ● — свои, ○ — общие из .env:\n"
            + "\n".join(lines)
            + f"\n\nЗадать: /set {login} &lt;настройка&gt; &lt;значение&gt;\n"
            f"Вернуть общую: /set {login} &lt;настройка&gt; -"
        )
        return
    name, value = words[0], " ".join(words[1:])
    if not value:
        await message.answer(f"Укажите значение: /set {login} {escape(name)} &lt;значение&gt;, а «-» вернёт общее.")
        return
    try:
        target = await streamers.set_setting(app, target.id, name, None if value.lower() in RESET_WORDS else value)
    except ValueError as exc:
        await message.answer(f"🔴 Не получилось: {escape(str(exc))}")
        return
    current = {key: (val, own) for key, val, own in describe_settings(app.settings, target.overrides)}
    val, own = current[name.strip().lower()]
    origin = "своя настройка" if own else "общая настройка из .env"
    await message.answer(f"✅ {escape(_name(target))}: {escape(name.lower())} = {escape(_shown(val))} ({origin}).")


# --- YouTube-канал стримера ---


@owner.message(Command("youtube"))
async def connect_youtube(message: Message, command: CommandObject, app: App) -> None:
    if not (app.settings.google_client_id and app.settings.google_client_secret):
        await message.answer("Сначала заполните GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET в .env и перезапустите контейнер.")
        return
    rows = await streamers.watched(app)
    if not rows:
        await message.answer(_streamer_list([]))
        return
    target = await _pick(message, command.args, rows, ASK_CONNECT, "Для какого стримера подключить YouTube-канал?")
    if target is not None:
        await _ask_consent(app, target)


async def _ask_consent(app: App, streamer: Streamer) -> None:
    # Вход только после согласия: так требуют правила YouTube API
    await app.notify(
        render_consent(app.config(streamer), _name(streamer)),
        markup=confirm_keyboard("✅ Принимаю, подключить", CONNECT, streamer.id),
    )


@owner.message(Command("disconnect"))
async def disconnect(message: Message, command: CommandObject, app: App) -> None:
    await streamers.watched(app)
    # Отключить можно и канал убранного стримера
    async with app.sessions() as session:
        connected = await all_streamers(session, connected=True)
    if not connected:
        await message.answer("YouTube-канал не подключён.")
        return
    target = await _pick(message, command.args, connected, ASK_DISCONNECT, "Чей YouTube-канал отключить?")
    if target is not None:
        await _ask_disconnect(app, target)


async def _ask_disconnect(app: App, streamer: Streamer) -> None:
    await app.notify(
        render_disconnect(streamer.youtube_channel_title or "", _name(streamer) if app.several else ""),
        markup=confirm_keyboard("🔌 Отключить и удалить данные", DISCONNECT, streamer.id),
    )


@owner.callback_query(StreamerAction.filter())
async def streamer_button(query: CallbackQuery, callback_data: StreamerAction, app: App) -> None:
    await _youtube_action(query, app, callback_data.action, callback_data.id)


@owner.callback_query(YouTubeAction.filter())
async def old_youtube_button(query: CallbackQuery, callback_data: YouTubeAction, app: App) -> None:
    """Кнопка под сообщением, отправленным, когда стример был один: она относится к стримеру из TWITCH_CHANNEL."""
    await _youtube_action(query, app, callback_data.action, (await streamers.first(app)).id)


async def _youtube_action(query: CallbackQuery, app: App, action: str, streamer_id: int) -> None:
    if action == CANCEL:
        await query.answer("Отменено")
        await _drop_buttons(query)
        return
    async with app.sessions() as session:
        streamer = await session.get(Streamer, streamer_id)
    if streamer is None:
        await query.answer("Такого стримера больше нет")
        await _drop_buttons(query)
        return
    if action == ASK_CONNECT:
        await query.answer()
        await _drop_buttons(query)
        await _ask_consent(app, streamer)
    elif action == ASK_DISCONNECT:
        await query.answer()
        await _drop_buttons(query)
        if streamer.youtube_token:
            await _ask_disconnect(app, streamer)
        else:
            await app.notify("Канал уже отключён.")
    elif action == CONNECT:
        await _accept_and_connect(query, app, streamer)
    elif action.startswith(ACCEPT):
        # Согласие с обновлённой политикой без повторного входа в Google. Принимается та версия, что была показана
        await consent.accept(app, streamer.id, accepted_from_action(action))
        await query.answer("Принято")
        await _drop_buttons(query)
    elif action == DISCONNECT:
        await _confirm_disconnect(query, app, streamer)
    else:
        await query.answer()


async def _accept_and_connect(query: CallbackQuery, app: App, streamer: Streamer) -> None:
    await query.answer()
    await _drop_buttons(query)
    await consent.accept(app, streamer.id)
    try:
        code = await app.youtube.start_device_flow()
    except (YouTubeError, httpx.HTTPError) as exc:
        await app.notify(f"🔴 Google не выдал код: {escape(str(exc))}")
        return
    whose = f" стримера {escape(_name(streamer))}" if app.several else ""
    prompt = await app.notify(
        f"Откройте {code.verification_url} и введите код <code>{code.user_code}</code>\n"
        f"Код действует {code.expires_in // 60} мин. Войдите в Google-аккаунт, которому принадлежит канал{whose}, "
        "выберите канал и разрешите управлять аккаунтом YouTube: без этого нельзя загружать ролики "
        "и менять их доступ."
    )
    if prompt:
        app.spawn(_finish_youtube(app, code, prompt, streamer.id))


async def _finish_youtube(app: App, code: DeviceCode, prompt: Message, streamer_id: int) -> None:
    try:
        refresh_token = await app.youtube.finish_device_flow(code)
        channel_id, title = await app.youtube.my_channel(refresh_token)
    except (YouTubeError, httpx.HTTPError) as exc:
        await prompt.edit_text(f"🔴 YouTube не подключён: {escape(str(exc))}")
        return
    async with app.sessions() as session, session.begin():
        streamer = await session.get(Streamer, streamer_id)
        streamer.youtube_token = app.vault.encrypt(refresh_token)
        streamer.youtube_channel_id = channel_id
        streamer.youtube_channel_title = title
        shared = await _same_channel(session, streamer)
    text = f"✅ Подключён канал «{escape(title)}»"
    if app.several:
        text += f" для стримера {escape(_name(streamer))}"
    if shared:
        names = ", ".join(escape(_name(other)) for other in shared)
        text += f"\nЭтот же канал подключён для: {names}. Ролики выходят на одном канале."
    if await is_paused(app, streamer.id):
        text += f"\nОбработка на паузе: {command_for('/resume', streamer, app.several)} — продолжить."
    await prompt.edit_text(text)


async def _same_channel(session, streamer: Streamer) -> list[Streamer]:
    """Другие стримеры, чьи ролики идут на тот же YouTube-канал."""
    rows = await session.scalars(
        select(Streamer).where(
            Streamer.id != streamer.id,
            Streamer.youtube_token.is_not(None),
            Streamer.youtube_channel_id == streamer.youtube_channel_id,
        )
    )
    return list(rows.all())


async def _confirm_disconnect(query: CallbackQuery, app: App, streamer: Streamer) -> None:
    if not streamer.youtube_token:
        await query.answer("Канал уже отключён")
        await _drop_buttons(query)
        return
    await query.answer("Отключаю…")
    async with app.sessions() as session:
        shared = await _same_channel(session, streamer)
    several = app.several
    if not shared:
        try:
            await app.youtube.revoke(app.vault.decrypt(streamer.youtube_token))
        except (YouTubeError, httpx.HTTPError) as exc:
            again = command_for("/disconnect", streamer, several)
            await app.notify(
                f"🔴 Google не отозвал доступ: {escape(str(exc))}. Данные не удалены, попробуйте ещё раз: {again}"
            )
            return
    await set_paused(app, True, streamer.id)
    await forget_youtube(app, streamer.id, DISCONNECTED_REASON)
    if shared:
        # Google отзывает доступ приложения к аккаунту целиком, а он ещё нужен другому стримеру
        access = (
            "доступ в Google не отозван, потому что этот канал подключён и для "
            + ", ".join(escape(_name(other)) for other in shared)
            + ";"
        )
    else:
        access = "доступ в Google отозван,"
    text = (
        f"✅ Канал «{escape(streamer.youtube_channel_title or '')}» отключён: {access} "
        "AutoVOD удалил токен, данные канала, ID и состояние роликов. Ролики на YouTube не тронуты.\n"
        f"Подключить снова — {command_for('/youtube', streamer, several)}, "
        f"затем {command_for('/resume', streamer, several)}."
    )
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text)
            return
        except TelegramBadRequest as exc:
            log.info("Сообщение об отключении не обновлено: %s", exc)
    await app.notify(text)


async def _drop_buttons(query: CallbackQuery) -> None:
    """Убирает кнопки подтверждения, чтобы их не нажали второй раз."""
    if not (isinstance(query.message, Message) and query.message.reply_markup):
        return
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as exc:  # кнопки уже убраны или сообщение слишком старое
        log.info("Кнопки не убраны: %s", exc)


# --- состояние ---


async def _streamer_status(app: App, streamer: Streamer, titled: bool) -> list[str]:
    """Строки /status про одного стримера; titled — с именем и хештегом в первой строке."""
    counts = await status_counts(app, streamer_id=streamer.id)
    short_counts = await status_counts(app, SHORT, streamer.id)
    async with app.sessions() as session:
        playlist_count = await session.scalar(
            select(func.count()).select_from(Playlist).where(Playlist.streamer_id == streamer.id)
        )
    connect = command_for("/youtube", streamer, app.several)
    youtube = (
        f"«{escape(streamer.youtube_channel_title or '')}»" if streamer.youtube_token else f"не подключён, выполните {connect}"
    )
    segments = ", ".join(f"{label} {counts[key]}" for key, label in STATUS_LABELS if counts.get(key))
    shorts_done = ", ".join(f"{label} {short_counts[key]}" for key, label in STATUS_LABELS if short_counts.get(key))
    wait_until = streamer.uploads_wait_until
    waiting = (
        f"Загрузка: ⏳ YouTube не принимает новые ролики — {limits.describe(streamer.uploads_wait_reason)}. "
        f"Следующая попытка в {local_time(wait_until, app.tz, '%H:%M')}, публикация идёт как обычно"
        if wait_until and as_utc(wait_until) > utcnow()
        else None
    )
    if streamer.removed_at:
        watch = "убран, бот за каналом не следит"
    elif app.settings.watch_interval_sec <= 0:
        watch = "слежение выключено (WATCH_INTERVAL_SEC=0), только /process"
    else:
        watch = app.watch_states.get(streamer.id, "ещё не проверялся")
    live_title = app.live_titles.get(streamer.id)
    settings = app.config(streamer)
    publishing = (
        f"{PUBLISH_PLACES[settings.publish_privacy]} через {settings.publish_delay_min} мин после обработки"
        if settings.auto_publish
        else "выключена"
    )
    if not settings.playlists:
        playlists = "выключены (PLAYLISTS=false)"
    elif not streamer.youtube_token:
        playlists = "YouTube-канал не подключён"
    elif consent.accepted_version(streamer) < consent.PLAYLISTS_SINCE:
        playlists = "ждут согласия с обновлённой политикой конфиденциальности"
    else:
        playlists = f"по категориям, создано {playlist_count}"
    if not settings.shorts:
        shorts = "выключены (SHORTS=false)"
    elif not streamer.youtube_token:
        shorts = "YouTube-канал не подключён"
    elif consent.accepted_version(streamer) < consent.SHORTS_SINCE:
        shorts = "ждут согласия с обновлённой политикой конфиденциальности"
    else:
        shorts = (
            f"из клипов за 7 дней от {settings.shorts_min_views} просмотров, не больше {settings.shorts_per_day} "
            f"в сутки, публикация через {settings.shorts_publish_delay_min} мин после обработки; "
            f"{shorts_done or 'пока нет'}"
        )
    resume = command_for("/resume", streamer, app.several)
    return [
        *([f"<b>{streamer_tag(streamer)}</b>"] if titled else []),
        f"Twitch: {escape(streamer.login)} — {watch}",
        *([f"Название стрима сейчас: «{escape(live_title)}»"] if live_title else []),
        f"YouTube: {youtube}",
        f"Обработка: {f'⏸ на паузе, {resume} — продолжить' if streamer.paused else '▶️ работает'}",
        *([waiting] if waiting else []),
        f"Автопубликация: {publishing}",
        f"Плейлисты: {playlists}",
        f"Сегменты: {segments or 'пока нет'}",
        f"Shorts: {shorts}",
    ]


def _pack(parts: list[str], room: int = MESSAGE_ROOM) -> list[str]:
    """Собирает части в сообщения: стримеров может быть больше, чем вмещает одно."""
    messages: list[str] = []
    current = ""
    for part in parts:
        if current and len(current) + len(part) + 2 > room:
            messages.append(current)
            current = part
        else:
            current = f"{current}\n\n{part}" if current else part
    if current:
        messages.append(current)
    return messages


@owner.message(Command("status"))
async def show_status(message: Message, app: App) -> None:
    await streamers.watched(app)
    async with app.sessions() as session:
        rows = await all_streamers(session)
    # Убранный стример виден, пока подключён его канал: ролики на нём бот ещё ведёт
    shown = [row for row in rows if row.removed_at is None or row.youtube_token]
    disk = shutil.disk_usage(app.settings.data_dir)
    common = [
        f"Диск: свободно {disk.free / 1024**3:.0f} из {disk.total / 1024**3:.0f} ГБ",
        f"yt-dlp: {escape(app.ytdlp_version)}",
    ]
    if not shown:
        await message.answer("\n".join([_streamer_list([]), *common]))
        return
    blocks = [await _streamer_status(app, row, titled=len(shown) > 1) for row in shown]
    if len(blocks) == 1:
        # Один стример: всё одним списком, как было до появления нескольких
        await message.answer("\n".join([*blocks[0], *common]))
        return
    for text in _pack(["\n".join(block) for block in blocks] + ["\n".join(common)]):
        await message.answer(text)


async def _pause_target(message: Message, args: str | None, app: App) -> Streamer | None | bool:
    """Стример из /pause или /resume. None — команда для всех, False — такого стримера нет (бот уже ответил)."""
    await streamers.watched(app)
    if not (args or "").strip():
        return None
    async with app.sessions() as session:
        rows = await all_streamers(session)
    target = match_streamer(args, rows)
    if target is None:
        await message.answer(f"Не нашёл такого стримера. Есть:\n{_streamer_list(rows)}")
        return False
    return target


@owner.message(Command("pause"))
async def pause(message: Message, command: CommandObject, app: App) -> None:
    target = await _pause_target(message, command.args, app)
    if target is False:
        return
    if target is None:
        await set_paused(app, True)
        await message.answer(
            "⏸ Загрузка и автопубликация остановлены: текущий сегмент догрузится, остальное ждёт /resume."
        )
        return
    await set_paused(app, True, target.id)
    await message.answer(
        f"⏸ {escape(_name(target))}: загрузка и автопубликация остановлены, остальные стримеры работают. "
        f"Продолжить: /resume {escape(target.login)}"
    )


@owner.message(Command("resume"))
async def resume(message: Message, command: CommandObject, app: App) -> None:
    target = await _pause_target(message, command.args, app)
    if target is False:
        return
    if target is None:
        await set_paused(app, False)
        await message.answer("▶️ Обработка продолжена.")
        return
    await set_paused(app, False, target.id)
    await message.answer(f"▶️ {escape(_name(target))}: обработка продолжена.")


# --- кнопки под сегментом ---


@owner.callback_query(SegmentAction.filter(F.action == PUBLISH))
async def publish_now(query: CallbackQuery, callback_data: SegmentAction, app: App) -> None:
    async with app.sessions() as session:
        seg = await session.get(Segment, callback_data.id)
    if seg is None or seg.status not in PUBLISHABLE:
        await query.answer("Уже обработан")
        await app.refresh_segment(callback_data.id)
        return
    await query.answer("Публикую…")
    try:
        result = await publish(app, seg.id)
    except AuthError:
        result = "нет доступа к YouTube: выполните /youtube"
    except (YouTubeError, httpx.HTTPError) as exc:
        result = f"YouTube ответил ошибкой: {exc}"
    if result not in ("Опубликовано", "YouTube не дал опубликовать"):
        await app.notify(f"🔴 Не опубликовано: {escape(result)}", reply_to=seg.tg_message_id)


@owner.callback_query(SegmentAction.filter(F.action == KEEP))
async def keep_private(query: CallbackQuery, callback_data: SegmentAction, app: App) -> None:
    changed = await set_status(app, callback_data.id, (Status.WAITING, Status.REVIEW), Status.PRIVATE, check_at=None)
    await query.answer("Оставлен приватным" if changed else "Уже обработан")
    await app.refresh_segment(callback_data.id)


@owner.callback_query(SegmentAction.filter(F.action == RETRY))
async def retry(query: CallbackQuery, callback_data: SegmentAction, app: App) -> None:
    changed = await set_status(app, callback_data.id, (Status.FAILED,), Status.QUEUED, queued_at=utcnow(), error=None)
    await query.answer("Повторяю" if changed else "Уже обработан")
    await app.refresh_segment(callback_data.id)
    if changed:
        app.wake.set()


@owner.callback_query(SegmentAction.filter(F.action == FORCE))
async def force_upload(query: CallbackQuery, callback_data: SegmentAction, app: App) -> None:
    async with app.sessions() as session:
        seg = await session.get(Segment, callback_data.id)
    # Короткий сегмент не опасен и публикуется как обычно, а пропущенный фильтром сериалов — только по решению
    review = not (seg and is_short_reason(seg.reason))
    changed = await queue_skipped(app, callback_data.id, review)
    answer = "Загружу, но опубликую только после вашего решения" if review else "Загружу"
    await query.answer(answer if changed else "Уже обработан")
    await app.refresh_segment(callback_data.id)
    if changed:
        app.wake.set()
