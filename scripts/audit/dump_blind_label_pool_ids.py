"""Dump every blind-label work order with the context needed to judge its number.

用法（只读，必须在 kb-backend 容器内运行，因为要复用容器的数据库配置）：
  docker cp scripts/audit/dump_blind_label_pool_ids.py kb-backend:/tmp/
  docker exec -e PYTHONPATH=/app kb-backend python /tmp/dump_blind_label_pool_ids.py
  docker cp kb-backend:/tmp/pool-ids.json /tmp/pool-ids.json
随后用 scripts/audit/scan_nmht_access_log_for_work_orders.py 在宿主机上判定号码真伪，
判定口径与结论见 docs/blind-label-work-order-number-audit-20261010.md。
"""

import json

from sqlalchemy import text
from app.core.database import SessionLocal

db = SessionLocal()


def rows(sql, params=None):
    return [dict(r._mapping) for r in db.execute(text(sql), params or {}).fetchall()]


tables = rows(
    "select table_name from information_schema.tables"
    " where table_schema='public' and table_name like 'blind_label%' order by 1"
)
print("tables:", [t["table_name"] for t in tables])

wo_cols = [
    r["column_name"]
    for r in rows(
        "select column_name from information_schema.columns"
        " where table_name='blind_label_work_orders' order by ordinal_position"
    )
]
print("work order columns:", wo_cols)

want = [
    "id",
    "question_form_id",
    "conversation_id",
    "source_event_id",
    "status",
    "category_id",
    "category_label",
    "query_text",
    "source_created_at",
    "created_at",
]
sel = [c for c in want if c in wo_cols]
items = rows(
    "select " + ", ".join(sel) + " from blind_label_work_orders order by created_at"
)
print("work orders:", len(items))

# assignment / annotation counts, discovered defensively
for table, label in (
    ("blind_label_assignments", "assignments"),
    ("blind_label_annotations", "annotations"),
):
    try:
        fk = [
            r["column_name"]
            for r in rows(
                "select column_name from information_schema.columns"
                " where table_name=:t and column_name like '%work_order%' order by ordinal_position",
                {"t": table},
            )
        ]
        if not fk:
            print(label, "-> no work_order column")
            continue
        fk = fk[0]
        counts = {
            r["k"]: r["n"]
            for r in rows(f"select {fk} as k, count(*) as n from {table} group by 1")
        }
        for item in items:
            item[label] = counts.get(item["id"], 0)
        print(label, "via", fk, "total", sum(counts.values()))
    except Exception as exc:  # noqa: BLE001
        print(label, "ERROR", type(exc).__name__, str(exc)[:160])

for item in items:
    if item.get("query_text"):
        item["query_text"] = item["query_text"][:100]

with open("/tmp/pool-ids.json", "w", encoding="utf-8") as handle:
    json.dump(items, handle, ensure_ascii=False, default=str)

print("wrote /tmp/pool-ids.json")
db.close()
