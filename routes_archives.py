"""基础档案 / 费率与关系 的增删改查。

从原 app.py 拆出（v2.0.1）。共用的常量和工具函数都在 core.py。"""
from core import *  # noqa: F401,F403


# ---------------------------------------------------------------------------
# 通用基础档案 API
# ---------------------------------------------------------------------------
@app.route("/api/<table>", methods=["GET", "POST"])
def archive_collection(table):
    cfg = ARCHIVE_TABLES.get(table)
    if not cfg:
        return jsonify({"error": "unknown table"}), 404
    if request.method == "GET":
        rows = query(f"SELECT * FROM {table} ORDER BY id DESC")
        return jsonify(rows_to_list(rows))
    data = request.get_json(force=True) or {}
    cols = [c for c in cfg if c in data]
    if not cols:
        return jsonify({"error": "empty payload"}), 400
    vals = [data[c] for c in cols]
    placeholders = ",".join(["?"] * len(cols))
    new_id = execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", vals)
    conn = get_db()
    audit(conn, table, new_id, "(新建)", None, data.get("name") or data.get("plate_no") or data.get("account_name") or f"#{new_id}")
    conn.commit()
    return jsonify({"id": new_id}), 201


@app.route("/api/<table>/<int:item_id>", methods=["GET", "PUT", "DELETE"])
def archive_item(table, item_id):
    cfg = ARCHIVE_TABLES.get(table)
    if not cfg:
        return jsonify({"error": "unknown table"}), 404
    if request.method == "GET":
        row = query(f"SELECT * FROM {table} WHERE id=?", (item_id,), one=True)
        return jsonify(row_to_dict(row))
    if request.method == "DELETE":
        conn = get_db()
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if row:
            title = row["name"] if "name" in row.keys() else (row["plate_no"] if "plate_no" in row.keys() else f"#{item_id}")
            system_ops.recycle(conn, table, item_id, f"{system_ops.TABLE_CN.get(table, table)} {title}")
            conn.commit()
        return jsonify({"ok": True, "recycled": True})
    data = request.get_json(force=True) or {}
    cols = [c for c in cfg if c in data]
    if not cols:
        return jsonify({"error": "empty payload"}), 400
    set_clause = ",".join(f"{c}=?" for c in cols)
    vals = [data[c] for c in cols] + [item_id]
    conn = get_db()
    audit_row_diff(conn, table, item_id, conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone(),
                   {c: data[c] for c in cols})
    execute(f"UPDATE {table} SET {set_clause} WHERE id=?", vals)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 费率与关系档案 API（rate_links：垫资费率/结算点位/调货员提成/服务费单价等
# 会随时间调整、且可能按实体两两组合区分的费率，统一走这里。基础CRUD复用上面的
# 通用 archive_collection/archive_item（已加入 ARCHIVE_TABLES），这里只补三个
# 通用CRUD覆盖不了的：目录/带自动截止的新增/关系图数据。）
# ---------------------------------------------------------------------------
@app.route("/api/rate_links/relation_types", methods=["GET"])
def rate_link_relation_types():
    return jsonify(engine.RELATION_TYPE_CATALOG)


@app.route("/api/rate_links/upsert", methods=["POST"])
def rate_link_upsert():
    data = request.get_json(force=True) or {}
    required = ["relation_type", "primary_entity_type", "rate_value", "effective_from"]
    missing = [f for f in required if data.get(f) in (None, "")]
    if missing:
        return jsonify({"error": f"缺少字段: {missing}"}), 400
    db = get_db()
    new_id = engine.upsert_rate(
        db,
        relation_type=data["relation_type"],
        primary_entity_type=data["primary_entity_type"],
        primary_entity_id=data.get("primary_entity_id") or None,
        rate_value=float(data["rate_value"]),
        effective_from=data["effective_from"],
        rate_unit=data.get("rate_unit") or "per_ton",
        secondary_entity_type=data.get("secondary_entity_type") or None,
        secondary_entity_id=data.get("secondary_entity_id") or None,
        notes=data.get("notes"),
    )
    audit(db, "rate_links", new_id, "rate_value", None,
          f"{data['relation_type']} {data['rate_value']} 自{data['effective_from']}起", "费率与关系·新增/续期")
    db.commit()
    return jsonify({"id": new_id}), 201


