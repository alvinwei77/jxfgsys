"""2026-09-29 #14、#69 两车固化快照里的「服务费单价」是 0（金额 40元/吨算对了，只是单价这一格没写），
补成 40 元/吨，免得计算过程页显示成 0。只改展示字段，不动金额、不动利润。"""
import os, sqlite3, sys
if len(sys.argv) > 1:
    os.environ["SCRAPMETAL_TEST_DB"] = os.path.abspath(sys.argv[1])
_o = sqlite3.connect
def _c(*a, **k):
    c = _o(*a, **k); c.execute("PRAGMA journal_mode=TRUNCATE"); return c
sqlite3.connect = _c
import db, system_ops  # noqa: E402
conn = sqlite3.connect(db.DB_PATH); conn.row_factory = sqlite3.Row
for r in conn.execute("""SELECT f.trip_id, f.service_fee_income, t.settlement_tonnage_total ton
                         FROM trip_finance f JOIN trips t ON t.id=f.trip_id
                         WHERE f.service_fee_income>0 AND coalesce(f.service_fee_value,0)=0""").fetchall():
    rate = round(r["service_fee_income"] / r["ton"], 4) if r["ton"] else 0
    conn.execute("UPDATE trip_finance SET service_fee_value=? WHERE trip_id=?", (rate, r["trip_id"]))
    system_ops.audit(conn, "trip_finance", r["trip_id"], "service_fee_value", 0, rate, "补登服务费单价（金额本来就对）")
    print(f"#{r['trip_id']} 服务费单价 → {rate:g} 元/吨")
conn.commit()
print("完成")
