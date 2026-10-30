"""v2 第一批：数据安全（2026-09-22）——自动备份 / 操作留痕 / 回收站。

- 自动备份：启动时备份一次，之后每小时检查一次、当天没有自动备份就补一份；只保留最近 30 份自动备份
  （手动备份不自动删）。用 SQLite 在线备份接口，系统运行中也能拷出一份一致的库。
  启动时把 data/ 里迁移脚本留下的 *.bak_* 旧备份挪进 data/backups/旧迁移备份/（只挪不删）。
- 操作留痕：audit(conn, ...) 写 audit_log；车次/磅重/状态/本车费率/档案/费率/批次/链条口径的改动都记。
- 回收站：删除车次/批次/档案时，把这条记录连同它的附属数据打包存进 recycle_bin，30 天内可一键恢复，
  过期启动时清掉。作废仍是作废（状态），不进回收站。
"""
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import threading
import time

from flask import Blueprint, jsonify, request

import db as db_module
from db import get_db, rows_to_list

bp = Blueprint("system_ops", __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEEP_AUTO = 30        # 回退默认；实际取值见「系统 → 参数设置」（keep_auto_backups）
RECYCLE_DAYS = 30     # 回退默认；实际取值见「系统 → 参数设置」（recycle_days）
VERSION_FILE = os.path.join(BASE_DIR, "VERSION")


def setting(conn, key, default):
    """读一个系统参数（见 settings.py）。传 conn=None 时自己开一个只读用途的连接。
    任何异常都回退默认值——参数读不到不能影响备份/回收站这些基础功能。"""
    try:
        import settings
        if conn is not None:
            return settings.get(conn, key, default)
        c = sqlite3.connect(_db_path())
        try:
            return settings.get(c, key, default)
        finally:
            c.close()
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 版本号 与 一键重启（v2.0.1）
#   程序文件(.py)更新后，正在运行的服务还是旧代码——新加的页面会显示"找不到这个页面"。
#   /api/version 比较"磁盘上的程序文件修改时间"和"本次启动时间"，页面顶部据此提示
#   "系统已更新，点这里重启生效"；/api/system/restart 让服务原地重启(同一个窗口、同一个端口)。
# ---------------------------------------------------------------------------
def read_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip() or "2.0"
    except OSError:
        return "2.0"


BOOT_VERSION = read_version()
BOOT_TIME = time.time()


def code_mtime():
    latest = 0.0
    for fn in os.listdir(BASE_DIR):
        if fn.endswith(".py") or fn == "VERSION":
            try:
                latest = max(latest, os.path.getmtime(os.path.join(BASE_DIR, fn)))
            except OSError:
                pass
    return latest


def _restart_later():
    time.sleep(0.8)  # 先让"正在重启"的响应发回浏览器
    print("[系统] 正在重启以加载新版本……", flush=True)
    sys.stdout.flush()
    # werkzeug 会把监听端口设成可继承并记在环境变量里：原地重启前关掉所有打开的文件/端口，
    # 否则新进程会报"端口被占用"
    os.environ.pop("WERKZEUG_SERVER_FD", None)
    os.environ.pop("WERKZEUG_RUN_MAIN", None)
    os.closerange(3, 4096)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _db_path():
    return db_module.DB_PATH


def backup_dir():
    d = os.path.join(os.path.dirname(_db_path()), "backups")
    os.makedirs(d, exist_ok=True)
    return d


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS recycle_bin (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_name TEXT NOT NULL, record_id INTEGER, title TEXT,
        payload TEXT NOT NULL, deleted_at TEXT DEFAULT (datetime('now','localtime')))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_table_record ON audit_log(table_name, record_id)")
    conn.commit()


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
def do_backup(kind="auto", note=""):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{kind}_{ts}.db"
    dest = os.path.join(backup_dir(), name)
    src = sqlite3.connect(_db_path())
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    if note:
        with open(dest + ".txt", "w", encoding="utf-8") as f:
            f.write(note)
    prune_backups()
    return name


def list_backups():
    bd = backup_dir()
    out = []
    for fn in sorted(os.listdir(bd), reverse=True):
        if not fn.endswith(".db"):
            continue
        p = os.path.join(bd, fn)
        note_p = p + ".txt"
        trips, healthy = _backup_health(p)
        out.append({"name": fn, "kind": fn.split("_")[0], "size": os.path.getsize(p),
                    "time": dt.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S"),
                    "note": open(note_p, encoding="utf-8").read() if os.path.exists(note_p) else "",
                    "pinned": os.path.exists(p + ".pin"),
                    "trips_count": trips, "healthy": healthy})
    return out


def _backup_health(path):
    """只读打开备份 db 查 trips 行数，校验完整性。返回 (trips_count, healthy)。损坏返回 (None, False)。"""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        n = c.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
        c.close()
        return n, True
    except Exception:
        return None, False


def pin_backup(name, pin):
    """标记/取消保留（v2.3 ③）：pin=True 创建 <name>.pin 空文件，False 删除。
    标记保留的备份不被 prune_backups 自动清理，用于月结后手动锁定关键备份。"""
    bd = backup_dir()
    fn = os.path.basename(name)
    if not fn.endswith(".db"):
        return False
    pin_path = os.path.join(bd, fn + ".pin")
    if pin:
        open(pin_path, "w").close()
    elif os.path.exists(pin_path):
        os.remove(pin_path)
    return True


def prune_backups():
    bd = backup_dir()
    keep = int(setting(None, "keep_auto_backups", KEEP_AUTO))
    autos = sorted(f for f in os.listdir(bd) if f.startswith("auto_") and f.endswith(".db"))
    for f in autos[:-keep]:
        if os.path.exists(os.path.join(bd, f + ".pin")):
            continue  # 标记保留的备份不被自动清理（v2.3 ③）
        for p in (f, f + ".txt"):
            fp = os.path.join(bd, p)
            if os.path.exists(fp):
                os.remove(fp)


def tidy_legacy_backups():
    """迁移脚本在 data/ 里留下的 scrapmetal.db.bak_* 挪到 backups/旧迁移备份/，只挪不删。"""
    data_dir = os.path.dirname(_db_path())
    legacy = os.path.join(backup_dir(), "旧迁移备份")
    moved = 0
    for fn in os.listdir(data_dir):
        if ".bak_" in fn or fn in ("_snapshot_for_dev.db", "_devcopy.db", "_devcopy.db-journal"):
            os.makedirs(legacy, exist_ok=True)
            try:
                shutil.move(os.path.join(data_dir, fn), os.path.join(legacy, fn))
                moved += 1
            except OSError:
                pass
    return moved


def last_auto_backup_date():
    autos = sorted(f for f in os.listdir(backup_dir()) if f.startswith("auto_") and f.endswith(".db"))
    return autos[-1][5:13] if autos else None  # YYYYMMDD


def _daily_loop():
    while True:
        try:
            if last_auto_backup_date() != dt.date.today().strftime("%Y%m%d"):
                do_backup("auto", "每日自动备份")
        except Exception as e:  # 备份失败不影响系统使用
            print("[备份] 失败:", e)
        time.sleep(3600)


def start_background():
    """app.py 启动时调用：整理旧备份 → 启动备份 → 每日备份线程 → 清理过期回收站。"""
    try:
        n = tidy_legacy_backups()
        if n:
            print(f"[备份] 已把 {n} 个旧迁移备份挪到 data/backups/旧迁移备份/")
        name = do_backup("auto", "启动时自动备份")
        print(f"[备份] 启动备份完成：data/backups/{name}")
        conn = sqlite3.connect(_db_path())
        ensure_tables(conn)
        conn.execute("DELETE FROM recycle_bin WHERE deleted_at < datetime('now','localtime', ?)",
                     (f"-{int(setting(conn, 'recycle_days', RECYCLE_DAYS))} days",))
        archived, apath = archive_audit_log(conn, 365)
        if archived:
            print(f"[备份] 归档 {archived} 条超期操作记录到 {os.path.basename(apath)}")
        conn.commit()
        conn.close()
    except Exception as e:
        print("[备份] 启动备份失败:", e)
    threading.Thread(target=_daily_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# 操作留痕
# ---------------------------------------------------------------------------
def _s(v):
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def audit(conn, table, record_id, field, old, new, note=""):
    if _s(old) == _s(new):
        return
    try:  # 手机上做的修改，备注里标上"手机"
        from flask import g, has_request_context
        if has_request_context() and g.get("via_mobile"):
            note = f"{note} · 手机" if note else "手机"
    except Exception:
        pass
    try:
        conn.execute("INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, note) VALUES (?,?,?,?,?,?)",
                     (table, record_id, field, _s(old), _s(new), note))
    except sqlite3.OperationalError:
        pass


def audit_row_diff(conn, table, record_id, old_row, new_values, note=""):
    """old_row: 修改前的整行(dict/Row)；new_values: 本次提交的 {字段: 新值}，只记真正变了的。"""
    for k, v in new_values.items():
        o = old_row[k] if old_row is not None and k in old_row.keys() else None
        if str(o if o is not None else "") != str(v if v is not None else ""):
            audit(conn, table, record_id, k, o, v, note)


def archive_audit_log(conn, days=365):
    """把超期 audit_log 导出到 data/audit_archive_<日期>.json 后删除（v2.3 ③）。
    返回 (archived_count, file_path)。无超期记录返回 (0, None)。audit_log 无限增长会拖慢，
    默认 365 天归档一次，启动时自动跑、系统页可手动触发。"""
    import json
    cut = dt.datetime.now() - dt.timedelta(days=days)
    cut_str = cut.strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("SELECT * FROM audit_log WHERE changed_at < ?", (cut_str,)).fetchall()
    if not rows:
        return 0, None
    data = [dict(r) for r in rows]
    data_dir = os.path.dirname(_db_path())
    fname = f"audit_archive_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    fpath = os.path.join(data_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump({"archived_at": cut_str, "days": days, "count": len(data), "rows": data},
                  f, ensure_ascii=False, default=str)
    conn.execute("DELETE FROM audit_log WHERE changed_at < ?", (cut_str,))
    conn.commit()
    return len(data), fpath


# ---------------------------------------------------------------------------
# 回收站
# ---------------------------------------------------------------------------
TRIP_CHILDREN = ["trip_scrap_details", "trip_finance", "trip_node_values", "payments", "trip_attachments", "batch_trips"]


def _rows(conn, sql, args):
    return rows_to_list(conn.execute(sql, args).fetchall())


def recycle(conn, table, record_id, title):
    """把 table 里 id=record_id 的记录(+附属)打包进回收站，然后删除。返回回收站id。"""
    ensure_tables(conn)
    main = conn.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
    if not main:
        return None
    payload = {"table": table, "row": dict(main), "children": {}}
    if table == "trips":
        for ct in TRIP_CHILDREN:
            payload["children"][ct] = _rows(conn, f"SELECT * FROM {ct} WHERE trip_id=?", (record_id,))
        for ct in TRIP_CHILDREN:
            conn.execute(f"DELETE FROM {ct} WHERE trip_id=?", (record_id,))
    elif table == "batches":
        payload["children"]["batch_trips"] = _rows(conn, "SELECT * FROM batch_trips WHERE batch_id=?", (record_id,))
        conn.execute("DELETE FROM batch_trips WHERE batch_id=?", (record_id,))
    conn.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))
    cur = conn.execute("INSERT INTO recycle_bin (table_name, record_id, title, payload) VALUES (?,?,?,?)",
                       (table, record_id, title, json.dumps(payload, ensure_ascii=False)))
    audit(conn, table, record_id, "(删除)", title, None, "进回收站")
    return cur.lastrowid


