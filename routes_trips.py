"""车次：列表、录入、修改、利润试算/固化、删除。

从原 app.py 拆出（v2.0.1）。共用的常量和工具函数都在 core.py。"""
import os
import secrets
import shutil
import time

from core import *  # noqa: F401,F403

# 附件上传（v2.3 ①）：multipart 真上传，文件落 uploads/，trip_attachments 自动登记。
# 老的 JSON 接口 POST /api/trips/<id>/attachments（只填路径）保留兼容。
ATT_CATEGORIES = {  # 与 schema.sql trip_attachments.category 注释保持一致
    "driver_license", "vehicle_license", "id_card", "env_list",
    "truck_head", "truck_tail", "cargo_photo",
    "weighing_slip", "settlement_doc", "payment_screenshot",
}
ATT_CATEGORY_CN = {
    "driver_license": "驾驶证", "vehicle_license": "行驶证", "id_card": "身份证",
    "env_list": "环保清单", "truck_head": "车头照", "truck_tail": "车尾照",
    "cargo_photo": "货物照", "weighing_slip": "过磅单",
    "settlement_doc": "结算单", "payment_screenshot": "付款截图",
}
ALLOWED_ATT_EXT = {"jpg", "jpeg", "png", "webp", "heic", "pdf"}
MAX_ATT_SIZE = 10 * 1024 * 1024  # 10MB；过磅单/结算单照片通常 <2MB，留足余量


@app.route("/api/trips/<int:trip_id>/node_docs/<node_key>", methods=["PUT"])
def update_trip_node_doc(trip_id, node_key):
    """两票三款通用化：给"doc_types含invoice"的节点录入发票号/实际金额/状态(如票库存中)，
    upsert到 trip_node_values(trip_id, node_key)，不影响 computed_value/override_value 这两列
    (那两列是给v2引擎全面切换后用的，本次不涉及)。"""
    conn = get_db()
    data = request.get_json(force=True) or {}
    fields = {k: data[k] for k in ("doc_no", "actual_amount", "doc_status") if k in data}
    if not fields:
        return jsonify({"error": "empty payload"}), 400
    existing = conn.execute(
        "SELECT 1 FROM trip_node_values WHERE trip_id=? AND node_key=?", (trip_id, node_key)
    ).fetchone()
    if existing:
        set_clause = ",".join(f"{c}=?" for c in fields) + ", updated_at=datetime('now','localtime')"
        conn.execute(
            f"UPDATE trip_node_values SET {set_clause} WHERE trip_id=? AND node_key=?",
            list(fields.values()) + [trip_id, node_key],
        )
    else:
        cols = ["trip_id", "node_key"] + list(fields.keys())
        placeholders = ",".join(["?"] * len(cols))
        conn.execute(
            f"INSERT INTO trip_node_values ({','.join(cols)}) VALUES ({placeholders})",
            [trip_id, node_key] + list(fields.values()),
        )
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/trips", methods=["GET"])
def list_trips():
    """车次列表 + 从各统计页"点数字跳详情"用的筛选参数（2026-09-21 内链）：
    status / status_in(逗号分隔) / exclude_voided=1；mill_id/base_id/dispatcher_id/invoice_source_id/
    template_id（值为 none 表示"未指定"那一桶，即字段为空）；family=split|service_fee(配票/带票)；
    transport_type；date_field=settlement_date|order_date（不传=结算日期优先，否则接单日期）+ date_from/date_to；
    ids=逗号分隔车次id（质量等按明细聚合的桶直接传id）。"""
    conn = get_db()
    a = request.args
    where = ["1=1"]
    args = []
    if a.get("status"):
        where.append("t.status=?")
        args.append(a["status"])
    if a.get("status_in"):
        sts = [x for x in a["status_in"].split(",") if x]
        where.append(f"t.status IN ({','.join('?' * len(sts))})")
        args.extend(sts)
    if a.get("exclude_voided"):
        where.append("t.status!='voided'")
    for key in ("mill_id", "base_id", "dispatcher_id", "invoice_source_id", "template_id"):
        v = a.get(key)
        if v == "none":
            where.append(f"t.{key} IS NULL")
        elif v:
            where.append(f"t.{key}=?")
            args.append(v)
    if a.get("family") in ("split", "service_fee"):
        where.append("ft.calc_family=?")
        args.append(a["family"])
    if a.get("transport_type"):
        where.append("t.transport_type=?")
        args.append(a["transport_type"])
    if a.get("ids"):
        ids = [int(x) for x in a["ids"].split(",") if x.strip().isdigit()]
        where.append(f"t.id IN ({','.join('?' * len(ids))})" if ids else "0")
        args.extend(ids)
    date_expr = {"settlement_date": "t.settlement_date", "order_date": "t.order_date"}.get(
        a.get("date_field"), "coalesce(t.settlement_date, t.order_date)")
    if a.get("date_from"):
        where.append(f"date({date_expr}) >= date(?)")
        args.append(a["date_from"])
    if a.get("date_to"):
        where.append(f"date({date_expr}) <= date(?)")
        args.append(a["date_to"])
    q = request.args.get("q")
    if q:
        where.append("(t.plate_no LIKE ? OR t.driver_name LIKE ?)")
        args.extend([f"%{q}%", f"%{q}%"])

    sql = f"""
        SELECT t.*, m.name AS mill_name, a.name AS account_name, b.name AS base_name,
               d.name AS dispatcher_name, ft.short_name AS template_short, ft.name AS template_name,
               EXISTS(SELECT 1 FROM trip_attachments ta WHERE ta.trip_id=t.id AND ta.category='weighing_slip')
                   AS has_weighing_slip,
               (SELECT COUNT(*) FROM trip_scrap_details sd WHERE sd.trip_id=t.id) AS detail_count,
               (SELECT COUNT(*) FROM template_nodes tn WHERE tn.template_id=t.template_id
                   AND tn.doc_types LIKE '%invoice%') AS invoice_nodes_total,
               (SELECT COUNT(*) FROM trip_node_values tnv WHERE tnv.trip_id=t.id AND tnv.doc_no IS NOT NULL)
                   AS invoice_nodes_done,
               (SELECT COUNT(*) FROM template_nodes tn WHERE tn.template_id=t.template_id
                   AND tn.doc_types LIKE '%payment%') AS payment_nodes_total,
               (SELECT COUNT(DISTINCT node_key) FROM payments p WHERE p.trip_id=t.id AND p.node_key IS NOT NULL)
                   AS payment_nodes_done
        FROM trips t
        LEFT JOIN steel_mills m ON t.mill_id = m.id
        LEFT JOIN accounts a ON t.account_id = a.id
        LEFT JOIN processing_bases b ON t.base_id = b.id
        LEFT JOIN dispatchers d ON t.dispatcher_id = d.id
        LEFT JOIN flow_templates ft ON t.template_id = ft.id
        WHERE {' AND '.join(where)}
        ORDER BY coalesce(t.settlement_date, t.order_date) DESC, t.id DESC
        LIMIT 2000
    """
    rows = conn.execute(sql, args).fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/trips/<int:trip_id>/finance_overrides", methods=["PUT"])
