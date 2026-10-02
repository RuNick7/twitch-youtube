"""Лимиты YouTube на загрузку: когда пробовать снова.

Лимит канала (uploadLimitExceeded) YouTube снимает в течение суток, но не сообщает когда,
поэтому бот пробует раз в несколько часов. Квота проекта общая для всех каналов
и сбрасывается в полночь по тихоокеанскому времени.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

CHANNEL_LIMIT = "uploadLimitExceeded"
RATE_LIMIT = "rateLimitExceeded"
PROJECT_LIMITS = ("quotaExceeded", "dailyLimitExceeded")

CHANNEL_RETRY = timedelta(hours=3)
RATE_RETRY = timedelta(minutes=15)
QUOTA_TZ = ZoneInfo("America/Los_Angeles")
QUOTA_MARGIN = timedelta(minutes=5)


def next_quota_reset(now: datetime) -> datetime:
    """Ближайшая полночь по тихоокеанскому времени с небольшим запасом, в часовом поясе now."""
    local = now.astimezone(QUOTA_TZ)
    midnight = datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=QUOTA_TZ)
    return (midnight + QUOTA_MARGIN).astimezone(now.tzinfo)


def retry_at(reason: str, now: datetime) -> datetime:
    """Когда снова пробовать загрузку после ошибки YouTube с этой причиной."""
    if reason in PROJECT_LIMITS:
        return next_quota_reset(now)
    if reason == RATE_LIMIT:
        return now + RATE_RETRY
    return now + CHANNEL_RETRY


def affects_everyone(reason: str) -> bool:
    """Квота проекта общая: если она кончилась, ждут загрузки всех стримеров."""
    return reason in PROJECT_LIMITS


def describe(reason: str | None) -> str:
    """Причина ожидания словами — для /status."""
    if reason == CHANNEL_LIMIT:
        return "исчерпан дневной лимит канала"
    if reason in PROJECT_LIMITS:
        return "кончилась суточная квота YouTube API"
    if reason == RATE_LIMIT:
        return "YouTube просит загружать реже"
    return f"лимит YouTube ({reason})"
