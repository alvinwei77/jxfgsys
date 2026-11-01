"""首页看板、统计报表、分析、预警、导入结算单、导出台账。

从原 app.py 拆出（v2.0.1）。共用的常量和工具函数都在 core.py。"""
from core import *  # noqa: F401,F403


@app.route("/api/dashboard")
def dashboard():
    """首页统计卡片数据源。2026-09-17起：不再用自然月口径（各钢厂量补周期不是都按
    26~次月25号算，自然月会把跨周期车次混在一起，数字对不上台账），接单数统一改用
    系统默认的26~次月25号"期"（跟具体钢厂无关，这几张卡片本来就是全局汇总）；
    已结算车次/吨位的"本期/上期"直接由前端复用 /api/dashboard/mill_periods 里
    每家钢厂按自己周期算完后加总的数字，保证跟下面"各钢厂结算周期·总计"卡片完全一致，
    这里只补"总计"（不分期，历史至今全量）。"""
    conn = get_db()
    today = dt.date.today()
    today_str = today.isoformat()

    today_orders = conn.execute(
        "SELECT COUNT(*) c FROM trips WHERE order_date=? AND status!='voided'", (today_str,)
    ).fetchone()["c"]

    pending_settle = conn.execute(
        "SELECT COUNT(*) c FROM trips WHERE status NOT IN ('settled','invoiced','closed','voided')"
    ).fetchone()["c"]

    # 接单数按默认26~次月25号"期"统计（不针对具体钢厂，按下单日期 order_date 计）
    period_start, period_end = calc.get_period_bounds(today_str)
    prev_ref_date = (dt.date.fromisoformat(period_start) - dt.timedelta(days=1)).isoformat()
    prev_period_start, prev_period_end = calc.get_period_bounds(prev_ref_date)

    def _order_count(p_start, p_end):
        return conn.execute(
            """SELECT COUNT(*) c FROM trips WHERE status!='voided' AND order_date IS NOT NULL
               AND date(order_date) BETWEEN date(?) AND date(?)""",
            (p_start, p_end),
        ).fetchone()["c"]

    current_period_orders = _order_count(period_start, period_end)
    prev_period_orders = _order_count(prev_period_start, prev_period_end)
    total_orders = conn.execute("SELECT COUNT(*) c FROM trips WHERE status!='voided'").fetchone()["c"]

    # 已结算车次/吨位：总计（历史至今全量，不分期；本期/上期由前端复用 mill_periods 加总）
    settled_totals = conn.execute(
        """SELECT COUNT(*) c, COALESCE(SUM(settlement_tonnage_total),0) t
           FROM trips WHERE status!='voided' AND settlement_date IS NOT NULL"""
    ).fetchone()
    total_settled_trips = settled_totals["c"]
    total_settled_tonnage = settled_totals["t"]

    # 利润：总利润(不含留存)/留存利润，都是历史至今全量。
    # 2026-09-19修复：这里原来是直接 SUM(trip_finance.net_profit) 并 INNER JOIN trip_finance，
    # 对"还没固化快照过"的车次(trip_finance里根本没有这一行，比如批量导入车次只设置了
    # status='settled'、从没点开详情页触发过一次实时试算/固化)会被INNER JOIN直接漏掉，
    # 总利润因此被低估——跟 report.html/首页周期卡片(_trips_stats/_period_trip_stats，
    # 逐笔用 _trip_breakdown 现算，未固化的车次也照算不误)口径不一致。改成跟它们一样，
    # 用 _trips_stats() 逐笔现算(已固化的车次_trip_finance_full内部会直接取固化快照值，
    # 不会重新计算)，保证"总利润"不会漏掉任何一笔未固化的车次。
    profit_totals = _trips_stats(conn, conn.execute("SELECT * FROM trips WHERE status!='voided'").fetchall())
    total_net_profit = profit_totals["profit"]
    total_reserved_profit = profit_totals["reserved_profit"]

    warns = conn.execute(
        "SELECT id FROM invoice_sources WHERE warn_threshold IS NOT NULL"
    ).fetchall()

    return jsonify({
        "today": today_str,
        "today_orders": today_orders,
        "period_start": period_start,
        "period_end": period_end,
        "current_period_orders": current_period_orders,
        "prev_period_start": prev_period_start, "prev_period_end": prev_period_end,
        "prev_period_orders": prev_period_orders,
        "total_orders": total_orders,
        "pending_settle": pending_settle,
        "total_settled_trips": total_settled_trips,
        "total_settled_tonnage": total_settled_tonnage,
        "total_net_profit": total_net_profit,
        "total_reserved_profit": total_reserved_profit,
        "invoice_source_count": len(warns),
    })


