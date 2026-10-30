// 手机录入（v2.1）：报车 / 在途(到厂、结算) / 查车 / 概况
const A = { bases: [], allBases: [], disps: [], srcs: [], tpls: [], plates: [], trips: [], defaults: {} };
const STATUS_CN = { reported: '已报备', arrived: '已到厂', settled: '已结算', invoiced: '已开票', closed: '已结案', voided: '作废' };
const NEXT = { reported: 'arrived', arrived: 'settled', settled: 'invoiced', invoiced: 'closed' };
const TITLES = { new: '报车', road: '在途车次', find: '查车', home: '概况' };
let tplPickedByHand = false;

function esc(v) { return String(v ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'); }
function money(v, d = 0) { return v == null || v === '' ? '-' : '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }); }
function num(v, d = 2) { return v == null || v === '' ? '-' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: d }); }
function today() { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }
function toast(msg, ms = 2600) {
  const t = document.getElementById('m-toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), ms);
}
async function api(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined, cache: 'no-store' });
  let j = null; try { j = await r.json(); } catch (e) { /* 非 JSON */ }
  if (r.status === 401 && j && j.need_login) { location.href = '/m/login?next=/m'; throw new Error('需要登录'); }
  if (!r.ok) throw new Error((j && j.error) || ('出错了（' + r.status + '）'));
  return j;
}
const nameIn = (list, id) => { const o = list.find(x => String(x.id) === String(id)); return o ? o.name : ''; };
const tplLabel = id => { const t = A.tpls.find(x => String(x.id) === String(id)); return t ? (t.short_name || t.name) : ''; };

// ---------- 标签切换 ----------
function showTab(t) {
  document.querySelectorAll('.m-tabs button').forEach(b => b.classList.toggle('on', b.dataset.t === t));
  ['new', 'road', 'find', 'home'].forEach(k => document.getElementById('tab-' + k).hidden = k !== t);
  document.getElementById('m-title').textContent = TITLES[t];
  try { sessionStorage.setItem('m_tab', t); } catch (e) { /* 忽略 */ }
  window.scrollTo(0, 0);
  if (t === 'road') loadRoad();
  if (t === 'find') loadTrips().then(renderFind);
  if (t === 'home') loadHome();
}

