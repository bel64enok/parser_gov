"""Стадия 2: ИИ-обработка. Извлекает текст из скачанных документов, прогоняет правила,
затем ИИ поверх правил, пишет результат в таблицу analysis.

Берёт тендеры со статусом 'fetched', переводит в 'analyzed'. ИИ опционален: без шлюза
анализ остаётся на правилах.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import ai
import db
from extract import extract_text_from_file
from rules import analyze_tender, collect_evidence, document_text_for_tender

# поля карточки, которые нужны функциям анализа
_TENDER_FIELDS = (
    "number", "title", "customer", "price", "method", "law",
    "publish_date", "deadline", "okpd2", "status", "contract_status", "url",
)


def _row_to_tender(row) -> dict[str, Any]:
    return {field: row[field] for field in _TENDER_FIELDS}


def _build_full_text(conn, tender: dict[str, Any]) -> str:
    """Извлекает текст из всех документов тендера, сохраняет его и highlights в БД."""
    parts: list[str] = []
    docs = db.documents_for(conn, tender["number"])
    for doc in docs:
        path = Path(doc["path"])
        text = extract_text_from_file(path) if path.exists() else (doc["extracted_text"] or "")
        highlights = collect_evidence(tender, text)
        db.update_document_text(conn, doc["id"], text, highlights)
        if text.strip():
            parts.append(f"# {doc['filename']}\n{text}")

    if not parts:
        # документов нет — запасной текст из полей карточки
        return document_text_for_tender(tender)
    return "\n\n".join(parts)


def analyze_new() -> dict[str, Any]:
    conn = db.connect()
    analyzed = 0
    failed = 0
    use_ai = ai.is_configured()
    print(f"[analyze] ИИ-шлюз {'подключён' if use_ai else 'не настроен — анализ на правилах'}")
    try:
        rows = db.tenders_by_status(conn, "fetched")
        for row in rows:
            tender = _row_to_tender(row)
            number = tender["number"]
            try:
                full_text = _build_full_text(conn, tender)
                rules_result = analyze_tender(tender, full_text)
                ai_result = ai.ai_enrich(tender, full_text)
                model = ai.MODEL if (use_ai and ai_result) else "rules-only"
                db.save_analysis(conn, number, rules_result, ai_result, model)
                db.set_status(conn, number, "analyzed")
                analyzed += 1
                print(f"[analyze] {number}: score {rules_result['score']}, ИИ {'+' if ai_result else '−'}")
            except Exception as exc:
                db.set_status(conn, number, "error", f"{exc.__class__.__name__}: {exc}")
                failed += 1
                print(f"[analyze] {number}: ошибка — {exc}")
        print(f"[analyze] готово: проанализировано {analyzed}, ошибок {failed}")
        return {"analyzed": analyzed, "failed": failed}
    finally:
        conn.close()


if __name__ == "__main__":
    analyze_new()
