"""链条规则录入简化迁移（2026-09-19，用户反馈"链条规则太复杂"后的结构调整）。

用户指出的复杂度根源之一：环节的起点/终点名称是**自由文本**，跟档案实体没有任何绑定关系。
实测29条边里13个不同名称，只有3个（俊兴/益周/华西特钢）跟档案对得上，其余全是手打泛称；
而且车次详情页渲染时会按 role_type 把名字替换成该车次的真实主体，用户手打的"欧冶(垫资户头)"
在车次图里会显示成档案里的"华西-欧冶户头"——等于打的字白打了。

这个脚本做三件事（全是加列/改标记，不删数据、不碰计算）：

1. `template_flow_edges` 加 5 列：
   - from_entity_type / from_entity_id / to_entity_type / to_entity_id
     一端绑定到某个**具体档案实体**时填（比如固定就是"华西-欧冶户头"这个户头）。
     绑定之后渲染时取档案里的当前名字，以后在基础档案里改名，图上自动跟着变，不会再漂移。
   - is_auto：这条边是系统自动推导出来的初版、还没人工核实过。
     用户在页面上编辑过任意一条，就自动置0视为已确认。

   于是一端有三种形态，前端一个下拉就能选完（原来要分别选 role_type 和手打 label 两次）：
     a) entity_id 有值            → 固定的具体对象
     b) entity_id 空 + 角色可替换  → 角色占位，车次图里自动换成该车次的真实主体
     c) 其余                      → 自定义文字（台账、最终客户这类不在档案里的概念）

2. 把 notes 含"自动推导初版"的边标成 is_auto=1（页面上会提示"待核实"）。

   **刻意不做自动绑定**：一开始写这个脚本时顺手把名称能跟档案对上的（俊兴/益周/华西特钢）
   自动绑到了实体上，跑完才想明白这是错的——链条规则是**一条规则覆盖很多笔车次**，
   而这些车次的票源方/供货厂各不相同（M1 的 68 笔里票源方有俊兴、益周、反向好几种）。
   把"益周"绑死，等于宣称"这条规则的票源方永远是益周"，那些票源方是俊兴的车次图就画错了。
   用户当初打"益周"只是想让图具体一点，本意仍是占位。
   所以：**一律保持角色占位**，真正固定不变的那几方（比如欧冶这个垫资通道）由用户在页面上
   显式选"指定具体对象"——是不是固定的，只有用户知道，系统不该替他猜。

3. 0 车次的死规则（service_fee_dispatcher/M4）停用，减少页面噪音。

用法：python3 migrate_flow_edges_v2.py [db_path]
"""
import os
import sys
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "scrapmetal.db")

NEW_COLS = [
    ("from_entity_type", "TEXT"),
    ("from_entity_id", "INTEGER"),
    ("to_entity_type", "TEXT"),
    ("to_entity_id", "INTEGER"),
    ("is_auto", "INTEGER NOT NULL DEFAULT 0"),
]

# role_type -> (档案表, 实体类型代码)。只有这5个角色能绑定到具体档案实体，
# 也只有这5个角色能在车次图里被替换成该车次的真实主体（跟 app.py::ROLE_TYPE_TRIP_FIELD 一致）。
ROLE_ENTITY_TABLE = {
    "supplier": ("processing_bases", "processing_base"),
    "dispatcher": ("dispatchers", "dispatcher"),
    "ticket_source": ("invoice_sources", "invoice_source"),
    "our_account": ("accounts", "account"),
    "mill": ("steel_mills", "steel_mill"),
}


def main(db_path):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{db_path}.bak_before_flow_edges_v2_{ts}"
    shutil.copy2(db_path, backup)
    print(f"[备份] {backup}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    existing = {r[1] for r in conn.execute("PRAGMA table_info(template_flow_edges)")}
    for col, decl in NEW_COLS:
        if col in existing:
            print(f"[结构] {col} 已存在，跳过")
        else:
            conn.execute(f"ALTER TABLE template_flow_edges ADD COLUMN {col} {decl}")
            print(f"[结构] 新增列 {col}")

    # --- 标记自动推导、未核实的边 ---
    n_auto = conn.execute(
        "UPDATE template_flow_edges SET is_auto=1 WHERE IFNULL(notes,'') LIKE '%自动推导初版%'"
    ).rowcount
    print(f"[标记] {n_auto} 条自动推导的边标成待核实(is_auto=1)")

    # --- 只统计、不自动绑定（原因见文件头第2条）---
    lookup = {}  # (role_type, name) -> (entity_type, id)
    for role, (tbl, etype) in ROLE_ENTITY_TABLE.items():
        for r in conn.execute(f"SELECT id, name FROM {tbl}"):
            lookup[(role, r["name"])] = (etype, r["id"])

    looks_fixed = []
    for e in conn.execute("SELECT * FROM template_flow_edges").fetchall():
        for side in ("from", "to"):
            role, label = e[f"{side}_role_type"], e[f"{side}_label"]
            if lookup.get((role, label)):
                looks_fixed.append((e["template_id"], e["seq"], side, label))

    if looks_fixed:
        print(f"[提示] 有 {len(looks_fixed)} 端的名字跟档案里的实体重名，但**没有**自动绑定：")
        for u in looks_fixed:
            print(f"    模板{u[0]} 第{u[1]}步 {u[2]:4s} 「{u[3]}」")
        print("    这些目前仍是角色占位——车次图里会替换成该车次的真实主体。")
        print("    如果其中某一方在这条规则里其实是固定不变的，去页面上把它改成「指定具体对象」。")

    # --- 0 车次的死规则停用 ---
    for t in conn.execute("SELECT id, code, name FROM flow_templates WHERE is_active=1").fetchall():
        n = conn.execute(
            "SELECT COUNT(*) c FROM trips WHERE template_id=? AND status!='voided'", (t["id"],)
        ).fetchone()["c"]
        if n == 0:
            conn.execute("UPDATE flow_templates SET is_active=0 WHERE id=?", (t["id"],))
            print(f"[去噪] {t['code']}（{t['name']}）0 车次在用，已停用；需要时在页面上勾回来即可")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
