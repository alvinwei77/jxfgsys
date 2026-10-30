let MILLS = [], SHEETS = [], SCRAP_TYPES = [], CURRENT_SHEET = null;

async function initQuotes() {
  [MILLS, SCRAP_TYPES] = await Promise.all([apiGet('/api/steel_mills'), apiGet('/api/scrap_types')]);
  document.getElementById('q-mill').innerHTML = optionsHtml(MILLS, 'id', 'name');
  document.getElementById('q-mill').addEventListener('change', loadSheetsForMill);
  document.getElementById('q-sheet').addEventListener('change', loadSheet);
  if (MILLS.length) await loadSheetsForMill();
}

async function loadSheetsForMill() {
  const millId = document.getElementById('q-mill').value;
  if (!millId) { SHEETS = []; document.getElementById('q-sheet').innerHTML = ''; renderEmpty(); return; }
  SHEETS = await apiGet('/api/quote_sheets?mill_id=' + millId);
  document.getElementById('q-sheet').innerHTML = SHEETS.map(s => `<option value="${s.id}">${s.period_label || s.effective_at}</option>`).join('');
  if (SHEETS.length) await loadSheet(); else renderEmpty();
}

function renderEmpty() {
  document.getElementById('quote-tbody').innerHTML = '<tr><td colspan="9" class="empty-hint">该钢厂暂无报价单，请点击"新增本期报价单"</td></tr>';
  document.getElementById('sheet-meta').textContent = '';
  document.getElementById('footer-notes').value = '';
}

async function loadSheet() {
  const sheetId = document.getElementById('q-sheet').value;
  if (!sheetId) { renderEmpty(); return; }
  const data = await apiGet('/api/quote_sheets/' + sheetId);
  CURRENT_SHEET = data;
  document.getElementById('sheet-meta').textContent = `${data.sheet.mill_name} · 生效时间：${data.sheet.effective_at}`;
  document.getElementById('footer-notes').value = data.sheet.footer_notes || '';

  const items = data.items;
  const byId = {}; items.forEach(it => byId[it.scrap_type_id] = it);
  const rows = SCRAP_TYPES.map(st => {
    const it = byId[st.id] || {};
    const change = it.price_change;
    const changeCls = change > 0 ? 'change-up' : (change < 0 ? 'change-down' : 'change-flat');
    const changeTxt = change == null ? '-' : (change > 0 ? '▲' : (change < 0 ? '▼' : '—')) + ' ' + fmtNum(Math.abs(change));
    return `<tr data-scrap="${st.id}">
      <td>${st.category}</td><td>${st.name}</td><td>${st.example_desc||''}</td>
      <td>${st.size_requirement||''}</td><td>${st.thickness_requirement||''}</td>
      <td class="num">${fmtNum(it.prev_price)}</td>
      <td class="num ${changeCls}">${changeTxt}</td>
      <td class="num"><input class="q-price" type="number" step="1" style="width:90px;text-align:right;" value="${it.price ?? ''}"></td>
      <td><input class="q-suspend" type="checkbox" ${it.is_suspended?'checked':''}></td>
    </tr>`;
  }).join('');
  document.getElementById('quote-tbody').innerHTML = rows || '<tr><td colspan="9" class="empty-hint">请先在"基础档案-货型字典"中录入货型</td></tr>';
}

async function newSheet() {
  const millId = document.getElementById('q-mill').value;
  if (!millId) { alert('请先选择钢厂'); return; }
  const label = prompt('期号说明，如：2026年9月12日12时起执行');
  if (label === null) return;
  const effective = prompt('生效时间戳 (YYYY-MM-DD HH:MM)', new Date().toISOString().slice(0,16).replace('T',' '));
  if (effective === null) return;
  const { id } = await apiPost('/api/quote_sheets', { mill_id: millId, period_label: label, effective_at: effective });
  await loadSheetsForMill();
  document.getElementById('q-sheet').value = id;
  await loadSheet();
}

async function saveItems() {
  if (!CURRENT_SHEET) return;
  const items = Array.from(document.querySelectorAll('#quote-tbody tr')).map(tr => ({
    scrap_type_id: tr.dataset.scrap,
    price: tr.querySelector('.q-price').value || null,
    is_suspended: tr.querySelector('.q-suspend').checked ? 1 : 0,
  }));
  await apiPut(`/api/quote_sheets/${CURRENT_SHEET.sheet.id}/items`, { items });
  alert('已保存本期价格');
  await loadSheet();
}

async function saveFooter() {
  if (!CURRENT_SHEET) return;
  await apiPut(`/api/quote_sheets/${CURRENT_SHEET.sheet.id}`, { footer_notes: document.getElementById('footer-notes').value });
}

initQuotes();
