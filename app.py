"""Стадия 3: backend. Отдаёт обработанные тендеры из локального хранилища (SQLite),
с фильтрами по датам/домену/тексту. Ничего не парсит вживую — это делает cron-пайплайн.

Запуск:  python app.py   (PORT=9000 python app.py — другой порт)
"""
from __future__ import annotations

import config  # noqa: F401 — загружает .env до обращений к ai

import io
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import analyzer
import db
import fetcher
from extract import extract_text_from_file
from rules import DOMAIN_TRIGGERS, analyze_tender, collect_evidence

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
UPLOAD_DIR = ROOT / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


def requirements_payload() -> dict[str, Any]:
    return {
        "functional": [
            "Cron-пайплайн скачивает закупки ЕИС и все вложения (docx, xlsx, pdf, zip) в локальное хранилище.",
            "ИИ-обработка извлекает текст документов и формирует резюме, требования и риски поверх правил.",
            "Выделение ключевых формулировок, тегов доменов и продуктовой маркировки.",
            "Обучаемый словарь доменов и продуктов для добавления новых категорий.",
            "Web-интерфейс поиска, фильтрации по датам, просмотра карточек и критериев анализа.",
        ],
        "non_functional": [
            "Скачивание и анализ разнесены по стадиям вокруг общего SQLite-хранилища.",
            "Очередь с приоритетом по дате окончания подачи заявок.",
            "Доступность web-интерфейса: целевой уровень 99% в месяц.",
            "Конфиденциальность данных и развертывание в инфраструктуре заказчика.",
            "Готовность к расширению критериев анализа и актуализации законодательства.",
        ],
        "checklist": [
            "Определение домена по словам-триггерам: МОБ, ФИКС, SI, М2М, BigData, Cloud, SBVAS.",
            "Проверка способа закупки: ОКПД2 61.*, сумма до 10 млн для запроса котировок, конкурс требует внимания.",
            "Проверка сроков подачи: аукцион 7/15 календарных дней, котировки 4 рабочих дня, конкурс 15 календарных дней.",
        ],
        "domains": DOMAIN_TRIGGERS,
    }


