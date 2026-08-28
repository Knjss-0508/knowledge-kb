"""本地第二部分接口模拟器。

只监听 127.0.0.1，用于验证日期范围 -> Answer Hub -> 本地候选价值复核链路。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


RECORD = {
    "工单ID": "local-test-card-slot-waterproof-v6",
    "聊天内容": "客户咨询卡槽防水标测试：卡槽位置的防水标出现变化，应该如何核验和回复？",
    "产品类型": "手机",
    "分析时间": "2026-08-07T10:00:00+08:00",
    "历史实际回复": "请先确认防水标的具体位置和颜色变化，再结合设备型号和检测结果判断。",
    "参考话术": "建议先核对卡槽防水标状态，并说明需要以实际检测结果为准。",
    "核心问题": "卡槽防水标变化如何核验",
    "判定结论": "仅凭防水标变化不能替代完整检测，需要结合实际检测结果判断。",
    "回收业务层级": "自营回收",
    "上传者": "本地第二部分模拟接口",
    "ai_result": {},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/records":
            self.send_error(404)
            return
        query = parse_qs(parsed.query)
        payload = {
            "data": {
                "items": [RECORD],
                "next_cursor": "",
                "has_more": False,
                "received_from": query.get("from", [""])[0],
                "received_to": query.get("to", [""])[0],
            }
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print("[LOCAL-SECOND-PART] " + (format % args))


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8799), Handler)
    print("local second-part mock listening on http://127.0.0.1:8799/records", flush=True)
    server.serve_forever()
