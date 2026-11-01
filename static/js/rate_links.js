// 费率与关系档案页面：列表增删改 + 业务关系图（同一份 rate_links 数据驱动两者）。
let CATALOG = [];
let ARCH = {}; // processing_bases / dispatchers / invoice_sources / accounts / steel_mills
let RATE_ROWS = [];
let editingId = null;
let RATE_FOCUS = null;
let PERIOD_LAYOUT = null;
let OPEN_PERIOD_SHEET = null;
const MILL_OPEN = {};

function parseRateFocusFromUrl() {
  const q = new URLSearchParams(location.search);
  if (!q.get('rt') && !q.get('pid')) return null;
  return {
    rt: q.get('rt') || null,
    pt: q.get('pt') || null,
    pid: q.get('pid') ? Number(q.get('pid')) : null,
    st: q.get('st') || null,
    sid: q.get('sid') ? Number(q.get('sid')) : null,
  };
}

function rowMatchesFocus(r, focus) {
  if (!focus) return false;
  if (focus.rt && r.relation_type !== focus.rt) return false;
  if (focus.pt && r.primary_entity_type !== focus.pt) return false;
  if (focus.pid != null && r.primary_entity_id !== focus.pid) return false;
  if (focus.st && r.secondary_entity_type !== focus.st) return false;
  if (focus.sid != null && r.secondary_entity_id !== focus.sid) return false;
  return true;
}

function groupMatchesFocus(g) {
  return RATE_FOCUS && g.some(r => rowMatchesFocus(r, RATE_FOCUS));
}

function updateRateFocusBanner() {
  const box = document.getElementById('rate-focus-banner');
  if (!box) return;
  if (!RATE_FOCUS) { box.style.display = 'none'; box.innerHTML = ''; return; }
  const cat = CATALOG.find(c => c.code === RATE_FOCUS.rt);
  const typeLabel = cat ? cat.label : (RATE_FOCUS.rt || '费率');
  const primaryName = RATE_FOCUS.pt && RATE_FOCUS.pid != null
    ? (entityName(RATE_FOCUS.pt, RATE_FOCUS.pid) || `#${RATE_FOCUS.pid}`)
    : '';
  const secName = RATE_FOCUS.st && RATE_FOCUS.sid != null
    ? (entityName(RATE_FOCUS.st, RATE_FOCUS.sid) || `#${RATE_FOCUS.sid}`)
    : '';
  const who = [primaryName, secName].filter(Boolean).join(' → ');
  box.style.display = 'block';
  box.innerHTML = `从关系图跳转：定位 <strong>${esc(typeLabel)}</strong>${who ? ` · ${esc(who)}` : ''}。` +
    ` <a href="/rate_links">清除定位</a>`;
}

const ENTITY_META = {
  processing_base: { source: 'processing_bases', label: '供货厂/基地' },
  dispatcher: { source: 'dispatchers', label: '调货员' },
  invoice_source: { source: 'invoice_sources', label: '票源方' },
  account: { source: 'accounts', label: '户头' },
  steel_mill: { source: 'steel_mills', label: '钢厂' },
};
const COLUMN_ORDER = ['processing_base', 'dispatcher', 'invoice_source', 'account', 'steel_mill'];

function entityName(type, id) {
  if (!type || !id) return null;
  const meta = ENTITY_META[type];
  if (!meta) return null;
  const row = (ARCH[meta.source] || []).find(r => r.id === id);
  return row ? row.name : `#${id}(已删除)`;
}
function unitLabel(u) { return u === 'pct' ? '%' : '元/吨'; }

async function loadAll() {
  const [catalog, bases, dispatchers, sources, accounts, mills, rows] = await Promise.all([
    apiGet('/api/rate_links/relation_types'),
    apiGet('/api/processing_bases'),
    apiGet('/api/dispatchers'),
    apiGet('/api/invoice_sources'),
    apiGet('/api/accounts'),
    apiGet('/api/steel_mills'),
    apiGet('/api/rate_links'),
  ]);
  CATALOG = catalog;
  ARCH = { processing_bases: bases, dispatchers, invoice_sources: sources, accounts, steel_mills: mills };
  RATE_ROWS = rows;
  await loadPeriodLayout();
}

