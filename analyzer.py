"""Стадия 2: ИИ-обработка. Извлекает текст из скачанных документов, прогоняет правила,
затем ReAct-агент извлекает структурированную карточку требований поверх правил, пишет
результат в analysis + agent_runs.

Берёт тендеры со статусом 'fetched', переводит в 'analyzed' (или 'error' при сбое извлечения).
Агент опционален: без шлюза анализ остаётся на правилах. Один и тот же код вызывается из
cron-пайплайна и из ручного запуска вкладки «ИИ-анализ» (через run_id + progress).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import agent
import db
from extract import extract_text_from_file
from rules import analyze_tender, collect_evidence, document_text_for_tender

# поля карточки, которые нужны функциям анализа
_TENDER_FIELDS = (
    "number", "title", "customer", "price", "method", "law",
    "publish_date", "deadline", "okpd2", "status", "contract_status", "url",
)

ProgressCb = Callable[[int, int], None]  # (done, total)


def _row_to_tender(row) -> dict[str, Any]:
    return {field: row[field] for field in _TENDER_FIELDS}


def _progress_callback(ar_id: int):
    """Колбэк живого прогресса агента → пишет метрики (шаги/вызовы/токены/действие)
    в agent_runs ПО ХОДУ прогона, чтобы UI не показывал «0 вызовов, 0 токенов»."""
    def cb(text: str, step_count: int = 0, tool_calls: int = 0, tokens: int = 0) -> None:
        db.update_agent_progress(ar_id, step_count=step_count, tool_calls=tool_calls,
                                 tokens=tokens, current_step=text)
    return cb


def _build_documents(conn, tender: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Извлекает текст документов тендера, сохраняет его и highlights в БД.

    Возвращает (full_text для правил, список {filename, text} для агента).
    """
    parts: list[str] = []
    docs: list[dict[str, Any]] = []
    for doc in db.documents_for(conn, tender["number"]):
        # архивы уже распакованы в стадии 1 (их члены — отдельные строки 'unpacked'); сам .zip
        # пропускаем, иначе двойной счёт. 'failed' — без файла на диске, извлекать нечего.
        if doc["status"] in ("archive", "failed"):
            continue
        path = Path(doc["path"]) if doc["path"] else None
        text = extract_text_from_file(path) if path and path.exists() else (doc["extracted_text"] or "")
        highlights = collect_evidence(tender, text)
        db.update_document_text(conn, doc["id"], text, highlights)
        if text.strip():
            parts.append(f"# {doc['filename']}\n{text}")
            docs.append({"filename": doc["filename"], "text": text})

    if not parts:
        # документов нет — запасной текст из полей карточки
        fallback = document_text_for_tender(tender)
        return fallback, [{"filename": "Карточка ЕИС", "text": fallback}]
    return "\n\n".join(parts), docs


def _ai_fields_from_card(card: dict[str, Any]) -> dict[str, Any]:
    """Маппит структурированную карточку агента в legacy-поля analysis (для вкладки «Тендеры»)."""
    reqs = [r["label"] for r in card.get("participant_requirements", []) if r.get("present")]
    deadline_note = ""
    for section in card.get("sections", []):
        if section.get("title") == "Сроки":
            for fact in section.get("facts", []):
                if fact.get("found"):
                    deadline_note = fact["value"]
                    break
    return {
        "summary": card.get("summary", ""),
        "requirements": reqs,
        "risks": card.get("risks", []),
        "suggested_domain": card.get("domain", ""),
        "confidence": card.get("confidence", ""),
        "deadline_note": deadline_note,
    }


