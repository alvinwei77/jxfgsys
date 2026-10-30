"""统计分桶（从 core.py 拆出）：利润构成聚合桶 + 逐车拆解 + 周期统计 + 票源额度水位。"""
import datetime as dt

import engine
from archives_config import SPLIT_MODE_CODES
from calc import get_period_bounds
from trip_compute import _compute_finance

# ---------------------------------------------------------------------------
# 首页看板 API
# ---------------------------------------------------------------------------
def _trip_profit(conn, t):
    """已固化快照直接取值，否则实时估算。"""
    fin_row = conn.execute("SELECT * FROM trip_finance WHERE trip_id=?", (t["id"],)).fetchone()
    if fin_row and fin_row["is_locked"]:
        return fin_row["net_profit"] or 0
    return _compute_finance(conn, t)["net_profit"] or 0


def _trip_reward(conn, t):
    """量补金额（已固化快照直接取值，否则实时估算），单独列出以便核对特殊周期是否按约定单价计算量补。"""
    fin_row = conn.execute("SELECT * FROM trip_finance WHERE trip_id=?", (t["id"],)).fetchone()
    if fin_row and fin_row["is_locked"]:
        return fin_row["reward_profit"] or 0
    return _compute_finance(conn, t)["reward_profit"] or 0


def _trip_finance_full(conn, t):
    """完整取一条车次的财务快照（已固化直接取值，否则实时估算），用于拆解利润构成。"""
    fin_row = conn.execute("SELECT * FROM trip_finance WHERE trip_id=?", (t["id"],)).fetchone()
    if fin_row and fin_row["is_locked"]:
        return dict(fin_row)
    return _compute_finance(conn, t)

def _trip_breakdown(conn, t):
    """单个车次的利润构成拆解：净利润 = 结算毛利 + 量补 − 垫资成本 + 其他净调整。
    首页周期卡片、一键导出台账的"统计汇总"都复用这一份逻辑，保证口径一致。
    注意：reserved_profit（票差留存利润，经过我们的票源方如益周才有）2026-09-17起
    已从净利润/结算毛利里剔除，单独留存、不计入这里任何一项，也不进入 net_profit 汇总——
    调用方如需展示，单独取 reserved_profit 字段，绝不能加进 net_profit 或 gross_margin。
    2026-09-17新增：额外返回"其他净调整"的4个原始分项(service_fee_income/
    dispatcher_commission_cost/ticket_cost/other_cost)和车次所属路径(is_split_path)，
    供首页"利润构成明细"面板做"A+B+C=结果"式的实际数字分项展示，而不是只给一个加总数。"""
    fin = _trip_finance_full(conn, t)
    gross_margin = fin.get("freight_diff_profit") or 0  # 票差利润已剔除，不再计入结算毛利
    reward_amount = fin.get("reward_profit") or 0
    advance_fee_cost = fin.get("advance_fee_cost") or 0
    service_fee_income = fin.get("service_fee_income") or 0
    dispatcher_commission_cost = fin.get("dispatcher_commission_cost") or 0
    ticket_cost = fin.get("ticket_cost") or 0
    other_cost = fin.get("other_cost") or 0
    other_net = service_fee_income - dispatcher_commission_cost - ticket_cost - other_cost
    return {
        "gross_margin": gross_margin,
        "reward_amount": reward_amount,
        "advance_fee_cost": advance_fee_cost,
        "other_net": other_net,
        "net_profit": fin.get("net_profit") or 0,
        "reserved_profit": fin.get("invoice_diff_profit") or 0,
        "is_split_path": (t["mode_code"] or "") in SPLIT_MODE_CODES,
        "service_fee_income": service_fee_income,
        "dispatcher_commission_cost": dispatcher_commission_cost,
        "ticket_cost": ticket_cost,
        "other_cost": other_cost,
    }

