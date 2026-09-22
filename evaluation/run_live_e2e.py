"""Black-box acceptance tests against the deployed EchoTrace API.

Run from apps/api after filling evaluation/live-e2e.env:
  .venv/Scripts/python ../../evaluation/run_live_e2e.py --suite smoke
  .venv/Scripts/python ../../evaluation/run_live_e2e.py --suite full
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent

CAPTURE_CASES = [
    "2026年8月，我总想重新养成阅读习惯，但下班后经常随手刷手机，真正读书的时间很少。",
    "2026年9月上旬，我开始把阅读安排在晚饭后，每次只读二十分钟，这样比临时决定要容易开始。",
    "最近两周，我每周大约读三次。固定阅读时间后，我拿起手机的次数少了一些，也更容易继续读下去。",
    "上周末出门旅行，我连续两天没有阅读，不过周一回家后又恢复了晚饭后的二十分钟。",
    "我希望接下来一个月保持每周至少三次阅读，并读完现在这本书。",
]


class LiveTestFailure(RuntimeError):
    pass


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config() -> dict[str, str]:
    values = {
        **read_env(PROJECT_ROOT / "apps" / "api" / ".env"),
        **read_env(ROOT / "live-e2e.env"),
        **os.environ,
    }
    values.setdefault("ECHOTRACE_API_URL", "https://echotrace-api.vercel.app")
    required = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "ECHOTRACE_E2E_EMAIL", "ECHOTRACE_E2E_PASSWORD")
    missing = [name for name in required if not values.get(name)]
    if missing:
        raise LiveTestFailure(
            f"缺少 {', '.join(missing)}。请复制 evaluation/live-e2e.example.env 为 "
            "evaluation/live-e2e.env，并填写专用测试账号。"
        )
    return values


class PublicApi:
    def __init__(self, config: dict[str, str]) -> None:
        self.api_url = config["ECHOTRACE_API_URL"].rstrip("/")
        self.supabase_url = config["SUPABASE_URL"].rstrip("/")
        self.anon_key = config["SUPABASE_ANON_KEY"]
        self.token = ""
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(100, connect=20),
            follow_redirects=True,
            headers={"User-Agent": "EchoTrace-Live-E2E/1.0"},
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def login(self, email: str, password: str) -> Any:
        response = await self.http.post(
            f"{self.supabase_url}/auth/v1/token",
            params={"grant_type": "password"},
            headers={"apikey": self.anon_key, "Content-Type": "application/json"},
            json={"email": email, "password": password},
        )
        payload = self.decode(response)
        if response.is_error:
            raise LiveTestFailure(f"登录失败 {response.status_code}: {self.safe(payload)}")
        self.token = payload.get("access_token", "")
        if not self.token:
            raise LiveTestFailure("登录响应中没有 access_token")
        return {"user_id": payload.get("user", {}).get("id")}

    async def request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        headers = {"X-Client-Version": "live-e2e-1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = await self.http.request(
                method,
                f"{self.api_url}{path}",
                headers=headers,
                json=body,
            )
        except httpx.RequestError as exc:
            raise LiveTestFailure(f"{method} {path} 网络失败: {type(exc).__name__}: {exc}") from exc
        payload = self.decode(response)
        if response.status_code not in expected:
            request_id = response.headers.get("x-vercel-id") or response.headers.get("x-request-id") or "unknown"
            raise LiveTestFailure(
                f"{method} {path} 返回 {response.status_code}; request={request_id}; body={self.safe(payload)}"
            )
        return payload

    @staticmethod
    def decode(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text[:1000]

    @staticmethod
    def safe(payload: Any) -> str:
        if isinstance(payload, dict):
            clean = {key: value for key, value in payload.items() if key not in {"access_token", "refresh_token"}}
            return json.dumps(clean, ensure_ascii=False)[:1000]
        return str(payload)[:1000]


class Runner:
    def __init__(self, client: PublicApi, run_id: str) -> None:
        self.client = client
        self.run_id = run_id
        self.checks: list[dict[str, Any]] = []

    async def checked(self, name: str, call: Coroutine[Any, Any, Any]) -> Any:
        started = datetime.now(UTC)
        try:
            result = await call
        except Exception as exc:
            elapsed = round((datetime.now(UTC) - started).total_seconds() * 1000)
            detail = f"{type(exc).__name__}: {exc}"
            self.checks.append({"name": name, "passed": False, "duration_ms": elapsed, "detail": detail})
            print(f"[FAIL] {name} ({elapsed} ms)\n       {detail}")
            raise
        elapsed = round((datetime.now(UTC) - started).total_seconds() * 1000)
        self.checks.append({"name": name, "passed": True, "duration_ms": elapsed, "detail": "ok"})
        print(f"[PASS] {name} ({elapsed} ms)")
        return result

    async def smoke(self, email: str, password: str) -> None:
        health = await self.checked("公网 API 健康检查", self.client.request("GET", "/health"))
        if health.get("status") != "ok":
            raise LiveTestFailure(f"健康检查响应异常: {health}")
        await self.checked("Supabase 真实登录", self.client.login(email, password))
        moments = await self.checked("读取 Moment 历史", self.client.request("GET", "/moments"))
        if not isinstance(moments, list):
            raise LiveTestFailure("GET /moments 没有返回列表")

        content = f"公网自动测试 {self.run_id}：今晚准备在晚饭后阅读二十分钟。"
        moment = await self.checked(
            "创建 Moment",
            self.client.request(
                "POST",
                "/moments",
                {"content": content, "mode": "capture", "input_type": "text", "memory_enabled": True},
            ),
        )
        moment_id = str(moment.get("id", ""))
        if not moment_id:
            raise LiveTestFailure("创建 Moment 后没有返回 id")
        moments = await self.checked("确认 Moment 已持久化", self.client.request("GET", "/moments"))
        if not any(str(item.get("id")) == moment_id and item.get("content") == content for item in moments):
            raise LiveTestFailure("新建 Moment 未出现在历史记录中")

    async def full(self) -> None:
        moment_ids: list[str] = []
        for index, base in enumerate(CAPTURE_CASES, start=1):
            content = f"{base}（自动测试批次 {self.run_id}，样本 {index}）"
            moment = await self.checked(
                f"创建样本 Moment {index}",
                self.client.request(
                    "POST",
                    "/moments",
                    {"content": content, "mode": "capture", "input_type": "text", "memory_enabled": True},
                ),
            )
            moment_id = str(moment.get("id", ""))
            if not moment_id:
                raise LiveTestFailure(f"样本 Moment {index} 没有 id")
            moment_ids.append(moment_id)
            await self.checked(
                f"提炼样本 Memory {index}",
                self.client.request("POST", f"/moments/{moment_id}/process"),
            )

        memories = await self.checked("读取长期 Memory", self.client.request("GET", "/memories"))
        if len(memories) < 2:
            raise LiveTestFailure(f"预期至少 2 条长期 Memory，实际 {len(memories)} 条")
        if any(not item.get("memory_sources") for item in memories):
            raise LiveTestFailure("存在没有来源 Moment 的 Memory")
        source_moments = {
            str(source["moment_id"])
            for memory in memories
            for source in memory.get("memory_sources", [])
        }
        if len(source_moments) < 4:
            raise LiveTestFailure(f"活跃 Memory 只保留了 {len(source_moments)} 个来源时刻，历史 Evidence 继承不足")

        first = await self.checked(
            "Companion 第一轮",
            self.client.request(
                "POST",
                "/chat",
                {
                    "content": "我总是容易在开始阅读前拖延，请帮我把今晚的行动拆得更小一点。",
                    "thread_id": None,
                    "source_moment_id": None,
                    "input_type": "text",
                },
            ),
        )
        thread_id = first.get("thread_id")
        if not thread_id or not first.get("message", {}).get("content"):
            raise LiveTestFailure("Companion 第一轮响应不完整")
        second = await self.checked(
            "Companion 短期上下文",
            self.client.request(
                "POST",
                "/chat",
                {
                    "content": "根据你刚才的建议，只给我一个今晚能完成的动作。",
                    "thread_id": thread_id,
                    "source_moment_id": None,
                    "input_type": "text",
                },
            ),
        )
        if second.get("thread_id") != thread_id:
            raise LiveTestFailure("第二轮对话没有保持在同一 Thread")

        grounded = await self.checked(
            "Companion 长期记忆召回",
            self.client.request(
                "POST",
                "/chat",
                {
                    "content": "结合我以前留下的记录，最近什么方法最有助于我坚持阅读？",
                    "thread_id": None,
                    "source_moment_id": None,
                    "input_type": "text",
                },
            ),
        )
        if not grounded.get("used_long_term_memory") or not grounded.get("evidence_moment_ids"):
            raise LiveTestFailure("长期记忆回复没有可回溯证据")

        await self.checked("自动 Insight 刷新", self.client.request("POST", "/insights/refresh"))
        insights = await self.checked("读取 AI 发现", self.client.request("GET", "/insights"))
        if not insights:
            raise LiveTestFailure("刷新后没有产生 Insight")
        answer = await self.checked(
            "带证据的我想知道",
            self.client.request(
                "POST",
                "/insights/query",
                {"question": "什么做法最有助于我坚持阅读，中断之后我是怎么恢复的？"},
            ),
        )
        if not answer.get("id") or not answer.get("evidence"):
            raise LiveTestFailure("我想知道没有返回可回溯证据")
        detail = await self.checked(
            "Evidence 详情回溯",
            self.client.request("GET", f"/insights/{answer['id']}"),
        )
        if not detail.get("insight_evidence"):
            raise LiveTestFailure("Insight 详情没有 Evidence")
        await self.checked(
            "证据不足时拒绝编造",
            self.client.request(
                "POST",
                "/insights/query",
                {"question": "我过去半年最喜欢哪位作家？"},
                expected=(422,),
            ),
        )

    def report(self, suite: str) -> dict[str, Any]:
        return {
            "created_at": datetime.now(UTC).isoformat(),
            "suite": suite,
            "run_id": self.run_id,
            "summary": {
                "passed": sum(item["passed"] for item in self.checks),
                "failed": sum(not item["passed"] for item in self.checks),
                "total": len(self.checks),
            },
            "checks": self.checks,
        }


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="EchoTrace 公网黑盒自动验收")
    parser.add_argument("--suite", choices=("smoke", "full"), default="smoke")
    args = parser.parse_args()
    try:
        config = load_config()
    except LiveTestFailure as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    client = PublicApi(config)
    runner = Runner(client, run_id)
    exit_code = 0
    try:
        await runner.smoke(config["ECHOTRACE_E2E_EMAIL"], config["ECHOTRACE_E2E_PASSWORD"])
        if args.suite == "full":
            await runner.full()
    except Exception as exc:
        exit_code = 1
        if not runner.checks or runner.checks[-1]["passed"]:
            runner.checks.append(
                {
                    "name": "完整链路断言",
                    "passed": False,
                    "duration_ms": 0,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            print(f"[FAIL] 完整链路断言\n       {type(exc).__name__}: {exc}")
    finally:
        await client.close()

    report = runner.report(args.suite)
    output = ROOT / "results" / f"live-e2e-{run_id}.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Report: {output}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
