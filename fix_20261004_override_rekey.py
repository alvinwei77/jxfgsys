#!/usr/bin/env python3
"""2026-10-04 配套修正：节点改名 give_customer → give_customer_pct 后，
车次手工点位覆盖键 `rate:give_customer` 需同步改为 `rate:give_customer_pct`
（引擎按 overrides[f"rate:{node_key}"] 匹配，engine.py L393）。
涉及 5 台封账车：#16/#19/#95/#96/#104（覆盖值 13.8~14.02 不等，均为历史议价点位）。
幂等：键已改则跳过。写库走 /tmp 副本 → commit → 校验 → 回写。"""
import json
import os
import shutil
import sqlite3
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "scrapmetal.db")
OLD, NEW = "rate:give_customer", "rate:give_customer_pct"


def main():
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(ROOT, "backups", f"scrapmetal_before_ovr_rekey_{ts}.db")
    shutil.copy2(DB, bak)
    print(f"[backup] {bak}")

    work = "/tmp/scrapmetal_ovr_rekey.db"
    shutil.copy2(DB, work)
    c = sqlite3.connect(work)
    c.row_factory = sqlite3.Row

    n = 0
    for r in c.execute("SELECT id,finance_overrides FROM trips WHERE finance_overrides LIKE '%'||?||'%' AND template_id IN (1,2)", (OLD,)).fetchall():
        o = json.loads(r["finance_overrides"])
        if OLD in o:
            o[NEW] = o.pop(OLD)
            c.execute("UPDATE trips SET finance_overrides=? WHERE id=?",
                      (json.dumps(o, ensure_ascii=False), r["id"]))
            n += 1
            print(f"  #{r['id']}: {OLD} → {NEW}")
    print(f"[rekey] 改写 {n} 台")
    c.commit()

    # 自检：5 台车重算应与各自覆盖点位吻合
    import sys
    sys.path.insert(0, ROOT)
    import engine
    ok = True
    for r in c.execute("SELECT id,finance_overrides,settlement_amount_total,settlement_tonnage_total FROM trips WHERE finance_overrides LIKE '%'||NEW||'%'".replace("NEW", repr(NEW))).fetchall():
        t = dict(c.execute("SELECT * FROM trips WHERE id=?", (r["id"],)).fetchone())
        ctx = {**t, "settlement_date": t.get("settlement_date") or t.get("order_date")}
        f = engine.compute_trip_finance_v2_flat(c, ctx, json.loads(r["finance_overrides"]))
        rate = json.loads(r["finance_overrides"])[NEW]
        expect = r["settlement_amount_total"] * (1 - rate / 100.0)
        good = abs(f["give_customer"] - expect) < 0.01
        ok &= good
        print(f"  #{r['id']} rate={rate} → 给客户付 {f['give_customer']:,.2f} (期望 {expect:,.2f}) {'✓' if good else '✗'}")
    print("  integrity:", c.execute("PRAGMA integrity_check").fetchone()[0])
    c.close()
    if not ok or n == 0:
        print("自检未过或无改动，不回写")
        raise SystemExit(1 if not ok else 0)

    shutil.copy2(work, DB)
    for ext in ("-journal", "-wal", "-shm"):
        try:
            os.remove(DB + ext)
        except FileNotFoundError:
            pass
    chk = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    print("[done] 回写完成 integrity =", chk.execute("PRAGMA integrity_check").fetchone()[0])
    chk.close()


if __name__ == "__main__":
    main()