// ---------- 报车 ----------
function fillSelect(id, list, emptyText) {
  document.getElementById(id).innerHTML = `<option value="">${emptyText}</option>` +
    list.map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join('');
}
function segValue(id) { const b = document.querySelector(`#${id} button.on`); return b ? b.dataset.v : ''; }
function setSeg(id, v) { document.querySelectorAll(`#${id} button`).forEach(b => b.classList.toggle('on', b.dataset.v === v)); }
function bindSeg(id, cb) {
  document.getElementById(id).addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return; setSeg(id, b.dataset.v); if (cb) cb(b.dataset.v);
  });
}
function renderTplChips(selected) {
  document.getElementById('n-tpl').innerHTML = A.tpls.map(t =>
    `<button type="button" data-id="${t.id}" class="${String(t.id) === String(selected) ? 'on' : ''}">${esc(t.short_name || t.name)}</button>`).join('');
}
function selectedTpl() { const b = document.querySelector('#n-tpl button.on'); return b ? Number(b.dataset.id) : null; }
function setTpl(id, why) {
  renderTplChips(id);
  document.getElementById('n-tpl-hint').textContent = id ? (why || '') : '请选一个，决定这车利润怎么算';
}
// 没按车牌历史带出链条时，按票源推荐：反向个人 → 反向；其它票源 → 配票；没有票源(自带票) → 直签带票
function suggestTemplate() {
  const srcId = document.getElementById('n-src').value;
  const src = A.srcs.find(s => String(s.id) === srcId);
  if (src && src.bound_base_id && !document.getElementById('n-base').value) document.getElementById('n-base').value = src.bound_base_id;
  if (tplPickedByHand) return;
  const code = !src ? 'service_fee_direct' : (src.source_type === 'reverse_individual' ? 'split_reverse' : 'split_company');
  const t = A.tpls.find(x => x.code === code);
  if (t) setTpl(t.id, `按票源推荐「${t.short_name || t.name}」，不对就点别的`);
}
async function onPlate() {
  const el = document.getElementById('n-plate');
  const plate = el.value.trim().toUpperCase();
  el.value = plate;
  const hint = document.getElementById('n-plate-hint');
  hint.classList.remove('warn');
  if (!plate) { hint.textContent = ''; return; }
  const onRoad = A.trips.filter(t => t.plate_no === plate && (t.status === 'reported' || t.status === 'arrived'));
  const prof = await api('GET', '/api/plate_profile?plate=' + encodeURIComponent(plate));
  const f = prof.fill || {};
  const filled = [];
  if (f.transport_type) { setSeg('n-type', f.transport_type); }
  if (f.base_id) { document.getElementById('n-base').value = f.base_id; filled.push('供货厂'); }
  if (f.dispatcher_id) { document.getElementById('n-disp').value = f.dispatcher_id; filled.push('调货人'); }
  if (f.invoice_source_id) { document.getElementById('n-src').value = f.invoice_source_id; filled.push('票源'); }
  if (f.driver_name) document.getElementById('n-driver').value = f.driver_name;
  if (f.driver_phone) document.getElementById('n-phone').value = f.driver_phone;
  if (f.template_id) { tplPickedByHand = true; setTpl(f.template_id, '按这辆车的历史带出'); filled.push('链条'); }
  else { tplPickedByHand = false; suggestTemplate(); }
  let msg;
  if (!prof.count) msg = '新车号，第一次报车';
  else if (prof.fleet_of_dispatcher) msg = `${nameIn(A.disps, prof.fleet_of_dispatcher)}车队的车（历史 ${prof.count} 次），已带出调货人；供货厂可以不填`;
  else msg = `历史 ${prof.count} 次` + (filled.length ? `，已带出${filled.join('、')}` : '') +
    (Object.keys(prof.varies || {}).length ? '；其它每次不一样，请选' : '');
  if (onRoad.length) { msg += `。注意：这辆车已有 ${onRoad.length} 条在途记录（${onRoad.map(t => t.order_date).join('、')}）`; hint.classList.add('warn'); }
  hint.textContent = msg;
}
function resetNew() {
  document.getElementById('new-form').reset();
  document.getElementById('n-date').value = today();
  setSeg('n-type', 'truck');
  tplPickedByHand = false;
  setTpl(null);
  document.getElementById('n-plate-hint').textContent = '';
  document.getElementById('n-tpl-hint').textContent = '';
}
async function submitNew(e) {
  e.preventDefault();
  const plate = document.getElementById('n-plate').value.trim().toUpperCase();
  const tpl = selectedTpl();
  if (!tpl) { toast('请选链条规则'); document.getElementById('n-tpl').scrollIntoView({ block: 'center', behavior: 'smooth' }); return; }
  const onRoad = A.trips.filter(t => t.plate_no === plate && (t.status === 'reported' || t.status === 'arrived'));
  if (onRoad.length && !confirm(`${plate} 已经有在途记录，确定再报一车？`)) return;
  const v = id => document.getElementById(id).value.trim();
  const body = {
    plate_no: plate, transport_type: segValue('n-type'), order_date: v('n-date'), status: 'reported',
    base_id: v('n-base') ? Number(v('n-base')) : null, dispatcher_id: v('n-disp') ? Number(v('n-disp')) : null,
    invoice_source_id: v('n-src') ? Number(v('n-src')) : null, template_id: tpl,
  };
  if (v('n-driver')) body.driver_name = v('n-driver');
  if (v('n-phone')) body.driver_phone = v('n-phone');
  if (v('n-remarks')) body.remarks = v('n-remarks');
  const btn = document.getElementById('n-submit'); btn.disabled = true;
  try {
    const r = await api('POST', '/api/trips', body);
    toast(`已报车：${plate}`);
    resetNew();
    if (!A.plates.find(p => p.plate_no === plate)) A.plates.unshift({ plate_no: plate });
    renderPlates();
    window._flashId = r.id;
    showTab('road');
  } catch (err) { toast('没报上：' + err.message, 4000); }
  finally { btn.disabled = false; }
}
function renderPlates() {
  document.getElementById('plate-list').innerHTML = A.plates.slice(0, 200).map(p => `<option value="${esc(p.plate_no)}"></option>`).join('');
}

