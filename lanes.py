"""三条线：货物线 / 开票线 / 回款线（B阶段，2026-09-21）。

用户口径（2026-09-21 原话整理）：
- 货物线：供货厂 →（调货人，可选）→ 钢厂，检查点是过磅、结算单是否已出。
  调货人自己车队的车（历史上供货厂记成调货人同名占位基地）供货厂留空，显示为"某某车队"。
- 开票线（结算单出了之后）：票商/反向（可选，二选一）→ 供货厂 → 欧冶。
  我们只关心两张票：进项(到开票主体) 和 销项(开给欧冶)。
  供货厂自己有票额 → 可以不要票商（自带票）；但有票额也可以走"不带票、我们配票"。
  所以开票路径按【车次】定（trips.invoice_path），为空时按链条规则+票源推断默认值。
- 回款线：欧冶(国企垫资)按开票路线原路、按票额往回打款；
  走我们配票(经俊兴)的，还要追最后一跳：钱是否落到客户账户（老板管理的卡，可能不是本人的）。
- 结算单/开票/打款都是一批一批做 → 追踪对象是批次(batches)，一批分摊到多车(batch_trips)。

历史数据暂不补录（要先和财务核对）：已开票/已结案但没登记开票批次的车次，开票环节显示
"按状态视为已开"(legacy)；已结案的回款也按状态视为完成；其余如实显示为"未登记"。
"""
import datetime as dt
from flask import Blueprint, jsonify, request
from db import get_db, rows_to_list
import engine

bp = Blueprint("lanes", __name__)

INVOICE_PATHS = {
    "self": "供货厂自带票（带票带货）",
    "source": "票商/反向 → 供货厂 → 欧冶",
    "ours": "我们配票（经俊兴）",
}
HOP_META = {
    # key: (所属线, 名称)
    "in": ("invoice", "进项"),
    "out": ("invoice", "销项→欧冶"),
    "p_out": ("payment", "欧冶回款"),
    "p_in": ("payment", "付票源"),
    "p_client": ("payment", "到客户账户"),
    "commission": ("commission", "调货人提成"),
}
HOP_KIND = {"in": "invoice", "out": "invoice", "p_out": "payment", "p_in": "payment",
            "p_client": "payment", "commission": "commission"}
SETTLED_OR_LATER = ("settled", "invoiced", "closed")
TOL = 0.01


def _ctx(conn):
    """一次性把名字/关系查好，批量算很多车次时不用逐车查档案。"""
    c = {}
    c["bases"] = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM processing_bases")}
    c["alias"] = {i for i, b in c["bases"].items() if b.get("is_dispatcher_alias")}
    own = [b for b in c["bases"].values() if b["base_type"] == "own"]
    c["our_name"] = own[0]["name"] if own else "我方"
    c["sources"] = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM invoice_sources")}
    c["dispatchers"] = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM dispatchers")}
    c["mills"] = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM steel_mills")}
    c["tpl_code"] = {r["id"]: r["code"] for r in conn.execute("SELECT id, code FROM flow_templates")}
    c["conn"] = conn
    c["accounts"] = {r["id"]: dict(r) for r in conn.execute(
        """SELECT a.id, a.advance_fee_rate, f.name AS financier_name FROM accounts a
           LEFT JOIN financiers f ON f.id = a.financier_id""")}
    return c


def _source_is_ours(src, ctx):
    return bool(src) and src["name"] == ctx["our_name"]


def default_invoice_path(trip, ctx):
    code = ctx["tpl_code"].get(trip["template_id"])
    src = ctx["sources"].get(trip["invoice_source_id"])
    if code == "split_company":
        return "ours"
    if code == "split_reverse":
        return "source"
    if not src:
        return "self"
    if _source_is_ours(src, ctx) or src.get("settlement_type") == "pass_through":
        return "ours"
    return "source"


def ticket_amount(trip, ctx):
    """票额（估）：开给欧冶的票 = 结算金额 − 吨位×垫资费单价，跟首页"票源额度水位"同一口径。
    垫资费单价按这一车的模板+费率档查（engine.advance_fee_unit_for_trip）：垫资费一直都有，
    只是某些月份反向票源的垫资费已含在反向票点里，那段时间反向这一档是0（费率与关系页按时段配）。
    口径待和财务核对，改这里一处即可。"""
    amt = trip["settlement_amount_total"]
    if not amt:
        return None
    rate = engine.advance_fee_unit_for_trip(ctx["conn"], trip)
    return round(amt - (trip["settlement_tonnage_total"] or 0) * rate, 2)


