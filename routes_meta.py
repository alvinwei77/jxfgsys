"""元数据与规则 hub 用接口：结算期、按期规则概览。"""
import datetime as dt

from calc import get_period_bounds
from core import app, query
from flask import request


def _period_label_cn(ref_date: str) -> str:
    """结算期标签，与前端 periodOfDate 一致（26~次25）。"""
    d = dt.date.fromisoformat(ref_date[:10])
    if d.day >= 26:
        if d.month == 12:
            yy, mm = d.year + 1, 1
        else:
            yy, mm = d.year, d.month + 1
    else:
        yy, mm = d.year, d.month
    return f"{yy}-{mm:02d}期"


@app.route("/api/meta/current_settlement_period")
def api_current_settlement_period():
    """默认结算周期（26~次25），供统计/录入/规则 hub 共用。"""
    today = dt.date.today().isoformat()
    start, end = get_period_bounds(today)
    label = _period_label_cn(today)
    return {
        "as_of_today": today,
        "period_label": label,
        "period_start": start,
        "period_end": end,
        "period_rule": "每月26日 ~ 次月25日（与量补/月度票额窗口一致）",
    }


@app.route("/api/rules/period_overview")
def api_rules_period_overview():
    """某参考日所在期内：票源分期、生效费率条数、链条模板（只读概览）。"""
    as_of = (request.args.get("as_of") or dt.date.today().isoformat())[:10]
    start, end = get_period_bounds(as_of)
    label = _period_label_cn(as_of)

    terms = query(
        """
        SELECT t.*, s.name AS source_name, s.source_type
        FROM invoice_source_terms t
        JOIN invoice_sources s ON s.id = t.invoice_source_id
        WHERE date(t.effective_from) <= date(?)
          AND (t.effective_to IS NULL OR date(t.effective_to) >= date(?))
        ORDER BY s.name, t.effective_from DESC
        """,
        (as_of, as_of),
    )
    terms_out = []
    seen = set()
    for row in terms:
        sid = row["invoice_source_id"]
        if sid in seen:
            continue
        seen.add(sid)
        terms_out.append(
            {
                "source_id": sid,
                "source_name": row["source_name"],
                "source_type": row["source_type"],
                "effective_from": row["effective_from"],
                "effective_to": row["effective_to"],
                "quota_amount": row["quota_amount"],
                "warn_threshold": row["warn_threshold"],
                "rate_point": row["rate_point"],
                "outbound_rate_point": row["outbound_rate_point"],
                "accepting": row["accepting"],
            }
        )

    rate_count = query(
        """
        SELECT COUNT(*) AS c FROM rate_links
        WHERE date(effective_from) <= date(?)
          AND (effective_to IS NULL OR date(effective_to) >= date(?))
        """,
        (as_of, as_of),
    )[0]["c"]

    templates = query(
        """
        SELECT ft.id, ft.code, ft.name, ft.short_name,
               (SELECT COUNT(*) FROM trips t WHERE t.template_id = ft.id AND t.status != 'voided') AS trip_count
        FROM flow_templates ft
        ORDER BY ft.id
        """
    )

    return {
        "as_of": as_of,
        "period_label": label,
        "period_start": start,
        "period_end": end,
        "invoice_source_terms": terms_out,
        "rate_links_active_count": rate_count,
        "flow_templates": [dict(r) for r in templates],
        "hint": "完整编辑仍分别在「费率与关系」「链条规则」；后续可在此页做思维导图式编排。",
    }
