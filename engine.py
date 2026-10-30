"""计算引擎：用"链条规则(模板) + 计算节点"通用计算每车利润。

系统里唯一的算钱引擎（v2.0.1 起旧的 calc.compute_trip_finance 硬编码实现已下线，
原文件存档在 legacy/calc_v1.py；legacy/compare_v2_*.py 可随时拿它做历史对照）。
对外入口：compute_trip_finance_v2_flat()，由 core._compute_finance() 调用。

节点计费类型 (calc_rule_type) 及其 calc_rule_config 约定，一共10种，够表达现有全部M1-M5公式：

  per_ton_fixed             {"unit_price": <number>}
      金额 = 吨位 × 写死单价（真正"永远不变"的常量才用这个，比如反向自然人垫资费恒为0）
  per_ton_rate_schedule     {"relation_type": "...", "primary_type": "account", "primary_ref": "trip:account_id",
                              "secondary_type": "invoice_source", "secondary_ref": "trip:invoice_source_id"}
      金额 = 吨位 × 按 relation_type+primary(+可选secondary)+车次结算日期，去 rate_links 表查到的
      "生效时间段费率"（查不到按0，见 get_effective_rate 的三级兜底说明）。secondary_type/secondary_ref
      可省略(该费率不区分第二维度)。这样垫资费率、调货员提成(可选按票源细分)、服务费单价(按票源)
      随时间/维度调整都不用改代码，只需在费率档案页面新增一条连接记录。
  per_ton_reward_tier       {}
      金额 = 吨位 × 按钢厂当期累计吨位查 mill_reward_tiers 阶梯得到的单价（本来就是按钢厂+周期区分的）
  rate_deduct_fixed         {"input": "<ref>", "rate_pct": <number>}
      金额 = 引用金额 × (1 − 写死点位% / 100)
  rate_deduct_rate_schedule {"input": "<ref>", "relation_type": "account_external_settlement",
                              "primary_type": "account", "primary_ref": "trip:account_id"}
      金额 = 引用金额 × (1 − 按 relation_type+primary+车次结算日期查到的生效点位% / 100)。
  rate_deduct_invoice_source {"input": "<ref>"}
      金额 = 引用金额 × (1 − 票源方成本点位% / 100)
      （direct类型用票源方自己的rate_point；pass_through类型用outbound_rate_point，复用calc._invoice_cost_rate_pct）
  subtract_nodes            {"minuend": "<ref>", "subtrahend": "<ref>"}
      金额 = 被减数引用 − 减数引用
  rate_spread_invoice_source {"input": "<ref>"}
      金额 = 引用金额 × (转出点位% − 进项点位%) / 100，仅pass_through类型票源方非零
      （复用calc._invoice_spread_profit）
  manual                    {"trip_fields": ["other_cost"]}   （2026-09-22 起切割费客户承担，不再列入）
      金额 = 人工项之和：overrides里有同名key则用override，否则取trip表对应字段
  sum_formula                {"terms": [{"node": "<node_key>", "sign": 1}, ...]}
      金额 = 各引用节点值按符号加总（净利润/留存利润都是这种节点）
  ref_value                 {"input": "<ref>"}
      金额 = 直接取引用值，不做任何变换。用途：doc_types标了"invoice"/"payment"的单据追踪节点，
      需要一个"应收应付基准金额"用于跟实际发票/打款核对，但这个基准本身就是别的节点算出来的
      (比如"销项发票基准"=give_customer)，不需要重新算一遍，直接照抄即可。
      trip_fields留空(=[])的 manual 节点，则表示"没有可计算的基准，纯追踪，只看实际录入的打款"
      （如"前端→实际供货客人"这种我们根本不知道公式的下游款）。

<ref> 引用语法：
  "trip:settlement_amount" / "trip:tonnage" / "trip:account_id" / "trip:dispatcher_id" / "trip:invoice_source_id"
                                              —— 取车次层面的原始字段值
  "node:<node_key>"                          —— 取同一模板内、seq更靠前的另一个节点的计算结果
"""
import json

import calc

# 实体类型 -> 对应档案表名，rate_links.primary_entity_type/secondary_entity_type 的取值范围，
# 供 app.py 解析显示名称、以及关系图页面按类型分组节点时复用，唯一权威定义放这里。
MODE_TO_TEMPLATE_CODE = {
    "M1": "split_company",
    "M5": "split_reverse",
    "M2": "service_fee_direct",
    "M3": "service_fee_account",
    "M4": "service_fee_dispatcher",
}

ENTITY_TABLES = {
    "account": "accounts",
    "dispatcher": "dispatchers",
    "invoice_source": "invoice_sources",
    "processing_base": "processing_bases",
    "steel_mill": "steel_mills",
}

