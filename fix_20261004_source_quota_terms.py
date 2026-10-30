#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""票源方「分期额度 / 接单状态」建表 + 种子（幂等）

背景（用户 2026-10-04 指令）：
  「前端公司，票额，也有时限（按月）。例如煌烽、益周 10 月期已经没票额了，不计入了。」
  「闫东朋反向，目前是不接了。仅仅 9 月份接他的单，10 月份不给他配票接单了。」
  「整个系统你也捋一下，把这种业务自由度、时效性之类的都升级一下，而不是这样写死。」

问题：invoice_sources.quota_amount 是**单值**，表达不了"这家这期有票额、下期没了"，
      也表达不了"某家从某期起不再接单"。于是水位页只能一直按老额度画，看着不对。

方案：新增 invoice_source_terms（票源方 × 生效区间 → 额度/预警线/是否接单）。
      水位计算按"今天"命中哪一行就用哪一行；没有命中行则回退 invoice_sources 的默认值
      （向后兼容，不影响俊兴/尹鹏飞等未做分期的票源）。

本脚本：
  1) 建表（与 schema.sql 一致，IF NOT EXISTS）；
  2) 幂等写入 3 家的分期记录：
       益周 / 煌烽     → 2026-09-26 起无票额（accepting=0，quota 置空）→ 不计入水位
       反向-闫东朋     → 2026-09-26 起停止接单（accepting=0）→ 不计入水位
     （历史行一并留档，便于追溯 8/9 期当时的额度）
  3) 写 audit_log 留痕。
不改任何车次金额、不改 rate_links、不改 invoice_sources 台账列。
"""
import os
import sys
import sqlite3
import shutil
import datetime

ROOT = "/Users/alvin/Documents/scrapmetal"
# 允许用 SCRAPMETAL_DB 指向副本（无沙箱限制的路径）先改再拷回，避免直接写生产库受限。
DB = os.environ.get("SCRAPMETAL_DB") or os.path.join(ROOT, "data", "scrapmetal.db")

DDL = """
CREATE TABLE IF NOT EXISTS invoice_source_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_source_id INTEGER NOT NULL REFERENCES invoice_sources(id) ON DELETE CASCADE,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    quota_amount REAL,
    warn_threshold REAL,
    accepting INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_source_terms_src ON invoice_source_terms(invoice_source_id, effective_from);
