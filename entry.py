"""录入辅助（从 core.py 拆出）：默认值/车牌画像/状态推进/快速结算。"""
import sqlite3

from archives_config import STATUS_DATE_FIELD
from system_ops import audit

def _get_setting(conn, key, default=None):
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row["value"] if row and row["value"] not in (None, "") else default

def _entry_defaults(conn):
    """新建车次默认值(A阶段)：钢厂只有一个 → 默认它；户头 = 绑定该钢厂的户头(只有一个时)；
    接单日期今天；发货地取 app_settings.default_departure_place(默认永康)。
    以后加了第二个钢厂，钢厂就不再默认，但选了钢厂后户头仍自动跟随(前端同一规则)。"""
    d = {"status": "reported", "transport_type": "truck",
         "order_date": conn.execute("SELECT date('now','localtime')").fetchone()[0]}
    place = _get_setting(conn, "default_departure_place", "永康")
    if place:
        d["departure_place"] = place
    mills = conn.execute("SELECT id FROM steel_mills").fetchall()
    if len(mills) == 1:
        d["mill_id"] = mills[0]["id"]
        accs = conn.execute("SELECT id FROM accounts WHERE mill_id=?", (d["mill_id"],)).fetchall()
        if len(accs) == 1:
            d["account_id"] = accs[0]["id"]
    return d

# 车牌画像里哪些字段参与"带出"：只有该车牌历史上这个字段足够稳定(同一取值占比≥80%)才带出，
# 否则留空让人选。例如苏DBB772是林凯车队的车：调货人16次全是林凯→带出；供货厂在林凯/君盛/
# 𠓾景通之间变→不带，留空。司机信息取最近一次非空值。
PLATE_PROFILE_FIELDS = ["transport_type", "base_id", "dispatcher_id", "invoice_source_id", "template_id"]
PLATE_STABLE_RATIO = 0.8   # 回退默认值；实际取值见「系统 → 参数设置」（plate_stable_ratio）
FLEET_ALIAS_RATIO = 0.5    # 回退默认值；实际取值见「系统 → 参数设置」（fleet_alias_ratio）

def _plate_profile(conn, plate):
    import settings   # 阈值可在「系统 → 参数设置」改（C档 2026-10-09），读不到就回退上面的默认
    stable_ratio = settings.get(conn, "plate_stable_ratio", PLATE_STABLE_RATIO)
    fleet_ratio = settings.get(conn, "fleet_alias_ratio", FLEET_ALIAS_RATIO)
    rows = conn.execute(
        f"""SELECT {','.join(PLATE_PROFILE_FIELDS)}, driver_name, driver_phone, driver_id_card
            FROM trips WHERE plate_no=? AND status!='voided' ORDER BY coalesce(order_date, settlement_date) DESC, id DESC""",
        (plate,)).fetchall()
    out = {"plate_no": plate, "count": len(rows), "fill": {}, "varies": {}}
    if not rows:
        return out
    # 调货人自己车队的车(如苏DBB772，林凯车队)：历史上供货厂记成了"林凯"这类占位基地
    # (is_dispatcher_alias=1，其实就是调货人本人)。用户2026-09-21明确：这类车供货厂先留空、
    # 不强制录入，所以占位基地不参与带出，也不算进"稳定"比例的分子。
    alias_bases = {r["id"] for r in conn.execute("SELECT id FROM processing_bases WHERE is_dispatcher_alias=1")}
    out["fleet_of_dispatcher"] = None
    for f in PLATE_PROFILE_FIELDS:
        vals = [r[f] for r in rows if r[f] not in (None, "") and not (f == "base_id" and r[f] in alias_bases)]
        if not vals:
            continue
        top = max(set(vals), key=vals.count)
        if vals.count(top) / len(rows) >= stable_ratio:
            out["fill"][f] = top
        else:
            out["varies"][f] = sorted(set(vals), key=lambda v: -vals.count(v))
    alias_n = sum(1 for r in rows if r["base_id"] in alias_bases)
    if alias_n / len(rows) >= fleet_ratio and out["fill"].get("dispatcher_id"):
        out["fleet_of_dispatcher"] = out["fill"]["dispatcher_id"]
    for f in ("driver_name", "driver_phone", "driver_id_card"):
        v = next((r[f] for r in rows if r[f]), None)
        if v:
            out["fill"][f] = v
    return out

def _fill_status_date(conn, trip_id, status, date=None):
    """把目标状态对应的日期填上：已有日期不覆盖(回退后再推进保留原日期)，没有就填
    传入日期(批量弹框里选的)或今天。"""
    field = STATUS_DATE_FIELD.get(status)
    if field:
        conn.execute(f"UPDATE trips SET {field}=coalesce(?, date('now','localtime')) WHERE id=? AND ({field} IS NULL OR {field}='')",
                     (date or None, trip_id))

def _quick_settle(conn, trip_id, tonnage, amount):
    """推进到"已结算"时的快速录入：只填结算吨位+金额。只在车次还没有任何磅重明细时生效
    （写一行不指定货型的明细，单价=金额/吨位，混料标记打上），已有明细的车次不覆盖，
    以明细为准——避免快速录入把用户逐货型录好的数据冲掉。返回是否写入。"""
    if tonnage in (None, "") or amount in (None, ""):
        return False
    tonnage, amount = float(tonnage), float(amount)
    if conn.execute("SELECT 1 FROM trip_scrap_details WHERE trip_id=?", (trip_id,)).fetchone():
        return False
    unit_price = round(amount / tonnage, 4) if tonnage else 0
    conn.execute(
        """INSERT INTO trip_scrap_details (trip_id, scrap_type_id, net_weight, deduction, settlement_tonnage,
           unit_price, cutting_fee, amount) VALUES (?,?,?,?,?,?,?,?)""",
        (trip_id, None, tonnage, 0, tonnage, unit_price, 0, amount))
    conn.execute(
        """UPDATE trips SET net_weight_total=?, deduction_total=0, settlement_tonnage_total=?, cutting_fee_total=0,
           settlement_amount_total=?, mixed_material=1 WHERE id=?""", (tonnage, tonnage, amount, trip_id))
    return True

def _apply_status(conn, trip_id, status, extra=None):
    """单车/批量共用：改状态 + 填日期 +（可选）结算单号/结算日期/快速结算吨位金额。"""
    extra = extra or {}
    old = conn.execute("SELECT status FROM trips WHERE id=?", (trip_id,)).fetchone()
    if old:
        audit(conn, "trips", trip_id, "status", old["status"], status)
    if extra.get("settlement_doc_no"):
        conn.execute("UPDATE trips SET settlement_doc_no=? WHERE id=?", (extra["settlement_doc_no"], trip_id))
    _fill_status_date(conn, trip_id, status, extra.get("status_date") or extra.get(STATUS_DATE_FIELD.get(status, "")))
    wrote = False
    if status == "settled":
        wrote = _quick_settle(conn, trip_id, extra.get("settlement_tonnage_total"), extra.get("settlement_amount_total"))
    conn.execute("UPDATE trips SET status=?, updated_at=datetime('now','localtime') WHERE id=?", (status, trip_id))
    return wrote
