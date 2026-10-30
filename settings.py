"""系统参数（C档「说人话 · 收口」2026-10-09）。

背景：改界面的时候清点了一遍"写死在代码里的数字"，发现一堆阈值散在 7 个模块里——
90% 预警线、8 天/30 天超期线、账龄分桶 15/30/60、车牌带出比例 0.8、回收站 30 天……
用户想调其中一个，得让开发改代码、重启系统。

做法：
- 值仍旧存在既有的 `app_settings(key,value)` 表里（本来就是通用 key-value，不新建表、不动数据）。
- 每一项都在下面的 SETTINGS_CATALOG 里声明：类型/默认值/单位/取值范围/一句人话说明/分组。
  **默认值 = 原来写死的那个数**，所以一个都不改时，系统行为与改动前逐字节一致
  （已用 tools_golden_master.py 519 接口 A/B 验证）。
- 读取统一走 `get(conn, key)`：读不到、不是数字、超出范围 → 一律回退默认值。
  配置填坏了绝不能让算钱/统计崩掉。
- 边界：这里只收「提醒阈值 / 显示比例 / 维护参数」这类**不参与算钱**的数字。
  真正的费率、点位、额度、阶梯仍在「费率与关系」「链条规则」「基础档案」里管——
  不把利润率这种东西放进一个可以随手改的输入框。
"""
import sqlite3

# group 决定在「系统 → 参数设置」页里分到哪一栏；顺序即展示顺序。
CATALOG = [
    # ---------------- 提醒与预警 ----------------
    {"key": "quota_warn_pct", "group": "提醒与预警", "label": "票源额度预警线", "type": "float",
     "default": 90.0, "unit": "%", "min": 1, "max": 100,
     "help": "票源额度用到这个百分比，首页就标成「快用完」（转黄）；到 100% 标「已用完」（转红）。"
             "票源方档案里单独设了预警金额的，以档案里的为准。"},
    {"key": "quota_high_water_pct", "group": "提醒与预警", "label": "经营报告·水位偏高线", "type": "float",
     "default": 80.0, "unit": "%", "min": 1, "max": 100,
     "help": "导出的《经营报告》Excel「票源额度水位」页里，占额度超过这个百分比就标「⚠ 水位偏高」。"},
    {"key": "report_stale_days", "group": "提醒与预警", "label": "报备多久没结算要提醒", "type": "int",
     "default": 3, "unit": "天", "min": 1, "max": 60,
     "help": "车次报备后超过这么多天还没结算，就进首页「待办与预警」（可能在提示你结算单出了没录）。"},
    {"key": "invoice_overdue_days", "group": "提醒与预警", "label": "结算多久没开销项票要提醒", "type": "int",
     "default": 7, "unit": "天", "min": 1, "max": 180,
     "help": "车次结算后超过这么多天还没开销项票，就进首页「待办与预警」。"},
    {"key": "payment_overdue_days", "group": "提醒与预警", "label": "开票多久没回款要提醒", "type": "int",
     "default": 30, "unit": "天", "min": 1, "max": 365,
     "help": "开票后超过这么多天还没登记欧冶回款批次，就进首页「待办与预警」（历史车次补登前都会算在这里）。"},

    # ---------------- 录入辅助 ----------------
    {"key": "plate_stable_ratio", "group": "录入辅助", "label": "车牌自动带出的稳定门槛", "type": "float",
     "default": 0.8, "unit": "比例", "min": 0.1, "max": 1.0,
     "help": "输入车牌后，某个字段（供货厂/调货人/票源/链条）历史上的取值占比达到这个比例，"
             "才自动带出；达不到就留空让你手选（比如跑好几个供货厂的车）。0.8 = 八成以上一致才带。"},
    {"key": "fleet_alias_ratio", "group": "录入辅助", "label": "判定「调货人自己车队」的最低占比", "type": "float",
     "default": 0.5, "unit": "比例", "min": 0.1, "max": 1.0,
     "help": "这个车牌历史上供货厂记成「调货人同名占位基地」的占比超过它，就认定是调货人自己的车，"
             "供货厂可以留空不填。"},

    # ---------------- 账龄分桶 ----------------
    {"key": "aging_days_1", "group": "账龄分桶", "label": "第一档（含以内）", "type": "int",
     "default": 15, "unit": "天", "min": 1, "max": 120,
     "help": "开票/回款待办按账龄分档统计，首页和回款追踪页的彩条用这几档。三档必须依次递增。"},
    {"key": "aging_days_2", "group": "账龄分桶", "label": "第二档上限", "type": "int",
     "default": 30, "unit": "天", "min": 2, "max": 365, "help": "同上，必须大于第一档。"},
    {"key": "aging_days_3", "group": "账龄分桶", "label": "第三档上限", "type": "int",
     "default": 60, "unit": "天", "min": 3, "max": 730, "help": "同上，必须大于第二档；超过这一档的算「N天以上」。"},

    # ---------------- 系统维护 ----------------
    {"key": "keep_auto_backups", "group": "系统维护", "label": "自动备份保留份数", "type": "int",
     "default": 30, "unit": "份", "min": 3, "max": 500,
     "help": "自动备份最多留几份，超出删最旧的。手动备份、以及点了「保留 📌」的备份不会被自动删。"},
    {"key": "recycle_days", "group": "系统维护", "label": "回收站保留天数", "type": "int",
     "default": 30, "unit": "天", "min": 1, "max": 365,
     "help": "删掉的车次/档案在回收站放多少天，过期的在系统启动时自动清掉。改小不会立刻清，下次启动才生效。"},
    {"key": "mobile_cookie_days", "group": "系统维护", "label": "手机端登录有效期", "type": "int",
     "default": 180, "unit": "天", "min": 1, "max": 3650,
     "help": "手机输一次口令后能免密用多少天，到期要重新输。"},
]

