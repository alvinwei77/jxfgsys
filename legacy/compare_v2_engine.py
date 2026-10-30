"""v2引擎 Phase 1 验收：对现有全部车次，逐笔对比"旧引擎(calc.py)" vs "新引擎(engine.py)"算出的
净利润/留存利润，把差异摆出来生成报告——不做硬性一致性断言(用户明确说了：对不上不一定是
新引擎错，有些旧车次的计算本身可能就不准，后续要跟财务一一核对，所以这里只负责"摆数据"）。

用法：python3 compare_v2_engine.py [db_path] [--csv out.csv]
"""
import sys
import sqlite3

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # 项目根目录(engine.py)
import calc_v1 as calc  # 旧引擎(已下线)，仅作历史对照
import engine


def run(db_path, csv_path=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    trips = conn.execute(
        "SELECT * FROM trips WHERE status != 'voided' ORDER BY id"
    ).fetchall()

    rows = []
    errors = []
    for t in trips:
        old_fin = calc.compute_trip_finance(conn, t)
        try:
            new_fin = engine.compute_trip_finance_v2(conn, t)
        except Exception as e:
            errors.append((t["id"], t["plate_no"], str(e)))
            continue
        if new_fin is None:
            errors.append((t["id"], t["plate_no"], "无 template_id，新引擎跳过"))
            continue

        old_np = round(old_fin.get("net_profit") or 0, 2)
        new_np = round(new_fin.get("net_profit") or 0, 2)
        old_rp = round(old_fin.get("invoice_diff_profit") or 0, 2)
        new_rp = round(new_fin.get("invoice_diff_profit") or 0, 2)
        diff_np = round(new_np - old_np, 2)
        diff_rp = round(new_rp - old_rp, 2)
        rows.append({
            "trip_id": t["id"], "plate_no": t["plate_no"], "mode_code": t["mode_code"],
            "template_code": new_fin["template_code"],
            "settlement_date": t["settlement_date"] or t["order_date"],
            "old_net_profit": old_np, "new_net_profit": new_np, "diff_net_profit": diff_np,
            "old_reserved_profit": old_rp, "new_reserved_profit": new_rp, "diff_reserved_profit": diff_rp,
        })

    n_total = len(rows)
    n_match = sum(1 for r in rows if abs(r["diff_net_profit"]) < 0.01 and abs(r["diff_reserved_profit"]) < 0.01)
    n_diff = n_total - n_match

    print(f"共比对 {n_total} 笔车次（不含作废），{len(errors)} 笔新引擎计算异常/无模板")
    print(f"净利润+留存利润都完全一致: {n_match} 笔")
    print(f"存在差异: {n_diff} 笔\n")

    if errors:
        print("--- 计算异常/跳过 ---")
        for tid, plate, msg in errors:
            print(f"  #{tid}\t{plate}\t{msg}")
        print()

    diffs = [r for r in rows if abs(r["diff_net_profit"]) >= 0.01 or abs(r["diff_reserved_profit"]) >= 0.01]
    if diffs:
        print("--- 差异明细（旧 vs 新，按|净利润差|从大到小）---")
        diffs.sort(key=lambda r: -abs(r["diff_net_profit"]))
        for r in diffs:
            print(
                f"  #{r['trip_id']}\t{r['plate_no']}\t{r['mode_code']}->{r['template_code']}\t{r['settlement_date']}\t"
                f"净利润 旧{r['old_net_profit']:,.2f} 新{r['new_net_profit']:,.2f} 差{r['diff_net_profit']:+,.2f}\t"
                f"留存 旧{r['old_reserved_profit']:,.2f} 新{r['new_reserved_profit']:,.2f} 差{r['diff_reserved_profit']:+,.2f}"
            )

    if csv_path:
        import csv as csv_mod
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            fieldnames = ["trip_id", "plate_no", "mode_code", "template_code", "settlement_date",
                          "old_net_profit", "new_net_profit", "diff_net_profit",
                          "old_reserved_profit", "new_reserved_profit", "diff_reserved_profit"]
            w = csv_mod.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        print(f"\n[已导出] 全部{n_total}笔明细: {csv_path}")

    conn.close()
    return rows, errors


if __name__ == "__main__":
    args = sys.argv[1:]
    csv_out = None
    if "--csv" in args:
        idx = args.index("--csv")
        csv_out = args[idx + 1]
        del args[idx:idx + 2]
    db_path = args[0] if args else "data/scrapmetal.db"
    run(db_path, csv_out)
