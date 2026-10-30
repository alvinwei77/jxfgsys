import os
import io
import json
import sqlite3
import datetime as dt
from flask import Flask, g, jsonify, request, render_template, send_from_directory, send_file

from db import get_db, close_db, query, execute, row_to_dict, rows_to_list
import calc
import engine
import lanes
import system_ops
from system_ops import audit, audit_row_diff

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

app = Flask(__name__)
app.teardown_appcontext(close_db)
app.register_blueprint(lanes.bp)  # B阶段：三条线/批次/回款追踪（lanes.py）
app.register_blueprint(system_ops.bp)  # v2：备份/操作留痕/回收站（system_ops.py）
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.jinja_env.auto_reload = True


@app.after_request
def _no_cache(resp):
    # 本地单机小系统：静态资源/页面一律不缓存，避免每次改完代码后浏览器仍显示旧版本
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

# ---------------------------------------------------------------------------
# 基础档案：表名 -> 允许读写的字段列表（白名单，防止任意字段/SQL注入）
# ---------------------------------------------------------------------------
ARCHIVE_TABLES = {
    "steel_mills": ["name", "is_whitelist", "settlement_terms", "quality_contact", "entry_requirement", "notes"],
    "financiers": ["name", "credit_info", "notes"],
    "accounts": ["name", "mill_id", "financier_id", "is_our_own", "advance_fee_rate", "external_settlement_rate", "notes"],
    "processing_bases": ["name", "base_type", "admission_level", "risk_approved", "default_mode_code", "service_fee_type", "service_fee_value", "contact", "notes"],
    "dispatchers": ["name", "phone", "commission_per_ton", "notes"],
    "invoice_sources": ["name", "source_type", "tax_type", "rate_point", "settlement_type", "outbound_rate_point", "quota_period", "quota_amount", "warn_threshold", "bound_mill_id", "bound_base_id", "contact", "notes"],
    "drivers": ["name", "phone", "id_card_no", "license_photo_path", "id_card_photo_path", "license_expiry", "notes"],
    "vehicles": ["plate_no", "transport_type", "default_driver_id", "vehicle_license_photo_path", "env_list_photo_path", "is_first_trailer_checked", "notes"],
    "scrap_types": ["category", "name", "example_desc", "size_requirement", "thickness_requirement"],
    "mill_reward_tiers": ["mill_id", "min_tonnage", "unit_price", "period_start_day", "period_end_day", "notes"],
    # B阶段：供货厂收款账户（老板管理的卡，可能不是本人的；反向走个人户）
    "base_accounts": ["base_id", "account_name", "bank", "card_tail", "holder_type", "is_active", "notes"],
    "rate_links": ["relation_type", "primary_entity_type", "primary_entity_id", "secondary_entity_type",
                   "secondary_entity_id", "rate_value", "rate_unit", "effective_from", "effective_to", "notes"],
}

TRIP_FIELDS = [
    "transport_type", "status", "mode_code", "order_date", "base_id", "dispatcher_id", "mill_id", "account_id",
    "invoice_source_id", "vehicle_id", "plate_no", "driver_id", "driver_name", "driver_phone", "driver_id_card",
    "departure_place", "expected_arrival", "remarks", "mixed_material", "water_tank_confirmed",
    "cutting_fee_total", "pay_to_base_amount", "other_cost", "settlement_doc_no", "settlement_doc_path",
    "settlement_date", "is_paid", "paid_amount", "payment_screenshot_path",
    # 两票三款核对（2026-09-17新增，仅"经过我们"票源方如益周使用）：
    # 销项发票（我们开给欧冶）+ 进项发票（前端开给我们），各自号码/金额；
    # 进项票因票源方票额度不足暂时开不出时，用 invoice_in_stock 标记"库存中"。
    "invoice_out_no", "invoice_out_amount", "invoice_in_no", "invoice_in_amount", "invoice_in_stock",
    # 2026-09-19：界面上"结算模式"和"链条规则"合并成一个入口后，前端直接传 template_id；
    # mode_code 由 _sync_mode_code_from_template 反推着写回去，两者始终保持一致。
    "template_id",
    # A阶段(2026-09-21)：每个状态一个日期，推进时为空自动填今天（见 STATUS_DATE_FIELD）
    "arrived_date", "invoiced_date", "closed_date",
    # B阶段：开票路径 self/source/ours，空=按链条规则+票源自动推断（lanes.default_invoice_path）
    "invoice_path",
]

# 状态简化（2026-09-17）：原11个状态精简为6个，旧状态数据通过一次性迁移脚本归并：
# 待报备/在途 -> 已报备；已过毛磅/已过皮磅 -> 已到厂；已回款 -> 已开票（回款信息改为
# "已开票"状态下的打款记录字段，不再单列状态）。数据库层 CHECK 约束暂保留旧的更宽的取值范围
# （未做表重建，风险更低），仅应用层（本列表）收紧为6项，新建/修改车次只会写入这6个值。
STATUS_LIST = [
    ("reported", "已报备"), ("arrived", "已到厂"), ("settled", "已结算"),
    ("invoiced", "已开票"), ("closed", "已结案"), ("voided", "作废"),
]

# 状态 → 对应日期字段(A阶段 2026-09-21)。推进到某状态时，该字段为空就填今天；回退状态不清空。
# 只填目标状态自己的日期，跳过的中间状态不补（不知道真实日期就留空，不瞎编）。
STATUS_DATE_FIELD = {
    "reported": "order_date", "arrived": "arrived_date", "settled": "settlement_date",
    "invoiced": "invoiced_date", "closed": "closed_date",
}
STATUS_LABEL = dict(STATUS_LIST)


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------
@app.route("/")
def page_dashboard():
    return render_template("dashboard.html", active="dashboard")


@app.route("/trips")
def page_trips():
    return render_template("trips.html", status_list=STATUS_LIST, active="trips")


@app.route("/archives")
def page_archives():
    return render_template("archives.html", active="archives")


@app.route("/quotes")
def page_quotes():
    return render_template("quotes.html", active="quotes")


@app.route("/reports")
def page_reports():
    return render_template("reports.html", active="reports")


@app.route("/rate_links")
def page_rate_links():
    return render_template("rate_links.html", active="rate_links")


@app.route("/system")
def page_system():
    return render_template("system.html", active="system")


@app.route("/tracking")
def page_tracking():
    return render_template("tracking.html", active="tracking")


@app.route("/chain_rules")
def page_chain_rules():
    return render_template("chain_rules.html", active="chain_rules")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


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
    today = dt.date.today().isoformat()
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
        (today, today),
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


# ---------------------------------------------------------------------------
# 链条规则（flow_templates + template_flow_edges，2026-09-19新增）
# ---------------------------------------------------------------------------
# 跟上面"费率与关系"(rate_links)是两套完全独立的东西：那边是"实体两两之间的费率数值"，
# 这边是"一条业务流程从头到尾怎么走"(票商->俊兴->欧冶->华西特钢这种)。一个flow_template
# = 一条链条规则，车次选哪条链条规则(trips.template_id)结算/利润就按哪条算(已在v2引擎切换
# 时接好，见_compute_finance)；template_flow_edges只是这条规则的"流程图描述"，可以自由编辑，
# 不影响计算(计算逻辑仍然是calc_rule_type/calc_rule_config，本次不开放编辑，避免打乱节点间
# 引用顺序导致算错)。

ROLE_TYPE_TRIP_FIELD = {  # 渲染某一笔具体车次的链条图时，用车次的真实主体名替换模板里的通用/示例名字
    "supplier": "base_name",
    "dispatcher": "dispatcher_name",
    "our_account": "account_name",
    "ticket_source": "invoice_source_name",
    "mill": "mill_name",
}


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


# ---------------------------------------------------------------------------
# 计算公式"读得懂版"（2026-09-21）：用户反馈计算节点口径看不懂也不会改。
# 把每个节点翻译成一句中文公式（①②③编号互相引用），标明费率从哪来、去哪改，
# 用该链条最近一车代入真实数字；只开放两类安全的口径修改，并且先预览影响再保存：
#   1) 费率节点：改"费率从哪来"——费率与关系里的某类费率(可选按票源细分) / 固定值
#   2) 净利润(汇总)节点：每一项选 加 / 减 / 不计入
# 其余节点(引用关系、单据核对等)只读，改错了会直接算错钱。
# ---------------------------------------------------------------------------
REF_CN = {"trip:settlement_amount": "结算金额", "trip:tonnage": "吨位", "trip:account_id": "户头",
          "trip:dispatcher_id": "调货人", "trip:invoice_source_id": "票源方"}
