"""一次性数据录入脚本（2026-10-08）：录入用户提供的 10/4-10/7 车次数据（7车8行）。

用户确认口径（2026-10-08）：
- 列含义：日期|车牌|货型|扣杂|结算吨|单价|切割费|金额|票源|调货人；10.7 苏DBE961 的 300 记切割费
  （171,141.40 = 73.58×2330 − 300，与系统明细算法一致），其余行切割费为 0。
- 「盐城宏峰」= 新建自带票票源（直签带票 M2，不占额度，B 区按票面流水），同步建同名供货厂（external），
  参照君盛/兴德的档案字段（source_type=front_company, settlement_type=direct, bound_base_id 指向同名基地）。
- 「邓」= 调货人邓晶焱；俊兴 M1 车的供货厂跟随调货人（林凯→林凯、邓晶焱→邓晶焱，同 9/26-9/28 补录批次惯例）。
- 10.5 苏DBG809 第二行"/"=同车两料（压块3+压块2，mixed_material=1）。
- 状态：结算单已出 → 已结算，结算日期=接单日期。

用法：python3 import_20261004_1007_batch.py [db_path]
走 Flask test_client 调现有API（与 import_20260929_0930_batch.py 同一套流程），写入前整库备份；
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
REMARK = "2026-10-08由用户提供的结算数据录入（10/4-10/7批次，Claude协助整理，用户确认口径）；10.7车300元记切割费"

# (date, plate, mode, dispatcher, base, invoice_source, mixed,
#  [(scrap_type, deduction, settlement_tonnage, unit_price, cutting_fee, amount), ...])
TRIPS = [
    ("2026-10-04", "鲁DC6106", "M2", "邓晶焱", "盐城宏峰", "盐城宏峰", 0,
     [("小废", 0.9, 44.62, 2290, 0, 102179.80)]),
    ("2026-10-04", "苏DBE075", "M1", "林凯", "林凯", "俊兴", 0,
     [("铁加压块", 0.3, 80.58, 2520, 0, 203061.60)]),
    ("2026-10-04", "苏J29822", "M1", "邓晶焱", "邓晶焱", "俊兴", 0,
     [("普通压块", 1, 41.50, 2250, 0, 93375.00)]),
    ("2026-10-04", "苏DBE961", "M1", "林凯", "林凯", "俊兴", 0,
     [("中废2", 0.4, 91.94, 2360, 0, 216978.40)]),
    ("2026-10-05", "苏DBG809", "M1", "林凯", "林凯", "俊兴", 1,
     [("废钢压块3", 0.28, 24.668, 2290, 0, 56489.72), ("废钢压块2", 0.12, 10.572, 2330, 0, 24632.76)]),
    ("2026-10-05", "苏DBE961", "M1", "林凯", "林凯", "俊兴", 0,
     [("废钢压块2", 0.8, 80.46, 2330, 0, 187471.80)]),
    ("2026-10-07", "苏DBE961", "M1", "林凯", "林凯", "俊兴", 0,
     [("废钢压块2", 2.1, 73.58, 2330, 300, 171141.40)]),
]


def run_import(db_path):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_before_import_20261004_1007_batch_{ts}"
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

    # ---- 档案：盐城宏峰（自带票票源 + 同名供货厂），已存在则复用 ----
    base_hf = lookup("processing_bases", "盐城宏峰")
    if base_hf is None:
        r = client.post("/api/processing_bases", json={
            "name": "盐城宏峰", "base_type": "external", "default_mode_code": "M2",
            "notes": "2026-10-08新增：自带票带货（直签带票 M2 的开票方，用户确认），企业全称/联系方式待补充"})
        assert r.status_code == 201, r.get_json()
        base_hf = r.get_json()["id"]
        print("[新建档案] 供货厂 盐城宏峰 id=", base_hf)
    src_hf = lookup("invoice_sources", "盐城宏峰")
    if src_hf is None:
        r = client.post("/api/invoice_sources", json={
            "name": "盐城宏峰", "source_type": "front_company", "settlement_type": "direct",
            "bound_base_id": base_hf,
            "notes": "带票公司（直签带票 M2 的开票方）；2026-10-08 新增（用户确认），额度不设（自带票不占额度）"})
        assert r.status_code == 201, r.get_json()
        src_hf = r.get_json()["id"]
        print("[新建档案] 票源方 盐城宏峰 id=", src_hf)

    bases = {n: lookup("processing_bases", n) for n in ["林凯", "邓晶焱", "盐城宏峰"]}
    disps = {n: lookup("dispatchers", n) for n in ["林凯", "邓晶焱"]}
    srcs = {n: lookup("invoice_sources", n) for n in ["俊兴", "盐城宏峰"]}
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
        total = round(sum(it[5] for it in items), 2)
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
             "net_weight": round(t + d, 3), "unit_price": p, "cutting_fee": cf, "amount": a}
            for n, d, t, p, cf, a in items]})
        assert r.status_code == 200, r.get_json()
        patch = {"settlement_date": date, "plate_no": plate, "mixed_material": mixed, "remarks": REMARK,
                 "base_id": bases[base], "dispatcher_id": disps[disp], "invoice_source_id": srcs[src]}
        r = client.put(f"/api/trips/{tid}", json=patch)
        assert r.status_code == 200, r.get_json()
        r = client.post(f"/api/trips/{tid}/status", json={"status": "settled"})
        assert r.status_code == 200, r.get_json()
        d = client.get(f"/api/trips/{tid}").get_json()
        created.append((tid, date, plate, mode, d["trip"]["settlement_tonnage_total"], total,
                        (d.get("finance") or {}).get("net_profit")))
    conn.close()
    print(f"[结果] 新建 {len(created)} 车，跳过 {len(skipped)} 车")
    for c in created:
        print("  #%s\t%s\t%s\t%s\t%.3f吨\t¥%s\t利润¥%s" % (c[0], c[1], c[2], c[3], c[4], f"{c[5]:,.2f}", f"{(c[6] or 0):,.2f}"))
    for s in skipped:
        print("  [跳过-已存在]", s)


if __name__ == "__main__":
    run_import(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
