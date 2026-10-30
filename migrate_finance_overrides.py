"""逐车费率覆盖（2026-09-21，用户：车次详情"利润分项"下面的框改不了，要能改——默认走费率与关系，但可以改）。

trips 新增 finance_overrides(TEXT, JSON)：{"rate:<节点key>": 数值, ...}，只存这一车改过的费率；
没改过的节点继续按"费率与关系"档案/模板默认值算。只加列，不改任何数据。可重复执行，自动备份。
"""
import os, sys, shutil, sqlite3, datetime as dt
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.environ.get("SCRAPMETAL_TEST_DB") or os.path.join(BASE_DIR, "data", "scrapmetal.db")


def main(db):
    b = f"{db}.bak_before_finance_overrides_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(db, b); print("备份:", b)
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=TRUNCATE")  # Cowork 虚拟机挂载目录不能删文件
    if "finance_overrides" in {r[1] for r in c.execute("PRAGMA table_info(trips)")}:
        print("已存在 trips.finance_overrides，跳过")
    else:
        c.execute("ALTER TABLE trips ADD COLUMN finance_overrides TEXT"); print("新增列 trips.finance_overrides")
    c.commit(); print("trips:", c.execute("SELECT COUNT(*) FROM trips").fetchone()[0])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
