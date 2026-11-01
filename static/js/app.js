// 通用工具函数
async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.error || msg; } catch (e) {}
    throw new Error(msg);
  }
  if (res.status === 204) return null;
  return res.json();
}
const apiGet = (url) => api("GET", url);
const apiPost = (url, body) => api("POST", url, body);
const apiPut = (url, body) => api("PUT", url, body);
const apiDelete = (url) => api("DELETE", url);

function fmtMoney(v) {
  if (v === null || v === undefined || v === "") return "-";
  const n = Number(v);
  return n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtNum(v, digits = 2) {
  if (v === null || v === undefined || v === "") return "-";
  const n = Number(v);
  return n.toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

// ---------------------------------------------------------------------------
// 术语表（A档「先治乱」2026-10-09）：系统里的黑话，鼠标悬停看一句人话解释。
// 全站统一从这一份取，避免同一个词在五个页面五种说法。
// 完整对照表（黑话 ↔ 系统叫法 ↔ 在哪个页面）在「术语与操作对照表」页。
// ---------------------------------------------------------------------------
const TERMS = {
  chain: '链条规则：一车货从供货厂到钢厂走的那条业务链——谁开票、谁垫资、谁付款。车次选了哪条链条，就算哪套钱。',
  chain_short: '链条：链条规则的简写，在「链条规则」页可改。',
  quota: '票源额度：这家票源方在我们这边最多能开多少票（金额）。月度额度按结算期累计，年度额度按自然年累计。',
  water_level: '水位：额度已经用掉多少的进度条。正常是蓝的，到预警线转黄，超过 100% 转红。',
  settle_period: '结算期：每月 26 日 ~ 次月 25 日算一个结算期。量补档次、月度票额都按这个窗口算，不是自然月。',
  advance_fee: '垫资费：我们先把钱垫给供货厂、等国企或钢厂回款才收回来，这段占用资金的成本，按 元/吨 计。',
  node: '节点：链条规则里一个"算钱的口子"（例如"给客户付"按对外点位算）。改节点就是改这条链的计算口径。',
  peipiao: '配票（M1）：用我方的票源方额度开票，赚差价；占月度票额。',
  reverse: '反向（M5）：票源是自然人、反向开票；占年度票额。',
  carry: '自带票（M2~M4，也叫直签带票）：供货方自己有票、自己开，我方只做通道/服务，不占我方票额。',
  account: '户头：我们用来跟钢厂/国企结算的那个"我方主体"。一个钢厂通常只绑一个户头，选了钢厂会自动跟上。',
  freeze: '固化：把这一车当前的利润数字存下来。固化后档案费率再怎么改，这一车的数字也不跟着变。',
  profit_calc: '利润估算：已固化的车次取固化快照，没固化的按当前档案参数实时算——所以它只是估算，不是最终会计数。',
  baseline: '应收应付基准：按链条规则自动算出来的"这车该收多少、该付多少"，用来跟实际开票/打款核对。',
  aging: '账龄：这笔待办从起算日（开票看结算日、回款看开票日）到现在过了多少天，用来盯超期。',
  mixed: '混料：一车里装了好几种货型、拆不开，以钢厂结算单的合计为准。',
  deduction: '扣杂：钢厂验货时按杂质比例扣掉的吨位，扣完剩下的才是结算吨位。',
  bill_face: '票面额：发票上的含税金额。自带票的票面额 = 含税结算额，不减垫资费。',
  ticket_diff: '票差留存利润：经过我方票源方开票才有的一段差额（转出点位 − 进项点位），单独留存，不计入净利润。',
  plate_profile: '车牌画像：这个车牌历史上常走谁家的货、用哪条链条。输入车牌后系统按历史（同一取值≥80%）自动带出，变来变去的字段就不猜、留给你选。',
  recycle: '回收站：删除的车次、档案先放这里，30 天内可按原编号恢复，过期自动清理。作废的车次不在回收站，它还留在列表里。',
  entry_defaults: '录入默认值：新建车次时自动填的东西（发货地、只有一个钢厂时的钢厂和户头、接单日期今天）。',
  amount: '结算金额：钢厂结算单上的含税金额，是后面一切算钱（垫资费、量补、利润）的起点。',
  tonnage: '结算吨位：毛重减皮重得净重，再扣掉杂质吨位，剩下的才是钢厂认账的吨位。',
  split_path: '配票/反向（差价模型）：赚的是"给客户付"和"前端实付"之间的差价。',
  service_path: '带票带货（服务费模型）：赚的是按吨收的服务费，不碰票源成本。',
};
// 生成带悬停解释的词。key 见 TERMS；找不到就原样返回文字，不会漏出坏符号。
function term(text, key, cls) {
  const tip = TERMS[key];
  if (!tip) return text;
  return `<span class="term ${cls || ''}" title="${tip.replace(/"/g, "&quot;")}">${text}</span>`;
}
function termTip(key) { return (TERMS[key] || "").replace(/"/g, "&quot;"); }
function el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  (Array.isArray(children) ? children : [children]).forEach((c) => {
    if (c === null || c === undefined) return;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  });
  return e;
}
// ---------------- 链条规则关系图渲染（2026-09-19新增，供chain_rules.js和trips.js共用：
// 链条规则页面画"抽象/编辑器口径"的图，车次详情页画"这一笔车次实际替换了主体名字+带真实
// 数值"的图，同一套渲染逻辑，只是后端 /api/flow_templates/<id>/graph 和
// /api/trips/<id>/flow_graph 返回的node/edge数据不同）----------------
const ROLE_TYPE_META = {
  supplier: { label: '供货厂', color: '#0891b2' },
  dispatcher: { label: '调货员', color: '#7c3aed' },
  our_account: { label: '我方户头', color: '#2563eb' },
  ticket_source: { label: '票源方', color: '#ea580c' },
  soe: { label: '国企', color: '#059669' },
  mill: { label: '钢厂', color: '#dc2626' },
  output: { label: '结果', color: '#64748b' },
};
function roleLabel(rt) { return (ROLE_TYPE_META[rt] || {}).label || rt || '-'; }
function roleColor(rt) { return (ROLE_TYPE_META[rt] || {}).color || '#64748b'; }

// 环节类型目录（开票/打款/出结算单/做台账/过磅单/发货）现在由后端
// engine.HOP_TYPE_CATALOG 统一定义，前端启动时拉一次缓存起来：以后业务上再缺类型，
// 只改后端那一处，下拉框和关系图配色都会自动跟上。下面这份是兜底默认值，万一接口没拉到
// 也不至于整张图画不出来。
let HOP_TYPE_META = {
  goods: { label: '发货/物流', color: '#94a3b8', path_group: 'logistics' },
  invoice: { label: '开票', color: '#ea580c', path_group: 'ticket' },
  payment: { label: '打款', color: '#2563eb', path_group: 'finance' },
};
let HOP_TYPE_LIST = Object.keys(HOP_TYPE_META).map(c => Object.assign({ code: c }, HOP_TYPE_META[c]));
let _hopTypesLoaded = null;
async function loadHopTypes() {
  if (!_hopTypesLoaded) {
    _hopTypesLoaded = apiGet('/api/flow_edge_hop_types').then(list => {
      if (Array.isArray(list) && list.length) {
        HOP_TYPE_LIST = list;
        HOP_TYPE_META = {};
        list.forEach(h => { HOP_TYPE_META[h.code] = h; });
      }
    }).catch(() => { /* 用兜底默认值 */ });
  }
  return _hopTypesLoaded;
}
function hopLabel(h) { return (HOP_TYPE_META[h] || {}).label || h; }
function hopColor(h) { return (HOP_TYPE_META[h] || {}).color || '#94a3b8'; }

// 点击图上任一环节弹出的详情面板（2026-09-19：用户希望"业务细节能在图中表达出来，
// 或点击细节看详情能看得到"）。链条规则页面点开看的是口径；车次详情页点开看的是这一笔
// 车次的真实金额和单据号——两边用的是同一份数据，所以图和账一定对得上。
function showHopDetail(e) {
  document.getElementById('hop-detail-overlay')?.remove();
  const row = (k, v) => v === null || v === undefined || v === '' ? ''
    : `<div style="display:flex;gap:10px;padding:5px 0;border-bottom:1px solid var(--gray-100);">
         <span style="min-width:96px;color:var(--gray-500);font-size:12px;">${k}</span>
         <span style="font-size:12.5px;">${v}</span></div>`;
  const money = (v) => v === null || v === undefined ? '' : '¥ ' + fmtMoney(v);
  const hasAmt = e.has_amount !== false;
  const box = document.createElement('div');
  box.id = 'hop-detail-overlay';
  box.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.35);z-index:900;'
    + 'display:flex;align-items:center;justify-content:center;';
  box.onclick = (ev) => { if (ev.target === box) box.remove(); };
  box.innerHTML = `
    <div class="card" style="max-width:460px;width:92%;max-height:80vh;overflow:auto;" onclick="event.stopPropagation()">
      <div class="toolbar" style="margin-bottom:6px;">
        <b style="font-size:15px;">${e.from_label || '?'} → ${e.to_label || '?'}</b>
        <div class="spacer"></div>
        <button class="btn-sm" onclick="document.getElementById('hop-detail-overlay').remove()">关闭</button>
      </div>
      <div style="margin-bottom:8px;">
        <span class="pill" style="background:${hopColor(e.hop_type)}1a;color:${hopColor(e.hop_type)};">${e.hop_label || hopLabel(e.hop_type)}</span>
        ${hasAmt ? '<span class="pill green">参与算钱</span>'
                 : '<span class="pill">仅流程说明 · 不参与算钱</span>'}
        ${e.is_auto ? '<span class="pill">自动推导 · 待核实</span>' : ''}
      </div>
      ${row('这一步是', e.notes || '（没写说明）')}
      ${row('第几步', e.seq)}
      ${row('金额口径', e.node_label ? e.node_label + `<span style="color:var(--gray-400);font-size:11px;"> （计算节点 ${e.node_key_ref}）</span>` : '未挂计算节点')}
      ${row('这笔车次金额', hasAmt ? money(e.value) : '')}
      ${row('实际发票金额', money(e.actual_amount))}
      ${row('发票号', e.doc_no)}
      ${row('单据状态', e.doc_status === 'in_stock' ? '票库存中（待恢复开票）' : e.doc_status)}
      <div style="font-size:11.5px;color:var(--gray-500);margin-top:10px;">
        ${hasAmt
          ? '这一步挂了计算节点，图上显示的金额跟结算/利润用的是同一份计算结果。'
          : '这一步只是流程说明，改它不会影响任何数字；想让它带金额，去下方环节列表里给它选一个计算节点。'}
      </div>
    </div>`;
  document.body.appendChild(box);
}

// 按边的seq顺序算一个"最长路径层级(rank)"做左右分层，rank差>1的边(跳层)画成绕到上方的
// 曲线，避免穿过中间层节点(呼应rate_links.js关系图同批修复的跨列直线穿过节点bug)。
// 分层用"第一次到达就定层"，而不是"后面路径更长就更新"——链条规则里同一个角色(比如
// "我方户头")可能在流程里出现好几次(先收一次钱、后面又付一次)，如果每次都用max更新层级，
// 会导致前面已经画好的节点位置被后面的边反过来往右推，越推越乱。定层之后，回头/交叉的边
// 照样画，只是可能画成从右指回左，这对"流程里有往返"的场景是正常且可读的。
//
// 2026-09-19 再修（用户反馈"打款和开票一个来回被覆盖了"）：同一对节点之间经常有好几条边
// ——最典型的就是"我们开票给欧冶、欧冶再打款回来"这种一来一回，原来两条边端点相同、
// 画出来完全重叠，线和标签互相盖住，看上去只剩一条。现在按"无向节点对"把平行边分组，
// 组内沿垂直方向均匀错开，再给每条边画箭头标明方向、线端裁到节点框边缘(不再插进框里)。
async function renderChainGraph(containerId, graph) {
  await loadHopTypes();
  const container = document.getElementById(containerId);
  if (!container) return;
  if (!graph.edges.length) {
    container.innerHTML = '<div class="empty-hint">还没有流程环节可画。</div>';
    return;
  }
  const rank = {};
  graph.edges.forEach(e => {
    if (!(e.from in rank)) rank[e.from] = 0;
    const proposed = rank[e.from] + 1;
    if (!(e.to in rank)) rank[e.to] = proposed;
  });
  graph.nodes.forEach(n => { if (!(n.key in rank)) rank[n.key] = 0; });

  const byRank = {};
  graph.nodes.forEach(n => { (byRank[rank[n.key]] = byRank[rank[n.key]] || []).push(n); });
  const maxRank = Math.max(0, ...Object.values(rank));
  const maxRows = Math.max(1, ...Object.values(byRank).map(l => l.length));

  // 列宽要留出"节点框 + 框之间放边标签的空档"：节点框固定150宽，列宽250，
  // 于是相邻两个框之间有100px空档专门给"开票/打款/出结算单"这些标签用。
  // 行高74是为了让同一对节点之间错开的几条平行边(以及各自的标签)上下排得开。
  const colW = 250, nodeW = 150, rowH = 74, padTop = 56, padSide = 16, bypassY = 42;
  const width = padSide * 2 + colW * (maxRank + 1);
  const height = padTop + rowH * maxRows + 24;

  const pos = {};
  for (let r = 0; r <= maxRank; r++) {
    const list = byRank[r] || [];
    const cx = padSide + colW * r + colW / 2;
    list.forEach((n, i) => { pos[n.key] = { x: cx, y: padTop + rowH * i + rowH / 2 }; });
  }

  const nodeHalfW = nodeW / 2, nodeHalfH = 16;
  // 把线端从节点中心收到节点框边缘，箭头才不会被框盖住
  const clipToBox = (from, to) => {
    const dx = to.x - from.x, dy = to.y - from.y;
    if (!dx && !dy) return to;
    const sx = dx ? nodeHalfW / Math.abs(dx) : Infinity;
    const sy = dy ? nodeHalfH / Math.abs(dy) : Infinity;
    const s = Math.min(1, Math.min(sx, sy));
    return { x: to.x - dx * s, y: to.y - dy * s };
  };
  const arrow = (x, y, angle, color, size = 6.5) => {
    const a1 = angle + Math.PI * 0.84, a2 = angle - Math.PI * 0.84;
    return `<path d="M${x},${y} L${x + size * Math.cos(a1)},${y + size * Math.sin(a1)} `
         + `L${x + size * Math.cos(a2)},${y + size * Math.sin(a2)} Z" fill="${color}"/>`;
  };

  // 平行边分组：同一对节点之间(不分方向)的所有边归一组，组内错开画
  const pairGroups = {};
  graph.edges.forEach(e => {
    const key = [e.from, e.to].sort().join('\u0000');
    (pairGroups[key] = pairGroups[key] || []).push(e);
  });
  const pairSeen = {};

  // 按自然尺寸画、外面套一层横向滚动容器，而不是 width="100%" 缩放：链条一长，
  // 缩放会把字压得看不清；宁可横向拖一下，也要保证每个环节的字是清楚的。
  let svg = `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" style="font-family:inherit;display:block;">`;
  graph.edges.forEach(e => {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) return;
    // 有金额(挂了计算节点)=实线彩色；纯流程说明=细虚线+灰色，一眼看出改它不影响任何数字
    const hasAmt = e.has_amount !== false;
    const color = hasAmt ? hopColor(e.hop_type) : '#cbd5e1';
    const sw = hasAmt ? 1.6 : 1.1;
    const dash = hasAmt ? '' : ' stroke-dasharray="4,3"';
    const tip = [e.notes, hasAmt ? '' : '仅流程说明，不参与算钱'].filter(Boolean).join(' · ');
    const hopJson = JSON.stringify(Object.assign({}, e, {
      from_label: (graph.nodes.find(n => n.key === e.from) || {}).label,
      to_label: (graph.nodes.find(n => n.key === e.to) || {}).label,
      hop_label: hopLabel(e.hop_type),
    })).replace(/'/g, '&apos;');
    const rankDiff = Math.abs(rank[e.from] - rank[e.to]);
    const key = [e.from, e.to].sort().join('\u0000');
    const n = pairGroups[key].length;
    const i = (pairSeen[key] = (pairSeen[key] || 0) + 1) - 1;
    const spread = (i - (n - 1) / 2) * 26; // 组内第几条，决定上下错开多少

    let midX, midY, tipX, tipY, tipAngle;
    if (rankDiff > 1) {
      // 跳层：绕到上方走曲线，平行的几条各用不同高度，互不叠在一起
      const by = bypassY - i * 12;
      const sx = a.x, sy = a.y - nodeHalfH, ex = b.x, ey = b.y - nodeHalfH - 2;
      svg += `<path d="M${sx},${sy} C${sx},${by} ${ex},${by} ${ex},${ey}" fill="none" stroke="${color}" stroke-width="${sw}"${dash} style="cursor:pointer;" onclick='showHopDetail(${hopJson})'><title>${tip}</title></path>`;
      midX = (a.x + b.x) / 2; midY = by;
      tipX = ex; tipY = ey; tipAngle = Math.PI / 2; // 从上方扎进节点框顶边
    } else {
      // 同层/相邻层：直线，沿垂直方向按 spread 错开，两端裁到节点框边缘。
      // 注意错开方向必须按"排序后的规范端点"算，不能按这条边自己的方向算——否则
      // "开票过去"和"打款回来"这一对，两条边的垂直方向正好相反，一个-13一个+13
      // 会同时落到同一侧，白错开了(这正是用户看到的"一个来回被覆盖了")。
      const [k1, k2] = [e.from, e.to].sort();
      const cdx = pos[k2].x - pos[k1].x, cdy = pos[k2].y - pos[k1].y;
      const clen = Math.hypot(cdx, cdy) || 1;
      const ox = -cdy / clen * spread, oy = cdx / clen * spread;
      const dx = b.x - a.x, dy = b.y - a.y;
      const a2 = { x: a.x + ox, y: a.y + oy }, b2 = { x: b.x + ox, y: b.y + oy };
      const start = clipToBox(b2, a2), end = clipToBox(a2, b2);
      svg += `<line x1="${start.x}" y1="${start.y}" x2="${end.x}" y2="${end.y}" stroke="${color}" stroke-width="${sw}"${dash} style="cursor:pointer;" onclick='showHopDetail(${hopJson})'><title>${tip}</title></line>`;
      midX = (start.x + end.x) / 2; midY = (start.y + end.y) / 2;
      tipX = end.x; tipY = end.y; tipAngle = Math.atan2(dy, dx);
    }
    svg += arrow(tipX, tipY, tipAngle, color);

    // 标签分两行写：第一行是环节类型(开票/打款/出结算单…)，第二行才是金额/单据号。
    // 两个框之间只有100px空档，挤成一行的话带金额的标签会被截断，恰恰金额才是要看的。
    const lines = [hopLabel(e.hop_type) + (hasAmt ? '' : '（仅说明）')];
    const extra = [];
    if (e.value !== undefined && e.value !== null) {
      extra.push(`¥${Number(e.value).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`);
    }
    if (e.doc_no) extra.push(`#${e.doc_no}`);
    if (extra.length) lines.push(extra.join(' '));
    const clip = (s) => (s.length > 11 ? s.slice(0, 10) + '…' : s);
    const shown = lines.map(clip);
    const halfW = Math.min(Math.max(...shown.map(s => s.length)) * 3.9 + 3, 48);
    const boxH = shown.length > 1 ? 25 : 14;
    const top = midY - boxH / 2;
    svg += `<rect x="${midX - halfW}" y="${top}" width="${halfW * 2}" height="${boxH}" rx="3" fill="#fff" opacity="0.95"/>`;
    shown.forEach((s, li) => {
      const ty = top + 10.5 + li * 11;
      svg += `<text x="${midX}" y="${ty}" font-size="${li ? 9.5 : 10}" text-anchor="middle" fill="${color}">${s}<title>${e.notes || ''}</title></text>`;
    });
  });
  graph.nodes.forEach(n => {
    const p = pos[n.key];
    const c = roleColor(n.role_type);
    const name = n.label && n.label.length > 11 ? n.label.slice(0, 10) + '…' : (n.label || '');
    svg += `<rect x="${p.x - nodeHalfW}" y="${p.y - 16}" width="${nodeW}" height="32" rx="7" fill="#fff" stroke="${c}" stroke-width="1.6"/>`;
    svg += `<text x="${p.x}" y="${p.y - 2}" font-size="11.5" font-weight="600" text-anchor="middle" fill="#0f172a">${name}<title>${n.label || ''}</title></text>`;
    svg += `<text x="${p.x}" y="${p.y + 12}" font-size="9" text-anchor="middle" fill="${c}">${roleLabel(n.role_type)}</text>`;
  });
  svg += `</svg>`;
  container.innerHTML = `<div style="overflow-x:auto;">${svg}</div>`;
}

// 轻量级 SVG 柱状图，不依赖任何外部图表库（本系统离线本地运行，避免依赖 CDN）。
// data: [{label, value}], opts: {height, color, valueFmt(v), maxLabels}
function renderBarChart(container, data, opts = {}) {
  const height = opts.height || 160;
  const color = opts.color || "#2563eb";
  const valueFmt = opts.valueFmt || ((v) => fmtNum(v, 1));
  const width = Math.max(container.clientWidth || 480, data.length * 34);
  const padTop = 18, padBottom = 26, padSide = 6;
  const plotH = height - padTop - padBottom;
  const maxV = Math.max(1, ...data.map((d) => d.value));
  const barGap = 6;
  const barW = Math.max(8, (width - padSide * 2) / data.length - barGap);

  let bars = "";
  data.forEach((d, i) => {
    const x = padSide + i * (barW + barGap);
    const h = maxV > 0 ? (d.value / maxV) * plotH : 0;
    const y = padTop + (plotH - h);
    const rect = `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(h,0).toFixed(1)}" rx="2" fill="${color}">
      <title>${d.label}: ${valueFmt(d.value)}${d.href ? '（点击查看车次）' : ''}</title>
    </rect>`;
    // 内链：数据项带 href 时整根柱子可点，跳到对应车次
    bars += d.href ? `<a href="${d.href}" style="cursor:pointer">${rect}</a>` : rect;
    if (d.value > 0) {
      bars += `<text x="${(x + barW / 2).toFixed(1)}" y="${(y - 4).toFixed(1)}" font-size="9" text-anchor="middle" fill="#64748b">${valueFmt(d.value)}</text>`;
    }
    bars += `<text x="${(x + barW / 2).toFixed(1)}" y="${height - 8}" font-size="9" text-anchor="middle" fill="#94a3b8">${d.label}</text>`;
  });
  const baselineY = padTop + plotH;
  container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" style="overflow:visible;">
    <line x1="${padSide}" y1="${baselineY}" x2="${width - padSide}" y2="${baselineY}" stroke="#e2e8f0" />
    ${bars}
  </svg>`;
}

// 利润构成公式：净利润 = 量补 - 垫资成本 (+ 其他净调整)，逐项列出，每项都带一行小字公式说明。
// 复用对象：dashboard.js(首页各钢厂周期卡片) 和 reports.js(统计报表任意维度汇总)共用，
// 两处传入的对象字段名完全一致(都来自 app.py::_empty_stats_bucket/_accum_bucket 的产出)，
// 所以口径永远一致，不会出现"首页一个算法、报表页另一个算法"的情况。
// m: 数据对象；prefix: 字段名前缀(dashboard.js的"本期/上期"用 ''/'prev_'，reports.js留空即可)。
// 车/船分开的小字摘要（2026-09-19用户要求"车船统计分开"）：一条船的吨位是一辆车的十倍
// 上下，合在一起看"车次数"、按车次平均的任何数字都会被带偏，所以凡是展示汇总数字的地方
// （首页卡片/钢厂周期卡片/统计报表）都在下面补一行车船分开的明细。数据来自聚合桶里的
// truck_*/ship_* 字段（app.py::_accum_bucket 累加），跟主数字同源，不会对不上。
// mode：'tonnage'(默认) 次数/吨位 | 'profit' 次数·利润 | 'both' 吨位和利润都要、每类各占一行
// （'both' 用在信息量大的周期卡片上：如果分成吨位、利润两行分别写，两行都以"车 71次"开头、
//   又没东西说明哪行是哪个，反而更难读）。
// 利润也必须分开看——6条船贡献的利润跟71辆车接近。
function transportSplitHtml(m, prefix, mode) {
  prefix = prefix || '';
  const tc = m[prefix + 'truck_trip_count'] || 0, sc = m[prefix + 'ship_trip_count'] || 0;
  if (!tc && !sc) return '';
  const both = mode === 'both', isProfit = mode === 'profit';
  // 某一类一条都没有时就不显示它，免得到处是"船 0次 / 0吨"这种没信息量的占位
  const part = (cls, label, p, c) => {
    if (!c) return '';
    const ton = m[prefix + p + '_tonnage'] || 0, profit = m[prefix + p + '_profit'] || 0;
    const tonTxt = `${c}次 / ${fmtNum(ton)}吨（均 ${fmtNum(ton / c, 1)}吨）`;
    const txt = both ? `${tonTxt} · 利润 ¥${fmtMoney(profit)}`
      : isProfit ? `${c}次 · ¥${fmtMoney(profit)}` : tonTxt;
    return `<span class="${cls}">${label} ${txt}</span>`;
  };
  return `<div class="transport-split${both ? ' stacked' : ''}">`
    + part('t-truck', '车', 'truck', tc) + part('t-ship', '船', 'ship', sc) + '</div>';
}

function profitFormulaHtml(m, prefix) {
  prefix = prefix || '';
  const reward = m[prefix + 'reward_amount'] || 0;
  const advance = m[prefix + 'advance_fee_cost'] || 0;
  const other = m[prefix + 'other_net'] || 0;
  const profit = m[prefix + 'profit'] || 0;
  const reserved = m[prefix + 'reserved_profit'] || 0;
  const reservedTripCount = m[prefix + 'reserved_trip_count'] || 0;
  const tonnage = m[prefix + 'tonnage'] || 0;
  const avgRewardPrice = tonnage ? reward / tonnage : 0;
  const splitAdvance = m[prefix + 'split_advance'] || 0;
  const splitTripCount = m[prefix + 'split_trip_count'] || 0;
  const serviceAdvance = m[prefix + 'service_advance'] || 0;
  const serviceTripCount = m[prefix + 'service_trip_count'] || 0;
  const serviceFeeIncome = m[prefix + 'service_fee_income_total'] || 0;
  const dispatcherCommission = m[prefix + 'dispatcher_commission_total'] || 0;
  const ticketCost = m[prefix + 'ticket_cost_total'] || 0;
  const otherCost = m[prefix + 'other_cost_total'] || 0;

  // 每一项除了抽象公式(formula-note)，再加一行按路径/分项代入实际数字的计算过程(formula-actual)，
  // 有几条路径/几个分项就写成 A(数字)+B(数字)=结果 的形式，而不是只给一个笼统的加总数。
  const advanceParts = [];
  if (splitTripCount) advanceParts.push(`路径1/3(${splitTripCount}笔) ¥${fmtMoney(splitAdvance)}`);
  if (serviceTripCount) advanceParts.push(`路径2(${serviceTripCount}笔) ¥${fmtMoney(serviceAdvance)}`);
  const advanceActual = advanceParts.length
    ? `= ${advanceParts.join(' + ')} = ¥${fmtMoney(advance)}`
    : `= ¥${fmtMoney(advance)}`;

  const otherLine = Math.abs(other) > 0.005
    ? `<div class="formula-line"><span>${other >= 0 ? '+' : '-'} 其他净调整<div class="formula-note">= 服务费收入 − 调货提成 − 买票成本 − 其他成本，逐车次累加；路径2(带票带货)车次主要靠服务费收入，路径1/3(配票/反向)车次这里通常只有调货提成/切割费等减项</div><div class="formula-actual">= 服务费收入¥${fmtMoney(serviceFeeIncome)} − 调货提成¥${fmtMoney(dispatcherCommission)} − 买票成本¥${fmtMoney(ticketCost)} − 其他成本¥${fmtMoney(otherCost)} = ¥${fmtMoney(other)}</div></span><span>¥ ${fmtMoney(Math.abs(other))}</span></div>`
    : '';
  // 留存利润：经过我们的票源方(如益周)才有的票差，专门留存，刻意放在"=净利润"这一行之后、
  // 单独一行展示，不参与上面的加总，避免被误读成净利润的一部分。
  const reservedLine = Math.abs(reserved) > 0.005
    ? `<div class="formula-line reserved-line"><span>另：留存利润<div class="formula-note">= Σ 前端(开票金额) × (转出点位 − 进项点位) / 100，仅"经过我们"结算形式的票源方才有(目前仅益周)，专项留存，不计入净利润</div><div class="formula-actual">= 益周(${reservedTripCount}笔) ¥${fmtMoney(reserved)} = ¥${fmtMoney(reserved)}</div></span><span>¥ ${fmtMoney(reserved)}</span></div>`
    : '';
  return `<details class="profit-formula-details">
    <summary>利润构成明细（点击展开计算过程）</summary>
    <div class="profit-formula">
      <div class="formula-line">
        <span>量补金额<div class="formula-note">= Σ(量补单价 × 车次吨位)，逐车次累加；单价按该车次所在钢厂结算周期的累计吨位，对照阶梯规则取值</div><div class="formula-actual">= ${fmtNum(tonnage)}t × ${fmtNum(avgRewardPrice)}元/吨（加权平均单价，各车次实际单价可能不同）= ¥${fmtMoney(reward)}</div></span>
        <span>¥ ${fmtMoney(reward)}</span>
      </div>
      <div class="formula-line">
        <span>− 垫资成本<div class="formula-note">= Σ 个人垫资，逐车次累加；路径1/3(配票/反向)车次 = 给客户付 − 前端实付；路径2(带票带货)车次 = 车次吨位 × 23元/吨垫资单价</div><div class="formula-actual">${advanceActual}</div></span>
        <span>¥ ${fmtMoney(advance)}</span>
      </div>
      ${otherLine}
      <div class="formula-line formula-total"><span>= 净利润</span><span class="${profit>=0?'':'red'}">¥ ${fmtMoney(profit)}</span></div>
      ${reservedLine}
    </div>
  </details>`;
}

function optionsHtml(list, valueKey, labelKey, selected) {
  return list
    .map((it) => {
      const v = it[valueKey];
      const sel = String(v) === String(selected) ? "selected" : "";
      return `<option value="${v}" ${sel}>${it[labelKey]}</option>`;
    })
    .join("");
}

// ---------------------------------------------------------------------------
// 趋势图（2026-09-21）：堆叠柱(车/船吨位) 与 正负柱(利润)。一张图一个量纲，不做双轴。
// 颜色沿用全系统车=#0891b2 船=#7c3aed（已跑配色校验：色盲分离 ΔE15，对比度≥3:1）。
// 悬停整列出浮层提示；段与段之间留2px白缝；图例常驻；数字只标在柱顶合计上。
// rows: [{label, ...}]；series: [{key, label, color}]；opts: {height, fmt, totalLabel}
// ---------------------------------------------------------------------------
const TRUCK_COLOR = '#0891b2', SHIP_COLOR = '#7c3aed', PROFIT_POS = '#16a34a', PROFIT_NEG = '#dc2626';

function chartTip() {
  let t = document.getElementById('chart-tip');
  if (!t) { t = document.createElement('div'); t.id = 'chart-tip'; t.className = 'chart-tip'; document.body.appendChild(t); }
  return t;
}
function bindChartHover(container, rows, htmlFn, hrefFn) {
  const tip = chartTip();
  container.querySelectorAll('[data-i]').forEach(el => {
    if (hrefFn) {
      el.style.cursor = 'pointer';
      el.addEventListener('click', () => { tip.style.display = 'none'; location.href = hrefFn(rows[+el.dataset.i]); });
    }
    el.addEventListener('mousemove', e => {
      tip.innerHTML = htmlFn(rows[+el.dataset.i]);
      tip.style.display = 'block';
      const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
      tip.style.left = x + 'px'; tip.style.top = (e.clientY + 14) + 'px';
      container.querySelectorAll(`.col-hl`).forEach(c => c.setAttribute('opacity', 0));
      const hl = container.querySelector(`.col-hl[data-h="${el.dataset.i}"]`); if (hl) hl.setAttribute('opacity', 1);
    });
    el.addEventListener('mouseleave', () => { tip.style.display = 'none'; container.querySelectorAll('.col-hl').forEach(c => c.setAttribute('opacity', 0)); });
  });
}
function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}
function chartLegend(series) {
  return `<div class="chart-legend">${series.map(s => `<span><i style="background:${s.color}"></i>${s.label}</span>`).join('')}</div>`;
}

function renderStackedBars(container, rows, series, opts = {}) {
  if (!rows.length) { container.innerHTML = '<div class="empty-hint">该范围内没有数据</div>'; return; }
  const H = opts.height || 200, fmt = opts.fmt || (v => fmtNum(v, 0));
  const padL = 44, padR = 8, padT = 16, padB = 26;
  const W = Math.max(container.clientWidth || 600, rows.length * 28 + padL + padR);
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const totals = rows.map(r => series.reduce((a, s) => a + (r[s.key] || 0), 0));
  const maxV = niceMax(Math.max(...totals));
  const step = plotW / rows.length, bw = Math.max(6, Math.min(36, step * 0.62));
  const y = v => padT + plotH - (v / maxV) * plotH;
  let g = '';
  for (let k = 0; k <= 4; k++) {
    const v = maxV * k / 4, yy = y(v);
    g += `<line x1="${padL}" x2="${W - padR}" y1="${yy}" y2="${yy}" stroke="#eef2f7"/>` +
      `<text x="${padL - 6}" y="${yy + 3}" text-anchor="end" font-size="10" fill="#94a3b8">${fmt(v)}</text>`;
  }
  const labelEvery = Math.ceil(rows.length / Math.floor(plotW / 48));
  rows.forEach((r, i) => {
    const cx = padL + step * i + step / 2, x = cx - bw / 2;
    g += `<rect class="col-hl" data-h="${i}" x="${padL + step * i}" y="${padT}" width="${step}" height="${plotH}" fill="#f1f5f9" opacity="0"/>`;
    let acc = 0;
    const segs = series.filter(s => (r[s.key] || 0) > 0);
    segs.forEach((s, j) => {
      const v = r[s.key]; const y1 = y(acc + v), y0 = y(acc); acc += v;
      const top = j === segs.length - 1;
      const h = Math.max(0, y0 - y1 - (j > 0 ? 2 : 0));
      g += top
        ? `<path d="M${x},${y0 - (j > 0 ? 2 : 0)} V${y1 + 4} Q${x},${y1} ${x + 4},${y1} H${x + bw - 4} Q${x + bw},${y1} ${x + bw},${y1 + 4} V${y0 - (j > 0 ? 2 : 0)} Z" fill="${s.color}"/>`
        : `<rect x="${x}" y="${y1}" width="${bw}" height="${h}" fill="${s.color}"/>`;
    });
    if (totals[i] > 0 && rows.length <= 16) g += `<text x="${cx}" y="${y(totals[i]) - 4}" text-anchor="middle" font-size="10" fill="#475569">${fmt(totals[i])}</text>`;
    if (i % labelEvery === 0) g += `<text x="${cx}" y="${H - 8}" text-anchor="middle" font-size="10" fill="#94a3b8">${r.label}</text>`;
    g += `<rect data-i="${i}" x="${padL + step * i}" y="${padT}" width="${step}" height="${plotH + padB}" fill="transparent"/>`;
  });
  g += `<line x1="${padL}" x2="${W - padR}" y1="${y(0)}" y2="${y(0)}" stroke="#cbd5e1"/>`;
  container.innerHTML = chartLegend(series) + `<div style="overflow-x:auto"><svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">${g}</svg></div>`;
  bindChartHover(container, rows, r => `<b>${r.label}</b>` + series.map(s => `<div><i style="background:${s.color}"></i>${s.label} ${fmt(r[s.key] || 0)}</div>`).join('') +
    `<div>合计 ${fmt(series.reduce((a, s) => a + (r[s.key] || 0), 0))}${opts.unit || ''}</div>` + (opts.extra ? opts.extra(r) : '') +
    (opts.href ? '<div class="muted">点击查看这些车次</div>' : ''), opts.href);
}

function renderSignedBars(container, rows, key, opts = {}) {
  if (!rows.length) { container.innerHTML = '<div class="empty-hint">该范围内没有数据</div>'; return; }
  const H = opts.height || 200, fmt = opts.fmt || (v => fmtNum(v, 0));
  const padL = 52, padR = 8, padT = 16, padB = 26;
  const W = Math.max(container.clientWidth || 600, rows.length * 28 + padL + padR);
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const vals = rows.map(r => r[key] || 0);
  const hi = niceMax(Math.max(0, ...vals)), lo = Math.min(0, ...vals) < 0 ? -niceMax(-Math.min(...vals)) : 0;
  const y = v => padT + (hi - v) / (hi - lo) * plotH;
  const step = plotW / rows.length, bw = Math.max(6, Math.min(36, step * 0.62));
  let g = '';
  for (let k = 0; k <= 4; k++) {
    const v = lo + (hi - lo) * k / 4, yy = y(v);
    g += `<line x1="${padL}" x2="${W - padR}" y1="${yy}" y2="${yy}" stroke="#eef2f7"/><text x="${padL - 6}" y="${yy + 3}" text-anchor="end" font-size="10" fill="#94a3b8">${fmt(v)}</text>`;
  }
  const labelEvery = Math.ceil(rows.length / Math.floor(plotW / 48));
  rows.forEach((r, i) => {
    const v = vals[i], cx = padL + step * i + step / 2, x = cx - bw / 2, y0 = y(0), y1 = y(v);
    g += `<rect class="col-hl" data-h="${i}" x="${padL + step * i}" y="${padT}" width="${step}" height="${plotH}" fill="#f1f5f9" opacity="0"/>`;
    if (Math.abs(y0 - y1) > 0.5) {
      const pos = v >= 0, top = Math.min(y0, y1), h = Math.abs(y0 - y1), c = pos ? (opts.color || PROFIT_POS) : PROFIT_NEG;
      g += pos ? `<path d="M${x},${y0} V${top + Math.min(4, h)} Q${x},${top} ${x + 4},${top} H${x + bw - 4} Q${x + bw},${top} ${x + bw},${top + Math.min(4, h)} V${y0} Z" fill="${c}"/>`
        : `<path d="M${x},${y0} V${top + h - Math.min(4, h)} Q${x},${top + h} ${x + 4},${top + h} H${x + bw - 4} Q${x + bw},${top + h} ${x + bw},${top + h - Math.min(4, h)} V${y0} Z" fill="${c}"/>`;
    }
    if (rows.length <= 16 && v) g += `<text x="${cx}" y="${v >= 0 ? y1 - 4 : y1 + 12}" text-anchor="middle" font-size="10" fill="#475569">${fmt(v)}</text>`;
    if (i % labelEvery === 0) g += `<text x="${cx}" y="${H - 8}" text-anchor="middle" font-size="10" fill="#94a3b8">${r.label}</text>`;
    g += `<rect data-i="${i}" x="${padL + step * i}" y="${padT}" width="${step}" height="${plotH + padB}" fill="transparent"/>`;
  });
  g += `<line x1="${padL}" x2="${W - padR}" y1="${y(0)}" y2="${y(0)}" stroke="#94a3b8"/>`;
  container.innerHTML = `<div style="overflow-x:auto"><svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">${g}</svg></div>`;
  bindChartHover(container, rows, r => `<b>${r.label}</b><div>${opts.name || ''} ${fmt(r[key] || 0)}${opts.unit || ''}</div>` + (opts.extra ? opts.extra(r) : '') +
    (opts.href ? '<div class="muted">点击查看这些车次</div>' : ''), opts.href);
}

// ---------------------------------------------------------------------------
// 内链（2026-09-21）：统计/汇总里的数字点一下跳到"车次管理"，带着对应筛选条件。
// params 支持：status / status_in / exclude_voided / mill_id / base_id / dispatcher_id / invoice_source_id /
// template_id（none=未指定那一桶）/ family(split|service_fee) / transport_type / date_field + date_from/date_to / ids / open
// from：跳过去后顶部提示"来自哪里"，方便核对。
// ---------------------------------------------------------------------------
function tripsUrl(params, from) {
  const p = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v === undefined || v === null || v === '') return;
    p.set(k, Array.isArray(v) ? v.join(',') : v);
  });
  if (from) p.set('from', from);
  return '/trips?' + p.toString();
}
function drill(text, params, from, title) {
  return `<a class="drill" href="${tripsUrl(params, from)}" title="${(title || '点击查看这些车次').replace(/"/g, '&quot;')}">${text}</a>`;
}
function tripLink(id, text) { return `<a class="drill" href="/trips?open=${id}" title="打开车次详情">${text}</a>`; }

