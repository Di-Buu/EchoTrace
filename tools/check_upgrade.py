"""Local syntax/config and frozen-file checks, no database or model calls."""

import hashlib
import json
from pathlib import Path

import yaml
from pglast import parse_plpgsql, parse_sql

root = Path(__file__).resolve().parents[1]
sql = (root / "supabase/migrations/20261024081048_durable_analysis.sql").read_text(
    encoding="utf-8"
)
print("SQL statements:", len(parse_sql(sql)))
print("PL/pgSQL definitions:", len(parse_plpgsql(sql)))
compose = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))
assert compose["services"]["api"]["image"] == compose["services"]["worker"]["image"]
assert "SUPABASE_SERVICE_ROLE_KEY" not in compose["services"]["api"]["environment"]
assert compose["services"]["worker"]["read_only"]
metrics = root / "evaluation/results/final-holdout-v2/final_holdout_metrics.json"
if metrics.exists():
    metadata = json.loads(metrics.read_text(encoding="utf-8"))
    hashes = metadata.get("source_sha256", metadata.get("source_hashes", {}))
    if not hashes:
        hashes = {
            k: v
            for section in metadata.values()
            if isinstance(section, dict)
            for k, v in section.items()
            if str(k).startswith("evaluation/")
        }
    checked = 0
    for name, expected in hashes.items():
        if name.endswith(
            (
                "raw_results.jsonl",
                "final_holdout_v2.jsonl",
                "final_holdout_v2.lock.json",
                "manifest.json",
                "metrics.json",
            )
        ):
            assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, (
                name
            )
            checked += 1
    assert checked >= 4, "没有找到足够的封存 hash，需人工核实"
    print("Frozen sources unchanged:", checked)
print("Static checks passed; this is NOT a Docker/DB integration result.")
