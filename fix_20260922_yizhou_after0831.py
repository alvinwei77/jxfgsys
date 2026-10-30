"""2026-09-22 用户确认：8/31 及以后结算、票源记为「俊兴」的配票车，实际都是走「益周」进项（益周 12.8 进、13.5 转出）。
财务台账上这些车仍写“俊兴”，以系统为准改成益周；主利润不变，多出一笔票差留存（前端 ×0.7%）。
运行前自动备份，全部走系统接口（有操作记录）；已固化的车按新票源重算快照（仍保持固化，量补单价不变）。
用法：python3 fix_20260922_yizhou_after0831.py [数据库路径]
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

CUTOFF = "2026-08-31"
C = app.app.test_client()
conn = sqlite3.connect(db.DB_PATH); conn.row_factory = sqlite3.Row
one = lambda sql, *a: (conn.execute(sql, a).fetchone() or [None])[0]


def must(r, what):
    if r.status_code >= 400:
        raise SystemExit(f"失败：{what} → {r.status_code} {r.get_data(as_text=True)[:200]}")
    return r.get_json()


JX = one("SELECT id FROM invoice_sources WHERE name='俊兴'")
YZ = one("SELECT id FROM invoice_sources WHERE name='益周'")
trips = conn.execute("SELECT * FROM trips WHERE status!='voided' AND invoice_source_id=? AND settlement_date>=? ORDER BY settlement_date, id",
                     (JX, CUTOFF)).fetchall()
if not trips:
    raise SystemExit("没有需要改的车（已经改过了？）")
print("备份：", system_ops.do_backup("manual", f"{CUTOFF} 起俊兴→益周进项 之前"))


def full(tid):
    t = conn.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()
    return app._trip_finance_full(conn, t)


before = {t["id"]: full(t["id"]) for t in trips}
for t in trips:
    tid = t["id"]
    snap = conn.execute("SELECT reward_unit_price, is_locked FROM trip_finance WHERE trip_id=?", (tid,)).fetchone()
    must(C.put(f"/api/trips/{tid}", json={"invoice_source_id": YZ}), f"改票源#{tid}")
    if snap and snap["is_locked"]:
        t2 = conn.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()
        live = app._compute_finance(conn, t2)
        patch = {}
        if abs((live.get("reward_unit_price") or 0) - (snap["reward_unit_price"] or 0)) > 0.001:
            patch["rate:reward_profit"] = round(snap["reward_unit_price"], 4)
        must(C.put(f"/api/trips/{tid}/finance_overrides", json=patch), f"重算#{tid}")

tot = {"np": [0, 0], "ret": [0, 0]}
print(f"{'车次':>4} {'结算日':<10} {'调货人':<6} {'利润前':>10} {'利润后':>10} {'票差留存':>9}")
for t in trips:
    a, b = before[t["id"]], full(t["id"])
    ra, rb = a.get("invoice_diff_profit") or 0, b.get("invoice_diff_profit") or 0
    tot["np"][0] += a["net_profit"]; tot["np"][1] += b["net_profit"]
    tot["ret"][0] += ra; tot["ret"][1] += rb
    d = one("SELECT name FROM dispatchers WHERE id=?", t["dispatcher_id"])
    print(f"#{t['id']:>3} {t['settlement_date']:<10} {d or '':<6} {a['net_profit']:>10.2f} {b['net_profit']:>10.2f} {rb:>9.2f}")
print(f"合计 {len(trips)} 车：利润 {tot['np'][0]:.2f} → {tot['np'][1]:.2f}；票差留存 {tot['ret'][0]:.2f} → {tot['ret'][1]:.2f}")
with app.app.app_context():
    print("利润核对基准更新：", regression.accept(conn), "车")
print("完成")
