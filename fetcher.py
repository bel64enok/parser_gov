"""Стадия 1: скачивание + распаковка. Тянет листинг ЕИС, кладёт карточку и вложения на диск
(архивы распаковывает рядом), пишет в БД per-файловый статус.

Идемпотентна: тендеры, уже присутствующие в БД, пропускаются. Один сбойный тендер не роняет
весь прогон (помечается pipeline_status='error'). Упавшие загрузки фиксируются строкой документа
со status='failed' (раньше молча терялись) — это даёт видимость и повтор.
"""
from __future__ import annotations

import socket
import urllib.error
from datetime import datetime
from typing import Any, Callable

import db
import scrape
from rules import sample_tenders, to_iso_date

# progress(done, total, downloaded, unpacked, failed) — необязательный колбэк для живого прогресса
ProgressCb = Callable[[int, int, int, int, int], None]


def _record_attachments(conn, number: str, attachments: list[dict[str, Any]], run_id, writer) -> dict[str, int]:
    """Пишет записи вложений в БД, связывая members архива с parent_doc_id по порядку.

    writer — db.add_document (первичный сбор) или db.upsert_document (ретрай: failed→ok на месте).
    Метрики: архив и обычные файлы → downloaded; члены архива → unpacked; сбои → failed.
    """
    parent_by_path: dict[str, int] = {}
    counts = {"downloaded": 0, "unpacked": 0, "failed": 0}
    for att in attachments:
        parent_id = parent_by_path.get(att.get("_parent_path")) if att.get("is_member") else None
        doc_id = writer(
            conn, number, att["filename"], att.get("path", "") or "", att.get("type", "file"),
            status=att.get("status", "ok"), error=att.get("error", ""),
            size_bytes=att.get("size_bytes"), source_url=att.get("source_url", ""),
            parent_doc_id=parent_id, run_id=run_id,
        )
        status = att.get("status", "ok")
        if status == "archive":
            parent_by_path[att["path"]] = doc_id
            counts["downloaded"] += 1
        elif status == "unpacked":
            counts["unpacked"] += 1
        elif status == "failed":
            counts["failed"] += 1
        else:
            counts["downloaded"] += 1
    return counts