# ---------------------------------------------------------------------------
# 统计桶维度定义（v2.3 重构，2026-09-30）：基础桶 + 维度装饰器。
# 旧版 _empty/_accum/_finalize 把 split_*/service_*/truck_*/ship_* 平铺在桶里，每加一个
# 维度要改 4 处（_empty/_accum/_finalize/接口铺开，22节踩过"桶里有接口没返回"的坑）。
# 改成基础桶只记总量+利润构成，维度(path/transport)作为可叠加的子桶挂在 _dims 上，
# _finalize 时再把子桶平铺回 split_*/truck_* 字段——对外 JSON 形状和旧版一字不差
# （golden_master + 118车次对照测试 md5 一致），但加新维度只需在 DIMENSIONS 加一项。
# ---------------------------------------------------------------------------
DIMENSIONS = [
    {
        # 结算路径维度：split=路径1/3配票·反向(差价模型M1/M5)，service=路径2带票带货(服务费模型M2/M3/M4)
        "name": "path",
        "keys": ("split", "service"),
        "key_of": lambda t, b: "split" if b["is_split_path"] else "service",
        "fields": {
            "trip_count": None,  # +1
            "advance": ("b", "advance_fee_cost"),
            "tonnage": ("t", "settlement_tonnage_total"),
            "settlement_amount": ("t", "settlement_amount_total"),
            "profit": ("b", "net_profit"),
            "commission": ("b", "dispatcher_commission_cost"),
        },
        "derivatives": [
            {"name": "profit_per_ton", "num": "profit", "den": "tonnage"},
        ],
    },
    {
        # 运输方式维度：truck=汽运，ship=船运（船均吨位是车的10倍，必须分开统计）
        "name": "transport",
        "keys": ("truck", "ship"),
        "key_of": lambda t, b: "ship" if t["transport_type"] == "ship" else "truck",
        "fields": {
            "trip_count": None,
            "tonnage": ("t", "settlement_tonnage_total"),
            "settlement_amount": ("t", "settlement_amount_total"),
            "profit": ("b", "net_profit"),
        },
        "derivatives": [
            {"name": "profit_per_ton", "num": "profit", "den": "tonnage"},
            {"name": "avg_tonnage", "num": "tonnage", "den": "trip_count"},
        ],
    },
]


def _empty_stats_bucket(name=None):
    """零值的利润构成聚合桶，字段跟 _trip_breakdown() 的产出一一对应。被首页周期卡片
    (_period_trip_stats) 和统计报表 (reports_summary) 共用，保证"按期/按钢厂/按调货员/
    按票源方/按结算路径"等任意维度汇总口径完全一致，都是从同一份节点化拆解(_trip_breakdown)
    累加出来的，不是另起一套算法。

    v2.3 重构：基础桶只记总量+利润构成，维度(path/transport)挂在 _dims 子桶上，
    _finalize 时平铺回 split_*/truck_* 字段，对外 JSON 形状与旧版一字不差。"""
    bk = {
        "name": name, "trip_count": 0, "tonnage": 0.0, "settlement_amount": 0.0,
        "profit": 0.0,
        "gross_margin": 0.0,      # 结算毛利：钢厂含税金额与磅差/开票差价等形成的基础毛利（不含量补、不扣垫资成本）
        "reward_amount": 0.0,     # 量补金额
        "advance_fee_cost": 0.0,  # 垫资成本（资金占用成本，是利润的减项）
        "other_net": 0.0,         # 其他净调整：调度提成、服务费收入、票务成本等杂项的净额
        "reserved_profit": 0.0,   # 票差留存利润（经过我们的票源方才有，如益周）：单独统计，不计入 profit
        "reserved_trip_count": 0,
        "service_fee_income_total": 0.0,
        "dispatcher_commission_total": 0.0,
        "ticket_cost_total": 0.0,
        "other_cost_total": 0.0,
    }
    bk["_dims"] = {
        dim["name"]: {
            key: {f: (0 if f == "trip_count" else 0.0) for f in dim["fields"]}
            for key in dim["keys"]
        }
        for dim in DIMENSIONS
    }
    return bk


def _accum_bucket(bk, t, b):
    """把单笔车次的节点化拆解(b = _trip_breakdown(conn, t))累加进聚合桶 bk。
    基础字段直接累加；维度子桶按车次属性(key_of)归入对应 key 后累加。"""
    bk["trip_count"] += 1
    bk["tonnage"] += t["settlement_tonnage_total"] or 0
    bk["settlement_amount"] += t["settlement_amount_total"] or 0
    bk["profit"] += b["net_profit"]
    bk["gross_margin"] += b["gross_margin"]
    bk["reward_amount"] += b["reward_amount"]
    bk["advance_fee_cost"] += b["advance_fee_cost"]
    bk["other_net"] += b["other_net"]
    bk["reserved_profit"] += b["reserved_profit"]
    if abs(b["reserved_profit"]) > 0.005:
        bk["reserved_trip_count"] += 1
    bk["service_fee_income_total"] += b["service_fee_income"]
    bk["dispatcher_commission_total"] += b["dispatcher_commission_cost"]
    bk["ticket_cost_total"] += b["ticket_cost"]
    bk["other_cost_total"] += b["other_cost"]
    for dim in DIMENSIONS:
        key = dim["key_of"](t, b)
        sub = bk["_dims"][dim["name"]][key]
        for f, src in dim["fields"].items():
            if src is None:  # trip_count
                sub[f] += 1
            else:
                src_obj, src_field = src
                val = t[src_field] if src_obj == "t" else b[src_field]
                sub[f] += val or 0


