"""手机录入（v2.1）：同一个 WiFi 下用手机报车、推进状态、查车。

访问规则（before_request 统一把关）：
  * 电脑本机(127.0.0.1)访问：和以前完全一样，不要口令。
  * 局域网里的其它设备(手机)：必须先在电脑「系统 → 手机访问」设好口令；手机第一次打开输入口令，
    之后记住 180 天。改口令后所有手机要重新输入。
  * 手机上可以看所有页面和数据（只读），能改的只有：新建车次、改车次信息、推进状态(含快速结算)。
    删除、恢复备份、重启、改档案/费率/链条规则/系统设置等一律只能在电脑上做。
  * 手机上的每次修改在操作记录里备注"手机"。
"""
import hashlib
import hmac
import os
import re
import secrets
import socket

from flask import g, redirect, render_template, request

from core import app, get_db, jsonify, _get_setting
import qr

COOKIE = "sm_mobile"
COOKIE_DAYS = 180
_FAILS = {}  # ip -> [连续输错次数, 锁到什么时候]；输错 5 次锁 5 分钟，防止被人在局域网里挨个试口令
LOCAL_ADDRS = {"127.0.0.1", "::1", "localhost"}
LAN_PORT = None        # 手机用的局域网端口，启动成功后才有值
LAN_ERROR = None       # 局域网端口没开起来的原因


def start_lan_server(flask_app, port):
    """在后台线程里另起一个监听所有网卡的服务，给手机用。起不来(端口被占)只提示，不影响电脑端。"""
    global LAN_PORT, LAN_ERROR
    import threading
    from werkzeug.serving import make_server
    try:  # werkzeug 端口被占时不抛异常而是直接 sys.exit，所以两种都要接住
        srv = make_server("0.0.0.0", port, flask_app, threaded=True)
    except (OSError, SystemExit) as e:
        LAN_ERROR = f"手机端口 {port} 被别的程序占用，手机暂时连不上（电脑端不受影响）"
        print("    [手机访问] " + LAN_ERROR, e if isinstance(e, OSError) else "", flush=True)
        return None
    threading.Thread(target=srv.serve_forever, daemon=True, name="lan-server").start()
    LAN_PORT = port
    urls = lan_addresses(port)
    if urls:
        print(f"    手机(连同一个 WiFi)打开 {urls[0]}  （口令在「系统 → 手机访问」里设置）", flush=True)
    return srv

# 手机上允许的修改操作（方法, 路径正则）
MOBILE_WRITE_ALLOWED = [
    ("POST", r"/api/trips"),
    ("PUT", r"/api/trips/\d+"),
    ("POST", r"/api/trips/\d+/status"),
    ("POST", r"/api/trips/batch_status"),
    ("POST", r"/api/trips/\d+/attachments/upload"),  # v2.3 ①：现场拍过磅单/结算单直传
    ("POST", r"/m/login"),
    ("POST", r"/m/logout"),
]
# 手机上即使是查看也不开放的地址
# 手机上即使是查看也不开放的地址（含系统参数：改阈值是电脑上的管理动作，不放到手机）
MOBILE_READ_BLOCKED = [r"/api/mobile/access", r"/api/system/backups.*", r"/api/app_settings",
                       r"/api/system/settings"]


def is_local():
    return (request.remote_addr or "") in LOCAL_ADDRS


