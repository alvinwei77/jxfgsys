async function loadInvoiceInGap(mode) {
  const params = new URLSearchParams();
  if (mode === 'all') {
    params.set('period', 'all');
    params.set('date_from', '2000-01-01');
    params.set('date_to', '2099-12-31');
  } else {
    params.set('period', 'current');
  }
  const data = await apiGet('/api/analytics/invoice_in_gap?' + params.toString());
  const rangeEl = document.getElementById('iig-range');
  if (data.date_from && data.date_to) {
    rangeEl.textContent = `筛选：${data.date_from} ~ ${data.date_to}`;
  } else {
    rangeEl.textContent = '全部历史';
  }
  document.getElementById('iig-sum').textContent =
    `${data.count} 车 · 结算额合计 ¥${fmtMoney(data.total_settlement || 0)}`;
  const tbody = document.querySelector('#iig-table tbody');
  if (!data.trips.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-hint">暂无缺口</td></tr>';
    return;
  }
  tbody.innerHTML = data.trips.map(t => `<tr>
    <td class="nowrap">${t.settlement_date || '—'}</td>
    <td>${esc(t.plate_no || '—')}</td>
    <td>${esc(t.source_name)}</td>
    <td class="num">${fmtNum(t.settlement_tonnage_total, 2)}</td>
    <td class="num">¥${fmtMoney(t.settlement_amount_total)}</td>
    <td>${esc(t.status)}</td>
    <td><a class="btn-link" href="/trips?id=${t.id}">打开车次</a></td>
  </tr>`).join('');
}

function esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
}

document.addEventListener('DOMContentLoaded', () => {
  const q = new URLSearchParams(location.search);
  loadInvoiceInGap(q.get('period') === 'all' ? 'all' : 'current');
});