def _store_tender(conn, tender: dict[str, Any], run_id=None) -> dict[str, int]:
    """Скачивает карточку + вложения одного тендера, пишет строки в БД.

    Возвращает счётчики {downloaded, unpacked, failed, docs}.
    """
    number = tender["number"]
    folder = db.tender_dir(number)
    counts = {"downloaded": 0, "unpacked": 0, "failed": 0, "docs": 0}

    # 1) HTML карточки
    card_html = scrape.fetch_card_html(tender.get("url", "")) if tender.get("url") else ""
    if card_html:
        card_path = folder / "card.html"
        card_path.write_text(card_html, encoding="utf-8")
        db.add_document(
            conn, number, "card.html", str(card_path), "html",
            status="ok", size_bytes=len(card_html.encode("utf-8")),
            source_url=tender.get("url", ""), run_id=run_id,
        )
        counts["downloaded"] += 1
        counts["docs"] += 1

    # 2) Вложения (best-effort) + распаковка архивов
    try:
        attachments = scrape.fetch_attachments(tender, folder)
    except Exception as exc:  # изоляция самой хрупкой части
        print(f"  [fetch] вложения {number} не скачаны: {exc.__class__.__name__}: {exc}")
        attachments = []
    att_counts = _record_attachments(conn, number, attachments, run_id, db.add_document)
    counts["downloaded"] += att_counts["downloaded"]
    counts["unpacked"] += att_counts["unpacked"]
    counts["failed"] += att_counts["failed"]
    counts["docs"] += len(attachments)

    record = dict(tender)
    record["publish_date_iso"] = to_iso_date(tender.get("publish_date"))
    record["deadline_iso"] = to_iso_date(tender.get("deadline"))
    record["source"] = tender.get("source", "zakupki.gov.ru")
    record["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    record["pipeline_status"] = "fetched"
    record["error"] = ""
    record["run_id"] = run_id
    db.upsert_tender(conn, record)
    return counts


def fetch_new(
    query: str = "мобильная связь",
    limit: int = 10,
    progress: ProgressCb | None = None,
    run_id=None,
) -> dict[str, Any]:
    conn = db.connect()
    try:
        source = "zakupki.gov.ru"
        try:
            page_html, _ = scrape.fetch_zakupki_html(query, limit)
            tenders = scrape.parse_zakupki_results(page_html)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            print(f"[fetch] ЕИС недоступна ({exc.__class__.__name__}) — сеют демо-данные")
            tenders = []

        if not tenders:
            # ЕИС недоступна/не распозналась — сеем демо, чтобы прототип был наполнен
            tenders = sample_tenders()
            source = "fallback"

        batch = tenders[:limit]
        total = len(batch)
        new_count = 0
        skipped = 0
        done = 0
        totals = {"downloaded": 0, "unpacked": 0, "failed": 0}

        def emit() -> None:
            if progress:
                progress(done, total, totals["downloaded"], totals["unpacked"], totals["failed"])

        emit()  # стартовое значение (0/total), чтобы UI сразу показал общий объём
        for tender in batch:
            number = tender.get("number")
            if not number:
                continue
            if db.tender_exists(conn, number):
                skipped += 1
                done += 1
                emit()
                continue
            tender["source"] = source
            try:
                counts = _store_tender(conn, tender, run_id=run_id)
                new_count += 1
                totals["downloaded"] += counts["downloaded"]
                totals["unpacked"] += counts["unpacked"]
                totals["failed"] += counts["failed"]
                print(f"[fetch] {number}: сохранено документов {counts['docs']}")
            except Exception as exc:
                # тендер всё равно фиксируем со статусом error, не роняя прогон
                record = dict(tender)
                record.update({
                    "publish_date_iso": to_iso_date(tender.get("publish_date")),
                    "deadline_iso": to_iso_date(tender.get("deadline")),
                    "source": source,
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "pipeline_status": "error",
                    "error": f"{exc.__class__.__name__}: {exc}",
                    "run_id": run_id,
                })
                db.upsert_tender(conn, record)
                print(f"[fetch] {number}: ошибка — {exc}")
            done += 1
            emit()

        summary = {
            "new": new_count, "skipped": skipped, "source": source,
            "downloaded": totals["downloaded"], "unpacked": totals["unpacked"], "failed": totals["failed"],
        }
        print(f"[fetch] готово: новых {new_count}, пропущено (уже в БД) {skipped}, источник {source}")
        return summary
    finally:
        conn.close()


def retry_tender(number: str) -> dict[str, Any]:
    """Перекачивает вложения одного тендера: чистит прежние неудачи и повторяет загрузку.

    Документы остаются привязаны к исходному прогону тендера (run_id из строки tenders), чтобы
    деталь того прогона освежилась. Успешно скачанные ранее файлы повторная загрузка не дублирует
    (upsert по UNIQUE). Возвращает {number, downloaded, unpacked, failed}.
    """
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM tenders WHERE number=?", (number,)).fetchone()
        if row is None:
            return {"error": "not_found", "number": number}
        # чистим прежние неудачи — успешные файлы и архивы оставляем как есть
        conn.execute("DELETE FROM documents WHERE tender_number=? AND status='failed'", (number,))
        conn.commit()
        tender = {key: row[key] for key in row.keys()}
        folder = db.tender_dir(number)
        try:
            attachments = scrape.fetch_attachments(tender, folder)
        except Exception as exc:
            return {"error": f"{exc.__class__.__name__}: {exc}", "number": number}
        counts = _record_attachments(conn, number, attachments, row["run_id"], db.upsert_document)
        return {"number": number, **counts}
    finally:
        conn.close()


def retry_run(run_id: int, progress: ProgressCb | None = None) -> dict[str, Any]:
    """Повторяет загрузку для всех тендеров запуска run_id, где есть упавшие файлы."""
    conn = db.connect()
    try:
        rows = db.failed_documents_for_run(conn, run_id)
    finally:
        conn.close()
    numbers: list[str] = []
    for r in rows:
        if r["tender_number"] not in numbers:
            numbers.append(r["tender_number"])

    total = len(numbers)
    done = 0
    totals = {"downloaded": 0, "unpacked": 0, "failed": 0}
    if progress:
        progress(0, total, 0, 0, 0)
    for number in numbers:
        res = retry_tender(number)
        for key in totals:
            totals[key] += int(res.get(key, 0) or 0)
        done += 1
        if progress:
            progress(done, total, totals["downloaded"], totals["unpacked"], totals["failed"])
    return {"tenders": total, **totals}


if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "мобильная связь"
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    fetch_new(q, lim)
