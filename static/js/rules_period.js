/** 规则 hub 共用：结算期参考日 + 同步到费率图 API */
const RULES_AS_OF_KEY = 'scrapmetal.rulesAsOf';

async function fetchSettlementPeriodMeta() {
  return apiGet('/api/meta/current_settlement_period');
}

function getRulesAsOf() {
  const inp = document.getElementById('rules-as-of');
  if (inp && inp.value) return inp.value;
  return localStorage.getItem(RULES_AS_OF_KEY) || '';
}

function setRulesAsOf(dateStr, opts = {}) {
  if (!dateStr) return;
  localStorage.setItem(RULES_AS_OF_KEY, dateStr);
  const inp = document.getElementById('rules-as-of');
  if (inp) inp.value = dateStr;
  if (opts.reload !== false) {
    document.dispatchEvent(new CustomEvent('rules-as-of-changed', { detail: { as_of: dateStr } }));
  }
  updateRulesPeriodMeta(dateStr);
}

async function updateRulesPeriodMeta(asOf) {
  const el = document.getElementById('rules-period-meta');
  if (!el || !asOf) return;
  try {
    const meta = await fetchSettlementPeriodMeta();
    const inPeriod = asOf >= meta.period_start && asOf <= meta.period_end;
    el.textContent = inPeriod
      ? `所在结算期：${meta.period_label}（${meta.period_start} ~ ${meta.period_end}）`
      : `参考日 ${asOf}（非当前默认结算期 ${meta.period_label}）`;
  } catch (e) {
    el.textContent = '';
  }
}

async function initRulesPeriodBar() {
  const bar = document.getElementById('rules-period-bar');
  if (!bar) return;
  const meta = await fetchSettlementPeriodMeta();
  const urlAsOf = new URLSearchParams(location.search).get('as_of');
  let asOf = (urlAsOf && urlAsOf.slice(0, 10)) || localStorage.getItem(RULES_AS_OF_KEY) || meta.as_of_today;
  const inp = document.getElementById('rules-as-of');
  if (inp) {
    inp.value = asOf;
    inp.addEventListener('change', () => setRulesAsOf(inp.value));
  }
  document.getElementById('rules-as-of-today')?.addEventListener('click', () => setRulesAsOf(meta.as_of_today));
  document.getElementById('rules-as-of-period-start')?.addEventListener('click', () => setRulesAsOf(meta.period_start));
  await updateRulesPeriodMeta(asOf);
}

document.addEventListener('DOMContentLoaded', () => { initRulesPeriodBar(); });

/** 统计 / 车次：默认筛本结算期 */
async function applyDefaultSettlementPeriodFilter(fromSel, toSel, opts = {}) {
  const q = new URLSearchParams(location.search);
  if (q.get('period') === 'all') return null;
  const fromEl = typeof fromSel === 'string' ? document.getElementById(fromSel) : fromSel;
  const toEl = typeof toSel === 'string' ? document.getElementById(toSel) : toSel;
  if (!fromEl || !toEl) return null;
  if (fromEl.value && toEl.value && !opts.force) return null;
  if (q.get('period') !== 'current' && fromEl.value && !opts.force) return null;
  const meta = await fetchSettlementPeriodMeta();
  fromEl.value = meta.period_start;
  toEl.value = meta.period_end;
  return meta;
}
