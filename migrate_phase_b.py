"""B阶段迁移（2026-09-21）：三条线（货物/开票/回款）+ 批次 + 供货厂收款账户。

只加表/加列，不改任何已有数据（用户明确：历史数据要先和财务核对，先按当前数据）。
1. trips.invoice_path：这一车的开票路径，NULL=按链条规则+票源自动推断（lanes.default_invoice_path）。
   取值 self(供货厂自带票) / source(票商·反向→供货厂→欧冶) / ours(我们配票，经俊兴)。
2. base_accounts：供货厂收款账户（一个供货厂多张卡：老板本人/老板指定的其他卡/反向个人户）。
3. batches + batch_trips：开票/打款/提成 批次，一批对应多车，每车分摊金额可改。

可重复执行。运行前自动备份。用法：python3 migrate_phase_b.py [db_path]
"""
import os
import sys
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.environ.get("SCRAPMETAL_TEST_DB") or os.path.join(BASE_DIR, "data", "scrapmetal.db")

DDL = [
    """CREATE TABLE IF NOT EXISTS base_accounts ( -- 供货厂收款账户（都由老板管理，不一定是老板本人的卡）
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        base_id INTEGER NOT NULL REFERENCES processing_bases(id),
        account_name TEXT NOT NULL,          -- 户名
        bank TEXT,                           -- 开户行
        card_tail TEXT,                      -- 卡号后四位（不存完整卡号）
        holder_type TEXT DEFAULT 'owner',    -- owner 老板本人 / designated 老板指定的其他卡 / reverse_personal 反向个人户
        is_active INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""",
    """CREATE TABLE IF NOT EXISTS batches ( -- 开票/打款/提成 批次
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,                  -- invoice / payment / commission
        hop_key TEXT NOT NULL,               -- in/out(开票) p_out/p_in/p_client(回款) commission(提成)
        from_name TEXT,                      -- 付款方/开票方（整批一致时的展示名，不一致记"多方"）
        to_name TEXT,
        doc_no TEXT,                         -- 发票号/流水号
        batch_date TEXT,
        base_account_id INTEGER REFERENCES base_accounts(id),  -- 打到哪张客户卡（p_client）
        total_amount REAL,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""",
    """CREATE TABLE IF NOT EXISTS batch_trips ( -- 批次分摊到车次
        batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
        trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
        amount REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (batch_id, trip_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_batch_trips_trip ON batch_trips(trip_id)",
]


def main(db_path):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{db_path}.bak_before_phase_b_{ts}"
    shutil.copy2(db_path, backup)
    print("备份:", backup)
    conn = sqlite3.connect(db_path)
    # Cowork 虚拟机挂载目录不能删文件，用 TRUNCATE 日志模式（只对本连接生效）
    conn.execute("PRAGMA journal_mode=TRUNCATE")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trips)")}
    if "invoice_path" not in cols:
        conn.execute("ALTER TABLE trips ADD COLUMN invoice_path TEXT")
        print("新增列 trips.invoice_path")
    else:
        print("跳过已存在列 trips.invoice_path")
    for sql in DDL:
        conn.execute(sql)
    conn.commit()
    for t in ("base_accounts", "batches", "batch_trips"):
        print(t, "行数:", conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    print("trips 行数:", conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0])
    conn.close()
    print("完成")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
