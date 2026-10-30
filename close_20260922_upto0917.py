"""2026-09-22 用户确认：结算日期截至 9/17 的车都已结单——开票、打款都完成了，调货人提成也已付。

处理：
  * 这些车状态推进到「已结案」。开票/回款环节按系统规则显示“按状态视为已完成”（不编造开票/回款批次）。
  * 结案日期：财务「发货记录表6」里有“实际付款日期”的用它（最后一笔钱到客户的日子）；
    没有的（带票、反向那些财务没填）结案日期留空，不瞎填。
  * 调货人提成：每个调货人登记一笔“提成已付”批次，金额=这些车的应付未付，备注说明付款日期未登记。
运行前自动备份，全部走系统接口（有操作记录）。用法：python3 close_20260922_upto0917.py [数据库路径] [paydates.json]
"""
import json
import os
import sqlite3
import sys

if len(sys.argv) > 1:
    os.environ["SCRAPMETAL_TEST_DB"] = os.path.abspath(sys.argv[1])
_orig = sqlite3.connect


def _connect(*a, **k):
    c = _orig(*a, **k)
    c.execute("PRAGMA journal_mode=TRUNCATE")
    return c


sqlite3.connect = _connect
import db  # noqa: E402
import app  # noqa: E402
import regression  # noqa: E402
import system_ops  # noqa: E402

CUTOFF = "2026-09-17"
PAYDATES = {int(k): v for k, v in json.load(open(sys.argv[2])).items()} if len(sys.argv) > 2 else {}
C = app.app.test_client()
conn = sqlite3.connect(db.DB_PATH); conn.row_factory = sqlite3.Row


def must(r, what):
    if r.status_code >= 400:
        raise SystemExit(f"失败：{what} → {r.status_code} {r.get_data(as_text=True)[:200]}")
    return r.get_json()


print("备份：", system_ops.do_backup("manual", f"结算日期≤{CUTOFF} 结案、提成已付 之前"))
trips = conn.execute("SELECT * FROM trips WHERE status!='voided' AND settlement_date<=? ORDER BY settlement_date, id", (CUTOFF,)).fetchall()
n_close = n_date = 0
for t in trips:
    pd = PAYDATES.get(t["id"])
    if t["status"] != "closed":
        must(C.post(f"/api/trips/{t['id']}/status", json={"status": "closed", **({"status_date": pd} if pd else {})}), f"结案#{t['id']}")
        n_close += 1
    # 结案日期统一按财务实际付款日期；没有就留空（以前迁移时顺手填过“当天”的也一并纠正）
    if t["closed_date"] != pd or t["status"] != "closed":
        must(C.put(f"/api/trips/{t['id']}", json={"closed_date": pd}), f"结案日期#{t['id']}")
    n_date += 1 if pd else 0
print(f"结案 {n_close} 车（其中 {n_date} 车用了财务的实际付款日期作结案日期）")

# 调货人提成：登记已付
comm = must(C.get(f"/api/analytics/commission?date_field=settlement_date&date_to={CUTOFF}"), "提成")
unpaid = [r for r in comm["unpaid_trips"] if (r["settlement_date"] or "") <= CUTOFF]
by = {}
for r in unpaid:
    by.setdefault(r["dispatcher_name"], []).append(r)
for name, rows in by.items():
    res = must(C.post("/api/batches", json={
        "hop_key": "commission", "batch_date": "2026-09-22",
        "notes": f"用户 2026-09-22 确认：结算日期≤{CUTOFF} 的提成已付清（实际付款日期未登记）",
        "allocations": [{"trip_id": r["trip_id"], "amount": r["unpaid"]} for r in rows]}), f"提成批次{name}")
    print(f"提成已付：{name} {len(rows)} 车 ¥{res['total_amount']:,.2f}")

left = must(C.get("/api/analytics/commission"), "提成")
print("提成未付（全部）：", {d["name"]: round(d["unpaid"], 2) for d in left["by_dispatcher"] if d["unpaid"] > 0.01} or "无")
with app.app.app_context():
    regression.check(conn)
print("完成")
