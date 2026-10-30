"""2026-09-29 按财务真实台账「2026华西发货明细8月」全面同步系统数据（用户：调货人、钱、票都以财务为准）。

一、修正已录入的车：
  #27 调货人/供货厂 高栾凤→邓晶焱（财务明细与 9/4 付款 69,170 都指向邓晶焱）
  #82 调货人/供货厂 邓晶焱→高栾凤（付款 82,641 付给栾高凤）
  #61 船号 皖城兴货688→皖诚兴货688
  #80 运输方式 车→船（皖四航6058，财务 9/15+9/16 两天卸完）
  #16/#95/#96 100元扣款按财务口径全额由客户承担：给客户付各减 14 元（用本车对外结算点位实现）
二、补录 9/22~9/28 共 15 车/船（配票票源按财务写的俊兴；带票按票源方；反向船按反向-尹鹏飞）
运行前自动备份，全部走系统接口（有操作记录）。用法：python3 sync_20260929_finreal.py [数据库路径]
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


def must(r, what):
    if r.status_code >= 400:
        raise SystemExit(f"失败：{what} → {r.status_code} {r.get_data(as_text=True)[:300]}")
    return r.get_json()


D = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM dispatchers")}
B = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM processing_bases")}
S = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM invoice_sources")}
T = {r["short_name"]: r["id"] for r in conn.execute("SELECT id,short_name FROM flow_templates")}

print("备份：", system_ops.do_backup("manual", "按财务真实台账(发货明细8月)全面同步 之前"))
trip = lambda tid: conn.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()
fin = lambda tid: app._trip_finance_full(conn, trip(tid))
before_total = sum((app._trip_finance_full(conn, t)["net_profit"] or 0)
                   for t in conn.execute("SELECT * FROM trips WHERE status!='voided'"))

# ---------- 一、修正 ----------
FIX = [
    (27, {"dispatcher_id": D["邓晶焱"], "base_id": B["邓晶焱"]}, "#27 8/28 皖KQ1052 → 邓晶焱"),
    (82, {"dispatcher_id": D["高栾凤"], "base_id": B["高栾凤"]}, "#82 9/16 皖AK6033 → 高栾凤"),
    (61, {"plate_no": "皖诚兴货688"}, "#61 船号改「诚」"),
    (80, {"transport_type": "ship"}, "#80 皖四航6058 改成船"),
]
for tid, patch, note in FIX:
    must(C.put(f"/api/trips/{tid}", json=patch), note)
    print("改：", note)

# 100 元扣款：给客户付按财务（含税金额+扣款）×点位−扣款
DEDUCT = {16: 58955.89, 95: 67748.67, 96: 67598.58}
for tid, target in DEDUCT.items():
    t = trip(tid)
    amt = float(t["settlement_amount_total"] or 0)
    rate = round((1 - target / amt) * 100, 8)
    must(C.put(f"/api/trips/{tid}/finance_overrides", json={"rate:give_customer": rate}), f"#{tid} 扣款口径")
    got = app._compute_finance(conn, trip(tid)).get("give_customer")
    print(f"改：#{tid} 给客户付 → {got:.2f}（财务 {target:.2f}，点位 {rate}%）")

# ---------- 二、补录 9/22~9/28 ----------
# (结算日期, 车号, 运输方式, 调货人, 供货厂, 链条, 票源, 吨, 金额, 财务给客户付, 任务编号/备注)
NEW = [
    ("2026-09-22", "江淮货88888", "ship", "尹鹏飞", "尹鹏飞", "反向", "反向-尹鹏飞", 443.78, 1069204.96, 919488.27, "9/22~9/24 卸完；含 200 元扣款"),
    ("2026-09-23", "苏DBE961", "truck", "林凯", "兴德", "直签带票", None, 78.98, 200609.20, 197450.00, "XS202609220008"),
    ("2026-09-24", "苏DBB772", "truck", "林凯", "力拓", "直签带票", None, 81.92, 206438.40, 203161.60, "XS202609230002"),
    ("2026-09-24", "苏DBE075", "truck", "林凯", "林凯", "配票", "俊兴", 53.50, 125725.00, 108123.50, "XS202609240008"),
    ("2026-09-24", "苏DBE075", "truck", "林凯", "兴德", "直签带票", None, 55.38, 140111.40, 137896.20, "XS202609230003"),
    ("2026-09-24", "苏DBE961", "truck", "林凯", "林凯", "配票", "俊兴", 119.54, 277093.72, 238300.60, "XS202609230012/XS202609240004"),
    ("2026-09-26", "苏DBB772", "truck", "林凯", "林凯", "配票", "俊兴", 79.42, 200138.40, 172119.02, "XS202609240007"),
    ("2026-09-26", "苏DBE075", "truck", "林凯", "林凯", "配票", "俊兴", 79.24, 199684.80, 171728.93, "XS202609240011"),
    ("2026-09-26", "苏DBE961", "truck", "林凯", "林凯", "配票", "俊兴", 59.26, 139261.00, 119764.46, "XS202609260002"),
    ("2026-09-26", "苏DBE961", "truck", "林凯", "兴德", "直签带票", None, 85.72, 217728.80, 214300.00, "XS202609240012"),
    ("2026-09-27", "苏DBE961", "truck", "林凯", "兴德", "直签带票", None, 57.51, 144925.20, 142624.80, "XS202609260017"),
    ("2026-09-27", "鲁DC6106", "truck", "高栾凤", "高栾凤", "配票", "俊兴", 38.68, 88577.20, 76176.39, "XS202609260008"),
    ("2026-09-28", "冀A91C24", "truck", "邓晶焱", "邓晶焱", "配票", "俊兴", 31.38, 71860.20, 61799.77, "XS202609270007"),
    ("2026-09-28", "苏DBB772", "truck", "林凯", "林凯", "配票", "俊兴", 79.50, 200340.00, 172292.40, "XS202609260006"),
    ("2026-09-28", "鲁MFK199", "truck", "邓晶焱", "邓晶焱", "配票", "俊兴", 37.46, 85783.40, 73773.72, "XS202609270004"),
]
added = []
for sd, plate, ttype, disp, base, tpl, src, ton, amt, pay, memo in NEW:
    dup = one("""SELECT id FROM trips WHERE status!='voided' AND settlement_date=? AND plate_no=?
                 AND ABS(settlement_amount_total-?)<0.01""", sd, plate, amt)
    if dup:
        print(f"跳过（已录过）：#{dup} {sd} {plate}")
        continue
    body = {"plate_no": plate, "transport_type": ttype, "order_date": sd, "status": "reported",
            "dispatcher_id": D[disp], "base_id": B[base], "template_id": T[tpl],
            "invoice_source_id": S[src] if src else None,
            "remarks": f"2026-09-29 按财务台账(发货明细8月)补录：{memo}"}
    r = C.post("/api/trips", json=body)
    if r.status_code != 201:
        raise SystemExit(f"新增失败 {sd} {plate}: {r.get_data(as_text=True)[:300]}")
    tid = r.get_json()["id"]
    must(C.post(f"/api/trips/{tid}/status", json={"status": "settled", "status_date": sd,
                                                  "settlement_tonnage_total": ton, "settlement_amount_total": amt}),
         f"结算#{tid}")
    f = app._compute_finance(conn, trip(tid))
    give = f.get("give_customer") or 0
    if tpl in ("配票", "反向") and abs(give - pay) > 0.01:
        rate = round((1 - pay / amt) * 100, 8)
        must(C.put(f"/api/trips/{tid}/finance_overrides", json={"rate:give_customer": rate}), f"#{tid} 给客户付对齐财务")
        f = app._compute_finance(conn, trip(tid)); give = f.get("give_customer") or 0
    added.append((tid, sd, plate, tpl, ton, amt, give, pay, f["net_profit"]))
    print(f"补录 #{tid} {sd} {plate:<12}{tpl:<5}{ton:>8.2f}{amt:>13.2f} 给客户付{give:>12.2f}(财务{pay:>12.2f}) 利润{f['net_profit']:>10.2f}")

# ---------- 核对 ----------
rows = [app._trip_finance_full(conn, t) for t in conn.execute("SELECT * FROM trips WHERE status!='voided'")]
print(f"\n车次总数 {len(rows)}；利润合计 {before_total:.2f} → {sum(r['net_profit'] or 0 for r in rows):.2f}")
print(f"票差留存合计 {sum(r.get('invoice_diff_profit') or 0 for r in rows):.2f}")
tot = conn.execute("SELECT ROUND(SUM(settlement_tonnage_total),2), ROUND(SUM(settlement_amount_total),2) FROM trips WHERE status!='voided'").fetchone()
print("钢厂数量/含税金额合计：", tuple(tot), "（财务 8/19~9/28：10,812.25 吨 / 26,184,228.33）")
with app.app.app_context():
    print("利润核对基准更新：", regression.accept(conn), "车")
print("完成")
