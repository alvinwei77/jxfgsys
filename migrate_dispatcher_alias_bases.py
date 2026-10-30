"""费率与关系图重做 Phase A（2026-09-19）：
1. processing_bases 表新增 is_dispatcher_alias 列（幂等，已存在则跳过）。
2. 自动识别"历史台账导入时被自动建成同名供货厂/基地记录，但实际就是调货员本人"的占位基地：
   规则 = 该 processing_base 的 name 跟某个 dispatchers.name 完全一致，且引用该 base_id 的车次里，
   出现次数最多的 dispatcher_id 对应的调货员名字 == 这个 base 名字（即"名字对得上、车次也对得上"，
   不是单纯同名巧合）。命中的打上 is_dispatcher_alias=1。
   本次核实：林凯(id2)/李涛(id3)/闫东朋(id4)/陈杰(id5)/邓晶焱(id9) 共5条，均是2026-09-16历史台账
   导入脚本自动建的("由台账历史数据自动创建，信息不完整，请在基础档案中补全")。
3. 不改动 trips.base_id、不删除任何记录、不影响任何计算结果——engine.py/calc.py 在 base 存在但
   没有专属服务费覆盖值时，取的就是 service_fee_type='per_ton' 的默认值，跟 base_id 为空时完全一致，
   所以这批"占位基地"打不打标记，车次的结算/利润数字都不会变。这个标记只用于：
   - 费率与关系图：不再把这些占位基地当成独立主体画进图里（原来就是它们的存在，加上图的跨列连线
     直穿"调货员"列的画法bug，一起造成了"闫东朋/林凯看着像有关系"的视觉误解，见rate_links.js同批修复）
   - 基地列表/新链条规则编辑器：显示"疑似占位(=调货员本人)"提示，方便后续人工清理/合并。

用法：python3 migrate_dispatcher_alias_bases.py [db_path]（默认dev库；会先备份再改）
"""
import os
import sys
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "scrapmetal.db")


def main(db_path):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_before_dispatcher_alias_{ts}"
    shutil.copy2(db_path, backup_path)
    print(f"[备份] 已备份到: {backup_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cols = {row[1] for row in conn.execute("PRAGMA table_info(processing_bases)")}
    if "is_dispatcher_alias" not in cols:
        conn.execute(
            "ALTER TABLE processing_bases ADD COLUMN is_dispatcher_alias INTEGER NOT NULL DEFAULT 0"
        )
        print("[结构] processing_bases 新增 is_dispatcher_alias 列")
    else:
        print("[结构] is_dispatcher_alias 列已存在，跳过")

    bases = conn.execute("SELECT id, name FROM processing_bases").fetchall()
    dispatchers = conn.execute("SELECT id, name FROM dispatchers").fetchall()
    disp_by_name = {}
    for d in dispatchers:
        disp_by_name.setdefault(d["name"], []).append(d["id"])

    marked = []
    for b in bases:
        cand_disp_ids = disp_by_name.get(b["name"])
        if not cand_disp_ids:
            continue
        rows = conn.execute(
            "SELECT dispatcher_id, COUNT(*) c FROM trips WHERE base_id=? GROUP BY dispatcher_id ORDER BY c DESC",
            (b["id"],),
        ).fetchall()
        if not rows:
            continue
        # 规则：这个base_id下所有"非空调货员"的车次，调货员都得是这个同名调货员本人
        # (哪怕有些车次调货员留空也没关系)——即没有别的调货员用过这条base，说明它不是
        # 真被多人共用的独立供货厂，就是这一个人自己的占位记录。
        non_null_rows = [r for r in rows if r["dispatcher_id"] is not None]
        total = sum(r["c"] for r in rows)
        matched_c = sum(r["c"] for r in non_null_rows if r["dispatcher_id"] in cand_disp_ids)
        conflicting_c = sum(r["c"] for r in non_null_rows if r["dispatcher_id"] not in cand_disp_ids)
        if matched_c > 0 and conflicting_c == 0:
            conn.execute(
                "UPDATE processing_bases SET is_dispatcher_alias=1 WHERE id=?", (b["id"],)
            )
            marked.append((b["id"], b["name"], cand_disp_ids, matched_c, total))

    conn.commit()
    print(f"[标记] 命中 {len(marked)} 条占位基地记录：")
    for row in marked:
        print(f"  base_id={row[0]} name={row[1]}  对应调货员id={row[2]}  {row[3]}/{row[4]}车次匹配")
    conn.close()
    return marked


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    main(target)