def _settings(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    return _get_setting(conn, "mobile_pin_hash"), _get_setting(conn, "mobile_pin_salt")


def _token(pin_hash, salt):
    return hmac.new((salt or "").encode(), ("ok:" + (pin_hash or "")).encode(), hashlib.sha256).hexdigest()


def _hash_pin(pin, salt):
    return hashlib.sha256((salt + ":" + pin).encode()).hexdigest()


def lan_addresses(port):
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # 不会真的发包，只是让系统选出局域网网卡
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    urls = [f"http://{ip}:{port}/m" for ip in ips if not ip.startswith("127.")]
    host = socket.gethostname()
    if host and re.fullmatch(r"[A-Za-z0-9-]+(\.local)?", host):
        urls.append(f"http://{host if host.endswith('.local') else host + '.local'}:{port}/m")
    return urls


@app.before_request
def _mobile_gate():
    if is_local():
        return None
    g.via_mobile = True
    path = request.path
    if path.startswith("/static/") or path in ("/favicon.ico", "/m/login", "/m/logout"):
        return None
    conn = get_db()
    pin_hash, salt = _settings(conn)
    is_api = path.startswith("/api/")
    if not pin_hash:
        if is_api:
            return jsonify({"error": "还没有开启手机访问：请先在电脑上打开「系统 → 手机访问」设置口令"}), 401
        return render_template("mobile_login.html", no_pin=True), 401
    if not hmac.compare_digest(request.cookies.get(COOKIE, ""), _token(pin_hash, salt)):
        if is_api:
            return jsonify({"error": "请先输入手机访问口令", "need_login": True}), 401
        return redirect("/m/login?next=" + (request.full_path.rstrip("?") if path != "/" else "/m"))
    if request.method in ("GET", "HEAD"):
        if any(re.fullmatch(p, path) for p in MOBILE_READ_BLOCKED):
            return jsonify({"error": "这个功能只能在电脑上用"}), 403
        # 手机直接打开首页(扫码/书签) → 手机录入页；从系统里面点过来的(导航栏"首页看板"、"电脑版")照常显示电脑版首页
        ref = request.headers.get("Referer", "")
        from_inside = ref.split("/")[2:3] == [request.host] if ref.startswith("http") else False
        if path == "/" and not request.args.get("full") and not from_inside:
            return redirect("/m")
        return None
    if any(request.method == m and re.fullmatch(p, path) for m, p in MOBILE_WRITE_ALLOWED):
        return None
    return jsonify({"error": "手机上不能做这个操作，请在电脑上操作"}), 403


@app.route("/m")
def page_mobile():
    return render_template("mobile.html")


@app.route("/m/login", methods=["GET", "POST"])
def mobile_login():
    conn = get_db()
    pin_hash, salt = _settings(conn)
    nxt = request.values.get("next") or "/m"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/m"
    if not pin_hash:
        return render_template("mobile_login.html", no_pin=True)
    if request.method == "POST":
        import time
        ip = request.remote_addr or ""
        n, until = _FAILS.get(ip, (0, 0))
        if until > time.time():
            return render_template("mobile_login.html", error=f"输错太多次，请 {int((until - time.time()) // 60) + 1} 分钟后再试", next=nxt), 429
        pin = (request.form.get("pin") or "").strip()
        if hmac.compare_digest(_hash_pin(pin, salt), pin_hash):
            _FAILS.pop(ip, None)
            resp = redirect(nxt)
            import settings as _settings_module   # 有效期可在「系统 → 参数设置」改（mobile_cookie_days）
            days = int(_settings_module.get(conn, "mobile_cookie_days", COOKIE_DAYS))
            resp.set_cookie(COOKIE, _token(pin_hash, salt), max_age=days * 86400, httponly=True, samesite="Lax")
            return resp
        n += 1
        _FAILS[ip] = (0, time.time() + 300) if n >= 5 else (n, 0)
        return render_template("mobile_login.html", error="口令不对，再试一次" if n < 5 else "输错 5 次，锁 5 分钟", next=nxt), 401
    return render_template("mobile_login.html", next=nxt)


@app.route("/m/logout", methods=["POST", "GET"])
def mobile_logout():
    resp = redirect("/m/login")
    resp.delete_cookie(COOKIE)
    return resp


@app.route("/api/mobile/access", methods=["GET", "POST"])
def api_mobile_access():
    """电脑本机专用：查看/设置手机访问口令，拿手机要打开的地址和二维码。"""
    if not is_local():
        return jsonify({"error": "只能在电脑上设置"}), 403
    conn = get_db()
    pin_hash, salt = _settings(conn)
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        from system_ops import audit
        if data.get("disable"):
            conn.execute("DELETE FROM app_settings WHERE key IN ('mobile_pin_hash','mobile_pin_salt')")
            audit(conn, "app_settings", None, "手机访问", "开启", "关闭")
        else:
            pin = str(data.get("pin") or "").strip()
            if not re.fullmatch(r"\d{4,8}", pin):
                return jsonify({"error": "口令请设 4~8 位数字"}), 400
            salt = secrets.token_hex(8)
            for k, v in (("mobile_pin_salt", salt), ("mobile_pin_hash", _hash_pin(pin, salt))):
                conn.execute("INSERT INTO app_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
            audit(conn, "app_settings", None, "手机访问口令", "(已设置)" if pin_hash else None, "(新口令)")
        conn.commit()
        pin_hash, salt = _settings(conn)
    listening_lan = LAN_PORT is not None
    urls = lan_addresses(LAN_PORT) if listening_lan else []
    return jsonify({"enabled": bool(pin_hash), "urls": urls, "listening_lan": listening_lan, "lan_error": LAN_ERROR,
                    "qr": qr.svg(urls[0], scale=5) if urls else None})