ENTITY_CN = {"account": "户头", "dispatcher": "调货人", "invoice_source": "票源方", "mill": "钢厂", "base": "供货厂"}
TRIP_FIELD_CN = {"cutting_fee_total": "切割费", "other_cost": "其他成本"}
EDITABLE_RATE_TYPES = {"per_ton": ("per_ton_fixed", "per_ton_rate_schedule"),
                       "pct": ("rate_deduct_fixed", "rate_deduct_rate_schedule")}


def _describe_template(conn, tid):
    nodes = rows_to_list(conn.execute("SELECT * FROM template_nodes WHERE template_id=? ORDER BY seq", (tid,)))
    num = {n["node_key"]: i + 1 for i, n in enumerate(nodes)}
    cat = {c["code"]: c for c in engine.RELATION_TYPE_CATALOG}

    def ref(r):
        if r.startswith("node:"):
            k = r[5:]
            n = next((x for x in nodes if x["node_key"] == k), None)
            return {"t": "node", "n": num.get(k), "label": n["label"] if n else k}
        return {"t": "field", "label": REF_CN.get(r, r)}

    def txt(s):
        return {"t": "text", "label": s}

    def rate_src(cfg):
        c = cat.get(cfg.get("relation_type"), {})
        by = ENTITY_CN.get(cfg.get("primary_type"), "")
        if cfg.get("secondary_type"):
            by += "（可按" + ENTITY_CN.get(cfg["secondary_type"], "") + "细分）"
        return {"kind": "rate_links", "label": c.get("label", cfg.get("relation_type")), "by": by, "href": "/rate_links"}

    out = []
    for n in nodes:
        rule = n["calc_rule_type"]
        cfg = json.loads(n["calc_rule_config"] or "{}")
        f, src, edit = [], None, None
        if rule == "per_ton_fixed":
            f = [ref("trip:tonnage"), txt("×"), txt(f"{cfg.get('unit_price', 0):g} 元/吨")]
            src = {"kind": "fixed", "label": "固定值（写在链条规则里）"}
            edit = "rate_per_ton"
        elif rule == "per_ton_rate_schedule":
            s_ = rate_src(cfg)
            f = [ref("trip:tonnage"), txt("×"), txt(s_["label"])]
            src, edit = s_, "rate_per_ton"
        elif rule == "per_ton_reward_tier":
            f = [ref("trip:tonnage"), txt("×"), txt("量补单价")]
            src = {"kind": "tiers", "label": "钢厂量补阶梯（按本周期累计吨位定档）", "href": "/archives?tab=mill_reward_tiers"}
        elif rule == "rate_deduct_fixed":
            f = [ref(cfg["input"]), txt("×（1 −"), txt(f"{cfg.get('rate_pct', 0):g}%"), txt("）")]
            src, edit = {"kind": "fixed", "label": "固定值（写在链条规则里）"}, "rate_pct"
        elif rule == "rate_deduct_rate_schedule":
            s_ = rate_src(cfg)
            f = [ref(cfg["input"]), txt("×（1 −"), txt(s_["label"]), txt("）")]
            src, edit = s_, "rate_pct"
        elif rule == "rate_deduct_invoice_source":
            f = [ref(cfg["input"]), txt("×（1 − 票源成本点位）")]
            src = {"kind": "source", "label": "票源方档案里的点位", "href": "/archives?tab=invoice_sources"}
        elif rule == "subtract_nodes":
            f = [ref(cfg["minuend"]), txt("−"), ref(cfg["subtrahend"])]
        elif rule == "rate_spread_invoice_source":
            f = [ref(cfg["input"]), txt("×（转出点位 − 进项点位），只有“经过我们”的票源方才有")]
            src = {"kind": "source", "label": "票源方档案里的进项/转出点位", "href": "/archives?tab=invoice_sources"}
        elif rule == "manual":
            fl = cfg.get("trip_fields") or []
            f = [txt("车次上手填：" + " + ".join(TRIP_FIELD_CN.get(x, x) for x in fl))] if fl else [txt("不计算，只记录实际打款")]
        elif rule == "ref_value":
            f = [txt("等于"), ref(cfg["input"]), txt("（开票/打款核对用的基准）")]
        elif rule == "sum_formula":
            for i, term in enumerate(cfg.get("terms", [])):
                sign = term.get("sign", 1)
                if i or sign < 0:
                    f.append(txt("+" if sign > 0 else "−"))
                f.append(ref("node:" + term["node"]))
            edit = "terms"
        out.append({"id": n["id"], "n": num[n["node_key"]], "node_key": n["node_key"], "label": n["label"], "rule": rule,
                    "config": cfg, "formula": f, "source": src, "edit": edit,
                    "is_total": rule == "sum_formula" and not n["is_reserved_component"],
                    "is_reserved": bool(n["is_reserved_component"]),
                    "doc_types": json.loads(n["doc_types"]) if n["doc_types"] else []})
    return out


def _template_profit_snapshot(conn, tid):
    """该链条所有(未作废)车次按当前口径实时算一遍：{trip_id: net_profit}。已固化车次也实时算，
    用来看"口径改了会差多少"；真正的统计里已固化车次仍取快照，不会被口径修改改掉。"""
    res = {}
    for t in conn.execute("SELECT * FROM trips WHERE template_id=? AND status!='voided'", (tid,)).fetchall():
        res[t["id"]] = (_compute_finance(conn, t) or {}).get("net_profit") or 0
    return res


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


def _validate_rule_change(conn, node, data):
    """只允许两类修改；返回 (rule_type, config_json) 或抛 ValueError。"""
    old_rule = node["calc_rule_type"]
    old_cfg = json.loads(node["calc_rule_config"] or "{}")
    new_rule = data.get("calc_rule_type")
    cfg = data.get("calc_rule_config") or {}
    cat = {c["code"]: c for c in engine.RELATION_TYPE_CATALOG}
    if old_rule == "sum_formula":
        if new_rule != "sum_formula":
            raise ValueError("汇总节点只能改各项加减")
        before = {r["node_key"] for r in conn.execute(
            "SELECT node_key FROM template_nodes WHERE template_id=? AND seq<?", (node["template_id"], node["seq"]))}
        terms = []
        for tm in cfg.get("terms") or []:
            if tm.get("node") not in before or tm.get("sign") not in (1, -1):
                raise ValueError(f"无效的项：{tm}")
            terms.append({"node": tm["node"], "sign": tm["sign"]})
        if not terms:
            raise ValueError("至少要有一项")
        return "sum_formula", json.dumps({"terms": terms}, ensure_ascii=False)
    unit = next((u for u, types in EDITABLE_RATE_TYPES.items() if old_rule in types), None)
    if not unit or new_rule not in EDITABLE_RATE_TYPES[unit]:
        raise ValueError("这个节点的口径不能在这里改")
    base = {"input": old_cfg["input"]} if unit == "pct" else {}
    if new_rule in ("per_ton_fixed", "rate_deduct_fixed"):
        key = "unit_price" if unit == "per_ton" else "rate_pct"
        try:
            v = float(cfg.get(key))
        except (TypeError, ValueError):
            raise ValueError("请填固定值")
        return new_rule, json.dumps({**base, key: v}, ensure_ascii=False)
    c = cat.get(cfg.get("relation_type"))
    if not c or c["rate_unit"] != unit:
        raise ValueError("费率类型不匹配（元/吨 和 % 不能混用）")
    ref_of = {"account": "trip:account_id", "dispatcher": "trip:dispatcher_id", "invoice_source": "trip:invoice_source_id"}
    new_cfg = {**base, "relation_type": c["code"], "primary_type": c["primary_type"], "primary_ref": ref_of[c["primary_type"]]}
    if cfg.get("by_secondary") and c.get("secondary_type"):
        new_cfg.update({"secondary_type": c["secondary_type"], "secondary_ref": ref_of[c["secondary_type"]]})
    return new_rule, json.dumps(new_cfg, ensure_ascii=False)


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


EDGE_EDITABLE_COLS = ("from_role_type", "from_label", "from_entity_type", "from_entity_id",
                       "to_role_type", "to_label", "to_entity_type", "to_entity_id",
                       "hop_type", "node_key_ref", "notes", "seq")


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


ROLE_TYPE_META_CN = {
    "supplier": "供货厂", "dispatcher": "调货员", "our_account": "我方户头",
    "ticket_source": "票源方", "soe": "国企", "mill": "钢厂", "output": "结果",
}


