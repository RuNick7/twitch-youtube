from __future__ import annotations

from pathlib import Path
from typing import List, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from .segments import split_list


class Settings(BaseSettings):
    """Настройки из переменных окружения (файл .env)."""

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    telegram_bot_token: str
    # Пока не задан, бот в ответ на /start присылает ID собеседника и больше ничего не делает
    telegram_owner_id: int | None = None

    # Логин стримера: twitch.tv/<логин>
    twitch_channel: str

    # OAuth-клиент Google типа «TVs and Limited Input devices»
    google_client_id: str = ""
    google_client_secret: str = ""

    # Ключ шифрования токенов в базе (Fernet)
    secret_key: str

    data_dir: Path = Path("/data")
    tz: str = "UTC"

    # Публикация: ролик без предупреждений становится публичным через publish_delay_min
    # после того, как YouTube его обработал (время на проверку Content ID)
    auto_publish: bool = True
    publish_privacy: Literal["public", "unlisted"] = "public"
    publish_delay_min: int = 60
    # Сколько дней после публикации следить, не заблокировал ли YouTube ролик
    monitor_days: int = 3

    youtube_language: str = "ru"
    youtube_category_id: str = "20"  # Gaming

    # Слежение за каналом: как часто проверять и сколько ждать после конца стрима
    watch_interval_sec: int = 300
    watch_grace_min: int = 10

    min_segment_sec: int = 120

    # Не загружать: сегменты этих категорий, если в названии стрима есть одно из слов
    # (так на стримах смотрят сериалы и фильмы), и категории из skip_categories всегда
    skip_title_keywords: str = "во все тяжкие,звоните солу,сериал,серия,фильм,кино"
    skip_title_categories: str = "Just Chatting"
    skip_categories: str = "Watch Party"
    # Загружать, но публиковать только после решения в Telegram
    warn_categories: str = "Slots,Virtual Casino,Poker"

    upload_chunk_mb: int = 32
    upload_limit_mbit: int = 200

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def keywords(self) -> List[str]:
        return split_list(self.skip_title_keywords)

    @property
    def title_categories(self) -> List[str]:
        return split_list(self.skip_title_categories)

    @property
    def skipped_categories(self) -> List[str]:
        return split_list(self.skip_categories)

    @property
    def warned_categories(self) -> List[str]:
        return split_list(self.warn_categories)