// ---------- 车次卡片 ----------
async function loadTrips() { A.trips = await api('GET', '/api/trips'); return A.trips; }
function party(t) {
  const who = [t.dispatcher_name, t.base_name].filter(Boolean);
  return who.length ? who.join('：') : '未填供货厂';
}
function card(t, withActions) {
  const acts = [];
  if (withActions && t.status === 'reported') acts.push(`<button onclick="event.stopPropagation();advance(${t.id},'arrived')">到厂</button>`);
  if (withActions && (t.status === 'reported' || t.status === 'arrived')) acts.push(`<button class="primary" onclick="event.stopPropagation();openSettle(${t.id})">结算</button>`);
  const nums = t.settlement_tonnage_total
    ? `<div class="card-nums"><span><b>${num(t.settlement_tonnage_total, 3)}</b> 吨</span><span><b>${money(t.settlement_amount_total)}</b></span>${t.settlement_date ? `<span>结算 ${esc(t.settlement_date)}</span>` : ''}</div>` : '';
  return `<div class="card" id="card-${t.id}" onclick="openTrip(${t.id})">
    <div class="card-top"><span class="plate">${esc(t.plate_no || '（无车号）')}</span><span class="tag">${t.transport_type === 'ship' ? '船' : '车'}</span>
      ${t.template_short ? `<span class="tag">${esc(t.template_short)}</span>` : ''}<span class="st st-${t.status}">${STATUS_CN[t.status] || t.status}</span></div>
    <div class="card-sub">${esc(party(t))} · 接单 ${esc(t.order_date || '-')}${t.arrived_date ? ' · 到厂 ' + esc(t.arrived_date) : ''}</div>
    ${nums}${acts.length ? `<div class="card-acts">${acts.join('')}</div>` : ''}</div>`;
}
async function loadRoad() {
  await loadTrips();
  const rows = A.trips.filter(t => t.status === 'reported' || t.status === 'arrived')
    .sort((a, b) => (b.order_date || '').localeCompare(a.order_date || '') || b.id - a.id);
  const nR = rows.filter(t => t.status === 'reported').length;
  document.getElementById('road-sum').textContent = rows.length ? `已报备 ${nR} 车 · 已到厂 ${rows.length - nR} 车` : '';
  document.getElementById('road-list').innerHTML = rows.map(t => card(t, true)).join('') || '<div class="empty">没有在途的车。报了车会出现在这里。</div>';
  const badge = document.getElementById('road-badge'); badge.hidden = !rows.length; badge.textContent = rows.length;
  if (window._flashId) { const el = document.getElementById('card-' + window._flashId); if (el) { el.classList.add('flash'); el.scrollIntoView({ block: 'center' }); } window._flashId = null; }
}
function renderFind() {
  const q = document.getElementById('f-q').value.trim().toUpperCase();
  const st = segValue('f-status');
  const rows = A.trips.filter(t => t.status !== 'voided' && (!st || t.status === st) &&
    (!q || [t.plate_no, t.base_name, t.dispatcher_name, t.template_short, t.settlement_doc_no].some(x => x && String(x).toUpperCase().includes(q))))
    .sort((a, b) => (b.settlement_date || b.order_date || '').localeCompare(a.settlement_date || a.order_date || '') || b.id - a.id);
  document.getElementById('find-list').innerHTML = (rows.length ? `<div class="muted">${rows.length} 车${rows.length > 40 ? '，显示最近 40 车' : ''}</div>` : '') +
    (rows.slice(0, 40).map(t => card(t, false)).join('') || '<div class="empty">没找到</div>');
}

