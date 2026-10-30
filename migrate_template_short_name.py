"""链条规则加"简写"（2026-09-21，用户：车次列表里模式还是M1/M2，不方便阅读）。

flow_templates 新增 short_name 列，车次列表/详情/下钻里都用它显示，M1-M5 代号只留在底层。
默认简写（可在链条规则页"编辑名称/说明"里改）：
  split_company        自营/外部配票-差价模型(企业票源)  → 配票
  split_reverse        反向自然人开票-差价模型          → 反向
  service_fee_direct   外部直签带票-服务费模型          → 直签带票
  service_fee_account  户头通道-服务费模型              → 户头通道
  service_fee_dispatcher 调货人对接-服务费模型          → 调货对接
只加列、只填空值，已有简写不覆盖。可重复执行，自动备份。
"""
import os
import sys
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.environ.get("SCRAPMETAL_TEST_DB") or os.path.join(BASE_DIR, "data", "scrapmetal.db")
DEFAULTS = {"split_company": "配票", "split_reverse": "反向", "service_fee_direct": "直签带票",
            "service_fee_account": "户头通道", "service_fee_dispatcher": "调货对接"}


def main(db_path):
    backup = f"{db_path}.bak_before_short_name_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(db_path, backup)
    print("备份:", backup)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=TRUNCATE")  # Cowork 虚拟机挂载目录不能删文件
    if "short_name" not in {r[1] for r in conn.execute("PRAGMA table_info(flow_templates)")}:
        conn.execute("ALTER TABLE flow_templates ADD COLUMN short_name TEXT")
        print("新增列 flow_templates.short_name")
    for code, short in DEFAULTS.items():
        n = conn.execute("UPDATE flow_templates SET short_name=? WHERE code=? AND (short_name IS NULL OR short_name='')",
                         (short, code)).rowcount
        if n:
            print(f"{code} → {short}")
    conn.commit()
    for r in conn.execute("SELECT id, code, name, short_name FROM flow_templates ORDER BY id"):
        print(r)
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
