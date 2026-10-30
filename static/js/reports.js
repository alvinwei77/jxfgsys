// 统计报表：按钢厂/供货厂/调货人 + 自定义时间范围 或 钢厂自身结算周期 查询吨位/金额/利润汇总

async function initReports() {
  const mills = await apiGet('/api/steel_mills');
  document.getElementById('r-mill').innerHTML = '<option value="">全部钢厂</option>' + optionsHtml(mills, 'id', 'name');
  document.getElementById('r-mill').addEventListener('change', async () => {
    await loadCyclePeriods();
    runQuery();
  });
  document.getElementById('r-cycle-period').addEventListener('change', (e) => {
    const v = e.target.value;
    if (!v) return;
    const [from, to] = v.split('|');
    document.getElementById('r-from').value = from;
    document.getElementById('r-to').value = to;
    document.getElementById('r-datefield').value = 'settlement_date';
    runQuery();
  });
  document.getElementById('r-transport').addEventListener('change', runQuery);
  await loadCyclePeriods();
  await runQuery();
}

async function loadCyclePeriods() {
  const millId = document.getElementById('r-mill').value;
  const sel = document.getElementById('r-cycle-period');
  if (!millId) {
    sel.innerHTML = '<option value="">选择钢厂后可按其历史结算周期快速筛选</option>';
    return;
  }
  const periods = await apiGet('/api/reports/mill_periods_list?mill_id=' + millId);
  if (!periods.length) {
    sel.innerHTML = '<option value="">该钢厂暂无历史车次</option>';
    return;
  }
  sel.innerHTML = '<option value="">-- 按结算周期快速选择 --</option>' +
    periods.map(p => `<option value="${p.period_start}|${p.period_end}">${p.period_start} ~ ${p.period_end}（${p.trip_count}车 / ${fmtNum(p.tonnage)}吨 / 利润¥${fmtNum(p.profit)}）</option>`).join('');
}

function resetQuery() {
  document.getElementById('r-transport').value = '';
  document.getElementById('r-mill').value = '';
  document.getElementById('r-cycle-period').innerHTML = '<option value="">选择钢厂后可按其历史结算周期快速筛选</option>';
  document.getElementById('r-datefield').value = 'settlement_date';
  document.getElementById('r-from').value = '';
  document.getElementById('r-to').value = '';
  runQuery();
}

async function runQuery() {
  const params = new URLSearchParams();
  const mill = document.getElementById('r-mill').value;
  const from = document.getElementById('r-from').value;
  const to = document.getElementById('r-to').value;
  const field = document.getElementById('r-datefield').value;
  const transport = document.getElementById('r-transport').value;
  if (transport) params.set('transport_type', transport);
  if (mill) params.set('mill_id', mill);
  if (from) params.set('date_from', from);
  if (to) params.set('date_to', to);
  params.set('date_field', field);
  LAST_PARAMS = params;
  const data = await apiGet('/api/reports/summary?' + params.toString());
  renderStats(data.totals);
  document.getElementById('r-profit-formula').innerHTML =
    transportSplitHtml(data.totals, '') + profitFormulaHtml(data.totals, '');
  renderBreakdown('r-table-mill', data.by_mill, 'mill');
  renderBreakdown('r-table-base', data.by_base, 'base');
  renderBreakdown('r-table-dispatcher', data.by_dispatcher, 'dispatcher');
  renderBreakdown('r-table-invoice-source', data.by_invoice_source, 'invoice_source');
  renderChart('r-chart-mill', data.by_mill, 'mill');
  renderChart('r-chart-dispatcher', data.by_dispatcher, 'dispatcher');
  renderPathCards(data.by_path, null, [{ family: 'split' }, { family: 'service_fee' }]);
  renderPathCards(data.by_transport, 'r-transport-cards', [{ transport_type: 'truck' }, { transport_type: 'ship' }]);
  LAST_PARAMS = params;
  loadTrend(); loadQuality(); loadCompleteness();
}

