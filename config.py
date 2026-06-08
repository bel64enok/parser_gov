"""Загрузка переменных окружения из .env (stdlib, без зависимостей).

Импортировать ПЕРВЫМ — до `ai`/`analyzer`, т.к. `ai.MODEL` читается на импорте.
Значения, уже заданные в окружении (например, через cron/systemd), имеют приоритет —
.env их не перетирает (`setdefault`).

Конфиг ИИ-стадии: RMR_GATEWAY_URL, RMR_API_KEY, RMR_MODEL.
Сам .env в репозиторий не коммитится (см. .gitignore).
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"


def load_env(path: Path | None = None) -> None:
    env_path = path or ENV_PATH
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)  # окружение важнее .env


load_env()
