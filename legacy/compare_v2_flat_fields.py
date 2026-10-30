"""任务#30全面切换引擎 —— 全字段逐笔对比脚本(2026-09-18)。

跟 compare_v2_engine.py 的区别：那个脚本只比对 net_profit + invoice_diff_profit 两个数字
(Phase 1验证用，够证明"两个引擎算出的净利润一样"，但不够证明"两个引擎返回的完整字典可以
互相替换")。这个脚本比对 calc.compute_trip_finance() 和 engine.compute_trip_finance_v2_flat()
返回字典里的每一个字段(calc.FIN_FIELDS全部18个 + calc_type/rate_out/rate_in/give_customer等
展示用字段)，确保 app.py::_compute_finance() 切换到v2引擎后，首页/报表/车次详情"利润分项"
表格/导出台账等所有读取这些字段的地方，数字和字段含义都跟切换前完全一致。

用法：python3 compare_v2_flat_fields.py [db_path]（默认用dev库，只读，不写库）
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(os.path.dirname(BASE_DIR), "data", "scrapmetal.db")
sys.path.insert(0, BASE_DIR)

import sqlite3
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # 项目根目录(engine.py)
import calc_v1 as calc  # 旧引擎(已下线)，仅作历史对照
import engine

# 数值字段用相对/绝对容差比较(浮点运算路径不同可能有极小误差)，其余字段严格相等。
NUMERIC_FIELDS = {
    "tonnage", "settlement_amount", "freight_diff_unit_price", "freight_diff_profit",
    "rate_out", "rate_in", "invoice_diff_profit", "reward_unit_price", "reward_profit",
    "period_cumulative_tonnage", "advance_fee_rate", "advance_fee_cost",
    "dispatcher_commission_rate", "dispatcher_commission_cost", "service_fee_value",
    "service_fee_income", "ticket_cost", "other_cost", "net_profit",
    "give_customer", "advance_fee_raw", "front_invoice_amount", "front_actual_paid",
    "personal_advance", "invoice_spread_profit", "cutting_fee", "gross_service_income",
}
EXACT_FIELDS = ["calc_type", "period_start", "period_end", "service_fee_type"]
ALL_FIELDS = calc.FIN_FIELDS + EXACT_FIELDS + [
    "give_customer", "advance_fee_raw", "front_invoice_amount", "front_actual_paid",
    "personal_advance", "invoice_spread_profit", "cutting_fee", "gross_service_income",
]


def close_enough(a, b, tol=0.01):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return abs((a or 0) - (b or 0)) <= tol
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def main(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    trips = conn.execute("SELECT * FROM trips WHERE status != 'voided' ORDER BY id").fetchall()

    n_ok, n_diff, n_v2_none = 0, 0, 0
    diffs = []
    for t in trips:
        old = calc.compute_trip_finance(conn, t)
        new = engine.compute_trip_finance_v2_flat(conn, t)
        if new is None:
            n_v2_none += 1
            diffs.append((t["id"], "v2引擎返回None(缺template_id/模板)", None, None))
            continue
        row_diffs = []
        for f in ALL_FIELDS:
            ov, nv = old.get(f), new.get(f)
            if f in NUMERIC_FIELDS:
                if not close_enough(ov, nv):
                    row_diffs.append((f, ov, nv))
            else:
                if ov != nv:
                    row_diffs.append((f, ov, nv))
        if row_diffs:
            n_diff += 1
            diffs.append((t["id"], t["mode_code"], row_diffs, None))
        else:
            n_ok += 1

    print(f"共比对 {len(trips)} 笔车次（不含作废）")
    print(f"全字段完全一致: {n_ok} 笔")
    print(f"存在字段差异: {n_diff} 笔")
    print(f"v2引擎返回None(缺template_id): {n_v2_none} 笔")
    if diffs:
        print("\n差异明细(最多显示20笔):")
        for d in diffs[:20]:
            print(" ", d)
    conn.close()
    return n_diff == 0 and n_v2_none == 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    ok = main(target)
    sys.exit(0 if ok else 1)
