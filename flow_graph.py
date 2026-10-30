"""链条规则展示（从 core.py 拆出）：模板公式翻译 / 利润快照 / 规则校验 / 流程图组装。"""
import json

import engine
from db import rows_to_list
from archives_config import (REF_CN, ENTITY_CN, TRIP_FIELD_CN, EDITABLE_RATE_TYPES,
                              ROLE_TYPE_TRIP_FIELD, ROLE_ENTITY_TABLE)
from trip_compute import _compute_finance

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