# role_type -> (档案表, 实体类型代码)。只有这5个角色能"指定具体对象"，也只有它们能在车次图里
# 被替换成该车次的真实主体（跟 ROLE_TYPE_TRIP_FIELD 一一对应）。
ROLE_ENTITY_TABLE = {
    "supplier": ("processing_bases", "processing_base"),
    "dispatcher": ("dispatchers", "dispatcher"),
    "ticket_source": ("invoice_sources", "invoice_source"),
    "our_account": ("accounts", "account"),
    "mill": ("steel_mills", "steel_mill"),
}


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


def _build_flow_graph(conn, tid, path, trip=None, node_baselines=None, tnv_rows=None, role_alias=None):
    """把一个template的template_flow_edges组装成前端要的{nodes,edges}结构。
    path='all'/'invoice'/'payment'过滤要画哪些边（'all'时invoice/payment/goods都画）。
    trip不为空时，把from_label/to_label按role_type替换成这笔车次的真实主体名字，
    并且如果edge.node_key_ref命中，把这笔车次对应节点的实际金额/单据信息附到边上——
    这样车次详情页看到的链条图，数字跟结算/利润用的是同一份计算结果，不会对不上。"""
    edges_rows = conn.execute(
        "SELECT * FROM template_flow_edges WHERE template_id=? ORDER BY seq", (tid,)
    ).fetchall()
    # 路径视图按环节类型所属的 path_group 过滤(engine.HOP_TYPE_CATALOG)：
    # "票路径"= ticket 组(结算单/台账/发票/过磅单，不只是发票)，"财务路径"= finance 组(打款)。
    # 参数值沿用 invoice/payment 这两个老名字，避免已保存的前端状态失效。
    want_group = {"invoice": "ticket", "ticket": "ticket",
                  "payment": "finance", "finance": "finance"}.get(path)
    if want_group:
        edges_rows = [e for e in edges_rows
                      if engine.HOP_TYPE_PATH_GROUP.get(e["hop_type"]) == want_group]

    # 一端的名字怎么来，三种形态优先级固定（2026-09-19 录入简化后的规则）：
    #   1) 绑定了具体档案实体 -> 永远取档案里的当前名字（在档案里改名，图上自动跟着变）
    #   2) 没绑 + 角色可替换 + 有车次上下文 -> 换成这笔车次的真实主体（角色占位的意义所在）
    #   3) 其余 -> 原样显示录入的文字（台账、最终客户这类不在档案里的概念）
    ent_name_cache = {}

    def entity_name(etype, eid):
        key = (etype, eid)
        if key not in ent_name_cache:
            tbl = {v[1]: v[0] for v in ROLE_ENTITY_TABLE.values()}.get(etype)
            row = conn.execute(f"SELECT name FROM {tbl} WHERE id=?", (eid,)).fetchone() if tbl else None
            ent_name_cache[key] = row["name"] if row else None
        return ent_name_cache[key]

    def label_for(role_type, static_label, etype=None, eid=None):
        if etype and eid:
            nm = entity_name(etype, eid)
            if nm:
                return nm
        if trip is not None:
            field = ROLE_TYPE_TRIP_FIELD.get(role_type)
            if field and trip.get(field):
                return trip[field]
        return static_label

    role_alias = role_alias or {}
    node_labels = {
        r["node_key"]: r["label"]
        for r in conn.execute("SELECT node_key,label FROM template_nodes WHERE template_id=?", (tid,))
    }
    nodes = {}
    out_edges = []
    for e in edges_rows:
        f_role = role_alias.get(e["from_role_type"], e["from_role_type"])
        t_role = role_alias.get(e["to_role_type"], e["to_role_type"])
        f_label = label_for(e["from_role_type"], e["from_label"],
                             e["from_entity_type"], e["from_entity_id"])
        t_label = label_for(e["to_role_type"], e["to_label"],
                             e["to_entity_type"], e["to_entity_id"])
        f_key = f"{f_role}:{f_label}"
        t_key = f"{t_role}:{t_label}"
        if f_key == t_key:
            # 归并后首尾变成同一个节点(比如supplier被并进dispatcher，正好这条边两端都是它)：
            # 这条边本身就没有意义了，跳过不画，只留下这一个节点。
            nodes.setdefault(f_key, {"key": f_key, "role_type": f_role, "label": f_label})
            continue
        nodes.setdefault(f_key, {"key": f_key, "role_type": f_role, "label": f_label})
        nodes.setdefault(t_key, {"key": t_key, "role_type": t_role, "label": t_label})
        edge_out = {
            "id": e["id"], "from": f_key, "to": t_key, "hop_type": e["hop_type"],
            "node_key_ref": e["node_key_ref"], "notes": e["notes"], "seq": e["seq"],
            # has_amount: 这一步挂了计算节点、图上要画成实线并显示金额；
            # 没挂的是纯流程说明，画成细虚线+灰色，一眼就能看出改它不影响任何数字。
            "has_amount": bool(e["node_key_ref"]),
            "is_auto": bool(e["is_auto"]),
            "node_label": node_labels.get(e["node_key_ref"]),
            "from_bound": bool(e["from_entity_id"]), "to_bound": bool(e["to_entity_id"]),
        }
        if trip is not None and e["node_key_ref"]:
            edge_out["value"] = (node_baselines or {}).get(e["node_key_ref"])
            tnv = (tnv_rows or {}).get(e["node_key_ref"])
            if tnv:
                edge_out["doc_no"] = tnv["doc_no"]
                edge_out["actual_amount"] = tnv["actual_amount"]
                edge_out["doc_status"] = tnv["doc_status"]
        out_edges.append(edge_out)
    return {"nodes": list(nodes.values()), "edges": out_edges}


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


# ---------------------------------------------------------------------------
# 车次 API
# ---------------------------------------------------------------------------
def _fetch_trip_full(conn, trip_id):
    trip = conn.execute(
        """
        SELECT t.*, m.name AS mill_name, a.name AS account_name, b.name AS base_name,
               d.name AS dispatcher_name, s.name AS invoice_source_name,
               s.settlement_type AS invoice_source_settlement_type, v.plate_no AS vehicle_plate_no
        FROM trips t
        LEFT JOIN steel_mills m ON t.mill_id = m.id
        LEFT JOIN accounts a ON t.account_id = a.id
        LEFT JOIN processing_bases b ON t.base_id = b.id
        LEFT JOIN dispatchers d ON t.dispatcher_id = d.id
        LEFT JOIN invoice_sources s ON t.invoice_source_id = s.id
        LEFT JOIN vehicles v ON t.vehicle_id = v.id
        WHERE t.id = ?
        """,
        (trip_id,),
    ).fetchone()
    if not trip:
        return None
    details = conn.execute(
        """
        SELECT tsd.*, st.name AS scrap_type_name, st.category AS scrap_category
        FROM trip_scrap_details tsd
        LEFT JOIN scrap_types st ON tsd.scrap_type_id = st.id
        WHERE tsd.trip_id = ?
        """,
        (trip_id,),
    ).fetchall()
    finance_row = conn.execute("SELECT * FROM trip_finance WHERE trip_id=?", (trip_id,)).fetchone()
    trip_dict = dict(trip)
    if finance_row and finance_row["is_locked"]:
        # 2026-09-21：固化快照只存了钱数，没存吨位/结算金额/周期起止/中间量(给客户付、前端开票金额…)，
        # 利润分项表里这些行一直显示"-"或"undefined"。展示时先用实时计算补齐这些说明性字段，
        # 再用快照里的数覆盖——快照里有的一律以快照为准，净利润等数字不变。
        finance = {**_compute_finance(conn, trip), **dict(finance_row)}
        # trip_finance 表从来没有 calc_type 列(它是纯展示字段，不落库)，固化快照走这条分支时
        # 一直缺这个字段——trips.js::renderProfitTable 靠 fin.calc_type==='split' 判断用哪套
        # "利润分项"展示模板，缺了它会导致所有已固化的路径1/3(配票/反向)车次误显示成路径2
        # (带票带货)的表格布局。任务#30梳理全字段对比时发现的既有小bug，顺手补上：直接从
        # mode_code同款分类规则(SPLIT_MODE_CODES，跟_trip_breakdown::is_split_path同一套)反推。
        finance["calc_type"] = "split" if (trip_dict.get("mode_code") or "") in SPLIT_MODE_CODES else "service_fee"
    else:
        finance = _compute_finance(conn, trip)
        if finance_row:
            finance["is_locked"] = 0
    payments = conn.execute("SELECT * FROM payments WHERE trip_id=? ORDER BY id", (trip_id,)).fetchall()
    attachments = conn.execute("SELECT * FROM trip_attachments WHERE trip_id=? ORDER BY id", (trip_id,)).fetchall()
    doc_panels = _compute_doc_panels(conn, trip_dict)
    return {
        "trip": trip_dict,
        "details": rows_to_list(details),
        "finance": finance,
        "payments": rows_to_list(payments),
        "attachments": rows_to_list(attachments),
        "doc_panels": doc_panels,
        "progress": _compute_trip_progress(trip_dict, rows_to_list(attachments), rows_to_list(payments), doc_panels),
    }


