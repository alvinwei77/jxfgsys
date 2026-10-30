"""导出系统全部车次(不含作废)+利润各环节到 JSON。用法：
  PYTHONPATH=<项目目录> SCRAPMETAL_TEST_DB=<数据库副本> python3 extract_trips.py 输出.json
只读，不改数据。"""
import app, sqlite3, db, json, sys
c=sqlite3.connect(db.DB_PATH); c.row_factory=sqlite3.Row
rows=[]
for t in c.execute('''select t.*, b.name bname, d.name dname, s.name sname, ft.short_name tpl, ft.calc_family fam from trips t left join processing_bases b on t.base_id=b.id left join dispatchers d on t.dispatcher_id=d.id left join invoice_sources s on t.invoice_source_id=s.id left join flow_templates ft on t.template_id=ft.id where t.status!='voided' order by coalesce(t.settlement_date,t.order_date), t.id'''):
    f=app._trip_finance_full(c,t); live=app._compute_finance(c,t)
    rows.append(dict(id=t['id'],plate=t['plate_no'],type=t['transport_type'],od=t['order_date'],sd=t['settlement_date'],ton=t['settlement_tonnage_total'],amt=t['settlement_amount_total'],base=t['bname'],disp=t['dname'],src=t['sname'],tpl=t['tpl'],fam=t['fam'],status=t['status'],
      net=f.get('net_profit'),reward_u=f.get('reward_unit_price'),reward=f.get('reward_profit'),adv=f.get('advance_fee_cost'),comm=f.get('dispatcher_commission_cost'),resv=f.get('invoice_diff_profit'),locked=f.get('is_locked'),cut=t['cutting_fee_total'],other=f.get('other_cost'),
      rate_in=f.get('rate_in'),rate_out=f.get('rate_out'),svc=f.get('service_fee_income'),svc_rate=f.get('service_fee_value'),overrides=t['finance_overrides'],
      give=live.get('give_customer'),adv_raw=live.get('advance_fee_raw'),front=live.get('front_invoice_amount'),front_paid=live.get('front_actual_paid'),padv=live.get('personal_advance'),live_net=live.get('net_profit')))
json.dump(rows,open(sys.argv[1],'w'),ensure_ascii=False,default=str)
print(len(rows), round(sum(r['net'] or 0 for r in rows),2), 'snapshot≠live:', [(r['id'],round(r['net'],2),round(r['live_net'],2)) for r in rows if abs((r['net'] or 0)-(r['live_net'] or 0))>0.01])