def _finalize_bucket(bk):
    """算基础派生字段 + 把维度子桶平铺回 split_*/truck_* 字段（兼容旧版 JSON 形状）+
    算各维度派生(profit_per_ton/avg_tonnage)平铺。平铺后 _dims 删除。"""
    # 基础派生
    bk["reward_avg_price"] = (bk["reward_amount"] / bk["tonnage"]) if bk["tonnage"] else None
    bk["profit_per_ton"] = (bk["profit"] / bk["tonnage"]) if bk["tonnage"] else None
    # 维度子桶平铺 + 派生
    dims = bk.pop("_dims", {})
    for dim in DIMENSIONS:
        for key in dim["keys"]:
            sub = dims[dim["name"]][key]
            for f in dim["fields"]:
                bk[f"{key}_{f}"] = sub[f]
            for d in dim["derivatives"]:
                num, den = sub[d["num"]], sub[d["den"]]
                bk[f"{key}_{d['name']}"] = (num / den) if den else None
    return bk

def _trips_stats(conn, trips, name=None):
    """任意一组车次(不限定维度)的完整利润构成聚合，逐车次拆解口径。"""
    bk = _empty_stats_bucket(name)
    for t in trips:
        _accum_bucket(bk, t, _trip_breakdown(conn, t))
    return _finalize_bucket(bk)

def _period_trip_stats(conn, mill_id, period_start, period_end):
    trips = conn.execute(
        """SELECT * FROM trips WHERE mill_id=? AND status!='voided' AND settlement_date IS NOT NULL
           AND date(settlement_date) BETWEEN date(?) AND date(?)""",
        (mill_id, period_start, period_end),
    ).fetchall()
    st = _trips_stats(conn, trips)
    st["trip_ids"] = [t["id"] for t in trips]  # 首页数字点进去看是哪些车
    return st

# ---------------------------------------------------------------------------
# 票源方「角色」：由车次结算模式推断，而不是由"档案里填没填额度"猜（2026-10-04 升级）
#   M1 配票 / M5 反向 → 占用票源额度 → A 区
#   M2/M3/M4 直签带票 → 自带票，不占额度 → B 区
# 用"有没有额度"区分的话，一旦某家「本期没有票额」，就会被误判成自带票混进 B 区。
# ---------------------------------------------------------------------------
QUOTA_ROLE_MODES = {"peipiao": ("M1",), "reverse": ("M5",), "carry": ("M2", "M3", "M4")}
QUOTA_ROLE_CN = {"peipiao": "配票", "reverse": "反向", "carry": "自带票", "unset": "未定"}


def _has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone() is not None


def _source_term_row(conn, source_id, on_date):
    """该票源方在 on_date 当天生效的「分期额度/接单状态」记录（没有则 None，走档案默认值）。
    业务上票额是按月给的（这家这月有、下月可能就没），反向也会中途停接单，
    这些随时间变化的自由度统一放在 invoice_source_terms，不写死在档案列上。"""
    return conn.execute(
        "SELECT * FROM invoice_source_terms WHERE invoice_source_id=? "
        "  AND date(effective_from)<=date(?) AND (effective_to IS NULL OR date(effective_to)>=date(?)) "
        "ORDER BY date(effective_from) DESC, id DESC LIMIT 1",
        (source_id, on_date, on_date),
    ).fetchone()


