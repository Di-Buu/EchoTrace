"""Deterministic Docker wiring fixture. NOT an AI quality implementation."""

import json
import os
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar


class Handler(BaseHTTPRequestHandler):
    control_lock = threading.Lock()
    control: ClassVar[dict] = {
        "fail_next": 0,
        "fail_status": 429,
        "bad_chat": False,
        "delay_seconds": None,
        "query_axis": "record",
        "chat_usage_tokens": 10,
    }
    requests: ClassVar[int] = 0

    def log_message(self, *args):
        pass  # Do not log synthetic or private prompt bodies.

    def do_GET(self):
        if self.path == "/__stats":
            with self.control_lock:
                count = type(self).requests
            self.send_json({"mock": True, "requests": count})
            return
        self.send_json({"mock": True})

    def send_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # A cancelled test request must not dump its payload into logs.

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/__control":
            with self.control_lock:
                for key in self.control:
                    if key in data:
                        self.control[key] = data[key]
            self.send_json({"mock": True, "configured": True})
            return
        with self.control_lock:
            type(self).requests += 1
            mode = dict(self.control)
            if mode["fail_next"] > 0:
                self.control["fail_next"] -= 1
        time.sleep(
            float(
                mode["delay_seconds"]
                if mode["delay_seconds"] is not None
                else os.getenv("MOCK_DELAY_SECONDS", "0")
            )
        )
        if mode["fail_next"] > 0:
            self.send_json(
                {"error": "synthetic transient failure"}, int(mode["fail_status"])
            )
            return
        if mode["bad_chat"] and self.path.endswith("/chat/completions"):
            self.send_json(
                {
                    "model": "mock",
                    "choices": [{"message": {"content": "not valid JSON"}}],
                    "usage": {"total_tokens": 1},
                }
            )
            return
        if self.path.endswith("/embeddings"):
            self.send_json(
                {
                    "model": "mock",
                    "data": [{"embedding": [1.0] + [0.0] * 1023}],
                    "usage": {"total_tokens": 1},
                }
            )
            return
        messages = data.get("messages", [])
        payload = (
            json.loads(messages[-1]["content"])
            if data.get("response_format") or data.get("tools")
            else {}
        )
        if data.get("tools"):
            seen = {o["tool"] for o in payload.get("observations", [])}
            tool = "search_current" if "search_current" not in seen else "finish"
            if (
                payload["plan"]["kind"] == "change"
                and "search_current" in seen
                and "search_comparison" not in seen
            ):
                tool = "search_comparison"
            if (
                tool == "finish"
                and payload["plan"].get("needs_counter")
                and "search_counter" not in seen
            ):
                tool = "search_counter"
            message = {
                "tool_calls": [
                    {
                        "id": "mock-tool",
                        "type": "function",
                        "function": {
                            "name": "history_action",
                            "arguments": json.dumps(
                                {
                                    "tool": tool,
                                    "ids": [
                                        item["id"]
                                        for item in payload.get("evidence", [])
                                    ]
                                    if tool == "finish"
                                    else [],
                                    "reason": "仅验证工程连接，不评价AI效果",
                                }
                            ),
                        },
                    }
                ]
            }
        elif data.get("response_format"):
            schema = json.loads(messages[0]["content"].splitlines()[-1])
            title = schema["title"]
            evidence = payload.get("evidence", [])
            mid = evidence[0]["moment_id"] if evidence else None
            if title == "QueryPlan":
                change = any(
                    cue in payload.get("question", "")
                    for cue in ("变化", "相比", "比以前", "前后", "变了")
                )
                now = datetime.fromisoformat(payload["now"])
                boundary = (now - timedelta(days=90)).isoformat()
                result = {
                    "kind": "change" if change else "fact",
                    "topic": "合成记录",
                    "current": {"start": boundary, "end": now.isoformat()}
                    if change or mode["query_axis"] == "event"
                    else {},
                    "comparison": {"end": boundary} if change else None,
                    "needs_counter": False,
                    "time_axis": mode["query_axis"],
                }
            elif title == "AgentRoute":
                result = {
                    "route_type": "simple",
                    "specialists": [],
                    "insight_type": "fact",
                    "retrieval_query": "合成记录",
                }
            elif title == "SpecialistOutput":
                result = {
                    "claims": [
                        {
                            "claim": evidence[0]["moment_content"],
                            "claim_type": "fact",
                            "source_moment_ids": [mid],
                            "confidence": 1,
                        }
                    ]
                    if mid
                    else []
                }
            elif title == "VerifierOutput":
                result = {
                    "claims": [
                        {
                            **x,
                            "verification_status": "PASS",
                            "verifier_note": "合成工程响应",
                        }
                        for x in payload["candidate_claims"]
                    ]
                }
            elif title == "SynthesisOutput":
                result = {
                    "title": "合成记录回顾",
                    "body": payload["verified_claims"][0]["claim"],
                    "limitation": "此结果仅用于工程连接验证。",
                }
            elif title == "SynthesisAuditOutput":
                result = {
                    "supported": True,
                    "reason": "合成工程响应",
                    "checks": [
                        {
                            "segment_id": s["segment_id"],
                            "supported": True,
                            "source_moment_ids": [mid],
                            "reason": "合成工程响应",
                        }
                        for s in payload["segments_to_check"]
                    ],
                }
            else:
                result = {"cards": [], "ungrouped_moment_ids": []}
            message = {"content": json.dumps(result, ensure_ascii=False)}
        else:
            message = {"content": "收到合成测试消息。"}
        self.send_json(
            {
                "model": "mock",
                "choices": [{"message": message}],
                "usage": {"total_tokens": mode["chat_usage_tokens"]},
            }
        )


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8089), Handler).serve_forever()
