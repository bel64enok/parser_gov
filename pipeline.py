"""Точка входа пайплайна для cron: стадия 1 (скачивание) → стадия 2 (ИИ-обработка).

Запуск вручную:    python pipeline.py [запрос] [лимит]
Запуск по cron:    0 * * * * cd /путь/к/parser_gov && .venv/bin/python pipeline.py >> data/pipeline.log 2>&1
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import analyzer
import fetcher


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PIPELINE_QUERY", "мобильная связь")
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("PIPELINE_LIMIT", "10"))

    print(f"=== pipeline старт {datetime.now().isoformat(timespec='seconds')} | запрос='{query}' лимит={limit} ===")
    fetch_summary = fetcher.fetch_new(query, limit)
    analyze_summary = analyzer.analyze_new()
    print(f"=== pipeline финиш: {fetch_summary} | {analyze_summary} ===")


if __name__ == "__main__":
    main()