// ---------- 推进状态 ----------
async function advance(id, status, extra) {
  try {
    await api('POST', `/api/trips/${id}/status`, { status, ...(extra || {}) });
    toast(`已改为「${STATUS_CN[status]}」`);
    closeSheet();
    window._flashId = id;
    if (!document.getElementById('tab-road').hidden) loadRoad(); else if (!document.getElementById('tab-find').hidden) { await loadTrips(); renderFind(); }
  } catch (e) { toast('没改成：' + e.message, 4000); }
}
function openSheet(html) {
  document.getElementById('sheet-body').innerHTML = html;
  document.getElementById('sheet').hidden = false; document.getElementById('sheet-mask').hidden = false;
}
function closeSheet() { document.getElementById('sheet').hidden = true; document.getElementById('sheet-mask').hidden = true; }

function openSettle(id) {
  const t = A.trips.find(x => x.id === id);
  const hasDetail = t.detail_count > 0;
  openSheet(`<h2>${esc(t.plate_no)} 结算</h2><div class="sub">${esc(party(t))} · 接单 ${esc(t.order_date || '-')}</div>
    <form onsubmit="doSettle(event, ${id})">
      <div class="field"><label for="s-date">结算日期</label><input id="s-date" type="date" value="${today()}" required></div>
      ${hasDetail ? `<div class="hint">这车已经录过磅重明细（${num(t.settlement_tonnage_total, 3)} 吨，${money(t.settlement_amount_total)}），按明细结算。</div>` : `
      <div class="field-row" style="grid-template-columns:1fr 1fr">
        <div class="field"><label for="s-ton">结算吨位</label><input id="s-ton" type="number" step="0.001" inputmode="decimal" required oninput="calcPrice()"></div>
        <div class="field"><label for="s-amt">结算金额（元）</label><input id="s-amt" type="number" step="0.01" inputmode="decimal" required oninput="calcPrice()"></div>
      </div><div class="calc-line" id="s-price"></div>`}
      <div class="field"><label for="s-doc">结算单号（选填）</label><input id="s-doc"></div>
      <button class="btn-big" type="submit">确认结算</button>
    </form>`);
}
function calcPrice() {
  const t = parseFloat(document.getElementById('s-ton').value), a = parseFloat(document.getElementById('s-amt').value);
  document.getElementById('s-price').textContent = t > 0 && a > 0 ? `折合单价 ${num(a / t, 2)} 元/吨` : '';
}
function doSettle(e, id) {
  e.preventDefault();
  const g = k => { const el = document.getElementById(k); return el ? el.value.trim() : ''; };
  const extra = { status_date: g('s-date') };
  if (g('s-ton')) extra.settlement_tonnage_total = Number(g('s-ton'));
  if (g('s-amt')) extra.settlement_amount_total = Number(g('s-amt'));
  if (g('s-doc')) extra.settlement_doc_no = g('s-doc');
  advance(id, 'settled', extra);
}

