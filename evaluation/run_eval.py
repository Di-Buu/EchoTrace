"""Run EchoTrace product evaluation locally with caching and resumable reports.

From the repository root:
  apps/api/.venv/Scripts/python.exe evaluation/run_eval.py
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
API_ROOT = PROJECT_ROOT / "apps" / "api"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(API_ROOT))

from app.clients.ai import AiResponseError, BailianClient
from app.config import Settings
from app.prompts import PROMPT_VERSIONS
from app.runtime import source_fingerprint

from evaluation.evidence_metrics import load_gold, summarize_evidence
from evaluation.incremental import cache_status
from evaluation.review_packet import render_review_packet

JUDGE_VERSION = "product-risk-judge-v1.2"


class EvaluationError(RuntimeError):
    pass


class JudgeOutput(BaseModel):
    verdict: Literal["pass", "fail", "review"]
    grounded: bool
    temporal_correct: bool | None = None
    over_inference: bool
    reason: str


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


def load_environment() -> dict[str, str]:
    values = {
        **read_env(API_ROOT / ".env"),
        **read_env(ROOT / "eval.env"),
        **read_env(ROOT / "live-e2e.env"),
        **os.environ,
    }
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for number, raw in enumerate((ROOT / "scenarios.jsonl").read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            case = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EvaluationError(f"scenarios.jsonl 第 {number} 行不是有效 JSON") from exc
        required = {"case_id", "description", "category", "workflow", "history", "request", "expected"}
        missing = sorted(required - set(case))
        if missing:
            raise EvaluationError(f"{case.get('case_id', number)} 缺少字段: {', '.join(missing)}")
        cases.append(case)
    return cases


def core_case_ids() -> list[str]:
    suite = json.loads((ROOT / "core_suite.json").read_text(encoding="utf-8"))
    return list(suite["case_ids"])


def evaluation_fingerprint() -> str:
    digest = hashlib.sha256()
    for name in (
        "run_eval.py", "core_suite.json", "scenarios.jsonl", "retrieval_gold.json",
        "isolation.py", "finalize_core.py", "evidence_metrics.py", "incremental.py",
    ):
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()[:20]


def git_metadata() -> dict[str, Any]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return (result.stdout or "").strip()

    status = run("status", "--short")
    diff = run("diff", "--no-ext-diff", "HEAD")
    return {
        "commit": run("rev-parse", "HEAD") or "unknown",
        "dirty": bool(status),
        "working_tree_hash": hashlib.sha256(f"{status}\n{diff}".encode()).hexdigest()[:16],
    }


class ProductClient:
    def __init__(self, values: dict[str, str], base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.supabase_url = values.get("SUPABASE_URL", "").rstrip("/")
        self.anon_key = values.get("SUPABASE_ANON_KEY", "")
        self.email = values.get("ECHOTRACE_EVAL_EMAIL") or values.get("ECHOTRACE_E2E_EMAIL", "")
        self.password = values.get("ECHOTRACE_EVAL_PASSWORD") or values.get("ECHOTRACE_E2E_PASSWORD", "")
        self.token = ""
        self.user_id = ""
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=20), follow_redirects=True)

    async def close(self) -> None:
        await self.http.aclose()

    def validate(self) -> None:
        missing = []
        for name, value in {
            "SUPABASE_URL": self.supabase_url,
            "SUPABASE_ANON_KEY": self.anon_key,
            "ECHOTRACE_EVAL_EMAIL": self.email,
            "ECHOTRACE_EVAL_PASSWORD": self.password,
        }.items():
            if not value:
                missing.append(name)
        if missing:
            raise EvaluationError(f"缺少评测配置: {', '.join(missing)}")

    async def verify_runtime(self, expected: str) -> None:
        try:
            response = await self.http.get(f"{self.base_url}/health")
            response.raise_for_status()
            actual = self._decode(response).get("runtime_fingerprint")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise EvaluationError("本地 API 无法进行版本校验；请确认后端已启动") from exc
        if actual != expected:
            raise EvaluationError(
                "本地 API 仍在运行旧代码。请重启本地后端，再运行评测；"
                "本次没有登录、清理数据或调用模型。"
            )

    async def login(self) -> None:
        self.validate()
        response = await self.http.post(
            f"{self.supabase_url}/auth/v1/token",
            params={"grant_type": "password"},
            headers={"apikey": self.anon_key, "Content-Type": "application/json"},
            json={"email": self.email, "password": self.password},
        )
        if response.status_code >= 400:
            signup = await self.http.post(
                f"{self.supabase_url}/auth/v1/signup",
                headers={"apikey": self.anon_key, "Content-Type": "application/json"},
                json={"email": self.email, "password": self.password},
            )
            payload = self._decode(signup)
            if signup.status_code >= 400:
                raise EvaluationError(f"评测账号登录和注册均失败: {self._safe(payload)}")
        else:
            payload = self._decode(response)
        self.token = str(payload.get("access_token", ""))
        self.user_id = str(payload.get("user", {}).get("id", ""))
        if not self.token or not self.user_id:
            raise EvaluationError("评测账号需要先完成邮箱确认，响应中没有可用 Session")

    async def api(self, method: str, path: str, body: Any | None = None) -> tuple[int, Any]:
        response = await self._api_request(method, path, body)
        if response.status_code == 401:
            await self.login()
            response = await self._api_request(method, path, body)
        return response.status_code, self._decode(response)

    async def _api_request(self, method: str, path: str, body: Any | None) -> httpx.Response:
        return await self.http.request(
            method,
            f"{self.base_url}{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-Client-Version": "local-eval-1.0",
            },
            json=body,
        )

    async def clear_data(self) -> None:
        status, payload = await self.api("DELETE", "/account/data")
        if status != 200:
            raise EvaluationError(f"无法清理评测数据 ({status}): {self._safe(payload)}")

    async def seed_history(self, case_id: str, history: list[dict[str, Any]]) -> list[str]:
        if not history:
            return []
        payload = []
        for index, item in enumerate(history):
            moment_id = str(uuid5(NAMESPACE_URL, f"echotrace:{case_id}:{index}"))
            payload.append(
                {
                    "id": moment_id,
                    "user_id": self.user_id,
                    "content": item["text"],
                    "mode": "capture",
                    "input_type": "text",
                    "memory_enabled": item.get("memory_enabled", True),
                    "created_at": item["at"],
                }
            )
        response = await self.http.post(
            f"{self.supabase_url}/rest/v1/moments",
            headers={
                "apikey": self.anon_key,
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
            json=payload,
        )
        data = self._decode(response)
        if response.status_code >= 400:
            raise EvaluationError(f"初始化 Moment 失败: {self._safe(data)}")
        return [str(item["id"]) for item in payload]

    async def retrieval_trace(self) -> list[dict[str, Any]]:
        """Read only this evaluation user's retrieval runs after the case workflow."""
        response = await self.http.get(
            f"{self.supabase_url}/rest/v1/ai_runs",
            params={
                "select": "trace_id,created_at,metadata",
                "user_id": f"eq.{self.user_id}",
                "task_type": "eq.retrieval",
                "order": "created_at.asc",
            },
            headers={"apikey": self.anon_key, "Authorization": f"Bearer {self.token}"},
        )
        if response.status_code >= 400:
            return [{"error": f"retrieval trace unavailable ({response.status_code})"}]
        rows = self._decode(response)
        if not isinstance(rows, list):
            return [{"error": "retrieval trace response is not a list"}]
        return [
            {
                "trace_id": row.get("trace_id"),
                "created_at": row.get("created_at"),
                "retrieved_moment_ids": (row.get("metadata") or {}).get("retrieved_moment_ids"),
            }
            for row in rows
        ]

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text[:2000]

    @staticmethod
    def _safe(payload: Any) -> str:
        if isinstance(payload, dict):
            payload = {key: value for key, value in payload.items() if "token" not in key.lower()}
        return json.dumps(payload, ensure_ascii=False)[:1200]


