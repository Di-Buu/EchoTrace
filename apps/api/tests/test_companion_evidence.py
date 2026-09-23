from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain import RetrievedEvidence, ThreadMessageCreate, UserContext
from app.services.companion import CompanionService

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
CURRENT_ID = UUID("22222222-2222-2222-2222-222222222222")
PAST_ID = UUID("33333333-3333-3333-3333-333333333333")
THREAD_ID = UUID("44444444-4444-4444-4444-444444444444")


class FakeDb:
    async def insert(self, table, _token, payload):
        if table == "thread_messages":
            return [{
                **payload,
                "id": "55555555-5555-5555-5555-555555555555",
                "created_at": datetime(2026, 9, 23, tzinfo=UTC).isoformat(),
            }]
        return [payload]

    async def select(self, table, _token, *, params=None):
        if table == "thread_messages":
            return [{"role": "user", "content": "你记得我以前喜欢什么吗？"}]
        if table == "threads":
            return [{"summary": None}]
        raise AssertionError(table)


class FakeAi:
    def __init__(self):
        self.context = ""

    async def chat(self, *, system, messages):
        self.context = messages[0]["content"]
        return "我只看到你刚问了这个问题。", {"model": "fake"}


class FakeRetriever:
    def __init__(self, items):
        self.items = items

    async def search(self, *_args, **_kwargs):
        return self.items


def evidence(moment_id: UUID, content: str) -> RetrievedEvidence:
    return RetrievedEvidence(
        memory_id=None,
        memory_content=content,
        memory_type="raw_moment",
        confidence=1,
        moment_id=moment_id,
        moment_content=content,
        occurred_at=datetime(2026, 9, 23, tzinfo=UTC),
        score=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("include_past", [False, True])
async def test_companion_excludes_current_question_from_past_evidence(monkeypatch, include_past) -> None:
    ai = FakeAi()
    items = [evidence(CURRENT_ID, "你记得我以前喜欢什么吗？")]
    if include_past:
        items.append(evidence(PAST_ID, "我喜欢爵士乐。"))
    service = CompanionService(FakeDb(), ai, FakeRetriever(items))

    async def ensure_thread(_user, _payload):
        return THREAD_ID, CURRENT_ID, True

    async def skip_summary(_user, _thread_id, _trace_id):
        return None

    monkeypatch.setattr(service, "_ensure_thread", ensure_thread)
    monkeypatch.setattr(service, "_maybe_update_summary", skip_summary)
    result = await service.reply(
        UserContext(id=USER_ID, access_token="token"),
        ThreadMessageCreate(content="你记得我以前喜欢什么吗？"),
    )

    assert result.evidence_moment_ids == ([PAST_ID] if include_past else [])
    assert result.used_long_term_memory is include_past
    assert str(CURRENT_ID) not in ai.context