async function loadPeriodLayout() {
  const asOf = (typeof getRulesAsOf === 'function' && getRulesAsOf()) || todayStr();
  PERIOD_LAYOUT = await apiGet('/api/rate_links/period_layout?as_of=' + encodeURIComponent(asOf));
  const hint = document.getElementById('rate-layout-hint');
  if (hint && PERIOD_LAYOUT.period_label) {
    hint.textContent =
      `当前维护 ${PERIOD_LAYOUT.period_label}（${PERIOD_LAYOUT.period_start} ~ ${PERIOD_LAYOUT.period_end}）· 按钢厂 × 结算期分组；往期默认折叠。`;
  }
  updateOpenPeriodBanner();
}

function millBucketForRow(r) {
  if (!PERIOD_LAYOUT) return 0;
  const am = PERIOD_LAYOUT.account_mill || {};
  if (r.primary_entity_type === 'steel_mill' && r.primary_entity_id) return r.primary_entity_id;
  if (r.primary_entity_type === 'account' && r.primary_entity_id) {
    return am[r.primary_entity_id] || 0;
  }
  return 0;
}

function currentPeriodLabel() {
  return PERIOD_LAYOUT ? PERIOD_LAYOUT.period_label : periodKeyOf(todayStr());
}

function sectionKey(millId, periodKey) {
  return `M${millId}|P${periodKey}`;
}

function toggleMill(mid) {
  MILL_OPEN[mid] = !MILL_OPEN[mid];
  renderTable();
}

// ---------------- 关系图（分栏SVG，不依赖外部库） ----------------
// 2026-09-19修复：以前所有边都画直线，跨列的边(比如"票源→基地"跳过"调货员"这一列)
// 直线会正好穿过中间那一列的节点位置，视觉上像是连到了中间列的节点上(用户反馈"闫东朋和
// 林凯看着像有关系"，根源之一)。现在跨列(colDiff>1)的边改画成"绕到最上方"的曲线，
// 保证不会跟任何列的节点行重叠；相邻列(colDiff=1)的边不受影响，仍画直线，更清爽。
async function renderGraph() {
  const asOf = (typeof getRulesAsOf === 'function' && getRulesAsOf()) || todayStr();
  const data = await apiGet('/api/rate_links/graph?as_of=' + encodeURIComponent(asOf));
  const container = document.getElementById('rate-graph');
  const nodesByType = {};
  COLUMN_ORDER.forEach(t => nodesByType[t] = []);
  data.nodes.forEach(n => { if (nodesByType[n.type]) nodesByType[n.type].push(n); });

  const colW = 210, rowH = 54, padTop = 48, padSide = 16, bypassY = 38;
  const maxRows = Math.max(1, ...COLUMN_ORDER.map(t => nodesByType[t].length));
  const width = padSide * 2 + colW * COLUMN_ORDER.length;
  const height = padTop + rowH * maxRows + 20;

  const pos = {}; // node.id -> {x, y}
  const colIndex = {}; // node.id -> column index，跨列边判断要用
  COLUMN_ORDER.forEach((type, ci) => {
    const list = nodesByType[type];
    const colCenterX = padSide + colW * ci + colW / 2;
    list.forEach((n, ri) => {
      pos[n.id] = { x: colCenterX, y: padTop + rowH * ri + rowH / 2 };
      colIndex[n.id] = ci;
    });
  });

  let svg = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" style="overflow:visible;font-family:inherit;">`;
  // 列标题
  COLUMN_ORDER.forEach((type, ci) => {
    const x = padSide + colW * ci + colW / 2;
    svg += `<text x="${x}" y="16" font-size="11" font-weight="600" text-anchor="middle" fill="#64748b">${ENTITY_META[type].label}</text>`;
  });
  // 边（先画，压在节点下方）
  const structColor = '#cbd5e1', rateColor = '#2563eb';
  data.edges.forEach(e => {
    if (!e.from || !e.to || !pos[e.from] || !pos[e.to]) return;
    const a = pos[e.from], b = pos[e.to];
    const color = e.kind === 'rate' ? rateColor : structColor;
    const dash = e.kind === 'structural' ? ' stroke-dasharray="3,3"' : '';
    const colDiff = Math.abs((colIndex[e.from] ?? 0) - (colIndex[e.to] ?? 0));
    let midX, midY, pathD;
    if (colDiff > 1) {
      // 跨列：走上方旁路曲线，绕开中间列的节点行(所有行都在 padTop 之后，bypassY 在其上方留白处)
      midX = (a.x + b.x) / 2;
      midY = bypassY;
      pathD = `M${a.x},${a.y} C${a.x},${bypassY} ${b.x},${bypassY} ${b.x},${b.y}`;
      svg += `<path d="${pathD}" fill="none" stroke="${color}" stroke-width="1.4"${dash}><title>${e.label}</title></path>`;
    } else {
      midX = (a.x + b.x) / 2; midY = (a.y + b.y) / 2;
      svg += `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${color}" stroke-width="1.4"${dash}>
        <title>${e.label}</title></line>`;
    }
    svg += `<rect x="${midX - Math.min(e.label.length * 3.4, 60)}" y="${midY - 8}" width="${Math.min(e.label.length * 6.8, 120)}" height="14" fill="#fff" opacity="0.9"/>`;
    svg += `<text x="${midX}" y="${midY + 3}" font-size="9.5" text-anchor="middle" fill="${color}">${e.label.length > 18 ? e.label.slice(0, 17) + '…' : e.label}<title>${e.label}</title></text>`;
  });
  // 节点
  COLUMN_ORDER.forEach(type => {
    nodesByType[type].forEach(n => {
      const p = pos[n.id];
      const w = colW - 30;
      svg += `<rect x="${p.x - w / 2}" y="${p.y - 15}" width="${w}" height="30" rx="6" fill="#eff6ff" stroke="#bfdbfe"/>`;
      svg += `<text x="${p.x}" y="${p.y + 4}" font-size="11.5" text-anchor="middle" fill="#0f172a">${n.name}${n.is_dispatcher_alias ? ' <tspan fill="#f59e0b" font-size="9">(≈调货员本人)</tspan>' : ''}</text>`;
      if (n.badges && n.badges.length) {
        const badgeText = n.badges.map(b => `${b.label} ${b.rate_value}${b.unit}`).join(' / ');
        svg += `<text x="${p.x}" y="${p.y + 24}" font-size="9" text-anchor="middle" fill="#94a3b8">${badgeText.length > 26 ? badgeText.slice(0, 25) + '…' : badgeText}<title>${badgeText}</title></text>`;
      }
    });
  });
  const globalFallbacks = data.edges.filter(e => e.kind === 'global_fallback');
  svg += `</svg>`;
  container.innerHTML = svg + (globalFallbacks.length
    ? `<div style="margin-top:6px;font-size:11.5px;color:var(--gray-500);">全局兜底档（未挂到具体实体，找不到专属费率时按此兜底）：${globalFallbacks.map(e => e.label).join('；')}</div>`
    : '');
}

