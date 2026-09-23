from uuid import UUID

import pytest

from app.domain import UserContext
from app.services.retrieval import PersonalMemoryRetriever

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
MOMENT_ID = UUID("22222222-2222-2222-2222-222222222222")


class FakeAi:
    async def embedding(self, text: str):
        assert text == "我以前说过读书吗"
        return [0.1, 0.2], {"model": "fake", "usage": {}}


class RawOnlyDb:
    def __init__(self) -> None:
        self.called: list[str] = []

    async def rpc(self, function: str, access_token: str, payload: dict) -> list[dict]:
        assert access_token == "token"
        self.called.append(function)
        if function != "search_personal_moment":
            return []
        return [
            {
                "memory_id": None,
                "memory_content": "我开始尝试每天读书十分钟。",
                "memory_type": "moment",
                "confidence": 1,
                "moment_id": str(MOMENT_ID),
                "moment_content": "我开始尝试每天读书十分钟。",
                "occurred_at": "2026-09-20T08:00:00+00:00",
                "score": 0.8,
            }
        ]

    async def select(self, table: str, access_token: str, *, params: dict) -> list[dict]:
        assert table == "moments"
        assert params["user_id"] == f"eq.{USER_ID}"
        return [{"id": str(MOMENT_ID), "user_id": str(USER_ID)}]


@pytest.mark.asyncio
async def test_raw_moment_is_retrievable_without_curated_memory(monkeypatch) -> None:
    async def no_telemetry(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.retrieval.Telemetry.ai_run", no_telemetry)
    db = RawOnlyDb()
    result = await PersonalMemoryRetriever(db, FakeAi()).search(
        UserContext(id=USER_ID, access_token="token"),
        "我以前说过读书吗",
    )
    assert len(result) == 1
    assert result[0].moment_id == MOMENT_ID
    assert result[0].memory_id is None
    assert "search_personal_moment" in db.called
