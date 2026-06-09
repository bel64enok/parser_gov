"""ReAct-агент стадии 2: извлечение структурированной карточки требований из комплекта
документов тендера.

Вместо обрезки всего текста в один промпт (как делал ai.ai_enrich) агент навигирует по
комплекту инструментами: смотрит список файлов, читает нужные по кускам, ищет факты,
сверяется со словарём доменов — и в конце вызывает submit_card. Петля написана руками на
OpenAI-совместимом `openai` SDK (RMR-шлюз, gpt-oss-120b).

Поддержка инструментов на шлюзе не гарантирована, поэтому есть два режима:
  • нативный function-calling (приоритетный);
  • текстовый JSON-action протокол через response_format=json_object (фолбэк).
Выбор делается автоматически: первый сбой нативного вызова → переключение на фолбэк.

Без шлюза/при ошибке возвращает status='error' с пустой карточкой — analyzer деградирует
на правила. Каждый факт карточки несёт цитату-источник (файл + фрагмент).
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

import ai  # переиспользуем MODEL / is_configured / построение клиента
from rules import DOMAIN_TRIGGERS, PRODUCT_CATALOG, evidence_snippet, lower_text, search_snippets

MODEL = ai.MODEL

MAX_STEPS = 16          # предел вызовов инструментов на тендер (упёрлись → частичная карточка)
CHUNK_CHARS = 6_000     # размер куска read_document
TENDER_TIMEOUT = 180    # сек, бюджет на один тендер
MAX_MATCHES = 6         # сколько совпадений отдаёт search_documents
DOMAINS = ["МОБ", "ФИКС", "SI", "М2М", "BigData", "Cloud", "SBVAS", "не определён"]
CONFIDENCE = ["высокая", "средняя", "низкая"]

# Режим стадии 2 (env AGENT_MODE): 'guided' — видимое чтение ключевых документов + один
# вызов извлечения (наглядно + качественно); 'oneshot' — один вызов по всему тексту;
# 'react' — модельная петля инструментов (наглядная навигация, но менее стабильна).
ONESHOT_TEXT_BUDGET = 40_000  # символов документации, отдаваемых в один вызов

def _agent_mode() -> str:
    return (os.environ.get("AGENT_MODE") or "oneshot").strip().lower()

# Переключатель режима на уровне процесса: None — не пробовали, True/False — выбран.
_NATIVE_TOOLS: bool | None = None


# ── Схема карточки и инструментов ──────────────────────────────────────────

_FACT = {
    "type": "object",
    "properties": {
        "value": {"type": "string", "description": "значение как в документе; '' если не найдено"},
        "source_file": {"type": "string", "description": "имя файла-источника"},
        "source_quote": {"type": "string", "description": "точная цитата из документа"},
    },
    "required": ["value"],
}

_SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {
            "type": "object",
            "properties": {"predmet": _FACT, "volume": _FACT, "territory": _FACT},
        },
        "commercial": {
            "type": "object",
            "properties": {
                "bid_security": _FACT, "contract_security": _FACT,
                "payment": _FACT, "penalties": _FACT,
            },
        },
        "deadlines": {
            "type": "object",
            "properties": {"service_period": _FACT, "contract_term": _FACT, "key_dates": _FACT},
        },
        "participant_requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement": {"type": "string"},
                    "present": {"type": "boolean"},
                    "source_file": {"type": "string"},
                    "source_quote": {"type": "string"},
                },
                "required": ["requirement", "present"],
            },
        },
        "technical": {
            "type": "object",
            "properties": {"sla": _FACT, "capacity": _FACT, "coverage": _FACT, "other": _FACT},
        },
        "domain": {"type": "string", "enum": DOMAINS},
        "summary": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": CONFIDENCE},
    },
    "required": ["subject", "summary", "domain", "confidence"],
}

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "Список документов тендера: индекс, имя файла, тип, размер в символах.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Читает один документ по кускам. chunk — номер куска с 0.",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "индекс документа из list_documents"},
                    "chunk": {"type": "integer", "description": "номер куска (с 0)", "default": 0},
                },
                "required": ["n"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "Ищет подстроку/ключевое слово по всем документам, возвращает фрагменты с именем файла.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_dictionary",
            "description": "Словарь доменов и продуктов Beeline (триггерные слова) — для согласованного домена.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_card",
            "description": "Финализация: заполненная структурированная карточка требований. Завершает работу.",
            "parameters": _SUBMIT_SCHEMA,
        },
    },
]

SYSTEM_PROMPT = (
    "Ты аналитик пресейла телеком-оператора Beeline. Цель — ПОЛНОСТЬЮ заполнить структурированную "
    "карточку требований по ПРИЛОЖЕННЫМ ДОКУМЕНТАМ. Не выдумывай: чего нет в тексте — оставь value "
    "пустым. Каждый факт сопровождай source_file и source_quote (точная цитата из документа).\n\n"
    "Инструменты: list_documents, read_document(n, chunk), search_documents(query), "
    "lookup_dictionary, submit_card.\n\n"
    "ПОРЯДОК РАБОТЫ:\n"
    "1) list_documents — посмотри список файлов и их размеры.\n"
    "2) ПРОЧИТАЙ ключевые документы через read_document (проект контракта, ТЗ, обоснование НМЦ, "
    "извещение) — по кускам, пока не увидишь нужные факты. Чтение документов ОБЯЗАТЕЛЬНО: карточка "
    "строится из их текста, а не из метаданных закупки.\n"
    "3) search_documents — точечно под конкретный факт. Ищи ОДНИМ коротким ключевым словом (корень), "
    "без длинных фраз: «обеспечение», «объём», «срок», «штраф», «лицензия». Пусто → смени слово или "
    "читай документ напрямую, НЕ повторяй ту же фразу.\n"
    "4) lookup_dictionary — сверь домен.\n"
    "5) submit_card — заполни КАЖДОЕ поле, для которого есть данные из прочитанного: обеспечение "
    "заявки и контракта, объём/количество, территорию, оплату, штрафы, сроки и ключевые даты, "
    "требования к участнику (лицензии/квалификация), SLA и тех. условия, домен, summary, риски.\n\n"
    "ВАЖНО: НЕ сабмить полупустую карточку, пока не прочитал ключевые документы. Стартовый список "
    "НАЙДЕНО — подсказка, но детали бери из прочитанного текста. Бюджет шагов ограничен: не зацикливайся "
    "на поиске — читай документы и заполняй поля. Отвечай по-русски."
)

ONESHOT_SYSTEM = (
    "Ты аналитик пресейла телеком-оператора Beeline. По карточке закупки и тексту документации "
    "заполни СТРУКТУРИРОВАННУЮ карточку требований ОДНИМ вызовом submit_card. "
    "Извлеки: предмет, объём/количество, территорию; обеспечение заявки и контракта, оплату, "
    "штрафы/неустойки; сроки оказания и исполнения, ключевые даты; требования к участнику "
    "(лицензии, квалификация) списком present=true/false; SLA, ёмкость/скорости, покрытие, "
    "прочие тех. условия; домен (один из словаря), краткое summary, риски, уверенность.\n"
    "Правила: НЕ выдумывай — если факта нет в тексте, оставь value пустым (''). Для каждого "
    "факта по возможности укажи source_file (имя файла) и source_quote (точную цитату из текста). "
    "Отвечай ТОЛЬКО вызовом submit_card, по-русски."
)


# ── Хранилище документов тендера для инструментов ──────────────────────────

class DocStore:
    """Документы одного тендера в виде, удобном инструментам агента."""

    def __init__(self, docs: list[dict[str, Any]]):
        # docs: [{filename, text}] — только с непустым текстом
        self.docs = [d for d in docs if (d.get("text") or "").strip()]

    def list(self) -> str:
        if not self.docs:
            return "Документов с распознанным текстом нет."
        lines = [
            f"[{i}] {d['filename']} · {len(d['text'])} симв."
            for i, d in enumerate(self.docs)
        ]
        return "\n".join(lines)

    def read(self, n: int, chunk: int = 0) -> str:
        if not (0 <= n < len(self.docs)):
            return f"Нет документа с индексом {n}. Используй list_documents."
        text = self.docs[n]["text"]
        start = max(0, chunk) * CHUNK_CHARS
        piece = text[start:start + CHUNK_CHARS]
        if not piece:
            return f"Документ [{n}] {self.docs[n]['filename']}: куска {chunk} нет (конец файла)."
        total_chunks = (len(text) + CHUNK_CHARS - 1) // CHUNK_CHARS
        header = f"[{n}] {self.docs[n]['filename']} · кусок {chunk + 1}/{total_chunks}\n"
        return header + piece

    def search(self, query: str) -> str:
        q = (query or "").strip()
        if not q:
            return "Пустой запрос."
        # Морфолого-толерантный поиск по корням токенов, скан ВСЕХ документов,
        # ранжирование по числу совпавших токенов запроса.
        scored: list[tuple[int, int, str, list[str]]] = []
        for i, d in enumerate(self.docs):
            score, snips = search_snippets(d["text"], query, radius=180, max_snippets=2)
            if score > 0:
                scored.append((score, i, d["filename"], snips))
        if not scored:
            return (f"По запросу «{query}» совпадений не найдено. "
                    "Попробуй ОДНО короткое ключевое слово (без склонений) "
                    "или прочитай документ напрямую через read_document.")
        scored.sort(key=lambda x: -x[0])
        lines: list[str] = []
        for _score, i, fname, snips in scored:
            for sn in snips:
                lines.append(f"[{i}] {fname}: …{sn}…")
                if len(lines) >= MAX_MATCHES:
                    break
            if len(lines) >= MAX_MATCHES:
                break
        return "\n".join(lines)


def _dictionary() -> str:
    domains = "; ".join(f"{k}: {', '.join(v[:6])}" for k, v in DOMAIN_TRIGGERS.items())
    products = "; ".join(f"{k}: {', '.join(v[:4])}" for k, v in PRODUCT_CATALOG.items())
    return f"ДОМЕНЫ → {domains}\n\nПРОДУКТЫ → {products}"


def _human(tool: str, args: dict[str, Any], store: "DocStore") -> str:
    """Человекочитаемый текущий шаг для живого прогресса."""
    if tool == "submit_card":
        return "формирует карточку"
    if tool == "list_documents":
        return "смотрит список документов"
    if tool == "read_document":
        raw_n = args.get("n", args.get("index", ""))
        n = int(raw_n) if str(raw_n).lstrip("-").isdigit() else -1
        name = store.docs[n]["filename"] if 0 <= n < len(store.docs) else f"#{raw_n or '?'}"
        return f"читает {name}"
    if tool == "search_documents":
        return f"ищет «{str(args.get('query', '')).strip()}»"
    if tool == "lookup_dictionary":
        return "сверяется со словарём доменов"
    return "рассуждает"


def _emit(on_step: Callable[..., None] | None, tool: str, args: dict[str, Any], store: "DocStore",
          *, step_count: int = 0, tool_calls: int = 0, tokens: int = 0) -> None:
    _emit_text(on_step, _human(tool, args, store),
               step_count=step_count, tool_calls=tool_calls, tokens=tokens)


def _emit_text(on_step: Callable[..., None] | None, text: str,
               *, step_count: int = 0, tool_calls: int = 0, tokens: int = 0) -> None:
    """Живой прогресс: текст текущего действия + метрики (шаги/вызовы/токены)."""
    if on_step:
        try:
            on_step(text, step_count, tool_calls, tokens)
        except Exception:
            pass


# ── Нормализация карточки в секции для UI ──────────────────────────────────

_SECTION_FIELDS = [
    ("Предмет", "subject", [
        ("predmet", "Предмет закупки"),
        ("volume", "Объём / количество"),
        ("territory", "Территория оказания"),
    ]),
    ("Коммерческие условия", "commercial", [
        ("bid_security", "Обеспечение заявки"),
        ("contract_security", "Обеспечение контракта"),
        ("payment", "Оплата"),
        ("penalties", "Штрафы / неустойки"),
    ]),
    ("Сроки", "deadlines", [
        ("service_period", "Период оказания услуг"),
        ("contract_term", "Срок исполнения контракта"),
        ("key_dates", "Ключевые даты"),
    ]),
    ("Технические требования", "technical", [
        ("sla", "SLA"),
        ("capacity", "Скорости / ёмкость"),
        ("coverage", "Покрытие / география"),
        ("other", "Прочие тех. условия"),
    ]),
]


def _step_ref_for(steps: list[dict[str, Any]], filename: str) -> int | None:
    """Находит шаг, на котором агент видел этот файл (read/search) — для якоря цитаты."""
    if not filename:
        return None
    target = lower_text(filename)
    found = None
    for step in steps:
        obs = lower_text(str(step.get("observation", "")))
        if target and target in obs:
            found = step.get("idx")
    return found


def _fact(label: str, raw: Any, steps: list[dict[str, Any]]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    value = str(raw.get("value", "") or "").strip()
    filename = str(raw.get("source_file", "") or "").strip()
    quote = str(raw.get("source_quote", "") or "").strip()
    found = bool(value)
    return {
        "label": label,
        "value": value if found else "не найдено в документации",
        "found": found,
        "source": {"filename": filename, "quote": quote} if (found and (filename or quote)) else None,
        "step_ref": _step_ref_for(steps, filename) if found else None,
    }


def normalize_card(raw: dict[str, Any], steps: list[dict[str, Any]], limit_reached: bool) -> dict[str, Any]:
    raw = raw or {}
    sections = []
    for title, key, fields in _SECTION_FIELDS:
        group = raw.get(key) if isinstance(raw.get(key), dict) else {}
        facts = [_fact(label, group.get(fkey), steps) for fkey, label in fields]
        sections.append({"title": title, "facts": facts})

    # Требования к участнику — отдельная секция со списком пунктов go/no-go
    reqs = raw.get("participant_requirements")
    req_items = []
    if isinstance(reqs, list):
        for r in reqs:
            if not isinstance(r, dict):
                continue
            text = str(r.get("requirement", "") or "").strip()
            if not text:
                continue
            filename = str(r.get("source_file", "") or "").strip()
            quote = str(r.get("source_quote", "") or "").strip()
            req_items.append({
                "label": text,
                "present": bool(r.get("present", False)),
                "source": {"filename": filename, "quote": quote} if (filename or quote) else None,
                "step_ref": _step_ref_for(steps, filename),
            })

    domain = str(raw.get("domain", "") or "").strip() or "не определён"
    confidence = str(raw.get("confidence", "") or "").strip() or "низкая"
    return {
        "sections": sections,
        "participant_requirements": req_items,
        "domain": domain,
        "summary": str(raw.get("summary", "") or "").strip(),
        "risks": [str(x).strip() for x in (raw.get("risks") or []) if str(x).strip()],
        "confidence": confidence,
        "limit_reached": bool(limit_reached),
    }


# ── Исполнение инструментов ────────────────────────────────────────────────

def _exec_tool(name: str, args: dict[str, Any], store: DocStore) -> str:
    try:
        if name == "list_documents":
            return store.list()
        if name == "read_document":
            n = args.get("n", args.get("index", 0))  # модель иногда шлёт index вместо n
            return store.read(int(n), int(args.get("chunk", 0) or 0))
        if name == "search_documents":
            return store.search(str(args.get("query", "")))
        if name == "lookup_dictionary":
            return _dictionary()
    except Exception as exc:  # защита от кривых аргументов модели
        return f"Ошибка инструмента {name}: {exc.__class__.__name__}: {exc}"
    return f"Неизвестный инструмент: {name}"


def _evidence_digest(evidence: list[dict[str, Any]] | None, limit: int = 12) -> str:
    """Компактный стартовый список улик из rules.collect_evidence для фронт-загрузки.

    Даёт модели готовые фрагменты (домен/способ/ОКПД/сроки), чтобы не искать заново.
    """
    if not evidence:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for item in evidence:
        label = str(item.get("label", "")).strip()
        snippet = str(item.get("snippet", "")).strip()
        if not snippet:
            continue
        key = f"{label}:{snippet[:60]}"
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"• {label}: …{snippet[:200]}…")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


def _card_metadata(tender: dict[str, Any], evidence_digest: str = "") -> str:
    found = f"\nНАЙДЕНО (стартовые фрагменты — используй, не ищи заново):\n{evidence_digest}\n" if evidence_digest else ""
    return (
        f"КАРТОЧКА ЗАКУПКИ:\n"
        f"Номер: {tender.get('number', '')}\n"
        f"Наименование: {tender.get('title', '')}\n"
        f"Заказчик: {tender.get('customer', '')}\n"
        f"Способ закупки: {tender.get('method', '')}\n"
        f"НМЦК: {tender.get('price', '')}\n"
        f"ОКПД2: {tender.get('okpd2', '')}\n"
        f"Дата размещения: {tender.get('publish_date', '')}\n"
        f"Окончание подачи: {tender.get('deadline', '')}\n"
        f"Закон: {tender.get('law', '')}\n"
        f"{found}\n"
        "Извлеки структурированную карточку требований. Начни с list_documents."
    )


# ── One-shot извлечение (один вызов submit_card по собранному тексту) ───────

def _assemble_text(store: DocStore, budget: int = ONESHOT_TEXT_BUDGET) -> str:
    """Склеивает текст документов (с заголовками-именами) до лимита символов."""
    parts: list[str] = []
    total = 0
    for d in store.docs:
        block = f"# {d['filename']}\n{d['text'] or ''}"
        if total + len(block) > budget:
            block = block[: max(0, budget - total)]
        if block:
            parts.append(block)
            total += len(block)
        if total >= budget:
            break
    return "\n\n".join(parts)


def _meta_block(tender: dict[str, Any], evidence_digest: str) -> str:
    found = f"\nНАЙДЕНО (стартовые фрагменты):\n{evidence_digest}\n" if evidence_digest else ""
    return (
        "КАРТОЧКА ЗАКУПКИ:\n"
        f"Номер: {tender.get('number', '')}\n"
        f"Наименование: {tender.get('title', '')}\n"
        f"Заказчик: {tender.get('customer', '')}\n"
        f"Способ закупки: {tender.get('method', '')}\n"
        f"НМЦК: {tender.get('price', '')}\n"
        f"ОКПД2: {tender.get('okpd2', '')}\n"
        f"Дата размещения: {tender.get('publish_date', '')}\n"
        f"Окончание подачи: {tender.get('deadline', '')}\n"
        f"Закон: {tender.get('law', '')}\n"
        f"{found}"
    )


def _oneshot_user(tender: dict[str, Any], store: DocStore, evidence_digest: str) -> str:
    text = _assemble_text(store) or "(документы без распознанного текста — заполни по карточке)"
    return f"{_meta_block(tender, evidence_digest)}\nТЕКСТ ДОКУМЕНТАЦИИ:\n{text}"


_ONESHOT_JSON_HINT = (
    "\n\nИнструментов-функций нет. Верни СТРОГО JSON-объект карточки со схемой: "
    "subject{predmet,volume,territory}, commercial{bid_security,contract_security,payment,penalties}, "
    "deadlines{service_period,contract_term,key_dates}, technical{sla,capacity,coverage,other}, "
    "participant_requirements:[{requirement,present,source_file,source_quote}], domain, summary, "
    "risks:[...], confidence. Каждый факт = {value, source_file, source_quote}."
)


def _submit_extract(client, model: str, user_text: str) -> tuple[dict[str, Any] | None, int]:
    """Один LLM-вызов извлечения карточки: форсируем submit_card; фолбэк — строгий JSON.
    Возвращает (card | None, tokens). card=None только при полном сбое обоих вызовов."""
    submit_tool = _TOOLS[-1]  # submit_card
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": ONESHOT_SYSTEM}, {"role": "user", "content": user_text}],
            tools=[submit_tool],
            tool_choice={"type": "function", "function": {"name": "submit_card"}},
            temperature=0.1, timeout=90,
        )
        tokens = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
        calls = resp.choices[0].message.tool_calls or []
        if calls:
            try:
                return json.loads(calls[0].function.arguments or "{}"), tokens
            except json.JSONDecodeError:
                return {}, tokens
    except Exception as exc:
        print(f"  [agent] forced submit недоступен ({exc.__class__.__name__}) — JSON-режим")

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": ONESHOT_SYSTEM + _ONESHOT_JSON_HINT},
                      {"role": "user", "content": user_text}],
            response_format={"type": "json_object"}, temperature=0.1, timeout=90,
        )
        tokens = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
        return json.loads(resp.choices[0].message.content or "{}"), tokens
    except Exception as exc:
        return None, 0


def _run_oneshot(client, model: str, tender: dict[str, Any], store: DocStore, deadline: float,
                 on_step: Callable[..., None] | None = None, evidence_digest: str = "") -> dict[str, Any]:
    """Один вызов: модель видит собранный текст документов и заполняет карточку (быстро,
    1 запрос — влезает в rate-limit; качество выше многошаговой петли)."""
    _emit_text(on_step, "извлекает карточку (one-shot)…", step_count=0, tool_calls=0, tokens=0)
    card, tokens = _submit_extract(client, model, _oneshot_user(tender, store, evidence_digest))
    if card is None:
        return _result("error", None, [], 0, 0, False) | {"error": "извлечение не удалось"}
    steps = [{"idx": 1, "kind": "final", "thought": "", "tool": "submit_card",
              "args": {}, "observation": "Карточка извлечена (one-shot)"}]
    _emit(on_step, "submit_card", {}, store, step_count=1, tool_calls=1, tokens=tokens)
    return _result("done", card, steps, 1, tokens, False)


# Имена-подсказки: документы, где обычно лежат факты карточки (для видимого чтения)
_READ_PRIORITY = ["контракт", "техническ", " тз", "тз ", "задание", "обоснован", "нмц",
                  "извещен", "требован", "спецификац", "услов"]


def _pick_docs_to_read(store: DocStore, max_docs: int = 6) -> list[int]:
    """Выбирает документы для чтения: сперва ключевые по имени (контракт/ТЗ/обоснование…),
    затем остальные по порядку. Детерминированно — без зацикливания модели."""
    def score(i: int) -> int:
        name = lower_text(store.docs[i]["filename"])
        return sum(1 for k in _READ_PRIORITY if k in name)
    order = sorted(range(len(store.docs)), key=lambda i: (-score(i), i))
    return order[:max_docs]


def _run_guided(client, model: str, tender: dict[str, Any], store: DocStore, deadline: float,
                on_step: Callable[..., None] | None = None, evidence_digest: str = "") -> dict[str, Any]:
    """Видимое чтение документов + сильное извлечение одним вызовом.

    Детерминированно читает ключевые документы (трейс показывает «читает X» — наглядная
    демонстрация работы с приложенными файлами), затем делает один submit_card по
    прочитанному тексту → one-shot-качество. LLM-вызов один (быстро, влезает в rate-limit),
    но в отличие от one-shot процесс чтения документов виден в трейсе.
    """
    steps: list[dict[str, Any]] = []
    _emit(on_step, "list_documents", {}, store, step_count=0, tool_calls=0, tokens=0)
    steps.append({"idx": 1, "kind": "tool", "thought": "", "tool": "list_documents",
                  "args": {}, "observation": store.list()})

    read_parts: list[str] = []
    total = 0
    per_doc = max(4_000, ONESHOT_TEXT_BUDGET // max(1, min(len(store.docs), 6)))
    for n in _pick_docs_to_read(store):
        if total >= ONESHOT_TEXT_BUDGET:
            break
        piece = (store.docs[n]["text"] or "")[:per_doc]
        if not piece.strip():
            continue
        fname = store.docs[n]["filename"]
        _emit(on_step, "read_document", {"n": n}, store,
              step_count=len(steps), tool_calls=len(steps), tokens=0)
        steps.append({"idx": len(steps) + 1, "kind": "tool", "thought": "",
                      "tool": "read_document", "args": {"n": n},
                      "observation": f"[{n}] {fname} · {len(store.docs[n]['text'])} симв.\n"
                                     f"{re.sub(r'\\s+', ' ', piece[:600])}…"})
        read_parts.append(f"# {fname}\n{piece}")
        total += len(piece)

    assembled = "\n\n".join(read_parts) or "(документы без распознанного текста)"
    _emit_text(on_step, "формирует карточку…", step_count=len(steps),
               tool_calls=len(steps), tokens=0)
    user = f"{_meta_block(tender, evidence_digest)}\nПРОЧИТАННЫЕ ДОКУМЕНТЫ:\n{assembled}"
    card, tokens = _submit_extract(client, model, user)
    if card is None:
        return _result("error", None, steps, len(steps), 0, False) | {"error": "извлечение не удалось"}
    steps.append({"idx": len(steps) + 1, "kind": "final", "thought": "", "tool": "submit_card",
                  "args": {}, "observation": "Карточка извлечена из прочитанных документов"})
    tool_calls = len(steps)
    _emit(on_step, "submit_card", {}, store, step_count=len(steps), tool_calls=tool_calls, tokens=tokens)
    return _result("done", card, steps, tool_calls, tokens, False)


# ── Нативная петля (function-calling) ──────────────────────────────────────

def _run_native(client, model: str, tender: dict[str, Any], store: DocStore, deadline: float,
                on_step: Callable[..., None] | None = None, evidence_digest: str = "") -> dict[str, Any]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _card_metadata(tender, evidence_digest)},
    ]
    steps: list[dict[str, Any]] = []
    tool_calls = 0
    tokens = 0
    raw_card: dict[str, Any] | None = None
    limit_reached = False

    for _ in range(MAX_STEPS):
        if time.monotonic() > deadline:
            limit_reached = True
            break
        # «обращается к модели…» — чтобы долгое ожидание ответа шлюза было видно (не «завис»)
        _emit_text(on_step, "обращается к модели…",
                   step_count=len(steps), tool_calls=tool_calls, tokens=tokens)
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=_TOOLS,
            tool_choice="auto", temperature=0.1, timeout=40,
        )
        tokens += getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
        msg = resp.choices[0].message
        thought = (msg.content or "").strip()
        calls = msg.tool_calls or []

        if not calls:
            # модель не вызвала инструмент — фиксируем мысль и подталкиваем к submit_card
            if thought:
                steps.append({"idx": len(steps) + 1, "kind": "note", "thought": thought,
                              "tool": "", "args": {}, "observation": ""})
            messages.append({"role": "assistant", "content": thought or ""})
            messages.append({"role": "user", "content": "Продолжай инструментами или вызови submit_card."})
            continue

        messages.append({
            "role": "assistant", "content": thought or None,
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls
            ],
        })

        done = False
        for c in calls:
            name = c.function.name
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls += 1

            if name == "submit_card":
                raw_card = args
                _emit(on_step, "submit_card", {}, store,
                      step_count=len(steps), tool_calls=tool_calls, tokens=tokens)
                steps.append({"idx": len(steps) + 1, "kind": "final", "thought": thought,
                              "tool": "submit_card", "args": {}, "observation": "Карточка сформирована"})
                messages.append({"role": "tool", "tool_call_id": c.id, "content": "ok"})
                done = True
                continue

            _emit(on_step, name, args, store,
                  step_count=len(steps), tool_calls=tool_calls, tokens=tokens)
            observation = _exec_tool(name, args, store)
            steps.append({"idx": len(steps) + 1, "kind": "tool",
                          "thought": thought if not steps or steps[-1].get("thought") != thought else "",
                          "tool": name, "args": args, "observation": observation})
            messages.append({"role": "tool", "tool_call_id": c.id, "content": observation[:4000]})

        if done:
            return _result("done", raw_card, steps, tool_calls, tokens, False)
    else:
        limit_reached = True  # цикл исчерпан без submit_card

    return _finalize_partial(client, model, messages, steps, store, tool_calls, tokens, limit_reached)


def _finalize_partial(client, model, messages, steps, store, tool_calls, tokens, limit_reached) -> dict[str, Any]:
    """Лимит шагов/таймаут: один принудительный submit_card тем, что собрали."""
    try:
        messages.append({"role": "user", "content":
                         "Достигнут лимит. Немедленно вызови submit_card тем, что уже собрал."})
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=_TOOLS,
            tool_choice={"type": "function", "function": {"name": "submit_card"}},
            temperature=0.1, timeout=40,
        )
        tokens += getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
        calls = resp.choices[0].message.tool_calls or []
        if calls:
            args = json.loads(calls[0].function.arguments or "{}")
            steps.append({"idx": len(steps) + 1, "kind": "final", "thought": "",
                          "tool": "submit_card", "args": {},
                          "observation": "Карточка сформирована (по лимиту)"})
            return _result("partial", args, steps, tool_calls, tokens, True)
    except Exception:
        pass
    return _result("partial" if limit_reached else "error", None, steps, tool_calls, tokens, limit_reached)


# ── Фолбэк: текстовый JSON-action протокол ─────────────────────────────────

_FALLBACK_SYSTEM = (
    SYSTEM_PROMPT + "\n\nИНСТРУМЕНТОВ-ФУНКЦИЙ НЕТ. На каждом шаге отвечай СТРОГО JSON-объектом одной из форм:\n"
    '{"thought": "...", "action": "list_documents|read_document|search_documents|lookup_dictionary", "action_input": {...}}\n'
    'или для финала: {"thought": "...", "submit_card": { ...карточка... }}.\n'
    "action_input для read_document: {\"n\":int,\"chunk\":int}; для search_documents: {\"query\":str}."
)


def _run_fallback(client, model: str, tender: dict[str, Any], store: DocStore, deadline: float,
                  on_step: Callable[..., None] | None = None, evidence_digest: str = "") -> dict[str, Any]:
    messages = [
        {"role": "system", "content": _FALLBACK_SYSTEM},
        {"role": "user", "content": _card_metadata(tender, evidence_digest)},
    ]
    steps: list[dict[str, Any]] = []
    tool_calls = 0
    tokens = 0
    limit_reached = False

    for _ in range(MAX_STEPS):
        if time.monotonic() > deadline:
            limit_reached = True
            break
        _emit_text(on_step, "обращается к модели…",
                   step_count=len(steps), tool_calls=tool_calls, tokens=tokens)
        resp = client.chat.completions.create(
            model=model, messages=messages,
            response_format={"type": "json_object"}, temperature=0.1, timeout=40,
        )
        tokens += getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
        content = resp.choices[0].message.content or "{}"
        messages.append({"role": "assistant", "content": content})
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            messages.append({"role": "user", "content": "Невалидный JSON. Ответь строго JSON-объектом."})
            continue

        thought = str(data.get("thought", "") or "").strip()
        if "submit_card" in data:
            _emit(on_step, "submit_card", {}, store,
                  step_count=len(steps), tool_calls=tool_calls, tokens=tokens)
            steps.append({"idx": len(steps) + 1, "kind": "final", "thought": thought,
                          "tool": "submit_card", "args": {}, "observation": "Карточка сформирована"})
            return _result("done", data.get("submit_card") or {}, steps, tool_calls, tokens, False)

        action = str(data.get("action", "") or "").strip()
        action_input = data.get("action_input") if isinstance(data.get("action_input"), dict) else {}
        if not action:
            messages.append({"role": "user", "content": "Укажи action или submit_card."})
            continue
        tool_calls += 1
        _emit(on_step, action, action_input, store,
              step_count=len(steps), tool_calls=tool_calls, tokens=tokens)
        observation = _exec_tool(action, action_input, store)
        steps.append({"idx": len(steps) + 1, "kind": "tool", "thought": thought,
                      "tool": action, "args": action_input, "observation": observation})
        messages.append({"role": "user", "content": f"НАБЛЮДЕНИЕ:\n{observation[:4000]}"})
    else:
        limit_reached = True

    # Принудительная финализация в фолбэке
    try:
        messages.append({"role": "user", "content":
                         "Достигнут лимит. Ответь JSON с ключом submit_card — карточкой из собранного."})
        resp = client.chat.completions.create(
            model=model, messages=messages,
            response_format={"type": "json_object"}, temperature=0.1, timeout=40,
        )
        tokens += getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
        data = json.loads(resp.choices[0].message.content or "{}")
        card = data.get("submit_card") or data
        steps.append({"idx": len(steps) + 1, "kind": "final", "thought": "",
                      "tool": "submit_card", "args": {}, "observation": "Карточка сформирована (по лимиту)"})
        return _result("partial", card, steps, tool_calls, tokens, True)
    except Exception:
        return _result("partial" if limit_reached else "error", None, steps, tool_calls, tokens, limit_reached)


def _result(status, raw_card, steps, tool_calls, tokens, limit_reached) -> dict[str, Any]:
    card = normalize_card(raw_card, steps, limit_reached) if raw_card is not None else None
    return {
        "status": status,
        "card": card,
        "steps": steps,
        "domain": (card or {}).get("domain", "") if card else "",
        "confidence": (card or {}).get("confidence", "") if card else "",
        "step_count": len(steps),
        "tool_calls": tool_calls,
        "tokens": tokens,
        "limit_reached": limit_reached,
        "error": "" if card is not None else "агент не сформировал карточку",
    }


# ── Публичная точка входа ───────────────────────────────────────────────────

def is_configured() -> bool:
    return ai.is_configured()


def _client():
    # Клиент активного провайдера (RMR/OpenRouter) — см. ai.active_config / AI_PROVIDER.
    # timeout=40 на попытку бьёт по зависанию; max_retries=2 (экспон. бэкофф SDK) переживает
    # 429 rate-limit. Для one-shot вызова поднимаем timeout отдельно (см. _run_oneshot).
    return ai.build_client(max_retries=2, timeout=40)


def run_agent(
    tender: dict[str, Any],
    documents: list[dict[str, Any]],
    model: str | None = None,
    on_step: Callable[..., None] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Прогоняет ReAct-агента по комплекту документов тендера.

    documents: [{filename, text}]. on_step(text) — колбэк живого прогресса (текущий шаг).
    evidence: список улик из rules.collect_evidence для фронт-загрузки (стартовые
    фрагменты, чтобы агент сходился быстрее). Возвращает dict с card/steps/метриками
    (см. _result). При недоступности шлюза — status='error', card=None.
    """
    global _NATIVE_TOOLS
    client = _client()
    if client is None:
        return _result("error", None, [], 0, 0, False)

    model = model or ai.MODEL
    store = DocStore(documents)
    deadline = time.monotonic() + TENDER_TIMEOUT
    evidence_digest = _evidence_digest(evidence)

    # Прямые режимы (без модельной петли): oneshot — один вызов по всему тексту;
    # guided — видимое чтение ключевых документов + один вызов извлечения.
    mode = _agent_mode()
    if mode in ("oneshot", "guided"):
        runner = _run_guided if mode == "guided" else _run_oneshot
        try:
            return runner(client, model, tender, store, deadline, on_step, evidence_digest)
        except Exception as exc:
            return _result("error", None, [], 0, 0, False) | {"error": f"{exc.__class__.__name__}: {exc}"}

    # Выбор режима: пробуем нативные tools, при сбое первого вызова — фолбэк (запоминаем).
    if _NATIVE_TOOLS is False:
        return _run_fallback(client, model, tender, store, deadline, on_step, evidence_digest)
    try:
        result = _run_native(client, model, tender, store, deadline, on_step, evidence_digest)
        _NATIVE_TOOLS = True
        return result
    except Exception as exc:
        if _NATIVE_TOOLS is None:
            _NATIVE_TOOLS = False
            print(f"  [agent] нативные tools недоступны ({exc.__class__.__name__}) — фолбэк на JSON-action")
            try:
                return _run_fallback(client, model, tender, store, deadline, on_step, evidence_digest)
            except Exception as exc2:
                return _result("error", None, [], 0, 0, False) | {"error": f"{exc2.__class__.__name__}: {exc2}"}
        return _result("error", None, [], 0, 0, False) | {"error": f"{exc.__class__.__name__}: {exc}"}
