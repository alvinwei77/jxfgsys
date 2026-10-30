"""车次利润分项计算逻辑。
对应文档《业务规则确认与计费模型 v0.2》第二节。
所有单价/费率都从档案表读取默认值，但允许调用方以 overrides 传入手工覆盖值
（对应页面上"利润分项"面板里可编辑的字段）。

2026-09 利润规则重算（与用户逐笔核对确认，作废此前的货差+票差+量补-垫资 / 固定服务费2元 模型）：
  路径1/3 差价模型 —— M1(自营带票/配票)、M5(反向自然人开票)：
    给客户付 = 结算金额A × (1 - 对外统一结算点位)
    垫资费   = 吨位 × 垫资费单价（M5反向不扣）
    前端(开票金额) = A - 垫资费
    前端实付 = 前端 × 成本系数(1-进项成本%，按票源方"对外结算点位"，见下)
    个人垫资 = 给客户付 - 前端实付
    调货提成 = 按 DISPATCHER_COMMISSION_RULES 配置（目前仅林凯：配票9/5前5元/吨，9/5起0）
    主营净利润 = 量补 - 个人垫资 - 调货提成 - 切割费
      —— "主营净利润"就是计入所有汇总统计(调货员业绩、票源方利润分布、总计等)的那个数字，
      只反映"A公司按对外口径开票给欧冶"这条清白账，不含下面的票差留存。

  票差利润（2026-09-17新增，单独留存，不计入主营净利润/任何汇总）：
    仅"经过我们"的票源方(settlement_type=pass_through)才有 = 前端 × (转出点位 - 进项点位) / 100
    —— 这是票源方按较低的进项点位开票给我们、我们再按较高的转出点位(与主营净利润用的同一个
    成本系数)开给欧冶，中间实际少付的差价。这部分是专门留存的利润，与主营账目完全切开：
    不加进 net_profit，只存在 trip_finance.invoice_diff_profit 字段里，由专门的"留存利润"
    统计口径单独汇总展示(app.py 的 reserved_profit_summary)。

  票源方分两种结算形式(invoice_sources.settlement_type)：
    - direct（直接对欧冶）：票源方自己直接开票给欧冶垫资国企，我方不经手这张票。
      成本系数直接用该票源方自己的 rate_point。当前：俊兴13.5%、煌烽13.2%、反向13.8%。
    - pass_through（经过我们的进项票）：票源方先按 rate_point(进项点位) 开票给我们，
      我们再按 outbound_rate_point(转出点位) 转开给欧冶。主营净利润的成本系数仍用
      outbound_rate_point(转出点位，即"对外"的口径，与direct票源方一致的口径)，
      票差利润按上面的公式单独计算、单独留存，不影响主营净利润。
      当前仅益周：进项12.8%、转出13.5%。
  路径2 服务费模型 —— M2(外部直签带票)、M3(外部不带票/户头通道)、M4(调货人对接)：
    统一按"带票带货"结算，不关心票源成本：
    毛服务费 = 吨位 × 毛服务费单价(40)
    垫资成本 = 吨位 × 垫资费单价
    调货提成 = 按 DISPATCHER_COMMISSION_RULES 配置（目前仅林凯：带票带货15元/吨，不分日期）
    净服务费 = 毛服务费 - 垫资成本 - 调货提成
    最终利润 = 净服务费 + 量补 - 切割费
  量补(reward_profit)沿用既有阶梯规则，不受本次改动影响。
"""
import datetime as dt

# 路径2(带票带货/户头通道)毛服务费单价：毛40 - 垫资23 - 提成15 = 净2元/吨，已用历史数据验证吻合。
SERVICE_FEE_GROSS_RATE = 40.0

# 调货员提成规则：目前仅林凯有约定，其余调货员默认0。
#   peiPiao_rate/peiPiao_cutoff：路径1/3(配票/反向)提成单价，及其失效日期(该日期当天起改为0)
#   daiPiao_rate：路径2(带票带货)提成单价，不分日期
DISPATCHER_COMMISSION_RULES = {
    "林凯": {"peiPiao_rate": 5.0, "peiPiao_cutoff": "2026-09-05", "daiPiao_rate": 15.0},
}


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


def compute_dispatcher_commission(dispatcher_name, path, settlement_date, tonnage):
    """path: 'peiPiao'(路径1/3配票/反向) 或 'daiPiao'(路径2带票带货)。
    返回该车次的调货提成金额(元)。"""
    if not dispatcher_name:
        return 0.0
    rule = DISPATCHER_COMMISSION_RULES.get(dispatcher_name)
    if not rule:
        return 0.0
    if path == "daiPiao":
        rate = rule.get("daiPiao_rate", 0.0)
        return rate * tonnage
    # peiPiao
    rate = rule.get("peiPiao_rate", 0.0)
    cutoff = rule.get("peiPiao_cutoff")
    if cutoff and settlement_date and str(settlement_date)[:10] >= cutoff:
        return 0.0
    return rate * tonnage


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


