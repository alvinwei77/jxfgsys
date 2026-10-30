function statSub(items) {
  // items: [[label, value], ...]，渲染成 stat-card 下方一排小字辅助数字
  return `<div class="stat-card-sub">${items.map(([k, v]) => `<div><span class="k">${k}</span><span class="v">${v}</span></div>`).join('')}</div>`;
}

async function loadDashboard() {
  const [d, millPeriods] = await Promise.all([
    apiGet('/api/dashboard'),
    apiGet('/api/dashboard/mill_periods'),
  ]);
  // "本期/上期"车次数、吨位、利润，都直接复用各钢厂按自己周期算完后加总的数字
  // （跟下方"各钢厂结算周期·全部钢厂总计"卡片保持完全一致，不再另造一套口径）；
  // "总计"是历史至今全量，不分期，来自 /api/dashboard 的新字段。
  const CUR_FIELDS = ['trip_count', 'tonnage', 'profit',
    'truck_trip_count', 'truck_tonnage', 'truck_profit',
    'ship_trip_count', 'ship_tonnage', 'ship_profit'];
  const cur = millPeriods.reduce((acc, m) => {
    CUR_FIELDS.forEach(f => { acc[f] += m[f] || 0; acc['prev_' + f] += m['prev_' + f] || 0; });
    return acc;
  }, Object.fromEntries(CUR_FIELDS.flatMap(f => [[f, 0], ['prev_' + f, 0]])));

  // 内链：本期/上期是各钢厂按自己周期取的车，直接用接口给的车次id跳转；接单数按接单日期区间跳
  const curIds = millPeriods.flatMap(m => m.trip_ids || []), prevIds = millPeriods.flatMap(m => m.prev_trip_ids || []);
  const idsLink = (text, ids, from) => ids.length ? drill(text, { ids }, from) : text;
  const allSettled = (text, from) => drill(text, { exclude_voided: 1, date_field: 'settlement_date', date_from: '2000-01-01' }, from);
  const orders = (text, a, b, from) => drill(text, { exclude_voided: 1, date_field: 'order_date', date_from: a, date_to: b }, from);
  document.getElementById('stats').innerHTML = `
    <div class="stat-card">
      <div class="label">今日接单</div><div class="value blue">${orders(d.today_orders, d.today, d.today, '首页·今日接单')}</div>
      ${statSub([['本期', orders(d.current_period_orders, d.period_start, d.period_end, '首页·本期接单')],
        ['上期', orders(d.prev_period_orders, d.prev_period_start, d.prev_period_end, '首页·上期接单')],
        ['总计', drill(d.total_orders, { exclude_voided: 1 }, '首页·全部车次')]])}
    </div>
    <div class="stat-card">
      <div class="label">待结算车次</div><div class="value">${drill(d.pending_settle, { status_in: 'reported,arrived' }, '首页·待结算车次')}</div>
    </div>
    <div class="stat-card">
      <div class="label">${term('已结算车次（本期）','settle_period')}</div><div class="value">${idsLink(cur.trip_count, curIds, '首页·本期已结算')}</div>
      ${statSub([['上期', idsLink(cur.prev_trip_count, prevIds, '首页·上期已结算')], ['总计', allSettled(d.total_settled_trips, '首页·全部已结算')]])}
    </div>
    <div class="stat-card">
      <div class="label">${term('送货吨位（本期）','tonnage')}</div><div class="value">${idsLink(fmtNum(cur.tonnage) + ' 吨', curIds, '首页·本期吨位')}</div>
      ${statSub([['上期', idsLink(fmtNum(cur.prev_tonnage) + ' 吨', prevIds, '首页·上期吨位')], ['总计', allSettled(fmtNum(d.total_settled_tonnage) + ' 吨', '首页·全部吨位')]])}
      ${transportSplitHtml(cur, '')}
    </div>
    <div class="stat-card">
      <div class="label" title="各钢厂按自己的结算周期分别统计后加总">${term('本期利润估算','profit_calc')}</div><div class="value ${cur.profit>=0?'green':'red'}">${idsLink('¥ ' + fmtMoney(cur.profit), curIds, '首页·本期利润')}</div>
      ${statSub([
        ['上期利润', idsLink('¥ ' + fmtMoney(cur.prev_profit), prevIds, '首页·上期利润')],
        ['留存利润', '¥ ' + fmtMoney(d.total_reserved_profit)],
        ['总利润(不含留存)', `<a class="drill" href="/reports" title="去统计报表看明细">¥ ${fmtMoney(d.total_net_profit)}</a>`],
      ])}
      ${transportSplitHtml(cur, '', 'profit')}
    </div>
  `;
}

