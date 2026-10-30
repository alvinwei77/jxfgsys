"""费率与关系图重做 Phase B（2026-09-19）：新增 template_flow_edges 表，并给现有5个链条规则
(M1-M5对应的flow_templates)灌入一版初始"从头到尾"业务流程边，供"链条规则"新页面渲染多张独立
关系图用。

种子数据分两种来源：
1. template 1(split_company/M1) —— 用户在对话里给的真实例子原样落地：
   票商 -[开票,打款]- 俊兴(我方加工厂) -[开票,打款]- 欧冶(垫资户头) -> 华西特钢
   + 林凯(调货员) -> 华西特钢(发货，纯物流)
   + 票商 -> 打款华西特钢账户(旁支：票商直接打款到钢厂账户这一支路，用户原话提到但不在
     主链上，具体触发条件/是否每笔都有待用户在链条规则页面核实调整)
   这条是用户亲口确认的真实业务链条，其余4条是根据现有 template_nodes 的 role_type/doc_types
   自动推导出的"初版"，都标了notes提示用户在页面上核实修改——毕竟这次改造的核心诉求就是
   "user在页面上可编辑"，不需要一次性做到100%准确。
2. template 2-5 —— 按该模板 template_nodes 的 seq 顺序，把相邻且role_type不同的节点连成一条边，
   节点doc_types含"invoice"/"payment"就生成对应hop_type的边，都没有就生成hop_type='goods'的
   骨架边，交给用户在页面上核对/补充成他们真实的业务链条。

全程只新增表、只插入数据，不改任何现有表结构/数据，不影响计算。
用法：python3 migrate_flow_edges.py [db_path]
"""
import os
import sys
import json
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "scrapmetal.db")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS template_flow_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES flow_templates(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    from_role_type TEXT NOT NULL,
    from_label TEXT NOT NULL,
    to_role_type TEXT NOT NULL,
    to_label TEXT NOT NULL,
    hop_type TEXT NOT NULL CHECK(hop_type IN ('invoice','payment','goods')),
    node_key_ref TEXT,
    notes TEXT,
    UNIQUE(template_id, seq)
)
"""

# (seq, from_role, from_label, to_role, to_label, hop_type, node_key_ref, notes)
TEMPLATE1_SPLIT_COMPANY_EDGES = [
    (1, "ticket_source", "票商", "supplier", "俊兴(我方加工厂)", "invoice", "front_invoice_amount",
     "用户确认的真实链条：票商给俊兴开票"),
    (2, "ticket_source", "票商", "supplier", "俊兴(我方加工厂)", "payment", "front_actual_paid",
     "票商给俊兴打款"),
    (3, "supplier", "俊兴(我方加工厂)", "our_account", "欧冶(垫资户头)", "invoice", "invoice_out_to_soe",
     "俊兴给欧冶开票"),
    (4, "supplier", "俊兴(我方加工厂)", "our_account", "欧冶(垫资户头)", "payment", "payment_in_from_soe",
     "俊兴收欧冶打款(销项款)"),
    (5, "our_account", "欧冶(垫资户头)", "mill", "华西特钢", "goods", "give_customer",
     "垫资通道最终对应到华西特钢的结算"),
    (6, "dispatcher", "林凯(调货员)", "mill", "华西特钢", "goods", None,
     "调货员直接发货到钢厂，纯物流环节，不产生独立计算金额"),
    (7, "ticket_source", "票商", "mill", "华西特钢账户", "payment", None,
     "旁支：部分情况票商直接打款到钢厂账户，请在此核实是否每笔都有、具体触发条件"),
]


def auto_derive_edges(nodes):
    """按seq顺序把相邻且role_type不同的节点连成一条边，doc_types含invoice/payment的
    节点各生成一条对应hop_type的边（用该节点自己的role_type当"到达方"，上一个不同role的
    节点当"出发方"）；都没有doc_types就用goods兜底，保证每条模板都至少有一版可看的骨架图。"""
    edges = []
    seq = 1
    prev_role = None
    prev_label = None
    role_label = {
        "supplier": "供货厂", "dispatcher": "调货员", "our_account": "我方户头",
        "ticket_source": "票源方", "soe": "国企", "mill": "钢厂", "output": "结果",
    }
    for n in nodes:
        role = n["role_type"] or "output"
        label = role_label.get(role, role)
        if prev_role and prev_role != role:
            doc_types = json.loads(n["doc_types"]) if n["doc_types"] else []
            if "invoice" in doc_types:
                edges.append((seq, prev_role, prev_label, role, label, "invoice", n["node_key"],
                               "自动推导初版，请在链条规则页面核实"))
                seq += 1
            if "payment" in doc_types:
                edges.append((seq, prev_role, prev_label, role, label, "payment", n["node_key"],
                               "自动推导初版，请在链条规则页面核实"))
                seq += 1
            if not doc_types:
                edges.append((seq, prev_role, prev_label, role, label, "goods", n["node_key"],
                               "自动推导初版，请在链条规则页面核实"))
                seq += 1
        prev_role, prev_label = role, label
    return edges


def main(db_path):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_before_flow_edges_{ts}"
    shutil.copy2(db_path, backup_path)
    print(f"[备份] 已备份到: {backup_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_SQL)
    print("[结构] template_flow_edges 表已就绪")

    existing = conn.execute("SELECT COUNT(*) c FROM template_flow_edges").fetchone()["c"]
    if existing:
        print(f"[跳过] template_flow_edges 已有 {existing} 条数据，不重复灌种子(如需重灌请先手动清空该表)")
        conn.close()
        return

    templates = conn.execute("SELECT * FROM flow_templates ORDER BY id").fetchall()
    total = 0
    for t in templates:
        if t["code"] == "split_company":
            rows = TEMPLATE1_SPLIT_COMPANY_EDGES
        else:
            nodes = conn.execute(
                "SELECT * FROM template_nodes WHERE template_id=? ORDER BY seq", (t["id"],)
            ).fetchall()
            rows = auto_derive_edges(nodes)
        for r in rows:
            conn.execute(
                """INSERT INTO template_flow_edges
                   (template_id, seq, from_role_type, from_label, to_role_type, to_label,
                    hop_type, node_key_ref, notes)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (t["id"], r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]),
            )
        print(f"[灌入] template_id={t['id']} {t['code']}: {len(rows)} 条边")
        total += len(rows)
    conn.commit()
    conn.close()
    print(f"[完成] 共灌入 {total} 条链条流程边")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    main(target)