def compute_trip_finance(conn, trip, overrides=None):
    """trip: sqlite3.Row 或 dict，需包含 trips 表字段。
    overrides: dict，可覆盖 ticket_cost / other_cost / reward_unit_price /
               service_fee_type / service_fee_value 等手工项。
    返回：完整利润分项 dict（未落库）。字段含义按 2026-09 新规则重新定义，详见模块顶部说明；
    calc_type='split' 对应路径1/3差价模型(M1/M5)，'service_fee' 对应路径2服务费模型(M2/M3/M4)。"""
    overrides = overrides or {}
    trip = dict(trip)

    tonnage = float(trip.get("settlement_tonnage_total") or 0)
    settlement_amount = float(trip.get("settlement_amount_total") or 0)
    cutting_fee = float(trip.get("cutting_fee_total") or 0)
    settlement_date = trip.get("settlement_date") or trip.get("order_date")

    mode_code = trip.get("mode_code") or "M3"
    mode = conn.execute("SELECT * FROM trip_modes WHERE code=?", (mode_code,)).fetchone()
    calc_type = mode["calc_type"] if mode else "service_fee"

    account = None
    if trip.get("account_id"):
        account = conn.execute("SELECT * FROM accounts WHERE id=?", (trip["account_id"],)).fetchone()

    invoice_source = None
    if trip.get("invoice_source_id"):
        invoice_source = conn.execute(
            "SELECT * FROM invoice_sources WHERE id=?", (trip["invoice_source_id"],)
        ).fetchone()

    dispatcher = None
    if trip.get("dispatcher_id"):
        dispatcher = conn.execute("SELECT * FROM dispatchers WHERE id=?", (trip["dispatcher_id"],)).fetchone()
    dispatcher_name = dispatcher["name"] if dispatcher else None

    base = None
    if trip.get("base_id"):
        base = conn.execute("SELECT * FROM processing_bases WHERE id=?", (trip["base_id"],)).fetchone()

    existing_fin = conn.execute("SELECT * FROM trip_finance WHERE trip_id=?", (trip["id"],)).fetchone()
    existing_fin = dict(existing_fin) if existing_fin else {}

    def pick(key, default=0):
        if key in overrides and overrides[key] not in (None, ""):
            return float(overrides[key])
        if existing_fin.get(key) not in (None, ""):
            return float(existing_fin[key])
        return default

    ticket_cost = pick("ticket_cost", 0)
    manual_other_cost = overrides.get("other_cost", trip.get("other_cost") or 0)
    manual_other_cost = float(manual_other_cost or 0)

    external_settlement_rate = float(account["external_settlement_rate"]) if account and account["external_settlement_rate"] is not None else 14.0
    advance_fee_unit = float(account["advance_fee_rate"]) if account and account["advance_fee_rate"] is not None else 23.0
    advance_fee_unit = float(overrides.get("advance_fee_rate", advance_fee_unit) or 0)
    cost_rate_pct = _invoice_cost_rate_pct(invoice_source)
    cost_rate_pct = float(overrides.get("invoice_cost_rate_pct", cost_rate_pct) or 0)

    mill_id = trip.get("mill_id")
    cycle_start_day, cycle_end_day = get_mill_cycle_days(conn, mill_id)
    period_start, period_end = get_period_bounds(settlement_date, cycle_start_day, cycle_end_day)
    cumulative = get_cumulative_tonnage(conn, mill_id, period_start, period_end)
    reward_unit_price = get_reward_unit_price(conn, mill_id, period_start, period_end, cumulative)
    reward_unit_price = float(overrides.get("reward_unit_price", reward_unit_price) or 0)
    reward_profit = reward_unit_price * tonnage

    service_fee_type = overrides.get("service_fee_type") or existing_fin.get("service_fee_type") or (
        base["service_fee_type"] if base else "per_ton"
    )
    service_fee_value = overrides.get("service_fee_value", existing_fin.get("service_fee_value"))

    if calc_type == "split":
        # 路径1(M1配票) / 路径3(M5反向)：给客户付-前端实付 差价模型
        give_customer = settlement_amount * (1 - external_settlement_rate / 100.0)
        advance_fee_cost = 0.0 if mode_code == "M5" else advance_fee_unit * tonnage
        front_invoice_amount = settlement_amount - advance_fee_cost
        cost_factor = 1 - cost_rate_pct / 100.0
        front_actual_paid = front_invoice_amount * cost_factor
        personal_advance = give_customer - front_actual_paid
        dispatcher_commission_cost = compute_dispatcher_commission(
            dispatcher_name, "peiPiao", settlement_date, tonnage
        )
        if overrides.get("dispatcher_commission_rate") not in (None, ""):
            dispatcher_commission_cost = float(overrides["dispatcher_commission_rate"]) * tonnage
        elif overrides.get("dispatcher_commission_cost") not in (None, ""):
            dispatcher_commission_cost = float(overrides["dispatcher_commission_cost"])

        invoice_spread_profit = _invoice_spread_profit(invoice_source, front_invoice_amount)

        # 字段映射（与展示层"量补-垫资成本+其他净调整=主营净利润"的分解对应）：
        #   垫资成本(advance_fee_cost) = 个人垫资（给客户付-前端实付，已内含吨位×23垫资费的影响）
        #   其他净调整 = -(调货提成) - (切割费+手工其他成本)
        #   invoice_diff_profit(票差利润) 仅作单独留存记录，不参与 net_profit 计算，见下。
        freight_diff_profit = 0.0
        invoice_diff_profit = invoice_spread_profit
        rate_out = external_settlement_rate
        rate_in = cost_rate_pct
        advance_fee_cost = personal_advance
        advance_fee_rate = personal_advance / tonnage if tonnage else 0.0
        service_fee_value = float(service_fee_value or 0)
        service_fee_income = 0.0
        other_cost = cutting_fee + manual_other_cost

        # 2026-09-17起：票差利润(invoice_spread_profit)单独留存，不再累加进主营净利润。
        net_profit = reward_profit - personal_advance - dispatcher_commission_cost - other_cost
        extra_detail = {
            "give_customer": give_customer,
            "advance_fee_raw": 0.0 if mode_code == "M5" else advance_fee_unit * tonnage,
            "front_invoice_amount": front_invoice_amount,
            "cost_factor": cost_factor,
            "front_actual_paid": front_actual_paid,
            "personal_advance": personal_advance,
            "invoice_spread_profit": invoice_spread_profit,
            "cutting_fee": cutting_fee,
        }
    else:
        # 路径2(M2/M3/M4)：带票带货服务费模型，不关心票源成本
        dispatcher_commission_cost = compute_dispatcher_commission(
            dispatcher_name, "daiPiao", settlement_date, tonnage
        )
        if overrides.get("dispatcher_commission_rate") not in (None, ""):
            dispatcher_commission_cost = float(overrides["dispatcher_commission_rate"]) * tonnage
        elif overrides.get("dispatcher_commission_cost") not in (None, ""):
            dispatcher_commission_cost = float(overrides["dispatcher_commission_cost"])
        gross_service_income = SERVICE_FEE_GROSS_RATE * tonnage
        advance_fee_cost = advance_fee_unit * tonnage
        other_cost = cutting_fee + manual_other_cost

        freight_diff_profit = 0.0
        invoice_diff_profit = 0.0
        rate_out = None
        rate_in = None
        advance_fee_rate = advance_fee_unit
        service_fee_value = float(service_fee_value if service_fee_value not in (None, "") else SERVICE_FEE_GROSS_RATE)
        service_fee_income = gross_service_income

        net_profit = (
            service_fee_income
            + reward_profit
            - advance_fee_cost
            - dispatcher_commission_cost
            - other_cost
        )
        extra_detail = {
            "gross_service_income": gross_service_income,
            "cutting_fee": cutting_fee,
        }

    return {
        **extra_detail,
        "trip_id": trip["id"],
        "calc_type": calc_type,
        "tonnage": tonnage,
        "settlement_amount": settlement_amount,
        "freight_diff_unit_price": 0.0,
        "freight_diff_profit": freight_diff_profit,
        "rate_out": rate_out,
        "rate_in": rate_in,
        "invoice_diff_profit": invoice_diff_profit,
        "reward_unit_price": reward_unit_price,
        "reward_profit": reward_profit,
        "period_cumulative_tonnage": cumulative,
        "period_start": period_start,
        "period_end": period_end,
        "advance_fee_rate": advance_fee_rate,
        "advance_fee_cost": advance_fee_cost,
        "dispatcher_commission_rate": (dispatcher_commission_cost / tonnage) if tonnage else 0.0,
        "dispatcher_commission_cost": dispatcher_commission_cost,
        "service_fee_type": service_fee_type,
        "service_fee_value": service_fee_value,
        "service_fee_income": service_fee_income,
        "ticket_cost": ticket_cost,
        "other_cost": other_cost,
        "net_profit": net_profit,
    }


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