async function loadTonnageChart() {
  const rows = await apiGet('/api/dashboard/daily_tonnage?days=14');
  const data = rows.map(r => ({ label: r.date.slice(5), value: r.tonnage,
    href: r.tonnage ? tripsUrl({ exclude_voided: 1, date_field: 'settlement_date', date_from: r.date, date_to: r.date }, '首页·' + r.date + ' 送货') : null }));
  renderBarChart(document.getElementById('tonnage-chart'), data, { height: 240, valueFmt: (v) => fmtNum(v, 0) });
}

// 票源名字点进去：看这个票源本额度周期内占用额度的车
function quotaLink(s) {
  return drill(s.name, { invoice_source_id: s.id, exclude_voided: 1, date_field: 'settlement_date', date_from: s.period_start }, `首页·${s.name}额度占用（${s.period_start}起）`);
}
const SOURCE_TYPE_LABELS = { front_company: '前端公司', reverse_individual: '反向自然人' };
const QUOTA_PERIOD_LABELS = { monthly: '月度额度', annual: '年度额度' };

// 票源额度水位 v3（2026-10-04）—— 两区，且 A 区再拆「配票 / 反向」两个小分区：
//   A 占额度：A1 配票（前端公司 M1）、A2 反向（自然人 M5）→ 保留"轨道+填充"水位条
//   B 自带票（直签带票 M2~M4）：不占我方额度，只关心"本期做了多少"→ 车次/吨位/票面额
// 角色由后端按车次结算模式推断（不再用"有没有额度"猜——否则某家"本期没票额"会被误判成自带票）。
// 时效：某家「本期没票额」或「已停止接单」后端会标记 excluded，这里只列一行灰字说明，不画水位条。
// 周期口径：月度额度=本期（26 日 ~ 次月 25 日，与经营报告同口径）、年度额度=自然年累计。
const QUOTA_SECTION = {
  A: 'A 配票 + 反向 · 占用票源额度',
  B: 'B 自带票（直签带票）· 不占我方额度',
};

function quotaSectionHead(title, note) {
  return `<div class="quota-sec-head"><span class="quota-sec-title">${title}</span><span class="quota-sec-note">${note}</span></div>`;
}
function quotaSubHead(title) {
  return `<div class="quota-sub-head">${title}</div>`;
}

function quotaMeterRow(s) {
  const periodLabel = QUOTA_PERIOD_LABELS[s.quota_period] || s.quota_period;
  const roleTxt = s.role_label || (s.source_type === 'reverse_individual' ? '反向' : '配票');
  const typeLabel = term(roleTxt + '·占额度', s.role === 'reverse' ? 'reverse' : 'peipiao');
  const pct = Math.min(100, s.pct || 0);
  const warnPct = s.warn_pct != null ? Math.min(100, s.warn_pct) : null;
  const defWarnPct = s.default_warn_pct != null ? s.default_warn_pct : 90;  // 全局预警线，见「系统 → 参数设置」
  let severity = 'accent';
  if (warnPct != null && pct >= warnPct) severity = pct >= 100 ? 'critical' : 'warning';
  else if (pct >= defWarnPct) severity = 'warning';
  const over = (s.pct || 0) >= 100;
  return `<div class="quota-row">
    <div class="quota-row-head">
      <span class="quota-name">${quotaLink(s)}</span><span class="tag">${typeLabel}</span>
      <span class="quota-pct sev-${severity}">${fmtNum(pct, 1)}%</span>
    </div>
    <div class="quota-meter">
      <div class="quota-meter-fill sev-${severity}" style="width:${pct}%"></div>
      ${warnPct != null ? `<div class="quota-meter-warnline" style="left:${warnPct}%" title="预警线 ¥${fmtMoney(s.warn_threshold)}"></div>` : ''}
    </div>
    <div class="quota-row-sub">${periodLabel}·${s.period_label || ''}${s.period_span ? `（${s.period_span}）` : ''} · 已用 ¥${fmtMoney(s.used_amount)} / ¥${fmtMoney(s.quota_amount)}${
      over ? ` · <b class="quota-over">已超 ¥${fmtMoney(s.used_amount - s.quota_amount)}</b>`
           : ` · 剩余 ¥${fmtMoney(s.quota_amount - s.used_amount)}`}${
      s.warn_threshold != null ? ` · 预警线 ¥${fmtMoney(s.warn_threshold)}` : ''}</div>
  </div>`;
}

