"""2026-09-22 按财务「发货记录表6」统一 调货人/供货厂 分类（用户确认：按财务那份做匹配和分类）。

财务表的分类方式：
  * 「林凯」表（配票）          → 调货人 林凯，供货厂 = 林凯（调货人自己的货源，系统里显示“林凯车队”）
  * 「林凯-君盛/力拓/兴德/鑫景通」→ 调货人 林凯，供货厂 = 对应供货厂
  * 按人分的表（李涛/邓晶焱/高栾凤/闫东朋/尹鹏飞/王荣胜/陈杰）→ 调货人 = 这个人，供货厂 = 这个人自己的货源
以前从旧台账导入时，有 12 车供货厂被填成了“俊兴”（其实是票源），7 车供货厂空着，4 条船没有调货人，这里统一改掉。
不影响利润（只改归属），运行前自动备份，每项修改有操作记录。用法：python3 sync_20260922_attr_v6.py [数据库路径]
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
        raise SystemExit(f"失败：{what} → {r.status_code} {r.get_data(as_text=True)[:200]}")
    return r.get_json()


print("备份：", system_ops.do_backup("manual", "按财务统一调货人/供货厂分类之前"))
before = {r["id"]: app._trip_finance_full(conn, r)["net_profit"] for r in conn.execute("SELECT * FROM trips WHERE status!='voided'")}

# 档案：调货人 尹鹏飞；调货人自己的货源（占位供货厂）高栾凤、王荣胜
if not one("SELECT id FROM dispatchers WHERE name='尹鹏飞'"):
    must(C.post("/api/dispatchers", json={"name": "尹鹏飞", "commission_per_ton": 0, "notes": "2026-09-22 按财务台账(尹鹏飞表)新增"}), "新增调货人尹鹏飞")
    print("新增调货人 尹鹏飞")
for name in ("高栾凤", "王荣胜"):
    if not one("SELECT id FROM processing_bases WHERE name=?", name):
        must(C.post("/api/processing_bases", json={"name": name, "base_type": "external", "notes": "调货人自己的货源（按财务台账分类）"}), f"新增{name}货源")
        bid = one("SELECT id FROM processing_bases WHERE name=?", name)
        conn.execute("UPDATE processing_bases SET is_dispatcher_alias=1 WHERE id=?", (bid,))
        system_ops.audit(conn, "processing_bases", bid, "is_dispatcher_alias", 0, 1, "调货人自己的货源")
        conn.commit()
        print("新增调货人货源", name)

D = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM dispatchers")}
B = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM processing_bases")}
PLAN = {  # 车次: (调货人, 供货厂)
    **{i: ("林凯", "林凯") for i in (4, 5, 9, 11, 34, 38, 39, 43, 90, 95, 96)},
    17: ("邓晶焱", "邓晶焱"), 35: ("邓晶焱", "邓晶焱"),
    19: ("王荣胜", "王荣胜"),
    27: ("高栾凤", "高栾凤"), 71: ("高栾凤", "高栾凤"), 77: ("高栾凤", "高栾凤"),
    **{i: ("尹鹏飞", "尹鹏飞") for i in (54, 57, 80, 93)},
    94: ("闫东朋", "闫东朋"),
}
t103 = one("SELECT id FROM trips WHERE settlement_date='2026-09-21' AND ABS(settlement_amount_total-70229.8)<0.01 AND status!='voided'")
if t103:
    PLAN[t103] = ("闫东朋", "闫东朋")
n = 0
for tid, (d, b) in sorted(PLAN.items()):
    cur = conn.execute("SELECT dispatcher_id, base_id FROM trips WHERE id=?", (tid,)).fetchone()
    patch = {}
    if cur["dispatcher_id"] != D[d]: patch["dispatcher_id"] = D[d]
    if cur["base_id"] != B[b]: patch["base_id"] = B[b]
    if patch:
        must(C.put(f"/api/trips/{tid}", json=patch), f"#{tid}"); n += 1
print("改了", n, "车")

after = {r["id"]: app._trip_finance_full(conn, r)["net_profit"] for r in conn.execute("SELECT * FROM trips WHERE status!='voided'")}
moved = {k: (before[k], after[k]) for k in after if abs(before.get(k, 0) - after[k]) > 0.005}
print("利润有变化的车：", moved or "无")
with app.app.app_context():
    regression.accept(conn)
print("完成")
