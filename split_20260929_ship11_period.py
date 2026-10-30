"""2026-09-29 用户：8/27 那条船（苏淮货16776）卸货分批，有一部分算上一期的量补。
财务口径：8月期（7/26~8/25）20 元/吨、759.01 吨、量补 15,180.20；9月期（8/26~9/25）25 元/吨、9,505.07 吨、量补 237,626.75。
系统里这条船整条 560.39 吨都记在 8/27（9月期），所以 9月期多算了 251.86 吨。
处理：按财务汇总把这条船拆成两车 —— 8/25 251.86 吨/585,813.14（上一期，量补 20）、8/26 308.53 吨/758,302.75（本期，量补 25）。
提成批次按吨位同比例拆；结案日期、票源、路径、调货人都沿用原车。运行前自动备份，走系统接口。
用法：python3 split_20260929_ship11_period.py [数据库路径]
"""
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

C = app.app.test_client()
conn = sqlite3.connect(db.DB_PATH); conn.row_factory = sqlite3.Row
one = lambda sql, *a: (conn.execute(sql, a).fetchone() or [None])[0]
trip = lambda tid: conn.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()


def must(r, what):
    if r.status_code >= 400:
        raise SystemExit(f"失败：{what} → {r.status_code} {r.get_data(as_text=True)[:300]}")
    return r.get_json()


OLD, P1_T, P1_A, P2_T, P2_A = 11, 251.86, 585813.14, 308.53, 758302.75
t = trip(OLD)
if abs((t["settlement_tonnage_total"] or 0) - (P1_T + P2_T)) > 0.01:
    raise SystemExit(f"#{OLD} 吨位不是 {P1_T + P2_T}，可能已经拆过了：{t['settlement_tonnage_total']}")
print("备份：", system_ops.do_backup("manual", "拆分 8/27 苏淮货16776 跨量补期 之前"))
before = {r["id"]: app._trip_finance_full(conn, r)["net_profit"] for r in conn.execute("SELECT * FROM trips WHERE status!='voided'")}

# ① 原车留本期部分（8/26）
must(C.put(f"/api/trips/{OLD}/details", json={"details": [
    {"settlement_tonnage": P2_T, "unit_price": round(P2_A / P2_T, 4), "amount": P2_A, "scrap_type_id": 24}]}), "改原车吨位")
must(C.put(f"/api/trips/{OLD}", json={"settlement_date": "2026-08-26", "order_date": "2026-08-26",
                                      "paid_amount": P2_A,
                                      "remarks": (t["remarks"] or "") + "；2026-09-29 按财务汇总拆分：本船 8/25 卸 251.86 吨记上一期(见拆出的车次)，本车为 8/26 卸的 308.53 吨"}),
     "改原车日期")

# ② 拆出上一期部分（8/25）
body = {"plate_no": t["plate_no"], "transport_type": "ship", "order_date": "2026-08-25", "status": "reported",
        "dispatcher_id": t["dispatcher_id"], "base_id": t["base_id"], "mill_id": t["mill_id"],
        "account_id": t["account_id"], "invoice_source_id": t["invoice_source_id"], "template_id": t["template_id"],
        "remarks": f"2026-09-29 按财务汇总从 #{OLD} 拆出：8/25 卸的 251.86 吨，属上一量补期（7/26~8/25，20 元/吨）"}
r = C.post("/api/trips", json=body)
if r.status_code != 201:
    raise SystemExit(f"新增失败：{r.get_data(as_text=True)[:300]}")
NEW = r.get_json()["id"]
must(C.post(f"/api/trips/{NEW}/status", json={"status": "settled", "status_date": "2026-08-25",
                                              "settlement_tonnage_total": P1_T, "settlement_amount_total": P1_A}), "结算新车")
must(C.post(f"/api/trips/{NEW}/status", json={"status": "closed", "status_date": t["closed_date"]}), "结案新车")
must(C.put(f"/api/trips/{NEW}", json={"settlement_date": "2026-08-25", "closed_date": t["closed_date"],
                                      "is_paid": t["is_paid"], "paid_amount": P1_A,
                                      "settlement_doc_no": t["settlement_doc_no"]}), "新车日期")
# 上一期按当期约定 20 元/吨（和 1~10 号车一样，阶梯到不了 3000 吨）
must(C.put(f"/api/trips/{NEW}/finance_overrides", json={"rate:reward_profit": 20}), "新车量补20")

# ③ 提成批次按吨位拆
for b in conn.execute("SELECT * FROM batch_trips WHERE trip_id=?", (OLD,)).fetchall():
    amt = b["amount"] or 0
    a2 = round(amt * P2_T / (P1_T + P2_T), 2); a1 = round(amt - a2, 2)
    conn.execute("UPDATE batch_trips SET amount=? WHERE batch_id=? AND trip_id=?", (a2, b["batch_id"], OLD))
    conn.execute("INSERT INTO batch_trips (batch_id, trip_id, amount) VALUES (?,?,?)", (b["batch_id"], NEW, a1))
    system_ops.audit(conn, "batch_trips", b["batch_id"], "allocation", f"#{OLD} {amt}", f"#{OLD} {a2} / #{NEW} {a1}",
                     "按拆船吨位比例分摊")
    print(f"批次 {b['batch_id']} 分摊：#{OLD} {a2}，#{NEW} {a1}")
conn.commit()

# ④ 两车都重算固化快照（原车仍固化；新车登记快照并固化）
must(C.put(f"/api/trips/{OLD}/finance_overrides", json={}), "重算原车")
import calc  # noqa: E402
fin_new = app._compute_finance(conn, trip(NEW))
calc.save_trip_finance(conn, NEW, fin_new, lock=True)
conn.commit()

for tid in (OLD, NEW):
    f = app._trip_finance_full(conn, trip(tid)); tt = trip(tid)
    print(f"#{tid} {tt['settlement_date']} {tt['settlement_tonnage_total']}吨 {tt['settlement_amount_total']:,.2f} "
          f"量补{f['reward_unit_price']:g}元/吨={f['reward_profit']:,.2f} 期内累计{f['period_cumulative_tonnage']:,.2f} 利润{f['net_profit']:,.2f}")

# ⑤ 量补分期核对
rows = [dict(r, **app._trip_finance_full(conn, r)) for r in conn.execute("SELECT * FROM trips WHERE status!='voided'")]


def period(d):
    y, m, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
    if dd >= 26:
        m += 1
        if m == 13: y, m = y + 1, 1
    return f"{y}-{m:02d}期"


import collections  # noqa: E402
agg = collections.defaultdict(lambda: [0, 0])
for r in rows:
    p = period(r["settlement_date"])
    agg[p][0] += r["settlement_tonnage_total"] or 0; agg[p][1] += r["reward_profit"] or 0
print("\n量补分期：")
for p, v in sorted(agg.items()):
    print(f"  {p}  吨位 {v[0]:>10,.2f}  量补 {v[1]:>12,.2f}")
print("  财务：8月期 759.01 吨 / 15,180.20；9月期 9,505.07 吨 / 237,626.75")
after = {r["id"]: r["net_profit"] for r in rows}
moved = {k: (round(before.get(k, 0), 2), round(after[k], 2)) for k in after if abs(before.get(k, 0) - after[k]) > 0.005}
print("利润有变化的车：", moved)
print(f"利润合计 {sum(before.values()):,.2f} → {sum(after.values()):,.2f}")
with app.app.app_context():
    print("利润核对基准更新：", regression.accept(conn), "车")
print("完成")
