"""经营报告（2026-09-22）：一键导出 PDF。

  GET /report                   报告页面（手机宽度排版，静态图表，可直接打印）
  GET /report?autoprint=1       打开后自动弹出打印框（没有 Chrome 时的后备方案：选“存储为 PDF”）
  GET /api/report/pdf           用电脑上的 Chrome / Edge 无界面模式把 /report 打成 PDF，直接下载；
                                同时在 data/reports/ 留一份。找不到浏览器时返回 fallback 地址。
报告数据全部来自系统当前数据；“和财务台账的核对”一段取 app_settings.last_recon（对账后更新）。
"""
import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import tempfile

from flask import request, send_file

from core import app, get_db, jsonify, _get_setting, _trip_finance_full, _compute_finance
import db as db_module

HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, "templates", "report_print.html")
PATH_OF = {"配票": "配票", "反向": "反向", "直签带票": "带票", "户头通道": "带票", "调货对接": "带票"}
BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def find_browser():
    env = os.environ.get("REPORT_BROWSER")
    if env and os.path.exists(env):
        return env
    for p in BROWSERS:
        if os.path.exists(p):
            return p
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    for p in sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")):
        return p
    return None


def build_data(conn):
    alias = {r[0] for r in conn.execute("SELECT name FROM processing_bases WHERE is_dispatcher_alias=1")}
    trips = []
    for t in conn.execute("""SELECT t.*, b.name bname, d.name dname, s.name sname, ft.short_name tpl, ft.calc_family fam FROM trips t
            LEFT JOIN processing_bases b ON t.base_id=b.id LEFT JOIN dispatchers d ON t.dispatcher_id=d.id
            LEFT JOIN invoice_sources s ON t.invoice_source_id=s.id LEFT JOIN flow_templates ft ON t.template_id=ft.id
            WHERE t.status!='voided' AND t.settlement_tonnage_total>0 AND t.settlement_date IS NOT NULL""").fetchall():
        f = _trip_finance_full(conn, t)
        live = _compute_finance(conn, t)
        b = t["bname"]
        split = t["fam"] == "split"
        trips.append(dict(
            id=t["id"], p=t["plate_no"] or "（车号待补）", ty="船" if t["transport_type"] == "ship" else "车", d=t["settlement_date"],
            ton=round(t["settlement_tonnage_total"], 3), amt=round(t["settlement_amount_total"] or 0, 2),
            path=PATH_OF.get(t["tpl"], t["tpl"] or "未设置"), src=t["sname"] or "自带票", disp=t["dname"] or "无调货人",
            base=(f"{b}（自有货源）" if b in alias else (b or "（未填）")), net=round(f.get("net_profit") or 0, 2),
            rw=round(f.get("reward_profit") or 0, 2), cm=round(f.get("dispatcher_commission_cost") or 0, 2),
            padv=round(live.get("personal_advance") or 0, 2) if split else None,
            svc=None if split else round(f.get("service_fee_income") or 0, 2),
            sadv=None if split else round(f.get("advance_fee_cost") or 0, 2),
            resv=round(f.get("invoice_diff_profit") or 0, 2), rwu=round(f.get("reward_unit_price") or 0, 2)))
    cl = app.test_client()
    g = lambda u: cl.get(u).get_json()
    m = (g("/api/dashboard/mill_periods") or [{}])[0]
    try:
        fin = json.loads(_get_setting(conn, "last_recon") or "null")
    except ValueError:
        fin = None
    today = dt.date.today().isoformat()
    return dict(
        trips=trips, asof=today, generated=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        quota=g("/api/dashboard/quota_levels") or [], reserved=(g("/api/dashboard/reserved_profit") or {}).get("total_reserved_profit", 0),
        commission=[dict(name=d["name"], payable=d["payable"], paid=d["paid"], unpaid=d["unpaid"])
                    for d in (g("/api/analytics/commission") or {}).get("by_dispatcher", [])],
        period=dict(start=m.get("period_start"), end=m.get("period_end"), ton=m.get("cumulative_tonnage") or 0, trips=m.get("trip_count") or 0,
                    profit=m.get("profit") or 0, ppt=m.get("profit_per_ton") or 0, rwp=m.get("current_reward_price") or 0,
                    prev_start=m.get("prev_period_start"), prev_end=m.get("prev_period_end"), prev_ton=m.get("prev_tonnage") or 0,
                    prev_trips=m.get("prev_trip_count") or 0, prev_profit=m.get("prev_profit") or 0, prev_ppt=m.get("prev_profit_per_ton") or 0),
        tiers=[dict(min=x["min_tonnage"], price=x["unit_price"]) for x in (g("/api/mill_reward_tiers") or [])],
        finance=fin)