// ---------------- 列表 ----------------
// 2026-10-04 重构（用户要求）：
//   ① 失效的记录不再平铺 —— 按「生效期」分区，往期整组用抽屉折叠（点分组行展开/收起）；
//   ② 常驻 / 当期 / 未生效 / 往期 分开标记（属性列徽章）；
//   ③ 同类合并 —— 同一费率类型+同一对象+同一生效区间的一批记录（如「户头对外结算点位」按调货人
//      分档的 7~8 条）合并成 1 行，下面「展开明细」逐项列出，而不是每人一行。
function todayStr() { return new Date().toISOString().slice(0, 10); }

// 结算期口径：每月 26 日 ~ 次月 25 日（与引擎 period 完全一致）
function periodKeyOf(dateStr) {
  if (!dateStr) return null;
  const d = new Date(dateStr + 'T00:00:00');
  let y = d.getFullYear(), m = d.getMonth() + 1;
  if (d.getDate() >= 26) { m += 1; if (m > 12) { m = 1; y += 1; } }
  return `${y}-${String(m).padStart(2, '0')}期`;
}

function attrOf(r) {
  const t = todayStr();
  if (r.effective_to && r.effective_to < t) return '往期';
  if (r.effective_from > t) return '未生效';
  return r.effective_to ? '当期' : '常驻';
}
const ATTR_CLS = { '常驻': 'green', '当期': 'blue', '未生效': 'gray', '往期': 'gray' };

