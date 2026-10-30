"""一次性导入脚本：
1) 华西特钢报价单品类与价格（转录自用户截图，2026-09-12期，附上一期9.11用于对比涨跌）
2) 台账 Excel（8.19付款明细-欧冶物宝1.xlsx）历史车次数据，作为已固化快照导入

运行： python imports/import_data.py /path/to/8.19付款明细-欧冶物宝1.xlsx
（需先运行过 init_db.py 建好库结构）

说明：历史台账数据的计费口径和新系统的"货差+票差"拆分模型不完全一致（台账用的是混合折算系数），
为忠实还原历史真实数字，导入时把台账的"综合折算系数×结算金额"整体记在"货差利润"字段（仅借用字段，
注释里会标注 legacy_import），"票差利润"记为0，这样 净利润 = 台账原始"实际利润"，不失真。
外部通道服务费类车次（供货厂列有值的几行）导入到 service_fee_income 字段，逻辑同理。
所有历史行导入后直接标记为已固化快照（is_locked=1），不会被新公式覆盖。
"""
import sqlite3
import sys
import os
import re
import datetime as dt

# 标准机动车车牌格式：省份汉字 + 一位字母 + 4~6位字母数字（如 苏DBB772）。
# 不符合此格式的"车号"视为船名（如 苏兴达货8566、通海机9998、鲁济宁货1567 等内河船舶命名）。
TRUCK_PLATE_RE = re.compile(r"^[一-鿿][A-Z][A-Z0-9]{4,6}$")


def guess_transport_type(plate):
    if plate and TRUCK_PLATE_RE.match(str(plate).strip()):
        return "truck"
    return "ship"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import openpyxl
from price_sheet_huaxi import SCRAP_ROWS, NAME_ALIASES, FOOTER_NOTES, MILL_NAME, ACCOUNT_NAME, FINANCIER_NAME

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "scrapmetal.db")


def get_or_create(conn, table, match, defaults=None):
    defaults = defaults or {}
    where = " AND ".join(f"{k}=?" for k in match)
    row = conn.execute(f"SELECT * FROM {table} WHERE {where}", list(match.values())).fetchone()
    if row:
        return row["id"]
    data = {**match, **defaults}
    cols = list(data.keys())
    placeholders = ",".join(["?"] * len(cols))
    cur = conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", [data[c] for c in cols])
    conn.commit()
    return cur.lastrowid


def import_price_sheet(conn):
    print("== 导入报价单品类与价格 ==")
    mill_id = get_or_create(conn, "steel_mills", {"name": MILL_NAME}, {
        "is_whitelist": 0,
        "entry_requirement": "必须国6排放或新能源车方能入门禁进厂，进厂前水箱必须放空",
        "notes": "白名单状态待用户确认",
    })
    get_or_create(conn, "mill_reward_tiers", {"mill_id": mill_id, "min_tonnage": 3000}, {"unit_price": 20})
    get_or_create(conn, "mill_reward_tiers", {"mill_id": mill_id, "min_tonnage": 5000}, {"unit_price": 25})

    financier_id = get_or_create(conn, "financiers", {"name": FINANCIER_NAME})
    account_id = get_or_create(conn, "accounts", {"name": ACCOUNT_NAME}, {
        "mill_id": mill_id, "financier_id": financier_id, "is_our_own": 1,
        "advance_fee_rate": 23, "external_settlement_rate": 14.0,
    })

    scrap_id_by_name = {}
    for category, name, example, size, thickness, prev_price, cur_price, suspended in SCRAP_ROWS:
        sid = get_or_create(conn, "scrap_types", {"category": category, "name": name}, {
            "example_desc": example, "size_requirement": size, "thickness_requirement": thickness,
        })
        scrap_id_by_name[name] = sid

    prev_sheet_id = get_or_create(conn, "quote_sheets", {"mill_id": mill_id, "period_label": "2026年9月11日期"}, {
        "effective_at": "2026-09-11 12:00",
    })
    cur_sheet_id = get_or_create(conn, "quote_sheets", {"mill_id": mill_id, "period_label": "2026年9月12日12时起执行"}, {
        "effective_at": "2026-09-12 12:00", "footer_notes": FOOTER_NOTES,
    })

    for category, name, example, size, thickness, prev_price, cur_price, suspended in SCRAP_ROWS:
        sid = scrap_id_by_name[name]
        conn.execute("DELETE FROM quote_items WHERE quote_sheet_id=? AND scrap_type_id=?", (prev_sheet_id, sid))
        conn.execute(
            "INSERT INTO quote_items (quote_sheet_id, scrap_type_id, price, is_suspended) VALUES (?,?,?,?)",
            (prev_sheet_id, sid, prev_price, int(bool(suspended and prev_price is None))),
        )
        conn.execute("DELETE FROM quote_items WHERE quote_sheet_id=? AND scrap_type_id=?", (cur_sheet_id, sid))
        conn.execute(
            "INSERT INTO quote_items (quote_sheet_id, scrap_type_id, price, is_suspended) VALUES (?,?,?,?)",
            (cur_sheet_id, sid, cur_price, int(bool(suspended))),
        )
    conn.commit()
    print(f"  钢厂id={mill_id} 户头id={account_id} 报价单(本期)id={cur_sheet_id}，货型{len(scrap_id_by_name)}个")
    return mill_id, account_id, scrap_id_by_name


