"""Скрейпинг zakupki.gov.ru: листинг (перенос из app.py) + скачивание вложений (новое).

Сетевая работа изолирована здесь и вызывается только стадией 1 (fetcher), не из HTTP-пути.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import socket
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from rules import parse_money

ZAKUPKI_BASE = "https://zakupki.gov.ru"

HEADERS = {
    "User-Agent": "Mozilla/5.0 TenderParserPrototype/0.1",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def build_search_url(query: str, page: int = 1, limit: int = 10, refresh_token: str | None = None) -> str:
    # Пустой запрос = «просто последние»: сортируем по дате размещения (свежие закупки),
    # а не по дате обновления (которая поднимает наверх правки старых тендеров).
    sort_by = "UPDATE_DATE" if (query or "").strip() else "PUBLISH_DATE"
    params = {
        "searchString": query,
        "morphology": "on",
        "search-filter": "Дате размещения",
        "pageNumber": str(page),
        "sortDirection": "false",
        "recordsPerPage": f"_{limit}",
        "showLotsInfoHidden": "false",
        "sortBy": sort_by,
        "fz44": "on",
        "fz223": "on",
        "pc": "on",
        "currencyIdGeneral": "-1",
    }
    if refresh_token:
        params["_refresh"] = refresh_token
    return f"{ZAKUPKI_BASE}/epz/order/extendedsearch/results.html?{urllib.parse.urlencode(params)}"


def http_get(url: str, timeout: int = 20) -> tuple[bytes, str, str]:
    """Возвращает (тело, content-type, имя файла из Content-Disposition или '')."""
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        content_type = response.headers.get("Content-Type", "")
        disposition = response.headers.get("Content-Disposition", "")
        filename = ""
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition)
        if match:
            filename = urllib.parse.unquote(match.group(1)).strip()
        return body, content_type, filename


def fetch_zakupki_html(query: str, limit: int, refresh_token: str | None = None) -> tuple[str, str]:
    url = build_search_url(query, limit=limit, refresh_token=refresh_token)
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=14) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="ignore"), url


def strip_tags(value: str) -> str:
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def find_near(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            return strip_tags(match.group(1))
    return ""


def parse_zakupki_results(page_html: str) -> list[dict[str, Any]]:
    chunks = re.split(r'class="[^"]*(?:search-registry-entry-block|registry-entry__form)[^"]*"', page_html)
    if len(chunks) <= 1:
        chunks = re.split(r"(?=№\s*\d{11,})", page_html)
    tenders = []
    for raw in chunks[1:]:
        chunk = raw[:20000]
        text = strip_tags(chunk)
        number = find_near(chunk, [r"№\s*</span>\s*<a[^>]*>\s*([^<]+)", r"№\s*([0-9]{8,})"])
        if not number:
            number_match = re.search(r"\b\d{11,19}\b", text)
            number = number_match.group(0) if number_match else ""
        if not number:
            continue
        title = find_near(
            chunk,
            [
                r'class="[^"]*registry-entry__body-value[^"]*"[^>]*>\s*<a[^>]*>([\s\S]*?)</a>',
                r'<a[^>]+href="[^"]*common-info[^"]*"[^>]*>([\s\S]*?)</a>',
            ],
        )
        if not title:
            title = re.sub(r"^.*?№\s*" + re.escape(number), "", text)[:220].strip()
        href_match = re.search(r'href="([^"]*common-info[^"]*)"', chunk)
        url = urllib.parse.urljoin(ZAKUPKI_BASE, href_match.group(1)) if href_match else build_search_url(number)
        price = find_near(
            chunk,
            [
                r"Начальная[^<]{0,80}</[^>]+>\s*<[^>]+>([\s\S]*?)</",
                r"Цена контракта[^<]{0,80}</[^>]+>\s*<[^>]+>([\s\S]*?)</",
            ],
        )
        publish_date = find_near(chunk, [r"Размещено\s*</[^>]+>\s*<[^>]+>([\d\.]+)"])
        deadline = find_near(chunk, [r"Окончание подачи[^<]*</[^>]+>\s*<[^>]+>([\d\.\s:]+)"])
        customer = find_near(chunk, [r"Заказчик[^<]*</[^>]+>\s*<[^>]+>([\s\S]*?)</"])
        method = find_near(chunk, [r"Способ определения[^<]*</[^>]+>\s*<[^>]+>([\s\S]*?)</"])
        tenders.append(
            {
                "number": number.strip(),
                "title": title or "Закупка без распознанного названия",
                "customer": customer or "Не распознано",
                "price": parse_money(price),
                "method": method or "Не распознано",
                "law": "44-ФЗ" if "44-ФЗ" in text else "223-ФЗ" if "223-ФЗ" in text else "ЕИС",
                "publish_date": publish_date,
                "deadline": deadline[:10] if deadline else "",
                "okpd2": "",
                "status": "Исполнение" if "Исполнение" in text else "Подача заявок" if "Подача заявок" in text else "",
                "contract_status": "Исполнение" if "Исполнение" in text else "Подача заявок" if "Подача заявок" in text else "",
                "appeared_at": publish_date,
                "url": url,
            }
        )
    return tenders[:20]


def fetch_card_html(url: str) -> str:
    """Скачивает HTML карточки извещения (страница common-info)."""
    try:
        body, _, _ = http_get(url, timeout=14)
        return body.decode("utf-8", errors="ignore")
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError):
        return ""


def _documents_page_urls(number: str, law: str) -> list[str]:
    """Кандидаты URL страницы со списком вложений (44-ФЗ и 223-ФЗ различаются)."""
    if law == "223-ФЗ":
        return [
            f"{ZAKUPKI_BASE}/223/purchase/public/purchase/info/documents.html?regNumber={number}",
        ]
    return [
        f"{ZAKUPKI_BASE}/epz/order/notice/notice-documents/list.html?regNumber={number}",
        f"{ZAKUPKI_BASE}/epz/order/notice/ea44/view/documents.html?regNumber={number}",
    ]


def _guess_extension(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix:
        return suffix
    ct = content_type.lower()
    if "pdf" in ct:
        return ".pdf"
    if "word" in ct or "msword" in ct or "officedocument.wordprocessing" in ct:
        return ".docx"
    if "sheet" in ct or "excel" in ct:
        return ".xlsx"
    if "zip" in ct:
        return ".zip"
    return ".bin"


def _unpack_archive(zip_path: Path, dest_dir: Path, source_url: str) -> list[dict[str, Any]]:
    """Распаковывает .zip в dest_dir/<stem>_unpacked/ и возвращает member-записи.

    Защита от zip-slip: путь каждого члена резолвится и обязан лежать внутри папки распаковки.
    Без рекурсии в стадии 1 — вложенный .zip остаётся отдельным member, его развернёт стадия 2.
    Имя члена в БД неймспейсится стемом архива (UNIQUE(tender, filename) — иначе коллизии).
    """
    members: list[dict[str, Any]] = []
    base = (dest_dir / f"{zip_path.stem}_unpacked").resolve()
    stem = zip_path.stem

    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        return [{
            "filename": f"{stem} (архив повреждён)", "path": "", "type": "zip",
            "status": "failed", "error": f"{exc.__class__.__name__}: {exc}",
            "size_bytes": None, "source_url": source_url, "is_member": True,
            "_parent_path": str(zip_path),
        }]

    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            member_name = Path(info.filename).name or "file"
            member_type = Path(member_name).suffix.lstrip(".").lower() or "file"
            key = f"{stem}/{info.filename}"
            target = (base / info.filename).resolve()
            if not str(target).startswith(str(base) + os.sep):
                members.append({
                    "filename": f"{key} (zip-slip отклонён)", "path": "", "type": member_type,
                    "status": "failed", "error": "zip-slip: путь за пределами папки распаковки",
                    "size_bytes": info.file_size, "source_url": source_url,
                    "is_member": True, "_parent_path": str(zip_path),
                })
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:  # RuntimeError: zip с паролем
                members.append({
                    "filename": key, "path": "", "type": member_type,
                    "status": "failed", "error": f"{exc.__class__.__name__}: {exc}",
                    "size_bytes": info.file_size, "source_url": source_url,
                    "is_member": True, "_parent_path": str(zip_path),
                })
                continue
            members.append({
                "filename": key, "path": str(target), "type": member_type,
                "status": "unpacked", "error": "", "size_bytes": info.file_size,
                "source_url": source_url, "is_member": True, "_parent_path": str(zip_path),
            })
    return members


def fetch_attachments(tender: dict[str, Any], dest_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Best-effort: находит, скачивает и распаковывает вложения извещения в dest_dir.

    Возвращает список записей с per-файловым статусом (в т.ч. упавшие — они больше не теряются):
    ``{filename, path, type, status (ok|failed|archive|unpacked), error, size_bytes,
    source_url, is_member, _parent_path}``. Для архива запись со status='archive' идёт сразу перед
    его members (status='unpacked'/'failed') — fetcher так связывает parent_doc_id по порядку.
    Структура страниц ЕИС нестабильна; сетевые сбои фиксируются записью status='failed', а не молча.
    """
    number = tender.get("number", "")
    law = tender.get("law", "")
    saved: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    download_links: list[str] = []

    for page_url in _documents_page_urls(number, law):
        try:
            body, _, _ = http_get(page_url, timeout=20)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError):
            continue
        page = body.decode("utf-8", errors="ignore")
        for match in re.finditer(r'href="([^"]*filestore[^"]*download[^"]*)"', page, re.I):
            link = urllib.parse.urljoin(ZAKUPKI_BASE, html.unescape(match.group(1)))
            if link not in seen_urls:
                seen_urls.add(link)
                download_links.append(link)
        if download_links:
            break  # страница распознана — дальше не пробуем

    for index, link in enumerate(download_links[:limit], start=1):
        try:
            body, content_type, server_name = http_get(link, timeout=30)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as exc:
            saved.append({
                "filename": f"вложение {index} (не скачано)", "path": "", "type": "file",
                "status": "failed", "error": f"{exc.__class__.__name__}: {exc}",
                "size_bytes": None, "source_url": link, "is_member": False, "_parent_path": None,
            })
            continue
        if not body:
            saved.append({
                "filename": f"вложение {index} (пустой ответ)", "path": "", "type": "file",
                "status": "failed", "error": "пустой ответ от сервера", "size_bytes": 0,
                "source_url": link, "is_member": False, "_parent_path": None,
            })
            continue
        base_name = server_name or f"attachment_{index}"
        ext = _guess_extension(base_name, content_type)
        if not Path(base_name).suffix:
            base_name = f"{base_name}{ext}"
        safe_name = re.sub(r"[^A-Za-zА-Яа-я0-9_.() -]+", "_", Path(base_name).name)[:120]
        file_type = ext.lstrip(".") or "file"
        target = dest_dir / safe_name
        try:
            target.write_bytes(body)
        except OSError as exc:
            saved.append({
                "filename": safe_name, "path": "", "type": file_type,
                "status": "failed", "error": f"запись на диск: {exc}", "size_bytes": len(body),
                "source_url": link, "is_member": False, "_parent_path": None,
            })
            continue

        if file_type == "zip":
            saved.append({
                "filename": safe_name, "path": str(target), "type": "zip",
                "status": "archive", "error": "", "size_bytes": len(body),
                "source_url": link, "is_member": False, "_parent_path": None,
            })
            saved.extend(_unpack_archive(target, dest_dir, link))
        else:
            saved.append({
                "filename": safe_name, "path": str(target), "type": file_type,
                "status": "ok", "error": "", "size_bytes": len(body),
                "source_url": link, "is_member": False, "_parent_path": None,
            })

    return saved