def update_finance_overrides(trip_id):
    """改这一车的费率：{"rate:advance_fee": 20} 设置，值为 null/"" 表示恢复默认(按费率与关系)。
    已固化的车次：按新费率重新计算并更新固化快照(仍保持固化)；未固化的：有利润记录就更新记录。"""
    conn = get_db()
    trip = conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not trip:
        return jsonify({"error": "not found"}), 404
    cur = _stored_overrides(trip)
    for k, v in (request.get_json(force=True) or {}).items():
        if not str(k).startswith("rate:"):
            continue
        if v in (None, ""):
            cur.pop(k, None)
        else:
            try:
                cur[k] = float(v)
            except (TypeError, ValueError):
                return jsonify({"error": f"{k} 不是数字"}), 400
    audit(conn, "trips", trip_id, "finance_overrides", trip["finance_overrides"], json.dumps(cur, ensure_ascii=False) if cur else None)
    conn.execute("UPDATE trips SET finance_overrides=?, updated_at=datetime('now','localtime') WHERE id=?",
                 (json.dumps(cur, ensure_ascii=False) if cur else None, trip_id))
    conn.commit()
    trip = conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
    fin = _compute_finance(conn, trip)
    row = conn.execute("SELECT is_locked FROM trip_finance WHERE trip_id=?", (trip_id,)).fetchone()
    if row:
        calc.save_trip_finance(conn, trip_id, fin, lock=bool(row["is_locked"]))
        fin["is_locked"] = row["is_locked"]
    return jsonify(fin)


@app.route("/api/entry_defaults", methods=["GET"])
def api_entry_defaults():
    return jsonify(_entry_defaults(get_db()))


