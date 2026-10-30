"""2026-09-21 用户确认：苏通源货6666(9/18船)走反向，但是【新的反向个人】，
有自己独立的350万年度额度，计入尹鹏飞名下，不占闫东朋那个反向的额度。

1. 原票源"反向"(绑定供货厂闫东朋) 改名"反向-闫东朋"，id/数据不变；
2. 新建票源"反向-尹鹏飞"：反向自然人，点位/额度/预警沿用原反向(13.8%、年度350万、预警180万)，绑定供货厂尹鹏飞；
3. 苏通源货6666 的票源改为"反向-尹鹏飞"；
4. 给新反向补上"本期垫资费已含在反向票点里"的 0 元/吨档（与原反向一致，见 migrate_reverse_advance_fee.py）。
幂等；运行前整库备份。用法：python3 fix_20260921_reverse_yinpengfei.py [db_path]
"""
import os, sys, shutil, sqlite3, datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "scrapmetal.db")
NOTE = ("反向票源的垫资费本期已算进反向票点成本(反向是欧冶介绍的渠道)，所以对反向不再单独扣；"
        "垫资费本身一直都有。哪个月不含了，就给这个户头+反向新增一条23元/吨并写生效日期。")


def main(db):
    b = f"{db}.bak_before_fix_reverse_yinpengfei_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(db, b); print("备份:", b)
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=TRUNCATE")
    old = c.execute("SELECT * FROM invoice_sources WHERE id=1").fetchone()
    assert old["source_type"] == "reverse_individual", dict(old)
    if old["name"] == "反向":
        c.execute("UPDATE invoice_sources SET name='反向-闫东朋' WHERE id=1"); print("改名：反向 → 反向-闫东朋")
    yin = c.execute("SELECT id FROM processing_bases WHERE name='尹鹏飞'").fetchone()["id"]
    new = c.execute("SELECT id FROM invoice_sources WHERE name='反向-尹鹏飞'").fetchone()
    if new:
        nid = new["id"]; print("已存在 反向-尹鹏飞 id=", nid)
    else:
        nid = c.execute(
            """INSERT INTO invoice_sources (name, source_type, tax_type, rate_point, quota_period, quota_amount,
               warn_threshold, bound_mill_id, bound_base_id, settlement_type, notes)
               VALUES ('反向-尹鹏飞','reverse_individual',?,?,?,?,?,?,?,?,?)""",
            (old["tax_type"], old["rate_point"], old["quota_period"], old["quota_amount"], old["warn_threshold"],
             old["bound_mill_id"], yin, old["settlement_type"] or "direct",
             "2026-09-21新增：新的反向个人，独立350万年度额度，计入尹鹏飞名下（用户确认）；点位/额度/预警沿用原反向，如有不同请在档案里改")).lastrowid
        print("新建票源 反向-尹鹏飞 id=", nid)
    for a in c.execute("SELECT id FROM accounts").fetchall():
        if not c.execute("""SELECT 1 FROM rate_links WHERE relation_type='account_advance_fee' AND primary_entity_id=?
                            AND secondary_entity_id=?""", (a["id"], nid)).fetchone():
            c.execute("""INSERT INTO rate_links (relation_type, primary_entity_type, primary_entity_id, secondary_entity_type,
                         secondary_entity_id, rate_value, rate_unit, effective_from, notes)
                         VALUES ('account_advance_fee','account',?,'invoice_source',?,0,'per_ton','2020-01-01',?)""",
                      (a["id"], nid, NOTE))
            print("补垫资费0档：户头", a["id"], "× 反向-尹鹏飞")
    n = c.execute("""UPDATE trips SET invoice_source_id=?, updated_at=datetime('now','localtime')
                     WHERE plate_no='苏通源货6666' AND settlement_date='2026-09-18' AND invoice_source_id=1""", (nid,)).rowcount
    print("苏通源货6666 票源改为 反向-尹鹏飞：", n, "车")
    c.commit()
    for r in c.execute("SELECT id,name,rate_point,quota_amount,warn_threshold,bound_base_id FROM invoice_sources"):
        print(tuple(r))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
