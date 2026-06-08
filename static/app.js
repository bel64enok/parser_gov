const state = {
  tenders: [],
  selectedNumber: null,
  sourceUrl: '#',
  viewed: new Set(JSON.parse(localStorage.getItem('viewedTenderCards') || '[]')),
  selectedTags: new Set(),
  manualTags: JSON.parse(localStorage.getItem('manualTenderTagsV2') || '{}'),
  currentPage: 1,
  pageSize: 6,
};

const fmtRub = new Intl.NumberFormat('ru-RU', {
  style: 'currency',
  currency: 'RUB',
  maximumFractionDigits: 0,
});

const byId = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function updateMetrics(payload) {
  const tenders = state.tenders;
  byId('queueCount').textContent = tenders.length;
  byId('hotCount').textContent = tenders.filter((item) => item.analysis.score >= 75).length;
  byId('viewedCount').textContent = state.viewed.size;
  byId('sourceLabel').textContent = payload.source === 'zakupki.gov.ru' ? 'ЕИС' : 'Demo';
  const parsedAt = payload.parsed_at ? `Парсинг: ${payload.parsed_at}` : 'Парсинг выполнен';
  const liveCount = Number.isFinite(payload.live_count) ? `, ЕИС: ${payload.live_count}` : '';
  byId('sourceHint').textContent = payload.error ? `${parsedAt}${liveCount}. ${payload.error}` : `${parsedAt}${liveCount}`;
  byId('sourceUrl').href = payload.source_url || '#';
}

function priorityClass(score) {
  if (score >= 75) return 'high';
  if (score >= 50) return 'mid';
  return 'low';
}

function tagClass(tag) {
  if (tag === 'МОБ') return 'mob';
  return '';
}

function priorityTagClass(priority) {
  if (priority === 'Высокий') return 'pass';
  if (priority === 'Средний') return 'warning';
  return 'risk';
}

function automaticTags(tender) {
  return [
    ...(tender.analysis?.domains || []),
    ...(tender.analysis?.products || []),
    tender.contract_status || tender.status,
    tender.analysis?.priority ? `Приоритет: ${tender.analysis.priority}` : '',
  ].filter(Boolean);
}

function tenderTags(tender) {
  return [...new Set([...automaticTags(tender), ...(state.manualTags[tender.number] || [])])];
}

function allAvailableTags() {
  return [...new Set(state.tenders.flatMap((tender) => tenderTags(tender)))].sort((a, b) => a.localeCompare(b, 'ru'));
}

function filteredTendersByTags() {
  if (!state.selectedTags.size) return state.tenders;
  return state.tenders.filter((tender) => {
    const tags = tenderTags(tender);
    return [...state.selectedTags].every((tag) => tags.includes(tag));
  });
}

function persistManualTags() {
  localStorage.setItem('manualTenderTagsV2', JSON.stringify(state.manualTags));
}

function addManualTag(tenderNumber, tag) {
  const clean = tag.trim();
  if (!tenderNumber || !clean) return;
  const tags = new Set(state.manualTags[tenderNumber] || []);
  tags.add(clean);
  state.manualTags[tenderNumber] = [...tags];
  persistManualTags();
}

function renderTagFilters() {
  const container = byId('tagFilters');
  if (!container) return;
  const tags = allAvailableTags();
  if (!tags.length) {
    container.innerHTML = '<span class="muted-text">Теги появятся после загрузки списка тендеров.</span>';
    return;
  }
  container.innerHTML = tags
    .map((tag) => {
      const active = state.selectedTags.has(tag) ? ' active' : '';
      return `<button type="button" class="tag-filter${active}" data-filter-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>`;
    })
    .join('');
}

function parseRuDateTime(value) {
  if (!value) return null;
  const match = String(value).match(/^(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{2}):(\d{2}))?/);
  if (!match) return null;
  const [, day, month, year, hour = '0', minute = '0'] = match;
  return new Date(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute));
}