@app.route("/api/app_settings", methods=["GET", "PUT"])
def api_app_settings():
    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    if request.method == "PUT":
        for k, v in (request.get_json(force=True) or {}).items():
            if str(k).startswith("mobile_"):
                continue  # 手机访问口令只能在「系统 → 手机访问」里改
            conn.execute("INSERT INTO app_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (k, v))
        conn.commit()
    return jsonify({r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM app_settings") if not r["key"].startswith("mobile_")})


@app.route("/api/plates", methods=["GET"])
def api_plates():
    """车牌/船号联想列表：历史出现过的车牌，按使用次数排序。"""
    rows = get_db().execute(
        """SELECT plate_no, transport_type, COUNT(*) AS n, MAX(coalesce(order_date, settlement_date)) AS last_date
           FROM trips WHERE plate_no IS NOT NULL AND plate_no!='' AND status!='voided'
           GROUP BY plate_no ORDER BY n DESC, last_date DESC""").fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/plate_profile", methods=["GET"])
def api_plate_profile():
    return jsonify(_plate_profile(get_db(), (request.args.get("plate") or "").strip()))


@app.route("/api/trips", methods=["POST"])
def create_trip():
    conn = get_db()
    data = request.get_json(force=True) or {}
    # 没传的字段用录入默认值补上；显式传了(哪怕是空)就尊重传入值
    for k, v in _entry_defaults(conn).items():
        data.setdefault(k, v)
    cols = [c for c in TRIP_FIELDS if c in data]
    vals = [data[c] for c in cols]
    placeholders = ",".join(["?"] * len(cols))
    cur = conn.execute(f"INSERT INTO trips ({','.join(cols)}) VALUES ({placeholders})", vals)
    audit(conn, "trips", cur.lastrowid, "(新建)", None, data.get("plate_no") or f"车次#{cur.lastrowid}")
    if "mode_code" in data:
        _sync_template_id(conn, cur.lastrowid, data["mode_code"])
    if "template_id" in data and data["template_id"]:
        _sync_mode_code_from_template(conn, cur.lastrowid, data["template_id"])
    conn.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/trips/<int:trip_id>", methods=["GET"])
def get_trip(trip_id):
    conn = get_db()
    result = _fetch_trip_full(conn, trip_id)
    if not result:
        return jsonify({"error": "not found"}), 404
    return jsonify(result)


@app.route("/api/trips/<int:trip_id>", methods=["PUT"])
def update_trip(trip_id):
    conn = get_db()
    data = request.get_json(force=True) or {}
    cols = [c for c in TRIP_FIELDS if c in data]
    if cols:
        old = conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
        audit_row_diff(conn, "trips", trip_id, old, {c: data[c] for c in cols})
        set_clause = ",".join(f"{c}=?" for c in cols)
        vals = [data[c] for c in cols] + [trip_id]
        conn.execute(f"UPDATE trips SET {set_clause}, updated_at=datetime('now','localtime') WHERE id=?", vals)
        if "mode_code" in data:
            _sync_template_id(conn, trip_id, data["mode_code"])
        if "template_id" in data and data["template_id"]:
            _sync_mode_code_from_template(conn, trip_id, data["template_id"])
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/trips/<int:trip_id>/status", methods=["POST"])
def update_trip_status(trip_id):
    conn = get_db()
    data = request.get_json(force=True) or {}
    status = data.get("status")
    valid = {s for s, _ in STATUS_LIST}
    if status not in valid:
        return jsonify({"error": "invalid status"}), 400
    _apply_status(conn, trip_id, status, data)
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/trips/batch_status", methods=["POST"])
def batch_trip_status():
    """批量推进(A阶段)：{status, ids:[...], common:{status_date, settlement_doc_no},
    per_trip:{id:{settlement_tonnage_total, settlement_amount_total, settlement_doc_no}}}。
    结算单/开票/打款经常一批一批做，所以结算单号和日期可以整批共用，逐车再覆盖。"""
    conn = get_db()
    data = request.get_json(force=True) or {}
    status = data.get("status")
    if status not in {s for s, _ in STATUS_LIST}:
        return jsonify({"error": "invalid status"}), 400
    ids = [int(i) for i in data.get("ids") or []]
    common = data.get("common") or {}
    per = {str(k): v for k, v in (data.get("per_trip") or {}).items()}
    quick = 0
    for tid in ids:
        extra = {**common, **{k: v for k, v in (per.get(str(tid)) or {}).items() if v not in (None, "")}}
        if _apply_status(conn, tid, status, extra):
            quick += 1
    conn.commit()
    return jsonify({"ok": True, "count": len(ids), "quick_settled": quick})


@app.route("/api/trips/<int:trip_id>/details", methods=["PUT"])
def update_trip_details(trip_id):
    conn = get_db()
    data = request.get_json(force=True) or {}
    details = data.get("details", [])
    _old = conn.execute("SELECT settlement_tonnage_total, settlement_amount_total FROM trips WHERE id=?", (trip_id,)).fetchone()

    conn.execute("DELETE FROM trip_scrap_details WHERE trip_id=?", (trip_id,))
    net_total = ded_total = ton_total = cut_total = amt_total = 0.0
    for d in details:
        gross = float(d.get("gross_weight") or 0) or None
        tare = float(d.get("tare_weight") or 0) or None
        net = d.get("net_weight")
        if net in (None, ""):
            net = (gross or 0) - (tare or 0) if (gross and tare) else None
        else:
            net = float(net)
        deduction = float(d.get("deduction") or 0)
        tonnage = d.get("settlement_tonnage")
        if tonnage in (None, ""):
            tonnage = (net or 0) - deduction
        else:
            tonnage = float(tonnage)
        unit_price = float(d.get("unit_price") or 0)
        cutting_fee = float(d.get("cutting_fee") or 0)
        amount = d.get("amount")
        if amount in (None, ""):
            amount = tonnage * unit_price - cutting_fee
        else:
            amount = float(amount)

        conn.execute(
            """INSERT INTO trip_scrap_details
               (trip_id, scrap_type_id, gross_weight, tare_weight, net_weight, deduction,
                settlement_tonnage, unit_price, cutting_fee, amount)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (trip_id, d.get("scrap_type_id"), gross, tare, net, deduction, tonnage, unit_price, cutting_fee, amount),
        )
        net_total += net or 0
        ded_total += deduction
        ton_total += tonnage
        cut_total += cutting_fee
        amt_total += amount

    conn.execute(
        """UPDATE trips SET net_weight_total=?, deduction_total=?, settlement_tonnage_total=?,
           cutting_fee_total=?, settlement_amount_total=?, updated_at=datetime('now','localtime')
           WHERE id=?""",
        (net_total, ded_total, ton_total, cut_total, amt_total, trip_id),
    )
    if _old:
        audit(conn, "trips", trip_id, "details",
              f"{_old['settlement_tonnage_total'] or 0:g}吨 / ¥{_old['settlement_amount_total'] or 0:,.2f}",
              f"{ton_total:g}吨 / ¥{amt_total:,.2f}（{len(details)}行）")
    conn.commit()
    return jsonify({"ok": True, "settlement_tonnage_total": ton_total, "settlement_amount_total": amt_total})


@app.route("/api/trips/<int:trip_id>/finance/preview", methods=["POST"])
def preview_trip_finance(trip_id):
    conn = get_db()
    trip = conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not trip:
        return jsonify({"error": "not found"}), 404
    overrides = request.get_json(force=True) or {}
    fin = _compute_finance(conn, trip, overrides)
    return jsonify(fin)


@app.route("/api/trips/<int:trip_id>/finance", methods=["POST"])
def save_finance(trip_id):
    conn = get_db()
    trip = conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not trip:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True) or {}
    lock = bool(data.pop("lock", False))
    fin = _compute_finance(conn, trip, data)
    ok = calc.save_trip_finance(conn, trip_id, fin, lock=lock)
    if ok and lock:
        audit(conn, "trips", trip_id, "lock", None, f"固化快照 净利润¥{fin.get('net_profit') or 0:,.2f}")
        conn.commit()
    if not ok:
        return jsonify({"error": "已固化快照，不能直接覆盖"}), 409
    fin["is_locked"] = 1 if lock else 0
    return jsonify(fin)


@app.route("/api/trips/<int:trip_id>", methods=["DELETE"])
def delete_trip(trip_id):
    """删除车次 → 进回收站（连同磅重/利润/单据/打款/批次分摊），30天内可在"系统"页恢复。"""
    conn = get_db()
    t = conn.execute("SELECT plate_no, settlement_date, order_date FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not t:
        return jsonify({"error": "not found"}), 404
    system_ops.recycle(conn, "trips", trip_id, f"车次#{trip_id} {t['plate_no'] or ''} {t['settlement_date'] or t['order_date'] or ''}".strip())
    conn.commit()
    return jsonify({"ok": True, "recycled": True})


@app.route("/api/trips/<int:trip_id>/attachments", methods=["POST"])
def add_attachment(trip_id):
    conn = get_db()
    data = request.get_json(force=True) or {}
    cur = conn.execute(
        "INSERT INTO trip_attachments (trip_id, category, file_path) VALUES (?,?,?)",
        (trip_id, data.get("category"), data.get("file_path")),
    )
    conn.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/trips/<int:trip_id>/attachments/upload", methods=["POST"])
def upload_attachment(trip_id):
    """附件真上传（v2.3 ①）：multipart/form-data，字段 file + category。
    文件落 uploads/，DB 存 /uploads/<filename>（前端可直接 <img src> 或 <a href>）。
    手机端经 mobile.py 白名单放行，外勤可现场拍过磅单直传。"""
    conn = get_db()
    t = conn.execute("SELECT id, plate_no FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not t:
        return jsonify({"error": "车次不存在"}), 404
    if "file" not in request.files:
        return jsonify({"error": "没收到文件"}), 400
    f = request.files["file"]
    category = (request.form.get("category") or "").strip()
    if category not in ATT_CATEGORIES:
        return jsonify({"error": "附件类型不对"}), 400
    if not f or not f.filename:
        return jsonify({"error": "文件为空"}), 400
    # 扩展名白名单（防上传可执行/脚本）
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED_ATT_EXT:
        return jsonify({"error": f"不支持的文件类型({ext})，仅支持 jpg/png/webp/heic/pdf"}), 400
    data = f.read()
    if len(data) == 0:
        return jsonify({"error": "文件为空"}), 400
    if len(data) > MAX_ATT_SIZE:
        return jsonify({"error": "文件太大，请压缩到 10MB 以内"}), 400
    # 唯一文件名：trip_<车次id>_<时间戳>_<6位随机>.<ext>，避免重名覆盖
    filename = f"trip_{trip_id}_{int(time.time())}_{secrets.token_hex(3)}.{ext}"
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    disk_path = os.path.join(UPLOAD_DIR, filename)
    with open(disk_path, "wb") as out:
        out.write(data)
    # DB 存带前导 / 的 URL 路径，前端可直接拼到 src/href
    url_path = f"/uploads/{filename}"
    cur = conn.execute(
        "INSERT INTO trip_attachments (trip_id, category, file_path) VALUES (?,?,?)",
        (trip_id, category, url_path),
    )
    from system_ops import audit
    audit(conn, "trip_attachments", cur.lastrowid, "file_path", None, url_path,
          f"上传附件·{ATT_CATEGORY_CN.get(category, category)}")
    conn.commit()
    return jsonify({"id": cur.lastrowid, "category": category, "file_path": url_path}), 201


@app.route("/api/trips/<int:trip_id>/attachments/<int:att_id>", methods=["DELETE"])
def del_attachment(trip_id, att_id):
    """删除附件（v2.3 ①）：DB 记录删除，物理文件移到 uploads/.trash/ 不破坏性删除。
    手机端不在 mobile.py 写白名单里，只能电脑端删（外勤误删可回电脑端找回）。"""
    conn = get_db()
    a = conn.execute(
        "SELECT * FROM trip_attachments WHERE id=? AND trip_id=?", (att_id, trip_id)
    ).fetchone()
    if not a:
        return jsonify({"error": "附件不存在"}), 404
    fp = a["file_path"] or ""
    # 物理文件移到 .trash（遵循"不做破坏性删除"原则）
    if fp.startswith("/uploads/"):
        src = os.path.join(UPLOAD_DIR, os.path.basename(fp))
        trash_dir = os.path.join(UPLOAD_DIR, ".trash")
        os.makedirs(trash_dir, exist_ok=True)
        if os.path.exists(src):
            try:
                shutil.move(src, os.path.join(trash_dir, os.path.basename(src)))
            except (OSError, shutil.Error):
                pass  # 文件移不走不阻塞删除记录（可能被占用，记录先清掉）
    conn.execute("DELETE FROM trip_attachments WHERE id=?", (att_id,))
    from system_ops import audit
    audit(conn, "trip_attachments", att_id, "(删除)", fp, None,
          f"删除附件·{ATT_CATEGORY_CN.get(a['category'], a['category'])}")
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/trips/<int:trip_id>/payments", methods=["POST"])
def add_payment(trip_id):
    conn = get_db()
    data = request.get_json(force=True) or {}
    cur = conn.execute(
        "INSERT INTO payments (trip_id, payer, payee, amount, screenshot_path, paid_at, status, pay_type, node_key) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            trip_id,
            data.get("payer"),
            data.get("payee"),
            data.get("amount"),
            data.get("screenshot_path"),
            data.get("paid_at"),
            data.get("status", "paid"),
            data.get("pay_type"),  # 旧字段，2026-09-18起新逻辑不再写入，仅历史兼容保留
            data.get("node_key"),  # 两票三款通用化：对应doc_panels里某个doc_types含payment的node_key，普通车次留空
        ),
    )
    conn.commit()
    return jsonify({"id": cur.lastrowid}), 201
