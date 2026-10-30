let ARCH = {};
let currentTable = 'steel_mills';
let editingId = null;

const TABS = [
  { table: 'steel_mills', label: '钢厂' },
  { table: 'mill_reward_tiers', label: '钢厂量补规则' },
  { table: 'accounts', label: '户头' },
  { table: 'financiers', label: '国企/垫资方' },
  { table: 'processing_bases', label: '加工基地' },
  { table: 'base_accounts', label: '供货厂收款账户' },
  { table: 'dispatchers', label: '调货人' },
  { table: 'invoice_sources', label: '前端公司/反向自然人' },
  { table: 'invoice_source_terms', label: '票源额度分期' },
  { table: 'vehicles', label: '车辆/船舶' },
  { table: 'drivers', label: '司机' },
  { table: 'scrap_types', label: '货型字典' },
];

// 把日期换算成结算期标签（26 日 ~ 次月 25 日，与引擎/首页同口径）
function periodOfDate(d) {
  const p = String(d || '').slice(0, 10).split('-').map(Number);
  if (!p[0] || !p[1] || !p[2]) return '';
  const yy = (p[1] === 12 && p[2] >= 26) ? p[0] + 1 : p[0];
  const mm = (p[2] >= 26) ? (p[1] === 12 ? 1 : p[1] + 1) : p[1];
  return `${yy}-${String(mm).padStart(2, '0')}期`;
}
function termPeriodCn(r) {
  const a = periodOfDate(r.effective_from);
  if (!a) return '-';
  if (!r.effective_to) return `${a} 起（长期）`;
  const b = periodOfDate(r.effective_to);
  return a === b ? a : `${a} ~ ${b}`;
}

