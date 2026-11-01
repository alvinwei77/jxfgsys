"""废钢供应链系统 · 启动入口。

v2.0.1 起主程序拆成几个文件（原来一个 app.py 近三千行）：
  core.py            Flask 实例、常量、所有共用工具函数（算利润、统计分桶、匹配车次……）
  routes_pages.py    各个菜单页面 + 404/出错页面
  routes_archives.py 基础档案、费率与关系
  routes_rules.py    链条规则（模板、计算节点、口径编辑、链条图）
  routes_trips.py    车次（列表、录入、修改、利润试算/固化、删除）
  routes_quotes.py   报价单
  routes_reports.py  首页看板、统计报表、分析、预警、导入结算单、导出台账
  lanes.py           三条线 / 批次 / 回款追踪
  system_ops.py      备份、操作记录、回收站、版本与重启
  regression.py      利润自动核对（输入没变、利润却变了的车次）
  mobile.py          手机录入页(/m) + 局域网访问口令把关；qr.py 生成手机地址二维码
  report_export.py   经营报告页面(/report) 与一键导出 PDF(/api/report/pdf)
  engine.py          计算引擎（链条规则 → 利润）；calc.py 只剩引擎共用的几个小工具

启动：python3 app.py（或双击「启动系统.command」）。旧脚本里的 `import app; app.app` 照常可用。
"""
import os
import sys

from core import *  # noqa: F401,F403  —— 保持 `import app as A; A._compute_finance` 这类旧用法可用
from core import app, system_ops, APP_VERSION
import routes_pages  # noqa: F401  注册路由
import routes_archives  # noqa: F401
import routes_rules  # noqa: F401
import routes_trips  # noqa: F401
import routes_quotes  # noqa: F401
import routes_reports  # noqa: F401
import regression  # noqa: F401
import mobile  # noqa: F401  手机录入 + 局域网访问把关
import report_export  # noqa: F401  经营报告一键导出 PDF
import routes_meta  # noqa: F401  结算期 / 规则 hub API

if __name__ == "__main__":
    # 启动时自动备份 + 每日备份线程 + 清理过期回收站 + 利润核对（只在真正启动服务时跑，测试/脚本 import 不触发）
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        system_ops.start_background()
        regression.startup_check(system_ops._db_path())
    # macOS「隔空播放接收器」(AirTunes) 会占用 *:5000，curl 会看到 Server: AirTunes/… 403，不是本系统。
    # 故 Mac 默认 5001；其它系统仍 5000。可设 PORT= 覆盖。
    _default_port = 5001 if sys.platform == "darwin" else 5000
    port = int(os.environ.get("PORT", _default_port))
    debug = os.environ.get("DEBUG", "0") == "1"
    # 电脑本机监听 127.0.0.1；手机另开局域网端口(默认 5080)。不想让手机连：SCRAPMETAL_HOST=127.0.0.1
    print(f"=== 废钢供应链系统 v{APP_VERSION} · 电脑打开 http://127.0.0.1:{port} ===", flush=True)
    if sys.platform == "darwin" and port == 5000:
        print(
            "    [提示] Mac 上 5000 常被「隔空播放接收器」占用；未设 PORT 时默认已用 5001。"
            " 若你强制 PORT=5000，请先关闭系统设置里的隔空播放接收器。",
            flush=True,
        )
    elif port == 5000:
        print(
            f"    若浏览器提示「Access to 127.0.0.1 was denied」且 curl 显示 Server: AirTunes："
            f" 那是 Mac 隔空播放占用了 5000，请 PORT=5001 python3 app.py",
            flush=True,
        )
    if os.environ.get("SCRAPMETAL_HOST", "0.0.0.0") != "127.0.0.1" and not debug:
        mobile.start_lan_server(app, int(os.environ.get("MOBILE_PORT", 5080)))
    try:
        app.run(host="127.0.0.1", port=port, debug=debug)
    except OSError as e:
        if getattr(e, "errno", None) in (98, 48, 10048):  # Linux/macOS/Windows: address already in use
            print(
                f"\n[错误] 端口 {port} 已被占用（macOS 上常见：系统「隔空播放接收器」占 5000）。\n"
                f"  解决：系统设置 → 通用 → AirDrop 与隔空播放 → 关闭「隔空播放接收器」；\n"
                f"  或换端口启动：PORT=5001 python3 app.py ，浏览器打开 http://127.0.0.1:5001\n",
                flush=True,
            )
        raise
