#!/usr/bin/env python3
"""2026-10-13：反向(模板2)吨减节点与配票解耦。

用户确认：「反向每吨不-5元，只有配票的（例如俊兴）非林凯才-5元」。
模板2 的 give_customer_adj 节点原与模板1 共用 account_external_settlement_ton_adj
（非林凯调货人 5 元/吨，2026-09-26 起），导致 #135 净利多算了 +2,495.20 吨减。
本脚本把模板2 节点改挂 account_external_settlement_ton_adj_reverse（目录已登记，
默认无任何费率行 = 0），#135 净利 -3,522.52 → -6,017.72（用户知情确认）。

用法：python fix_20261013_reverse_no_tonadj.py [db_path]
幂等：节点已是新关系时跳过。默认库路径 data/scrapmetal.db。
"""
import json
import sqlite3
import sys
import os

BASE = os.path.dirname(os.path.abspath(__file__))
DB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "data", "scrapmetal.db")

OLD_REL = "account_external_settlement_ton_adj"
NEW_REL = "account_external_settlement_ton_adj_reverse"
NEW_CFG = json.dumps({
    "relation_type": NEW_REL,
    "primary_type": "account",
    "primary_ref": "trip:account_id",
    "secondary_type": "dispatcher",
    "secondary_ref": "trip:dispatcher_id",
}, ensure_ascii=False)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
row = conn.execute(
    "SELECT id, calc_rule_config FROM template_nodes WHERE template_id=2 AND node_key='give_customer_adj'"
).fetchone()
if not row:
    print("未找到模板2 give_customer_adj 节点，中止")
    sys.exit(1)
if NEW_REL in (row["calc_rule_config"] or ""):
    print(f"已是新关系，跳过（node id={row['id']}）")
else:
    conn.execute(
        "UPDATE template_nodes SET calc_rule_config=? WHERE id=?",
        (NEW_CFG, row["id"]),
    )
    conn.execute(
        "INSERT INTO audit_log(table_name, record_id, field_name, old_value, new_value, note, changed_at)"
        " VALUES ('template_nodes', ?, 'calc_rule_config', ?, ?, ?, datetime('now','localtime'))",
        (row["id"], row["calc_rule_config"], NEW_CFG,
         "2026-10-13 用户确认：反向不参与-5元/吨吨减，模板2吨减节点改挂反向专用空档；#135净利-3522.52→-6017.72"),
    )
    conn.commit()
    print(f"已更新 node id={row['id']} -> {NEW_REL}")
conn.close()
