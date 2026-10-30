#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-10-04 新期(2026-10期，结算日 2026-09-26 起)规则落库。

用户 2026-10-04 指令（原文）：
  "上一期已结束，这一期的话，新的规则如下：
   1 票额俊兴1000万（可配票）。
   2 邓晶焱带货带票的，1000吨以上一顿5元提成。
   3 除林凯外，其他报价统一 14个点减5元，林凯正常14个点报价。
   其他规则例如量补，林凯带票15元奖励等不变。"

落库内容（全部为「配置数据」改动，不动任何车次金额、不动引擎代码）：
  A. invoice_sources#2 俊兴：quota_amount 5,000,000 -> 10,000,000（本期票额上限）。
  B. rate_links 新增：dispatcher_commission_daiPiao / 调货人#5(邓晶焱) = 5.0 元/吨，
     effective_from=2026-09-26。⚠「1000吨以上」是期级门槛，费率档只表达单价；
     即时影响=0（本期尚无邓晶焱带票车次），待期末按累计吨位判断是否计提。
  C. rate_links 新增 7 条：account_external_settlement（户头对外结算点位）/
     户头#1 + 调货人#2..#8（非林凯）= 13.8 %，effective_from=2026-09-26。
     林凯(#1)不建行 → 自动落到户头通用档 14.0%，即"林凯正常14个点"。
     13.8% = 14个点让 5 元/吨（用户在 #104 已确认 5元/吨 ≈ 0.2 个点）。
  D. template_nodes：split_company(配票 M1) 的 give_customer 节点加上
     secondary_type=dispatcher / secondary_ref=trip:dispatcher_id，
     使"对外结算点位"能按调货人分档。**只改 split_company**，
     split_reverse(反向 M5) 不动 → 反向的 对外14% / 票源13.8% 口径保持不变。

⚠ 口径假设（写在明处）：第3条的"14个点减5元"按「让 5 元/吨（非林凯报价更优）」落为 13.8%。
   若原意是「在14个点基础上再扣5元/吨」（≈14.2%），改这 7 行的 rate_value 即可，一条 SQL。

安全：调用前自动整库备份；幂等（重复执行不重复插入）；执行后自检。
"""
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

DB = sys.argv[1] if len(sys.argv) > 1 else "data/scrapmetal.db"
if not os.path.isabs(DB):
    DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), DB)

EFF = "2026-09-26"          # 2026-10期首日
NON_LK = [2, 3, 4, 5, 6, 7, 8]   # 除林凯(#1)外的全部调货人
QUOTE_PCT = 13.8            # 非林凯：14个点让5元/吨

assert os.path.exists(DB), DB
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("== DB:", DB)

# ---------- 备份（仅生产库） ----------
if "/tmp/" not in DB:
    bdir = os.path.join(os.path.dirname(DB), "backup")
    os.makedirs(bdir, exist_ok=True)
    dst = os.path.join(bdir, "manual_%s.db" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(DB, dst)
    print("== 备份 ->", dst)

# ---------- A. 俊兴票额 ----------
q = cur.execute("SELECT id,name,quota_period,quota_amount,notes FROM invoice_sources WHERE id=2").fetchone()
assert q and q["name"] == "俊兴", dict(q) if q else None
if abs(float(q["quota_amount"]) - 10_000_000) > 1e-6:
    note = (q["notes"] or "")
    add = ("2026-10-04 用户指令：本期票额 1000 万（可配票），原 500 万；"
           "2026-10期结算日起生效。")
    if "2026-10-04" not in note:
        note = (note + " | " + add) if note else add
    cur.execute("UPDATE invoice_sources SET quota_amount=?, notes=? WHERE id=2", (10_000_000.0, note))
    print("A. 俊兴票额 5,000,000 -> 10,000,000  ✓")
else:
    print("A. 俊兴票额已是 10,000,000，跳过")

# ---------- B. 邓晶焱带票提成 ----------
r = cur.execute("""SELECT id,rate_value FROM rate_links
                   WHERE relation_type='dispatcher_commission_daiPiao'
                     AND primary_entity_id=5 AND secondary_entity_id IS NULL
                     AND effective_from=?""", (EFF,)).fetchone()
if r is None:
    cur.execute("""INSERT INTO rate_links
        (relation_type, primary_entity_type, primary_entity_id,
         secondary_entity_type, secondary_entity_id, rate_value, rate_unit,
         effective_from, effective_to, notes)
        VALUES ('dispatcher_commission_daiPiao','dispatcher',5,NULL,NULL,5.0,'per_ton',?,NULL,?)""",
        (EFF, "2026-10-04 用户指令：邓晶焱带货带票(M2/M3/M4)单期累计>1000吨部分按 5 元/吨提成。"
              "费率档只表达单价，1000吨门槛为期级条件，期末按累计吨位判断是否计提。"))
    print("B. 邓晶焱 带票提成 5 元/吨 已新增（%s 起）  ✓" % EFF)
else:
    print("B. 邓晶焱 带票提成档已存在，跳过")

# ---------- D. split_company.give_customer 增加调货人维度 ----------
row = cur.execute("""SELECT id,calc_rule_config FROM template_nodes
                     WHERE template_id=1 AND node_key='give_customer'""").fetchone()
assert row, "找不到 split_company 的 give_customer 节点"
cfg = json.loads(row["calc_rule_config"] or "{}")
if cfg.get("secondary_ref") != "trip:dispatcher_id":
    cfg["secondary_type"] = "dispatcher"
    cfg["secondary_ref"] = "trip:dispatcher_id"
    cur.execute("UPDATE template_nodes SET calc_rule_config=? WHERE id=?",
                (json.dumps(cfg, ensure_ascii=False), row["id"]))
    print("D. split_company.give_customer 已加 dispatcher 维度  ✓")
else:
    print("D. split_company.give_customer 已含 dispatcher 维度，跳过")
# 反向模板必须保持原样（不带 secondary）
rr = cur.execute("""SELECT calc_rule_config FROM template_nodes
                    WHERE template_id=2 AND node_key='give_customer'""").fetchone()
assert "secondary_ref" not in json.loads(rr["calc_rule_config"] or "{}"), \
    "split_reverse 不应带 secondary_ref（反向口径要保持 14%）"
print("   反向(split_reverse) 未改动  ✓")

# ---------- C. 非林凯 对外结算点位 13.8% ----------
n = 0
for d in NON_LK:
    ex = cur.execute("""SELECT id FROM rate_links
                        WHERE relation_type='account_external_settlement'
                          AND primary_entity_id=1 AND secondary_entity_id=?
                          AND effective_from=?""", (d, EFF)).fetchone()
    if ex:
        continue
    cur.execute("""INSERT INTO rate_links
        (relation_type, primary_entity_type, primary_entity_id,
         secondary_entity_type, secondary_entity_id, rate_value, rate_unit,
         effective_from, effective_to, notes)
        VALUES ('account_external_settlement','account',1,'dispatcher',?,?,'pct',?,NULL,?)""",
        (d, QUOTE_PCT, EFF,
         "2026-10-04 用户指令：除林凯外，其他调货人报价统一 14个点减5元。"
         "5元/吨 ≈ 0.2个点 → 对外结算点位 13.8%（林凯不建行，落户头通用档 14%）。"
         "如原意为『再扣5元/吨（14.2%）』，改本行 rate_value 即可。"))
    n += 1
print("C. 非林凯 对外结算点位 13.8%% 新增 %d 条  ✓" % n)

conn.commit()

# ---------- 自检 ----------
print("\n=== 自检 ===")
print("俊兴票额:", cur.execute("SELECT quota_amount FROM invoice_sources WHERE id=2").fetchone()[0])
print("邓晶焱带票档:", [tuple(x) for x in cur.execute(
    "SELECT rate_value,rate_unit,effective_from FROM rate_links WHERE relation_type='dispatcher_commission_daiPiao' AND primary_entity_id=5")])
print("对外结算点位(户头#1)全部行:")
for x in cur.execute("""SELECT r.id, r.secondary_entity_id, d.name, r.rate_value, r.rate_unit,
                               r.effective_from, r.effective_to
                        FROM rate_links r LEFT JOIN dispatchers d ON d.id=r.secondary_entity_id
                        WHERE r.relation_type='account_external_settlement' ORDER BY r.id"""):
    print("   ", tuple(x))
print("rate_links 总条数:", cur.execute("SELECT COUNT(*) FROM rate_links").fetchone()[0])
print("split_company.give_customer:", cur.execute(
    "SELECT calc_rule_config FROM template_nodes WHERE template_id=1 AND node_key='give_customer'").fetchone()[0])
conn.close()
print("\nOK")