function rowStatus(r) { const a = attrOf(r); return { text: a, cls: ATTR_CLS[a] }; }

const SEC_OPEN = {};   // 分区抽屉展开状态（key → bool）
const GRP_OPEN = {};   // 同类合并行的「逐项明细」展开状态

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function groupKeyOf(r) {
  return [r.relation_type, r.primary_entity_type, r.primary_entity_id,
          r.effective_from, r.effective_to].join('~');
}

function toggleSection(key) { SEC_OPEN[key] = !SEC_OPEN[key]; renderTable(); }
function toggleGroup(key) { GRP_OPEN[key] = !GRP_OPEN[key]; renderTable(); }

function renderTableBodyForRows(rows) {
  const groups = new Map();
  rows.forEach(r => {
    const k = groupKeyOf(r);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(r);
  });
  return [...groups.values()]
    .sort((a, b) => a[0].relation_type.localeCompare(b[0].relation_type) ||
                    (a[0].primary_entity_id || 0) - (b[0].primary_entity_id || 0) ||
                    (a[0].secondary_entity_id || 0) - (b[0].secondary_entity_id || 0))
    .map(g => groupHtml(g)).join('');
}

function renderTable() {
  const table = document.getElementById('rate-table');
  const curLabel = currentPeriodLabel();
  const mills = PERIOD_LAYOUT?.mills || [{ id: 0, name: '全局 / 跨厂' }];

  const byMill = new Map();
  RATE_ROWS.forEach(r => {
    const mid = millBucketForRow(r);
    const pk = periodKeyOf(r.effective_from) || '未定';
    if (!byMill.has(mid)) byMill.set(mid, new Map());
    const pm = byMill.get(mid);
    if (!pm.has(pk)) pm.set(pk, []);
    pm.get(pk).push(r);
  });

  let body = '';
  mills.forEach(m => {
    const mid = m.id;
    const periods = byMill.get(mid);
    if (!periods || !periods.size) return;
    if (MILL_OPEN[mid] === undefined) MILL_OPEN[mid] = true;
    const total = [...periods.values()].reduce((n, arr) => n + arr.length, 0);
    const millOpen = MILL_OPEN[mid];
    body += `<tr class="rate-mill-row" onclick="toggleMill(${mid})">
      <td colspan="8">
        <span style="display:inline-block;width:14px;">${millOpen ? '▾' : '▸'}</span>
        <strong>${esc(m.name)}</strong>
        <span class="muted">（${total} 条）</span>
      </td></tr>`;
    if (!millOpen) return;
    [...periods.keys()].sort((a, b) => b.localeCompare(a)).forEach(pk => {
      const secKey = sectionKey(mid, pk);
      const isCurrent = pk === curLabel;
      if (SEC_OPEN[secKey] === undefined) SEC_OPEN[secKey] = isCurrent;
      if (RATE_FOCUS && periods.get(pk).some(r => rowMatchesFocus(r, RATE_FOCUS))) SEC_OPEN[secKey] = true;
      const open = SEC_OPEN[secKey];
      const rows = periods.get(pk);
      const periodTag = isCurrent
        ? '<span class="tag-blue">当前结算期</span>'
        : '<span class="tag-gray">已封账</span>';
      body += `<tr class="rate-section-row" onclick="event.stopPropagation(); toggleSection('${secKey}')">
        <td colspan="8">
          <span style="display:inline-block;width:14px;margin-left:12px;">${open ? '▾' : '▸'}</span>
          ${esc(pk)} · ${periodTag}
          <span style="color:var(--gray-400);font-weight:400;">共 ${rows.length} 条${!isCurrent && !open ? ' · 已折叠' : ''}</span>
        </td></tr>`;
      if (open) body += renderTableBodyForRows(rows);
    });
  });

  if (!body) body = `<tr><td colspan="8" class="empty-hint">暂无数据</td></tr>`;

  table.innerHTML = `<thead><tr>
      <th>费率/关系类型</th><th>对象</th><th>细分维度</th><th class="nowrap">费率</th>
      <th class="nowrap">生效区间</th><th class="nowrap">属性</th><th>备注</th><th class="nowrap">操作</th>
    </tr></thead><tbody>${body}</tbody>`;
  updateRateFocusBanner();
}

