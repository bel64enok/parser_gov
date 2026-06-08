# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
# opens at http://127.0.0.1:8000
PORT=9000 python app.py  # custom port
```

No build step — frontend is plain HTML/CSS/JS served directly from `static/`.

## Architecture

Everything lives in two places:

- **`app.py`** — single-file Python backend (~780 lines). Uses only stdlib + optional `python-docx` / `openpyxl`. Runs as a `ThreadingHTTPServer`.
- **`static/`** — vanilla JS SPA (`app.js`, `styles.css`, `index.html`). No bundler or framework.

### Backend layers in `app.py`

| Layer | Key symbols |
|---|---|
| Domain/product detection | `DOMAIN_TRIGGERS`, `PRODUCT_CATALOG`, `detect_domains()`, `detect_products()` |
| Tender scoring & analysis | `analyze_tender()`, `ChecklistItem`, `EvidenceItem` |
| EIS scraping + fallback | `search_tenders()`, `fetch_zakupki_html()`, `parse_zakupki_results()` |
| Document extraction | `extract_text_from_file()` — dispatches to `extract_docx/xlsx/pdf_text_layer()` |
| HTTP layer | `TenderHandler(BaseHTTPRequestHandler)` — `do_GET`, `do_POST`, `serve_static` |

### API endpoints

- `GET /api/tenders?query=...&limit=10` — fetch, scrape, analyze and rank tenders
- `POST /api/upload` (multipart `file`) — extract text from uploaded doc and run analysis
- `GET /api/requirements` — return domain dictionary and checklist criteria
- `GET /api/health` — liveness check

### Fallback behavior

When `zakupki.gov.ru` is unreachable or returns unrecognizable HTML, `search_tenders()` falls back to `SAMPLE_TENDERS` / `sample_tenders()` demo data. The frontend shows a "Demo" badge in this case.

### Scoring logic

`analyze_tender()` builds a 0–100 score: base 42, +24 for МОБ domain, +14 for ОКПД2 61.*, +8 for matched products, −18 per risk item, −6 per warning. Tenders are sorted by `(-score, deadline)`.
