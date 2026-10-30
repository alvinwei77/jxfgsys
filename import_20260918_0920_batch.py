"""一次性数据录入脚本（2026-09-21）：录入用户提供的 9/18-9/20 新车次数据（14个车次）。

用户确认口径：
- 没标票源的（君盛、兴德）= 自带票带货 → 链条"直签带票"(M2)，调货人林凯，无票源。
- 煌烽 = 配票(M1)，调货人林凯，供货厂留空（调货人车队的车，按2026-09-21规则不强制录入供货厂）。
- 反向 = 反向开票(M5)：皖AP7737 调货人闫东朋、供货厂留空；船 苏通源货6666 供货厂尹鹏飞、无调货人、混料。
- "/" 行跟上一行是同一车（一车多料）。结算单都已出 → 状态"已结算"，接单日期=结算日期=表中日期。
- 新增供货厂档案"兴德"（外部，自带票）。
- 数据按用户原表录入，未改动货型/单价/扣杂。

用法：python3 import_20260918_0920_batch.py [db_path]
走 Flask test_client 调现有API（与上一批 import_20260915_0917_batch.py 同一套流程），写入前整库备份；
幂等：同车号+同日期+同结算金额已存在则跳过。
"""
import os
import sys
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "scrapmetal.db")
MILL_NAME = "华西特钢"
ACCOUNT_NAME = "华西-欧冶户头"
REMARK = "2026-09-21由用户提供的台账数据整理后录入（9/18-9/20批次，Claude协助整理，用户确认）"

TRIPS = [
    # (date, plate, transport, mode_code, dispatcher, base_or_None, invoice_source_or_None, mixed,
    #  [(scrap_type_or_None, deduction, tonnage, unit_price, amount), ...])
    ("2026-09-18", "苏DBB772", "truck", "M2", "林凯", "君盛", None, 0, [("中废1", 0.8, 83.74, 2420, 202650.80)]),
    ("2026-09-18", "苏NEN536", "truck", "M1", "林凯", None, "煌烽", 0, [("废钢压块2", 0.5, 34.02, 2330, 79266.60)]),
    ("2026-09-18", "苏DBE075", "truck", "M2", "林凯", "君盛", None, 0,
        [("废钢压块3", 0.42, 21.504, 2290, 49244.16), ("废钢压块2", 0.18, 9.216, 2330, 21473.28)]),
    ("2026-09-18", "苏DBB772", "truck", "M2", "林凯", "君盛", None, 0,
        [("废钢压块3", 0.42, 21.924, 2290, 50205.96), ("废钢压块2", 0.18, 9.396, 2330, 21892.68)]),
    ("2026-09-18", "苏通源货6666", "ship", "M5", None, "尹鹏飞", "反向", 1, [(None, 7.97, 646.85, 0, 1563945.84)]),
    ("2026-09-19", "皖AP7737", "truck", "M5", "闫东朋", None, "反向", 0, [("重废3", 0.3, 30.22, 2510, 75852.20)]),
    ("2026-09-19", "苏GE2356", "truck", "M1", "林凯", None, "煌烽", 0, [("废钢压块2", 0.6, 33.86, 2330, 78893.80)]),
    ("2026-09-19", "苏NHY179", "truck", "M1", "林凯", None, "煌烽", 0,
        [("废钢压块2", 0.42, 23.772, 2330, 55388.76), ("废钢压块3", 0.18, 10.188, 2290, 23330.52)]),
    ("2026-09-19", "皖SG1498", "truck", "M2", "林凯", "兴德", None, 0, [("重废3", 5.6, 30.06, 2510, 75450.60)]),
    ("2026-09-20", "苏DBB772", "truck", "M2", "林凯", "君盛", None, 0,
        [("废钢压块2", 0.49, 22.484, 2290, 51488.36), ("废钢压块3", 0.21, 9.636, 2330, 22451.88)]),
    ("2026-09-20", "苏DBE075", "truck", "M2", "林凯", "君盛", None, 0,
        [("废钢压块2", 0.42, 22.904, 2290, 52450.16), ("废钢压块3", 0.18, 9.816, 2330, 22871.28)]),
    ("2026-09-20", "苏BYR535", "truck", "M2", "林凯", "兴德", None, 0, [("重废1", 3, 57.86, 2550, 147543.00)]),
    ("2026-09-20", "冀A224E6", "truck", "M2", "林凯", "兴德", None, 0, [("重废1", 5, 42.94, 2550, 109497.00)]),
    ("2026-09-20", "苏DBB772", "truck", "M2", "林凯", "兴德", None, 0, [("钢筋压块", 0.3, 75.44, 2610, 196898.40)]),
]


