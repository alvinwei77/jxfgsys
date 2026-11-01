const STATUS_LIST = Array.from(document.querySelectorAll('#f-status option'))
  .filter(o => o.value)
  .map(o => ({ code: o.value, label: o.textContent }));

let ARCH = { mills: [], bases: [], dispatchers: [], accounts: [], sources: [], scrapTypes: [], modes: [], vehicles: [], drivers: [] };
let currentTripId = null;
let previewTimer = null;

async function loadArchives() {
  const [mills, bases, dispatchers, accounts, sources, scrapTypes, modes, vehicles, drivers] = await Promise.all([
    apiGet('/api/steel_mills'), apiGet('/api/processing_bases'), apiGet('/api/dispatchers'),
    apiGet('/api/accounts'), apiGet('/api/invoice_sources'), apiGet('/api/scrap_types'),
    apiGet('/api/trip_modes'), apiGet('/api/vehicles'), apiGet('/api/drivers'),
  ]);
  // 2026-09-19：界面上不再出现 M1/M2 这套代号，"结算模式"直接选链条规则（同一个东西，
  // 以前两套名字两个入口，用户每次都要在脑子里翻译）。只列启用中的，0车次的死规则不出现。
  ALL_TEMPLATES = await apiGet('/api/flow_templates');
  const templates = ALL_TEMPLATES.filter(t => t.is_active);
  ARCH = { mills, bases, dispatchers, accounts, sources, scrapTypes, modes, vehicles, drivers, templates };
  loadPlates();
  LANE_META = await apiGet('/api/lanes/meta');
  document.getElementById('f-mill').innerHTML = '<option value="">全部钢厂</option>' + optionsHtml(mills, 'id', 'name');
  document.getElementById('f-base').innerHTML = '<option value="">全部供货厂</option>' + optionsHtml(bases, 'id', 'name');
}