const CONFIGS = {
  steel_mills: [
    { key: 'name', label: '钢厂名称', type: 'text', required: true },
    { key: 'is_whitelist', label: '国家白名单', type: 'checkbox' },
    { key: 'settlement_terms', label: '结算账期规则', type: 'text', placeholder: '如：货到45天 / 月结30天' },
    { key: 'quality_contact', label: '质检部联系方式', type: 'text' },
    { key: 'entry_requirement', label: '进场要求备注', type: 'text', placeholder: '如：仅支持国六标准' },
    { key: 'notes', label: '备注', type: 'textarea' },
  ],
  mill_reward_tiers: [
    { key: 'mill_id', label: '钢厂', type: 'select', source: 'steel_mills', required: true },
    { key: 'min_tonnage', label: '起始吨位(达到即整周期享受)', type: 'number', required: true },
    { key: 'unit_price', label: '奖励单价(元/吨)', type: 'number', required: true },
    { key: 'period_start_day', label: '周期起始日', type: 'number', placeholder: '默认26' },
    { key: 'period_end_day', label: '周期截止日(次月)', type: 'number', placeholder: '默认25' },
    { key: 'notes', label: '备注', type: 'text' },
  ],
  accounts: [
    { key: 'name', label: '户头名称', type: 'text', required: true },
    { key: 'mill_id', label: '所属钢厂', type: 'select', source: 'steel_mills', required: true },
    { key: 'financier_id', label: '垫资方(国企，可空)', type: 'select', source: 'financiers' },
    { key: 'is_our_own', label: '我方专属户头', type: 'checkbox' },
    { key: 'advance_fee_rate', label: '垫资费率(元/吨)', type: 'number', placeholder: '默认23' },
    { key: 'external_settlement_rate', label: '对外结算点位(%)', type: 'number', placeholder: '如14.0' },
    { key: 'notes', label: '备注', type: 'text' },
  ],
  financiers: [
    { key: 'name', label: '国企/垫资方名称', type: 'text', required: true },
    { key: 'credit_info', label: '授信信息', type: 'text' },
    { key: 'notes', label: '备注', type: 'textarea' },
  ],
  base_accounts: [
    { key: 'base_id', label: '供货厂', type: 'select', source: 'processing_bases', required: true },
    { key: 'account_name', label: '户名', type: 'text', required: true },
    { key: 'bank', label: '开户行', type: 'text' },
    { key: 'card_tail', label: '卡号后四位', type: 'text', placeholder: '只记后四位，不存完整卡号' },
    { key: 'holder_type', label: '账户类型', type: 'select', options: [['owner','老板本人'],['designated','老板指定的其他卡'],['reverse_personal','反向个人户']] },
    { key: 'is_active', label: '在用', type: 'checkbox' },
    { key: 'notes', label: '备注', type: 'text', placeholder: '如：银行限额临时用这张' },
  ],
  processing_bases: [
    { key: 'name', label: '基地名称', type: 'text', required: true },
    { key: 'base_type', label: '类型', type: 'select', options: [['own','我方'],['external','外部合作']] },
    { key: 'admission_level', label: '准入分类', type: 'select', options: [['national','国家准入基地'],['normal','普通基地']] },
    { key: 'risk_approved', label: '国企风控直签准入', type: 'checkbox' },
    { key: 'default_mode_code', label: '默认结算模式', type: 'select', source: 'trip_modes' },
    { key: 'service_fee_type', label: '服务费口径', type: 'select', options: [['per_ton','元/吨'],['rate','结算金额比例%']] },
    { key: 'service_fee_value', label: '服务费数值', type: 'number' },
    { key: 'contact', label: '联系方式', type: 'text' },
    { key: 'notes', label: '备注', type: 'textarea' },
  ],
  dispatchers: [
    { key: 'name', label: '调货人姓名', type: 'text', required: true },
    { key: 'phone', label: '联系电话', type: 'text' },
    { key: 'commission_per_ton', label: '提成单价(元/吨)', type: 'number' },
    { key: 'notes', label: '备注', type: 'text' },
  ],
  invoice_sources: [
    { key: 'name', label: '名称', type: 'text', required: true },
    { key: 'source_type', label: '类型', type: 'select', options: [['front_company','前端公司'],['reverse_individual','反向自然人']] },
    { key: 'tax_type', label: '票种', type: 'text', placeholder: '3%简易征收/13%一般纳税人/反向' },
    { key: 'rate_point', label: '点位(%)', type: 'number', placeholder: '如12.8' },
    { key: 'quota_period', label: '额度周期', type: 'select', options: [['monthly','月度'],['annual','年度']] },
    { key: 'quota_amount', label: '额度上限', type: 'number', placeholder: '反向自然人默认2100000' },
    { key: 'warn_threshold', label: '预警阈值', type: 'number', placeholder: '反向自然人默认1800000' },
    { key: 'bound_mill_id', label: '绑定钢厂(反向自然人)', type: 'select', source: 'steel_mills' },
    { key: 'bound_base_id', label: '绑定供货厂(反向自然人)', type: 'select', source: 'processing_bases' },
    { key: 'contact', label: '联系方式', type: 'text' },
    { key: 'notes', label: '备注', type: 'textarea' },
  ],
  // 票源额度分期：一条记录 = 一段生效区间内该票源方的额度与是否接单。
  // 首页「票源额度水位」只认"今天"命中的那一条；没有命中记录就回退上面档案里的默认值。
  invoice_source_terms: [
    { key: 'invoice_source_id', label: '票源方', type: 'select', source: 'invoice_sources', required: true },
    { key: 'effective_from', label: '生效起(含)', type: 'date', required: true },
    { key: 'effective_to', label: '生效止(含·空=至今)', type: 'date' },
    { key: 'quota_amount', label: '额度上限(空=本期无票额)', type: 'number' },
    { key: 'warn_threshold', label: '预警线(空=沿用档案)', type: 'number' },
    { key: 'rate_point', label: '票点成本点位%(空=沿用档案)', type: 'number', placeholder: '如13.5；只影响这段生效区间的车次' },
    { key: 'outbound_rate_point', label: '对外转出点位%(空=沿用档案)', type: 'number', placeholder: '仅"经过我们"票源方(pass_through)用' },
    { key: 'accepting', label: '接单中(取消勾选=停止接单)', type: 'checkbox', default: 1 },
    { key: 'notes', label: '备注', type: 'textarea' },
    { key: 'period_cn', label: '涵盖期', type: 'computed', render: row => termPeriodCn(row) },
  ],
  vehicles: [
    { key: 'plate_no', label: '车牌号/船号', type: 'text', required: true },
    { key: 'transport_type', label: '类型', type: 'select', options: [['truck','车'],['ship','船']] },
    { key: 'default_driver_id', label: '常用司机', type: 'select', source: 'drivers' },
    { key: 'is_first_trailer_checked', label: '半挂车环保清单已核验', type: 'checkbox' },
    { key: 'notes', label: '备注', type: 'text' },
  ],
  drivers: [
    { key: 'name', label: '姓名', type: 'text', required: true },
    { key: 'phone', label: '电话', type: 'text' },
    { key: 'id_card_no', label: '身份证号', type: 'text' },
    { key: 'license_expiry', label: '驾驶证有效期', type: 'date' },
    { key: 'notes', label: '备注', type: 'text' },
  ],
  scrap_types: [
    { key: 'category', label: '废钢类型(大类)', type: 'text', required: true, placeholder: '如：废钢 / 铁水罐压块类 / 废钢压块类 / 破碎料类' },
    { key: 'name', label: '货型名称', type: 'text', required: true, placeholder: '如：小废 / 中废1' },
    { key: 'example_desc', label: '品种典型举例', type: 'text' },
    { key: 'size_requirement', label: '外形尺寸要求', type: 'text' },
    { key: 'thickness_requirement', label: '厚度要求', type: 'text' },
  ],
};