// ---------------- 内链（2026-09-21）：报表里的数字点进去看对应车次，带上当前筛选 ----------------
const DIM_KEY = { mill: 'mill_id', base: 'base_id', dispatcher: 'dispatcher_id', invoice_source: 'invoice_source_id' };
const DIM_CN = { mill: '钢厂', base: '供货厂', dispatcher: '调货人', invoice_source: '票源方' };
function reportFilter(extra) {
  const f = { exclude_voided: 1 };
  ['mill_id', 'transport_type', 'date_from', 'date_to'].forEach(k => { if (LAST_PARAMS.get(k)) f[k] = LAST_PARAMS.get(k); });
  if (f.date_from || f.date_to) f.date_field = LAST_PARAMS.get('date_field') || 'settlement_date';
  return Object.assign(f, extra || {});
}
function reportScope() {
  const a = LAST_PARAMS.get('date_from'), b = LAST_PARAMS.get('date_to');
  return (a || b) ? `（${a || '…'} ~ ${b || '…'}）` : '（全部）';
}
function rDrill(text, extra, from) { return drill(text, reportFilter(extra), '统计报表·' + from + reportScope()); }

// ---------------- 全方位统计补充（2026-09-21）----------------
let LAST_PARAMS = new URLSearchParams(), TREND_GRAN = 'week';
function pptFmt(v) { return v == null ? '-' : fmtNum(v, 1); }
function setTrendGran(g) {
  TREND_GRAN = g;
  document.querySelectorAll('#r-trend-tabs button').forEach(b => b.classList.toggle('active', b.dataset.g === g));
  loadTrend();
}
// 趋势柱子点进去：该区间(日/周/月/结算周期)的车次；跟报表其它筛选(钢厂/车船)叠加，日期用柱子自己的区间
function trendHref(r) {
  const f = reportFilter({ date_field: LAST_PARAMS.get('date_field') || 'settlement_date', date_from: r.start, date_to: r.end });
  return tripsUrl(f, `统计报表·趋势 ${r.label}`);
}
async function loadTrend() {
  const p = new URLSearchParams(LAST_PARAMS); p.set('granularity', TREND_GRAN);
  const { series } = await apiGet('/api/analytics/trend?' + p.toString());
  const rows = series.map(r => ({ ...r, ppt: r.profit_per_ton || 0 }));
  const tonExtra = r => `<div>车次 ${r.trip_count}（车${r.truck_trip_count}/船${r.ship_trip_count}）</div>`;
  renderStackedBars(document.getElementById('r-trend-ton'), rows,
    [{ key: 'truck_tonnage', label: '车', color: TRUCK_COLOR }, { key: 'ship_tonnage', label: '船', color: SHIP_COLOR }],
    { unit: ' 吨', extra: tonExtra, href: trendHref });
  renderSignedBars(document.getElementById('r-trend-profit'), rows, 'profit',
    { name: '利润', unit: ' 元', href: trendHref, extra: r => `<div>配票 ${fmtNum(r.split_profit, 0)} · 带票 ${fmtNum(r.service_profit, 0)}</div><div>车 ${fmtNum(r.truck_profit, 0)} · 船 ${fmtNum(r.ship_profit, 0)}</div>` });
  renderSignedBars(document.getElementById('r-trend-ppt'), rows, 'ppt',
    { name: '单吨利润', unit: ' 元/吨', color: '#2563eb', href: trendHref, fmt: v => fmtNum(v, 1), extra: r => `<div>${fmtNum(r.tonnage)} 吨 · ¥${fmtNum(r.profit, 0)}</div>` });
  document.querySelector('#r-trend-table tbody').innerHTML = series.map(r => `<tr><td>${r.label}</td>
    <td class="num"><a class="drill" href="${trendHref(r)}">${r.trip_count}</a></td><td class="num">${fmtNum(r.tonnage)}</td><td class="num">${fmtNum(r.truck_tonnage)}</td><td class="num">${fmtNum(r.ship_tonnage)}</td>
    <td class="num">${fmtMoney(r.settlement_amount)}</td><td class="num">${fmtMoney(r.profit)}</td><td class="num">${pptFmt(r.profit_per_ton)}</td>
    <td class="num">${fmtMoney(r.split_profit)}</td><td class="num">${fmtMoney(r.service_profit)}</td></tr>`).join('') || '<tr><td colspan="10" class="empty-hint">暂无数据</td></tr>';
}
async function loadQuality() {
  const q = await apiGet('/api/analytics/quality?' + LAST_PARAMS.toString());
  const rate = v => v == null ? '-' : fmtNum(v, 2) + '%';
  document.querySelector('#r-q-origin tbody').innerHTML = q.by_origin.map(o => `<tr><td>${drill(o.name, { ids: o.trip_ids }, '统计报表·质量 ' + o.name)}</td><td class="num">${drill(o.trip_count, { ids: o.trip_ids }, '统计报表·质量 ' + o.name)}</td>
    <td class="num">${fmtNum(o.net)}</td><td class="num">${fmtNum(o.deduction)}</td><td class="num">${rate(o.deduction_rate)}</td>
    <td class="num">${o.avg_price == null ? '-' : fmtNum(o.avg_price, 0)}</td></tr>`).join('') || '<tr><td colspan="6" class="empty-hint">暂无数据</td></tr>';
  document.querySelector('#r-q-type tbody').innerHTML = q.by_type.map(t => `<tr><td>${drill(t.name, { ids: t.trip_ids }, '统计报表·货型 ' + t.name)}</td><td class="num">${fmtNum(t.tonnage)}</td>
    <td class="num">${t.avg_price == null ? '-' : fmtNum(t.avg_price, 0)}</td><td class="num">${rate(t.deduction_rate)}</td>
    <td class="nowrap muted">${t.monthly.map(m => `${m.month.slice(5)}月 ${m.avg_price == null ? '-' : fmtNum(m.avg_price, 0)}`).join(' → ')}</td></tr>`).join('') || '<tr><td colspan="5" class="empty-hint">暂无数据</td></tr>';
}
async function loadCompleteness() {
  const c = await apiGet('/api/analytics/completeness?' + LAST_PARAMS.toString());
  const bad = c.rules.filter(r => r.count);
  document.getElementById('r-completeness').innerHTML = `<div class="muted" style="margin-bottom:8px;">共 ${c.total_trips} 车；${bad.length ? `有 ${bad.length} 类问题` : '没有发现缺失'}。车队车（调货人自己的车）没填供货厂不算缺失。</div>` +
    c.rules.map(r => `<details class="comp-row" ${r.count ? '' : 'disabled'}><summary><span class="pill ${r.count ? 'red' : 'green'}">${r.count}</span> ${r.label}</summary>
      ${r.count ? `<div class="comp-trips">${r.trips.map(t => `<a href="/trips?open=${t.trip_id}" class="node-chip">${t.plate_no || '#' + t.trip_id} <span class="muted">${t.date || ''}</span></a>`).join(' ')}</div>` : ''}</details>`).join('');
}

