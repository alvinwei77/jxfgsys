"""一键导出台账（2026-09-21 重做）。

用户要求：
- 主表重要数据靠前；每车利润的过程参数细化（费率+金额两列一组），净利润能从过程列算出来、对得上；
- 可以按带票/配票等过滤（主表/明细/票款表都开了自动筛选，路径、链条、车船、状态都是独立列）；
- 子表补齐各类统计分析，包括票款（开票/回款环节、批次、提成、票源额度）。

口径：
- 每车数字 = 系统里的同一套计算（已固化车次取固化快照、未固化按费率与关系实时算、本车费率覆盖已合并），
  跟页面/报表完全一致。
- 净利润列是公式：毛服务费 + 量补 − 垫资成本 − 调货人提成 − 其他成本（配票/反向路径的"垫资成本"就是个人垫资），
  导出时用系统值校验过，逐车一致。
- 统计汇总全部是引用主表的 SUMIFS/COUNTIFS 公式，在 Excel 里改主表数字，汇总跟着变。
"""
import datetime as dt

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

FONT = "Arial"
HDR_FILL = PatternFill("solid", fgColor="DCE6F1")
GRP_FILLS = {  # 表头分组底色：基本信息 / 利润 / 过程参数 / 票款 / 其他
    "base": "DCE6F1", "profit": "E2EFDA", "proc": "FFF2CC", "lane": "EDE7F6", "other": "EDEDED",
}
MONEY = '#,##0.00;[Red]-#,##0.00;"-"'
TON = '#,##0.000;[Red]-#,##0.000;"-"'
RATE = '0.00;[Red]-0.00;"-"'
STATUS_CN = {"reported": "已报备", "arrived": "已到厂", "settled": "已结算", "invoiced": "已开票", "closed": "已结案"}
HOP_ORDER = ["in", "out", "p_out", "p_in", "p_client"]
STATE_CN = {"done": "已登记", "partial": "部分登记", "todo": "待登记", "blocked": "未开始(前一环节未完成)",
            "legacy": "按状态视为已完成(历史)"}


def _hdr(ws, row, headers, groups=None):
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = Font(name=FONT, bold=True, size=10)
        c.fill = PatternFill("solid", fgColor=GRP_FILLS.get((groups or {}).get(i, "base"), "DCE6F1"))
        c.alignment = Alignment(wrap_text=True, vertical="center")


def _fonts(ws):
    for row in ws.iter_rows():
        for c in row:
            f = c.font
            c.font = Font(name=FONT, bold=f.bold, size=f.size or 10, color=f.color, italic=f.italic)


