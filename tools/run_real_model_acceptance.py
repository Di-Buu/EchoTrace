"""Three approved synthetic scenarios; local Supabase, real configured model."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))
from app.config import Settings

DOCKER = r"D:\DockerDesktop\resources\bin\docker.exe"


async def main(
    execute, case_ids=None, seed_all=False, capture_audit=False, token_budget=None
):
    cases = json.loads(
        (ROOT / "evaluation/v2/scenarios.json").read_text(encoding="utf-8")
    )
    all_cases = cases
    if case_ids:
        cases = [case for case in cases if case["case_id"] in case_ids]
    seed_cases = all_cases if seed_all else cases
    moment_count = sum(len(case["records"]) for case in seed_cases)
    ceiling = 24 * (len(cases) + moment_count)
    if not execute:
        print(
            f"Plan only: {[case['case_id'] for case in cases]}, {moment_count} synthetic Moments, <={ceiling} physical requests; --execute required"
        )
        return
    folder = ROOT / ".acceptance/echotrace-acceptance-222c498ffb"
    marker = json.loads((folder / "acceptance.json").read_text(encoding="utf-8-sig"))
    if marker.get("local_only") is not True:
        raise RuntimeError("Local isolation marker required")
    env = {**os.environ, "PATH": str(Path(DOCKER).parent) + ";" + os.environ["PATH"]}

    def command(args):
        result = subprocess.run(
            args, env=env, cwd=ROOT, capture_output=True, check=False
        )
        if result.returncode:
            raise RuntimeError("Command failed; credentials and raw output withheld")
        return result.stdout.decode("utf-8", errors="replace")

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
            ]
        )
    )
    if local["API_URL"] != "http://127.0.0.1:55321":
        raise RuntimeError("Refusing nonlocal database")
    source_hash = hashlib.sha256(
        b"".join(
            path.read_bytes() for path in sorted((ROOT / "apps/api/app").rglob("*.py"))
        )
    ).hexdigest()
    image_hash = command(
        [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            "echotrace:acceptance",
            "-c",
            "import hashlib;from pathlib import Path;print(hashlib.sha256(b''.join(p.read_bytes() for p in sorted(Path('/app/app').rglob('*.py')))).hexdigest())",
        ]
    ).strip()
    if source_hash != image_hash:
        raise RuntimeError(
            "Acceptance image is older than current source. Rebuild before paid calls; no model invoked."
        )
    model = Settings(_env_file=ROOT / "apps/api/.env")
    if not model.dashscope_api_key or not model.chat_model or not model.embedding_model:
        raise RuntimeError("Configured model credentials required; values withheld")
    env.update(
        SUPABASE_URL="http://host.docker.internal:55321",
        SUPABASE_ANON_KEY=local["ANON_KEY"],
        SUPABASE_SERVICE_ROLE_KEY=local["SERVICE_ROLE_KEY"],
        DASHSCOPE_BASE_URL=model.dashscope_base_url,
        DASHSCOPE_API_KEY=model.dashscope_api_key,
        CHAT_MODEL=model.chat_model,
        EMBEDDING_MODEL=model.embedding_model,
        EMBEDDING_DIMENSION=str(model.embedding_dimension),
        ECHOTRACE_IMAGE="echotrace:acceptance",
        ECHOTRACE_BIND_PORT="18001",
        TASK_TIMEOUT_SECONDS="600",
        AGENT_MAX_CALLS="24",
        AGENT_MAX_STEPS="5",
        AGENT_MAX_TOKENS=str(token_budget or model.agent_max_tokens),
        ANALYSIS_STRATEGY="dynamic",
        RETRIEVAL_FUSION="rrf",
        ECHO_V2_TEST_CONFIRM="isolated",
        ECHO_V2_SUPABASE_URL=local["API_URL"],
        ECHO_V2_ANON_KEY=local["ANON_KEY"],
        ECHO_V2_API_URL="http://127.0.0.1:18001",
    )
    async with httpx.AsyncClient(trust_env=False, timeout=15) as http:
        admin = {
            "apikey": local["ANON_KEY"],
            "Authorization": "Bearer " + local["SERVICE_ROLE_KEY"],
        }
        pending = await http.get(
            local["API_URL"] + "/rest/v1/background_jobs",
            headers=admin,
            params={"select": "id", "status": "in.(queued,running)", "limit": "1"},
        )
        pending.raise_for_status()
        if pending.json():
            raise RuntimeError(
                "Pending isolated tasks exist; refusing additional model work"
            )
        email, password = (
            f"real-acceptance-{uuid4().hex}@example.invalid",
            secrets.token_urlsafe(24) + "aA1!",
        )
        response = await http.post(
            local["API_URL"] + "/auth/v1/admin/users",
            headers=admin,
            json={"email": email, "password": password, "email_confirm": True},
        )
        if response.is_error:
            raise RuntimeError("Synthetic account creation failed; response withheld")
        test_user_id = response.json()["id"]
    env.update(ECHO_V2_EMAIL=email, ECHO_V2_PASSWORD=password)
    project = "echotrace-real-acceptance"
    compose = [DOCKER, "compose", "-p", project, "-f", "compose.yaml"]
    receipt = {
        "started_at": datetime.now(UTC).isoformat(),
        "models": {"chat": model.chat_model, "embedding": model.embedding_model},
        "verified_image_source_sha256": source_hash,
        "cases": len(cases),
        "case_ids": [case["case_id"] for case in cases],
        "seeded_case_ids": [case["case_id"] for case in seed_cases],
        "moments": moment_count,
        "physical_call_ceiling": ceiling,
        "analysis_limits": {
            "tokens": token_budget or model.agent_max_tokens,
            "calls": 24,
            "steps": 5,
            "seconds": 600,
        },
        "local_database_only": True,
        "status": "starting",
    }
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    if capture_audit:
        capture_folder = folder / ("audit-" + run_id)
        capture_folder.mkdir(exist_ok=False)
        override = folder / ("compose-audit-" + run_id + ".json")
        override.write_text(
            json.dumps(
                {
                    "services": {
                        "worker": {
                            "command": ["python", "/capture_worker.py"],
                            "environment": {"SYNTHETIC_CAPTURE_USER_ID": test_user_id},
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": (
                                        ROOT / "tools/capture_synthetic_audit.py"
                                    ).as_posix(),
                                    "target": "/capture_worker.py",
                                    "read_only": True,
                                },
                                {
                                    "type": "bind",
                                    "source": capture_folder.as_posix(),
                                    "target": "/capture",
                                },
                            ],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        compose += ["-f", str(override)]
        receipt["synthetic_audit_capture"] = str(capture_folder.relative_to(ROOT))
    path = folder / ("real-model-run-" + run_id + ".json")
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    mock_names = [marker["project_id"] + "-worker-1", marker["project_id"] + "-api-1"]
    command([DOCKER, "stop", *mock_names])
    try:
        command(compose + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "120"])
        print(
            f"Real model containers ready; selected cases: {[case['case_id'] for case in cases]}",
            flush=True,
        )
        selection = [arg for case in cases for arg in ("--case-id", case["case_id"])]
        if seed_all:
            selection.append("--seed-all")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "evaluation/v2/run_comparison.py",
            "--execute",
            "--cases",
            "3",
            "--label",
            "dynamic-rrf",
            "--call-budget",
            str(ceiling),
            *selection,
            cwd=ROOT,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        safe = stdout.decode("utf-8", errors="replace") + stderr.decode(
            "utf-8", errors="replace"
        )
        for value in (
            model.dashscope_api_key,
            local["ANON_KEY"],
            local["SERVICE_ROLE_KEY"],
            password,
            email,
        ):
            safe = safe.replace(value, "[redacted]")
        (folder / ("real-model-run-output-" + run_id + ".txt")).write_text(
            safe, encoding="utf-8"
        )
        receipt["exit_code"] = process.returncode
        receipt["status"] = (
            "runner_completed" if process.returncode == 0 else "runner_failed"
        )
        print(
            "Runner exit:",
            process.returncode,
            "— raw synthetic results saved separately.",
            flush=True,
        )
    finally:
        command(compose + ["stop", "--timeout", "30"])
        async with httpx.AsyncClient(trust_env=False, timeout=15) as http:
            remaining = await http.patch(
                local["API_URL"] + "/rest/v1/background_jobs",
                headers=admin,
                params={
                    "user_id": "eq." + test_user_id,
                    "status": "in.(queued,running)",
                },
                json={
                    "status": "cancelled",
                    "error_code": "ACCEPTANCE_ENDED",
                    "progress": "本轮验收已结束",
                },
            )
            remaining.raise_for_status()
        command([DOCKER, "start", *mock_names])
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--case-id", action="append", choices=["U01", "U02", "U03"])
    parser.add_argument("--seed-all", action="store_true")
    parser.add_argument(
        "--capture-audit", action="store_true", help="仅记录本次合成账号的审计输入输出"
    )
    parser.add_argument(
        "--token-budget", type=int, choices=range(1000, 200001), metavar="1000..200000"
    )
    options = parser.parse_args()
    asyncio.run(
        main(
            options.execute,
            options.case_id,
            options.seed_all,
            options.capture_audit,
            options.token_budget,
        )
    )
