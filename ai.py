"""ИИ-обогащение поверх правил через внутренний шлюз RMR (OpenAI-совместимый).

Конфиг через env: RMR_GATEWAY_URL (base_url), RMR_API_KEY, RMR_MODEL.
Деградация: если ключей нет или вызов упал — возвращает пустой dict, пайплайн продолжает
работать на одних правилах.
"""
from __future__ import annotations

import json
import os
from typing import Any

def _provider() -> str:
    return (os.environ.get("AI_PROVIDER") or "rmr").strip().lower()


def active_config() -> dict[str, str]:
    """Настройки активного провайдера ИИ-шлюза (выбор через AI_PROVIDER=rmr|openrouter)."""
    if _provider() == "openrouter":
        return {
            "provider": "openrouter",
            "base_url": os.environ.get("OPENROUTER_GATEWAY_URL", "https://openrouter.ai/api/v1"),
            "api_key": os.environ.get("OPENROUTER_API_KEY", ""),
            "model": os.environ.get("OPENROUTER_MODEL", "openai/gpt-oss-120b:free"),
        }
    return {
        "provider": "rmr",
        "base_url": os.environ.get("RMR_GATEWAY_URL", ""),
        "api_key": os.environ.get("RMR_API_KEY", ""),
        "model": os.environ.get("RMR_MODEL", "gpt-oss-120b"),
    }


MODEL = active_config()["model"]
TEXT_BUDGET = 30_000  # символов документации, отдаваемых модели

SYSTEM_PROMPT = (
    "Ты аналитик пресейла телеком-оператора. По карточке закупки и тексту документации "
    "выдели практичную выжимку для менеджера. Отвечай строго JSON-объектом со схемой: "
    '{"summary": str, "requirements": [str], "risks": [str], '
    '"suggested_domain": str, "confidence": str, "deadline_note": str}. '
    "summary — 2-3 предложения о сути закупки. requirements — ключевые требования к поставщику/услуге. "
    "risks — риски и подводные камни из документации, которых не видно по карточке. "
    "suggested_domain — один из: МОБ, ФИКС, SI, М2М, BigData, Cloud, SBVAS или 'не определён'. "
    "confidence — 'высокая'/'средняя'/'низкая'. deadline_note — комментарий по срокам подачи. "
    "Пиши по-русски, кратко, без воды."
)

EMPTY: dict[str, Any] = {}


def build_client(max_retries: int = 2, timeout: float = 40):
    """OpenAI-совместимый клиент активного провайдера (RMR или OpenRouter) или None."""
    c = active_config()
    if not c["base_url"] or not c["api_key"]:
        return None
    try:
        from openai import OpenAI
    except Exception:
        return None
    headers = {"X-Title": "Tender Radar"} if c["provider"] == "openrouter" else {}
    return OpenAI(base_url=c["base_url"], api_key=c["api_key"],
                  max_retries=max_retries, timeout=timeout, default_headers=headers)


def _client():
    return build_client()


def is_configured() -> bool:
    c = active_config()
    return bool(c["base_url"] and c["api_key"])


def ai_enrich(tender: dict[str, Any], full_text: str) -> dict[str, Any]:
    """Возвращает ИИ-поля или {} при недоступности шлюза/ошибке."""
    client = _client()
    if client is None:
        return EMPTY

    card = (
        f"Номер: {tender.get('number', '')}\n"
        f"Наименование: {tender.get('title', '')}\n"
        f"Заказчик: {tender.get('customer', '')}\n"
        f"Способ закупки: {tender.get('method', '')}\n"
        f"НМЦК: {tender.get('price', '')}\n"
        f"ОКПД2: {tender.get('okpd2', '')}\n"
        f"Дата размещения: {tender.get('publish_date', '')}\n"
        f"Окончание подачи: {tender.get('deadline', '')}\n"
        f"Закон: {tender.get('law', '')}\n"
    )
    docs = (full_text or "")[:TEXT_BUDGET]
    user_prompt = f"КАРТОЧКА ЗАКУПКИ:\n{card}\n\nТЕКСТ ДОКУМЕНТАЦИИ:\n{docs}"

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        content = response.choices[0].message.content or "{}"
        data = json.loads(content)
    except Exception as exc:  # сеть/таймаут/невалидный JSON — деградируем на правила
        print(f"  [ai] пропуск ИИ для {tender.get('number', '')}: {exc.__class__.__name__}: {exc}")
        return EMPTY

    return {
        "summary": str(data.get("summary", "")),
        "requirements": [str(x) for x in data.get("requirements", []) if x],
        "risks": [str(x) for x in data.get("risks", []) if x],
        "suggested_domain": str(data.get("suggested_domain", "")),
        "confidence": str(data.get("confidence", "")),
        "deadline_note": str(data.get("deadline_note", "")),
    }
