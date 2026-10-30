"""开发自检：把所有 GET 接口的返回内容拍成快照，重构前后对比，确保一字不差。
用法：SCRAPMETAL_TEST_DB=<副本db> python3 tools_golden_master.py <输出json>
只发 GET 请求，不改数据；但为保险起见务必指向数据库副本。"""
import json
import sys
import sqlite3
import hashlib
import os

import app as A

db_path = os.environ["SCRAPMETAL_TEST_DB"]
c = sqlite3.connect(db_path)
c.row_factory = sqlite3.Row
trip_ids = [r[0] for r in c.execute("SELECT id FROM trips ORDER BY id")]
tpl_ids = [r[0] for r in c.execute("SELECT id FROM flow_templates ORDER BY id")]
node_ids = [r[0] for r in c.execute("SELECT id FROM template_nodes ORDER BY id LIMIT 8")]
batch_ids = [r[0] for r in c.execute("SELECT id FROM batches ORDER BY id")] if c.execute(
    "SELECT name FROM sqlite_master WHERE name='batches'").fetchone() else []
sheet_ids = [r[0] for r in c.execute("SELECT id FROM quote_sheets ORDER BY id LIMIT 5")]
tables = sorted(A.ARCHIVE_TABLES.keys())

urls = []
for r in A.app.url_map.iter_rules():
    if "GET" not in r.methods or r.endpoint in ("static", "uploaded_file"):
        continue
    if r.rule in ("/api/system/backups",):
        continue  # 备份列表随文件时间变，不纳入
    args = r.arguments
    if not args:
        urls.append(r.rule)
    elif args == {"trip_id"}:
        urls += [r.rule.replace("<int:trip_id>", str(i)) for i in trip_ids]
    elif args == {"tid"}:
        urls += [r.rule.replace("<int:tid>", str(i)) for i in tpl_ids]
    elif args == {"node_id"}:
        urls += [r.rule.replace("<int:node_id>", str(i)) for i in node_ids]
    elif args == {"batch_id"}:
        urls += [r.rule.replace("<int:batch_id>", str(i)) for i in batch_ids]
    elif args == {"sheet_id"}:
        urls += [r.rule.replace("<int:sheet_id>", str(i)) for i in sheet_ids]
    elif args == {"table"}:
        urls += [r.rule.replace("<table>", t) for t in tables]
    elif args == {"table", "item_id"}:
        for t in tables:
            ids = [x[0] for x in c.execute(f"SELECT id FROM {t} ORDER BY id LIMIT 3")]
            urls += [f"/api/{t}/{i}" for i in ids]
    else:
        print("跳过", r.rule, file=sys.stderr)

# 带查询参数的常用组合
extra = [
    "/api/trips?status_in=settled,invoiced", "/api/trips?exclude_voided=1", "/api/trips?family=split",
    "/api/trips?ids=1,2,3", "/api/reports/summary?group=month", "/api/analytics/trend?granularity=week",
    "/trips?open=5", "/system?tab=recycle", "/tracking?hop=in",
]
urls += extra

out = {}
client = A.app.test_client()
for u in sorted(set(urls)):
    resp = client.get(u)
    body = resp.get_data()
    ctype = resp.headers.get("Content-Type", "")
    if "json" in ctype:
        try:
            body = json.dumps(json.loads(body), sort_keys=True, ensure_ascii=False).encode()
        except ValueError:
            pass
    elif "spreadsheet" in ctype or "octet" in ctype:
        body = str(len(body)).encode()  # xlsx 内含时间戳，只比较能否生成
    out[u] = {"status": resp.status_code, "md5": hashlib.md5(body).hexdigest(), "len": len(body)}
    if os.environ.get("GM_DUMP") and u in os.environ["GM_DUMP"].split(","):
        out[u]["body"] = body.decode("utf-8", "replace")

json.dump(out, open(sys.argv[1], "w"), ensure_ascii=False, indent=1, sort_keys=True)
print(len(out), "个接口已快照；非200：", {k: v["status"] for k, v in out.items() if v["status"] != 200})
