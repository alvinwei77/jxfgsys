function moEsc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
}

async function loadMillOnboarding() {
  const data = await apiGet('/api/setup/mill_onboarding');
  const tipsBox = document.getElementById('mo-tips');
  if (data.tips && data.tips.length) {
    tipsBox.hidden = false;
    tipsBox.innerHTML = data.tips.map(t =>
      `<div class="note-banner" style="margin-bottom:8px;background:${t.level === 'warn' ? '#fffbeb' : 'var(--blue-light)'};">
        ${t.href ? `<a href="${moEsc(t.href)}">${moEsc(t.text)}</a>` : moEsc(t.text)}
      </div>`
    ).join('');
  } else if (tipsBox) {
    tipsBox.hidden = true;
  }
  const pb = document.getElementById('mo-playbook');
  if (pb) {
    pb.innerHTML = (data.playbook || []).map(s => `<li>${moEsc(s)}</li>`).join('');
  }
  const mt = document.querySelector('#mo-mills tbody');
  if (mt) {
    if (!data.mills.length) {
      mt.innerHTML = '<tr><td colspan="6" class="empty-hint">尚无钢厂 — 请先到基础档案添加</td></tr>';
    } else {
      mt.innerHTML = data.mills.map(m => `<tr>
        <td>${moEsc(m.name)}</td>
        <td>${m.ok_tiers ? `<span class="pill green">${m.tier_count} 档</span>` : '<span class="pill red">未配置</span>'}</td>
        <td class="nowrap">${m.period_start_day != null ? `${m.period_start_day}日～${m.period_end_day}日` : '—'}</td>
        <td>${m.ok_accounts ? m.account_count : '<span class="muted">0</span>'}</td>
        <td class="num">${m.trip_count}</td>
        <td class="num">${m.settled_trip_count}</td>
      </tr>`).join('');
    }
  }
  const tt = document.querySelector('#mo-templates tbody');
  if (tt) {
    tt.innerHTML = (data.templates || []).map(t => {
      const calcHref = `/chain_rules?template=${encodeURIComponent(t.code)}&tab=calc`;
      const plainHref = `/chain_rules?template=${encodeURIComponent(t.code)}`;
      return `<tr>
        <td>${moEsc(t.short_name || t.name)}</td>
        <td class="nowrap"><code>${moEsc(t.code)}</code></td>
        <td>${t.is_active ? '启用' : '<span class="muted">停用</span>'}</td>
        <td class="num">${t.trip_count || 0}</td>
        <td class="nowrap">
          <a href="${plainHref}">关系图</a>
          · <a href="${calcHref}">计算口径</a>
          · <button type="button" class="btn-link" data-tid="${t.id}" data-code="${moEsc(t.code)}" data-label="${moEsc(t.short_name || t.name)}" onclick="moCloneTemplate(this)">复制</button>
        </td>
      </tr>`;
    }).join('') || '<tr><td colspan="5" class="empty-hint">无模板</td></tr>';
  }
}

async function moCloneTemplate(btn) {
  const id = +btn.dataset.tid;
  const code = btn.dataset.code;
  const label = btn.dataset.label;
  const newCode = prompt(`复制「${label}」为新规则 — 英文代码（唯一）`, `${code}_v2`);
  if (!newCode || !newCode.trim()) return;
  const newName = prompt('展示名', `${label}（副本）`);
  if (!newName || !newName.trim()) return;
  try {
    const r = await apiPost(`/api/flow_templates/${id}/clone`, {
      code: newCode.trim(),
      name: newName.trim(),
    });
    toast(`已复制为 ${r.code}（默认停用）`);
    if (confirm('打开新规则的「计算口径」页继续改？')) {
      location.href = `/chain_rules?template=${encodeURIComponent(r.code)}&tab=calc`;
    } else {
      loadMillOnboarding();
    }
  } catch (e) {
    alert(e.message || String(e));
  }
}

document.addEventListener('DOMContentLoaded', () => {
  if (document.getElementById('mo-mills')) loadMillOnboarding();
});
