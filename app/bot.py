"""Команды и кнопки бота. Всё, кроме /start, доступно только владельцу."""

from __future__ import annotations

import logging
import shutil
from html import escape

import httpx
from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from .context import App
from .db import Status, Vod, find_streamer, get_streamer
from .segments import parse_vod_id
from .service import IngestError, ingest_vod, set_status, status_counts
from .tools import ToolError, fetch_vod_info
from .ui import APPROVE, REJECT, REJECT_NO, REJECT_YES, RETRY, SegmentAction, confirm_reject_keyboard
from .worker import is_paused, set_paused
from .youtube import DeviceCode, YouTubeError

log = logging.getLogger(__name__)

HELP = (
    "/process &lt;ссылка на VOD&gt; — нарезать VOD и прислать сегменты на проверку\n"
    "/youtube — подключить YouTube-канал\n"
    "/status — состояние обработки\n"
    "/pause, /resume — остановить и продолжить обработку"
)

STATUS_LABELS = (
    (Status.PENDING, "ждут проверки"),
    (Status.APPROVED, "в очереди"),
    (Status.DOWNLOADING, "скачиваются"),
    (Status.UPLOADING, "загружаются"),
    (Status.UPLOADED, "загружены"),
    (Status.FAILED, "с ошибкой"),
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
    await progress.delete()


@owner.message(Command("youtube"))
async def connect_youtube(message: Message, app: App) -> None:
    if not (app.settings.google_client_id and app.settings.google_client_secret):
        await message.answer("Сначала заполните GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET в .env и перезапустите контейнер.")
        return
    try:
        code = await app.youtube.start_device_flow()
    except (YouTubeError, httpx.HTTPError) as exc:
        await message.answer(f"🔴 Google не выдал код: {escape(str(exc))}")
        return
    prompt = await message.answer(
        f"Откройте {code.verification_url} и введите код <code>{code.user_code}</code>\n"
        f"Код действует {code.expires_in // 60} мин. Войдите в Google-аккаунт, "
        "которому принадлежит канал, и выберите нужный канал."
    )
    app.spawn(_finish_youtube(app, code, prompt))


async def _finish_youtube(app: App, code: DeviceCode, prompt: Message) -> None:
    try:
        refresh_token = await app.youtube.finish_device_flow(code)
        channel_id, title = await app.youtube.my_channel(refresh_token)
    except (YouTubeError, httpx.HTTPError) as exc:
        await prompt.edit_text(f"🔴 YouTube не подключён: {escape(str(exc))}")
        return
    async with app.sessions() as session, session.begin():
        streamer = await get_streamer(session, app.settings.twitch_channel)
        streamer.youtube_token = app.vault.encrypt(refresh_token)
        streamer.youtube_channel_id = channel_id
        streamer.youtube_channel_title = title
    await prompt.edit_text(f"✅ Подключён канал «{escape(title)}»")


@owner.message(Command("status"))
async def show_status(message: Message, app: App) -> None:
    counts = await status_counts(app)
    async with app.sessions() as session:
        streamer = await find_streamer(session, app.settings.twitch_channel)
    youtube = (
        f"«{escape(streamer.youtube_channel_title or '')}»"
        if streamer and streamer.youtube_token
        else "не подключён, выполните /youtube"
    )
    disk = shutil.disk_usage(app.settings.work_dir)
    paused = await is_paused(app)
    await message.answer(
        "\n".join(
            [
                f"Twitch: {escape(app.settings.twitch_channel)}",
                f"YouTube: {youtube}",
                f"Обработка: {'⏸ на паузе, /resume — продолжить' if paused else '▶️ работает'}",
                "Сегменты: " + ", ".join(f"{label} {counts.get(key, 0)}" for key, label in STATUS_LABELS),
                f"Диск: свободно {disk.free / 1024**3:.0f} из {disk.total / 1024**3:.0f} ГБ",
                f"yt-dlp: {escape(app.ytdlp_version)}",
            ]
        )
    )


@owner.message(Command("pause"))
async def pause(message: Message, app: App) -> None:
    await set_paused(app, True)
    await message.answer("⏸ Обработка остановлена: текущий сегмент доделается, следующие ждут /resume.")


@owner.message(Command("resume"))
async def resume(message: Message, app: App) -> None:
    await set_paused(app, False)
    await message.answer("▶️ Обработка продолжена.")


@owner.callback_query(SegmentAction.filter(F.action == APPROVE))
async def approve(query: CallbackQuery, callback_data: SegmentAction, app: App) -> None:
    changed = await set_status(app, callback_data.id, (Status.PENDING,), Status.APPROVED)
    await query.answer("Одобрено" if changed else "Уже обработан")
    await app.refresh_segment(callback_data.id)
    if changed:
        app.wake.set()


@owner.callback_query(SegmentAction.filter(F.action == REJECT))
async def ask_reject(query: CallbackQuery, callback_data: SegmentAction) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=confirm_reject_keyboard(callback_data.id))


@owner.callback_query(SegmentAction.filter(F.action == REJECT_YES))
async def reject(query: CallbackQuery, callback_data: SegmentAction, app: App) -> None:
    changed = await set_status(app, callback_data.id, (Status.PENDING,), Status.REJECTED)
    await query.answer("Отклонено" if changed else "Уже обработан")
    await app.refresh_segment(callback_data.id)


@owner.callback_query(SegmentAction.filter(F.action == REJECT_NO))
async def cancel_reject(query: CallbackQuery, callback_data: SegmentAction, app: App) -> None:
    await query.answer()
    await app.refresh_segment(callback_data.id)


@owner.callback_query(SegmentAction.filter(F.action == RETRY))
async def retry(query: CallbackQuery, callback_data: SegmentAction, app: App) -> None:
    changed = await set_status(app, callback_data.id, (Status.FAILED,), Status.APPROVED)
    await query.answer("Повторяю" if changed else "Уже обработан")
    await app.refresh_segment(callback_data.id)
    if changed:
        app.wake.set()