function quotaCarryRow(s) {
  // 自带票（直签带票）：带票公司自行开票，看的是"票面额"=含税结算额（不减垫资费），与《经营报告》B 区同口径
  const amt = s.gross_amount != null ? s.gross_amount : s.used_amount;
  return `<div class="quota-row">
    <div class="quota-row-head">
      <span class="quota-name">${quotaLink(s)}</span>${term('<span class="tag tag-carrying">自带票</span>', 'carry')}
      <span class="quota-carry-amt">¥ ${fmtMoney(amt)}</span>
    </div>
    <div class="quota-row-sub">不占我方额度 · ${s.period_label || '本期'}${s.period_span ? `（${s.period_span}）` : ''} ${s.trip_count || 0} 车 · ${fmtNum(s.tonnage || 0)} 吨 · 票面额 ¥${fmtMoney(amt)}</div>
  </div>`;
}

function loadQuotaLevels() {
  return apiGet('/api/dashboard/quota_levels').then(rows => {
    const box = document.getElementById('quota-levels');
    if (!rows.length) {
      box.innerHTML = '<div class="empty-hint">暂无票源档案，请先在"基础档案"中录入前端公司/反向自然人</div>';
      return;
    }
    // 角色由后端按车次结算模式判定：peipiao 配票(M1) / reverse 反向(M5) / carry 自带票(M2~M4)
    const excluded = rows.filter(r => r.excluded);
    const aPeipiao = rows.filter(r => !r.excluded && r.role === 'peipiao' && r.quota_amount);
    const aReverse = rows.filter(r => !r.excluded && r.role === 'reverse' && r.quota_amount);
    const noQuota = rows.filter(r => !r.excluded && r.role !== 'carry' && !r.quota_amount);
    const carrying = rows.filter(r => !r.excluded && r.role === 'carry');

    const aBody =
      (aPeipiao.length ? quotaSubHead('A1 配票（前端公司 · 额度按月）') + aPeipiao.map(quotaMeterRow).join('') : '') +
      (aReverse.length ? quotaSubHead('A2 反向（自然人 · 额度按年）') + aReverse.map(quotaMeterRow).join('') : '') +
      (!aPeipiao.length && !aReverse.length ? '<div class="empty-hint">本期没有占用票额度的票源方</div>' : '') +
      (noQuota.length ? `<div class="quota-skip">未设额度上限（去「基础档案 · 票源额度分期」补）：${noQuota.map(s => s.name).join('、')}</div>` : '') +
      (excluded.length ? `<div class="quota-skip">本期不计入：${excluded.map(s => `${s.name}（${s.exclude_reason || '未启用'}）`).join('、')}</div>` : '');

    const bTotal = carrying.reduce((a, s) => ({
      trip_count: a.trip_count + (s.trip_count || 0),
      tonnage: a.tonnage + (s.tonnage || 0),
      amount: a.amount + (s.gross_amount != null ? s.gross_amount : (s.used_amount || 0)),
    }), { trip_count: 0, tonnage: 0, amount: 0 });
    const bBody = carrying.length
      ? carrying.map(quotaCarryRow).join('') +
        `<div class="quota-sec-total">合计 ${bTotal.trip_count} 车 · ${fmtNum(bTotal.tonnage)} 吨 · 票面额 ¥${fmtMoney(bTotal.amount)}</div>`
      : '<div class="empty-hint">本期没有自带票（直签带票）车次</div>';

    box.innerHTML =
      quotaSectionHead(QUOTA_SECTION.A, '月度额度看本期（26 日 ~ 次月 25 日），年度额度看自然年累计；票额是否有、按「票源额度分期」表生效') + aBody +
      quotaSectionHead(QUOTA_SECTION.B, '不占额度，只看本期做了多少（与 A 同窗口）') + bBody;
  });
}

