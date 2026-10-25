"""Opt-in local test launcher, outside the production image and queue code."""

import functools
import json
import os
import runpy
import sys
from pathlib import Path
from uuid import UUID


def install_capture(engine_class, user_id, destination):
    owner = str(UUID(user_id))
    original = engine_class._audit_synthesis

    def append(row):
        with Path(destination).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    @functools.wraps(original)
    async def audited(
        self, user, question, evidence, accepted, synthesis, trace_id, *, attempt
    ):
        if str(user.id) != owner:
            return await original(
                self,
                user,
                question,
                evidence,
                accepted,
                synthesis,
                trace_id,
                attempt=attempt,
            )
        append(
            {
                "event": "audit_input",
                "attempt": attempt,
                "evidence_ids": [str(item["moment_id"]) for item in evidence],
                "synthesis": synthesis.model_dump(mode="json"),
            }
        )
        try:
            result = await original(
                self,
                user,
                question,
                evidence,
                accepted,
                synthesis,
                trace_id,
                attempt=attempt,
            )
        except Exception as exc:
            append(
                {"event": "audit_error", "attempt": attempt, "code": type(exc).__name__}
            )
            raise
        append(
            {
                "event": "audit_result",
                "attempt": attempt,
                "result": result.model_dump(mode="json"),
            }
        )
        return result

    engine_class._audit_synthesis = audited


if __name__ == "__main__":
    if os.environ.get("SUPABASE_URL") != "http://host.docker.internal:55321":
        raise SystemExit("Audit capture only permits the isolated local database")
    sys.path.insert(0, "/app")
    from app.services.insights import InsightEngine

    install_capture(
        InsightEngine, os.environ["SYNTHETIC_CAPTURE_USER_ID"], "/capture/audit.jsonl"
    )
    runpy.run_module("app.worker", run_name="__main__")
