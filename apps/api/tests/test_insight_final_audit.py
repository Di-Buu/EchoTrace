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
        raise AssertionError("Insight must use one atomic RPC, not split writes")

    async def rpc(self, function: str, _: str, payload: dict) -> list[dict]:
        if function == "evidence_snapshot":
            return [
                {
                    "id": mid,
                    "revision": 1,
                    "content": "最近六周大多能保持每周读三次。"
                    if mid == str(MOMENT_ID)
                    else "此前开始把阅读安排在晚饭后。",
                }
                for mid in payload["p_ids"]
            ]
        assert function == "commit_insight"
        self.inserted.append((function, payload))
        return [{**payload["p_insight"], "id": "insight-1", "evidence": payload["p_evidence"]}]


class FakeAi:
    def __init__(
        self,
        audit_results: list[bool],
        *,
        omit_last_check: bool = False,
        first_body: str = "用户已建立稳定的阅读习惯。",
        audit_source_ids: list[UUID] | None = None,
    ) -> None:
        self.audit_results = iter(audit_results)
        self.omit_last_check = omit_last_check
        self.first_body = first_body
        self.audit_source_ids = audit_source_ids or [MOMENT_ID]
        self.synthesis_inputs: list[dict] = []
        self.request_options: list[tuple[object, dict]] = []

    async def structured_chat(self, **kwargs: object) -> tuple[object, dict]:
        schema = kwargs["schema"]
        self.request_options.append((schema, {k: kwargs.get(k) for k in ("enable_thinking", "max_tokens")}))
        payload = json.loads(str(kwargs["user"]))
        if schema is SynthesisOutput:
            self.synthesis_inputs.append(payload)
            if len(self.synthesis_inputs) == 1:
                return SynthesisOutput(
                    title="已建立稳定习惯",
                    body=self.first_body,
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
                    source_moment_ids=self.audit_source_ids,
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


async def analyze(engine: InsightEngine, *, extra_evidence_count: int = 0) -> dict:
    evidence = [
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
    ]
    for index in range(extra_evidence_count):
        evidence.append(
            RetrievedEvidence(
                memory_id=None,
                memory_content="",
                memory_type="moment",
                confidence=1,
                moment_id=UUID(int=MOMENT_ID.int + index + 1),
                moment_content="此前开始把阅读安排在晚饭后。",
                occurred_at=datetime(2026, 3, 10, tzinfo=UTC),
                score=1,
            )
        )
    return await engine._analyze_and_store(
        user=UserContext(id=USER_ID, access_token="test-token"),
        question="我的阅读情况怎么样？",
        route=AgentRoute(
            route_type="simple",
            specialists=[],
            insight_type="fact",
            retrieval_query="阅读",
        ),
        evidence=evidence,
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
    assert [table for table, _ in db.inserted] == ["commit_insight"]


@pytest.mark.asyncio
async def test_writer_budget_does_not_remove_final_evidence_audit() -> None:
    db = FakeDb()
    ai = FakeAi([False, True])
    engine = make_engine(db, ai)
    engine.settings = Settings(_env_file=None, insight_enable_thinking=True)
    await analyze(engine)
    writers = [options for schema, options in ai.request_options if schema is SynthesisOutput]
    audits = [options for schema, options in ai.request_options if schema is SynthesisAuditOutput]
    assert len(writers) == len(audits) == 2
    assert all(options == {"enable_thinking": False, "max_tokens": 2000} for options in writers)
    assert all(options == {"enable_thinking": True, "max_tokens": 6000} for options in audits)
    assert all("output_schema" not in payload for payload in ai.synthesis_inputs)
    assert len(db.inserted) == 1  # No publication before the second audit succeeds.


@pytest.mark.asyncio
async def test_twice_unsupported_synthesis_is_not_published() -> None:
    db = FakeDb()
    ai = FakeAi([False, False])

    with pytest.raises(InsufficientEvidenceError, match="成稿未通过"):
        await analyze(make_engine(db, ai))

    assert db.inserted == []


@pytest.mark.asyncio
async def test_evidence_rich_user_query_can_pass_after_second_bounded_repair() -> None:
    db = FakeDb()
    ai = FakeAi([False, False, True])

    result = await analyze(make_engine(db, ai), extra_evidence_count=1)

    assert result["title"] == "最近六周的阅读记录"
    assert len(ai.synthesis_inputs) == 3
    assert ai.synthesis_inputs[2]["audit_feedback"]["unsupported_phrases"]
    assert [table for table, _ in db.inserted] == ["commit_insight"]


@pytest.mark.asyncio
async def test_evidence_rich_user_query_still_rejects_three_unsupported_drafts() -> None:
    db = FakeDb()
    ai = FakeAi([False, False, False])

    with pytest.raises(InsufficientEvidenceError, match="成稿未通过"):
        await analyze(make_engine(db, ai), extra_evidence_count=1)

    assert len(ai.synthesis_inputs) == 3
    assert db.inserted == []


@pytest.mark.asyncio
async def test_incomplete_sentence_audit_is_not_published() -> None:
    db = FakeDb()
    ai = FakeAi([True, True], omit_last_check=True)

    with pytest.raises(InsufficientEvidenceError, match="成稿未通过"):
        await analyze(make_engine(db, ai))

    assert len(ai.synthesis_inputs) == 2
    assert db.inserted == []


@pytest.mark.asyncio
async def test_audit_cannot_attach_a_moment_outside_retrieved_evidence() -> None:
    db = FakeDb()
    unrelated_id = UUID(int=MOMENT_ID.int + 100)
    ai = FakeAi([True, True], audit_source_ids=[unrelated_id])

    with pytest.raises(InsufficientEvidenceError, match="成稿未通过"):
        await analyze(make_engine(db, ai))

    assert db.inserted == []


@pytest.mark.parametrize(
    "body",
    [
        "出差可能与停跑有关。",
        "晚饭后的安排有助于维持阅读。",
        "在状态良好时享受聚会。",
        "你目前尚未决定读研。",
        "你通过选择空间来定义或追求自由。",
        "你在周末持续进行摄影活动。",
        "你倾向于在疲劳时回避社交。",
        "晚饭后多次与阅读及较易进入状态的体验相关联。",
        "晚饭后是有效时间段。",
    ],
)
def test_risk_guard_catches_unrecorded_personal_inference(body: str) -> None:
    synthesis = SynthesisOutput(title="记录观察", body=body)
    evidence = [{"moment_content": "最近六周大多能保持每周读三次。"}]
    assert InsightEngine._unsupported_risk_phrases(synthesis, evidence)


def test_repeated_weekly_frequency_must_keep_source_uncertainty() -> None:
    evidence = [{"moment_content": "最近六周大多能保持每周读三次。"}]
    unsupported = SynthesisOutput(
        title="阅读记录",
        body="最近六周大多每周读三次。在3月安排后维持了每周三次的频率。",
    )
    supported = SynthesisOutput(
        title="阅读记录",
        body="截至5月记录，最近六周大多每周读三次。",
    )

    assert "在3月安排后维持了每周三次的频率" in InsightEngine._unsupported_risk_phrases(unsupported, evidence)
    assert not InsightEngine._unsupported_risk_phrases(supported, evidence)


@pytest.mark.asyncio
async def test_rule_guard_repairs_even_when_model_audit_approves() -> None:
    db = FakeDb()
    ai = FakeAi([True, True], first_body="阅读安排可能与频率有关。")

    result = await analyze(make_engine(db, ai))

    assert len(ai.synthesis_inputs) == 2
    assert "可能与频率有关" in ai.synthesis_inputs[1]["audit_feedback"]["unsupported_phrases"]
    assert "可能与" not in result["body"]


@pytest.mark.asyncio
async def test_final_audit_sources_are_persisted_even_if_upstream_claim_omitted_them() -> None:
    db = FakeDb()
    second_id = UUID(int=MOMENT_ID.int + 1)
    ai = FakeAi([True], first_body="用户说最近六周大多每周读三次。", audit_source_ids=[MOMENT_ID, second_id])

    result = await analyze(make_engine(db, ai), extra_evidence_count=1)

    assert {row["moment_id"] for row in result["evidence"]} == {str(MOMENT_ID), str(second_id)}


def test_dated_cooccurrence_does_not_license_causal_wording() -> None:
    source_id = str(MOMENT_ID)
    checks = {0: SynthesisAuditCheck(segment_id=0, supported=True, source_moment_ids=[MOMENT_ID], reason="ok")}
    evidence = [
        {
            "moment_id": source_id,
            "moment_content": "这周出差，连续两次没有跑步。",
            "occurred_at": "2026-03-01T08:00:00Z",
        }
    ]

    assert InsightEngine._unsupported_structured_claims(
        [{"segment_id": 0, "text": "当周因出差连续两次没有跑步"}], checks, evidence
    )
    evidence[0]["moment_content"] = "这周因为出差连续两次没有跑步。"
    assert not InsightEngine._unsupported_structured_claims(
        [{"segment_id": 0, "text": "当周因出差连续两次没有跑步"}], checks, evidence
    )


def test_factor_word_is_not_a_causal_assertion() -> None:
    checks = {0: SynthesisAuditCheck(segment_id=0, supported=True, source_moment_ids=[MOMENT_ID], reason="ok")}
    evidence = [
        {"moment_id": str(MOMENT_ID), "moment_content": "最近睡得好一些。", "occurred_at": "2026-01-05T08:00:00Z"}
    ]
    assert not InsightEngine._unsupported_structured_claims(
        [{"segment_id": 0, "text": "记录未提供排除其他因素影响的对照数据"}], checks, evidence
    )


def test_factor_word_in_source_does_not_license_real_causal_claim() -> None:
    checks = {0: SynthesisAuditCheck(segment_id=0, supported=True, source_moment_ids=[MOMENT_ID], reason="ok")}
    evidence = [
        {"moment_id": str(MOMENT_ID), "moment_content": "同时还有其他因素。", "occurred_at": "2026-01-05T08:00:00Z"}
    ]
    assert InsightEngine._unsupported_structured_claims(
        [{"segment_id": 0, "text": "因为运动所以睡眠改善"}], checks, evidence
    )


@pytest.mark.parametrize(
    "text,blocked",
    [
        ("由于这些记录在时间上重合，无法判断它们之间是否存在因果", False),
        ("由于证据不足，无法证明两者存在因果", False),
        ("由于运动导致睡眠改善的记录在时间上重合，无法判断其他因素", True),
        ("由于运动，睡眠改善了", True),
    ],
)
def test_evidence_limitation_does_not_hide_an_actual_causal_assertion(text, blocked):
    checks = {0: SynthesisAuditCheck(segment_id=0, supported=True, source_moment_ids=[MOMENT_ID], reason="ok")}
    evidence = [
        {
            "moment_id": str(MOMENT_ID),
            "moment_content": "本月午间散步，最近睡得更好些。",
            "recorded_at": "2026-01-05T08:00:00Z",
        }
    ]
    assert (
        bool(InsightEngine._unsupported_structured_claims([{"segment_id": 0, "text": text}], checks, evidence))
        == blocked
    )


def test_two_isolated_events_do_not_establish_continuous_activity() -> None:
    second_id = UUID(int=MOMENT_ID.int + 1)
    checks = {
        0: SynthesisAuditCheck(segment_id=0, supported=True, source_moment_ids=[MOMENT_ID, second_id], reason="ok")
    }
    evidence = [
        {"moment_id": str(MOMENT_ID), "moment_content": "这周开始学摄影。", "occurred_at": "2026-01-05T08:00:00Z"},
        {"moment_id": str(second_id), "moment_content": "周末又出去拍了照片。", "occurred_at": "2026-02-05T08:00:00Z"},
    ]

    assert InsightEngine._unsupported_structured_claims(
        [{"segment_id": 0, "text": "摄影活动仍在持续"}], checks, evidence
    )


def test_record_span_is_computed_from_cited_moment_dates() -> None:
    second_id = UUID(int=MOMENT_ID.int + 1)
    checks = {
        0: SynthesisAuditCheck(segment_id=0, supported=True, source_moment_ids=[MOMENT_ID, second_id], reason="ok")
    }
    evidence = [
        {
            "moment_id": str(MOMENT_ID),
            "moment_content": "这周因为生病没有去跑步。",
            "occurred_at": "2026-02-09T08:00:00Z",
        },
        {
            "moment_id": str(second_id),
            "moment_content": "过去三周都保持每周跑两次。",
            "occurred_at": "2026-03-09T08:00:00Z",
        },
    ]

    assert InsightEngine._unsupported_structured_claims(
        [{"segment_id": 0, "text": "现有记录的时间跨度为三周"}], checks, evidence
    )
    assert not InsightEngine._unsupported_structured_claims(
        [{"segment_id": 0, "text": "现有记录的时间跨度为四周"}], checks, evidence
    )


@pytest.mark.asyncio
async def test_independent_verifier_can_restore_a_counterexample_omitted_by_planner():
    second = UUID(int=MOMENT_ID.int + 1)
    unrelated = UUID(int=MOMENT_ID.int + 2)
    db = FakeDb()
    ai = FakeAi([True], first_body="用户说最近六周大多每周阅读三次。", audit_source_ids=[MOMENT_ID, second])
    engine = make_engine(db, ai)
    seen = []

    async def verify(user, question, payload, claims, trace_id):
        seen.extend(item["moment_id"] for item in payload)
        return VerifierOutput(
            claims=[
                VerifiedClaim(
                    claim="用户说最近六周大多每周阅读三次。",
                    claim_type="fact",
                    source_moment_ids=[MOMENT_ID],
                    counter_evidence_moment_ids=[second],
                    confidence=1,
                    verification_status="PASS",
                    verifier_note="合成核验记录",
                )
            ]
        )

    engine._verify = verify
    pool = [
        RetrievedEvidence(
            memory_content="合成",
            memory_type="moment",
            confidence=1,
            moment_id=mid,
            moment_content="合成",
            occurred_at=datetime(2026, 5, 10, tzinfo=UTC),
            score=1,
        )
        for mid in (MOMENT_ID, second, unrelated)
    ]
    await engine._analyze_and_store(
        user=UserContext(id=USER_ID, access_token="test-token"),
        question="合成问题",
        route=AgentRoute(route_type="simple", specialists=[], insight_type="fact", retrieval_query="合成"),
        evidence=pool[:1],
        review_evidence=pool,
        trigger_type="user_query",
        trace_id="scope-test",
    )
    assert set(seen) == {str(MOMENT_ID), str(second), str(unrelated)}
    assert {item["moment_id"] for item in ai.synthesis_inputs[0]["evidence"]} == {str(MOMENT_ID), str(second)}
    assert any(
        row["moment_id"] == str(second) and row["stance"] == "counter" for row in db.inserted[0][1]["p_evidence"]
    )
