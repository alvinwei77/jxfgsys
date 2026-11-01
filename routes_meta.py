"""元数据与规则 hub 用接口：结算期、按期规则概览。"""
import datetime as dt

from calc import get_period_bounds
from core import app, get_db, query
from flask import jsonify, request
from system_ops import audit
import engine


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
    """默认结算周期（26~次25），供统计/录入/规则 hub 共用。

    若「日历今天所在期」内还没有任何结算车次（新期头几天常见），
    suggested_filter_* 回退到上一结算期，避免统计/日历整页空白。
    """
    today = dt.date.today().isoformat()
    start, end = get_period_bounds(today)
    label = _period_label_cn(today)
    conn = get_db()
    n = conn.execute(
        """SELECT COUNT(*) FROM trips WHERE status!='voided' AND settlement_date IS NOT NULL
           AND date(settlement_date) BETWEEN date(?) AND date(?)""",
        (start, end),
    ).fetchone()[0]
    sug_start, sug_end, sug_label = start, end, label
    fallback = False
    if n == 0:
        prev_ref = (dt.date.fromisoformat(start) - dt.timedelta(days=1)).isoformat()
        sug_start, sug_end = get_period_bounds(prev_ref)
        sug_label = _period_label_cn(sug_start)
        fallback = True
    return {
        "as_of_today": today,
        "period_label": label,
        "period_start": start,
        "period_end": end,
        "period_rule": "每月26日 ~ 次月25日（与量补/月度票额窗口一致）",
        "trip_count_in_current_period": n,
        "suggested_filter_start": sug_start,
        "suggested_filter_end": sug_end,
        "suggested_filter_label": sug_label,
        "filter_fallback_previous": fallback,
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


def _term_row_at(source_id: int, ref_date: str):
    rows = query(
        """
        SELECT * FROM invoice_source_terms
        WHERE invoice_source_id = ?
          AND date(effective_from) <= date(?)
          AND (effective_to IS NULL OR date(effective_to) >= date(?))
        ORDER BY effective_from DESC
        LIMIT 1
        """,
        (source_id, ref_date, ref_date),
    )
    return rows[0] if rows else None


def _term_row_for_period_start(source_id: int, period_start: str):
    rows = query(
        """
        SELECT * FROM invoice_source_terms
        WHERE invoice_source_id = ? AND date(effective_from) = date(?)
        LIMIT 1
        """,
        (source_id, period_start),
    )
    return rows[0] if rows else None


def _wizard_entity_name(entity_type, entity_id):
    if entity_id is None:
        return None
    tables = {
        "account": ("accounts", "name"),
        "dispatcher": ("dispatchers", "name"),
        "invoice_source": ("invoice_sources", "name"),
        "processing_base": ("processing_bases", "name"),
        "steel_mill": ("steel_mills", "name"),
    }
    spec = tables.get(entity_type)
    if not spec:
        return f"#{entity_id}"
    table, col = spec
    rows = query(f"SELECT {col} AS n FROM {table} WHERE id=?", (entity_id,))
    return rows[0]["n"] if rows else f"#{entity_id}(已删)"


def _attach_wizard_existing_link_ids(items, period_start: str):
    for it in items:
        ex = query(
            """
            SELECT id FROM rate_links
            WHERE relation_type=? AND primary_entity_type=?
              AND ((? IS NULL AND primary_entity_id IS NULL) OR primary_entity_id=?)
              AND ((secondary_entity_id IS NULL AND ? IS NULL) OR secondary_entity_id=?)
              AND date(effective_from)=date(?)
            LIMIT 1
            """,
            (
                it["relation_type"],
                it["primary_entity_type"],
                it["primary_entity_id"],
                it["primary_entity_id"],
                it["secondary_entity_id"],
                it["secondary_entity_id"],
                period_start,
            ),
        )
        it["existing_link_id"] = ex[0]["id"] if ex else None


def _build_rate_wizard_from_effective(conn, ref_day: str, period_start: str, relation_type: str):
    """列出参考日生效的费率组合（改价向导用，不含户头垫资的「全量×票源」展开）。"""
    catalog = {c["code"]: c for c in engine.RELATION_TYPE_CATALOG}
    cat = catalog.get(relation_type) or {}
    ptype = cat.get("primary_type")
    stype = cat.get("secondary_type")
    rows = query(
        """
        SELECT DISTINCT primary_entity_type, primary_entity_id,
               secondary_entity_type, secondary_entity_id
        FROM rate_links
        WHERE relation_type=?
          AND date(effective_from) <= date(?)
          AND (effective_to IS NULL OR date(effective_to) >= date(?))
        ORDER BY primary_entity_id, secondary_entity_id
        """,
        (relation_type, ref_day, ref_day),
    )
    items = []
    for row in rows:
        pid = row["primary_entity_id"]
        sid = row["secondary_entity_id"]
        kwargs = {}
        if stype:
            kwargs["secondary_entity_id"] = sid if sid is not None else "__unset__"
        rate_val = engine.get_effective_rate(conn, relation_type, pid, ref_day, **kwargs)
        if pid is None:
            label = f"{cat.get('label', relation_type)} · 全局兜底"
        elif sid is not None and stype:
            label = (
                f"{_wizard_entity_name(ptype, pid)} × {_wizard_entity_name(stype, sid)}"
                f" · {cat.get('label', relation_type)}"
            )
        else:
            label = f"{_wizard_entity_name(ptype, pid)} · {cat.get('label', relation_type)}"
        items.append(
            {
                "key": f"{relation_type}|{pid}|{sid}",
                "label": label,
                "relation_type": relation_type,
                "primary_entity_type": ptype,
                "primary_entity_id": pid,
                "secondary_entity_type": row["secondary_entity_type"] if sid is not None else None,
                "secondary_entity_id": sid,
                "rate_unit": cat.get("rate_unit") or "per_ton",
                "current_rate": rate_val,
                "proposed_rate": rate_val,
                "apply_default": False,
            }
        )
    _attach_wizard_existing_link_ids(items, period_start)
    for it in items:
        if it["existing_link_id"]:
            it["apply_default"] = True
    return items


WIZARD_EXTRA_RATE_RELATIONS = [
    ("commission_dai", "③ 带票提成 M2/M3/M4（元/吨）", "dispatcher_commission_daiPiao"),
    ("commission_pei", "④ 配票提成 M1/M5（元/吨）", "dispatcher_commission_peiPiao"),
    ("settlement_pct", "⑤ 户头对外结算点位（%）", "account_external_settlement"),
    ("settlement_ton_adj", "⑥ 户头对外吨减（元/吨）", "account_external_settlement_ton_adj"),
    ("service_fee", "⑦ 服务费单价（元/吨）", "service_fee_gross"),
]


def _build_all_wizard_rate_sections(conn, ref_day: str, period_start: str):
    sections = [
        {
            "key": "advance",
            "title": "② 户头垫资费（元/吨，自本期起 upsert）",
            "items": _build_rate_wizard_items(conn, ref_day, period_start),
        }
    ]
    for key, title, rel in WIZARD_EXTRA_RATE_RELATIONS:
        sections.append(
            {"key": key, "title": title, "items": _build_rate_wizard_from_effective(conn, ref_day, period_start, rel)}
        )
    return sections


def _build_rate_wizard_items(conn, ref_day: str, period_start: str):
    """户头垫资费：通用档 + 有专项或数值与通用不同的票源档。"""
    catalog = {c["code"]: c for c in engine.RELATION_TYPE_CATALOG}
    rel = "account_advance_fee"
    cat = catalog.get(rel, {})
    accounts = query("SELECT id, name FROM accounts ORDER BY id")
    sources = query("SELECT id, name FROM invoice_sources ORDER BY name")
    items = []
    for acc in accounts:
        global_val = engine.get_effective_rate(conn, rel, acc["id"], ref_day)
        items.append(
            {
                "key": f"{rel}|account|{acc['id']}|",
                "label": f"{acc['name']} · 垫资费(户头通用)",
                "relation_type": rel,
                "primary_entity_type": "account",
                "primary_entity_id": acc["id"],
                "secondary_entity_type": None,
                "secondary_entity_id": None,
                "rate_unit": cat.get("rate_unit") or "per_ton",
                "current_rate": global_val,
                "proposed_rate": global_val,
                "apply_default": False,
            }
        )
        for src in sources:
            spec_val = engine.get_effective_rate(
                conn, rel, acc["id"], ref_day, secondary_entity_id=src["id"]
            )
            has_row = query(
                """
                SELECT id FROM rate_links
                WHERE relation_type=? AND primary_entity_type='account' AND primary_entity_id=?
                  AND secondary_entity_type='invoice_source' AND secondary_entity_id=?
                  AND date(effective_from) <= date(?)
                  AND (effective_to IS NULL OR date(effective_to) >= date(?))
                LIMIT 1
                """,
                (rel, acc["id"], src["id"], ref_day, ref_day),
            )
            if not has_row and abs(spec_val - global_val) < 0.001:
                continue
            items.append(
                {
                    "key": f"{rel}|account|{acc['id']}|source|{src['id']}",
                    "label": f"{acc['name']} × {src['name']} · 垫资费",
                    "relation_type": rel,
                    "primary_entity_type": "account",
                    "primary_entity_id": acc["id"],
                    "secondary_entity_type": "invoice_source",
                    "secondary_entity_id": src["id"],
                    "rate_unit": cat.get("rate_unit") or "per_ton",
                    "current_rate": spec_val,
                    "proposed_rate": spec_val,
                    "apply_default": bool(has_row),
                }
            )
    _attach_wizard_existing_link_ids(items, period_start)
    return items


@app.route("/api/rules/wizard/sheet")
def api_rules_wizard_sheet():
    """本期改价向导：列出全部票源，默认从「上一日命中分期」复制到本结算期窗口。"""
    period_start = (request.args.get("period_start") or "")[:10] or None
    period_end = (request.args.get("period_end") or "")[:10] or None
    as_of = (request.args.get("as_of") or dt.date.today().isoformat())[:10]
    if not period_start or not period_end:
        period_start, period_end = get_period_bounds(as_of)
    ref_day = (dt.date.fromisoformat(period_start) - dt.timedelta(days=1)).isoformat()
    label = _period_label_cn(period_start)

    sources = query("SELECT * FROM invoice_sources ORDER BY name")
    items = []
    for s in sources:
        prev = _term_row_at(s["id"], ref_day)
        existing = _term_row_for_period_start(s["id"], period_start)

        def pick(key, arch_key=None):
            ak = arch_key or key
            if prev and prev[key] is not None:
                return prev[key]
            if s[ak] is not None:
                return s[ak]
            return None

        proposed = {
            "effective_from": period_start,
            "effective_to": period_end,
            "rate_point": pick("rate_point"),
            "outbound_rate_point": pick("outbound_rate_point"),
            "quota_amount": pick("quota_amount"),
            "warn_threshold": pick("warn_threshold"),
            "accepting": int(prev["accepting"]) if prev and prev["accepting"] is not None else 1,
            "notes": (prev["notes"] if prev and prev["notes"] else "") or "",
        }
        items.append(
            {
                "source_id": s["id"],
                "source_name": s["name"],
                "source_type": s["source_type"],
                "existing_term_id": existing["id"] if existing else None,
                "has_prev_term": prev is not None,
                "apply_default": existing is not None or prev is not None,
                "proposed": proposed,
            }
        )

    conn = get_db()
    rate_sections = _build_all_wizard_rate_sections(conn, ref_day, period_start)
    rate_items = [it for sec in rate_sections for it in sec["items"]]

    return {
        "period_label": label,
        "period_start": period_start,
        "period_end": period_end,
        "ref_day": ref_day,
        "items": items,
        "rate_sections": rate_sections,
        "rate_items": rate_items,
    }


@app.route("/api/rules/wizard/apply", methods=["POST"])
def api_rules_wizard_apply():
    """批量写入/更新 invoice_source_terms（仅分期，不动算钱模板）。"""
    data = request.get_json(silent=True) or {}
    period_start = (data.get("period_start") or "")[:10]
    period_end = (data.get("period_end") or "")[:10]
    if not period_start:
        return jsonify({"error": "缺少 period_start"}), 400
    rows_in = data.get("items") or []
    rate_rows_in = data.get("rate_items") or []
    conn = get_db()
    created, updated, skipped = 0, 0, 0
    rate_upserted = 0
    for row in rows_in:
        if not row.get("apply"):
            skipped += 1
            continue
        sid = row.get("source_id")
        if not sid:
            continue
        payload = row.get("proposed") or row
        eff_from = (payload.get("effective_from") or period_start)[:10]
        eff_to = payload.get("effective_to")
        if eff_to:
            eff_to = str(eff_to)[:10]
        else:
            eff_to = period_end or None
        accepting = 1 if payload.get("accepting") in (True, 1, "1", "true") else 0

        def num_or_none(k):
            v = payload.get(k)
            if v is None or v == "":
                return None
            return float(v)

        cols = {
            "quota_amount": num_or_none("quota_amount"),
            "warn_threshold": num_or_none("warn_threshold"),
            "rate_point": num_or_none("rate_point"),
            "outbound_rate_point": num_or_none("outbound_rate_point"),
            "accepting": accepting,
            "effective_to": eff_to,
            "notes": (payload.get("notes") or "").strip() or None,
        }
        term_id = row.get("existing_term_id")
        if not term_id:
            ex = _term_row_for_period_start(int(sid), eff_from)
            term_id = ex["id"] if ex else None
        if term_id:
            conn.execute(
                """
                UPDATE invoice_source_terms SET
                  effective_to=?, quota_amount=?, warn_threshold=?,
                  rate_point=?, outbound_rate_point=?, accepting=?, notes=?
                WHERE id=?
                """,
                (
                    cols["effective_to"],
                    cols["quota_amount"],
                    cols["warn_threshold"],
                    cols["rate_point"],
                    cols["outbound_rate_point"],
                    cols["accepting"],
                    cols["notes"],
                    term_id,
                ),
            )
            audit(
                conn,
                "invoice_source_terms",
                term_id,
                "(向导更新)",
                None,
                f"票源{sid} {eff_from}~{eff_to or '至今'}",
                "规则向导·本期分期",
            )
            updated += 1
        else:
            cur = conn.execute(
                """
                INSERT INTO invoice_source_terms
                  (invoice_source_id, effective_from, effective_to,
                   quota_amount, warn_threshold, accepting,
                   rate_point, outbound_rate_point, notes)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    sid,
                    eff_from,
                    cols["effective_to"],
                    cols["quota_amount"],
                    cols["warn_threshold"],
                    cols["accepting"],
                    cols["rate_point"],
                    cols["outbound_rate_point"],
                    cols["notes"],
                ),
            )
            new_id = cur.lastrowid
            audit(
                conn,
                "invoice_source_terms",
                new_id,
                "(向导新建)",
                None,
                f"票源{sid} {eff_from}~{eff_to or '至今'}",
                "规则向导·本期分期",
            )
            created += 1
    for rr in rate_rows_in:
        if not rr.get("apply"):
            continue
        rv = rr.get("proposed_rate")
        if rv is None or rv == "":
            continue
        eff_from = (rr.get("effective_from") or period_start)[:10]
        sec_type = rr.get("secondary_entity_type")
        sec_id = rr.get("secondary_entity_id")
        if not sec_id:
            sec_type = None
        new_id = engine.upsert_rate(
            conn,
            rr["relation_type"],
            rr["primary_entity_type"],
            rr["primary_entity_id"],
            float(rv),
            eff_from,
            rate_unit=rr.get("rate_unit") or "per_ton",
            secondary_entity_type=sec_type,
            secondary_entity_id=sec_id,
            notes={
                "account_advance_fee": "规则向导·垫资费",
            }.get(rr["relation_type"], "规则向导·费率"),
        )
        audit(
            conn,
            "rate_links",
            new_id,
            "rate_value",
            rr.get("current_rate"),
            float(rv),
            {
                "account_advance_fee": "规则向导·垫资费",
            }.get(rr["relation_type"], "规则向导·费率"),
        )
        rate_upserted += 1
    conn.commit()
    return jsonify({"created": created, "updated": updated, "skipped": skipped, "rate_upserted": rate_upserted})


@app.route("/api/rules/mindmap")
def api_rules_mindmap():
    """只读业务关系图数据（与费率页同口径），供规则 hub 思维导图视图。"""
    as_of = (request.args.get("as_of") or dt.date.today().isoformat())[:10]
    from routes_archives import rate_link_graph

    with app.test_request_context("/api/rate_links/graph", query_string={"as_of": as_of}):
        resp = rate_link_graph()
        graph = resp.get_json()
    return {
        "as_of": as_of,
        "period_start": get_period_bounds(as_of)[0],
        "period_end": get_period_bounds(as_of)[1],
        "nodes": graph.get("nodes") or [],
        "edges": graph.get("edges") or [],
    }


@app.route("/api/meta/periods_in_range")
def api_periods_in_range():
    """统计筛选日期范围内覆盖的结算期标签（用于导出经营报告 Excel）。"""
    date_from = (request.args.get("date_from") or "")[:10]
    date_to = (request.args.get("date_to") or "")[:10]
    if not date_from or not date_to:
        return jsonify({"error": "需要 date_from 与 date_to"}), 400
    try:
        d0 = dt.date.fromisoformat(date_from)
        d1 = dt.date.fromisoformat(date_to)
    except ValueError:
        return jsonify({"error": "日期格式无效"}), 400
    if d1 < d0:
        d0, d1 = d1, d0
    keys = []
    seen = set()
    d = d0
    while d <= d1:
        label = _period_label_cn(d.isoformat()).replace("期", "")
        if label not in seen:
            seen.add(label)
            keys.append(label)
        d += dt.timedelta(days=1)
    return {"date_from": d0.isoformat(), "date_to": d1.isoformat(), "periods": keys}


@app.route("/api/rules/relation_calc_map")
def api_rules_relation_calc_map():
    """费率类型 ↔ 链条模板算价节点（只读对照，供规则 hub 理解算价链路）。"""
    import json

    catalog = {c["code"]: c["label"] for c in engine.RELATION_TYPE_CATALOG}
    relations = {code: [] for code in catalog}
    templates = query("SELECT id, code, name, short_name FROM flow_templates ORDER BY id")
    for t in templates:
        nodes = query(
            "SELECT node_key, label, calc_rule_config FROM template_nodes WHERE template_id=? ORDER BY seq",
            (t["id"],),
        )
        for n in nodes:
            cfg = n["calc_rule_config"] or ""
            if not cfg:
                continue
            for code in catalog:
                if code not in cfg:
                    continue
                relations[code].append(
                    {
                        "template_id": t["id"],
                        "template_code": t["code"],
                        "template_name": t["short_name"] or t["name"],
                        "node_key": n["node_key"],
                        "node_label": n["label"],
                    }
                )
    out = [
        {"code": code, "label": catalog[code], "nodes": relations[code]}
        for code in catalog
        if relations[code]
    ]
    return {"relations": out, "hint": "改费率走向导或「费率与关系」；改公式结构仍到「链条规则」。"}


@app.route("/api/setup/mill_onboarding")
def api_setup_mill_onboarding():
    """新钢厂 / 新链条上线核对清单（只读扫描当前库）。"""
    conn = get_db()
    mills = query("SELECT id, name FROM steel_mills ORDER BY name")
    mill_rows = []
    for m in mills:
        mid = m["id"]
        tiers = query("SELECT COUNT(*) AS c FROM mill_reward_tiers WHERE mill_id=?", (mid,))[0]["c"]
        tier_row = query(
            "SELECT period_start_day, period_end_day FROM mill_reward_tiers WHERE mill_id=? LIMIT 1", (mid,)
        )
        psd = tier_row[0]["period_start_day"] if tier_row else None
        ped = tier_row[0]["period_end_day"] if tier_row else None
        accounts = query("SELECT COUNT(*) AS c FROM accounts WHERE mill_id=?", (mid,))[0]["c"]
        trips = query(
            "SELECT COUNT(*) AS c FROM trips WHERE mill_id=? AND status!='voided'", (mid,)
        )[0]["c"]
        settled = query(
            """SELECT COUNT(*) AS c FROM trips WHERE mill_id=? AND status IN ('settled','invoiced','closed')""",
            (mid,),
        )[0]["c"]
        mill_rows.append(
            {
                "id": mid,
                "name": m["name"],
                "tier_count": tiers,
                "period_start_day": psd,
                "period_end_day": ped,
                "account_count": accounts,
                "trip_count": trips,
                "settled_trip_count": settled,
                "ok_tiers": tiers > 0,
                "ok_accounts": accounts > 0,
                "ok_trips": trips > 0,
            }
        )
    templates = query(
        """SELECT ft.id, ft.code, ft.name, ft.short_name, ft.is_active,
                  (SELECT COUNT(*) FROM trips t WHERE t.template_id=ft.id AND t.status!='voided') AS trip_count
           FROM flow_templates ft ORDER BY ft.id"""
    )
    global_tips = []
    if not mills:
        global_tips.append({"level": "warn", "text": "尚未建立任何钢厂，请先在基础档案添加。"})
    for m in mill_rows:
        if not m["ok_tiers"]:
            global_tips.append(
                {
                    "level": "warn",
                    "text": f"「{m['name']}」未配置量补阶梯（周期起止日也在此表）。",
                    "href": "/archives?tab=mill_reward_tiers",
                }
            )
        if not m["ok_accounts"]:
            global_tips.append(
                {
                    "level": "info",
                    "text": f"「{m['name']}」还没有绑定户头。",
                    "href": "/archives?tab=accounts",
                }
            )
    return {
        "mills": mill_rows,
        "templates": [dict(t) for t in templates],
        "tips": global_tips,
        "playbook": [
            "基础档案：钢厂 → 量补阶梯（含周期起止日）→ 户头",
            "车次：为新车选 template_id / 结算模式（多数复用 M1~M5）",
            "规则与算价：本期向导改票源分期与费率；链条规则「计算口径」改节点用哪类费率",
            "验证：试算几车 → 系统「利润核对」→ python3 regression_check.py",
        ],
    }
