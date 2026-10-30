"""钢厂结算单导入辅助（从 core.py 拆出）。"""
from db import rows_to_list

# ---------------------------------------------------------------------------
# v2：钢厂结算单导入（2026-09-22）。前端把粘贴的表格解析成车次(一车多料已合并)，
# 这里按 车号 + 日期(±3天) 找已有车次；找不到的可以新建。确认后写磅重明细、结算日期/单号、推到已结算。
# ---------------------------------------------------------------------------
def _match_trips(conn, plate, date):
    if not plate:
        return []
    rows = conn.execute(
        """SELECT t.id, t.status, t.order_date, t.settlement_date, t.settlement_amount_total, t.settlement_tonnage_total,
                  (SELECT COUNT(*) FROM trip_scrap_details d WHERE d.trip_id=t.id) AS detail_count
           FROM trips t WHERE t.plate_no=? AND t.status!='voided'
             AND (? IS NULL OR abs(julianday(coalesce(t.settlement_date, t.order_date)) - julianday(?)) <= 3)
           ORDER BY abs(julianday(coalesce(t.settlement_date, t.order_date)) - julianday(coalesce(?, t.order_date))), t.id DESC""",
        (plate, date, date, date)).fetchall()
    return rows_to_list(rows)

def _lookup_id(conn, table, name):
    if not name:
        return None
    r = conn.execute(f"SELECT id FROM {table} WHERE name=?", (name.strip(),)).fetchone()
    return r["id"] if r else None