def load_alloc(conn, trip_ids=None):
    """{trip_id: {hop_key: {"amount": x, "batches": [ids]}}}"""
    sql = """SELECT bt.trip_id, b.hop_key, SUM(bt.amount) AS amt, GROUP_CONCAT(b.id) AS ids
             FROM batch_trips bt JOIN batches b ON b.id = bt.batch_id"""
    args = []
    if trip_ids is not None:
        if not trip_ids:
            return {}
        sql += f" WHERE bt.trip_id IN ({','.join('?' * len(trip_ids))})"
        args = list(trip_ids)
    sql += " GROUP BY bt.trip_id, b.hop_key"
    out = {}
    for r in conn.execute(sql, args):
        out.setdefault(r["trip_id"], {})[r["hop_key"]] = {
            "amount": r["amt"] or 0, "batches": [int(x) for x in (r["ids"] or "").split(",") if x]}
    return out


def _origin(trip, ctx):
    """货物起点：真实供货厂 / 调货人车队 / 未填。"""
    bid = trip["base_id"]
    disp = ctx["dispatchers"].get(trip["dispatcher_id"])
    if bid and bid not in ctx["alias"]:
        return ctx["bases"][bid]["name"], "base"
    if disp and (bid in ctx["alias"] or not bid):
        return f"{disp}车队", "fleet"
    return "未填供货厂", "none"


def build_lanes(trip, ctx, alloc_for_trip):
    t = trip
    status = t["status"]
    settled = status in SETTLED_OR_LATER
    path = t["invoice_path"] if ("invoice_path" in t.keys() and t["invoice_path"]) else default_invoice_path(t, ctx)
    path_is_default = not ("invoice_path" in t.keys() and t["invoice_path"])
    origin, origin_kind = _origin(t, ctx)
    disp = ctx["dispatchers"].get(t["dispatcher_id"])
    mill = ctx["mills"].get(t["mill_id"]) or "钢厂"
    src = ctx["sources"].get(t["invoice_source_id"])
    src_name = src["name"] if src and not _source_is_ours(src, ctx) else None
    acc = ctx["accounts"].get(t["account_id"]) or {}
    financier = acc.get("financier_name") or "欧冶"
    seller = ctx["our_name"] if path == "ours" else (origin if origin_kind != "none" else "供货厂")
    amount = ticket_amount(t, ctx)

    goods = {
        "nodes": [origin] + ([disp] if disp else []) + [mill],
        "checks": [
            {"key": "arrived", "label": "到厂", "done": status in ("arrived",) + SETTLED_OR_LATER},
            {"key": "settled", "label": "结算单", "done": settled or bool(t["settlement_doc_no"]),
             "doc_no": t["settlement_doc_no"], "date": t["settlement_date"]},
        ],
    }

    inv = []
    if path in ("source", "ours") and src_name:
        inv.append(("in", src_name, seller))
    inv.append(("out", seller, financier))
    pay = [("p_out", financier, seller)]
    if path in ("source", "ours") and src_name:
        pay.append(("p_in", seller, src_name))
    if path == "ours":
        last = src_name or ctx["our_name"]
        client = origin if origin_kind != "none" else "客户"
        pay.append(("p_client", last, f"{client}·客户账户"))

    def hop(key, frm, to, blocked, legacy):
        a = alloc_for_trip.get(key) or {"amount": 0, "batches": []}
        got = a["amount"]
        if amount is not None and got >= amount - TOL and got > 0:
            state = "done"
        elif got > 0:
            state = "partial" if amount is not None else "done"
        elif legacy:
            state = "legacy"
        elif blocked:
            state = "blocked"
        else:
            state = "todo"
        return {"key": key, "label": HOP_META[key][1], "from": frm, "to": to, "expected": amount,
                "allocated": round(got, 2), "remaining": (round(amount - got, 2) if amount is not None else None),
                "state": state, "batches": a["batches"]}

    inv_legacy = status in ("invoiced", "closed")
    inv_hops = [hop(k, f, to, not settled, inv_legacy) for k, f, to in inv]
    out_ok = any(h["key"] == "out" and h["state"] in ("done", "legacy") for h in inv_hops)
    pay_hops = []
    prev_ok = out_ok
    for k, f, to in pay:
        h = hop(k, f, to, not prev_ok, status == "closed")
        pay_hops.append(h)
        prev_ok = h["state"] in ("done", "legacy")
    return {
        "trip_id": t["id"], "path": path, "path_label": INVOICE_PATHS[path], "path_is_default": path_is_default,
        "ticket_amount": amount, "origin": origin, "origin_kind": origin_kind,
        "goods": goods, "invoice": inv_hops, "payment": pay_hops,
    }