def _insert(conn, table, row):
    cols = list(row.keys())
    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])


def restore(conn, bin_id):
    it = conn.execute("SELECT * FROM recycle_bin WHERE id=?", (bin_id,)).fetchone()
    if not it:
        raise ValueError("回收站里没有这条")
    p = json.loads(it["payload"])
    t, row = p["table"], p["row"]
    if conn.execute(f"SELECT 1 FROM {t} WHERE id=?", (row["id"],)).fetchone():
        raise ValueError("原编号已被占用，不能恢复")
    _insert(conn, t, row)
    for ct, rows in p["children"].items():
        for r in rows:
            if ct == "batch_trips" and t == "trips" and not conn.execute(
                    "SELECT 1 FROM batches WHERE id=?", (r["batch_id"],)).fetchone():
                continue  # 对应批次已经删了，这条分摊就不恢复
            _insert(conn, ct, r)
    conn.execute("DELETE FROM recycle_bin WHERE id=?", (bin_id,))
    audit(conn, t, row["id"], "(恢复)", None, it["title"], "从回收站恢复")
    return t, row["id"]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
TABLE_CN = {"trips": "车次", "trip_scrap_details": "磅重明细", "template_nodes": "链条口径", "flow_templates": "链条规则",
            "rate_links": "费率与关系", "batches": "批次", "steel_mills": "钢厂", "accounts": "户头", "processing_bases": "供货厂",
            "dispatchers": "调货人", "invoice_sources": "票源方", "invoice_source_terms": "票源额度分期", "vehicles": "车辆", "drivers": "司机", "base_accounts": "收款账户",
            "scrap_types": "货型", "financiers": "垫资方", "mill_reward_tiers": "量补规则", "app_settings": "系统设置",
            "trip_finance": "利润快照", "profit_baseline": "利润核对"}