// 全局轻提示（各页面已有自己的 toast 的会覆盖这个，行为一致）
if (typeof window.toast !== 'function') {
  window.toast = function (msg, ms = 2600) {
    let t = document.getElementById('toast');
    if (!t) { t = document.createElement('div'); t.id = 'toast'; t.className = 'toast'; document.body.appendChild(t); }
    t.textContent = msg; t.classList.add('show');
    clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), ms);
  };
}

// v2.0.1 版本检查：程序文件更新后，运行中的服务还是旧版本（新页面会"找不到"）。
// 每个页面打开时、以及每 2 分钟问一次服务，发现磁盘上的程序比运行中的新，就在顶部提示一键重启。
async function restartApp(btn) {
  const b = document.getElementById('update-banner');
  if (b) { b.hidden = false; b.innerHTML = '正在重启系统，几秒后自动刷新……'; }
  try { await fetch('/api/system/restart', { method: 'POST' }); } catch (e) { /* 重启中断开是正常的 */ }
  const t0 = Date.now();
  await new Promise(r => setTimeout(r, 1500));
  while (Date.now() - t0 < 30000) {
    try {
      const v = await (await fetch('/api/version', { cache: 'no-store' })).json();
      if (!v.code_changed) { location.reload(); return; }
    } catch (e) { /* 还没起来 */ }
    await new Promise(r => setTimeout(r, 800));
  }
  if (b) b.innerHTML = '重启时间有点长：请关掉「启动系统」那个黑色窗口，再双击「启动系统.command」。';
}
async function checkAppVersion() {
  try {
    const v = await (await fetch('/api/version', { cache: 'no-store' })).json();
    const b = document.getElementById('update-banner');
    if (!b) return;
    if (v.code_changed) {
      b.hidden = false;
      b.innerHTML = `系统程序已更新${v.disk_version && v.disk_version !== v.version ? `（v${v.version} → v${v.disk_version}）` : ''}，`
        + `正在运行的还是旧版本，新功能要重启后才有。<button class="btn-sm" onclick="restartApp(this)">现在重启</button>`;
    } else { b.hidden = true; }
  } catch (e) { /* 旧版本服务没有这个接口：什么都不做 */ }
}
document.addEventListener('DOMContentLoaded', () => { checkAppVersion(); setInterval(checkAppVersion, 120000); });

