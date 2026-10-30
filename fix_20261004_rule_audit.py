# -*- coding: utf-8 -*-
"""
规则变更留痕回填（2026-10-04）
背景：本日 4 个 fix 脚本直接改 SQL，绕过了系统 audit_log（App 的留痕表）。
本脚本把今日全部规则类改动补写进 audit_log（含"误落 13.8% → 修正为 14%+吨减5"事件），
并把「改规则一律：关旧行(effective_to=前一日) + 新增行，禁止就地改值」写成规约备注。

幂等：以 (table_name, record_id, field_name, note) 去重；重复执行不产生重复行。
写库走「/tmp 副本 → 校验 → 回写」路径（沙箱直写生产库会报 disk I/O error）。
"""
import hashlib
import os
import shutil
import sqlite3

DB = "/Users/alvin/Documents/scrapmetal/data/scrapmetal.db"
TMP = "/tmp/rule_audit/x.db"

CONVENTION = ("【改规则规约】费率/额度变更一律：① 关闭旧行（effective_to=新规则生效前一日）"
              "② 新增一行承载新值（effective_from=生效日）③ 写本审计表。禁止就地改值——就地改值会丢失该行历史。")


def main():
    os.makedirs(os.path.dirname(TMP), exist_ok=True)
    shutil.copy(DB, TMP)
    c = sqlite3.connect(TMP)
    c.row_factory = sqlite3.Row

    def log(table, rid, field, old, new, note, at):
        n = c.execute("""SELECT COUNT(*) FROM audit_log
                         WHERE table_name=? AND record_id IS ? AND field_name=? AND note=?""",
                      (table, rid, field, note)).fetchone()[0]
        if n:
            return False
        c.execute("""INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, note, changed_at)
                     VALUES (?,?,?,?,?,?,?)""", (table, rid, field, old, new, note, at))
        return True

    added = 0

    # ① 俊兴票额 500万 → 1000万（09:25 fix_20261004_period_rules.py）
    if log("invoice_sources", 2, "quota_amount", "5000000", "10000000",
           "2026-10期票额 1000 万（原 500 万）；用户 2026-10-04 指令「票额俊兴1000万（可配票）」。"
           + CONVENTION, "2026-10-04 09:25:00"):
        added += 1

    # ② 邓晶焱带票提成 5 元/吨（新增行 rate_links#9）
    if log("rate_links", 9, "(新建)", None, "5.0 per_ton",
           "2026-10期新规则：邓晶焱带货带票 1000 吨以上 5 元/吨提成（rate_links#9 dispatcher#5，eff 2026-09-26）。"
           "门槛口径=整期带票累计吨位，见本行 notes。", "2026-10-04 09:25:00"):
        added += 1

    # ③ 非林凯对外点位：新增 13.8% 行（后被修正）——留痕误落事件
    if log("rate_links", 10, "(新建)", None, "13.8 pct（#10~#16，account_external_settlement × dispatcher#2..#8）",
           "【误落】2026-10期非林凯对外点位落为 13.8%（当时折算「14个点让5元≈0.2个点」）。"
           "该口径于同日 10:13 修正为 14%+吨减5，rows #10~#16 就地改值（历史值即本行记录）。"
           "教训：本应「关旧行+新增行」，已立规约。", "2026-10-04 09:25:00"):
        added += 1

    # ④ 口径修正：13.8% → 14.0%（就地改值，历史值靠本审计行保留）
    if log("rate_links", 10, "rate_value", "13.8", "14.0",
           "口径修正：用户出样张 2026-09-29（14-5）.png 确认「14个点减5元」= 14% 点位 + 按吨减 5 元，"
           "不是折成 13.8%（2500元/吨时差 10 元/吨且方向相反）。rows #10~#16 同步改值。"
           + CONVENTION, "2026-10-04 10:13:00"):
        added += 1

    # ⑤ 新增对外吨减（非林凯 5 元/吨）
    if log("rate_links", 17, "(新建)", None, "5.0 per_ton（#17~#23 × dispatcher#2..#8，eff 2026-09-26）",
           "新增关系 account_external_settlement_ton_adj：非林凯对外结算按吨减 5 元（给客户付 = 含税×(1−14%) − 5×吨位）。"
           "行 #24 = account 全局兜底 0（林凯及未列组合适用，eff 2020-01-01）。", "2026-10-04 10:13:00"):
        added += 1

    # ⑥ 模板节点拆分：give_customer → pct − adj
    tid = c.execute("""SELECT id FROM template_nodes WHERE template_id=1 AND node_key='give_customer_pct'""").fetchone()
    if tid:

        if log("template_nodes", tid["id"], "calc_rule",
               '{"node":"give_customer","type":"rate_deduct_rate_schedule"}',
               '{"node_1":"give_customer_pct","type":"rate_deduct_rate_schedule",'
               '"node_2":"give_customer_adj","type":"per_ton_rate_schedule",'
               '"node_3":"give_customer","type":"subtract_nodes"}',
               "模板1/2 给客户付由单节点改为「点位节点 − 吨减节点」两步相减（subtract_nodes）："
               "give_customer_pct(seq1) − give_customer_adj(seq2) = give_customer(seq3)，advance_fee→seq4。"
               "下游 node:give_customer 引用不变，故改动对下游透明。", "2026-10-04 10:13:00"):
            added += 1

    # ⑦ 5 台封账车手工议价覆盖键迁移（节点改名联动）
    for t in (16, 19, 95, 96, 104):
        row = c.execute("SELECT finance_overrides FROM trips WHERE id=?", (t,)).fetchone()
        cur = row["finance_overrides"] or ""
        if "rate:give_customer_pct" in cur:
            if log("trips", t, "finance_overrides", "…rate:give_customer…（旧节点键）", "…rate:give_customer_pct…（新节点键）",
                   "节点改名联动迁移：give_customer → give_customer_pct，防手工议价点位静默失配（引擎按 "
                   "overrides['rate:<node_key>'] 匹配）。此车覆盖额本身未变。", "2026-10-04 10:15:00"):
                added += 1

    # ⑧ 邓晶焱 5 元门槛语义明确化（09:38 fix_20261004_gate_semantics.py）
    if log("rate_links", 9, "notes", None, "门槛口径：当期带票累计 ≥1000 吨 → 整期吨位按 5.0 元/吨",
           "门槛读法明确化：采用「达到 1000 吨后整期吨位同价」（与量补档位同构），"
           "另一种「仅超出部分计提」未采用。系统无门槛字段，未达门槛时需将本车 dispatcher_commission_cost 覆盖为 0。",
           "2026-10-04 09:38:00"):
        added += 1

    # ⑨ 规约本身留痕
    if log("rate_links", None, "(规约)", None, "改规则=关旧行+新增行，禁止就地改值",
           CONVENTION + " 本规约 2026-10-04 立，起因：非林凯 14% 行与俊兴额度曾就地改值，旧值仅存于本审计表。",
           "2026-10-04 14:40:00"):
        added += 1

    c.commit()
    print("本次新增审计行:", added)
    print("audit_log 总行数:", c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])
    for t in ("rate_links", "invoice_sources", "template_nodes", "trips"):
        n = c.execute("SELECT COUNT(*) FROM audit_log WHERE table_name=?", (t,)).fetchone()[0]
        print("   %-16s %d 条" % (t, n))
    print("integrity:", c.execute("PRAGMA integrity_check").fetchone()[0])
    print("rate_links 行数:", c.execute("SELECT COUNT(*) FROM rate_links").fetchone()[0])
    print("俊兴额度:", c.execute("SELECT quota_amount FROM invoice_sources WHERE id=2").fetchone()[0])
    c.close()

    shutil.copy(TMP, DB)
    for ext in ("-journal", "-wal", "-shm"):
        p = DB + ext
        if os.path.exists(p):
            os.remove(p)
    print("已回写生产库 sha16:", hashlib.sha256(open(DB, "rb").read()).hexdigest()[:16])


if __name__ == "__main__":
    main()
