from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.dependencies import CurrentUser, Database
from app.domain import ThreadMessageCreate
from app.service_dependencies import Companion
from app.services.telemetry import Telemetry

router = APIRouter(tags=["chat"])


@router.post("/chat")
async def chat(payload: ThreadMessageCreate, user: CurrentUser, companion: Companion) -> dict:
    try:
        return (await companion.reply(user, payload)).model_dump(mode="json")
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/threads/{thread_id}/messages")
async def list_messages(thread_id: UUID, user: CurrentUser, db: Database) -> list[dict]:
    thread = await db.select(
        "threads",
        user.access_token,
        params={
            "select": "id",
            "id": f"eq.{thread_id}",
            "user_id": f"eq.{user.id}",
            "limit": "1",
        },
    )
    if not thread:
        raise HTTPException(404, "Thread 不存在")
    return await db.select(
        "thread_messages",
        user.access_token,
        params={
            "select": "id,thread_id,role,content,evidence_moment_ids,created_at",
            "thread_id": f"eq.{thread_id}",
            "order": "created_at.asc",
        },
    )


@router.post("/threads/{thread_id}/resumed")
async def mark_resumed(thread_id: UUID, user: CurrentUser, db: Database) -> dict:
    threads = await db.select(
        "threads",
        user.access_token,
        params={
            "select": "id",
            "id": f"eq.{thread_id}",
            "user_id": f"eq.{user.id}",
            "limit": "1",
        },
    )
    if not threads:
        raise HTTPException(404, "Thread 不存在")
    messages = await db.select(
        "thread_messages",
        user.access_token,
        params={
            "select": "created_at",
            "thread_id": f"eq.{thread_id}",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    inactive_duration = 0
    if messages:
        last_message_at = datetime.fromisoformat(messages[0]["created_at"].replace("Z", "+00:00"))
        if last_message_at.tzinfo is None:
            last_message_at = last_message_at.replace(tzinfo=UTC)
        inactive_duration = max(0, round((datetime.now(UTC) - last_message_at).total_seconds()))
    await Telemetry(db, user.access_token, user.id).product_event(
        event_name="thread_resumed",
        properties={"thread_id": str(thread_id), "inactive_duration": inactive_duration},
    )
    return {"ok": True}
