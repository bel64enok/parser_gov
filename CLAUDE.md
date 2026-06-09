# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                       # optional: AI stage — fill RMR_API_KEY
python pipeline.py "мобильная связь" 10   # fill the store (fetch + analyze)
python app.py                              # serve UI at http://127.0.0.1:8000
PORT=9000 python app.py                    # custom port
```

No build step — frontend is plain HTML/CSS/JS served directly from `static/`.

`config.py` loads `.env` (stdlib, no deps) on import of `app.py`/`pipeline.py`, so the
RMR gateway env vars work for both the web server and cron. Real environment variables
take precedence over `.env`. Without them the pipeline runs rules-only (no AI). `.env`
is gitignored — never commit the key.

## Architecture

A 3-stage pipeline around a local store (SQLite + files on disk). Scraping no longer
happens per HTTP request — `app.py` only reads what the cron pipeline has stored.

```
pipeline.py (cron) → fetcher.fetch_new() → analyzer.analyze_new() → data/tenders.db
                                                                          ↓ reads
                                                                       app.py → browser
```

Per-tender status flow in the DB: `fetched` → `analyzed` (or `error`). Stages are idempotent.

### Modules

| Module | Role | Key symbols |
|---|---|---|
| `db.py` | SQLite store (tenders / documents / analysis) | `connect()`, `upsert_tender()`, `add_document()`, `save_analysis()`, `query_tenders(filters)` |
| `scrape.py` | EIS listing scrape + **attachment download** | `parse_zakupki_results()`, `fetch_zakupki_html()`, `fetch_card_html()`, `fetch_attachments()` |
| `extract.py` | text extraction | `extract_text_from_file()` → `extract_docx/xlsx/pdf_text_layer()` (+ html/zip) |
| `rules.py` | deterministic analysis | `DOMAIN_TRIGGERS`, `PRODUCT_CATALOG`, `detect_domains()`, `analyze_tender()`, `sample_tenders()`, `ChecklistItem`, `EvidenceItem`, `to_iso_date()` |
| `ai.py` | legacy one-shot RMR enrich (kept for reference) | `ai_enrich(tender, full_text)`, `is_configured()` |
| `agent.py` | **ReAct-агент стадии 2** — петля над документами, structured card | `run_agent(tender, documents, on_step)`, `normalize_card()`, `_TOOLS`, `is_configured()` |
| `fetcher.py` | **stage 1** — download | `fetch_new(query, limit)` |
| `analyzer.py` | **stage 2** — extract + rules + ReAct-агент | `analyze_new(run_id, progress)`, `reanalyze_tender(number)` |
| `pipeline.py` | cron entrypoint | `main()` |
| `app.py` | **stage 3** — HTTP server, reads from DB | `TenderHandler`, `requirements_payload()`, `analysis_payload()` |
| `static/` | vanilla JS SPA | `app.js`, `crawler.js`, `analysis.js`, `styles.css` |

### API endpoints (`app.py`)

- `GET /api/tenders` — read stored tenders. Filters: `date_from`, `date_to` (YYYY-MM-DD, by publish date), `query` (title LIKE), `domain`, `limit`. SQL filtering in `db.query_tenders()`.
- `POST /api/upload` (multipart `file`) — one-off analysis of an uploaded doc; independent of the pipeline.
- `GET /api/requirements` — domain dictionary and checklist criteria.
- `GET /api/health` — liveness check.
- `GET /api/analysis` — обзор вкладки «ИИ-анализ»: статус шлюза, очередь, активный прогон + живой шаг, прогоны.
- `GET /api/analysis/run?id=` — тендеры прогона с агентным статусом/доменом/уверенностью/шагами.
- `GET /api/analysis/tender?number=` — ReAct-трейс + структурированная карточка + документы + история попыток.
- `POST /api/analysis/start` — фоновый батч-анализ очереди (`fetched`); 409, если уже идёт прогон.
- `POST /api/analysis/reanalyze` (`{number}`) — фоновый переанализ одного тендера.

### AI layer — ReAct-агент (стадия 2)

`agent.run_agent()` — это ReAct-петля поверх правил (правила не заменяет). Вместо обрезки всего
текста в один промпт агент навигирует по комплекту инструментами (`list_documents`,
`read_document`, `search_documents`, `lookup_dictionary`, `submit_card`) и собирает
**структурированную карточку требований** с цитатами-источниками. Петля написана руками на
`openai` SDK; режим выбирается автоматически — нативный function-calling или фолбэк на
JSON-action протокол (`response_format=json_object`) при первом сбое. Предохранители: лимит
12–15 шагов (→ частичная карточка `done`), таймаут 120 c, последовательно (общий single-run
guard с «Сбором»). Без шлюза или при сбое — деградация на правила (`model="rules-only"`).

Результат: `analysis.agent_card_json` (последняя карточка, видна и во вкладке «Тендеры») +
`agent_runs` (история всех попыток: трейс `steps_json`, метрики, статус `done|partial|error`).
Управление и разбор — на технической вкладке **«ИИ-анализ»** (`/analysis.html`), повторяющей
вкладку «Сбор»: control panel + master прогонов + detail тендеров + drawer (табы Трейс/Карточка/
Документы). Запускается из cron-пайплайна и вручную одним кодом (`analyzer.analyze_new`).

`ai.ai_enrich()` — прежний one-shot, оставлен для справки; в пайплайне больше не используется.

### Scoring logic (rules)

`analyze_tender()` builds a 0–100 score: base 42, +24 for МОБ domain, +14 for ОКПД2 61.*,
+8 for matched products, −18 per risk item, −6 per warning. `query_tenders()` returns tenders
sorted by `(-score, deadline)`.

### Fallback behavior

When `zakupki.gov.ru` is unreachable, `fetcher.fetch_new()` seeds `sample_tenders()` demo data
(source `fallback`). Attachment download (`fetch_attachments`) is best-effort and isolated — a
tender with no recognizable documents still gets stored and analyzed from its card text.
