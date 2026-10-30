#!/usr/bin/env python3
"""2026-10-04 口径修正：报价「14个点减5元」的正确落库。

上一版（fix_20261004_period_rules.py）把「14个点减5元」折成了 13.8% 点位——错误。
用户 2026-10-04 出示价格表样张（江阴华西特钢废钢收购类型表，"2026-09-29（14-5）.png"）
确认实际口径：
    不含税价(对外结算/给客户付) = 含税价 × (1 − 14%) − 5 元/吨，尾数截断
    （样表 23 行全部吻合：2690→2308、2580→2213、2520→2162 …）
即：14% 点位不变，「减5元」是按吨的绝对额，不是点位折算。
2500 元/吨时两种口径差 10 元/吨且方向相反，必须修正。

修正内容（全部配置数据，不改引擎代码）：
  A. rate_links #10..#16（account_external_settlement 非林凯7行）：13.8 → 14.0，
     notes 改写说明修正原因。
  B. 新增关系 account_external_settlement_ton_adj（per_ton，元/吨）：
     - (account#1 × dispatcher#2..#8) = 5.0，effective_from 2026-09-26
     - (account#1, NULL)              = 0.0，effective_from 2020-01-01
       —— 林凯精确档查不到 → 落到 primary 档 0（林凯正常14个点，无吨减）；
          2026-09-26 前任何调货人也落 0（旧规则无吨减），已封账期 0 影响。
     get_effective_rate 查不到返回 0.0，故未列组合同样安全。
  C. 模板 1(split_company)/2(split_reverse) 节点重排：
     seq1 give_customer  → 改名 give_customer_pct（rate_deduct_rate_schedule 原配置不动）
     seq2 新增            → give_customer_adj（per_ton_rate_schedule，挂 ton_adj 关系）
     seq2 advance_fee    → seq 改 4
     seq3 新增            → give_customer（subtract_nodes = pct − adj）
     下游 node:give_customer 引用不变；give_customer 仍 = 最终给客户付。

幂等：重复执行检测到目标状态即跳过。写库走 /tmp 副本 → 校验 → 回写（沙箱下
SQLite 直接写 data/ 会报 disk I/O error，见 V2_ARCHITECTURE §34）。
"""
import json
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "scrapmetal.db")
BACKUP_DIR = os.path.join(ROOT, "backups")
EFF = "2026-09-26"
ADJ_REL = "account_external_settlement_ton_adj"
NOTE_SETTLE = (
    "2026-10-04 修正：报价口径=含税×(1−14%)−5元/吨（价格表样张 23 行验证）。"
    "『减5元』为按吨绝对额，落在本户头 account_external_settlement_ton_adj 关系，"
    "本行回归标准点位 14.0%。"
)
NOTE_ADJ = (
    "2026-10-04 用户确认：除林凯外报价统一『14个点减5元』= 含税×0.86 − 5元/吨。"
    "本行即『−5元/吨』吨减档（非林凯调货人）。林凯无吨减（落 account 档 0）。"
    "2026-09-26 前无吨减（旧规则）。样张验证：2690→2308、2580→2213、2520→2162（尾数截断）。"
)