"""

# (票源方名字, effective_from, effective_to, quota, warn, accepting, notes)
# 语义：accepting=0 → 已停止接单；accepting=1 且 quota 为空 → 本期没有票额。两者都不计入水位。
SEEDS = [
    # —— 益周：8/9 期月度 500 万；10 期起没有票额（仍可接单，只是这期没票额） ——
    ("益周", "2026-07-01", "2026-09-25", 5_000_000.0, None, 1,
     "历史留档：2026-08/09 期月度票额 500 万（当时按 500 万计水位）"),
    ("益周", "2026-09-26", None, None, None, 1,
     "2026-10 期起没有票额（用户指令 2026-10-04）→ 不计入水位。若以后恢复，新增一条自恢复日的记录即可。"),
    # —— 煌烽：同上 ——
    ("煌烽", "2026-07-01", "2026-09-25", 5_000_000.0, None, 1,
     "历史留档：2026-08/09 期月度票额 500 万（当时按 500 万计水位）"),
    ("煌烽", "2026-09-26", None, None, None, 1,
     "2026-10 期起没有票额（用户指令 2026-10-04）→ 不计入水位。若以后恢复，新增一条自恢复日的记录即可。"),
    # —— 反向-闫东朋：只 9 月接单，10 期起停止接单 ——
    ("反向-闫东朋", "2026-01-01", "2026-09-25", 3_500_000.0, 1_800_000.0, 1,
     "历史留档：年度额度 350 万、预警线 180 万；仅 2026-09 期有接单（8 车）"),
    ("反向-闫东朋", "2026-09-26", None, None, None, 0,
     "2026-10 期起停止接单，不再给他配票（用户指令 2026-10-04）。"
     "名下车次仍保留在历史期，只是不再进入本期水位与预警。"),
]


def main():
    if not os.path.exists(DB):
        print("DB not found:", DB)
        sys.exit(1)
    bak_dir = os.path.join(ROOT, "data", "backup")
    os.makedirs(bak_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(bak_dir, f"manual_{ts}_source_quota_terms.db")
    shutil.copy2(DB, bak)
    print("备份:", bak)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    existed = cur.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='invoice_source_terms'"
    ).fetchone()[0]
    cur.executescript(DDL)
    conn.commit()
    print(("✓ 表 invoice_source_terms 已存在" if existed else "✓ 已新建表 invoice_source_terms"))

    # 票源方 id 解析（按名字，避免写死 id）
    name2id = {r["name"]: r["id"] for r in cur.execute("SELECT id, name FROM invoice_sources")}
    missing = [n for (n, *_rest) in SEEDS if n not in name2id]
    if missing:
        print("⚠ 未在档案里找到票源方:", missing)
        sys.exit(1)

    ins = upd = 0
    touched = []
    for name, efrom, eto, quota, warn, accepting, notes in SEEDS:
        sid = name2id[name]
        dup = cur.execute(
            "SELECT * FROM invoice_source_terms WHERE invoice_source_id=? AND effective_from=?",
            (sid, efrom),
        ).fetchone()
        want = (eto, quota, warn, accepting, notes)
        if dup:
            have = (dup["effective_to"], dup["quota_amount"], dup["warn_threshold"], dup["accepting"], dup["notes"])
            if have == want:
                print(f"  · 幂等跳过 {name} @ {efrom}（已是目标值 #{dup['id']}）")
            else:
                cur.execute(
                    "UPDATE invoice_source_terms SET effective_to=?, quota_amount=?, warn_threshold=?, "
                    "accepting=?, notes=? WHERE id=?",
                    (eto, quota, warn, accepting, notes, dup["id"]),
                )
                upd += 1
                touched.append((name, efrom, dup["id"], "更新"))
                print(f"  ~ 更新 {name} #{sid} [{efrom} ~ {eto or '至今'}] quota={quota} accepting={accepting}")
            continue
        cur.execute(
            "INSERT INTO invoice_source_terms "
            "(invoice_source_id, effective_from, effective_to, quota_amount, warn_threshold, accepting, notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, efrom, eto, quota, warn, accepting, notes),
        )
        ins += 1
        touched.append((name, efrom, cur.lastrowid, "新建"))
        print(f"  + {name} #{sid} [{efrom} ~ {eto or '至今'}] quota={quota} warn={warn} accepting={accepting}")
    conn.commit()

    if ins or upd:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for name, efrom, rid, how in touched:
            cur.execute(
                "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, note, changed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("invoice_source_terms", rid, "(%s)" % how, None,
                 efrom, f"票源额度分期·{name} 自{efrom}起", now),
            )
        conn.commit()
        print(f"✓ 新建 {ins} 条 / 更新 {upd} 条，已写 audit_log")
    else:
        print("✓ 全部已存在且为目标值，无改动（幂等）")

    # 复核
    print("\n=== 复核：current 生效行（今天 %s）===" % datetime.date.today().isoformat())
    today = datetime.date.today().isoformat()
    for name in ("益周", "煌烽", "反向-闫东朋", "俊兴", "反向-尹鹏飞"):
        sid = name2id[name]
        t = cur.execute(
            "SELECT * FROM invoice_source_terms WHERE invoice_source_id=? AND date(effective_from)<=date(?) "
            "AND (effective_to IS NULL OR date(effective_to)>=date(?)) "
            "ORDER BY date(effective_from) DESC, id DESC LIMIT 1",
            (sid, today, today),
        ).fetchone()
        print("  %-12s → %s" % (name, ("(无分期记录，走档案默认)" if not t else
              "quota=%s warn=%s accepting=%s" % (t["quota_amount"], t["warn_threshold"], t["accepting"]))))

    ic = cur.execute("PRAGMA integrity_check").fetchone()[0]
    ntot = cur.execute("SELECT COUNT(*) FROM invoice_source_terms").fetchone()[0]
    ntrip = cur.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
    print(f"\n体检 integrity_check={ic}  terms 行数={ntot}  trips={ntrip}")
    conn.close()


if __name__ == "__main__":
    main()