def build_workbook(conn, trips, fin_of, breakdown_of, lanes_of, origin_of, alloc, extras):
    """trips: 已按日期排序的车次行（含 base_name/dispatcher_name/... 连表字段）
    fin_of(t)->完整财务字典；breakdown_of(t)->_trip_breakdown；lanes_of: {trip_id: lanes}；
    origin_of(t)->供货厂/车队展示名；alloc: lanes.load_alloc 结果；extras: 其它子表数据。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "车次台账"
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    # ---------------- 主表列定义：(表头, 分组, 宽度, 格式, 取值函数) ----------------
    # 顺序：谁/什么时候/什么路径 → 吨位金额 → 净利润/单吨利润 → 利润过程(费率+金额成对) → 票款 → 其余信息
    C = []

    def col(h, g, w, fmt, fn):
        C.append((h, g, w, fmt, fn))

    col("车次ID", "base", 7, None, lambda t, f, b, L: t["id"])
    col("结算日期", "base", 11, "yyyy-mm-dd", lambda t, f, b, L: _d(t["settlement_date"]))
    col("车号/船号", "base", 13, None, lambda t, f, b, L: t["plate_no"])
    col("车/船", "base", 6, None, lambda t, f, b, L: "船" if t["transport_type"] == "ship" else "车")
    col("路径", "base", 9, None, lambda t, f, b, L: "配票/反向" if b["is_split_path"] else "带票带货")
    col("链条", "base", 9, None, lambda t, f, b, L: t["template_short"] or t["template_name"])
    col("调货人", "base", 8, None, lambda t, f, b, L: t["dispatcher_name"])
    col("供货厂/车队", "base", 11, None, lambda t, f, b, L: origin_of(t))
    col("票源方", "base", 11, None, lambda t, f, b, L: t["invoice_source_name"])
    col("状态", "base", 8, None, lambda t, f, b, L: STATUS_CN.get(t["status"], t["status"]))
    col("结算吨位(吨)", "base", 11, TON, lambda t, f, b, L: t["settlement_tonnage_total"] or 0)
    col("结算金额(元)", "base", 14, MONEY, lambda t, f, b, L: t["settlement_amount_total"] or 0)
    col("净利润(元)", "profit", 12, MONEY, None)            # 公式，见下
    col("单吨利润(元/吨)", "profit", 10, RATE, None)        # 公式
    col("留存利润(元)·不计入净利润", "profit", 12, MONEY, lambda t, f, b, L: b["reserved_profit"])
    # 过程参数：服务费 / 量补 / 垫资 / 提成 / 其他，每项"单价 + 金额"两列
    col("服务费单价(元/吨)", "proc", 9, RATE, lambda t, f, b, L: _per_ton(f.get("service_fee_income"), t) if not b["is_split_path"] else None)
    col("毛服务费(元)", "proc", 11, MONEY, lambda t, f, b, L: b["service_fee_income"])
    col("量补单价(元/吨)", "proc", 9, RATE, lambda t, f, b, L: f.get("reward_unit_price"))
    col("量补金额(元)", "proc", 11, MONEY, lambda t, f, b, L: b["reward_amount"])
    col("对外结算点位(%)", "proc", 8, RATE, lambda t, f, b, L: f.get("rate_out") if b["is_split_path"] else None)
    col("给客户付(元)", "proc", 13, MONEY, lambda t, f, b, L: f.get("give_customer") if b["is_split_path"] else None)
    col("垫资费单价(元/吨)", "proc", 9, RATE, lambda t, f, b, L: _per_ton(f.get("advance_fee_raw"), t) if b["is_split_path"] else f.get("advance_fee_rate"))
    col("垫资费(元)", "proc", 11, MONEY, lambda t, f, b, L: f.get("advance_fee_raw") if b["is_split_path"] else b["advance_fee_cost"])
    col("前端开票金额(元)", "proc", 13, MONEY, lambda t, f, b, L: f.get("front_invoice_amount") if b["is_split_path"] else None)
    col("票源成本点位(%)", "proc", 8, RATE, lambda t, f, b, L: f.get("rate_in") if b["is_split_path"] else None)
    col("前端实付(元)", "proc", 13, MONEY, lambda t, f, b, L: f.get("front_actual_paid") if b["is_split_path"] else None)
    col("垫资成本(元)·配票=个人垫资", "proc", 12, MONEY, lambda t, f, b, L: b["advance_fee_cost"])
    col("提成单价(元/吨)", "proc", 8, RATE, lambda t, f, b, L: f.get("dispatcher_commission_rate"))
    col("调货人提成(元)", "proc", 11, MONEY, lambda t, f, b, L: b["dispatcher_commission_cost"])
    col("其他成本(元)·含切割费", "proc", 11, MONEY, lambda t, f, b, L: b["other_cost"])
    col("本车改过的费率", "proc", 16, None, lambda t, f, b, L: _overrides_text(f))
    # 票款：开票路径 + 各环节已登记金额
    col("开票路径", "lane", 14, None, lambda t, f, b, L: L["path_label"] if L else None)
    col("票额(估)(元)", "lane", 13, MONEY, lambda t, f, b, L: L["ticket_amount"] if L else None)
    for key, label in (("in", "进项"), ("out", "销项→欧冶"), ("p_out", "欧冶回款"), ("p_in", "付票源"), ("p_client", "到客户账户")):
        col(f"{label}", "lane", 11, None, (lambda k: lambda t, f, b, L: _hop_state(L, k))(key))
    col("提成已付(元)", "lane", 11, MONEY, lambda t, f, b, L: (alloc.get(t["id"], {}).get("commission") or {}).get("amount", 0))
    # 其余信息
    col("接单日期", "other", 11, "yyyy-mm-dd", lambda t, f, b, L: _d(t["order_date"]))
    col("结算月份", "other", 9, None, lambda t, f, b, L: (t["settlement_date"] or "")[:7] or None)
    col("扣杂(吨)", "other", 8, TON, lambda t, f, b, L: t["deduction_total"])
    col("结算单号", "other", 16, None, lambda t, f, b, L: t["settlement_doc_no"])
    col("钢厂", "other", 10, None, lambda t, f, b, L: t["mill_name"])
    col("户头", "other", 14, None, lambda t, f, b, L: t["account_name"])
    col("司机", "other", 8, None, lambda t, f, b, L: t["driver_name"])
    col("备注", "other", 40, None, lambda t, f, b, L: t["remarks"])

    headers = [c[0] for c in C]
    idx = {h: i + 1 for i, h in enumerate(headers)}
    L_ = lambda h: get_column_letter(idx[h])
    ws.cell(row=1, column=1, value=f"车次台账（导出 {now}，共 {len(trips)} 车，不含作废）　"
                                    "表头颜色：蓝=基本信息 绿=利润 黄=利润过程参数 紫=票款 灰=其他；"
                                    "净利润 = 毛服务费 + 量补金额 − 垫资成本 − 调货人提成 − 其他成本").font = Font(name=FONT, bold=True)
    HR = 2
    _hdr(ws, HR, headers, {i + 1: c[1] for i, c in enumerate(C)})
    ws.row_dimensions[HR].height = 42
    first = HR + 1
    checks = []
    for n, t in enumerate(trips):
        r = first + n
        f, b, L = fin_of(t), breakdown_of(t), lanes_of.get(t["id"])
        for i, (h, g, w, fmt, fn) in enumerate(C, 1):
            if fn is None:
                continue
            v = fn(t, f, b, L)
            cell = ws.cell(row=r, column=i, value=v)
            if fmt:
                cell.number_format = fmt
        net = (f"={L_('毛服务费(元)')}{r}+{L_('量补金额(元)')}{r}-{L_('垫资成本(元)·配票=个人垫资')}{r}"
               f"-{L_('调货人提成(元)')}{r}-{L_('其他成本(元)·含切割费')}{r}")
        c = ws.cell(row=r, column=idx["净利润(元)"], value=net)
        c.number_format = MONEY
        c.font = Font(name=FONT, bold=True)
        ws.cell(row=r, column=idx["单吨利润(元/吨)"],
                value=f"=IF({L_('结算吨位(吨)')}{r}=0,0,{L_('净利润(元)')}{r}/{L_('结算吨位(吨)')}{r})").number_format = RATE
        checks.append((t["id"], b["service_fee_income"] + b["reward_amount"] - b["advance_fee_cost"]
                       - b["dispatcher_commission_cost"] - b["other_cost"], b["net_profit"]))
    last = first + len(trips) - 1 if trips else first
    for i, c in enumerate(C, 1):
        ws.column_dimensions[get_column_letter(i)].width = c[2]
    ws.freeze_panes = ws.cell(row=first, column=idx["车/船"])
    if trips:
        ws.auto_filter.ref = f"A{HR}:{get_column_letter(len(C))}{last}"
    # 合计行（SUBTOTAL：筛选后只加可见行，按带票/配票等筛选时合计跟着变）
    tr = last + 2
    ws.cell(row=tr, column=1, value="合计(随筛选变化)").font = Font(name=FONT, bold=True)
    for h in ("结算吨位(吨)", "结算金额(元)", "净利润(元)", "留存利润(元)·不计入净利润", "毛服务费(元)", "量补金额(元)",
              "垫资费(元)", "垫资成本(元)·配票=个人垫资", "调货人提成(元)", "其他成本(元)·含切割费", "票额(估)(元)", "提成已付(元)"):
        c = ws.cell(row=tr, column=idx[h], value=f"=SUBTOTAL(9,{L_(h)}{first}:{L_(h)}{last})")
        c.number_format = TON if "吨位" in h else MONEY
        c.font = Font(name=FONT, bold=True)
    ws.cell(row=tr, column=idx["车号/船号"], value=f"=SUBTOTAL(3,{L_('车次ID')}{first}:{L_('车次ID')}{last})&\"车\"").font = Font(name=FONT, bold=True)
    ws.cell(row=tr, column=idx["单吨利润(元/吨)"],
            value=f"=IF({L_('结算吨位(吨)')}{tr}=0,0,{L_('净利润(元)')}{tr}/{L_('结算吨位(吨)')}{tr})").number_format = RATE

    main = {"ws": ws, "first": first, "last": last, "idx": idx, "L": L_}

    def rng(h):
        return f"'车次台账'!${L_(h)}${first}:${L_(h)}${last}"

    # ---------------- 磅重明细 ----------------
    ws2 = wb.create_sheet("磅重明细")
    h2 = ["车次ID", "结算日期", "车号/船号", "车/船", "路径", "链条", "调货人", "供货厂/车队", "货型大类", "货型",
          "扣杂(吨)", "结算吨数(吨)", "单价(元/吨)", "切割费(元)", "金额(元)"]
    _hdr(ws2, 1, h2)
    r = 2
    for t in trips:
        b = breakdown_of(t)
        for d in extras["details"].get(t["id"], []):
            ws2.append([t["id"], _d(t["settlement_date"]), t["plate_no"], "船" if t["transport_type"] == "ship" else "车",
                        "配票/反向" if b["is_split_path"] else "带票带货", t["template_short"], t["dispatcher_name"], origin_of(t),
                        d["scrap_category"], d["scrap_name"] or "未分货型(混料)", d["deduction"], d["settlement_tonnage"],
                        d["unit_price"], d["cutting_fee"], d["amount"]])
            for col_i, fmt in ((2, "yyyy-mm-dd"), (11, TON), (12, TON), (13, RATE), (14, MONEY), (15, MONEY)):
                ws2.cell(row=r, column=col_i).number_format = fmt
            r += 1
    for i, w in enumerate([7, 11, 13, 6, 9, 9, 8, 11, 14, 14, 9, 11, 10, 10, 13], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A2"
    if r > 2:
        ws2.auto_filter.ref = f"A1:O{r - 1}"

    # ---------------- 票款追踪（每车每环节一行）----------------
    ws4 = wb.create_sheet("票款追踪")
    h4 = ["车次ID", "结算日期", "车号/船号", "路径", "开票路径", "供货厂/车队", "线", "环节", "从", "到",
          "应登记(票额)(元)", "已登记(元)", "待登记(元)", "状态", "账龄(天)", "批次ID"]
    _hdr(ws4, 1, h4)
    r = 2
    today = dt.date.today()
    for t in trips:
        L = lanes_of.get(t["id"])
        if not L or t["status"] not in ("settled", "invoiced", "closed"):
            continue
        b = breakdown_of(t)
        for line_cn, hops in (("开票", L["invoice"]), ("回款", L["payment"])):
            for h in hops:
                since = t["settlement_date"] if line_cn == "开票" else (t["invoiced_date"] or t["settlement_date"])
                age = (today - dt.date.fromisoformat(since[:10])).days if since and h["state"] in ("todo", "partial") else None
                ws4.append([t["id"], _d(t["settlement_date"]), t["plate_no"], "配票/反向" if b["is_split_path"] else "带票带货",
                            L["path_label"], L["origin"], line_cn, h["label"], h["from"], h["to"], h["expected"],
                            h["allocated"], None, STATE_CN.get(h["state"], h["state"]), age,
                            ",".join(str(x) for x in h["batches"]) or None])
                ws4.cell(row=r, column=13, value=f"=IF(OR(N{r}=\"已登记\",LEFT(N{r},2)=\"按状\"),0,K{r}-L{r})")
                for col_i in (11, 12, 13):
                    ws4.cell(row=r, column=col_i).number_format = MONEY
                ws4.cell(row=r, column=2).number_format = "yyyy-mm-dd"
                r += 1
    lane_last = r - 1
    for i, w in enumerate([7, 11, 13, 9, 20, 11, 6, 10, 12, 16, 14, 12, 12, 20, 8, 10], 1):
        ws4.column_dimensions[get_column_letter(i)].width = w
    ws4.freeze_panes = "A2"
    if lane_last >= 2:
        ws4.auto_filter.ref = f"A1:P{lane_last}"

    # ---------------- 批次记录 ----------------
    ws5 = wb.create_sheet("批次记录")
    h5 = ["批次ID", "日期", "类型", "环节", "从", "到", "单号/流水号", "收款账户", "车次数", "批次金额(元)", "备注",
          "", "明细：批次ID", "车次ID", "车号", "分摊金额(元)"]
    _hdr(ws5, 1, h5)
    kind_cn = {"invoice": "开票", "payment": "打款", "commission": "提成"}
    for i, bt in enumerate(extras["batches"], 2):
        ws5.cell(row=i, column=1, value=bt["id"])
        ws5.cell(row=i, column=2, value=bt["batch_date"])
        ws5.cell(row=i, column=3, value=kind_cn.get(bt["kind"], bt["kind"]))
        ws5.cell(row=i, column=4, value=extras["hop_label"].get(bt["hop_key"], bt["hop_key"]))
        ws5.cell(row=i, column=5, value=bt["from_name"])
        ws5.cell(row=i, column=6, value=bt["to_name"])
        ws5.cell(row=i, column=7, value=bt["doc_no"])
        ws5.cell(row=i, column=8, value=bt["account_label"])
        ws5.cell(row=i, column=9, value=f"=COUNTIF($M:$M,A{i})")
        ws5.cell(row=i, column=10, value=f"=SUMIF($M:$M,A{i},$P:$P)").number_format = MONEY
        ws5.cell(row=i, column=11, value=bt["notes"])
    for i, it in enumerate(extras["batch_items"], 2):
        ws5.cell(row=i, column=13, value=it["batch_id"])
        ws5.cell(row=i, column=14, value=it["trip_id"])
        ws5.cell(row=i, column=15, value=it["plate_no"])
        ws5.cell(row=i, column=16, value=it["amount"]).number_format = MONEY
    if not extras["batches"]:
        ws5.cell(row=2, column=1, value="还没有登记过批次（在系统「回款追踪」页勾选车次登记）")
    for i, w in enumerate([7, 11, 6, 10, 12, 16, 16, 18, 7, 13, 20, 2, 10, 8, 12, 13], 1):
        ws5.column_dimensions[get_column_letter(i)].width = w
    ws5.freeze_panes = "A2"

    # ---------------- 票源额度 ----------------
    ws6 = wb.create_sheet("票源额度")
    h6 = ["票源方", "类型", "额度周期", "周期起", "额度上限(元)", "预警线(元)", "已用(元)", "占用比例", "剩余额度(元)", "状态"]
    _hdr(ws6, 1, h6)
    for i, q in enumerate(extras["quota"], 2):
        ws6.cell(row=i, column=1, value=q["name"])
        ws6.cell(row=i, column=2, value="反向自然人" if q["source_type"] == "reverse_individual" else "前端公司")
        ws6.cell(row=i, column=3, value="年度" if q["quota_period"] == "annual" else "月度")
        ws6.cell(row=i, column=4, value=q["period_start"])
        ws6.cell(row=i, column=5, value=q["quota_amount"]).number_format = MONEY
        ws6.cell(row=i, column=6, value=q["warn_threshold"]).number_format = MONEY
        ws6.cell(row=i, column=7, value=q["used_amount"]).number_format = MONEY
        ws6.cell(row=i, column=8, value=f"=IF(N(E{i})=0,0,G{i}/E{i})").number_format = "0.0%"
        ws6.cell(row=i, column=9, value=f"=IF(N(E{i})=0,0,E{i}-G{i})").number_format = MONEY
        ws6.cell(row=i, column=10, value=f'=IF(N(E{i})=0,"未设额度",IF(G{i}>=E{i},"已超额度",IF(AND(N(F{i})>0,G{i}>=F{i}),"超预警线",IF(H{i}>=0.9,"接近额度","正常"))))')
    n6 = len(extras["quota"]) + 2
    ws6.cell(row=n6 + 1, column=1, value="已用 = 本周期内该票源车次的票额(结算金额 − 吨位×垫资费单价)，与系统首页「票源额度水位」同口径。").font = Font(name=FONT, italic=True, color="808080", size=9)
    for i, w in enumerate([14, 11, 8, 11, 14, 14, 14, 9, 14, 10], 1):
        ws6.column_dimensions[get_column_letter(i)].width = w

    # ---------------- 统计汇总（放第二个sheet） ----------------
    ws3 = wb.create_sheet("统计汇总", 1)
    ws3.column_dimensions["A"].width = 26
    for c_ in "BCDEFGHI":
        ws3.column_dimensions[c_].width = 15
    bold = Font(name=FONT, bold=True)
    gray = Font(name=FONT, size=9, italic=True, color="808080")
    ws3.cell(row=1, column=1, value=f"统计汇总（导出 {now}；全部为引用「车次台账」的公式，不含作废）").font = bold
    r = 3
    if not trips:
        ws3.cell(row=r, column=1, value="暂无数据")
    else:
        # 总体 + 利润过程
        ws3.cell(row=r, column=1, value="一、总体与利润构成").font = bold
        r += 1
        rows_ = [
            ("车次数", f"=COUNT({rng('车次ID')})", None),
            ("结算吨位(吨)", f"=SUM({rng('结算吨位(吨)')})", TON),
            ("结算金额(元)", f"=SUM({rng('结算金额(元)')})", MONEY),
            ("  毛服务费(元)", f"=SUM({rng('毛服务费(元)')})", MONEY),
            ("  + 量补金额(元)", f"=SUM({rng('量补金额(元)')})", MONEY),
            ("  − 垫资成本(元)", f"=SUM({rng('垫资成本(元)·配票=个人垫资')})", MONEY),
            ("  − 调货人提成(元)", f"=SUM({rng('调货人提成(元)')})", MONEY),
            ("  − 其他成本(元)", f"=SUM({rng('其他成本(元)·含切割费')})", MONEY),
            ("净利润(元)（上面各项加减）", f"=SUM({rng('净利润(元)')})", MONEY),
            ("单吨利润(元/吨)", f"=IF(B{r + 1}=0,0,B{r + 8}/B{r + 1})", RATE),
            ("留存利润(元)·专项留存，不计入净利润", f"=SUM({rng('留存利润(元)·不计入净利润')})", MONEY),
        ]
        for label, fml, fmt in rows_:
            is_total = label.startswith("净利润")
            ws3.cell(row=r, column=1, value=label).font = bold if is_total else Font(name=FONT)
            c = ws3.cell(row=r, column=2, value=fml)
            if fmt:
                c.number_format = fmt
            if is_total:
                c.font = bold
            r += 1
        r += 1

        dim_headers = ["名称", "车次数", "吨位(吨)", "结算金额(元)", "净利润(元)", "单吨利润(元/吨)", "调货人提成(元)", "留存利润(元)"]

        def dim_table(title, key_header, values, note=None):
            nonlocal r
            ws3.cell(row=r, column=1, value=title).font = bold
            if note:
                ws3.cell(row=r, column=3, value=note).font = gray
            r += 1
            _hdr(ws3, r, dim_headers)
            r += 1
            start = r
            crit = rng(key_header)
            for v in values:
                q = '"' + str(v).replace('"', '""') + '"'
                ws3.cell(row=r, column=1, value=v)
                ws3.cell(row=r, column=2, value=f"=COUNTIFS({crit},{q})")
                ws3.cell(row=r, column=3, value=f"=SUMIFS({rng('结算吨位(吨)')},{crit},{q})").number_format = TON
                ws3.cell(row=r, column=4, value=f"=SUMIFS({rng('结算金额(元)')},{crit},{q})").number_format = MONEY
                ws3.cell(row=r, column=5, value=f"=SUMIFS({rng('净利润(元)')},{crit},{q})").number_format = MONEY
                ws3.cell(row=r, column=6, value=f"=IF(C{r}=0,0,E{r}/C{r})").number_format = RATE
                ws3.cell(row=r, column=7, value=f"=SUMIFS({rng('调货人提成(元)')},{crit},{q})").number_format = MONEY
                ws3.cell(row=r, column=8, value=f"=SUMIFS({rng('留存利润(元)·不计入净利润')},{crit},{q})").number_format = MONEY
                r += 1
            ws3.cell(row=r, column=1, value="小计").font = bold
            for ci, fmt in ((2, None), (3, TON), (4, MONEY), (5, MONEY), (7, MONEY), (8, MONEY)):
                c = ws3.cell(row=r, column=ci, value=f"=SUM({get_column_letter(ci)}{start}:{get_column_letter(ci)}{r - 1})")
                c.font = bold
                if fmt:
                    c.number_format = fmt
            ws3.cell(row=r, column=6, value=f"=IF(C{r}=0,0,E{r}/C{r})").number_format = RATE
            r += 2

        def distinct(fn):
            return sorted({v for v in (fn(t) for t in trips) if v not in (None, "")})

        dim_table("二、按路径（带票带货 / 配票·反向）", "路径", ["配票/反向", "带票带货"])
        dim_table("三、按车/船", "车/船", ["车", "船"])
        dim_table("四、按链条规则", "链条", distinct(lambda t: t["template_short"] or t["template_name"]))
        dim_table("五、按调货人", "调货人", distinct(lambda t: t["dispatcher_name"]), "没填调货人的车次不在此表")
        dim_table("六、按供货厂/车队", "供货厂/车队", distinct(origin_of))
        dim_table("七、按票源方", "票源方", distinct(lambda t: t["invoice_source_name"]), "带票带货(无票源)的车次不在此表")
        dim_table("八、按状态", "状态", [v for v in STATUS_CN.values() if any(STATUS_CN.get(t["status"]) == v for t in trips)])

        # 按月
        ws3.cell(row=r, column=1, value="九、按结算月份").font = bold
        r += 1
        _hdr(ws3, r, ["月份", "车次数", "吨位(吨)", "结算金额(元)", "净利润(元)", "单吨利润(元/吨)"])
        r += 1
        for m in sorted({(t["settlement_date"] or "")[:7] for t in trips if t["settlement_date"]}):
            q = f'"{m}"'
            dcol = rng("结算月份")
            ws3.cell(row=r, column=1, value=m)
            ws3.cell(row=r, column=2, value=f"=COUNTIFS({dcol},{q})")
            ws3.cell(row=r, column=3, value=f"=SUMIFS({rng('结算吨位(吨)')},{dcol},{q})").number_format = TON
            ws3.cell(row=r, column=4, value=f"=SUMIFS({rng('结算金额(元)')},{dcol},{q})").number_format = MONEY
            ws3.cell(row=r, column=5, value=f"=SUMIFS({rng('净利润(元)')},{dcol},{q})").number_format = MONEY
            ws3.cell(row=r, column=6, value=f"=IF(C{r}=0,0,E{r}/C{r})").number_format = RATE
            r += 1
        r += 1

        # 票款环节
        ws3.cell(row=r, column=1, value="十、票款环节（引用「票款追踪」）").font = bold
        ws3.cell(row=r, column=3, value="待登记 = 应登记 − 已登记；历史车次未补登批次前，回款环节会显示为待登记").font = gray
        r += 1
        _hdr(ws3, r, ["环节", "涉及车次", "应登记(元)", "已登记(元)", "待登记(元)", "待登记车次"])
        r += 1
        if lane_last >= 2:
            lr = lambda c_: f"'票款追踪'!${c_}$2:${c_}${lane_last}"
            for hk in HOP_ORDER:
                lbl = extras["hop_label"][hk]
                q = f'"{lbl}"'
                ws3.cell(row=r, column=1, value=lbl)
                ws3.cell(row=r, column=2, value=f"=COUNTIFS({lr('H')},{q})")
                ws3.cell(row=r, column=3, value=f"=SUMIFS({lr('K')},{lr('H')},{q})").number_format = MONEY
                ws3.cell(row=r, column=4, value=f"=SUMIFS({lr('L')},{lr('H')},{q})").number_format = MONEY
                ws3.cell(row=r, column=5, value=f"=SUMIFS({lr('M')},{lr('H')},{q})").number_format = MONEY
                ws3.cell(row=r, column=6, value=f'=COUNTIFS({lr("H")},{q},{lr("M")},">0")')
                r += 1
        r += 1

        # 调货人提成
        ws3.cell(row=r, column=1, value="十一、调货人提成 应付/已付/未付").font = bold
        r += 1
        _hdr(ws3, r, ["调货人", "车次数", "吨位(吨)", "应付提成(元)", "已付(元)", "未付(元)"])
        r += 1
        for dname in distinct(lambda t: t["dispatcher_name"]):
            q = '"' + dname.replace('"', '""') + '"'
            crit = rng("调货人")
            ws3.cell(row=r, column=1, value=dname)
            ws3.cell(row=r, column=2, value=f"=COUNTIFS({crit},{q})")
            ws3.cell(row=r, column=3, value=f"=SUMIFS({rng('结算吨位(吨)')},{crit},{q})").number_format = TON
            ws3.cell(row=r, column=4, value=f"=SUMIFS({rng('调货人提成(元)')},{crit},{q})").number_format = MONEY
            ws3.cell(row=r, column=5, value=f"=SUMIFS({rng('提成已付(元)')},{crit},{q})").number_format = MONEY
            ws3.cell(row=r, column=6, value=f"=D{r}-E{r}").number_format = MONEY
            r += 1
        r += 1

        # 质量
        ws3.cell(row=r, column=1, value="十二、质量：各货型（引用「磅重明细」）").font = bold
        r += 1
        _hdr(ws3, r, ["货型", "吨位(吨)", "金额(元)", "均价(元/吨)", "扣杂(吨)"])
        r += 1
        dl = ws2.max_row
        if dl >= 2:
            dr = lambda c_: f"'磅重明细'!${c_}$2:${c_}${dl}"
            names = sorted({(ws2.cell(row=i, column=10).value) for i in range(2, dl + 1)} - {None})
            for nm in names:
                q = '"' + str(nm).replace('"', '""') + '"'
                ws3.cell(row=r, column=1, value=nm)
                ws3.cell(row=r, column=2, value=f"=SUMIFS({dr('L')},{dr('J')},{q})").number_format = TON
                ws3.cell(row=r, column=3, value=f"=SUMIFS({dr('O')},{dr('J')},{q})").number_format = MONEY
                ws3.cell(row=r, column=4, value=f"=IF(B{r}=0,0,C{r}/B{r})").number_format = RATE
                ws3.cell(row=r, column=5, value=f"=SUMIFS({dr('K')},{dr('J')},{q})").number_format = TON
                r += 1
    ws3.freeze_panes = "A3"

    for s in wb.worksheets:
        _fonts(s)
    return wb, checks


def _d(v):
    try:
        return dt.date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return v


def _per_ton(amount, t):
    ton = t["settlement_tonnage_total"] or 0
    return (amount or 0) / ton if ton else None


def _hop_state(L, key):
    if not L:
        return None
    for h in L["invoice"] + L["payment"]:
        if h["key"] == key:
            s = STATE_CN.get(h["state"], h["state"])
            return s if h["state"] not in ("done", "partial") else f"{s} {h['allocated']:,.2f}"
    return "—"


def _overrides_text(f):
    items = [i for i in (f.get("rate_items") or []) if i.get("overridden")]
    return "；".join(f"{i['label']} {i['default']:g}→{i['value']:g}{i['unit']}" for i in items) or None