def crawler_payload() -> dict[str, Any]:
    """Обзор вкладки «Сбор документов»: активный прогон, сводка по документам, список прогонов."""
    runs = db.recent_runs(50)
    active = db.active_run()
    conn = db.connect()
    try:
        summary = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM tenders) AS tenders_total,
                (SELECT COUNT(DISTINCT tender_number) FROM documents
                    WHERE status IN ('ok','unpacked','archive')) AS tenders_with_files,
                (SELECT COUNT(*) FROM documents WHERE status='failed') AS failed_files,
                (SELECT COUNT(*) FROM documents WHERE status='archive') AS archives
            """
        ).fetchone()
    finally:
        conn.close()
    return {
        "active_run": active,
        "summary": {
            "tenders_total": summary["tenders_total"] or 0,
            "tenders_with_files": summary["tenders_with_files"] or 0,
            "failed_files": summary["failed_files"] or 0,
            "archives": summary["archives"] or 0,
        },
        "runs": runs,
        "cron_note": "Полный пайплайн (сбор + анализ) запускается по cron раз в час",
    }


def crawler_run_payload(run_id: int) -> dict[str, Any]:
    """Деталь прогона: тендеры, добавленные этим прогоном, со сводкой загрузки по каждому.

    Счётчики файлов скоупим по run_id документа — деталь показывает ровно то, что сделал
    ЭТОТ запуск. Итоги шапки (`totals`) выводим из тех же строк documents, поэтому
    «скачано/распаковано/упало» в шапке = сумма строк таблицы, без расхождений.
    """
    run = next((r for r in db.recent_runs(200) if r.get("id") == run_id), None)
    conn = db.connect()
    try:
        tenders = []
        for t in db.tenders_for_run(conn, run_id):
            agg = conn.execute(
                "SELECT SUM(status IN ('ok','archive')) AS downloaded, "
                "SUM(status='unpacked') AS unpacked, "
                "SUM(status='failed') AS failed "
                "FROM documents WHERE tender_number=? AND run_id=?",
                (t["number"], run_id),
            ).fetchone()
            tenders.append({
                "number": t["number"],
                "title": t["title"],
                "pipeline_status": t["pipeline_status"],
                "downloaded": agg["downloaded"] or 0,
                "unpacked": agg["unpacked"] or 0,
                "files_failed": agg["failed"] or 0,
            })
        totals_row = conn.execute(
            "SELECT SUM(status IN ('ok','archive')) AS downloaded, "
            "SUM(status='unpacked') AS unpacked, "
            "SUM(status='failed') AS failed "
            "FROM documents WHERE run_id=?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    totals = {
        "downloaded": totals_row["downloaded"] or 0,
        "unpacked": totals_row["unpacked"] or 0,
        "failed": totals_row["failed"] or 0,
    }
    return {"run": run, "tenders": tenders, "totals": totals}


def crawler_tender_payload(number: str) -> dict[str, Any]:
    """Дерево документов одного тендера со статусом каждого файла."""
    conn = db.connect()
    try:
        t = conn.execute("SELECT * FROM tenders WHERE number=?", (number,)).fetchone()
        docs = db.documents_for(conn, number)
    finally:
        conn.close()
    documents = [
        {
            "id": d["id"],
            "filename": d["filename"],
            "type": d["type"] or "file",
            "status": d["status"] or "ok",
            "error": d["error"] or "",
            "size_bytes": d["size_bytes"],
            "source_url": d["source_url"] or "",
            "parent_doc_id": d["parent_doc_id"],
        }
        for d in docs
    ]
    return {
        "tender": {
            "number": number,
            "title": (t["title"] if t else number) or number,
            "url": (t["url"] if t else "") or "",
            "run_id": t["run_id"] if t else None,
            "pipeline_status": (t["pipeline_status"] if t else "") or "",
        },
        "documents": documents,
    }


# ── Вкладка «ИИ-анализ» (стадия 2) ─────────────────────────────────────────

def analysis_payload() -> dict[str, Any]:
    """Обзор вкладки «ИИ-анализ»: статус шлюза, очередь, активный прогон, живой шаг, прогоны."""
    import agent as agent_module

    runs = [r for r in db.recent_runs(50) if r.get("kind") in ("analyze", "pipeline")]
    active = db.active_run()
    status = db.pipeline_status()

    current = None
    conn = db.connect()
    try:
        if active:
            running = db.running_agent_run(conn, active["id"])
            if running is not None:
                t = conn.execute(
                    "SELECT title FROM tenders WHERE number=?", (running["tender_number"],)
                ).fetchone()
                elapsed = None
                started = running["started_at"]
                if started:
                    try:
                        elapsed = max(0, int((datetime.now() - datetime.fromisoformat(started)).total_seconds()))
                    except (ValueError, TypeError):
                        elapsed = None
                current = {
                    "tender_number": running["tender_number"],
                    "tender_title": (t["title"] if t else "") or running["tender_number"],
                    "step": running["current_step"] or "",
                    "step_count": running["step_count"] or 0,
                    "tool_calls": running["tool_calls"] or 0,
                    "tokens": running["tokens"] or 0,
                    "elapsed_sec": elapsed,
                }
        agg = conn.execute(
            "SELECT COUNT(*) AS total, SUM(status='done') AS done, "
            "SUM(status='partial') AS partial, SUM(status='error') AS err FROM agent_runs"
        ).fetchone()
    finally:
        conn.close()

    cfg = agent_module.ai.active_config()
    gateway_host = urllib.parse.urlparse(cfg["base_url"]).hostname or "" if cfg["base_url"] else ""

    return {
        "active_run": active,
        "current": current,
        "queue": {
            "pending": status.get("pending", 0),
            "analyzed": status.get("analyzed", 0),
            "error": status.get("error", 0),
            "awaiting": status.get("awaiting", 0),
            "with_card": status.get("with_card", 0),
        },
        "summary": {
            "agent_runs": agg["total"] or 0,
            "done": agg["done"] or 0,
            "partial": agg["partial"] or 0,
            "error": agg["err"] or 0,
        },
        "ai": {
            "enabled": agent_module.is_configured(),
            "model": cfg["model"],
            "gateway": gateway_host,
            "provider": cfg["provider"],
        },
        "runs": runs,
        "cron_note": "Полный пайплайн (сбор + анализ) запускается по cron раз в час",
    }


def analysis_run_payload(run_id: int) -> dict[str, Any]:
    """Деталь прогона анализа: тендеры с агентным статусом, доменом, уверенностью, шагами."""
    run = next((r for r in db.recent_runs(200) if r.get("id") == run_id), None)
    conn = db.connect()
    try:
        tenders = []
        for ar in db.agent_runs_for_pipeline(conn, run_id):
            t = conn.execute(
                "SELECT title, pipeline_status FROM tenders WHERE number=?", (ar["tender_number"],)
            ).fetchone()
            tenders.append({
                "number": ar["tender_number"],
                "title": (t["title"] if t else "") or ar["tender_number"],
                "agent_status": ar["status"] or "—",
                "domain": ar["domain"] or "",
                "confidence": ar["confidence"] or "",
                "step_count": ar["step_count"] or 0,
                "limit_reached": bool(ar["limit_reached"]),
                "tender_status": (t["pipeline_status"] if t else "") or "",
            })
    finally:
        conn.close()
    return {"run": run, "tenders": tenders}


def analysis_tender_payload(number: str) -> dict[str, Any]:
    """Трейс + карточка + документы одного тендера (последняя попытка) + история попыток."""
    conn = db.connect()
    try:
        t = conn.execute("SELECT * FROM tenders WHERE number=?", (number,)).fetchone()
        latest = db.latest_agent_run(conn, number)
        history = db.agent_runs_for_tender(conn, number)
        docs = db.documents_for(conn, number)
    finally:
        conn.close()

    card = json.loads(latest["card_json"]) if latest and latest["card_json"] else None
    steps = json.loads(latest["steps_json"]) if latest and latest["steps_json"] else []
    documents = [
        {
            "id": d["id"],
            "filename": d["filename"],
            "type": d["type"] or "file",
            "status": d["status"] or "ok",
            "size_bytes": d["size_bytes"],
        }
        for d in docs
    ]
    run_info = None
    if latest is not None:
        run_info = {
            "id": latest["id"],
            "status": latest["status"],
            "model": latest["model"],
            "step_count": latest["step_count"] or 0,
            "tool_calls": latest["tool_calls"] or 0,
            "tokens": latest["tokens"] or 0,
            "duration_sec": latest["duration_sec"],
            "limit_reached": bool(latest["limit_reached"]),
            "error": latest["error"] or "",
            "started_at": latest["started_at"],
        }
    history_list = [
        {
            "id": r["id"],
            "status": r["status"],
            "step_count": r["step_count"] or 0,
            "limit_reached": bool(r["limit_reached"]),
            "started_at": r["started_at"],
            "model": r["model"],
        }
        for r in history
    ]
    return {
        "tender": {
            "number": number,
            "title": (t["title"] if t else number) or number,
            "url": (t["url"] if t else "") or "",
            "pipeline_status": (t["pipeline_status"] if t else "") or "",
        },
        "run": run_info,
        "card": card,
        "steps": steps,
        "documents": documents,
        "history": history_list,
    }


# ── Фоновый сбор (стадия 1) ───────────────────────────────────────────────
# Сервер — ThreadingHTTPServer, так что запросы не блокируют друг друга; запуск отделяем в
# daemon-тред, чтобы POST отвечал мгновенно и сбор пережил отключение клиента. Лок сериализует
# только проверку-и-старт (single-run guard).
_crawl_lock = threading.Lock()


def _progress_writer(run_id: int):
    def cb(done: int, total: int, downloaded: int, unpacked: int, failed: int) -> None:
        db.update_run_progress(
            run_id, tenders_total=total, tenders_done=done,
            files_downloaded=downloaded, files_unpacked=unpacked, files_failed=failed,
        )
    return cb


def _run_fetch_job(run_id: int, query: str, limit: int) -> None:
    source = "fallback"
    try:
        summary = fetcher.fetch_new(query, limit, progress=_progress_writer(run_id), run_id=run_id)
        source = summary.get("source", "fallback")
        db.finish_run(run_id, summary, None, source, "completed", "")
    except Exception:
        db.finish_run(run_id, {}, None, source, "error", traceback.format_exc())


def _run_retry_job(retry_run_id: int, original_run_id: int) -> None:
    try:
        summary = fetcher.retry_run(original_run_id, progress=_progress_writer(retry_run_id))
        db.finish_run(
            retry_run_id, {"new": summary.get("downloaded", 0)}, None,
            "zakupki.gov.ru", "completed", "",
        )
    except Exception:
        db.finish_run(retry_run_id, {}, None, "zakupki.gov.ru", "error", traceback.format_exc())


def start_crawl(query: str, limit: int) -> dict[str, Any]:
    """Стартует ручной сбор (стадия 1) в фоне. Отклоняет, если уже идёт прогон."""
    with _crawl_lock:
        active = db.active_run()
        if active is not None:
            return {"started": False, "reason": "already_running", "active_run": active}
        run_id = db.start_run(query, limit, kind="fetch", stage="fetch")
    threading.Thread(target=_run_fetch_job, args=(run_id, query, limit), daemon=True).start()
    return {"started": True, "run_id": run_id}


def start_retry_run(original_run_id: int) -> dict[str, Any]:
    """Стартует фоновый повтор всех упавших загрузок прогона original_run_id."""
    with _crawl_lock:
        active = db.active_run()
        if active is not None:
            return {"started": False, "reason": "already_running", "active_run": active}
        retry_run_id = db.start_run(
            f"повтор упавших · прогон #{original_run_id}", 0, kind="fetch", stage="retry"
        )
    threading.Thread(target=_run_retry_job, args=(retry_run_id, original_run_id), daemon=True).start()
    return {"started": True, "run_id": retry_run_id}


# ── Фоновый анализ (стадия 2) ──────────────────────────────────────────────
# Общий single-run guard с сбором: сбор и анализ взаимоисключающи (одна тяжёлая работа за раз).

def _analyze_progress_writer(run_id: int):
    def cb(done: int, total: int) -> None:
        db.update_run_progress(run_id, tenders_total=total, tenders_done=done)
    return cb


def _run_analyze_job(run_id: int) -> None:
    source = db.pipeline_status().get("source", "fallback")
    try:
        summary = analyzer.analyze_new(run_id=run_id, progress=_analyze_progress_writer(run_id))
        db.finish_run(run_id, None, summary, source, "completed", "")
    except Exception:
        db.finish_run(run_id, None, {}, source, "error", traceback.format_exc())


def _run_reanalyze_job(run_id: int, number: str) -> None:
    source = db.pipeline_status().get("source", "fallback")
    try:
        db.update_run_progress(run_id, tenders_total=1, tenders_done=0)
        result = analyzer.reanalyze_tender(number, run_id=run_id)
        db.update_run_progress(run_id, tenders_total=1, tenders_done=1)
        ok = bool(result.get("ok"))
        db.finish_run(
            run_id, None, {"analyzed": 1 if ok else 0, "failed": 0 if ok else 1},
            source, "completed" if ok else "error", "" if ok else str(result.get("error", "")),
        )
    except Exception:
        db.finish_run(run_id, None, {}, source, "error", traceback.format_exc())


def start_analysis() -> dict[str, Any]:
    """Стартует батч-анализ очереди (стадия 2) в фоне. Отклоняет, если уже идёт прогон."""
    with _crawl_lock:
        active = db.active_run()
        if active is not None:
            return {"started": False, "reason": "already_running", "active_run": active}
        run_id = db.start_run("анализ очереди", 0, kind="analyze", stage="analyze")
    threading.Thread(target=_run_analyze_job, args=(run_id,), daemon=True).start()
    return {"started": True, "run_id": run_id}


def start_reanalyze(number: str) -> dict[str, Any]:
    """Стартует фоновый переанализ одного тендера. Отклоняет, если уже идёт прогон."""
    with _crawl_lock:
        active = db.active_run()
        if active is not None:
            return {"started": False, "reason": "already_running", "active_run": active}
        run_id = db.start_run(f"переанализ · {number}", 0, kind="analyze", stage="reanalyze")
    threading.Thread(target=_run_reanalyze_job, args=(run_id, number), daemon=True).start()
    return {"started": True, "run_id": run_id}


def _read_json_body(rfile: io.RawIOBase, headers: object) -> dict:
    """Читает JSON-тело POST-запроса. Возвращает {} при пустом/битом теле."""
    length = int(headers.get("Content-Length", 0) or 0)
    if length <= 0:
        return {}
    raw = rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_multipart(rfile: io.RawIOBase, headers: object) -> dict:
    """Minimal multipart/form-data parser replacing the removed cgi.FieldStorage."""
    content_type: str = headers.get("Content-Type", "")
    content_length = int(headers.get("Content-Length", 0) or 0)
    boundary: bytes | None = None
    for chunk in content_type.split(";"):
        chunk = chunk.strip()
        if chunk.startswith("boundary="):
            boundary = chunk[9:].strip().encode()
            break
    if not boundary:
        return {}
    body = rfile.read(content_length)
    fields: dict = {}
    for raw_part in body.split(b"--" + boundary)[1:]:
        if raw_part.lstrip(b"\r\n").startswith(b"--"):
            break
        if b"\r\n\r\n" not in raw_part:
            continue
        raw_headers, content = raw_part.split(b"\r\n\r\n", 1)
        if content.endswith(b"\r\n"):
            content = content[:-2]
        name: str | None = None
        filename: str | None = None
        for line in raw_headers.decode("utf-8", errors="replace").splitlines():
            lower = line.lower()
            if "content-disposition" in lower:
                for item in line.split(";"):
                    item = item.strip()
                    if item.startswith("name="):
                        name = item[5:].strip('"')
                    elif item.startswith("filename="):
                        filename = item[9:].strip('"')
        if name:
            fields[name] = type("_Field", (), {"filename": filename, "file": io.BytesIO(content)})()
    return fields


class TenderHandler(BaseHTTPRequestHandler):
    server_version = "TenderParserPrototype/0.2"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json({"status": "ok", "time": datetime.now().isoformat(timespec="seconds")})
            return
        if parsed.path == "/api/requirements":
            self.send_json(requirements_payload())
            return
        if parsed.path == "/api/tenders":
            query = urllib.parse.parse_qs(parsed.query)
            filters = {
                "query": query.get("query", [""])[0].strip(),
                "date_from": query.get("date_from", [""])[0].strip(),
                "date_to": query.get("date_to", [""])[0].strip(),
                "domain": query.get("domain", [""])[0].strip(),
                "limit": query.get("limit", ["50"])[0],
            }
            self.send_json(db.query_tenders(filters))
            return
        if parsed.path == "/api/pipeline":
            self.send_json(db.pipeline_status())
            return
        if parsed.path == "/api/crawler":
            self.send_json(crawler_payload())
            return
        if parsed.path == "/api/crawler/run":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                run_id = int(query.get("id", ["0"])[0])
            except ValueError:
                run_id = 0
            self.send_json(crawler_run_payload(run_id))
            return
        if parsed.path == "/api/crawler/tender":
            query = urllib.parse.parse_qs(parsed.query)
            self.send_json(crawler_tender_payload(query.get("number", [""])[0].strip()))
            return
        if parsed.path == "/api/analysis":
            self.send_json(analysis_payload())
            return
        if parsed.path == "/api/analysis/run":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                run_id = int(query.get("id", ["0"])[0])
            except ValueError:
                run_id = 0
            self.send_json(analysis_run_payload(run_id))
            return
        if parsed.path == "/api/analysis/tender":
            query = urllib.parse.parse_qs(parsed.query)
            self.send_json(analysis_tender_payload(query.get("number", [""])[0].strip()))
            return
        if parsed.path == "/api/file":
            query = urllib.parse.parse_qs(parsed.query)
            self.serve_file(query.get("id", [""])[0])
            return
        if parsed.path == "/crawler":
            self.send_response(302)
            self.send_header("Location", "/crawler.html")
            self.end_headers()
            return
        if parsed.path == "/analysis":
            self.send_response(302)
            self.send_header("Location", "/analysis.html")
            self.end_headers()
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/upload":
            self.handle_upload()
            return
        if parsed.path == "/api/crawler/start":
            body = _read_json_body(self.rfile, self.headers)
            # Пустой запрос разрешён: сбор «последних опубликованных» без фильтра по теме.
            query = str(body.get("query", "")).strip()
            try:
                limit = int(body.get("limit", 10))
            except (TypeError, ValueError):
                limit = 10
            limit = max(1, min(limit, 50))
            result = start_crawl(query, limit)
            self.send_json(result, 200 if result.get("started") else 409)
            return
        if parsed.path == "/api/crawler/retry-tender":
            body = _read_json_body(self.rfile, self.headers)
            number = str(body.get("number", "")).strip()
            if not number:
                self.send_json({"error": "number обязателен"}, 400)
                return
            if db.active_run() is not None:
                self.send_json({"error": "already_running"}, 409)
                return
            self.send_json(fetcher.retry_tender(number))
            return
        if parsed.path == "/api/crawler/retry-run":
            body = _read_json_body(self.rfile, self.headers)
            try:
                run_id = int(body.get("run_id", 0))
            except (TypeError, ValueError):
                run_id = 0
            if not run_id:
                self.send_json({"error": "run_id обязателен"}, 400)
                return
            result = start_retry_run(run_id)
            self.send_json(result, 200 if result.get("started") else 409)
            return
        if parsed.path == "/api/analysis/start":
            result = start_analysis()
            self.send_json(result, 200 if result.get("started") else 409)
            return
        if parsed.path == "/api/analysis/reanalyze":
            body = _read_json_body(self.rfile, self.headers)
            number = str(body.get("number", "")).strip()
            if not number:
                self.send_json({"error": "number обязателен"}, 400)
                return
            result = start_reanalyze(number)
            self.send_json(result, 200 if result.get("started") else 409)
            return
        self.send_json({"error": "Not found"}, 404)

    def handle_upload(self) -> None:
        form = _parse_multipart(self.rfile, self.headers)
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            self.send_json({"error": "Файл не передан"}, 400)
            return
        safe_name = re.sub(r"[^A-Za-zА-Яа-я0-9_.() -]+", "_", Path(upload.filename).name)
        target = UPLOAD_DIR / f"{int(time.time())}_{safe_name}"
        with target.open("wb") as file_obj:
            shutil.copyfileobj(upload.file, file_obj)
        text = extract_text_from_file(target)
        virtual_tender = {
            "number": "LOCAL-" + datetime.now().strftime("%Y%m%d%H%M%S"),
            "title": safe_name,
            "customer": "Загруженный комплект документации",
            "price": 0,
            "method": "",
            "law": "Локальный анализ",
            "publish_date": "",
            "deadline": "",
            "okpd2": "61" if "61" in text else "",
            "status": "В обработке",
            "url": "",
        }
        analysis = analyze_tender(virtual_tender, text)
        document = {
            "id": "upload-" + datetime.now().strftime("%Y%m%d%H%M%S"),
            "name": safe_name,
            "type": target.suffix.lower().lstrip(".") or "file",
            "text": text,
            "highlights": collect_evidence(virtual_tender, text),
        }
        self.send_json(
            {
                "file": safe_name,
                "stored_as": str(target),
                "characters": len(text),
                "analysis": analysis,
                "document": document,
                "preview": text[:1200],
            }
        )

    def serve_file(self, doc_id_raw: str) -> None:
        """Отдаёт скачанный файл по id документа. Защита от path traversal: реальный путь
        обязан лежать внутри TENDERS_DIR. Стримит с диска, не держа файл целиком в памяти."""
        try:
            doc_id = int(doc_id_raw)
        except (TypeError, ValueError):
            self.send_json({"error": "Некорректный id"}, 400)
            return
        conn = db.connect()
        try:
            doc = db.document_by_id(conn, doc_id)
        finally:
            conn.close()
        if doc is None or not doc["path"] or doc["status"] == "failed":
            self.send_json({"error": "Файл не найден"}, 404)
            return
        target = Path(doc["path"]).resolve()
        base = db.TENDERS_DIR.resolve()
        if not str(target).startswith(str(base) + os.sep) or not target.is_file():
            self.send_json({"error": "Файл не найден"}, 404)
            return
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        disposition = urllib.parse.quote(target.name)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{disposition}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with target.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve()) + os.sep) or not target.exists() or not target.is_file():
            self.send_json({"error": "Not found"}, 404)
            return
        content = target.read_bytes()
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else ""))
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), TenderHandler)
    print(f"Tender Parser: http://{host}:{port}  (данные из data/tenders.db — наполните через python pipeline.py)")
    httpd.serve_forever()


if __name__ == "__main__":
    run(port=int(os.environ.get("PORT", "8000")))
