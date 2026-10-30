"""命令行利润核对：python3 regression_check.py [数据库路径] [--accept]

列出「车次本身没改、利润却变了」的车次（和「系统 → 利润核对」一样）。
--accept 把当前结果全部作为新的核对基准。升级程序后跑一遍，输出为空就说明算法没有意外变化。"""
import os
import sqlite3
import sys

args = [a for a in sys.argv[1:] if not a.startswith("--")]
if args:
    os.environ["SCRAPMETAL_TEST_DB"] = os.path.abspath(args[0])

import db  # noqa: E402
import app  # noqa: E402,F401  注册全部路由/工具函数
import regression  # noqa: E402

conn = sqlite3.connect(db.DB_PATH)
conn.row_factory = sqlite3.Row
with app.app.app_context():
    if "--accept" in sys.argv:
        print("已以当前为准：", regression.accept(conn), "车")
        sys.exit(0)
    rows = regression.check(conn)
    total = conn.execute("SELECT COUNT(*) FROM profit_baseline").fetchone()[0]
if not rows:
    print(f"OK：{total} 车利润与上次核对一致")
    sys.exit(0)
print(f"有 {len(rows)} 车利润和上次核对时不一样（车次本身没改过）：")
for r in rows:
    print(f"  #{r['trip_id']:<4} {r['plate_no'] or '':<10} {r['settlement_date'] or '':<10} "
          f"{r['before']:>12,.2f} → {r['after']:>12,.2f}  ({r['diff']:+,.2f})")
sys.exit(1)
