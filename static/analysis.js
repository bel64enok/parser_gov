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

const AGENT_STATUS = {
  done: { dot: 'dot-ok', label: 'готов' },
  partial: { dot: 'dot-warn', label: 'готов · лимит' },
  running: { dot: 'dot-warn', label: 'анализируется', pulse: true },
  error: { dot: 'dot-err', label: 'ошибка' },
};

const TOOL_LABEL = {
  list_documents: 'список документов',
  read_document: 'чтение документа',
  search_documents: 'поиск по документам',
  lookup_dictionary: 'словарь доменов',
  submit_card: 'карточка готова',
};

function runStatusBadge(status) {
  const m = RUN_STATUS[status] || { dot: 'dot-idle', label: status || '—' };
  const pulse = m.pulse ? ' admin-badge--pulse' : '';
  return `<span class="admin-badge${pulse}"><span class="stage-dot ${m.dot}"></span>${escapeHtml(m.label)}</span>`;
}

function agentStatusBadge(status, limitReached) {
  const m = AGENT_STATUS[status] || { dot: 'dot-idle', label: status || '—' };
  const pulse = m.pulse ? ' admin-badge--pulse' : '';
  const label = status === 'partial' || (status === 'done' && limitReached) ? 'готов · лимит' : m.label;
  return `<span class="admin-badge${pulse}"><span class="stage-dot ${m.dot}"></span>${escapeHtml(label)}</span>`;
}

function kindLabel(run) {
  if (run.kind === 'analyze') return run.stage === 'reanalyze' ? 'переанализ' : 'ручной';
  if (run.kind === 'pipeline') return 'cron';
  if (run.kind === 'fetch') return 'сбор';
  return run.kind || '—';
}

function confidenceTag(value) {
  const v = (value || '').toLowerCase();
  const cls = v === 'высокая' ? 'pass' : v === 'низкая' ? 'risk' : 'warning';
  return value ? `<span class="tag ${cls}">${escapeHtml(value)}</span>` : '—';
}

// ── Состояние ──────────────────────────────────────────────────────────
const state = {
  activeRunId: null,
  currentRunId: null,
  drawerNumber: null,
  drawerTrigger: null,
  activeTab: 'trace',
  tenderData: null,
  pollTimer: null,
  busy: false,
  wasActive: false,
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
    data = await getJson('/api/analysis');
  } catch {
    byId('runsBody').innerHTML =
      '<tr><td colspan="6" class="admin-empty">Не удалось загрузить данные</td></tr>';
    return;
  }
  renderGateway(data.ai || {});
  renderQueue(data.queue || {}, data.summary || {});
  renderControl(data.active_run, data.current, data.cron_note, data.ai || {}, data.queue || {});
  renderRuns(data.runs || []);
  scheduleNextPoll(Boolean(data.active_run));

  // деталь открытого прогона держим свежей во время активного анализа
  if (state.currentRunId && data.active_run) {
    loadRun(state.currentRunId, { silent: true });
  }
  // прогон завершился, пока drawer открыт → перечитать разбор
  if (state.wasActive && !data.active_run && state.drawerNumber) {
    reloadDrawer();
  }
  state.wasActive = Boolean(data.active_run);
}

function renderGateway(ai) {
  const badge = byId('gatewayBadge');
  const label = byId('gatewayLabel');
  const off = byId('gatewayOff');
  const dot = badge.querySelector('.stage-dot');
  if (ai.enabled) {
    dot.className = 'stage-dot dot-ok';
    label.textContent = `Шлюз: ${ai.gateway || 'подключён'} · ${ai.model || ''}`.trim();
    off.hidden = true;
  } else {
    dot.className = 'stage-dot dot-err';
    label.textContent = 'Шлюз не настроен';
    off.hidden = false;
  }
}