async def execute_workflow(client: ProductClient, case: dict[str, Any]) -> dict[str, Any]:
    await client.clear_data()
    moment_ids = await client.seed_history(case["case_id"], case["history"])
    process_results = []
    for moment_id, item in zip(moment_ids, case["history"], strict=True):
        if not item.get("memory_enabled", True):
            continue
        status, payload = await client.api("POST", f"/moments/{moment_id}/process")
        if status != 200:
            raise EvaluationError(f"Moment 原文索引失败 ({status}): {ProductClient._safe(payload)}")
        process_results.append(payload)

    workflow = case["workflow"]
    if workflow == "companion":
        status, output = await client.api(
            "POST",
            "/chat",
            {
                "content": case["request"],
                "thread_id": None,
                "source_moment_id": None,
                "input_type": "text",
            },
        )
    elif workflow in {"insight", "memory_retrieval"}:
        status, output = await client.api("POST", "/insights/query", {"question": case["request"]})
    else:
        raise EvaluationError(f"不支持的 workflow: {workflow}")
    retrieval_trace = await client.retrieval_trace()
    return {
        "http_status": status,
        "output": output,
        "process_results": process_results,
        "seeded_moment_ids": moment_ids,
        "retrieval_trace": retrieval_trace,
    }


def flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(flatten_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(flatten_text(item) for item in value.values())
    return ""


def candidate_text(workflow: str, output: Any) -> str:
    """Score the generated answer, never the question or quoted source evidence."""
    if not isinstance(output, dict):
        return flatten_text(output)
    if workflow == "companion":
        message = output.get("message") or {}
        return str(message.get("content", "")) if isinstance(message, dict) else ""
    if workflow in {"insight", "memory_retrieval"}:
        return " ".join(str(output.get(field) or "") for field in ("title", "body", "limitation"))
    return ""


def evidence_count(workflow: str, output: Any) -> int:
    if workflow == "companion" and isinstance(output, dict):
        return len(set(output.get("evidence_moment_ids", [])))
    if workflow in {"insight", "memory_retrieval"} and isinstance(output, dict):
        evidence = output.get("evidence") or output.get("insight_evidence") or []
        return len({str(item.get("moment_id")) for item in evidence})
    return 0


def rule_judge(case: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    output = execution["output"]
    status = execution["http_status"]
    text = candidate_text(case["workflow"], output)
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, actual: Any = None) -> None:
        checks.append({"name": name, "passed": bool(passed), "actual": actual})

    expect_answer = expected.get("expect_answer", True)
    add("response_status", status == 200 if expect_answer else status == 422, status)
    for term in expected.get("required_terms", []):
        add(f"required:{term}", term in text)
    for term in expected.get("forbidden_terms", []):
        add(f"forbidden:{term}", term not in text)

    count = evidence_count(case["workflow"], output)
    if "min_evidence" in expected:
        add("min_evidence", count >= int(expected["min_evidence"]), count)
    if expected.get("require_no_long_term"):
        used = bool(output.get("used_long_term_memory")) if isinstance(output, dict) else False
        add("no_long_term_memory", not used, used)
    if case["workflow"] == "companion" and status == 200 and isinstance(output, dict):
        cited = {str(item) for item in output.get("evidence_moment_ids", [])}
        historical = {str(item) for item in execution.get("seeded_moment_ids", [])}
        unexpected = sorted(cited - historical)
        add("historical_evidence_ids", not unexpected, unexpected)

    return {
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
        "evidence_count": count,
    }


async def llm_judge(ai: BailianClient, case: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    system = (
        "你是 EchoTrace 产品效果评测员。只依据测试历史、预期与候选结果判断。"
        "重点检查个人事实是否有真实记录、时间顺序、反例、证据充分性和过度推断。"
        "不要因为语言流畅而给高分；不能可靠自动判断时返回 review。只输出 JSON。"
    )
    seeded_ids = execution.get("seeded_moment_ids", [])
    history = [
        {**item, "moment_id": seeded_ids[index] if index < len(seeded_ids) else None}
        for index, item in enumerate(case["history"])
    ]
    payload = {
        "scenario": case["description"],
        "categories": case["category"],
        "history": history,
        "user_request": case["request"],
        "expected": case["expected"],
        "candidate": execution["output"],
        "user_facing_text": candidate_text(case["workflow"], execution["output"]),
        "id_note": (
            "历史证据只能引用 history 中的 moment_id。Companion 顶层 moment_id 是本轮新消息，"
            "它不能作为过去事实的证据；若 evidence_moment_ids 包含该 ID，应判为无效引用。"
        ),
        "stance_note": (
            "Insight evidence 的 support/counter 是相对于生成的洞察结论，"
            "不是相对于被引用书籍或用户问题中的观点。"
        ),
    }
    result, metadata = await ai.structured_chat(
        system=system,
        user=json.dumps(payload, ensure_ascii=False),
        schema=JudgeOutput,
        temperature=0,
        enable_thinking=False,
        max_tokens=1200,
    )
    return {"version": JUDGE_VERSION, "result": result.model_dump(), "metadata": metadata}


async def run_case(
    client: ProductClient,
    ai: BailianClient,
    case: dict[str, Any],
    *,
    baseline: dict[str, Any],
    cache_dir: Path,
    force: bool,
    enable_judge: bool,
    retries: int,
    case_timeout: float,
) -> dict[str, Any]:
    status = cache_status(case, baseline, cache_dir, force=force)
    fingerprint = status["fingerprint"]
    cache_path = status["path"]
    if status["status"].startswith("reuse"):
        cached = dict(status["item"])
        cached["cached"] = True
        cached["fingerprint"] = fingerprint
        cached["cache_origin"] = status["status"]
        cached["reuse_reason"] = status["reason"]
        if status["status"] == "reuse_historical":
            cached["source_run"] = status["source"]
            cached["provenance_warning"] = (
                "历史基线含未定位的未提交改动；按相关代码和 Prompt 版本判断可复用，"
                "不等同干净的当前版本重跑。"
            )
        cached["rule_judge"] = rule_judge(case, cached["execution"])
        judge_result = cached.get("llm_judge")
        if (
            enable_judge
            and case.get("llm_judge", False)
            and (not judge_result or judge_result.get("version") != JUDGE_VERSION)
        ):
            try:
                judge_result = await llm_judge(ai, case, cached["execution"])
            except AiResponseError as exc:
                judge_result = {"version": JUDGE_VERSION, "error": str(exc)}
            cached["llm_judge"] = judge_result
        cached["verdict"] = score_verdict(
            case, cached["execution"], None, cached["rule_judge"], judge_result
        )
        if cached["verdict"] in {"pass", "review"}:
            cache_path.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
        return cached

    started = datetime.now(UTC)
    execution: dict[str, Any] | None = None
    error: str | None = None
    attempts = 0
    for attempt in range(retries + 1):
        attempts = attempt + 1
        try:
            if not client.token:
                await client.login()
            execution = await asyncio.wait_for(execute_workflow(client, case), timeout=case_timeout)
            error = None
            break
        except (TimeoutError, httpx.HTTPError, EvaluationError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                await asyncio.sleep(1.2 * (attempt + 1))

    rule_result = rule_judge(case, execution) if execution else {"passed": False, "checks": []}
    judge_result = None
    valid_candidate = bool(execution and execution.get("http_status") in {200, 422})
    if valid_candidate and enable_judge and case.get("llm_judge", False):
        try:
            judge_result = await llm_judge(ai, case, execution)
        except AiResponseError as exc:
            judge_result = {"version": JUDGE_VERSION, "error": str(exc)}

    verdict = score_verdict(case, execution, error, rule_result, judge_result)

    result = {
        "case_id": case["case_id"],
        "description": case["description"],
        "category": case["category"],
        "workflow": case["workflow"],
        "fingerprint": fingerprint,
        "api_runtime_fingerprint": baseline["api_runtime_fingerprint"],
        "cached": False,
        "attempts": attempts,
        "started_at": started.isoformat(),
        "duration_ms": round((datetime.now(UTC) - started).total_seconds() * 1000),
        "verdict": verdict,
        "error": error,
        "rule_judge": rule_result,
        "llm_judge": judge_result,
        "execution": execution,
        "manual_review": bool(case.get("manual_review")),
        "cache_origin": "new_run",
    }
    if execution and not error and verdict in {"pass", "review"}:
        cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def score_verdict(
    case: dict[str, Any],
    execution: dict[str, Any] | None,
    error: str | None,
    rule_result: dict[str, Any],
    judge_result: dict[str, Any] | None,
) -> Literal["pass", "fail", "review"]:
    if error or not execution:
        return "fail"
    failed = [item["name"] for item in rule_result["checks"] if not item["passed"]]
    judge_verdict = (judge_result or {}).get("result", {}).get("verdict")
    if judge_verdict == "fail":
        return "fail"
    if any(name in {"no_long_term_memory", "historical_evidence_ids", "min_evidence"} for name in failed):
        return "fail"
    if judge_verdict == "review":
        return "review"
    if not failed:
        if case.get("manual_review") or judge_verdict == "review" or (judge_result and judge_result.get("error")):
            return "review"
        return "pass"
    semantic_checks = all(
        name == "response_status" or name.startswith(("required:", "forbidden:")) for name in failed
    )
    if semantic_checks and judge_verdict == "pass":
        return "review"
    if (
        semantic_checks
        and not case["expected"].get("expect_answer", True)
        and execution["http_status"] == 200
    ):
        # A cautious answer may correctly refuse a claim; inspect its meaning before scoring.
        return "review"
    return "fail"


def write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
        encoding="utf-8",
    )


def build_metrics(
    results: list[dict[str, Any]], baseline: dict[str, Any], cases: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    categories = sorted({category for item in results for category in item["category"]})
    by_category = {
        category: {
            "total": sum(category in item["category"] for item in results),
            "passed": sum(category in item["category"] and item["verdict"] == "pass" for item in results),
            "failed": sum(category in item["category"] and item["verdict"] == "fail" for item in results),
            "review": sum(category in item["category"] and item["verdict"] == "review" for item in results),
        }
        for category in categories
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "baseline": baseline,
        "overall": {
            "total": len(results),
            "passed": sum(item["verdict"] == "pass" for item in results),
            "failed": sum(item["verdict"] == "fail" for item in results),
            "review": sum(item["verdict"] == "review" for item in results),
            "cached": sum(item.get("cached", False) for item in results),
            "reused_historical": sum(bool(item.get("source_run")) for item in results),
        },
        "evidence_metrics": summarize_evidence(cases or load_cases(), results),
        "by_category": by_category,
        "product_risk_metrics": {
            category: {
                "automatic_passed": values["passed"],
                "automatic_failed": values["failed"],
                "pending_human_review": values["review"],
                "evaluated": values["total"],
            }
            for category, values in by_category.items()
        },
    }


def core_product_metrics(
    results: list[dict[str, Any]], evidence: dict[str, Any], isolation: list[dict[str, Any]]
) -> dict[str, Any]:
    """Only publish an outcome when its actual evidence and denominator are present."""
    scored = [item for item in evidence["cases"] if item["retrieval_recall_at_k"] is not None]
    missing_positive = [
        item for item in evidence["cases"]
        if item["expected_moment_ids"] and item["retrieval_recall_at_k"] is None
    ]
    expected = sum(len(item["expected_moment_ids"]) for item in scored)
    found = sum(
        len(set(item["expected_moment_ids"]) & set(item["retrieved_moment_ids"] or []))
        for item in scored
    )
    insights = [
        item for item in results
        if item["workflow"] == "insight" and (item.get("execution") or {}).get("http_status") == 200
    ]
    cited = 0
    for item in insights:
        execution = item["execution"]
        source_ids = {
            str(entry.get("moment_id")) for entry in (execution["output"].get("evidence") or [])
            if entry.get("moment_id")
        }
        cited += bool(source_ids and source_ids <= set(execution.get("seeded_moment_ids") or []))
    judged = [item for item in insights if isinstance((item.get("llm_judge") or {}).get("result"), dict)]
    isolation_complete = len(isolation) == 5 and all(item["status"] in {"pass", "fail"} for item in isolation)
    leaks = sum(item["status"] == "fail" for item in isolation)
    return {
        "historical_memory_recall_rate": {
            "value": found / expected if expected and not missing_positive else None,
            "observed_partial_rate": found / expected if expected else None,
            "found": found, "expected": expected, "scored_cases": len(scored),
            "missing_trace_cases": len(missing_positive),
            "note": "所有应召回正例都须有真实 Top-K；缺追踪时仅保留部分观察值，不发布总比例。",
        },
        "insight_evidence_rate": {
            "value": None,
            "structurally_cited": cited, "generated_insights": len(insights),
            "judge_scored": len(judged), "pending_semantic_review": len(insights),
            "note": "来源 ID 合规不等于证据语义支持；须审读结论后才能给最终比例。",
        },
        "over_inference_rate": {
            "value": None,
            "judge_flagged": sum(item["llm_judge"]["result"].get("over_inference") is True for item in judged),
            "judge_scored": len(judged), "pending_semantic_review": len(insights),
            "note": "Judge 曾漏判，不能把自动筛查结果冒充最终过度推断率。",
        },
        "cross_user_leakage_rate": {
            "value": leaks / len(isolation) if isolation_complete else None,
            "leaking_pairs": leaks, "completed_pairs": len(isolation), "required_pairs": 5,
            "note": "五组真实 A/B 全部完成才给比例；错误或未运行时不能宣称 0%。",
        },
    }


def write_reports(run_dir: Path, results: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    write_jsonl(run_dir / "raw_results.jsonl", results)
    bad_cases = [item for item in results if item["verdict"] == "fail"]
    reviews = [item for item in results if item["verdict"] == "review" or item["manual_review"]]
    write_jsonl(run_dir / "bad_cases.jsonl", bad_cases)
    write_jsonl(run_dir / "manual_review.jsonl", reviews)
    (run_dir / "review_packet.md").write_text(
        render_review_packet(load_cases(), results, metrics["baseline"]), encoding="utf-8"
    )
    (run_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "evidence_metrics.json").write_text(
        json.dumps(metrics["evidence_metrics"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "baseline.json").write_text(
        json.dumps(metrics["baseline"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (run_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "case_id", "categories", "workflow", "verdict", "cached", "cache_origin",
                "source_run", "attempts", "duration_ms", "error",
            ],
        )
        writer.writeheader()
        for item in results:
            writer.writerow(
                {
                    "case_id": item["case_id"],
                    "categories": "|".join(item["category"]),
                    "workflow": item["workflow"],
                    "verdict": item["verdict"],
                    "cached": item.get("cached", False),
                    "cache_origin": item.get("cache_origin", "new_run"),
                    "source_run": item.get("source_run", ""),
                    "attempts": item["attempts"],
                    "duration_ms": item["duration_ms"],
                    "error": item.get("error") or "",
                }
            )

    overall = metrics["overall"]
    lines = [
        "# EchoTrace Evaluation Report",
        "",
        f"- 运行时间：{metrics['generated_at']}",
        f"- Git commit：`{metrics['baseline']['git']['commit']}`",
        f"- 工作区是否有未提交改动：{metrics['baseline']['git']['dirty']}",
        f"- API 运行指纹：`{metrics['baseline']['api_runtime_fingerprint']}`",
        f"- Evaluation 指纹：`{metrics['baseline']['evaluation_fingerprint']}`",
        f"- Chat Model：`{metrics['baseline']['chat_model']}`",
        f"- Embedding Model：`{metrics['baseline']['embedding_model']}`",
        f"- Prompt Versions：`{json.dumps(metrics['baseline']['prompt_versions'], ensure_ascii=False)}`",
        "",
        "## 汇总",
        "",
        f"- 总数：{overall['total']}",
        f"- 自动通过：{overall['passed']}",
        f"- 失败：{overall['failed']}",
        f"- 待人工复核：{overall['review']}",
        f"- 复用缓存：{overall['cached']}",
        f"- 其中复用历史运行：{overall['reused_historical']}（见逐 Case 来源；历史 dirty 状态未完全定位）",
        "",
        "## 来源与检索证据",
        "",
        f"- 有真实检索 Top-K 可计算 Recall@K 的 Case：{metrics['evidence_metrics']['retrieval_scored']}",
        f"- 缺少检索阶段记录或遥测错误的 Case：{metrics['evidence_metrics']['retrieval_missing_trace']}",
        f"- 有原始 Moment 引用可计算引用召回的 Case：{metrics['evidence_metrics']['citation_scored']}",
        "- 旧报告的最终引用不等于检索 Top-K；缺失数据不计作 0 分。明细见 evidence_metrics.json。",
        "",
        "## 分类",
        "",
        "| 类别 | 总数 | 通过 | 失败 | 待复核 |",
        "|---|---:|---:|---:|---:|",
    ]
    for category, values in metrics["by_category"].items():
        lines.append(
            f"| {category} | {values['total']} | {values['passed']} | {values['failed']} | {values['review']} |"
        )
    product = metrics.get("core_product_metrics")
    if product:
        lines.extend(["", "## 核心用户风险", "", "| 指标 | 本次结果 | 口径 |", "|---|---:|---|"])
        for label, key in (
            ("历史记忆召回率", "historical_memory_recall_rate"),
            ("洞察有据率", "insight_evidence_rate"),
            ("过度推断率", "over_inference_rate"),
            ("跨用户串数据率", "cross_user_leakage_rate"),
        ):
            item = product[key]
            value = "待补证据/人工裁决" if item["value"] is None else f"{item['value']:.1%}"
            lines.append(f"| {label} | {value} | {item['note']} |")
    lines.extend(["", "> 本报告只记录本次实际执行结果；未运行的 Case 不计入任何指标。", ""])
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def write_core_review(run_dir: Path, results: list[dict[str, Any]], cases: list[dict[str, Any]]) -> None:
    """Prepare every generated Insight for human claim-level review, including auto-pass cases."""
    by_id = {case["case_id"]: case for case in cases}
    packet = ["# 核心洞察人工复核", "", "对每条重要个人结论判断：是否有真实记录支持、是否过度概括；时间错误写在备注。"]
    with (run_dir / "core_review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["case_id", "grounded", "over_inference", "temporal_correct", "review_notes"],
        )
        writer.writeheader()
        for item in results:
            execution = item.get("execution") or {}
            if item["workflow"] != "insight" or execution.get("http_status") != 200:
                continue
            writer.writerow({"case_id": item["case_id"]})
            case = by_id[item["case_id"]]
            packet.extend(["", f"## {item['case_id']}", "", f"问题：{case['request']}", "", "原始记录："])
            for index, source in enumerate(case["history"]):
                packet.append(f"- [{index}] {source['at']}｜{source['text']}")
            output = execution.get("output") or {}
            packet.extend([
                "", f"标题：{output.get('title', '')}", "", f"正文：{output.get('body', '')}",
                "", f"局限：{output.get('limitation', '')}", "",
                "引用 Moment ID：" + ", ".join(
                    str(entry.get("moment_id")) for entry in (output.get("evidence") or [])
                ),
                "", "人工结论：grounded / over_inference / temporal_correct 填写 yes 或 no，备注解释争议。",
            ])
    (run_dir / "core_review_packet.md").write_text("\n".join(packet) + "\n", encoding="utf-8")


async def async_main() -> int:
    values = load_environment()
    parser = argparse.ArgumentParser(description="EchoTrace 本地产品效果评测")
    parser.add_argument(
        "--base-url",
        default=values.get("ECHOTRACE_EVAL_API_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--suite", choices=("core", "legacy-all"), default="core")
    parser.add_argument("--category", action="append")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--plan", action="store_true", help="只列出复用/待运行原因，不登录、不调用产品或模型")
    parser.add_argument("--ragas", action="store_true", help="对本次保存结果运行可缓存的 Ragas 专项检查")
    parser.add_argument("--ragas-no-faithfulness", action="store_true", help="仅运行 Ragas ID 指标，不调用评判模型")
    parser.add_argument("--resume", action="store_true", help="兼容入口；成功 Case 始终会自动复用缓存")
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--case-timeout", type=float, default=600)
    args = parser.parse_args()

    settings = Settings()
    git = git_metadata()
    baseline = {
        "git": git,
        "chat_model": settings.chat_model,
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "execution_profile": settings.insight_execution_profile,
        "insight_thinking": settings.use_quality_insight_reasoning,
        "insight_timeout_seconds": settings.insight_ai_timeout_seconds,
        "weekly_insight_min_total_moments": settings.weekly_insight_min_total_moments,
        "weekly_insight_min_new_moments": settings.weekly_insight_min_new_moments,
        "prompt_versions": PROMPT_VERSIONS,
        "judge_version": JUDGE_VERSION,
        "api_runtime_fingerprint": source_fingerprint(),
        "evaluation_fingerprint": evaluation_fingerprint(),
    }
    cases = load_cases()
    if args.suite == "core":
        selected_ids = core_case_ids()
        by_id = {case["case_id"]: case for case in cases}
        missing = sorted(set(selected_ids) - set(by_id))
        if missing:
            raise EvaluationError(f"core_suite.json 中存在未知 Case: {', '.join(missing)}")
        cases = [by_id[case_id] for case_id in selected_ids]
    if args.case_id:
        selected = set(args.case_id)
        cases = [case for case in cases if case["case_id"] in selected]
    if args.category:
        selected_categories = set(args.category)
        cases = [case for case in cases if selected_categories.intersection(case["category"])]
    if args.max_cases is not None:
        cases = cases[: max(0, args.max_cases)]
    if not cases:
        raise EvaluationError("筛选后没有可运行的 Case")

    cache_dir = ROOT / "results" / "cache"
    run_isolation = args.suite == "core" and not (args.case_id or args.category or args.max_cases is not None)
    gold = load_gold()

    def refresh_missing_top_k(case: dict[str, Any], status: dict[str, Any]) -> bool:
        return bool(run_isolation and gold[case["case_id"]] and status.get("missing_retrieval_trace"))

    if args.plan:
        for case in cases:
            status = cache_status(case, baseline, cache_dir, force=args.force)
            if refresh_missing_top_k(case, status):
                print(f"{case['case_id']}: run — 旧回答可复用，但正式召回率缺真实 Top-K，需仅补采这条")
                continue
            trace_note = "；旧结果无检索 Top-K" if status.get("missing_retrieval_trace") else ""
            print(f"{case['case_id']}: {status['status']} — {status['reason']}{trace_note}")
        if run_isolation:
            print("A/B 隔离：5 组真实账号检查；--plan 不登录、不清理数据、不调用模型")
        return 0

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = ROOT / "results" / f"run-{run_id}"

    client = ProductClient(values, args.base_url, timeout=args.case_timeout)
    second_client = None
    if run_isolation:
        from evaluation.isolation import second_account_values

        second_client = ProductClient(second_account_values(values), args.base_url, timeout=args.case_timeout)
    ai = BailianClient(settings)
    results: list[dict[str, Any]] = []
    isolation_results: list[dict[str, Any]] = []
    test_data_started = False
    try:
        if any(
            (status := cache_status(case, baseline, cache_dir, force=args.force))["status"] == "run"
            or refresh_missing_top_k(case, status)
            for case in cases
        ):
            await client.verify_runtime(baseline["api_runtime_fingerprint"])
        if second_client:
            from evaluation.isolation import prepare_pair

            await client.verify_runtime(baseline["api_runtime_fingerprint"])
            await prepare_pair(client, second_client)
        run_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        for index, case in enumerate(cases, start=1):
            print(f"[{index}/{len(cases)}] {case['case_id']}")
            case_status = cache_status(case, baseline, cache_dir, force=args.force)
            force_case = args.force or refresh_missing_top_k(case, case_status)
            if case_status["status"] == "run" or force_case:
                test_data_started = True
            result = await run_case(
                client,
                ai,
                case,
                baseline=baseline,
                cache_dir=cache_dir,
                force=force_case,
                enable_judge=not args.no_judge,
                retries=max(0, args.retries),
                case_timeout=args.case_timeout,
            )
            results.append(result)
            write_jsonl(run_dir / "raw_results.jsonl", results)
            print(f"  -> {result['verdict']}{' (cached)' if result.get('cached') else ''}")
        if second_client:
            from evaluation.isolation import run_isolation_pairs

            test_data_started = True
            try:
                isolation_results = await run_isolation_pairs(client, second_client)
            except Exception as exc:  # noqa: BLE001 - incomplete is not a zero-leak result
                isolation_results = [{"case_id": "isolation_runtime_error", "status": "error", "error": str(exc)}]
    finally:
        if client.token and test_data_started:
            try:
                await client.clear_data()
            except Exception as exc:  # noqa: BLE001 - preserve report on cleanup failure
                print(f"评测数据清理失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        await client.close()
        if second_client:
            if second_client.token and test_data_started:
                try:
                    await second_client.clear_data()
                except Exception as exc:  # noqa: BLE001 - preserve report on cleanup failure
                    print(f"B 账号评测数据清理失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            await second_client.close()

    metrics = build_metrics(results, baseline, cases)
    if args.suite == "core":
        metrics["core_product_metrics"] = core_product_metrics(
            results, metrics["evidence_metrics"], isolation_results
        )
    write_reports(run_dir, results, metrics)
    if args.suite == "core":
        write_jsonl(run_dir / "isolation_results.jsonl", isolation_results)
        write_core_review(run_dir, results, cases)
        write_jsonl(
            run_dir / "bad_cases.jsonl",
            [item for item in results if item["verdict"] == "fail"]
            + [item for item in isolation_results if item["status"] != "pass"],
        )
    ragas_error = None
    if args.ragas:
        from evaluation.ragas_support import score_saved_run

        try:
            ragas_summary = await score_saved_run(run_dir, faithfulness=not args.ragas_no_faithfulness)
            with (run_dir / "report.md").open("a", encoding="utf-8") as handle:
                handle.write(
                    "\n## Ragas 辅助检查\n\n"
                    f"- ID 指标可评分 Case：{ragas_summary['id_scored']}\n"
                    f"- Faithfulness 可评分 Case：{ragas_summary['faithfulness_scored']}\n"
                    f"- 旧结果缺检索 Top-K 而跳过：{ragas_summary['skipped_missing_trace']}\n"
                    f"- Judge 错误：{ragas_summary['judge_errors']}\n"
                    "- 详细分数见 ragas_results.jsonl；不作为产品总分。\n"
                )
        except RuntimeError as exc:
            ragas_error = str(exc)
            print(f"Ragas 未完成：{exc}", file=sys.stderr)
    print(json.dumps(metrics["overall"], ensure_ascii=False, indent=2))
    print(f"Report: {run_dir / 'report.md'}")
    if ragas_error:
        return 2
    return 1 if metrics["overall"]["failed"] or any(item["status"] != "pass" for item in isolation_results) else 0


if __name__ == "__main__":
    from evaluation.isolation import IsolationError

    try:
        raise SystemExit(asyncio.run(async_main()))
    except (EvaluationError, IsolationError) as exc:
        print(f"评测配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