BY_KEY = {c["key"]: c for c in CATALOG}
GROUPS = []
for _c in CATALOG:
    if _c["group"] not in GROUPS:
        GROUPS.append(_c["group"])


def default_of(key):
    c = BY_KEY.get(key)
    return c["default"] if c else None


def get(conn, key, default=None):
    """读一个参数。任何异常（表不存在/值不是数字/越界）都回退默认值——配置填坏不影响算钱。"""
    meta = BY_KEY.get(key)
    fallback = default if default is not None else (meta["default"] if meta else None)
    if conn is None:
        return fallback
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return fallback
    if row is None:
        return fallback
    raw = row["value"] if not isinstance(row, (tuple, list)) else row[0]
    if raw in (None, ""):
        return fallback
    try:
        v = float(raw) if (meta and meta["type"] == "float") else int(float(raw))
    except (TypeError, ValueError):
        return fallback
    if meta:
        lo, hi = meta.get("min"), meta.get("max")
        if lo is not None and v < lo:
            return fallback
        if hi is not None and v > hi:
            return fallback
    return v


def aging_buckets(conn):
    """账龄分桶 [(上限天数, 标签)]，标签格式与旧版写死时一字不差（"15天内"/"16-30天"/…）。
    三档填得不递增（比如把第二档填成 10）就整体回退默认 15/30/60，不给出半个奇怪的分档。"""
    d = [int(get(conn, "aging_days_%d" % i)) for i in (1, 2, 3)]
    if not (0 < d[0] < d[1] < d[2]):
        d = [int(default_of("aging_days_%d" % i)) for i in (1, 2, 3)]
    return [(d[0], "%d天内" % d[0]), (d[1], "%d-%d天" % (d[0] + 1, d[1])),
            (d[2], "%d-%d天" % (d[1] + 1, d[2])), (None, "%d天以上" % d[2])]


def payload(conn):
    """给「系统 → 参数设置」页的完整清单：每项的说明 + 当前值（未设置则 = 默认值）。"""
    out = []
    for c in CATALOG:
        row = dict(c)
        row["value"] = get(conn, c["key"])
        row["is_default"] = str(row["value"]) == str(c["default"])
        out.append(row)
    return {"groups": GROUPS, "items": out}
