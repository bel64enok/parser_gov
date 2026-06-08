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

// статус прогона → класс точки и подпись
const STATUS_MAP = {
  completed: { dot: 'dot-ok', label: 'завершён' },
  running: { dot: 'dot-warn', label: 'идёт', pulse: true },
  error: { dot: 'dot-err', label: 'ошибка' },
  stalled: { dot: 'dot-err', label: 'прервано' },
};

function statusBadge(status) {
  const m = STATUS_MAP[status] || { dot: 'dot-idle', label: status || '—' };
  const pulse = m.pulse ? ' admin-badge--pulse' : '';
  return `<span class="admin-badge${pulse}"><span class="stage-dot ${m.dot}"></span>${escapeHtml(m.label)}</span>`;
}

function renderCurrent(current) {
  const running = current.is_running;
  byId('curDot').className = 'stage-dot ' + (running ? 'dot-warn' : 'dot-ok');
  byId('curState').textContent = running
    ? 'Прогон выполняется'
    : 'Простой — ожидание следующего запуска';
  const last = current.last_run;
  byId('curMeta').textContent = last
    ? `последний: ${fmtTs(last.started_at)} • ${STATUS_MAP[last.status]?.label || last.status} • источник ${last.source === 'zakupki.gov.ru' ? 'ЕИС' : last.source || '—'}`
    : 'запусков пока не было';
  byId('curNote').textContent = current.cron_note || '';
}

function renderRuns(runs) {
  const body = byId('runsBody');
  byId('runsCount').textContent = runs.length ? `${runs.length} последних` : '';
  if (!runs.length) {
    body.innerHTML = '<tr><td colspan="8" class="admin-empty">Запусков пока нет — запустите pipeline.py</td></tr>';
    return;
  }
  body.innerHTML = runs
    .map(
      (r) => `
      <tr>
        <td>${fmtTs(r.started_at)}</td>
        <td>${fmtDuration(r.duration_sec)}</td>
        <td class="admin-table__query" title="${escapeHtml(r.query)}">${escapeHtml(r.query || '—')}</td>
        <td>${r.limit_n ?? '—'}</td>
        <td>${r.source === 'zakupki.gov.ru' ? 'ЕИС' : r.source ? 'demo' : '—'}</td>
        <td>${r.fetched_new ?? 0}<span class="admin-sub"> / ${r.fetched_skipped ?? 0} проп.</span></td>
        <td>${r.analyzed_ok ?? 0}<span class="admin-sub${(r.analyzed_failed ?? 0) ? ' admin-sub--err' : ''}"> / ${r.analyzed_failed ?? 0} ош.</span></td>
        <td>${statusBadge(r.status)}</td>
      </tr>`
    )
    .join('');
}

function renderQueue(queue) {
  byId('queueCount').textContent = `${queue.pending || 0} ожидают анализа${queue.error ? `, ${queue.error} с ошибкой` : ''}`;
  const pending = queue.pending_sample || [];
  byId('queuePending').innerHTML = pending.length
    ? pending
        .map(
          (t) =>
            `<div class="admin-queue__item"><code>${escapeHtml(t.number)}</code><span>${escapeHtml(t.title || '')}</span></div>`
        )
        .join('')
    : '<div class="admin-empty">Очередь пуста</div>';
  const errs = queue.error_sample || [];
  byId('queueError').innerHTML = errs.length
    ? `<div class="admin-queue__title">Ошибки</div>` +
      errs
        .map(
          (t) =>
            `<div class="admin-queue__item"><code>${escapeHtml(t.number)}</code><span>${escapeHtml(t.error || t.title || '')}</span></div>`
        )
        .join('')
    : '';
}

function renderConfig(config) {
  const gateway = config.ai_configured_gateway ? config.ai_gateway || 'настроен' : 'не настроен';
  byId('aiConfig').innerHTML = `
    <dt>Режим</dt><dd>${config.ai_enabled ? 'правила + ИИ' : 'только правила'}</dd>
    <dt>Модель</dt><dd>${escapeHtml(config.ai_model || '—')}</dd>
    <dt>Шлюз</dt><dd>${escapeHtml(gateway)}</dd>
    <dt>Бюджет текста</dt><dd>${(config.text_budget || 0).toLocaleString('ru-RU')} символов</dd>`;
  byId('aiPrompt').textContent = config.system_prompt || '—';
}

async function loadAdmin() {
  try {
    const d = await fetch('/api/admin', { cache: 'no-store' }).then((r) => r.json());
    renderCurrent(d.current || {});
    renderRuns(d.runs || []);
    renderQueue(d.queue || {});
    renderConfig(d.config || {});
  } catch {
    byId('curState').textContent = 'Не удалось загрузить данные мониторинга';
  }
}

loadAdmin();
setInterval(loadAdmin, 20_000);
