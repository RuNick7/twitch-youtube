from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    youtube_privacy: str = "private"
    youtube_language: str = "ru"
    youtube_category_id: str = "20"  # Gaming

    min_segment_sec: int = 120
    upload_chunk_mb: int = 32
    upload_limit_mbit: int = 250
    disk_reserve_gb: float = 2.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"