// ---------- 车次详情 ----------
async function openTrip(id) {
  openSheet('<div class="empty">加载中…</div>');
  let d;
  try { d = await api('GET', '/api/trips/' + id); } catch (e) { openSheet(`<div class="empty">${esc(e.message)}</div>`); return; }
  const t = d.trip, f = d.finance || {};
  const nx = NEXT[t.status];
  const rows = [
    ['状态', `<span class="st st-${t.status}">${STATUS_CN[t.status]}</span>`],
    ['调货人：供货厂', esc(party({ dispatcher_name: nameIn(A.disps, t.dispatcher_id), base_name: nameIn(A.allBases, t.base_id) }))],
    ['票源', esc(nameIn(A.srcs, t.invoice_source_id) || '自带票')],
    ['链条规则', esc(tplLabel(t.template_id) || '-')],
    ['接单日期', esc(t.order_date || '-')],
  ];
  if (t.arrived_date) rows.push(['到厂日期', esc(t.arrived_date)]);
  if (t.settlement_date) rows.push(['结算日期', esc(t.settlement_date)]);
  if (t.settlement_tonnage_total) {
    rows.push(['结算吨位', num(t.settlement_tonnage_total, 3) + ' 吨'], ['结算金额', money(t.settlement_amount_total, 2)]);
    rows.push(['本车利润', `<span class="${(f.net_profit || 0) < 0 ? 'neg' : 'pos'}">${money(f.net_profit, 2)}</span>`]);
    if (f.invoice_diff_profit) rows.push(['票差留存', money(f.invoice_diff_profit, 2)]);
  }
  if (t.driver_name || t.driver_phone) rows.push(['司机', esc([t.driver_name, t.driver_phone].filter(Boolean).join(' '))]);
  if (t.settlement_doc_no) rows.push(['结算单号', esc(t.settlement_doc_no)]);
  let act = '';
  if (nx === 'arrived') act = `<div class="btn-row"><button class="btn-sec" onclick="advance(${id},'arrived')">到厂</button><button class="btn-big" onclick="openSettle(${id})">结算</button></div>`;
  else if (nx === 'settled') act = `<button class="btn-big" onclick="openSettle(${id})">结算</button>`;
  else if (nx) act = `<button class="btn-big" onclick="if(confirm('推进到「${STATUS_CN[nx]}」？日期记今天'))advance(${id},'${nx}')">推进到「${STATUS_CN[nx]}」</button>`;
  openSheet(`<h2>${esc(t.plate_no || '（无车号）')}<span class="tag">${t.transport_type === 'ship' ? '船' : '车'}</span></h2>
    <div class="sub">车次 #${t.id}</div>
    <dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
    ${renderAttSection(d.attachments || [], id)}
    ${act}<div class="btn-row"><a class="btn-sec" href="/trips?open=${id}">电脑版打开</a><button class="btn-sec" onclick="closeSheet()">关闭</button></div>`);
  if (!A.trips.find(x => x.id === id)) A.trips.push({ ...t, detail_count: (d.details || []).length });
}

// ---------- 附件区（v2.3 ① 现场拍照直传） ----------
const ATT_CN = {weighing_slip:'过磅单',settlement_doc:'结算单',payment_screenshot:'付款截图',cargo_photo:'货物照',truck_head:'车头照',truck_tail:'车尾照',driver_license:'驾驶证',vehicle_license:'行驶证',id_card:'身份证',env_list:'环保清单'};
function renderAttSection(atts, id) {
  const list = (atts || []).map(a => {
    const fp = a.file_path || '';
    let cell;
    if (fp.startsWith('/uploads/')) {
      const ext = (fp.split('.').pop() || '').toLowerCase();
      cell = ['jpg','jpeg','png','webp','heic'].includes(ext)
        ? `<a href="${esc(fp)}" target="_blank"><img src="${esc(fp)}" style="max-width:50px;max-height:50px;object-fit:cover;border-radius:4px;vertical-align:middle;"></a>`
        : `<a href="${esc(fp)}" target="_blank" style="color:#1d4ed8;">查看</a>`;
    } else {
      cell = `<span style="color:#999;">${esc(fp) || '（空）'}</span>`;
    }
    return `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid #f3f4f6;"><span style="flex:0 0 auto;font-size:13px;color:#374151;">${ATT_CN[a.category]||a.category}</span><span style="flex:1;">${cell}</span></div>`;
  }).join('') || '<div style="padding:8px;color:#9ca3af;font-size:13px;">暂无附件</div>';
  return `
    <div style="margin-top:12px;border-top:1px solid #e5e7eb;padding-top:8px;">
      <div style="font-size:13px;color:#6b7280;margin:4px 0 6px;">附件</div>
      <div>${list}</div>
      <div class="field-row" style="margin-top:8px;">
        <select id="m_att_cat" style="flex:0 0 auto;">
          <option value="weighing_slip">过磅单</option><option value="settlement_doc">结算单</option>
          <option value="payment_screenshot">付款截图</option><option value="cargo_photo">货物照</option>
        </select>
        <input id="m_att_file" type="file" accept="image/*,application/pdf" capture="environment" style="flex:1;font-size:12px;">
      </div>
      <button class="btn-sec" style="margin-top:6px;width:100%;" onclick="uploadMobileAtt(${id})">拍照/选文件上传</button>
    </div>`;
}
async function uploadMobileAtt(id) {
  const input = document.getElementById('m_att_file');
  const file = input && input.files[0];
  if (!file) { toast('请先选文件或拍照', 2500); return; }
  const category = document.getElementById('m_att_cat').value;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('category', category);
  try {
    const r = await fetch(`/api/trips/${id}/attachments/upload`, { method: 'POST', body: fd });
    if (!r.ok) { const e = await r.json().catch(() => ({})); toast('上传失败：' + (e.error || r.status), 3500); return; }
    toast('已上传', 1400);
    openTrip(id);
  } catch (e) { toast('上传失败：' + e.message, 3000); }
}

