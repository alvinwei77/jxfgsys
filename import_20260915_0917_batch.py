"""一次性数据录入脚本（2026-09-18）：录入用户提供的 9/15-9/17 新车次数据（15个车次）。
用户已逐项确认：结算模式(M1/M2/M5)、调货人/供货厂、票源方(煌烽id4/反向id1，均已在档案中)，
唯一新增档案为供货厂"尹鹏飞"。状态统一设为"已结算"。

用法：python3 import_20260915_0917_batch.py [db_path]
默认操作 data/scrapmetal.db。通过 Flask test_client 调用现有 API（走完整业务逻辑：
详情表按货型分行自动累加吨位/金额、trip_finance 按档案参数实时试算等），不手写裸 SQL。
写入前会先做一份带时间戳的整库备份。幂等：如果同一车号+日期+结算金额的车次已存在，跳过。
"""
import os
import sys
import shutil
import sqlite3
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "scrapmetal.db")

# 供货厂/票源方常量 id（用户确认，已在档案中；尹鹏飞需新建，占位 id 用 None 由脚本创建后回填）
BASE_LINKAI = None       # 林凯，运行时按名字查
BASE_JUNSHENG = None
BASE_LITUO = None
BASE_GEJINGTONG = None   # 𠓾景通
BASE_YANDONGPENG = None  # 闫东朋
BASE_DENGJINGYAN = None  # 邓晶焱
DISPATCHER_LINKAI = None
DISPATCHER_YANDONGPENG = None
SRC_HUANGFENG = None      # 煌烽 票源方
SRC_FANXIANG = None       # 反向 票源方

MILL_NAME = "华西特钢"
ACCOUNT_NAME = "华西-欧冶户头"

TRIPS = [
    # (date, plate, mode_code, dispatcher_name_or_None, base_name, invoice_source_name_or_None,
    #  mixed_material, [ (scrap_type_name_or_None, deduction, tonnage, unit_price, amount), ... ])
    ("2026-09-15", "苏ELU629", "M1", None, "邓晶焱", "煌烽", 0,
        [("中废2", 0.3, 34.42, 2390, 82263.80)]),
    ("2026-09-15", "苏DBE075", "M2", "林凯", "君盛", None, 0,
        [("普通压块", 0.3, 31.46, 2250, 70785.00)]),
    ("2026-09-15", "苏L80153", "M5", "闫东朋", "闫东朋", "反向", 0,
        [("边角料压块1", 0.2, 47.98, 2570, 123308.60)]),
    ("2026-09-15", "苏J19371", "M5", "闫东朋", "闫东朋", "反向", 0,
        [("重废3", 0.4, 79.58, 2510, 199745.80)]),
    ("2026-09-15", "苏CLK667", "M1", None, "邓晶焱", "煌烽", 0,
        [("小废", 0.5, 33.02, 2320, 76606.40)]),
    ("2026-09-15", "苏DBB772", "M2", "林凯", "𠓾景通", None, 0,
        [("废钢压块1", 0.7, 71.5, 2370, 169455.00)]),
    ("2026-09-15", "苏DBE961", "M2", "林凯", "君盛", None, 0,
        [("普通压块", 0.8, 31.38, 2250, 70605.00)]),
    ("2026-09-15", "皖四航6058", "M1", None, "尹鹏飞", "煌烽", 1,
        [(None, 6.92, 589.24, 0, 1444596.08)]),
    ("2026-09-16", "皖SC6167", "M5", "闫东朋", "闫东朋", "反向", 0,
        [("重废2", 0.35, 35.504, 2530, 89825.12), ("重废3", 0.15, 15.216, 2510, 38192.16)]),
    ("2026-09-16", "皖AK6033", "M1", None, "邓晶焱", "煌烽", 0,
        [("小废", 0.6, 41.42, 2320, 96094.40)]),
    ("2026-09-17", "苏DBE075", "M2", "林凯", "力拓", None, 0,
        [("铁加压块", 0.4, 81.46, 2530, 206093.80)]),
    ("2026-09-17", "皖AP1626", "M5", "闫东朋", "闫东朋", "反向", 0,
        [("中废1", 0.63, 36.4, 2420, 88088.00), ("剪切中废1", 0.27, 15.6, 2450, 38220.00)]),
    ("2026-09-17", "苏J19371", "M5", "闫东朋", "闫东朋", "反向", 0,
        [("铁加压块", 0.5, 73.54, 2530, 186056.20)]),
    ("2026-09-17", "苏DBE075", "M2", "林凯", "君盛", None, 0,
        [("废钢压块3", 0.315, 20.853, 2290, 47753.37), ("废钢压块2", 0.135, 8.937, 2330, 20823.21)]),
    ("2026-09-17", "苏DBB772", "M2", "林凯", "君盛", None, 0,
        [("废钢压块3", 1.12, 20.188, 2290, 46230.52), ("废钢压块2", 0.48, 8.652, 2330, 20159.16)]),
]


