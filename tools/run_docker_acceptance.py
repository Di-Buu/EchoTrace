"""Local Supabase + real Docker HTTP checks. No production .env or paid model."""

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
DOCKER = r"D:\DockerDesktop\resources\bin\docker.exe"


def command(args, env=None):
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, check=False)
    if result.returncode:
        # Do not spill CLI status JSON, container env or HTTP credentials.
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]} {args[1]}")
    return result.stdout.decode("utf-8", errors="replace")


async def main(workdir, retry_only=False, graceful_only=False, model_fixes_only=False):
    folder = Path(workdir).resolve()
    if not folder.is_relative_to(ROOT / ".acceptance"):
        raise RuntimeError("Only isolated .acceptance workspace allowed")
    marker = json.loads((folder / "acceptance.json").read_text(encoding="utf-8-sig"))
    if not marker.get("local_only"):
        raise RuntimeError("Isolation marker missing")
    env = {**os.environ, "PATH": str(Path(DOCKER).parent) + ";" + os.environ["PATH"]}
    local = json.loads(
        command(
            [
                "cmd.exe",
                "/d",
                "/c",
                "npm.cmd",
                "exec",
                "--offline",
                "--package=supabase@2.122.0",
                "--",
                "supabase",
                "status",
                "-o",
                "json",
                "--workdir",
                str(folder),
            ],
            env,
        )
    )
    if local.get("API_URL") != "http://127.0.0.1:55321":
        raise RuntimeError("Refusing nonlocal Supabase endpoint")
    public, privileged = local["ANON_KEY"], local["SERVICE_ROLE_KEY"]
    env.update(
        SUPABASE_URL="http://host.docker.internal:55321",
        SUPABASE_ANON_KEY=public,
        SUPABASE_SERVICE_ROLE_KEY=privileged,
        ECHOTRACE_IMAGE="echotrace:acceptance",
        ECHOTRACE_BIND_PORT="18000",
        DASHSCOPE_API_KEY="mock",
        CHAT_MODEL="mock",
        EMBEDDING_MODEL="mock",
        ANALYSIS_STRATEGY="dynamic",
        RETRIEVAL_FUSION="rrf",
        TASK_TIMEOUT_SECONDS="180",
        AGENT_MAX_TOKENS="40000",  # Keep the fault-injection cap stable for the existing mock checks.
    )
    compose = [
        DOCKER,
        "compose",
        "-p",
        marker["project_id"],
        "-f",
        "compose.yaml",
        "-f",
        "compose.smoke.yaml",
    ]
    config = json.loads(command(compose + ["config", "--format", "json"], env))
    assert "SUPABASE_SERVICE_ROLE_KEY" not in config["services"]["api"]["environment"]
    assert (
        config["services"]["worker"]["environment"]["SUPABASE_SERVICE_ROLE_KEY"]
        == privileged
    )
    image = json.loads(command([DOCKER, "image", "inspect", "echotrace:acceptance"]))[0]
    assert image["Config"]["User"] == "10001:10001"
    assert not any(privileged in value for value in image["Config"].get("Env", []))
    command(
        compose + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "120"], env
    )
    results = []

    def check(name, condition):
        results.append({"check": name, "passed": bool(condition)})
        (
            folder
            / (
                "http-model-fixes-checks.json"
                if model_fixes_only
                else "http-graceful-checks.json"
                if graceful_only
                else "http-retry-error-checks.json"
                if retry_only
                else "http-checks.json"
            )
        ).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(("PASS " if condition else "FAIL ") + name, flush=True)
        if not condition:
            raise AssertionError(name)

    sb = "http://127.0.0.1:55321"
    api = "http://127.0.0.1:18000"
    async with httpx.AsyncClient(timeout=25, trust_env=False) as http:

        async def user():
            email = f"acceptance-{uuid4().hex}@example.invalid"
            password = secrets.token_urlsafe(24) + "aA1!"
            response = await http.post(
                sb + "/auth/v1/admin/users",
                headers={"apikey": public, "Authorization": "Bearer " + privileged},
                json={"email": email, "password": password, "email_confirm": True},
            )
            check("auth_create_synthetic_user", response.status_code in (200, 201))
            signed = await http.post(
                sb + "/auth/v1/token?grant_type=password",
                headers={"apikey": public},
                json={"email": email, "password": password},
            )
            check("password_login", signed.status_code == 200)
            return {
                "apikey": public,
                "Authorization": "Bearer " + signed.json()["access_token"],
            }

        a, b = await user(), await user()
        check("api_ready", (await http.get(api + "/ready")).status_code == 200)
        check(
            "unauthenticated_denied",
            (await http.get(api + "/moments")).status_code == 401,
        )
        content = "本地验收合成记录：昨天买了一个蓝色水杯。"
        payload = {"content": content, "memory_enabled": True}
        key = uuid4().hex
        saved = await http.post(
            api + "/moments", headers={**a, "Idempotency-Key": key}, json=payload
        )
        check("moment_save", saved.status_code == 200)
        mid = saved.json()["id"]
        duplicate = await http.post(
            api + "/moments", headers={**a, "Idempotency-Key": key}, json=payload
        )
        check(
            "same_key_same_moment",
            duplicate.status_code == 200 and duplicate.json()["id"] == mid,
        )
        conflict = await http.post(
            api + "/moments",
            headers={**a, "Idempotency-Key": key},
            json={**payload, "content": "不同的合成内容"},
        )
        check("same_key_conflict", conflict.status_code == 409)
        check(
            "foreign_moment_api_denied",
            (await http.get(api + "/moments/" + mid, headers=b)).status_code == 404,
        )
        foreign = await http.get(
            sb + "/rest/v1/moments",
            headers=b,
            params={"select": "id", "id": "eq." + mid},
        )
        check("foreign_moment_rls", foreign.status_code == 200 and foreign.json() == [])
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            indexed = await http.get(
                sb + "/rest/v1/moment_index_chunks",
                headers=a,
                params={"select": "chunk_index", "moment_id": "eq." + mid},
            )
            if indexed.status_code == 200 and indexed.json():
                break
            await asyncio.sleep(1)
        check(
            "durable_index_complete",
            indexed.status_code == 200 and bool(indexed.json()),
        )

        async def submit(question, key):
            response = await http.post(
                api + "/insights/query",
                headers={**a, "Idempotency-Key": key},
                json={"question": question},
            )
            check("analysis_accepted_202", response.status_code == 202)
            return response.json()["id"]

        async def status(jid, until):
            end = time.monotonic() + 180
            while time.monotonic() < end:
                response = await http.get(api + "/jobs/" + jid, headers=a)
                check_status = response.status_code
                if check_status != 200:
                    raise RuntimeError("Task status unavailable")
                row = response.json()
                if row["status"] in until:
                    return row
                if row["status"] in {"failed", "cancelled", "succeeded"}:
                    return row
                await asyncio.sleep(1)
            raise TimeoutError("Task did not reach expected state")

        if model_fixes_only:

            async def configure(**values):
                response = await http.post(
                    "http://127.0.0.1:8089/__control", json=values
                )
                response.raise_for_status()

            try:
                await configure(
                    query_axis="event",
                    chat_usage_tokens=10,
                    delay_seconds=0,
                    fail_next=0,
                    bad_chat=False,
                )
                event_job = await submit("事件时间缺失时查找我的水杯记录", uuid4().hex)
                check(
                    "event_time_unknown_analysis_completes",
                    (await status(event_job, {"succeeded"}))["status"] == "succeeded",
                )
                snapshot = await http.get(
                    sb + "/rest/v1/background_jobs",
                    headers=a,
                    params={"id": "eq." + event_job, "select": "checkpoint"},
                )
                snapshot.raise_for_status()
                state = snapshot.json()[0]["checkpoint"]
                check(
                    "event_axis_preserved_with_explicit_uncertainty",
                    state["plan"]["time_axis"] == "event"
                    and "不能据此断言" in state["plan"]["time_uncertainty"],
                )
                check(
                    "fallback_source_is_traced",
                    any(
                        mid
                        in o.get("retrieval", {}).get("record_time_fallback_ids", [])
                        for o in state["observations"]
                    ),
                )
                original = await http.get(
                    sb + "/rest/v1/moments",
                    headers=a,
                    params={"id": "eq." + mid, "select": "event_at"},
                )
                check(
                    "unknown_event_time_not_fabricated",
                    original.json()[0]["event_at"] is None,
                )
                result = await http.get(
                    api + "/jobs/" + event_job + "/result", headers=a
                )
                check(
                    "fallback_result_cites_original",
                    result.status_code == 200
                    and any(
                        e["moment_id"] == mid for e in result.json()["insight_evidence"]
                    ),
                )
                await configure(query_axis="record", chat_usage_tokens=40001)
                budget_job = await submit("预算错误的定向检查", uuid4().hex)
                budget_row = await status(budget_job, {"failed"})
                check(
                    "api_exposes_specific_budget_reason",
                    budget_row["status"] == "failed"
                    and budget_row["error_code"] == "BUDGET_TOKENS",
                )
                trace = await http.get(
                    sb + "/rest/v1/background_jobs",
                    headers=a,
                    params={"id": "eq." + budget_job, "select": "checkpoint,attempts"},
                )
                trace.raise_for_status()
                state = trace.json()[0]
                check(
                    "over_limit_usage_survives_in_database",
                    state["checkpoint"]["tokens"] == 40001
                    and state["checkpoint"]["budget_failure"]["code"]
                    == "BUDGET_TOKENS",
                )
                check(
                    "budget_failure_is_not_automatically_retried",
                    state["attempts"] == 1,
                )
            finally:
                await configure(
                    query_axis="record", chat_usage_tokens=10, delay_seconds=None
                )
            return
        if graceful_only:
            await http.post(
                "http://127.0.0.1:8089/__control",
                json={"fail_next": 0, "bad_chat": False, "delay_seconds": 8},
            )
            active = await submit("退出恢复检查水杯记录", uuid4().hex)
            check(
                "graceful_observed_running",
                (await status(active, {"running"}))["status"] == "running",
            )
            worker_id = command(compose + ["ps", "-q", "worker"], env).strip()
            command([DOCKER, "stop", "--time", "25", worker_id], env)
            state = json.loads(command([DOCKER, "inspect", worker_id]))[0]["State"]
            check(
                "sigterm_exits_cleanly",
                state["ExitCode"] == 0 and not state["OOMKilled"],
            )
            waiting = await submit("停机排队检查水杯记录", uuid4().hex)
            queued = await status(waiting, {"queued"})
            check(
                "no_claim_while_worker_stopped",
                queued["status"] == "queued" and queued["attempts"] == 0,
            )
            await http.post(
                "http://127.0.0.1:8089/__control", json={"delay_seconds": None}
            )
            command([DOCKER, "start", worker_id], env)
            check(
                "interrupted_job_recovers_after_graceful_exit",
                (await status(active, {"succeeded"}))["status"] == "succeeded",
            )
            check(
                "queued_job_runs_after_restart",
                (await status(waiting, {"succeeded"}))["status"] == "succeeded",
            )
            return
        if retry_only:
            configured = await http.post(
                "http://127.0.0.1:8089/__control",
                json={
                    "fail_next": 3,
                    "fail_status": 429,
                    "bad_chat": False,
                    "delay_seconds": None,
                },
            )
            configured.raise_for_status()
            job = await submit("定向检查重试后的水杯记录", uuid4().hex)
            finished = await status(job, {"succeeded"})
            check(
                "retry_succeeds_and_clears_old_error",
                finished["status"] == "succeeded"
                and finished["attempts"] == 2
                and finished["error_code"] is None,
            )
            return
        q = "回顾我的水杯记录"
        key = uuid4().hex
        jid = await submit(q, key)
        check("same_key_same_analysis", await submit(q, key) == jid)
        check(
            "foreign_task_denied",
            (await http.get(api + "/jobs/" + jid, headers=b)).status_code == 404,
        )
        completed = await status(jid, {"succeeded"})
        check("analysis_succeeded", completed["status"] == "succeeded")
        result = await http.get(api + "/jobs/" + jid + "/result", headers=a)
        check("result_read", result.status_code == 200)
        evidence = result.json()["insight_evidence"]
        check(
            "result_has_owned_evidence",
            bool(evidence) and all(x["moment_id"] == mid for x in evidence),
        )
        check(
            "foreign_result_denied",
            (await http.get(api + "/jobs/" + jid + "/result", headers=b)).status_code
            == 404,
        )
        cancel_id = await submit("再次回顾我的水杯记录", uuid4().hex)
        running = await status(cancel_id, {"running"})
        check("cancellation_observed_running", running["status"] == "running")
        cancelled = await http.post(api + "/jobs/" + cancel_id + "/cancel", headers=a)
        check("cancel_request", cancelled.status_code == 200)
        check(
            "cancelled_status",
            (await status(cancel_id, {"cancelled"}))["status"] == "cancelled",
        )

        async def control(**values):
            response = await http.post("http://127.0.0.1:8089/__control", json=values)
            if response.status_code != 200:
                raise RuntimeError("Mock control unavailable")

        await control(fail_next=3, fail_status=429)
        retry_id = await submit("重试检查我的水杯记录", uuid4().hex)
        retried = await status(retry_id, {"succeeded"})
        check(
            "transient_failure_retried_successfully",
            retried["status"] == "succeeded" and retried["attempts"] == 2,
        )

        await control(bad_chat=True)
        bad_id = await submit("结构异常检查我的水杯记录", uuid4().hex)
        bad = await status(bad_id, {"failed"})
        check(
            "invalid_model_output_fails_bounded",
            bad["status"] == "failed"
            and bad["attempts"] == 2
            and bool(bad["error_code"]),
        )
        await control(bad_chat=False)

        provider_before = (await http.get("http://127.0.0.1:8089/__stats")).json()[
            "requests"
        ]
        crash_id = await submit("恢复检查我的水杯记录", uuid4().hex)
        check(
            "crash_observed_running",
            (await status(crash_id, {"running"}))["status"] == "running",
        )
        admin = {"apikey": public, "Authorization": "Bearer " + privileged}
        previous = await http.get(
            sb + "/rest/v1/background_jobs",
            headers=admin,
            params={"select": "lease_token,user_id,checkpoint", "id": "eq." + crash_id},
        )
        previous.raise_for_status()
        lease = previous.json()[0]["lease_token"]
        owner = previous.json()[0]["user_id"]
        reserved = previous.json()[0]["checkpoint"].get("calls", 0)
        worker_id = command(compose + ["ps", "-q", "worker"], env).strip()
        command([DOCKER, "kill", "--signal", "KILL", worker_id], env)
        command([DOCKER, "start", worker_id], env)
        recovered = await status(crash_id, {"succeeded"})
        check(
            "forced_kill_then_lease_recovery",
            recovered["status"] == "succeeded" and recovered["attempts"] == 2,
        )
        fenced = await http.post(
            sb + "/rest/v1/rpc/worker_evidence_rpc",
            headers=admin,
            json={
                "p_id": crash_id,
                "p_lease": lease,
                "p_function": "commit_insight",
                "p_payload": {},
            },
        )
        check(
            "old_worker_cannot_publish",
            fenced.status_code >= 400 and "LEASE_LOST" in fenced.text,
        )
        after = await http.get(
            sb + "/rest/v1/background_jobs",
            headers=admin,
            params={
                "select": "checkpoint",
                "id": "eq." + crash_id,
                "user_id": "eq." + owner,
            },
        )
        after.raise_for_status()
        check(
            "restart_did_not_reset_call_count",
            after.json()[0]["checkpoint"].get("calls", 0) >= reserved,
        )
        provider_after = (await http.get("http://127.0.0.1:8089/__stats")).json()[
            "requests"
        ]
        charged = after.json()[0]["checkpoint"].get("calls", 0)
        check(
            "recovery_count_covers_actual_http_requests",
            provider_after - provider_before
            <= charged
            <= provider_after - provider_before + 1,
        )

        await control(delay_seconds=60)
        timed_id = await submit("期限检查我的水杯记录", uuid4().hex)
        check(
            "timeout_observed_running",
            (await status(timed_id, {"running"}))["status"] == "running",
        )
        shortened = await http.patch(
            sb + "/rest/v1/background_jobs",
            headers=admin,
            params={"id": "eq." + timed_id, "user_id": "eq." + owner},
            json={
                "deadline_at": (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
            },
        )
        check("isolated_test_deadline_shortened", shortened.status_code in (200, 204))
        timed = await status(timed_id, {"failed"})
        check(
            "elapsed_deadline_becomes_explicit_failure",
            timed["status"] == "failed" and bool(timed["error_code"]),
        )
        await control(delay_seconds=None)
        revoke_id = await submit("核对我的水杯记录", uuid4().hex)
        check(
            "revocation_observed_running",
            (await status(revoke_id, {"running"}))["status"] == "running",
        )
        revoked = await http.patch(
            api + "/moments/" + mid + "/memory", headers=a, params={"enabled": "false"}
        )
        check("memory_revoke", revoked.status_code == 200)
        check(
            "revoked_analysis_cancelled",
            (await status(revoke_id, {"cancelled"}))["status"] == "cancelled",
        )
        check(
            "old_result_stale",
            (await http.get(api + "/jobs/" + jid + "/result", headers=a)).status_code
            == 410,
        )
        logs = command(compose + ["logs", "--no-color", "--tail", "1000"], env)
        check(
            "no_credential_or_body_in_app_logs",
            not any(
                value in logs
                for value in (
                    privileged,
                    public,
                    a["Authorization"],
                    b["Authorization"],
                    content,
                )
            ),
        )
        check("image_and_api_key_isolation", True)
    print(
        "Local mock HTTP checks complete. This is NOT real-model quality evaluation.",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workdir")
    parser.add_argument("--retry-only", action="store_true")
    parser.add_argument("--graceful-only", action="store_true")
    parser.add_argument("--model-fixes-only", action="store_true")
    options = parser.parse_args()
    asyncio.run(
        main(
            options.workdir,
            options.retry_only,
            options.graceful_only,
            options.model_fixes_only,
        )
    )