@app.route("/api/dashboard/mill_periods")
def dashboard_mill_periods():
    """按钢厂分别展示各自的量补结算周期状态（每家钢厂的周期规则可能不同）。
    主要展示仍是当前周期；同时附带上一周期的汇总数字，方便环比对照。"""
    conn = get_db()
    mills = conn.execute("SELECT id, name FROM steel_mills ORDER BY id").fetchall()
    result = []
    for m in mills:
        status = calc.get_mill_period_status(conn, m["id"])
        cur_stats = _period_trip_stats(conn, m["id"], status["period_start"], status["period_end"])

        cycle_start_day, cycle_end_day = calc.get_mill_cycle_days(conn, m["id"])
        prev_ref_date = (dt.date.fromisoformat(status["period_start"]) - dt.timedelta(days=1)).isoformat()
        prev_period_start, prev_period_end = calc.get_period_bounds(prev_ref_date, cycle_start_day, cycle_end_day)
        prev_stats = _period_trip_stats(conn, m["id"], prev_period_start, prev_period_end)
        special_note = SPECIAL_PERIOD_NOTES.get((m["id"], prev_period_start, prev_period_end))

        # 本期/上期两套数字，字段名完全一样、上期统一加 prev_ 前缀。原来是把每个字段手写
        # 两遍(40多行)，聚合桶每加一个字段就要记得在这里补两行，漏了前端就拿不到——
        # 2026-09-19加车/船分开统计时改成直接铺开整个桶，以后加字段这里不用再动。
        # name 是桶里给"按维度汇总"用的展示名，这个接口不需要，去掉免得跟 mill_name 混。
        cur_fields = {k: v for k, v in cur_stats.items() if k != "name"}
        prev_fields = {f"prev_{k}": v for k, v in prev_stats.items() if k != "name"}
        result.append({
            **status,
            "mill_name": m["name"],
            **cur_fields,
            "prev_period_start": prev_period_start,
            "prev_period_end": prev_period_end,
            **prev_fields,
            "prev_special_note": special_note,
        })
    return jsonify(result)


@app.route("/api/dashboard/daily_tonnage")
def dashboard_daily_tonnage():
    """近 N 天每日送货吨位/车次数，用于首页趋势图。"""
    conn = get_db()
    days = int(request.args.get("days", 14))
    end = dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT settlement_date AS d, COALESCE(SUM(settlement_tonnage_total),0) AS tonnage, COUNT(*) AS trip_count
           FROM trips
           WHERE status!='voided' AND settlement_date IS NOT NULL
             AND date(settlement_date) BETWEEN date(?) AND date(?)
           GROUP BY settlement_date""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    by_date = {r["d"]: {"tonnage": r["tonnage"], "trip_count": r["trip_count"]} for r in rows}
    out = []
    for i in range(days):
        d = (start + dt.timedelta(days=i)).isoformat()
        v = by_date.get(d, {"tonnage": 0, "trip_count": 0})
        out.append({"date": d, "tonnage": v["tonnage"], "trip_count": v["trip_count"]})
    return jsonify(out)


