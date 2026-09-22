from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.dependencies import CurrentUser, Database
from app.domain import MemoryPatch
from app.service_dependencies import AiClient
from app.services.telemetry import Telemetry

router = APIRouter(prefix="/memories", tags=["memories"])


@router.get("")
async def list_memories(user: CurrentUser, db: Database) -> list[dict]:
    return await db.select(
        "memories",
        user.access_token,
        params={
            "select": (
                "id,memory_type,content,confidence,status,occurred_at,created_at,updated_at,memory_sources(moment_id)"
            ),
            "status": "in.(active,disputed)",
            "order": "updated_at.desc",
        },
    )


@router.patch("/{memory_id}")
async def correct_memory(
    memory_id: UUID,
    payload: MemoryPatch,
    user: CurrentUser,
    db: Database,
    ai: AiClient,
) -> dict:
    vector, _ = await ai.embedding(payload.content)
    rows = await db.update(
        "memories",
        user.access_token,
        {
            "content": payload.content,
            "embedding": vector,
            "embedding_model": ai.settings.embedding_model,
            "embedding_dimension": len(vector),
            "status": "active",
            "confidence": 1,
        },
        params={"id": f"eq.{memory_id}", "user_id": f"eq.{user.id}"},
    )
    if not rows:
        raise HTTPException(404, "Memory 不存在")
    await db.update(
        "insights",
        user.access_token,
        {"status": "stale"},
        params={"user_id": f"eq.{user.id}", "status": "eq.active"},
    )
    await Telemetry(db, user.access_token, user.id).product_event(
        event_name="memory_corrected",
        properties={"memory_type": rows[0]["memory_type"], "correction_type": "user_edit"},
    )
    return rows[0]


@router.delete("/{memory_id}")
async def delete_memory(memory_id: UUID, user: CurrentUser, db: Database) -> dict:
    rows = await db.update(
        "memories",
        user.access_token,
        {"status": "deleted"},
        params={"id": f"eq.{memory_id}", "user_id": f"eq.{user.id}"},
    )
    if not rows:
        raise HTTPException(404, "Memory 不存在")
    await db.update(
        "insights",
        user.access_token,
        {"status": "stale"},
        params={"user_id": f"eq.{user.id}", "status": "eq.active"},
    )
    sources = await db.select(
        "memory_sources",
        user.access_token,
        params={"select": "moment_id", "memory_id": f"eq.{memory_id}"},
    )
    await Telemetry(db, user.access_token, user.id).product_event(
        event_name="memory_deleted",
        properties={"memory_type": rows[0]["memory_type"], "source_count": len(sources)},
    )
    return {"ok": True}
