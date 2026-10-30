"""一次性数据录入脚本（2026-10-05）：录入用户提供的 9/29-9/30 车次数据（3车4行）。

用户截图列含义（已与历史车次/imports/price_sheet_huaxi.py 别名表核对）：
  日期 | 车牌 | 货型 | 扣杂(吨) | 结算吨位 | 单价(元/吨) | 金额 | 票源
- 票源"俊兴" = 配票(M1)，调货人林凯、供货厂林凯（两台车近期 M1 车次的稳定画像，与录入页车牌带出一致）。
- "剪中1" = 货型字典"剪切中废1"（id 10，别名表 price_sheet_huaxi.py: "剪中1"→"剪切中废1"）。
- "/" 行跟上一行同一车（9/30 苏DBE075 一车两料，mixed_material=1）。
- 净重按 结算吨位+扣杂 回填；结算单已出 → 状态"已结算"，接单日期=结算日期=表中日期。

用法：python3 import_20260929_0930_batch.py [db_path]
走 Flask test_client 调现有API（与 import_20260918_0920_batch.py 同一套流程），写入前整库备份；
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
REMARK = "2026-10-05由用户提供的结算数据录入（9/29-9/30批次，Claude协助整理，列含义已核对）"

TRIPS = [
    # (date, plate, mode_code, dispatcher, base, invoice_source, mixed,
    #  [(scrap_type, deduction, settlement_tonnage, unit_price, amount), ...])
    ("2026-09-29", "苏DBE075", "M1", "林凯", "林凯", "俊兴", 0,
     [("废钢压块3", 2.4, 78.62, 2290, 180039.80)]),
    ("2026-09-29", "苏DBB772", "M1", "林凯", "林凯", "俊兴", 0,
     [("剪切中废1", 0.6, 81.78, 2420, 197907.60)]),
    ("2026-09-30", "苏DBE075", "M1", "林凯", "林凯", "俊兴", 1,
     [("废钢压块2", 0.8, 55.804, 2330, 130023.32), ("废钢压块3", 0.36, 23.916, 2290, 54767.64)]),
]


def run_import(db_path):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_before_import_20260929_0930_batch_{ts}"
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

    bases = {n: lookup("processing_bases", n) for n in ["林凯"]}
    disps = {n: lookup("dispatchers", n) for n in ["林凯"]}
    srcs = {n: lookup("invoice_sources", n) for n in ["俊兴"]}
    stypes = {row["name"]: row["id"] for row in client.get("/api/scrap_types").get_json()}
    assert all(bases.values()) and all(disps.values()) and all(srcs.values()), (bases, disps, srcs)
    for _, _, _, _, _, _, _, items in TRIPS:
        for it in items:
            assert it[0] in stypes, f"货型不在字典: {it[0]}"
    mill_id, account_id = lookup("steel_mills", MILL_NAME), lookup("accounts", ACCOUNT_NAME)
    assert mill_id and account_id

    conn = sqlite3.connect(db_path)
    created, skipped = [], []
    for date, plate, mode, disp, base, src, mixed, items in TRIPS:
        total = round(sum(it[4] for it in items), 2)
        ex = conn.execute("SELECT id FROM trips WHERE plate_no=? AND (order_date=? OR settlement_date=?) "
                          "AND ABS(settlement_amount_total-?)<0.02 AND status!='voided'", (plate, date, date, total)).fetchone()
        if ex:
            skipped.append((plate, date, total, ex[0]))
            continue
        r = client.post("/api/trips", json={"transport_type": "truck", "mode_code": mode, "order_date": date,
                                            "mill_id": mill_id, "account_id": account_id})
        assert r.status_code == 201, r.get_json()
        tid = r.get_json()["id"]
        r = client.put(f"/api/trips/{tid}/details", json={"details": [
            {"scrap_type_id": stypes[n], "deduction": d, "settlement_tonnage": t,
             "net_weight": round(t + d, 3), "unit_price": p, "amount": a} for n, d, t, p, a in items]})
        assert r.status_code == 200, r.get_json()
        patch = {"settlement_date": date, "plate_no": plate, "mixed_material": mixed, "remarks": REMARK,
                 "base_id": bases[base], "dispatcher_id": disps[disp], "invoice_source_id": srcs[src]}
        r = client.put(f"/api/trips/{tid}", json=patch)
        assert r.status_code == 200, r.get_json()
        r = client.post(f"/api/trips/{tid}/status", json={"status": "settled"})
        assert r.status_code == 200, r.get_json()
        d = client.get(f"/api/trips/{tid}").get_json()
        created.append((tid, date, plate, d["trip"]["settlement_tonnage_total"], total, (d.get("finance") or {}).get("net_profit")))
    conn.close()
    print(f"[结果] 新建 {len(created)} 车，跳过 {len(skipped)} 车")
    for c in created:
        print("  #%s\t%s\t%s\t%.3f吨\t¥%s\t利润¥%s" % (c[0], c[1], c[2], c[3], f"{c[4]:,.2f}", f"{(c[5] or 0):,.2f}"))
    for s in skipped:
        print("  [跳过-已存在]", s)


if __name__ == "__main__":
    run_import(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
