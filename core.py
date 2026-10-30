"""核心：Flask 实例 + 子模块统一重导出。路由分散在 routes_*.py，由 app.py 统一加载。

v2.3 结构拆分（2026-09-30）：原 1100 行按职责拆成 7 个子模块——
  archives_config   档案/状态/链条规则/统计分桶等常量
  flow_graph        链条规则展示（公式翻译/利润快照/规则校验/流程图组装）
  trip_compute      车次组装/计算/同步（取全量/进度/单据面板/利润试算入口）
  stats             统计分桶（利润构成聚合/逐车拆解/周期统计/票源额度水位）
  entry             录入辅助（默认值/车牌画像/状态推进/快速结算）
  entity_stats      实体下钻/报表筛选/质量补充
  import_helpers    钢厂结算单导入辅助
本文件只保留 Flask 实例和全局配置；子模块名字统一搬到 core 命名空间，
`from core import *` 行为零改动。原文件存档 legacy/core_before_split.py。
"""
import os
import io
import json
import sqlite3
import datetime as dt
from flask import Flask, g, jsonify, request, render_template, send_from_directory, send_file

from db import get_db, close_db, query, execute, row_to_dict, rows_to_list
import calc
import engine
import lanes
import system_ops
from system_ops import audit, audit_row_diff

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

app = Flask(__name__)
app.teardown_appcontext(close_db)
app.register_blueprint(lanes.bp)  # B阶段：三条线/批次/回款追踪（lanes.py）
app.register_blueprint(system_ops.bp)  # v2：备份/操作留痕/回收站（system_ops.py）
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.jinja_env.auto_reload = True
APP_VERSION = system_ops.BOOT_VERSION


@app.context_processor
def _inject_version():
    return {"app_version": APP_VERSION}


@app.after_request
def _no_cache(resp):
    # 本地单机小系统：静态资源/页面一律不缓存，避免每次改完代码后浏览器仍显示旧版本
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


# ---------------------------------------------------------------------------
# 子模块重导出
# ---------------------------------------------------------------------------
# 不用 `from xxx import *`（`*` 默认不导出 `_` 开头的名字，而工具函数都是 `_` 开头），
# 改用 globals().update 把所有子模块名字搬到 core 命名空间，保持 `from core import *`
# 拿到的名字集合和拆分前完全一致。
import archives_config, flow_graph, trip_compute, stats, entry, entity_stats, import_helpers
for _m in (archives_config, flow_graph, trip_compute, stats, entry, entity_stats, import_helpers):
    globals().update({n: getattr(_m, n) for n in dir(_m) if not n.startswith("__")})


# 路由模块用 `from core import *` 取到这里所有名字（包括下划线开头的工具函数）
__all__ = [n for n in dir() if not n.startswith("__")]
