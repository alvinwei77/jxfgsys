"""核心：Flask 实例、常量、共用工具函数。路由分散在 routes_*.py，由 app.py 统一加载。"""
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
APP_VERSION = system_ops.BOOT_VERSION


@app.context_processor
def _inject_version():
    return {"app_version": APP_VERSION}


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


def _validate_rule_change(conn, node, data):
    """只允许两类修改；返回 (rule_type, config_json) 或抛 ValueError。"""
    old_rule = node["calc_rule_type"]
    old_cfg = json.loads(node["calc_rule_config"] or "{}")
    new_rule = data.get("calc_rule_type")
    cfg = data.get("calc_rule_config") or {}
    if isinstance(cfg, str):  # 允许直接传 JSON 字符串（比如照抄节点接口返回的原值）
        try:
            cfg = json.loads(cfg or "{}")
        except ValueError:
            raise ValueError("口径配置格式不对")
    if not isinstance(cfg, dict):
        raise ValueError("口径配置格式不对")
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


EDGE_EDITABLE_COLS = ("from_role_type", "from_label", "from_entity_type", "from_entity_id",
                       "to_role_type", "to_label", "to_entity_type", "to_entity_id",
                       "hop_type", "node_key_ref", "notes", "seq")


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


def _sync_mode_code_from_template(conn, trip_id, template_id):
    """反方向同步（2026-09-19，界面上把"结算模式"和"链条规则"合并成一个入口之后用）。

    以前车次页选的是 mode_code(M1-M5)，链条规则页显示的是 flow_templates 的名字，
    同一个东西两套名字两个入口，用户每次都要在脑子里翻译一遍（实测库里 68 笔 M1 全部对应
    模板1，一一对应、没有任何例外，纯属冗余）。现在界面上只保留"链条规则"一个入口，
    用户选的是 template_id；mode_code 仍然写回去，因为它还有两个用处：
      1) _ensure_template_id 在 template_id 万一缺失时按 mode_code 补上
      2) 旧数据/导入脚本里还有按 M1/M3 识别的地方
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
    (_compute_finance)返回带 engine_error 的全 0 结果，不让单条车次的数据问题影响其它车次。"""
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


def _compute_finance(conn, trip, overrides=None):
    """车次利润试算的唯一入口：按车次的「链条规则」(template_id) 用 engine 逐节点计算，
    费率来自「费率与关系」，本车改过的费率(trips.finance_overrides)在这里统一合并，
    本次请求临时传的覆盖优先。详情/首页/报表/导出所有算钱的地方都经过这里。

    v2.0.1 起旧的 calc.compute_trip_finance(M1~M5 硬编码) 已下线，不再兜底。万一某车次
    没有链条规则(理论上不会出现，_ensure_template_id 会按 mode_code 补上)，返回全 0 的结果
    并带上 engine_error，页面上能看到原因，不会让整张报表报错。"""
    trip = _ensure_template_id(conn, trip)
    overrides = {**_stored_overrides(trip), **(overrides or {})}
    fin = engine.compute_trip_finance_v2_flat(conn, trip, overrides) if trip["template_id"] else None
    if fin is not None:
        return fin
    empty = {f: 0.0 for f in calc.FIN_FIELDS}
    empty.update({"service_fee_type": None, "engine_error": "这一车没有设置链条规则，利润按 0 计，请在车次详情里选择链条规则"})
    return empty


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




# 路由模块用 `from core import *` 取到这里所有名字（包括下划线开头的工具函数）
__all__ = [n for n in dir() if not n.startswith("__")]
