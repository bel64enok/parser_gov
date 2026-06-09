"""Точка входа пайплайна для cron: стадия 1 (скачивание) → стадия 2 (ИИ-обработка).

Запуск вручную:    python pipeline.py [запрос] [лимит]
Запуск по cron:    0 * * * * cd /путь/к/parser_gov && .venv/bin/python pipeline.py >> data/pipeline.log 2>&1
"""
from __future__ import annotations

import config  # noqa: F401 — загружает .env до импорта analyzer/ai

import os
import sys
import traceback
from datetime import datetime

import analyzer
import db
import fetcher


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PIPELINE_QUERY", "мобильная связь")
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("PIPELINE_LIMIT", "10"))

    print(f"=== pipeline старт {datetime.now().isoformat(timespec='seconds')} | запрос='{query}' лимит={limit} ===")
    run_id = db.start_run(query, limit)
    fetch_summary: dict = {}
    analyze_summary: dict = {}
    source = "fallback"
    try:
        # run_id + прогресс: тендеры/документы привязываются к прогону (drill-down во вкладке
        # «Сбор документов») и cron-прогон тоже получает счётчики файлов.
        def _progress(done: int, total: int, dl: int, up: int, failed: int) -> None:
            db.update_run_progress(
                run_id, tenders_total=total, tenders_done=done,
                files_downloaded=dl, files_unpacked=up, files_failed=failed,
            )

        fetch_summary = fetcher.fetch_new(query, limit, progress=_progress, run_id=run_id)
        source = fetch_summary.get("source", "fallback")
        analyze_summary = analyzer.analyze_new()
        db.finish_run(run_id, fetch_summary, analyze_summary, source, "completed", "")
        print(f"=== pipeline финиш: {fetch_summary} | {analyze_summary} ===")
    except Exception as exc:
        tb = traceback.format_exc()
        db.finish_run(run_id, fetch_summary, analyze_summary, source, "error", tb)
        print(f"=== pipeline ОШИБКА: {exc.__class__.__name__}: {exc} ===")
        print(tb)
        raise


if __name__ == "__main__":
    main()