FIELD_CN = {"status": "状态", "plate_no": "车号", "order_date": "接单日期", "settlement_date": "结算日期", "base_id": "供货厂",
            "dispatcher_id": "调货人", "invoice_source_id": "票源方", "template_id": "链条规则", "mode_code": "模式代号",
            "mill_id": "钢厂", "account_id": "户头", "transport_type": "车/船", "settlement_doc_no": "结算单号",
            "driver_name": "司机", "driver_phone": "司机电话", "departure_place": "发货地", "remarks": "备注",
            "invoice_path": "开票路径", "finance_overrides": "本车费率", "details": "磅重明细", "arrived_date": "到厂日期",
            "invoiced_date": "开票日期", "closed_date": "结案日期", "mixed_material": "混料", "calc_rule": "计算口径",
            "rate_value": "费率", "lock": "固化", "(删除)": "删除", "(恢复)": "恢复", "(新建)": "新建"}


@bp.route("/api/system/audit")
def api_audit():
    conn = get_db()
    where, args = ["1=1"], []
    if request.args.get("table"):
        where.append("table_name=?")
        args.append(request.args["table"])
    if request.args.get("record_id"):
        where.append("record_id=?")
        args.append(request.args["record_id"])
    if request.args.get("q"):
        where.append("(old_value LIKE ? OR new_value LIKE ? OR note LIKE ?)")
        args += [f"%{request.args['q']}%"] * 3
    limit = min(int(request.args.get("limit", 300)), 2000)
    rows = rows_to_list(conn.execute(
        f"SELECT * FROM audit_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT {limit}", args))
    for r in rows:
        r["table_cn"] = TABLE_CN.get(r["table_name"], r["table_name"])
        r["field_cn"] = FIELD_CN.get(r["field_name"], r["field_name"])
    return jsonify(rows)