def run_import(db_path):
    if not os.path.exists(db_path):
        print(f"[跳过] 数据库不存在: {db_path}")
        return

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_before_import_20260915_0917_batch_{ts}"
    shutil.copy2(db_path, backup_path)
    print(f"[备份] 已备份到: {backup_path}")

    # 通过 Flask test_client 调用现有 API，复用全部既有业务逻辑（避免手写 SQL 出错/绕过校验）
    sys.path.insert(0, BASE_DIR)
    import db as db_module
    db_module.DB_PATH = db_path  # 覆盖模块级 DB_PATH，确保指向目标库（cloud dev 或 device 均可复用本脚本）；
    # get_db() 函数体内直接引用 db 模块的全局 DB_PATH，调用时才读取，这里覆盖后无需重新 import/reload app
    import app as app_module
    client = app_module.app.test_client()

    def get_lookup(table, name):
        r = client.get(f"/api/{table}")
        rows = r.get_json()
        for row in rows:
            if row.get("name") == name:
                return row["id"]
        return None

    # 供货厂"尹鹏飞"若不存在则新建
    base_yin = get_lookup("processing_bases", "尹鹏飞")
    if base_yin is None:
        r = client.post("/api/processing_bases", json={
            "name": "尹鹏飞", "base_type": "external", "contact": "尹鹏飞",
            "notes": "2026-09-18新增：新供货厂，企业名称待补充，对接人为本人（用户口头确认）",
        })
        assert r.status_code == 201, r.get_json()
        base_yin = r.get_json()["id"]
        print(f"[新建档案] 供货厂 尹鹏飞 -> id={base_yin}")
    else:
        print(f"[已存在] 供货厂 尹鹏飞 id={base_yin}，跳过新建")

    base_ids = {"尹鹏飞": base_yin}
    for name in ["林凯", "君盛", "𠓾景通", "闫东朋", "邓晶焱", "力拓"]:
        bid = get_lookup("processing_bases", name)
        assert bid is not None, f"供货厂档案缺失: {name}"
        base_ids[name] = bid

    dispatcher_ids = {}
    for name in ["林凯", "闫东朋"]:
        did = get_lookup("dispatchers", name)
        assert did is not None, f"调货人档案缺失: {name}"
        dispatcher_ids[name] = did

    invoice_source_ids = {}
    for name in ["煌烽", "反向"]:
        sid = get_lookup("invoice_sources", name)
        assert sid is not None, f"票源方档案缺失: {name}"
        invoice_source_ids[name] = sid

    scrap_type_ids = {}
    r = client.get("/api/scrap_types")
    for row in r.get_json():
        scrap_type_ids[row["name"]] = row["id"]

    mill_id = get_lookup("steel_mills", MILL_NAME)
    account_id = get_lookup("accounts", ACCOUNT_NAME)
    assert mill_id is not None and account_id is not None, "钢厂/户头档案缺失"

    conn = sqlite3.connect(db_path)
    created, skipped = [], []

    for date, plate, mode, dispatcher_name, base_name, src_name, mixed, items in TRIPS:
        total_amt = round(sum(it[4] for it in items), 2)
        # 幂等检查：同车号+同日期+结算金额合计 已存在则跳过（避免重复录入）
        existing = conn.execute(
            "SELECT id FROM trips WHERE plate_no=? AND (order_date=? OR settlement_date=?) "
            "AND ABS(settlement_amount_total - ?) < 0.02",
            (plate, date, date, total_amt),
        ).fetchone()
        if existing:
            skipped.append((plate, date, total_amt, existing[0]))
            continue

        r = client.post("/api/trips", json={
            "status": "reported", "transport_type": "truck", "mode_code": mode,
        })
        assert r.status_code == 201, r.get_json()
        trip_id = r.get_json()["id"]

        details = []
        for scrap_name, ded, ton, price, amt in items:
            details.append({
                "scrap_type_id": scrap_type_ids.get(scrap_name) if scrap_name else None,
                "deduction": ded, "settlement_tonnage": ton, "unit_price": price, "amount": amt,
            })
        r = client.put(f"/api/trips/{trip_id}/details", json={"details": details})
        assert r.status_code == 200, r.get_json()

        patch = {
            "order_date": date, "settlement_date": date, "plate_no": plate,
            "mill_id": mill_id, "account_id": account_id,
            "base_id": base_ids[base_name], "mixed_material": mixed,
            "remarks": "2026-09-18由用户提供的台账数据人工核对后录入（Claude协助整理，用户逐项确认结算模式/供货厂/票源方后导入）",
        }
        if dispatcher_name:
            patch["dispatcher_id"] = dispatcher_ids[dispatcher_name]
        if src_name:
            patch["invoice_source_id"] = invoice_source_ids[src_name]
        r = client.put(f"/api/trips/{trip_id}", json=patch)
        assert r.status_code == 200, r.get_json()

        r = client.post(f"/api/trips/{trip_id}/status", json={"status": "settled"})
        assert r.status_code == 200, r.get_json()

        fin = client.get(f"/api/trips/{trip_id}").get_json()["finance"]
        created.append((trip_id, plate, date, total_amt, fin.get("net_profit")))

    conn.close()

    print(f"\n[结果] 新建 {len(created)} 个车次，跳过 {len(skipped)} 个已存在车次")
    for tid, plate, date, amt, np in created:
        print(f"  #{tid}\t{date}\t{plate}\t结算金额{amt:,.2f}\t净利润{np:,.2f}" if np is not None else f"  #{tid}\t{date}\t{plate}\t结算金额{amt:,.2f}\t净利润计算失败")
    for plate, date, amt, existing_id in skipped:
        print(f"  [跳过-已存在] {date}\t{plate}\t{amt:,.2f}\t(已有车次#{existing_id})")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    run_import(target)
