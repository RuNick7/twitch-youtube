"""Запросы к Claude Haiku 5.5: ответ строго по JSON-схеме, учёт токенов и цены.

Ключ берётся из ANTHROPIC_API_KEY. Подписка Claude (Pro, Max) для API не годится: нужен ключ из console.anthropic.com
с предоплатой.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

try:
    import anthropic
except ImportError:  # пока запросы не идут, SDK не нужен (юнит-тесты работают с подставным клиентом)
    anthropic = None

log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"
LONG_PROMPT_TOKENS = 100_000
# $ за миллион токенов (вход, выход): у Haiku 5.5 запрос длиннее 100 тыс. токенов в пять раз дороже
PRICES = {MODEL: {"short": (0.10, 0.50), "long": (0.50, 2.50)}}


class ClaudeError(RuntimeError):
    pass


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd


def request_cost(input_tokens: int, output_tokens: int, model: str = MODEL) -> float:
    prices = PRICES.get(model, PRICES[MODEL])
    rate_in, rate_out = prices["long" if input_tokens > LONG_PROMPT_TOKENS else "short"]
    return (input_tokens * rate_in + output_tokens * rate_out) / 1_000_000


def make_client(api_key: str):
    """Асинхронный клиент Anthropic. Повторы при 429 и сбоях сервера делает сам SDK."""
    if anthropic is None:
        raise ClaudeError("не установлен пакет anthropic (pip install -r requirements.txt)")
    return anthropic.AsyncAnthropic(api_key=api_key, max_retries=3, timeout=300.0)


def _explain(exc: Exception) -> ClaudeError:
    if anthropic is not None:
        if isinstance(exc, anthropic.AuthenticationError):
            return ClaudeError("Anthropic не принял ключ: проверьте ANTHROPIC_API_KEY")
        if isinstance(exc, anthropic.PermissionDeniedError):
            return ClaudeError(f"у ключа Anthropic нет доступа к модели: {exc.message}")
        if isinstance(exc, anthropic.RateLimitError):
            return ClaudeError("Anthropic ограничил частоту запросов, повторы не помогли")
        if isinstance(exc, anthropic.BadRequestError):
            # Сюда же приходит «Your credit balance is too low»: баланс на console.anthropic.com закончился
            return ClaudeError(f"Anthropic отклонил запрос: {exc.message}")
        if isinstance(exc, anthropic.APIStatusError):
            return ClaudeError(f"Anthropic ответил HTTP {exc.status_code}: {exc.message}")
        if isinstance(exc, anthropic.APIConnectionError):
            return ClaudeError("нет связи с Anthropic")
    return ClaudeError(f"запрос к Claude не удался: {exc}")


async def ask_json(
    client: Any,
    *,
    system: str,
    user: str,
    schema: dict,
    model: str = MODEL,
    max_tokens: int = 8000,
    effort: str = "low",
) -> tuple[dict, Usage]:
    """Один запрос: ответ гарантированно по schema (structured outputs). Возвращает данные и расход.

    effort — глубина размышлений Haiku 5.5 (low дешевле всего, по умолчанию у модели medium). Размышления
    входят в max_tokens, поэтому он с запасом. Температуру и другие параметры сэмплирования у этой модели
    задавать нельзя.
    """
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except Exception as exc:  # noqa: BLE001 — все ошибки SDK приводятся к понятному сообщению
        raise _explain(exc) from exc

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise ClaudeError(f"Claude отказался отвечать ({getattr(details, 'category', None) or 'причина не указана'})")
    if response.stop_reason == "max_tokens":
        raise ClaudeError(f"ответ обрезан на {max_tokens} токенах: участок слишком большой или max_tokens мал")
    text = next((block.text for block in response.content if block.type == "text"), None)
    if text is None:
        raise ClaudeError("в ответе Claude нет текста")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ClaudeError("Claude вернул не JSON") from exc
    if not isinstance(data, dict):
        raise ClaudeError("Claude вернул не объект JSON")

    usage = response.usage
    prompt = (
        usage.input_tokens
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    )
    return data, Usage(1, prompt, usage.output_tokens, request_cost(prompt, usage.output_tokens, model))