@app.route("/api/rate_links/graph", methods=["GET"])
def rate_link_graph():
    """给"关系图"页面用：把 rate_links(当前生效的部分) + 几条已有的静态归属关系
    (户头所属钢厂、反向自然人绑定的钢厂/供货厂) 一起拼成 nodes+edges，
    同一份 rate_links 数据既驱动引擎计算(engine.py)也驱动这张图，不会两边不一致。"""
    as_of = (request.args.get("as_of") or dt.date.today().isoformat())[:10]
    nodes = {}

    def add_node(etype, row, name_field="name"):
        if row is None:
            return None
        key = f"{etype}:{row['id']}"
        if key not in nodes:
            node = {"id": key, "type": etype, "entity_id": row["id"], "name": row[name_field]}
            # 2026-09-19：供货厂/基地里有一批是历史台账导入时把"调货员"名字误建成同名占位基地
            # (is_dispatcher_alias=1，见migrate_dispatcher_alias_bases.py)，前端据此加个小标签，
            # 提示这不是独立的供货厂主体，避免跟同名调货员混淆。
            if etype == "processing_base" and "is_dispatcher_alias" in row.keys() and row["is_dispatcher_alias"]:
                node["is_dispatcher_alias"] = True
            nodes[key] = node
        return key

    bases = {r["id"]: r for r in query("SELECT * FROM processing_bases")}
    dispatchers = {r["id"]: r for r in query("SELECT * FROM dispatchers")}
    accounts = {r["id"]: r for r in query("SELECT * FROM accounts")}
    sources = {r["id"]: r for r in query("SELECT * FROM invoice_sources")}
    mills = {r["id"]: r for r in query("SELECT * FROM steel_mills")}
    ENTITY_ROWS = {"processing_base": bases, "dispatcher": dispatchers, "account": accounts,
                   "invoice_source": sources, "steel_mill": mills}

    edges = []

    # 1) rate_links 里当前生效的记录：有secondary的画成两节点间的边；只有primary(单实体属性)的
    #    不单独占一个节点，而是作为primary节点上的一条"费率标签"附加信息返回给前端。
    rate_rows = query(
        "SELECT * FROM rate_links WHERE effective_from<=? AND (effective_to IS NULL OR effective_to>=?) "
        "ORDER BY relation_type, primary_entity_id, secondary_entity_id, effective_from DESC",
        (as_of, as_of),
    )
    catalog_by_code = {c["code"]: c for c in engine.RELATION_TYPE_CATALOG}
    seen_primary_secondary = set()  # 去重：同一primary+secondary+relation_type只取最新一条(SQL已按日期倒序)
    badges = {}  # node_key -> [ {label, rate_value, rate_unit}, ... ]
    for r in rate_rows:
        dedup_key = (r["relation_type"], r["primary_entity_id"], r["secondary_entity_id"])
        if dedup_key in seen_primary_secondary:
            continue
        seen_primary_secondary.add(dedup_key)
        cat = catalog_by_code.get(r["relation_type"], {})
        label = cat.get("label", r["relation_type"])
        unit_label = "元/吨" if r["rate_unit"] == "per_ton" else "%"
        p_row = ENTITY_ROWS.get(r["primary_entity_type"], {}).get(r["primary_entity_id"])
        p_key = add_node(r["primary_entity_type"], p_row) if p_row else None
        if r["secondary_entity_id"]:
            s_row = ENTITY_ROWS.get(r["secondary_entity_type"], {}).get(r["secondary_entity_id"])
            s_key = add_node(r["secondary_entity_type"], s_row) if s_row else None
            if p_key and s_key:
                edges.append({
                    "from": p_key, "to": s_key, "kind": "rate",
                    "label": f"{label} {r['rate_value']}{unit_label}",
                    "relation_type": r["relation_type"],
                })
        elif p_key:
            badges.setdefault(p_key, []).append(
                {"label": label, "rate_value": r["rate_value"], "unit": unit_label}
            )
        elif r["primary_entity_id"] is None:
            # 全局兜底档(如服务费40元/吨不挂具体票源)：不画边，挂在一个虚拟"全局"提示上，
            # 由前端在对应票源类型的说明区展示，这里直接原样返回，前端决定怎么呈现。
            edges.append({
                "from": None, "to": None, "kind": "global_fallback",
                "label": f"{label}(全局兜底) {r['rate_value']}{unit_label}",
                "relation_type": r["relation_type"],
            })

    # 2) 既有的静态归属关系，本来就存在于档案表里，不属于rate_links，但同属"业务关系图"该画的：
    for a in accounts.values():
        if a["mill_id"] and a["mill_id"] in mills:
            a_key = add_node("account", a)
            m_key = add_node("steel_mill", mills[a["mill_id"]])
            edges.append({"from": a_key, "to": m_key, "kind": "structural", "label": "所属"})
    for s in sources.values():
        s_key = None
        if s["bound_base_id"] and s["bound_base_id"] in bases:
            s_key = s_key or add_node("invoice_source", s)
            b_key = add_node("processing_base", bases[s["bound_base_id"]])
            edges.append({"from": s_key, "to": b_key, "kind": "structural", "label": "绑定供货厂"})
        if s["bound_mill_id"] and s["bound_mill_id"] in mills:
            s_key = s_key or add_node("invoice_source", s)
            m_key = add_node("steel_mill", mills[s["bound_mill_id"]])
            edges.append({"from": s_key, "to": m_key, "kind": "structural", "label": "绑定钢厂"})

    for key, tags in badges.items():
        nodes[key]["badges"] = tags

    return jsonify({"nodes": list(nodes.values()), "edges": edges})
