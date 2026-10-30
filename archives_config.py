"""档案/状态/链条规则/统计分桶定义等常量（从 core.py 机械拆出）。纯数据，无依赖。"""

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
    # 票源方「分期额度 / 接单状态 / 票点成本」（2026-10-04）：票额按月给、中途停止接单、票点每期变化
    # 这类时效性自由度都在这里，不再写死在 invoice_sources 的单值列上。水位计算按"今天"命中哪条就用哪条；
    # 票点成本（rate_point / outbound_rate_point）由 engine._effective_invoice_source() 在算钱前覆盖，
    # 留空 = 沿用票源方档案列（零影响）。
    "invoice_source_terms": ["invoice_source_id", "effective_from", "effective_to", "quota_amount", "warn_threshold",
                             "rate_point", "outbound_rate_point", "accepting", "notes"],
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