def render_html(conn):
    tpl = open(TPL, encoding="utf-8").read()
    data = json.dumps(build_data(conn), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>'
            + tpl.replace("__DATA__", data) + "</body></html>")


def _pdf_done(path):
    """PDF 已完整写出：文件以 %%EOF 结尾。"""
    try:
        if os.path.getsize(path) < 1000:
            return False
        with open(path, "rb") as f:
            f.seek(-64, os.SEEK_END)
            return b"%%EOF" in f.read()
    except OSError:
        return False


def _run_print(cmd, out, timeout=60):
    """启动无界面浏览器打印 PDF。
    2026-09-22 修：Mac 上的 Chrome 打完 PDF 后，残留的辅助进程(crashpad 等)一直占着输出管道/不退出，
    以前用 subprocess.run(capture_output) 会一直等到 90 秒超时——其实 PDF 早就生成好了。
    现在不接管道(输出写临时文件)，轮询 PDF 是否完整写出，写完就把整组浏览器进程关掉。"""
    import signal
    import time
    log = tempfile.TemporaryFile()
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, **kw)
    t0, done_at, last = time.time(), None, -1
    try:
        while time.time() - t0 < timeout:
            if _pdf_done(out):
                size = os.path.getsize(out)
                if size == last and done_at and time.time() - done_at > 0.5:
                    return True, ""
                if size != last:
                    last, done_at = size, time.time()
            elif proc.poll() is not None:
                time.sleep(0.3)
                if _pdf_done(out):
                    continue
                log.seek(0)
                return False, log.read().decode("utf-8", "ignore")[-300:]
            time.sleep(0.25)
        return (True, "") if _pdf_done(out) else (False, "timeout")
    finally:
        if proc.poll() is None:
            try:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL) if os.name != "nt" else proc.kill()
                except Exception:
                    pass
        log.close()


@app.route("/report")
def page_report():
    return render_html(get_db())


@app.route("/api/report/pdf")
def api_report_pdf():
    browser = find_browser()
    if not browser:
        return jsonify({"error": "这台电脑上没找到 Chrome / Edge，改用打印方式：在打开的页面里选“存储为 PDF”",
                        "fallback": "/report?autoprint=1"}), 404
    port = request.host.rsplit(":", 1)[-1] if ":" in request.host else "5000"
    out_dir = os.path.join(os.path.dirname(db_module.DB_PATH), "reports")
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(out_dir, f"华西废钢经营报告_{stamp}.pdf")
    prof = tempfile.mkdtemp(prefix="sm_report_")
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
           "--no-pdf-header-footer", "--print-to-pdf-no-header", f"--print-to-pdf={out}", "--virtual-time-budget=8000",
           "--window-size=454,945", f"--user-data-dir={prof}",
           "--disable-background-networking", "--disable-component-update", "--disable-sync", "--no-service-autorun",
           "--disable-extensions", "--password-store=basic", "--use-mock-keychain", f"http://127.0.0.1:{port}/report"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.insert(1, "--no-sandbox")  # 只有以 root 运行（服务器/测试环境）才需要
    try:
        ok, detail = _run_print(cmd, out, timeout=60)
    finally:
        shutil.rmtree(prof, ignore_errors=True)
    if not ok:
        return jsonify({"error": "PDF 生成失败，改用打印方式" if detail != "timeout" else "生成超时，改用打印方式",
                        "detail": detail, "fallback": "/report?autoprint=1"}), 504 if detail == "timeout" else 500
    return send_file(out, mimetype="application/pdf", as_attachment=True,
                     download_name=f"华西废钢经营报告_{dt.date.today().strftime('%Y%m%d')}.pdf")
