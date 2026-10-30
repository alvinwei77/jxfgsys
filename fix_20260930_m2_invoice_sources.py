"""2026-09-30 用户确认：系统里 27 台【直签带票(M2)】车次的"票源方"字段一直是空的
（录入时不确定，留空了），引擎按 rate_links 的"服务费单价全局兜底档 40 元/吨"取数。
现在按 财务/会计 的原始记录把票源方补全、核对后写入系统。

数据来源（两份外部账，交叉核对一致）：
  1) 会计《2026华西发货明细8月(2).xlsx》→ 汇总表 J 列"开票方"：
     每台带票车次的票源方 = 对应带票公司（常州君盛 / 溧阳市力拓 / 无锡鑫景通 / 无锡兴德），
     与逐笔吨位一一吻合（例：09-12 三车 79.76/78.04/65.44 → 力拓/鑫景通/君盛）。
  2) 财务《发货记录表(8).xlsx》→ 林凯-君盛 / 林凯-力拓 / 林凯-鑫景通 / 林凯-兴德
     四张带票分表，逐车吨位与上述归属完全一致。

处理动作：
  a. 新建 4 个"票源方"档案（source_type=front_company 前端公司，
     settlement_type=direct，不带点位；带票路径服务费仍走 40 元/吨全局兜底档，
     所以不新增任何 rate_links，算钱结果零变化）。
  b. 27 台 M2 车次的 invoice_source_id 指向对应票源方；维护 audit_log 留痕。
  c. 幂等：已写过的不重复写。

⚠ 唯一存疑项（不做静默处理，已在交付报表中单列）：
  车次 #86（2026-09-17，29.79 吨，苏DBE075，含税 68,576.58）
  —— 系统 + 财务表《林凯-力拓》都归"力拓"，会计最终表归"君盛"，
  影响 ±506.43 元（29.79×17）。本脚本按 系统/财务表 口径写"溧阳市力拓"保持内部一致，
  是否改判为君盛请人工二次核对后决定。

用法：python3 fix_20260930_m2_invoice_sources.py [db_path]（默认 data/scrapmetal.db）
"""
import os
import sys
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "scrapmetal.db")

SCOPE_NOTE = ("2026-09-30 补录：直签带票(M2)车次的票源方（录入时留空）。"
              "依据 会计《2026华西发货明细8月(2).xlsx》汇总表开票方列 + "
              "财务《发货记录表(8).xlsx》带票分表（林凯-君盛/力拓/鑫景通/兴德），逐车吨位核对一致。")

# 票源方档案：(会计全称, 对应供货厂名, 备注)
SOURCES = [
    ("常州君盛", "君盛", "带票公司（直签带票 M2 的开票方），会计台账号「常州君盛」"),
    ("溧阳市力拓", "力拓", "带票公司（直签带票 M2 的开票方），会计台账号「溧阳市力拓」"),
    ("无锡鑫景通", "鑫景通", "带票公司（直签带票 M2 的开票方），会计台账号「无锡鑫景通」"),
    ("无锡兴德", "兴德", "带票公司（直签带票 M2 的开票方），会计台账号「无锡兴德」"),
]

# 车次 → 票源方（27 台）
TRIP_MAP = {
    "常州君盛": [14, 68, 69, 74, 79, 87, 91, 92, 98, 99],
    "溧阳市力拓": [63, 64, 65, 70, 83, 86, 106],
    "无锡鑫景通": [66, 78],
    "无锡兴德": [97, 100, 101, 102, 105, 108, 113, 114],
}
# 系统 base_id（供货厂）→ 票源方，用于自检"归属与供货厂一致"
BASE_TO_SRC = {6: "常州君盛", 7: "溧阳市力拓", 8: "无锡鑫景通", 11: "无锡兴德"}