def analyze_new(run_id: int | None = None, progress: ProgressCb | None = None) -> dict[str, Any]:
    """Анализирует все тендеры в статусе 'fetched'.

    run_id привязывает agent_runs к прогону (для вкладки «ИИ-анализ»); progress(done, total)
    обновляет счётчики прогона.
    """
    conn = db.connect()
    analyzed = 0
    failed = 0
    use_ai = agent.is_configured()
    print(f"[analyze] ИИ-агент {'подключён' if use_ai else 'не настроен — анализ на правилах'}")
    try:
        # При настроенном шлюзе очередь = все тендеры без карточки агента (включая ранее
        # помеченные 'analyzed' rules-only/упавшие) — иначе они застревали бы без карточки.
        # Без шлюза агент бессмысленен, поэтому обрабатываем только свежие 'fetched'.
        rows = db.tenders_awaiting_agent(conn) if use_ai else db.tenders_by_status(conn, "fetched")
        total = len(rows)
        if progress:
            progress(0, total)
        for index, row in enumerate(rows, start=1):
            tender = _row_to_tender(row)
            number = tender["number"]
            try:
                full_text, documents = _build_documents(conn, tender)
                rules_result = analyze_tender(tender, full_text)

                ai_result: dict[str, Any] = {}
                card = None
                model = "rules-only"
                if use_ai:
                    ar_id = db.create_agent_run(conn, number, run_id, agent.MODEL)
                    result = agent.run_agent(
                        tender, documents,
                        on_step=_progress_callback(ar_id),
                        evidence=collect_evidence(tender, full_text),
                    )
                    card = result.get("card")
                    db.finish_agent_run(
                        conn, ar_id,
                        status=result["status"], card=card, steps=result["steps"],
                        domain=result.get("domain", ""), confidence=result.get("confidence", ""),
                        step_count=result["step_count"], tool_calls=result["tool_calls"],
                        tokens=result["tokens"], limit_reached=result["limit_reached"],
                        error=result.get("error", ""),
                    )
                    if card is not None:
                        ai_result = _ai_fields_from_card(card)
                        model = agent.MODEL

                db.save_analysis(conn, number, rules_result, ai_result, model)
                db.save_agent_card(conn, number, card)
                db.set_status(conn, number, "analyzed")
                analyzed += 1
                tag = "agent" if card is not None else ("rules-only" if use_ai else "rules")
                print(f"[analyze] {number}: score {rules_result['score']} · {tag}")
            except Exception as exc:
                db.set_status(conn, number, "error", f"{exc.__class__.__name__}: {exc}")
                failed += 1
                print(f"[analyze] {number}: ошибка — {exc}")
            if progress:
                progress(index, total)
        print(f"[analyze] готово: проанализировано {analyzed}, ошибок {failed}")
        return {"analyzed": analyzed, "failed": failed}
    finally:
        conn.close()


def reanalyze_tender(number: str, run_id: int | None = None) -> dict[str, Any]:
    """Повторный анализ одного тендера (в т.ч. уже 'analyzed'). Новая попытка агента."""
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM tenders WHERE number=?", (number,)).fetchone()
        if row is None:
            return {"ok": False, "error": "тендер не найден"}
        tender = _row_to_tender(row)
        use_ai = agent.is_configured()
        full_text, documents = _build_documents(conn, tender)
        rules_result = analyze_tender(tender, full_text)
        ai_result: dict[str, Any] = {}
        card = None
        model = "rules-only"
        if use_ai:
            ar_id = db.create_agent_run(conn, number, run_id, agent.MODEL)
            result = agent.run_agent(
                tender, documents,
                on_step=_progress_callback(ar_id),
                evidence=collect_evidence(tender, full_text),
            )
            card = result.get("card")
            db.finish_agent_run(
                conn, ar_id,
                status=result["status"], card=card, steps=result["steps"],
                domain=result.get("domain", ""), confidence=result.get("confidence", ""),
                step_count=result["step_count"], tool_calls=result["tool_calls"],
                tokens=result["tokens"], limit_reached=result["limit_reached"],
                error=result.get("error", ""),
            )
            if card is not None:
                ai_result = _ai_fields_from_card(card)
                model = agent.MODEL
        db.save_analysis(conn, number, rules_result, ai_result, model)
        db.save_agent_card(conn, number, card)
        db.set_status(conn, number, "analyzed")
        return {"ok": True, "number": number, "has_card": card is not None}
    finally:
        conn.close()


if __name__ == "__main__":
    analyze_new()