# ---------------------------------------------------------------------------
# 2026-10 期「切割费不含税扣」一次性口径（2026-10-09 用户确认，只这一期）
# ---------------------------------------------------------------------------
# 历史口径：切割费在**含税侧**先扣，再算「给客户付」——
#     结算金额 = 吨位×单价 − 切割费；给客户付 = 结算金额 × (1−对外点位%) − 吨位×吨减。
# 于是切割费被整体 ×(1−14%) 稀释：客户实际只承担 切割费 × 0.86，另外 14% 落进我们口袋。
# 用户 2026-10 期改口径（"切割费计算，不含税扣"）：切割费改到**税后（不含税）侧全额扣**——
#     给客户付 = 吨位×单价 × (1−对外点位%) − 吨位×吨减 − 切割费。
# 两式相减恰好 = −对外点位% × 切割费，所以实现上只需在现有 give_customer 结果上再减这一项，
# 不必去动模板节点结构；窗口外的期次与改造前逐字节相同（零影响）。
# **清空下面的窗口元组 = 整体回退历史口径**（以后哪期要恢复，把日期写进来即可）。
CUTTING_FEE_AFTER_TAX_WINDOW = ("2026-09-26", "2026-10-25")   # 2026-10 期 = 9/26 ~ 10/25


def cutting_fee_after_tax(as_of):
    """该结算日是否适用「切割费在税后全额扣」的一次性口径。"""
    if not as_of or not CUTTING_FEE_AFTER_TAX_WINDOW:
        return False
    d = str(as_of)[:10]
    return CUTTING_FEE_AFTER_TAX_WINDOW[0] <= d <= CUTTING_FEE_AFTER_TAX_WINDOW[1]

# 费率/关系类型目录：每种 relation_type 对应哪个 primary/secondary 实体类型、默认单位、
# 展示用中文名。前端"费率与关系"页面的录入表单、以及本文件/migrate脚本的种子数据都以此为准，
# 新增一种费率类型时只需要在这里加一条 + 在对应模板节点的 calc_rule_config 里引用它。
RELATION_TYPE_CATALOG = [
    # 2026-09-21：垫资费一直都有；只是某些月份反向票源(欧冶介绍的渠道)的垫资费已经算进反向票点成本里，
    # 那段时间对反向就不再单独扣。所以按票源(第二维度，可选)分时段配置：挂"反向"的那条优先于户头通用档。
    {"code": "account_advance_fee", "label": "户头垫资费率", "primary_type": "account",
     "secondary_type": "invoice_source", "rate_unit": "per_ton"},
    {"code": "account_external_settlement", "label": "户头对外结算点位", "primary_type": "account",
     "secondary_type": "dispatcher", "rate_unit": "pct"},
    # 2026-10-04：报价口径改为「含税×(1−14%) − 5元/吨」后新增的吨减维度，按调货人分档
    # （非林凯 = 5 元/吨，林凯落户头兜底档 = 0）。此前只在数据库里有这个 relation_type、
    # 目录里没有，导致"费率与关系"页显示原始代码、且编辑时会因下拉无此项而改错类型。
    {"code": "account_external_settlement_ton_adj", "label": "户头对外结算吨减", "primary_type": "account",
     "secondary_type": "dispatcher", "rate_unit": "per_ton"},
    # 2026-10-13：用户确认「反向每吨不-5元，吨减只属配票报价口径（非林凯）」。
    # 模板2(反向)的 give_customer_adj 节点改挂本关系（默认不配置=0），
    # 与配票共用的 account_external_settlement_ton_adj（非林凯 5 元/吨）不再影响反向路径。
    # #135 净利由 -3,522.52 变为 -6,017.72（去掉 +2,495.20 吨减），用户知情确认。
    {"code": "account_external_settlement_ton_adj_reverse", "label": "户头对外结算吨减(反向专用)", "primary_type": "account",
     "secondary_type": "dispatcher", "rate_unit": "per_ton"},
    {"code": "dispatcher_commission_peiPiao", "label": "调货员配票提成(M1/M5路径)", "primary_type": "dispatcher",
     "secondary_type": "invoice_source", "rate_unit": "per_ton"},
    {"code": "dispatcher_commission_daiPiao", "label": "调货员带票提成(M2/M3/M4路径)", "primary_type": "dispatcher",
     "secondary_type": "invoice_source", "rate_unit": "per_ton"},
    {"code": "service_fee_gross", "label": "服务费单价(按票源方)", "primary_type": "invoice_source",
     "secondary_type": None, "rate_unit": "per_ton"},
]

# 链条规则流程边的"环节类型"目录（2026-09-19新增，取代原先写死在表CHECK约束里的
# invoice/payment/goods三种）。用户反馈"类型不够"：真实链条里 钢厂出结算单 → 我方户头做台账
# → 票源方据台账开票 → 我们开票给国企 → 再逐级往回打款，光靠"开票"一种类型表达不了前两步。
# path_group 决定这条边属于哪条路径视图：
#   ticket   —— 票据/单证流（结算单、台账、发票、过磅单），"票路径"视图只画这一组
#   finance  —— 资金流（打款），"财务路径"视图只画这一组
#   logistics—— 实物流（发货），只在"全部"视图出现
# 以后还缺类型，只要在这里加一条就行：数据库那张表已经去掉了CHECK约束(见
# migrate_hop_types.py)，校验统一在 app.py 按本目录做，前端下拉框也是拉本目录渲染的，
# 不需要再改表结构。color 是关系图上这条边的线和标签颜色，六种互相区分得开。
HOP_TYPE_CATALOG = [
    {"code": "goods", "label": "发货/物流", "path_group": "logistics", "color": "#94a3b8"},
    {"code": "weighing", "label": "过磅单", "path_group": "ticket", "color": "#059669"},
    {"code": "settlement", "label": "出结算单", "path_group": "ticket", "color": "#0891b2"},
    {"code": "ledger", "label": "做台账", "path_group": "ticket", "color": "#7c3aed"},
    {"code": "invoice", "label": "开票", "path_group": "ticket", "color": "#ea580c"},
    {"code": "payment", "label": "打款", "path_group": "finance", "color": "#2563eb"},
]
HOP_TYPE_CODES = {h["code"] for h in HOP_TYPE_CATALOG}
HOP_TYPE_PATH_GROUP = {h["code"]: h["path_group"] for h in HOP_TYPE_CATALOG}


