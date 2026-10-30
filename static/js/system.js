// v2「系统」页（2026-09-22）：操作记录 / 回收站 / 数据备份
function esc(v) { return String(v ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'); }
const TABLE_OPTIONS = { trips: '车次', batches: '批次', rate_links: '费率与关系', template_nodes: '链条口径', flow_templates: '链条规则',
  processing_bases: '供货厂', dispatchers: '调货人', invoice_sources: '票源方', base_accounts: '收款账户', accounts: '户头', steel_mills: '钢厂' };
const STATUS_CN = { reported: '已报备', arrived: '已到厂', settled: '已结算', invoiced: '已开票', closed: '已结案', voided: '作废' };

function sysTab(t) {
  document.querySelectorAll('#sys-tabs button').forEach(b => b.classList.toggle('active', b.dataset.t === t));
  if (!document.getElementById('pane-' + t)) t = 'audit';  // 不认识的 tab 参数：回到操作记录
  ['audit', 'recycle', 'backup', 'verify', 'settings', 'mobile'].forEach(k => document.getElementById('pane-' + k).hidden = k !== t);
  document.querySelectorAll('#sys-tabs button').forEach(b => b.classList.toggle('active', b.dataset.t === t));
  ({ audit: loadAudit, recycle: loadRecycle, backup: loadBackups, verify: loadVerify,
     settings: loadSettings, mobile: loadMobile })[t]();
  history.replaceState(null, '', '/system?tab=' + t);
}

// ---------------- 参数设置（C档 2026-10-09）----------------
// 把散在各模块里的写死阈值收拢到一页可改。清单由后端 settings.py 定义（类型/范围/说明），
// 前端只负责画出来 + 提交，范围校验也在后端再做一遍，避免越界值写进去。
let SET_DATA = null;
function setRowHtml(i) {
  const step = i.type === 'float' ? '0.01' : '1';
  return `<div class="set-row">
    <label for="set-${esc(i.key)}">${esc(i.label)}${i.is_default ? '' : ' <span class="pill blue">已改</span>'}</label>
    <div><input id="set-${esc(i.key)}" data-key="${esc(i.key)}" type="number" step="${step}"
        min="${i.min}" max="${i.max}" value="${i.value}"> <span class="muted">${esc(i.unit || '')}</span></div>
    <div class="set-help">${esc(i.help)}<br><span class="muted">默认 ${i.default}${esc(i.unit || '')}，可填 ${i.min} ~ ${i.max}</span></div>
  </div>`;
}
async function loadSettings() {
  const d = await apiGet('/api/system/settings');
  SET_DATA = d;
  document.getElementById('set-body').innerHTML = d.groups.map(g => `
    <div class="set-group">
      <h3>${esc(g)}</h3>
      ${d.items.filter(i => i.group === g).map(setRowHtml).join('')}
    </div>`).join('');
}
async function saveSettings(btn) {
  const body = {};
  document.querySelectorAll('#set-body input[data-key]').forEach(el => { body[el.dataset.key] = el.value; });
  if (btn) { btn.disabled = true; btn.dataset.orig = btn.textContent; btn.textContent = '保存中…'; }
  try {
    const r = await apiPut('/api/system/settings', body);
    toast(`已保存${r.changed && r.changed.length ? '（改了 ' + r.changed.length + ' 项）' : '（没有变化）'}`);
    SET_DATA = r;
    await loadSettings();
  } catch (e) {
    alert('保存失败：' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.orig || '保存'; }
  }
}
function niceVal(field, v) {
  if (v === null || v === undefined || v === '') return '<span class="muted">（空）</span>';
  if (field === 'status') return STATUS_CN[v] || esc(v);
  const s = String(v);
  return esc(s.length > 80 ? s.slice(0, 80) + '…' : s);
}
function recordLink(r) {
  if (r.record_id == null) return '-';
  if (r.table_name === 'trips') return tripLink(r.record_id, '#' + r.record_id);
  return '#' + r.record_id;
}
async function loadAudit() {
  const p = new URLSearchParams();
  ['table', 'record', 'q'].forEach(k => { const v = document.getElementById('a-' + k).value.trim(); if (v) p.set(k === 'record' ? 'record_id' : k, v); });
  const rows = await apiGet('/api/system/audit?' + p.toString());
  document.querySelector('#a-table-list tbody').innerHTML = rows.map(r => `<tr>
    <td class="nowrap">${r.changed_at}</td><td>${esc(r.table_cn)}</td><td>${recordLink(r)}</td><td>${esc(r.field_cn)}</td>
    <td>${niceVal(r.field_name, r.old_value)}</td><td>${niceVal(r.field_name, r.new_value)}</td><td class="muted">${esc(r.note || '')}</td></tr>`).join('')
    || '<tr><td colspan="7" class="empty-hint">没有记录</td></tr>';
}
async function loadRecycle() {
  const d = await apiGet('/api/system/recycle');
  document.getElementById('r-hint').textContent = `删除的车次、批次、档案先放在这里，${d.days} 天内可以恢复（原编号不变），过期后自动清掉。作废的车次不在这里，它们还在车次列表里。`;
  const now = Date.now();
  document.querySelector('#r-list tbody').innerHTML = d.rows.map(r => {
    const left = d.days - Math.floor((now - new Date(r.deleted_at.replace(' ', 'T')).getTime()) / 86400000);
    return `<tr><td class="nowrap">${r.deleted_at}</td><td>${esc(r.table_cn)}</td><td>${esc(r.title)}</td><td>${left} 天</td>
      <td><button class="btn-sm" onclick="restoreItem(${r.id})">恢复</button></td></tr>`;
  }).join('') || '<tr><td colspan="5" class="empty-hint">回收站是空的</td></tr>';
}
async function restoreItem(id) {
  try { const r = await apiPost(`/api/system/recycle/${id}/restore`, {}); toast('已恢复'); loadRecycle();
    if (r.table === 'trips') setTimeout(() => location.href = '/trips?open=' + r.id, 600);
  } catch (e) { alert('恢复失败：' + e.message); }
}
async function loadBackups() {
  const d = await apiGet('/api/system/backups');
  document.getElementById('b-hint').textContent = `备份放在 data/backups/ 文件夹。每次启动系统和每天自动备份，自动备份只保留最近 ${d.keep_auto} 份；标记保留 📌 的备份不会被自动清理。`;
  document.querySelector('#b-list tbody').innerHTML = d.rows.map(b => `<tr><td class="nowrap">${b.time}</td>
    <td>${b.kind === 'auto' ? '自动' : '手动'}${b.pinned ? ' 📌' : ''}</td><td>${esc(b.note)}</td><td class="num">${(b.size / 1024).toFixed(0)} KB</td>
    <td class="num">${b.healthy ? (b.trips_count + ' 车') : '<span style="color:#c00;">损坏</span>'}</td>
    <td class="muted">${esc(b.name)}</td><td>
      <button class="btn-sm" onclick="restoreBackup('${esc(b.name)}')">恢复</button>
      <button class="btn-sm" onclick="pinBackup('${esc(b.name)}', ${!b.pinned})">${b.pinned ? '取消保留' : '保留'}</button>
    </td></tr>`).join('')
    || '<tr><td colspan="7" class="empty-hint">还没有备份</td></tr>';
}
async function pinBackup(name, pin) {
  const r = await apiPost(`/api/system/backups/${encodeURIComponent(name)}/pin`, { pin });
  if (r.ok) { toast(pin ? '已标记保留' : '已取消保留'); loadBackups(); }
  else toast('操作失败：' + (r.error || ''), 3000);
}
async function archiveAudit() {
  if (!confirm('把超过 365 天的操作记录导出到文件后从库里删除？\n\n导出的文件在 data/ 目录，需要时可以查看。')) return;
  const r = await apiPost('/api/system/audit/archive', { days: 365 });
  if (r.ok) toast(r.archived ? `已归档 ${r.archived} 条到 ${r.file}` : '没有需要归档的记录', 3000);
  else toast('归档失败：' + (r.error || ''), 3000);
}
async function manualBackup() {
  const note = prompt('备份说明（可空）', '手动备份');
  if (note === null) return;
  await apiPost('/api/system/backups', { note }); toast('已备份'); loadBackups();
}
async function restoreBackup(name) {
  if (!confirm(`用 ${name} 覆盖现在的数据？\n\n恢复前会先把现在的数据另存一份备份，万一选错还能再恢复回来。`)) return;
  const r = await apiPost(`/api/system/backups/${encodeURIComponent(name)}/restore`, {});
  alert(`已恢复。恢复前的数据已另存为 ${r.saved_before}`); loadBackups();
}
function money(v) { return (v == null ? '-' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })); }
async function loadVerify() {
  const d = await apiGet('/api/system/verify');
  const n = d.rows.length;
  const badge = document.getElementById('verify-count');
  badge.hidden = !n; badge.textContent = n;
  document.getElementById('v-accept-all').hidden = !n;
  document.getElementById('v-hint').textContent = n
    ? `有 ${n} 车：车次本身没改过，利润却和上次核对时不一样。看一眼没问题就点「以当前为准」。`
    : `全部 ${d.total} 车利润都和上次核对时一致。改了车次本身（磅重、金额、本车费率等）或同期进车导致量补换档，会自动更新，不在这里提示。`;
  document.getElementById('v-changes').innerHTML = d.recent_changes.length
    ? '这段时间改过：' + d.recent_changes.map(c => `<a href="/system?tab=audit&table=${esc(c.table)}">${esc(c.table_cn)} ${c.count} 处</a>`).join('、')
      + '（利润变动多半来自这些修改）'
    : (n ? '这段时间没有改过档案/费率/规则——如果刚升级过程序，请把这张表截图发给开发者核对。' : '');
  document.querySelector('#v-list tbody').innerHTML = d.rows.map(r => `<tr>
    <td>${tripLink(r.trip_id, '#' + r.trip_id)}</td><td>${esc(r.plate_no)}</td><td class="nowrap">${esc(r.settlement_date || '')}</td>
    <td class="num">${money(r.before)}</td><td class="num">${money(r.after)}</td>
    <td class="num ${r.diff < 0 ? 'neg' : 'pos'}">${r.diff > 0 ? '+' : ''}${money(r.diff)}</td>
    <td class="muted nowrap">${esc(r.checked_at)}</td>
    <td><button class="btn-sm" onclick="acceptVerify([${r.trip_id}])">以当前为准</button></td></tr>`).join('')
    || '<tr><td colspan="8" class="empty-hint">没有需要核对的车次</td></tr>';
}
async function acceptVerify(ids) {
  if (!ids && !confirm('把这些车次的利润全部以现在的计算结果为准？')) return;
  const r = await apiPost('/api/system/verify/accept', ids ? { ids } : { all: true });
  toast(`已确认 ${r.accepted} 车`); loadVerify();
}
async function loadMobile() {
  const d = await apiGet('/api/mobile/access');
  document.getElementById('m-state').innerHTML = d.enabled
    ? '<span class="ma-on">手机访问已开启</span> 手机扫右边的二维码，或在手机浏览器输入下面的地址，然后输入口令。'
    : '<span class="ma-off">手机访问还没开启</span> 设一个口令就能用手机报车、推进状态。';
  document.getElementById('m-disable').hidden = !d.enabled;
  document.querySelector('#pane-mobile .btn-primary').textContent = d.enabled ? '更换口令' : '保存口令并开启';
  document.getElementById('m-urls').innerHTML = !d.listening_lan
    ? `<p class="muted">${d.lan_error ? esc(d.lan_error) + '。重启一次系统通常就好；还不行告诉开发者换个端口。' : '手机端口没有开（启动时设了 SCRAPMETAL_HOST=127.0.0.1，或者是在开发调试模式下运行），手机连不上。'}</p>`
    : (d.urls.length ? '<div class="ma-urls">' + d.urls.map((u, i) => `<div><span class="muted">${i === 0 ? '手机地址' : '备用地址'}</span> <code>${esc(u)}</code></div>`).join('') + '</div>'
      : '<p class="muted">没找到这台电脑的局域网地址：请确认电脑连着 WiFi。</p>');
  document.getElementById('m-qr').innerHTML = d.enabled && d.qr ? d.qr + '<div class="muted">手机相机扫一扫</div>' : '';
}
async function saveMobilePin() {
  const pin = document.getElementById('m-pin').value.trim();
  try { await apiPost('/api/mobile/access', { pin }); document.getElementById('m-pin').value = ''; toast('口令已保存，手机访问已开启'); loadMobile(); }
  catch (e) { alert(e.message); }
}
async function disableMobile() {
  if (!confirm('关闭后手机就打不开了（已经登录的手机也会失效）。确定关闭？')) return;
  await apiPost('/api/mobile/access', { disable: true }); toast('已关闭手机访问'); loadMobile();
}
(function init() {
  document.getElementById('a-table').innerHTML = '<option value="">全部类型</option>' +
    Object.entries(TABLE_OPTIONS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('');
  const u = new URLSearchParams(location.search);
  if (u.get('record_id')) document.getElementById('a-record').value = u.get('record_id');
  if (u.get('table')) document.getElementById('a-table').value = u.get('table');
  sysTab(u.get('tab') || 'audit');
})();
