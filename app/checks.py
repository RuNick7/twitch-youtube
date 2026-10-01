"""Разбор состояния ролика из videos.list. Только стандартная библиотека.

Претензий Content ID и раздела «Проверки» из YouTube Studio в API нет. Видно
только то, что меняет доступность ролика: блокировку целиком или в отдельных
странах, отклонение, возрастное ограничение и сбои обработки.
"""

from __future__ import annotations

import re
from typing import List, Optional

from .segments import fmt_duration

REJECTION_REASONS = {
    "claim": "претензия правообладателя (Content ID)",
    "copyright": "нарушение авторских прав",
    "duplicate": "дубликат уже загруженного ролика",
    "inappropriate": "неприемлемый контент",
    "legal": "юридические причины",
    "length": "ролик слишком длинный",
    "termsOfUse": "нарушение условий использования",
    "trademark": "товарный знак",
    "uploaderAccountClosed": "канал закрыт",
    "uploaderAccountSuspended": "канал заблокирован",
}
FAILURE_REASONS = {
    "codec": "неподдерживаемый кодек",
    "conversion": "YouTube не смог перекодировать ролик",
    "emptyFile": "пустой файл",
    "invalidFile": "файл повреждён",
    "tooSmall": "файл слишком маленький",
    "uploadAborted": "загрузка прервана",
}

READY, PROCESSING, REJECTED, FAILED = "ready", "processing", "rejected", "failed"

_ISO_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?")


def parse_duration(value: Optional[str]) -> Optional[float]:
    """ISO 8601 из contentDetails.duration: «PT3H29M2S» → 12542."""
    match = _ISO_DURATION.fullmatch(value or "")
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = match.groups()
    return int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)


def processing_state(item: dict) -> str:
    status = item.get("status") or {}
    processing = (item.get("processingDetails") or {}).get("processingStatus")
    upload = status.get("uploadStatus")
    if upload == "rejected":
        return REJECTED
    if upload in ("failed", "deleted") or processing in ("failed", "terminated"):
        return FAILED
    if upload == "processed" or processing == "succeeded":
        return READY
    return PROCESSING


def failure_reason(item: dict) -> str:
    status = item.get("status") or {}
    if status.get("uploadStatus") == "rejected":
        reason = status.get("rejectionReason")
        return REJECTION_REASONS.get(reason, reason or "причина не указана")
    if status.get("uploadStatus") == "deleted":
        return "ролик удалён"
    if status.get("failureReason"):
        return FAILURE_REASONS.get(status["failureReason"], status["failureReason"])
    details = item.get("processingDetails") or {}
    return details.get("processingFailureReason") or "YouTube не смог обработать ролик"


def youtube_warnings(item: dict, expected_sec: Optional[float]) -> List[str]:
    """Что мешает публиковать ролик без решения человека."""
    warnings: List[str] = []
    details = item.get("contentDetails") or {}
    restriction = details.get("regionRestriction") or {}
    blocked = restriction.get("blocked") or []
    if blocked:
        shown = ", ".join(blocked[:10]) + (" и другие" if len(blocked) > 10 else "")
        warnings.append(f"заблокирован в {len(blocked)} странах: {shown}")
    if "allowed" in restriction:
        warnings.append(f"доступен только в {len(restriction['allowed'] or [])} странах")
    if (details.get("contentRating") or {}).get("ytRating") == "ytAgeRestricted":
        warnings.append("возрастное ограничение 18+")
    actual = parse_duration(details.get("duration"))
    if expected_sec and actual is not None and abs(actual - expected_sec) > max(30.0, expected_sec * 0.01):
        warnings.append(f"длительность {fmt_duration(actual)} вместо {fmt_duration(expected_sec)}")
    return warnings
