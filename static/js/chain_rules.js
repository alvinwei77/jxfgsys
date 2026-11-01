// 链条规则页面：flow_templates(链条规则元信息) + template_flow_edges(可编辑的流程环节，
// 驱动关系图) + template_nodes(实际计算节点，本页只开放label/role_type/doc_types编辑，
// 公式本身不在这里改，避免打乱计算顺序)。
//
// 2026-09-19 交互修复（用户反馈"编辑链条规则时页面老是上下挪动/整块重新渲染"）：
// 初版每次保存、切视图、勾启用都调 renderTemplateCard() 把整张卡片 innerHTML 重建一遍，
// 后果是①展开的"流程环节列表"被收起②卡片高度先塌陷再撑开，页面跟着上下跳③同一张表里
// 其它行没保存的修改被一起抹掉。现在改成"谁变了只重画谁"：
//   - 切换全部/票路径/财务路径 → 只换按钮高亮 + 重画那一张图（拉数据期间锁住图的高度，不塌陷）
//   - 勾选启用 / 改名称说明    → 只改对应的那个小标签/那行文字，卡片其它部分一动不动
//   - 保存某一行环节           → 表格一行都不动，只重画图，按钮上闪一下"✓已保存"
//   - 新增/删除/上下移环节     → 只重画表格+图，并保留其它行未保存的编辑、还原滚动位置
// ROLE_TYPE_META/HOP_TYPE_META/roleLabel/roleColor/hopLabel/hopColor/renderChainGraph
// 都在 app.js 里（每页都加载），供本页和车次详情页(trips.js)共用同一份关系图渲染逻辑。

let TEMPLATES = [];
const PATH_STATE = {}; // template_id -> 'all'/'invoice'/'payment'，记住每张图当前选的是哪条路径视图
const CR_TEMPLATE_TAB = {}; // template_id -> graph|edges|calc|display
const CR_TAB_LABELS = { graph: '关系图', edges: '流程环节', calc: '计算口径', display: '展示字段' };

