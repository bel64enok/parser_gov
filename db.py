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

STALE_RUN_MIN = 15  # running дольше 15 мин при часовом cron → 'stalled' (прервано)

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
    error           TEXT,
    run_id          INTEGER          -- прогон, в котором тендер скачан (FK pipeline_runs.id)
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
    status          TEXT DEFAULT 'ok',   -- ok | failed | archive | unpacked
    error           TEXT DEFAULT '',
    size_bytes      INTEGER,
    source_url      TEXT DEFAULT '',
    parent_doc_id   INTEGER,             -- для member архива → id строки самого архива
    run_id          INTEGER,
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
    agent_card_json     TEXT,            -- структурированная карточка ReAct-агента (последняя)
    model               TEXT,
    analyzed_at         TEXT
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_number   TEXT,
    pipeline_run_id INTEGER,             -- прогон, в котором отработал агент (FK pipeline_runs.id)
    status          TEXT,                -- running | done | partial | error
    model           TEXT,
    domain          TEXT,                -- денормализовано для списка (из карточки)
    confidence      TEXT,                -- денормализовано для списка
    step_count      INTEGER DEFAULT 0,
    tool_calls      INTEGER DEFAULT 0,
    tokens          INTEGER DEFAULT 0,
    duration_sec    REAL,
    limit_reached   INTEGER DEFAULT 0,   -- упёрлись в лимит шагов → частичная карточка
    error           TEXT DEFAULT '',
    current_step    TEXT DEFAULT '',     -- человекочитаемый текущий шаг (для живого прогресса)
    card_json       TEXT,                -- структурированная карточка C
    steps_json      TEXT,                -- полный ReAct-трейс (массив шагов)
    started_at      TEXT,
    finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT,
    finished_at     TEXT,
    duration_sec    REAL,
    query           TEXT,
    limit_n         INTEGER,
    source          TEXT,
    fetched_new     INTEGER,
    fetched_skipped INTEGER,
    analyzed_ok     INTEGER,
    analyzed_failed INTEGER,
    status          TEXT,            -- running | completed | error
    error           TEXT,
    ai_enabled      INTEGER,
    ai_model        TEXT,
    kind            TEXT DEFAULT 'pipeline',  -- pipeline (cron) | fetch (ручной сбор)
    stage           TEXT DEFAULT 'full',      -- full | fetch | retry
    tenders_total    INTEGER DEFAULT 0,
    tenders_done     INTEGER DEFAULT 0,
    files_downloaded INTEGER DEFAULT 0,
    files_unpacked   INTEGER DEFAULT 0,
    files_failed     INTEGER DEFAULT 0,
    progress_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tenders_status ON tenders(pipeline_status);
CREATE INDEX IF NOT EXISTS idx_tenders_publish ON tenders(publish_date_iso);
CREATE INDEX IF NOT EXISTS idx_runs_started ON pipeline_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_tender ON agent_runs(tender_number);
CREATE INDEX IF NOT EXISTS idx_agent_runs_pipeline ON agent_runs(pipeline_run_id);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, name: str, decl: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate(conn: sqlite3.Connection) -> None:
    """Идемпотентно добавляет недостающие колонки в существующую БД (SQLite не умеет
    ADD COLUMN IF NOT EXISTS). Для свежей БД колонки уже созданы из SCHEMA — ALTER пропускается.
    Индексы на новые колонки создаём здесь, а не в SCHEMA: к моменту executescript(SCHEMA)
    на старой БД колонок ещё нет и CREATE INDEX упал бы."""
    _add_column(conn, "documents", "status", "TEXT DEFAULT 'ok'")
    _add_column(conn, "documents", "error", "TEXT DEFAULT ''")
    _add_column(conn, "documents", "size_bytes", "INTEGER")
    _add_column(conn, "documents", "source_url", "TEXT DEFAULT ''")
    _add_column(conn, "documents", "parent_doc_id", "INTEGER")
    _add_column(conn, "documents", "run_id", "INTEGER")
    _add_column(conn, "tenders", "run_id", "INTEGER")
    _add_column(conn, "pipeline_runs", "kind", "TEXT DEFAULT 'pipeline'")
    _add_column(conn, "pipeline_runs", "stage", "TEXT DEFAULT 'full'")
    _add_column(conn, "pipeline_runs", "tenders_total", "INTEGER DEFAULT 0")
    _add_column(conn, "pipeline_runs", "tenders_done", "INTEGER DEFAULT 0")
    _add_column(conn, "pipeline_runs", "files_downloaded", "INTEGER DEFAULT 0")
    _add_column(conn, "pipeline_runs", "files_unpacked", "INTEGER DEFAULT 0")
    _add_column(conn, "pipeline_runs", "files_failed", "INTEGER DEFAULT 0")
    _add_column(conn, "pipeline_runs", "progress_at", "TEXT")
    _add_column(conn, "analysis", "agent_card_json", "TEXT")
    _add_column(conn, "agent_runs", "current_step", "TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_tender ON documents(tender_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_tender ON agent_runs(tender_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_pipeline ON agent_runs(pipeline_run_id)")
    conn.commit()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA busy_timeout=4000")  # переживаем кратковременные блокировки воркер↔polling
    _migrate(conn)
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
        "source", "fetched_at", "pipeline_status", "error", "run_id",
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