function groupHtml(g) {
  const r0 = g[0];
  const cat = CATALOG.find(c => c.code === r0.relation_type);
  const st = rowStatus(r0);
  const gk = groupKeyOf(r0);
  const open = !!GRP_OPEN[gk];
  const focused = groupMatchesFocus(g);
  const primaryName = entityName(r0.primary_entity_type, r0.primary_entity_id) || '(全局兜底)';
  // 细分维度类型优先取记录自身的 secondary_entity_type（模板层加的维度可能不在费率目录里），再回落目录
  const secType = r0.secondary_entity_type || (cat && cat.secondary_type);
  const vals = [...new Set(g.map(x => `${x.rate_value}${unitLabel(x.rate_unit)}`))];
  const dimCell = secType
    ? `${ENTITY_META[secType].label} ${g.length} 项${g.length > 1 ? '（分档）' : ''}`
    : '不分';
  const rateCell = vals.length === 1 ? vals[0] : `分档（${vals.length} 档）`;
  let html = `<tr class="rate-merged-row${focused ? ' rate-row-highlight' : ''}"${focused ? ' id="rate-focus-row"' : ''}>
      <td class="nowrap">${esc(cat ? cat.label : r0.relation_type)}${g.length > 1 ? ` <span class="tag">${g.length} 条</span>` : ''}</td>
      <td class="nowrap">${esc(primaryName)}</td>
      <td class="nowrap">${esc(dimCell)}</td>
      <td class="num">${esc(rateCell)}</td>
      <td class="nowrap">${r0.effective_from} ~ ${r0.effective_to || '至今'}</td>
      <td class="nowrap"><span class="tag-${st.cls}">${st.text}</span></td>
      <td class="notes-cell"><div class="clamp2" title="${esc(r0.notes)}">${esc(r0.notes) || '-'}</div></td>
      <td class="nowrap">
        ${g.length > 1 ? `<button class="btn-sm" onclick="toggleGroup('${gk}')">${open ? '收起明细' : '展开明细'}</button>` : ''}
        <button class="btn-sm" onclick="editRow(${r0.id})">编辑</button>
        <button class="btn-sm" onclick="continueRow(${r0.id})">新增后续费率</button>
        <button class="btn-sm btn-danger" onclick="deleteRow(${r0.id})">删除</button>
      </td>
    </tr>`;
  if (g.length > 1) {
    html += g.slice().sort((a, b) => (a.secondary_entity_id || 0) - (b.secondary_entity_id || 0)).map(x => {
      const nm = entityName(x.secondary_entity_type, x.secondary_entity_id) || '不分';
      const xs = rowStatus(x);
      return `<tr class="rate-subrow" style="${open ? '' : 'display:none;'}">
        <td></td>
        <td class="nowrap" style="color:var(--gray-400);">└ 逐项</td>
        <td class="nowrap">${esc(nm)}</td>
        <td class="num">${x.rate_value}${unitLabel(x.rate_unit)}</td>
        <td class="nowrap">${x.effective_from} ~ ${x.effective_to || '至今'}</td>
        <td class="nowrap"><span class="tag-${xs.cls}">${xs.text}</span></td>
        <td class="notes-cell"><div class="clamp2" title="${esc(x.notes)}">${esc(x.notes) || '-'}</div></td>
        <td class="nowrap">
          <button class="btn-sm" onclick="editRow(${x.id})">编辑</button>
          <button class="btn-sm" onclick="continueRow(${x.id})">新增后续</button>
          <button class="btn-sm btn-danger" onclick="deleteRow(${x.id})">删除</button>
        </td>
      </tr>`;
    }).join('');
  }
  return html;
}

function scrollToRateFocusRow() {
  if (!RATE_FOCUS) return;
  requestAnimationFrame(() => {
    document.getElementById('rate-focus-row')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });
}

// ---------------- 表单 ----------------
function entitySelect(id, type, selectedId, allowEmpty, emptyLabel) {
  if (!type) return `<select id="${id}" disabled><option>-</option></select>`;
  const meta = ENTITY_META[type];
  const list = ARCH[meta.source] || [];
  const emptyOpt = `<option value="">${emptyLabel || '-'}</option>`;
  return `<select id="${id}">${allowEmpty !== false ? emptyOpt : ''}${list.map(x =>
    `<option value="${x.id}" ${String(x.id) === String(selectedId) ? 'selected' : ''}>${x.name}</option>`
  ).join('')}</select>`;
}

