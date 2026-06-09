'use strict';

const byId = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function fmtTs(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function fmtDuration(sec) {
  if (sec == null || Number.isNaN(sec)) return '—';
  if (sec < 60) return `${Math.round(sec)} с`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m} м ${s} с`;
}

function fmtBytes(n) {
  if (n == null || Number.isNaN(n)) return '—';
  if (n < 1024) return `${n} Б`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} КБ`;
  return `${(n / 1024 / 1024).toFixed(1)} МБ`;
}

const RUN_STATUS = {
  completed: { dot: 'dot-ok', label: 'завершён' },
  running: { dot: 'dot-warn', label: 'идёт', pulse: true },
  error: { dot: 'dot-err', label: 'ошибка' },
  stalled: { dot: 'dot-err', label: 'прервано' },
};

function runStatusBadge(status) {
  const m = RUN_STATUS[status] || { dot: 'dot-idle', label: status || '—' };
  const pulse = m.pulse ? ' admin-badge--pulse' : '';
  return `<span class="admin-badge${pulse}"><span class="stage-dot ${m.dot}"></span>${escapeHtml(m.label)}</span>`;
}

function kindLabel(run) {
  if (run.kind === 'fetch') return run.stage === 'retry' ? 'повтор' : 'ручной';
  return 'cron';
}

function stageLabel(run) {
  if (run.stage === 'retry') return 'ретрай';
  if (run.stage === 'fetch') return 'стадия 1';
  return 'полный';
}

const DOC_STATUS = {
  ok: { dot: 'dot-ok', label: 'скачан' },
  unpacked: { dot: 'dot-ok', label: 'распакован' },
  archive: { dot: 'dot-idle', label: 'архив' },
  failed: { dot: 'dot-err', label: 'ошибка' },
};

// ── Состояние ──────────────────────────────────────────────────────────
const state = {
  activeRunId: null, // id незавершённого прогона (для polling)
  currentRunId: null, // открытая деталь прогона
  drawerNumber: null, // тендер, открытый в drawer
  drawerTrigger: null,
  pollTimer: null,
  busy: false, // идёт сетевой запрос (старт/ретрай) — защита от двойных кликов
};

const IDLE_REFRESH = 20_000;
const ACTIVE_REFRESH = 2_000;

async function getJson(url) {
  const res = await fetch(url, { cache: 'no-store' });
  return res.json();
}

async function postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
    cache: 'no-store',
  });
  let data = {};
  try {
    data = await res.json();
  } catch {
    data = {};
  }
  return { ok: res.ok, status: res.status, data };
}

// ── Обзор: контрольная панель + список прогонов ──────────────────────────
async function loadOverview() {
  let data;
  try {
    data = await getJson('/api/crawler');
  } catch {
    byId('runsBody').innerHTML =
      '<tr><td colspan="9" class="admin-empty">Не удалось загрузить данные</td></tr>';
    return;
  }
  renderControl(data.active_run, data.cron_note);
  renderSummary(data.summary || {});
  renderRuns(data.runs || []);
  scheduleNextPoll(Boolean(data.active_run));
  // деталь открытого прогона держим в актуальном состоянии во время активного сбора
  if (state.currentRunId && data.active_run) {
    loadRun(state.currentRunId, { silent: true });
  }
}

function renderControl(activeRun, cronNote) {
  state.activeRunId = activeRun ? activeRun.id : null;
  const progress = byId('crawlProgress');
  const startBtn = byId('crawlStart');
  byId('crawlHint').textContent =
    cronNote || 'Скачивает карточку и вложения, распаковывает архивы на диск. Анализ догонит cron.';

  if (!activeRun) {
    progress.hidden = true;
    startBtn.disabled = false;
    startBtn.textContent = 'Запустить сбор';
    return;
  }

  startBtn.disabled = true;
  startBtn.textContent = 'Идёт сбор…';
  progress.hidden = false;

  const total = activeRun.tenders_total || 0;
  const done = activeRun.tenders_done || 0;
  const dl = activeRun.files_downloaded || 0;
  const up = activeRun.files_unpacked || 0;
  const failed = activeRun.files_failed || 0;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : (done > 0 ? 100 : 6);

  const isRetry = activeRun.stage === 'retry';
  byId('crawlProgressLabel').textContent = isRetry
    ? `Повтор упавших · тендер ${done}/${total}`
    : `Идёт сбор · тендер ${done}/${total}`;
  byId('crawlCounters').textContent = `скачано ${dl} · распаковано ${up} · упало ${failed}`;
  byId('crawlBarFill').style.width = `${pct}%`;
}

