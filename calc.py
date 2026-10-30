"""计算引擎共用的小工具（v2.0.1 起）。

利润怎么算，现在完全由「链条规则」决定（engine.py 按模板节点逐个计算，费率来自「费率与关系」）。
这里只剩引擎要用的几个通用函数：钢厂结算周期、周期累计吨位、量补阶梯单价、票源方点位/票差、
利润快照落库(save_trip_finance)。

旧的按 M1~M5 硬编码 if/else 的计算函数(compute_trip_finance)已下线，原文件完整保存在
legacy/calc_v1.py，只作历史对照，系统不再调用。
"""
import datetime as dt

# 路径2(带票带货/户头通道)毛服务费单价：毛40 - 垫资23 - 提成15 = 净2元/吨，已用历史数据验证吻合。
SERVICE_FEE_GROSS_RATE = 40.0


def _invoice_cost_rate_pct(invoice_source):
    """返回用于计算"前端实付/个人垫资"的成本系数点位：
    direct(直接对欧冶) 用票源方自己的 rate_point；
    pass_through(经过我们) 用我们对外转出的 outbound_rate_point。
    invoice_source 为 sqlite3.Row 或 None。"""
    if not invoice_source:
        return 0.0
    settlement_type = invoice_source["settlement_type"] if "settlement_type" in invoice_source.keys() else "direct"
    if settlement_type == "pass_through" and invoice_source["outbound_rate_point"] is not None:
        return float(invoice_source["outbound_rate_point"])
    return float(invoice_source["rate_point"] or 0)


def _invoice_spread_profit(invoice_source, front_invoice_amount):
    """票差利润：仅 pass_through(经过我们的进项票) 票源方才有。
    = 前端开票金额 × (转出点位 - 进项点位) / 100。
    这是专门单独留存的利润，不计入 net_profit / 任何汇总统计，只存进
    trip_finance.invoice_diff_profit，由单独的"留存利润"口径汇总展示。"""
    if not invoice_source:
        return 0.0
    settlement_type = invoice_source["settlement_type"] if "settlement_type" in invoice_source.keys() else "direct"
    if settlement_type != "pass_through" or invoice_source["outbound_rate_point"] is None:
        return 0.0
    inbound = float(invoice_source["rate_point"] or 0)
    outbound = float(invoice_source["outbound_rate_point"])
    return front_invoice_amount * (outbound - inbound) / 100.0


def get_period_bounds(date_str, start_day=26, end_day=25):
    """给定日期，返回其所在结算周期 [start, end]（字符串 YYYY-MM-DD）。
    周期规则：每期从当月26日到次月25日。"""
    if not date_str:
        date_str = dt.date.today().isoformat()
    d = dt.date.fromisoformat(str(date_str)[:10])
    if d.day >= start_day:
        period_start = dt.date(d.year, d.month, start_day)
    else:
        # 上个月的 start_day
        prev_month = d.month - 1 or 12
        prev_year = d.year if d.month > 1 else d.year - 1
        period_start = dt.date(prev_year, prev_month, start_day)
    # period_end = 下个月的 end_day
    end_month = period_start.month + 1
    end_year = period_start.year
    if end_month > 12:
        end_month = 1
        end_year += 1
    period_end = dt.date(end_year, end_month, end_day)
    return period_start.isoformat(), period_end.isoformat()


def get_cumulative_tonnage(conn, mill_id, period_start, period_end, exclude_trip_id=None):
    if not mill_id:
        return 0.0
    sql = """
        SELECT COALESCE(SUM(settlement_tonnage_total),0) AS total
        FROM trips
        WHERE mill_id = ?
          AND status != 'voided'
          AND settlement_date IS NOT NULL
          AND date(settlement_date) BETWEEN date(?) AND date(?)
    """
    args = [mill_id, period_start, period_end]
    if exclude_trip_id:
        sql += " AND id != ?"
        args.append(exclude_trip_id)
    row = conn.execute(sql, args).fetchone()
    return row["total"] or 0.0