function renderQueue(queue, summary) {
  const awaiting = queue.awaiting || 0;
  byId('queueLine').innerHTML =
    `<strong>${awaiting}</strong> ожидают ИИ-анализа · ` +
    `<strong>${queue.with_card || 0}</strong> проанализировано · ` +
    `<strong class="${queue.error ? 'analysis-q-err' : ''}">${queue.error || 0}</strong> ошибок`;
  byId('analysisSummary').textContent =
    `Прогонов агента: ${summary.agent_runs || 0} · готово: ${summary.done || 0}` +
    ` · с лимитом: ${summary.partial || 0} · ошибок: ${summary.error || 0}`;
}

function renderControl(activeRun, current, cronNote, ai, queue) {
  state.activeRunId = activeRun ? activeRun.id : null;
  const progress = byId('analyzeProgress');
  const startBtn = byId('analyzeStart');
  const awaiting = (queue && queue.awaiting) || 0;
  byId('analyzeHint').textContent =
    cronNote || 'Агент читает документы из очереди и извлекает требования с цитатами.';

  if (!activeRun) {
    progress.hidden = true;
    if (!ai.enabled) {
      startBtn.disabled = true;
      startBtn.textContent = 'Шлюз не настроен';
      startBtn.title = 'Заполните RMR_GATEWAY_URL и RMR_API_KEY';
    } else if (awaiting === 0) {
      startBtn.disabled = true;
      startBtn.textContent = 'Нет тендеров для анализа';
      startBtn.title = 'Все скачанные тендеры уже разобраны агентом';
    } else {
      startBtn.disabled = false;
      startBtn.textContent = `Запустить анализ (${awaiting})`;
      startBtn.title = '';
    }
    return;
  }

  startBtn.disabled = true;
  startBtn.textContent = 'Идёт анализ…';
  progress.hidden = false;

  const total = activeRun.tenders_total || 0;
  const done = activeRun.tenders_done || 0;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : done > 0 ? 100 : 6;
  const isAnalyze = activeRun.kind === 'analyze' || activeRun.kind === 'pipeline';

  // Живые метрики агента по текущему тендеру (шаг/вызовы/токены/таймер)
  const metrics = [];
  if (current) {
    if (current.step_count != null) metrics.push(`шаг ${current.step_count}`);
    if (current.tool_calls != null) metrics.push(`${current.tool_calls} выз.`);
    if (current.tokens) metrics.push(`${fmtTokens(current.tokens)} ток.`);
    if (current.elapsed_sec != null) metrics.push(`⏱ ${fmtElapsed(current.elapsed_sec)}`);
  }
  const metricsSuffix = isAnalyze && metrics.length ? ` · ${metrics.join(' · ')}` : '';
  byId('progressLabel').textContent = isAnalyze
    ? `Идёт анализ · тендер ${done}/${total}${metricsSuffix}`
    : `Идёт сбор · тендер ${done}/${total}`;

  if (current && (current.step || current.tender_title)) {
    const step = current.step ? `${current.step} · ` : '';
    byId('progressStep').textContent = `${step}${current.tender_title || current.tender_number || ''}`;
  } else {
    byId('progressStep').textContent = '';
  }
  byId('progressFill').style.width = `${pct}%`;
}