// 多选：一次为勾选的多个对象各生成/续接一条费率（用户要求「下面可以选择对哪些调货人生效」）
function entityChecklist(type, selectedId) {
  const meta = ENTITY_META[type];
  const list = ARCH[meta.source] || [];
  return `<div class="rate-checklist">` + list.map(x =>
    `<label class="rate-checklist-item"><input type="checkbox" class="rf-multi-cb" value="${x.id}" ` +
    `${String(x.id) === String(selectedId) ? 'checked' : ''}> ${x.name}</label>`
  ).join('') + `</div>
    <div style="font-size:11px;color:var(--gray-500);margin-top:4px;">勾选多个 = 一次为这些对象各新建/续接一条费率；全不勾 = 不区分该维度（一条通用档）。</div>`;
}

function refreshDependentFields(preset, isEdit) {
  const code = document.getElementById('rf_relation_type').value;
  const cat = CATALOG.find(c => c.code === code);
  const primaryBox = document.getElementById('rf_primary_box');
  const secondaryBox = document.getElementById('rf_secondary_box');
  if (!cat) { primaryBox.innerHTML = ''; secondaryBox.innerHTML = ''; return; }
  primaryBox.innerHTML = `<label>${ENTITY_META[cat.primary_type].label}${cat.primary_type === 'invoice_source' ? '（留空=全局兜底，找不到专属票源费率时用这条）' : ''}</label>` +
    entitySelect('rf_primary_id', cat.primary_type, preset ? preset.primary_entity_id : '', true, '(全局兜底)');
  if (cat.secondary_type && !isEdit) {
    secondaryBox.innerHTML = `<label>${ENTITY_META[cat.secondary_type].label}（可多选）</label>` +
      entityChecklist(cat.secondary_type, preset ? preset.secondary_entity_id : '');
  } else if (cat.secondary_type) {
    secondaryBox.innerHTML = `<label>${ENTITY_META[cat.secondary_type].label}（可选，留空=不区分该维度）</label>` +
      entitySelect('rf_secondary_id', cat.secondary_type, preset ? preset.secondary_entity_id : '', true, '(不区分)');
  } else {
    secondaryBox.innerHTML = '';
  }
  document.getElementById('rf_rate_unit').value = preset ? preset.rate_unit : cat.rate_unit;
}

function showForm(row, presetForContinue) {
  editingId = row ? row.id : null;
  const preset = row || presetForContinue || {};
  document.getElementById('rate-form-box').innerHTML = `
    <div class="card" style="background:var(--blue-light);border-color:#bfdbfe;margin-bottom:12px;">
      <div class="field-row" style="grid-template-columns:repeat(auto-fit, minmax(200px, 1fr));">
        <div class="field"><label>费率/关系类型 *</label>
          <select id="rf_relation_type" ${row ? 'disabled' : ''} onchange="refreshDependentFields()">
            ${CATALOG.map(c => `<option value="${c.code}" ${c.code === preset.relation_type ? 'selected' : ''}>${c.label}</option>`).join('')}
          </select>
        </div>
        <div class="field" id="rf_primary_box"></div>
        <div class="field" id="rf_secondary_box"></div>
        <div class="field"><label>费率数值 *</label><input id="rf_rate_value" type="number" step="0.01" value="${preset.rate_value ?? ''}"></div>
        <div class="field"><label>单位</label><select id="rf_rate_unit"><option value="per_ton">元/吨</option><option value="pct">%</option></select></div>
        <div class="field"><label>生效起始日 *</label><input id="rf_effective_from" type="date" value="${row ? row.effective_from : (presetForContinue ? '' : new Date().toISOString().slice(0,10))}"></div>
        <div class="field"><label>备注</label><input id="rf_notes" type="text" value="${preset.notes || ''}"></div>
      </div>
      <div style="font-size:11.5px;color:var(--gray-500);margin-bottom:8px;">
        ${row ? '直接修改这条记录本身（用于改正录入错误，不会自动调整其它记录的生效区间）。' : '新增一条生效记录：如果同一对象+维度已有"至今生效"的旧记录，会自动把旧记录截止到新记录生效日前一天。'}
      </div>
      <button class="btn-primary" onclick="saveForm()">保存</button>
      <button onclick="document.getElementById('rate-form-box').innerHTML=''">取消</button>
    </div>`;
  refreshDependentFields(row, !!row);
}