def next_hop(lanes):
    for h in lanes["invoice"] + lanes["payment"]:
        if h["state"] in ("todo", "partial"):
            return h
    return None


def _age_days(date_str):
    if not date_str:
        return None
    try:
        return (dt.date.today() - dt.date.fromisoformat(str(date_str)[:10])).days
    except ValueError:
        return None


def trips_lanes(conn, where_sql="1=1", args=()):
    ctx = _ctx(conn)
    trips = conn.execute(
        f"""SELECT t.*, b.name AS base_name, d.name AS dispatcher_name FROM trips t
            LEFT JOIN processing_bases b ON b.id=t.base_id LEFT JOIN dispatchers d ON d.id=t.dispatcher_id
            WHERE t.status!='voided' AND {where_sql}""", args).fetchall()
    alloc = load_alloc(conn, [t["id"] for t in trips])
    return [(t, build_lanes(t, ctx, alloc.get(t["id"], {}))) for t in trips]


# 账龄分桶默认值（15/30/60）。2026-10-09 起可在「系统 → 参数设置 → 账龄分桶」改；
# 这里是回退默认，settings.aging_buckets(conn) 读不到/填坏配置时给的就是这一份。
AGING_BUCKETS = [(15, "15天内"), (30, "16-30天"), (60, "31-60天"), (None, "60天以上")]


def current_aging_buckets(conn):
    """当前生效的账龄分桶（取系统参数，回退默认）。"""
    import settings
    return settings.aging_buckets(conn)


def pending_rows(conn):
    """所有还没走完的开票/回款环节（todo/partial），带账龄：开票环节从结算日期起算，
    回款环节从开票日期(没有则结算日期)起算。"""
    rows = []
    for t, ln in trips_lanes(conn, "t.status IN ('settled','invoiced','closed')"):
        for h in ln["invoice"] + ln["payment"]:
            if h["state"] not in ("todo", "partial"):
                continue
            since = t["settlement_date"] if h["key"] in ("in", "out") else (t["invoiced_date"] or t["settlement_date"])
            rows.append({
                "trip_id": t["id"], "plate_no": t["plate_no"], "status": t["status"], "transport_type": t["transport_type"],
                "settlement_date": t["settlement_date"], "base_id": t["base_id"], "origin": ln["origin"],
                "dispatcher_name": t["dispatcher_name"], "path": ln["path"], "hop_key": h["key"], "hop_label": h["label"],
                "from": h["from"], "to": h["to"], "expected": h["expected"], "allocated": h["allocated"],
                "remaining": h["remaining"], "state": h["state"], "age_days": _age_days(since),
                "settlement_doc_no": t["settlement_doc_no"],
            })
    return rows


def summarize_pending(rows, buckets=None):
    buckets = buckets or AGING_BUCKETS
    by_hop = {}
    for r in rows:
        s = by_hop.setdefault(r["hop_key"], {"hop_key": r["hop_key"], "label": HOP_META[r["hop_key"]][1],
                                             "line": HOP_META[r["hop_key"]][0], "trip_count": 0, "remaining": 0.0,
                                             "aging": {lbl: 0.0 for _, lbl in buckets}, "max_age": 0})
        s["trip_count"] += 1
        rem = r["remaining"] or 0
        s["remaining"] += rem
        age = r["age_days"] or 0
        s["max_age"] = max(s["max_age"], age)
        for lim, lbl in buckets:
            if lim is None or age <= lim:
                s["aging"][lbl] += rem
                break
    order = ["in", "out", "p_out", "p_in", "p_client"]
    return [by_hop[k] for k in order if k in by_hop]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@bp.route("/api/lanes/meta")
