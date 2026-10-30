"""车次组装/计算/同步（从 core.py 拆出）：取全量、进度、单据面板、mode/template 同步、利润试算入口。"""
import json

import calc
import engine
from db import rows_to_list
from archives_config import SPLIT_MODE_CODES, STATUS_LIST

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