// ---------- 概况 ----------
async function loadHome() {
  const [dash, alerts] = await Promise.all([api('GET', '/api/dashboard'), api('GET', '/api/alerts')]);
  document.getElementById('kpis').innerHTML = `
    <div class="kpi"><div class="k">本期车次</div><div class="v">${dash.current_period_orders}</div><div class="s">${dash.period_start.slice(5)} ~ ${dash.period_end.slice(5)}</div></div>
    <div class="kpi"><div class="k">今天报车</div><div class="v">${dash.today_orders}</div><div class="s">待结算 ${dash.pending_settle} 车</div></div>
    <div class="kpi"><div class="k">累计利润</div><div class="v">${money(dash.total_net_profit)}</div><div class="s">${dash.total_settled_trips} 车已结算</div></div>
    <div class="kpi"><div class="k">票差留存</div><div class="v">${money(dash.total_reserved_profit)}</div><div class="s">累计 ${num(dash.total_settled_tonnage, 0)} 吨</div></div>`;
  document.getElementById('alerts').innerHTML = alerts.map(a =>
    `<a class="alert ${a.level}" href="${esc(a.href)}"><div class="t">${esc(a.title)}</div><div class="d">${esc(a.detail)}</div></a>`).join('')
    || '<div class="empty">没有需要处理的事</div>';
}

// ---------- 启动 ----------
(async function init() {
  bindSeg('n-type'); bindSeg('f-status', renderFind);
  document.getElementById('n-tpl').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    tplPickedByHand = true; setTpl(Number(b.dataset.id), '');
  });
  try {
    const [bases, disps, srcs, tpls, plates, defaults] = await Promise.all([
      api('GET', '/api/processing_bases'), api('GET', '/api/dispatchers'), api('GET', '/api/invoice_sources'),
      api('GET', '/api/flow_templates'), api('GET', '/api/plates'), api('GET', '/api/entry_defaults')]);
    A.allBases = bases;
    A.bases = bases.filter(b => !b.is_dispatcher_alias).sort((a, b) => a.name.localeCompare(b.name, 'zh'));
    A.disps = disps; A.srcs = srcs; A.plates = plates; A.defaults = defaults;
    A.tpls = tpls.filter(t => t.is_active).sort((a, b) => (b.trip_count || 0) - (a.trip_count || 0));
    fillSelect('n-base', A.bases, '不填 / 调货人自己的车队');
    fillSelect('n-disp', A.disps, '没有调货人');
    fillSelect('n-src', A.srcs, '无（自带票）');
    renderPlates();
    resetNew();
    await loadTrips();
    const n = A.trips.filter(t => t.status === 'reported' || t.status === 'arrived').length;
    const badge = document.getElementById('road-badge'); badge.hidden = !n; badge.textContent = n;
  } catch (e) { toast('加载失败：' + e.message, 5000); }
  let t = 'new'; try { t = sessionStorage.getItem('m_tab') || 'new'; } catch (e) { /* 忽略 */ }
  if (t !== 'new') showTab(t);
})();
