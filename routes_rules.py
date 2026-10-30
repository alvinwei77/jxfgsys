"""链条规则：模板、计算节点、口径编辑、链条图。

从原 app.py 拆出（v2.0.1）。共用的常量和工具函数都在 core.py。"""
from core import *  # noqa: F401,F403


@app.route("/api/flow_edge_hop_types", methods=["GET"])
def list_hop_types():
    """链条规则流程边的环节类型目录（开票/打款/出结算单/做台账/过磅单/发货）。
    前端下拉框、关系图配色都拉这个接口，唯一权威定义在 engine.HOP_TYPE_CATALOG，
    以后加类型只改那一处。"""
    return jsonify(engine.HOP_TYPE_CATALOG)


@app.route("/api/flow_templates", methods=["GET"])
def list_flow_templates():
    rows = query("""SELECT ft.*, (SELECT COUNT(*) FROM trips t WHERE t.template_id=ft.id AND t.status!='voided') AS trip_count
                     FROM flow_templates ft ORDER BY ft.id""")
    return jsonify(rows_to_list(rows))


@app.route("/api/flow_templates", methods=["POST"])
def create_flow_template():
    data = request.get_json(force=True) or {}
    code, name, calc_family = data.get("code"), data.get("name"), data.get("calc_family")
    if not code or not name or calc_family not in ("split", "service_fee"):
        return jsonify({"error": "code/name必填，calc_family必须是split或service_fee"}), 400
    conn = get_db()
    if conn.execute("SELECT 1 FROM flow_templates WHERE code=?", (code,)).fetchone():
        return jsonify({"error": f"链条规则代码'{code}'已存在"}), 400
    cur = conn.execute(
        "INSERT INTO flow_templates (code,name,short_name,calc_family,description,is_active) VALUES (?,?,?,?,?,1)",
        (code, name, data.get("short_name") or name[:4], calc_family, data.get("description")),
    )
    conn.commit()
    return jsonify({"id": cur.lastrowid, "ok": True})


@app.route("/api/flow_templates/<int:tid>", methods=["PUT"])
def update_flow_template(tid):
    """只能改展示用字段(name/short_name/description/is_active)，code和calc_family创建后不可改——
    code被迁移脚本/engine.MODE_TO_TEMPLATE_CODE引用，calc_family决定走哪套字段映射
    (engine.compute_trip_finance_v2_flat)，改了会导致历史车次的口径对不上。"""
    data = request.get_json(force=True) or {}
    cols = [c for c in ("name", "short_name", "description", "is_active") if c in data]
    if not cols:
        return jsonify({"error": "empty payload"}), 400
    conn = get_db()
    set_clause = ",".join(f"{c}=?" for c in cols)
    audit_row_diff(conn, "flow_templates", tid, conn.execute("SELECT * FROM flow_templates WHERE id=?", (tid,)).fetchone(),
                   {c: data[c] for c in cols})
    conn.execute(f"UPDATE flow_templates SET {set_clause} WHERE id=?", [data[c] for c in cols] + [tid])
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/flow_templates/<int:tid>/nodes", methods=["GET"])
def list_template_nodes(tid):
    rows = query("SELECT * FROM template_nodes WHERE template_id=? ORDER BY seq", (tid,))
    out = rows_to_list(rows)
    for r in out:
        r["doc_types"] = json.loads(r["doc_types"]) if r["doc_types"] else []
    return jsonify(out)


@app.route("/api/flow_templates/<int:tid>/formula", methods=["GET"])
def template_formula(tid):
    conn = get_db()
    nodes = _describe_template(conn, tid)
    sample = conn.execute(
        """SELECT * FROM trips WHERE template_id=? AND status!='voided' AND settlement_tonnage_total>0
           ORDER BY coalesce(settlement_date, order_date) DESC, id DESC LIMIT 1""", (tid,)).fetchone()
    ex = None
    if sample:
        r = engine._run_template(conn, sample, _stored_overrides(sample))
        if r:
            rates = {i["node_key"]: i for i in r["rate_items"]}
            ex = {"trip_id": sample["id"], "plate_no": sample["plate_no"], "date": sample["settlement_date"],
                  "tonnage": r["tonnage"], "settlement_amount": r["settlement_amount"],
                  "values": {k: round(v, 2) for k, v in r["node_values"].items()},
                  "rates": {k: {"value": v["value"], "unit": v["unit"]} for k, v in rates.items()}}
    cat = [c for c in engine.RELATION_TYPE_CATALOG]
    locked = conn.execute("""SELECT COUNT(*) FROM trips t JOIN trip_finance f ON f.trip_id=t.id
                             WHERE t.template_id=? AND t.status!='voided' AND f.is_locked=1""", (tid,)).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM trips WHERE template_id=? AND status!='voided'", (tid,)).fetchone()[0]
    return jsonify({"nodes": nodes, "example": ex, "relation_catalog": cat, "trip_count": total, "locked_count": locked})