@app.route("/api/dashboard/status_breakdown")
def dashboard_status_breakdown():
    """车次状态分布（不含作废）。目前首页已改用"票源额度水位"，此接口保留备用。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM trips WHERE status!='voided' GROUP BY status"
    ).fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/dashboard/quota_levels")
def dashboard_quota_levels():
    return jsonify(_quota_levels(get_db()))


@app.route("/api/dashboard/reserved_profit")
def dashboard_reserved_profit():
    """票差留存利润专项汇总（2026-09-17新增）：只统计"经过我们"结算形式的票源方
    (如益周) 产生的票点差价，这部分利润专门留存，不计入净利润/任何业绩汇总，
    这里单独列出来，方便随时核对留存了多少、来自哪些车次。"""
    conn = get_db()
    rows = conn.execute(
        """SELECT t.id AS trip_id, t.settlement_date, t.settlement_tonnage_total AS tonnage,
                  s.name AS invoice_source_name, s.rate_point, s.outbound_rate_point,
                  f.invoice_diff_profit AS reserved_profit, f.net_profit
           FROM trip_finance f
           JOIN trips t ON t.id = f.trip_id
           LEFT JOIN invoice_sources s ON t.invoice_source_id = s.id
           WHERE COALESCE(f.invoice_diff_profit, 0) != 0 AND t.status != 'voided'
           ORDER BY t.settlement_date ASC, t.id ASC"""
    ).fetchall()
    trips_out = rows_to_list(rows)
    total = sum((r["reserved_profit"] or 0) for r in rows)

    by_source = {}
    for r in rows:
        key = r["invoice_source_name"] or "-"
        b = by_source.setdefault(key, {
            "invoice_source_name": key,
            "rate_point": r["rate_point"],
            "outbound_rate_point": r["outbound_rate_point"],
            "trip_count": 0,
            "tonnage": 0,
            "reserved_profit": 0,
        })
        b["trip_count"] += 1
        b["tonnage"] += r["tonnage"] or 0
        b["reserved_profit"] += r["reserved_profit"] or 0

    return jsonify({
        "total_reserved_profit": total,
        "trip_count": len(trips_out),
        "by_invoice_source": sorted(by_source.values(), key=lambda x: x["reserved_profit"], reverse=True),
        "trips": trips_out,
    })


# ---------------------------------------------------------------------------
# 统计报表 API：按钢厂/供货厂/调货人/自定义时间范围 或 钢厂自身结算周期 查询汇总
# ---------------------------------------------------------------------------
@app.route("/api/reports/mill_periods_list")
def reports_mill_periods_list():
    """列出某钢厂历史上实际出现过车次的结算周期（按该钢厂自己的周期规则分桶），
    供报表页做"按结算周期快速筛选"下拉框使用（不同钢厂周期规则不同，不都是26~次月25）。"""
    conn = get_db()
    mill_id = request.args.get("mill_id")
    if not mill_id:
        return jsonify([])
    cycle_start_day, cycle_end_day = calc.get_mill_cycle_days(conn, mill_id)
    trips = conn.execute(
        "SELECT * FROM trips WHERE mill_id=? AND status!='voided' AND settlement_date IS NOT NULL",
        (mill_id,),
    ).fetchall()
    buckets = {}
    for t in trips:
        ps, pe = calc.get_period_bounds(t["settlement_date"], cycle_start_day, cycle_end_day)
        key = (ps, pe)
        b = buckets.setdefault(key, {
            "period_start": ps, "period_end": pe,
            "trip_count": 0, "tonnage": 0, "settlement_amount": 0, "profit": 0,
        })
        b["trip_count"] += 1
        b["tonnage"] += t["settlement_tonnage_total"] or 0
        b["settlement_amount"] += t["settlement_amount_total"] or 0
        b["profit"] += _trip_profit(conn, t)
    return jsonify(sorted(buckets.values(), key=lambda x: x["period_start"], reverse=True))


@app.route("/api/entity_stats")
def entity_stats():
    kind = request.args.get("kind", "dispatcher")
    entity_id = request.args.get("id")
    if kind not in ENTITY_STATS_KINDS or not entity_id:
        return jsonify({"error": "kind必须是 dispatcher/base/mill/invoice_source 之一，且要带id"}), 400
    col, tbl, kind_cn = ENTITY_STATS_KINDS[kind]
    conn = get_db()
    row = conn.execute(f"SELECT id,name FROM {tbl} WHERE id=?", (entity_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    cur, cur_label = _entity_period_stats(conn, col, entity_id, "current")
    prev, prev_label = _entity_period_stats(conn, col, entity_id, "prev")
    total = _trips_stats(conn, conn.execute(
        f"SELECT * FROM trips WHERE {col}=? AND status!='voided'", (entity_id,)).fetchall())

    # 最近几笔，方便点开就能看到具体是哪些车
    recent = rows_to_list(conn.execute(
        f"""SELECT t.id, t.settlement_date, t.order_date, t.plate_no, t.transport_type,
                   t.mode_code, t.settlement_tonnage_total, t.settlement_amount_total,
                   m.name AS mill_name, ft.calc_family, ft.short_name AS template_short
            FROM trips t LEFT JOIN steel_mills m ON t.mill_id=m.id
            LEFT JOIN flow_templates ft ON t.template_id=ft.id
            WHERE t.{col}=? AND t.status!='voided'
            ORDER BY COALESCE(t.settlement_date, t.order_date) DESC LIMIT 12""",
        (entity_id,)).fetchall())

    return jsonify({
        "kind": kind, "kind_cn": kind_cn, "id": row["id"], "name": row["name"], "col": col,
        "current": cur, "current_label": cur_label,
        "prev": prev, "prev_label": prev_label,
        "total": total, "recent": recent,
    })


@app.route("/api/reports/summary")
def reports_summary():
    """灵活查询：可按钢厂筛选 + 自定义时间范围（或配合 mill_periods_list 选中的某个结算周期），
    返回总计 + 按钢厂/供货厂/调货人/票源方/结算路径 五个维度的汇总明细，每个维度都是完整的
    利润构成拆解(节点化数据模型的service_fee_income/dispatcher_commission_cost/ticket_cost
    等分项)，不只是一个净利润数字——供报表页画图表、展开利润构成明细用。"""
    conn = get_db()
    trips = _report_trips(conn, request.args)

    totals_bk = _empty_stats_bucket()
    by_mill, by_base, by_dispatcher, by_invoice_source = {}, {}, {}, {}
    by_path = {key: _empty_stats_bucket(label) for key, _, label in PATH_BUCKET_DEFS}
    path_key_by_split = {True: "split", False: "service"}
    by_transport = {key: _empty_stats_bucket(label) for key, label in TRANSPORT_BUCKET_DEFS}

    for t in trips:
        b = _trip_breakdown(conn, t)
        _accum_bucket(totals_bk, t, b)
        _accum_bucket(by_transport["ship" if t["transport_type"] == "ship" else "truck"], t, b)
        _accum_bucket(by_mill.setdefault(t["mill_id"], _empty_stats_bucket(t["mill_name"] or "-")), t, b)
        # 调货人车队的车(占位基地)显示成"某某车队"，跟回款追踪/三条线的货物起点叫法一致
        _accum_bucket(by_base.setdefault(t["base_id"], _empty_stats_bucket(_origin_label(t) if (t["base_id"] is None or t["base_is_alias"]) else t["base_name"])), t, b)
        _accum_bucket(by_dispatcher.setdefault(t["dispatcher_id"], _empty_stats_bucket(t["dispatcher_name"] or "-")), t, b)
        _accum_bucket(by_invoice_source.setdefault(t["invoice_source_id"], _empty_stats_bucket(t["invoice_source_name"] or "-")), t, b)
        _accum_bucket(by_path[path_key_by_split[b["is_split_path"]]], t, b)

    totals = _finalize_bucket(totals_bk)

    def sorted_list(bucket):
        # 带上实体id，前端才能从这一行下钻到该调货员/供货厂的详情（/api/entity_stats）
        for ent_id, b in bucket.items():
            b["entity_id"] = ent_id
        return [_finalize_bucket(b) for b in sorted(bucket.values(), key=lambda x: x["settlement_amount"], reverse=True)]

    return jsonify({
        "totals": totals,
        "by_mill": sorted_list(by_mill),
        "by_base": sorted_list(by_base),
        "by_dispatcher": sorted_list(by_dispatcher),
        "by_invoice_source": sorted_list(by_invoice_source),
        "by_path": [_finalize_bucket(by_path[key]) for key, _, _ in PATH_BUCKET_DEFS],
        "by_transport": [_finalize_bucket(by_transport[key]) for key, _ in TRANSPORT_BUCKET_DEFS],
    })


@app.route("/api/analytics/trend")
def analytics_trend():
    conn = get_db()
    gran = request.args.get("granularity", "week")
    if gran not in ("day", "week", "month", "cycle"):
        gran = "week"
    field = request.args.get("date_field", "settlement_date")
    if field not in ("settlement_date", "order_date"):
        field = "settlement_date"
    buckets, cache = {}, {}
    for t in _report_trips(conn, request.args):
        if not t[field]:
            continue
        key, label, b_start, b_end = _bucket_key(conn, t[field], gran, t["mill_id"], cache)
        bk = buckets.setdefault(key, _empty_stats_bucket(label))
        bk["start"], bk["end"] = min(bk.get("start") or b_start, b_start), max(bk.get("end") or b_end, b_end)
        _accum_bucket(bk, t, _trip_breakdown(conn, t))
    out = []
    for key in sorted(buckets):
        b = _finalize_bucket(buckets[key])
        out.append({"key": key, "label": b["name"], "start": b["start"], "end": b["end"], **{k: b[k] for k in (
            "trip_count", "tonnage", "settlement_amount", "profit", "profit_per_ton",
            "truck_trip_count", "truck_tonnage", "truck_profit", "ship_trip_count", "ship_tonnage", "ship_profit",
            "split_tonnage", "split_profit", "service_tonnage", "service_profit")}})
    return jsonify({"granularity": gran, "series": out})


@app.route("/api/analytics/quality")
def analytics_quality():
    """质量：各供货厂扣杂率/结算均价；各货型吨位、均价及按月均价走势。"""
    conn = get_db()
    # 净重：历史导入车次多数没存 net_weight_total，用 结算吨位+扣杂 还原（结算吨位 = 净重 − 扣杂）
    def net_of(t):
        return t["net_weight_total"] or ((t["settlement_tonnage_total"] or 0) + (t["deduction_total"] or 0))
    trips = [t for t in _report_trips(conn, request.args) if net_of(t) > 0]
    by_origin = {}
    for t in trips:
        o = by_origin.setdefault(_origin_label(t), {"name": _origin_label(t), "trip_count": 0, "net": 0.0,
                                                    "deduction": 0.0, "tonnage": 0.0, "amount": 0.0, "trip_ids": []})
        o["trip_count"] += 1
        o["trip_ids"].append(t["id"])
        o["net"] += net_of(t)
        o["deduction"] += t["deduction_total"] or 0
        o["tonnage"] += t["settlement_tonnage_total"] or 0
        o["amount"] += t["settlement_amount_total"] or 0
    for o in by_origin.values():
        o["deduction_rate"] = o["deduction"] / o["net"] * 100 if o["net"] else None
        o["avg_price"] = o["amount"] / o["tonnage"] if o["tonnage"] else None
    ids = [t["id"] for t in trips]
    types, monthly = {}, {}
    if ids:
        rows = conn.execute(
            f"""SELECT d.*, st.category, st.name AS type_name, t.settlement_date
                FROM trip_scrap_details d JOIN trips t ON t.id=d.trip_id
                LEFT JOIN scrap_types st ON st.id=d.scrap_type_id
                WHERE d.trip_id IN ({','.join('?' * len(ids))})""", ids).fetchall()
        for r in rows:
            name = f"{r['category']}·{r['type_name']}" if r["type_name"] else "未分货型(混料)"
            ty = types.setdefault(name, {"name": name, "rows": 0, "tonnage": 0.0, "amount": 0.0, "deduction": 0.0, "net": 0.0,
                                         "trip_ids": []})
            ty["rows"] += 1
            if r["trip_id"] not in ty["trip_ids"]:
                ty["trip_ids"].append(r["trip_id"])
            ty["tonnage"] += r["settlement_tonnage"] or 0
            ty["amount"] += r["amount"] or 0
            ty["deduction"] += r["deduction"] or 0
            ty["net"] += r["net_weight"] or ((r["settlement_tonnage"] or 0) + (r["deduction"] or 0))
            if r["settlement_date"] and r["settlement_tonnage"]:
                m = monthly.setdefault(name, {}).setdefault(r["settlement_date"][:7], [0.0, 0.0])
                m[0] += r["amount"] or 0
                m[1] += r["settlement_tonnage"] or 0
        for ty in types.values():
            ty["avg_price"] = ty["amount"] / ty["tonnage"] if ty["tonnage"] else None
            ty["deduction_rate"] = ty["deduction"] / ty["net"] * 100 if ty["net"] else None
            ty["monthly"] = [{"month": k, "avg_price": v[0] / v[1] if v[1] else None, "tonnage": v[1]}
                             for k, v in sorted(monthly.get(ty["name"], {}).items())]
    return jsonify({
        "by_origin": sorted(by_origin.values(), key=lambda x: -x["tonnage"]),
        "by_type": sorted(types.values(), key=lambda x: -x["tonnage"]),
    })


@app.route("/api/analytics/commission")
def analytics_commission():
    """调货人提成：应付(逐车拆解的调货提成) / 已付(提成批次) / 未付，另给逐车未付明细供登记付款批次。"""
    conn = get_db()
    trips = [t for t in _report_trips(conn, request.args) if t["dispatcher_id"]]
    paid = lanes.load_alloc(conn, [t["id"] for t in trips])
    by_d, rows = {}, []
    for t in trips:
        payable = _trip_breakdown(conn, t)["dispatcher_commission_cost"] or 0
        got = (paid.get(t["id"], {}).get("commission") or {}).get("amount", 0)
        d = by_d.setdefault(t["dispatcher_id"], {"dispatcher_id": t["dispatcher_id"], "name": t["dispatcher_name"],
                                                 "trip_count": 0, "tonnage": 0.0, "payable": 0.0, "paid": 0.0})
        d["trip_count"] += 1
        d["tonnage"] += t["settlement_tonnage_total"] or 0
        d["payable"] += payable
        d["paid"] += got
        if payable - got > 0.01:
            rows.append({"trip_id": t["id"], "plate_no": t["plate_no"], "dispatcher_id": t["dispatcher_id"],
                         "dispatcher_name": t["dispatcher_name"], "settlement_date": t["settlement_date"],
                         "tonnage": t["settlement_tonnage_total"], "payable": round(payable, 2),
                         "paid": round(got, 2), "unpaid": round(payable - got, 2), "status": t["status"]})
    for d in by_d.values():
        d["unpaid"] = d["payable"] - d["paid"]
    rows.sort(key=lambda r: (r["dispatcher_name"] or "", r["settlement_date"] or ""))
    return jsonify({"by_dispatcher": sorted(by_d.values(), key=lambda x: -x["payable"]), "unpaid_trips": rows})


@app.route("/api/analytics/completeness")
def analytics_completeness():
    conn = get_db()
    trips = _report_trips(conn, request.args)
    out = []
    for key, label, fn in COMPLETENESS_RULES:
        hit = [{"trip_id": t["id"], "plate_no": t["plate_no"], "date": t["settlement_date"] or t["order_date"],
                "status": t["status"]} for t in trips if fn(t)]
        out.append({"key": key, "label": label, "count": len(hit), "trips": hit[:200]})
    return jsonify({"total_trips": len(trips), "rules": out})


# ---------------------------------------------------------------------------
# v2：首页"待办与预警"（2026-09-22）。每条都带跳转链接，点进去就是要处理的那几车/那个页面。
# level: danger 需要马上处理 / warn 近期要处理 / info 提醒
# ---------------------------------------------------------------------------
@app.route("/api/alerts")
def api_alerts():
    conn = get_db()
    today = dt.date.today()
    out = []
    # 阈值全部可在「系统 → 参数设置」改（C档 2026-10-09），读不到就回退原来的写死值。
    import settings as _settings
    quota_warn_pct = float(_settings.get(conn, "quota_warn_pct", 90.0))
    stale_days = int(_settings.get(conn, "report_stale_days", 3))
    inv_days = int(_settings.get(conn, "invoice_overdue_days", 7))
    pay_days = int(_settings.get(conn, "payment_overdue_days", 30))

    def add(level, title, detail, href, count=None):
        out.append({"level": level, "title": title, "detail": detail, "href": href, "count": count})

    # 1. 票源额度
    for q in _quota_levels(conn):
        if not q["quota_amount"]:
            continue
        pct = (q["used_amount"] or 0) / q["quota_amount"] * 100
        remain = q["quota_amount"] - (q["used_amount"] or 0)
        href = f"/trips?invoice_source_id={q['id']}&exclude_voided=1&date_field=settlement_date&date_from={q['period_start']}&from=" + \
               f"首页预警·{q['name']}额度占用"
        if pct >= 100:
            add("danger", f"{q['name']} 额度已用完", f"已用 {pct:.1f}%，超出 ¥{-remain:,.0f}", href)
        elif pct >= quota_warn_pct or (q["warn_threshold"] and (q["used_amount"] or 0) >= q["warn_threshold"]):
            add("warn", f"{q['name']} 额度快用完", f"已用 {pct:.1f}%，剩 ¥{remain:,.0f}（{q.get('period_label') or ('全年' if q['quota_period'] == 'annual' else '本期')}）", href)
    # 2. 报了很久还没结算的车
    stale = [r["id"] for r in conn.execute(
        """SELECT id FROM trips WHERE status IN ('reported','arrived') AND order_date IS NOT NULL
           AND date(order_date) <= date('now','localtime', ?)""", (f"-{stale_days} days",))]
    if stale:
        add("warn", f"{len(stale)} 车报备超过{stale_days}天还没结算", "看看是不是结算单出了没录，或者车没到",
            "/trips?ids=" + ",".join(map(str, stale)) + f"&from=首页预警·报备超{stale_days}天未结算", len(stale))
    # 3. 开票/回款待办（按账龄）
    rows = lanes.pending_rows(conn)
    inv_old = [r for r in rows if r["hop_key"] == "out" and (r["age_days"] or 0) > inv_days]
    if inv_old:
        add("warn", f"{len(inv_old)} 车结算超过{inv_days}天还没开销项票", f"合计 ¥{sum(r['remaining'] or 0 for r in inv_old):,.0f}", "/tracking?hop=out", len(inv_old))
    pay_old = [r for r in rows if r["hop_key"] == "p_out" and (r["age_days"] or 0) > pay_days]
    if pay_old:
        add("info", f"{len(pay_old)} 车欧冶回款超过{pay_days}天未登记",
            f"合计 ¥{sum(r['remaining'] or 0 for r in pay_old):,.0f}；历史车次补登批次前都会算在这里", "/tracking?hop=p_out", len(pay_old))
    # 4. 调货人提成未付
    unpaid = conn.execute(
        """SELECT COUNT(*) FROM trips t WHERE t.dispatcher_id IS NOT NULL AND t.status IN ('settled','invoiced','closed')""").fetchone()[0]
    if unpaid:
        comm = analytics_commission().get_json()
        tot = sum(d["unpaid"] for d in comm["by_dispatcher"])
        if tot > 0.01:
            who = "、".join(f"{d['name']} ¥{d['unpaid']:,.0f}" for d in comm["by_dispatcher"] if d["unpaid"] > 0.01)
            add("info", f"调货人提成未付 ¥{tot:,.0f}", who, "/tracking#c-table")
    # 5. 量补档位
    for m in conn.execute("SELECT id, name FROM steel_mills"):
        st = calc.get_mill_period_status(conn, m["id"])
        gap = st.get("gap_to_next_tier")
        if gap is not None and st.get("next_tier_min_tonnage") and gap <= st["next_tier_min_tonnage"] * 0.25:
            days_left = (dt.date.fromisoformat(st["period_end"]) - today).days
            add("info", f"{m['name']} 还差 {gap:,.0f} 吨升档",
                f"到 {st['next_tier_min_tonnage']:,.0f} 吨量补单价变 {st['next_tier_unit_price']:g} 元/吨，本期还剩 {days_left} 天", "/")
    # 6. 数据缺项
    comp = analytics_completeness().get_json()
    miss = [r for r in comp["rules"] if r["count"] and r["key"] != "settled_no_docno"]
    if miss:
        add("info", "有车次信息不全", "；".join(f"{r['label']} {r['count']} 车" for r in miss), "/reports#r-completeness")
    # 7. 备份
    last = system_ops.last_auto_backup_date()
    if not last or (today - dt.datetime.strptime(last, "%Y%m%d").date()).days >= 2:
        add("warn", "最近没有自动备份", "重启一次系统会自动备份；也可以在「系统」页手动备份", "/system?tab=backup")
    # 8. 利润核对：车次没改、利润却变了
    import regression
    drift = regression.check(conn)
    if drift:
        add("warn", f"{len(drift)} 车利润和上次核对时不一样",
            f"车次本身没改过，合计变动 ¥{sum(d['diff'] for d in drift):+,.2f}；多半是改了费率/档案/规则，看一眼没问题就点「以当前为准」",
            "/system?tab=verify", len(drift))
    order = {"danger": 0, "warn": 1, "info": 2}
    out.sort(key=lambda x: order[x["level"]])
    return jsonify(out)


@app.route("/api/import/settlement/preview", methods=["POST"])
def import_settlement_preview():
    conn = get_db()
    data = request.get_json(force=True) or {}
    stypes = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM scrap_types")}
    out = []
    for i, g in enumerate(data.get("trips") or []):
        total = round(sum(float(it.get("amount") or 0) for it in g.get("items") or []), 2)
        tons = round(sum(float(it.get("tonnage") or 0) for it in g.get("items") or []), 3)
        cands = _match_trips(conn, (g.get("plate") or "").strip(), g.get("date") or None)
        dup = next((c for c in cands if c["status"] in ("settled", "invoiced", "closed")
                    and abs((c["settlement_amount_total"] or 0) - total) < 0.02), None)
        open_c = [c for c in cands if not (c["status"] in ("settled", "invoiced", "closed") and c["detail_count"])]
        if dup:
            action, target = "skip", dup["id"]
        elif open_c:
            action, target = "update", open_c[0]["id"]
        else:
            action, target = "create", None
        unknown_types = sorted({it.get("scrap_name") for it in g.get("items") or []
                                if it.get("scrap_name") and it["scrap_name"] not in stypes and it["scrap_name"] != "混料"})
        prof = _plate_profile(conn, (g.get("plate") or "").strip()) if action == "create" else None
        disp_id = _lookup_id(conn, "dispatchers", g.get("dispatcher"))
        base_id = _lookup_id(conn, "processing_bases", g.get("base"))
        if g.get("dispatcher") and not disp_id and not base_id:
            # "调货人"那列写的其实是供货厂（比如船的货主尹鹏飞）
            base_id = _lookup_id(conn, "processing_bases", g.get("dispatcher"))
        src_id = _lookup_id(conn, "invoice_sources", g.get("source"))
        party = (g.get("party") or "").strip()
        party_note, party_unresolved = None, False
        if party and not src_id and not (base_id and party == g.get("base")):
            src_id = _lookup_id(conn, "invoice_sources", party)
            party_base = None if src_id else _lookup_id(conn, "processing_bases", party)
            if party_base:
                base_id = base_id or party_base
            if not src_id and not party_base:
                # 写的是简称（如"反向"，档案里是"反向-闫东朋/反向-尹鹏飞"）：按绑定的供货厂/调货人挑
                scands = conn.execute("SELECT id, name, bound_base_id FROM invoice_sources WHERE name LIKE ?", (party + "%",)).fetchall()
                hint = {x for x in (g.get("dispatcher"), g.get("base")) if x}
                pick = [c for c in scands if c["bound_base_id"] and conn.execute(
                    "SELECT name FROM processing_bases WHERE id=?", (c["bound_base_id"],)).fetchone()["name"] in hint]
                if len(pick) == 1 or len(scands) == 1:
                    src_id = (pick or scands)[0]["id"]
                    party_note = f"“{party}”按绑定关系认成票源「{(pick or scands)[0]['name']}」，不对请导入后在车次里改"
                elif scands:
                    src_id = scands[0]["id"]
                    party_note = f"“{party}”有多个票源（{'、'.join(c['name'] for c in scands)}），先按「{scands[0]['name']}」，请核对"
                else:
                    party_unresolved = True
        out.append({"idx": i, "total": total, "tonnage": tons, "candidates": cands, "action": action, "target": target,
                    "duplicate_of": dup["id"] if dup else None, "unknown_types": unknown_types,
                    "profile": prof,
                    "party_unresolved": party_unresolved, "party_note": party_note,
                    "resolved": {"dispatcher_id": disp_id, "invoice_source_id": src_id, "base_id": base_id}})
    return jsonify({"rows": out})


@app.route("/api/import/settlement/apply", methods=["POST"])
def import_settlement_apply():
    """rows: [{action: update|create|skip, trip_id, plate, date, doc_no, transport_type, template_id,
              dispatcher_id, base_id, invoice_source_id,
              items:[{scrap_name, deduction, tonnage, unit_price, amount, cutting_fee}]}]"""
    conn = get_db()
    rows = (request.get_json(force=True) or {}).get("rows") or []
    stypes = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM scrap_types")}
    client = app.test_client()  # 复用现有接口的完整逻辑(明细累加、默认值、链条同步、状态日期)，不另写一套
    done = []
    system_ops.do_backup("manual", "导入结算单之前自动备份")
    for r in rows:
        if r.get("action") == "skip":
            continue
        items = r.get("items") or []
        mixed = 1 if any((it.get("scrap_name") in (None, "", "混料")) for it in items) and len(items) == 1 else 0
        if r.get("action") == "create":
            body = {"transport_type": r.get("transport_type") or "truck", "order_date": r.get("date"), "plate_no": r.get("plate")}
            if r.get("template_id"):
                body["template_id"] = int(r["template_id"])
            else:
                body["mode_code"] = "M1"
            resp = client.post("/api/trips", json=body)
            tid = resp.get_json()["id"]
            patch = {k: r.get(k) for k in ("dispatcher_id", "base_id", "invoice_source_id") if r.get(k)}
            if patch:
                client.put(f"/api/trips/{tid}", json=patch)
        else:
            tid = int(r["trip_id"])
        client.put(f"/api/trips/{tid}/details", json={"details": [
            {"scrap_type_id": stypes.get(it.get("scrap_name")), "deduction": it.get("deduction") or 0,
             "settlement_tonnage": it.get("tonnage"), "unit_price": it.get("unit_price") or 0, "amount": it.get("amount"),
             "cutting_fee": it.get("cutting_fee") or 0}
            for it in items]})
        client.put(f"/api/trips/{tid}", json={"settlement_date": r.get("date"), "mixed_material": mixed,
                                              **({"settlement_doc_no": r["doc_no"]} if r.get("doc_no") else {})})
        st = conn.execute("SELECT status FROM trips WHERE id=?", (tid,)).fetchone()
        if st and st["status"] in ("reported", "arrived"):
            client.post(f"/api/trips/{tid}/status", json={"status": "settled", "status_date": r.get("date")})
        done.append(tid)
    return jsonify({"ok": True, "trip_ids": done, "count": len(done)})


# ---------------------------------------------------------------------------
# 一键导出台账：车次明细 + 磅重明细 + 统计汇总（三个 sheet 的 Excel 文件）
# ---------------------------------------------------------------------------
@app.route("/api/export/ledger", methods=["GET"])
def export_ledger():
    """一键导出台账（2026-09-21 重做，生成逻辑在 ledger_export.py）：
    车次台账(重要列靠前+利润过程参数+票款，自动筛选) / 统计汇总(全公式) / 磅重明细 / 票款追踪 / 批次记录 / 票源额度。"""
    try:
        import ledger_export
    except ImportError:
        return jsonify({"error": "缺少 openpyxl 依赖，请重新双击\"启动系统.command\"（会自动联网安装），再点一次导出"}), 500

    conn = get_db()
    date_from = (request.args.get("date_from") or "")[:10] or None
    date_to = (request.args.get("date_to") or "")[:10] or None
    date_field = request.args.get("date_field") or "settlement_date"
    if date_field not in ("settlement_date", "order_date"):
        date_field = "settlement_date"
    sql = """SELECT t.*, m.name AS mill_name, a.name AS account_name, b.name AS base_name,
                  b.is_dispatcher_alias AS base_is_alias, d.name AS dispatcher_name, s.name AS invoice_source_name,
                  ft.short_name AS template_short, ft.name AS template_name
           FROM trips t
           LEFT JOIN steel_mills m ON t.mill_id = m.id
           LEFT JOIN accounts a ON t.account_id = a.id
           LEFT JOIN processing_bases b ON t.base_id = b.id
           LEFT JOIN dispatchers d ON t.dispatcher_id = d.id
           LEFT JOIN invoice_sources s ON t.invoice_source_id = s.id
           LEFT JOIN flow_templates ft ON t.template_id = ft.id
           WHERE t.status != 'voided'"""
    args = []
    if date_from:
        sql += f" AND t.{date_field} IS NOT NULL AND date(t.{date_field}) >= date(?)"
        args.append(date_from)
    if date_to:
        sql += f" AND t.{date_field} IS NOT NULL AND date(t.{date_field}) <= date(?)"
        args.append(date_to)
    sql += " ORDER BY coalesce(t.settlement_date, t.order_date, '') ASC, t.id ASC"
    trips = conn.execute(sql, args).fetchall()

    fin_cache, bd_cache = {}, {}

    def fin_of(t):
        # 跟车次详情同一口径：已固化取快照，快照没存的说明性字段(费率/中间量)用实时计算补齐
        if t["id"] not in fin_cache:
            live = _compute_finance(conn, t)
            row = conn.execute("SELECT * FROM trip_finance WHERE trip_id=?", (t["id"],)).fetchone()
            fin_cache[t["id"]] = {**live, **dict(row)} if row and row["is_locked"] else live
        return fin_cache[t["id"]]

    def breakdown_of(t):
        if t["id"] not in bd_cache:
            bd_cache[t["id"]] = _trip_breakdown(conn, t)
        return bd_cache[t["id"]]

    lanes_of = {t["id"]: ln for t, ln in lanes.trips_lanes(conn)}
    alloc = lanes.load_alloc(conn)
    details = {}
    for d in conn.execute(
            """SELECT tsd.*, st.category AS scrap_category, st.name AS scrap_name
               FROM trip_scrap_details tsd LEFT JOIN scrap_types st ON tsd.scrap_type_id = st.id ORDER BY tsd.id"""):
        details.setdefault(d["trip_id"], []).append(d)
    batches = rows_to_list(conn.execute(
        """SELECT b.*, CASE WHEN ba.id IS NULL THEN NULL
                  ELSE coalesce(pb.name,'') || ' · ' || ba.account_name || coalesce(' 尾号' || ba.card_tail, '') END AS account_label
           FROM batches b LEFT JOIN base_accounts ba ON ba.id=b.base_account_id
           LEFT JOIN processing_bases pb ON pb.id=ba.base_id ORDER BY b.batch_date, b.id"""))
    batch_items = rows_to_list(conn.execute(
        """SELECT bt.batch_id, bt.trip_id, bt.amount, t.plate_no FROM batch_trips bt JOIN trips t ON t.id=bt.trip_id
           ORDER BY bt.batch_id, bt.trip_id"""))
    extras = {"details": details, "batches": batches, "batch_items": batch_items, "quota": _quota_levels(conn),
              "hop_label": {k: v[1] for k, v in lanes.HOP_META.items()}}

    wb, checks = ledger_export.build_workbook(conn, trips, fin_of, breakdown_of, lanes_of, _origin_label, alloc, extras)
    bad = [(tid, a, b) for tid, a, b in checks if abs(a - b) > 0.05]
    if bad:  # 过程列加总跟系统净利润对不上：不静默出错，在主表顶部标红提示
        from openpyxl.styles import Font
        ws = wb["车次台账"]
        ws["A1"].value = (ws["A1"].value or "") + f"　⚠ 有 {len(bad)} 车过程列加总与系统净利润不一致(车次ID: "
        ws["A1"].value += ",".join(str(x[0]) for x in bad[:20]) + ")，请以系统为准并反馈"
        ws["A1"].font = Font(name="Arial", bold=True, color="C00000")
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    suffix = dt.date.today().strftime("%Y%m%d")
    if date_from or date_to:
        suffix += f"_{date_from or '起'}_{date_to or '止'}"
    filename = f"废钢供应链台账_{suffix}.xlsx"
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------------------
# 一键导出「经营报告（分期与专题）」Excel（2026-10-07，生成逻辑在 ops_report_export.py）：
# ①总览 / ②各期明细 / 调货人工资 / 量补汇总 / 票源额度水位。缺省=库中全部期。
# ---------------------------------------------------------------------------
@app.route("/api/export/ops_report", methods=["GET"])
def export_ops_report():
    try:
        import ops_report_export
    except ImportError:
        return jsonify({"error": "缺少 openpyxl 依赖，请重新双击\"启动系统.command\"（会自动联网安装），再点一次导出"}), 500
    try:
        wb, periods, _scope, _sel = ops_report_export.build_workbook(request.args.get("periods"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    tag = "_".join(x.replace("期", "") for x in periods)
    filename = f"经营报告_{tag}_{dt.date.today().strftime('%Y%m%d')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
