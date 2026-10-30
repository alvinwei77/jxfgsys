// 回款追踪（B阶段 2026-09-21）：开票/回款按环节看待办 + 账龄，勾选多车登记批次；调货人提成应付已付。
const AGING_COLORS = ['#93c5fd', '#3b82f6', '#f59e0b', '#dc2626']; // 账龄越久越醒目（15天内→60天以上）
const HOP_ORDER = ['in', 'out', 'p_out', 'p_in', 'p_client'];
const LINE_LABEL = { invoice: '开票线', payment: '回款线' };
let META = null, TRACK = null, CUR_HOP = null, SEL = new Set(), BASE_ACCOUNTS = [], BASES = [];
let COMM = null, CSEL = new Set();

// 历史导入的结算单号是一长串说明文字("台账历史数据-已出结算单（具体单号未录入）")，表格里缩成"历史·未录单号"
function shortDoc(v) { if (!v) return '-'; return /台账历史数据/.test(v) ? '历史·未录单号' : v; }
function esc(v) { return String(v ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'); }
function todayStr() { return new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10); }
function toast(msg) {
  let t = document.getElementById('toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; t.className = 'toast'; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2800);
}

async function init() {
  [META, BASE_ACCOUNTS, BASES] = await Promise.all([apiGet('/api/lanes/meta'), apiGet('/api/base_accounts'), apiGet('/api/processing_bases')]);
  await loadTracking();
  loadCommission();
  loadBatches();
  document.getElementById('t-origin').addEventListener('change', renderPending);
  document.getElementById('t-q').addEventListener('input', renderPending);
}

async function loadTracking() {
  TRACK = await apiGet('/api/tracking');
  const urlHop = new URLSearchParams(location.search).get('hop');  // 首页资金卡片点进来 → 直接定位到那个环节
  if (!CUR_HOP && urlHop) CUR_HOP = urlHop;
  if (!CUR_HOP || !TRACK.summary.find(s => s.hop_key === CUR_HOP)) CUR_HOP = (TRACK.summary[0] || {}).hop_key || 'out';
  renderSummary();
  renderTabs();
  const origins = [...new Set(TRACK.rows.map(r => r.origin))].sort();
  const sel = document.getElementById('t-origin'), keep = sel.value;
  sel.innerHTML = '<option value="">全部供货厂/车队</option>' + origins.map(o => `<option ${o === keep ? 'selected' : ''}>${esc(o)}</option>`).join('');
  renderPending();
}

function renderSummary() {
  const box = document.getElementById('t-summary');
  if (!TRACK.summary.length) { box.innerHTML = '<div class="empty-hint">没有待办的开票/回款环节</div>'; return; }
  const buckets = META.aging_buckets;
  box.innerHTML = TRACK.summary.map(s => {
    const tot = s.remaining || 0;
    const bar = buckets.map((b, i) => {
      const v = s.aging[b] || 0; return v > 0 ? `<span style="width:${(v / tot * 100).toFixed(2)}%;background:${AGING_COLORS[i]}" title="${b} ¥${fmtMoney(v)}"></span>` : '';
    }).join('');
    const leg = buckets.map((b, i) => (s.aging[b] || 0) > 0 ? `<span><i style="background:${AGING_COLORS[i]}"></i>${b} ¥${fmtNum((s.aging[b] || 0) / 10000, 1)}万</span>` : '').join('');
    return `<div class="hop-card ${s.hop_key === CUR_HOP ? 'active' : ''}" onclick="switchHop('${s.hop_key}')">
      <div class="line-tag">${LINE_LABEL[s.line] || ''}</div>
      <div><strong>${s.label}</strong> · 待办 ${s.trip_count} 车</div>
      <div class="amt">¥ ${fmtMoney(tot)}</div>
      <div class="muted">最久 ${s.max_age} 天</div>
      <div class="aging-bar">${bar}</div><div class="aging-legend">${leg}</div>
    </div>`;
  }).join('');
}

function renderTabs() {
  const tabs = document.getElementById('t-hop-tabs');
  tabs.innerHTML = TRACK.summary.map(s => `<button class="${s.hop_key === CUR_HOP ? 'active' : ''}" onclick="switchHop('${s.hop_key}')">${s.label}（${s.trip_count}）</button>`).join('');
}

function switchHop(k) { CUR_HOP = k; SEL.clear(); document.getElementById('t-all').checked = false; renderSummary(); renderTabs(); renderPending(); }

function pendingFiltered() {
  const o = document.getElementById('t-origin').value, q = document.getElementById('t-q').value.trim();
  return TRACK.rows.filter(r => r.hop_key === CUR_HOP && (!o || r.origin === o) &&
    (!q || (r.plate_no || '').includes(q) || (r.settlement_doc_no || '').includes(q)));
}

function renderPending() {
  const rows = pendingFiltered();
  const tb = document.querySelector('#t-table tbody');
  tb.innerHTML = rows.length ? rows.map(r => `<tr>
      <td><input type="checkbox" class="p-check" data-id="${r.trip_id}" ${SEL.has(r.trip_id) ? 'checked' : ''} onchange="togglePending(${r.trip_id}, this.checked)"></td>
      <td>${tripLink(r.trip_id, esc(r.plate_no || '#' + r.trip_id))}${r.transport_type === 'ship' ? ' <span class="tag">船</span>' : ''}</td>
      <td>${r.settlement_date || '-'}</td><td class="muted clip" title="${esc(r.settlement_doc_no || '')}">${esc(shortDoc(r.settlement_doc_no))}</td>
      <td>${esc(r.origin)}</td><td>${esc(r.hop_label)}${r.state === 'partial' ? ' <span class="pill">部分</span>' : ''}</td>
      <td>${esc(r.from)} → ${esc(r.to)}</td>
      <td class="num">${fmtMoney(r.expected)}</td><td class="num">${fmtMoney(r.allocated)}</td>
      <td class="num"><strong>${fmtMoney(r.remaining)}</strong></td><td class="num">${r.age_days ?? '-'}</td>
    </tr>`).join('') : '<tr><td colspan="11" class="empty-hint">这个环节没有待办</td></tr>';
  updateSelInfo();
}

function togglePending(id, on) { on ? SEL.add(id) : SEL.delete(id); updateSelInfo(); }
function toggleAllPending(on) {
  pendingFiltered().forEach(r => on ? SEL.add(r.trip_id) : SEL.delete(r.trip_id));
  document.querySelectorAll('.p-check').forEach(c => c.checked = on); updateSelInfo();
}
function updateSelInfo() {
  const rows = TRACK.rows.filter(r => r.hop_key === CUR_HOP && SEL.has(r.trip_id));
  const sum = rows.reduce((a, r) => a + (r.remaining || 0), 0);
  document.getElementById('t-sel-info').textContent = rows.length ? `已选 ${rows.length} 车，待登记合计 ¥${fmtMoney(sum)}` : '';
  document.getElementById('t-make-batch').disabled = !rows.length;
}

function accountOptions(originNames) {
  // 客户账户：优先列出所选车次对应供货厂的账户；车队车(没有真实供货厂)列出全部
  const baseIds = BASES.filter(b => originNames.includes(b.name)).map(b => b.id);
  const list = [...BASE_ACCOUNTS].sort((a, b) => (b.is_active ? 1 : 0) - (a.is_active ? 1 : 0));
  const pref = list.filter(a => baseIds.includes(a.base_id)), rest = list.filter(a => !baseIds.includes(a.base_id));
  const bn = id => (BASES.find(b => b.id === id) || {}).name || '';
  const opt = a => `<option value="${a.id}">${esc(bn(a.base_id))} · ${esc(a.account_name)}${a.card_tail ? ' 尾号' + esc(a.card_tail) : ''}${a.is_active ? '' : '（停用）'}</option>`;
  return '<option value="">（未指定）</option>' + pref.map(opt).join('') + (rest.length ? `<optgroup label="其他供货厂">${rest.map(opt).join('')}</optgroup>` : '');
}

function openBatchDialog() {
  const rows = TRACK.rows.filter(r => r.hop_key === CUR_HOP && SEL.has(r.trip_id));
  const hop = META.hops[CUR_HOP];
  const isClient = CUR_HOP === 'p_client';
  const docLabel = hop.line === 'invoice' ? '发票号（可多个，逗号分隔）' : '流水号/备注号';
  batchDialog({
    title: `登记${hop.label}批次 · ${rows.length} 车`,
    hopKey: CUR_HOP, docLabel,
    extraField: isClient ? `<div class="field"><label>打到哪张卡</label><select id="bd-acc">${accountOptions([...new Set(rows.map(r => r.origin))])}</select>
      <div class="muted">没有的话先到"基础档案 → 供货厂收款账户"里加</div></div>` : '',
    note: hop.line === 'invoice' && CUR_HOP === 'out' ? '登记后，还在"已结算"的车次会自动推进到"已开票"（开票日期=批次日期）。' : '',
    rows: rows.map(r => ({ trip_id: r.trip_id, plate: r.plate_no, sub: `${r.settlement_date || ''} · ${r.origin} · ${r.from}→${r.to}`, amount: r.remaining })),
    onSaved: async () => { SEL.clear(); await loadTracking(); loadBatches(); },
  });
}

// 通用批次弹框：整批日期/单号，逐车金额（默认=待登记），合计实时显示
function batchDialog(cfg) {
  const root = document.getElementById('dialog-root');
  root.innerHTML = `<div class="modal-overlay" onclick="if(event.target===this) closeBatchDialog()"><div class="modal adv-dialog">
    <div class="modal-header"><h2>${esc(cfg.title)}</h2><button class="close" onclick="closeBatchDialog()">&times;</button></div>
    <div class="field-row">
      <div class="field"><label>日期</label><input type="date" id="bd-date" value="${todayStr()}"></div>
      <div class="field"><label>${cfg.docLabel}</label><input id="bd-doc"></div>
      ${cfg.extraField || ''}
    </div>
    <div class="field-row"><div class="field" style="flex:1"><label>备注</label><input id="bd-notes" style="width:100%"></div></div>
    ${cfg.note ? `<div class="hint">${cfg.note}</div>` : ''}
    <div class="table-scroll"><table class="adv-table"><thead><tr><th>车号</th><th>说明</th><th class="num">本批金额</th></tr></thead><tbody>
      ${cfg.rows.map(r => `<tr><td>${esc(r.plate || '#' + r.trip_id)}</td><td class="muted">${esc(r.sub)}</td>
        <td><input class="bd-amt" data-id="${r.trip_id}" type="number" step="0.01" value="${r.amount ?? ''}"></td></tr>`).join('')}
    </tbody></table></div>
    <div class="modal-actions" style="justify-content:flex-end;">
      <span class="muted" id="bd-total"></span><span class="spacer"></span>
      <button onclick="closeBatchDialog()">取消</button><button class="btn-primary" id="bd-go">保存批次</button>
    </div></div></div>`;
  const total = () => { const s = [...document.querySelectorAll('.bd-amt')].reduce((a, i) => a + (+i.value || 0), 0); document.getElementById('bd-total').textContent = `本批合计 ¥${fmtMoney(s)}`; };
  document.querySelectorAll('.bd-amt').forEach(i => i.addEventListener('input', total)); total();
  document.getElementById('bd-go').onclick = async () => {
    const allocations = [...document.querySelectorAll('.bd-amt')].filter(i => i.value !== '' && +i.value !== 0).map(i => ({ trip_id: +i.dataset.id, amount: +i.value }));
    if (!allocations.length) { toast('至少填一车金额'); return; }
    const acc = document.getElementById('bd-acc');
    const res = await apiPost('/api/batches', {
      hop_key: cfg.hopKey, batch_date: document.getElementById('bd-date').value, doc_no: document.getElementById('bd-doc').value || null,
      notes: document.getElementById('bd-notes').value || null, base_account_id: acc ? (acc.value || null) : null, allocations,
    });
    closeBatchDialog();
    toast(`已登记 ${res.trip_count} 车，合计 ¥${fmtMoney(res.total_amount)}`);
    cfg.onSaved && cfg.onSaved();
  };
}
function closeBatchDialog() { document.getElementById('dialog-root').innerHTML = ''; }

// ---- 调货人提成 ----
async function loadCommission() {
  COMM = await apiGet('/api/analytics/commission');
  document.querySelector('#c-table tbody').innerHTML = COMM.by_dispatcher.map(d => `<tr>
    <td>${drill(esc(d.name), { dispatcher_id: d.dispatcher_id, exclude_voided: 1 }, '回款追踪·调货人 ' + d.name)}</td><td class="num">${drill(d.trip_count, { dispatcher_id: d.dispatcher_id, exclude_voided: 1 }, '回款追踪·调货人 ' + d.name)}</td><td class="num">${fmtNum(d.tonnage)}</td>
    <td class="num">${fmtMoney(d.payable)}</td><td class="num">${fmtMoney(d.paid)}</td>
    <td class="num"><strong class="${d.unpaid > 0.01 ? 'red' : ''}">${fmtMoney(d.unpaid)}</strong></td></tr>`).join('')
    || '<tr><td colspan="6" class="empty-hint">暂无调货人车次</td></tr>';
  document.querySelector('#c-trips tbody').innerHTML = COMM.unpaid_trips.map(r => `<tr>
    <td><input type="checkbox" class="c-check" data-id="${r.trip_id}" ${CSEL.has(r.trip_id) ? 'checked' : ''} onchange="toggleComm(${r.trip_id}, this.checked)"></td>
    <td>${esc(r.dispatcher_name)}</td><td>${tripLink(r.trip_id, esc(r.plate_no || '#' + r.trip_id))}</td><td>${r.settlement_date || '-'}</td>
    <td class="num">${fmtNum(r.tonnage)}</td><td class="num">${fmtMoney(r.payable)}</td><td class="num">${fmtMoney(r.paid)}</td><td class="num">${fmtMoney(r.unpaid)}</td></tr>`).join('')
    || '<tr><td colspan="8" class="empty-hint">没有未付提成</td></tr>';
  document.getElementById('c-make-batch').disabled = !CSEL.size;
}
function toggleComm(id, on) { on ? CSEL.add(id) : CSEL.delete(id); document.getElementById('c-make-batch').disabled = !CSEL.size; }
function toggleAllComm(on) { COMM.unpaid_trips.forEach(r => on ? CSEL.add(r.trip_id) : CSEL.delete(r.trip_id)); document.querySelectorAll('.c-check').forEach(c => c.checked = on); document.getElementById('c-make-batch').disabled = !CSEL.size; }
function openCommissionDialog() {
  const rows = COMM.unpaid_trips.filter(r => CSEL.has(r.trip_id));
  const names = [...new Set(rows.map(r => r.dispatcher_name))];
  batchDialog({
    title: `登记提成付款 · ${names.join('、')} · ${rows.length} 车`, hopKey: 'commission', docLabel: '流水号/备注号',
    note: names.length > 1 ? '选中了多个调货人的车次，建议按调货人分开登记（一次打给一个人）。' : '',
    rows: rows.map(r => ({ trip_id: r.trip_id, plate: r.plate_no, sub: `${r.dispatcher_name} · ${r.settlement_date || ''} · ${fmtNum(r.tonnage)}吨`, amount: r.unpaid })),
    onSaved: async () => { CSEL.clear(); await loadCommission(); loadBatches(); },
  });
}

// ---- 批次记录 ----
async function loadBatches() {
  const kind = document.getElementById('b-kind').value;
  const rows = await apiGet('/api/batches' + (kind ? '?kind=' + kind : ''));
  document.querySelector('#b-table tbody').innerHTML = rows.map(b => `<tr>
    <td>${b.batch_date || '-'}</td><td>${esc((META.hops[b.hop_key] || {}).label || b.hop_key)}</td>
    <td>${esc(b.from_name || '-')} → ${esc(b.to_name || '-')}</td><td>${esc(b.doc_no || '-')}</td>
    <td>${b.account_name ? esc((b.account_base_name || '') + ' · ' + b.account_name + (b.card_tail ? ' 尾号' + b.card_tail : '')) : '-'}</td>
    <td class="num"><a class="drill" href="javascript:void(0)" onclick="viewBatch(${b.id})">${b.trip_count}</a></td><td class="num">${fmtMoney(b.allocated)}</td><td class="muted">${esc(b.notes || '')}</td>
    <td class="nowrap"><button class="btn-sm" onclick="viewBatch(${b.id})">明细</button> <button class="btn-sm btn-danger" onclick="deleteBatch(${b.id})">删除</button></td>
  </tr>`).join('') || '<tr><td colspan="9" class="empty-hint">还没有登记过批次</td></tr>';
}
async function viewBatch(id) {
  const d = await apiGet('/api/batches/' + id);
  const b = d.batch;
  document.getElementById('dialog-root').innerHTML = `<div class="modal-overlay" onclick="if(event.target===this) closeBatchDialog()"><div class="modal adv-dialog">
    <div class="modal-header"><h2>${esc((META.hops[b.hop_key] || {}).label)}批次 · ${b.batch_date || ''}</h2><button class="close" onclick="closeBatchDialog()">&times;</button></div>
    <div class="muted">${esc(b.from_name || '')} → ${esc(b.to_name || '')}　单号：${esc(b.doc_no || '-')}　合计 ¥${fmtMoney(b.total_amount)}</div>
    <div class="table-scroll"><table class="adv-table"><thead><tr><th>车号</th><th>结算日期</th><th>结算单号</th><th>供货厂</th><th class="num">金额</th></tr></thead><tbody>
    ${d.trips.map(t => `<tr><td>${tripLink(t.trip_id, esc(t.plate_no || '#' + t.trip_id))}</td><td>${t.settlement_date || '-'}</td><td class="muted">${esc(t.settlement_doc_no || '-')}</td><td>${esc(t.base_name || '-')}</td><td class="num">${fmtMoney(t.amount)}</td></tr>`).join('')}
    </tbody></table></div></div></div>`;
}
async function deleteBatch(id) {
  if (!confirm('删除这个批次？分摊到各车次的金额会一起撤销（车次状态不会回退）。')) return;
  await apiDelete('/api/batches/' + id);
  toast('已删除批次');
  await loadTracking(); loadCommission(); loadBatches();
}

init();
