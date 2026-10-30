"""报价单。

从原 app.py 拆出（v2.0.1）。共用的常量和工具函数都在 core.py。"""
from core import *  # noqa: F401,F403


# ---------------------------------------------------------------------------
# 报价单 API
# ---------------------------------------------------------------------------
@app.route("/api/quote_sheets", methods=["GET", "POST"])
def quote_sheets():
    conn = get_db()
    if request.method == "GET":
        mill_id = request.args.get("mill_id")
        sql = """SELECT q.*, m.name AS mill_name FROM quote_sheets q
                  LEFT JOIN steel_mills m ON q.mill_id=m.id"""
        args = []
        if mill_id:
            sql += " WHERE q.mill_id=?"
            args.append(mill_id)
        sql += " ORDER BY q.effective_at DESC"
        rows = conn.execute(sql, args).fetchall()
        return jsonify(rows_to_list(rows))
    data = request.get_json(force=True) or {}
    cur = conn.execute(
        "INSERT INTO quote_sheets (mill_id, period_label, effective_at, footer_notes) VALUES (?,?,?,?)",
        (data.get("mill_id"), data.get("period_label"), data.get("effective_at"), data.get("footer_notes")),
    )
    conn.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/quote_sheets/<int:sheet_id>", methods=["PUT"])
def update_quote_sheet(sheet_id):
    conn = get_db()
    data = request.get_json(force=True) or {}
    cols = [c for c in ["mill_id", "period_label", "effective_at", "footer_notes"] if c in data]
    if not cols:
        return jsonify({"error": "empty payload"}), 400
    set_clause = ",".join(f"{c}=?" for c in cols)
    vals = [data[c] for c in cols] + [sheet_id]
    conn.execute(f"UPDATE quote_sheets SET {set_clause} WHERE id=?", vals)
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/quote_sheets/<int:sheet_id>", methods=["GET"])
def get_quote_sheet(sheet_id):
    conn = get_db()
    sheet = conn.execute(
        """SELECT q.*, m.name AS mill_name FROM quote_sheets q
           LEFT JOIN steel_mills m ON q.mill_id=m.id WHERE q.id=?""",
        (sheet_id,),
    ).fetchone()
    if not sheet:
        return jsonify({"error": "not found"}), 404
    items = conn.execute(
        """SELECT qi.*, st.name AS scrap_name, st.category, st.example_desc, st.size_requirement, st.thickness_requirement
           FROM quote_items qi LEFT JOIN scrap_types st ON qi.scrap_type_id = st.id
           WHERE qi.quote_sheet_id=?""",
        (sheet_id,),
    ).fetchall()
    prev_sheet = conn.execute(
        """SELECT id FROM quote_sheets WHERE mill_id=? AND effective_at < ? ORDER BY effective_at DESC LIMIT 1""",
        (sheet["mill_id"], sheet["effective_at"]),
    ).fetchone()
    prev_prices = {}
    if prev_sheet:
        prev_items = conn.execute(
            "SELECT scrap_type_id, price FROM quote_items WHERE quote_sheet_id=?", (prev_sheet["id"],)
        ).fetchall()
        prev_prices = {r["scrap_type_id"]: r["price"] for r in prev_items}

    items_out = []
    for it in items:
        d = dict(it)
        prev_price = prev_prices.get(it["scrap_type_id"])
        d["prev_price"] = prev_price
        d["price_change"] = (d["price"] - prev_price) if (d["price"] is not None and prev_price is not None) else None
        items_out.append(d)

    return jsonify({"sheet": dict(sheet), "items": items_out})


@app.route("/api/quote_sheets/<int:sheet_id>/items", methods=["PUT"])
def upsert_quote_items(sheet_id):
    conn = get_db()
    data = request.get_json(force=True) or {}
    items = data.get("items", [])
    conn.execute("DELETE FROM quote_items WHERE quote_sheet_id=?", (sheet_id,))
    for it in items:
        conn.execute(
            "INSERT INTO quote_items (quote_sheet_id, scrap_type_id, price, is_suspended) VALUES (?,?,?,?)",
            (sheet_id, it.get("scrap_type_id"), it.get("price"), int(bool(it.get("is_suspended")))),
        )
    conn.commit()
    return jsonify({"ok": True})