def lanes_meta():
    return jsonify({"paths": INVOICE_PATHS, "hops": {k: {"line": v[0], "label": v[1]} for k, v in HOP_META.items()},
                    "aging_buckets": [lbl for _, lbl in current_aging_buckets(get_db())]})


@bp.route("/api/trips/<int:trip_id>/lanes")
def trip_lanes(trip_id):
    conn = get_db()
    res = trips_lanes(conn, "t.id=?", (trip_id,))
    if not res:
        return jsonify({"error": "not found"}), 404
    t, ln = res[0]
    ln["default_path"] = default_invoice_path(t, _ctx(conn))
    ln["next"] = next_hop(ln)
    batch_ids = sorted({b for h in ln["invoice"] + ln["payment"] for b in h["batches"]})
    ln["batch_list"] = _batches_by_ids(conn, batch_ids)
    return jsonify(ln)


@bp.route("/api/trips_lanes_brief")
def trips_lanes_brief():
    """车次列表用：每车三条线的一行摘要（下一步是什么）。"""
    conn = get_db()
    out = {}
    for t, ln in trips_lanes(conn):
        nh = next_hop(ln)
        inv_done = sum(1 for h in ln["invoice"] if h["state"] in ("done", "legacy"))
        pay_done = sum(1 for h in ln["payment"] if h["state"] in ("done", "legacy"))
        out[t["id"]] = {"path": ln["path"], "origin": ln["origin"], "origin_kind": ln["origin_kind"],
                        "inv": [inv_done, len(ln["invoice"])], "pay": [pay_done, len(ln["payment"])],
                        "inv_legacy": any(h["state"] == "legacy" for h in ln["invoice"]),
                        "next": ({"key": nh["key"], "label": nh["label"], "to": nh["to"]} if nh else None)}
    return jsonify(out)


@bp.route("/api/tracking")
def tracking():
    conn = get_db()
    rows = pending_rows(conn)
    hop = request.args.get("hop_key")
    summary = summarize_pending(rows, current_aging_buckets(conn))
    if hop:
        rows = [r for r in rows if r["hop_key"] == hop]
    rows.sort(key=lambda r: -(r["age_days"] or 0))
    return jsonify({"summary": summary, "rows": rows})


def _batches_by_ids(conn, ids):
    if not ids:
        return []
    rows = conn.execute(
        f"""SELECT b.*, ba.account_name, ba.card_tail, COUNT(bt.trip_id) AS trip_count
            FROM batches b LEFT JOIN base_accounts ba ON ba.id=b.base_account_id
            LEFT JOIN batch_trips bt ON bt.batch_id=b.id
            WHERE b.id IN ({','.join('?' * len(ids))}) GROUP BY b.id ORDER BY b.batch_date DESC, b.id DESC""",
        ids).fetchall()
    return rows_to_list(rows)


@bp.route("/api/batches", methods=["GET"])
def list_batches():
    conn = get_db()
    where, args = ["1=1"], []
    for k in ("kind", "hop_key"):
        if request.args.get(k):
            where.append(f"b.{k}=?")
            args.append(request.args[k])
    rows = conn.execute(
        f"""SELECT b.*, ba.account_name, ba.card_tail, pb.name AS account_base_name,
                   COUNT(bt.trip_id) AS trip_count, COALESCE(SUM(bt.amount),0) AS allocated
            FROM batches b LEFT JOIN base_accounts ba ON ba.id=b.base_account_id
            LEFT JOIN processing_bases pb ON pb.id=ba.base_id
            LEFT JOIN batch_trips bt ON bt.batch_id=b.id
            WHERE {' AND '.join(where)} GROUP BY b.id ORDER BY b.batch_date DESC, b.id DESC LIMIT 300""",
        args).fetchall()
    return jsonify(rows_to_list(rows))