def get_mill_cycle_days(conn, mill_id):
    """读取该钢厂配置的量补结算周期起止日（取该钢厂任一档规则的配置；未配置则默认26/25）。"""
    if not mill_id:
        return 26, 25
    row = conn.execute(
        "SELECT period_start_day, period_end_day FROM mill_reward_tiers WHERE mill_id=? LIMIT 1",
        (mill_id,),
    ).fetchone()
    if row and row["period_start_day"] and row["period_end_day"]:
        return row["period_start_day"], row["period_end_day"]
    return 26, 25


def get_mill_period_status(conn, mill_id, ref_date=None):
    """给定钢厂，返回其当前量补结算周期状态：周期起止、累计吨位、当前档单价、距下一档还差多少吨。"""
    start_day, end_day = get_mill_cycle_days(conn, mill_id)
    period_start, period_end = get_period_bounds(ref_date, start_day, end_day)
    cumulative = get_cumulative_tonnage(conn, mill_id, period_start, period_end)
    tiers = conn.execute(
        "SELECT min_tonnage, unit_price FROM mill_reward_tiers WHERE mill_id=? ORDER BY min_tonnage ASC",
        (mill_id,),
    ).fetchall()
    current_price = 0.0
    next_tier = None
    for t in tiers:
        if cumulative >= t["min_tonnage"]:
            current_price = t["unit_price"]
        elif next_tier is None:
            next_tier = t
    return {
        "mill_id": mill_id,
        "period_start": period_start,
        "period_end": period_end,
        "cycle_start_day": start_day,
        "cycle_end_day": end_day,
        "cumulative_tonnage": cumulative,
        "current_reward_price": current_price,
        "next_tier_min_tonnage": next_tier["min_tonnage"] if next_tier else None,
        "next_tier_unit_price": next_tier["unit_price"] if next_tier else None,
        "gap_to_next_tier": (next_tier["min_tonnage"] - cumulative) if next_tier else None,
    }


def get_reward_unit_price(conn, mill_id, period_start, period_end, cumulative_tonnage):
    if not mill_id:
        return 0.0
    tiers = conn.execute(
        "SELECT min_tonnage, unit_price FROM mill_reward_tiers WHERE mill_id=? ORDER BY min_tonnage DESC",
        (mill_id,),
    ).fetchall()
    for t in tiers:
        if cumulative_tonnage >= t["min_tonnage"]:
            return t["unit_price"]
    return 0.0


FIN_FIELDS = [
    "freight_diff_unit_price", "freight_diff_profit", "rate_out", "rate_in", "invoice_diff_profit",
    "reward_unit_price", "reward_profit", "period_cumulative_tonnage", "advance_fee_rate", "advance_fee_cost",
    "dispatcher_commission_rate", "dispatcher_commission_cost", "service_fee_type", "service_fee_value",
    "service_fee_income", "ticket_cost", "other_cost", "net_profit",
]


def save_trip_finance(conn, trip_id, fin, lock=False):
    existing = conn.execute("SELECT trip_id, is_locked FROM trip_finance WHERE trip_id=?", (trip_id,)).fetchone()
    if existing and existing["is_locked"] and not lock:
        # 已固化，不允许被普通保存覆盖
        return False
    values = [fin.get(f) for f in FIN_FIELDS]
    if existing:
        set_clause = ", ".join(f"{f}=?" for f in FIN_FIELDS)
        sql = f"UPDATE trip_finance SET {set_clause}"
        if lock:
            sql += ", is_locked=1, locked_at=datetime('now','localtime')"
        sql += " WHERE trip_id=?"
        conn.execute(sql, values + [trip_id])
    else:
        cols = ["trip_id"] + FIN_FIELDS
        placeholders = ",".join(["?"] * len(cols))
        insert_cols = cols[:]
        insert_vals = [trip_id] + values
        if lock:
            insert_cols += ["is_locked", "locked_at"]
            sql = (
                f"INSERT INTO trip_finance ({','.join(insert_cols)}) "
                f"VALUES ({placeholders}, 1, datetime('now','localtime'))"
            )
        else:
            sql = f"INSERT INTO trip_finance ({','.join(insert_cols)}) VALUES ({placeholders})"
        conn.execute(sql, insert_vals)
    conn.commit()
    return True
