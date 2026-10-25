"""Start only an isolated local workspace; redact CLI-generated keys from logs."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(workdir):
    target = Path(workdir).resolve()
    if not target.is_relative_to(ROOT / ".acceptance"):
        raise SystemExit("Only .acceptance workspaces are permitted")
    marker = json.loads((target / "acceptance.json").read_text(encoding="utf-8-sig"))
    if marker.get("local_only") is not True or not marker["project_id"].startswith(
        "echotrace-acceptance-"
    ):
        raise SystemExit("Missing local isolation marker")
    env = {
        **os.environ,
        "PATH": r"D:\DockerDesktop\resources\bin;" + os.environ["PATH"],
    }
    # Supported upstream override; use official source images, not a third-party mirror.
    env["SUPABASE_INTERNAL_IMAGE_REGISTRY"] = "docker.io"
    command = [
        "cmd.exe",
        "/d",
        "/c",
        "npm.cmd",
        "exec",
        "--offline",
        "--package=supabase@2.122.0",
        "--",
        "supabase",
        "start",
        "--workdir",
        str(target),
        "-x",
        "studio,realtime,storage-api,imgproxy,edge-runtime,logflare,vector,supavisor,postgres-meta,mailpit",
    ]
    with (
        subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        ) as process,
        (target / "start-redacted.log").open("a", encoding="utf-8") as log,
    ):
        for raw_line in process.stdout:
            if re.search(
                r"(?i)(secret|publishable|anon.?key|service.?role|jwt.?secret|password|api.?key|postgres(?:ql)?://)",
                raw_line,
            ):
                line = "[local credential line redacted]"
            else:
                line = re.sub(
                    r"\b(?:eyJ|sb_secret_|sb_publishable_)[A-Za-z0-9_.-]+",
                    "[redacted]",
                    raw_line.rstrip(),
                )
                line = re.sub(
                    r'(https?://[^?\s"]+)\?[^\s"]+', r"\1?[query-redacted]", line
                )
            log.write(line + "\n")
            log.flush()
            print(line, flush=True)
        code = process.wait()
    if code == 0:
        for service in ("db", "auth", "rest", "kong"):
            name = f"supabase_{service}_{marker['project_id']}"
            inspected = subprocess.run(
                [r"D:\DockerDesktop\resources\bin\docker.exe", "inspect", name],
                capture_output=True,
                check=False,
            )
            if (
                inspected.returncode
                or not json.loads(inspected.stdout)[0]["State"]["Running"]
            ):
                print(
                    f"Required local service not running: {service}; this is NOT successful startup",
                    flush=True,
                )
                code = 1
                break
    print(
        json.dumps(
            {
                "exit": code,
                "workdir": str(target),
                "log": "start-redacted.log",
            }
        )
    )
    raise SystemExit(code)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workdir")
    main(parser.parse_args().workdir)
