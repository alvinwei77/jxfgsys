"""2026-10 期反向票点成本 14.5%（10-01 起）——幂等落库脚本（v2.4.4 配套）。

背景（用户 2026-10-11 指令）：
  「10月期，10.1 开始的反向路径按照 14.5 票点成本计算」。
  经 AskUserQuestion 确认：① 只 2026-10 期（10-01 ~ 10-25，11 期起自动回退档案 13.8）；
  ② 两个反向自然人都配（反向-闫东朋 id 1、反向-尹鹏飞 id 5）。
  用户同时确认：14.5 > 对外 14% → #135 苏淮货15666 净利由 +4,902.29 翻为 −3,522.52（Δ=−8,424.80
  = 票面额 1,203,543.55 × 0.7%），属预期（「成本就是涨到 14.5」）。

实现方式：**零代码改动**，只往 §45 建的「票源方分期」表 `invoice_source_terms` 插两行：
  · rate_point = 14.5（引擎 `_effective_invoice_source(as_of=settlement_date)` 命中即覆盖档案列）
  · 额度/预警/接单状态**沿用各自当前生效值**，避免改动水位面板语义：
      - 尹鹏飞(id5)：档案 350 万/180 万、accepting=1（原本无分期行）
      - 闫东朋(id1)：quota 空、accepting=0（原本生效行 id 6「已停止接单」，语义保持）
  窗口外（≤09-30 或 ≥10-26）没有命中行 → 自动回退档案 13.8，零影响。

线上落地走的是运行中服务的通用档案接口（自动写 audit_log）。
本脚本供从旧备份重建时使用：幂等（同 (invoice_source_id, effective_from) 已存在即跳过）。
用法：python fix_20261011_reverse_point_145.py [db_path]
"""
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "data/scrapmetal.db"

ROWS = [
    # (invoice_source_id, effective_from, effective_to, quota_amount, warn_threshold,
    #  accepting, rate_point, notes)
    (5, "2026-10-01", "2026-10-25", 3500000.0, 1800000.0, 1, 14.5,
     "2026-10期(10-01起)反向票点成本按14.5计（用户2026-10-11）；额度350万/预警180万沿用档案，11期起自动回退13.8"),
    (1, "2026-10-01", "2026-10-25", None, None, 0, 14.5,
     "2026-10期(10-01起)反向票点成本按14.5计（用户2026-10-11）；沿用停接单状态(accepting=0)"),
]


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    added = 0
    for (sid, ef, et, quota, warn, acc, rp, notes) in ROWS:
        ex = cur.execute(
            "SELECT * FROM invoice_source_terms WHERE invoice_source_id=? AND date(effective_from)=date(?)",
            (sid, ef),
        ).fetchone()
        if ex:
            print(f"· 跳过 src={sid} {ef}（已存在 id={ex['id']} rate_point={ex['rate_point']}）")
            continue
        cur.execute(
            "INSERT INTO invoice_source_terms "
            "(invoice_source_id, effective_from, effective_to, quota_amount, warn_threshold, "
            " accepting, rate_point, notes) VALUES (?,?,?,?,?,?,?,?)",
            (sid, ef, et, quota, warn, acc, rp, notes),
        )
        rid = cur.lastrowid
        cur.execute(
            "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, note) "
            "VALUES ('invoice_source_terms', ?, '(新建)', NULL, ?, ?)",
            (rid, f"src={sid} rate_point=14.5 {ef}~{et}", "2026-10 期反向票点成本 14.5%（用户 2026-10-11）"),
        )
        print(f"✓ 新增分期 id={rid} src={sid} {ef}~{et} rate_point={rp}")
        added += 1
    conn.commit()
    print(f"\n完成：新增 {added} 行")
    print("--- 复核 ---")
    for r in cur.execute(
        "SELECT id, invoice_source_id, effective_from, effective_to, quota_amount, "
        "accepting, rate_point FROM invoice_source_terms ORDER BY id"
    ):
        print("  ", tuple(r))
    conn.close()


if __name__ == "__main__":
    main()