function renderSummary(s) {
  byId('crawlSummary').textContent =
    `Всего тендеров: ${s.tenders_total || 0} · с файлами: ${s.tenders_with_files || 0}` +
    ` · архивов: ${s.archives || 0} · упавших файлов: ${s.failed_files || 0}`;
}

function renderRuns(runs) {
  const body = byId('runsBody');
  byId('runsCount').textContent = runs.length ? `${runs.length} последних` : '';
  if (!runs.length) {
    body.innerHTML =
      '<tr><td colspan="9" class="admin-empty">Запусков ещё не было. Введите запрос и нажмите «Запустить сбор».</td></tr>';
    return;
  }
  body.innerHTML = runs
    .map((r) => {
      const tenders = r.status === 'running'
        ? `${r.tenders_done || 0}/${r.tenders_total || 0}`
        : `${r.tenders_done || r.fetched_new || 0}`;
      const failed = r.files_failed || 0;
      const failedCell = failed
        ? `<span class="admin-sub admin-sub--err">${failed}</span>`
        : '0';
      return `
      <tr class="run-row" data-run="${r.id}" tabindex="0" role="button" aria-label="Открыть запуск ${r.id}">
        <td>${runStatusBadge(r.status)}</td>
        <td><span class="tag">${kindLabel(r)}</span></td>
        <td>${escapeHtml(stageLabel(r))}</td>
        <td class="admin-table__query" title="${escapeHtml(r.query || '')}">${escapeHtml(r.query || '—')}</td>
        <td>${tenders}</td>
        <td>${r.files_downloaded || 0}</td>
        <td>${failedCell}</td>
        <td>${fmtDuration(r.duration_sec)}</td>
        <td>${fmtTs(r.started_at)}</td>
      </tr>`;
    })
    .join('');
}

// ── Деталь прогона ───────────────────────────────────────────────────────
async function loadRun(runId, opts = {}) {
  state.currentRunId = runId;
  if (!opts.silent) {
    byId('runsView').hidden = true;
    byId('runView').hidden = false;
    byId('runTendersBody').innerHTML = '<tr><td colspan="6" class="admin-empty">Загрузка…</td></tr>';
  }
  let data;
  try {
    data = await getJson(`/api/crawler/run?id=${encodeURIComponent(runId)}`);
  } catch {
    byId('runTendersBody').innerHTML =
      '<tr><td colspan="6" class="admin-empty">Не удалось загрузить запуск</td></tr>';
    return;
  }
  renderRunDetail(data);
}

function renderRunDetail(data) {
  const run = data.run || {};
  const tenders = data.tenders || [];
  byId('runTitle').textContent = `Запуск #${run.id ?? '—'} · ${kindLabel(run)} · ${stageLabel(run)}`;
  byId('runMeta').textContent =
    `${runStatusText(run.status)} · запрос «${run.query || '—'}» · начат ${fmtTs(run.started_at)}` +
    ` · скачано ${run.files_downloaded || 0} · распаковано ${run.files_unpacked || 0}` +
    ` · упало ${run.files_failed || 0}`;

  const totalFailed = tenders.reduce((acc, t) => acc + (t.files_failed || 0), 0);
  const retryBtn = byId('retryRunBtn');
  if (totalFailed > 0 && !state.activeRunId) {
    retryBtn.hidden = false;
    retryBtn.textContent = `Повторить всё упавшее (${totalFailed})`;
    retryBtn.dataset.run = run.id;
  } else {
    retryBtn.hidden = true;
  }

  const body = byId('runTendersBody');
  if (!tenders.length) {
    body.innerHTML =
      '<tr><td colspan="6" class="admin-empty">У этого запуска нет связанных тендеров (старый запуск до обновления).</td></tr>';
    return;
  }
  body.innerHTML = tenders
    .map((t) => {
      const failedCell = t.files_failed
        ? `<span class="admin-sub admin-sub--err">${t.files_failed}</span>`
        : '0';
      return `
      <tr class="tender-row" data-number="${escapeHtml(t.number)}" tabindex="0" role="button" aria-label="Документы тендера ${escapeHtml(t.number)}">
        <td><code>${escapeHtml(t.number)}</code></td>
        <td class="admin-table__query" title="${escapeHtml(t.title || '')}">${escapeHtml(t.title || '—')}</td>
        <td>${t.files_total || 0}</td>
        <td>${t.unpacked || 0}</td>
        <td>${failedCell}</td>
        <td>${escapeHtml(t.pipeline_status || '—')}</td>
      </tr>`;
    })
    .join('');
}