def get_scrap_id(conn, scrap_id_by_name, raw_name):
    raw_name = raw_name or "未注明"
    name = NAME_ALIASES.get(raw_name, raw_name)
    if name in scrap_id_by_name:
        return scrap_id_by_name[name]
    # 台账里出现但报价单没有的品名，临时归到"历史导入-待归类"
    sid = get_or_create(conn, "scrap_types", {"category": "历史导入-待归类", "name": name}, {})
    scrap_id_by_name[name] = sid
    return sid


def import_invoice_sources(conn):
    print("== 导入票源方档案（Sheet1）==")
    rows = [
        ("反向", "reverse_individual", 13.8, "annual", 2100000, 1800000),
        ("俊兴", "front_company", 13.5, "monthly", None, None),
        ("益周", "front_company", 12.8, "monthly", None, None),
        ("煌烽", "front_company", 13.2, "monthly", None, None),
    ]
    ids = {}
    for name, stype, rate, qperiod, qamount, warn in rows:
        sid = get_or_create(conn, "invoice_sources", {"name": name}, {
            "source_type": stype, "rate_point": rate, "quota_period": qperiod,
            "quota_amount": qamount, "warn_threshold": warn,
        })
        ids[name] = sid
    print(f"  导入 {len(ids)} 个票源方")
    return ids


# 台账历史数据中，货主(V列)/供货厂(I列) 出现的以下姓名实际是外部调货人（调货人对接货源），
# 而非真正的货主签收人或司机姓名——2026-09 用户确认。
# 闫东鹏 与 闫东朋 同音（péng），无其他区分信息，暂判断为同一人的笔误变体，统一归并为"闫东朋"；
# 如后续核实其实是两个不同的人，请在调货人档案中拆分并改回历史车次的 dispatcher_id。
DISPATCHER_NAMES = {"林凯", "邓晶焱", "闫东朋", "李涛", "陈杰"}
DISPATCHER_NAME_ALIASES = {"闫东鹏": "闫东朋"}

# 台账 A 列（表头"序号"，实际被挪用记录这一车走的是哪个票源方）里出现的值：
# 具体票源方名称（反向/煌烽/益周/俊兴）=> 关联对应 invoice_sources 档案；
# "/" 或 "自带票" => 供货方自带发票，不走我方票源，不关联；空白 => 未标注，不关联。
INVOICE_SOURCE_MARKS = {"反向", "煌烽", "益周", "俊兴"}


