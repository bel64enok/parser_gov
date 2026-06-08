"""ИИ-обогащение поверх правил через внутренний шлюз RMR (OpenAI-совместимый).

Конфиг через env: RMR_GATEWAY_URL (base_url), RMR_API_KEY, RMR_MODEL.
Деградация: если ключей нет или вызов упал — возвращает пустой dict, пайплайн продолжает
работать на одних правилах.
"""
from __future__ import annotations

import json
import os
from typing import Any

MODEL = os.environ.get("RMR_MODEL", "gpt-oss-120b")
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


def _client():
    base_url = os.environ.get("RMR_GATEWAY_URL")
    api_key = os.environ.get("RMR_API_KEY")
    if not base_url or not api_key:
        return None
    try:
        from openai import OpenAI
    except Exception:
        return None
    return OpenAI(base_url=base_url, api_key=api_key)


def is_configured() -> bool:
    return bool(os.environ.get("RMR_GATEWAY_URL") and os.environ.get("RMR_API_KEY"))


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
