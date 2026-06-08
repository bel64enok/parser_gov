"""Стадия 1: скачивание. Тянет листинг ЕИС, кладёт карточку и вложения на диск, пишет в БД.

Идемпотентна: тендеры, уже присутствующие в БД, пропускаются. Один сбойный тендер не роняет
весь прогон (помечается pipeline_status='error').
"""
from __future__ import annotations

import socket
import urllib.error
from datetime import datetime
from typing import Any

import db
import scrape
from rules import sample_tenders, to_iso_date


def _store_tender(conn, tender: dict[str, Any]) -> int:
    """Скачивает карточку + вложения одного тендера, пишет строки в БД. Возвращает число документов."""
    number = tender["number"]
    folder = db.tender_dir(number)

    # 1) HTML карточки
    card_html = scrape.fetch_card_html(tender.get("url", "")) if tender.get("url") else ""
    doc_count = 0
    if card_html:
        card_path = folder / "card.html"
        card_path.write_text(card_html, encoding="utf-8")
        db.add_document(conn, number, "card.html", str(card_path), "html")
        doc_count += 1

    # 2) Вложения (best-effort)
    try:
        attachments = scrape.fetch_attachments(tender, folder)
    except Exception as exc:  # изоляция самой хрупкой части
        print(f"  [fetch] вложения {number} не скачаны: {exc.__class__.__name__}: {exc}")
        attachments = []
    for att in attachments:
        db.add_document(conn, number, att["filename"], att["path"], att["type"])
        doc_count += 1

    record = dict(tender)
    record["publish_date_iso"] = to_iso_date(tender.get("publish_date"))
    record["deadline_iso"] = to_iso_date(tender.get("deadline"))
    record["source"] = tender.get("source", "zakupki.gov.ru")
    record["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    record["pipeline_status"] = "fetched"
    record["error"] = ""
    db.upsert_tender(conn, record)
    return doc_count


def fetch_new(query: str = "мобильная связь", limit: int = 10) -> dict[str, Any]:
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

        new_count = 0
        skipped = 0
        for tender in tenders[:limit]:
            number = tender.get("number")
            if not number:
                continue
            if db.tender_exists(conn, number):
                skipped += 1
                continue
            tender["source"] = source
            try:
                docs = _store_tender(conn, tender)
                new_count += 1
                print(f"[fetch] {number}: сохранено документов {docs}")
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
                })
                db.upsert_tender(conn, record)
                print(f"[fetch] {number}: ошибка — {exc}")

        summary = {"new": new_count, "skipped": skipped, "source": source}
        print(f"[fetch] готово: новых {new_count}, пропущено (уже в БД) {skipped}, источник {source}")
        return summary
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "мобильная связь"
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    fetch_new(q, lim)
