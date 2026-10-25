"""New isolated-data runner. Default prints a plan; never touches frozen results.

Run each strategy using a separately configured empty TEST account. Worker env
chooses ANALYSIS_STRATEGY=fixed|dynamic and RETRIEVAL_FUSION=legacy|rrf.
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]


async def run(args):
    api_source_sha256 = hashlib.sha256(
        b"".join(p.read_bytes() for p in sorted((ROOT / "apps/api/app").rglob("*.py")))
    ).hexdigest()
    cases = json.loads(
        (Path(__file__).with_name("scenarios.json")).read_text(encoding="utf-8")
    )
    all_cases = cases
    cases = (
        [case for case in cases if case["case_id"] in args.case_id]
        if args.case_id
        else cases[: args.cases]
    )
    seed_cases = all_cases if args.seed_all else cases
    max_calls = 24 * (len(cases) + sum(len(c["records"]) for c in seed_cases))
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "plan_only",
                    "cases": len(cases),
                    "physical_call_ceiling_at_default_limits": max_calls,
                    "normal_index_calls": sum(len(c["records"]) for c in seed_cases),
                    "note": "每个分析最多24次物理模型/Embedding请求；每个索引任务也有预算。真实金额取决于模型定价。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if os.getenv("ECHO_V2_TEST_CONFIRM") != "isolated" or args.call_budget < max_calls:
        raise SystemExit(
            "需要显式确认独立测试账号及足够的调用预算；默认命令不调用模型。"
        )
    names = [
        "ECHO_V2_SUPABASE_URL",
        "ECHO_V2_ANON_KEY",
        "ECHO_V2_EMAIL",
        "ECHO_V2_PASSWORD",
    ]
    if any(not os.getenv(name) for name in names):
        raise SystemExit("缺少独立测试凭据；不会自动读取项目 .env")
    api = os.getenv("ECHO_V2_API_URL", "http://127.0.0.1:8000").rstrip("/")
    sb = os.environ["ECHO_V2_SUPABASE_URL"].rstrip("/")
    folder = (
        ROOT
        / "evaluation"
        / "results"
        / "upgrade-v2"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + args.label)
    )
    folder.mkdir(parents=True, exist_ok=False)
    async with httpx.AsyncClient(timeout=30) as client:
        auth = await client.post(
            sb + "/auth/v1/token?grant_type=password",
            headers={"apikey": os.environ[names[1]]},
            json={"email": os.environ[names[2]], "password": os.environ[names[3]]},
        )
        if auth.is_error:
            raise SystemExit("测试账号登录失败；不输出认证响应")
        user = auth.json()["user"]["id"]
        headers = {
            "apikey": os.environ[names[1]],
            "Authorization": "Bearer " + auth.json()["access_token"],
        }
        existing = await client.get(
            sb + "/rest/v1/moments",
            headers=headers,
            params={"select": "id", "user_id": f"eq.{user}", "limit": 1},
        )
        existing.raise_for_status()
        if existing.json():
            raise SystemExit("账号已有记录，拒绝覆盖或清空。请换新的测试账号。")
        dataset = {}
        snapshot = []
        now = datetime.now(UTC)
        for case in seed_cases:
            ids = {item["id"]: str(uuid4()) for item in case["records"]}
            dataset[case["case_id"]] = ids
            rows = [
                {
                    "id": ids[item["id"]],
                    "user_id": user,
                    "content": item["text"],
                    "mode": "capture",
                    "created_at": (now - timedelta(days=item["days_ago"])).isoformat(),
                }
                for item in case["records"]
            ]
            snapshot.append(
                {
                    "case_id": case["case_id"],
                    "question": case["question"],
                    "records": rows,
                    "expected_ids": [ids[mid] for mid in case["expected"]],
                    "review": case["review"],
                }
            )
            response = await client.post(
                sb + "/rest/v1/moments", headers=headers, json=rows
            )
            response.raise_for_status()
        (folder / "dataset_snapshot.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # Durable indexing must finish before interpreting retrieval results.
        for _ in range(180):
            jobs = await client.get(
                sb + "/rest/v1/background_jobs",
                headers=headers,
                params={
                    "select": "id,status,checkpoint",
                    "kind": "eq.index",
                    "user_id": f"eq.{user}",
                },
            )
            jobs.raise_for_status()
            statuses = [row["status"] for row in jobs.json()]
            if any(x in {"failed", "cancelled"} for x in statuses):
                raise SystemExit("索引失败，停止效果测试")
            if statuses and all(x == "succeeded" for x in statuses):
                (folder / "index_tasks.json").write_text(
                    json.dumps(jobs.json(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                break
            await asyncio.sleep(2)
        else:
            raise SystemExit("索引未就绪，停止效果测试")
        for case in cases:
            start = time.monotonic()
            submitted = await client.post(
                api + "/insights/query",
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json={"question": case["question"]},
            )
            submitted.raise_for_status()
            task = submitted.json()
            for _ in range(210):
                response = await client.get(
                    api + "/jobs/" + task["id"], headers=headers
                )
                response.raise_for_status()
                task = response.json()
                if task["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                await asyncio.sleep(3)
            traces = await client.get(
                sb + "/rest/v1/background_jobs",
                headers=headers,
                params={
                    "select": "checkpoint",
                    "id": f"eq.{task['id']}",
                    "user_id": f"eq.{user}",
                },
            )
            traces.raise_for_status()
            checkpoint = traces.json()[0]["checkpoint"]
            actual_label = (
                checkpoint.get("strategy", "") + "-" + checkpoint.get("fusion", "")
            )
            if actual_label != args.label:
                raise SystemExit("Worker 策略与实验标签不同，停止，不能混用结果。")
            ai_runs = await client.get(
                sb + "/rest/v1/ai_runs",
                headers=headers,
                params={
                    "select": "task_type,metadata,latency_ms,input_tokens,output_tokens,prompt_version,model",
                    "trace_id": f"eq.{task['id']}",
                    "user_id": f"eq.{user}",
                },
            )
            ai_runs.raise_for_status()
            output = None
            if task["status"] == "succeeded":
                response = await client.get(
                    api + "/jobs/" + task["id"] + "/result", headers=headers
                )
                response.raise_for_status()
                output = response.json()
            raw = {
                "case_id": case["case_id"],
                "label": args.label,
                "task": task,
                "checkpoint": checkpoint,
                "ai_runs": ai_runs.json(),
                "expected_ids": [
                    dataset[case["case_id"]][mid] for mid in case["expected"]
                ],
                "elapsed_seconds": round(time.monotonic() - start, 2),
                "output": output,
            }
            with (folder / "raw_results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(raw, ensure_ascii=False) + "\n")
    manifest = {
        "git": (
            await asyncio.to_thread(
                subprocess.check_output,
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                text=True,
            )
        ).strip(),
        "dirty": bool(
            await asyncio.to_thread(
                subprocess.check_output,
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                text=True,
            )
        ),
        "api_source_sha256": api_source_sha256,
        "dataset_sha256": hashlib.sha256(
            Path(__file__).with_name("scenarios.json").read_bytes()
        ).hexdigest(),
        "label": args.label,
        "analyzed_case_ids": [case["case_id"] for case in cases],
        "seeded_case_ids": [case["case_id"] for case in seed_cases],
        "created_at": datetime.now(UTC).isoformat(),
        "models": "See each task checkpoint and ai_runs in the isolated test account; no invented prices.",
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    results = [
        json.loads(line)
        for line in (folder / "raw_results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    summaries = []
    for row in results:
        final = {
            x["moment_id"] for x in (row["output"] or {}).get("insight_evidence", [])
        }
        retrieved = {
            mid
            for item in row["checkpoint"].get("observations", [])
            for mid in item.get("ids", [])
        }
        for trace in row["ai_runs"]:
            retrieved.update(trace.get("metadata", {}).get("retrieved_moment_ids", []))
        expected = set(row["expected_ids"])
        summaries.append(
            {
                "case_id": row["case_id"],
                "status": row["task"]["status"],
                "retrieval_union_target_coverage": len(expected & retrieved)
                / len(expected),
                "final_evidence_target_hits": len(expected & final),
                "target_count": len(expected),
                "seconds": row["elapsed_seconds"],
                "physical_calls": row["checkpoint"].get("calls"),
                "reported_tokens": row["checkpoint"].get("tokens"),
                "grounded": None,
                "over_inference": None,
                "wrong_refusal": None,
                "time_correct": None,
                "currency_cost": None,
            }
        )
    with (folder / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    (folder / "metrics.json").write_text(
        json.dumps(
            {
                "cases": summaries,
                "note": "检索并集覆盖不是 Recall@K；语义、错误拒答和金额等待人工/价格配置，不伪造分数。",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with (folder / "bad_cases.jsonl").open("w", encoding="utf-8") as handle:
        for row in results:
            if row["task"]["status"] != "succeeded":
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (folder / "report.md").write_text(
        "# 升级版小规模对照\n\n原始输出见 raw_results.jsonl。\n"
        "检索质量见 summary.csv，时间窗口和分路排名见 checkpoint.observations。\n"
        "结论有据、过度推断、时间与错误拒答待人工填写 review.csv。\n"
        "本轮不使用旧冻结集，也不预设新策略一定更好。\n",
        encoding="utf-8",
    )
    with (folder / "review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "case_id",
                "grounded",
                "over_inference",
                "wrong_refusal",
                "time_correct",
                "reason",
            ]
        )
        writer.writerows(
            [case["case_id"], "", "", "", "", case["review"]] for case in cases
        )
    print("已保存原始结果和空白人工复核表：", folder)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--cases", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument("--case-id", action="append", choices=["U01", "U02", "U03"])
    parser.add_argument(
        "--seed-all",
        action="store_true",
        help="保留所有场景的干扰记录，但只分析指定场景",
    )
    parser.add_argument(
        "--label",
        choices=["fixed-legacy", "fixed-rrf", "dynamic-legacy", "dynamic-rrf"],
        default="dynamic-rrf",
    )
    parser.add_argument("--call-budget", type=int, default=0)
    asyncio.run(run(parser.parse_args()))
