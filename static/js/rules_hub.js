async function loadRulesHub() {
  const asOf = getRulesAsOf() || (await fetchSettlementPeriodMeta()).as_of_today;
  const data = await apiGet('/api/rules/period_overview?as_of=' + encodeURIComponent(asOf));
  const tbody = document.querySelector('#rh-terms tbody');
  if (!data.invoice_source_terms.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-hint">参考日无分期行（用档案默认列或去新增分期）</td></tr>';
  } else {
    tbody.innerHTML = data.invoice_source_terms.map(t => `<tr>
      <td>${esc(t.source_name)}</td>
      <td>${esc(t.source_type || '')}</td>
      <td class="nowrap">${t.effective_from} ~ ${t.effective_to || '至今'}</td>
      <td class="num">${t.rate_point != null ? fmtNum(t.rate_point, 1) : '—'}</td>
      <td class="num">${t.quota_amount != null ? '¥' + fmtMoney(t.quota_amount) : '—'}</td>
      <td>${t.accepting ? '接单' : '<span class="muted">停接</span>'}</td>
    </tr>`).join('');
  }
  document.getElementById('rh-rate-count').textContent = String(data.rate_links_active_count) + ' 条生效';
  document.getElementById('rh-templates').innerHTML = (data.flow_templates || []).map(ft =>
    `<div class="rh-tpl-row"><span>${esc(ft.short_name || ft.name)}</span><span class="muted">${ft.trip_count} 车在用的</span></div>`
  ).join('') || '<span class="muted">无模板</span>';
  loadRelationCalcMap();
}

async function loadRelationCalcMap() {
  const table = document.getElementById('rh-calc-map');
  if (!table) return;
  const data = await apiGet('/api/rules/relation_calc_map');
  const tbody = table.querySelector('tbody');
  if (!data.relations || !data.relations.length) {
    tbody.innerHTML = '<tr><td colspan="2" class="empty-hint">暂无映射</td></tr>';
    return;
  }
  tbody.innerHTML = data.relations.map(rel => {
    const nodes = rel.nodes.map(n => {
      const qs = new URLSearchParams({
        template: n.template_code,
        tab: 'calc',
        node: n.node_key,
      });
      return `<a href="/chain_rules?${qs}">${esc(n.template_name)}</a> · ${esc(n.node_label)}`;
    }).join('<br>');
    return `<tr><td class="nowrap">${esc(rel.label)}</td><td>${nodes}</td></tr>`;
  }).join('');
}

function esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
}

document.addEventListener('rules-as-of-changed', loadRulesHub);
document.addEventListener('DOMContentLoaded', () => {
  if (document.getElementById('rh-terms')) loadRulesHub();
});