function renderStats(totals) {
  document.getElementById('r-stats').innerHTML = `
    <div class="stat-card"><div class="label">车次数</div><div class="value blue">${rDrill(totals.trip_count, {}, '全部车次')}</div></div>
    <div class="stat-card"><div class="label">${term('总吨位','tonnage')}</div><div class="value">${fmtNum(totals.tonnage)} 吨</div></div>
    <div class="stat-card"><div class="label">${term('总结算金额','amount')}</div><div class="value">¥${fmtMoney(totals.settlement_amount)}</div></div>
    <div class="stat-card"><div class="label">${term('总利润估算','profit_calc')}</div><div class="value ${totals.profit>=0?'green':'red'}">¥${fmtMoney(totals.profit)}</div></div>
    <div class="stat-card"><div class="label">${term('单吨利润','profit_calc')}</div><div class="value">${pptFmt(totals.profit_per_ton)} <span style="font-size:13px;">元/吨</span></div>
      <div class="stat-card-sub"><div><span class="k">配票</span><span class="v">${pptFmt(totals.split_profit_per_ton)}</span></div><div><span class="k">带票</span><span class="v">${pptFmt(totals.service_profit_per_ton)}</span></div><div><span class="k">车</span><span class="v">${pptFmt(totals.truck_profit_per_ton)}</span></div><div><span class="k">船</span><span class="v">${pptFmt(totals.ship_profit_per_ton)}</span></div></div></div>
  `;
}

