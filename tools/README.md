# tools/ — 对账与经营报告（2026-09-22 起常用）

recon/（财务台账对账）
- extract_trips.py      导出系统车次+利润各环节 → JSON
- match_ledger.py       财务「发货记录表」逐行配对系统车次 → match.json
- build_recon_xlsx.py   生成分步对账 Excel（颜色标注）

report/（经营报告 PDF，手机宽 120mm，微信能打开）
- build_report_data.py  从数据库生成数据并套模板 → HTML
- report_pdf_tpl.html   报告模板（静态 SVG 图表，Noto Sans CJK 字体）
- render_pdf.js         Playwright 渲染 PDF

参考脚本：../sync_20260922_finance_v6.py（按财务同步字段/规则）、../sync_20260922_attr_v6.py（按财务统一调货人/供货厂分类）、../add_20260921_yandongpeng.py（补录车次）
