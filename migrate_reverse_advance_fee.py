"""反向票源垫资费改为"按时段配置"（2026-09-21，用户说明）。

用户原话要点：垫资费是一直都有的；只是这个月的垫资费已经被算进反向票点成本里了，下个月就不一定了
（反向票源是垫资公司欧冶介绍的渠道）。

原来的做法是把"反向不扣垫资费"写死成常量(模板 split_reverse 的 advance_fee 节点 = per_ton_fixed 0，
calc.py/首页额度水位/票额估算里也各写了一遍 M5 不扣)，下个月规则一变就得改代码。

这个脚本：
1. 给每个户头 × 每个反向自然人票源，加一条 rate_links：account_advance_fee = 0 元/吨，
   从 2020-01-01 起、不设截止（=覆盖目前所有反向车次，数字跟现在完全一样）。备注写明原因。
   以后哪个月反向票点不再包含垫资费了，到"费率与关系"页给这个户头+反向新增一条 23 元/吨、
   从某天开始生效，旧的这条会自动截止到前一天。
2. 把 split_reverse 模板的 advance_fee 节点从 per_ton_fixed 0 改为 per_ton_rate_schedule，
   按 户头 + 票源 查生效费率（查不到反向专属档时，自动落到户头通用档 23）。
只影响反向差价模型(M5)车次；带票路径里用了反向票源的车次(服务费模型)不改，照旧按户头通用档。

可重复执行，运行前自动备份。用法：python3 migrate_reverse_advance_fee.py [db_path]
"""
import os
import sys
import json
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.environ.get("SCRAPMETAL_TEST_DB") or os.path.join(BASE_DIR, "data", "scrapmetal.db")
NOTE = ("反向票源的垫资费本期已算进反向票点成本(反向是欧冶介绍的渠道)，所以对反向不再单独扣；"
        "垫资费本身一直都有。哪个月不含了，就给这个户头+反向新增一条23元/吨并写生效日期。")
NEW_CFG = {"relation_type": "account_advance_fee", "primary_type": "account", "primary_ref": "trip:account_id",
           "secondary_type": "invoice_source", "secondary_ref": "trip:invoice_source_id"}


def main(db_path):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{db_path}.bak_before_reverse_advance_fee_{ts}"
    shutil.copy2(db_path, backup)
    print("备份:", backup)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=TRUNCATE")  # Cowork 虚拟机挂载目录不能删文件
    accounts = conn.execute("SELECT id, name FROM accounts").fetchall()
    reverses = conn.execute("SELECT id, name FROM invoice_sources WHERE source_type='reverse_individual'").fetchall()
    for a in accounts:
        for r in reverses:
            exists = conn.execute(
                """SELECT 1 FROM rate_links WHERE relation_type='account_advance_fee' AND primary_entity_id=?
                   AND secondary_entity_id=?""", (a["id"], r["id"])).fetchone()
            if exists:
                print(f"跳过：{a['name']} × {r['name']} 已有反向专属垫资费档")
                continue
            conn.execute(
                """INSERT INTO rate_links (relation_type, primary_entity_type, primary_entity_id,
                   secondary_entity_type, secondary_entity_id, rate_value, rate_unit, effective_from, notes)
                   VALUES ('account_advance_fee','account',?,'invoice_source',?,0,'per_ton','2020-01-01',?)""",
                (a["id"], r["id"], NOTE))
            print(f"新增：{a['name']} × {r['name']} 垫资费 0 元/吨（2020-01-01 起，不设截止）")
    n = conn.execute(
        """SELECT tn.id, tn.calc_rule_type, tn.calc_rule_config FROM template_nodes tn
           JOIN flow_templates ft ON ft.id=tn.template_id
           WHERE ft.code='split_reverse' AND tn.node_key='advance_fee'""").fetchone()
    if n and n["calc_rule_type"] == "per_ton_fixed":
        conn.execute("UPDATE template_nodes SET calc_rule_type='per_ton_rate_schedule', calc_rule_config=? WHERE id=?",
                     (json.dumps(NEW_CFG, ensure_ascii=False), n["id"]))
        print("split_reverse.advance_fee：per_ton_fixed 0 → 按户头+票源查生效费率")
    else:
        print("split_reverse.advance_fee 已是按费率档查询，跳过")
    conn.commit()
    conn.close()
    print("完成")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