// profitFormulaHtml() 已挪到 app.js（每页都加载），供本页和 reports.js 共用同一份口径。

function idsLinkM(text, ids, from) { return ids && ids.length ? drill(text, { ids }, from) : text; }
async function loadMillPeriods() {
  const rows = await apiGet('/api/dashboard/mill_periods');
  const box = document.getElementById('mill-periods');
  if (!rows.length) { box.innerHTML = '<div class="empty-hint">暂无钢厂档案，请先在"基础档案-钢厂"中录入</div>'; return; }

  // 总计卡片：跨钢厂加总（目前只有一家钢厂时数字会和其自身一致，多钢厂时体现汇总价值）。
  // 用字段名列表做加总，两个方向(本期/上期)共用同一份字段清单，避免逐个手写漏加字段。
  const SUM_FIELDS = [
    'trip_count', 'tonnage', 'settlement_amount', 'profit', 'gross_margin', 'reward_amount',
    'advance_fee_cost', 'other_net', 'reserved_profit', 'reserved_trip_count',
    'split_advance', 'split_trip_count', 'service_advance', 'service_trip_count',
    'service_fee_income_total', 'dispatcher_commission_total', 'ticket_cost_total', 'other_cost_total',
    'truck_trip_count', 'truck_tonnage', 'truck_profit',
    'ship_trip_count', 'ship_tonnage', 'ship_profit',
  ];
  const total = rows.reduce((acc, m) => {
    for (const f of SUM_FIELDS) {
      acc[f] += m[f] || 0;
      acc['prev_' + f] += m['prev_' + f] || 0;
    }
    return acc;
  }, Object.fromEntries(SUM_FIELDS.flatMap(f => [[f, 0], ['prev_' + f, 0]])));

  const totalCard = `<div class="mill-period-total-card">
    <div class="mill-period-name">全部钢厂 · 总计</div>
    <div class="mill-period-cycle">各钢厂按自己周期分别统计后加总；下方仍以每家钢厂"本期"为主展示</div>
    <div class="mill-period-metrics">
      <div><span class="k">本期车次数</span><span class="v">${idsLinkM(total.trip_count, rows.flatMap(m => m.trip_ids || []), '全部钢厂·本期')}</span></div>
      <div><span class="k">本期结算金额</span><span class="v">¥ ${fmtMoney(total.settlement_amount)}</span></div>
      <div><span class="k">本期吨位</span><span class="v">${fmtNum(total.tonnage)} 吨</span></div>
      <div><span class="k">本期量补金额</span><span class="v">¥ ${fmtMoney(total.reward_amount)}</span></div>
      <div><span class="k">本期利润估算</span><span class="v ${total.profit>=0?'':'red'}">¥ ${fmtMoney(total.profit)}</span></div>
    </div>
    ${transportSplitHtml(total, '', 'both')}
    ${profitFormulaHtml(total, '')}
    <div class="mill-period-prev">
      <div class="mill-period-prev-label">上期总计</div>
      <div class="mill-period-metrics">
        <div><span class="k">上期车次数</span><span class="v">${idsLinkM(total.prev_trip_count, rows.flatMap(m => m.prev_trip_ids || []), '全部钢厂·上期')}</span></div>
        <div><span class="k">上期结算金额</span><span class="v">¥ ${fmtMoney(total.prev_settlement_amount)}</span></div>
        <div><span class="k">上期吨位</span><span class="v">${fmtNum(total.prev_tonnage)} 吨</span></div>
        <div><span class="k">上期量补金额</span><span class="v">¥ ${fmtMoney(total.prev_reward_amount)}</span></div>
        <div><span class="k">上期利润估算</span><span class="v ${total.prev_profit>=0?'':'red'}">¥ ${fmtMoney(total.prev_profit)}</span></div>
      </div>
      ${profitFormulaHtml(total, 'prev_')}
    </div>
  </div>`;

  const cards = rows.map(m => {
    const gapTxt = m.gap_to_next_tier != null
      ? `距下一档(${fmtNum(m.next_tier_min_tonnage)}吨→${fmtNum(m.next_tier_unit_price)}元/吨) 还差 ${fmtNum(m.gap_to_next_tier)} 吨`
      : (m.current_reward_price ? '已达最高档量补' : '暂无量补阶梯配置');
    return `<div class="mill-period-card">
      <div class="mill-period-name">${drill(m.mill_name, { mill_id: m.mill_id, exclude_voided: 1 }, m.mill_name + '·全部车次')}</div>
      <div class="mill-period-cycle">本期：${m.period_start} ~ ${m.period_end}（每月${m.cycle_start_day}日 ~ 次月${m.cycle_end_day}日）</div>
      <div class="mill-period-metrics">
        <div><span class="k">周期累计吨位</span><span class="v">${fmtNum(m.cumulative_tonnage)} 吨</span></div>
        <div><span class="k">当前量补单价</span><span class="v">${fmtNum(m.current_reward_price)} 元/吨</span></div>
        <div><span class="k">周期车次数</span><span class="v">${idsLinkM(m.trip_count, m.trip_ids || [], m.mill_name + '·本期 ' + m.period_start + '~' + m.period_end)}</span></div>
        <div><span class="k">周期结算金额</span><span class="v">¥ ${fmtMoney(m.settlement_amount)}</span></div>
        <div><span class="k">周期量补金额</span><span class="v">¥ ${fmtMoney(m.reward_amount)}</span></div>
        <div><span class="k">周期利润估算</span><span class="v ${m.profit>=0?'':'red'}">¥ ${fmtMoney(m.profit)}</span></div>
      </div>
      ${transportSplitHtml(m, '', 'both')}
      ${profitFormulaHtml(m, '')}
      <div class="mill-period-gap">${gapTxt}</div>
      <div class="mill-period-prev">
        <div class="mill-period-prev-label">上期（${m.prev_period_start} ~ ${m.prev_period_end}）</div>
        <div class="mill-period-metrics">
          <div><span class="k">上期车次数</span><span class="v">${idsLinkM(m.prev_trip_count, m.prev_trip_ids || [], m.mill_name + '·上期 ' + m.prev_period_start + '~' + m.prev_period_end)}</span></div>
          <div><span class="k">上期结算金额</span><span class="v">¥ ${fmtMoney(m.prev_settlement_amount)}</span></div>
          <div><span class="k">上期吨位</span><span class="v">${fmtNum(m.prev_tonnage)} 吨</span></div>
          <div><span class="k">上期量补单价</span><span class="v">${fmtNum(m.prev_reward_avg_price)} 元/吨</span></div>
          <div><span class="k">上期量补金额</span><span class="v">¥ ${fmtMoney(m.prev_reward_amount)}</span></div>
          <div><span class="k">上期利润估算</span><span class="v ${m.prev_profit>=0?'':'red'}">¥ ${fmtMoney(m.prev_profit)}</span></div>
        </div>
        ${profitFormulaHtml(m, 'prev_')}
        ${m.prev_special_note ? `<div class="mill-period-note">⚠ ${m.prev_special_note}</div>` : ''}
      </div>
    </div>`;
  }).join('');

  document.getElementById('mill-periods-total').innerHTML = totalCard;
  box.innerHTML = cards;
}

