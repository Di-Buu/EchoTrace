import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.config import Settings
from app.domain import (
    AgentRoute,
    InsightClaim,
    RetrievedEvidence,
    SynthesisOutput,
    UserContext,
    VerifiedClaim,
    VerifierOutput,
)
from app.services.insights import (
    InsightEngine,
    InsufficientEvidenceError,
    SynthesisAuditCheck,
    SynthesisAuditOutput,
)


MOMENT_ID = UUID("11111111-1111-1111-1111-111111111111")
USER_ID = UUID("22222222-2222-2222-2222-222222222222")


class FakeDb:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, dict | list[dict]]] = []

    async def insert(self, table: str, _: str, payload: dict | list[dict]) -> list[dict]:
        self.inserted.append((table, payload))
        if table == "insights":
            assert isinstance(payload, dict)
            return [{**payload, "id": "insight-1"}]
        assert isinstance(payload, list)
        return payload


class FakeAi:
    def __init__(self, audit_results: list[bool], *, omit_last_check: bool = False) -> None:
        self.audit_results = iter(audit_results)
        self.omit_last_check = omit_last_check
        self.synthesis_inputs: list[dict] = []

    async def structured_chat(self, **kwargs: object) -> tuple[object, dict]:
        schema = kwargs["schema"]
        payload = json.loads(str(kwargs["user"]))
        if schema is SynthesisOutput:
            self.synthesis_inputs.append(payload)
            if len(self.synthesis_inputs) == 1:
                return SynthesisOutput(
                    title="已建立稳定习惯",
                    body="用户已建立稳定的阅读习惯。",
                ), {"model": "test"}
            return SynthesisOutput(
                title="最近六周的阅读记录",
                body="用户说最近六周大多每周阅读三次，之后能否保持仍未知。",
            ), {"model": "test"}
        assert schema is SynthesisAuditOutput
        supported = next(self.audit_results)
        segments = payload["segments_to_check"]
        if self.omit_last_check:
            segments = segments[:-1]
        return SynthesisAuditOutput(
            supported=supported,
            unsupported_phrases=[] if supported else ["已建立稳定习惯"],
            reason="有原文支持" if supported else "六周记录不能证明长期稳定",
            checks=[
                SynthesisAuditCheck(
                    segment_id=segment["segment_id"],
                    supported=supported,
                    source_moment_ids=[MOMENT_ID],
                    reason="原文支持" if supported else "超出原文",
                )
                for segment in segments
            ],
        ), {"model": "test"}


def make_engine(db: FakeDb, ai: FakeAi) -> InsightEngine:
    engine = InsightEngine(db, ai, object(), Settings())  # type: ignore[arg-type]
    claim = InsightClaim(
        claim="用户说最近六周大多每周阅读三次。",
        claim_type="fact",
        source_moment_ids=[MOMENT_ID],
        confidence=0.9,
    )

    async def fake_reader(*_: object) -> list[InsightClaim]:
        return [claim]

    async def fake_verify(*_: object) -> VerifierOutput:
        return VerifierOutput(
            claims=[
                VerifiedClaim(
                    **claim.model_dump(),
                    verification_status="PASS",
                    verifier_note="原文支持有限观察",
                )
            ]
        )

    async def fake_trace(*_: object, **__: object) -> None:
        return None

    engine._simple_reader = fake_reader  # type: ignore[method-assign]
    engine._verify = fake_verify  # type: ignore[method-assign]
    engine._trace = fake_trace  # type: ignore[method-assign]
    return engine


async def analyze(engine: InsightEngine) -> dict:
    return await engine._analyze_and_store(
        user=UserContext(id=USER_ID, access_token="test-token"),
        question="我的阅读情况怎么样？",
        route=AgentRoute(
            route_type="simple",
            specialists=[],
            insight_type="fact",
            retrieval_query="阅读",
        ),
        evidence=[
            RetrievedEvidence(
                memory_id=None,
                memory_content="",
                memory_type="moment",
                confidence=1,
                moment_id=MOMENT_ID,
                moment_content="最近六周大多能保持每周读三次。",
                occurred_at=datetime(2026, 5, 10, tzinfo=UTC),
                score=1,
            )
        ],
        trigger_type="user_query",
        trace_id="trace-1",
    )


@pytest.mark.asyncio
async def test_unsupported_synthesis_is_repaired_and_reaudited_before_storage() -> None:
    db = FakeDb()
    ai = FakeAi([False, True])
    result = await analyze(make_engine(db, ai))

    assert result["title"] == "最近六周的阅读记录"
    assert "大多" in result["body"]
    assert len(ai.synthesis_inputs) == 2
    assert ai.synthesis_inputs[0]["evidence"][0]["moment_content"] == "最近六周大多能保持每周读三次。"
    assert "已建立稳定习惯" in ai.synthesis_inputs[1]["audit_feedback"]["unsupported_phrases"]
    assert [table for table, _ in db.inserted] == ["insights", "insight_evidence"]


@pytest.mark.asyncio
async def test_twice_unsupported_synthesis_is_not_published() -> None:
    db = FakeDb()
    ai = FakeAi([False, False])

    with pytest.raises(InsufficientEvidenceError, match="成稿未通过"):
        await analyze(make_engine(db, ai))

    assert db.inserted == []


@pytest.mark.asyncio
async def test_incomplete_sentence_audit_is_not_published() -> None:
    db = FakeDb()
    ai = FakeAi([True, True], omit_last_check=True)

    with pytest.raises(InsufficientEvidenceError, match="成稿未通过"):
        await analyze(make_engine(db, ai))

    assert len(ai.synthesis_inputs) == 2
    assert db.inserted == []