function runStatusText(status) {
  return (RUN_STATUS[status] || {}).label || status || '—';
}

function backToRuns() {
  state.currentRunId = null;
  byId('runView').hidden = true;
  byId('runsView').hidden = false;
  loadOverview();
}

// ── Drawer: дерево файлов тендера ─────────────────────────────────────────
async function openDrawer(number, trigger) {
  state.drawerNumber = number;
  state.drawerTrigger = trigger || document.activeElement;
  byId('detailBody').innerHTML = '<p class="loading">Загрузка документов…</p>';
  showDrawer();
  await reloadDrawer();
}

async function reloadDrawer() {
  const number = state.drawerNumber;
  if (!number) return;
  let data;
  try {
    data = await getJson(`/api/crawler/tender?number=${encodeURIComponent(number)}`);
  } catch {
    byId('detailBody').innerHTML = '<p class="loading">Не удалось загрузить документы</p>';
    return;
  }
  renderTree(data);
}

function docMeta(doc) {
  const meta = [escapeHtml(doc.type || 'file'), fmtBytes(doc.size_bytes)];
  return meta.join(' · ');
}

function docActions(doc) {
  const actions = [];
  if (doc.status !== 'failed' && doc.status !== 'archive') {
    actions.push(`<a class="open-link" href="/api/file?id=${doc.id}">Скачать</a>`);
  }
  if (doc.source_url) {
    actions.push(
      `<a class="document-source-link open-link" href="${escapeHtml(doc.source_url)}" target="_blank" rel="noreferrer">Источник</a>`
    );
  }
  return actions.length ? `<div class="document-actions">${actions.join('')}</div>` : '';
}

function docRow(doc) {
  const s = DOC_STATUS[doc.status] || DOC_STATUS.ok;
  const err = doc.status === 'failed' && doc.error
    ? `<small class="admin-sub admin-sub--err">${escapeHtml(doc.error)}</small>`
    : '';
  const name = displayName(doc.filename);
  return `
    <div class="doc-node">
      <div class="doc-node__line">
        <span class="stage-dot ${s.dot}"></span>
        <span class="doc-node__name">${escapeHtml(name)}</span>
        <span class="doc-node__meta">${docMeta(doc)} · ${escapeHtml(s.label)}</span>
      </div>
      ${err}
      ${docActions(doc)}
    </div>`;
}

function displayName(filename) {
  const f = String(filename || '');
  const slash = f.lastIndexOf('/');
  return slash >= 0 ? f.slice(slash + 1) : f;
}

function renderTree(data) {
  const tender = data.tender || {};
  const docs = data.documents || [];
  const tenderUrl = tender.url
    ? `<a class="open-link" href="${escapeHtml(tender.url)}" target="_blank" rel="noreferrer">Открыть в ЕИС</a>`
    : '';
  const retryDisabled = state.activeRunId ? ' disabled title="Идёт другой запуск"' : '';
  const failedCount = docs.filter((d) => d.status === 'failed').length;
  const retryBtn = failedCount
    ? `<button type="button" id="retryTenderBtn" data-number="${escapeHtml(tender.number)}"${retryDisabled}>Повторить загрузку тендера (${failedCount})</button>`
    : '';

  if (!docs.length) {
    byId('detailBody').innerHTML = `
      <div class="drawer-meta"><code>${escapeHtml(tender.number)}</code> ${tenderUrl}</div>
      <p class="empty small-empty">Документы не найдены — у тендера нет распознанных вложений.</p>`;
    return;
  }

  // members группируем под их архивом (parent_doc_id)
  const childrenOf = new Map();
  const tops = [];
  for (const d of docs) {
    if (d.parent_doc_id) {
      if (!childrenOf.has(d.parent_doc_id)) childrenOf.set(d.parent_doc_id, []);
      childrenOf.get(d.parent_doc_id).push(d);
    } else {
      tops.push(d);
    }
  }

  const nodes = tops
    .map((d) => {
      const kids = childrenOf.get(d.id) || [];
      if (d.status === 'archive' || kids.length) {
        const inner = kids.map(docRow).join('') || '<p class="muted-text">Архив пуст</p>';
        return `
          <details class="document-card" open>
            <summary>
              <span><span class="stage-dot dot-idle"></span> ${escapeHtml(displayName(d.filename))}</span>
              <small>${docMeta(d)} · ${kids.length} внутри</small>
            </summary>
            <div class="doc-children">${inner}</div>
          </details>`;
      }
      return `<div class="document-card document-card--flat">${docRow(d)}</div>`;
    })
    .join('');

  byId('detailBody').innerHTML = `
    <div class="drawer-meta"><code>${escapeHtml(tender.number)}</code> ${tenderUrl}</div>
    ${retryBtn}
    <div class="doc-tree">${nodes}</div>`;
}

