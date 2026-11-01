/** 规则 hub · 本期改价向导（票源分期 + 户头垫资费） */
let WIZ_SHEET = null;

async function loadWizardSheet() {
  const asOf = getRulesAsOf() || (await fetchSettlementPeriodMeta()).as_of_today;
  WIZ_SHEET = await apiGet('/api/rules/wizard/sheet?as_of=' + encodeURIComponent(asOf));
  document.getElementById('wiz-period-label').textContent = WIZ_SHEET.period_label;
  document.getElementById('wiz-period-range').textContent =
    `${WIZ_SHEET.period_start} ~ ${WIZ_SHEET.period_end}`;
  const tbody = document.querySelector('#wiz-table tbody');
  tbody.innerHTML = WIZ_SHEET.items.map((it, idx) => {
    const p = it.proposed;
    const chk = it.apply_default ? 'checked' : '';
    return `<tr data-idx="${idx}">
      <td><input type="checkbox" class="wiz-apply" ${chk} data-idx="${idx}"></td>
      <td>${escW(it.source_name)}${it.existing_term_id ? ' <span class="tag">已有</span>' : ''}</td>
      <td>${escW(it.source_type || '')}</td>
      <td class="num"><input type="number" step="0.1" class="wiz-inp" data-k="rate_point" data-idx="${idx}" value="${p.rate_point ?? ''}" style="width:72px;"></td>
      <td class="num"><input type="number" step="1" class="wiz-inp" data-k="quota_amount" data-idx="${idx}" value="${p.quota_amount ?? ''}" style="width:100px;"></td>
      <td class="num"><input type="number" step="1" class="wiz-inp" data-k="warn_threshold" data-idx="${idx}" value="${p.warn_threshold ?? ''}" style="width:90px;"></td>
      <td><input type="checkbox" class="wiz-inp" data-k="accepting" data-idx="${idx}" ${p.accepting ? 'checked' : ''}></td>
      <td><input type="text" class="wiz-inp" data-k="notes" data-idx="${idx}" value="${escW(p.notes || '')}" style="width:120px;"></td>
    </tr>`;
  }).join('');
  const rtbody = document.querySelector('#wiz-rate-table tbody');
  const rates = WIZ_SHEET.rate_items || [];
  rtbody.innerHTML = rates.map((it, idx) => {
    const chk = it.apply_default ? 'checked' : '';
    return `<tr data-rate-idx="${idx}">
      <td><input type="checkbox" class="wiz-rate-apply" ${chk} data-rate-idx="${idx}"></td>
      <td>${escW(it.label)}${it.existing_link_id ? ' <span class="tag">本期已有</span>' : ''}</td>
      <td class="num">${fmtNum(it.current_rate, 2)}</td>
      <td class="num"><input type="number" step="0.01" class="wiz-rate-val" data-rate-idx="${idx}" value="${it.proposed_rate ?? it.current_rate}" style="width:72px;"></td>
    </tr>`;
  }).join('') || '<tr><td colspan="4" class="empty-hint">无垫资档位</td></tr>';
  document.getElementById('wiz-check-all').checked = false;
  document.getElementById('wiz-rate-check-all').checked = false;
  document.getElementById('wiz-status').textContent = '';
}

function escW(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
}

function collectWizardItem(idx) {
  const it = WIZ_SHEET.items[idx];
  const p = { ...it.proposed };
  document.querySelectorAll(`.wiz-inp[data-idx="${idx}"]`).forEach(inp => {
    const k = inp.dataset.k;
    if (k === 'accepting') p[k] = inp.checked ? 1 : 0;
    else if (inp.type === 'number') p[k] = inp.value === '' ? null : Number(inp.value);
    else p[k] = inp.value;
  });
  const apply = document.querySelector(`.wiz-apply[data-idx="${idx}"]`)?.checked;
  return {
    source_id: it.source_id,
    existing_term_id: it.existing_term_id,
    apply,
    proposed: p,
  };
}

function collectRateItem(idx) {
  const it = WIZ_SHEET.rate_items[idx];
  const inp = document.querySelector(`.wiz-rate-val[data-rate-idx="${idx}"]`);
  const apply = document.querySelector(`.wiz-rate-apply[data-rate-idx="${idx}"]`)?.checked;
  return {
    ...it,
    apply,
    proposed_rate: inp ? Number(inp.value) : it.proposed_rate,
    effective_from: WIZ_SHEET.period_start,
  };
}

async function applyWizard() {
  if (!WIZ_SHEET) return;
  const btn = document.getElementById('wiz-apply-btn');
  btn.disabled = true;
  const items = WIZ_SHEET.items.map((_, idx) => collectWizardItem(idx));
  const rate_items = (WIZ_SHEET.rate_items || []).map((_, idx) => collectRateItem(idx));
  const n = items.filter(i => i.apply).length;
  const nr = rate_items.filter(i => i.apply).length;
  if (!n && !nr) {
    alert('请至少勾选一个票源分期或垫资档位');
    btn.disabled = false;
    return;
  }
  if (!confirm(`确认写入？票源分期 ${n} 条 · 垫资费 ${nr} 条（自 ${WIZ_SHEET.period_start} 起）`)) {
    btn.disabled = false;
    return;
  }
  try {
    const res = await apiPost('/api/rules/wizard/apply', {
      period_start: WIZ_SHEET.period_start,
      period_end: WIZ_SHEET.period_end,
      items,
      rate_items,
    });
    document.getElementById('wiz-status').textContent =
      `分期 新${res.created}/改${res.updated} · 垫资 ${res.rate_upserted || 0} · 跳过 ${res.skipped}`;
    if (window.toast) toast(document.getElementById('wiz-status').textContent);
    await loadWizardSheet();
    if (typeof loadRulesHub === 'function') loadRulesHub();
  } catch (e) {
    alert(e.message);
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  if (!document.getElementById('wiz-table')) return;
  document.getElementById('wiz-check-all')?.addEventListener('change', (e) => {
    document.querySelectorAll('.wiz-apply').forEach(c => { c.checked = e.target.checked; });
  });
  document.getElementById('wiz-rate-check-all')?.addEventListener('change', (e) => {
    document.querySelectorAll('.wiz-rate-apply').forEach(c => { c.checked = e.target.checked; });
  });
  loadWizardSheet();
});
document.addEventListener('rules-as-of-changed', () => {
  if (document.getElementById('wiz-table')) loadWizardSheet();
});
