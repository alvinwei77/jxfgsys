"""利润自动核对（v2.0.1）。

目的：程序升级、改档案/费率之后，自动发现「车次本身没改，利润却变了」的车。

做法：profit_baseline 表给每车记一份"上次核对时"的指纹和利润——
  指纹 = 车次这一行的全部字段(不含 updated_at) + 是否固化/固化时间 + 本期累计吨位。
  * 指纹变了：说明车次本身被改过(磅重、金额、本车费率……)或者同期新进了车导致量补换档，
    利润跟着变是正常的，静默更新基准。
  * 指纹没变、利润变了：列出来，让人看一眼。原因通常是改了「费率与关系」「基础档案」
    「链条规则」，或者程序升级改了算法。确认没问题就点「以当前为准」。
  * 在链条规则页确认修改口径时(已经看过影响预览)，自动把该规则下的车次以当前为准。

入口：系统 → 利润核对；首页预警；命令行 python3 regression_check.py。
"""
import hashlib
import json

from core import app, get_db, jsonify, request, _trip_finance_full

TOL = 0.005


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS profit_baseline (
        trip_id INTEGER PRIMARY KEY,
        sig TEXT NOT NULL,
        net_profit REAL,
        reserved_profit REAL,
        checked_at TEXT DEFAULT (datetime('now','localtime')))""")


def _current(conn):
    """{trip_id: (sig, net_profit, reserved_profit, trip_row)}，不含作废车次。"""
    locks = {r["trip_id"]: r for r in conn.execute("SELECT trip_id, is_locked, locked_at FROM trip_finance")}
    out = {}
    for t in conn.execute("SELECT * FROM trips WHERE status != 'voided' ORDER BY id"):
        fin = _trip_finance_full(conn, t)
        d = {k: t[k] for k in t.keys() if k not in ("updated_at",)}
        lk = locks.get(t["id"])
        d["_locked"] = [lk["is_locked"], lk["locked_at"]] if lk else None
        d["_period_tonnage"] = round(float(fin.get("period_cumulative_tonnage") or 0), 3)
        sig = hashlib.md5(json.dumps(d, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        out[t["id"]] = (sig, round(float(fin.get("net_profit") or 0), 2),
                        round(float(fin.get("invoice_diff_profit") or 0), 2), t)
    return out


def check(conn):
    """核对一遍：返回不一致的车次列表；顺带补齐新车次的基准、更新指纹变了的车次的基准。"""
    ensure_table(conn)
    cur = _current(conn)
    base = {r["trip_id"]: r for r in conn.execute("SELECT * FROM profit_baseline")}
    mismatches, upserts = [], []
    for tid, (sig, net, res, t) in cur.items():
        b = base.get(tid)
        if b is None or b["sig"] != sig:
            upserts.append((tid, sig, net, res))
            continue
        if abs((b["net_profit"] or 0) - net) > TOL or abs((b["reserved_profit"] or 0) - res) > TOL:
            mismatches.append({
                "trip_id": tid, "plate_no": t["plate_no"], "settlement_date": t["settlement_date"] or t["order_date"],
                "before": b["net_profit"], "after": net, "diff": round(net - (b["net_profit"] or 0), 2),
                "reserved_before": b["reserved_profit"], "reserved_after": res, "checked_at": b["checked_at"]})
    conn.executemany(
        "INSERT INTO profit_baseline (trip_id, sig, net_profit, reserved_profit, checked_at) VALUES (?,?,?,?,datetime('now','localtime')) "
        "ON CONFLICT(trip_id) DO UPDATE SET sig=excluded.sig, net_profit=excluded.net_profit, "
        "reserved_profit=excluded.reserved_profit, checked_at=excluded.checked_at", upserts)
    gone = [tid for tid in base if tid not in cur]
    conn.executemany("DELETE FROM profit_baseline WHERE trip_id=?", [(x,) for x in gone])
    conn.commit()
    mismatches.sort(key=lambda m: -abs(m["diff"]))
    return mismatches


def accept(conn, trip_ids=None, template_id=None):
    """以当前为准：把指定车次(不传=全部)的基准更新成现在的指纹和利润。"""
    ensure_table(conn)
    cur = _current(conn)
    if template_id is not None:
        trip_ids = [tid for tid, v in cur.items() if v[3]["template_id"] == template_id]
    ids = set(cur) if trip_ids is None else {int(x) for x in trip_ids} & set(cur)
    conn.executemany(
        "INSERT INTO profit_baseline (trip_id, sig, net_profit, reserved_profit, checked_at) VALUES (?,?,?,?,datetime('now','localtime')) "
        "ON CONFLICT(trip_id) DO UPDATE SET sig=excluded.sig, net_profit=excluded.net_profit, "
        "reserved_profit=excluded.reserved_profit, checked_at=excluded.checked_at",
        [(tid, cur[tid][0], cur[tid][1], cur[tid][2]) for tid in ids])
    conn.commit()
    return len(ids)


def recent_setting_changes(conn, since):
    """基准之后，档案/费率/规则方面改过什么（帮助判断利润为什么变）。"""
    if not since:
        return []
    try:
        rows = conn.execute(
            """SELECT table_name, COUNT(*) n, MAX(changed_at) last FROM audit_log
               WHERE changed_at >= ? AND table_name NOT IN ('trips','batches','app_settings')
               GROUP BY table_name ORDER BY last DESC""", (since,)).fetchall()
    except Exception:
        return []
    from system_ops import TABLE_CN
    return [{"table": r["table_name"], "table_cn": TABLE_CN.get(r["table_name"], r["table_name"]),
             "count": r["n"], "last": r["last"]} for r in rows]


def startup_check(db_path):
    """启动时跑一次，结果打印在启动窗口里。"""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        with app.app_context():
            m = check(conn)
        conn.close()
        if m:
            print(f"[利润核对] 有 {len(m)} 车利润和上次核对时不一样，请到「系统 → 利润核对」看一下")
        else:
            print("[利润核对] 全部车次利润与上次核对一致")
    except Exception as e:
        print("[利润核对] 跳过：", e)


@app.route("/api/system/verify", methods=["GET"])
def api_verify():
    conn = get_db()
    rows = check(conn)
    since = min((r["checked_at"] for r in rows), default=None)
    total = conn.execute("SELECT COUNT(*) FROM profit_baseline").fetchone()[0]
    return jsonify({"rows": rows, "total": total, "tolerance": TOL,
                    "recent_changes": recent_setting_changes(conn, since)})


@app.route("/api/system/verify/accept", methods=["POST"])
def api_verify_accept():
    data = request.get_json(silent=True) or {}
    conn = get_db()
    ids = data.get("ids")
    n = accept(conn, None if data.get("all") else (ids or []))
    try:
        from system_ops import audit
        audit(conn, "profit_baseline", None, "(以当前为准)", None,
              "全部" if data.get("all") else ",".join(map(str, ids or [])), f"利润核对·确认 {n} 车")
        conn.commit()
    except Exception:
        pass
    return jsonify({"ok": True, "accepted": n})
