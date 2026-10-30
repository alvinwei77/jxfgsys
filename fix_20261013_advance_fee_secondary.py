#!/usr/bin/env python3
"""fix_20261013_advance_fee_secondary.py — 模板 advance_fee 节点补票源维度 (secondary_ref)

背景（2026-10-13 发现）：
  只有模板 2（反向）的 advance_fee 节点配了 secondary_ref: trip:invoice_source_id，
  模板 1/3/4/5 没配 → get_effective_rate 只查「不区分票源」一级 →
  票源专项行（俊兴 23 / 禄国 25 / 反向 0）对模板 1/3/4/5 车次不生效，
  10-12 后俊兴配票车会被通用档 25 误伤（用户口径：非俊兴才是 25）。

动作：给 template_id IN (1,3,4,5) 的 advance_fee 节点 calc_rule_config 追加
  "secondary_type": "invoice_source", "secondary_ref": "trip:invoice_source_id"
幂等：已含 secondary_ref 即跳过。写 audit_log 留痕。
"""
import json
import shutil
import sqlite3
import sys
from datetime import date

DB = sys.argv[1] if len(sys.argv) > 1 else "data/scrapmetal.db"
TPL_IDS = (1, 3, 4, 5)
FIELD = "calc_rule_config"
KEYS = {"secondary_type": "invoice_source", "secondary_ref": "trip:invoice_source_id"}


def main():
    shutil.copy2(DB, f"data/backup/manual_{date.today():%Y%m%d}_advance_fee_secondary.db")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    changed = []
    for tid in TPL_IDS:
        row = conn.execute(
            "SELECT id, calc_rule_config FROM template_nodes WHERE template_id=? AND node_key='advance_fee'",
            (tid,)).fetchone()
        if not row:
            print(f"template {tid}: 无 advance_fee 节点，跳过")
            continue
        cfg = json.loads(row[FIELD] or "{}")
        if cfg.get("secondary_ref") == KEYS["secondary_ref"]:
            print(f"template {tid} node#{row['id']}: 已含 secondary_ref，跳过")
            continue
        old = row[FIELD]
        cfg.update(KEYS)
        new = json.dumps(cfg, ensure_ascii=False)
        conn.execute("UPDATE template_nodes SET calc_rule_config=? WHERE id=?", (new, row["id"]))
        conn.execute(
            "INSERT INTO audit_log(table_name, record_id, field_name, old_value, new_value, changed_at)"
            " VALUES ('template_nodes', ?, ?, ?, ?, datetime('now','localtime'))",
            (row["id"], FIELD, old, new))
        changed.append((tid, row["id"]))
    conn.commit()
    # 自检：四模板节点均含 secondary_ref
    bad = [r["template_id"] for r in conn.execute(
        "SELECT template_id, calc_rule_config FROM template_nodes WHERE node_key='advance_fee'")
        if not (r["template_id"] == 2 or json.loads(r["calc_rule_config"] or "{}").get("secondary_ref") == KEYS["secondary_ref"])]
    n = conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
    conn.close()
    print(f"改挂节点: {changed}")
    print(f"自检: 未含 secondary_ref 的模板 {bad or '无'}；trips 行数 {n}")
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
