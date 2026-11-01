/** 规则 hub · 只读关系思维导图（复用 rate_links 图数据） */
const MM_COLUMNS = ['processing_base', 'dispatcher', 'invoice_source', 'account', 'steel_mill'];
const MM_LABELS = {
  processing_base: '供货厂',
  dispatcher: '调货员',
  invoice_source: '票源',
  account: '户头',
  steel_mill: '钢厂',
};

async function renderRulesMindmap() {
  const host = document.getElementById('rules-mindmap');
  if (!host) return;
  const asOf = getRulesAsOf() || (await fetchSettlementPeriodMeta()).as_of_today;
  const data = await apiGet('/api/rules/mindmap?as_of=' + encodeURIComponent(asOf));
  const nodesByType = {};
  MM_COLUMNS.forEach(t => { nodesByType[t] = []; });
  (data.nodes || []).forEach(n => {
    if (nodesByType[n.type]) nodesByType[n.type].push(n);
  });

  const colW = 200, rowH = 52, padTop = 44, padSide = 12, bypassY = 34;
  const maxRows = Math.max(1, ...MM_COLUMNS.map(t => nodesByType[t].length));
  const width = padSide * 2 + colW * MM_COLUMNS.length;
  const height = padTop + rowH * maxRows + 24;
  const pos = {}, colIndex = {};
  MM_COLUMNS.forEach((type, ci) => {
    const list = nodesByType[type];
    const cx = padSide + colW * ci + colW / 2;
    list.forEach((n, ri) => {
      pos[n.id] = { x: cx, y: padTop + rowH * ri + rowH / 2, node: n };
      colIndex[n.id] = ci;
    });
  });

  let svg = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" class="mindmap-svg">`;
  MM_COLUMNS.forEach((type, ci) => {
    const x = padSide + colW * ci + colW / 2;
    svg += `<text x="${x}" y="18" font-size="11" font-weight="600" text-anchor="middle" fill="#64748b">${MM_LABELS[type]}</text>`;
  });

  (data.edges || []).forEach(e => {
    if (e.kind === 'global_fallback') return;
    if (!e.from || !e.to || !pos[e.from] || !pos[e.to]) return;
    const a = pos[e.from], b = pos[e.to];
    const colDiff = Math.abs((colIndex[e.from] ?? 0) - (colIndex[e.to] ?? 0));
    const color = e.kind === 'rate' ? '#2563eb' : '#94a3b8';
    if (colDiff > 1) {
      svg += `<path d="M${a.x},${a.y} C${a.x},${bypassY} ${b.x},${bypassY} ${b.x},${b.y}" fill="none" stroke="${color}" stroke-width="1.2"><title>${e.label}</title></path>`;
    } else {
      svg += `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${color}" stroke-width="1.2"><title>${e.label}</title></line>`;
    }
  });

  MM_COLUMNS.forEach(type => {
    nodesByType[type].forEach(n => {
      const p = pos[n.id];
      if (!p) return;
      const badges = (n.badges || []).map(b => b.label).join(' ');
      const href = mindmapNodeHref(n);
      svg += `<a href="${href}" class="mm-node"><rect x="${p.x - 72}" y="${p.y - 18}" width="144" height="36" rx="8" fill="#fff" stroke="#cbd5e1"/>`;
      svg += `<text x="${p.x}" y="${p.y + 4}" font-size="11" text-anchor="middle" fill="#0f172a">${n.name.length > 10 ? n.name.slice(0, 9) + '…' : n.name}</text>`;
      if (badges) {
        svg += `<text x="${p.x}" y="${p.y + 16}" font-size="8" text-anchor="middle" fill="#64748b">${badges.slice(0, 14)}</text>`;
      }
      svg += `</a>`;
    });
  });
  svg += '</svg>';
  host.innerHTML = `<p class="muted" style="font-size:12px;margin:0 0 8px;">参考日 ${data.as_of} · 点击节点去档案；蓝线=费率 · 灰线=归属。编辑费率 → <a href="/rate_links">费率与关系</a></p>${svg}`;
}

function mindmapNodeHref(n) {
  const id = n.entity_id;
  if (n.type === 'invoice_source') return `/archives?tab=invoice_sources`;
  if (n.type === 'processing_base') return `/archives?tab=processing_bases`;
  if (n.type === 'dispatcher') return `/archives?tab=dispatchers`;
  if (n.type === 'account') return `/archives?tab=accounts`;
  if (n.type === 'steel_mill') return `/archives?tab=steel_mills`;
  return '/archives';
}

document.addEventListener('DOMContentLoaded', () => {
  if (document.getElementById('rules-mindmap')) renderRulesMindmap();
});
document.addEventListener('rules-as-of-changed', () => renderRulesMindmap());