def add_document(
    conn: sqlite3.Connection,
    tender_number: str,
    filename: str,
    path: str,
    doc_type: str,
    status: str = "ok",
    error: str = "",
    size_bytes: int | None = None,
    source_url: str = "",
    parent_doc_id: int | None = None,
    run_id: int | None = None,
) -> int:
    """Пишет строку документа (INSERT OR IGNORE по UNIQUE) и возвращает её id.

    При IGNORE lastrowid=0, поэтому id всегда дочитываем обратным SELECT — он нужен,
    чтобы привязать members архива к parent_doc_id.
    """
    conn.execute(
        "INSERT OR IGNORE INTO documents "
        "(tender_number, filename, path, type, status, error, size_bytes, source_url, "
        "parent_doc_id, run_id, downloaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tender_number, filename, path, doc_type, status, error, size_bytes, source_url,
            parent_doc_id, run_id, datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM documents WHERE tender_number=? AND filename=?",
        (tender_number, filename),
    ).fetchone()
    return int(row["id"]) if row else 0


def upsert_document(
    conn: sqlite3.Connection,
    tender_number: str,
    filename: str,
    path: str,
    doc_type: str,
    status: str = "ok",
    error: str = "",
    size_bytes: int | None = None,
    source_url: str = "",
    parent_doc_id: int | None = None,
    run_id: int | None = None,
) -> int:
    """Как add_document, но при существующей строке (tender_number, filename) обновляет её.

    Нужно для ретрая: INSERT OR IGNORE проглотил бы повтор, оставив строку в статусе 'failed';
    здесь же она перезаписывается (failed → ok с новым путём/размером). parent_doc_id/run_id
    при обновлении не трогаем — сохраняем исходную привязку к архиву и прогону.
    """
    conn.execute(
        "INSERT INTO documents "
        "(tender_number, filename, path, type, status, error, size_bytes, source_url, "
        "parent_doc_id, run_id, downloaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tender_number, filename) DO UPDATE SET "
        "path=excluded.path, type=excluded.type, status=excluded.status, error=excluded.error, "
        "size_bytes=excluded.size_bytes, source_url=excluded.source_url, "
        "downloaded_at=excluded.downloaded_at",
        (
            tender_number, filename, path, doc_type, status, error, size_bytes, source_url,
            parent_doc_id, run_id, datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM documents WHERE tender_number=? AND filename=?",
        (tender_number, filename),
    ).fetchone()
    return int(row["id"]) if row else 0


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


def document_by_id(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()


def failed_documents_for_tender(conn: sqlite3.Connection, tender_number: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE tender_number=? AND status='failed' ORDER BY id",
        (tender_number,),
    ).fetchall()


def failed_documents_for_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE run_id=? AND status='failed' ORDER BY id", (run_id,)
    ).fetchall()


def tenders_for_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tenders WHERE run_id=? ORDER BY fetched_at", (run_id,)
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


# ── Агентные прогоны (ReAct-стадия 2) ─────────────────────────────────────

def create_agent_run(
    conn: sqlite3.Connection, tender_number: str, pipeline_run_id: int | None, model: str
) -> int:
    """Открывает попытку анализа тендера агентом (status='running'). Возвращает id."""
    cur = conn.execute(
        "INSERT INTO agent_runs (tender_number, pipeline_run_id, status, model, started_at) "
        "VALUES (?, ?, 'running', ?, ?)",
        (tender_number, pipeline_run_id, model, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_agent_run(
    conn: sqlite3.Connection,
    agent_run_id: int,
    *,
    status: str,
    card: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
    domain: str = "",
    confidence: str = "",
    step_count: int = 0,
    tool_calls: int = 0,
    tokens: int = 0,
    limit_reached: bool = False,
    error: str = "",
) -> None:
    """Закрывает попытку агента: статус, метрики, карточка и трейс (JSON-блобы)."""
    row = conn.execute("SELECT started_at FROM agent_runs WHERE id=?", (agent_run_id,)).fetchone()
    finished = datetime.now()
    duration = None
    if row and row["started_at"]:
        try:
            duration = (finished - datetime.fromisoformat(row["started_at"])).total_seconds()
        except ValueError:
            duration = None
    conn.execute(
        "UPDATE agent_runs SET status=?, domain=?, confidence=?, step_count=?, tool_calls=?, "
        "tokens=?, duration_sec=?, limit_reached=?, error=?, card_json=?, steps_json=?, "
        "finished_at=? WHERE id=?",
        (
            status, domain, confidence, step_count, tool_calls, tokens, duration,
            int(limit_reached), error,
            json.dumps(card, ensure_ascii=False) if card is not None else None,
            json.dumps(steps or [], ensure_ascii=False),
            finished.isoformat(timespec="seconds"),
            agent_run_id,
        ),
    )
    conn.commit()


def set_agent_current_step(agent_run_id: int, text: str) -> None:
    """Пишет человекочитаемый текущий шаг агента (для живого прогресса). Открывает короткое
    собственное соединение — вызывается из воркер-треда во время прогона."""
    if not agent_run_id:
        return
    conn = connect()
    try:
        conn.execute("UPDATE agent_runs SET current_step=? WHERE id=?", (text[:200], agent_run_id))
        conn.commit()
    finally:
        conn.close()


def running_agent_run(conn: sqlite3.Connection, pipeline_run_id: int) -> sqlite3.Row | None:
    """Текущая выполняющаяся попытка агента в рамках прогона (для живого прогресса)."""
    return conn.execute(
        "SELECT * FROM agent_runs WHERE pipeline_run_id=? AND status='running' ORDER BY id DESC LIMIT 1",
        (pipeline_run_id,),
    ).fetchone()


def save_agent_card(conn: sqlite3.Connection, tender_number: str, card: dict[str, Any] | None) -> None:
    """Кладёт последнюю карточку агента в analysis (для вкладки «Тендеры»). Строка analysis
    к этому моменту уже создана save_analysis()."""
    conn.execute(
        "UPDATE analysis SET agent_card_json=? WHERE tender_number=?",
        (json.dumps(card, ensure_ascii=False) if card is not None else None, tender_number),
    )
    conn.commit()


def latest_agent_run(conn: sqlite3.Connection, tender_number: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM agent_runs WHERE tender_number=? ORDER BY id DESC LIMIT 1",
        (tender_number,),
    ).fetchone()


def agent_runs_for_tender(conn: sqlite3.Connection, tender_number: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM agent_runs WHERE tender_number=? ORDER BY id DESC", (tender_number,)
    ).fetchall()


def agent_runs_for_pipeline(conn: sqlite3.Connection, pipeline_run_id: int) -> list[sqlite3.Row]:
    """Последняя попытка агента по каждому тендеру в рамках прогона."""
    return conn.execute(
        "SELECT a.* FROM agent_runs a "
        "JOIN (SELECT tender_number, MAX(id) AS mid FROM agent_runs "
        "      WHERE pipeline_run_id=? GROUP BY tender_number) last "
        "  ON a.id = last.mid "
        "ORDER BY a.id",
        (pipeline_run_id,),
    ).fetchall()


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
        # структурированная карточка ReAct-агента (для менеджерской вкладки «Тендеры»)
        "agent_card": json.loads(field("agent_card_json", "null")) if field("agent_card_json") else None,
    }
    documents = []
    for doc in documents_for(conn, row["number"]):
        documents.append({
            "id": f"doc-{doc['id']}",
            "name": doc["filename"],
            "type": doc["type"] or "file",
            "text": doc["extracted_text"] or "",
            "source_url": row["url"] or "",
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


def pipeline_status() -> dict[str, Any]:
    """ETL state summary for /api/pipeline."""
    conn = connect()
    try:
        counts = conn.execute(
            """
            SELECT
                COUNT(*)                                  AS total,
                SUM(pipeline_status = 'fetched')          AS pending,
                SUM(pipeline_status = 'analyzed')         AS analyzed,
                SUM(pipeline_status = 'error')            AS error,
                MAX(fetched_at)                           AS last_fetched_at
            FROM tenders
            """
        ).fetchone()
        last_analyzed = conn.execute(
            "SELECT MAX(analyzed_at) AS ts FROM analysis"
        ).fetchone()["ts"]
        docs_total = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        has_real = conn.execute(
            "SELECT 1 FROM tenders WHERE source='zakupki.gov.ru' LIMIT 1"
        ).fetchone()
        error_rows = conn.execute(
            "SELECT number, error FROM tenders "
            "WHERE pipeline_status='error' ORDER BY fetched_at DESC LIMIT 3"
        ).fetchall()
        import ai as ai_module  # lazy — keeps db.py testable without openai package
        return {
            "total":            counts["total"] or 0,
            "pending":          counts["pending"] or 0,
            "analyzed":         counts["analyzed"] or 0,
            "error":            counts["error"] or 0,
            "last_fetched_at":  counts["last_fetched_at"] or "",
            "last_analyzed_at": last_analyzed or "",
            "source":           "zakupki.gov.ru" if has_real else "fallback",
            "ai_enabled":       ai_module.is_configured(),
            "docs_total":       docs_total or 0,
            "errors":           [{"number": r["number"], "error": r["error"]} for r in error_rows],
        }
    finally:
        conn.close()


def start_run(query: str, limit: int, kind: str = "pipeline", stage: str = "full") -> int:
    """Открывает запись прогона (status='running'). Возвращает id для finish_run.

    kind: 'pipeline' (cron, обе стадии) | 'fetch' (ручной сбор из UI, только стадия 1).
    stage: 'full' | 'fetch' | 'retry'.
    """
    import ai as ai_module  # lazy — db.py не должен тянуть openai
    configured = ai_module.is_configured()
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO pipeline_runs "
            "(started_at, query, limit_n, status, ai_enabled, ai_model, kind, stage) "
            "VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                query,
                int(limit),
                int(configured),
                ai_module.MODEL if configured else "rules-only",
                kind,
                stage,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def active_run() -> dict[str, Any] | None:
    """Единственный незавершённый прогон ('running' свежее STALE_RUN_MIN), иначе None.

    Используется как single-run guard перед ручным сбором. recent_runs() уже понижает
    протухшие 'running' до 'stalled', поэтому зависший воркер не блокирует запуски навсегда.
    """
    for r in recent_runs(20):
        if r.get("status") == "running":
            return r
    return None


def update_run_progress(
    run_id: int,
    *,
    tenders_total: int | None = None,
    tenders_done: int | None = None,
    files_downloaded: int | None = None,
    files_unpacked: int | None = None,
    files_failed: int | None = None,
) -> None:
    """Обновляет счётчики прогресса прогона. Открывает собственное короткое соединение —
    sqlite3-соединения нельзя шарить между тредами, а это вызывается из воркер-треда."""
    if not run_id:
        return
    sets: list[str] = []
    params: list[Any] = []
    for col, val in (
        ("tenders_total", tenders_total),
        ("tenders_done", tenders_done),
        ("files_downloaded", files_downloaded),
        ("files_unpacked", files_unpacked),
        ("files_failed", files_failed),
    ):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if not sets:
        return
    sets.append("progress_at=?")
    params.append(datetime.now().isoformat(timespec="seconds"))
    params.append(run_id)
    conn = connect()
    try:
        conn.execute(f"UPDATE pipeline_runs SET {', '.join(sets)} WHERE id=?", params)
        conn.commit()
    finally:
        conn.close()


def finish_run(
    run_id: int,
    fetch_summary: dict[str, Any] | None,
    analyze_summary: dict[str, Any] | None,
    source: str,
    status: str,
    error: str = "",
) -> None:
    """Закрывает запись прогона: время, длительность, счётчики, статус."""
    if not run_id:
        return
    fetch_summary = fetch_summary or {}
    analyze_summary = analyze_summary or {}
    conn = connect()
    try:
        row = conn.execute(
            "SELECT started_at FROM pipeline_runs WHERE id=?", (run_id,)
        ).fetchone()
        finished = datetime.now()
        duration = None
        if row and row["started_at"]:
            try:
                duration = (finished - datetime.fromisoformat(row["started_at"])).total_seconds()
            except ValueError:
                duration = None
        conn.execute(
            "UPDATE pipeline_runs SET "
            "finished_at=?, duration_sec=?, source=?, "
            "fetched_new=?, fetched_skipped=?, analyzed_ok=?, analyzed_failed=?, "
            "status=?, error=? WHERE id=?",
            (
                finished.isoformat(timespec="seconds"),
                duration,
                source,
                fetch_summary.get("new"),
                fetch_summary.get("skipped"),
                analyze_summary.get("analyzed"),
                analyze_summary.get("failed"),
                status,
                error,
                run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def recent_runs(limit: int = 50) -> list[dict[str, Any]]:
    """История прогонов, новые сверху. 'running' старше STALE_RUN_MIN → 'stalled'."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        now = datetime.now()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if d.get("status") == "running" and d.get("started_at"):
                try:
                    age_min = (now - datetime.fromisoformat(d["started_at"])).total_seconds() / 60
                    if age_min > STALE_RUN_MIN:
                        d["status"] = "stalled"
                except ValueError:
                    pass
            out.append(d)
        return out
    finally:
        conn.close()
