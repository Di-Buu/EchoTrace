"""Browser acceptance using only marked local Supabase and a mock provider."""

import argparse
import json
import os
import secrets
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
DOCKER = r"D:\DockerDesktop\resources\bin\docker.exe"


def verify_weekly(client, local, signed, output):
    headers = {
        "apikey": local["ANON_KEY"],
        "Authorization": "Bearer " + signed["access_token"],
    }
    checks = []

    def check(name, ok):
        checks.append({"check": name, "passed": bool(ok)})
        (output / "checks.json").write_text(
            json.dumps(checks, indent=2), encoding="utf-8"
        )
        print(("PASS " if ok else "FAIL ") + name, flush=True)
        if not ok:
            raise AssertionError(name)

    zone = timezone(timedelta(hours=8))
    today = datetime.now(UTC).astimezone(zone)
    monday = (today - timedelta(days=today.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    older = monday - timedelta(weeks=3)
    last = monday - timedelta(weeks=1)
    rows = [
        {
            "user_id": signed["user"]["id"],
            "content": "合成周回顾：这周留下一条散步记录。",
            "mode": "capture",
            "created_at": (older + timedelta(days=1)).isoformat(),
        }
    ]
    rows += [
        {
            "user_id": signed["user"]["id"],
            "content": f"合成周回顾第{i + 1}条：今天午休读了几页书。",
            "mode": "capture",
            "created_at": (last + timedelta(hours=12 + i * 12)).isoformat(),
        }
        for i in range(8)
    ]
    response = client.post(
        local["API_URL"] + "/rest/v1/moments",
        headers={**headers, "Prefer": "return=representation"},
        json=rows,
    )
    check("weekly_synthetic_records_created", response.status_code == 201)
    moments = response.json()
    for _ in range(120):
        jobs = client.get(
            local["API_URL"] + "/rest/v1/background_jobs",
            headers=headers,
            params={"kind": "eq.index", "select": "status"},
        ).json()
        if len(jobs) == 9 and all(job["status"] == "succeeded" for job in jobs):
            break
        time.sleep(1)
    check(
        "weekly_fixture_durable_indexes_ready",
        len(jobs) == 9 and all(job["status"] == "succeeded" for job in jobs),
    )
    response = client.post(
        "http://127.0.0.1:18000/weekly-reports/ensure", headers=headers
    )
    check(
        "weekly_reports_queued",
        response.status_code == 200 and response.json()["scheduled"] == 3,
    )
    weeks = []
    for _ in range(180):
        response = client.get("http://127.0.0.1:18000/weekly-reports", headers=headers)
        response.raise_for_status()
        weeks = response.json()
        if len(weeks) == 3 and all(week["status"] != "processing" for week in weeks):
            break
        time.sleep(1)
    by_start = {week["week_start"]: week for week in weeks}
    check("weekly_history_three_weeks_preserved", len(weeks) == 3)
    check(
        "single_record_week_insufficient_with_original",
        by_start[older.date().isoformat()]["status"] == "insufficient",
    )
    empty = by_start[(older + timedelta(weeks=1)).date().isoformat()]
    check("empty_week_has_no_records_state", empty["status"] == "no_records")
    current = by_start[last.date().isoformat()]
    check(
        "populated_week_completed_with_insight",
        current["status"] == "completed" and bool(current["insight_id"]),
    )
    response = client.get(
        local["API_URL"] + "/rest/v1/background_jobs",
        headers=headers,
        params={"kind": "eq.weekly", "select": "status,payload,checkpoint"},
    )
    response.raise_for_status()
    empty_job = next(
        job for job in response.json() if job["payload"]["report_id"] == empty["id"]
    )
    check(
        "empty_week_uses_no_model_calls",
        empty_job["status"] == "succeeded"
        and empty_job["checkpoint"].get("calls", 0) == 0,
    )
    response = client.post(
        "http://127.0.0.1:18000/weekly-reports/ensure", headers=headers
    )
    check(
        "completed_weeks_not_requeued",
        response.status_code == 200 and response.json()["scheduled"] == 0,
    )
    response = client.patch(
        "http://127.0.0.1:18000/moments/" + moments[-1]["id"] + "/memory",
        headers=headers,
        params={"enabled": "false"},
    )
    check("weekly_source_revoke", response.status_code == 200)
    response = client.get(
        "http://127.0.0.1:18000/weekly-reports/" + current["id"], headers=headers
    )
    check(
        "weekly_revocation_hides_stale_digest",
        response.status_code == 200
        and response.json()["status"] == "stale"
        and not response.json()["digest"]
        and response.json()["insight_id"] is None,
    )


def main(weekly_only=False):
    env = {**os.environ, "PATH": str(Path(DOCKER).parent) + ";" + os.environ["PATH"]}
    folder = ROOT / ".acceptance/echotrace-acceptance-222c498ffb"
    marker = json.loads((folder / "acceptance.json").read_text(encoding="utf-8-sig"))
    if not marker.get("local_only"):
        raise RuntimeError("Isolation marker required")

    def command(args):
        result = subprocess.run(
            args, cwd=ROOT, env=env, capture_output=True, check=False
        )
        if result.returncode:
            raise RuntimeError("Command failed; raw output withheld")
        return result.stdout.decode("utf-8", errors="replace")

    running = json.loads(
        command([DOCKER, "inspect", "echotrace-real-acceptance-worker-1"])
    )[0]
    if running["State"]["Running"]:
        raise RuntimeError(
            "Wait for real-model acceptance before changing the local mock environment"
        )
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
        raise RuntimeError("Only isolated local Supabase allowed")
    env.update(
        SUPABASE_URL="http://host.docker.internal:55321",
        SUPABASE_ANON_KEY=local["ANON_KEY"],
        SUPABASE_SERVICE_ROLE_KEY=local["SERVICE_ROLE_KEY"],
        DASHSCOPE_API_KEY="mock",
        CHAT_MODEL="mock",
        EMBEDDING_MODEL="mock",
        ECHOTRACE_IMAGE="echotrace:acceptance",
        ECHOTRACE_BIND_PORT="18000",
        AGENT_MAX_TOKENS="40000",
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
    command(compose + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "120"])
    build = ROOT / ".acceptance/web-local"
    if not (build / "index.html").exists():
        raise RuntimeError("First export the local-only web build")

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if (
                not Path(self.translate_path(self.path)).is_file()
                and not Path(self.path).suffix
            ):
                self.path = "/index.html"
            super().do_GET()

    server = ThreadingHTTPServer(
        ("127.0.0.1", 8081), partial(Handler, directory=str(build))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(trust_env=False, timeout=15) as client:
            email, password = (
                f"ui-{uuid4().hex}@example.invalid",
                secrets.token_urlsafe(24) + "aA1!",
            )
            admin = {
                "apikey": local["ANON_KEY"],
                "Authorization": "Bearer " + local["SERVICE_ROLE_KEY"],
            }
            response = client.post(
                local["API_URL"] + "/auth/v1/admin/users",
                headers=admin,
                json={"email": email, "password": password, "email_confirm": True},
            )
            response.raise_for_status()
            signed = client.post(
                local["API_URL"] + "/auth/v1/token?grant_type=password",
                headers={"apikey": local["ANON_KEY"]},
                json={"email": email, "password": password},
            )
            signed.raise_for_status()
            output = folder / ("browser-ui-" + uuid4().hex[:8])
            output.mkdir()
            if weekly_only:
                verify_weekly(client, local, signed.json(), output)
                print("Local mock weekly checks:", output)
                return
            browser_env = {
                **os.environ,
                "UI_TEST_EMAIL": email,
                "UI_TEST_PASSWORD": password,
                "UI_TEST_TOKEN": signed.json()["access_token"],
                "UI_PUBLIC_KEY": local["ANON_KEY"],
                "UI_RESULT_DIR": str(output),
            }
            result = subprocess.run(
                ["node.exe", "tools/check_local_web.mjs"],
                cwd=ROOT,
                env=browser_env,
                capture_output=True,
                check=False,
            )
            text = result.stdout.decode(
                "utf-8", errors="replace"
            ) + result.stderr.decode("utf-8", errors="replace")
            for value in (
                email,
                password,
                browser_env["UI_TEST_TOKEN"],
                local["ANON_KEY"],
                local["SERVICE_ROLE_KEY"],
            ):
                text = text.replace(value, "[redacted]")
            (output / "run.log").write_text(text, encoding="utf-8")
            print(text)
            print("Browser result directory:", output)
            raise SystemExit(result.returncode)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--weekly-only", action="store_true")
    main(parser.parse_args().weekly_only)