def get_effective_rate(conn, relation_type, primary_entity_id, as_of_date,
                        secondary_entity_id="__unset__"):
    """按 relation_type + primary_entity_id (+可选 secondary_entity_id)，查车次结算日期当天
    生效的费率。三级兜底，逐级放宽，查不到任何一级就返回0（与迁移前"没配置就是0"的兜底行为一致）：
      1) relation_type + primary_entity_id + secondary_entity_id 精确匹配
      2) relation_type + primary_entity_id，忽略secondary(即该实体不区分第二维度的通用费率)
         —— 仅当调用方传了 secondary_entity_id 时才需要这一级；没传secondary时1/2等价，只查一次
      3) relation_type 本身，primary/secondary都不限定(该费率类型的全局兜底值，比如历史上
         服务费统一40元/吨、不挂具体票源时落在这一级)
    生效区间用 effective_from<=日期<=(effective_to或至今) 判断，同一档理论上不应有重叠区间，
    若有则取 effective_from 最新的一条。"""
    as_of = (as_of_date or "")[:10] or None
    has_secondary = secondary_entity_id != "__unset__"

    def _query(p_id, s_id, require_s_null):
        sql = """SELECT rate_value FROM rate_links
                 WHERE relation_type=?
                   AND ((? IS NULL AND primary_entity_id IS NULL) OR primary_entity_id=?)
                   AND effective_from <= ? AND (effective_to IS NULL OR effective_to >= ?)"""
        params = [relation_type, p_id, p_id, as_of, as_of]
        if require_s_null:
            sql += " AND secondary_entity_id IS NULL"
        else:
            sql += " AND ((? IS NULL AND secondary_entity_id IS NULL) OR secondary_entity_id=?)"
            params += [s_id, s_id]
        sql += " ORDER BY effective_from DESC LIMIT 1"
        return conn.execute(sql, params).fetchone()

    row = None
    if has_secondary:
        row = _query(primary_entity_id, secondary_entity_id, require_s_null=False)
    if row is None:
        row = _query(primary_entity_id, None, require_s_null=True)
    if row is None and primary_entity_id is not None:
        row = _query(None, None, require_s_null=True)
    return float(row["rate_value"]) if row else 0.0


