from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.clients.supabase import SupabaseError
from app.domain import MomentCreate, UserContext
from app.routers.moments import list_moments, revise_moment
from app.services.companion import CompanionService

USER = UserContext(id=UUID("11111111-1111-1111-1111-111111111111"), access_token="synthetic")
MID = UUID("22222222-2222-2222-2222-222222222222")


@pytest.mark.asyncio
async def test_cursor_uses_date_and_id_to_break_timestamp_ties():
    db = AsyncMock()
    stamp = datetime(2026, 10, 25, tzinfo=UTC)
    await list_moments(USER, db, 50, stamp, MID)
    params = db.select.call_args.kwargs["params"]
    assert params["order"] == "created_at.desc,id.desc"
    assert params["user_id"] == f"eq.{USER.id}"
    assert f"id.lt.{MID}" in params["or"] and f"created_at.eq.{stamp.isoformat()}" in params["or"]


@pytest.mark.asyncio
async def test_incomplete_or_timezone_less_cursor_is_rejected():
    for before_at, before_id in [(datetime.now(UTC), None), (datetime(2026, 10, 25), MID)]:
        with pytest.raises(HTTPException) as result:
            await list_moments(USER, AsyncMock(), 50, before_at, before_id)
        assert result.value.status_code == 422


@pytest.mark.asyncio
async def test_content_edit_does_not_reset_permission_or_event_time():
    db = AsyncMock()
    db.update.return_value = [{"id": str(MID), "content": "corrected"}]
    await revise_moment(MID, MomentCreate(content="corrected"), USER, db)
    assert db.update.call_args.args[2] == {"content": "corrected"}


@pytest.mark.asyncio
async def test_chat_conflict_and_busy_have_explicit_http_errors():
    for marker in ["IDEMPOTENCY_CONFLICT", "CHAT_BUSY", "CHAT_LEASE_LOST", "EVIDENCE_CHANGED"]:
        db = AsyncMock()
        db.rpc.side_effect = SupabaseError(marker)
        service = CompanionService(db, AsyncMock(), AsyncMock())
        with pytest.raises(HTTPException) as result:
            await service._turn_rpc(USER, "chat_prepare", {})
        assert result.value.status_code == 409


@pytest.mark.asyncio
async def test_chat_failure_releases_turn_with_same_key_for_retry():
    from app.domain import ThreadMessageCreate

    db = AsyncMock()
    db.rpc.return_value = [{"lease": str(MID)}]
    service = CompanionService(db, AsyncMock(), AsyncMock())
    service._reply_prepared = AsyncMock(side_effect=RuntimeError("synthetic model outage"))
    with pytest.raises(RuntimeError, match="synthetic model outage"):
        await service.reply(USER, ThreadMessageCreate(content="synthetic"), "same-key")
    assert db.rpc.call_args.args[0] == "chat_control"
    assert db.rpc.call_args.args[2] == {
        "p_key": "same-key", "p_lease": str(MID), "p_action": "fail", "p_data": {},
    }