# 车次进度节点化(Phase 3, 2026-09-18)：不新建状态机、不改现有6态status，而是把"到了什么阶段"
# 拆成一串可以并行推进的检查点，直接从已有数据(status/settlement_doc_no/attachments/
# doc_panels/payments)算出来，纯只读展示，不需要用户手动勾选。
def _compute_trip_progress(trip_dict, attachments, payments, doc_panels):
    status_order = [s for s, _ in STATUS_LIST if s != "voided"]
    cur_idx = status_order.index(trip_dict["status"]) if trip_dict["status"] in status_order else -1

    def status_at_least(code):
        return cur_idx >= 0 and cur_idx >= status_order.index(code)

    steps = [
        {"key": "reported", "label": "已报备", "done": True},
        {"key": "arrived", "label": "已到厂", "done": status_at_least("arrived")},
        {"key": "weighing_slip", "label": "过磅单",
         "done": any(a["category"] == "weighing_slip" for a in attachments)},
        # 结算单口径(2026-09-19用户明确)：车次状态只要推进到"已结算"，就说明钢厂结算单已经出了
        # ——业务上正是拿到结算单才会把状态点成已结算，所以状态本身就是"结算单已出"的证据。
        # settlement_doc_no(结算单号)是"有没有把单号录进系统"的另一回事，没录不代表单没出，
        # 所以这里两个条件取或：状态到了已结算、或者单号已录入，都算这一步完成。
        {"key": "settlement_doc", "label": "结算单",
         "done": status_at_least("settled") or bool(trip_dict.get("settlement_doc_no"))},
    ]
    for n in doc_panels:
        if "invoice" in n["doc_types"]:
            steps.append({"key": f"doc_{n['node_key']}", "label": f"发票·{n['label']}", "done": bool(n["doc_no"])})
    for n in doc_panels:
        if "payment" in n["doc_types"]:
            paid = sum(float(p["amount"] or 0) for p in payments if p["node_key"] == n["node_key"])
            baseline = n["baseline"]
            if baseline is not None and baseline > 0:
                done = paid >= float(baseline) - 0.01
                partial = 0 < paid < float(baseline) - 0.01
            else:
                done = paid > 0
                partial = False
            steps.append({
                "key": f"pay_{n['node_key']}", "label": f"打款·{n['label']}",
                "done": done, "partial": partial, "paid": paid, "baseline": baseline,
            })
    steps.append({"key": "closed", "label": "已结案", "done": trip_dict["status"] == "closed"})
    return steps


def _compute_doc_panels(conn, trip_dict):
    """两票三款通用化(2026-09-18)：不再局限于pass_through票源的5个固定字段，而是把trip关联
    模板里所有 doc_types 非空的节点都列出来，每个节点给一个"应收应付基准"(来自v2引擎算的
    node_values，跟现网净利润计算互不影响，只是拿来当核对参照)，前端据此动态渲染发票/打款
    录入区，不再是硬编码的"两票三款"卡片。trip还没关联template_id(极少数历史脏数据)时返回[]，
    前端按老样子不显示该区域。"""
    template_id = trip_dict.get("template_id")
    if not template_id:
        return []
    nodes = conn.execute(
        "SELECT * FROM template_nodes WHERE template_id=? AND doc_types IS NOT NULL ORDER BY seq",
        (template_id,),
    ).fetchall()
    if not nodes:
        return []
    try:
        v2 = engine.compute_trip_finance_v2(conn, trip_dict)
        baselines = v2["nodes"] if v2 else {}
    except Exception:
        baselines = {}
    tnv_rows = {
        r["node_key"]: r for r in conn.execute(
            "SELECT * FROM trip_node_values WHERE trip_id=?", (trip_dict["id"],)
        ).fetchall()
    }
    panels = []
    for n in nodes:
        tnv = tnv_rows.get(n["node_key"])
        panels.append({
            "node_key": n["node_key"],
            "label": n["label"],
            "role_type": n["role_type"],
            "doc_types": json.loads(n["doc_types"]) if n["doc_types"] else [],
            "baseline": baselines.get(n["node_key"]),
            "doc_no": tnv["doc_no"] if tnv else None,
            "actual_amount": tnv["actual_amount"] if tnv else None,
            "doc_status": tnv["doc_status"] if tnv else None,
        })
    return panels


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


