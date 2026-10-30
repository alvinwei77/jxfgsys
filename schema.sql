-- 废钢供应链信息系统 数据库结构 v0.1
PRAGMA foreign_keys = ON;

-- ========== 基础档案 ==========

CREATE TABLE IF NOT EXISTS steel_mills ( -- 钢厂
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    is_whitelist INTEGER NOT NULL DEFAULT 0,      -- 是否国家白名单钢厂（分类，暂不影响计算）
    settlement_terms TEXT,                        -- 结算账期规则（开放文本，如"货到45天"）
    quality_contact TEXT,                          -- 质检部联系方式
    entry_requirement TEXT,                        -- 进场要求备注（如"仅支持国六标准"）
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS mill_reward_tiers ( -- 钢厂量补阶梯规则（每钢厂可配置多档，全额累进）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mill_id INTEGER NOT NULL REFERENCES steel_mills(id) ON DELETE CASCADE,
    min_tonnage REAL NOT NULL,                     -- 达到该吨位起
    unit_price REAL NOT NULL,                      -- 对应奖励单价 元/吨
    period_start_day INTEGER NOT NULL DEFAULT 26,  -- 结算周期起始日（默认26日）
    period_end_day INTEGER NOT NULL DEFAULT 25,    -- 结算周期截止日（默认次月25日）
    notes TEXT
);

CREATE TABLE IF NOT EXISTS financiers ( -- 国企/垫资方
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    credit_info TEXT,                              -- 授信信息
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS accounts ( -- 户头（钢厂 x 垫资方 的组合，部分钢厂无需国企资质）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                            -- 户头名称，如"华西-欧冶户头"
    mill_id INTEGER NOT NULL REFERENCES steel_mills(id),
    financier_id INTEGER REFERENCES financiers(id), -- 可为空：该钢厂户头不需要国企资质
    is_our_own INTEGER NOT NULL DEFAULT 0,          -- 是否我方专属户头
    advance_fee_rate REAL DEFAULT 23,               -- 垫资费率 元/吨（无国企垫资时可为0/NULL）
    external_settlement_rate REAL,                  -- 对外统一结算点位 %，如14.0，用于票差计算
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS processing_bases ( -- 加工基地（我方 + 外部合作）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    base_type TEXT NOT NULL DEFAULT 'external' CHECK(base_type IN ('own','external')), -- 我方/外部
    admission_level TEXT NOT NULL DEFAULT 'normal' CHECK(admission_level IN ('national','normal')), -- 国家准入/普通（暂不影响计算）
    risk_approved INTEGER NOT NULL DEFAULT 0,       -- 是否国企风控直签准入
    default_mode_code TEXT DEFAULT 'M3',            -- 默认结算模式
    service_fee_type TEXT DEFAULT 'per_ton' CHECK(service_fee_type IN ('per_ton','rate')), -- 服务费口径：固定元/吨 或 结算金额比例
    service_fee_value REAL,                         -- 服务费数值（元/吨 或 百分比）
    contact TEXT,
    notes TEXT,
    is_dispatcher_alias INTEGER NOT NULL DEFAULT 0, -- 2026-09-19新增：历史台账导入时把"货主"字段
        -- 自动建成了一条同名"供货厂/基地"记录，但实际这条车次并没有独立的外部供货厂——货是调货员
        -- 本人直接带的。=1表示这条基地记录只是某个调货员的同名占位，不是真实供货厂主体，仅用于
        -- 关系图/基地列表做视觉区分与后续人工清理提示，不影响任何计算(base_id本身不改，计算逻辑
        -- 里base为空和这类"无追加费率"的占位base结果一致)。
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS dispatchers ( -- 外部调货人
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    commission_per_ton REAL DEFAULT 0,              -- 提成单价 元/吨（不同调货人不同）
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS invoice_sources ( -- 票源方：前端公司 + 反向自然人 统一表
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('front_company','reverse_individual')),
    tax_type TEXT,                                  -- 3%简易征收 / 13%一般纳税人 / 反向
    rate_point REAL,                                -- 点位 %，如12.8
    quota_period TEXT DEFAULT 'monthly' CHECK(quota_period IN ('monthly','annual')),
    quota_amount REAL,                              -- 额度上限（前端：月度；反向自然人：年度默认210万）
    warn_threshold REAL,                            -- 预警阈值（反向自然人默认180万）
    bound_mill_id INTEGER REFERENCES steel_mills(id),      -- 反向自然人常绑定单一钢厂
    bound_base_id INTEGER REFERENCES processing_bases(id), -- 反向自然人常绑定单一供货厂
    contact TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS invoice_source_terms ( -- 票源方「分期额度 / 接单状态」（2026-10-04）
    -- 业务上一家前端公司的票额是按月给的（这个月有、下个月可能就没了），反向自然人也会中途停止接单；
    -- 这些都随时间变化，不能写死成 invoice_sources 上的单一数值。本表按时段覆盖：
    --   水位计算时先按"今天"找命中的那一行；找不到就用 invoice_sources 上的默认值（向后兼容）。
    --   accepting=0 → 停用/不再接单，不计入水位；quota_amount 为空 → 本期无票额，也不计入。
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_source_id INTEGER NOT NULL REFERENCES invoice_sources(id) ON DELETE CASCADE,
    effective_from TEXT NOT NULL,                   -- 生效起始日（含）
    effective_to TEXT,                              -- 生效截止日（含）；NULL = 长期/至今
    quota_amount REAL,                              -- 该时段额度上限；NULL = 无额度（如本期没票额）
    warn_threshold REAL,                            -- 该时段预警线；NULL = 沿用档案默认
    accepting INTEGER NOT NULL DEFAULT 1,           -- 1=接单中；0=已停用（不再配票/接单）
    rate_point REAL,                                -- 该时段票源成本点位%；NULL = 沿用档案 invoice_sources.rate_point
    outbound_rate_point REAL,                       -- 该时段对外转出点位%(pass_through)；NULL = 沿用档案
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_source_terms_src ON invoice_source_terms(invoice_source_id, effective_from);

CREATE TABLE IF NOT EXISTS invoice_source_vehicles ( -- 反向自然人绑定车辆
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_source_id INTEGER NOT NULL REFERENCES invoice_sources(id) ON DELETE CASCADE,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS drivers ( -- 司机档案
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    id_card_no TEXT,
    license_photo_path TEXT,                        -- 驾驶证照片
    id_card_photo_path TEXT,                         -- 身份证照片
    license_expiry TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS vehicles ( -- 车辆/船舶档案
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_no TEXT NOT NULL UNIQUE,                   -- 车牌号/船号
    transport_type TEXT NOT NULL DEFAULT 'truck' CHECK(transport_type IN ('truck','ship')),
    default_driver_id INTEGER REFERENCES drivers(id),
    vehicle_license_photo_path TEXT,                 -- 行驶证照片
    env_list_photo_path TEXT,                        -- 环保清单照片（首次半挂车）
    is_first_trailer_checked INTEGER DEFAULT 0,       -- 是否已完成首次半挂车环保清单核验
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS scrap_types ( -- 货型字典
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,                          -- 废钢类型大类，如"废钢""铁水罐压块类""废钢压块类""破碎料类"
    name TEXT NOT NULL,                               -- 具体货型名称，如"小废""中废1"
    example_desc TEXT,                                -- 品种典型举例
    size_requirement TEXT,                            -- 外形尺寸要求
    thickness_requirement TEXT,                        -- 厚度要求
    UNIQUE(category, name)
);

CREATE TABLE IF NOT EXISTS trip_modes ( -- 车次结算模式 M1-M5（v2起逐步由 flow_templates 取代，过渡期并存）
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    calc_type TEXT NOT NULL CHECK(calc_type IN ('split','service_fee'))
    -- split: 货差+票差+量补-垫资-调货人提成-买票成本-其他成本
    -- service_fee: 服务费收入+量补-其他成本
);

-- ========== v2 流程模板引擎（2026-09-18新增） ==========
-- 用"模板+节点"取代 calc.py 里硬编码的 if/else 分支：一个模板由若干有序节点组成，
-- 每个节点用固定的计费规则类型(calc_rule_type)算出一个金额，节点之间可以互相引用
-- (calc_rule_config 里用 "node:<node_key>" / "trip:settlement_amount" / "trip:tonnage" 这种
-- 字符串做引用)，最终"净利润"/"留存利润"也只是普通节点，用 sum_formula 把其它节点加总。
-- 这样以后业务流程变体再多，都是加/改模板配置，不用再动 Python 代码。
CREATE TABLE IF NOT EXISTS flow_templates ( -- 结算流程模板
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,             -- 模板代码，如 split_company / split_reverse / service_fee
    name TEXT NOT NULL,                    -- 展示名，如"自营/外部配票-差价模型"
    short_name TEXT,                       -- 简写，车次列表等处显示，如"配票"(2026-09-21)
    calc_family TEXT NOT NULL CHECK(calc_family IN ('split','service_fee')), -- 沿用现有两大类，便于展示分组
    description TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS template_nodes ( -- 模板节点（有序，构成一条计算链）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES flow_templates(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,                  -- 排序，决定计算/展示顺序
    node_key TEXT NOT NULL,                -- 模板内唯一标识，供其它节点通过 "node:xxx" 引用
    label TEXT NOT NULL,                   -- 展示名，如"垫资成本"
    role_type TEXT,                        -- 角色标签，仅展示/流程图分组用，不参与计算：
                                            -- supplier(供货厂)/dispatcher(调货员)/our_account(我方户头)/
                                            -- ticket_source(票源方)/soe(国企)/mill(钢厂)/output(结果)
    doc_types TEXT,                        -- JSON数组，这个节点关联的单据类型，如 ["invoice","payment"]；
                                            -- 可选值 weighing_slip/settlement_doc/invoice/payment；纯计算节点留空
    calc_rule_type TEXT NOT NULL CHECK(calc_rule_type IN (
        'per_ton_fixed', 'per_ton_rate_schedule', 'per_ton_reward_tier',
        'rate_deduct_fixed', 'rate_deduct_rate_schedule', 'rate_deduct_invoice_source',
        'subtract_nodes', 'rate_spread_invoice_source', 'manual', 'sum_formula', 'ref_value'
    )),
    calc_rule_config TEXT,                 -- JSON，参数因 calc_rule_type 而异，见 engine.py 顶部注释
    is_profit_component INTEGER NOT NULL DEFAULT 0,  -- 是否计入主营净利润的 sum_formula
    profit_sign INTEGER,                   -- +1/-1，仅 is_profit_component=1 时有意义
    is_reserved_component INTEGER NOT NULL DEFAULT 0, -- 是否是"留存利润"专项节点（不进主营净利润）
    UNIQUE(template_id, node_key)
);

CREATE TABLE IF NOT EXISTS trip_node_values ( -- 车次逐节点计算结果快照 + 单据追踪（2026-09-18新增后两列，
    -- 配合"两票三款"通用化：doc_types含"invoice"的节点用 doc_no/actual_amount/doc_status 记录
    -- 实际发票号/金额/状态；doc_types含"payment"的节点，实际打款记录仍在 payments 表，
    -- 通过 payments.node_key 关联到这里，本表只存"应收应付基准"(computed_value，来自ref_value等
    -- 节点的计算结果，用于核对页面跟实际打款合计做差额对比)。
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    node_key TEXT NOT NULL,
    computed_value REAL,
    override_value REAL,                   -- 人工覆盖值，优先于 computed_value
    doc_no TEXT,                            -- 单据号(仅doc_types含invoice的节点使用)
    actual_amount REAL,                     -- 实际发票金额(仅doc_types含invoice的节点使用，跟computed_value的"基准"分开存，不互相推算)
    doc_status TEXT,                        -- 单据状态，如 'in_stock'(票库存中/待恢复开票)，NULL=正常
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (trip_id, node_key)
);

CREATE TABLE IF NOT EXISTS template_flow_edges ( -- 链条规则-业务流程边（2026-09-19新增）：
    -- rate_links的关系图是"实体两两之间的费率关系"，跟flow_templates/template_nodes(v2计算引擎
    -- 用的计算链)是两套不相关的数据，都不是用户要的"一条链条规则从头到尾展示业务流转"的图。
    -- 这张表专门给"链条规则"页面的关系图用：按seq顺序把一条链条规则(=一个flow_template)从头到尾
    -- 会经过哪些方(from_label/to_label，可以是抽象角色也可以是具体名字如"欧冶")、每一步是开票/
    -- 打款/纯发货(hop_type)，一条一条列出来。跟计算引擎的关系是"软关联"：node_key_ref可选填
    -- template_nodes.node_key，填了的话前端渲染这一步时能顺带显示这个节点算出来的金额(编辑器里
    -- 显示模板口径，车次详情页显示这一笔车次的实际数字)，不填就是纯展示性的流程说明(比如物流环节
    -- "林凯->XXX厂发货"，本身不产生独立的计算金额，不需要关联任何计算节点)。
    -- 一条链条规则可以有多条edges，前端按seq顺序把不重复出现的from/to方连成一条流程图；
    -- "票路径"视图画 path_group=ticket 的边(结算单/台账/发票/过磅单)，"财务路径"视图画
    -- path_group=finance 的边(打款)，"全部"视图都画(goods物流边只在全部视图出现)。
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES flow_templates(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    from_role_type TEXT NOT NULL,   -- 复用template_nodes.role_type同一套词汇，仅用于图上配色分组，不参与计算
    from_label TEXT NOT NULL,       -- 起点展示名，可以是具体名字(如"欧冶")也可以是通用角色名(如"票源方")
    to_role_type TEXT NOT NULL,
    to_label TEXT NOT NULL,
    -- 环节类型：goods发货/weighing过磅单/settlement出结算单/ledger做台账/invoice开票/payment打款。
    -- 这里故意不写CHECK约束：用户反馈过"类型不够"，业务上还会继续补类型，写死在表结构里每加
    -- 一种就要重建表。唯一权威定义在 engine.HOP_TYPE_CATALOG，校验在 app.py 按那份目录做。
    hop_type TEXT NOT NULL,
    node_key_ref TEXT,              -- 可选，关联template_nodes.node_key，用于显示这一步的实际/口径金额
    notes TEXT,
    -- 2026-09-19：一端可以"绑定到某个具体档案实体"，而不只是手打名字。绑了之后渲染取档案里的
    -- 当前名字，以后改名图上自动跟着变；没绑且角色是可替换的5种之一，就是"角色占位"——车次图里
    -- 换成该车次的真实主体。区别很重要：链条规则一条覆盖很多笔车次，这些车次的票源方/供货厂
    -- 各不相同，所以默认应该是占位；只有真正固定不变的那几方（比如某个垫资通道户头）才该绑死。
    from_entity_type TEXT,
    from_entity_id INTEGER,
    to_entity_type TEXT,
    to_entity_id INTEGER,
    -- 这条边是系统按计算节点自动推导出来的初版、还没人工核实过；用户编辑过任意字段就置0。
    is_auto INTEGER NOT NULL DEFAULT 0,
    UNIQUE(template_id, seq)
);

CREATE TABLE IF NOT EXISTS rate_links ( -- 费率与关系档案（2026-09-18新增，取代初版rate_schedules）：
    -- 不仅仅是"户头/调货员各自一个费率数字"，而是把费率本身建模成"两个实体之间的一条带生效时间段
    -- 的连接"：可以是单实体属性(如户头的垫资费率，secondary留空)，也可以是两个实体间的关系
    -- (如"某调货员+某票源"这个组合的提成单价，secondary填票源)。同一份数据既驱动引擎计算取数，
    -- 也用于渲染业务关系图(供货厂/调货员-票源-户头-钢厂)。
    -- 取数规则(engine.get_effective_rate)三级兜底：
    --   1) relation_type+primary+secondary 精确匹配(该时间点生效的一条)
    --   2) 找不到则退化为 relation_type+primary，忽略secondary(即该实体的通用费率)
    --   3) 找不到则退化为 relation_type 本身、primary/secondary都不限定(该费率类型的全局兜底值)
    --   4) 仍找不到则按0（与迁移前"没配置就是0"的兜底行为保持一致）
    -- 新增一条时把上一条同 relation_type+primary+secondary 的"至今生效"(effective_to IS NULL)旧记录
    -- 自动截止到新记录生效日前一天，用户只需要填"新费率+从哪天开始"。
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_type TEXT NOT NULL,    -- 费率/关系类型代码，不做死枚举(未来可在页面上新增类型)：
                                     -- account_advance_fee(户头垫资费率,元/吨) /
                                     -- account_external_settlement(户头对外结算点位,%) /
                                     -- dispatcher_commission_peiPiao(调货员配票路径提成,元/吨，可选按票源细分) /
                                     -- dispatcher_commission_daiPiao(调货员带票路径提成,元/吨，可选按票源细分) /
                                     -- service_fee_gross(服务费单价,元/吨，按票源方区分，无票源时走全局兜底)
    primary_entity_type TEXT NOT NULL CHECK(primary_entity_type IN
        ('account','dispatcher','invoice_source','processing_base','steel_mill')),
    primary_entity_id INTEGER,      -- 可空=该relation_type的全局兜底值(如历史服务费40元/吨,不挂具体票源)
    secondary_entity_type TEXT CHECK(secondary_entity_type IN
        ('account','dispatcher','invoice_source','processing_base','steel_mill') OR secondary_entity_type IS NULL),
    secondary_entity_id INTEGER,    -- 可空=不区分该维度(如提成不区分票源时留空)
    rate_value REAL NOT NULL,
    rate_unit TEXT NOT NULL DEFAULT 'per_ton' CHECK(rate_unit IN ('per_ton','pct')), -- 元/吨 或 百分比%，仅展示用，避免录入时单位混淆
    effective_from TEXT NOT NULL,   -- 生效起始日(含)
    effective_to TEXT,              -- 生效截止日(含)，NULL=至今仍生效
    notes TEXT,                     -- 如"钢厂2026年9月新报价"
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_rate_links_lookup
    ON rate_links(relation_type, primary_entity_id, secondary_entity_id, effective_from);

-- ========== 报价单 ==========

CREATE TABLE IF NOT EXISTS quote_sheets ( -- 报价单期号（按钢厂+生效时间）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mill_id INTEGER NOT NULL REFERENCES steel_mills(id),
    period_label TEXT,                                -- 期号展示文本，如"2026-09-12 12时"
    effective_at TEXT NOT NULL,                        -- 生效时间戳，用于排序和"最新一期"判断
    footer_notes TEXT,                                 -- 页脚全局规则说明（富文本/文本）
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS quote_items ( -- 报价单明细
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_sheet_id INTEGER NOT NULL REFERENCES quote_sheets(id) ON DELETE CASCADE,
    scrap_type_id INTEGER NOT NULL REFERENCES scrap_types(id),
    price REAL,                                        -- 本期价格；NULL 表示停收
    is_suspended INTEGER DEFAULT 0                      -- 是否停收
);

-- ========== 车次（核心单据） ==========

CREATE TABLE IF NOT EXISTS trips ( -- 车次/船次主表
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transport_type TEXT NOT NULL DEFAULT 'truck' CHECK(transport_type IN ('truck','ship')),
    -- 状态精简（2026-09-17）：只保留 已报备/已到厂/已结算/已开票/已结案/作废 6个状态，
    -- 旧的 待报备/在途/已过毛磅/已过皮磅/已回款 不再使用（老库数据通过迁移脚本归并，
    -- 已回款信息改为"已开票"状态下的打款记录，见 payments 表）
    status TEXT NOT NULL DEFAULT 'reported'
        CHECK(status IN ('reported','arrived','settled','invoiced','closed','voided')),
    mode_code TEXT REFERENCES trip_modes(code),        -- M1-M5（v2引擎上线前的现役字段，暂不删除）
    template_id INTEGER REFERENCES flow_templates(id), -- v2新引擎用：结算流程模板，与mode_code并存过渡

    order_date TEXT,                                   -- 接单日期
    base_id INTEGER REFERENCES processing_bases(id),   -- 供货厂
    dispatcher_id INTEGER REFERENCES dispatchers(id),   -- 调货人（如适用）
    mill_id INTEGER REFERENCES steel_mills(id),
    account_id INTEGER REFERENCES accounts(id),         -- 户头
    invoice_source_id INTEGER REFERENCES invoice_sources(id), -- 票源方（用于票差计算）

    vehicle_id INTEGER REFERENCES vehicles(id),
    plate_no TEXT,                                      -- 冗余：车牌号/船号，便于展示与导入历史数据
    driver_id INTEGER REFERENCES drivers(id),
    driver_name TEXT,                                    -- 冗余
    driver_phone TEXT,
    driver_id_card TEXT,
    departure_place TEXT,                                -- 发货地
    expected_arrival TEXT,                                -- 预计到厂时间
    remarks TEXT,                                          -- 备注（如国六标准限制）

    mixed_material INTEGER DEFAULT 0,                     -- 混料标记（无法/无需逐货型拆分时使用）
    water_tank_confirmed INTEGER DEFAULT 0,               -- 水箱清空确认

    -- 磅重与结算（车次汇总层，明细在 trip_scrap_details）
    net_weight_total REAL,                                -- 结算净重合计（吨）
    deduction_total REAL,                                 -- 扣杂合计（吨）
    settlement_tonnage_total REAL,                        -- 结算吨数合计（钢厂结算为准）
    cutting_fee_total REAL DEFAULT 0,                      -- 切割费合计
    settlement_amount_total REAL,                          -- 结算金额合计（含税）
    pay_to_base_amount REAL DEFAULT 0,                     -- 付加工厂金额
    other_cost REAL DEFAULT 0,                              -- 其他成本

    settlement_doc_no TEXT,                                 -- 结算单编号
    settlement_doc_path TEXT,                                -- 结算单图片
    settlement_date TEXT,                                     -- 结算日期
    arrived_date TEXT,                                        -- 已到厂日期(A阶段：推进时自动填)
    invoiced_date TEXT,                                       -- 已开票日期(同上)
    closed_date TEXT,                                         -- 已结案日期(同上)
    invoice_path TEXT,                                        -- B阶段：开票路径 self/source/ours，空=自动判断
    finance_overrides TEXT,                                   -- 本车费率覆盖 JSON {"rate:<节点key>": 数值}，空=全按费率与关系

    is_paid INTEGER DEFAULT 0,
    paid_amount REAL,
    payment_screenshot_path TEXT,

    -- 两票三款核对（2026-09-17新增，仅"经过我们"票源方如益周使用，见 invoice_sources.settlement_type）：
    -- 销项发票=我们开给欧冶，进项发票=前端开给我们；对应的3笔打款记在 payments 表(pay_type字段)。
    -- 发票金额与实际打款金额经常不一致，因此分开记录，不互相推算。
    invoice_out_no TEXT,                                    -- 销项发票号（我们→欧冶）
    invoice_out_amount REAL,                                 -- 销项发票金额
    invoice_in_no TEXT,                                       -- 进项发票号（前端→我们）
    invoice_in_amount REAL,                                    -- 进项发票金额
    invoice_in_stock INTEGER DEFAULT 0,                          -- 进项票因票源方票额度不足暂开不出，标记"库存中"

    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS trip_scrap_details ( -- 车次明细：按货型拆分（一车多料）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    scrap_type_id INTEGER REFERENCES scrap_types(id),
    gross_weight REAL,      -- 毛重
    tare_weight REAL,       -- 皮重
    net_weight REAL,        -- 净重（可直接录入，或由毛重-皮重自动计算）
    deduction REAL DEFAULT 0, -- 扣杂
    settlement_tonnage REAL, -- 结算吨数 = 净重 - 扣杂（或直接录入）
    unit_price REAL,        -- 含税单价
    cutting_fee REAL DEFAULT 0,
    amount REAL              -- 金额小计 = 结算吨数 * 单价 - 切割费（可覆盖）
);

CREATE TABLE IF NOT EXISTS trip_finance ( -- 车次利润分项（可固化快照）
    trip_id INTEGER PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE,

    freight_diff_unit_price REAL DEFAULT 0,   -- 货差单价 元/吨
    freight_diff_profit REAL DEFAULT 0,       -- 货差利润

    rate_out REAL,                             -- 户头对外结算点位 %
    rate_in REAL,                              -- 票源方点位 %
    invoice_diff_profit REAL DEFAULT 0,        -- 票差利润

    reward_unit_price REAL DEFAULT 0,          -- 量补单价（按周期累计吨位定档后取得）
    reward_profit REAL DEFAULT 0,              -- 量补金额
    period_cumulative_tonnage REAL,             -- 快照时的周期累计吨位（仅作展示留痕）

    advance_fee_rate REAL DEFAULT 0,            -- 垫资费率 元/吨
    advance_fee_cost REAL DEFAULT 0,             -- 垫资费成本

    dispatcher_commission_rate REAL DEFAULT 0,   -- 调货人提成单价 元/吨
    dispatcher_commission_cost REAL DEFAULT 0,

    service_fee_type TEXT,                        -- per_ton / rate（M3等通道服务费模式使用）
    service_fee_value REAL,
    service_fee_income REAL DEFAULT 0,

    ticket_cost REAL DEFAULT 0,                    -- 买票成本（手工）
    other_cost REAL DEFAULT 0,                      -- 其他成本（手工，与trips.other_cost可保持一致）

    net_profit REAL DEFAULT 0,

    is_locked INTEGER DEFAULT 0,                    -- 是否已固化快照
    locked_at TEXT
);

CREATE TABLE IF NOT EXISTS trip_attachments ( -- 车次附件
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    category TEXT NOT NULL,  -- driver_license/vehicle_license/id_card/env_list/truck_head/truck_tail/cargo_photo/weighing_slip/settlement_doc/payment_screenshot
    file_path TEXT NOT NULL,
    uploaded_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS payments ( -- 付款记录（支持多笔）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    payer TEXT,
    payee TEXT,
    amount REAL,
    screenshot_path TEXT,
    paid_at TEXT,
    status TEXT DEFAULT 'paid' CHECK(status IN ('planned','paid')),
    -- 两票三款核对：pay_type是2026-09-17的初版(仅3个固定值，服务pass_through票源专用)，
    -- 2026-09-18改为模板节点驱动后由 node_key 取代——node_key对应 template_nodes.node_key，
    -- 标记这笔打款是模板里哪个doc_types含"payment"的节点产生的(如front_actual_paid/
    -- payment_in_from_soe/downstream_payment)，不再局限于pass_through票源，任何模板任何
    -- 打款类节点都能用。pay_type字段保留(未删除任何已有数据/字段)，新逻辑不再写入/依赖它。
    pay_type TEXT CHECK(pay_type IN ('invoice_out','invoice_in','downstream') OR pay_type IS NULL),
    node_key TEXT
);

CREATE TABLE IF NOT EXISTS audit_log ( -- 操作日志
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT,
    record_id INTEGER,
    field_name TEXT,
    old_value TEXT,
    new_value TEXT,
    note TEXT,
    changed_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_trips_status ON trips(status);
CREATE INDEX IF NOT EXISTS idx_trips_mill ON trips(mill_id);
CREATE INDEX IF NOT EXISTS idx_trips_base ON trips(base_id);
CREATE INDEX IF NOT EXISTS idx_trips_settlement_date ON trips(settlement_date);
CREATE INDEX IF NOT EXISTS idx_quote_items_sheet ON quote_items(quote_sheet_id);

-- 录入默认值(A阶段 2026-09-21)：目前 default_departure_place=永康
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT);

-- B阶段(2026-09-21)：三条线 + 批次 + 供货厂收款账户（见 lanes.py）
CREATE TABLE IF NOT EXISTS base_accounts ( -- 供货厂收款账户（都由老板管理，不一定是老板本人的卡）
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        base_id INTEGER NOT NULL REFERENCES processing_bases(id),
        account_name TEXT NOT NULL,          -- 户名
        bank TEXT,                           -- 开户行
        card_tail TEXT,                      -- 卡号后四位（不存完整卡号）
        holder_type TEXT DEFAULT 'owner',    -- owner 老板本人 / designated 老板指定的其他卡 / reverse_personal 反向个人户
        is_active INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
CREATE TABLE IF NOT EXISTS batches ( -- 开票/打款/提成 批次
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,                  -- invoice / payment / commission
        hop_key TEXT NOT NULL,               -- in/out(开票) p_out/p_in/p_client(回款) commission(提成)
        from_name TEXT,                      -- 付款方/开票方（整批一致时的展示名，不一致记"多方"）
        to_name TEXT,
        doc_no TEXT,                         -- 发票号/流水号
        batch_date TEXT,
        base_account_id INTEGER REFERENCES base_accounts(id),  -- 打到哪张客户卡（p_client）
        total_amount REAL,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
CREATE TABLE IF NOT EXISTS batch_trips ( -- 批次分摊到车次
        batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
        trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
        amount REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (batch_id, trip_id)
    );
CREATE INDEX IF NOT EXISTS idx_batch_trips_trip ON batch_trips(trip_id);
