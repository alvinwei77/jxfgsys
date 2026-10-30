"""链条规则环节类型扩展迁移（2026-09-19，用户反馈"类型不够"）。

背景：template_flow_edges.hop_type 建表时写了 CHECK(hop_type IN ('invoice','payment','goods'))，
只有开票/打款/发货三种。但真实链条是：
    钢厂出结算单 → 我方户头根据结算单做台账 → 票源方(益周)根据台账给我们开进项票
    → 我们(俊兴)开票给欧冶(国企垫资) → 再逐级往回打款
前两步既不是"开票"也不是"打款"，用户现在只能凑合选"开票"，图上语义是错的。

这个脚本做两件事：
1. 去掉 hop_type 的 CHECK 约束（SQLite 不能 ALTER 掉 CHECK，只能重建表再搬数据）。
   去掉之后新增环节类型只需要在 engine.HOP_TYPE_CATALOG 加一行，不用再动表结构，
   校验改由 app.py 统一按那份目录做。
2. 把明显被迫误选成"开票"的两类边改成正确类型（只动 hop_type='invoice' 且备注里写明了
   是结算单/台账的那几条，逐条打印出来给人核对；用户要是已经自己改过，条件不命中就不动）。

数据本身(id/seq/各字段内容)原样保留，不新增不删除。
用法：python3 migrate_hop_types.py [db_path]
"""
import os
import sys
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "scrapmetal.db")

NEW_TABLE_SQL = """
CREATE TABLE template_flow_edges_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES flow_templates(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    from_role_type TEXT NOT NULL,
    from_label TEXT NOT NULL,
    to_role_type TEXT NOT NULL,
    to_label TEXT NOT NULL,
    hop_type TEXT NOT NULL,
    node_key_ref TEXT,
    notes TEXT,
    UNIQUE(template_id, seq)
)
"""

COLS = ("id, template_id, seq, from_role_type, from_label, to_role_type, to_label, "
        "hop_type, node_key_ref, notes")

# (备注/标签里出现这个词, 且当前类型是invoice) -> 改成这个类型
RETYPE_RULES = [("结算单", "settlement"), ("台账", "ledger")]


def table_has_check(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='template_flow_edges'"
    ).fetchone()
    return bool(row) and "CHECK" in (row[0] or "").upper()


def main(db_path):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_before_hop_types_{ts}"
    shutil.copy2(db_path, backup_path)
    print(f"[备份] 已备份到: {backup_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    before = conn.execute("SELECT COUNT(*) c FROM template_flow_edges").fetchone()["c"]

    if table_has_check(conn):
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE IF EXISTS template_flow_edges_new")
        conn.execute(NEW_TABLE_SQL)
        conn.execute(f"INSERT INTO template_flow_edges_new ({COLS}) SELECT {COLS} FROM template_flow_edges")
        conn.execute("DROP TABLE template_flow_edges")
        conn.execute("ALTER TABLE template_flow_edges_new RENAME TO template_flow_edges")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        after = conn.execute("SELECT COUNT(*) c FROM template_flow_edges").fetchone()["c"]
        print(f"[结构] 已去掉 hop_type 的 CHECK 约束（重建表），数据 {before} 条 -> {after} 条")
        if before != after:
            raise SystemExit(f"!! 搬运前后条数不一致({before} vs {after})，已回滚前的备份在 {backup_path}")
    else:
        print("[结构] hop_type 已经没有 CHECK 约束，跳过重建")

    changed = []
    for keyword, new_type in RETYPE_RULES:
        rows = conn.execute(
            "SELECT id, template_id, seq, from_label, to_label, notes FROM template_flow_edges "
            "WHERE hop_type='invoice' AND (IFNULL(notes,'') LIKE ? OR from_label LIKE ? OR to_label LIKE ?)",
            (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"),
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE template_flow_edges SET hop_type=? WHERE id=?", (new_type, r["id"]))
            changed.append((r["id"], r["template_id"], r["seq"], r["from_label"], r["to_label"],
                            r["notes"], new_type))
    conn.commit()

    if changed:
        print(f"[改类型] 把 {len(changed)} 条原先只能凑合选'开票'的边改成了正确类型：")
        for c in changed:
            print(f"  边id={c[0]} 模板{c[1]} 第{c[2]}步  {c[3]} -> {c[4]}  备注:{c[5]}  新类型:{c[6]}")
    else:
        print("[改类型] 没有命中需要改类型的边（可能用户已自行调整过）")

    print("[分布] 迁移后各类型条数：")
    for r in conn.execute("SELECT hop_type, COUNT(*) c FROM template_flow_edges GROUP BY hop_type ORDER BY c DESC"):
        print(f"  {r['hop_type']}: {r['c']}")
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
