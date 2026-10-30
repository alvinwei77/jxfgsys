"""2026-09-22 按财务「发货记录表6」同步系统数据（用户确认）。

用户确认的规则：
  * 调货人 / 供货厂 / 票源 / 计算路径 全部按财务表标记同步；
  * 王荣胜 8/29 鲁济宁货4095 特批：给客户付按 13.8%；
  * 切割费由客户承担：结算金额里已经扣掉，利润里不再扣（改链条规则的“其他成本”节点，只留手工其他成本）；
  * 林凯配票提成 9/05 停（系统本来就是这样，不改）；
  * 9/21 闫东鹏那车用户自己录，这里不动。
全部走系统自己的接口（有操作记录），运行前自动备份。用法：python3 sync_20260922_finance_v6.py [数据库路径]
"""
import json
import os
import sqlite3
import sys

if len(sys.argv) > 1:
    os.environ["SCRAPMETAL_TEST_DB"] = os.path.abspath(sys.argv[1])

import db  # noqa: E402

_orig_connect = sqlite3.connect


def _connect(*a, **k):  # 设备上的虚拟机删不了文件：日志用 TRUNCATE 模式
    c = _orig_connect(*a, **k)
    c.execute("PRAGMA journal_mode=TRUNCATE")
    return c


sqlite3.connect = _connect

import app  # noqa: E402
import regression  # noqa: E402
import system_ops  # noqa: E402

NOTE = "财务对账同步(发货记录表6)"
C = app.app.test_client()
conn = sqlite3.connect(db.DB_PATH)
conn.row_factory = sqlite3.Row


def one(sql, *a):
    r = conn.execute(sql, a).fetchone()
    return r[0] if r else None


def must(resp, what):
    if resp.status_code >= 400:
        raise SystemExit(f"失败：{what} → {resp.status_code} {resp.get_data(as_text=True)[:200]}")
    return resp.get_json()


print("备份：", system_ops.do_backup("manual", "财务对账同步(发货记录表6)之前"))

# ---------- 档案 ----------
bid = one("SELECT id FROM processing_bases WHERE name IN ('𠓾景通','鑫景通')")
if bid and one("SELECT name FROM processing_bases WHERE id=?", bid) != "鑫景通":
    must(C.put(f"/api/processing_bases/{bid}", json={"name": "鑫景通"}), "供货厂改名 鑫景通")
    print("供货厂 𠓾景通 → 鑫景通")
DISP = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM dispatchers")}
for name in ("高栾凤", "王荣胜"):
    if name not in DISP:
        r = must(C.post("/api/dispatchers", json={"name": name, "notes": "2026-09-22 按财务台账新增"}), f"新增调货人{name}")
        DISP[name] = r.get("id") or one("SELECT id FROM dispatchers WHERE name=?", name)
        print("新增调货人", name)
BASE = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM processing_bases")}
SRC = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM invoice_sources")}
TPL = {r["short_name"]: r["id"] for r in conn.execute("SELECT id, short_name FROM flow_templates")}

# ---------- 逐车同步（车次ID: 要改的字段）----------
D, Bs, S, T = DISP, BASE, SRC, TPL
CHANGES = {
    # 调货人按财务：林凯表里系统没填调货人的车
    **{i: {"dispatcher_id": D["林凯"]} for i in (4, 5, 9, 11, 34, 38, 39, 43)},
    # 邓晶焱表
    **{i: {"dispatcher_id": D["邓晶焱"]} for i in (17, 35, 73, 82)},
    # 高栾凤表（新调货人；原来挂的“邓晶焱”占位供货厂去掉）
    27: {"dispatcher_id": D["高栾凤"]},
    71: {"dispatcher_id": D["高栾凤"], "base_id": None, "template_id": T["配票"], "invoice_source_id": S["煌烽"]},
    77: {"dispatcher_id": D["高栾凤"], "base_id": None},
    # 王荣胜（新调货人）
    19: {"dispatcher_id": D["王荣胜"]},
    # 尹鹏飞表的两条船：货主=尹鹏飞
    54: {"base_id": Bs["尹鹏飞"], "invoice_source_id": S["益周"]},
    57: {"base_id": Bs["尹鹏飞"]},
    # 供货厂：力拓 / 君盛
    63: {"base_id": Bs["力拓"], "template_id": T["直签带票"], "invoice_source_id": None},
    64: {"base_id": Bs["力拓"], "template_id": T["直签带票"], "invoice_source_id": None},
    86: {"base_id": Bs["力拓"]},
    69: {"base_id": Bs["君盛"], "template_id": T["直签带票"], "invoice_source_id": None},
    # 计算路径：财务是带票(供货厂自带票) → 直签带票、不挂票源
    14: {"template_id": T["直签带票"], "invoice_source_id": None},
    **{i: {"template_id": T["直签带票"], "invoice_source_id": None} for i in (65, 66, 68, 70)},
    # 财务：林凯配票(煌烽)
    89: {"base_id": Bs["林凯"], "template_id": T["配票"], "invoice_source_id": S["煌烽"]},
    # 反向
    72: {"template_id": T["反向"]},
    # 票源按财务
    **{i: {"invoice_source_id": S["俊兴"]} for i in (45, 46, 47, 48, 49, 50, 51, 52, 53, 61, 58, 59)},
    60: {"invoice_source_id": S["煌烽"]},
    67: {"invoice_source_id": S["煌烽"]},
}
changed = set()
for tid, patch in sorted(CHANGES.items()):
    cur = conn.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()
    diff = {k: v for k, v in patch.items() if cur[k] != v}
    if not diff:
        continue
    must(C.put(f"/api/trips/{tid}", json=diff), f"车次#{tid}")
    changed.add(tid)
