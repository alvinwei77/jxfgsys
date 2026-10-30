"""2026-09-22 补录财务「发货记录表6」闫东鹏表第10行：9/21 闫东朋 反向 27.98吨 单价2510 金额70,229.80。
财务表没有车号，先留空（备注“车号待补”），用户确认后补。已结算、结算日期 9/21。"""
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

conn = sqlite3.connect(db.DB_PATH); conn.row_factory = sqlite3.Row
one = lambda sql, *a: (conn.execute(sql, a).fetchone() or [None])[0]
if one("SELECT id FROM trips WHERE settlement_date='2026-09-21' AND ABS(settlement_amount_total-70229.8)<0.01 AND status!='voided'"):
    raise SystemExit("这一车已经录过了")
print("备份：", system_ops.do_backup("manual", "补录 9/21 闫东朋反向之前"))
C = app.app.test_client()
body = {"plate_no": None, "transport_type": "truck", "order_date": "2026-09-21", "status": "reported",
        "dispatcher_id": one("SELECT id FROM dispatchers WHERE name='闫东朋'"),
        "invoice_source_id": one("SELECT id FROM invoice_sources WHERE name='反向-闫东朋'"),
        "template_id": one("SELECT id FROM flow_templates WHERE short_name='反向'"),
        "remarks": "2026-09-22 按财务发货记录表6(闫东鹏表第10行)补录：单价2510；车号待补"}
r = C.post("/api/trips", json=body); assert r.status_code == 201, r.get_data(as_text=True)
tid = r.get_json()["id"]
r = C.post(f"/api/trips/{tid}/status", json={"status": "settled", "status_date": "2026-09-21",
                                               "settlement_tonnage_total": 27.98, "settlement_amount_total": 70229.8})
assert r.status_code == 200, r.get_data(as_text=True)
t = conn.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()
fin = app._compute_finance(conn, t)
print(f"已录入 #{tid}：{t['settlement_tonnage_total']}吨 {t['settlement_amount_total']} 利润 {fin['net_profit']:.2f}（财务 840.59）")
with app.app.app_context():
    regression.check(conn)
