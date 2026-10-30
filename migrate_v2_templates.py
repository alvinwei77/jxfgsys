"""v2引擎 Phase 1 迁移脚本（2026-09-18）：
1. 建 flow_templates / template_nodes / trip_node_values 三张新表（纯新增，不改现有表结构）。
2. trips 表新增 template_id 列（新增，与现役 mode_code 并存，互不影响）。
3. 把现有 M1-M5 五个模式，按节点拆解成5个模板（2个split家族+3个service_fee家族），写入种子数据。
4. 把所有现存车次的 mode_code 按1:1映射回填 template_id。

全程只做"新增列/新增表/回填新列"，不删除、不修改任何现有字段或数据，
旧的 mode_code + calc.py 计算路径完全不受影响，app.py/dashboard.js 无需改动即可继续正常工作。

用法：python3 migrate_v2_templates.py [db_path]
"""
import os
import sys
import json
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "scrapmetal.db")
sys.path.insert(0, BASE_DIR)
from engine import MODE_TO_TEMPLATE_CODE  # 唯一权威定义在engine.py，app.py新建/编辑车次时也从那里导入同步template_id

# ---- 模板+节点种子数据 --------------------------------------------------
# 销项发票/销项款(我们→国企/国企→我们)：2026-09-18新增，两票三款通用化第一步——金额基准
# 直接照抄 give_customer 节点的值(用户确认)，用 ref_value 规则表示"不重新算，照抄引用"。
# give_customer 在两个模板里都是各自的seq=1节点，这两个节点固定排在seq=5/6，比它靠后即可。
INVOICE_OUT_NODES = [
    (5, "invoice_out_to_soe", "销项发票(我们→国企)", "our_account", ["invoice"], "ref_value",
        {"input": "node:give_customer"}, 0, None, 0),
    (6, "payment_in_from_soe", "销项款(国企→我们)", "our_account", ["payment"], "ref_value",
        {"input": "node:give_customer"}, 0, None, 0),
]

SPLIT_SHARED_TAIL_NODES = [
    # (seq, node_key, label, role_type, doc_types, calc_rule_type, calc_rule_config,
    #  is_profit_component, profit_sign, is_reserved_component)
    (10, "front_invoice_amount", "前端开票金额", "ticket_source", ["invoice"], "subtract_nodes",
        {"minuend": "trip:settlement_amount", "subtrahend": "node:advance_fee"}, 0, None, 0),
    (40, "front_actual_paid", "前端实付", "ticket_source", ["payment"], "rate_deduct_invoice_source",
        {"input": "node:front_invoice_amount"}, 0, None, 0),
    (50, "personal_advance", "垫资成本(个人垫资)", "our_account", None, "subtract_nodes",
        {"minuend": "node:give_customer", "subtrahend": "node:front_actual_paid"}, 0, None, 0),
    (60, "dispatcher_commission", "调货人提成", "dispatcher", None, "per_ton_rate_schedule",
        {"relation_type": "dispatcher_commission_peiPiao", "primary_type": "dispatcher",
         "primary_ref": "trip:dispatcher_id", "secondary_type": "invoice_source",
         "secondary_ref": "trip:invoice_source_id"}, 0, None, 0),
    (70, "reward_profit", "量补", "mill", None, "per_ton_reward_tier", {}, 0, None, 0),
    (80, "other_cost", "其他成本(切割费+手工)", "output", None, "manual",
        {"trip_fields": ["other_cost"]}, 0, None, 0),
    (45, "downstream_payment", "下游款(前端→实际供货客人)", "ticket_source", ["payment"], "manual",
        {"trip_fields": []}, 0, None, 0),  # 无对应发票、也无可计算基准，纯追踪打款进度用(2026-09-18新增)
    (90, "invoice_reserved", "票差留存利润", "ticket_source", None, "rate_spread_invoice_source",
        {"input": "node:front_invoice_amount"}, 0, None, 1),
    (100, "net_profit", "主营净利润", "output", None, "sum_formula", {"terms": [
        {"node": "reward_profit", "sign": 1}, {"node": "personal_advance", "sign": -1},
        {"node": "dispatcher_commission", "sign": -1}, {"node": "other_cost", "sign": -1},
    ]}, 0, None, 0),
]