async function loadAll() {
  const names = ['steel_mills','financiers','accounts','processing_bases','base_accounts','dispatchers','invoice_sources','invoice_source_terms','vehicles','drivers','scrap_types','trip_modes','mill_reward_tiers'];
  const results = await Promise.all(names.map(n => apiGet('/api/' + n)));
  names.forEach((n, i) => ARCH[n] = results[i]);
}

function renderTabs() {
  document.getElementById('archive-tabs').innerHTML = TABS.map(t =>
    `<button class="${t.table===currentTable?'active':''}" onclick="switchTab('${t.table}')">${t.label}</button>`
  ).join('');
}
function switchTab(table) {
  currentTable = table;
  editingId = null;
  document.getElementById('archive-form-box').innerHTML = '';
  renderTabs();
  renderTable();
}

function labelFor(field, row) {
  if (field.type === 'computed' && field.render) return field.render(row);
  if (field.type === 'select' && field.source) {
    const list = ARCH[field.source] || [];
    const item = list.find(x => String(x.id) === String(row[field.key]));
    return item ? (item.name || item.plate_no) : '-';
  }
  if (field.type === 'select' && field.options) {
    const opt = field.options.find(o => o[0] === row[field.key]);
    return opt ? opt[1] : (row[field.key] || '-');
  }
  if (field.type === 'checkbox') return row[field.key] ? '是' : '否';
  return row[field.key] ?? '-';
}

// 枚举/勾选/数字/日期类字段的展示值都很短，禁止自动换行，避免和"备注"这类长文本
// 字段挤在一起时把短字段也撑成多行，看着凌乱
const NOWRAP_TYPES = ['select', 'checkbox', 'number', 'date'];

// 内链：档案里的钢厂/供货厂/调货人/票源方/车辆，一键看它的全部车次
const TRIP_LINK_KEY = { steel_mills: 'mill_id', processing_bases: 'base_id', dispatchers: 'dispatcher_id', invoice_sources: 'invoice_source_id' };
function tripsCell(r) {
  if (TRIP_LINK_KEY[currentTable]) return drill('车次 ›', { [TRIP_LINK_KEY[currentTable]]: r.id, exclude_voided: 1 }, `基础档案·${r.name}`);
  if (currentTable === 'vehicles') return `<a class="drill" href="/trips?q=${encodeURIComponent(r.plate_no)}&from=${encodeURIComponent('基础档案·' + r.plate_no)}">车次 ›</a>`;
  return '';
}
// 每个档案页顶部的一句说明（只读提示，帮助理解时效性字段怎么用）
const TAB_NOTES = {
  invoice_sources: '下面「额度上限 / 预警线」是<b>没有单独设分期时</b>的默认值；票额按月给、这个月有没有、要不要继续接单，都去「票源额度分期」页按生效日期维护，首页水位按"今天"命中的那条算。',
  invoice_source_terms: '一条记录 = 一段生效区间内该票源方的额度、接单状态与<b>票点成本</b>。<b>额度留空</b>=这段时期没有票额（不计入水位）；<b>取消"接单中"</b>=已停止接单（同样不计入）。<b>票点留空</b>=沿用票源方档案里的点位（推荐只给"这一期变了"的票源方填，改历史不会反过来动到别的期）。生效止留空表示长期有效。',
};

