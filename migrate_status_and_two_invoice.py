"""一次性迁移脚本（2026-09-17）：
1. 状态精简：11个状态 -> 6个（已报备/已到厂/已结算/已开票/已结案/作废）。
   旧状态归并：待报备/在途 -> 已报备；已过毛磅/已过皮磅 -> 已到厂；已回款 -> 已开票
   （回款信息改为"已开票"状态下的打款记录字段，不再单列状态）。
   注意：数据库 CHECK 约束本身不做收紧（不重建 trips 表，风险更低），只迁移数据 +
   应用层 STATUS_LIST 收紧为6项，今后新建/修改车次只会写入这6个值。
2. 两票三款核对：trips 新增 invoice_out_no/invoice_out_amount/invoice_in_no/
   invoice_in_amount/invoice_in_stock 5个字段；payments 新增 pay_type 字段。
   仅"经过我们"票源方（invoice_sources.settlement_type='pass_through'，目前即益周）车次使用。

用法：python3 migrate_status_and_two_invoice.py [db_path]
默认操作 data/scrapmetal.db。会先做一份带时间戳的整库备份。可重复执行（幂等）。
"""
import os
import sys
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "scrapmetal.db")

STATUS_MAP = {
    "pending_report": "reported",
    "in_transit": "reported",
    "gross_weighed": "arrived",
    "tare_weighed": "arrived",
    "paid": "invoiced",
}

TRIP_NEW_COLUMNS = [
    ("invoice_out_no", "TEXT"),
    ("invoice_out_amount", "REAL"),
    ("invoice_in_no", "TEXT"),
    ("invoice_in_amount", "REAL"),
    ("invoice_in_stock", "INTEGER DEFAULT 0"),
]

PAYMENT_NEW_COLUMNS = [
    ("pay_type", "TEXT"),
]


def _existing_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(conn, table, columns):
    existing = _existing_columns(conn, table)
    added = []
    for name, decl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            added.append(name)
    return added


def migrate(db_path):
    if not os.path.exists(db_path):
        print(f"[跳过] 数据库不存在: {db_path}")
        return

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_before_status_two_invoice_migration_{ts}"
    shutil.copy2(db_path, backup_path)
    print(f"[备份] 已备份到: {backup_path}")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        print("[步骤1/3] 检查/新增字段 ...")
        added_trip_cols = _add_missing_columns(conn, "trips", TRIP_NEW_COLUMNS)
        added_pay_cols = _add_missing_columns(conn, "payments", PAYMENT_NEW_COLUMNS)
        conn.commit()
        print(f"  trips 新增字段: {added_trip_cols or '(已存在，跳过)'}")
        print(f"  payments 新增字段: {added_pay_cols or '(已存在，跳过)'}")

        print("[步骤2/3] 归并旧状态 ...")
        before = dict(conn.execute("SELECT status, COUNT(*) c FROM trips GROUP BY status").fetchall())
        print(f"  迁移前状态分布: {before}")
        total_updated = 0
        for old_status, new_status in STATUS_MAP.items():
            cur = conn.execute("UPDATE trips SET status=? WHERE status=?", (new_status, old_status))
            if cur.rowcount:
                print(f"  {old_status} -> {new_status}: {cur.rowcount} 条")
                total_updated += cur.rowcount
        conn.commit()
        after = dict(conn.execute("SELECT status, COUNT(*) c FROM trips GROUP BY status").fetchall())
        print(f"  迁移后状态分布: {after}")

        print("[步骤3/3] 校验 ...")
        allowed = {"reported", "arrived", "settled", "invoiced", "closed", "voided"}
        bad = {s: c for s, c in after.items() if s not in allowed}
        if bad:
            print(f"  [警告] 仍有非法状态未归并: {bad}（请检查 STATUS_MAP 是否遗漏）")
        else:
            print("  OK：所有车次状态均已在新的6个状态范围内")

        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            print(f"  [警告] 外键校验发现问题: {fk_errors}")
        else:
            print("  OK：外键完整性校验通过")

        print(f"\n共更新 {total_updated} 条车次状态。迁移完成。")
    finally:
        conn.close()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    migrate(target)