TEMPLATES = [
    {
        "code": "split_company", "name": "自营/外部配票-差价模型(企业票源)", "calc_family": "split",
        "description": "对应现有M1。给客户付按户头对外结算点位算，垫资费按吨位×户头垫资费率扣（两者都按车次结算日期查生效费率档案）。",
        "nodes": [
            (1, "give_customer", "给客户付", "mill", None, "rate_deduct_rate_schedule",
                {"input": "trip:settlement_amount", "relation_type": "account_external_settlement",
                 "primary_type": "account", "primary_ref": "trip:account_id"}, 0, None, 0),
            (2, "advance_fee", "垫资费(原始)", "our_account", None, "per_ton_rate_schedule",
                {"relation_type": "account_advance_fee", "primary_type": "account",
                 "primary_ref": "trip:account_id"}, 0, None, 0),
        ] + INVOICE_OUT_NODES + SPLIT_SHARED_TAIL_NODES,
    },
    {
        "code": "split_reverse", "name": "反向自然人开票-差价模型", "calc_family": "split",
        "description": "对应现有M5。与企业票源差价模型唯一的区别：垫资费恒为0(反向不扣)。",
        "nodes": [
            (1, "give_customer", "给客户付", "mill", None, "rate_deduct_rate_schedule",
                {"input": "trip:settlement_amount", "relation_type": "account_external_settlement",
                 "primary_type": "account", "primary_ref": "trip:account_id"}, 0, None, 0),
            (2, "advance_fee", "垫资费(反向不扣)", "our_account", None, "per_ton_fixed",
                {"unit_price": 0}, 0, None, 0),
        ] + INVOICE_OUT_NODES + SPLIT_SHARED_TAIL_NODES,
    },
]

SERVICE_FEE_NODES = [
    (1, "service_fee_income", "毛服务费", "ticket_source", None, "per_ton_rate_schedule",
        {"relation_type": "service_fee_gross", "primary_type": "invoice_source",
         "primary_ref": "trip:invoice_source_id"}, 0, None, 0),
    (2, "advance_fee", "垫资成本", "our_account", None, "per_ton_rate_schedule",
        {"relation_type": "account_advance_fee", "primary_type": "account",
         "primary_ref": "trip:account_id"}, 0, None, 0),
    (3, "dispatcher_commission", "调货人提成", "dispatcher", None, "per_ton_rate_schedule",
        {"relation_type": "dispatcher_commission_daiPiao", "primary_type": "dispatcher",
         "primary_ref": "trip:dispatcher_id", "secondary_type": "invoice_source",
         "secondary_ref": "trip:invoice_source_id"}, 0, None, 0),
    (4, "reward_profit", "量补", "mill", None, "per_ton_reward_tier", {}, 0, None, 0),
    (5, "other_cost", "其他成本(切割费+手工)", "output", None, "manual",
        {"trip_fields": ["other_cost"]}, 0, None, 0),
    (6, "net_profit", "净利润", "output", None, "sum_formula", {"terms": [
        {"node": "service_fee_income", "sign": 1}, {"node": "reward_profit", "sign": 1},
        {"node": "advance_fee", "sign": -1}, {"node": "dispatcher_commission", "sign": -1},
        {"node": "other_cost", "sign": -1},
    ]}, 0, None, 0),
]

for code, name, desc in [
    ("service_fee_direct", "外部直签带票-服务费模型", "对应现有M2。票/财务流不经过我方，只收服务费+量补。"),
    ("service_fee_account", "户头通道-服务费模型", "对应现有M3。外部基地不带票，走我方户头通道。"),
    ("service_fee_dispatcher", "调货人对接-服务费模型", "对应现有M4。公式与M2/M3目前完全相同，仅业务归类不同。"),
]:
    TEMPLATES.append({
        "code": code, "name": name, "calc_family": "service_fee", "description": desc,
        "nodes": SERVICE_FEE_NODES,
    })


