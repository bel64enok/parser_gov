"""Детерминированный слой анализа на правилах (домены, скоринг, чеклист, evidence).

Перенесён из app.py без изменения логики. ИИ (ai.py) добавляется поверх, не заменяя это.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

DOMAIN_TRIGGERS: dict[str, list[str]] = {
    "МОБ": [
        "мобильная связь",
        "сотовая связь",
        "сим-карт",
        "sim",
        "смс",
        "sms",
        "lte",
        "4g",
        "5g",
        "радиотелефон",
        "подвижной связи",
        "подвижная связь",
        "голосовая связь",
        "мобильный интернет",
    ],
    "ФИКС": [
        "доступ в интернет",
        "фиксированная связь",
        "канал связи",
        "vpn",
        "волс",
        "ip-телефони",
        "телематическ",
    ],
    "SI": ["системная интеграция", "оборудование", "монтаж", "пусконалад"],
    "М2М": ["m2m", "iot", "телеметр", "датчик", "мониторинг транспорта"],
    "BigData": ["big data", "большие данные", "аналитика данных", "скоринг"],
    "Cloud": ["облач", "iaas", "saas", "виртуальн", "цод", "хостинг"],
    "SBVAS": ["vas", "контент", "массовые вызовы", "короткий номер", "ivr"],
}

PRODUCT_CATALOG = {
    "Корпоративная мобильная связь": ["мобильная связь", "сотовая связь", "радиотелефон", "sim", "сим-карт"],
    "Мобильный интернет": ["мобильный интернет", "lte", "4g", "5g"],
    "SMS и уведомления": ["sms", "смс", "коротк", "рассыл"],
    "Фиксированный интернет": ["доступ в интернет", "канал связи", "волс"],
    "Облачные сервисы": ["облач", "виртуальн", "iaas", "saas"],
}

SAMPLE_TENDERS = [
    {
        "number": "0373200123426000311",
        "title": "Оказание услуг подвижной радиотелефонной связи и мобильного доступа в интернет",
        "customer": "ГБУ города Москвы",
        "price": 8_450_000,
        "method": "Электронный аукцион",
        "law": "44-ФЗ",
        "publish_date": "02.06.2026",
        "deadline": "11.06.2026",
        "appeared_at": "05.06.2026 09:20",
        "contract_status": "Исполнение",
        "okpd2": "61.20.11.000",
        "status": "Исполнение",
        "url": "https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=0373200123426000311",
    },
    {
        "number": "0173100003226000042",
        "title": "Оказание услуг фиксированного доступа к сети Интернет и каналов связи",
        "customer": "Федеральное казенное учреждение",
        "price": 16_900_000,
        "method": "Запрос котировок",
        "law": "44-ФЗ",
        "publish_date": "03.06.2026",
        "deadline": "09.06.2026",
        "appeared_at": "04.06.2026 15:45",
        "contract_status": "Исполнение",
        "okpd2": "61.10.30.190",
        "status": "Исполнение",
        "url": "https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=0173100003226000042",
    },
    {
        "number": "32515229187",
        "title": "Поставка облачной платформы для обработки данных и виртуальных машин",
        "customer": "АО Региональный оператор",
        "price": 42_000_000,
        "method": "Конкурс в электронной форме",
        "law": "223-ФЗ",
        "publish_date": "01.06.2026",
        "deadline": "19.06.2026",
        "appeared_at": "01.06.2026 11:10",
        "contract_status": "Завершен",
        "okpd2": "63.11.19.000",
        "status": "Завершен",
        "url": "https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=32515229187",
    },
]


def sample_tenders() -> list[dict[str, Any]]:
    tenders = [dict(item) for item in SAMPLE_TENDERS]
    templates = [
        ("Оказание услуг мобильной связи для территориальных подразделений", "МОБ", "Электронный аукцион", "61.20.11.000", 6_900_000),
        ("Предоставление каналов связи и фиксированного доступа в интернет", "ФИКС", "Электронный аукцион", "61.10.30.190", 12_400_000),
        ("Услуги M2M мониторинга транспорта и SIM-карт для телеметрии", "М2М", "Запрос котировок", "61.20.30.000", 4_700_000),
        ("Услуги SMS-информирования и коротких сообщений", "SBVAS", "Электронный аукцион", "61.20.42.000", 3_300_000),
        ("Корпоративная мобильная связь и мобильный интернет", "МОБ", "Электронный аукцион", "61.20.11.000", 9_850_000),
        ("Фиксированные VPN-каналы связи между объектами заказчика", "ФИКС", "Запрос котировок", "61.10.30.190", 5_600_000),
        ("Передача данных IoT-устройств и телеметрии", "М2М", "Электронный аукцион", "61.20.30.000", 7_100_000),
        ("Подключение мобильных номеров и SIM-карт для сотрудников", "МОБ", "Запрос котировок", "61.20.11.000", 2_900_000),
        ("Организация защищенных каналов связи", "ФИКС", "Электронный аукцион", "61.10.30.190", 18_200_000),
        ("Услуги голосовой связи и мобильного интернета", "МОБ", "Электронный аукцион", "61.20.11.000", 14_500_000),
        ("SMS-рассылки для уведомления граждан", "SBVAS", "Запрос котировок", "61.20.42.000", 1_700_000),
        ("Сотовая связь для оперативных служб", "МОБ", "Электронный аукцион", "61.20.11.000", 22_000_000),
        ("Интернет-каналы для филиальной сети", "ФИКС", "Электронный аукцион", "61.10.30.190", 31_000_000),
        ("Сервис мониторинга датчиков с передачей данных", "М2М", "Запрос котировок", "61.20.30.000", 3_950_000),
        ("Мобильная связь и SMS-пакеты", "МОБ", "Электронный аукцион", "61.20.11.000", 8_750_000),
    ]
    for index, (title, domain, method, okpd2, price) in enumerate(templates, start=4):
        day = min(27, 5 + index)
        tenders.append(
            {
                "number": f"037320012342600{index:04d}",
                "title": title,
                "customer": f"Заказчик демо-пула {index}",
                "price": price,
                "method": method,
                "law": "44-ФЗ",
                "publish_date": f"{max(1, day - 7):02d}.06.2026",
                "deadline": f"{day:02d}.06.2026",
                "appeared_at": f"{max(1, 5 - (index % 3)):02d}.06.2026 {9 + (index % 8):02d}:15",
                "contract_status": "Исполнение",
                "okpd2": okpd2,
                "status": "Исполнение",
                "url": f"https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=037320012342600{index:04d}",
                "demo_domain": domain,
            }
        )
    return tenders


@dataclass
class ChecklistItem:
    name: str
    status: str
    result: str
    comment: str


@dataclass
class EvidenceItem:
    category: str
    label: str
    term: str
    snippet: str


def lower_text(value: str) -> str:
    return (value or "").lower().replace("ё", "е")


def parse_money(value: str | int | float | None) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if not value:
        return 0
    clean = re.sub(r"[^\d,\.]", "", value)
    if not clean:
        return 0
    clean = clean.replace(",", ".")
    if clean.count(".") > 1:
        clean = clean.replace(".", "")
    return int(float(clean))


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%Y %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip()[:16], fmt)
        except ValueError:
            pass
    return None


def to_iso_date(value: str | None) -> str:
    """Русская дата '11.06.2026' → ISO '2026-06-11' для SQL-фильтрации (или '')."""
    dt = parse_date(value)
    return dt.date().isoformat() if dt else ""


def days_between_exclusive(start: str | None, end: str | None, business: bool = False) -> int | None:
    start_dt = parse_date(start)
    end_dt = parse_date(end)
    if not start_dt or not end_dt or end_dt <= start_dt:
        return None
    current = start_dt.date() + timedelta(days=1)
    last = end_dt.date() - timedelta(days=1)
    count = 0
    while current <= last:
        if not business or current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def detect_domains(text: str) -> list[str]:
    haystack = lower_text(text)
    domains = []
    for domain, triggers in DOMAIN_TRIGGERS.items():
        if any(lower_text(trigger) in haystack for trigger in triggers):
            domains.append(domain)
    return domains


def detect_products(text: str) -> list[str]:
    haystack = lower_text(text)
    return [
        product
        for product, triggers in PRODUCT_CATALOG.items()
        if any(lower_text(trigger) in haystack for trigger in triggers)
    ]


def document_text_for_tender(tender: dict[str, Any]) -> str:
    """Запасной источник текста, когда у тендера нет скачанных документов."""
    title = tender.get("title") or "Без названия"
    method = tender.get("method") or "Способ закупки не распознан"
    okpd2 = tender.get("okpd2") or "ОКПД2 не указан"
    price = tender.get("price") or 0
    publish_date = tender.get("publish_date") or "Дата размещения не распознана"
    deadline = tender.get("deadline") or "Дата окончания не распознана"
    customer = tender.get("customer") or "Заказчик не распознан"
    status = tender.get("status") or "Статус не распознан"
    return (
        f"Извещение о закупке № {tender.get('number', '')}.\n"
        f"Наименование объекта закупки: {title}.\n"
        f"Заказчик: {customer}.\n"
        f"Способ определения поставщика: {method}.\n"
        f"Начальная максимальная цена контракта: {price} руб.\n"
        f"Код ОКПД2: {okpd2}.\n"
        f"Дата размещения извещения: {publish_date}.\n"
        f"Дата и время окончания подачи заявок: {deadline}.\n"
        f"Статус контракта: {tender.get('contract_status') or status}.\n"
        "Текст карточки извещения ЕИС."
    )


def evidence_snippet(text: str, term: str, radius: int = 150) -> str:
    normalized_text = lower_text(text)
    normalized_term = lower_text(term)
    index = normalized_text.find(normalized_term)
    if index < 0:
        return ""
    start = max(0, index - radius)
    end = min(len(text), index + len(term) + radius)
    snippet = text[start:end].strip()
    return re.sub(r"\s+", " ", snippet)


def collect_evidence(tender: dict[str, Any], document_text: str) -> list[dict[str, str]]:
    evidence: list[EvidenceItem] = []
    seen: set[tuple[str, str, str]] = set()

    def add(category: str, label: str, term: str, source_text: str = document_text) -> None:
        snippet = evidence_snippet(source_text, term)
        key = (category, label, term)
        if snippet and key not in seen:
            evidence.append(EvidenceItem(category, label, term, snippet))
            seen.add(key)

    for domain, triggers in DOMAIN_TRIGGERS.items():
        for trigger in triggers:
            add("Домен", domain, trigger)

    method = str(tender.get("method", ""))
    for term in ["аукцион", "котиров", "конкурс", "способ определения поставщика"]:
        add("Способ закупки", method or "Способ закупки", term)

    for term in ["ОКПД2", str(tender.get("okpd2", "")), "61.", "начальная максимальная цена", "цена контракта"]:
        if term:
            add("Критерии", "ОКПД2 и сумма", term)

    for term in ["дата размещения", "окончания подачи заявок", str(tender.get("publish_date", "")), str(tender.get("deadline", ""))]:
        if term:
            add("Критерии", "Сроки подачи", term)

    return [asdict(item) for item in evidence]


def analyze_tender(tender: dict[str, Any], document_text: str = "") -> dict[str, Any]:
    combined = " ".join(
        str(tender.get(key, ""))
        for key in ("title", "customer", "method", "okpd2", "status", "law")
    )
    combined = f"{combined} {document_text}"
    domains = detect_domains(combined)
    products = detect_products(combined)
    price = parse_money(tender.get("price"))
    method_text = lower_text(str(tender.get("method", "")))
    okpd2 = str(tender.get("okpd2", ""))
    checklist: list[ChecklistItem] = []

    if domains:
        status = "pass" if "МОБ" in domains else "info"
        result = "Найден домен МОБ, анализ запущен" if "МОБ" in domains else "МОБ не найден"
        checklist.append(
            ChecklistItem(
                "Определение домена",
                status,
                result,
                "Домены: " + ", ".join(domains),
            )
        )
    else:
        checklist.append(
            ChecklistItem(
                "Определение домена",
                "warning",
                "Домен не определен",
                "Добавьте ключевые слова или загрузите документацию.",
            )
        )

    method_ok = True
    method_comment = []
    if okpd2 and not okpd2.startswith("61"):
        method_comment.append("ОКПД2 не относится к телеком-услугам 61.*")
    if "котиров" in method_text and price > 10_000_000:
        method_ok = False
        method_comment.append("Запрос котировок выше 10 млн руб. требует проверки")
    if "конкурс" in method_text:
        method_ok = False
        method_comment.append("Конкурс по заданным критериям помечается как неверный способ")
    if "аукцион" in method_text:
        method_comment.append("Аукцион допустим при любой сумме")
    checklist.append(
        ChecklistItem(
            "Определение способа закупки",
            "pass" if method_ok else "risk",
            "Способ закупки выглядит корректно" if method_ok else "Способ закупки требует внимания",
            "; ".join(method_comment) or "Недостаточно данных по способу закупки.",
        )
    )

    required_days = None
    actual_days = None
    business = False
    if "аукцион" in method_text:
        required_days = 15 if price > 300_000_000 else 7
    elif "котиров" in method_text:
        required_days = 4
        business = True
    elif "конкурс" in method_text:
        required_days = 15
    if required_days:
        actual_days = days_between_exclusive(tender.get("publish_date"), tender.get("deadline"), business)
    if required_days and actual_days is not None:
        deadline_ok = actual_days >= required_days
        unit = "рабочих" if business else "календарных"
        checklist.append(
            ChecklistItem(
                "Сроки подачи заявок",
                "pass" if deadline_ok else "risk",
                f"{actual_days} из требуемых {required_days} {unit} дней",
                "Срок считается без даты размещения и даты окончания подачи.",
            )
        )
    else:
        checklist.append(
            ChecklistItem(
                "Сроки подачи заявок",
                "warning",
                "Недостаточно дат для проверки",
                "Нужны дата размещения, дата окончания и способ закупки.",
            )
        )

    risk_count = sum(1 for item in checklist if item.status == "risk")
    warning_count = sum(1 for item in checklist if item.status == "warning")
    score = 42
    if "МОБ" in domains:
        score += 24
    if okpd2.startswith("61"):
        score += 14
    if products:
        score += 8
    score -= risk_count * 18
    score -= warning_count * 6
    if price and price <= 10_000_000:
        score += 4
    score = max(0, min(100, score))

    priority = "Высокий" if score >= 75 else "Средний" if score >= 50 else "Низкий"
    return {
        "domains": domains,
        "products": products,
        "checklist": [asdict(item) for item in checklist],
        "evidence": collect_evidence(tender, combined),
        "risk_count": risk_count,
        "warning_count": warning_count,
        "score": score,
        "priority": priority,
        "queue_priority": tender.get("deadline") or "Без срока",
    }
