"""Upgrade only the marked local legacy database. No models, cloud or reset."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg

ROOT = Path(__file__).resolve().parents[1]


def main(workdir):
    folder = Path(workdir).resolve()
    if not folder.is_relative_to(ROOT / ".acceptance"):
        raise RuntimeError("Only local acceptance workspace permitted")
    marker = json.loads((folder / "acceptance.json").read_text(encoding="utf-8-sig"))
    if not marker.get("local_only") or marker.get("stage") != "legacy":
        raise RuntimeError("Legacy isolation marker required")
    env = {
        **os.environ,
        "PATH": r"D:\DockerDesktop\resources\bin;" + os.environ["PATH"],
    }
    cli = [
        "cmd.exe",
        "/d",
        "/c",
        "npm.cmd",
        "exec",
        "--offline",
        "--package=supabase@2.122.0",
        "--",
        "supabase",
    ]

    def run(args):
        result = subprocess.run(
            cli + args + ["--workdir", str(folder)],
            env=env,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Local CLI command failed; credentials withheld")
        return result.stdout

    local = json.loads(run(["status", "-o", "json"]))
    parsed = urlparse(local["DB_URL"])
    if parsed.hostname != "127.0.0.1" or parsed.port != 55322:
        raise RuntimeError("Isolated local DB required")
    checks = []

    def check(name, ok):
        checks.append({"check": name, "passed": bool(ok)})
        (folder / "upgrade-result.json").write_text(
            json.dumps(checks, indent=2), encoding="utf-8"
        )
        print(("PASS " if ok else "FAIL ") + name, flush=True)
        if not ok:
            raise AssertionError(name)

    uid, mid, iid = str(uuid4()), str(uuid4()), str(uuid4())
    original = "旧版升级合成记录：我开始在午休时看纸质书。"
    expected = hashlib.sha256(original.encode()).hexdigest()
    with psycopg.connect(local["DB_URL"], autocommit=True) as conn:
        check(
            "legacy_database_empty_and_old_schema",
            conn.execute("select count(*) from auth.users").fetchone()[0] == 0
            and conn.execute("select to_regclass('public.background_jobs')").fetchone()[
                0
            ]
            is None,
        )
        with conn.transaction():
            conn.execute(
                "insert into auth.users(id,email) values(%s,%s)",
                (uid, f"legacy-{uid}@example.invalid"),
            )
            conn.execute(
                "insert into public.moments(id,user_id,content,mode) values(%s,%s,%s,'capture')",
                (mid, uid, original),
            )
            conn.execute(
                "insert into public.insights(id,user_id,insight_type,trigger_type,title,body,verification_status,evidence_version,agent_version) values(%s,%s,'fact','user_query','合成旧洞察','旧数据保留检查','PASS','old','old')",
                (iid, uid),
            )
            conn.execute(
                "insert into public.insight_evidence(insight_id,moment_id,user_id) values(%s,%s,%s)",
                (iid, mid, uid),
            )
    for migration in sorted((ROOT / "supabase/migrations").glob("*.sql")):
        if migration.name >= "20261024081048_durable_analysis.sql":
            shutil.copy2(migration, folder / "supabase/migrations" / migration.name)
    run(["migration", "up", "--local"])
    with psycopg.connect(local["DB_URL"], autocommit=True) as conn:
        row = conn.execute(
            "select content,revision,event_at from public.moments where id=%s", (mid,)
        ).fetchone()
        check(
            "original_content_preserved_and_revision_initialized",
            hashlib.sha256(row[0].encode()).hexdigest() == expected
            and row[1] == 1
            and row[2] is None,
        )
        check(
            "old_evidence_preserved",
            conn.execute(
                "select count(*) from public.insight_evidence where insight_id=%s and moment_id=%s",
                (iid, mid),
            ).fetchone()[0]
            == 1,
        )
        check(
            "old_unversioned_result_marked_stale",
            conn.execute(
                "select status from public.insights where id=%s", (iid,)
            ).fetchone()[0]
            == "stale",
        )
        with conn.transaction():
            conn.execute("set local role authenticated")
            conn.execute(
                "select set_config('request.jwt.claims',%s,true)",
                (json.dumps({"sub": uid, "role": "authenticated"}),),
            )
            created = conn.execute(
                "select public.submit_operation('moment','legacy-upgrade-check','untrusted','{\"content\":\"升级后合成记录\"}'::jsonb)"
            ).fetchone()[0]
        check(
            "new_atomic_save_creates_job",
            conn.execute(
                "select count(*) from public.background_jobs where user_id=%s and kind='index'",
                (uid,),
            ).fetchone()[0]
            == 1
            and bool(created["id"]),
        )
        with conn.transaction():
            conn.execute("set local role authenticated")
            conn.execute(
                "select set_config('request.jwt.claims',%s,true)",
                (json.dumps({"sub": str(uuid4()), "role": "authenticated"}),),
            )
            check(
                "upgraded_rls_hides_other_user",
                conn.execute(
                    "select count(*) from public.moments where user_id=%s", (uid,)
                ).fetchone()[0]
                == 0,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workdir")
    main(parser.parse_args().workdir)