// 一条船的吨位是一辆车的十倍上下，只看合计的"总车次/总吨位"分不出来某个调货人是跑了50趟车
// 还是拉了2条船；想看"只有船的那部分利润是多少"，用上方的"只看船运"筛选，所有维度只统计船。
// 车/船合成一列：只写次数，吨位放悬停提示；没有船就只显示车（反之亦然），省出横向空间
function carShipCell(r) {
  const parts = [];
  if (r.truck_trip_count) parts.push(`<span class="t-truck" title="车 ${r.truck_trip_count}次 / ${fmtNum(r.truck_tonnage)}吨">车${r.truck_trip_count}</span>`);
  if (r.ship_trip_count) parts.push(`<span class="t-ship" title="船 ${r.ship_trip_count}次 / ${fmtNum(r.ship_tonnage)}吨">船${r.ship_trip_count}</span>`);
  return `<td class="num">${parts.join(' · ') || '-'}</td>`;
}
function renderBreakdown(tableId, rows, kind) {
  const tbody = document.querySelector('#' + tableId + ' tbody');
  if (!rows.length) { tbody.innerHTML = '<tr><td colspan="7" class="empty-hint">暂无数据</td></tr>'; return; }
  // 名称可点：下钻到该实体的本期/上期/总计三栏对照（带票 vs 配票、提成、车船、利润）。
  // entity_id 为空的是"未指定"那一行（比如没填调货人的车次），没有实体可下钻。
  const dimVal = r => r.entity_id ?? 'none';
  const nameCell = (r) => (kind && r.entity_id)
    ? `<td><a href="javascript:void(0)" onclick="openEntityDetail('${kind}',${r.entity_id})"
         style="color:var(--blue);text-decoration:none;border-bottom:1px dashed currentColor;">${r.name}</a></td>`
    : `<td>${r.name}</td>`;
  tbody.innerHTML = rows.map(r => `<tr>
    ${nameCell(r)}
    <td class="num">${rDrill(r.trip_count, { [DIM_KEY[kind]]: dimVal(r) }, `${DIM_CN[kind]} ${r.name}`)}</td>
    <td class="num">${fmtNum(r.tonnage)}</td>
    <td class="num">${fmtMoney(r.settlement_amount)}</td>
    <td class="num">${fmtMoney(r.profit)}</td>
    <td class="num">${pptFmt(r.profit_per_ton)}</td>
    ${carShipCell(r)}
  </tr>`).join('');
}

// 给领导看的可视化：按钢厂/调货人利润排名的柱状图，复用 app.js::renderBarChart（不依赖
// 外部图表库）。负利润的柱子也能正常画出来（renderBarChart 内部按最大值定比例，负值柱高为0，
// 数值仍标注在轴上方——极少数亏损维度不会导致图表报错或空白）。
function renderChart(containerId, rows, kind) {
  const box = document.getElementById(containerId);
  if (!rows.length) { box.innerHTML = '<div class="empty-hint">暂无数据</div>'; return; }
  const top = rows.slice(0, 12); // 最多展示12条，避免维度过多时柱子挤在一起看不清
  const data = top.map(r => ({ label: r.name.length > 6 ? r.name.slice(0, 6) + '…' : r.name, value: r.profit,
    href: tripsUrl(reportFilter({ [DIM_KEY[kind]]: r.entity_id ?? 'none' }), `统计报表·${DIM_CN[kind]} ${r.name}${reportScope()}`) }));
  renderBarChart(box, data, { color: '#16a34a', valueFmt: (v) => fmtNum(v, 0) });
}

// 按结算路径(配票/反向差价模型 vs 带票带货服务费模型)对比：固定两张卡片，即使某条路径
// 当前筛选范围内0车次也展示出来，方便直观对比两种模式的占比与利润结构差异。
function renderPathCards(rows, containerId, filters) {
  const box = document.getElementById(containerId || 'r-path-cards');
  // 车/船对比卡片本身就是按运输方式分的，卡片里再补一行车船明细纯属重复，跳过
  const withSplit = containerId !== 'r-transport-cards';
  box.innerHTML = (rows || []).map((r, i) => `
    <div class="card" style="flex:1;min-width:280px;">
      <div class="panel-title">${r.name}</div>
      <div class="mill-period-metrics">
        <div><span class="k">车次数</span><span class="v">${filters && filters[i] ? rDrill(r.trip_count, filters[i], r.name) : r.trip_count}</span></div>
        <div><span class="k">吨位</span><span class="v">${fmtNum(r.tonnage)} 吨</span></div>
        <div><span class="k">结算金额</span><span class="v">¥ ${fmtMoney(r.settlement_amount)}</span></div>
        <div><span class="k">利润估算</span><span class="v ${r.profit>=0?'':'red'}">¥ ${fmtMoney(r.profit)}</span></div>
      </div>
      ${withSplit ? transportSplitHtml(r, '') : ''}
      ${profitFormulaHtml(r, '')}
    </div>
  `).join('');
}

