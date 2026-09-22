from uuid import UUID

import pytest

from app.services.telemetry import Telemetry


class RecordingDb:
    def __init__(self) -> None:
        self.payload: dict | None = None

    async def insert(self, table: str, access_token: str, payload: dict) -> list[dict]:
        assert table == "ai_runs"
        assert access_token == "token"
        self.payload = payload
        return [payload]


@pytest.mark.asyncio
async def test_ai_run_keeps_shared_trace_and_sanitized_details() -> None:
    db = RecordingDb()
    telemetry = Telemetry(db, "token", UUID("11111111-1111-1111-1111-111111111111"))  # type: ignore[arg-type]

    await telemetry.ai_run(
        task_type="insight",
        agent_name="temporal_agent",
        prompt_version="temporal-v1",
        metadata={"model": "qwen", "latency_ms": 120, "usage": {"input_tokens": 10}, "retry_count": 1},
        trace_id="trace-1",
        details={"route_type": "complex"},
    )

    assert db.payload is not None
    assert db.payload["trace_id"] == "trace-1"
    assert db.payload["input_tokens"] == 10
    assert db.payload["metadata"] == {"retry_count": 1, "route_type": "complex"}
    assert "content" not in db.payload["metadata"]