async function saveForm() {
  const cat = CATALOG.find(c => c.code === document.getElementById('rf_relation_type').value);
  const base = {
    relation_type: document.getElementById('rf_relation_type').value,
    rate_value: document.getElementById('rf_rate_value').value,
    rate_unit: document.getElementById('rf_rate_unit').value,
    effective_from: document.getElementById('rf_effective_from').value,
    notes: document.getElementById('rf_notes').value || null,
    primary_entity_type: cat.primary_type,
  };
  const primaryNode = document.getElementById('rf_primary_id');
  base.primary_entity_id = primaryNode && primaryNode.value ? Number(primaryNode.value) : null;
  if (!base.rate_value || !base.effective_from) { alert('请填写费率数值和生效起始日'); return; }

  if (editingId) {
    // 表单未渲染细分维度控件时，不提交这两个字段，避免静默清空已有维度（PUT 只更新提交的列）
    const secondaryNode = document.getElementById('rf_secondary_id');
    if (secondaryNode) {
      base.secondary_entity_id = secondaryNode.value ? Number(secondaryNode.value) : null;
      base.secondary_entity_type = base.secondary_entity_id ? cat.secondary_type : null;
    }
    await apiPut(`/api/rate_links/${editingId}`, base);
  } else {
    const boxes = [...document.querySelectorAll('.rf-multi-cb')];
    if (boxes.length) {
      const ids = boxes.filter(b => b.checked).map(b => Number(b.value));
      if (!ids.length) {
        await apiPost('/api/rate_links/upsert', { ...base, secondary_entity_id: null, secondary_entity_type: null });
      } else {
        for (const id of ids) {
          await apiPost('/api/rate_links/upsert', { ...base, secondary_entity_id: id, secondary_entity_type: cat.secondary_type });
        }
      }
    } else {
      const secondaryNode = document.getElementById('rf_secondary_id');
      base.secondary_entity_id = secondaryNode && secondaryNode.value ? Number(secondaryNode.value) : null;
      base.secondary_entity_type = base.secondary_entity_id ? cat.secondary_type : null;
      await apiPost('/api/rate_links/upsert', base);
    }
  }
  RATE_ROWS = await apiGet('/api/rate_links');
  document.getElementById('rate-form-box').innerHTML = '';
  await loadPeriodLayout();
  renderTable();
  renderGraph();
}

function editRow(id) {
  showForm(RATE_ROWS.find(r => r.id === id));
}
function continueRow(id) {
  const r = RATE_ROWS.find(r => r.id === id);
  showForm(null, r);
}
async function deleteRow(id) {
  if (!confirm('确认删除该条记录？')) return;
  await apiDelete(`/api/rate_links/${id}`);
  await refreshRateData();
}

async function refreshRateData() {
  RATE_ROWS = await apiGet('/api/rate_links');
  await loadPeriodLayout();
  renderTable();
  renderGraph();
}

function dismissOpenPeriodBanner() {
  if (PERIOD_LAYOUT?.period_start) {
    sessionStorage.setItem('rate_open_dismiss_' + PERIOD_LAYOUT.period_start, '1');
  }
  updateOpenPeriodBanner();
}

function updateOpenPeriodBanner() {
  const box = document.getElementById('rate-open-period-box');
  const btn = document.getElementById('btn-open-period');
  if (!PERIOD_LAYOUT?.needs_open_period) {
    if (box) { box.style.display = 'none'; box.innerHTML = ''; }
    if (btn) btn.hidden = true;
    return;
  }
  if (btn) btn.hidden = false;
  if (!box) return;
  const dismissed = PERIOD_LAYOUT.period_start &&
    sessionStorage.getItem('rate_open_dismiss_' + PERIOD_LAYOUT.period_start);
  if (dismissed) {
    box.style.display = 'none';
    return;
  }
  box.style.display = 'block';
  box.innerHTML = `
    <div class="panel-title-row">
      <div class="panel-title">开 ${esc(PERIOD_LAYOUT.period_label)} 费率包</div>
    </div>
    <p class="muted" style="font-size:13px;margin:0 0 10px;">
      本期还没有以 <b>${esc(PERIOD_LAYOUT.period_start)}</b> 起算的新费率行；可从
      <b>${esc(PERIOD_LAYOUT.prev_period_label)}</b> 仍生效的组合里勾选继承（票源分期请到
      <a href="/rules">规则 hub · 本期改价向导</a>）。
    </p>
    <button type="button" class="btn-primary" onclick="openOpenPeriodDialog()">选择要继承的费率…</button>
    <button type="button" class="btn-sm" onclick="dismissOpenPeriodBanner()">稍后提醒</button>`;
}