function renderTable() {
  const cfg = CONFIGS[currentTable];
  const rows = ARCH[currentTable] || [];
  const note = document.getElementById('archive-note');
  if (note) note.innerHTML = TAB_NOTES[currentTable] || '';
  const table = document.getElementById('archive-table');
  table.innerHTML = `<thead><tr>${cfg.map(f=>`<th class="${NOWRAP_TYPES.includes(f.type)?'nowrap':''}">${f.label}</th>`).join('')}<th class="nowrap">操作</th></tr></thead>
    <tbody>${rows.length ? rows.map(r => `<tr>
        ${cfg.map(f=>`<td class="${NOWRAP_TYPES.includes(f.type)?'nowrap':''}">${labelFor(f,r)}</td>`).join('')}
        <td class="nowrap">${tripsCell(r)} <button class="btn-sm" onclick="editRow(${r.id})">编辑</button> <button class="btn-sm btn-danger" onclick="deleteRow(${r.id})">删除</button></td>
      </tr>`).join('') : `<tr><td colspan="${cfg.length+1}" class="empty-hint">暂无数据</td></tr>`}
    </tbody>`;
}

function fieldInput(field, value) {
  const id = 'af_' + field.key;
  if (field.type === 'select') {
    const opts = field.source ? (ARCH[field.source]||[]).map(x => [x.id, x.name || x.plate_no || x.code]) : field.options;
    const emptyOpt = field.required ? '' : '<option value="">-</option>';
    return `<select id="${id}">${emptyOpt}${opts.map(([v,l]) => `<option value="${v}" ${String(v)===String(value)?'selected':''}>${l}</option>`).join('')}</select>`;
  }
  if (field.type === 'checkbox') {
    return `<input type="checkbox" id="${id}" ${value?'checked':''}>`;
  }
  if (field.type === 'textarea') {
    return `<textarea id="${id}" rows="2" placeholder="${field.placeholder||''}">${value||''}</textarea>`;
  }
  return `<input id="${id}" type="${field.type}" placeholder="${field.placeholder||''}" value="${value ?? ''}">`;
}

function showForm(row) {
  const cfg = CONFIGS[currentTable];
  // 计算列（如"涵盖期"）只用于列表展示，不进表单
  const fields = cfg.filter(f => f.type !== 'computed');
  editingId = row ? row.id : null;
  document.getElementById('archive-form-box').innerHTML = `
    <div class="card" style="background:var(--blue-light);border-color:#bfdbfe;margin-bottom:12px;">
      <div class="field-row" style="grid-template-columns:repeat(auto-fit, minmax(200px, 1fr));">
        ${fields.map(f => {
          const v = row ? row[f.key] : (f.default !== undefined ? f.default : '');
          return `<div class="field"><label>${f.label}${f.required?' *':''}</label>${fieldInput(f, v)}</div>`;
        }).join('')}
      </div>
      <button class="btn-primary" onclick="saveForm()">保存</button>
      <button onclick="document.getElementById('archive-form-box').innerHTML=''">取消</button>
    </div>`;
}

async function saveForm() {
  const cfg = CONFIGS[currentTable];
  const data = {};
  for (const f of cfg) {
    if (f.type === 'computed') continue;
    const node = document.getElementById('af_' + f.key);
    if (f.type === 'checkbox') data[f.key] = node.checked ? 1 : 0;
    else data[f.key] = node.value === '' ? null : node.value;
  }
  if (editingId) {
    await apiPut(`/api/${currentTable}/${editingId}`, data);
  } else {
    await apiPost(`/api/${currentTable}`, data);
  }
  await loadAll();
  document.getElementById('archive-form-box').innerHTML = '';
  renderTable();
}

function editRow(id) {
  const row = (ARCH[currentTable]||[]).find(r => r.id === id);
  showForm(row);
}
async function deleteRow(id) {
  if (!confirm('确认删除该条记录？')) return;
  await apiDelete(`/api/${currentTable}/${id}`);
  await loadAll();
  renderTable();
}

document.getElementById('btn-add').addEventListener('click', () => showForm(null));

// 其它页面链过来：/archives?tab=invoice_sources 直接打开对应档案
{ const tab = new URLSearchParams(location.search).get('tab'); if (tab && TABS.some(t => t.table === tab)) currentTable = tab; }
loadAll().then(() => { renderTabs(); renderTable(); });
