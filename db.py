"""SQLite-хранилище пайплайна: тендеры, документы, результаты анализа.

Стадия 1 (fetcher) пишет tenders + documents, стадия 2 (analyzer) пишет analysis,
стадия 3 (app.py) только читает через query_tenders().
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
TENDERS_DIR = DATA_DIR / "tenders"
DB_PATH = DATA_DIR / "tenders.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    number          TEXT PRIMARY KEY,
    title           TEXT,
    customer        TEXT,
    price           INTEGER,
    method          TEXT,
    law             TEXT,
    publish_date    TEXT,
    deadline        TEXT,
    publish_date_iso TEXT,
    deadline_iso    TEXT,
    appeared_at     TEXT,
    okpd2           TEXT,
    status          TEXT,
    contract_status TEXT,
    url             TEXT,
    source          TEXT,
    fetched_at      TEXT,
    pipeline_status TEXT,            -- fetched | analyzed | error
    error           TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_number   TEXT,
    filename        TEXT,
    path            TEXT,
    type            TEXT,
    extracted_text  TEXT,
    highlights_json TEXT,
    downloaded_at   TEXT,
    UNIQUE(tender_number, filename)
);

CREATE TABLE IF NOT EXISTS analysis (
    tender_number       TEXT PRIMARY KEY,
    score               INTEGER,
    priority            TEXT,
    queue_priority      TEXT,
    risk_count          INTEGER,
    warning_count       INTEGER,
    domains_json        TEXT,
    products_json       TEXT,
    checklist_json      TEXT,
    evidence_json       TEXT,
    ai_summary          TEXT,
    ai_requirements_json TEXT,
    ai_risks_json       TEXT,
    ai_suggested_domain TEXT,
    ai_confidence       TEXT,
    ai_deadline_note    TEXT,
    model               TEXT,
    analyzed_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_tenders_status ON tenders(pipeline_status);
CREATE INDEX IF NOT EXISTS idx_tenders_publish ON tenders(publish_date_iso);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def tender_dir(number: str) -> Path:
    safe = "".join(ch for ch in number if ch.isalnum() or ch in "-_")
    path = TENDERS_DIR / (safe or "unknown")
    path.mkdir(parents=True, exist_ok=True)
    return path


def upsert_tender(conn: sqlite3.Connection, tender: dict[str, Any]) -> None:
    columns = (
        "number", "title", "customer", "price", "method", "law",
        "publish_date", "deadline", "publish_date_iso", "deadline_iso",
        "appeared_at", "okpd2", "status", "contract_status", "url",
        "source", "fetched_at", "pipeline_status", "error",
    )
    values = [tender.get(col) for col in columns]
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{col}=excluded.{col}" for col in columns if col != "number")
    conn.execute(
        f"INSERT INTO tenders ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(number) DO UPDATE SET {updates}",
        values,
    )
    conn.commit()


def set_status(conn: sqlite3.Connection, number: str, status: str, error: str = "") -> None:
    conn.execute(
        "UPDATE tenders SET pipeline_status=?, error=? WHERE number=?",
        (status, error, number),
    )
    conn.commit()


def add_document(conn: sqlite3.Connection, tender_number: str, filename: str, path: str, doc_type: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO documents (tender_number, filename, path, type, downloaded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (tender_number, filename, path, doc_type, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def update_document_text(conn: sqlite3.Connection, doc_id: int, text: str, highlights: list[dict[str, str]]) -> None:
    conn.execute(
        "UPDATE documents SET extracted_text=?, highlights_json=? WHERE id=?",
        (text, json.dumps(highlights, ensure_ascii=False), doc_id),
    )
    conn.commit()


def documents_for(conn: sqlite3.Connection, tender_number: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE tender_number=? ORDER BY id", (tender_number,)
    ).fetchall()


def tenders_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tenders WHERE pipeline_status=? ORDER BY fetched_at", (status,)
    ).fetchall()


def tender_exists(conn: sqlite3.Connection, number: str) -> bool:
    row = conn.execute("SELECT 1 FROM tenders WHERE number=?", (number,)).fetchone()
    return row is not None


def save_analysis(conn: sqlite3.Connection, tender_number: str, rules: dict[str, Any], ai: dict[str, Any], model: str) -> None:
    conn.execute(
        """
        INSERT INTO analysis (
            tender_number, score, priority, queue_priority, risk_count, warning_count,
            domains_json, products_json, checklist_json, evidence_json,
            ai_summary, ai_requirements_json, ai_risks_json,
            ai_suggested_domain, ai_confidence, ai_deadline_note, model, analyzed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tender_number) DO UPDATE SET
            score=excluded.score, priority=excluded.priority, queue_priority=excluded.queue_priority,
            risk_count=excluded.risk_count, warning_count=excluded.warning_count,
            domains_json=excluded.domains_json, products_json=excluded.products_json,
            checklist_json=excluded.checklist_json, evidence_json=excluded.evidence_json,
            ai_summary=excluded.ai_summary, ai_requirements_json=excluded.ai_requirements_json,
            ai_risks_json=excluded.ai_risks_json, ai_suggested_domain=excluded.ai_suggested_domain,
            ai_confidence=excluded.ai_confidence, ai_deadline_note=excluded.ai_deadline_note,
            model=excluded.model, analyzed_at=excluded.analyzed_at
        """,
        (
            tender_number, rules.get("score"), rules.get("priority"), rules.get("queue_priority"),
            rules.get("risk_count"), rules.get("warning_count"),
            json.dumps(rules.get("domains", []), ensure_ascii=False),
            json.dumps(rules.get("products", []), ensure_ascii=False),
            json.dumps(rules.get("checklist", []), ensure_ascii=False),
            json.dumps(rules.get("evidence", []), ensure_ascii=False),
            ai.get("summary", ""),
            json.dumps(ai.get("requirements", []), ensure_ascii=False),
            json.dumps(ai.get("risks", []), ensure_ascii=False),
            ai.get("suggested_domain", ""),
            str(ai.get("confidence", "")),
            ai.get("deadline_note", ""),
            model,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def _row_to_item(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """Собирает JSON в форме, которую ожидает фронтенд (items[].analysis, items[].documents)."""
    a = conn.execute(
        "SELECT * FROM analysis WHERE tender_number=?", (row["number"],)
    ).fetchone()
    a = a if a is not None else {}

    def field(key: str, default: Any = None) -> Any:
        try:
            value = a[key]
        except (IndexError, KeyError, TypeError):
            value = None
        return value if value is not None else default

    analysis = {
        "domains": json.loads(field("domains_json", "[]")),
        "products": json.loads(field("products_json", "[]")),
        "checklist": json.loads(field("checklist_json", "[]")),
        "evidence": json.loads(field("evidence_json", "[]")),
        "risk_count": field("risk_count", 0),
        "warning_count": field("warning_count", 0),
        "score": field("score", 0),
        "priority": field("priority", "Низкий"),
        "queue_priority": field("queue_priority", "Без срока"),
        # ИИ-поля поверх правил
        "summary": field("ai_summary", ""),
        "requirements": json.loads(field("ai_requirements_json", "[]")),
        "risks": json.loads(field("ai_risks_json", "[]")),
        "suggested_domain": field("ai_suggested_domain", ""),
        "confidence": field("ai_confidence", ""),
        "deadline_note": field("ai_deadline_note", ""),
    }
    documents = []
    for doc in documents_for(conn, row["number"]):
        documents.append({
            "id": f"doc-{doc['id']}",
            "name": doc["filename"],
            "type": doc["type"] or "file",
            "text": doc["extracted_text"] or "",
            "highlights": json.loads(doc["highlights_json"] or "[]"),
        })
    return {
        "number": row["number"],
        "title": row["title"],
        "customer": row["customer"],
        "price": row["price"] or 0,
        "method": row["method"],
        "law": row["law"],
        "publish_date": row["publish_date"],
        "deadline": row["deadline"],
        "appeared_at": row["appeared_at"],
        "okpd2": row["okpd2"],
        "status": row["status"],
        "contract_status": row["contract_status"],
        "url": row["url"],
        "documents": documents,
        "analysis": analysis,
    }


def query_tenders(filters: dict[str, Any]) -> dict[str, Any]:
    """Читает обработанные тендеры из БД с фильтрами по датам/домену/тексту."""
    conn = connect()
    try:
        where = ["a.tender_number IS NOT NULL"]
        params: list[Any] = []
        if filters.get("date_from"):
            where.append("t.publish_date_iso >= ?")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            where.append("t.publish_date_iso <= ?")
            params.append(filters["date_to"])
        if filters.get("query"):
            where.append("t.title LIKE ?")
            params.append(f"%{filters['query']}%")
        if filters.get("domain"):
            where.append("a.domains_json LIKE ?")
            params.append(f"%{filters['domain']}%")
        limit = max(1, min(int(filters.get("limit", 50)), 200))
        sql = (
            "SELECT t.* FROM tenders t JOIN analysis a ON a.tender_number = t.number "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY a.score DESC, t.deadline_iso ASC LIMIT ?"
        )
        rows = conn.execute(sql, [*params, limit]).fetchall()
        items = [_row_to_item(conn, row) for row in rows]

        last_run = conn.execute("SELECT MAX(analyzed_at) AS ts FROM analysis").fetchone()["ts"]
        has_real = conn.execute(
            "SELECT 1 FROM tenders WHERE source='zakupki.gov.ru' LIMIT 1"
        ).fetchone()
        return {
            "items": items,
            "source": "zakupki.gov.ru" if has_real else "fallback",
            "source_url": "https://zakupki.gov.ru/epz/order/extendedsearch/results.html",
            "error": "" if items else "Хранилище пусто — запустите пайплайн: python pipeline.py",
            "query": filters.get("query", ""),
            "parsed_at": last_run or "",
            "live_count": len(items),
        }
    finally:
        conn.close()