def upsert_rate(conn, relation_type, primary_entity_type, primary_entity_id,
                 rate_value, effective_from, rate_unit="per_ton",
                 secondary_entity_type=None, secondary_entity_id=None, notes=None):
    """新增一条生效费率/关系记录，并自动把同 relation_type+primary+secondary 当前
    "至今生效"(effective_to IS NULL)的旧记录截止到新记录生效日的前一天——用户只需要填
    "新费率+从哪天开始"，不用管衔接。"""
    import datetime as _dt
    prev_day = (_dt.date.fromisoformat(effective_from) - _dt.timedelta(days=1)).isoformat()
    conn.execute(
        """UPDATE rate_links SET effective_to=?
           WHERE relation_type=?
             AND ((? IS NULL AND primary_entity_id IS NULL) OR primary_entity_id=?)
             AND ((? IS NULL AND secondary_entity_id IS NULL) OR secondary_entity_id=?)
             AND effective_to IS NULL AND effective_from < ?""",
        (prev_day, relation_type, primary_entity_id, primary_entity_id,
         secondary_entity_id, secondary_entity_id, effective_from),
    )
    cur = conn.execute(
        """INSERT INTO rate_links
           (relation_type, primary_entity_type, primary_entity_id,
            secondary_entity_type, secondary_entity_id, rate_value, rate_unit, effective_from, notes)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (relation_type, primary_entity_type, primary_entity_id,
         secondary_entity_type, secondary_entity_id, rate_value, rate_unit, effective_from, notes),
    )
    conn.commit()
    return cur.lastrowid


def _resolve_ref(ref, trip_ctx, node_values):
    if ref.startswith("trip:"):
        return trip_ctx[ref[5:]]
    if ref.startswith("node:"):
        key = ref[5:]
        if key not in node_values:
            raise ValueError(f"节点引用顺序错误: {key} 还未计算（模板里 seq 要排在引用它的节点前面）")
        return node_values[key]
    raise ValueError(f"无法识别的引用: {ref}")


def _rate_schedule_lookup(cfg, trip_ctx, node_values, conn):
    """per_ton_rate_schedule / rate_deduct_rate_schedule 共用：按 config 里的
    primary_ref(必填)/secondary_ref(选填) 引用取到车次上的实体id，再查 get_effective_rate。
    没配置 secondary_ref 时，不传 secondary 给 get_effective_rate（直接查"不区分该维度"这一级）。"""
    relation_type = cfg["relation_type"]
    primary_id = _resolve_ref(cfg["primary_ref"], trip_ctx, node_values)
    kwargs = {}
    if "secondary_ref" in cfg:
        kwargs["secondary_entity_id"] = _resolve_ref(cfg["secondary_ref"], trip_ctx, node_values)
    return get_effective_rate(conn, relation_type, primary_id, trip_ctx.get("settlement_date"), **kwargs)


def advance_fee_unit_for_trip(conn, trip):
    """这一车适用的垫资费单价(元/吨)：直接按车次所属模板里 advance_fee 节点的配置去查，
    跟利润计算用的是同一条规则(模板节点 + rate_links 生效时段)，票额估算/额度水位等地方
    都调这个，不再各自写死"M5不扣"。查不到模板节点时退回户头通用垫资费率。"""
    t = dict(trip)
    tpl_id = t.get("template_id")
    if not tpl_id and t.get("mode_code") in MODE_TO_TEMPLATE_CODE:
        r = conn.execute("SELECT id FROM flow_templates WHERE code=?", (MODE_TO_TEMPLATE_CODE[t["mode_code"]],)).fetchone()
        tpl_id = r["id"] if r else None
    node = conn.execute(
        "SELECT calc_rule_type, calc_rule_config FROM template_nodes WHERE template_id=? AND node_key='advance_fee'",
        (tpl_id,)).fetchone() if tpl_id else None
    ctx = {**t, "settlement_date": t.get("settlement_date") or t.get("order_date")}
    try:
        _ov = json.loads(t.get("finance_overrides") or "{}").get("rate:advance_fee")
    except (ValueError, TypeError):
        _ov = None
    if _ov not in (None, ""):
        return float(_ov)
    if node:
        cfg = json.loads(node["calc_rule_config"] or "{}")
        if node["calc_rule_type"] == "per_ton_fixed":
            return float(cfg.get("unit_price") or 0)
        if node["calc_rule_type"] == "per_ton_rate_schedule":
            return _rate_schedule_lookup(cfg, ctx, {}, conn)
    return get_effective_rate(conn, "account_advance_fee", t.get("account_id"), ctx["settlement_date"])


# 可逐车覆盖费率的节点类型 → 费率单位（per_ton: 元/吨，pct: 扣减点位%）
RATE_RULE_UNITS = {
    "per_ton_fixed": "per_ton", "per_ton_rate_schedule": "per_ton", "per_ton_reward_tier": "per_ton",
    "rate_deduct_fixed": "pct", "rate_deduct_rate_schedule": "pct", "rate_deduct_invoice_source": "pct",
}
RATE_RULE_SOURCE = {
    "per_ton_fixed": "模板固定值", "per_ton_rate_schedule": "费率与关系", "per_ton_reward_tier": "钢厂量补阶梯",
    "rate_deduct_fixed": "模板固定值", "rate_deduct_rate_schedule": "费率与关系", "rate_deduct_invoice_source": "票源方点位",
}


def _effective_invoice_source(conn, invoice_source, as_of):
    """票源方在 as_of 当天「生效」的档案视图（票点成本可按期配置）。

    `invoice_sources.rate_point / outbound_rate_point` 是**单值列**——改一次就把历史口径一起改了。
    但票点成本是**每期都可能变**的业务量（用户 2026-10-04：「每一期都有新的结算规则和票点成本…
    整个系统你也捋一下，把这种业务自由度、时效性之类的都升级一下，而不是这样写死」）。
    现在统一走「票源方分期」表：先查 `invoice_source_terms` 里 as_of 命中的那条记录，
    若它给了 rate_point / outbound_rate_point 就用它覆盖档案列；
    **没命中、或该字段为空 → 原样返回档案行**，因此"没配分期"的票源方与改造前行为完全一致（零影响）。
    """
    if conn is None or invoice_source is None or not as_of:
        return invoice_source
    try:
        term = conn.execute(
            "SELECT * FROM invoice_source_terms WHERE invoice_source_id=? "
            "  AND date(effective_from)<=date(?) AND (effective_to IS NULL OR date(effective_to)>=date(?)) "
            "ORDER BY date(effective_from) DESC, id DESC LIMIT 1",
            (invoice_source["id"], as_of, as_of),
        ).fetchone()
    except Exception:
        return invoice_source            # 老库还没建这张表 → 直接用档案列
    if term is None:
        return invoice_source
    keys = term.keys()
    rp = term["rate_point"] if "rate_point" in keys else None
    op = term["outbound_rate_point"] if "outbound_rate_point" in keys else None
    if rp is None and op is None:
        return invoice_source
    eff = dict(invoice_source)
    if rp is not None:
        eff["rate_point"] = rp
    if op is not None:
        eff["outbound_rate_point"] = op
    return eff


def _default_rate(rule, cfg, trip_ctx, node_values, conn, default_reward):
    if rule == "per_ton_fixed":
        return float(cfg.get("unit_price", 0) or 0)
    if rule in ("per_ton_rate_schedule", "rate_deduct_rate_schedule"):
        return _rate_schedule_lookup(cfg, trip_ctx, node_values, conn)
    if rule == "per_ton_reward_tier":
        return default_reward
    if rule == "rate_deduct_fixed":
        return float(cfg.get("rate_pct", 0) or 0)
    if rule == "rate_deduct_invoice_source":
        return float(_invoice_point_pct(conn, trip_ctx) or 0)
    return 0.0


def _invoice_point_pct(conn, trip_ctx):
    """票源成本点位（%）：按期取「生效」票点（票源方分期表可覆盖），未配置则回退档案列。"""
    src = _effective_invoice_source(conn, trip_ctx.get("invoice_source"),
                                    trip_ctx.get("settlement_date"))
    return calc._invoice_cost_rate_pct(src)


def _eval_node(rule_type, config_json, trip_ctx, node_values, overrides, conn=None):
    cfg = json.loads(config_json) if config_json else {}
    tonnage = trip_ctx["tonnage"]

    if rule_type == "per_ton_fixed":
        return tonnage * float(cfg.get("unit_price", 0) or 0)

    if rule_type == "per_ton_rate_schedule":
        rate = _rate_schedule_lookup(cfg, trip_ctx, node_values, conn)
        return tonnage * rate

    if rule_type == "per_ton_reward_tier":
        return trip_ctx["reward_unit_price"] * tonnage

    if rule_type == "rate_deduct_fixed":
        base = _resolve_ref(cfg["input"], trip_ctx, node_values)
        return base * (1 - float(cfg.get("rate_pct", 0) or 0) / 100.0)

    if rule_type == "rate_deduct_rate_schedule":
        base = _resolve_ref(cfg["input"], trip_ctx, node_values)
        rate = _rate_schedule_lookup(cfg, trip_ctx, node_values, conn)
        return base * (1 - rate / 100.0)

    if rule_type == "rate_deduct_invoice_source":
        base = _resolve_ref(cfg["input"], trip_ctx, node_values)
        rate = _invoice_point_pct(conn, trip_ctx)
        return base * (1 - float(rate or 0) / 100.0)

    if rule_type == "subtract_nodes":
        a = _resolve_ref(cfg["minuend"], trip_ctx, node_values)
        b = _resolve_ref(cfg["subtrahend"], trip_ctx, node_values)
        return a - b

    if rule_type == "rate_spread_invoice_source":
        base = _resolve_ref(cfg["input"], trip_ctx, node_values)
        src = _effective_invoice_source(conn, trip_ctx.get("invoice_source"),
                                        trip_ctx.get("settlement_date"))
        return calc._invoice_spread_profit(src, base)

    if rule_type == "manual":
        total = 0.0
        for field in cfg.get("trip_fields", []):
            if field in overrides and overrides[field] not in (None, ""):
                total += float(overrides[field])
            else:
                total += float(trip_ctx.get(field) or 0)
        return total

    if rule_type == "ref_value":
        return _resolve_ref(cfg["input"], trip_ctx, node_values)

    if rule_type == "sum_formula":
        total = 0.0
        for term in cfg.get("terms", []):
            total += node_values[term["node"]] * term.get("sign", 1)
        return total

    raise ValueError(f"未知计费类型: {rule_type}")


def _run_template(conn, trip, overrides):
    """两个对外入口(compute_trip_finance_v2 / compute_trip_finance_v2_flat)共用的核心计算：
    按 trip['template_id'] 对应的模板节点，按 seq 顺序逐个求值。返回 None 表示该车次还没有
    template_id 或模板/节点缺失，调用方应回退处理（compute_trip_finance_v2 回退返回None给
    doc_panels等场景；compute_trip_finance_v2_flat返回None给core._compute_finance，
    由它返回带 engine_error 的全0结果，理论上只有历史脏数据才会走到）。"""
    trip = dict(trip)
    template_id = trip.get("template_id")
    if not template_id:
        return None

    template = conn.execute("SELECT * FROM flow_templates WHERE id=?", (template_id,)).fetchone()
    if not template:
        return None
    nodes = conn.execute(
        "SELECT * FROM template_nodes WHERE template_id=? ORDER BY seq", (template_id,)
    ).fetchall()

    tonnage = float(trip.get("settlement_tonnage_total") or 0)
    settlement_amount = float(trip.get("settlement_amount_total") or 0)
    settlement_date = trip.get("settlement_date") or trip.get("order_date")

    account = None
    if trip.get("account_id"):
        account = conn.execute("SELECT * FROM accounts WHERE id=?", (trip["account_id"],)).fetchone()
    invoice_source = None
    if trip.get("invoice_source_id"):
        invoice_source = conn.execute(
            "SELECT * FROM invoice_sources WHERE id=?", (trip["invoice_source_id"],)
        ).fetchone()
    dispatcher_name = None
    if trip.get("dispatcher_id"):
        d = conn.execute("SELECT * FROM dispatchers WHERE id=?", (trip["dispatcher_id"],)).fetchone()
        dispatcher_name = d["name"] if d else None

    mill_id = trip.get("mill_id")
    cycle_start_day, cycle_end_day = calc.get_mill_cycle_days(conn, mill_id)
    period_start, period_end = calc.get_period_bounds(settlement_date, cycle_start_day, cycle_end_day)
    cumulative = calc.get_cumulative_tonnage(conn, mill_id, period_start, period_end)
    reward_unit_price = calc.get_reward_unit_price(conn, mill_id, period_start, period_end, cumulative)
    default_reward = float(reward_unit_price or 0)
    _rw = overrides.get("rate:reward_profit")
    if _rw in (None, ""):
        _rw = overrides.get("reward_unit_price")
    reward_unit_price = float(_rw) if _rw not in (None, "") else default_reward

    trip_ctx = dict(trip)
    trip_ctx.update({
        "tonnage": tonnage,
        "settlement_amount": settlement_amount,
        "settlement_date": settlement_date,
        "account": account,
        "invoice_source": invoice_source,
        "dispatcher_name": dispatcher_name,
        "reward_unit_price": reward_unit_price,
    })

    # "调货人提成"节点是唯一一个前端UI直接暴露"手工改这一车的费率/金额"入口的节点
    # (trips.js「利润分项」编辑区的"调货人提成(元/吨)"输入框，走 dispatcher_commission_rate
    # 这个override key，跟rate_links档案费率是两回事——档案费率改的是"以后所有车次"的默认值，
    # 这个改的是"就这一车不一样")。两个模板家族(split/service_fee)的这个节点都固定叫
    # node_key="dispatcher_commission"，所以在通用节点循环外单独拦一道，其余节点走正常的
    # _eval_node 通用求值，不需要给 calc_rule_type 体系新增"支持override"这种特殊状态。
    # 语义跟 calc.py 原实现完全对齐：rate override优先于cost override，都没有则按正常公式算。
    node_values = {}
    rate_items = []
    for n in nodes:
        node_key = n["node_key"]
        rule = n["calc_rule_type"]
        if node_key == "dispatcher_commission" and (
            overrides.get("dispatcher_commission_cost") not in (None, "")
            and overrides.get("dispatcher_commission_rate") in (None, "")
            and overrides.get("rate:dispatcher_commission") in (None, "")
        ):
            node_values[node_key] = float(overrides["dispatcher_commission_cost"])
            continue
        if rule in RATE_RULE_UNITS:
            # 逐车费率覆盖（2026-09-21）：默认按"费率与关系"档案/模板配置取费率；某一车不一样时，
            # 车次详情里直接改这一车的费率，存在 trips.finance_overrides（键 "rate:<node_key>"），
            # 只影响这一车。旧的 dispatcher_commission_rate / reward_unit_price 覆盖键继续兼容。
            cfg = json.loads(n["calc_rule_config"]) if n["calc_rule_config"] else {}
            default_rate = _default_rate(rule, cfg, trip_ctx, node_values, conn, default_reward)
            ov = overrides.get(f"rate:{node_key}")
            if ov in (None, "") and node_key == "dispatcher_commission":
                ov = overrides.get("dispatcher_commission_rate")
            if ov in (None, "") and rule == "per_ton_reward_tier":
                ov = overrides.get("reward_unit_price")
            overridden = ov not in (None, "")
            rate = float(ov) if overridden else default_rate
            if RATE_RULE_UNITS[rule] == "per_ton":
                node_values[node_key] = tonnage * rate
            else:
                base = _resolve_ref(cfg["input"], trip_ctx, node_values)
                node_values[node_key] = base * (1 - rate / 100.0)
            rate_items.append({"node_key": node_key, "key": f"rate:{node_key}", "label": n["label"],
                               "unit": "元/吨" if RATE_RULE_UNITS[rule] == "per_ton" else "%",
                               "default": round(default_rate, 6), "value": round(rate, 6), "overridden": overridden,
                               "source": RATE_RULE_SOURCE.get(rule, "")})
            continue
        node_values[node_key] = _eval_node(rule, n["calc_rule_config"], trip_ctx, node_values, overrides, conn)

        # 2026-10 期一次性口径（见文件上方 CUTTING_FEE_AFTER_TAX_WINDOW 说明）：
        # 「给客户付」在算完对外点位后，再全额减一次切割费（历史口径里它只被减了 0.86 倍）。
        # 这里改的是节点**结果**而不是模板结构，所以窗口外的期次/车次与改造前逐字节相同。
        if node_key == "give_customer" and cutting_fee_after_tax(settlement_date):
            _cut = float(trip.get("cutting_fee_total") or 0)
            if _cut:
                _pct = next((it["value"] for it in rate_items if it["node_key"] == "give_customer_pct"), 0.0)
                node_values[node_key] -= _cut * float(_pct or 0) / 100.0

    net_profit_node = next(
        (n for n in nodes if n["calc_rule_type"] == "sum_formula" and not n["is_reserved_component"]), None
    )
    reserved_node = next((n for n in nodes if n["is_reserved_component"]), None)
    net_profit = node_values.get(net_profit_node["node_key"], 0.0) if net_profit_node else 0.0
    reserved_profit = node_values.get(reserved_node["node_key"], 0.0) if reserved_node else 0.0

    return {
        "trip": trip, "template": template, "nodes": nodes, "node_values": node_values,
        "tonnage": tonnage, "settlement_amount": settlement_amount,
        "net_profit": net_profit, "reserved_profit": reserved_profit,
        "reward_unit_price": reward_unit_price, "cumulative": cumulative, "rate_items": rate_items,
        "period_start": period_start, "period_end": period_end,
        "account": account, "invoice_source": invoice_source,
    }


def compute_trip_finance_v2(conn, trip, overrides=None):
    """与 calc.compute_trip_finance 平行的新实现：按 trip['template_id'] 对应的模板节点逐个计算。
    trip 必须已经关联了 template_id（本函数不做 mode_code->template_id 的推断，调用方负责）。
    返回 None 表示该车次还没有 template_id，调用方应回退到旧引擎或跳过。
    只返回"通用节点视图"(net_profit/留存利润/nodes字典)，给doc_panels/进度节点化/关系图这类
    只关心节点值本身的场景用；需要跟现网UI(利润分项表格/首页/报表/导出)字段完全兼容的场景，
    见下面的 compute_trip_finance_v2_flat()。"""
    overrides = overrides or {}
    r = _run_template(conn, trip, overrides)
    if r is None:
        return None
    template = r["template"]
    return {
        "template_id": template["id"],
        "template_code": template["code"],
        "template_name": template["name"],
        "tonnage": r["tonnage"],
        "settlement_amount": r["settlement_amount"],
        "nodes": r["node_values"],
        "net_profit": r["net_profit"],
        "invoice_diff_profit": r["reserved_profit"],
        "reward_unit_price": r["reward_unit_price"],
        "period_cumulative_tonnage": r["cumulative"],
        "period_start": r["period_start"],
        "period_end": r["period_end"],
    }


# 任务#30(2026-09-18)全面切换引擎：这个函数是app.py现在唯一实际调用的计算入口
# (通过app.py::_compute_finance())。它在compute_trip_finance_v2()的通用节点计算之上，
# 把结果重新整理成跟 calc.compute_trip_finance() 的返回字典字段名/含义完全一致的"扁平视图"
# ——app.py/dashboard.js/trips.js/导出台账等所有下游代码完全不用区分是哪个引擎算的，
# 字段名、字段含义、金额都保持不变，只是取数路径从"mode_code+硬编码if/else"换成了
# "template_id+节点链(rate_links驱动费率)"。字段映射关系(split家族 vs service_fee家族)见下方
# 两个分支的注释，跟 calc.py 逐行对照过，历史87笔车次已用独立脚本验证过全字段一致，不只是
# net_profit/留存利润两个数字(那是 compare_v2_engine.py 验证的范围)。
def compute_trip_finance_v2_flat(conn, trip, overrides=None):
    """返回 None 表示该车次没有template_id/模板缺失，调用方(core._compute_finance)会给出
    带 engine_error 的全0结果。"""
    overrides = overrides or {}
    r = _run_template(conn, trip, overrides)
    if r is None:
        return None
    trip_row = r["trip"]
    template = r["template"]
    nv = r["node_values"]
    tonnage = r["tonnage"]
    calc_family = template["calc_family"]  # 'split' 或 'service_fee'，跟 calc.py 的 calc_type 同义同值

    # ticket_cost(买票成本)、service_fee_type/service_fee_value：这三个字段两个引擎都不是
    # "算"出来的(v2的模板节点里根本没有对应节点，calc.py里也只是手工录入/沿用旧值)，纯粹是
    # "手工录入或沿用上次保存值"的元数据，逻辑原样从calc.py搬过来，保证行为完全一致
    # (包括ticket_cost从不真正参与net_profit扣减这个历史怪癖，见V2_ARCHITECTURE.md第6节)。
    existing_fin = conn.execute("SELECT * FROM trip_finance WHERE trip_id=?", (trip_row["id"],)).fetchone()
    existing_fin = dict(existing_fin) if existing_fin else {}

    def pick(key, default=0):
        if key in overrides and overrides[key] not in (None, ""):
            return float(overrides[key])
        if existing_fin.get(key) not in (None, ""):
            return float(existing_fin[key])
        return default

    ticket_cost = pick("ticket_cost", 0)

    base = None
    if trip_row.get("base_id"):
        base = conn.execute("SELECT * FROM processing_bases WHERE id=?", (trip_row["base_id"],)).fetchone()
    service_fee_type = overrides.get("service_fee_type") or existing_fin.get("service_fee_type") or (
        base["service_fee_type"] if base else "per_ton"
    )
    service_fee_value_raw = overrides.get("service_fee_value", existing_fin.get("service_fee_value"))

    # cutting_fee：2026-09-22 起切割费由客户承担（结算金额里已扣），other_cost 节点只含手工其他成本；
    # 这里的 cutting_fee 只用于页面展示。以下为旧说明：v2的 other_cost 节点内部把 cutting_fee_total(trip字段) + other_cost(trip字段)
    # 合并计算了(见migrate_v2_templates.py::SPLIT_SHARED_TAIL_NODES/SERVICE_FEE_NODES的
    # other_cost节点定义)，但前端"利润分项"表格要把切割费单独一行展示(区别于手工其他成本)，
    # 所以这里跟calc.py一样单独读一次trip字段，不影响 other_cost 节点本身的计算结果。
    cutting_fee = float(trip_row.get("cutting_fee_total") or 0)
    other_cost = nv.get("other_cost", 0.0)

    dispatcher_commission_cost = nv.get("dispatcher_commission", 0.0)
    dispatcher_commission_rate = (dispatcher_commission_cost / tonnage) if tonnage else 0.0

    if calc_family == "split":
        # 路径1(M1配票)/路径3(M5反向)差价模型，节点名跟calc.py字段名的对应关系：
        #   give_customer(节点)      = give_customer(calc.py同名变量，结算金额×(1-对外结算点位%)，
        #                              点位现在从rate_links(account_external_settlement)取，
        #                              历史数据跟原account表字段数值相同，已用87笔验证)
        #   advance_fee(节点)        = advance_fee_raw(calc.py)，M5恒为0、M1=吨位×rate_links垫资费率
        #   front_invoice_amount     = 前端开票金额 = 结算金额 - advance_fee
        #   front_actual_paid        = 前端实付 = front_invoice_amount × (1-票源成本点位%)
        #   personal_advance(节点)   = 个人垫资 = give_customer - front_actual_paid
        #                              = FIN_FIELDS.advance_fee_cost(calc.py里"垫资成本"这个
        #                              对外字段名，容易和"advance_fee"节点本身混淆，命名是calc.py
        #                              历史遗留，这里原样保留兼容)
        #   invoice_reserved(节点)   = 票差留存利润 = FIN_FIELDS.invoice_diff_profit
        give_customer = nv.get("give_customer", 0.0)
        advance_fee_raw = nv.get("advance_fee", 0.0)
        front_invoice_amount = nv.get("front_invoice_amount", 0.0)
        front_actual_paid = nv.get("front_actual_paid", 0.0)
        personal_advance = nv.get("personal_advance", 0.0)

        extra_detail = {
            "give_customer": give_customer,
            "advance_fee_raw": advance_fee_raw,
            "front_invoice_amount": front_invoice_amount,
            "front_actual_paid": front_actual_paid,
            "personal_advance": personal_advance,
            "invoice_spread_profit": r["reserved_profit"],
            "cutting_fee": cutting_fee,
        }
        _items = {i["node_key"]: i["value"] for i in r["rate_items"]}
        rate_out = _items.get("give_customer", get_effective_rate(
            conn, "account_external_settlement", trip_row.get("account_id"),
            trip_row.get("settlement_date") or trip_row.get("order_date")))
        rate_in = _items.get("front_actual_paid", calc._invoice_cost_rate_pct(
            _effective_invoice_source(conn, r["invoice_source"],
                                      trip_row.get("settlement_date") or trip_row.get("order_date"))))
        advance_fee_cost = personal_advance
        advance_fee_rate = (personal_advance / tonnage) if tonnage else 0.0
        service_fee_value = float(service_fee_value_raw or 0)
        service_fee_income = 0.0
    else:
        # 路径2(M2/M3/M4)带票带货服务费模型：
        #   service_fee_income(节点) = 毛服务费 = 吨位 × rate_links(service_fee_gross，按票源方，
        #                              查不到该票源专属档时退化到全局兜底40元/吨，跟calc.py旧的
        #                              全局常量SERVICE_FEE_GROSS_RATE历史数值一致，已用87笔验证)
        #   advance_fee(节点)        = 垫资成本 = 吨位 × rate_links(account_advance_fee)
        #   dispatcher_commission(节点) = 调货人提成(带票带货费率档dispatcher_commission_daiPiao)
        service_fee_income = nv.get("service_fee_income", 0.0)
        advance_fee_cost = nv.get("advance_fee", 0.0)
        advance_fee_rate = (advance_fee_cost / tonnage) if tonnage else 0.0
        extra_detail = {
            "gross_service_income": service_fee_income,
            "cutting_fee": cutting_fee,
        }
        rate_out = None
        rate_in = None
        service_fee_value = float(
            service_fee_value_raw if service_fee_value_raw not in (None, "") else calc.SERVICE_FEE_GROSS_RATE
        )

    return {
        **extra_detail,
        "trip_id": trip_row["id"],
        "calc_type": calc_family,
        "template_id": template["id"],
        "template_code": template["code"],
        "template_name": template["name"],
        "rate_items": r["rate_items"],
        "tonnage": tonnage,
        "settlement_amount": r["settlement_amount"],
        "freight_diff_unit_price": 0.0,   # 历史怪癖：从未真正启用过，恒为0，calc.py同样如此
        "freight_diff_profit": 0.0,       # 同上
        "rate_out": rate_out,
        "rate_in": rate_in,
        "invoice_diff_profit": r["reserved_profit"],
        "reward_unit_price": r["reward_unit_price"],
        "reward_profit": nv.get("reward_profit", 0.0),
        "period_cumulative_tonnage": r["cumulative"],
        "period_start": r["period_start"],
        "period_end": r["period_end"],
        "advance_fee_rate": advance_fee_rate,
        "advance_fee_cost": advance_fee_cost,
        "dispatcher_commission_rate": dispatcher_commission_rate,
        "dispatcher_commission_cost": dispatcher_commission_cost,
        "service_fee_type": service_fee_type,
        "service_fee_value": service_fee_value,
        "service_fee_income": service_fee_income,
        "ticket_cost": ticket_cost,
        "other_cost": other_cost,
        "net_profit": r["net_profit"],
    }
