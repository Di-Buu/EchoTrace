from datetime import UTC, datetime
from uuid import UUID

from app.domain import RetrievedEvidence
from app.services.insights import InsightEngine


def test_compact_evidence_keeps_source_and_time() -> None:
    moment_id = UUID("11111111-1111-1111-1111-111111111111")
    evidence = [
        RetrievedEvidence(
            memory_id=UUID("22222222-2222-2222-2222-222222222222"),
            memory_content="用户决定学习摄影",
            memory_type="decision",
            confidence=0.95,
            moment_id=moment_id,
            moment_content="我决定这个月开始学摄影",
            occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
            score=0.9,
        )
    ]
    result = InsightEngine._compact_evidence(evidence)
    assert result[0]["moment_id"] == str(moment_id)
    assert result[0]["moment_content"] == "我决定这个月开始学摄影"
    assert result[0]["occurred_at"].startswith("2026-01-02")