@bp.route("/api/version")
def api_version():
    changed = code_mtime() > BOOT_TIME + 1
    return jsonify({"version": BOOT_VERSION, "disk_version": read_version(), "code_changed": changed,
                    "started_at": dt.datetime.fromtimestamp(BOOT_TIME).strftime("%Y-%m-%d %H:%M:%S")})


@bp.route("/api/system/restart", methods=["POST"])
def api_restart():
    if request.remote_addr not in ("127.0.0.1", "::1", None):
        return jsonify({"error": "只能在本机重启"}), 403
    threading.Thread(target=_restart_later, daemon=True).start()
    return jsonify({"ok": True})


@bp.route("/api/system/recycle")
def api_recycle_list():
    conn = get_db()
    ensure_tables(conn)
    rows = rows_to_list(conn.execute(
        "SELECT id, table_name, record_id, title, deleted_at FROM recycle_bin ORDER BY id DESC"))
    for r in rows:
        r["table_cn"] = TABLE_CN.get(r["table_name"], r["table_name"])
    return jsonify({"days": int(setting(conn, "recycle_days", RECYCLE_DAYS)), "rows": rows})


@bp.route("/api/system/recycle/<int:bin_id>/restore", methods=["POST"])
def api_recycle_restore(bin_id):
    conn = get_db()
    try:
        t, rid = restore(conn, bin_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    conn.commit()
    return jsonify({"ok": True, "table": t, "id": rid})


@bp.route("/api/system/backups", methods=["GET", "POST"])
def api_backups():
    if request.method == "POST":
        name = do_backup("manual", (request.get_json(silent=True) or {}).get("note") or "手动备份")
        return jsonify({"ok": True, "name": name})
    conn = get_db()
    return jsonify({"dir": backup_dir(), "keep_auto": int(setting(conn, "keep_auto_backups", KEEP_AUTO)),
                    "rows": list_backups()})


@bp.route("/api/system/settings", methods=["GET", "PUT"])
def api_settings():
    """系统参数（C档 2026-10-09）：把散在各模块的写死阈值收拢到一处可改。
    GET 返回清单（每项含类型/默认值/当前值/说明）；PUT 逐项更新，只接受目录里声明过的键，
    其它键一律忽略（防止前端手滑写进无关的 app_settings 键）。"""
    import settings as settings_module
    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    if request.method == "PUT":
        data = request.get_json(silent=True) or {}
        changed = []
        for k, v in data.items():
            meta = settings_module.BY_KEY.get(k)
            if not meta:
                continue
            try:
                old = settings_module.get(conn, k)
                nv = (float(v) if meta["type"] == "float" else int(float(v)))
            except (TypeError, ValueError):
                return jsonify({"error": f"「{meta['label']}」要填数字"}), 400
            lo, hi = meta.get("min"), meta.get("max")
            if lo is not None and nv < lo or hi is not None and nv > hi:
                return jsonify({"error": f"「{meta['label']}」要在 {lo} ~ {hi} 之间"}), 400
            conn.execute("INSERT INTO app_settings(key,value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(nv)))
            if str(old) != str(nv):
                audit(conn, "app_settings", None, meta["label"], old, nv, "系统参数")
                changed.append(k)
        conn.commit()
        return jsonify({"ok": True, "changed": changed, **settings_module.payload(conn)})
    return jsonify(settings_module.payload(conn))


@bp.route("/api/system/backups/<name>/restore", methods=["POST"])
def api_backup_restore(name):
    """用某份备份覆盖当前数据：先把当前数据另存一份"恢复前"备份，再在线恢复，出错不会半截。"""
    fn = os.path.basename(name)
    src_path = os.path.join(backup_dir(), fn)
    if not fn.endswith(".db") or not os.path.exists(src_path):
        return jsonify({"error": "备份文件不存在"}), 404
    before = do_backup("manual", f"恢复 {fn} 之前自动保存的当前数据")
    conn = get_db()
    conn.commit()
    src = sqlite3.connect(src_path)
    try:
        src.backup(conn)
    finally:
        src.close()
    audit(conn, "app_settings", None, "(恢复备份)", before, fn, "用备份覆盖当前数据")
    conn.commit()
    return jsonify({"ok": True, "saved_before": before})


@bp.route("/api/system/backups/<name>/pin", methods=["POST"])
def api_backup_pin(name):
    """标记/取消保留（v2.3 ③）：标记的备份不被自动清理，用于锁定月结等关键备份。"""
    fn = os.path.basename(name)
    if not fn.endswith(".db") or not os.path.exists(os.path.join(backup_dir(), fn)):
        return jsonify({"error": "备份文件不存在"}), 404
    data = request.get_json(silent=True) or {}
    pin = bool(data.get("pin", True))
    pin_backup(fn, pin)
    conn = get_db()
    audit(conn, "app_settings", None, "(备份保留)", fn, "保留" if pin else "取消", "标记备份保留")
    conn.commit()
    return jsonify({"ok": True, "pinned": pin})


@bp.route("/api/system/audit/archive", methods=["POST"])
def api_audit_archive():
    """归档超期操作记录（v2.3 ③）：导出到 data/audit_archive_*.json 后删除。默认 365 天。"""
    data = request.get_json(silent=True) or {}
    days = int(data.get("days", 365))
    if days < 30:
        return jsonify({"error": "归档天数不能少于 30 天"}), 400
    conn = get_db()
    n, fpath = archive_audit_log(conn, days)
    audit(conn, "audit_log", None, "(归档)", f"{days}天前", None,
          f"归档 {n} 条" + (f"到 {os.path.basename(fpath)}" if fpath else ""))
    conn.commit()
    return jsonify({"ok": True, "archived": n, "file": os.path.basename(fpath) if fpath else None})