def import_ledger(conn, xlsx_path, mill_id, account_id, scrap_id_by_name):
    print("== 导入台账历史车次（作为已固化快照）==")
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["总"]

    base_cache = {}
    dispatcher_cache = {}

    def get_base(name):
        if not name:
            return None
        if name not in base_cache:
            base_cache[name] = get_or_create(conn, "processing_bases", {"name": name}, {
                "base_type": "external", "default_mode_code": "M3",
                "notes": "由台账历史数据自动创建，信息不完整，请在基础档案中补全",
            })
        return base_cache[name]

    def canon_name(name):
        return DISPATCHER_NAME_ALIASES.get(name, name) if name else name

    def get_dispatcher(name):
        canon = canon_name(name)
        if canon not in dispatcher_cache:
            dispatcher_cache[canon] = get_or_create(conn, "dispatchers", {"name": canon}, {
                "commission_per_ton": 0,
                "notes": "台账历史导入-货主字段识别为调货人",
            })
        return dispatcher_cache[canon]

    default_base_id = get_or_create(conn, "processing_bases", {"name": "台账历史导入-默认基地(待核实)"}, {
        "base_type": "own", "default_mode_code": "M1",
        "notes": "台账中未标注具体供货厂的历史车次统一挂在此处，请在车次详情中逐条核实后改派到正确的供货厂",
    })

    invoice_source_cache = {}

    def get_invoice_source(name):
        if name not in invoice_source_cache:
            invoice_source_cache[name] = get_or_create(conn, "invoice_sources", {"name": name}, {
                "source_type": "front_company", "quota_period": "monthly",
                "notes": "由台账历史数据自动创建，请在基础档案中补全票种/点位等信息",
            })
        return invoice_source_cache[name]

    imported = 0
    merged_lines = 0
    skipped = 0
    last_trip_id = None
    last_trip_invoice_source_id = None
    last_trip_is_channel_fee = False

    for r in range(5, ws.max_row + 1):
        weigh_date = ws.cell(row=r, column=3).value  # C 过磅日期（合并单元格，续行留空）
        plate = ws.cell(row=r, column=4).value  # D 车号（续行可能留空，也可能重复上一行车号）
        scrap_name = ws.cell(row=r, column=5).value  # E 品名
        settlement_tonnage = ws.cell(row=r, column=16).value  # P
        settlement_amount = ws.cell(row=r, column=20).value  # T
        if settlement_tonnage in (None, "") or settlement_amount in (None, ""):
            skipped += 1
            continue

        gross = ws.cell(row=r, column=13).value  # M 钢厂毛重
        tare = ws.cell(row=r, column=14).value  # N 钢厂皮重
        deduction = ws.cell(row=r, column=15).value or 0  # O 钢厂扣杂
        unit_price = ws.cell(row=r, column=17).value  # Q
        cutting_fee = ws.cell(row=r, column=18).value or 0  # R
        advance_profit = ws.cell(row=r, column=24).value  # X 垫资利润(成本)
        reward_profit = ws.cell(row=r, column=25).value  # Y 奖励利润
        net_profit = ws.cell(row=r, column=26).value  # Z 实际利润
        advance_profit = float(advance_profit) if advance_profit is not None else 0.0
        reward_profit = float(reward_profit) if reward_profit is not None else 0.0
        net_profit = float(net_profit) if net_profit is not None else None

        scrap_id = get_scrap_id(conn, scrap_id_by_name, scrap_name)

        source_mark = ws.cell(row=r, column=1).value  # A 列（表头写"序号"，实际记录票源方标注）

        # 续行判定：以"过磅日期"(C列，合并单元格)是否留空为准 —— 不能只看车号，因为台账里
        # 一车多料的续行有两种写法：车号也留空，或者车号重复写一遍但日期留空。两种都代表
        # "上一车"的第二个（或更多）货型，都应归并到上一车而不是新开一车。
        if weigh_date is None:
            # 只归并到刚导入的那一车，不新建车次；没有可归并的上车则视为无法归属，跳过。
            if last_trip_id is None:
                skipped += 1
                continue
            conn.execute(
                """INSERT INTO trip_scrap_details
                   (trip_id, scrap_type_id, gross_weight, tare_weight, net_weight, deduction,
                    settlement_tonnage, unit_price, cutting_fee, amount)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (last_trip_id, scrap_id, gross, tare, (gross - tare) if (gross and tare) else None, deduction,
                 settlement_tonnage, unit_price, cutting_fee, settlement_amount),
            )
            conn.execute(
                """UPDATE trips SET settlement_tonnage_total = settlement_tonnage_total + ?,
                       deduction_total = deduction_total + ?, settlement_amount_total = settlement_amount_total + ?,
                       cutting_fee_total = cutting_fee_total + ?, mixed_material = 1
                   WHERE id = ?""",
                (settlement_tonnage, deduction, settlement_amount, cutting_fee, last_trip_id),
            )
            # 续行自己也标注了票源方，且这一车此前还没有票源方时，补上（极少见，历史数据里
            # 目前都标在主行上，这里只是兜底）
            if source_mark in INVOICE_SOURCE_MARKS and last_trip_invoice_source_id is None:
                last_trip_invoice_source_id = get_invoice_source(source_mark)
                conn.execute("UPDATE trips SET invoice_source_id=? WHERE id=?", (last_trip_invoice_source_id, last_trip_id))
            if last_trip_is_channel_fee:
                row_service_fee_income = (net_profit - reward_profit) if net_profit is not None else 0
                conn.execute(
                    """UPDATE trip_finance SET
                           service_fee_income = COALESCE(service_fee_income,0) + ?,
                           reward_profit = COALESCE(reward_profit,0) + ?,
                           net_profit = COALESCE(net_profit,0) + ?
                       WHERE trip_id = ?""",
                    (row_service_fee_income, reward_profit, net_profit or 0, last_trip_id),
                )
            else:
                row_blended = (net_profit + advance_profit - reward_profit) if net_profit is not None else 0
                conn.execute(
                    """UPDATE trip_finance SET
                           freight_diff_profit = COALESCE(freight_diff_profit,0) + ?,
                           advance_fee_cost = COALESCE(advance_fee_cost,0) + ?,
                           reward_profit = COALESCE(reward_profit,0) + ?,
                           net_profit = COALESCE(net_profit,0) + ?
                       WHERE trip_id = ?""",
                    (row_blended, advance_profit, reward_profit, net_profit or 0, last_trip_id),
                )
            merged_lines += 1
            continue

        date_str = weigh_date.date().isoformat() if isinstance(weigh_date, (dt.date, dt.datetime)) else None

        supplier = ws.cell(row=r, column=9).value  # I 供货厂（仅部分行有值）
        owner = ws.cell(row=r, column=22).value  # V 货主

        owner_clean = owner if owner not in ("/", None) else None
        owner_canon = canon_name(owner_clean)
        supplier_canon = canon_name(supplier)

        dispatcher_id = None
        if owner_canon in DISPATCHER_NAMES:
            dispatcher_id = get_dispatcher(owner_canon)
        elif supplier_canon in DISPATCHER_NAMES:
            dispatcher_id = get_dispatcher(supplier_canon)

        # 模式判定沿用原逻辑：以台账 I 列原始是否有值为准，不受调货人识别影响
        is_channel_fee_row = bool(supplier)  # I 列有值 => 外部供货厂通道服务费模式（模式二/三）
        mode_code = "M3" if is_channel_fee_row else "M1"

        if supplier:
            base_id = get_base(supplier_canon)
        elif owner_canon in DISPATCHER_NAMES:
            # I 列（供货厂）未填写，但货主已被识别为调货人：按用户确认的兜底规则，
            # 供货厂暂用调货人同名占位（例如"邓晶焱"货主 -> "邓晶焱"供货厂），后续核实后再改派
            base_id = get_base(owner_canon)
        else:
            base_id = default_base_id

        # 货主字段：若被识别为调货人，则不再写入司机姓名字段（司机姓名未知），
        # 改为通过 dispatcher_id 结构化记录；否则保持原样写入 driver_name（如李涛/陈杰等真实姓名，未被指认为调货人）
        final_driver_name = None if owner_canon in DISPATCHER_NAMES else owner_clean

        owner_note = ""
        if owner_clean and owner_canon in DISPATCHER_NAMES:
            if owner_clean != owner_canon:
                owner_note = f" [原货主:{owner_clean}，识别为调货人{owner_canon}(同音疑似同一人，待用户确认)]"
            else:
                owner_note = f" [原货主:{owner_clean}]"

        transport_type = guess_transport_type(plate)

        invoice_source_id = get_invoice_source(source_mark) if source_mark in INVOICE_SOURCE_MARKS else None

        row_remarks = (
            "由台账 Excel 历史数据自动导入（8.19付款明细-欧冶物宝1.xlsx 第%d行），日期为过磅日期近似值" % r
        ) + owner_note

        cur = conn.execute(
            """INSERT INTO trips
               (transport_type, status, mode_code, order_date, base_id, mill_id, account_id,
                plate_no, driver_name, dispatcher_id, invoice_source_id, settlement_tonnage_total, deduction_total,
                settlement_amount_total, cutting_fee_total, settlement_date, is_paid, remarks)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
            (
                transport_type, "paid", mode_code, date_str, base_id, mill_id, account_id,
                plate, final_driver_name, dispatcher_id, invoice_source_id,
                settlement_tonnage, deduction, settlement_amount, cutting_fee, date_str,
                row_remarks,
            ),
        )
        trip_id = cur.lastrowid

        conn.execute(
            """INSERT INTO trip_scrap_details
               (trip_id, scrap_type_id, gross_weight, tare_weight, net_weight, deduction,
                settlement_tonnage, unit_price, cutting_fee, amount)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (trip_id, scrap_id, gross, tare, (gross - tare) if (gross and tare) else None, deduction,
             settlement_tonnage, unit_price, cutting_fee, settlement_amount),
        )

        if is_channel_fee_row:
            conn.execute(
                """INSERT INTO trip_finance
                   (trip_id, service_fee_type, service_fee_value, service_fee_income,
                    reward_unit_price, reward_profit, net_profit, is_locked, locked_at)
                   VALUES (?,?,?,?,?,?,?,1,datetime('now','localtime'))""",
                (trip_id, "per_ton", None,
                 (net_profit - reward_profit) if net_profit is not None else None,
                 (reward_profit / settlement_tonnage) if settlement_tonnage else 0,
                 reward_profit, net_profit),
            )
        else:
            blended_margin = (net_profit + advance_profit - reward_profit) if net_profit is not None else 0
            conn.execute(
                """INSERT INTO trip_finance
                   (trip_id, freight_diff_unit_price, freight_diff_profit, invoice_diff_profit,
                    reward_unit_price, reward_profit, advance_fee_rate, advance_fee_cost,
                    net_profit, is_locked, locked_at)
                   VALUES (?,?,?,?,?,?,?,?,?,1,datetime('now','localtime'))""",
                (trip_id, None, blended_margin, 0,
                 (reward_profit / settlement_tonnage) if settlement_tonnage else 0,
                 reward_profit, 23, advance_profit, net_profit),
            )
        last_trip_id = trip_id
        last_trip_invoice_source_id = invoice_source_id
        last_trip_is_channel_fee = is_channel_fee_row
        imported += 1

    # 一车多料合并后，量补单价(元/吨)按整车合计吨位重新摊算，避免仍停留在只按第一个货型算出的单价
    for row in conn.execute("SELECT id, settlement_tonnage_total FROM trips WHERE mixed_material = 1"):
        fin = conn.execute("SELECT reward_profit FROM trip_finance WHERE trip_id=?", (row["id"],)).fetchone()
        if fin and row["settlement_tonnage_total"]:
            conn.execute(
                "UPDATE trip_finance SET reward_unit_price=? WHERE trip_id=?",
                (fin["reward_profit"] / row["settlement_tonnage_total"], row["id"]),
            )

    conn.commit()
    print(f"  导入车次 {imported} 条（含一车多料自动归并续行 {merged_lines} 条），跳过（缺关键字段/无法归属车次）{skipped} 条")


def main():
    if len(sys.argv) < 2:
        print("用法：python imports/import_data.py /path/to/8.19付款明细-欧冶物宝1.xlsx")
        sys.exit(1)
    xlsx_path = sys.argv[1]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    mill_id, account_id, scrap_id_by_name = import_price_sheet(conn)
    import_invoice_sources(conn)
    import_ledger(conn, xlsx_path, mill_id, account_id, scrap_id_by_name)

    conn.close()
    print("导入完成。")


if __name__ == "__main__":
    main()
