"""Prepare a NEW local-only Supabase workspace. Never reset or link a project."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def prepare(*, legacy=False):
    name = "echotrace-acceptance-" + uuid4().hex[:10]
    target = ROOT / ".acceptance" / name
    (target / "supabase/migrations").mkdir(parents=True, exist_ok=False)
    source = (ROOT / "supabase/config.toml").read_text(encoding="utf-8")
    source = re.sub(
        r"^project_id = .*$", f'project_id = "{name}"', source, flags=re.MULTILINE
    )
    # Distinct from the normal project's 543xx ports. No existing volumes reused.
    source = re.sub(r"(?m)^(\w*port) = 543(\d\d)$", r"\1 = 553\2", source)
    source = re.sub(r"(?m)^inspector_port = 8083$", "inspector_port = 55383", source)
    (target / "supabase/config.toml").write_text(source, encoding="utf-8")
    (target / "supabase/seed.sql").write_text(
        "-- Isolated synthetic-data acceptance only.\n", encoding="utf-8"
    )
    hashes = {}
    for path in sorted((ROOT / "supabase/migrations").glob("*.sql")):
        if legacy and path.name >= "20261024081048_durable_analysis.sql":
            continue
        data = path.read_bytes()
        (target / "supabase/migrations" / path.name).write_bytes(data)
        hashes[path.name] = hashlib.sha256(data).hexdigest()
    manifest = {
        "project_id": name,
        "local_only": True,
        "created_at": datetime.now(UTC).isoformat(),
        "stage": "legacy" if legacy else "full",
        "migration_sha256": hashes,
        "execution": "files_prepared_only_not_started",
    }
    (target / "acceptance.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "workdir": str(target),
                "project_id": name,
                "stage": manifest["stage"],
                "database_port": 55322,
                "api_port": 55321,
                "started": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Only copy old migrations for an upgrade-path test",
    )
    prepare(legacy=parser.parse_args().legacy)