@bp.route("/api/batches/<int:batch_id>", methods=["GET", "DELETE"])
def batch_item(batch_id):
    conn = get_db()
    if request.method == "DELETE":
        import system_ops  # 删除批次进回收站（连同分摊明细），30天内可恢复
        b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if b:
            system_ops.recycle(conn, "batches", batch_id,
                               f"批次#{batch_id} {HOP_META.get(b['hop_key'], ('', b['hop_key']))[1]} {b['batch_date'] or ''} ¥{b['total_amount'] or 0:,.2f}")
        conn.commit()
        return jsonify({"ok": True, "recycled": True})
    b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not b:
        return jsonify({"error": "not found"}), 404
    trips = conn.execute(
        """SELECT bt.trip_id, bt.amount, t.plate_no, t.settlement_date, t.settlement_doc_no, pb.name AS base_name
           FROM batch_trips bt JOIN trips t ON t.id=bt.trip_id LEFT JOIN processing_bases pb ON pb.id=t.base_id
           WHERE bt.batch_id=? ORDER BY t.settlement_date, t.id""", (batch_id,)).fetchall()
    return jsonify({"batch": dict(b), "trips": rows_to_list(trips)})


@bp.route("/api/batches", methods=["POST"])
def create_batch():
    """{hop_key, doc_no, batch_date, base_account_id, notes, allocations:[{trip_id, amount}]}
    from/to 名字按各车次的开票路径算出来：整批一致就记名字，不一致记"多方"。"""
    conn = get_db()
    d = request.get_json(force=True) or {}
    hop = d.get("hop_key")
    if hop not in HOP_KIND:
        return jsonify({"error": "invalid hop_key"}), 400
    allocs = [a for a in (d.get("allocations") or []) if a.get("trip_id") and a.get("amount") not in (None, "")]
    if not allocs:
        return jsonify({"error": "至少选一车并填金额"}), 400
    ids = [int(a["trip_id"]) for a in allocs]
    froms, tos = set(), set()
    if hop != "commission":
        for t, ln in trips_lanes(conn, f"t.id IN ({','.join('?' * len(ids))})", ids):
            h = next((x for x in ln["invoice"] + ln["payment"] if x["key"] == hop), None)
            if h:
                froms.add(h["from"])
                tos.add(h["to"])
    else:
        tos = {r["name"] for r in conn.execute(
            f"""SELECT DISTINCT d.name FROM trips t JOIN dispatchers d ON d.id=t.dispatcher_id
                WHERE t.id IN ({','.join('?' * len(ids))})""", ids)}
        froms = {"我方"}
    one = lambda s: next(iter(s)) if len(s) == 1 else ("多方" if s else None)
    total = round(sum(float(a["amount"]) for a in allocs), 2)
    cur = conn.execute(
        """INSERT INTO batches (kind, hop_key, from_name, to_name, doc_no, batch_date, base_account_id, total_amount, notes)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (HOP_KIND[hop], hop, d.get("from_name") or one(froms), d.get("to_name") or one(tos), d.get("doc_no"),
         d.get("batch_date") or dt.date.today().isoformat(), d.get("base_account_id") or None, total, d.get("notes")))
    bid = cur.lastrowid
    for a in allocs:
        conn.execute("INSERT OR REPLACE INTO batch_trips (batch_id, trip_id, amount) VALUES (?,?,?)",
                     (bid, int(a["trip_id"]), float(a["amount"])))
    # 销项开票登记后，车次还在"已结算"的顺手推到"已开票"（开票日期=批次日期，已有不覆盖）
    if hop == "out" and d.get("advance_status", True):
        conn.execute(
            f"""UPDATE trips SET status='invoiced', invoiced_date=coalesce(nullif(invoiced_date,''), ?),
                updated_at=datetime('now','localtime') WHERE status='settled' AND id IN ({','.join('?' * len(ids))})""",
            [d.get("batch_date") or dt.date.today().isoformat()] + ids)
    import system_ops
    system_ops.audit(conn, "batches", bid, "(新建)", None,
                     f"{HOP_META[hop][1]} {d.get('batch_date') or ''} {len(allocs)}车 ¥{total:,.2f}", "登记批次")
    conn.commit()
    return jsonify({"id": bid, "total_amount": total, "trip_count": len(allocs)}), 201
