from uuid import UUID

import pytest

from app.domain import UserContext
from app.services.retrieval import PersonalMemoryRetriever, UserIsolationError

USER_A = UUID("11111111-1111-1111-1111-111111111111")
USER_B = UUID("22222222-2222-2222-2222-222222222222")
MEMORY_ID = UUID("33333333-3333-3333-3333-333333333333")
MOMENT_ID = UUID("44444444-4444-4444-4444-444444444444")


class FakeAi:
    async def embedding(self, text: str):
        return [0.1, 0.2], {"model": "test", "usage": {}}


class LeakingDb:
    async def select(self, table, access_token, *, params=None):
        if table == "memories":
            return [{"id": str(MEMORY_ID)}]
        if table == "moments":
            # The requested IDs exist, but belong to another user and must be rejected.
            return [{"id": str(MOMENT_ID), "user_id": str(USER_B)}]
        return []

    async def rpc(self, function, access_token, payload):
        return [
            {
                "memory_id": str(MEMORY_ID),
                "memory_content": "B 用户喜欢滑雪",
                "memory_type": "interest",
                "confidence": 0.9,
                "moment_id": str(MOMENT_ID),
                "moment_content": "我周末去滑雪",
                "occurred_at": "2026-01-01T00:00:00Z",
                "score": 0.95,
            }
        ]


@pytest.mark.asyncio
async def test_cross_user_result_is_rejected_before_return() -> None:
    retriever = PersonalMemoryRetriever(LeakingDb(), FakeAi())
    user = UserContext(id=USER_A, email="a@example.com", access_token="token-a")
    with pytest.raises(UserIsolationError):
        await retriever.search(user, "我聊过滑雪吗")