// ── Действия ──────────────────────────────────────────────────────────────
async function startCrawl(event) {
  event.preventDefault();
  if (state.busy || state.activeRunId) return;
  const query = byId('crawlQuery').value.trim() || 'мобильная связь';
  const limit = Math.max(1, Math.min(parseInt(byId('crawlLimit').value, 10) || 10, 50));
  state.busy = true;
  byId('crawlStart').disabled = true;
  const { ok, status, data } = await postJson('/api/crawler/start', { query, limit });
  state.busy = false;
  if (!ok && status === 409) {
    byId('crawlHint').textContent = 'Уже выполняется запуск — дождитесь завершения.';
  }
  loadOverview();
}

async function retryTender(number, btn) {
  if (state.busy || state.activeRunId) return;
  state.busy = true;
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Повтор…';
  }
  const { ok, status } = await postJson('/api/crawler/retry-tender', { number });
  state.busy = false;
  if (!ok && status === 409) {
    byId('crawlHint').textContent = 'Идёт другой запуск — повтор недоступен.';
  }
  await reloadDrawer();
  if (state.currentRunId) loadRun(state.currentRunId, { silent: true });
  loadOverview();
}

async function retryRun(runId, btn) {
  if (state.busy || state.activeRunId) return;
  state.busy = true;
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Запуск повтора…';
  }
  await postJson('/api/crawler/retry-run', { run_id: runId });
  state.busy = false;
  loadOverview();
}

// ── Drawer open/close (зеркалит паттерн app.js) ───────────────────────────
function showDrawer() {
  const drawer = byId('detailDrawer');
  const backdrop = byId('drawerBackdrop');
  drawer.classList.add('open');
  drawer.setAttribute('aria-hidden', 'false');
  backdrop.classList.add('open');
  document.body.classList.add('drawer-open');
  byId('drawerClose').focus();
}

function closeDrawer() {
  const drawer = byId('detailDrawer');
  const backdrop = byId('drawerBackdrop');
  drawer.classList.remove('open');
  drawer.setAttribute('aria-hidden', 'true');
  backdrop.classList.remove('open');
  document.body.classList.remove('drawer-open');
  state.drawerNumber = null;
  if (state.drawerTrigger && typeof state.drawerTrigger.focus === 'function') {
    state.drawerTrigger.focus();
  }
}

// ── Polling ─────────────────────────────────────────────────────────────
function scheduleNextPoll(active) {
  if (state.pollTimer) clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(loadOverview, active ? ACTIVE_REFRESH : IDLE_REFRESH);
}

// ── События ───────────────────────────────────────────────────────────────
byId('crawlForm').addEventListener('submit', startCrawl);
byId('backToRuns').addEventListener('click', backToRuns);
byId('drawerClose').addEventListener('click', closeDrawer);
byId('drawerBackdrop').addEventListener('click', closeDrawer);

document.addEventListener('click', (event) => {
  const runRow = event.target.closest('.run-row');
  if (runRow && byId('runView').contains(event.target) === false) {
    loadRun(Number(runRow.dataset.run));
    return;
  }
  const tenderRow = event.target.closest('.tender-row');
  if (tenderRow) {
    openDrawer(tenderRow.dataset.number, tenderRow);
    return;
  }
  const retryTenderBtn = event.target.closest('#retryTenderBtn');
  if (retryTenderBtn) {
    retryTender(retryTenderBtn.dataset.number, retryTenderBtn);
    return;
  }
  const retryRunBtn = event.target.closest('#retryRunBtn');
  if (retryRunBtn) {
    retryRun(Number(retryRunBtn.dataset.run), retryRunBtn);
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && byId('detailDrawer').classList.contains('open')) {
    closeDrawer();
    return;
  }
  // Enter/Space на строках-кнопках таблиц
  if (event.key === 'Enter' || event.key === ' ') {
    const runRow = event.target.closest && event.target.closest('.run-row');
    if (runRow) {
      event.preventDefault();
      loadRun(Number(runRow.dataset.run));
      return;
    }
    const tenderRow = event.target.closest && event.target.closest('.tender-row');
    if (tenderRow) {
      event.preventDefault();
      openDrawer(tenderRow.dataset.number, tenderRow);
    }
  }
});

loadOverview();