def main():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(BACKUP_DIR, f"scrapmetal_before_quote14m5_{ts}.db")
    shutil.copy2(DB, bak)
    print(f"[backup] {bak}")

    work = "/tmp/scrapmetal_quote14m5_work.db"
    shutil.copy2(DB, work)
    for ext in ("-journal", "-wal", "-shm"):
        try:
            os.remove(work + ext)
        except FileNotFoundError:
            pass

    c = sqlite3.connect(work)
    c.row_factory = sqlite3.Row

    # ---------- A. 7 行 13.8 → 14.0 ----------
    rows = c.execute(
        "SELECT id, rate_value FROM rate_links WHERE relation_type='account_external_settlement'"
        " AND primary_entity_id=1 AND secondary_entity_id IS NOT NULL AND secondary_entity_id>=2"
    ).fetchall()
    if rows and all(abs(r["rate_value"] - 14.0) < 1e-9 for r in rows):
        print("[A] 7 行已是 14.0，跳过")
    else:
        cur = c.execute(
            "UPDATE rate_links SET rate_value=14.0, notes=? WHERE id IN (%s)"
            % ",".join(str(r["id"]) for r in rows), (NOTE_SETTLE,))
        print(f"[A] 修正 {cur.rowcount} 行 → 14.0%（原 13.8%）")

    # ---------- B. 吨减关系 ----------
    n = c.execute("SELECT COUNT(*) FROM rate_links WHERE relation_type=?", (ADJ_REL,)).fetchone()[0]
    if n >= 8:
        print(f"[B] {ADJ_REL} 已有 {n} 行，跳过")
    else:
        for disp in range(2, 9):
            c.execute(
                "INSERT INTO rate_links(relation_type,primary_entity_type,primary_entity_id,"
                "secondary_entity_type,secondary_entity_id,rate_value,rate_unit,effective_from,notes)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (ADJ_REL, "account", 1, "dispatcher", disp, 5.0, "per_ton", EFF, NOTE_ADJ))
        # account 档 0：林凯兜底 + 2026-09-26 前兜底
        c.execute(
            "INSERT INTO rate_links(relation_type,primary_entity_type,primary_entity_id,"
            "secondary_entity_type,secondary_entity_id,rate_value,rate_unit,effective_from,notes)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (ADJ_REL, "account", 1, None, None, 0.0, "per_ton", "2020-01-01",
             "兜底档：林凯正常14个点无吨减；2026-09-26 前无吨减。"))
        print(f"[B] {ADJ_REL} 新增 8 行（7×5.0 + 兜底0）")

    # ---------- C. 模板节点重排 ----------
    for tpl in (1, 2):
        cur = c.execute(
            "SELECT id, seq, calc_rule_type, calc_rule_config FROM template_nodes"
            " WHERE template_id=? AND node_key='give_customer'", (tpl,)).fetchone()
        if cur is None:
            node = c.execute(
                "SELECT seq FROM template_nodes WHERE template_id=? AND node_key='give_customer_pct'",
                (tpl,)).fetchone()
            if node:
                print(f"[C] 模板{tpl} 已重排，跳过")
                continue
            raise SystemExit(f"[C] 模板{tpl} 状态异常：既无 give_customer 也无 give_customer_pct")
        cfg = json.loads(cur["calc_rule_config"])
        if cur["calc_rule_type"] != "rate_deduct_rate_schedule" or "relation_type" not in cfg:
            raise SystemExit(f"[C] 模板{tpl} give_customer 规则类型不符，中止")
        c.execute("UPDATE template_nodes SET node_key='give_customer_pct', label='对外结算(点位14%)'"
                  " WHERE id=?", (cur["id"],))
        c.execute("UPDATE template_nodes SET seq=4 WHERE template_id=? AND node_key='advance_fee'", (tpl,))
        c.execute(
            "INSERT INTO template_nodes(template_id,seq,node_key,label,role_type,calc_rule_type,"
            "calc_rule_config,is_profit_component,is_reserved_component) VALUES(?,?,?,?,?,?,?,?,?)",
            (tpl, 2, "give_customer_adj", "对外结算吨减(−5元/吨)", "mill", "per_ton_rate_schedule",
             json.dumps({"relation_type": ADJ_REL, "primary_type": "account",
                         "primary_ref": "trip:account_id",
                         "secondary_type": "dispatcher", "secondary_ref": "trip:dispatcher_id"},
                        ensure_ascii=False), 0, 0))
        c.execute(
            "INSERT INTO template_nodes(template_id,seq,node_key,label,role_type,calc_rule_type,"
            "calc_rule_config,is_profit_component,is_reserved_component) VALUES(?,?,?,?,?,?,?,?,?)",
            (tpl, 3, "give_customer", "给客户付", "mill", "subtract_nodes",
             json.dumps({"minuend": "node:give_customer_pct", "subtrahend": "node:give_customer_adj"},
                        ensure_ascii=False), 0, 0))
        print(f"[C] 模板{tpl} 重排完成：pct(seq1) − adj(seq2) = give_customer(seq3)，advance_fee→seq4")

    # ---------- 自检 ----------
    print("\n===== 自检 =====")
    ok = True
    v = c.execute("SELECT rate_value FROM rate_links WHERE id=10").fetchone()["rate_value"]
    ok &= v == 14.0
    print(f"  #10 rate_value = {v} (期望14.0)")
    n = c.execute("SELECT COUNT(*) FROM rate_links WHERE relation_type=?", (ADJ_REL,)).fetchone()[0]
    ok &= n == 8
    print(f"  {ADJ_REL} 行数 = {n} (期望8)")
    for tpl in (1, 2):
        seqs = {r["node_key"]: r["seq"] for r in c.execute(
            "SELECT node_key,seq FROM template_nodes WHERE template_id=? AND seq<=6", (tpl,))}
        exp = {"give_customer_pct": 1, "give_customer_adj": 2, "give_customer": 3, "advance_fee": 4}
        good = all(seqs.get(k) == s for k, s in exp.items())
        ok &= good
        print(f"  模板{tpl} 前6节点 seq {seqs} {'✓' if good else '✗ 期望' + str(exp)}")
        g = c.execute("SELECT calc_rule_type,calc_rule_config FROM template_nodes"
                      " WHERE template_id=? AND node_key='give_customer'", (tpl,)).fetchone()
        cfg = json.loads(g["calc_rule_config"])
        good = g["calc_rule_type"] == "subtract_nodes" and cfg["minuend"] == "node:give_customer_pct"
        ok &= good
        print(f"  模板{tpl} give_customer = {g['calc_rule_type']} {cfg} {'✓' if good else '✗'}")
    print("  integrity:", c.execute("PRAGMA integrity_check").fetchone()[0])

    # 引擎抽算：非林凯配票车 应=含税×0.86−吨×5
    sys.path.insert(0, ROOT)
    import engine
    conn = c
    conn.row_factory = sqlite3.Row
    trips = c.execute(
        "SELECT * FROM trips WHERE settlement_date>=? AND template_id IN (1,2)"
        " AND dispatcher_id IS NOT NULL AND dispatcher_id!=1 ORDER BY id", (EFF,)).fetchall()
    print(f"\n  新期非林凯配/反向车 {len(trips)} 台，逐台抽算：")
    for t in trips:
        t = dict(t)
        ctx = {**t, "settlement_date": t.get("settlement_date") or t.get("order_date")}
        fin = engine.compute_trip_finance_v2_flat(conn, ctx, None)
        amt = t["settlement_amount_total"]
        expect = amt * 0.86 - t["settlement_tonnage_total"] * 5
        good = abs(fin["give_customer"] - expect) < 0.01
        ok &= good
        print(f"    #{t['id']} disp{t['dispatcher_id']} 含税{amt:,.2f} ×{t['settlement_tonnage_total']}吨"
              f" → 给客户付 {fin['give_customer']:,.2f} (期望 {expect:,.2f}) {'✓' if good else '✗'}")
    c.commit()
    c.close()
    if not ok:
        print("\n自检未全过，不回写。")
        raise SystemExit(1)

    # ---------- 回写 ----------
    shutil.copy2(work, DB)
    for ext in ("-journal", "-wal", "-shm"):
        try:
            os.remove(DB + ext)
        except FileNotFoundError:
            pass
    chk = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    print("\n[done] 已回写生产库 integrity =", chk.execute("PRAGMA integrity_check").fetchone()[0])
    print("  生产库 13.8 残留 =", chk.execute("SELECT COUNT(*) FROM rate_links WHERE rate_value=13.8").fetchone()[0])
    print("  生产库 ton_adj 行数 =", chk.execute("SELECT COUNT(*) FROM rate_links WHERE relation_type=?", (ADJ_REL,)).fetchone()[0])
    print("  生产库 give_customer 节点 =", chk.execute(
        "SELECT calc_rule_type FROM template_nodes WHERE template_id=1 AND node_key='give_customer'").fetchone()[0])
    chk.close()


if __name__ == "__main__":
    main()
