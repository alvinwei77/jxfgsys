"""初始化 SQLite 数据库：建表 + 基础字典种子数据（结算模式 M1-M5）。
运行：python init_db.py  （如需重建，先删除 data/scrapmetal.db 再运行）
"""
import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "scrapmetal.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(open(SCHEMA_PATH, encoding="utf-8").read())

    # 结算模式种子数据（按 v0.2 文档第三节，M1~M5，后续可在"基础档案-结算模式"页面调整）
    modes = [
        ("M1", "自营带票", "我方加工基地自营供货，我方自带票，走货差+票差+量补-垫资模型", "split"),
        ("M2", "外部直签带票", "外部合作加工基地已通过国企风控直签准入，票/财务流不经过我方，我方仅收服务费+量补", "service_fee"),
        ("M3", "外部不带票(户头通道)", "外部合作加工基地不带票，走我方户头通道，收固定服务费/吨或按比例服务费", "service_fee"),
        ("M4", "调货人对接", "外部基地通过调货人对接，走服务费模型并扣减调货人提成", "service_fee"),
        ("M5", "反向自然人开票", "反向自然人开票车次，按其点位计算票差，占用其年度开票额度", "split"),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO trip_modes(code, name, description, calc_type) VALUES (?,?,?,?)",
        modes,
    )

    conn.commit()
    conn.close()
    print(f"数据库已初始化：{DB_PATH}")


if __name__ == "__main__":
    init_db()