@app.route("/api/template_nodes/<int:node_id>/rule", methods=["POST"])
def change_node_rule(node_id):
    """{calc_rule_type, calc_rule_config, confirm}：confirm 为假只预览（改完立即回滚），为真才保存并记操作日志。"""
    conn = get_db()
    node = conn.execute("SELECT * FROM template_nodes WHERE id=?", (node_id,)).fetchone()
    if not node:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True) or {}
    try:
        rule, cfg_json = _validate_rule_change(conn, node, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    tid = node["template_id"]
    before = _template_profit_snapshot(conn, tid)
    conn.execute("UPDATE template_nodes SET calc_rule_type=?, calc_rule_config=? WHERE id=?", (rule, cfg_json, node_id))
    after = _template_profit_snapshot(conn, tid)
    locked = {r[0] for r in conn.execute("SELECT trip_id FROM trip_finance WHERE is_locked=1")}
    changed = [{"trip_id": k, "before": round(before[k], 2), "after": round(after.get(k, 0), 2), "locked": k in locked}
               for k in before if abs(before[k] - after.get(k, 0)) > 0.005]
    summary = {"trip_count": len(before), "changed_count": len(changed),
               "changed_locked": sum(1 for c in changed if c["locked"]),
               "profit_before": round(sum(before.values()), 2), "profit_after": round(sum(after.values()), 2),
               "changed": sorted(changed, key=lambda c: -abs(c["after"] - c["before"]))[:20]}
    if data.get("confirm"):
        conn.execute("INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, note) VALUES (?,?,?,?,?,?)",
                     ("template_nodes", node_id, "calc_rule", json.dumps({"type": node["calc_rule_type"], "config": node["calc_rule_config"]}, ensure_ascii=False),
                      json.dumps({"type": rule, "config": cfg_json}, ensure_ascii=False), "链条规则页·改计算口径"))
        conn.commit()
        import regression  # 改口径前已经看过影响预览，这条规则下的车次利润核对基准直接以新结果为准
        regression.accept(conn, template_id=tid)
        summary["saved"] = True
    else:
        conn.rollback()
        summary["saved"] = False
    return jsonify(summary)


@app.route("/api/template_nodes/<int:node_id>/rule_history", methods=["GET"])
def node_rule_history(node_id):
    rows = rows_to_list(get_db().execute(
        "SELECT id, old_value, new_value, changed_at FROM audit_log WHERE table_name='template_nodes' AND record_id=? "
        "AND field_name='calc_rule' ORDER BY id DESC LIMIT 10", (node_id,)))
    return jsonify(rows)


@app.route("/api/template_nodes/<int:node_id>", methods=["PUT"])
def update_template_node(node_id):
    """只开放label/role_type/doc_types可编辑——这三个纯展示用，不影响计算。
    calc_rule_type/calc_rule_config/seq不开放编辑：seq决定节点求值顺序(后面的节点可能靠
    node:xxx引用前面节点的值)，calc_rule_*是实际算钱的公式，这两类改错了会直接算错利润，
    需要改的话目前仍走改代码+跑compare_v2_flat_fields.py全字段回归验证这条老路。"""
    data = request.get_json(force=True) or {}
    fields = {}
    if "label" in data:
        fields["label"] = data["label"]
    if "role_type" in data:
        fields["role_type"] = data["role_type"]
    if "doc_types" in data:
        fields["doc_types"] = json.dumps(data["doc_types"]) if data["doc_types"] else None
    if not fields:
        return jsonify({"error": "只能修改label/role_type/doc_types"}), 400
    conn = get_db()
    set_clause = ",".join(f"{c}=?" for c in fields)
    conn.execute(f"UPDATE template_nodes SET {set_clause} WHERE id=?", list(fields.values()) + [node_id])
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/flow_templates/<int:tid>/flow_edges", methods=["GET"])
def list_flow_edges(tid):
    rows = query("SELECT * FROM template_flow_edges WHERE template_id=? ORDER BY seq", (tid,))
    return jsonify(rows_to_list(rows))


@app.route("/api/flow_templates/<int:tid>/flow_edges", methods=["POST"])
def create_flow_edge(tid):
    data = request.get_json(force=True) or {}
    required = ["from_role_type", "from_label", "to_role_type", "to_label", "hop_type"]
    if any(not data.get(k) for k in required):
        return jsonify({"error": f"缺少必填字段: {', '.join(required)}"}), 400
    if data["hop_type"] not in engine.HOP_TYPE_CODES:
        return jsonify({"error": f"hop_type必须是以下之一: {', '.join(sorted(engine.HOP_TYPE_CODES))}"}), 400
    conn = get_db()
    max_seq = conn.execute(
        "SELECT COALESCE(MAX(seq),0) m FROM template_flow_edges WHERE template_id=?", (tid,)
    ).fetchone()["m"]
    cols = ["template_id", "seq"] + [c for c in EDGE_EDITABLE_COLS if c != "seq"]
    vals = [tid, data.get("seq") or max_seq + 1] + [
        (data.get(c) or None) for c in EDGE_EDITABLE_COLS if c != "seq"
    ]
    cur = conn.execute(
        f"INSERT INTO template_flow_edges ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})",
        vals,
    )
    conn.commit()
    return jsonify({"id": cur.lastrowid, "ok": True})


@app.route("/api/flow_edges/<int:edge_id>", methods=["PUT"])
def update_flow_edge(edge_id):
    data = request.get_json(force=True) or {}
    cols = [c for c in EDGE_EDITABLE_COLS if c in data]
    if not cols:
        return jsonify({"error": "empty payload"}), 400
    if "hop_type" in data and data["hop_type"] not in engine.HOP_TYPE_CODES:
        return jsonify({"error": f"hop_type必须是以下之一: {', '.join(sorted(engine.HOP_TYPE_CODES))}"}), 400
    conn = get_db()
    # 用户动过这条边 = 已人工核实，自动摘掉"待核实"标记
    set_clause = ",".join(f"{c}=?" for c in cols) + ", is_auto=0"
    conn.execute(f"UPDATE template_flow_edges SET {set_clause} WHERE id=?", [data[c] for c in cols] + [edge_id])
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/flow_edges/<int:edge_id>", methods=["DELETE"])
def delete_flow_edge(edge_id):
    conn = get_db()
    conn.execute("DELETE FROM template_flow_edges WHERE id=?", (edge_id,))
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/flow_edge_endpoint_options", methods=["GET"])
def flow_edge_endpoint_options():
    """环节起点/终点的下拉选项。原来要先选"角色"再手打"名称"两步，现在合并成一个下拉，
    分三组，语义上正好是这一端的三种形态：
      role   角色占位——车次图里自动换成该车次的真实主体（一条规则覆盖很多车次时应该用这个）
      entity 指定具体对象——固定就是档案里的某一个，改名图上自动跟着变
      custom 自定义文字——台账/最终客户这类不在档案里的概念
    """
    conn = get_db()
    groups = [{"group": "按角色（车次图里自动换成该车次的真实主体）", "kind": "role", "items": [
        {"value": f"role:{r}", "role_type": r, "label": f"这笔车次的{ROLE_TYPE_META_CN[r]}"}
        for r in ROLE_ENTITY_TABLE
    ]}]
    ent_items = []
    for role, (tbl, etype) in ROLE_ENTITY_TABLE.items():
        for r in conn.execute(f"SELECT id, name FROM {tbl} ORDER BY id"):
            ent_items.append({
                "value": f"entity:{role}:{r['id']}", "role_type": role,
                "entity_type": etype, "entity_id": r["id"],
                "label": f"{ROLE_TYPE_META_CN[role]}·{r['name']}", "name": r["name"],
            })
    groups.append({"group": "指定具体对象（固定不变的那一方）", "kind": "entity", "items": ent_items})
    groups.append({"group": "自定义文字", "kind": "custom", "items": [
        {"value": f"custom:{r}", "role_type": r, "label": f"自定义（按{ROLE_TYPE_META_CN[r]}配色）"}
        for r in ("output", "soe", "supplier", "our_account", "ticket_source", "mill", "dispatcher")
    ]})
    return jsonify(groups)


@app.route("/api/flow_templates/<int:tid>/flow_edges/auto_generate", methods=["POST"])
def auto_generate_flow_edges(tid):
    """一键按计算节点推导出一版流程边（用户要的"自动 + 可二次编辑"里的"自动"那半）。

    推导规则跟当初 migrate_flow_edges.py 一样：按 template_nodes 的 seq 顺序走，相邻两个
    role_type 不同就连一条边，节点 doc_types 含 invoice/payment 就用对应类型、都没有就用 goods，
    并把这条边关联到那个计算节点(node_key_ref)——所以生成出来的图天然就跟算钱的口径对得上。
    跟迁移脚本的区别：这里一律用**角色占位**(不绑定具体实体、label 用角色名)，而不是手打泛称；
    生成的边标 is_auto=1，页面上显示"待核实"，用户改过任意一条就自动转成已确认。

    body: {"replace": true} 时先清空现有边（默认 false，只在没有边时生成，避免手滑覆盖）。
    """
    conn = get_db()
    tpl = conn.execute("SELECT * FROM flow_templates WHERE id=?", (tid,)).fetchone()
    if not tpl:
        return jsonify({"error": "not found"}), 404
    replace = bool((request.get_json(silent=True) or {}).get("replace"))
    existing = conn.execute(
        "SELECT COUNT(*) c FROM template_flow_edges WHERE template_id=?", (tid,)
    ).fetchone()["c"]
    if existing and not replace:
        return jsonify({"error": f"这条规则已经有 {existing} 个环节了，要重新生成请先确认覆盖"}), 400

    nodes = conn.execute(
        "SELECT * FROM template_nodes WHERE template_id=? ORDER BY seq", (tid,)
    ).fetchall()
    edges, seq, prev_role = [], 1, None
    for n in nodes:
        role = n["role_type"] or "output"
        if prev_role and prev_role != role:
            doc_types = json.loads(n["doc_types"]) if n["doc_types"] else []
            hops = [h for h in ("invoice", "payment") if h in doc_types] or ["goods"]
            for hop in hops:
                edges.append((tid, seq, prev_role, ROLE_TYPE_META_CN.get(prev_role, prev_role),
                              role, ROLE_TYPE_META_CN.get(role, role), hop, n["node_key"],
                              f"自动推导：{n['label']}"))
                seq += 1
        prev_role = role

    if replace:
        conn.execute("DELETE FROM template_flow_edges WHERE template_id=?", (tid,))
    conn.executemany(
        """INSERT INTO template_flow_edges
           (template_id,seq,from_role_type,from_label,to_role_type,to_label,hop_type,node_key_ref,notes,is_auto)
           VALUES (?,?,?,?,?,?,?,?,?,1)""",
        edges,
    )
    conn.commit()
    return jsonify({"ok": True, "generated": len(edges)})


@app.route("/api/flow_templates/<int:tid>/flow_edges/reorder", methods=["POST"])
def reorder_flow_edges(tid):
    """纯展示排序，不影响计算，可以放心让用户在页面上拖拽调整。
    (template_id,seq)有UNIQUE约束，直接按新序号挨个UPDATE会在中间状态撞车(比如两条记录
    互换seq，先改第一条就会跟还没改的第二条暂时重复)，所以先统一挪到负数区间占位，
    避开新旧序号有交集的情况，再统一写回正式序号。"""
    data = request.get_json(force=True) or {}
    order = data.get("order") or []
    conn = get_db()
    for i, edge_id in enumerate(order, start=1):
        conn.execute(
            "UPDATE template_flow_edges SET seq=? WHERE id=? AND template_id=?", (-i, edge_id, tid)
        )
    for i, edge_id in enumerate(order, start=1):
        conn.execute(
            "UPDATE template_flow_edges SET seq=? WHERE id=? AND template_id=?", (i, edge_id, tid)
        )
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/flow_templates/<int:tid>/graph", methods=["GET"])
def flow_template_graph(tid):
    """链条规则页面用：模板层面的抽象流程图(不挂具体车次，label就是编辑器里录入的
    示例/角色名字)。"""
    conn = get_db()
    tpl = conn.execute("SELECT * FROM flow_templates WHERE id=?", (tid,)).fetchone()
    if not tpl:
        return jsonify({"error": "not found"}), 404
    path = request.args.get("path", "all")
    graph = _build_flow_graph(conn, tid, path)
    graph["template"] = row_to_dict(tpl)
    return jsonify(graph)


@app.route("/api/trips/<int:trip_id>/flow_graph", methods=["GET"])
def trip_flow_graph(trip_id):
    """车次详情页用：这笔车次实际链条规则的流程图，party名字替换成车次的真实供货厂/调货员/
    户头/票源方/钢厂，边上带这笔车次的真实计算数值——保证关系图和结算/利润过程对得上。"""
    conn = get_db()
    trip = conn.execute(
        """SELECT t.*, m.name AS mill_name, a.name AS account_name, b.name AS base_name,
                  b.is_dispatcher_alias AS base_is_dispatcher_alias,
                  d.name AS dispatcher_name, s.name AS invoice_source_name
           FROM trips t
           LEFT JOIN steel_mills m ON t.mill_id = m.id
           LEFT JOIN accounts a ON t.account_id = a.id
           LEFT JOIN processing_bases b ON t.base_id = b.id
           LEFT JOIN dispatchers d ON t.dispatcher_id = d.id
           LEFT JOIN invoice_sources s ON t.invoice_source_id = s.id
           WHERE t.id=?""",
        (trip_id,),
    ).fetchone()
    if not trip:
        return jsonify({"error": "not found"}), 404
    trip = dict(trip)
    if not trip.get("template_id"):
        return jsonify({"error": "该车次尚未关联链条规则(template_id)"}), 400
    # 这笔车次的"供货厂"如果其实就是调货员本人的同名占位记录(is_dispatcher_alias)，画图时
    # 不要把supplier和dispatcher画成两个同名但角色不同的节点(正是"闫东朋/林凯看着像有关系"
    # 那个bug的车次级翻版)，直接把supplier角色归并到dispatcher节点上。
    role_alias = {}
    if trip.get("base_is_dispatcher_alias") and trip.get("base_name") == trip.get("dispatcher_name"):
        role_alias["supplier"] = "dispatcher"
    path = request.args.get("path", "all")
    try:
        v2 = engine.compute_trip_finance_v2(conn, trip)
        baselines = v2["nodes"] if v2 else {}
    except Exception:
        baselines = {}
    tnv_rows = {
        r["node_key"]: r for r in conn.execute(
            "SELECT * FROM trip_node_values WHERE trip_id=?", (trip_id,)
        ).fetchall()
    }
    graph = _build_flow_graph(conn, trip["template_id"], path, trip=trip,
                               node_baselines=baselines, tnv_rows=tnv_rows, role_alias=role_alias)
    tpl = conn.execute("SELECT * FROM flow_templates WHERE id=?", (trip["template_id"],)).fetchone()
    graph["template"] = row_to_dict(tpl) if tpl else None
    return jsonify(graph)


@app.route("/api/trip_modes", methods=["GET"])
def list_trip_modes():
    rows = query("SELECT * FROM trip_modes ORDER BY code")
    return jsonify(rows_to_list(rows))


@app.route("/api/trip_modes/<code>", methods=["PUT"])
def update_trip_mode(code):
    data = request.get_json(force=True) or {}
    cols = [c for c in ["name", "description", "calc_type"] if c in data]
    if not cols:
        return jsonify({"error": "empty payload"}), 400
    set_clause = ",".join(f"{c}=?" for c in cols)
    vals = [data[c] for c in cols] + [code]
    execute(f"UPDATE trip_modes SET {set_clause} WHERE code=?", vals)
    return jsonify({"ok": True})
