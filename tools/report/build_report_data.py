"""生成经营报告用的数据 JSON，并套进模板得到可打印的 HTML。
用法：PYTHONPATH=<项目目录> SCRAPMETAL_TEST_DB=<数据库副本> python3 build_report_data.py 输出.html [财务利润合计]
然后：SRC=输出.html OUT=报告.pdf NODE_PATH=$(npm root -g) node render_pdf.js"""
import json, os, sqlite3, sys
import app, db
here = os.path.dirname(os.path.abspath(__file__))
c = sqlite3.connect(db.DB_PATH); c.row_factory = sqlite3.Row
P = {'配票': '配票', '反向': '反向', '直签带票': '带票', '户头通道': '带票', '调货对接': '带票'}
alias = {r[0] for r in c.execute('select name from processing_bases where is_dispatcher_alias=1')}
trips = []
for t in c.execute('''select t.*, b.name bname, d.name dname, s.name sname, ft.short_name tpl, ft.calc_family fam from trips t
   left join processing_bases b on t.base_id=b.id left join dispatchers d on t.dispatcher_id=d.id left join invoice_sources s on t.invoice_source_id=s.id
   left join flow_templates ft on t.template_id=ft.id where t.status!='voided' and t.settlement_tonnage_total>0'''):
    f = app._trip_finance_full(c, t); live = app._compute_finance(c, t); b = t['bname']
    trips.append(dict(id=t['id'], p=t['plate_no'] or '（车号待补）', ty='船' if t['transport_type'] == 'ship' else '车', d=t['settlement_date'],
        ton=round(t['settlement_tonnage_total'], 3), amt=round(t['settlement_amount_total'], 2), path=P.get(t['tpl'], t['tpl']), src=t['sname'] or '自带票',
        disp=t['dname'] or '无调货人', base=(f'{b}（自有货源）' if b in alias else (b or '（未填）')), net=round(f['net_profit'], 2),
        rw=round(f.get('reward_profit') or 0, 2), cm=round(f.get('dispatcher_commission_cost') or 0, 2),
        padv=round(live.get('personal_advance') or 0, 2) if t['fam'] == 'split' else None,
        svc=round(f.get('service_fee_income') or 0, 2) if t['fam'] != 'split' else None,
        sadv=round(f.get('advance_fee_cost') or 0, 2) if t['fam'] != 'split' else None,
        resv=round(f.get('invoice_diff_profit') or 0, 2), rwu=round(f.get('reward_unit_price') or 0, 2)))
cl = app.app.test_client()
g = lambda u: cl.get(u).get_json()
m = g('/api/dashboard/mill_periods')[0]
data = dict(trips=trips, quota=g('/api/dashboard/quota_levels'), reserved=g('/api/dashboard/reserved_profit')['total_reserved_profit'],
    commission=[dict(name=d['name'], payable=d['payable'], paid=d['paid'], unpaid=d['unpaid']) for d in g('/api/analytics/commission')['by_dispatcher']],
    period=dict(start=m['period_start'], end=m['period_end'], ton=m['cumulative_tonnage'], trips=m['trip_count'], profit=m['profit'], ppt=m['profit_per_ton'],
                rwp=m['current_reward_price'], prev_start=m['prev_period_start'], prev_end=m['prev_period_end'], prev_ton=m['prev_tonnage'],
                prev_trips=m['prev_trip_count'], prev_profit=m['prev_profit'], prev_ppt=m['prev_profit_per_ton'], prev_note=m.get('prev_special_note')),
    tiers=[dict(min=x['min_tonnage'], price=x['unit_price']) for x in g('/api/mill_reward_tiers')],
    finance=dict(fin_total=float(sys.argv[2]) if len(sys.argv) > 2 else sum(t['net'] for t in trips), rows=len(trips), matched=len(trips)))
tpl = open(os.path.join(here, 'report_pdf_tpl.html'), encoding='utf-8').read()
html = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body>' + \
       tpl.replace('__DATA__', json.dumps(data, ensure_ascii=False, separators=(',', ':'))) + '</body></html>'
open(sys.argv[1], 'w', encoding='utf-8').write(html)
print('车次', len(trips), '利润', round(sum(t['net'] for t in trips), 2))
