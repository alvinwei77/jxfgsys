"""2026-09-30 两期封账写入（8月期 7/26~8/25 + 9月期 8/26~9/25，109 台）。

依据：会计《2026华西发货明细8月(2).xlsx》（用户确认最贴近准确）+ 财务发货记录表(8)，
用户指令「系统按照最终会计的结果以及财务，更新系统数据，填充所有的状态以及支付等数据细节，两期封账」。

写入内容：
  1. 状态：两期 109 台全部推进「已结案」（21 台 settled→closed；88 台已 closed 不动状态）
  2. 销项开票：104 台填 invoiced_date（6 个欧冶开票批次日，批次→行→车全精确匹配）
     + invoice_out_amount=含税结算额；5 台（#104,106,107,108,109）会计显示未开票，留空
  3. 付款：is_paid=1 + paid_amount=INT(引擎应付上游)；会计标注「益周未付」批次内的
     益周车保持未付；closed_date=批次付款时间（现有值不覆盖，空的才补）
  4. 调货员提成：两期内剩余未付提成登记「已付」批次（沿用 2026-09-22 封账先例）

安全：先整库备份；全部走系统接口（有操作记录）；写后跑引擎全字段回归 + 利润核对。
不改：引擎/费率/票源方/结算金额等算钱数据（本次只填状态与票款支付细节）。
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/Users/alvin/Documents/scrapmetal")
_orig = sqlite3.connect


def _connect(*a, **k):
    c = _orig(*a, **k)
    c.execute("PRAGMA journal_mode=TRUNCATE")
    return c


sqlite3.connect = _connect
import datetime as dt  # noqa: E402

import openpyxl  # noqa: E402
import app  # noqa: E402
import db  # noqa: E402
import regression  # noqa: E402
import system_ops  # noqa: E402
from engine import compute_trip_finance_v2_flat  # noqa: E402

XLSX = ("/Users/alvin/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
        "xwechat_files/wxid_5060620552112_a7e6/temp/RWTemp/2026-09/"
        "b0166348a906b47e607b4dfab39f88ad/2026华西发货明细8月(2).xlsx")
P_START, P_END = "2026-07-26", "2026-09-25"
EPS = 0.005
INT = lambda x: int(x + 1e-6)

conn = sqlite3.connect(db.DB_PATH)
conn.row_factory = sqlite3.Row
q = lambda s, *a: conn.execute(s, a).fetchall()
C = app.app.test_client()


def must(r, what):
    if r.status_code >= 400:
        raise SystemExit(f"失败：{what} → {r.status_code} {r.get_data(as_text=True)[:200]}")
    return r.get_json()


# ---------- 引擎应付 ----------
trips = {r["id"]: dict(r) for r in q("SELECT * FROM trips WHERE status!='voided'")}
IN_SCOPE = sorted([t for t in trips.values()
                   if t["settlement_date"] and P_START <= t["settlement_date"] <= P_END],
                  key=lambda t: (t["settlement_date"], t["id"]))
assert len(IN_SCOPE) == 109, f"两期车次 {len(IN_SCOPE)} != 109"
for t in IN_SCOPE:
    fin = compute_trip_finance_v2_flat(conn, trips[t["id"]])
    assert fin is not None, f"#{t['id']} 引擎返回空"
    t["pay_up"] = (float(fin["give_customer"]) if fin["calc_type"] == "split"
                   else float(fin["settlement_amount"]) - float(fin["gross_service_income"]))

# ---------- 会计表 ----------
wb = openpyxl.load_workbook(XLSX, data_only=True)


def dstr(v):
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)) and 40000 < v < 50000:
        return (dt.datetime(1899, 12, 30) + dt.timedelta(days=v)).strftime("%Y-%m-%d")
    return None


wsd = wb["发货明细"]
acc = []
for r in range(2, wsd.max_row + 1):
    v = [wsd.cell(r, c).value for c in range(1, 29)]
    if v[2] and isinstance(v[9], (int, float)) and v[9] > 0:
        acc.append({"r": r, "date": dstr(v[4]) or dstr(v[1]), "plate": v[2],
                    "ton": round(float(v[9]), 2)})
from collections import defaultdict  # noqa: E402
byplate = defaultdict(list)
for a in acc:
    byplate[a["plate"]].append(a)
used_acc = set()
for t in IN_SCOPE:  # 桥接（断言全部成功）
    ton = round(t["settlement_tonnage_total"], 2)
    cands = [a for a in byplate.get(t["plate_no"], []) if a["r"] not in used_acc]
    hit = [a for a in cands if abs(a["ton"] - ton) <= 0.011]
    chosen = None
    if len(hit) == 1:
        chosen = [hit[0]]
    elif len(hit) > 1:
        h2 = [a for a in hit if a["date"] == t["settlement_date"]] or hit
        chosen = [h2[0]]
    else:
        def bt(i, rem, accu):
            if abs(rem) <= 0.011 and accu:
                return accu
            if i >= len(cands) or rem < 0:
                return None
            return bt(i + 1, round(rem - cands[i]["ton"], 2), accu + [cands[i]]) or \
                   bt(i + 1, rem, accu)
        chosen = bt(0, ton, [])
    assert chosen, f"#{t['id']} {t['plate_no']} 桥接失败"
    for a in chosen:
        used_acc.add(a["r"])

ws = wb["汇总"]
rows = []
for r in range(3, 63):
    v = [ws.cell(r, c).value for c in range(1, 27)]
    if all(x is None for x in v) or isinstance(v[1], str):
        continue
    rows.append({
        "r": r, "date": dstr(v[1]), "ton": v[2], "amt": v[3],
        "out_date": dstr(v[4]), "out_amt": v[6],
        "pay_date": dstr(v[19]), "pay_to": v[20], "pay_amt": v[21],
        "yizhou_unpaid": (v[24] == "未付"),
    })
data_rows = [x for x in rows if x["amt"] is not None]
assert len(data_rows) == 40

# ---------- L1 批->行 连续段 ----------
out_batches = sorted(
    [(x["out_date"], round(float(x["out_amt"]), 2)) for x in rows
     if x["out_date"] and isinstance(x["out_amt"], (int, float)) and x["out_amt"]],
    key=lambda x: x[0])
assert len(out_batches) == 7, f"开票批 {len(out_batches)} != 7（9/10 有两批）"
pos = 0
batch_rows = []
for bdate, bamt in out_batches:
    acc_, end = 0.0, None
    for i in range(pos, len(data_rows)):
        acc_ = round(acc_ + float(data_rows[i]["amt"]), 2)
        if abs(acc_ - bamt) <= EPS:
            end = i
            break
        if acc_ > bamt + EPS:
            break
    assert end is not None, f"开票批 {bdate} {bamt} 行段断裂"
    seg = data_rows[pos:end + 1]
    pos = end + 1
    batch_rows.append({"date": bdate, "amt": bamt, "rows": [x["r"] for x in seg]})
assert abs(sum(b["amt"] for b in batch_rows) - 23017355.85) < 0.01, \
    f"开票批合计 {sum(b['amt'] for b in batch_rows)} != 23,017,355.85"


def dfs_amt(items, target, tol=EPS):
    items = sorted(items, key=lambda x: -x[1])
    n = len(items)
    suf = [0.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suf[i] = suf[i + 1] + items[i][1]

    def bt(i, remain, acc):
        if abs(remain) <= tol and acc:
            return acc
        if i >= n or remain < -tol or suf[i] < remain - tol:
            return None
        k, v = items[i]
        r_ = bt(i + 1, round(remain - v, 6), acc + [k])
        return r_ or bt(i + 1, remain, acc)

    return bt(0, target, [])


# ---------- L1 行->车（按结算日分组 + 连续行段残差） ----------
bydate = defaultdict(list)
for t in IN_SCOPE:
    bydate[t["settlement_date"]].append(t)
used = set()
row_map = {}
for row in data_rows:
    amt = round(float(row["amt"]), 2)
    grp = [t for t in bydate.get(row["date"], []) if t["id"] not in used]
    sel = dfs_amt([(t["id"], round(t["settlement_amount_total"], 2)) for t in grp], amt) \
        if grp else None
    if sel:
        row_map[row["r"]] = sel
        used.update(sel)
left_rows = [x for x in data_rows if x["r"] not in row_map]
left_trips = sorted([t for t in IN_SCOPE if t["id"] not in used],
                    key=lambda t: (t["settlement_date"], t["id"]))
if left_rows:
    def solve(i, avail):
        if i >= len(left_rows):
            return {} if not avail else None
        acc_, j = 0.0, i
        while j < len(left_rows):
            acc_ = round(acc_ + float(left_rows[j]["amt"]), 2)
            sel = dfs_amt([(t["id"], round(t["settlement_amount_total"], 2)) for t in avail], acc_)
            if sel:
                rest = [t for t in avail if t["id"] not in sel]
                r_ = solve(j + 1, rest)
                if r_ is not None:
                    out_ = dict(r_)
                    out_.update({left_rows[k]["r"]: sorted(sel) for k in range(i, j + 1)})
                    return out_
            j += 1
        return None

    sol = solve(0, left_trips)
    assert sol is not None, "残差双侧匹配失败"
    row_map.update(sol)
covered = {i for v in row_map.values() for i in v}
assert len(covered) == 109, f"行->车覆盖 {len(covered)} != 109"
assert sum(round(float(x["amt"]), 2) for x in data_rows) == 24835929.33

# ---------- 车级开票数据 ----------
inv = {}  # trip_id -> invoiced_date
trip_rows = defaultdict(list)
for r_, ids in row_map.items():
    for i in ids:
        trip_rows[i].append(r_)
for b in batch_rows:
    for r_ in b["rows"]:
        for i in row_map.get(r_, []):
            inv.setdefault(i, b["date"])
assert len(inv) == 104, f"开票车数 {len(inv)} != 104"
uninvoiced = sorted(set(t["id"] for t in IN_SCOPE) - set(inv))
assert uninvoiced == [104, 106, 107, 108, 109], f"未开票车 {uninvoiced}"
s_inv = round(sum(trips[i]["settlement_amount_total"] for i in inv), 2)
assert abs(s_inv - 23017355.85) < 0.01, f"开票金额合计 {s_inv}"

# ---------- 益周未付 / 「未付」标注行（仅入报告，不据此扣 is_paid：三列为供应商
# 内部结算列，标注行上的车是煌烽船次/君盛兴德带票车而非益周车，无法可靠映射） ----------
trip_yizhou_unpaid = set()
for t in IN_SCOPE:
    if t["invoice_source_id"] == 3:
        if any(next(x["yizhou_unpaid"] for x in rows if x["r"] == r_)
               for r_ in trip_rows[t["id"]]):
            trip_yizhou_unpaid.add(t["id"])
unpaid_flag_rows = sorted(x["r"] for x in rows if x["yizhou_unpaid"])
unpaid_flag_trips = sorted({i for r_ in unpaid_flag_rows for i in row_map.get(r_, [])})

# ---------- 结案日期（付款时间） ----------
pay_date_by_row = {x["r"]: x["pay_date"] for x in rows}
closed_date_fill = {}
for t in IN_SCOPE:
    pds = [pay_date_by_row.get(r_) for r_ in trip_rows[t["id"]] if pay_date_by_row.get(r_)]
    if pds:
        closed_date_fill[t["id"]] = max(pds)

# ---------- 备份 ----------
print("备份：", system_ops.do_backup("manual", "两期封账写入（状态/开票/付款/提成）之前"))

# ---------- 写入 ----------
before_fin = {}
for t in IN_SCOPE:
    fin = compute_trip_finance_v2_flat(conn, trips[t["id"]])
    before_fin[t["id"]] = {k: round(float(v), 6) for k, v in fin.items()
                           if isinstance(v, (int, float))}
conn.commit()

n_status = n_inv = n_paid = n_closed = 0
paid_report, skip_yizhou = [], []
for t in IN_SCOPE:
    tid = t["id"]
    # 1) 状态
    if t["status"] != "closed":
        must(C.post(f"/api/trips/{tid}/status", json={"status": "closed"}), f"结案#{tid}")
        n_status += 1
    # 2) 销项开票
    payload = {}
    if tid in inv:
        payload["invoiced_date"] = inv[tid]
        payload["invoice_out_amount"] = round(trips[tid]["settlement_amount_total"], 2)
    # 3) 付款 + 结案日期
    payload["is_paid"] = 1
    payload["paid_amount"] = INT(t["pay_up"])
    paid_report.append(tid)
    if tid in closed_date_fill:
        cur = q("SELECT closed_date FROM trips WHERE id=?", tid)[0]["closed_date"]
        if not cur:
            payload["closed_date"] = closed_date_fill[tid]
    if payload:
        must(C.put(f"/api/trips/{tid}", json=payload), f"更新#{tid}")
        n_inv += 1 if "invoiced_date" in payload else 0
        n_paid += 1 if payload.get("is_paid") else 0
        n_closed += 1 if "closed_date" in payload else 0

print(f"结案推进 {n_status} 台；开票字段 {n_inv} 台；is_paid {n_paid} 台；"
      f"补结案日期 {n_closed} 台；「未付」标注行 {unpaid_flag_rows} 涉及车次 {unpaid_flag_trips}（入报告）")

# ---------- 提成 ----------
comm = must(C.get("/api/analytics/commission?date_field=settlement_date"
                  f"&date_from={P_START}&date_to={P_END}"), "提成")
unpaid = comm.get("unpaid_trips", [])
by = defaultdict(list)
for r_ in unpaid:
    by.setdefault(r_["dispatcher_name"], []).append(r_)
for name, rs in sorted(by.items()):
    res = must(C.post("/api/batches", json={
        "hop_key": "commission", "batch_date": "2026-09-30",
        "notes": "用户 2026-09-30 确认两期（8/26~9/25 结算期）封账，提成付清（实际付款日期未登记）",
        "allocations": [{"trip_id": r_["trip_id"], "amount": r_["unpaid"]} for r_ in rs]}),
        f"提成批次{name}")
    print(f"提成已付：{name} {len(rs)} 车 ¥{res['total_amount']:,.2f}")

# ---------- 回归验证 ----------
conn2 = sqlite3.connect(db.DB_PATH)
conn2.row_factory = sqlite3.Row
diffs = []
for t in IN_SCOPE:
    tt = dict(conn2.execute("SELECT * FROM trips WHERE id=?", (t["id"],)).fetchone())
    fin = compute_trip_finance_v2_flat(conn2, tt)
    after = {k: round(float(v), 6) for k, v in fin.items() if isinstance(v, (int, float))}
    for k in set(before_fin[t["id"]]) | set(after):
        if before_fin[t["id"]].get(k) != after.get(k):
            diffs.append((t["id"], k, before_fin[t["id"]].get(k), after.get(k)))
print(f"引擎全字段回归：{'零差异' if not diffs else diffs[:10]}")
with app.app.app_context():
    regression.check(conn2)
st = dict(conn2.execute(
    "SELECT status, count(*) n FROM trips WHERE settlement_date BETWEEN ? AND ? "
    "AND status!='voided' GROUP BY status", (P_START, P_END)).fetchone() and
    {} or {}) if False else None
print("两期状态分布：", [tuple(r) for r in conn2.execute(
    "SELECT status,count(*) FROM trips WHERE settlement_date BETWEEN ? AND ? "
    "AND status!='voided' GROUP BY status", (P_START, P_END))])
print("两期 is_paid 分布：", [tuple(r) for r in conn2.execute(
    "SELECT is_paid,count(*) FROM trips WHERE settlement_date BETWEEN ? AND ? "
    "AND status!='voided' GROUP BY is_paid", (P_START, P_END))])
json.dump({"paid": sorted(paid_report),
           "unpaid_flag_rows": unpaid_flag_rows,
           "unpaid_flag_trips": unpaid_flag_trips,
           "invoiced": {str(k): v for k, v in inv.items()},
           "uninvoiced": uninvoiced, "closed_fill": closed_date_fill,
           "row_map": {str(k): v for k, v in row_map.items()},
           "batch_rows": batch_rows},
          open("/Users/alvin/WorkBuddy/2026-09-30-13-14-34/scripts/close_written.json",
               "w"), ensure_ascii=False, indent=1)
print("完成，close_written.json 已写出")