def run_import(db_path):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_before_import_20260918_0920_batch_{ts}"
    shutil.copy2(db_path, backup_path)
    print(f"[备份] {backup_path}")
    sys.path.insert(0, BASE_DIR)
    import db as db_module
    db_module.DB_PATH = db_path
    import app as app_module
    client = app_module.app.test_client()

    def lookup(table, name):
        for row in client.get(f"/api/{table}").get_json():
            if row.get("name") == name:
                return row["id"]
        return None

    if lookup("processing_bases", "兴德") is None:
        r = client.post("/api/processing_bases", json={
            "name": "兴德", "base_type": "external",
            "notes": "2026-09-21新增：外部供货厂，自带票带货（用户确认），企业全称/联系方式待补充"})
        assert r.status_code == 201, r.get_json()
        print("[新建档案] 供货厂 兴德 id=", r.get_json()["id"])
    bases = {n: lookup("processing_bases", n) for n in ["君盛", "兴德", "尹鹏飞"]}
    disps = {n: lookup("dispatchers", n) for n in ["林凯", "闫东朋"]}
    srcs = {n: lookup("invoice_sources", n) for n in ["煌烽", "反向"]}
    assert all(bases.values()) and all(disps.values()) and all(srcs.values()), (bases, disps, srcs)
    stypes = {row["name"]: row["id"] for row in client.get("/api/scrap_types").get_json()}
    for _, _, _, _, _, _, _, _, items in TRIPS:
        for it in items:
            assert it[0] is None or it[0] in stypes, f"货型不在字典: {it[0]}"
    mill_id, account_id = lookup("steel_mills", MILL_NAME), lookup("accounts", ACCOUNT_NAME)
    assert mill_id and account_id

    conn = sqlite3.connect(db_path)
    created, skipped = [], []
    for date, plate, transport, mode, disp, base, src, mixed, items in TRIPS:
        total = round(sum(it[4] for it in items), 2)
        ex = conn.execute("SELECT id FROM trips WHERE plate_no=? AND (order_date=? OR settlement_date=?) "
                          "AND ABS(settlement_amount_total-?)<0.02 AND status!='voided'", (plate, date, date, total)).fetchone()
        if ex:
            skipped.append((plate, date, total, ex[0]))
            continue
        r = client.post("/api/trips", json={"transport_type": transport, "mode_code": mode, "order_date": date,
                                             "mill_id": mill_id, "account_id": account_id})
        assert r.status_code == 201, r.get_json()
        tid = r.get_json()["id"]
        r = client.put(f"/api/trips/{tid}/details", json={"details": [
            {"scrap_type_id": stypes.get(n) if n else None, "deduction": d, "settlement_tonnage": t,
             "unit_price": p, "amount": a} for n, d, t, p, a in items]})
        assert r.status_code == 200, r.get_json()
        patch = {"settlement_date": date, "plate_no": plate, "mixed_material": mixed, "remarks": REMARK,
                 "base_id": bases[base] if base else None, "dispatcher_id": disps[disp] if disp else None,
                 "invoice_source_id": srcs[src] if src else None}
        r = client.put(f"/api/trips/{tid}", json=patch)
        assert r.status_code == 200, r.get_json()
        r = client.post(f"/api/trips/{tid}/status", json={"status": "settled"})
        assert r.status_code == 200, r.get_json()
        d = client.get(f"/api/trips/{tid}").get_json()
        created.append((tid, date, plate, d["trip"]["settlement_tonnage_total"], total, d["finance"].get("net_profit")))
    conn.close()
    print(f"[结果] 新建 {len(created)} 车，跳过 {len(skipped)} 车")
    for c in created:
        print("  #%s\t%s\t%s\t%.3f吨\t¥%s\t利润¥%s" % (c[0], c[1], c[2], c[3], f"{c[4]:,.2f}", f"{(c[5] or 0):,.2f}"))
    for s in skipped:
        print("  [跳过-已存在]", s)


if __name__ == "__main__":
    run_import(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
