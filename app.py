"""Стадия 3: backend. Отдаёт обработанные тендеры из локального хранилища (SQLite),
с фильтрами по датам/домену/тексту. Ничего не парсит вживую — это делает cron-пайплайн.

Запуск:  python app.py   (PORT=9000 python app.py — другой порт)
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import shutil
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import db
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


def admin_payload() -> dict[str, Any]:
    """Данные для страницы мониторинга /admin: прогоны, очередь, конфиг ИИ."""
    import ai as ai_module

    runs = db.recent_runs(50)
    status = db.pipeline_status()

    active = next((r for r in runs if r.get("status") == "running"), None)
    last_done = next((r for r in runs if r.get("status") in ("completed", "error", "stalled")), None)

    conn = db.connect()
    try:
        pending_rows = conn.execute(
            "SELECT number, title FROM tenders WHERE pipeline_status='fetched' "
            "ORDER BY fetched_at DESC LIMIT 25"
        ).fetchall()
        error_rows = conn.execute(
            "SELECT number, title, error FROM tenders WHERE pipeline_status='error' "
            "ORDER BY fetched_at DESC LIMIT 25"
        ).fetchall()
    finally:
        conn.close()

    gateway_url = os.environ.get("RMR_GATEWAY_URL", "")
    gateway_host = urllib.parse.urlparse(gateway_url).hostname or "" if gateway_url else ""

    return {
        "current": {
            "is_running": bool(active),
            "active_run": active,
            "last_run": last_done,
            "source": status.get("source"),
            "cron_note": "Пайплайн запускается по cron раз в час",
        },
        "runs": runs,
        "queue": {
            "pending": status.get("pending", 0),
            "error": status.get("error", 0),
            "pending_sample": [{"number": r["number"], "title": r["title"]} for r in pending_rows],
            "error_sample": [
                {"number": r["number"], "title": r["title"], "error": r["error"]}
                for r in error_rows
            ],
        },
        "config": {
            "ai_enabled": ai_module.is_configured(),
            "ai_model": ai_module.MODEL,
            "ai_gateway": gateway_host,                # только хост, без ключа
            "ai_configured_gateway": bool(gateway_url),
            "text_budget": ai_module.TEXT_BUDGET,
            "system_prompt": ai_module.SYSTEM_PROMPT,
        },
    }


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
        if parsed.path == "/api/admin":
            self.send_json(admin_payload())
            return
        if parsed.path == "/admin":
            self.send_response(302)
            self.send_header("Location", "/admin.html")
            self.end_headers()
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/upload":
            self.send_json({"error": "Not found"}, 404)
            return
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

    def serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists() or not target.is_file():
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