function fmtElapsed(sec) {
  const s = Math.max(0, Math.round(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function fmtTokens(n) {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n);
}

function renderRuns(runs) {
  const body = byId('runsBody');
  byId('runsCount').textContent = runs.length ? `${runs.length} последних` : '';
  if (!runs.length) {
    body.innerHTML =
      '<tr><td colspan="6" class="admin-empty">Прогонов анализа ещё не было. Нажмите «Запустить анализ».</td></tr>';
    return;
  }
  body.innerHTML = runs
    .map((r) => {
      const tenders =
        r.status === 'running'
          ? `${r.tenders_done || 0}/${r.tenders_total || 0}`
          : `${r.analyzed_ok != null ? r.analyzed_ok : r.tenders_done || 0}`;
      return `
      <tr class="run-row" data-run="${r.id}" tabindex="0" role="button" aria-label="Открыть прогон ${r.id}">
        <td>${runStatusBadge(r.status)}</td>
        <td><span class="tag">${kindLabel(r)}</span></td>
        <td class="admin-table__query" title="${escapeHtml(r.query || '')}">${escapeHtml(r.query || '—')}</td>
        <td>${tenders}</td>
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
    data = await getJson(`/api/analysis/run?id=${encodeURIComponent(runId)}`);
  } catch {
    byId('runTendersBody').innerHTML =
      '<tr><td colspan="6" class="admin-empty">Не удалось загрузить прогон</td></tr>';
    return;
  }
  renderRunDetail(data);
}

function renderRunDetail(data) {
  const run = data.run || {};
  const tenders = data.tenders || [];
  byId('runTitle').textContent = `Прогон #${run.id ?? '—'} · ${kindLabel(run)}`;
  byId('runMeta').textContent =
    `${runStatusText(run.status)} · запрос «${run.query || '—'}» · начат ${fmtTs(run.started_at)}` +
    ` · тендеров ${tenders.length}`;

  const body = byId('runTendersBody');
  if (!tenders.length) {
    body.innerHTML =
      '<tr><td colspan="6" class="admin-empty">Нет агентных прогонов (анализ шёл на правилах либо прогон ещё стартует).</td></tr>';
    return;
  }
  body.innerHTML = tenders
    .map((t) => {
      const domain = t.domain ? `<span class="tag ${t.domain === 'МОБ' ? 'mob' : ''}">${escapeHtml(t.domain)}</span>` : '—';
      return `
      <tr class="tender-row" data-number="${escapeHtml(t.number)}" tabindex="0" role="button" aria-label="Разбор тендера ${escapeHtml(t.number)}">
        <td><code>${escapeHtml(t.number)}</code></td>
        <td class="admin-table__query" title="${escapeHtml(t.title || '')}">${escapeHtml(t.title || '—')}</td>
        <td>${agentStatusBadge(t.agent_status, t.limit_reached)}</td>
        <td>${domain}</td>
        <td>${confidenceTag(t.confidence)}</td>
        <td>${t.step_count || 0}</td>
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

// ── Drawer: трейс / карточка / документы ──────────────────────────────────
async function openDrawer(number, trigger) {
  state.drawerNumber = number;
  state.drawerTrigger = trigger || document.activeElement;
  state.activeTab = 'trace';
  syncTabs();
  byId('detailBody').innerHTML = '<p class="loading">Загрузка разбора…</p>';
  showDrawer();
  await reloadDrawer();
}

async function reloadDrawer() {
  const number = state.drawerNumber;
  if (!number) return;
  let data;
  try {
    data = await getJson(`/api/analysis/tender?number=${encodeURIComponent(number)}`);
  } catch {
    byId('detailBody').innerHTML = '<p class="loading">Не удалось загрузить разбор</p>';
    return;
  }
  state.tenderData = data;
  byId('drawerSubtitle').textContent = data.tender ? `№ ${data.tender.number}` : '';
  renderActiveTab();
}

function selectTab(tab) {
  state.activeTab = tab;
  syncTabs();
  renderActiveTab();
}

function syncTabs() {
  document.querySelectorAll('.drawer-tab').forEach((el) => {
    el.setAttribute('aria-selected', el.dataset.tab === state.activeTab ? 'true' : 'false');
  });
}

function renderActiveTab() {
  const data = state.tenderData;
  if (!data) return;
  if (state.activeTab === 'card') renderCard(data);
  else if (state.activeTab === 'docs') renderDocs(data);
  else renderTrace(data);
}

// ── Таб: трейс ─────────────────────────────────────────────────────────
function renderTrace(data) {
  const run = data.run;
  const steps = data.steps || [];
  const busy = Boolean(state.activeRunId);
  const reanalyze = `<button type="button" id="reanalyzeBtn" data-number="${escapeHtml(data.tender.number)}"${busy ? ' disabled title="Идёт другой прогон"' : ''}>Переанализировать</button>`;

  if (!run) {
    byId('detailBody').innerHTML = `
      ${drawerMeta(data)}
      <div class="drawer-actions">${reanalyze}</div>
      <p class="empty small-empty">Агент ещё не отрабатывал по этому тендеру (анализ на правилах или нет шлюза).</p>`;
    return;
  }

  const metrics = [
    `${run.step_count} шагов`,
    `${run.tool_calls} вызовов`,
    `${run.tokens || 0} токенов`,
    fmtDuration(run.duration_sec),
    escapeHtml(run.model || ''),
  ]
    .filter(Boolean)
    .map((m) => `<span class="trace-metric">${m}</span>`)
    .join('');

  const limitBadge = run.limit_reached
    ? '<span class="tag warning">достигнут лимит шагов</span>'
    : '';
  const errBadge = run.status === 'error' && run.error
    ? `<p class="trace-error">${escapeHtml(run.error)}</p>`
    : '';

  const nodes = steps.length
    ? steps.map(traceStep).join('')
    : '<p class="empty small-empty">Шагов нет.</p>';

  byId('detailBody').innerHTML = `
    ${drawerMeta(data)}
    <div class="drawer-actions">${reanalyze} ${limitBadge}</div>
    <div class="trace-metrics">${metrics}</div>
    ${errBadge}
    <ol class="trace" id="traceList">${nodes}</ol>`;
}

function traceStep(step) {
  const isFinal = step.kind === 'final';
  const isNote = step.kind === 'note';
  const cls = isFinal ? 'trace-node trace-node--final' : 'trace-node';
  const toolName = step.tool ? (TOOL_LABEL[step.tool] || step.tool) : 'рассуждение';
  const thought = step.thought
    ? `<p class="trace-thought">${escapeHtml(step.thought)}</p>`
    : '';

  let call = '';
  if (step.tool && !isFinal) {
    const args = formatArgs(step.args);
    call = `<code class="trace-call">${escapeHtml(step.tool)}(${escapeHtml(args)})</code>`;
  } else if (isFinal) {
    call = `<code class="trace-call trace-call--final">submit_card()</code>`;
  }

  let obs = '';
  if (step.observation && !isFinal) {
    const text = String(step.observation);
    const short = text.length > 240;
    obs = short
      ? `<details class="trace-obs"><summary>наблюдение (${text.length} симв.)</summary><pre>${escapeHtml(text)}</pre></details>`
      : `<pre class="trace-obs trace-obs--flat">${escapeHtml(text)}</pre>`;
  } else if (isFinal) {
    obs = `<p class="trace-obs trace-obs--flat">${escapeHtml(step.observation || 'Карточка сформирована')}</p>`;
  }

  return `
    <li class="${cls}" id="trace-step-${step.idx}">
      <div class="trace-node__rail"><span class="trace-node__num">${step.idx}</span></div>
      <div class="trace-node__body">
        <div class="trace-node__head">
          <span class="trace-kind trace-kind--${isFinal ? 'final' : isNote ? 'note' : 'tool'}">${escapeHtml(toolName)}</span>
        </div>
        ${thought}
        ${call}
        ${obs}
      </div>
    </li>`;
}

function formatArgs(args) {
  if (!args || typeof args !== 'object') return '';
  return Object.entries(args)
    .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(', ')
    .slice(0, 120);
}

// ── Таб: структурированная карточка ──────────────────────────────────────
function renderCard(data) {
  const card = data.card;
  if (!card) {
    byId('detailBody').innerHTML = `
      ${drawerMeta(data)}
      <p class="empty small-empty">Карточка не сформирована — агент не отрабатывал или упал.</p>`;
    return;
  }

  const domainTag = card.domain
    ? `<span class="tag ${card.domain === 'МОБ' ? 'mob' : ''}">${escapeHtml(card.domain)}</span>`
    : '';
  const limit = card.limit_reached ? '<span class="tag warning">частичная · лимит шагов</span>' : '';
  const header = `
    <div class="card-header">
      <div class="card-header__tags">${domainTag} ${confidenceTag(card.confidence)} ${limit}</div>
      ${card.summary ? `<p class="card-summary">${escapeHtml(card.summary)}</p>` : ''}
    </div>`;

  const sections = (card.sections || []).map(cardSection).join('');

  const reqs = card.participant_requirements || [];
  const reqSection = reqs.length
    ? `
      <section class="card-section">
        <h3 class="card-section__title">Требования к участнику</h3>
        <ul class="req-list">
          ${reqs.map(reqRow).join('')}
        </ul>
      </section>`
    : '';

  const risks = (card.risks || []).length
    ? `
      <section class="card-section">
        <h3 class="card-section__title">Риски</h3>
        <ul class="risk-list">${card.risks.map((r) => `<li>${escapeHtml(r)}</li>`).join('')}</ul>
      </section>`
    : '';

  byId('detailBody').innerHTML = `${drawerMeta(data)}${header}${sections}${reqSection}${risks}`;
}

function cardSection(section) {
  const rows = (section.facts || []).map(factRow).join('');
  return `
    <section class="card-section">
      <h3 class="card-section__title">${escapeHtml(section.title)}</h3>
      <dl class="fact-list">${rows}</dl>`;
}

function factRow(fact) {
  const notFound = !fact.found;
  const value = notFound
    ? `<span class="fact-empty">не найдено в документации</span>`
    : escapeHtml(fact.value);
  const cite = fact.found && fact.source && (fact.source.filename || fact.source.quote)
    ? citationChip(fact.source, fact.step_ref)
    : '';
  return `
    <div class="fact-row">
      <dt class="fact-label">${escapeHtml(fact.label)}</dt>
      <dd class="fact-value">${value}${cite}</dd>
    </div>`;
}

function reqRow(req) {
  const dot = req.present ? 'dot-ok' : 'dot-idle';
  const mark = req.present ? 'есть в документации' : 'не подтверждено';
  const cite = req.source && (req.source.filename || req.source.quote)
    ? citationChip(req.source, req.step_ref)
    : '';
  return `
    <li class="req-row">
      <span class="stage-dot ${dot}"></span>
      <span class="req-text">${escapeHtml(req.label)}<small class="req-mark">${mark}</small></span>
      ${cite}
    </li>`;
}

let citeSeq = 0;
function citationChip(source, stepRef) {
  const id = `cite-${++citeSeq}`;
  const file = source.filename || 'источник';
  const quote = source.quote ? `<blockquote class="cite-quote">«${escapeHtml(source.quote)}»</blockquote>` : '';
  const jump = stepRef
    ? `<button type="button" class="cite-jump" data-step="${stepRef}">→ шаг ${stepRef}</button>`
    : '';
  return `
    <span class="cite">
      <button type="button" class="cite-chip" aria-expanded="false" data-cite="${id}">📄 ${escapeHtml(file)}</button>
      <span class="cite-body" id="${id}" hidden>${quote}${jump}</span>
    </span>`;
}

// ── Таб: документы ───────────────────────────────────────────────────────
const DOC_STATUS = {
  ok: { dot: 'dot-ok', label: 'скачан' },
  unpacked: { dot: 'dot-ok', label: 'распакован' },
  archive: { dot: 'dot-idle', label: 'архив' },
  failed: { dot: 'dot-err', label: 'ошибка' },
};

function renderDocs(data) {
  const docs = data.documents || [];
  if (!docs.length) {
    byId('detailBody').innerHTML = `
      ${drawerMeta(data)}
      <p class="empty small-empty">У тендера нет распознанных документов — агент работал по карточке ЕИС.</p>`;
    return;
  }
  const rows = docs
    .map((d) => {
      const s = DOC_STATUS[d.status] || DOC_STATUS.ok;
      const dl = d.status !== 'failed' && d.status !== 'archive'
        ? `<a class="open-link" href="/api/file?id=${d.id}">Скачать</a>`
        : '';
      return `
      <div class="doc-node">
        <div class="doc-node__line">
          <span class="stage-dot ${s.dot}"></span>
          <span class="doc-node__name">${escapeHtml(displayName(d.filename))}</span>
          <span class="doc-node__meta">${escapeHtml(d.type || 'file')} · ${fmtBytes(d.size_bytes)} · ${escapeHtml(s.label)}</span>
        </div>
        ${dl ? `<div class="document-actions">${dl}</div>` : ''}
      </div>`;
    })
    .join('');
  byId('detailBody').innerHTML = `${drawerMeta(data)}<div class="doc-tree">${rows}</div>`;
}

function displayName(filename) {
  const f = String(filename || '');
  const slash = f.lastIndexOf('/');
  return slash >= 0 ? f.slice(slash + 1) : f;
}

function drawerMeta(data) {
  const t = data.tender || {};
  const url = t.url
    ? `<a class="open-link" href="${escapeHtml(t.url)}" target="_blank" rel="noreferrer">Открыть в ЕИС</a>`
    : '';
  const hist = (data.history || []).length > 1
    ? `<span class="muted-text">попыток: ${data.history.length}</span>`
    : '';
  return `<div class="drawer-meta"><code>${escapeHtml(t.number)}</code> ${hist} ${url}</div>
          <p class="drawer-tender-title">${escapeHtml(t.title || '')}</p>`;
}

// ── Действия ──────────────────────────────────────────────────────────────
async function startAnalysis() {
  if (state.busy || state.activeRunId) return;
  state.busy = true;
  byId('analyzeStart').disabled = true;
  const { ok, status } = await postJson('/api/analysis/start', {});
  state.busy = false;
  if (!ok && status === 409) {
    byId('analyzeHint').textContent = 'Уже выполняется прогон — дождитесь завершения.';
  }
  loadOverview();
}

async function reanalyze(number, btn) {
  if (state.busy || state.activeRunId) return;
  state.busy = true;
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Запуск…';
  }
  const { ok, status } = await postJson('/api/analysis/reanalyze', { number });
  state.busy = false;
  if (!ok && status === 409) {
    byId('analyzeHint').textContent = 'Идёт другой прогон — переанализ недоступен.';
  } else if (ok) {
    byId('detailBody').insertAdjacentHTML(
      'afterbegin',
      '<p class="trace-note">Переанализ запущен — следите за прогрессом наверху, разбор обновится автоматически.</p>'
    );
  }
  loadOverview();
}

// ── Drawer open/close ─────────────────────────────────────────────────────
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
  state.tenderData = null;
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
byId('analyzeStart').addEventListener('click', startAnalysis);
byId('backToRuns').addEventListener('click', backToRuns);
byId('drawerClose').addEventListener('click', closeDrawer);
byId('drawerBackdrop').addEventListener('click', closeDrawer);

document.querySelectorAll('.drawer-tab').forEach((tab) => {
  tab.addEventListener('click', () => selectTab(tab.dataset.tab));
});

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
  const reanalyzeBtn = event.target.closest('#reanalyzeBtn');
  if (reanalyzeBtn) {
    reanalyze(reanalyzeBtn.dataset.number, reanalyzeBtn);
    return;
  }
  const citeChip = event.target.closest('.cite-chip');
  if (citeChip) {
    const body = byId(citeChip.dataset.cite);
    if (body) {
      const open = !body.hidden;
      body.hidden = open;
      citeChip.setAttribute('aria-expanded', String(!open));
      citeChip.classList.toggle('cite-chip--open', !open);
    }
    return;
  }
  const jump = event.target.closest('.cite-jump');
  if (jump) {
    selectTab('trace');
    requestAnimationFrame(() => {
      const node = byId(`trace-step-${jump.dataset.step}`);
      if (node) {
        node.scrollIntoView({ behavior: 'smooth', block: 'center' });
        node.classList.add('trace-node--flash');
        setTimeout(() => node.classList.remove('trace-node--flash'), 1600);
      }
    });
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && byId('detailDrawer').classList.contains('open')) {
    closeDrawer();
    return;
  }
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
  // навигация по табам стрелками
  if ((event.key === 'ArrowRight' || event.key === 'ArrowLeft') && event.target.classList.contains('drawer-tab')) {
    const tabs = ['trace', 'card', 'docs'];
    const i = tabs.indexOf(state.activeTab);
    const next = event.key === 'ArrowRight' ? (i + 1) % 3 : (i + 2) % 3;
    selectTab(tabs[next]);
    byId(`tab-${tabs[next]}`).focus();
  }
});

loadOverview();