// ------- A阶段(2026-09-21)：录入提速 -------
let PLATES = [];
async function loadPlates() {
  PLATES = await apiGet('/api/plates');
  let dl = document.getElementById('plate-datalist');
  if (!dl) { dl = document.createElement('datalist'); dl.id = 'plate-datalist'; document.body.appendChild(dl); }
  dl.innerHTML = PLATES.map(p => `<option value="${attrEsc(p.plate_no)}">${p.transport_type === 'ship' ? '船' : '车'} · ${p.n}次 · 最近${p.last_date || '-'}</option>`).join('');
}
function attrEsc(v) { return String(v ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'); }
function toast(msg, ms = 2600) {
  let t = document.getElementById('toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; t.className = 'toast'; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), ms);
}
const PROFILE_LABEL = { transport_type: '车/船', base_id: '供货厂', dispatcher_id: '调货人', invoice_source_id: '票源方', template_id: '链条规则', driver_name: '司机', driver_phone: '司机电话', driver_id_card: '身份证' };
function nameOf(field, id) {
  const src = { base_id: ARCH.bases, dispatcher_id: ARCH.dispatchers, invoice_source_id: ARCH.sources, template_id: ARCH.templates }[field];
  if (!src) return field === 'transport_type' ? (id === 'ship' ? '船' : '车') : id;
  const o = src.find(x => String(x.id) === String(id));
  return o ? o.name : id;
}
// 车牌输入后按历史带出：只填当前为空的字段，且只带"这个车牌历史上稳定"的值
// (同一取值≥80%)；像林凯车队的车，调货人稳定会带出，供货厂经常变就留空不猜。
async function applyPlateProfile(trip, plate) {
  const prof = await apiGet('/api/plate_profile?plate=' + encodeURIComponent(plate));
  const patch = { plate_no: plate };
  const filled = [];
  for (const [f, v] of Object.entries(prof.fill || {})) {
    if (trip[f] === null || trip[f] === undefined || trip[f] === '' || (f === 'transport_type' && prof.count)) {
      if (String(trip[f] ?? '') !== String(v)) { patch[f] = v; filled.push(`${PROFILE_LABEL[f]}：${nameOf(f, v)}`); }
    }
  }
  await apiPut(`/api/trips/${trip.id}`, patch);
  const varies = Object.keys(prof.varies || {}).filter(f => !trip[f]).map(f => PROFILE_LABEL[f]);
  if (prof.fleet_of_dispatcher) {
    toast(`${plate} 是${nameOf('dispatcher_id', prof.fleet_of_dispatcher)}车队的车（历史${prof.count}次），已带出调货人；供货厂可留空` +
      (varies.filter(v => v !== '供货厂').length ? `；${varies.filter(v => v !== '供货厂').join('/')}每次不同，请手选` : ''), 4500);
  } else if (prof.count) {
    toast(`${plate} 历史${prof.count}次` + (filled.length ? `，已带出 ${filled.join('、')}` : '') +
      (varies.length ? `；${varies.join('/')}每次不同，请手选` : ''), 4200);
  } else toast(`${plate} 是新车牌`);
  if (!PLATES.find(p => p.plate_no === plate)) loadPlates();
}

// 钢厂→户头跟随：选了钢厂后，绑定该钢厂的户头只有一个就自动选上
function accountForMill(millId) {
  const accs = ARCH.accounts.filter(a => String(a.mill_id) === String(millId));
  return accs.length === 1 ? accs[0].id : null;
}

function nextStatus(code) {
  const usable = STATUS_LIST.filter(s => s.code !== 'voided');
  const i = usable.findIndex(s => s.code === code);
  return i >= 0 && i < usable.length - 1 ? usable[i + 1] : null;
}
const STATUS_DATE_LABEL = { reported: '接单日期', arrived: '到厂日期', settled: '结算日期', invoiced: '开票日期', closed: '结案日期' };
let TRIP_ROWS = {};
let ALL_TEMPLATES = [];
// 链条规则简写（在链条规则页"编辑名称/说明"里维护），界面上不再露 M1/M2 代号
function tplShort(templateId) {
  const t = ALL_TEMPLATES.find(x => x.id === templateId);
  return t ? (t.short_name || t.name) : '';
}
function tplName(templateId) { const t = ALL_TEMPLATES.find(x => x.id === templateId); return t ? t.name : ''; }
let SELECTED = new Set();

function statusLabel(code) {
  const s = STATUS_LIST.find(s => s.code === code);
  return s ? s.label : code;
}

// 从统计页点数字跳过来(/trips?...)：能放进顶部筛选框的就放进去，放不进去的(调货人/票源/链条/id列表/按哪个日期…)
// 记在 EXTRA_FILTER 里一起查询，并在列表上方显示"来自哪里 + 条件 + 清除"。
const TOOLBAR_MAP = { 'f-status': 'status', 'f-mill': 'mill_id', 'f-base': 'base_id', 'f-type': 'transport_type', 'f-from': 'date_from', 'f-to': 'date_to', 'f-q': 'q' };
let EXTRA_FILTER = {}, DRILL_FROM = '';
const EXTRA_LABEL = { status_in: '状态', exclude_voided: '不含作废', dispatcher_id: '调货人', invoice_source_id: '票源方', template_id: '链条',
  family: '路径', ids: '指定车次', date_field: '按', date_from: '从', date_to: '到', base_id: '供货厂', mill_id: '钢厂' };
function applyUrlFilters() {
  const u = new URLSearchParams(location.search);
  DRILL_FROM = u.get('from') || '';
  const byDateField = u.has('date_field');
  for (const [elId, key] of Object.entries(TOOLBAR_MAP)) {
    if (!u.has(key)) continue;
    if (byDateField && (key === 'date_from' || key === 'date_to')) continue;
    const el = document.getElementById(elId), v = u.get(key);
    if (el.tagName === 'SELECT' && ![...el.options].some(o => o.value === v)) continue;
    el.value = v; u.delete(key);
  }
  ['open', 'from'].forEach(k => u.delete(k));
  EXTRA_FILTER = Object.fromEntries(u.entries());
  renderDrillBanner();
}
function extraValueText(k, v) {
  const find = (list, id) => (list.find(x => String(x.id) === String(id)) || {}).name;
  if (v === 'none') return '未指定';
  if (k === 'dispatcher_id') return find(ARCH.dispatchers, v) || v;
  if (k === 'invoice_source_id') return find(ARCH.sources, v) || v;
  if (k === 'base_id') return find(ARCH.bases, v) || v;
  if (k === 'mill_id') return find(ARCH.mills, v) || v;
  if (k === 'template_id') return tplShort(+v) || v;
  if (k === 'family') return v === 'split' ? '配票/反向' : '带票带货';
  if (k === 'status_in') return v.split(',').map(statusLabel).join('/');
  if (k === 'date_field') return v === 'order_date' ? '接单日期' : '结算日期';
  if (k === 'ids') return v.split(',').length + ' 车';
  if (k === 'exclude_voided') return '是';
  return v;
}
function renderDrillBanner() {
  const box = document.getElementById('drill-banner');
  const keys = Object.keys(EXTRA_FILTER);
  if (!DRILL_FROM && !keys.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<span>${DRILL_FROM ? '来自：<strong>' + attrEsc(DRILL_FROM) + '</strong>' : '附加筛选'}</span>` +
    keys.map(k => `<span class="chip">${EXTRA_LABEL[k] || k}：${attrEsc(extraValueText(k, EXTRA_FILTER[k]))}</span>`).join('') +
    `<span class="sum" id="drill-sum"></span><span class="spacer"></span><a href="/trips">清除，看全部车次</a>`;
}

async function loadTrips() {
  const params = new URLSearchParams(EXTRA_FILTER);
  for (const [id, key] of Object.entries(TOOLBAR_MAP)) {
    const v = document.getElementById(id).value;
    if (v) params.set(key, v);
  }
  const [rows, brief] = await Promise.all([apiGet('/api/trips?' + params.toString()), apiGet('/api/trips_lanes_brief')]);
  LANE_BRIEF = brief;
  TRIP_ROWS = Object.fromEntries(rows.map(r => [r.id, r]));
  const sumEl = document.getElementById('drill-sum');  // 跳转过来时顺手显示合计，方便跟来源页的数字核对
  if (sumEl) sumEl.textContent = `共 ${rows.length} 车 · ${fmtNum(rows.reduce((a, r) => a + (r.settlement_tonnage_total || 0), 0))} 吨 · ¥${fmtMoney(rows.reduce((a, r) => a + (r.settlement_amount_total || 0), 0))}`;
  SELECTED = new Set([...SELECTED].filter(id => TRIP_ROWS[id]));
  updateBatchBar();
  const tbody = document.getElementById('trip-tbody');
  if (!rows.length) { tbody.innerHTML = '<tr><td colspan="13" class="empty-hint">暂无车次数据</td></tr>'; return; }
  const multiMill = (ARCH.mills || []).length > 1;
  document.getElementById('trip-table').classList.toggle('single-mill', !multiMill);
  tbody.innerHTML = rows.map(r => {
    const nx = r.status !== 'voided' ? nextStatus(r.status) : null;
    return `
    <tr onclick="openDrawer(${r.id})" class="${SELECTED.has(r.id) ? 'row-selected' : ''}" data-id="${r.id}">
      <td class="col-check" onclick="event.stopPropagation()"><input type="checkbox" class="row-check" data-id="${r.id}" ${SELECTED.has(r.id) ? 'checked' : ''} onchange="toggleSelect(${r.id}, this.checked)"></td>
      <td class="nowrap" data-col="date">${r.settlement_date || r.order_date || '-'}</td>
      <td class="nowrap" data-col="plate">${r.plate_no || '<span class="muted">未填</span>'}${r.transport_type === 'ship' ? '<span class="tag t-ship-tag">船</span>' : ''}</td>
      <td class="nowrap" data-col="chain" title="${attrEsc(r.template_name || '')}">${r.template_short || r.template_name || '-'}${r.mixed_material ? '<span class="tag">混料</span>' : ''}</td>
      <td class="num" data-col="tonnage">${fmtNum(r.settlement_tonnage_total)}</td>
      <td class="num" data-col="deduction">${fmtNum(r.deduction_total)}</td>
      <td class="num" data-col="amount">${fmtMoney(r.settlement_amount_total)}</td>
      <td class="nowrap" data-col="party">${partyLabel(r)}</td>
      <td class="col-mill nowrap" data-col="mill">${r.mill_name || '-'}</td>
      <td class="nowrap" data-col="status"><span class="pill ${SETTLED_OR_LATER.includes(r.status)?'green':(r.status==='voided'?'red':'blue')}">${statusLabel(r.status)}</span></td>
      <td class="progress-cell" data-col="progress">${progressBadges(r)}</td>
      <td class="col-ops nowrap">${nx ? `<button class="btn-sm btn-next" title="推进到${nx.label}（自动填${STATUS_DATE_LABEL[nx.code]}）" onclick="event.stopPropagation();advanceOne(${r.id})">→${nx.label}</button>` : ''}<button class="btn-sm" onclick="event.stopPropagation();openTrip(${r.id})">编辑</button></td>
    </tr>`;
  }).join('');
  applyColumnPrefs();
}

// ------- 列设置（B档 2026-10-09）：列表列太多，让人自己挑要看的，选完记住 -------
// 只做「显示/隐藏」，不动数据；列顺序固定，不做拖拽（拖拽要重排整个表，收益不如稳定的位置）。
const TRIP_COLS = [
  ['date', '日期'], ['plate', '车号/船号'], ['chain', '链条'], ['tonnage', '结算吨位'],
  ['deduction', '扣杂'], ['amount', '结算金额'], ['party', '调货人：供货厂'],
  ['mill', '钢厂'], ['status', '状态'], ['progress', '进度'],
];
const COL_HIDE_KEY = 'scrapmetal.trips.hiddenCols';
let HIDDEN_COLS = new Set();
try { HIDDEN_COLS = new Set(JSON.parse(localStorage.getItem(COL_HIDE_KEY) || '[]')); } catch (e) { HIDDEN_COLS = new Set(); }
function saveHiddenCols() {
  try { localStorage.setItem(COL_HIDE_KEY, JSON.stringify([...HIDDEN_COLS])); } catch (e) { /* 隐私模式存不了，忽略 */ }
}
function applyColumnPrefs() {
  const table = document.getElementById('trip-table');
  if (!table) return;
  table.querySelectorAll('[data-col]').forEach(el => el.classList.toggle('hidden-col', HIDDEN_COLS.has(el.dataset.col)));
}
function toggleColSet(ev) {
  if (ev) ev.stopPropagation();
  const menu = document.getElementById('colset-menu');
  if (!menu) return;
  if (!menu.hidden) { menu.hidden = true; return; }
  menu.innerHTML = TRIP_COLS.map(([k, label]) =>
    `<label><input type="checkbox" ${HIDDEN_COLS.has(k) ? '' : 'checked'} onchange="setColVisible('${k}', this.checked)"> ${label}</label>`).join('') +
    `<div class="colset-foot"><a href="javascript:void(0)" onclick="resetColSet()">全部显示</a>
      <a href="javascript:void(0)" onclick="toggleColSet()">收起</a></div>`;
  menu.hidden = false;
}
function setColVisible(key, on) {
  on ? HIDDEN_COLS.delete(key) : HIDDEN_COLS.add(key);
  saveHiddenCols();
  applyColumnPrefs();
}
function resetColSet() {
  HIDDEN_COLS.clear();
  saveHiddenCols();
  applyColumnPrefs();
  const menu = document.getElementById('colset-menu');
  if (menu) { menu.querySelectorAll('input[type=checkbox]').forEach(c => { c.checked = true; }); }
}
document.addEventListener('click', () => { const m = document.getElementById('colset-menu'); if (m) m.hidden = true; });

// 调货人：供货厂 合并显示。调货人自己车队的车(供货厂是调货人同名占位基地) → "林凯车队"；
// 没有调货人只显示供货厂；都没有显示"-"。
function partyLabel(r) {
  const base = (ARCH.bases || []).find(b => b.id === r.base_id);
  const alias = base && base.is_dispatcher_alias;
  const d = r.dispatcher_name;
  if (d && (alias || !r.base_id)) return `${d}<span class="muted">车队</span>`;
  if (d) return `${d}<span class="sep">：</span>${r.base_name || '-'}`;
  if (alias) return `${r.base_name}<span class="muted">车队</span>`;
  return r.base_name || '<span class="muted">-</span>';
}

// ------- 列表选择 + 批量推进 -------
function toggleSelect(id, on) {
  on ? SELECTED.add(id) : SELECTED.delete(id);
  const tr = document.querySelector(`#trip-tbody tr[data-id="${id}"]`);
  if (tr) tr.classList.toggle('row-selected', on);
  updateBatchBar();
}
function toggleSelectAll(on) {
  Object.keys(TRIP_ROWS).forEach(id => on ? SELECTED.add(+id) : SELECTED.delete(+id));
  document.querySelectorAll('.row-check').forEach(c => { c.checked = on; c.closest('tr').classList.toggle('row-selected', on); });
  updateBatchBar();
}
function updateBatchBar() {
  const bar = document.getElementById('batch-bar');
  if (!bar) return;
  const n = SELECTED.size;
  bar.hidden = n === 0;
  if (!n) return;
  const rows = [...SELECTED].map(id => TRIP_ROWS[id]).filter(Boolean);
  // 默认目标：选中车次里最多的那个状态的下一步
  const cnt = {};
  rows.forEach(r => { const nx = nextStatus(r.status); if (nx) cnt[nx.code] = (cnt[nx.code] || 0) + 1; });
  const best = Object.entries(cnt).sort((a, b) => b[1] - a[1])[0];
  const byStatus = {};
  rows.forEach(r => { byStatus[r.status] = (byStatus[r.status] || 0) + 1; });
  document.getElementById('batch-count').textContent =
    `已选 ${n} 车（` + Object.entries(byStatus).map(([k, v]) => `${statusLabel(k)}${v}`).join('、') + '）';
  const selEl = document.getElementById('batch-target');
  const keep = selEl.dataset.touched ? selEl.value : (best ? best[0] : 'settled');
  selEl.innerHTML = STATUS_LIST.filter(s => s.code !== 'voided').map(s => `<option value="${s.code}" ${s.code === keep ? 'selected' : ''}>${s.label}</option>`).join('');
}
function clearSelection() { SELECTED.clear(); document.getElementById('batch-target').dataset.touched = ''; loadTrips(); }
function batchAdvance() {
  const target = document.getElementById('batch-target').value;
  openAdvanceDialog([...SELECTED], target);
}
async function advanceOne(id) {
  const r = TRIP_ROWS[id];
  const nx = nextStatus(r.status);
  if (!nx) return;
  if (nx.code === 'settled' && !r.detail_count) return openAdvanceDialog([id], 'settled');
  await apiPost(`/api/trips/${id}/status`, { status: nx.code });
  toast(`${r.plate_no || '#' + id} → ${nx.label}`);
  loadTrips();
}

// 推进弹框：批量共用日期；推到"已结算"时逐车填结算吨位/金额(没有磅重明细的车)，
// 结算单号整批共用、逐车可覆盖。吨位列支持直接从Excel粘贴两列(吨位\t金额)多行。
function openAdvanceDialog(ids, target, onDone) {
  const rows = ids.map(id => TRIP_ROWS[id]).filter(Boolean);
  const isSettle = target === 'settled';
  const tgt = STATUS_LIST.find(s => s.code === target);
  const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const root = document.getElementById('dialog-root');
  root.innerHTML = `
  <div class="modal-overlay" onclick="if(event.target===this) closeAdvanceDialog()">
    <div class="modal adv-dialog">
      <div class="modal-header"><h2>推进 ${rows.length} 车到「${tgt.label}」</h2><button class="close" onclick="closeAdvanceDialog()">&times;</button></div>
      <div class="field-row">
        <div class="field"><label>${STATUS_DATE_LABEL[target]}（整批）</label><input type="date" id="adv-date" value="${today}"></div>
        ${isSettle ? `<div class="field"><label>结算单号（整批共用，可空）</label><input id="adv-docno" placeholder="同一张结算单的车次填同一个号"></div>` : ''}
      </div>
      <div class="hint">已有${STATUS_DATE_LABEL[target]}的车次保留原日期不覆盖。${isSettle ? '已有磅重明细的车次以明细为准，这里不改；可以从Excel复制「吨位、金额」两列，粘贴到第一行吨位框。' : ''}</div>
      <div class="table-scroll"><table class="adv-table"><thead><tr>
        <th>车号</th><th>接单</th><th>供货厂</th><th>当前</th>
        ${isSettle ? '<th class="num">结算吨位</th><th class="num">结算金额</th><th>单号(覆盖)</th>' : ''}
      </tr></thead><tbody>
      ${rows.map((r, i) => `<tr data-id="${r.id}">
        <td>${r.plate_no || '#' + r.id}</td><td>${r.order_date || '-'}</td><td>${r.base_name || '-'}</td><td>${statusLabel(r.status)}</td>
        ${isSettle ? (r.detail_count
          ? `<td class="num">${fmtNum(r.settlement_tonnage_total)}</td><td class="num">${fmtMoney(r.settlement_amount_total)}</td><td><input class="adv-docno" data-id="${r.id}" value="${attrEsc(r.settlement_doc_no || '')}"></td>`
          : `<td><input class="adv-ton" data-i="${i}" data-id="${r.id}" type="number" step="0.001" value="${r.settlement_tonnage_total || ''}"></td>
             <td><input class="adv-amt" data-i="${i}" data-id="${r.id}" type="number" step="0.01" value="${r.settlement_amount_total || ''}"></td>
             <td><input class="adv-docno" data-id="${r.id}" value="${attrEsc(r.settlement_doc_no || '')}"></td>`) : ''}
      </tr>`).join('')}
      </tbody></table></div>
      <div class="modal-actions" style="justify-content:flex-end;">
        <button onclick="closeAdvanceDialog()">取消</button>
        <button class="btn-primary" id="adv-go">确认推进</button>
      </div>
    </div>
  </div>`;
  document.querySelectorAll('.adv-ton').forEach(inp => inp.addEventListener('paste', e => {
    const text = (e.clipboardData || window.clipboardData).getData('text');
    if (!/[\n\t]/.test(text.trim())) return;
    e.preventDefault();
    const lines = text.trim().split(/\r?\n/);
    const tons = [...document.querySelectorAll('.adv-ton')];
    const start = tons.indexOf(inp);
    lines.forEach((ln, k) => {
      const t = tons[start + k]; if (!t) return;
      const [a, b] = ln.split('\t').map(x => x.replace(/[,，\s]/g, ''));
      t.value = a || '';
      if (b !== undefined) document.querySelector(`.adv-amt[data-id="${t.dataset.id}"]`).value = b;
    });
  }));
  document.getElementById('adv-go').onclick = async () => {
    const per = {};
    document.querySelectorAll('.adv-dialog tbody tr').forEach(tr => {
      const id = tr.dataset.id, q = c => tr.querySelector(c);
      per[id] = {
        settlement_tonnage_total: q('.adv-ton') ? q('.adv-ton').value : null,
        settlement_amount_total: q('.adv-amt') ? q('.adv-amt').value : null,
        settlement_doc_no: q('.adv-docno') ? q('.adv-docno').value : null,
      };
    });
    const common = { status_date: document.getElementById('adv-date').value };
    const dn = document.getElementById('adv-docno');
    if (dn && dn.value) common.settlement_doc_no = dn.value;
    const res = await apiPost('/api/trips/batch_status', { status: target, ids: rows.map(r => r.id), common, per_trip: per });
    closeAdvanceDialog();
    toast(`已推进 ${res.count} 车到「${tgt.label}」` + (res.quick_settled ? `，其中 ${res.quick_settled} 车录入了结算吨位/金额` : ''));
    SELECTED.clear();
    await loadTrips();
    if (onDone) onDone();
  };
  const first = root.querySelector('.adv-ton'); if (first) first.focus();
}
function closeAdvanceDialog() { document.getElementById('dialog-root').innerHTML = ''; }

// 结算单口径(2026-09-19用户明确)：状态推进到"已结算"就意味着钢厂结算单已经出了——业务上
// 正是拿到结算单才会把状态点成已结算。结算单号(settlement_doc_no)只是"有没有把单号录进系统"，
// 没录不等于单没出。所以显示"已出/未出"看的是 状态≥已结算 或 已录单号；单号没录的，鼠标悬停
// 有提示，方便以后想补录时能找出来，但不再显示成"未出"误导人。
const SETTLED_OR_LATER = ['settled', 'invoiced', 'closed'];
function settlementDocIssued(r) {
  return SETTLED_OR_LATER.includes(r.status) || !!r.settlement_doc_no;
}
// 车次进度节点化(Phase 3)：列表里用紧凑的小徽标一眼看出"到了什么阶段"，不用逐条点开详情。
// 过磅单/结算单是有/无二值；发票/打款是"完成数/总数"分数(只有split家族pass_through车次才有)。
let LANE_BRIEF = {};
function progressBadges(r) {
  const parts = [];
  // 2026-09-22：去掉"磅单"，进度直接从结算单开始（付款列也去掉了，钱款看这里的"款x/y"）
  parts.push(settlementDocIssued(r) ? '<span class="pill green" title="' + (r.settlement_doc_no ? '结算单号：' + attrEsc(r.settlement_doc_no) : '状态已到已结算，视为结算单已出') + '">结算单✓</span>' : '<span class="pill">结算单-</span>');
  const b = LANE_BRIEF[r.id];
  if (b && settlementDocIssued(r)) {
    const [id_, it] = b.inv, [pd, pt] = b.pay;
    parts.push(`<span class="pill ${id_ >= it ? 'green' : ''}" title="${b.inv_legacy ? '历史车次：按状态视为已开' : ''}">票${id_}/${it}</span>`);
    parts.push(`<span class="pill ${pd >= pt ? 'green' : ''}">款${pd}/${pt}</span>`);
    if (b.next) parts.push(`<span class="pill blue" title="下一步：${b.next.label} → ${attrEsc(b.next.to)}">${b.next.label}</span>`);
  }
  return parts.join(' ');
}

function exportCsv() {
  const rows = Array.from(document.querySelectorAll('#trip-table tr')).map(tr =>
    Array.from(tr.children).slice(1, 11).map(td => '"' + td.textContent.replace(/"/g, '""') + '"').join(',')
  );
  const blob = new Blob(["﻿" + rows.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '车次导出_' + new Date().toISOString().slice(0, 10) + '.csv';
  a.click();
}

function exportLedger() {
  // 直接用浏览器原生下载（后端返回带 Content-Disposition 的文件流），
  // 不走 apiGet（那个走 fetch+JSON，不适合下载二进制文件）。
  window.location.href = '/api/export/ledger';
}

// 新建：钢厂/户头/接单日期/发货地由后端按录入默认值补齐(/api/entry_defaults)，打开后光标直接在车牌框
async function createTrip() {
  const { id } = await apiPost('/api/trips', { mode_code: 'M1' });
  await loadTrips();
  await openTrip(id);
  focusPlate();
}
function focusPlate() { const p = document.getElementById('f_plate_no'); if (p) p.focus(); }

async function openTrip(id) {
  closeDrawer();
  currentTripId = id;
  const data = await apiGet('/api/trips/' + id);
  renderModal(data);
}

function closeModal() {
  document.getElementById('modal-root').innerHTML = '';
  currentTripId = null;
}

// 车次详情：三个信息分组默认只开第一组（核心信息 7 项），单据/司机两组收起。
// 一个按钮在"全部展开"和"只留核心"之间来回切，按钮文字跟着变，省得用户猜现在是什么状态。
function toggleInfoGroups(btn) {
  const groups = [...document.querySelectorAll('#modal-root .info-group')];
  if (!groups.length) return;
  const anyClosed = groups.some(g => !g.open);
  groups.forEach(g => { g.open = anyClosed; });
  if (btn) btn.textContent = anyClosed ? '只留核心字段' : '展开全部字段';
}

// ------- 明细抽屉：轻量查看一车多料的磅重明细，无需打开完整详情面板 -------
function closeDrawer() {
  document.getElementById('drawer-root').innerHTML = '';
}

async function openDrawer(id) {
  const data = await apiGet('/api/trips/' + id);
  renderDrawer(data);
}

function renderDrawer(data) {
  const t = data.trip, details = data.details || [];
  const root = document.getElementById('drawer-root');
  const rows = details.length ? details.map(d => {
    return `<tr>
      <td>${d.scrap_type_name || '-'}</td>
      <td class="num">${fmtNum(d.gross_weight)}</td>
      <td class="num">${fmtNum(d.tare_weight)}</td>
      <td class="num">${fmtNum(d.net_weight)}</td>
      <td class="num">${fmtNum(d.deduction)}</td>
      <td class="num">${fmtNum(d.settlement_tonnage)}</td>
      <td class="num">${fmtNum(d.unit_price)}</td>
      <td class="num">${fmtNum(d.cutting_fee)}</td>
      <td class="num">${fmtMoney(d.amount)}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="9" class="empty-hint">暂无磅重明细</td></tr>';

  root.innerHTML = `
  <div class="drawer-overlay" onclick="if(event.target===this) closeDrawer()">
    <div class="drawer">
      <div class="modal-header">
        <h2>车次明细 #${t.id} ${t.mixed_material ? '<span class="tag">混料</span>' : ''}</h2>
        <button class="close" onclick="closeDrawer()">&times;</button>
      </div>
      <div class="info-grid" style="grid-template-columns:repeat(2,1fr);">
        <div><span class="k">日期</span>${t.settlement_date || t.order_date || '-'}</div>
        <div><span class="k">车号/船号</span>${t.plate_no || '-'} (${t.transport_type==='ship'?'船':'车'})</div>
        <div><span class="k">供货厂</span>${t.base_id ? drill(t.base_name || '-', { base_id: t.base_id, exclude_voided: 1 }, '车次明细·供货厂 ' + (t.base_name || '')) : '-'}</div>
        <div><span class="k">调货人</span>${t.dispatcher_id ? drill(t.dispatcher_name || '-', { dispatcher_id: t.dispatcher_id, exclude_voided: 1 }, '车次明细·调货人 ' + (t.dispatcher_name || '')) : '-'}</div>
        <div><span class="k">钢厂</span>${t.mill_name || '-'}</div>
        <div><span class="k">户头</span>${t.account_name || '-'}</div>
        <div><span class="k">状态</span><span class="pill blue">${statusLabel(t.status)}</span></div>
        <div><span class="k">链条规则</span>${tplShort(t.template_id) || '-'}</div>
      </div>

      <div class="panel-title" style="margin-top:14px;">磅重与结算明细（一车多料按货型分行）</div>
      ${(!t.plate_no && !details.length) ? `<div class="empty-hint" style="text-align:left;padding:8px 2px;">这辆是「+ 新建车次」点了之后没填完的空车（没车号、没明细），不影响统计——不想留就点下面的<b>删除</b>，30 天内可从「系统 → 回收站」找回。</div>` : ''}
      <div class="detail-table-wrap"><table><thead><tr>
        <th>货型</th><th>毛重</th><th>皮重</th><th>净重</th><th>扣杂</th><th>结算吨数</th><th>单价</th><th>切割费</th><th>金额</th>
      </tr></thead><tbody>${rows}</tbody></table></div>

      <div class="field-row" style="margin-top:10px;">
        <div><span class="k">合计结算吨位</span> ${fmtNum(t.settlement_tonnage_total)}</div>
        <div><span class="k">合计结算金额</span> ${fmtMoney(t.settlement_amount_total)}</div>
      </div>

      <div style="margin-top:18px;display:flex;gap:8px;">
        <button class="btn-primary" onclick="openTrip(${t.id})">详情 →（编辑参数/查看利润分项）</button>
        <span class="spacer"></span>
        <button class="btn-danger" onclick="voidTripById(${t.id})" title="不想统计它、但想留个记录时用这个">作废</button>
        <button class="btn-danger" onclick="deleteTripById(${t.id})" title="删除后进回收站，30 天内可在「系统 → 回收站」恢复。点错新建的车直接删这一条。">删除</button>
        <button onclick="closeDrawer()">关闭</button>
      </div>
    </div>
  </div>`;
}

function sel(list, valueKey, labelKey, selected, extraEmpty) {
  let html = extraEmpty ? `<option value="">${extraEmpty}</option>` : '';
  return html + optionsHtml(list, valueKey, labelKey, selected);
}

// 两票三款通用化(2026-09-18)：不再是硬编码的3个pay_type，改成按 data.doc_panels 里
// doc_types含"payment"的节点动态生成——node_key才是权威标识，label只用于展示。
function paymentNodeLabel(doc_panels, nodeKey) {
  const p = (doc_panels || []).find(p => p.node_key === nodeKey);
  return p ? p.label : (nodeKey || '-');
}
function sumByNodeKey(payments, nodeKey) {
  return payments.filter(p => p.node_key === nodeKey).reduce((s, p) => s + (Number(p.amount) || 0), 0);
}
function reconRow(label, baseline, paidAmt) {
  const hasBaseline = baseline !== null && baseline !== undefined && baseline !== '';
  const diff = hasBaseline ? (paidAmt - Number(baseline)) : null;
  return `<tr><td>${label}</td><td class="num">${hasBaseline ? fmtMoney(baseline) : '-'}</td><td class="num">${fmtMoney(paidAmt)}</td><td class="num" style="${diff && Math.abs(diff)>0.005?'color:#ad4a24;':''}">${diff===null?'-':(diff>=0?'+':'')+fmtMoney(diff)}</td></tr>`;
}

function renderModal(data) {
  const t = data.trip, fin = data.finance, details = data.details;
  window.CUR_TRIP = t;
  const docPanels = data.doc_panels || [];
  // 两票三款相关区域的显示条件：继续绑定"票源方settlement_type=pass_through"（与现行为一致，
  // 用户2026-09-18明确确认），不是所有split家族车次都显示，只有选了pass_through票源(如益周)的才显示。
  const isPassThrough = t.invoice_source_settlement_type === 'pass_through';
  const invoiceNodes = docPanels.filter(p => p.doc_types.includes('invoice'));
  const paymentNodes = docPanels.filter(p => p.doc_types.includes('payment'));
  const statusIdx = STATUS_LIST.findIndex(s => s.code === t.status);

  const root = document.getElementById('modal-root');
  root.innerHTML = `
  <div class="modal-overlay" onclick="if(event.target===this) closeModal()">
    <div class="modal">
      <div class="modal-header">
        <h2>车次详情</h2>
        <button class="close" onclick="closeModal()">&times;</button>
      </div>
      <div class="modal-actions">
        <strong>车次 #${t.id}</strong>
        <span class="pill blue">${statusLabel(t.status)}</span>
        <span class="pill" title="${attrEsc(tplName(t.template_id))}">${tplShort(t.template_id)}</span>
        <button onclick="stepStatus(-1)">← 回退</button>
        <button onclick="stepStatus(1)">推进 →</button>
        <span class="spacer"></span>
        <button class="btn-sm" id="info-group-toggle" onclick="toggleInfoGroups(this)" title="默认只展开「核心信息」；需要改单据/司机信息时点这里一次全开">展开全部字段</button>
        <button onclick="cloneTrip()" title="同供货厂/调货人/票源，日期今天，换车牌">再来一车</button>
        <button class="btn-danger" onclick="voidTrip()">作废</button>
        <button class="btn-danger" onclick="deleteTrip()" title="删除后进回收站，30天内可在「系统」页恢复">删除</button>
      </div>
      <div class="status-pipeline">
        ${STATUS_LIST.map(s => `<span class="tab ${s.code===t.status?'active':''}" onclick="setStatus('${s.code}')">${s.label}</span>`).join('')}
      </div>
      <div id="trip-lanes" class="lanes"></div>

      <div class="info-groups">
        <details class="info-group" open>
          <summary><span class="ig-title">核心信息</span><span class="ig-hint">录一车要填的就这几项</span><span class="ig-count">7 项</span></summary>
          <div class="info-grid">
            <div><span class="k">接单日期</span><input type="date" id="f_order_date" value="${t.order_date||''}"></div>
            <div><span class="k">车号/船号</span><input id="f_plate_no" list="plate-datalist" autocomplete="off" value="${attrEsc(t.plate_no||'')}" placeholder="输入车牌，自动带出历史信息"></div>
            <div><span class="k">${term('链条规则','chain')}</span><select id="f_template_id">${sel(ARCH.templates,'id','name',t.template_id,'-')}</select></div>
            <div><span class="k">供货厂</span><select id="f_base_id">${sel(ARCH.bases,'id','name',t.base_id,'-')}</select></div>
            <div><span class="k">钢厂</span><select id="f_mill_id">${sel(ARCH.mills,'id','name',t.mill_id,'-')}</select></div>
            <div><span class="k">调货人</span><select id="f_dispatcher_id">${sel(ARCH.dispatchers,'id','name',t.dispatcher_id,'-')}</select></div>
            <div><span class="k">${term('票源方','quota')}</span><select id="f_invoice_source_id">${sel(ARCH.sources,'id','name',t.invoice_source_id,'-')}</select></div>
          </div>
        </details>

        <details class="info-group">
          <summary><span class="ig-title">单据与日期</span><span class="ig-hint">结算单/到厂/开票/结案，推进状态时会自动填</span><span class="ig-count">9 项</span></summary>
          <div class="info-grid">
            <div><span class="k">类型</span><select id="f_transport_type">
                <option value="truck" ${t.transport_type==='truck'?'selected':''}>车</option>
                <option value="ship" ${t.transport_type==='ship'?'selected':''}>船</option></select></div>
            <div><span class="k">${term('户头','account')}</span><select id="f_account_id">${sel(ARCH.accounts,'id','name',t.account_id,'-')}</select></div>
            <div><span class="k">开票路径</span><select id="f_invoice_path" title="空=按链条规则和票源自动判断；供货厂有票额也可以选我们配票"><option value="">自动判断</option>${Object.entries(LANE_META ? LANE_META.paths : {}).map(([k, v]) => `<option value="${k}" ${t.invoice_path===k?'selected':''}>${v}</option>`).join('')}</select></div>
            <div><span class="k">结算单号</span><input id="f_settlement_doc_no" value="${t.settlement_doc_no||''}"></div>
            <div><span class="k">到厂日期</span><input type="date" id="f_arrived_date" value="${t.arrived_date||''}"></div>
            <div><span class="k">结算日期</span><input type="date" id="f_settlement_date" value="${t.settlement_date||''}"></div>
            <div><span class="k">开票日期</span><input type="date" id="f_invoiced_date" value="${t.invoiced_date||''}"></div>
            <div><span class="k">结案日期</span><input type="date" id="f_closed_date" value="${t.closed_date||''}"></div>
            <div><span class="k">${term('混料','mixed')}</span><label style="display:inline-block;"><input type="checkbox" id="f_mixed_material" ${t.mixed_material?'checked':''}> 无法逐货型拆分，以钢厂结算单为准</label></div>
          </div>
        </details>

        <details class="info-group">
          <summary><span class="ig-title">司机 · 物流 · 备注</span><span class="ig-hint">只在需要时填</span><span class="ig-count">5 项</span></summary>
          <div class="info-grid">
            <div><span class="k">司机</span><input id="f_driver_name" value="${t.driver_name||''}" placeholder="姓名"></div>
            <div><span class="k">司机电话</span><input id="f_driver_phone" value="${t.driver_phone||''}"></div>
            <div><span class="k">发货地</span><input id="f_departure_place" value="${t.departure_place||''}"></div>
            <div><span class="k">预计到厂</span><input id="f_expected_arrival" value="${t.expected_arrival||''}" placeholder="日期/时间"></div>
            <div style="grid-column:1/-1;"><span class="k">备注</span><input id="f_remarks" style="width:100%;" value="${t.remarks||''}" placeholder="如：仅支持国六标准入场"></div>
          </div>
        </details>
      </div>

      <div class="split-panels">
        <div>
          <div class="panel-title">磅重与结算（按货型，一车可多料）</div>
          <div class="detail-table-wrap"><table id="detail-table"><thead><tr>
            <th>货型</th><th>毛重</th><th>皮重</th><th>净重</th><th>扣杂</th><th>结算吨数</th><th>单价</th><th>切割费</th><th>金额</th><th></th>
          </tr></thead><tbody id="detail-tbody"></tbody></table></div>
          <button class="btn-sm" onclick="addDetailRow()">+ 增加货型行</button>
          <div class="field-row" style="margin-top:12px;">
            <div class="field"><label>付加工厂金额</label><input type="number" step="0.01" id="f_pay_to_base_amount" value="${t.pay_to_base_amount||0}"></div>
            <div class="field"><label>其他成本</label><input type="number" step="0.01" id="f_other_cost" value="${t.other_cost||0}"></div>
          </div>
          <button class="btn-primary" onclick="saveDetails()">保存磅重/明细</button>
          <span style="color:var(--gray-400);font-size:12px;margin-left:8px;">保存后自动刷新右侧利润试算；净重/结算吨数/金额留空则按 毛重-皮重、净重-扣杂、吨数×单价-切割费 自动计算</span>
        </div>

        <div>
          <div class="panel-title">利润分项（实时试算）${fin.is_locked ? '<span class="pill green">已固化</span>' : ''}</div>
          <table class="profit-table" id="profit-table"></table>
          <div class="rate-edit">
            <div class="rate-edit-head">本车费率 <span class="muted">默认按"费率与关系"，这一车不一样就直接改，只影响这一车；清空=恢复默认</span></div>
            <table class="rate-table" id="rate-edit-table"></table>
          </div>
          <div class="lock-row">
            ${fin.is_locked
              ? '<span class="muted">已固化：档案费率以后再变，这一车数字不跟着变；在上面改本车费率会按新值重新固化。</span>'
              : '<button class="btn-primary" onclick="lockFinance()">固化快照</button><span class="muted">固化后，档案费率以后再变，这一车数字不跟着变</span>'}
          </div>
        </div>
      </div>

      <div class="split-panels" style="margin-top:16px;">
        <div>
          <div class="panel-title">付款记录</div>
          <table id="payment-table"><thead><tr>${isPassThrough ? '<th>类型</th>' : ''}<th>付款方</th><th>收款方</th><th class="num">金额</th><th>日期</th><th>状态</th></tr></thead>
          <tbody>${data.payments.map(p=>`<tr>${isPassThrough ? `<td>${paymentNodeLabel(docPanels, p.node_key)}</td>` : ''}<td>${p.payer||''}</td><td>${p.payee||''}</td><td class="num">${fmtMoney(p.amount)}</td><td>${p.paid_at||''}</td><td>${p.status}</td></tr>`).join('') || `<tr><td colspan="${isPassThrough?6:5}" class="empty-hint">暂无付款记录</td></tr>`}</tbody></table>
          ${isPassThrough && paymentNodes.length ? `<div class="field-row" style="margin-top:8px;">
            <select id="pay_node_key">
              <option value="">款项类型（可选）</option>
              ${paymentNodes.map(p => `<option value="${p.node_key}">${p.label}</option>`).join('')}
            </select>
          </div>` : ''}
          <div class="field-row" style="margin-top:8px;">
            <input id="pay_payer" placeholder="付款方">
            <input id="pay_payee" placeholder="收款方">
          </div>
          <div class="field-row" style="margin-top:8px;">
            <input id="pay_amount" type="number" step="0.01" placeholder="金额">
            <input id="pay_date" type="date">
          </div>
          <button class="btn-sm" style="margin-top:6px;" onclick="addPayment()">+ 记一笔付款</button>
        </div>
        <div>
          <div class="panel-title">凭证附件（信息流）</div>
          <table><thead><tr><th>类别</th><th>文件</th><th></th></tr></thead>
          <tbody>${data.attachments.map(a=>attRow(a)).join('') || '<tr><td colspan="3" class="empty-hint">暂无附件，过磅单/结算单可直接上传</td></tr>'}</tbody></table>
          <div class="field-row" style="margin-top:8px;">
            <select id="att_category">
              <option value="weighing_slip">过磅单</option><option value="settlement_doc">结算单</option>
              <option value="payment_screenshot">付款截图</option><option value="cargo_photo">货物照</option>
              <option value="truck_head">车头照</option><option value="truck_tail">车尾照</option>
              <option value="driver_license">驾驶证</option><option value="vehicle_license">行驶证</option>
              <option value="id_card">身份证</option><option value="env_list">环保清单</option>
            </select>
            <input id="att_file" type="file" accept="image/*,application/pdf" style="flex:1;">
          </div>
          <button class="btn-sm" style="margin-top:6px;" onclick="uploadAttachment()">↑ 上传文件</button>
          <details style="margin-top:6px;"><summary>手工填路径（兜底，非上传）</summary>
            <div class="field-row" style="margin-top:6px;">
              <input id="att_path" placeholder="文件路径/文件名（仅记录，不上传）">
            </div>
            <button class="btn-sm" onclick="addAttachment()">+ 记录路径</button>
          </details>
        </div>
      </div>

      ${isPassThrough && docPanels.length ? `
      <div class="card" style="margin-top:16px;">
        <div class="panel-title">发票/打款核对（经过我们的票源方专用：${t.invoice_source_name || ''}，按结算流程模板自动列出各环节）</div>
        ${invoiceNodes.length ? `<div class="field-row" style="flex-wrap:wrap;">
          ${invoiceNodes.map(n => `
            <div class="field"><label>${n.label} 发票号</label><input class="docno-input" data-node="${n.node_key}" value="${n.doc_no||''}"></div>
            <div class="field"><label>${n.label} 实际金额（基准${fmtMoney(n.baseline)}）</label><input class="docamt-input" data-node="${n.node_key}" type="number" step="0.01" value="${n.actual_amount??''}"></div>
            <div class="field" style="justify-content:flex-end;"><label style="display:flex;align-items:center;gap:4px;"><input type="checkbox" class="docstatus-input" data-node="${n.node_key}" ${n.doc_status==='in_stock'?'checked':''}> ${n.label}票库存中（待恢复开票）</label></div>
          `).join('')}
        </div>` : ''}
        ${paymentNodes.length ? `<table class="profit-table" style="margin-top:8px;">
          <thead><tr><th>核对项</th><th class="num">应收应付基准</th><th class="num">实际打款合计</th><th class="num">差额(打款-基准)</th></tr></thead>
          <tbody>
            ${paymentNodes.map(n => reconRow(
              n.label + (n.role_type === 'ticket_source' && n.node_key === 'downstream_payment' ? '（无对应发票，仅追踪打款）' : ''),
              n.node_key === 'downstream_payment' ? null : n.baseline,
              sumByNodeKey(data.payments, n.node_key)
            )).join('')}
          </tbody>
        </table>` : ''}
        <div style="color:var(--gray-400);font-size:11.5px;margin-top:4px;">发票金额与实际打款金额可能不一致，不互相推算，分开录入；"应收应付基准"来自结算流程自动计算，仅供核对参考。</div>
      </div>` : ''}

      ${t.template_id ? `
      <div class="card" style="margin-top:16px;">
        <div class="toolbar" id="trip-flow-path-btns">
          <div class="panel-title" style="margin:0;">这笔车次的链条规则关系图（主体名字/金额都是这笔车次的真实数据，跟上面利润分项对得上）</div>
          <div class="spacer"></div>
          ${['all', 'invoice', 'payment'].map(p => `
            <button class="btn-sm ${(tripFlowPath === p) ? 'btn-primary' : ''}" data-path="${p}" onclick="switchTripFlowPath('${p}')">
              ${p === 'all' ? '全部' : p === 'invoice' ? '票据路径(结算单/台账/发票)' : '财务路径(打款)'}
            </button>`).join('')}
        </div>
        <div id="trip-flow-graph" class="chart-card"></div>
      </div>` : ''}

      <details class="card" style="margin-top:16px;">
        <summary class="panel-title" style="cursor:pointer;margin:0;">修改记录</summary>
        <div id="trip-history" style="margin-top:8px;"></div>
      </details>

    </div>
  </div>`;

  renderDetailRows(details);
  renderProfitTable(fin);
  bindInfoAutoSave(docPanels);
  if (t.template_id) renderTripFlowGraph(t.id);
  renderTripLanes(t.id);
  renderTripHistory(t.id);
}

// ------- 三条线（B阶段）：货物线 / 开票线 / 回款线，每个环节显示 从→到、票额、已登记，批次在"回款追踪"页登记 -------
let LANE_META = null;
const HOP_STATE_TXT = { done: '✓ 已登记', partial: '部分登记', todo: '待办', blocked: '未开始', legacy: '按状态视为已完成' };
async function renderTripLanes(tripId) {
  const box = document.getElementById('trip-lanes');
  if (!box) return;
  const L = await apiGet(`/api/trips/${tripId}/lanes`);
  if (currentTripId !== tripId) return;
  const hopHtml = h => `<span class="hop ${h.state}" title="${attrEsc(h.from)} → ${attrEsc(h.to)}">
      <span class="h1">${h.label}</span><span class="h2">${attrEsc(h.from)} → ${attrEsc(h.to)}</span>
      <span class="h2">${HOP_STATE_TXT[h.state]}${h.expected != null ? ` · ${h.allocated ? '¥' + fmtMoney(h.allocated) + ' / ' : ''}¥${fmtMoney(h.expected)}` : ''}</span></span>`;
  const arrow = '<span class="lane-arrow">→</span>';
  const g = L.goods;
  box.innerHTML = `
    <div class="lane"><span class="lane-name">货物线</span>${g.nodes.map(n => `<span class="node-chip">${attrEsc(n)}</span>`).join(arrow)}
      ${g.checks.map(c => `<span class="pill ${c.done ? 'green' : ''}">${c.done ? '✓' : '·'} ${c.label}${c.doc_no ? '：' + attrEsc(c.doc_no) : ''}</span>`).join(' ')}</div>
    <div class="lane"><span class="lane-name">开票线</span>${L.invoice.map(hopHtml).join(arrow)}
      <span class="muted">${L.path_label}${L.path_is_default ? '（自动判断）' : ''}</span></div>
    <div class="lane"><span class="lane-name">回款线</span>${L.payment.map(hopHtml).join(arrow)}
      <a class="muted" href="/tracking">去登记批次 ›</a></div>`;
}

let tripFlowPath = 'all';
// 2026-09-19修复：原来切换视图是整个弹窗重新渲染一遍(openTrip)，弹窗会滚回顶部、正在填的
// 输入框被重建，用户体感就是"点一下就刷新"。改成只换按钮高亮 + 重画那张图。
async function switchTripFlowPath(path) {
  tripFlowPath = path;
  const btnBox = document.getElementById('trip-flow-path-btns');
  if (btnBox) {
    btnBox.querySelectorAll('button[data-path]').forEach(b => {
      b.classList.toggle('btn-primary', b.dataset.path === path);
    });
  }
  await renderTripFlowGraph(currentTripId);
}
async function renderTripFlowGraph(tripId) {
  const box = document.getElementById('trip-flow-graph');
  if (!box) return;
  // 拉数据期间锁住高度，避免容器先塌成0再撑开，弹窗里的内容跟着上下跳。
  const lock = box.offsetHeight;
  if (lock) box.style.minHeight = lock + 'px';
  try {
    const graph = await apiGet(`/api/trips/${tripId}/flow_graph?path=${tripFlowPath}`);
    renderChainGraph('trip-flow-graph', graph);
  } catch (e) {
    box.innerHTML = `<div class="empty-hint">链条规则关系图加载失败：${e.message}</div>`;
  } finally {
    box.style.minHeight = '';
  }
}

function renderDetailRows(details) {
  const tbody = document.getElementById('detail-tbody');
  tbody.innerHTML = '';
  (details.length ? details : [{}]).forEach(d => tbody.appendChild(detailRow(d)));
}
function detailRow(d = {}) {
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td><select class="d-scrap">${sel(ARCH.scrapTypes,'id','name',d.scrap_type_id,'请选择')}</select></td>
    <td><input class="d-gross" type="number" step="0.01" value="${d.gross_weight ?? ''}" style="width:70px;"></td>
    <td><input class="d-tare" type="number" step="0.01" value="${d.tare_weight ?? ''}" style="width:70px;"></td>
    <td><input class="d-net" type="number" step="0.01" value="${d.net_weight ?? ''}" style="width:70px;"></td>
    <td><input class="d-ded" type="number" step="0.01" value="${d.deduction ?? ''}" style="width:60px;"></td>
    <td><input class="d-ton" type="number" step="0.01" value="${d.settlement_tonnage ?? ''}" style="width:70px;"></td>
    <td><input class="d-price" type="number" step="0.01" value="${d.unit_price ?? ''}" style="width:70px;"></td>
    <td><input class="d-cut" type="number" step="0.01" value="${d.cutting_fee ?? ''}" style="width:60px;"></td>
    <td><input class="d-amt" type="number" step="0.01" value="${d.amount ?? ''}" style="width:90px;"></td>
    <td><button class="btn-sm btn-danger" onclick="this.closest('tr').remove()">删</button></td>
  `;
  return tr;
}
function addDetailRow() { document.getElementById('detail-tbody').appendChild(detailRow()); }

function collectDetails() {
  return Array.from(document.querySelectorAll('#detail-tbody tr')).map(tr => ({
    scrap_type_id: tr.querySelector('.d-scrap').value || null,
    gross_weight: tr.querySelector('.d-gross').value || null,
    tare_weight: tr.querySelector('.d-tare').value || null,
    net_weight: tr.querySelector('.d-net').value || null,
    deduction: tr.querySelector('.d-ded').value || 0,
    settlement_tonnage: tr.querySelector('.d-ton').value || null,
    unit_price: tr.querySelector('.d-price').value || 0,
    cutting_fee: tr.querySelector('.d-cut').value || 0,
    amount: tr.querySelector('.d-amt').value || null,
  })).filter(r => r.scrap_type_id);
}

async function saveDetails() {
  await apiPut(`/api/trips/${currentTripId}/details`, { details: collectDetails() });
  await apiPut(`/api/trips/${currentTripId}`, {
    pay_to_base_amount: document.getElementById('f_pay_to_base_amount').value || 0,
    other_cost: document.getElementById('f_other_cost').value || 0,
  });
  await refreshPreview();
  await loadTrips();
}

function profitRow(label, formula, value, extra = '') {
  return `<tr><td>${label}</td><td class="formula">${formula}</td><td class="num">${value}</td></tr>`;
}

function renderProfitTable(fin) {
  const t = document.getElementById('profit-table');
  let rows = '';
  rows += profitRow('结算吨数', '-', fmtNum(fin.tonnage));
  rows += profitRow('结算金额(含税)', '-', fmtMoney(fin.settlement_amount));
  if (fin.calc_type === 'split') {
    // 路径1(配票)/路径3(反向)差价模型：给客户付 - 前端实付 = 个人垫资；最终利润 = 量补 - 个人垫资 - 提成 - 其他成本（切割费客户承担，不扣）
    rows += profitRow('给客户付', `结算金额 × (1 - 对外点位${fmtNum(fin.rate_out)}%)`, fmtMoney(fin.give_customer));
    const afUnit = fin.tonnage ? (fin.advance_fee_raw || 0) / fin.tonnage : 0;
    rows += profitRow('垫资费', `${fmtNum(fin.tonnage)}吨 × ${fmtNum(afUnit)}元/吨` + (afUnit === 0 && (window.CUR_TRIP && CUR_TRIP.mode_code === 'M5') ? '（本期反向票点已含垫资费，按"费率与关系"里户头+反向的生效档）' : '（按"费率与关系"里的生效档）'), fmtMoney(fin.advance_fee_raw));
    rows += profitRow('前端(开票金额)', '结算金额 - 垫资费', fmtMoney(fin.front_invoice_amount));
    rows += profitRow('前端实付', `前端 × 成本系数${fin.rate_in!=null ? `(1-${fin.rate_in}%)` : ''}`, fmtMoney(fin.front_actual_paid));
    rows += profitRow('个人垫资', '给客户付 - 前端实付', fmtMoney(fin.personal_advance));
    rows += profitRow('量补', `${fmtNum(fin.reward_unit_price)}元/吨 × ${fmtNum(fin.tonnage)}吨 (周期累计${fmtNum(fin.period_cumulative_tonnage)}吨, ${fin.period_start}~${fin.period_end})`, fmtMoney(fin.reward_profit));
    if (fin.dispatcher_commission_cost) rows += profitRow('调货人提成', `-${fmtNum(fin.dispatcher_commission_rate)}元/吨 × ${fmtNum(fin.tonnage)}吨`, '-' + fmtMoney(fin.dispatcher_commission_cost));
    if (fin.cutting_fee) rows += profitRow('切割费', '客户承担：已从结算金额里扣掉，不再计入成本', `<span class="muted">(${fmtMoney(fin.cutting_fee)})</span>`);
    rows += profitRow('买票成本', '手工录入', '-' + fmtMoney(fin.ticket_cost));
  } else {
    // 路径2(带票带货/户头通道)服务费模型：不关心票源成本，只算服务费净额
    rows += profitRow('毛服务费', `${fmtNum(fin.tonnage)}吨 × ${fmtNum(fin.service_fee_value)}元/吨`, fmtMoney(fin.service_fee_income));
    rows += profitRow('垫资成本', `${fmtNum(fin.tonnage)}吨 × ${fmtNum(fin.advance_fee_rate)}元/吨`, '-' + fmtMoney(fin.advance_fee_cost));
    if (fin.dispatcher_commission_cost) rows += profitRow('调货人提成', `-${fmtNum(fin.dispatcher_commission_rate)}元/吨 × ${fmtNum(fin.tonnage)}吨`, '-' + fmtMoney(fin.dispatcher_commission_cost));
    rows += profitRow('量补', `${fmtNum(fin.reward_unit_price)}元/吨 × ${fmtNum(fin.tonnage)}吨 (周期累计${fmtNum(fin.period_cumulative_tonnage)}吨)`, fmtMoney(fin.reward_profit));
    if (fin.cutting_fee) rows += profitRow('切割费', '客户承担：已从结算金额里扣掉，不再计入成本', `<span class="muted">(${fmtMoney(fin.cutting_fee)})</span>`);
  }
  const manualOtherCost = fin.other_cost || 0;  // 2026-09-22 起切割费客户承担，其他成本只含手工录入
  if (Math.abs(manualOtherCost) > 0.005) rows += profitRow('其他成本', '手工录入', '-' + fmtMoney(manualOtherCost));
  rows += `<tr class="total"><td>净利润</td><td></td><td class="num">${fmtMoney(fin.net_profit)}</td></tr>`;
  // 票差留存利润(2026-09-17起)：经过我们的票源方(如益周)才有，专门单独留存，
  // 不计入上面的"净利润"、也不进入任何汇总统计，所以刻意放在总计行之后、单独一块展示，
  // 而不是并入上面的加总公式里。
  if (fin.invoice_diff_profit) {
    rows += `<tr class="reserved-profit"><td colspan="3" style="padding-top:10px;border-top:1px dashed #ccc;color:#ad4a24;font-size:12.5px;">
      <b>票差留存利润（专项留存，不计入净利润）：${fmtMoney(fin.invoice_diff_profit)}</b><br>
      <span style="color:#8a6a55;">= 前端(${fmtMoney(fin.front_invoice_amount)}) × (转出点位 − 进项点位) / 100，
      经过我们的票源方(如益周：12.8%进、13.5%出)才有，单独留存，不加进上面的净利润，
      也不计入调货员业绩/票源方利润分布等任何汇总</span>
    </td></tr>`;
  }
  t.innerHTML = rows;
  renderRateEditor(fin);
}

// 本车费率(2026-09-21)：每个"按费率算"的节点一行——默认值来自费率与关系/模板/量补阶梯/票源点位，
// 输入框里填了就是这一车的覆盖值(存 trips.finance_overrides)，清空恢复默认。
function currentOverrides() { return {}; }
const RATE_LABEL = { give_customer: '对外结算点位（给客户付）', advance_fee: '垫资费', front_actual_paid: '票源成本点位（前端实付）',
  dispatcher_commission: '调货人提成', reward_profit: '量补单价', service_fee_income: '服务费' };
function renderRateEditor(fin) {
  const box = document.getElementById('rate-edit-table');
  if (!box) return;
  const items = fin.rate_items || [];
  if (!items.length) { box.innerHTML = '<tr><td class="empty-hint">这一车还没有链条规则，没有可改的费率</td></tr>'; return; }
  box.innerHTML = `<thead><tr><th>项目</th><th class="num">默认</th><th>本车</th><th></th></tr></thead><tbody>` +
    items.map(i => `<tr class="${i.overridden ? 'ov' : ''}">
      <td>${RATE_LABEL[i.node_key] || i.label}<div class="muted">默认来源：${i.source}</div></td>
      <td class="num">${fmtNum(i.default, 2)} ${i.unit}</td>
      <td class="nowrap"><input type="number" step="0.01" class="rate-in" data-key="${i.key}" value="${i.overridden ? i.value : ''}" placeholder="${fmtNum(i.default, 2)}"> ${i.unit}</td>
      <td class="nowrap">${i.overridden ? `<span class="pill blue">已改</span> <a href="javascript:void(0)" onclick="resetRate('${i.key}')">恢复默认</a>` : ''}</td>
    </tr>`).join('') + '</tbody>';
  box.querySelectorAll('.rate-in').forEach(inp => inp.addEventListener('change', () => saveRate(inp.dataset.key, inp.value)));
}
async function saveRate(key, value) {
  const fin = await apiPut(`/api/trips/${currentTripId}/finance_overrides`, { [key]: value === '' ? null : value });
  renderProfitTable(fin);
  renderTripLanes(currentTripId);
  toast(value === '' ? '已恢复默认费率' : '已按本车费率重新计算' + (fin.is_locked ? '，并更新固化快照' : ''));
  loadTrips();
}
function resetRate(key) { saveRate(key, ''); }

async function refreshPreview() {
  if (!currentTripId) return;
  const fin = await apiPost(`/api/trips/${currentTripId}/finance/preview`, currentOverrides());
  renderProfitTable(fin);
}

async function lockFinance() {
  if (!confirm('固化后本车次利润分项不再随档案参数变动，确认固化？')) return;
  const body = Object.assign({ lock: true }, currentOverrides());
  const fin = await apiPost(`/api/trips/${currentTripId}/finance`, body);
  await openTrip(currentTripId);
}

function bindInfoAutoSave(docPanels) {
  const fieldMap = {
    f_order_date: 'order_date', f_transport_type: 'transport_type', f_plate_no: 'plate_no',
    f_base_id: 'base_id', f_mill_id: 'mill_id', f_account_id: 'account_id', f_dispatcher_id: 'dispatcher_id',
    f_invoice_source_id: 'invoice_source_id', f_driver_name: 'driver_name', f_driver_phone: 'driver_phone',
    f_departure_place: 'departure_place', f_expected_arrival: 'expected_arrival', f_settlement_doc_no: 'settlement_doc_no',
    f_template_id: 'template_id',
    f_settlement_date: 'settlement_date', f_remarks: 'remarks',
    f_arrived_date: 'arrived_date', f_invoiced_date: 'invoiced_date', f_closed_date: 'closed_date',
    f_invoice_path: 'invoice_path',
  };
  for (const [elId, field] of Object.entries(fieldMap)) {
    const node = document.getElementById(elId);
    if (!node) continue;
    node.addEventListener('change', async () => {
      const id = currentTripId;
      if (field === 'plate_no' && node.value.trim()) {
        const data = await apiGet(`/api/trips/${id}`);
        await applyPlateProfile(data.trip, node.value.trim());
        await openTrip(id);
        loadTrips();
        return;
      }
      const patch = { [field]: node.value };
      if (field === 'mill_id') {
        const acc = accountForMill(node.value);
        if (acc) { patch.account_id = acc; document.getElementById('f_account_id').value = acc; }
      }
      if (field === 'invoice_path' && !node.value) patch.invoice_path = null;
      await apiPut(`/api/trips/${id}`, patch);
      await refreshPreview();
      if (['invoice_path', 'invoice_source_id', 'template_id', 'base_id', 'dispatcher_id'].includes(field)) renderTripLanes(id);
      loadTrips();
    });
  }
  document.getElementById('f_mixed_material').addEventListener('change', async (e) => {
    await apiPut(`/api/trips/${currentTripId}`, { mixed_material: e.target.checked ? 1 : 0 });
  });
  // 发票/打款核对区（两票三款通用化后）：任意 doc_types含invoice 的节点都会渲染出这三类输入框，
  // 靠 data-node 属性带上 node_key，统一走 PUT /api/trips/<id>/node_docs/<node_key>，
  // 不再是写死的 f_invoice_out_no 这几个字段。
  document.querySelectorAll('.docno-input').forEach(el => el.addEventListener('change', () =>
    apiPut(`/api/trips/${currentTripId}/node_docs/${el.dataset.node}`, { doc_no: el.value })));
  document.querySelectorAll('.docamt-input').forEach(el => el.addEventListener('change', () =>
    apiPut(`/api/trips/${currentTripId}/node_docs/${el.dataset.node}`, { actual_amount: el.value || null })));
  document.querySelectorAll('.docstatus-input').forEach(el => el.addEventListener('change', () =>
    apiPut(`/api/trips/${currentTripId}/node_docs/${el.dataset.node}`, { doc_status: el.checked ? 'in_stock' : null })));

}

async function setStatus(code) {
  const id = currentTripId;
  const r = TRIP_ROWS[id];
  if (code === 'settled' && r && r.status !== 'settled' && !r.detail_count) {
    return openAdvanceDialog([id], 'settled', () => openTrip(id));
  }
  await apiPost(`/api/trips/${id}/status`, { status: code });
  await openTrip(id);
  loadTrips();
}
function stepStatus(dir) {
  const usable = STATUS_LIST.filter(s => s.code !== 'voided');
  apiGet(`/api/trips/${currentTripId}`).then(data => {
    const i = usable.findIndex(s => s.code === data.trip.status);
    const ni = Math.min(Math.max(i + dir, 0), usable.length - 1);
    setStatus(usable[ni].code);
  });
}
async function voidTrip() { return voidTripById(currentTripId); }
// 作废/删除都做成「按 id」，这样列表里点开的那张小抽屉（不经过详情弹窗）也能直接用。
async function voidTripById(id) {
  if (!id) return;
  if (!confirm('确认作废该车次？作废后不再计入任何统计，但记录还在（可在「系统」页看到）。')) return;
  await apiPost(`/api/trips/${id}/status`, { status: 'voided' });
  closeDrawer(); closeModal(); loadTrips();
  toast('已作废');
}
// 再来一车：同供货厂/调货人/票源/链条规则/钢厂户头/发货地，接单日期=今天，车牌和司机清空、
// 光标落在车牌框——输完车牌会再按该车历史带出司机等信息。
async function deleteTrip() { return deleteTripById(currentTripId); }
async function deleteTripById(id) {
  if (!id) return;
  if (!confirm('删除这一车？删除后进回收站，30 天内可以在「系统 → 回收站」恢复。\n（只是不想统计它的话，用"作废"就行）')) return;
  await apiDelete(`/api/trips/${id}`);
  closeDrawer(); closeModal(); loadTrips();
  toast('已删除，可在「系统 → 回收站」恢复');
}
// 修改记录：这一车的每次改动（谁在什么时候把什么改成什么）
async function renderTripHistory(tripId) {
  const box = document.getElementById('trip-history');
  if (!box) return;
  const rows = await apiGet(`/api/system/audit?table=trips&record_id=${tripId}&limit=100`);
  const SC = { reported: '已报备', arrived: '已到厂', settled: '已结算', invoiced: '已开票', closed: '已结案', voided: '作废' };
  const v = (f, x) => x == null || x === '' ? '<span class="muted">（空）</span>' : attrEsc(f === 'status' ? (SC[x] || x) : String(x).slice(0, 60));
  box.innerHTML = rows.length ? `<table class="hist-table"><tbody>${rows.map(r => `<tr><td class="nowrap muted">${r.changed_at}</td>
    <td>${attrEsc(r.field_cn)}</td><td>${v(r.field_name, r.old_value)} → ${v(r.field_name, r.new_value)}</td></tr>`).join('')}</tbody></table>
    <a class="muted" href="/system?tab=audit&table=trips&record_id=${tripId}">在「系统」页看全部 ›</a>`
    : '<div class="muted">还没有修改记录（v2 升级之后的改动才会记录）</div>';
}
async function cloneTrip() {
  const data = await apiGet(`/api/trips/${currentTripId}`);
  const t = data.trip;
  const { id } = await apiPost('/api/trips', {
    transport_type: t.transport_type, template_id: t.template_id, mode_code: t.mode_code,
    base_id: t.base_id, mill_id: t.mill_id, account_id: t.account_id, dispatcher_id: t.dispatcher_id,
    invoice_source_id: t.invoice_source_id, departure_place: t.departure_place,
  });
  await loadTrips();
  await openTrip(id);
  focusPlate();
  toast('已复制供货厂/调货人/票源/链条规则，接单日期为今天，请输入车牌');
}
async function addPayment() {
  const nodeKeyEl = document.getElementById('pay_node_key');
  await apiPost(`/api/trips/${currentTripId}/payments`, {
    payer: document.getElementById('pay_payer').value,
    payee: document.getElementById('pay_payee').value,
    amount: document.getElementById('pay_amount').value,
    paid_at: document.getElementById('pay_date').value,
    status: 'paid',
    node_key: nodeKeyEl ? (nodeKeyEl.value || null) : null,
  });
  await apiPut(`/api/trips/${currentTripId}`, { is_paid: 1 });
  openTrip(currentTripId);
}
function attRow(a) {
  const CN = {driver_license:'驾驶证',vehicle_license:'行驶证',id_card:'身份证',env_list:'环保清单',truck_head:'车头照',truck_tail:'车尾照',cargo_photo:'货物照',weighing_slip:'过磅单',settlement_doc:'结算单',payment_screenshot:'付款截图'};
  const _e = s => String(s??'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
  const fp = a.file_path || '';
  let cell;
  if (fp.startsWith('/uploads/')) {
    const ext = (fp.split('.').pop()||'').toLowerCase();
    const isImg = ['jpg','jpeg','png','webp','heic'].includes(ext);
    cell = isImg
      ? `<a href="${_e(fp)}" target="_blank"><img src="${_e(fp)}" style="max-width:60px;max-height:60px;object-fit:cover;border-radius:4px;"></a>`
      : `<a href="${_e(fp)}" target="_blank">查看</a>`;
  } else {
    cell = _e(fp) || '<span style="color:#999;">（空）</span>';
  }
  return `<tr><td>${CN[a.category]||_e(a.category)}</td><td>${cell}</td><td><button class="btn-sm" onclick="delAttachment(${a.id})" style="color:#c00;padding:2px 6px;">删</button></td></tr>`;
}
async function uploadAttachment() {
  const input = document.getElementById('att_file');
  const file = input && input.files[0];
  if (!file) { alert('请先选文件'); return; }
  const category = document.getElementById('att_category').value;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('category', category);
  const r = await fetch(`/api/trips/${currentTripId}/attachments/upload`, { method: 'POST', body: fd });
  if (!r.ok) { const e = await r.json().catch(()=>({})); alert('上传失败：'+(e.error||r.status)); return; }
  input.value = '';
  openTrip(currentTripId);
}
async function delAttachment(attId) {
  if (!confirm('删除这条附件记录？文件移到 .trash/，不直接删。')) return;
  const r = await fetch(`/api/trips/${currentTripId}/attachments/${attId}`, { method: 'DELETE' });
  if (!r.ok) { const e = await r.json().catch(()=>({})); alert('删除失败：'+(e.error||r.status)); return; }
  openTrip(currentTripId);
}
async function addAttachment() {
  const category = document.getElementById('att_category').value;
  const path = document.getElementById('att_path').value;
  if (!path) return;
  await fetch(`/api/trips/${currentTripId}/attachments`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ category, file_path: path }),
  });
  openTrip(currentTripId);
}

async function editEntryDefaults() {
  const cur = await apiGet('/api/app_settings');
  const v = prompt('默认发货地（新建车次时自动填入，留空则不填）', cur.default_departure_place || '');
  if (v === null) return;
  await apiPut('/api/app_settings', { default_departure_place: v.trim() });
  const d = await apiGet('/api/entry_defaults');
  const mill = ARCH.mills.find(m => m.id === d.mill_id), acc = ARCH.accounts.find(a => a.id === d.account_id);
  toast(`新建默认：接单日期今天｜发货地 ${d.departure_place || '（空）'}｜钢厂 ${mill ? mill.name : '（多个，需手选）'}｜户头 ${acc ? acc.name : '（跟随钢厂）'}`, 4500);
}

loadArchives().then(async () => {
  applyUrlFilters();
  if (typeof applyDefaultSettlementPeriodFilter === 'function') {
    const meta = await applyDefaultSettlementPeriodFilter('f-from', 'f-to');
    const hint = document.getElementById('trips-period-hint');
    if (hint && meta) {
      hint.innerHTML = `列表默认显示本结算期 <b>${meta.period_label}</b>（${meta.period_start}~${meta.period_end}，按结算日期）。<a href="/trips?period=all" class="drill">看全部历史</a>`;
    }
  }
  const q = new URLSearchParams(location.search);
  if (q.get('period') === 'all') {
    document.getElementById('f-from').value = '';
    document.getElementById('f-to').value = '';
    const hint = document.getElementById('trips-period-hint');
    if (hint) hint.textContent = '当前显示全部历史车次（结算日期不限）';
  }
  return loadTrips();
}).then(() => {
  applyColumnPrefs();   // 列表为空时也要把隐藏列作用到表头
  const q = new URLSearchParams(location.search);
  // 从别的页面(统计报表-数据完整度)点过来：/trips?open=<id> 直接打开该车次
  const id = +q.get('open');
  if (id) { openTrip(id); return; }
  // 首页快捷入口：/trips?new=1 直接开一车新的；/trips?import=1 直接开批量录入。
  // 参数用完立刻抹掉（replaceState），否则一刷新就又建一车 / 弹一次框。
  if (q.get('new')) { history.replaceState(null, '', '/trips'); createTrip(); return; }
  if (q.get('import')) {
    history.replaceState(null, '', '/trips');
    if (typeof openImportDialog === 'function') openImportDialog();
  }
});
