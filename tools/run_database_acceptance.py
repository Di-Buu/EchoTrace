"""Run real local DB tests without printing status credentials or loading .env."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main(workdir):
    target = Path(workdir).resolve()
    if not target.is_relative_to(ROOT / ".acceptance"):
        raise SystemExit("Only isolated local workspace allowed")
    marker = json.loads((target / "acceptance.json").read_text(encoding="utf-8-sig"))
    if marker.get("local_only") is not True:
        raise SystemExit("Local isolation marker missing")
    env = {
        **os.environ,
        "PATH": r"D:\DockerDesktop\resources\bin;" + os.environ["PATH"],
    }
    status = subprocess.run(
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
            str(target),
        ],
        env=env,
        capture_output=True,
        check=False,
    )
    if status.returncode:
        raise SystemExit("Local Supabase is not ready; credentials not printed")
    local = json.loads(status.stdout)
    dsn = local["DB_URL"]
    parsed = urlparse(dsn)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != 55322:
        raise SystemExit("Refusing nonisolated DB URL")
    env.update(
        ECHO_TEST_DATABASE_URL=dsn,
        ECHO_TEST_DB_CONFIRM="isolated",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    run = subprocess.run(
        [
            str(ROOT / "apps/api/.venv/Scripts/python.exe"),
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            str(ROOT / "apps/api/tests/integration"),
            "-q",
            "--tb=short",
            "-o",
            f"cache_dir={ROOT / '.test-cache'}",
            f"--basetemp={ROOT / ('.test-tmp-db-' + uuid4().hex[:8])}",
        ],
        env=env,
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    text = run.stdout.decode("utf-8", errors="replace") + run.stderr.decode(
        "utf-8", errors="replace"
    )
    for value in local.values():
        if isinstance(value, str) and (
            value.startswith(("eyJ", "sb_secret_", "sb_publishable_", "postgresql://"))
        ):
            text = text.replace(value, "[local credential redacted]")
    (target / "database-test-result.txt").write_text(text, encoding="utf-8")
    print(text)
    raise SystemExit(run.returncode)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workdir")
    main(parser.parse_args().workdir)