// 用户录进来的名称/备注是自由文本，可能带引号或尖括号，塞进 value="..." 前必须转义，
// 否则一个双引号就能把整行表格结构撑坏。
function attr(v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function flashSaved(btn, text = '✓已保存') {
  if (!btn) return;
  const oldText = btn.textContent, oldCls = btn.className;
  btn.textContent = text;
  btn.className = oldCls + ' btn-primary';
  btn.disabled = true;
  setTimeout(() => { btn.textContent = oldText; btn.className = oldCls; btn.disabled = false; }, 1200);
}

// 重画表格这类会改变高度的操作，事后把滚动位置还原回去，避免"点一下页面就往上蹿"。
function restoreScroll(y) {
  requestAnimationFrame(() => window.scrollTo(0, y));
}

async function loadAll() {
  await loadHopTypes();   // 环节类型目录由后端 engine.HOP_TYPE_CATALOG 定义，下拉框和配色都用它
  TEMPLATES = await apiGet('/api/flow_templates');
  const dl = parseChainRulesDeepLink();
  if (dl.templateCode || dl.tab || dl.nodeKey) CR_VIEW = 'edit';
  await applyCrView();
}

// ---------------- 只读「一眼看懂」视图（B档 2026-10-09）----------------
// 用户反馈：链条规则页信息密度太高（流程环节表 + 计算公式 + 节点角色 + 三张图），
// "要消化理解半天"。于是把"看"和"改"分开：默认落在只读视图，每条规则用
// 一句人话 + 一条主体流程线 + 参与算钱的口子讲清楚；要动就去「编辑」。
// 只读视图不写任何数据，纯展示，切回编辑视图时再按需构建可编辑卡片。
let CR_VIEW = 'plain';
let CARDS_BUILT = false;

function setCrView(v) {
  CR_VIEW = v;
  applyCrView().catch(err => console.error(err));
}
async function applyCrView() {
  const isPlain = CR_VIEW === 'plain';
  document.querySelectorAll('#cr-view-toggle button').forEach(b => b.classList.toggle('active', b.dataset.v === CR_VIEW));
  const plain = document.getElementById('cr-plain');
  const list = document.getElementById('template-list');
  const addBtn = document.getElementById('btn-add-template');
  const hint = document.getElementById('cr-view-hint');
  const intro = document.getElementById('cr-edit-intro');
  if (plain) plain.hidden = !isPlain;
  if (list) list.hidden = isPlain;
  if (addBtn) addBtn.hidden = isPlain;
  if (intro) intro.hidden = isPlain;
  if (hint) hint.innerHTML = isPlain
    ? '每条链条规则用一句人话 + 一条流程线讲清楚：货走哪些主体、我们赚哪一段钱。<b>要改就去右上角「编辑」。</b>'
    : '编辑视图：用卡片内标签切换「关系图 / 流程环节 / 计算口径 / 展示字段」。改口径只影响以后新算的钱——已固化历史车次不变。';
  if (isPlain) renderPlainList();
  else {
    if (!CARDS_BUILT) await renderTemplateList();
    await applyChainRulesDeepLink();
  }
}

const PLAIN_MODEL = {
  split: { label: '差价模型', tip: '赚的是「给客户付」和「前端实付」之间的差价（配票/反向走这条）。', cls: 'green' },
  service_fee: { label: '服务费模型', tip: '赚的是按吨收的服务费，不碰票源成本（带票带货/户头通道走这条）。', cls: '' },
};
function plainSteps(graph) {
  if (!graph || !graph.edges.length) return '<span class="muted">还没配流程环节</span>';
  const nodeOf = k => graph.nodes.find(n => n.key === k) || {};
  const seq = [...graph.edges].sort((a, b) => (a.seq || 0) - (b.seq || 0));
  const parts = [];
  const chip = k => {
    const n = nodeOf(k);
    return `<span class="cr-plain-step s-${n.role_type || ''}" title="这一个是：${attr(roleLabel(n.role_type))}">${attr(n.label || k)}</span>`;
  };
  parts.push(chip(seq[0].from));
  seq.forEach(e => {
    parts.push(`<span class="cr-plain-arrow" title="${attr(hopLabel(e.hop_type))}${e.has_amount ? '' : '（仅流程说明，不参与算钱）'}">→</span>`);
    parts.push(chip(e.to));
  });
  return `<div class="cr-plain-steps">${parts.join('')}</div>`;
}
function plainCard(t, graph) {
  const m = PLAIN_MODEL[t.calc_family] || { label: t.calc_family || '未知', tip: '', cls: '' };
  const moneySteps = [...new Set((graph ? graph.edges : []).filter(e => e.has_amount).map(e => e.node_label).filter(Boolean))];
  return `<div class="cr-plain-card">
    <div class="cr-plain-head">
      <b>${attr(t.name)}</b>
      <span class="pill">简写：${attr(t.short_name || '未设')}</span>
      <span class="pill ${m.cls}">${attr(m.label)}</span>
      ${drill(`在用 ${t.trip_count || 0} 车`, { template_id: t.id, exclude_voided: 1 }, '链条规则·' + (t.short_name || t.name))}
      <span style="flex:1"></span>
      <a class="muted" href="javascript:void(0)" onclick="setCrView('edit')">去编辑 ›</a>
    </div>
    <div class="cr-plain-line"><b>怎么走：</b></div>
    ${plainSteps(graph)}
    <div class="cr-plain-line"><b>赚哪一段：</b>${attr(m.tip)}</div>
    <div class="cr-plain-line"><b>参与算钱的口子（${moneySteps.length} 个）：</b>${
      moneySteps.length ? moneySteps.map(attr).join('、') : '（无——这条链条目前不参与算钱，只记流程）'}</div>
    ${t.description ? `<div class="cr-plain-line"><b>说明：</b>${attr(t.description)}</div>` : ''}
  </div>`;
}
async function renderPlainList() {
  const box = document.getElementById('cr-plain');
  if (!box) return;
  const list = TEMPLATES.filter(t => t.is_active || t.trip_count);
  if (!list.length) { box.innerHTML = '<div class="empty-hint">还没有启用的链条规则</div>'; return; }
  box.innerHTML = '<div class="empty-hint">加载中…</div>';
  const graphs = await Promise.all(list.map(t =>
    apiGet(`/api/flow_templates/${t.id}/graph?path=all`).catch(() => null)));
  if (CR_VIEW !== 'plain') return;   // 拉数据期间用户切走了，就不要覆盖编辑视图
  box.innerHTML = list.map((t, i) => plainCard(t, graphs[i])).join('');
}

function renderTemplateList() {
  const box = document.getElementById('template-list');
  box.innerHTML = '';
  TEMPLATES.forEach(t => {
    const card = document.createElement('div');
    card.className = 'card';
    card.id = `tpl-card-${t.id}`;
    box.appendChild(card);
  });
  CARDS_BUILT = true;
  return Promise.all(TEMPLATES.map(t => renderTemplateCard(t.id)));
}

function switchTemplateTab(tid, tab) {
  if (!CR_TAB_LABELS[tab]) tab = 'graph';
  CR_TEMPLATE_TAB[tid] = tab;
  const card = document.getElementById(`tpl-card-${tid}`);
  if (!card) return;
  card.querySelectorAll('.cr-template-tabs button[data-tab]').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  ['graph', 'edges', 'calc', 'display'].forEach(p => {
    const el = document.getElementById(`tpl-panel-${p}-${tid}`);
    if (el) el.hidden = p !== tab;
  });
}

function parseChainRulesDeepLink() {
  const q = new URLSearchParams(location.search);
  return {
    templateCode: (q.get('template') || '').trim(),
    tab: (q.get('tab') || '').trim(),
    nodeKey: (q.get('node') || '').trim(),
  };
}

async function applyChainRulesDeepLink() {
  const { templateCode, tab, nodeKey } = parseChainRulesDeepLink();
  if (!templateCode && !tab && !nodeKey) return;
  if (templateCode) {
    const t = TEMPLATES.find(x => x.code === templateCode || x.short_name === templateCode);
    if (!t) return;
    if (tab && CR_TAB_LABELS[tab]) CR_TEMPLATE_TAB[t.id] = tab;
    switchTemplateTab(t.id, CR_TEMPLATE_TAB[t.id] || 'graph');
    const card = document.getElementById(`tpl-card-${t.id}`);
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (nodeKey && (CR_TEMPLATE_TAB[t.id] || tab) === 'calc') {
      await flashNodeByKey(t.id, nodeKey);
    }
  } else if (tab && CR_TAB_LABELS[tab]) {
    TEMPLATES.forEach(t => switchTemplateTab(t.id, tab));
  }
}

function flashNodeByKey(tid, nodeKey) {
  const d = FORMULA[tid];
  if (!d) return Promise.resolve();
  const n = d.nodes.find(x => x.node_key === nodeKey);
  if (!n) return Promise.resolve();
  flashNode(tid, n.n);
  return Promise.resolve();
}

// 整张卡片重建：只在首次加载、以及新建链条规则之后调用。日常编辑一律走下面的局部刷新。
async function renderTemplateCard(tid) {
  const card = document.getElementById(`tpl-card-${tid}`);
  if (!card) return;
  const t = TEMPLATES.find(x => x.id === tid);
  const path = PATH_STATE[tid] || 'all';
  const activeTab = CR_TEMPLATE_TAB[tid] || 'graph';
  card.innerHTML = `
    <div class="toolbar">
      <div>
        <b style="font-size:15px;" id="tpl-name-${tid}">${attr(t.name)}</b>
        <span class="pill" id="tpl-short-${tid}" title="简写：车次列表等处显示用">简写：${attr(t.short_name || '未设')}</span>
        <span class="pill blue">${attr(t.code)}</span>
        <span class="pill ${t.calc_family === 'split' ? 'green' : ''}">${t.calc_family === 'split' ? '差价模型' : '服务费模型'}</span>
        ${drill(`在用 ${t.trip_count || 0} 车`, { template_id: tid, exclude_voided: 1 }, '链条规则·' + (t.short_name || t.name))}
        <span class="pill red" id="tpl-inactive-${tid}" style="display:${t.is_active ? 'none' : 'inline-block'};">已停用·0车次在用</span>
      </div>
      <div class="spacer"></div>
      <label style="font-size:12px;color:var(--gray-500);margin-right:10px;">
        <input type="checkbox" ${t.is_active ? 'checked' : ''} onchange="toggleActive(${tid}, this.checked)"> 启用
      </label>
      <button class="btn-sm" onclick="editTemplateMeta(${tid})">编辑名称/说明</button>
      <button class="btn-sm" onclick="cloneTemplate(${tid})" title="复制计算节点与流程环节，默认停用">复制为新规则</button>
    </div>
    <div style="font-size:12.5px;color:var(--gray-500);margin:2px 0 8px;" id="tpl-desc-${tid}">${attr(t.description || '')}</div>
    <div id="tpl-meta-form-${tid}"></div>
    <div class="cr-template-tabs tabs-inline" id="tpl-tabs-${tid}">
      ${Object.entries(CR_TAB_LABELS).map(([k, lab]) =>
        `<button type="button" data-tab="${k}" class="${activeTab === k ? 'active' : ''}" onclick="switchTemplateTab(${tid}, '${k}')">${lab}</button>`
      ).join('')}
    </div>
    <div id="tpl-panel-graph-${tid}" ${activeTab === 'graph' ? '' : 'hidden'}>
      <div class="toolbar" style="margin:8px 0 4px;" id="tpl-path-btns-${tid}">
        <div style="font-size:12px;color:var(--gray-500);">关系图视图：</div>
        ${['all', 'invoice', 'payment'].map(p => `
          <button class="btn-sm ${path === p ? 'btn-primary' : ''}" data-path="${p}" onclick="switchPath(${tid}, '${p}')">
            ${p === 'all' ? '全部' : p === 'invoice' ? '票据路径(结算单/台账/发票)' : '财务路径(打款)'}
          </button>`).join('')}
      </div>
      <div id="tpl-banner-${tid}"></div>
      <div id="tpl-graph-${tid}" class="chart-card"></div>
    </div>
    <div id="tpl-panel-edges-${tid}" style="margin-top:8px;" ${activeTab === 'edges' ? '' : 'hidden'}>
      <div class="muted" style="font-size:12px;margin-bottom:6px;">只影响关系图怎么画，不改变计算公式本身。</div>
      <div id="tpl-edges-${tid}"></div>
    </div>
    <div id="tpl-panel-calc-${tid}" style="margin-top:8px;" ${activeTab === 'calc' ? '' : 'hidden'}>
      <div id="tpl-formula-${tid}"></div>
    </div>
    <div id="tpl-panel-display-${tid}" style="margin-top:8px;" ${activeTab === 'display' ? '' : 'hidden'}>
      <div class="muted" style="font-size:12px;margin-bottom:6px;">节点在关系图里的角色、名称与单据类型（不改公式结构）。</div>
      <div id="tpl-nodes-${tid}"></div>
    </div>
  `;
  await refreshGraph(tid);
  renderEdgesEditor(tid);
  renderNodesEditor(tid);
  renderFormula(tid);
  switchTemplateTab(tid, activeTab);
}

// 只重画关系图。拉数据期间先把容器高度锁住，等新图画好再放开——否则容器会先塌成0高度，
// 下面的内容整体往上跳一下再跳回来，就是用户说的"页面上下挪动"。
async function refreshGraph(tid) {
  const box = document.getElementById(`tpl-graph-${tid}`);
  if (!box) return;
  const lock = box.offsetHeight;
  if (lock) box.style.minHeight = lock + 'px';
  try {
    const path = PATH_STATE[tid] || 'all';
    const graph = await apiGet(`/api/flow_templates/${tid}/graph?path=${path}`);
    renderChainGraph(`tpl-graph-${tid}`, graph);
    // 整张图都还是自动推导、没人工核过 -> 顶部给个横幅，避免把没核实的图当真
    const banner = document.getElementById(`tpl-banner-${tid}`);
    if (banner) {
      const autoN = graph.edges.filter(x => x.is_auto).length;
      banner.innerHTML = autoN && autoN === graph.edges.length
        ? `<div style="background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:6px;
             padding:6px 10px;font-size:12px;margin-bottom:6px;">
             这张图是系统按计算节点自动推导的初版（${autoN} 个环节），还没人工核实过。
             展开下面的环节列表改任意一条，那一条就会转成已确认。</div>`
        : '';
    }
  } finally {
    box.style.minHeight = '';
  }
}

function switchPath(tid, path) {
  PATH_STATE[tid] = path;
  // 只换按钮高亮 + 重画图，不重建卡片：展开的编辑区保持展开，滚动位置不动。
  const btnBox = document.getElementById(`tpl-path-btns-${tid}`);
  if (btnBox) {
    btnBox.querySelectorAll('button[data-path]').forEach(b => {
      b.classList.toggle('btn-primary', b.dataset.path === path);
    });
  }
  refreshGraph(tid);
}

async function toggleActive(tid, checked) {
  await apiPut(`/api/flow_templates/${tid}`, { is_active: checked ? 1 : 0 });
  const t = TEMPLATES.find(x => x.id === tid);
  t.is_active = checked ? 1 : 0;
  // 只改"已停用"这个小标签的显隐，卡片其它部分不动。
  const badge = document.getElementById(`tpl-inactive-${tid}`);
  if (badge) badge.style.display = checked ? 'none' : 'inline-block';
}

function editTemplateMeta(tid) {
  const t = TEMPLATES.find(x => x.id === tid);
  document.getElementById(`tpl-meta-form-${tid}`).innerHTML = `
    <div class="card" style="background:var(--blue-light);border-color:#bfdbfe;">
      <div class="field"><label>展示名</label><input id="tm-name-${tid}" type="text" value="${attr(t.name)}"></div>
      <div class="field"><label>简写（车次列表显示，建议2-4个字）</label><input id="tm-short-${tid}" type="text" maxlength="8" value="${attr(t.short_name || '')}"></div>
      <div class="field"><label>说明</label><input id="tm-desc-${tid}" type="text" value="${attr(t.description || '')}"></div>
      <button class="btn-primary" onclick="saveTemplateMeta(${tid}, this)">保存</button>
      <button onclick="document.getElementById('tpl-meta-form-${tid}').innerHTML=''">取消</button>
    </div>`;
}
async function saveTemplateMeta(tid, btn) {
  const name = document.getElementById(`tm-name-${tid}`).value;
  const description = document.getElementById(`tm-desc-${tid}`).value;
  const short_name = document.getElementById(`tm-short-${tid}`).value.trim();
  await apiPut(`/api/flow_templates/${tid}`, { name, short_name, description });
  const t = TEMPLATES.find(x => x.id === tid);
  t.name = name; t.description = description; t.short_name = short_name;
  document.getElementById(`tpl-short-${tid}`).textContent = '简写：' + (short_name || '未设');
  // 就地改标题和说明那两行文字，不重建卡片。
  document.getElementById(`tpl-name-${tid}`).textContent = name;
  document.getElementById(`tpl-desc-${tid}`).textContent = description || '';
  flashSaved(btn);
  setTimeout(() => {
    const box = document.getElementById(`tpl-meta-form-${tid}`);
    if (box) box.innerHTML = '';
  }, 600);
}

// ---------------- 新建链条规则 ----------------
function showNewTemplateForm() {
  document.getElementById('new-template-box').innerHTML = `
    <div class="card" style="background:var(--blue-light);border-color:#bfdbfe;">
      <div class="field-row" style="grid-template-columns:repeat(auto-fit, minmax(180px, 1fr));">
        <div class="field"><label>代码 *（英文，唯一）</label><input id="nt-code" type="text" placeholder="如 split_new_channel"></div>
        <div class="field"><label>展示名 *</label><input id="nt-name" type="text" placeholder="如 xx新渠道-差价模型"></div>
        <div class="field"><label>简写（车次列表显示）</label><input id="nt-short" type="text" maxlength="8" placeholder="如 新渠道"></div>
        <div class="field"><label>计算口径 *</label>
          <select id="nt-family"><option value="split">差价模型(split)</option><option value="service_fee">服务费模型(service_fee)</option></select>
        </div>
        <div class="field"><label>说明</label><input id="nt-desc" type="text"></div>
      </div>
      <div style="font-size:11.5px;color:var(--gray-500);margin-bottom:8px;">
        新建后只有链条规则本身和它的流程图是空的，车次真正要用这条规则计算，还需要在代码里补上对应
        的计算节点(template_nodes)——这一步目前仍需开发处理。如果只是想调整/补充现有5条规则(M1-M5)
        的流程图，不需要新建，直接在下面对应卡片里编辑流程环节即可。
      </div>
      <button class="btn-primary" onclick="saveNewTemplate()">保存</button>
      <button onclick="document.getElementById('new-template-box').innerHTML=''">取消</button>
    </div>`;
}
async function saveNewTemplate() {
  const code = document.getElementById('nt-code').value.trim();
  const name = document.getElementById('nt-name').value.trim();
  const calc_family = document.getElementById('nt-family').value;
  const description = document.getElementById('nt-desc').value;
  if (!code || !name) { alert('请填写代码和展示名'); return; }
  const short_name = document.getElementById('nt-short').value.trim();
  await apiPost('/api/flow_templates', { code, name, short_name, calc_family, description });
  document.getElementById('new-template-box').innerHTML = '';
  CARDS_BUILT = false;
  await loadAll();
}
async function cloneTemplate(tid) {
  const t = TEMPLATES.find(x => x.id === tid);
  if (!t) return;
  const newCode = prompt(`复制「${t.name}」— 新规则英文代码（唯一）`, `${t.code}_copy`);
  if (!newCode || !newCode.trim()) return;
  const newName = prompt('展示名', `${t.name}（副本）`);
  if (!newName || !newName.trim()) return;
  try {
    const r = await apiPost(`/api/flow_templates/${tid}/clone`, { code: newCode.trim(), name: newName.trim() });
    toast(`已复制为 ${r.code}（默认停用，核对后再启用）`);
    CARDS_BUILT = false;
    CR_TEMPLATE_TAB[r.id] = 'calc';
    await loadAll();
    location.hash = '';
    history.replaceState(null, '', `/chain_rules?template=${encodeURIComponent(r.code)}&tab=calc`);
    await applyChainRulesDeepLink();
  } catch (e) {
    alert(e.message || String(e));
  }
}
document.getElementById('btn-add-template').addEventListener('click', showNewTemplateForm);


// ---------------- 端点下拉（2026-09-19 录入简化）----------------
// 原来一端要选"角色"再手打"名称"两步，而且手打的名字在车次图里还会被按角色替换掉，
// 等于白打。现在合并成一个下拉，选项本身就说清楚了这一端是哪种形态：
//   role:<角色>              角色占位 —— 车次图里自动换成该车次的真实主体
//   entity:<角色>:<实体id>   指定具体对象 —— 固定就是档案里的那一个，改名图上自动跟着变
//   custom:<角色>            自定义文字 —— 旁边的输入框才是真正要显示的字
let ENDPOINT_OPTIONS = [];
async function loadEndpointOptions() {
  if (!ENDPOINT_OPTIONS.length) ENDPOINT_OPTIONS = await apiGet('/api/flow_edge_endpoint_options');
  return ENDPOINT_OPTIONS;
}
function endpointOptionsHtml() {
  return ENDPOINT_OPTIONS.map(g =>
    `<optgroup label="${attr(g.group)}">` +
    g.items.map(i => `<option value="${attr(i.value)}">${attr(i.label)}</option>`).join('') +
    `</optgroup>`).join('');
}
// 从一条边的数据反推出下拉该选哪一项
function endpointValueOf(e, side) {
  const role = e[`${side}_role_type`], entId = e[`${side}_entity_id`];
  if (entId) return `entity:${role}:${entId}`;
  const isRolePlaceholder = ROLE_PLACEHOLDER_ROLES.includes(role)
    && (e[`${side}_label`] || '') === (ROLE_CN[role] || '');
  return isRolePlaceholder ? `role:${role}` : `custom:${role}`;
}
// 把下拉选中的值 + 自定义文字框，拆回后端要的四个字段
function endpointFields(side, selectValue, customText) {
  const [kind, role, entId] = String(selectValue || '').split(':');
  const out = {};
  out[`${side}_role_type`] = role || 'output';
  if (kind === 'entity') {
    const item = ENDPOINT_OPTIONS.flatMap(g => g.items).find(i => i.value === selectValue);
    out[`${side}_entity_type`] = item ? item.entity_type : null;
    out[`${side}_entity_id`] = Number(entId);
    out[`${side}_label`] = item ? item.name : '';
  } else {
    out[`${side}_entity_type`] = null;
    out[`${side}_entity_id`] = null;
    out[`${side}_label`] = kind === 'custom' ? (customText || '').trim() : (ROLE_CN[role] || role);
  }
  return out;
}
const ROLE_CN = { supplier: '供货厂', dispatcher: '调货员', our_account: '我方户头',
  ticket_source: '票源方', soe: '国企', mill: '钢厂', output: '结果' };
const ROLE_PLACEHOLDER_ROLES = ['supplier', 'dispatcher', 'our_account', 'ticket_source', 'mill'];

// 本模板可挂的计算节点（下拉用，显示中文label不是内部node_key）
const TEMPLATE_NODES = {};
async function loadTemplateNodes(tid) {
  if (!TEMPLATE_NODES[tid]) TEMPLATE_NODES[tid] = await apiGet(`/api/flow_templates/${tid}/nodes`);
  return TEMPLATE_NODES[tid];
}
function nodeOptionsHtml(tid) {
  const nodes = TEMPLATE_NODES[tid] || [];
  return '<option value="">（不挂·仅流程说明）</option>' +
    nodes.map(n => `<option value="${attr(n.node_key)}">${attr(n.label)}</option>`).join('');
}

// ---------------- 流程环节(template_flow_edges)编辑 ----------------
// 读一行当前在页面上的值（不是数据库里的值）——保存时用它，重画表格前也用它把"还没保存的
// 修改"收集起来，重画完再贴回去，避免用户改了A行又去点B行的↑↓，A行的修改被悄悄抹掉。
// id 命名统一成「前缀-字段」：行用 ee-<边id>-xx，新增表单用 ae-<模板id>-xx。
// （之前端点单元格按"前缀-字段"生成、读取却按"字段-前缀"找，正好错开，一进页面就报 null。）
function edgeRowValues(id) {
  const g = (p) => document.getElementById(`ee-${id}-${p}`);
  if (!g('f')) return null;
  return Object.assign(
    endpointFields('from', g('f').value, g('fc') ? g('fc').value : ''),
    endpointFields('to', g('t').value, g('tc') ? g('tc').value : ''),
    {
      hop_type: g('ht').value,
      node_key_ref: g('nk').value || null,
      notes: g('nt').value.trim() || null,
    });
}
function captureEdgeEdits(tid) {
  const out = {};
  const box = document.getElementById(`tpl-edges-${tid}`);
  if (!box) return out;
  box.querySelectorAll('tr[data-edge]').forEach(tr => {
    const v = edgeRowValues(tr.dataset.edge);
    if (v) out[tr.dataset.edge] = v;
  });
  return out;
}

// 一端的单元格：下拉 + （只有选了"自定义文字"才出现的）文本框
function endpointCell(idPrefix, e, side) {
  const val = endpointValueOf(e, side);
  const isCustom = val.startsWith('custom:');
  return `<select id="${idPrefix}-${side === 'from' ? 'f' : 't'}" style="width:158px;"
            onchange="toggleCustomBox('${idPrefix}','${side}')">${endpointOptionsHtml()}</select>
    <input id="${idPrefix}-${side === 'from' ? 'fc' : 'tc'}" type="text" placeholder="自定义名称"
      value="${attr(isCustom ? (e[side + '_label'] || '') : '')}"
      style="width:110px;margin-top:3px;${isCustom ? '' : 'display:none;'}">`;
}
function toggleCustomBox(idPrefix, side) {
  const sel = document.getElementById(`${idPrefix}-${side === 'from' ? 'f' : 't'}`);
  const box = document.getElementById(`${idPrefix}-${side === 'from' ? 'fc' : 'tc'}`);
  if (sel && box) box.style.display = sel.value.startsWith('custom:') ? '' : 'none';
}

async function renderEdgesEditor(tid, keep) {
  const box = document.getElementById(`tpl-edges-${tid}`);
  if (!box) return;
  const [edges] = await Promise.all([
    apiGet(`/api/flow_templates/${tid}/flow_edges`), loadEndpointOptions(), loadTemplateNodes(tid),
  ]);
  const hopOpts = HOP_TYPE_LIST.map(h => `<option value="${h.code}">${h.label}</option>`).join('');
  // 服务器值打底，叠加上用户还没保存的改动
  const merged = edges.map(e => Object.assign({}, e, (keep && keep[e.id]) || {}));
  box.innerHTML = `
    <div class="table-scroll"><table>
      <thead><tr>
        <th>#</th><th>起点</th><th>终点</th><th>类型</th>
        <th>金额来自哪个计算节点</th><th>备注</th><th class="nowrap">操作</th>
      </tr></thead>
      <tbody>${merged.map(e => `
        <tr data-edge="${e.id}">
          <td class="nowrap">${e.seq}${e.is_auto ? ' <span class="pill" style="font-size:10px;">待核实</span>' : ''}</td>
          <td>${endpointCell(`ee-${e.id}`, e, 'from')}</td>
          <td>${endpointCell(`ee-${e.id}`, e, 'to')}</td>
          <td><select id="ee-${e.id}-ht">${hopOpts}</select></td>
          <td><select id="ee-${e.id}-nk" style="width:160px;">${nodeOptionsHtml(tid)}</select></td>
          <td><input id="ee-${e.id}-nt" type="text" value="${attr(e.notes || '')}" style="width:150px;"></td>
          <td class="nowrap">
            <button class="btn-sm" onclick="moveEdge(${tid}, ${e.id}, -1)">↑</button>
            <button class="btn-sm" onclick="moveEdge(${tid}, ${e.id}, 1)">↓</button>
            <button class="btn-sm" onclick="saveEdge(${tid}, ${e.id}, this)">保存</button>
            <button class="btn-sm btn-danger" onclick="deleteEdge(${tid}, ${e.id})">删除</button>
          </td>
        </tr>`).join('')}
      </tbody>
    </table></div>
    <div style="font-size:11.5px;color:var(--gray-500);margin-top:4px;">
      「金额来自哪个计算节点」选了，这一步在图上就是实线并显示金额；选"不挂"就是细虚线的纯流程说明，
      改它不会影响任何数字。改完点该行的"保存"，只有上面那张图会更新，表格和页面位置都不动。
    </div>
    <div class="toolbar" style="margin-top:8px;">
      <button class="btn-sm" onclick="showAddEdgeForm(${tid})">+ 新增一个环节</button>
      <button class="btn-sm" onclick="autoGenerate(${tid})" title="按计算节点自动推导一版，覆盖现有环节">按计算节点重新生成一版</button>
    </div>
    <div id="add-edge-form-${tid}"></div>
  `;
  merged.forEach(e => {
    document.getElementById(`ee-${e.id}-f`).value = endpointValueOf(e, 'from');
    document.getElementById(`ee-${e.id}-t`).value = endpointValueOf(e, 'to');
    document.getElementById(`ee-${e.id}-ht`).value = e.hop_type;
    document.getElementById(`ee-${e.id}-nk`).value = e.node_key_ref || '';
  });
}

async function autoGenerate(tid) {
  if (!confirm('按计算节点自动推导一版流程环节，会覆盖这条规则现有的全部环节。继续？')) return;
  const y = window.scrollY;
  await apiPost(`/api/flow_templates/${tid}/flow_edges/auto_generate`, { replace: true });
  await renderEdgesEditor(tid);
  await refreshGraph(tid);
  restoreScroll(y);
}

async function showAddEdgeForm(tid) {
  await Promise.all([loadEndpointOptions(), loadTemplateNodes(tid)]);
  const edges = await apiGet(`/api/flow_templates/${tid}/flow_edges`);
  const last = edges[edges.length - 1];
  // 链式录入：起点默认承接上一条的终点，所以常规情况下只要选"终点"和"类型"两项就能加一环。
  const fromVal = last ? endpointValueOf(last, 'to') : 'role:supplier';
  const fromCustom = last && fromVal.startsWith('custom:') ? (last.to_label || '') : '';
  const hopOpts = HOP_TYPE_LIST.map(h => `<option value="${h.code}">${h.label}</option>`).join('');
  document.getElementById(`add-edge-form-${tid}`).innerHTML = `
    <div class="card" style="background:var(--blue-light);border-color:#bfdbfe;margin-top:6px;">
      ${last ? `<div style="font-size:11.5px;color:var(--gray-500);margin-bottom:6px;">
        起点已自动接上一条环节的终点，一般只需要选「终点」和「类型」。</div>` : ''}
      <div class="field-row" style="grid-template-columns:repeat(auto-fit, minmax(170px, 1fr));">
        <div class="field"><label>起点</label>
          <select id="ae-${tid}-f" onchange="toggleCustomBox('ae-${tid}','from')">${endpointOptionsHtml()}</select>
          <input id="ae-${tid}-fc" type="text" placeholder="自定义名称" value="${attr(fromCustom)}"
            style="margin-top:3px;${fromVal.startsWith('custom:') ? '' : 'display:none;'}">
        </div>
        <div class="field"><label>终点 *</label>
          <select id="ae-${tid}-t" onchange="toggleCustomBox('ae-${tid}','to')">${endpointOptionsHtml()}</select>
          <input id="ae-${tid}-tc" type="text" placeholder="自定义名称" style="margin-top:3px;display:none;">
        </div>
        <div class="field"><label>类型 *</label><select id="ae-${tid}-ht">${hopOpts}</select></div>
        <div class="field"><label>金额来自哪个计算节点</label>
          <select id="ae-${tid}-nk">${nodeOptionsHtml(tid)}</select></div>
        <div class="field"><label>备注</label><input id="ae-${tid}-nt" type="text" placeholder="这一步在业务上是什么"></div>
      </div>
      <button class="btn-primary" onclick="saveNewEdge(${tid})">保存</button>
      <button onclick="document.getElementById('add-edge-form-${tid}').innerHTML=''">取消</button>
    </div>`;
  document.getElementById(`ae-${tid}-f`).value = fromVal;
  document.getElementById(`ae-${tid}-t`).value = 'role:mill';
}
async function saveNewEdge(tid) {
  const g = (p) => document.getElementById(`ae-${tid}-${p}`);
  const data = Object.assign(
    endpointFields('from', g('f').value, g('fc').value),
    endpointFields('to', g('t').value, g('tc').value),
    {
      hop_type: g('ht').value,
      node_key_ref: g('nk').value || null,
      notes: g('nt').value.trim() || null,
    });
  if (!data.from_label || !data.to_label) { alert('选了"自定义文字"的那一端要填名称'); return; }
  const y = window.scrollY;
  const keep = captureEdgeEdits(tid);
  await apiPost(`/api/flow_templates/${tid}/flow_edges`, data);
  await renderEdgesEditor(tid, keep);
  await refreshGraph(tid);
  restoreScroll(y);
}
async function saveEdge(tid, edgeId, btn) {
  const values = edgeRowValues(edgeId);
  if (!values) return;
  await apiPut(`/api/flow_edges/${edgeId}`, values);
  flashSaved(btn);
  await refreshGraph(tid);
}
async function deleteEdge(tid, edgeId) {
  if (!confirm('确认删除这个流程环节？')) return;
  const y = window.scrollY;
  const keep = captureEdgeEdits(tid);
  delete keep[edgeId];
  await apiDelete(`/api/flow_edges/${edgeId}`);
  await renderEdgesEditor(tid, keep);
  await refreshGraph(tid);
  restoreScroll(y);
}
async function moveEdge(tid, edgeId, dir) {
  const y = window.scrollY;
  const keep = captureEdgeEdits(tid);
  const edges = await apiGet(`/api/flow_templates/${tid}/flow_edges`);
  const idx = edges.findIndex(e => e.id === edgeId);
  const swapIdx = idx + dir;
  if (swapIdx < 0 || swapIdx >= edges.length) return;
  [edges[idx], edges[swapIdx]] = [edges[swapIdx], edges[idx]];
  await apiPost(`/api/flow_templates/${tid}/flow_edges/reorder`, { order: edges.map(e => e.id) });
  await renderEdgesEditor(tid, keep);
  await refreshGraph(tid);
  restoreScroll(y);
}

// ---------------- 计算节点(template_nodes)口径编辑：只开放label/role_type/doc_types ----------------
async function renderNodesEditor(tid) {
  const box = document.getElementById(`tpl-nodes-${tid}`);
  if (!box) return;
  const nodes = await apiGet(`/api/flow_templates/${tid}/nodes`);
  const roleOpts = Object.keys(ROLE_TYPE_META).map(r => `<option value="${r}">${roleLabel(r)}</option>`).join('');
  const docTypeOptions = ['invoice', 'payment', 'weighing_slip', 'settlement_doc'];
  box.innerHTML = `
    <div class="table-scroll"><table>
      <thead><tr><th>#</th><th>node_key</th><th>label</th><th>角色</th><th>单据类型</th><th>计算规则(只读)</th><th>操作</th></tr></thead>
      <tbody>${nodes.map(n => `
        <tr>
          <td>${n.seq}</td>
          <td class="nowrap" style="font-family:monospace;font-size:11.5px;">${attr(n.node_key)}</td>
          <td><input id="tn-label-${n.id}" type="text" value="${attr(n.label)}" style="width:120px;"></td>
          <td><select id="tn-role-${n.id}">${roleOpts}</select></td>
          <td>${docTypeOptions.map(dt => `
            <label style="font-size:11px;margin-right:6px;">
              <input type="checkbox" id="tn-dt-${n.id}-${dt}" ${n.doc_types.includes(dt) ? 'checked' : ''}> ${dt}
            </label>`).join('')}</td>
          <td class="nowrap" style="font-size:11px;color:var(--gray-500);">${n.calc_rule_type}</td>
          <td><button class="btn-sm" onclick="saveNode(${n.id}, this)">保存</button></td>
        </tr>`).join('')}
      </tbody>
    </table></div>`;
  nodes.forEach(n => { document.getElementById(`tn-role-${n.id}`).value = n.role_type || 'output'; });
}
// 节点口径改的是展示字段，跟这张关系图（走的是流程环节表）没关系，所以存完什么都不用重画，
// 按钮闪一下就行——原来弹 alert 打断操作，改掉。
async function saveNode(nodeId, btn) {
  const docTypeOptions = ['invoice', 'payment', 'weighing_slip', 'settlement_doc'];
  const doc_types = docTypeOptions.filter(dt => document.getElementById(`tn-dt-${nodeId}-${dt}`).checked);
  await apiPut(`/api/template_nodes/${nodeId}`, {
    label: document.getElementById(`tn-label-${nodeId}`).value.trim(),
    role_type: document.getElementById(`tn-role-${nodeId}`).value,
    doc_types,
  });
  flashSaved(btn);
}

// ---------------- 计算公式"读得懂版"（2026-09-21） ----------------
// 每个节点一行：①编号 名称 = 中文公式（引用别的节点用编号小标签）；右边是用这条规则最近一车代入的真实数字，
// 以及费率从哪来、去哪改。能改的只有两类：费率节点"费率从哪来"、净利润"加减哪些项"，都先预览影响再保存。
const CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳';
const FORMULA = {};
const DOC_CN = { invoice: '开票核对', payment: '打款核对', weighing_slip: '过磅单', settlement_doc: '结算单' };
function circ(n) { return CIRCLED[n - 1] || `(${n})`; }
function fTok(x, tid) {
  if (x.t === 'node') return `<span class="f-ref" onclick="flashNode(${tid},${x.n})" title="看第${x.n}步">${circ(x.n)}${attr(x.label)}</span>`;
  if (x.t === 'field') return `<span class="f-field">${attr(x.label)}</span>`;
  return `<span class="f-op">${attr(x.label)}</span>`;
}
function flashNode(tid, n) {
  const el = document.querySelector(`#tpl-formula-${tid} .f-row[data-n="${n}"]`);
  if (!el) return;
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1200);
}
async function renderFormula(tid) {
  const box = document.getElementById(`tpl-formula-${tid}`);
  if (!box) return;
  const d = await apiGet(`/api/flow_templates/${tid}/formula`);
  FORMULA[tid] = d;
  const ex = d.example;
  const total = d.nodes.find(n => n.is_total);
  const val = n => ex && ex.values[n.node_key] !== undefined ? fmtMoney(ex.values[n.node_key]) : '';
  const rateOf = n => ex && ex.rates[n.node_key] ? `${fmtNum(ex.rates[n.node_key].value, 2)} ${ex.rates[n.node_key].unit}` : '';
  box.innerHTML = `
    <div class="f-head">
      ${total ? `<div><b>净利润 =</b> ${total.formula.map(x => fTok(x, tid)).join(' ')}</div>` : ''}
      <div class="muted">${ex ? `右边数字用 ${tripLink(ex.trip_id, attr(ex.plate_no || '#' + ex.trip_id))}（${ex.date || ''}，${fmtNum(ex.tonnage)} 吨，结算金额 ¥${fmtMoney(ex.settlement_amount)}）代入` : '这条规则还没有车次，没有示例数字'}
        · 使用中 ${d.trip_count} 车，其中 ${d.locked_count} 车已固化（改口径不影响已固化车次的统计数字）</div>
      <div class="muted">怎么改：<b>费率数值</b>（如垫资费23元/吨）去「费率与关系」改，按生效日期管理；这里的「改口径」改的是<b>用哪个费率 / 固定值</b>和<b>净利润加减哪几项</b>，保存前会先算出影响多少车、差多少钱。单车特殊情况在车次详情「本车费率」里改。</div>
    </div>
    <div class="f-list">${d.nodes.map(n => `
      <div class="f-row ${n.is_total ? 'total' : ''} ${n.is_reserved ? 'reserved' : ''}" data-n="${n.n}" id="frow-${n.id}">
        <div class="f-num">${circ(n.n)}</div>
        <div class="f-main">
          <div class="f-title"><span class="f-name" id="fname-${n.id}">${attr(n.label)}</span>
            <a href="javascript:void(0)" class="f-edit-name" onclick="renameNode(${tid},${n.id})" title="改名称">✎</a>
            ${n.doc_types.map(t => `<span class="pill">${DOC_CN[t] || t}</span>`).join(' ')}
            ${n.is_reserved ? '<span class="pill red">专项留存，不计入净利润</span>' : ''}</div>
          <div class="f-expr">= ${n.formula.map(x => fTok(x, tid)).join(' ')}</div>
          ${n.source ? `<div class="f-src">费率来源：${n.source.href ? `<a href="${n.source.href}" target="_blank">${attr(n.source.label)}</a>` : attr(n.source.label)}${n.source.by ? `（按${attr(n.source.by)}查）` : ''}${rateOf(n) ? ` · 示例车 ${rateOf(n)}` : ''}</div>` : ''}
          <div class="f-editor" id="fedit-${n.id}"></div>
        </div>
        <div class="f-val">${val(n) ? '¥' + val(n) : ''}</div>
        <div class="f-ops">${n.edit ? `<button class="btn-sm" onclick="openRuleEditor(${tid},${n.id})">改口径</button>` : ''}</div>
      </div>`).join('')}
    </div>`;
}
async function renameNode(tid, id) {
  const n = FORMULA[tid].nodes.find(x => x.id === id);
  const v = prompt('节点名称（只影响显示，不影响计算）', n.label);
  if (v === null || !v.trim()) return;
  await apiPut(`/api/template_nodes/${id}`, { label: v.trim() });
  renderFormula(tid); renderNodesEditor(tid);
}
function openRuleEditor(tid, id) {
  const d = FORMULA[tid], n = d.nodes.find(x => x.id === id);
  const box = document.getElementById(`fedit-${id}`);
  if (box.innerHTML) { box.innerHTML = ''; return; }
  if (n.edit === 'terms') {
    const cur = Object.fromEntries((n.config.terms || []).map(t => [t.node, t.sign]));
    const cands = d.nodes.filter(x => x.n < n.n && !x.doc_types.length && !x.is_reserved && x.rule !== 'ref_value');
    box.innerHTML = `<div class="f-edit-box"><div class="muted">每一项选"加 / 减 / 不计入"：</div>
      ${cands.map(x => `<div class="f-term"><span>${circ(x.n)} ${attr(x.label)}</span>
        <select data-node="${x.node_key}"><option value="1" ${cur[x.node_key] === 1 ? 'selected' : ''}>加 +</option><option value="-1" ${cur[x.node_key] === -1 ? 'selected' : ''}>减 −</option><option value="0" ${cur[x.node_key] === undefined ? 'selected' : ''}>不计入</option></select></div>`).join('')}
      ${ruleButtons(tid, id)}</div>`;
    return;
  }
  const isPct = n.edit === 'rate_pct', unit = isPct ? 'pct' : 'per_ton';
  const fixed = n.rule === 'per_ton_fixed' || n.rule === 'rate_deduct_fixed';
  const cat = d.relation_catalog.filter(c => c.rate_unit === unit);
  const fixedVal = isPct ? n.config.rate_pct : n.config.unit_price;
  box.innerHTML = `<div class="f-edit-box">
    <label class="f-opt"><input type="radio" name="fsrc-${id}" value="links" ${fixed ? '' : 'checked'}> 按「费率与关系」里的费率：
      <select id="frel-${id}">${cat.map(c => `<option value="${c.code}" ${c.code === n.config.relation_type ? 'selected' : ''}>${attr(c.label)}</option>`).join('')}</select>
      <label class="muted"><input type="checkbox" id="fsec-${id}" ${n.config.secondary_type ? 'checked' : ''}> 允许按票源细分（某票源单独设了费率就用它，没设用通用的）</label></label>
    <label class="f-opt"><input type="radio" name="fsrc-${id}" value="fixed" ${fixed ? 'checked' : ''}> 固定值：
      <input id="ffix-${id}" type="number" step="0.01" style="width:90px" value="${fixedVal ?? ''}"> ${isPct ? '%' : '元/吨'}（所有车都用这个数，不随时间变）</label>
    ${ruleButtons(tid, id)}</div>`;
}
function ruleButtons(tid, id) {
  return `<div class="f-edit-actions"><button onclick="previewRule(${tid},${id},false)">预览影响</button>
    <button class="btn-primary" onclick="previewRule(${tid},${id},true)">确认保存</button>
    <button onclick="document.getElementById('fedit-${id}').innerHTML=''">取消</button>
    <a href="javascript:void(0)" class="muted" onclick="undoRule(${tid},${id})">撤销上次修改</a></div>
    <div class="f-impact" id="fimp-${id}"></div>`;
}
function collectRule(tid, id) {
  const n = FORMULA[tid].nodes.find(x => x.id === id);
  if (n.edit === 'terms') {
    const terms = [...document.querySelectorAll(`#fedit-${id} select[data-node]`)].filter(s => s.value !== '0')
      .map(s => ({ node: s.dataset.node, sign: +s.value }));
    return { calc_rule_type: 'sum_formula', calc_rule_config: { terms } };
  }
  const isPct = n.edit === 'rate_pct';
  const src = document.querySelector(`input[name="fsrc-${id}"]:checked`).value;
  if (src === 'fixed') {
    const v = document.getElementById(`ffix-${id}`).value;
    return { calc_rule_type: isPct ? 'rate_deduct_fixed' : 'per_ton_fixed', calc_rule_config: isPct ? { rate_pct: v } : { unit_price: v } };
  }
  return { calc_rule_type: isPct ? 'rate_deduct_rate_schedule' : 'per_ton_rate_schedule',
    calc_rule_config: { relation_type: document.getElementById(`frel-${id}`).value, by_secondary: document.getElementById(`fsec-${id}`).checked } };
}
async function previewRule(tid, id, confirmSave) {
  const body = collectRule(tid, id);
  const box = document.getElementById(`fimp-${id}`);
  let r;
  try { r = await apiPost(`/api/template_nodes/${id}/rule`, { ...body, confirm: false }); }
  catch (e) { box.innerHTML = `<div class="f-err">${attr(e.message)}</div>`; return; }
  const diff = r.profit_after - r.profit_before;
  const txt = r.changed_count
    ? `这条规则 ${r.trip_count} 车里有 <b>${r.changed_count}</b> 车净利润会变：合计 ¥${fmtMoney(r.profit_before)} → <b>¥${fmtMoney(r.profit_after)}</b>（${diff >= 0 ? '+' : ''}${fmtMoney(diff)}）。` +
      (r.changed_locked ? `其中 ${r.changed_locked} 车已固化，统计里仍按固化快照不变。` : '')
    : '改了之后所有车的净利润都不变。';
  const list = r.changed.slice(0, 8).map(c => `${tripLink(c.trip_id, '#' + c.trip_id)} ¥${fmtMoney(c.before)}→¥${fmtMoney(c.after)}${c.locked ? '(已固化)' : ''}`).join('　');
  if (!confirmSave) { box.innerHTML = `<div class="f-imp">${txt}${list ? '<div class="muted">' + list + '</div>' : ''}</div>`; return; }
  if (r.changed_count && !confirm(`确认保存？\n${txt.replace(/<[^>]+>/g, '')}`)) return;
  await apiPost(`/api/template_nodes/${id}/rule`, { ...body, confirm: true });
  toast('已保存计算口径');
  renderFormula(tid);
}
async function undoRule(tid, id) {
  const h = await apiGet(`/api/template_nodes/${id}/rule_history`);
  if (!h.length) { toast('这个节点还没改过'); return; }
  const old = JSON.parse(h[0].old_value);
  const cfg = JSON.parse(old.config || '{}');
  const body = { calc_rule_type: old.type, calc_rule_config: cfg.relation_type ? { ...cfg, by_secondary: !!cfg.secondary_type } : cfg };
  const r = await apiPost(`/api/template_nodes/${id}/rule`, { ...body, confirm: false });
  if (!confirm(`撤销到 ${h[0].changed_at} 之前的口径？净利润合计 ¥${fmtMoney(r.profit_before)} → ¥${fmtMoney(r.profit_after)}`)) return;
  await apiPost(`/api/template_nodes/${id}/rule`, { ...body, confirm: true });
  toast('已撤销'); renderFormula(tid);
}

loadAll();