initReports();

// ---------------- 调货员/供货厂 详情下钻（2026-09-20）----------------
// 表格一行只给一个汇总数，回答不了"这个调货员本期比上期怎么样、提成拿了多少、其中多少是
// 带票多少是我们配票"。点名称弹出三栏对照：本期 / 上期 / 总计。
// "本期/上期"跟首页、钢厂周期卡片同一套口径（各钢厂按自己的结算周期分别算再加总），
// 不另造一套，否则同一个人在两个页面会看到两个不一样的"本期"。
async function openEntityDetail(kind, id) {
  document.getElementById('entity-detail-overlay')?.remove();
  const box = document.createElement('div');
  box.id = 'entity-detail-overlay';
  box.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.35);z-index:900;'
    + 'display:flex;align-items:center;justify-content:center;';
  box.onclick = (ev) => { if (ev.target === box) box.remove(); };
  box.innerHTML = '<div class="card" style="min-width:320px;">加载中…</div>';
  document.body.appendChild(box);

  let d;
  try {
    d = await apiGet(`/api/entity_stats?kind=${kind}&id=${id}`);
  } catch (e) {
    box.querySelector('.card').innerHTML = `加载失败：${e.message}`;
    return;
  }

  const cols = [
    { key: 'current', title: '本期', sub: d.current_label },
    { key: 'prev', title: '上期', sub: d.prev_label },
    { key: 'total', title: '总计', sub: '历史至今' },
  ];
  const cell = (fn) => cols.map(c => `<td class="num">${fn(d[c.key])}</td>`).join('');
  const carShip = (b) => {
    const parts = [];
    if (b.truck_trip_count) parts.push(`<span class="t-truck">车 ${b.truck_trip_count}</span>`);
    if (b.ship_trip_count) parts.push(`<span class="t-ship">船 ${b.ship_trip_count}</span>`);
    return parts.join(' / ') || '-';
  };
  const dash = (v, fmt) => v ? fmt(v) : '<span style="color:var(--gray-300);">-</span>';
  const modeName = (fam) => fam === 'service_fee' ? '带票' : '配票';

  box.querySelector('.card').outerHTML = `
    <div class="card" style="max-width:720px;width:94%;max-height:86vh;overflow:auto;" onclick="event.stopPropagation()">
      <div class="toolbar" style="margin-bottom:10px;">
        <b style="font-size:16px;">${d.kind_cn} · ${d.name}</b>
        <div class="spacer"></div>
        <button class="btn-sm" onclick="document.getElementById('entity-detail-overlay').remove()">关闭</button>
      </div>
      <div class="table-scroll"><table>
        <thead><tr><th></th>${cols.map(c =>
          `<th class="num">${c.title}<div style="font-weight:400;color:var(--gray-400);font-size:10.5px;">${c.sub}</div></th>`).join('')}</tr></thead>
        <tbody>
          <tr><td>车次数</td>${cols.map(c => `<td class="num">${c.key === 'total'
            ? drill(d.total.trip_count, { [d.col]: d.id, exclude_voided: 1 }, `${d.kind_cn} ${d.name}·全部`)
            : ((d[c.key].trip_ids || []).length ? drill(d[c.key].trip_count, { ids: d[c.key].trip_ids }, `${d.kind_cn} ${d.name}·${c.title} ${c.sub}`) : d[c.key].trip_count)}</td>`).join('')}</tr>
          <tr><td>其中车/船</td>${cell(carShip)}</tr>
          <tr><td>总吨位</td>${cell(b => fmtNum(b.tonnage) + ' 吨')}</tr>
          <tr><td>结算金额</td>${cell(b => '¥ ' + fmtMoney(b.settlement_amount))}</tr>
          <tr style="background:var(--blue-light);">
            <td><b>调货人提成</b></td>${cell(b => '<b>¥ ' + fmtMoney(b.dispatcher_commission_total) + '</b>')}</tr>
          <tr><td>利润估算</td>${cell(b => '¥ ' + fmtMoney(b.profit))}</tr>
          <tr><td>单吨利润</td>${cell(b => b.tonnage ? fmtNum(b.profit / b.tonnage, 1) + ' 元/吨' : '-')}</tr>
          <tr><td colspan="4" style="padding-top:12px;color:var(--gray-500);font-size:12px;">
            按结算路径拆开 —— 配票/反向是差价模型(M1/M5)，带票带货是服务费模型(M2/M3/M4)</td></tr>
          <tr><td>配票 · 车次</td>${cell(b => dash(b.split_trip_count, v => v))}</tr>
          <tr><td>配票 · 吨位</td>${cell(b => dash(b.split_tonnage, v => fmtNum(v) + ' 吨'))}</tr>
          <tr><td>配票 · 提成</td>${cell(b => dash(b.split_commission, v => '¥ ' + fmtMoney(v)))}</tr>
          <tr><td>配票 · 利润</td>${cell(b => dash(b.split_profit, v => '¥ ' + fmtMoney(v)))}</tr>
          <tr><td>带票 · 车次</td>${cell(b => dash(b.service_trip_count, v => v))}</tr>
          <tr><td>带票 · 吨位</td>${cell(b => dash(b.service_tonnage, v => fmtNum(v) + ' 吨'))}</tr>
          <tr><td>带票 · 提成</td>${cell(b => dash(b.service_commission, v => '¥ ' + fmtMoney(v)))}</tr>
          <tr><td>带票 · 利润</td>${cell(b => dash(b.service_profit, v => '¥ ' + fmtMoney(v)))}</tr>
        </tbody>
      </table></div>

      <div class="panel-title" style="margin-top:14px;">最近车次（最多12笔） ${drill('查看全部车次 ›', { [d.col]: d.id, exclude_voided: 1 }, `${d.kind_cn} ${d.name}·全部`)}</div>
      <div class="table-scroll"><table>
        <thead><tr><th>日期</th><th>车号/船号</th><th>类型</th><th>钢厂</th><th>路径</th>
          <th class="num">吨位</th><th class="num">结算金额</th></tr></thead>
        <tbody>${(d.recent || []).map(t => `<tr>
          <td class="nowrap">${t.settlement_date || t.order_date || '-'}</td>
          <td>${tripLink(t.id, t.plate_no || '#' + t.id)}</td>
          <td>${t.transport_type === 'ship' ? '<span class="t-ship">船</span>' : '<span class="t-truck">车</span>'}</td>
          <td>${t.mill_name || '-'}</td>
          <td class="nowrap">${t.template_short || modeName(t.calc_family)}</td>
          <td class="num">${fmtNum(t.settlement_tonnage_total)}</td>
          <td class="num">${fmtMoney(t.settlement_amount_total)}</td>
        </tr>`).join('') || '<tr><td colspan="7" class="empty-hint">暂无车次</td></tr>'}</tbody>
      </table></div>

      <div style="font-size:11.5px;color:var(--gray-500);margin-top:10px;">
        本期/上期是各钢厂按自己的结算周期分别统计后加总，跟首页看板、钢厂周期卡片口径一致；
        「总计」是历史至今全量，不受上方筛选条件影响。利润为已固化车次取快照、未固化车次实时估算。
      </div>
    </div>`;
}

// 一键导出「经营报告（分期与专题）」Excel：①总览/各期明细/调货人工资/量补汇总/票源额度水位。
// 与台账导出一样走浏览器原生下载（后端返回带 Content-Disposition 的文件流），缺省=库中全部期。
function exportOpsReportXlsx(btn) {
  if (btn) { btn.disabled = true; btn.dataset.orig = btn.textContent; btn.textContent = '生成中…'; }
  // fetch 更稳：下载完成/失败都能恢复按钮，且能感知后端报错
  fetch('/api/export/ops_report').then(r => {
    if (!r.ok) return r.json().then(j => { throw new Error(j.error || ('HTTP ' + r.status)); });
    const cd = r.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/);
    return r.blob().then(b => ({ b, name: m ? decodeURIComponent(m[1]) : null }));
  }).then(({ b, name }) => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(b);
    a.download = name || ('经营报告_' + new Date().toISOString().slice(0, 10) + '.xlsx');
    a.click();
    URL.revokeObjectURL(a.href);
  }).catch(e => { alert('导出失败：' + e.message); }).finally(() => {
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.orig; }
  });
}