def ensure_schema(conn):
    # 若 template_nodes 表已存在但还是"rate_schedules改造前"的旧CHECK约束(比如本库第一次迁移时
    # 建的表)，CREATE TABLE IF NOT EXISTS 不会去更新已存在表的约束，需要先整表丢弃重建——
    # 这张表只存"模板节点定义"，100%由 TEMPLATES/SERVICE_FEE_NODES 常量重新生成，不含任何
    # 用户录入的业务数据，重建不丢真实数据。
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='template_nodes'"
    ).fetchone()
    if row and row["sql"] and "ref_value" not in row["sql"]:
        conn.execute("DROP TABLE template_nodes")
    # rate_schedules 是本次(2026-09-18)费率/关系模型改造前的初版设计(单实体+固定5种scope)，
    # 已被更通用的 rate_links (支持任意两实体连接) 取代；这张表当天建好当天就发现设计不够用，
    # 里面只有脚本自己种的测试数据，没有任何用户录入内容，直接丢弃重建为 rate_links。
    conn.execute("DROP TABLE IF EXISTS rate_schedules")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS flow_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        calc_family TEXT NOT NULL CHECK(calc_family IN ('split','service_fee')),
        description TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS template_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        template_id INTEGER NOT NULL REFERENCES flow_templates(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        node_key TEXT NOT NULL,
        label TEXT NOT NULL,
        role_type TEXT,
        doc_types TEXT,
        calc_rule_type TEXT NOT NULL CHECK(calc_rule_type IN (
            'per_ton_fixed', 'per_ton_rate_schedule', 'per_ton_reward_tier',
            'rate_deduct_fixed', 'rate_deduct_rate_schedule', 'rate_deduct_invoice_source',
            'subtract_nodes', 'rate_spread_invoice_source', 'manual', 'sum_formula', 'ref_value'
        )),
        calc_rule_config TEXT,
        is_profit_component INTEGER NOT NULL DEFAULT 0,
        profit_sign INTEGER,
        is_reserved_component INTEGER NOT NULL DEFAULT 0,
        UNIQUE(template_id, node_key)
    );
    CREATE TABLE IF NOT EXISTS trip_node_values (
        trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
        node_key TEXT NOT NULL,
        computed_value REAL,
        override_value REAL,
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        PRIMARY KEY (trip_id, node_key)
    );
    CREATE TABLE IF NOT EXISTS rate_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        relation_type TEXT NOT NULL,
        primary_entity_type TEXT NOT NULL CHECK(primary_entity_type IN
            ('account','dispatcher','invoice_source','processing_base','steel_mill')),
        primary_entity_id INTEGER,
        secondary_entity_type TEXT CHECK(secondary_entity_type IN
            ('account','dispatcher','invoice_source','processing_base','steel_mill') OR secondary_entity_type IS NULL),
        secondary_entity_id INTEGER,
        rate_value REAL NOT NULL,
        rate_unit TEXT NOT NULL DEFAULT 'per_ton' CHECK(rate_unit IN ('per_ton','pct')),
        effective_from TEXT NOT NULL,
        effective_to TEXT,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_rate_links_lookup
        ON rate_links(relation_type, primary_entity_id, secondary_entity_id, effective_from);
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trips)")}
    if "template_id" not in cols:
        conn.execute("ALTER TABLE trips ADD COLUMN template_id INTEGER REFERENCES flow_templates(id)")

    # Phase 2(两票三款通用化)新增列：trip_node_values 补单据追踪字段，payments 补通用 node_key
    tnv_cols = {row[1] for row in conn.execute("PRAGMA table_info(trip_node_values)")}
    for col_sql, col_name in [
        ("ALTER TABLE trip_node_values ADD COLUMN doc_no TEXT", "doc_no"),
        ("ALTER TABLE trip_node_values ADD COLUMN actual_amount REAL", "actual_amount"),
        ("ALTER TABLE trip_node_values ADD COLUMN doc_status TEXT", "doc_status"),
    ]:
        if col_name not in tnv_cols:
            conn.execute(col_sql)

    pay_cols = {row[1] for row in conn.execute("PRAGMA table_info(payments)")}
    if "node_key" not in pay_cols:
        conn.execute("ALTER TABLE payments ADD COLUMN node_key TEXT")