def _sync_mode_code_from_template(conn, trip_id, template_id):
    """反方向同步（2026-09-19，界面上把"结算模式"和"链条规则"合并成一个入口之后用）。

    以前车次页选的是 mode_code(M1-M5)，链条规则页显示的是 flow_templates 的名字，
    同一个东西两套名字两个入口，用户每次都要在脑子里翻译一遍（实测库里 68 笔 M1 全部对应
    模板1，一一对应、没有任何例外，纯属冗余）。现在界面上只保留"链条规则"一个入口，
    用户选的是 template_id；mode_code 仍然写回去，因为它还有两个用处：
      1) calc.py 兜底路径（template_id 万一缺失时按 mode_code 算）
      2) 车次列表/导出台账里还在展示 M1/M3 这种简写
    所以这里按 MODE_TO_TEMPLATE_CODE 反查，保证两个字段永远一致，不会一个新一个旧。"""
    row = conn.execute("SELECT code FROM flow_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        return
    rev = {v: k for k, v in engine.MODE_TO_TEMPLATE_CODE.items()}
    mode_code = rev.get(row["code"])
    if mode_code:
        conn.execute("UPDATE trips SET mode_code=? WHERE id=?", (mode_code, trip_id))


def _sync_template_id(conn, trip_id, mode_code):
    """只要车次的 mode_code 被设置/修改，就顺带把 template_id 也同步过去(通过
    engine.MODE_TO_TEMPLATE_CODE 1:1映射)。2026-09-18任务#30全面切换引擎后，template_id
    不再只是"两票三款"这类节点功能的辅助字段——它现在直接决定 net_profit 怎么算
    (见 _compute_finance/engine.compute_trip_finance_v2_flat)，所以这一步比切换前更关键，
    必须保证每次 mode_code 变化都同步上；万一某处遗漏，_ensure_template_id() 在真正算钱时
    还会再兜底一次。"""
    tpl_code = engine.MODE_TO_TEMPLATE_CODE.get(mode_code)
    if not tpl_code:
        return
    row = conn.execute("SELECT id FROM flow_templates WHERE code=?", (tpl_code,)).fetchone()
    if row:
        conn.execute("UPDATE trips SET template_id=? WHERE id=?", (row["id"], trip_id))


def _ensure_template_id(conn, trip):
    """算钱前的兜底：正常情况下 create_trip/update_trip 在 mode_code 变化时已经调用过
    _sync_template_id() 同步好 template_id，这里只处理"万一没同步上"的边缘情况(比如
    导入脚本绕过app.py直接写库、或历史脏数据)。找不到对应模板就原样返回，让调用方
    (_compute_finance)退回 calc.py 兜底，不让单条车次的数据问题影响其它车次。"""
    if trip["template_id"]:
        return trip
    tpl_code = engine.MODE_TO_TEMPLATE_CODE.get(trip["mode_code"])
    if not tpl_code:
        return trip
    row = conn.execute("SELECT id FROM flow_templates WHERE code=?", (tpl_code,)).fetchone()
    if not row:
        return trip
    conn.execute("UPDATE trips SET template_id=? WHERE id=?", (row["id"], trip["id"]))
    conn.commit()
    trip = dict(trip)
    trip["template_id"] = row["id"]
    return trip


def _stored_overrides(trip):
    try:
        raw = trip["finance_overrides"]
    except (IndexError, KeyError):
        return {}
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


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


def _compute_finance(conn, trip, overrides=None):
    """车次利润试算的唯一入口(任务#30，2026-09-18完成全面切换)：优先用v2引擎
    (engine.compute_trip_finance_v2_flat，按template_id的节点链计算，费率来自rate_links)，
    只有该车次连template_id都补不上(mode_code为空/不在M1-M5映射表里，理论上不应该出现，
    真实87笔历史车次已全部验证过)时才退回 calc.py(mode_code+硬编码if/else，旧实现原样保留
    未删除)。两个引擎返回的字典字段名/含义完全一致，调用方不用关心走的是哪条路径。"""
    trip = _ensure_template_id(conn, trip)
    # 逐车费率覆盖(2026-09-21)：trips.finance_overrides 里存着这一车改过的费率，所有算钱的地方
    # (详情/首页/报表/导出)都经过这里，所以在这里统一合并；本次请求临时传的覆盖优先。
    overrides = {**_stored_overrides(trip), **(overrides or {})}
    if trip["template_id"]:
        fin = engine.compute_trip_finance_v2_flat(conn, trip, overrides)
        if fin is not None:
            return fin
    return calc.compute_trip_finance(conn, trip, overrides)


def _get_setting(conn, key, default=None):
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row["value"] if row and row["value"] not in (None, "") else default


def _entry_defaults(conn):
    """新建车次默认值(A阶段)：钢厂只有一个 → 默认它；户头 = 绑定该钢厂的户头(只有一个时)；
    接单日期今天；发货地取 app_settings.default_departure_place(默认永康)。
    以后加了第二个钢厂，钢厂就不再默认，但选了钢厂后户头仍自动跟随(前端同一规则)。"""
    d = {"status": "reported", "transport_type": "truck",
         "order_date": conn.execute("SELECT date('now','localtime')").fetchone()[0]}
    place = _get_setting(conn, "default_departure_place", "永康")
    if place:
        d["departure_place"] = place
    mills = conn.execute("SELECT id FROM steel_mills").fetchall()
    if len(mills) == 1:
        d["mill_id"] = mills[0]["id"]
        accs = conn.execute("SELECT id FROM accounts WHERE mill_id=?", (d["mill_id"],)).fetchall()
        if len(accs) == 1:
            d["account_id"] = accs[0]["id"]
    return d


@app.route("/api/entry_defaults", methods=["GET"])
def api_entry_defaults():
    return jsonify(_entry_defaults(get_db()))


@app.route("/api/app_settings", methods=["GET", "PUT"])
def api_app_settings():
    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    if request.method == "PUT":
        for k, v in (request.get_json(force=True) or {}).items():
            conn.execute("INSERT INTO app_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (k, v))
        conn.commit()
    return jsonify({r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM app_settings")})


@app.route("/api/plates", methods=["GET"])
def api_plates():
    """车牌/船号联想列表：历史出现过的车牌，按使用次数排序。"""
    rows = get_db().execute(
        """SELECT plate_no, transport_type, COUNT(*) AS n, MAX(coalesce(order_date, settlement_date)) AS last_date
           FROM trips WHERE plate_no IS NOT NULL AND plate_no!='' AND status!='voided'
           GROUP BY plate_no ORDER BY n DESC, last_date DESC""").fetchall()
    return jsonify(rows_to_list(rows))


# 车牌画像里哪些字段参与"带出"：只有该车牌历史上这个字段足够稳定(同一取值占比≥80%)才带出，
# 否则留空让人选。例如苏DBB772是林凯车队的车：调货人16次全是林凯→带出；供货厂在林凯/君盛/
# 𠓾景通之间变→不带，留空。司机信息取最近一次非空值。
PLATE_PROFILE_FIELDS = ["transport_type", "base_id", "dispatcher_id", "invoice_source_id", "template_id"]
PLATE_STABLE_RATIO = 0.8


def _plate_profile(conn, plate):
    rows = conn.execute(
        f"""SELECT {','.join(PLATE_PROFILE_FIELDS)}, driver_name, driver_phone, driver_id_card
            FROM trips WHERE plate_no=? AND status!='voided' ORDER BY coalesce(order_date, settlement_date) DESC, id DESC""",
        (plate,)).fetchall()
    out = {"plate_no": plate, "count": len(rows), "fill": {}, "varies": {}}
    if not rows:
        return out
    # 调货人自己车队的车(如苏DBB772，林凯车队)：历史上供货厂记成了"林凯"这类占位基地
    # (is_dispatcher_alias=1，其实就是调货人本人)。用户2026-09-21明确：这类车供货厂先留空、
    # 不强制录入，所以占位基地不参与带出，也不算进"稳定"比例的分子。
    alias_bases = {r["id"] for r in conn.execute("SELECT id FROM processing_bases WHERE is_dispatcher_alias=1")}
    out["fleet_of_dispatcher"] = None
    for f in PLATE_PROFILE_FIELDS:
        vals = [r[f] for r in rows if r[f] not in (None, "") and not (f == "base_id" and r[f] in alias_bases)]
        if not vals:
            continue
        top = max(set(vals), key=vals.count)
        if vals.count(top) / len(rows) >= PLATE_STABLE_RATIO:
            out["fill"][f] = top
        else:
            out["varies"][f] = sorted(set(vals), key=lambda v: -vals.count(v))
    alias_n = sum(1 for r in rows if r["base_id"] in alias_bases)
    if alias_n / len(rows) >= 0.5 and out["fill"].get("dispatcher_id"):
        out["fleet_of_dispatcher"] = out["fill"]["dispatcher_id"]
    for f in ("driver_name", "driver_phone", "driver_id_card"):
        v = next((r[f] for r in rows if r[f]), None)
        if v:
            out["fill"][f] = v
    return out


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


def _fill_status_date(conn, trip_id, status, date=None):
    """把目标状态对应的日期填上：已有日期不覆盖(回退后再推进保留原日期)，没有就填
    传入日期(批量弹框里选的)或今天。"""
    field = STATUS_DATE_FIELD.get(status)
    if field:
        conn.execute(f"UPDATE trips SET {field}=coalesce(?, date('now','localtime')) WHERE id=? AND ({field} IS NULL OR {field}='')",
                     (date or None, trip_id))


def _quick_settle(conn, trip_id, tonnage, amount):
    """推进到"已结算"时的快速录入：只填结算吨位+金额。只在车次还没有任何磅重明细时生效
    （写一行不指定货型的明细，单价=金额/吨位，混料标记打上），已有明细的车次不覆盖，
    以明细为准——避免快速录入把用户逐货型录好的数据冲掉。返回是否写入。"""
    if tonnage in (None, "") or amount in (None, ""):
        return False
    tonnage, amount = float(tonnage), float(amount)
    if conn.execute("SELECT 1 FROM trip_scrap_details WHERE trip_id=?", (trip_id,)).fetchone():
        return False
    unit_price = round(amount / tonnage, 4) if tonnage else 0
    conn.execute(
        """INSERT INTO trip_scrap_details (trip_id, scrap_type_id, net_weight, deduction, settlement_tonnage,
           unit_price, cutting_fee, amount) VALUES (?,?,?,?,?,?,?,?)""",
        (trip_id, None, tonnage, 0, tonnage, unit_price, 0, amount))
    conn.execute(
        """UPDATE trips SET net_weight_total=?, deduction_total=0, settlement_tonnage_total=?, cutting_fee_total=0,
           settlement_amount_total=?, mixed_material=1 WHERE id=?""", (tonnage, tonnage, amount, trip_id))
    return True


def _apply_status(conn, trip_id, status, extra=None):
    """单车/批量共用：改状态 + 填日期 +（可选）结算单号/结算日期/快速结算吨位金额。"""
    extra = extra or {}
    old = conn.execute("SELECT status FROM trips WHERE id=?", (trip_id,)).fetchone()
    if old:
        audit(conn, "trips", trip_id, "status", old["status"], status)
    if extra.get("settlement_doc_no"):
        conn.execute("UPDATE trips SET settlement_doc_no=? WHERE id=?", (extra["settlement_doc_no"], trip_id))
    _fill_status_date(conn, trip_id, status, extra.get("status_date") or extra.get(STATUS_DATE_FIELD.get(status, "")))
    wrote = False
    if status == "settled":
        wrote = _quick_settle(conn, trip_id, extra.get("settlement_tonnage_total"), extra.get("settlement_amount_total"))
    conn.execute("UPDATE trips SET status=?, updated_at=datetime('now','localtime') WHERE id=?", (status, trip_id))
    return wrote


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


# ---------------------------------------------------------------------------
# 报价单 API
# ---------------------------------------------------------------------------
@app.route("/api/quote_sheets", methods=["GET", "POST"])
def quote_sheets():
    conn = get_db()
    if request.method == "GET":
        mill_id = request.args.get("mill_id")
        sql = """SELECT q.*, m.name AS mill_name FROM quote_sheets q
                  LEFT JOIN steel_mills m ON q.mill_id=m.id"""
        args = []
        if mill_id:
            sql += " WHERE q.mill_id=?"
            args.append(mill_id)
        sql += " ORDER BY q.effective_at DESC"
        rows = conn.execute(sql, args).fetchall()
        return jsonify(rows_to_list(rows))
    data = request.get_json(force=True) or {}
    cur = conn.execute(
        "INSERT INTO quote_sheets (mill_id, period_label, effective_at, footer_notes) VALUES (?,?,?,?)",
        (data.get("mill_id"), data.get("period_label"), data.get("effective_at"), data.get("footer_notes")),
    )
    conn.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/quote_sheets/<int:sheet_id>", methods=["PUT"])
def update_quote_sheet(sheet_id):
    conn = get_db()
    data = request.get_json(force=True) or {}
    cols = [c for c in ["mill_id", "period_label", "effective_at", "footer_notes"] if c in data]
    if not cols:
        return jsonify({"error": "empty payload"}), 400
    set_clause = ",".join(f"{c}=?" for c in cols)
    vals = [data[c] for c in cols] + [sheet_id]
    conn.execute(f"UPDATE quote_sheets SET {set_clause} WHERE id=?", vals)
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/quote_sheets/<int:sheet_id>", methods=["GET"])
def get_quote_sheet(sheet_id):
    conn = get_db()
    sheet = conn.execute(
        """SELECT q.*, m.name AS mill_name FROM quote_sheets q
           LEFT JOIN steel_mills m ON q.mill_id=m.id WHERE q.id=?""",
        (sheet_id,),
    ).fetchone()
    if not sheet:
        return jsonify({"error": "not found"}), 404
    items = conn.execute(
        """SELECT qi.*, st.name AS scrap_name, st.category, st.example_desc, st.size_requirement, st.thickness_requirement
           FROM quote_items qi LEFT JOIN scrap_types st ON qi.scrap_type_id = st.id
           WHERE qi.quote_sheet_id=?""",
        (sheet_id,),
    ).fetchall()
    prev_sheet = conn.execute(
        """SELECT id FROM quote_sheets WHERE mill_id=? AND effective_at < ? ORDER BY effective_at DESC LIMIT 1""",
        (sheet["mill_id"], sheet["effective_at"]),
    ).fetchone()
    prev_prices = {}
    if prev_sheet:
        prev_items = conn.execute(
            "SELECT scrap_type_id, price FROM quote_items WHERE quote_sheet_id=?", (prev_sheet["id"],)
        ).fetchall()
        prev_prices = {r["scrap_type_id"]: r["price"] for r in prev_items}

    items_out = []
    for it in items:
        d = dict(it)
        prev_price = prev_prices.get(it["scrap_type_id"])
        d["prev_price"] = prev_price
        d["price_change"] = (d["price"] - prev_price) if (d["price"] is not None and prev_price is not None) else None
        items_out.append(d)

    return jsonify({"sheet": dict(sheet), "items": items_out})


@app.route("/api/quote_sheets/<int:sheet_id>/items", methods=["PUT"])
def upsert_quote_items(sheet_id):
    conn = get_db()
    data = request.get_json(force=True) or {}
    items = data.get("items", [])
    conn.execute("DELETE FROM quote_items WHERE quote_sheet_id=?", (sheet_id,))
    for it in items:
        conn.execute(
            "INSERT INTO quote_items (quote_sheet_id, scrap_type_id, price, is_suspended) VALUES (?,?,?,?)",
            (sheet_id, it.get("scrap_type_id"), it.get("price"), int(bool(it.get("is_suspended")))),
        )
    conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 首页看板 API
# ---------------------------------------------------------------------------
def _trip_profit(conn, t):
    """已固化快照直接取值，否则实时估算。"""
    fin_row = conn.execute("SELECT * FROM trip_finance WHERE trip_id=?", (t["id"],)).fetchone()
    if fin_row and fin_row["is_locked"]:
        return fin_row["net_profit"] or 0
    return _compute_finance(conn, t)["net_profit"] or 0


def _trip_reward(conn, t):
    """量补金额（已固化快照直接取值，否则实时估算），单独列出以便核对特殊周期是否按约定单价计算量补。"""
    fin_row = conn.execute("SELECT * FROM trip_finance WHERE trip_id=?", (t["id"],)).fetchone()
    if fin_row and fin_row["is_locked"]:
        return fin_row["reward_profit"] or 0
    return _compute_finance(conn, t)["reward_profit"] or 0


def _trip_finance_full(conn, t):
    """完整取一条车次的财务快照（已固化直接取值，否则实时估算），用于拆解利润构成。"""
    fin_row = conn.execute("SELECT * FROM trip_finance WHERE trip_id=?", (t["id"],)).fetchone()
    if fin_row and fin_row["is_locked"]:
        return dict(fin_row)
    return _compute_finance(conn, t)


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


# 特殊历史周期标记：仅此一期特殊处理，不写入 mill_reward_tiers 等既定规则表，
# 也不影响该钢厂后续周期的常规阶梯量补规则——数据本身（trip_finance.reward_profit）
# 在台账历史导入时已经如实按台账原始"奖励利润"列还原（20元/吨，未按3000吨门槛核算），
# 这里只是在首页周期面板上把"为什么这期量补比例跟阶梯规则对不上"标注清楚，避免误以为是bug。
SPECIAL_PERIOD_NOTES = {
    (1, "2026-07-26", "2026-08-25"): (
        "该周期为业务恢复初期，经与钢厂协商量补按20元/吨特殊结算，不执行常规3000吨最低门槛要求"
        "（仅此一期特殊处理，后续周期仍按既定阶梯规则执行）"
    ),
}


# 路径1/3(配票/反向, M1/M5) 用差价模型，路径2(带票带货, M2/M3/M4) 用服务费模型——
# 跟 calc.py 里 trip_modes.calc_type 的分类完全一致，这里直接按 mode_code 分桶，
# 省一次逐车次查 trip_modes 表（首页周期卡片这类高频小查询，没必要为5行的档案表再联查）。
SPLIT_MODE_CODES = {"M1", "M5"}


def _trip_breakdown(conn, t):
    """单个车次的利润构成拆解：净利润 = 结算毛利 + 量补 − 垫资成本 + 其他净调整。
    首页周期卡片、一键导出台账的"统计汇总"都复用这一份逻辑，保证口径一致。
    注意：reserved_profit（票差留存利润，经过我们的票源方如益周才有）2026-09-17起
    已从净利润/结算毛利里剔除，单独留存、不计入这里任何一项，也不进入 net_profit 汇总——
    调用方如需展示，单独取 reserved_profit 字段，绝不能加进 net_profit 或 gross_margin。
    2026-09-17新增：额外返回"其他净调整"的4个原始分项(service_fee_income/
    dispatcher_commission_cost/ticket_cost/other_cost)和车次所属路径(is_split_path)，
    供首页"利润构成明细"面板做"A+B+C=结果"式的实际数字分项展示，而不是只给一个加总数。"""
    fin = _trip_finance_full(conn, t)
    gross_margin = fin.get("freight_diff_profit") or 0  # 票差利润已剔除，不再计入结算毛利
    reward_amount = fin.get("reward_profit") or 0
    advance_fee_cost = fin.get("advance_fee_cost") or 0
    service_fee_income = fin.get("service_fee_income") or 0
    dispatcher_commission_cost = fin.get("dispatcher_commission_cost") or 0
    ticket_cost = fin.get("ticket_cost") or 0
    other_cost = fin.get("other_cost") or 0
    other_net = service_fee_income - dispatcher_commission_cost - ticket_cost - other_cost
    return {
        "gross_margin": gross_margin,
        "reward_amount": reward_amount,
        "advance_fee_cost": advance_fee_cost,
        "other_net": other_net,
        "net_profit": fin.get("net_profit") or 0,
        "reserved_profit": fin.get("invoice_diff_profit") or 0,
        "is_split_path": (t["mode_code"] or "") in SPLIT_MODE_CODES,
        "service_fee_income": service_fee_income,
        "dispatcher_commission_cost": dispatcher_commission_cost,
        "ticket_cost": ticket_cost,
        "other_cost": other_cost,
    }


def _empty_stats_bucket(name=None):
    """零值的利润构成聚合桶，字段跟 _trip_breakdown() 的产出一一对应。被首页周期卡片
    (_period_trip_stats) 和统计报表 (reports_summary) 共用，保证"按期/按钢厂/按调货员/
    按票源方/按结算路径"等任意维度汇总口径完全一致，都是从同一份节点化拆解(_trip_breakdown)
    累加出来的，不是另起一套算法。"""
    return {
        "name": name, "trip_count": 0, "tonnage": 0.0, "settlement_amount": 0.0,
        "profit": 0.0,
        "gross_margin": 0.0,      # 结算毛利：钢厂含税金额与磅差/开票差价等形成的基础毛利（不含量补、不扣垫资成本）
        "reward_amount": 0.0,     # 量补金额
        "advance_fee_cost": 0.0,  # 垫资成本（资金占用成本，是利润的减项）
        "other_net": 0.0,         # 其他净调整：调度提成、服务费收入、票务成本等杂项的净额
        "reserved_profit": 0.0,   # 票差留存利润（经过我们的票源方才有，如益周）：单独统计，不计入 profit
        "reserved_trip_count": 0,
        # 垫资成本按路径拆分(路径1/3差价模型 vs 路径2服务费模型)、其他净调整拆成4个原始
        # 分项——供"利润构成明细"面板做"A+B+C=结果"式实际数字展示。
        "split_advance": 0.0,
        "split_trip_count": 0,
        "service_advance": 0.0,
        "service_trip_count": 0,
        # 2026-09-20：按调货员/供货厂做详情下钻时要能回答"多少是带票的、多少是我们配票的"，
        # 原来桶里只有这两条路径各自的车次数和垫资成本，吨位/金额/利润/提成都没分开记。
        # split = 路径1/3 配票·反向（差价模型 M1/M5）；service = 路径2 带票带货（服务费模型 M2/M3/M4）
        "split_tonnage": 0.0, "split_settlement_amount": 0.0, "split_profit": 0.0, "split_commission": 0.0,
        "service_tonnage": 0.0, "service_settlement_amount": 0.0, "service_profit": 0.0, "service_commission": 0.0,
        "service_fee_income_total": 0.0,
        "dispatcher_commission_total": 0.0,
        "ticket_cost_total": 0.0,
        "other_cost_total": 0.0,
        # 车/船分开统计（2026-09-19用户要求）：一条船的吨位是一辆车的十倍上下
        # （实测：船均576吨/车均59吨，6条船就占了全部吨位的4成），混在一起看"车次数"和
        # 任何按车次平均的数字都会被带偏。所以每个聚合桶内部都同时记一份车/船分开的数字，
        # 任何维度(按钢厂/按调货人/按期/首页总计…)都能就地拆出车船，不用另起一套统计。
        "truck_trip_count": 0, "truck_tonnage": 0.0, "truck_settlement_amount": 0.0, "truck_profit": 0.0,
        "ship_trip_count": 0, "ship_tonnage": 0.0, "ship_settlement_amount": 0.0, "ship_profit": 0.0,
    }


def _accum_bucket(bk, t, b):
    """把单笔车次的节点化拆解(b = _trip_breakdown(conn, t))累加进聚合桶 bk。"""
    bk["trip_count"] += 1
    bk["tonnage"] += t["settlement_tonnage_total"] or 0
    bk["settlement_amount"] += t["settlement_amount_total"] or 0
    bk["profit"] += b["net_profit"]
    bk["gross_margin"] += b["gross_margin"]
    bk["reward_amount"] += b["reward_amount"]
    bk["advance_fee_cost"] += b["advance_fee_cost"]
    bk["other_net"] += b["other_net"]
    bk["reserved_profit"] += b["reserved_profit"]
    if abs(b["reserved_profit"]) > 0.005:
        bk["reserved_trip_count"] += 1
    pre = "split" if b["is_split_path"] else "service"
    bk[f"{pre}_advance"] += b["advance_fee_cost"]
    bk[f"{pre}_trip_count"] += 1
    bk[f"{pre}_tonnage"] += t["settlement_tonnage_total"] or 0
    bk[f"{pre}_settlement_amount"] += t["settlement_amount_total"] or 0
    bk[f"{pre}_profit"] += b["net_profit"]
    bk[f"{pre}_commission"] += b["dispatcher_commission_cost"]
    bk["service_fee_income_total"] += b["service_fee_income"]
    bk["dispatcher_commission_total"] += b["dispatcher_commission_cost"]
    bk["ticket_cost_total"] += b["ticket_cost"]
    bk["other_cost_total"] += b["other_cost"]
    # 车/船分开：transport_type 只有 truck/ship 两种，历史脏数据缺失时按车处理
    p = "ship" if t["transport_type"] == "ship" else "truck"
    bk[f"{p}_trip_count"] += 1
    bk[f"{p}_tonnage"] += t["settlement_tonnage_total"] or 0
    bk[f"{p}_settlement_amount"] += t["settlement_amount_total"] or 0
    bk[f"{p}_profit"] += b["net_profit"]


def _finalize_bucket(bk):
    # 量补加权平均单价：reward_amount / tonnage，用于直观核对"这期是不是按20元/吨结算的"
    bk["reward_avg_price"] = (bk["reward_amount"] / bk["tonnage"]) if bk["tonnage"] else None
    # 车/船各自的平均吨位：这两个数字差一个数量级，是"必须分开看"最直观的证据
    bk["truck_avg_tonnage"] = (bk["truck_tonnage"] / bk["truck_trip_count"]) if bk["truck_trip_count"] else None
    bk["ship_avg_tonnage"] = (bk["ship_tonnage"] / bk["ship_trip_count"]) if bk["ship_trip_count"] else None
    # 单吨利润(元/吨)：比总利润更能横向比较供货厂/调货人/票源的"质量"(2026-09-21)
    bk["profit_per_ton"] = (bk["profit"] / bk["tonnage"]) if bk["tonnage"] else None
    bk["truck_profit_per_ton"] = (bk["truck_profit"] / bk["truck_tonnage"]) if bk["truck_tonnage"] else None
    bk["ship_profit_per_ton"] = (bk["ship_profit"] / bk["ship_tonnage"]) if bk["ship_tonnage"] else None
    bk["split_profit_per_ton"] = (bk["split_profit"] / bk["split_tonnage"]) if bk["split_tonnage"] else None
    bk["service_profit_per_ton"] = (bk["service_profit"] / bk["service_tonnage"]) if bk["service_tonnage"] else None
    return bk


def _trips_stats(conn, trips, name=None):
    """任意一组车次(不限定维度)的完整利润构成聚合，逐车次拆解口径。"""
    bk = _empty_stats_bucket(name)
    for t in trips:
        _accum_bucket(bk, t, _trip_breakdown(conn, t))
    return _finalize_bucket(bk)


def _period_trip_stats(conn, mill_id, period_start, period_end):
    trips = conn.execute(
        """SELECT * FROM trips WHERE mill_id=? AND status!='voided' AND settlement_date IS NOT NULL
           AND date(settlement_date) BETWEEN date(?) AND date(?)""",
        (mill_id, period_start, period_end),
    ).fetchall()
    st = _trips_stats(conn, trips)
    st["trip_ids"] = [t["id"] for t in trips]  # 首页数字点进去看是哪些车
    return st


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


def _quota_levels(conn):
    """票源额度水位：按票源方自己的额度周期(月度/年度)计算已用金额与占比，用于首页水位图。
    已用金额 = 该票源方名下车次"前端给垫资开票的金额"合计(结算金额A - 垫资费)，而非结算金额本身——
    这是我们找外部票商配票、开给欧冶(国企垫资)的发票金额，国企再开给钢厂的部分不关心。
    垫资费单价按车次模板+费率档查（反向在其票点已含垫资费的时段=0，见 migrate_reverse_advance_fee.py）。"""
    today = dt.date.today()
    month_start = today.replace(day=1).isoformat()
    year_start = today.replace(month=1, day=1).isoformat()

    sources = conn.execute("SELECT * FROM invoice_sources ORDER BY id").fetchall()
    result = []
    for s in sources:
        is_annual = s["quota_period"] == "annual"
        period_start = year_start if is_annual else month_start
        # 垫资费单价逐车按模板+费率档查(engine.advance_fee_unit_for_trip)，不再写死"M5不扣"：
        # 垫资费一直都有，只是某些月份反向的垫资费已含在反向票点里(那段时间反向档=0)。
        rows = conn.execute(
            """SELECT t.* FROM trips t WHERE t.invoice_source_id = ? AND t.status != 'voided'
                 AND t.settlement_date IS NOT NULL AND date(t.settlement_date) >= date(?)""",
            (s["id"], period_start),
        ).fetchall()
        used = sum((t["settlement_amount_total"] or 0)
                   - (t["settlement_tonnage_total"] or 0) * engine.advance_fee_unit_for_trip(conn, t) for t in rows)
        quota = s["quota_amount"]
        warn = s["warn_threshold"]
        pct = (used / quota * 100) if quota else None
        warn_pct = (warn / quota * 100) if (quota and warn) else None
        result.append({
            "id": s["id"],
            "name": s["name"],
            "source_type": s["source_type"],
            "quota_period": s["quota_period"],
            "period_start": period_start,
            "quota_amount": quota,
            "warn_threshold": warn,
            "used_amount": used,
            "pct": pct,
            "warn_pct": warn_pct,
        })
    return result


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


PATH_BUCKET_DEFS = [
    # (key, is_split, 中文名) —— 固定两条，即使筛选区间内某条路径0车次也展示出来，方便
    # 领导对比两种模式(配票/反向差价模型 vs 带票带货服务费模型)在当前筛选范围内的占比。
    ("split", True, "路径1/3 · 配票/反向（差价模型，M1/M5）"),
    ("service", False, "路径2 · 带票带货（服务费模型，M2/M3/M4）"),
]

TRANSPORT_BUCKET_DEFS = [
    # 同样固定两条，0车次也占位展示，方便直接对比车运/船运的体量和利润结构差异。
    ("truck", "汽运（车）"),
    ("ship", "船运（船）"),
]


# ---------------------------------------------------------------------------
# 调货员/供货厂 详情下钻（2026-09-20）
# ---------------------------------------------------------------------------
# 统计报表的四张明细表只给一行汇总，回答不了"这个调货员本期比上期怎么样、提成拿了多少、
# 其中多少是带票多少是我们配票"。这里给单个实体出一份三栏对照（本期/上期/总计）。
# "本期/上期"沿用全系统唯一那套口径：各钢厂按自己的结算周期分别取，再加总——跟首页卡片、
# 钢厂周期卡片完全一致，不另造一套，否则同一个调货员在两个页面会看到两个"本期"。
ENTITY_STATS_KINDS = {
    "dispatcher": ("dispatcher_id", "dispatchers", "调货员"),
    "base": ("base_id", "processing_bases", "供货厂"),
    "mill": ("mill_id", "steel_mills", "钢厂"),
    "invoice_source": ("invoice_source_id", "invoice_sources", "票源方"),
}


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


# ---------------------------------------------------------------------------
# 全方位统计补充（2026-09-21）：趋势 / 质量 / 调货人提成应付已付 / 数据完整度。
# 资金占用与账龄走 lanes.py 的 /api/tracking（按开票/回款环节算）。
# 所有接口都复用 _report_trips 的筛选和 _trip_breakdown 的逐车拆解，口径与报表一致。
# ---------------------------------------------------------------------------
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


COMPLETENESS_RULES = [
    ("no_plate", "缺车号/船号", lambda t: not t["plate_no"]),
    ("no_origin", "缺供货厂（非调货人车队）", lambda t: not t["base_id"] and not t["dispatcher_id"]),
    ("no_template", "缺链条规则", lambda t: not t["template_id"]),
    ("no_source_split", "配票车次缺票源方", lambda t: (t["mode_code"] in SPLIT_MODE_CODES) and not t["invoice_source_id"]),
    ("settled_no_amount", "已结算但缺吨位/金额", lambda t: t["status"] in ("settled", "invoiced", "closed")
        and not ((t["settlement_tonnage_total"] or 0) > 0 and (t["settlement_amount_total"] or 0) > 0)),
    ("settled_no_date", "已结算但缺结算日期", lambda t: t["status"] in ("settled", "invoiced", "closed") and not t["settlement_date"]),
    ("settled_no_docno", "已结算但没录结算单号", lambda t: t["status"] in ("settled", "invoiced", "closed") and not t["settlement_doc_no"]),
    ("no_order_date", "缺接单日期", lambda t: not t["order_date"]),
]


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
        elif pct >= 90 or (q["warn_threshold"] and (q["used_amount"] or 0) >= q["warn_threshold"]):
            add("warn", f"{q['name']} 额度快用完", f"已用 {pct:.1f}%，剩 ¥{remain:,.0f}（{'年度' if q['quota_period'] == 'annual' else '本月'}）", href)
    # 2. 报了很久还没结算的车
    stale = [r["id"] for r in conn.execute(
        """SELECT id FROM trips WHERE status IN ('reported','arrived') AND order_date IS NOT NULL
           AND date(order_date) <= date('now','localtime','-3 days')""")]
    if stale:
        add("warn", f"{len(stale)} 车报备超过3天还没结算", "看看是不是结算单出了没录，或者车没到", 
            "/trips?ids=" + ",".join(map(str, stale)) + "&from=首页预警·报备超3天未结算", len(stale))
    # 3. 开票/回款待办（按账龄）
    rows = lanes.pending_rows(conn)
    inv_old = [r for r in rows if r["hop_key"] == "out" and (r["age_days"] or 0) > 7]
    if inv_old:
        add("warn", f"{len(inv_old)} 车结算超过7天还没开销项票", f"合计 ¥{sum(r['remaining'] or 0 for r in inv_old):,.0f}", "/tracking?hop=out", len(inv_old))
    pay_old = [r for r in rows if r["hop_key"] == "p_out" and (r["age_days"] or 0) > 30]
    if pay_old:
        add("info", f"{len(pay_old)} 车欧冶回款超过30天未登记",
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
        add("warn", "最近没有自动备份", "重启一次系统会自动备份；也可以在「系统」页手动备份", "/system")
    order = {"danger": 0, "warn": 1, "info": 2}
    out.sort(key=lambda x: order[x["level"]])
    return jsonify(out)


# ---------------------------------------------------------------------------
# v2：钢厂结算单导入（2026-09-22）。前端把粘贴的表格解析成车次(一车多料已合并)，
# 这里按 车号 + 日期(±3天) 找已有车次；找不到的可以新建。确认后写磅重明细、结算日期/单号、推到已结算。
# ---------------------------------------------------------------------------
def _match_trips(conn, plate, date):
    if not plate:
        return []
    rows = conn.execute(
        """SELECT t.id, t.status, t.order_date, t.settlement_date, t.settlement_amount_total, t.settlement_tonnage_total,
                  (SELECT COUNT(*) FROM trip_scrap_details d WHERE d.trip_id=t.id) AS detail_count
           FROM trips t WHERE t.plate_no=? AND t.status!='voided'
             AND (? IS NULL OR abs(julianday(coalesce(t.settlement_date, t.order_date)) - julianday(?)) <= 3)
           ORDER BY abs(julianday(coalesce(t.settlement_date, t.order_date)) - julianday(coalesce(?, t.order_date))), t.id DESC""",
        (plate, date, date, date)).fetchall()
    return rows_to_list(rows)


def _lookup_id(conn, table, name):
    if not name:
        return None
    r = conn.execute(f"SELECT id FROM {table} WHERE name=?", (name.strip(),)).fetchone()
    return r["id"] if r else None


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
              dispatcher_id, base_id, invoice_source_id, items:[{scrap_name, deduction, tonnage, unit_price, amount}]}]"""
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
             "settlement_tonnage": it.get("tonnage"), "unit_price": it.get("unit_price") or 0, "amount": it.get("amount")}
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
    trips = conn.execute(
        """SELECT t.*, m.name AS mill_name, a.name AS account_name, b.name AS base_name,
                  b.is_dispatcher_alias AS base_is_alias, d.name AS dispatcher_name, s.name AS invoice_source_name,
                  ft.short_name AS template_short, ft.name AS template_name
           FROM trips t
           LEFT JOIN steel_mills m ON t.mill_id = m.id
           LEFT JOIN accounts a ON t.account_id = a.id
           LEFT JOIN processing_bases b ON t.base_id = b.id
           LEFT JOIN dispatchers d ON t.dispatcher_id = d.id
           LEFT JOIN invoice_sources s ON t.invoice_source_id = s.id
           LEFT JOIN flow_templates ft ON t.template_id = ft.id
           WHERE t.status != 'voided'
           ORDER BY coalesce(t.settlement_date, t.order_date, '') ASC, t.id ASC"""
    ).fetchall()

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
    filename = f"废钢供应链台账_{dt.date.today().strftime('%Y%m%d')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    # v2：启动时自动备份 + 每日备份线程 + 清理过期回收站（只在真正启动服务时跑，测试/脚本 import 不触发）
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        system_ops.start_background()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)
