"""A阶段迁移（2026-09-21，录入提速 + 状态日期联动）。

1. trips 新增 arrived_date / invoiced_date / closed_date 三个日期字段。
   和已有的 order_date(已报备) / settlement_date(已结算) 一起，每个状态都有一个对应日期：
   推进到某状态时该日期为空就自动填今天，回退状态不清空已有日期。
   历史车次这三个字段留空（不瞎编日期），只影响以后的推进。
2. 新建 app_settings(key,value) 小表，存"录入默认值"，目前只有 default_departure_place=永康。
   钢厂/户头不存默认值，按"启用的钢厂只有一个 → 自动选；户头跟钢厂绑定"实时推断。

可重复执行（已存在的列/表/键跳过）。运行前自动备份。
用法：python3 migrate_phase_a.py [db_path]
"""
import os
import sys
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.environ.get("SCRAPMETAL_TEST_DB") or os.path.join(BASE_DIR, "data", "scrapmetal.db")

NEW_TRIP_COLS = [
    ("arrived_date", "TEXT"),   # 已到厂日期
    ("invoiced_date", "TEXT"),  # 已开票日期
    ("closed_date", "TEXT"),    # 已结案日期
]
DEFAULT_SETTINGS = {"default_departure_place": "永康"}


def main(db_path):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{db_path}.bak_before_phase_a_{ts}"
    shutil.copy2(db_path, backup)
    print("备份:", backup)
    conn = sqlite3.connect(db_path)
    # 在 Cowork 的 Linux 虚拟机里跑时，挂载目录不允许删除文件，SQLite 默认 DELETE 日志模式
    # 提交时要删 -journal 文件 → "disk I/O error"。改用 TRUNCATE（只清空不删除），
    # 这个设置只对本连接有效，不影响 Mac 上系统本身的日志模式。
    conn.execute("PRAGMA journal_mode=TRUNCATE")
    existing = {r[1] for r in conn.execute("PRAGMA table_info(trips)")}
    for col, typ in NEW_TRIP_COLS:
        if col in existing:
            print("跳过已存在列 trips.", col)
        else:
            conn.execute(f"ALTER TABLE trips ADD COLUMN {col} {typ}")
            print("新增列 trips.", col)
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    for k, v in DEFAULT_SETTINGS.items():
        cur = conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES (?,?)", (k, v))
        print(("写入" if cur.rowcount else "已存在，跳过"), "设置", k, "=", v)
    conn.commit()
    print("trips 行数:", conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0])
    conn.close()
    print("完成")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
