"""实体下钻/报表筛选/质量补充（从 core.py 拆出）。"""
import datetime as dt

import calc
from stats import _trips_stats

def _entity_period_stats(conn, col, entity_id, which):
    """which='current'/'prev'：按各钢厂自己的周期取这个实体的车次，合并后走同一套聚合。"""
    spans, all_trips = [], []
    for m in conn.execute("SELECT id FROM steel_mills").fetchall():
        status = calc.get_mill_period_status(conn, m["id"])
        if which == "current":
            ps, pe = status["period_start"], status["period_end"]
        else:
            csd, ced = calc.get_mill_cycle_days(conn, m["id"])
            ref = (dt.date.fromisoformat(status["period_start"]) - dt.timedelta(days=1)).isoformat()
            ps, pe = calc.get_period_bounds(ref, csd, ced)
        spans.append((ps, pe))
        all_trips.extend(conn.execute(
            f"""SELECT * FROM trips WHERE {col}=? AND mill_id=? AND status!='voided'
                AND settlement_date IS NOT NULL
                AND date(settlement_date) BETWEEN date(?) AND date(?)""",
            (entity_id, m["id"], ps, pe)).fetchall())
    if not spans:
        label = "-"
    elif len(set(spans)) == 1:
        label = f"{spans[0][0]} ~ {spans[0][1]}"
    else:
        label = f"各钢厂周期（{min(x[0] for x in spans)} ~ {max(x[1] for x in spans)}）"
    st = _trips_stats(conn, all_trips)
    st["trip_ids"] = [t["id"] for t in all_trips]  # 点"查看车次"直接跳到这些车
    return st, label

def _report_trips(conn, args):
    """报表类接口共用的车次筛选：钢厂 / 车船 / 起止日期(按结算或接单日期)。"""
    mill_id = args.get("mill_id")
    date_from = args.get("date_from")
    date_to = args.get("date_to")
    date_field = args.get("date_field", "settlement_date")
    if date_field not in ("settlement_date", "order_date"):
        date_field = "settlement_date"
    where = ["t.status != 'voided'"]
    params = []
    if mill_id:
        where.append("t.mill_id = ?")
        params.append(mill_id)
    # 车/船筛选：所有维度(按钢厂/按调货人/总计…)都只统计选中的那一类。
    transport_type = args.get("transport_type")
    if transport_type in ("truck", "ship"):
        where.append("t.transport_type = ?")
        params.append(transport_type)
    if date_from:
        where.append(f"date(t.{date_field}) >= date(?)")
        params.append(date_from)
    if date_to:
        where.append(f"date(t.{date_field}) <= date(?)")
        params.append(date_to)
    sql = f"""SELECT t.*, sm.name AS mill_name, pb.name AS base_name, d.name AS dispatcher_name,
                     s.name AS invoice_source_name, tm.name AS mode_name, pb.is_dispatcher_alias AS base_is_alias
              FROM trips t
              LEFT JOIN steel_mills sm ON t.mill_id = sm.id
              LEFT JOIN processing_bases pb ON t.base_id = pb.id
              LEFT JOIN dispatchers d ON t.dispatcher_id = d.id
              LEFT JOIN invoice_sources s ON t.invoice_source_id = s.id
              LEFT JOIN trip_modes tm ON t.mode_code = tm.code
              WHERE {' AND '.join(where)}"""
    return conn.execute(sql, params).fetchall()

def _origin_label(t):
    """供货厂展示名：调货人车队的车(占位基地)显示"某某车队"，跟三条线的货物起点一致。"""
    if t["base_id"] and not t["base_is_alias"]:
        return t["base_name"]
    if t["dispatcher_name"]:
        return f"{t['dispatcher_name']}车队"
    return "未填供货厂"

def _bucket_key(conn, date_str, gran, mill_id, cache):
    """返回 (key, 展示名, 区间起, 区间止)；区间用于点柱子跳到对应车次列表。"""
    d = dt.date.fromisoformat(str(date_str)[:10])
    if gran == "day":
        return d.isoformat(), d.strftime("%m-%d"), d.isoformat(), d.isoformat()
    if gran == "week":
        mon = d - dt.timedelta(days=d.weekday())
        return mon.isoformat(), mon.strftime("%m-%d") + "周", mon.isoformat(), (mon + dt.timedelta(days=6)).isoformat()
    if gran == "month":
        first = d.replace(day=1)
        nxt = (first + dt.timedelta(days=32)).replace(day=1)
        return d.strftime("%Y-%m"), d.strftime("%Y-%m"), first.isoformat(), (nxt - dt.timedelta(days=1)).isoformat()
    if mill_id not in cache:
        cache[mill_id] = calc.get_mill_cycle_days(conn, mill_id)
    ps, pe = calc.get_period_bounds(d.isoformat(), *cache[mill_id])
    return ps, f"{ps[5:]}~{pe[5:]}", ps, pe
