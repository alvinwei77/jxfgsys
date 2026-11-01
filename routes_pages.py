"""页面路由（各个菜单页面）。

从原 app.py 拆出（v2.0.1）。共用的常量和工具函数都在 core.py。"""
from core import *  # noqa: F401,F403


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------
@app.route("/")
def page_dashboard():
    return render_template("dashboard.html", active="dashboard")


@app.route("/rules")
def page_rules_hub():
    return render_template("rules_hub.html", active="rules", rules_sub="hub")


@app.route("/setup")
def page_setup_hub():
    return render_template("setup_hub.html", active="setup", setup_sub="hub")


@app.route("/trips")
def page_trips():
    return render_template("trips.html", status_list=STATUS_LIST, active="trips")


@app.route("/archives")
def page_archives():
    return render_template("archives.html", active="setup", setup_sub="archives")


@app.route("/quotes")
def page_quotes():
    return render_template("quotes.html", active="setup", setup_sub="quotes")


@app.route("/reports")
def page_reports():
    return render_template("reports.html", active="reports")


@app.route("/rate_links")
def page_rate_links():
    return render_template("rate_links.html", active="rules", rules_sub="rates")


@app.route("/system")
def page_system():
    return render_template("system.html", active="setup", setup_sub="system")


@app.route("/tracking")
def page_tracking():
    return render_template("tracking.html", active="tracking")


@app.route("/invoice_in_gap")
def page_invoice_in_gap():
    return render_template("invoice_in_gap.html", active="tracking")


@app.route("/chain_rules")
def page_chain_rules():
    return render_template("chain_rules.html", active="rules", rules_sub="chains")


@app.route("/glossary")
def page_glossary():
    """术语与操作对照表（C档 2026-10-09）：黑话 ↔ 系统叫法 ↔ 在哪，附常见操作怎么做。"""
    return render_template("glossary.html", active="setup", setup_sub="glossary")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------------------------------------------------------------------------
# 出错页面（v2.0.1）：以前点到不存在的地址只显示一行英文 "Not Found"
# ---------------------------------------------------------------------------
def _wants_json():
    return request.path.startswith("/api/")


@app.errorhandler(404)
def _page_not_found(e):
    if _wants_json():
        return jsonify({"error": f"接口不存在：{request.path}（如果刚更新过系统，请先重启）"}), 404
    return render_template("error.html", code=404, path=request.full_path.rstrip("?"),
                           code_changed=system_ops.code_mtime() > system_ops.BOOT_TIME + 1), 404


@app.errorhandler(500)
def _server_error(e):
    if _wants_json():
        return jsonify({"error": f"系统出错了：{getattr(e, 'original_exception', e)}"}), 500
    return render_template("error.html", code=500, path=request.full_path.rstrip("?"),
                           detail=str(getattr(e, "original_exception", e)),
                           code_changed=system_ops.code_mtime() > system_ops.BOOT_TIME + 1), 500
