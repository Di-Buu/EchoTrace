from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query

from app.config import get_settings
from app.dependencies import CurrentUser, Database
from app.domain import MomentCreate
from app.service_dependencies import Indexer
from app.services.tasks import submit

router = APIRouter(prefix="/moments", tags=["moments"])


@router.post("")
async def create_moment(
    payload: MomentCreate,
    user: CurrentUser,
    db: Database,
    idempotency_key: str | None = Header(default=None),
) -> dict:
    # DB transaction creates both Moment and required index job.
    moment = await submit(db, user, "moment", payload.model_dump(mode="json"), idempotency_key)
    return moment


@router.get("")
async def list_moments(
    user: CurrentUser,
    db: Database,
    limit: int = Query(default=50, ge=1, le=100),
    before_at: datetime | None = None,
    before_id: UUID | None = None,
) -> list[dict]:
    if (before_at is None) != (before_id is None):
        raise HTTPException(422, "分页位置需要同时包含日期和记录编号")
    if before_at is not None and before_at.tzinfo is None:
        raise HTTPException(422, "分页日期需要包含时区")
    params = {
        "select": "id,user_id,content,mode,input_type,memory_enabled,thread_id,created_at,updated_at",
        "user_id": f"eq.{user.id}",
        "order": "created_at.desc,id.desc",
        "limit": str(limit),
    }
    if before_at is not None:
        stamp = before_at.isoformat()
        params["or"] = f"(created_at.lt.{stamp},and(created_at.eq.{stamp},id.lt.{before_id}))"
    return await db.select(
        "moments",
        user.access_token,
        params=params,
    )


@router.post("/index-pending")
async def index_pending_moments(
    user: CurrentUser,
    db: Database,
    indexer: Indexer,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return (
        await db.rpc(
            "enqueue_indexes",
            user.access_token,
            {
                "p_model": get_settings().embedding_model,
                "p_limit": limit,
                "p_offset": offset,
            },
        )
    )[0]


@router.get("/{moment_id}")
async def get_moment(moment_id: UUID, user: CurrentUser, db: Database) -> dict:
    rows = await db.select(
        "moments",
        user.access_token,
        params={
            "select": (
                "id,user_id,content,mode,input_type,memory_enabled,thread_id,created_at,updated_at,event_at,revision"
            ),
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
    rows = await db.rpc("set_moment_memory", user.access_token, {"p_id": str(moment_id), "p_enabled": enabled})
    if not rows or not rows[0].get("id"):
        raise HTTPException(404, "Moment 不存在")
    return rows[0]


@router.post("/{moment_id}/process")
async def process_moment(
    moment_id: UUID,
    user: CurrentUser,
    indexer: Indexer,
    db: Database,
) -> dict:
    task = await submit(db, user, "index", {"moment_id": str(moment_id)}, None)
    return {"task": task, "indexed_chunks": 0, "memories_created": 0, "insight_refresh_recommended": False}


@router.patch("/{moment_id}")
async def revise_moment(moment_id: UUID, payload: MomentCreate, user: CurrentUser, db: Database) -> dict:
    values = payload.model_dump(mode="json", exclude_unset=True)
    if "event_at" in values:
        values["event_time_source"] = "user_confirmed" if payload.event_at else "unknown"
    rows = await db.update(
        "moments", user.access_token, values, params={"id": f"eq.{moment_id}", "user_id": f"eq.{user.id}"}
    )
    if not rows:
        raise HTTPException(404, "记录不存在")
    return rows[0]


@router.delete("/{moment_id}")
async def delete_moment(moment_id: UUID, user: CurrentUser, db: Database) -> dict:
    rows = await db.rpc("delete_moment_data", user.access_token, {"p_id": str(moment_id)})
    if not rows or not rows[0].get("deleted"):
        raise HTTPException(404, "记录不存在")
    return rows[0]
