#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-10期规则落库 终态核对 v3（只读生产库；回归对比在 /tmp 副本上做）

  1) 现状（含 2026-10 期新规则）下重算全部车次 → 与经营报告基线对齐
  2) 在 /tmp 副本上删掉 2026-09-26 起的新 rate_links，重算 → 已封账期必须 0 差异
  3) 输出新期逐车变化明细
"""
import os, sys, json, shutil, sqlite3, logging
logging.disable(logging.CRITICAL)
ROOT = "/Users/alvin/Documents/scrapmetal"
sys.path.insert(0, ROOT)
import engine

PROD = os.path.join(ROOT, "data", "scrapmetal.db")
TMP  = "/tmp/regr_no_new_rules.db"
FIELDS = ["give_customer","front_invoice_amount","front_actual_paid","personal_advance",
          "reward_profit","dispatcher_commission_cost","net_profit","settlement_amount",
          "service_fee_income","invoice_diff_profit","other_cost"]

def load(conn):
    rows = [dict(r) for r in conn.execute("""
        select t.* from trips t where t.status != 'voided'
        order by t.settlement_date, t.id""")]
    for t in rows:
        t["_ov"] = json.loads(t.get("finance_overrides") or "{}")
    return rows

def flat(conn, t):
    try:
        return engine.compute_trip_finance_v2_flat(conn, t, t["_ov"]) or {}
    except Exception as e:
        return {"_err": str(e)}

# ---------- 1) 现状 ----------
c1 = sqlite3.connect(PROD); c1.row_factory = sqlite3.Row
trips = load(c1)
now = {t["id"]: flat(c1, t) for t in trips}

closed = [t for t in trips if (t.get("settlement_date") or "") <= "2026-09-25"]
newp   = [t for t in trips if (t.get("settlement_date") or "") >  "2026-09-26"[0:0] + "2026-09-25"]
newp   = [t for t in trips if (t.get("settlement_date") or "") >= "2026-09-26"]
print(f"trips(非作废)={len(trips)}  已封账(<=09-25)={len(closed)}  新期(>=09-26)={len(newp)}")

def agg(rows, src):
    s = {k:0.0 for k in FIELDS}
    for t in rows:
        f = src.get(t["id"]) or {}
        for k in FIELDS:
            v = f.get(k)
            if isinstance(v,(int,float)): s[k]+=v
    return s

print("\n【现状·已封账两期合计（8/26~9/25 全量含 8月期）】")
for k,v in agg(closed, now).items(): print(f"  {k:26s} {v:>16,.2f}")

print("\n【现状·新期（2026-09-26 起）合计】")
for k,v in agg(newp, now).items(): print(f"  {k:26s} {v:>16,.2f}")

# 分月期
import datetime
def per(s):
    d = datetime.date.fromisoformat(str(s)[:10])
    return "%d-%02d期" % ((d.year+1,1) if d.month==12 and d.day>=26 else (d.year, d.month+(1 if d.day>=26 else 0)))
for p in ["2026-08期","2026-09期","2026-10期"]:
    rs = [t for t in trips if per(t["settlement_date"]) == p]
    a = agg(rs, now)
    if not rs: continue
    print(f"\n  周期 {p}: {len(rs)} 台 含税 {a['settlement_amount']:,.2f}  给客户付 {a['give_customer']:,.2f}  "
          f"量补 {a['reward_profit']:,.2f}  垫资 {a['personal_advance']:,.2f}  提成 {a['dispatcher_commission_cost']:,.2f}  "
          f"净利 {a['net_profit']:,.2f}")

# ---------- 2) 副本上删新规则 ----------
shutil.copy2(PROD, TMP)
c2 = sqlite3.connect(TMP); c2.row_factory = sqlite3.Row
before = c2.execute("SELECT COUNT(*) FROM rate_links WHERE effective_from>='2026-09-26'").fetchone()[0]
c2.execute("DELETE FROM rate_links WHERE effective_from>='2026-09-26'"); c2.commit()
after = c2.execute("SELECT COUNT(*) FROM rate_links WHERE effective_from>='2026-09-26'").fetchone()[0]
print(f"\n【副本回归】删除 2026-09-26 起 rate_links：{before} → {after}")
trips2 = load(c2)
old = {t["id"]: flat(c2, t) for t in trips2}

diffs = []
for t in closed:
    a, b = old.get(t["id"], {}), now.get(t["id"], {})
    for k in FIELDS:
        x, y = a.get(k), b.get(k)
        if isinstance(x,(int,float)) and isinstance(y,(int,float)) and abs(x-y) > 0.005:
            diffs.append((t["id"], k, x, y, y-x))
print(f"  已封账期差异条数 = {len(diffs)}  （应为 0）")
for d in diffs[:25]: print("   ", d)

print("\n【新期逐车 变化】")
for t in newp:
    a, b = old.get(t["id"], {}), now.get(t["id"], {})
    dg = b.get("give_customer",0) - a.get("give_customer",0)
    dn = b.get("net_profit",0)    - a.get("net_profit",0)
    tag = "★变化" if abs(dg) > 0.005 else "  不变"
    print(f"  #{t['id']:3d} tpl={a.get('template_code','') or b.get('template_code',''):18s} "
          f"disp={t.get('dispatcher_id')} 含税 {b.get('settlement_amount',0):>12,.2f}  "
          f"给客户付 {a.get('give_customer',0):>11,.2f} → {b.get('give_customer',0):>11,.2f} (Δ{dg:>8,.2f})  "
          f"净利Δ{dn:>8,.2f}  {tag}")

c2.close(); c1.close()
os.remove(TMP)
print("\nDONE（生产库未被修改）")