def main(db):
    assert os.path.exists(db), db
    b = f"{db}.bak_before_fix_m2_sources_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(db, b)
    print("备份:", b)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=TRUNCATE")

    # ---------- 1. 自检：27 台车次的现状 ----------
    all_ids = [i for ids in TRIP_MAP.values() for i in ids]
    assert len(all_ids) == len(set(all_ids)) == 27, "车次清单重复"
    bad = []
    for tid in sorted(all_ids):
        r = c.execute("SELECT id, mode_code, base_id, invoice_source_id FROM trips WHERE id=?",
                      (tid,)).fetchone()
        if r is None:
            bad.append((tid, "车次不存在")); continue
        if r["mode_code"] != "M2":
            bad.append((tid, "非 M2：%s" % r["mode_code"])); continue
        if r["invoice_source_id"] is not None:
            bad.append((tid, "票源方已非空：%s" % r["invoice_source_id"])); continue
        want = BASE_TO_SRC.get(r["base_id"])
        got = [k for k, v in TRIP_MAP.items() if tid in v][0]
        if want != got:
            bad.append((tid, "归属与供货厂不符：base=%s→%s，清单=%s" % (r["base_id"], want, got)))
    if bad:
        print("⚠ 自检不通过，已中止：")
        for x in bad:
            print("   ", x)
        sys.exit(1)
    print("自检通过：27 台车次均为 M2 且票源方为空，归属与供货厂一致（#86 见文件头说明）")

    # ---------- 2. 建票源方档案 ----------
    name2id = {}
    for name, base_name, note in SOURCES:
        row = c.execute("SELECT id FROM invoice_sources WHERE name=?", (name,)).fetchone()
        if row:
            name2id[name] = row["id"]; print("已存在票源方：", name, "id=", row["id"]); continue
        base = c.execute("SELECT id FROM processing_bases WHERE name=?", (base_name,)).fetchone()
        bid = base["id"] if base else None
        nid = c.execute(
            """INSERT INTO invoice_sources (name, source_type, tax_type, rate_point, settlement_type,
               outbound_rate_point, quota_period, quota_amount, warn_threshold, bound_mill_id,
               bound_base_id, contact, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, "front_company", None, None, "direct", None, None, None, None, None, bid, None,
             note + "；" + SCOPE_NOTE)).lastrowid
        name2id[name] = nid
        print("新建票源方：", name, "id=", nid, "绑定供货厂=", base_name)

    # ---------- 3. 写车次票源方 ----------
    total = 0
    for name, ids in TRIP_MAP.items():
        nid = name2id[name]
        for tid in ids:
            old = c.execute("SELECT invoice_source_id, base_id, settlement_date, plate_no,"
                            " settlement_tonnage_total FROM trips WHERE id=?", (tid,)).fetchone()
            c.execute("""UPDATE trips SET invoice_source_id=?, updated_at=datetime('now','localtime')
                         WHERE id=? AND invoice_source_id IS NULL""", (nid, tid))
            c.execute("""INSERT INTO audit_log (table_name, record_id, field_name, old_value,
                         new_value, note) VALUES ('trips',?,'invoice_source_id','',?,?)""",
                      (tid, name, SCOPE_NOTE))
            total += 1
            print("  #%-4s %s %-10s base=%-2s → 票源方=%s (id=%s)" % (
                tid, old["settlement_date"], old["plate_no"] or "-", old["base_id"], name, nid))
    c.commit()
    print("共写入 %d 台车次的票源方" % total)

    # ---------- 4. 复核 ----------
    left = c.execute("""SELECT COUNT(*) n FROM trips WHERE status!='voided'
                        AND mode_code='M2' AND invoice_source_id IS NULL""").fetchone()["n"]
    print("复核：仍未配票源方的 M2 车次 =", left, "（应为 0）")
    print("\n当前票源方档案：")
    for r in c.execute("""SELECT s.id, s.name, s.source_type, s.settlement_type, p.name base
                          FROM invoice_sources s LEFT JOIN processing_bases p ON p.id=s.bound_base_id
                          ORDER BY s.id"""):
        print("   %-2s %-12s %-18s %-8s 绑定供货厂=%s" % (
            r["id"], r["name"], r["source_type"], r["settlement_type"] or "-", r["base"] or "-"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