async function openOpenPeriodDialog(force) {
  const asOf = (typeof getRulesAsOf === 'function' && getRulesAsOf()) || todayStr();
  OPEN_PERIOD_SHEET = await apiGet('/api/rate_links/open_period/sheet?as_of=' + encodeURIComponent(asOf));
  const items = OPEN_PERIOD_SHEET.items || [];
  if (!items.length) {
    alert('没有可继承的费率组合（上一日无生效档位）。');
    return;
  }
  const bySec = new Map();
  items.forEach((it, idx) => {
    const t = it.section_title || '费率';
    if (!bySec.has(t)) bySec.set(t, []);
    bySec.get(t).push({ ...it, _idx: idx });
  });
  let blocks = '';
  bySec.forEach((list, title) => {
    blocks += `<div class="panel-title" style="font-size:13px;margin:12px 0 6px;">${esc(title)}</div>
      <div class="rate-checklist">${list.map(it => {
        const chk = it.apply_default ? 'checked' : '';
        return `<label class="rate-checklist-item"><input type="checkbox" class="op-inherit-cb" data-idx="${it._idx}" ${chk}>
          ${esc(it.label)} · 当前 ${fmtNum(it.current_rate, 2)}</label>`;
      }).join('')}</div>`;
  });
  document.getElementById('rate-form-box').innerHTML = `
    <div class="card" style="background:#fff;border-color:var(--gray-200);margin-bottom:12px;">
      <div class="panel-title">继承到 ${esc(OPEN_PERIOD_SHEET.period_label)}（自 ${esc(OPEN_PERIOD_SHEET.period_start)} 起）</div>
      ${blocks}
      <div class="toolbar" style="margin-top:12px;">
        <button class="btn-primary" onclick="applyOpenPeriodInherit()">确认写入勾选项</button>
        <button onclick="document.getElementById('rate-form-box').innerHTML=''">取消</button>
      </div>
    </div>`;
  if (force) document.getElementById('rate-form-box').scrollIntoView({ behavior: 'smooth' });
}

async function applyOpenPeriodInherit() {
  if (!OPEN_PERIOD_SHEET) return;
  const picked = [];
  document.querySelectorAll('.op-inherit-cb').forEach(cb => {
    const idx = +cb.dataset.idx;
    const it = { ...OPEN_PERIOD_SHEET.items[idx], apply: cb.checked };
    picked.push(it);
  });
  const n = picked.filter(x => x.apply).length;
  if (!n) { alert('请至少勾选一条'); return; }
  if (!confirm(`确认继承 ${n} 条费率到 ${OPEN_PERIOD_SHEET.period_start} 起？`)) return;
  await apiPost('/api/rate_links/open_period/apply', {
    period_start: OPEN_PERIOD_SHEET.period_start,
    items: picked,
  });
  toast(`已写入 ${n} 条`);
  document.getElementById('rate-form-box').innerHTML = '';
  if (PERIOD_LAYOUT?.period_start) {
    sessionStorage.removeItem('rate_open_dismiss_' + PERIOD_LAYOUT.period_start);
  }
  await refreshRateData();
}

document.getElementById('btn-add-rate').addEventListener('click', () => showForm(null));
document.getElementById('btn-open-period')?.addEventListener('click', () => openOpenPeriodDialog(true));

RATE_FOCUS = parseRateFocusFromUrl();
loadAll().then(() => { renderTable(); scrollToRateFocusRow(); renderGraph(); });
document.addEventListener('rules-as-of-changed', async () => {
  await loadPeriodLayout();
  renderTable();
  renderGraph();
});
