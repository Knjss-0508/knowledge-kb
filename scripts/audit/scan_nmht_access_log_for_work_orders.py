"""Judge every blind-label work-order number against the real upstream traffic
recorded by the zzdy reverse proxy (no cookie needed).

Signature (validated on 2026-10-08 and again on 2026-10-10):
  * GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=<n> -> body > 300 B  => real 工单号
  * GET /nmhtapi/im/history?conversationId=<n>              -> body > 300 B  => 会话号 with chat
  * body 123/124 B means "查不到" for the work-order detail API.

用法（在生产服务器上跑，只读日志）：
  1. 先在 kb-backend 容器里导出号码清单（见 scripts/audit/dump_blind_label_pool_ids.py）：
     docker cp dump_blind_label_pool_ids.py kb-backend:/tmp/ && \
     docker exec -e PYTHONPATH=/app kb-backend python /tmp/dump_blind_label_pool_ids.py
     docker cp kb-backend:/tmp/pool-ids.json /tmp/pool-ids.json
  2. 再在宿主机上跑本脚本：python3 scan_nmht_access_log_for_work_orders.py
     输出 /tmp/pool-verdict.json 与 /tmp/pool-verdict.tsv（本次扫描：zzdy 日志 32,523,205 行 /
     命中 19,834 行 / 45 秒；nmht-paths 12,526,483 行 / 命中 1,558 行 / 23 秒；
     结论 session-number-only 216、real-work-order 1,076）。
  3. 本机留存的原始判定数据快照：outputs/blind-label-work-order-audit-20261010/pool-verdict.{json,tsv}。

结果解读见 docs/blind-label-work-order-number-audit-20261010.md。
"""

import json
import re
import sys
import time

LOGS = [
    "/www/wwwlogs/zzdy.powerzhuan.cn.log",
    "/root/nmht-paths-20261008.txt",
]
POOL = "/tmp/pool-ids.json"
OUT_JSON = "/tmp/pool-verdict.json"
OUT_TSV = "/tmp/pool-verdict.tsv"
THRESHOLD = 300

with open(POOL, encoding="utf-8") as handle:
    pool = json.load(handle)

wanted = {}
for item in pool:
    for key in ("question_form_id", "conversation_id"):
        value = item.get(key)
        if value:
            wanted[str(value)] = True
print("pool rows:", len(pool), "distinct numbers:", len(wanted), flush=True)

line_re = re.compile(r'"(GET|POST) (/[^"]*)" (\d{3}) (\d+)')
qfd_re = re.compile(r"questionFormId=(\d+)")
scene_re = re.compile(r"sceneType=(\d)")
cid_re = re.compile(r"conversationId=(\d+)")

stats = {}


def bucket(number):
    entry = stats.get(number)
    if entry is None:
        entry = {
            "qfd_n": 0,
            "qfd_max": -1,
            "qfd_ok_n": 0,
            "qfd_scene1_max": -1,
            "qfd_scene2_max": -1,
            "qfd_min": 10 ** 9,
            "chat_n": 0,
            "chat_max": -1,
            "chat_ok_n": 0,
        }
        stats[number] = entry
    return entry


for path in LOGS:
    started = time.time()
    scanned = 0
    kept = 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            scanned += 1
            if "questionFormId=" not in line and "conversationId=" not in line:
                continue
            match = line_re.search(line)
            if not match:
                continue
            url = match.group(2)
            if "questionFormId=" in url:
                found = qfd_re.search(url)
                if not found:
                    continue
                number = found.group(1)
                if number not in wanted:
                    continue
                size = int(match.group(4))
                scene = scene_re.search(url)
                scene = scene.group(1) if scene else "?"
                entry = bucket(number)
                entry["qfd_n"] += 1
                entry["qfd_max"] = max(entry["qfd_max"], size)
                entry["qfd_min"] = min(entry["qfd_min"], size)
                if size > THRESHOLD:
                    entry["qfd_ok_n"] += 1
                if scene == "1":
                    entry["qfd_scene1_max"] = max(entry["qfd_scene1_max"], size)
                elif scene == "2":
                    entry["qfd_scene2_max"] = max(entry["qfd_scene2_max"], size)
                kept += 1
            else:
                found = cid_re.search(url)
                if not found:
                    continue
                number = found.group(1)
                if number not in wanted:
                    continue
                size = int(match.group(4))
                entry = bucket(number)
                entry["chat_n"] += 1
                entry["chat_max"] = max(entry["chat_max"], size)
                if size > THRESHOLD:
                    entry["chat_ok_n"] += 1
                kept += 1
            if scanned % 2000000 == 0:
                print(
                    "  %s scanned=%d matched=%d elapsed=%.0fs"
                    % (path, scanned, kept, time.time() - started),
                    flush=True,
                )
    print(
        "%s scanned=%d matched=%d elapsed=%.0fs" % (path, scanned, kept, time.time() - started),
        flush=True,
    )

verdicts = []
for item in pool:
    number = str(item.get("question_form_id") or item.get("conversation_id") or "")
    entry = stats.get(number)
    if entry is None:
        verdict = "no-log-record"
    elif entry["qfd_max"] > THRESHOLD:
        verdict = "real-work-order"
    elif entry["chat_max"] > THRESHOLD:
        verdict = "session-number-only"
    elif entry["qfd_n"] or entry["chat_n"]:
        verdict = "log-record-without-data"
    else:
        verdict = "no-log-record"
    row = dict(item)
    row["number"] = number
    row["verdict"] = verdict
    row["same_number_for_conversation"] = str(item.get("conversation_id")) == number
    row.update(entry or {})
    verdicts.append(row)

order = {
    "session-number-only": 0,
    "log-record-without-data": 1,
    "real-work-order": 2,
    "no-log-record": 3,
}
verdicts.sort(key=lambda r: (order.get(r["verdict"], 9), str(r.get("created_at") or "")))

summary = {}
for row in verdicts:
    summary[row["verdict"]] = summary.get(row["verdict"], 0) + 1

print("VERDICT SUMMARY:", json.dumps(summary, ensure_ascii=False), flush=True)

with open(OUT_JSON, "w", encoding="utf-8") as handle:
    json.dump({"summary": summary, "rows": verdicts}, handle, ensure_ascii=False, default=str)

columns = [
    "number",
    "verdict",
    "same_number_for_conversation",
    "created_at",
    "source_created_at",
    "status",
    "qfd_n",
    "qfd_max",
    "qfd_min",
    "qfd_scene1_max",
    "qfd_scene2_max",
    "chat_n",
    "chat_max",
    "assignments",
    "annotations",
    "id",
    "query_text",
]
with open(OUT_TSV, "w", encoding="utf-8") as handle:
    handle.write("\t".join(columns) + "\n")
    for row in verdicts:
        handle.write(
            "\t".join(str(row.get(c, "")).replace("\t", " ").replace("\n", " ") for c in columns)
            + "\n"
        )

print("wrote", OUT_JSON, "and", OUT_TSV, flush=True)
sys.exit(0)