# 以前为了“切割费不算我们成本”用 其他成本=−切割费 冲抵过的车(如李涛 皖LE9644 罚款16310)：规则改了以后冲抵要去掉，否则会多算
for r in conn.execute("SELECT id, other_cost, cutting_fee_total FROM trips WHERE other_cost<0 AND cutting_fee_total>0").fetchall():
    if abs(r["other_cost"] + r["cutting_fee_total"]) < 0.01:
        must(C.put(f"/api/trips/{r['id']}", json={"other_cost": 0}), f"去掉冲抵#{r['id']}")
        changed.add(r["id"])
        print(f"#{r['id']} 去掉 其他成本={r['other_cost']} 的冲抵")
print("改了", len(changed), "车：", sorted(changed))

# ---------- 规则：切割费客户承担，利润不再扣 ----------
n = 0
for node in conn.execute("SELECT id, calc_rule_config FROM template_nodes WHERE node_key='other_cost'").fetchall():
    cfg = json.loads(node["calc_rule_config"] or "{}")
    if "cutting_fee_total" in cfg.get("trip_fields", []):
        new = {"trip_fields": [f for f in cfg["trip_fields"] if f != "cutting_fee_total"]}
        conn.execute("UPDATE template_nodes SET calc_rule_config=?, label='其他成本(手工)' WHERE id=?",
                     (json.dumps(new, ensure_ascii=False), node["id"]))
        system_ops.audit(conn, "template_nodes", node["id"], "calc_rule", node["calc_rule_config"],
                         json.dumps(new, ensure_ascii=False), "切割费由客户承担：已从结算金额扣除，利润不再重复扣")
        n += 1
conn.commit()
print("链条规则 其他成本 节点去掉切割费：", n, "个")

# ---------- 王荣胜那条船：给客户付 13.8%（特批） ----------
must(C.put("/api/trips/19/finance_overrides", json={"rate:give_customer": 13.8}), "王荣胜给客户付13.8")
changed.add(19)
print("#19 给客户付 → 13.8%")

# ---------- 已固化的车：按新数据/新规则重算快照（仍保持固化） ----------
# 7/26~8/25 那期(车次1~10)的量补按当时约定 20 元/吨固化，不按阶梯重算：重算前把 20 写进本车费率
cut_ids = {r[0] for r in conn.execute("SELECT id FROM trips WHERE cutting_fee_total>0 AND status!='voided'")}
locked = {r[0] for r in conn.execute("SELECT trip_id FROM trip_finance WHERE is_locked=1")}
# 固化量补单价和现行阶梯算出来不一样的车(主要是 7/26~8/25 那期约定 20 元/吨)，也把量补写进本车费率，以后重算不会变
for r in conn.execute("SELECT t.*, f.reward_unit_price AS snap_rw FROM trips t JOIN trip_finance f ON f.trip_id=t.id WHERE f.is_locked=1").fetchall():
    if abs((app._compute_finance(conn, r).get("reward_unit_price") or 0) - (r["snap_rw"] or 0)) > 0.001:
        cut_ids.add(r["id"])
redo = sorted((changed | cut_ids) & locked)
for tid in redo:
    snap = conn.execute("SELECT reward_unit_price FROM trip_finance WHERE trip_id=?", (tid,)).fetchone()
    t = conn.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()
    live = app._compute_finance(conn, t)
    patch = {}
    if abs((live.get("reward_unit_price") or 0) - (snap["reward_unit_price"] or 0)) > 0.001:
        patch["rate:reward_profit"] = round(snap["reward_unit_price"], 4)
    must(C.put(f"/api/trips/{tid}/finance_overrides", json=patch), f"重算#{tid}")
print("重算固化快照：", redo)

# ---------- 利润核对基准以当前为准 ----------
with app.app.app_context():
    print("利润核对基准更新：", regression.accept(conn), "车")
conn.close()
print("完成")