// 排名汇总：调货人排名 / 供货厂汇总，复用统计报表页同一个 /api/reports/summary 接口
// （不加筛选条件 = 全量车次全时段汇总），用 tab 切换同一张表格展示的维度，而不是两张卡片。
const RANK_DATA = { base: [], dispatcher: [] };
const RANK_LABELS = { base: '供货厂', dispatcher: '调货人' };
let rankActiveTab = 'dispatcher';

async function loadRankSummary() {
  const data = await apiGet('/api/reports/summary');
  RANK_DATA.base = data.by_base;
  RANK_DATA.dispatcher = data.by_dispatcher;
  renderRankTable();
}

function switchRankTab(tab) {
  rankActiveTab = tab;
  document.querySelectorAll('#rank-tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  renderRankTable();
}

function renderRankTable() {
  document.getElementById('rank-table-name-th').textContent = RANK_LABELS[rankActiveTab];
  const rows = RANK_DATA[rankActiveTab] || [];
  const tbody = document.querySelector('#rank-table tbody');
  if (!rows.length) { tbody.innerHTML = '<tr><td colspan="5" class="empty-hint">暂无数据</td></tr>'; return; }
  const key = rankActiveTab === 'base' ? 'base_id' : 'dispatcher_id';
  tbody.innerHTML = rows.map(r => `<tr>
    <td>${drill(r.name, { [key]: r.entity_id ?? 'none', exclude_voided: 1 }, `首页排名·${RANK_LABELS[rankActiveTab]} ${r.name}`)}</td>
    <td class="num">${drill(r.trip_count, { [key]: r.entity_id ?? 'none', exclude_voided: 1 }, `首页排名·${RANK_LABELS[rankActiveTab]} ${r.name}`)}</td>
    <td class="num">${fmtNum(r.tonnage)}</td>
    <td class="num">${fmtMoney(r.settlement_amount)}</td>
    <td class="num">${fmtMoney(r.profit)}</td>
  </tr>`).join('');
}

loadDashboard();
loadTonnageChart();
loadQuotaLevels();
loadMillPeriods();
loadRankSummary();


// 资金占用（B阶段 2026-09-21）：各开票/回款环节待办车次、未登记金额、账龄分布，数据同回款追踪页
async function loadCapital() {
  const box = document.getElementById('capital-cards');
  if (!box) return;
  const [{ summary }, meta] = await Promise.all([apiGet('/api/tracking'), apiGet('/api/lanes/meta')]);
  const colors = ['#93c5fd', '#3b82f6', '#f59e0b', '#dc2626'];
  if (!summary.length) { box.innerHTML = '<div class="empty-hint">没有待办的开票/回款环节</div>'; return; }
  box.innerHTML = summary.map(s => {
    const bar = meta.aging_buckets.map((b, i) => (s.aging[b] || 0) > 0 ? `<span style="width:${(s.aging[b] / s.remaining * 100).toFixed(2)}%;background:${colors[i]}" title="${b} ¥${fmtMoney(s.aging[b])}"></span>` : '').join('');
    const leg = meta.aging_buckets.map((b, i) => (s.aging[b] || 0) > 0 ? `<span><i style="background:${colors[i]}"></i>${b}</span>` : '').join('');
    return `<a class="hop-card" href="/tracking?hop=${s.hop_key}" style="text-decoration:none;color:inherit;" title="去回款追踪看这些车">
      <div class="line-tag">${s.line === 'invoice' ? '开票线' : '回款线'}</div>
      <div><strong>${s.label}</strong> · 待办 ${s.trip_count} 车</div>
      <div class="amt">¥ ${fmtNum(s.remaining / 10000, 1)} 万</div>
      <div class="muted">最久 ${s.max_age} 天</div>
      <div class="aging-bar">${bar}</div><div class="aging-legend">${leg}</div></a>`;
  }).join('');
}
loadCapital();


// v2 待办与预警（2026-09-22）：额度、积压、开票/回款超期、提成未付、量补升档、数据缺项、备份
async function loadAlerts() {
  const box = document.getElementById('alerts');
  if (!box) return;
  const rows = await apiGet('/api/alerts');
  const ICON = { danger: '⛔', warn: '⚠', info: 'ℹ' };
  const LBL = { danger: '马上处理', warn: '近期处理', info: '提醒' };
  const n = { danger: 0, warn: 0, info: 0 };
  rows.forEach(r => n[r.level]++);
  document.getElementById('alerts-sum').textContent = rows.length ? `马上处理 ${n.danger} · 近期处理 ${n.warn} · 提醒 ${n.info}` : '';
  box.innerHTML = rows.length ? rows.map(r => `<a class="alert-row lv-${r.level}" href="${r.href}">
      <span class="alert-ico" aria-label="${LBL[r.level]}">${ICON[r.level]}</span>
      <span class="alert-lv">${LBL[r.level]}</span>
      <span class="alert-title">${r.title}</span>
      <span class="alert-detail">${r.detail || ''}</span>
      <span class="alert-go">›</span></a>`).join('') : '<div class="empty-hint">没有需要处理的事项</div>';
}
loadAlerts();
