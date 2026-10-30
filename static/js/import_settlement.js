// v2：导入钢厂结算单（2026-09-22）
// 用法：Excel/微信表格里整块复制 → 粘贴 → 系统自动认列（认错可改）→ 按车号+日期匹配车次 → 确认导入。
// "/" 或车号为空的行算上一车的第二种货（一车多料）。找不到车次的可以直接新建（按车牌历史带出调货人等）。
const IMP_ROLES = [
  ['', '（忽略）'], ['date', '日期'], ['plate', '车号/船号'], ['transport', '车/船'], ['scrap', '货型'],
  ['deduction', '扣杂'], ['tonnage', '结算吨位'], ['price', '单价'], ['amount', '金额'], ['cutting', '切割费'],
  ['docno', '结算单号'],
  ['party', '票源或供货厂'], ['source', '票源方'], ['base', '供货厂'], ['dispatcher', '调货人'],
];
const IMP_HEADER_KEYS = [
  ['date', /日期|时间|过磅日/], ['plate', /车号|车牌|船号|船名/], ['transport', /^车\/?船$|运输/], ['scrap', /货型|品名|品种|物料/],
  ['deduction', /扣杂|扣重|扣水/], ['tonnage', /结算.*吨|净重|吨位|重量/], ['price', /单价|价格/], ['amount', /金额|货款|合计/],
  ['cutting', /切割|加工费/], ['docno', /结算单|单号/], ['source', /票源/], ['base', /供货|基地|厂家/], ['dispatcher', /调货|业务员/],
];
let IMP = { rows: [], header: null, roles: [], groups: [], preview: null };

function openImportDialog() {
  IMP = { rows: [], header: null, roles: [], groups: [], preview: null };
  document.getElementById('dialog-root').innerHTML = `<div class="modal-overlay" onclick="if(event.target===this) closeImport()"><div class="modal imp-dialog">
    <div class="modal-header"><h2>导入钢厂结算单</h2><button class="close" onclick="closeImport()">&times;</button></div>
    <div id="imp-body">
      <div class="muted">从 Excel / 钢厂结算单里<b>整块复制</b>（带不带表头都行）粘贴到下面，系统会<b>自动认出每一列是什么</b>（日期/车号/货型/扣杂/吨位/单价/金额/切割费/结算单号/票源/调货人），认错的在下一步用下拉改。<br>一车多种货时，第二行车号写"/"或留空即可。</div>
      <textarea id="imp-text" rows="10" style="width:100%;margin-top:8px;font-family:monospace;font-size:12px;" placeholder="例：\n君盛\t\t9/18/2026\t苏DBB772\t中废1\t林凯\t0.8\t83.74\t2420\t\t202650.80"></textarea>
      <div class="modal-actions" style="justify-content:flex-end;"><button onclick="closeImport()">取消</button><button class="btn-primary" onclick="impParse()">下一步：认列</button></div>
    </div></div></div>`;
  setTimeout(() => document.getElementById('imp-text').focus(), 50);
}
function closeImport() { document.getElementById('dialog-root').innerHTML = ''; }

function impNum(v) { const n = parseFloat(String(v ?? '').replace(/[,，¥\s元吨]/g, '')); return isNaN(n) ? null : n; }
function impDate(v) {
  const s = String(v || '').trim(); if (!s) return null;
  const y0 = new Date().getFullYear();
  let m = s.match(/^(\d{4})[\/\-.年](\d{1,2})[\/\-.月](\d{1,2})/); if (m) return `${m[1]}-${m[2].padStart(2, '0')}-${m[3].padStart(2, '0')}`;
  m = s.match(/^(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{4})/); if (m) return `${m[3]}-${m[1].padStart(2, '0')}-${m[2].padStart(2, '0')}`;
  m = s.match(/^(\d{1,2})[\/\-.月](\d{1,2})日?$/); if (m) return `${y0}-${m[1].padStart(2, '0')}-${m[2].padStart(2, '0')}`;
  return null;
}
const isPlate = v => /^[一-龥][A-Z][A-Z0-9]{4,6}$/.test(v) || /[一-龥].*(货|航|船).*\d{2,}/.test(v);
const names = list => new Set((list || []).map(x => x.name));

