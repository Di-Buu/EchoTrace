from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.config import Settings
from app.domain import CuratorOutput, MemoryCandidate, RetrievedEvidence, UserContext
from app.services.insights import InsightEngine
from app.services.memory import MemoryCurator

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
OLD_MEMORY_ID = UUID("22222222-2222-2222-2222-222222222222")
NEW_MEMORY_ID = UUID("33333333-3333-3333-3333-333333333333")
CURRENT_MOMENT_ID = UUID("44444444-4444-4444-4444-444444444444")
OLD_MOMENT_IDS = [
    UUID("55555555-5555-5555-5555-555555555555"),
    UUID("66666666-6666-6666-6666-666666666666"),
]


class MemoryDb:
    def __init__(self) -> None:
        self.source_payload: list[dict] = []

    async def select(self, table: str, _: str, *, params: dict | None = None) -> list[dict]:
        if table == "moments":
            return [
                {
                    "id": str(CURRENT_MOMENT_ID),
                    "user_id": str(USER_ID),
                    "content": "我现在固定在晚饭后阅读二十分钟。",
                    "memory_enabled": True,
                    "created_at": datetime(2026, 9, 22, tzinfo=UTC).isoformat(),
                    "thread_id": None,
                }
            ]
        if table == "memories":
            return [
                {
                    "id": str(OLD_MEMORY_ID),
                    "memory_type": "state",
                    "content": "用户尝试恢复阅读习惯。",
                    "status": "active",
                    "occurred_at": None,
                    "updated_at": datetime(2026, 9, 20, tzinfo=UTC).isoformat(),
                }
            ]
        if table == "memory_sources":
            return [{"moment_id": str(item)} for item in OLD_MOMENT_IDS]
        return []

    async def insert(self, table: str, _: str, payload: dict | list[dict]) -> list[dict]:
        if table == "memories":
            assert isinstance(payload, dict)
            return [{**payload, "id": str(NEW_MEMORY_ID)}]
        if table == "memory_sources":
            assert isinstance(payload, list)
            self.source_payload = payload
        return payload if isinstance(payload, list) else [payload]

    async def update(self, _: str, __: str, payload: dict, *, params: dict) -> list[dict]:
        return [payload]


class MemoryAi:
    settings = Settings(embedding_model="test-embedding")

    async def structured_chat(self, **_: object) -> tuple[CuratorOutput, dict]:
        return (
            CuratorOutput(
                memories=[
                    MemoryCandidate(
                        content="用户目前固定在晚饭后阅读二十分钟。",
                        memory_type="state",
                        confidence=0.95,
                        operation="update",
                        related_memory_id=OLD_MEMORY_ID,
                    )
                ]
            ),
            {"model": "test"},
        )

    async def embedding(self, _: str) -> tuple[list[float], dict]:
        return [0.1, 0.2, 0.3], {"model": "test-embedding"}


@pytest.mark.asyncio
async def test_memory_update_inherits_all_previous_source_moments() -> None:
    db = MemoryDb()
    curator = MemoryCurator(db, MemoryAi())  # type: ignore[arg-type]
    user = UserContext(id=USER_ID, access_token="token")

    await curator.process_moment(user, CURRENT_MOMENT_ID)

    inherited = {row["moment_id"] for row in db.source_payload}
    assert inherited == {str(CURRENT_MOMENT_ID), *(str(item) for item in OLD_MOMENT_IDS)}
    assert all(row["memory_id"] == str(NEW_MEMORY_ID) for row in db.source_payload)


class InsightDb:
    async def select(self, table: str, _: str, *, params: dict | None = None) -> list[dict]:
        if table == "memories":
            return [
                {
                    "id": str(OLD_MEMORY_ID),
                    "content": "固定时间阅读",
                    "memory_type": "state",
                    "confidence": 0.9,
                    "occurred_at": datetime(2026, 9, 1, tzinfo=UTC).isoformat(),
                    "updated_at": datetime(2026, 9, 22, tzinfo=UTC).isoformat(),
                },
                {
                    "id": str(NEW_MEMORY_ID),
                    "content": "保持每周三次阅读",
                    "memory_type": "goal",
                    "confidence": 0.9,
                    "occurred_at": datetime(2026, 9, 2, tzinfo=UTC).isoformat(),
                    "updated_at": datetime(2026, 9, 22, tzinfo=UTC).isoformat(),
                },
            ]
        if table == "insights":
            return []
        raise AssertionError(f"Unexpected table: {table}")


@pytest.mark.asyncio
async def test_automatic_insight_threshold_counts_evidence_moments_not_merged_memories() -> None:
    db = InsightDb()
    engine = InsightEngine(
        db,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        Settings(auto_insight_min_memories=4),
    )
    user = UserContext(id=USER_ID, access_token="token")
    evidence = [
        RetrievedEvidence(
            memory_id=OLD_MEMORY_ID if index < 2 else NEW_MEMORY_ID,
            memory_content="阅读记录",
            memory_type="state",
            confidence=0.9,
            moment_id=UUID(f"77777777-7777-7777-7777-{index:012d}"),
            moment_content=f"第 {index} 条阅读记录",
            occurred_at=datetime(2026, 9, index, tzinfo=UTC),
            score=1,
        )
        for index in range(1, 5)
    ]
    called: dict[str, object] = {}

    async def fake_evidence(_: UserContext, __: list[dict]) -> list[RetrievedEvidence]:
        return evidence

    async def fake_analyze(**kwargs: object) -> dict:
        called.update(kwargs)
        return {"id": "insight-1"}

    engine._evidence_from_memories = fake_evidence  # type: ignore[method-assign]
    engine._analyze_and_store = fake_analyze  # type: ignore[method-assign]

    result = await engine.maybe_generate_automatic(user)

    assert result == {"id": "insight-1"}
    assert called["evidence"] == evidence
