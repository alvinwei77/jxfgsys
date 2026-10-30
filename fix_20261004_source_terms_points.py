#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给「票源方分期」表补两个可按期覆盖的**票点成本**字段（幂等）。

背景（用户 2026-10-04 指令）：
  「每一期都有新的结算规则和**票点成本**…整个系统你也捋一下，把这种业务自由度、时效性之类的
    都升级一下，而不是这样写死。」

问题：`invoice_sources.rate_point` / `outbound_rate_point` 是**单值列**——票点成本每期都会变
（比如俊兴这期 13.5、下期 13.2），但改一次就把 8/9 月期的历史口径一起改了，且没有留痕。
`rate_links` 那套「生效区间」机制只覆盖了费率类节点，票点走的是 `rate_deduct_invoice_source`
计算规则、直接读档案列，绕过了 rate_links。

方案：把票点也纳入**同一张 `invoice_source_terms` 分期表**（按票源方 × 生效区间），
  新增 `rate_point` / `outbound_rate_point` 两列：
    · 分期记录里给了值 → 该时段用分期值；
    · 分期记录里留空 / 该时段没命中任何分期记录 → **回退档案列**（零影响，向后兼容）。
  引擎 `engine._effective_invoice_source()` 在取票点前先做这一层覆盖。

本脚本：
  1) 幂等 ALTER TABLE 补两列（已存在则跳过）；
  2) 不写任何业务数据（现有票源方一律留空 = 继续用档案列值，改造前后完全一致）；
  3) 写一条 audit_log 留痕（结构变更）。
"""
import os
import sys
import sqlite3
import shutil
import datetime

ROOT = "/Users/alvin/Documents/scrapmetal"
DB = os.environ.get("SCRAPMETAL_DB") or os.path.join(ROOT, "data", "scrapmetal.db")

NEW_COLS = [
    ("rate_point", "REAL"),
    ("outbound_rate_point", "REAL"),
]


def main():
    if not os.path.exists(DB):
        print("DB not found:", DB)
        sys.exit(1)

    bak_dir = os.path.join(ROOT, "data", "backup")
    os.makedirs(bak_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(bak_dir, f"manual_{ts}_source_terms_points.db")
    shutil.copy2(DB, bak)
    print("备份:", bak)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    exists = cur.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='invoice_source_terms'"
    ).fetchone()[0]
    if not exists:
        print("✗ 表 invoice_source_terms 不存在，请先跑 fix_20261004_source_quota_terms.py")
        sys.exit(1)

    have = {r["name"] for r in cur.execute("PRAGMA table_info(invoice_source_terms)")}
    added = []
    for col, typ in NEW_COLS:
        if col in have:
            print(f"  · 已存在，跳过 {col}")
            continue
        cur.execute(f"ALTER TABLE invoice_source_terms ADD COLUMN {col} {typ}")
        added.append(col)
        print(f"  + 已新增列 {col} {typ}")
    conn.commit()

    if added:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute(
            "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, note, changed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("invoice_source_terms", 0, "(新增列)", None, ",".join(added),
             "票源方分期表补「票点成本」可按期覆盖字段（rate_point/outbound_rate_point），"
             "留空=沿用票源方档案列", now),
        )
        conn.commit()
        print(f"✓ 新增 {len(added)} 列：{added}；已写 audit_log")
    else:
        print("✓ 两列都已存在，无结构改动（幂等）")

    # —— 复核：结构 + 数据现状 + 体检 ——
    cols = [r["name"] for r in cur.execute("PRAGMA table_info(invoice_source_terms)")]
    print("\n=== 复核 invoice_source_terms 列 ===")
    print("  ", cols)
    print("=== 现有分期行（rate_point / outbound_rate_point 应全为空）===")
    for r in cur.execute("SELECT id, invoice_source_id, effective_from, effective_to, quota_amount, "
                         "accepting, rate_point, outbound_rate_point FROM invoice_source_terms ORDER BY id"):
        print("  ", dict(r))
    ic = cur.execute("PRAGMA integrity_check").fetchone()[0]
    ntrips = cur.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
    print(f"\n体检 integrity_check={ic}  terms={cur.execute('SELECT COUNT(*) FROM invoice_source_terms').fetchone()[0]} 行  trips={ntrips}")
    conn.close()


if __name__ == "__main__":
    main()
