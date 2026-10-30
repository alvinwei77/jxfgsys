"""2026-09-22 用户更正：调货人是闫东朋的车没有走益周进项，票源改回俊兴（8/31 后只有 #55 一车）。
运行前自动备份，走系统接口；已固化的车重算快照（量补单价不变）。用法：python3 fix_20260922_yizhou_revert55.py [数据库路径]"""
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
        raise SystemExit(f"失败：{what} → {r.status_code} {r.get_data(as_text=True)[:200]}")
    return r.get_json()


JX = one("SELECT id FROM invoice_sources WHERE name='俊兴'")
YZ = one("SELECT id FROM invoice_sources WHERE name='益周'")
DP = one("SELECT id FROM dispatchers WHERE name='闫东朋'")
trips = conn.execute("SELECT * FROM trips WHERE status!='voided' AND invoice_source_id=? AND dispatcher_id=? AND settlement_date>='2026-08-31'",
                     (YZ, DP)).fetchall()
if not trips:
    raise SystemExit("没有需要改的车")
print("备份：", system_ops.do_backup("manual", "闫东朋的车 益周→俊兴 之前"))
full = lambda tid: app._trip_finance_full(conn, conn.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone())
for t in trips:
    tid = t["id"]
    a = full(tid)
    snap = conn.execute("SELECT reward_unit_price, is_locked FROM trip_finance WHERE trip_id=?", (tid,)).fetchone()
    must(C.put(f"/api/trips/{tid}", json={"invoice_source_id": JX}), f"改票源#{tid}")
    if snap and snap["is_locked"]:
        live = app._compute_finance(conn, conn.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone())
        patch = {}
        if abs((live.get("reward_unit_price") or 0) - (snap["reward_unit_price"] or 0)) > 0.001:
            patch["rate:reward_profit"] = round(snap["reward_unit_price"], 4)
        must(C.put(f"/api/trips/{tid}/finance_overrides", json=patch), f"重算#{tid}")
    b = full(tid)
    print(f"#{tid} {t['settlement_date']} 利润 {a['net_profit']:.2f} → {b['net_profit']:.2f}；票差留存 {a.get('invoice_diff_profit') or 0:.2f} → {b.get('invoice_diff_profit') or 0:.2f}")
rs = [app._trip_finance_full(conn, t) for t in conn.execute("SELECT * FROM trips WHERE status!='voided'")]
print(f"全系统：利润 {sum(r['net_profit'] for r in rs):.2f}，票差留存 {sum(r.get('invoice_diff_profit') or 0 for r in rs):.2f}")
with app.app.app_context():
    print("利润核对基准更新：", regression.accept(conn), "车")
print("完成")
