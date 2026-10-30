#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-10 期「邓晶焱带票 ≥1000 吨」门槛语义 明确化（幂等）

背景：rate_links 只有「单价」，没有门槛字段；引擎 dispatcher_commission_daiPiao
      按单价无条件计提。用户原话「1000吨以上一顿5元提成」有两种读法：
        (a) 当期带票累计 ≥1000 吨 → 当期带票吨位**整体**按 5 元/吨
        (b) **超出** 1000 吨的部分按 5 元/吨
      与「量补」档位的既有惯例同构（≥5000 吨 → 25 元/吨 是整期吨位，见 9 月期
      9,505.07×25），故采用 (a) 为默认口径，并把两种读法与切换点写进 notes，
      避免后续期数误用。

本脚本只改 rate_links#9 的 notes 文本（不动 rate_value、不动任何车次金额）。
"""
import os, sys, sqlite3, shutil, datetime

ROOT = "/Users/alvin/Documents/scrapmetal"
DB = os.path.join(ROOT, "data", "scrapmetal.db")
RID = 9

NEW_NOTES = (
    "2026-10-04 用户指令：邓晶焱带货带票(M2/M3/M4) 1000 吨以上 5 元/吨提成。"
    "【门槛口径】与量补档位同构，采用『当期带票累计吨位 ≥1000 吨 → 当期带票吨位整体按 5.0 元/吨』；"
    "另一读法『仅超出 1000 吨的部分按 5 元/吨』未采用。"
    "【系统承载方式】本行只表达单价；1000 吨是按结算期(26日~次25日)累计的判断条件，"
    "系统无门槛字段，未达门槛时应将本车 dispatcher_commission_cost 覆盖为 0，"
    "期末按 tripload 累计吨位复核。切换口径只改本行备注 + 期末覆盖规则，不动 rate_value。"
    "【本期影响】2026-10 期尚无邓晶焱带票车次，即时影响 0。"
)

def main():
    if not os.path.exists(DB):
        print("DB not found:", DB); sys.exit(1)
    bak_dir = os.path.join(ROOT, "data", "backup")
    os.makedirs(bak_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(bak_dir, f"manual_{ts}_gate_semantics.db")
    shutil.copy2(DB, bak)
    print("备份:", bak)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    row = cur.execute("SELECT * FROM rate_links WHERE id=?", (RID,)).fetchone()
    if not row:
        print(f"⚠ 未找到 rate_links#{RID}"); sys.exit(1)
    row = dict(row)
    print(f"改前 #{RID}: relation={row['relation_type']} primary=dispatcher#{row['primary_entity_id']} "
          f"value={row['rate_value']}{row['rate_unit']} from={row['effective_from']}")
    print(f"  旧 notes: {row['notes']}")

    # 自检：必须仍是邓晶焱(#5) 的带票提成档
    assert row["relation_type"] == "dispatcher_commission_daiPiao", "relation_type 不符"
    assert row["primary_entity_id"] == 5, "primary 不是 dispatcher#5 邓晶焱"
    assert float(row["rate_value"]) == 5.0, "rate_value 不是 5.0"
    assert row["effective_from"] == "2026-09-26", "生效日不符"

    if row["notes"] == NEW_NOTES:
        print("✓ 已是目标备注，幂等跳过（未写入）")
    else:
        cur.execute("UPDATE rate_links SET notes=? WHERE id=?", (NEW_NOTES, RID))
        conn.commit()
        print(f"✓ 已更新 notes（{len(row['notes'] or '')} → {len(NEW_NOTES)} 字符）")

    # 复核
    nv = cur.execute("SELECT rate_value, notes FROM rate_links WHERE id=?", (RID,)).fetchone()
    assert float(nv["rate_value"]) == 5.0, "rate_value 被误改！"
    assert nv["notes"] == NEW_NOTES, "notes 未写入"
    print("✓ 复核通过：rate_value 仍为 5.0，notes 已就位")
    print(f"  新 notes: {nv['notes']}")

    # 全库体检
    ic = cur.execute("PRAGMA integrity_check").fetchone()[0]
    cnt = cur.execute("SELECT COUNT(*) FROM rate_links WHERE effective_from>='2026-09-26'").fetchone()[0]
    quota = cur.execute("SELECT quota_amount FROM invoice_sources WHERE id=2").fetchone()[0]
    trips = cur.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
    print(f"体检 integrity_check={ic}  新增费率行={cnt}(应 8)  俊兴票额={quota:,.0f}  trips={trips}")
    conn.close()

if __name__ == "__main__":
    main()