function impParse() {
  const text = document.getElementById('imp-text').value.replace(/\r/g, '');
  let rows = text.split('\n').map(l => l.split('\t').map(c => c.trim())).filter(r => r.some(c => c));
  if (!rows.length) { toast('没有内容'); return; }
  const width = Math.max(...rows.map(r => r.length));
  rows = rows.map(r => r.concat(Array(width - r.length).fill('')));
  const hdr = rows[0].some(c => IMP_HEADER_KEYS.some(([, re]) => re.test(c))) && !rows[0].some(c => impDate(c)) ? rows.shift() : null;
  IMP.rows = rows; IMP.header = hdr;
  IMP.roles = impGuess(rows, hdr, width);
  impRenderMap();
}
function impGuess(rows, hdr, width) {
  const roles = Array(width).fill('');
  const used = new Set();
  if (hdr) hdr.forEach((h, i) => { const k = IMP_HEADER_KEYS.find(([r, re]) => re.test(h) && !used.has(r)); if (k) { roles[i] = k[0]; used.add(k[0]); } });
  const col = i => rows.map(r => r[i]).filter(v => v && v !== '/');
  const scrapN = names(ARCH.scrapTypes), srcN = names(ARCH.sources), baseN = names(ARCH.bases), dispN = names(ARCH.dispatchers);
  const share = (vals, fn) => vals.length ? vals.filter(fn).length / vals.length : 0;
  for (let i = 0; i < width; i++) {
    if (roles[i]) continue;
    const v = col(i); if (!v.length) continue;
    const pick = r => { if (!used.has(r)) { roles[i] = r; used.add(r); return true; } return false; };
    if (share(v, x => impDate(x)) > 0.6 && pick('date')) continue;
    if (share(v, isPlate) > 0.6 && pick('plate')) continue;
    if (share(v, x => x === '船' || x === '车') > 0.6 && pick('transport')) continue;
    if (share(v, x => scrapN.has(x) || x === '混料') > 0.5 && pick('scrap')) continue;
    if (share(v, x => dispN.has(x)) > 0.5 && pick('dispatcher')) continue;
    if (share(v, x => srcN.has(x) || baseN.has(x) || /^[一-龥]{2,6}$/.test(x)) > 0.6 && share(v, x => impNum(x) === null) > 0.9 && pick('party')) continue;
  }
  // 数字列（2026-10-09 修）：必须满足「整列非空覆盖率 ≥80%」才算业务数字列。
  // 否则像"只有某几行才有值"的切割费列会被当成吨位/扣杂，把真正的那两列挤掉（用户实际踩过）。
  const nRows = rows.length || 1;
  const numCols = [];
  for (let i = 0; i < width; i++) {
    if (roles[i]) continue;
    const vals = rows.map(r => impNum(r[i]));            // 与行对齐（空为 null）
    const filled = rows.filter(r => String(r[i] ?? '').trim() !== '' && r[i] !== '/').length;
    const nums = vals.filter(x => x !== null);
    if (!nums.length || filled / nRows < 0.8 || nums.length / filled < 0.8) continue;
    numCols.push({ i, avg: nums.reduce((a, b) => a + b, 0) / nums.length, vals });
  }
  numCols.sort((a, b) => b.avg - a.avg);
  const setR = (c, r) => { if (c && !used.has(r)) { roles[c.i] = r; used.add(r); return true; } return false; };
  // 金额 = 均值最大的一列
  const amountC = numCols[0] && setR(numCols[0], 'amount') ? numCols[0] : null;
  // 单价 = 均值落在 1000~5000 的列
  setR(numCols.find(c => roles[c.i] === '' && c.avg >= 1000 && c.avg <= 5000), 'price');
  const priceC = numCols.find(c => roles[c.i] === 'price');
  const price = priceC ? priceC.avg : 0;
  const rest = numCols.filter(c => roles[c.i] === '');
  // 吨位 = 「吨位×单价 ≈ 金额」命中率最高的列（这就是注释里一直说要做的校验，之前漏了实现）
  let tonC = null;
  if (amountC && price) {
    const okShare = c => {
      let hit = 0, tot = 0;
      for (let k = 0; k < nRows; k++) {
        const t = c.vals[k], a = amountC.vals[k];
        if (t === null || a === null || !a) continue;
        tot++; if (Math.abs(t * price - a) / Math.abs(a) <= 0.02) hit++;
      }
      return tot ? hit / tot : 0;
    };
    const scored = rest.map(c => ({ c, s: okShare(c) })).sort((a, b) => b.s - a.s);
    if (scored.length && scored[0].s >= 0.6) tonC = scored[0].c;
  }
  if (!tonC) tonC = rest[0];                             // 没有单价/对不上，退回按均值排序
  setR(tonC, 'tonnage');
  // 扣杂 = 剩下里均值最小的一列（扣杂天生比吨位小一个量级）
  const left = numCols.filter(c => roles[c.i] === '').sort((a, b) => a.avg - b.avg);
  setR(left[0], 'deduction');
  return roles;
}
function impRenderMap() {
  const sample = IMP.rows.slice(0, 6);
  document.getElementById('imp-body').innerHTML = `
    <div class="muted">系统按内容认了每一列是什么，<b>认错的在表头下拉里改</b>；不需要的列选"忽略"。共 ${IMP.rows.length} 行。</div>
    <div class="table-scroll" style="margin-top:8px;"><table class="imp-map"><thead><tr>
      ${IMP.roles.map((r, i) => `<th><select data-i="${i}" onchange="IMP.roles[${i}]=this.value">${IMP_ROLES.map(([k, l]) => `<option value="${k}" ${k === r ? 'selected' : ''}>${l}</option>`).join('')}</select>
        ${IMP.header ? `<div class="muted">${attrEsc(IMP.header[i] || '')}</div>` : ''}</th>`).join('')}
    </tr></thead><tbody>${sample.map(r => `<tr>${r.map(c => `<td>${attrEsc(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
    ${IMP.rows.length > 6 ? `<div class="muted">……还有 ${IMP.rows.length - 6} 行</div>` : ''}
    <div class="modal-actions" style="justify-content:flex-end;"><button onclick="openImportDialog()">重新粘贴</button><button class="btn-primary" onclick="impGroupAndPreview()">下一步：匹配车次</button></div>`;
}
function impGroupAndPreview() {
  const R = IMP.roles, get = (row, role) => { const i = R.indexOf(role); return i >= 0 ? row[i] : ''; };
  if (!R.includes('amount') && !R.includes('tonnage')) { toast('至少要有吨位或金额列'); return; }
  const srcN = names(ARCH.sources), baseN = names(ARCH.bases);
  const groups = [];
  let lastDate = null;
  for (const row of IMP.rows) {
    const plate = (get(row, 'plate') || '').trim();
    const cont = !plate || plate === '/';
    const item = { scrap_name: get(row, 'scrap') && get(row, 'scrap') !== '/' ? get(row, 'scrap') : '混料', deduction: impNum(get(row, 'deduction')) || 0,
      tonnage: impNum(get(row, 'tonnage')), unit_price: impNum(get(row, 'price')) || 0, amount: impNum(get(row, 'amount')),
      cutting_fee: impNum(get(row, 'cutting')) || 0 };
    if (item.amount === null && item.tonnage !== null) item.amount = +(item.tonnage * item.unit_price).toFixed(2);
    if (cont && groups.length) { groups[groups.length - 1].items.push(item); continue; }
    const party = get(row, 'party') === '/' ? '' : get(row, 'party');
    const date = impDate(get(row, 'date')) || lastDate; lastDate = date;
    groups.push({ plate, date, transport: get(row, 'transport') === '船' || /货|航/.test(plate) ? 'ship' : 'truck',
      doc_no: get(row, 'docno') || '', dispatcher: get(row, 'dispatcher') !== '/' ? get(row, 'dispatcher') : '',
      source: get(row, 'source') || (srcN.has(party) ? party : ''), base: get(row, 'base') || (baseN.has(party) ? party : ''),
      party, items: [item] });
  }
  IMP.groups = groups;
  apiPost('/api/import/settlement/preview', { trips: groups }).then(res => { IMP.preview = res.rows; impRenderPreview(); });
}
function tplIdByCode(code) { const t = (ALL_TEMPLATES || []).find(x => x.code === code); return t ? t.id : ''; }
function impDefaultTemplate(g, p) {
  const src = ARCH.sources.find(s => s.id === p.resolved.invoice_source_id);
  if (src) return tplIdByCode(src.source_type === 'reverse_individual' ? 'split_reverse' : 'split_company');
  if (p.profile && p.profile.fill && p.profile.fill.template_id && !p.resolved.base_id) return p.profile.fill.template_id;
  return tplIdByCode('service_fee_direct');
}
function impRenderPreview() {
  const tplOpts = sel => (ARCH.templates || []).map(t => `<option value="${t.id}" ${String(t.id) === String(sel) ? 'selected' : ''}>${attrEsc(t.short_name || t.name)}</option>`).join('');
  const nm = (list, id) => (list.find(x => x.id === id) || {}).name || '';
  let totT = 0, totA = 0;
  const rows = IMP.groups.map((g, i) => {
    const p = IMP.preview[i];
    totT += p.tonnage; totA += p.total;
    const disp = p.resolved.dispatcher_id || (p.profile && p.profile.fill.dispatcher_id) || '';
    const cands = p.candidates.map(c => `<option value="u:${c.id}" ${p.action === 'update' && p.target === c.id ? 'selected' : ''}>更新 #${c.id}（${statusLabel(c.status)} ${c.order_date || c.settlement_date || ''}${c.detail_count ? ' 已有明细' : ''}）</option>`).join('');
    const warn = [];
    if (p.duplicate_of) warn.push(`已有同车号同金额的已结算车次 #${p.duplicate_of}，默认跳过`);
    if (p.unknown_types.length) warn.push('货型字典里没有：' + p.unknown_types.join('、') + '（导入后货型为空）');
    if (p.party_unresolved) warn.push(`"${g.party}" 不是已有的票源/供货厂，新建前请先去基础档案加`);
    if (p.party_note) warn.push(p.party_note);
    if (!g.date) warn.push('没认出日期');
    return `<tr data-i="${i}">
      <td>${attrEsc(g.plate || '?')}${g.transport === 'ship' ? '<span class="tag">船</span>' : ''}<div class="muted">${g.date || '-'}</div></td>
      <td>${g.items.map(it => `${attrEsc(it.scrap_name)} ${fmtNum(it.tonnage, 3)}吨${it.unit_price ? '×' + fmtNum(it.unit_price, 0) : ''}${it.deduction ? ' 扣' + it.deduction : ''}`).join('<br>')}</td>
      <td class="num">${fmtNum(p.tonnage, 3)}</td><td class="num">${fmtMoney(p.total)}</td>
      <td class="muted">${[p.resolved.invoice_source_id && '票源 ' + nm(ARCH.sources, p.resolved.invoice_source_id), p.resolved.base_id && '供货厂 ' + nm(ARCH.bases, p.resolved.base_id), nm(ARCH.dispatchers, disp) && '调货人 ' + nm(ARCH.dispatchers, disp)].filter(Boolean).join('<br>')}</td>
      <td><select class="imp-act">${cands}<option value="create" ${p.action === 'create' ? 'selected' : ''}>新建车次</option><option value="skip" ${p.action === 'skip' ? 'selected' : ''}>跳过</option></select>
        <div class="imp-tpl" ${p.action === 'create' ? '' : 'hidden'}>链条 <select class="imp-tplsel">${tplOpts(impDefaultTemplate(g, p))}</select></div></td>
      <td class="imp-warn">${warn.map(w => '⚠ ' + attrEsc(w)).join('<br>')}</td></tr>`;
  }).join('');
  document.getElementById('imp-body').innerHTML = `
    <div class="muted">共 ${IMP.groups.length} 车 · ${fmtNum(totT, 3)} 吨 · ¥${fmtMoney(totA)}。按<b>车号 + 日期前后3天</b>找已有车次；找不到的默认新建（按车牌历史带出调货人等），已经结算过同一金额的默认跳过。</div>
    <div class="field-row" style="margin:8px 0;"><div class="field"><label>结算单号（整批，可空；表里有单号列的以表为准）</label><input id="imp-docno"></div></div>
    <div class="table-scroll"><table class="imp-prev"><thead><tr><th>车号/日期</th><th>货型明细</th><th class="num">吨位</th><th class="num">金额</th><th>票源/供货厂/调货人</th><th>处理</th><th>提示</th></tr></thead>
    <tbody>${rows}</tbody></table></div>
    <div class="modal-actions" style="justify-content:flex-end;"><button onclick="impRenderMap()">上一步</button><button class="btn-primary" id="imp-go" onclick="impApply()">确认导入</button></div>`;
  document.querySelectorAll('.imp-prev .imp-act').forEach(s => s.addEventListener('change', () => {
    s.closest('td').querySelector('.imp-tpl').hidden = s.value !== 'create';
  }));
}
async function impApply() {
  const batchDoc = document.getElementById('imp-docno').value.trim();
  const payload = [];
  document.querySelectorAll('.imp-prev tbody tr').forEach(tr => {
    const i = +tr.dataset.i, g = IMP.groups[i], p = IMP.preview[i];
    const act = tr.querySelector('.imp-act').value;
    const row = { plate: g.plate, date: g.date, transport_type: g.transport, doc_no: g.doc_no || batchDoc || null, items: g.items };
    if (act === 'skip') row.action = 'skip';
    else if (act === 'create') {
      Object.assign(row, { action: 'create', template_id: tr.querySelector('.imp-tplsel').value,
        dispatcher_id: p.resolved.dispatcher_id || (p.profile && p.profile.fill.dispatcher_id) || null,
        base_id: p.resolved.base_id || (p.profile && p.profile.fill.base_id && !p.resolved.invoice_source_id ? p.profile.fill.base_id : null),
        invoice_source_id: p.resolved.invoice_source_id || null });
    } else Object.assign(row, { action: 'update', trip_id: +act.slice(2) });
    payload.push(row);
  });
  const n = payload.filter(r => r.action !== 'skip').length;
  if (!n) { toast('没有要导入的车'); return; }
  if (!confirm(`导入 ${n} 车（新建 ${payload.filter(r => r.action === 'create').length}、更新 ${payload.filter(r => r.action === 'update').length}）？导入前会自动备份。`)) return;
  document.getElementById('imp-go').disabled = true;
  const res = await apiPost('/api/import/settlement/apply', { rows: payload });
  closeImport();
  toast(`已导入 ${res.count} 车，都已推进到「已结算」`);
  location.href = tripsUrl({ ids: res.trip_ids }, `导入结算单 ${new Date().toLocaleString()}`);
}