// 经营报告一键导出（2026-09-22）：电脑上有 Chrome/Edge 就直接生成 PDF 下载；没有就打开报告页并弹出打印框（选“存储为 PDF”）
async function exportReportPdf(btn) {
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '正在生成…（约 5~15 秒）'; }
  try {
    const r = await fetch('/api/report/pdf', { cache: 'no-store' });
    if (r.ok) {
      const blob = await r.blob();
      const cd = r.headers.get('Content-Disposition') || '';
      const m = cd.match(/filename\*=UTF-8''([^;]+)/);
      const name = m ? decodeURIComponent(m[1]) : '经营报告.pdf';
      const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name;
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      if (window.toast) toast('已生成：' + name + '（另存了一份在 data/reports）');
    } else {
      let j = {}; try { j = await r.json(); } catch (e) { /* 忽略 */ }
      alert((j.error || '生成失败') + (j.fallback ? '\n\n接下来会打开报告页面，在打印框里选“存储为 PDF”即可。' : ''));
      if (j.fallback) window.open(j.fallback, '_blank');
    }
  } catch (e) { alert('生成失败：' + e.message); }
  finally { if (btn) { btn.disabled = false; btn.textContent = old; } }
}

function exportLedger() {
  window.location.href = '/api/export/ledger';
}

function initMainNavDropdowns() {
  document.querySelectorAll('.nav-group').forEach(group => {
    const btn = group.querySelector('.nav-group-btn');
    if (!btn) return;
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = group.classList.contains('open');
      document.querySelectorAll('.nav-group.open').forEach(g => g.classList.remove('open'));
      if (!open) group.classList.add('open');
    });
  });
  document.addEventListener('click', () => {
    document.querySelectorAll('.nav-group.open').forEach(g => g.classList.remove('open'));
  });
}

document.addEventListener('DOMContentLoaded', initMainNavDropdowns);
