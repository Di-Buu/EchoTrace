import asyncio
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from app.config import get_settings
from app.dependencies import CurrentUser, Database
from app.domain import MomentCreate
from app.service_dependencies import Indexer
from app.services.moment_index import run_moment_index_job
from app.services.telemetry import Telemetry
from app.services.weekly import REPORT_ZONE, week_start_for

router = APIRouter(prefix="/moments", tags=["moments"])


@router.post("")
async def create_moment(
    payload: MomentCreate,
    user: CurrentUser,
    db: Database,
    background_tasks: BackgroundTasks,
) -> dict:
    rows = await db.insert(
        "moments",
        user.access_token,
        {
            "user_id": str(user.id),
            "content": payload.content,
            "mode": payload.mode,
            "input_type": payload.input_type,
            "memory_enabled": payload.memory_enabled,
        },
    )
    moment = rows[0]
    telemetry = Telemetry(db, user.access_token, user.id)
    await asyncio.gather(
        telemetry.product_event(
            event_name="moment_created",
            properties={
                "input_type": payload.input_type,
                "mode": payload.mode,
                "has_image": False,
                "has_audio": False,
            },
        ),
        telemetry.product_event(
            event_name="capture_mode_used" if payload.mode == "capture" else "chat_mode_used",
            properties={"input_type": payload.input_type},
        ),
    )
    if payload.memory_enabled:
        background_tasks.add_task(
            run_moment_index_job,
            user,
            UUID(moment["id"]),
            get_settings(),
        )
    return moment


@router.get("")
async def list_moments(
    user: CurrentUser,
    db: Database,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict]:
    return await db.select(
        "moments",
        user.access_token,
        params={
            "select": "id,user_id,content,mode,input_type,memory_enabled,thread_id,created_at,updated_at",
            "user_id": f"eq.{user.id}",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    )


@router.post("/index-pending")
async def index_pending_moments(
    user: CurrentUser,
    db: Database,
    indexer: Indexer,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    moments = await db.select(
        "moments",
        user.access_token,
        params={
            "select": "id",
            "user_id": f"eq.{user.id}",
            "memory_enabled": "eq.true",
            "order": "created_at.asc",
            "limit": str(limit),
            "offset": str(offset),
        },
    )
    indexed = 0
    for moment in moments:
        indexed += await indexer.index_moment(user, UUID(moment["id"]))
    return {"moments_checked": len(moments), "chunks_available": indexed, "next_offset": offset + len(moments)}


@router.get("/{moment_id}")
async def get_moment(moment_id: UUID, user: CurrentUser, db: Database) -> dict:
    rows = await db.select(
        "moments",
        user.access_token,
        params={
            "select": "id,user_id,content,mode,input_type,memory_enabled,thread_id,created_at,updated_at",
            "id": f"eq.{moment_id}",
            "user_id": f"eq.{user.id}",
            "limit": "1",
        },
    )
    if not rows:
        raise HTTPException(404, "Moment 不存在")
    moment = rows[0]
    if moment.get("thread_id"):
        moment["messages"] = await db.select(
            "thread_messages",
            user.access_token,
            params={
                "select": "id,thread_id,role,content,evidence_moment_ids,created_at",
                "thread_id": f"eq.{moment['thread_id']}",
                "order": "created_at.asc",
            },
        )
    else:
        moment["messages"] = []
    return moment


@router.patch("/{moment_id}/memory")
async def set_memory_enabled(moment_id: UUID, enabled: bool, user: CurrentUser, db: Database) -> dict:
    rows = await db.update(
        "moments",
        user.access_token,
        {"memory_enabled": enabled},
        params={"id": f"eq.{moment_id}", "user_id": f"eq.{user.id}"},
    )
    if not rows:
        raise HTTPException(404, "Moment 不存在")
    moment_week = week_start_for(
        datetime.fromisoformat(rows[0]["created_at"].replace("Z", "+00:00")).astimezone(REPORT_ZONE).date()
    )
    await db.update(
        "weekly_reports",
        user.access_token,
        {"status": "stale"},
        params={"week_start": f"eq.{moment_week.isoformat()}", "user_id": f"eq.{user.id}"},
    )
    if not enabled:
        await db.delete(
            "moment_index_chunks",
            user.access_token,
            params={"moment_id": f"eq.{moment_id}", "user_id": f"eq.{user.id}"},
        )
        sources = await db.select(
            "memory_sources",
            user.access_token,
            params={"select": "memory_id", "moment_id": f"eq.{moment_id}"},
        )
        for source in sources:
            await db.update(
                "memories",
                user.access_token,
                # Opting a Moment out is reversible. Explicit deletion of an old
                # Memory is distinct and still respected by evidence_scope.
                {"status": "disputed"},
                params={
                    "id": f"eq.{source['memory_id']}",
                    "user_id": f"eq.{user.id}",
                    "status": "eq.active",
                },
            )
        await db.update(
            "insights",
            user.access_token,
            {"status": "stale"},
            params={"user_id": f"eq.{user.id}", "status": "eq.active"},
        )
    return rows[0]


@router.post("/{moment_id}/process")
async def process_moment(
    moment_id: UUID,
    user: CurrentUser,
    indexer: Indexer,
) -> dict:
    try:
        chunks = await indexer.index_moment(user, moment_id)
        return {"indexed_chunks": chunks, "memories_created": 0, "insight_refresh_recommended": False}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