def _quota_levels(conn):
    """票源额度水位：按票源方自己的额度周期计算已用金额与占比，用于首页水位图。
    已用金额 = 该票源方名下车次"前端给垫资开票的金额"合计(结算金额A - 垫资费)，而非结算金额本身——
    这是我们找外部票商配票、开给欧冶(国企垫资)的发票金额，国企再开给钢厂的部分不关心。
    垫资费单价按车次模板+费率档查（反向在其票点已含垫资费的时段=0，见 migrate_reverse_advance_fee.py）。

    周期口径（2026-10-04 修正）：
      · 月度额度（配票公司）→ **我方结算期**，即 26 日 ~ 次月 25 日（calc.get_period_bounds 默认值，
        与"各钢厂结算周期"卡片、经营报告同口径）。
      · 年度额度（反向个人）→ **自然年**累计（额度按年给，与月期无关）。

    时效自由度（2026-10-04 新增，用户要求"别写死"）：
      · 每家怎么算（配票/反向/自带票）→ 按车次 mode_code 推断（QUOTA_ROLE_MODES），不再看额度有无；
      · 每家的额度/预警线/是否接单 → 先看 invoice_source_terms 里"今天"命中的那条分期记录
        （票额按月给、中途停止接单都表达在这里），没命中才回退 invoice_sources 的档案默认值；
      · 被停用（accepting=0）或本期无票额（quota 为空）的票源 → excluded=True，不计入水位，
        但仍原样返回，供首页用一行灰字说明"本期不计入：某某（原因）"，信息不丢。"""
    today = dt.date.today()
    today_s = today.isoformat()
    year_start = today.replace(month=1, day=1).isoformat()
    year_end = "%d-12-31" % today.year
    period_start, period_end = get_period_bounds(today_s)

    # 全局额度预警线（%）：票源方档案里单独设了预警金额就以档案为准，否则前端按这个百分比染色。
    # 2026-10-09 起可在「系统 → 参数设置」改（原来写死 90）。
    import settings as _settings_module
    default_warn_pct = float(_settings_module.get(conn, "quota_warn_pct", 90.0))

    has_terms = _has_table(conn, "invoice_source_terms")

    # 各票源方的车次模式分布（一次分组查完，用于判定角色，避免逐家再查）
    modes = {}
    for r in conn.execute(
        "SELECT invoice_source_id AS sid, mode_code, COUNT(*) AS c FROM trips "
        "WHERE status!='voided' AND invoice_source_id IS NOT NULL AND mode_code IS NOT NULL "
        "GROUP BY invoice_source_id, mode_code"
    ):
        modes.setdefault(r["sid"], {})[r["mode_code"]] = r["c"]

    sources = conn.execute("SELECT * FROM invoice_sources ORDER BY id").fetchall()
    result = []
    for s in sources:
        ms = set(modes.get(s["id"], {}))
        # —— 角色：先看车次模式；没有车次的新档案再按档案兜底 ——
        if s["source_type"] == "reverse_individual" or "M5" in ms:
            role = "reverse"
        elif "M1" in ms:
            role = "peipiao"
        elif ms & {"M2", "M3", "M4"}:
            role = "carry"
        elif s["quota_amount"]:
            role = "peipiao"
        elif s["source_type"] == "front_company":
            role = "carry"
        else:
            role = "reverse"

        # —— 分期覆盖：票额按月 / 中途停接单 ——
        term = _source_term_row(conn, s["id"], today_s) if has_terms else None
        excluded, exclude_reason = False, None
        if term is not None:
            if not (term["accepting"] or 0):
                excluded, exclude_reason = True, "已停止接单"
            elif term["quota_amount"] is None:
                excluded, exclude_reason = True, "本期无票额"
            quota = term["quota_amount"]
            warn = term["warn_threshold"] if term["warn_threshold"] is not None else s["warn_threshold"]
        else:
            quota, warn = s["quota_amount"], s["warn_threshold"]

        is_annual = s["quota_period"] == "annual"
        win_start, win_end = (year_start, year_end) if is_annual else (period_start, period_end)
        # 垫资费单价逐车按模板+费率档查(engine.advance_fee_unit_for_trip)，不再写死"M5不扣"：
        # 垫资费一直都有，只是某些月份反向的垫资费已含在反向票点里(那段时间反向档=0)。
        rows = conn.execute(
            """SELECT t.* FROM trips t WHERE t.invoice_source_id = ? AND t.status != 'voided'
                 AND t.settlement_date IS NOT NULL
                 AND date(t.settlement_date) BETWEEN date(?) AND date(?)""",
            (s["id"], win_start, win_end),
        ).fetchall()
        used = sum((t["settlement_amount_total"] or 0)
                   - (t["settlement_tonnage_total"] or 0) * engine.advance_fee_unit_for_trip(conn, t) for t in rows)
        tonnage = sum((t["settlement_tonnage_total"] or 0) for t in rows)
        # 票面额（含税结算额）：自带票（直签带票）由带票公司自行开票，
        # 口径就是含税结算额本身，不减垫资费——与《经营报告》B 区一致
        gross = sum((t["settlement_amount_total"] or 0) for t in rows)
        pct = (used / quota * 100) if quota else None
        warn_pct = (warn / quota * 100) if (quota and warn) else None
        result.append({
            "id": s["id"],
            "name": s["name"],
            "source_type": s["source_type"],
            "role": role,
            "role_label": QUOTA_ROLE_CN.get(role, role),
            "excluded": excluded,
            "exclude_reason": exclude_reason,
            "term_notes": (term["notes"] if term is not None else None),
            "quota_period": s["quota_period"],
            "period_start": win_start,
            "period_end": win_end,
            "period_label": "全年" if is_annual else "本期",
            "period_span": ("%s 年累计" % today.year) if is_annual
                           else ("%s ~ %s" % (win_start[5:], win_end[5:])),
            # 自带票（直签带票，不设额度）在首页只关心"本期做了多少"，故一并给出车次/吨位
            # （与 used_amount 同窗口：月度额度=本期，年度额度=全年）
            "trip_count": len(rows),
            "tonnage": tonnage,
            "gross_amount": gross,
            "quota_amount": quota,
            "warn_threshold": warn,
            "used_amount": used,
            "pct": pct,
            "warn_pct": warn_pct,
            "default_warn_pct": default_warn_pct,
        })
    return result