def seed_templates(conn):
    """创建或刷新模板。模板本身(code/name/description)存在就跳过，但节点定义(template_nodes)
    总是"先清空再按当前TEMPLATES/SERVICE_FEE_NODES定义重新写入"——因为节点的计费规则本身会随本脚本
    的代码修改而迭代(比如本次 rate_schedules 改造)，不能因为模板code已存在就跳过刷新，否则
    库里留着的是上一版本(已废弃计费类型)的旧节点定义。"""
    created_codes, refreshed_codes = [], []
    for tpl in TEMPLATES:
        existing = conn.execute("SELECT id FROM flow_templates WHERE code=?", (tpl["code"],)).fetchone()
        if existing:
            template_id = existing["id"]
            conn.execute("DELETE FROM template_nodes WHERE template_id=?", (template_id,))
            refreshed_codes.append(tpl["code"])
        else:
            cur = conn.execute(
                "INSERT INTO flow_templates (code, name, calc_family, description) VALUES (?,?,?,?)",
                (tpl["code"], tpl["name"], tpl["calc_family"], tpl["description"]),
            )
            template_id = cur.lastrowid
            created_codes.append(tpl["code"])
        for (seq, node_key, label, role_type, doc_types, rule_type, rule_config,
             is_profit, profit_sign, is_reserved) in tpl["nodes"]:
            conn.execute(
                """INSERT INTO template_nodes
                   (template_id, seq, node_key, label, role_type, doc_types,
                    calc_rule_type, calc_rule_config, is_profit_component, profit_sign, is_reserved_component)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (template_id, seq, node_key, label, role_type,
                 json.dumps(doc_types, ensure_ascii=False) if doc_types else None,
                 rule_type, json.dumps(rule_config, ensure_ascii=False), is_profit, profit_sign, is_reserved),
            )
    return created_codes, refreshed_codes


# ---- 费率/关系档案种子数据 ------------------------------------------------
# 当前已知的生效费率(2026-09-18确认，2026-09-18二次修正：垫资费率单位是元/吨不是%；
# 服务费不是全局固定，是"外部带票带货"路径按票源方区分；调货员提成除按时间段外还可按
# 票源细分，但目前手上没有具体的"某调货员+某票源"专属数字，先只种"不分票源"的通用档，
# 具体票源细分等业务上有实际数字了，再通过新的费率页面单独补充)。
# effective_from 统一用系统最早业务日期(不影响历史车次计算——历史车次早已结算完成、
# trip_finance 早已落库，这里只是给 rate_links 一个"从有记录起就生效"的起点，供新引擎
# 对历史车次做对比测算用)。
RATE_LINK_SEED_START = "2020-01-01"

# (relation_type, primary_entity_type, primary_entity_name_or_None, primary_entity_table_or_None,
#  secondary_entity_type_or_None, rate_value, rate_unit, effective_from, effective_to, notes)
RATE_LINKS_SEED = [
    ("account_advance_fee", "account", "华西-欧冶户头", "accounts", None,
     23.0, "per_ton", RATE_LINK_SEED_START, None,
     "户头垫资费率，元/吨，随国企垫资成本调整，需按发布时间更新生效区间"),
    ("account_external_settlement", "account", "华西-欧冶户头", "accounts", None,
     14.0, "pct", RATE_LINK_SEED_START, None,
     "钢厂对外统一结算点位，%，钢厂每期报价发布后同步更新生效区间"),
    ("dispatcher_commission_peiPiao", "dispatcher", "林凯", "dispatchers", None,
     5.0, "per_ton", RATE_LINK_SEED_START, "2026-09-04",
     "配票/反向路径提成(不分票源的通用档)，2026-09-05起改为0（对应原DISPATCHER_COMMISSION_RULES.peiPiao_cutoff）"),
    ("dispatcher_commission_peiPiao", "dispatcher", "林凯", "dispatchers", None,
     0.0, "per_ton", "2026-09-05", None,
     "配票/反向路径提成(不分票源的通用档)，2026-09-05起生效"),
    ("dispatcher_commission_daiPiao", "dispatcher", "林凯", "dispatchers", None,
     15.0, "per_ton", RATE_LINK_SEED_START, None,
     "带票带货(户头通道)路径提成(不分票源的通用档)，不分日期"),
    ("service_fee_gross", "invoice_source", None, None, None,
     40.0, "per_ton", RATE_LINK_SEED_START, None,
     "服务费单价全局兜底档(不挂具体票源方)，沿用历史统一40元/吨；后续某票源方单独议价时，"
     "在费率页面新增一条挂到该票源方上即可，会优先于本条兜底档生效"),
]


def seed_rate_links(conn):
    """把 calc.py 里硬编码的费率常量(accounts表固定字段/DISPATCHER_COMMISSION_RULES/
    SERVICE_FEE_GROSS_RATE)迁移进 rate_links 表，成为可按生效时间段+实体关系管理的档案记录。
    幂等：按 relation_type+primary+secondary+effective_from 判断是否已存在，已存在则跳过。"""
    inserted = 0
    for (relation_type, primary_type, primary_name, primary_table, secondary_type,
         rate_value, rate_unit, eff_from, eff_to, notes) in RATE_LINKS_SEED:
        primary_id = None
        if primary_table:
            row = conn.execute(f"SELECT id FROM {primary_table} WHERE name=?", (primary_name,)).fetchone()
            if not row:
                print(f"  [跳过] {relation_type} 找不到对象: {primary_name}")
                continue
            primary_id = row["id"]
        existing = conn.execute(
            "SELECT id FROM rate_links WHERE relation_type=? AND "
            "((? IS NULL AND primary_entity_id IS NULL) OR primary_entity_id=?) AND "
            "secondary_entity_id IS NULL AND effective_from=?",
            (relation_type, primary_id, primary_id, eff_from),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """INSERT INTO rate_links
               (relation_type, primary_entity_type, primary_entity_id, secondary_entity_type,
                secondary_entity_id, rate_value, rate_unit, effective_from, effective_to, notes)
               VALUES (?,?,?,?,NULL,?,?,?,?,?)""",
            (relation_type, primary_type, primary_id, secondary_type,
             rate_value, rate_unit, eff_from, eff_to, notes),
        )
        inserted += 1
    return inserted


def backfill_template_id(conn):
    code_to_id = dict(conn.execute("SELECT code, id FROM flow_templates").fetchall())
    total_updated = 0
    for mode_code, tpl_code in MODE_TO_TEMPLATE_CODE.items():
        tid = code_to_id.get(tpl_code)
        if not tid:
            continue
        cur = conn.execute(
            "UPDATE trips SET template_id=? WHERE mode_code=? AND (template_id IS NULL OR template_id != ?)",
            (tid, mode_code, tid),
        )
        total_updated += cur.rowcount
    return total_updated


def migrate(db_path):
    if not os.path.exists(db_path):
        print(f"[跳过] 数据库不存在: {db_path}")
        return
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_before_v2_templates_{ts}"
    shutil.copy2(db_path, backup_path)
    print(f"[备份] 已备份到: {backup_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        print("[步骤1/4] 建表/新增列 ...")
        ensure_schema(conn)
        conn.commit()

        print("[步骤2/4] 写入/刷新5个模板的节点定义 ...")
        created, refreshed = seed_templates(conn)
        conn.commit()
        print(f"  新建模板: {created or '(无)'}")
        print(f"  刷新节点定义: {refreshed or '(无)'}")

        print("[步骤3/4] 写入费率/关系档案种子数据 ...")
        n_rates = seed_rate_links(conn)
        conn.commit()
        print(f"  新增费率/关系记录: {n_rates} 条")

        print("[步骤4/4] 回填车次 template_id ...")
        n = backfill_template_id(conn)
        conn.commit()
        print(f"  回填 {n} 条车次")

        dist = conn.execute(
            "SELECT ft.code, COUNT(*) c FROM trips t JOIN flow_templates ft ON t.template_id=ft.id "
            "WHERE t.status!='voided' GROUP BY ft.code"
        ).fetchall()
        print("  当前 template_id 分布:", {r["code"]: r["c"] for r in dist})

        no_template = conn.execute(
            "SELECT COUNT(*) c FROM trips WHERE status!='voided' AND template_id IS NULL"
        ).fetchone()["c"]
        print(f"  仍未关联 template_id 的车次: {no_template} 条（mode_code为空或不在M1-M5映射表里）")
    finally:
        conn.close()
    print("\n迁移完成（mode_code/trip_finance 均未改动，app.py现网行为不受影响）。")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    migrate(target)