function isNewTender(tender) {
  const hours = 72;
  const appeared = parseRuDateTime(tender.appeared_at || tender.publish_date);
  if (!appeared) return false;
  return Date.now() - appeared.getTime() <= hours * 60 * 60 * 1000;
}

function markViewed(number) {
  if (!number) return;
  state.viewed.add(number);
  localStorage.setItem('viewedTenderCards', JSON.stringify([...state.viewed]));
  byId('viewedCount').textContent = state.viewed.size;
}

function termsFromDocument(document) {
  return [...new Set((document.highlights || []).map((item) => item.term).filter(Boolean))].sort((a, b) => b.length - a.length);
}

function highlightText(text, terms) {
  let result = escapeHtml(text || 'Текст документа не распознан');
  terms.forEach((term) => {
    const escaped = escapeHtml(term);
    const pattern = new RegExp(`(${escaped.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
    result = result.replace(pattern, '<mark>$1</mark>');
  });
  return result;
}

function renderEvidence(highlights) {
  if (!highlights?.length) {
    return '<div class="empty small-empty">Доказательные фрагменты не найдены</div>';
  }
  return `
    <div class="evidence-list">
      ${highlights
        .map(
          (item) => `
          <article class="evidence-item">
            <span>${escapeHtml(item.category)} • ${escapeHtml(item.label)}</span>
            <strong>${escapeHtml(item.term)}</strong>
            <p>${highlightText(item.snippet, [item.term])}</p>
          </article>
        `
        )
        .join('')}
    </div>
  `;
}

function renderDocuments(documents) {
  if (!documents?.length) {
    return '<div class="empty small-empty">К карточке пока не прикреплены документы</div>';
  }
  return `
    <div class="document-toolbar">
      <span>${documents.length} документа в карточке</span>
      <span>Оригиналы открываются на сайте госзакупок</span>
    </div>
    <div class="document-list">
      ${documents
        .map((document) => {
          const terms = termsFromDocument(document);
          return `
            <details class="document-card" open>
              <summary>
                <span>
                  ${escapeHtml(document.name)}
                  <small>${escapeHtml(document.type)} • ${terms.length} выделений</small>
                </span>
              </summary>
              <div class="document-actions">
                ${document.source_url ? `<a class="open-link document-source-link" href="${escapeHtml(document.source_url)}" target="_blank" rel="noreferrer">Открыть в ЕИС</a>` : ''}
                <button class="download-document" type="button" data-doc-id="${escapeHtml(document.id)}">Выгрузить текст</button>
              </div>
              ${renderEvidence(document.highlights || [])}
              <pre class="document-text">${highlightText(document.text, terms)}</pre>
            </details>
          `;
        })
        .join('')}
    </div>
  `;
}

function renderPagination(totalItems) {
  const container = byId('pagination');
  if (!container) return;
  const totalPages = Math.max(1, Math.ceil(totalItems / state.pageSize));
  if (totalPages <= 1) {
    container.innerHTML = '';
    return;
  }
  const pageButtons = Array.from({ length: totalPages }, (_, index) => {
    const page = index + 1;
    return `<button type="button" class="${page === state.currentPage ? 'active' : ''}" data-page-number="${page}">${page}</button>`;
  }).join('');
  container.innerHTML = `
    <button type="button" data-page="${Math.max(1, state.currentPage - 1)}" ${state.currentPage === 1 ? 'disabled' : ''}>Назад</button>
    ${pageButtons}
    <button type="button" data-page="${Math.min(totalPages, state.currentPage + 1)}" ${state.currentPage === totalPages ? 'disabled' : ''}>Вперед</button>
    <span>Страница ${state.currentPage} из ${totalPages}</span>
  `;
}

function renderTenders() {
  const items = filteredTendersByTags();
  const container = byId('tenderList');
  if (!items.length) {
    container.innerHTML = '<div class="loading">По выбранным тегам карточки не найдены. Сбросьте теги или выберите другую комбинацию.</div>';
    byId('resultNote').textContent = `0 из ${state.tenders.length} карточек`;
    renderPagination(0);
    return;
  }
  const totalPages = Math.max(1, Math.ceil(items.length / state.pageSize));
  state.currentPage = Math.min(Math.max(1, state.currentPage), totalPages);
  const start = (state.currentPage - 1) * state.pageSize;
  const pageItems = items.slice(start, start + state.pageSize);
  const suffix = state.selectedTags.size ? `, фильтр: ${[...state.selectedTags].join(', ')}` : '';
  byId('resultNote').textContent = `${items.length} из ${state.tenders.length} карточек, показаны ${start + 1}-${start + pageItems.length}${suffix}`;
  container.innerHTML = pageItems
    .map((tender) => {
      const analysis = tender.analysis;
      const tags = tenderTags(tender);
      const active = tender.number === state.selectedNumber ? ' active' : '';
      const viewed = state.viewed.has(tender.number) ? ' viewed' : '';
      const fresh = isNewTender(tender);
      const price = Number(tender.price || 0) > 0 ? fmtRub.format(tender.price) : 'Цена не распознана';
      const deadline = tender.deadline || 'без срока';
      return `
        <article class="tender-card${active}${viewed}${fresh ? ' fresh' : ''}" data-number="${escapeHtml(tender.number)}" role="button" tabindex="0" aria-label="Открыть анализ тендера ${escapeHtml(tender.title)}">
          <div>
            <div class="score ${priorityClass(analysis.score)}">${analysis.score}</div>
          </div>
          <div>
            <h3 class="tender-title">${escapeHtml(tender.title)}</h3>
            <div class="meta">
              <span>№ ${escapeHtml(tender.number)}</span>
              <span>${escapeHtml(tender.law)}</span>
              <span>${escapeHtml(tender.method || 'Способ не распознан')}</span>
              <span>Статус: ${escapeHtml(tender.contract_status || tender.status || 'не распознан')}</span>
            </div>
            <div class="tag-row">
              ${fresh ? `<span class="tag new">Новый</span>` : ''}
              ${state.viewed.has(tender.number) ? `<span class="tag viewed-tag">Просмотрено</span>` : ''}
              <span class="tag ${priorityTagClass(analysis.priority)}">Приоритет: ${escapeHtml(analysis.priority)}</span>
              <span class="tag">Окончание: ${escapeHtml(deadline)}</span>
              <span class="tag">Документы: ${(tender.documents || []).length}</span>
              ${tags.map((tag) => `<span class="tag ${tagClass(tag)}">${escapeHtml(tag)}</span>`).join('')}
            </div>
          </div>
          <div class="amount">
            <strong>${price}</strong>
            <span>${escapeHtml(tender.customer || 'Заказчик не распознан')}</span>
            ${tender.url ? `<a class="open-link" href="${escapeHtml(tender.url)}" target="_blank" rel="noreferrer">Карточка ЕИС</a>` : ''}
          </div>
        </article>
      `;
    })
    .join('');
  renderPagination(items.length);
}

function renderDetail(tender) {
  if (!tender) {
    byId('detailBody').innerHTML = '<div class="empty">Нет выбранной закупки</div>';
    return;
  }
  const analysis = tender.analysis;
  const price = Number(tender.price || 0) > 0 ? fmtRub.format(tender.price) : 'не распознана';
  byId('detailBody').innerHTML = `
    <div class="tag-row">
      <span class="tag ${priorityTagClass(analysis.priority)}">${escapeHtml(analysis.priority)}</span>
      <span class="tag">Score ${analysis.score}/100</span>
      <span class="tag">Приоритет очереди: ${escapeHtml(analysis.queue_priority)}</span>
      <span class="tag">Статус: ${escapeHtml(tender.contract_status || tender.status || 'не распознан')}</span>
      ${state.viewed.has(tender.number) ? '<span class="tag viewed-tag">Просмотрено</span>' : ''}
    </div>
    <h3 class="tender-title">${escapeHtml(tender.title)}</h3>
    <div class="meta">
      <span>№ ${escapeHtml(tender.number)}</span>
      <span>${escapeHtml(tender.law)}</span>
      <span>НМЦК: ${escapeHtml(price)}</span>
      <span>ОКПД2: ${escapeHtml(tender.okpd2 || 'не распознан')}</span>
    </div>
    <div class="tag-row">
      ${tenderTags(tender).map((tag) => `<span class="tag ${tagClass(tag)}">${escapeHtml(tag)}</span>`).join('')}
    </div>
    <form class="manual-tag-form" id="manualTagForm">
      <input id="manualTagInput" name="tag" placeholder="Добавить тег к карточке" autocomplete="off" />
      <button type="submit">Проставить тег</button>
    </form>
    <div class="checklist">
      ${analysis.checklist
        .map(
          (item) => `
          <article class="checkitem ${escapeHtml(item.status)}">
            <strong>${escapeHtml(item.name)}</strong>
            <p>${escapeHtml(item.result)}</p>
            <small>${escapeHtml(item.comment)}</small>
          </article>
        `
        )
        .join('')}
    </div>
    <section class="document-section">
      <h3>Документы и доказательства</h3>
      <p>Подсвечены фрагменты, которые подтверждают домен, способ закупки и критерии анализа.</p>
      <button class="download-all-documents" type="button">Выгрузить все документы карточки</button>
      ${renderDocuments(tender.documents || [])}
    </section>
    ${tender.url ? `<p style="margin-top:16px"><a href="${escapeHtml(tender.url)}" target="_blank" rel="noreferrer">Открыть карточку на zakupki.gov.ru</a></p>` : ''}
  `;
}

let drawerTrigger = null;

function isDrawerOpen() {
  return byId('detailDrawer').classList.contains('open');
}

function openDrawer(tender) {
  if (!tender) return;
  renderDetail(tender);
  const drawer = byId('detailDrawer');
  const backdrop = byId('drawerBackdrop');
  drawerTrigger = document.querySelector(`.tender-card[data-number="${CSS.escape(tender.number)}"]`);
  drawer.classList.add('open');
  drawer.setAttribute('aria-hidden', 'false');
  backdrop.classList.add('open');
  document.body.classList.add('drawer-open');
  byId('drawerClose').focus();
}

function closeDrawer() {
  const drawer = byId('detailDrawer');
  if (!drawer.classList.contains('open')) return;
  drawer.classList.remove('open');
  drawer.setAttribute('aria-hidden', 'true');
  byId('drawerBackdrop').classList.remove('open');
  document.body.classList.remove('drawer-open');
  if (drawerTrigger && document.contains(drawerTrigger)) {
    drawerTrigger.focus();
  }
  drawerTrigger = null;
}

function selectTender(number) {
  if (!number) return;
  state.selectedNumber = number;
  markViewed(number);
  const tender = state.tenders.find((item) => item.number === number);
  renderTenders();
  openDrawer(tender);
}

async function loadTenders() {
  const syncBtn = byId('syncBtn');
  const syncLabel = syncBtn?.querySelector('.sync-label');

  if (syncBtn) {
    syncBtn.disabled = true;
    syncBtn.classList.remove('error');
    syncBtn.classList.add('loading');
    syncBtn.setAttribute('aria-busy', 'true');
  }
  if (syncLabel) syncLabel.textContent = 'Загрузка...';

  byId('tenderList').innerHTML = '<div class="loading">Загружается список тендеров и проставляются теги...</div>';
  byId('resultNote').textContent = 'Загрузка';

  try {
    const refreshToken = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const response = await fetch(`/api/tenders?limit=50&refresh=${encodeURIComponent(refreshToken)}`, { cache: 'no-store' });
    const payload = await response.json();
    state.tenders = payload.items || [];
    state.sourceUrl = payload.source_url;
    state.selectedTags = new Set([...state.selectedTags].filter((tag) => allAvailableTags().includes(tag)));
    state.currentPage = 1;
    state.selectedNumber = null;
    updateMetrics(payload);
    renderTagFilters();
    renderTenders();

    if (syncBtn && syncLabel) {
      const isReal = payload.source === 'zakupki.gov.ru';
      const parsedDate = payload.parsed_at ? new Date(payload.parsed_at) : new Date();
      const hhmm = parsedDate.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
      syncLabel.textContent = `${isReal ? 'ЕИС' : 'Демо'} · ${hhmm}`;
      syncBtn.classList.toggle('demo', !isReal);
    }
  } catch {
    if (syncBtn) syncBtn.classList.add('error');
    if (syncLabel) {
      syncLabel.textContent = 'Ошибка';
      setTimeout(() => {
        syncBtn?.classList.remove('error');
        syncLabel.textContent = 'Обновить';
      }, 3000);
    }
    byId('tenderList').innerHTML = '<div class="loading">Не удалось загрузить тендеры. Нажмите «Обновить» для повторной попытки.</div>';
  } finally {
    if (syncBtn) {
      syncBtn.disabled = false;
      syncBtn.classList.remove('loading');
      syncBtn.removeAttribute('aria-busy');
    }
  }
}

document.addEventListener('submit', (event) => {
  if (event.target.id === 'manualTagForm') {
    event.preventDefault();
    const tender = state.tenders.find((item) => item.number === state.selectedNumber);
    const input = byId('manualTagInput');
    addManualTag(tender?.number, input.value);
    input.value = '';
    renderTagFilters();
    renderTenders();
    renderDetail(tender);
  }
});

document.addEventListener('click', (event) => {
  const tagButton = event.target.closest('[data-filter-tag]');
  if (tagButton) {
    const tag = tagButton.dataset.filterTag;
    if (state.selectedTags.has(tag)) {
      state.selectedTags.delete(tag);
    } else {
      state.selectedTags.add(tag);
    }
    state.currentPage = 1;
    renderTagFilters();
    renderTenders();
    return;
  }
  if (event.target.closest('#resetTags')) {
    state.selectedTags.clear();
    state.currentPage = 1;
    renderTagFilters();
    renderTenders();
    return;
  }
  if (event.target.closest('#drawerClose') || event.target.closest('#drawerBackdrop')) {
    closeDrawer();
    return;
  }
  const card = event.target.closest('.tender-card');
  if (card && !event.target.closest('a')) {
    selectTender(card.dataset.number);
  }
  const downloadOne = event.target.closest('.download-document');
  if (downloadOne) {
    const tender = state.tenders.find((item) => item.number === state.selectedNumber);
    const document = tender?.documents?.find((item) => item.id === downloadOne.dataset.docId);
    if (document) downloadDocument(document);
  }
  if (event.target.closest('.download-all-documents')) {
    const tender = state.tenders.find((item) => item.number === state.selectedNumber);
    if (tender) downloadAllDocuments(tender);
  }
  const pageButton = event.target.closest('[data-page], [data-page-number]');
  if (pageButton && pageButton.closest('#pagination')) {
    state.currentPage = Number(pageButton.dataset.page || pageButton.dataset.pageNumber);
    renderTenders();
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && isDrawerOpen()) {
    closeDrawer();
    return;
  }
  if (event.key === 'Enter' || event.key === ' ') {
    const card = event.target.closest?.('.tender-card');
    if (card) {
      event.preventDefault();
      selectTender(card.dataset.number);
    }
  }
});

function downloadText(filename, text) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function safeFileName(name) {
  return String(name || 'document').replace(/[\\/:*?"<>|]+/g, '_').slice(0, 90);
}

function downloadDocument(document) {
  downloadText(`${safeFileName(document.name)}.txt`, document.text || '');
}

function downloadAllDocuments(tender) {
  const content = (tender.documents || [])
    .map((document) => `# ${document.name}\nТип: ${document.type}\n\n${document.text || ''}`)
    .join('\n\n---\n\n');
  downloadText(`tender_${safeFileName(tender.number)}_documents.txt`, content);
}

loadTenders();
