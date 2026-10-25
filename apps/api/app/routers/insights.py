import logging
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from app.config import get_settings
from app.dependencies import CurrentUser, Database
from app.domain import InsightQuery
from app.service_dependencies import Insights
from app.services.tasks import submit
from app.services.telemetry import Telemetry

router = APIRouter(prefix="/insights", tags=["insights"])


class FeedbackPayload(BaseModel):
    rating: str


@router.get("")
async def list_insights(
    user: CurrentUser,
    db: Database,
    limit: int = Query(default=30, ge=1, le=100),
) -> list[dict]:
    return await db.select(
        "insights",
        user.access_token,
        params={
            "select": (
                "id,insight_type,trigger_type,query,title,body,limitation,verification_status,"
                "time_start,time_end,feedback,created_at,insight_evidence(moment_id,memory_id,stance)"
            ),
            "status": "eq.active",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    )


@router.get("/{insight_id}")
async def get_insight(insight_id: UUID, user: CurrentUser, db: Database) -> dict:
    rows = await db.select(
        "insights",
        user.access_token,
        params={
            "select": ("*,insight_evidence(moment_id,memory_id,stance,moments(id,content,created_at,input_type))"),
            "id": f"eq.{insight_id}",
            "status": "eq.active",
            "limit": "1",
        },
    )
    if not rows:
        raise HTTPException(404, "Insight 不存在或已失效")
    return rows[0]


@router.post("/query", status_code=202)
async def query_insight(
    payload: InsightQuery,
    request: Request,
    user: CurrentUser,
    db: Database,
    engine: Insights,
    idempotency_key: str | None = Header(default=None),
) -> dict:
    if not get_settings().background_tasks_available:
        raise HTTPException(503, "后台整理服务尚未启用，请稍后再试")
    task = await submit(db, user, "analysis", payload.model_dump(mode="json"), idempotency_key)
    logging.getLogger("echotrace.tasks").info(
        "analysis_accepted request_id=%s task_id=%s", getattr(request.state, "request_id", "unknown"), task["id"]
    )
    return task


@router.post("/refresh", status_code=202)
async def refresh_automatic(user: CurrentUser, db: Database) -> dict:
    if not get_settings().background_tasks_available:
        raise HTTPException(503, "后台整理服务尚未启用，请稍后再试")
    task = await submit(
        db,
        user,
        "analysis",
        {
            "question": "根据现有记录，是否有值得回看的变化或重复？证据不足时明确说明。",
        },
        None,
    )
    return {"insight": None, "task": task}


@router.post("/{insight_id}/feedback")
async def feedback(
    insight_id: UUID,
    payload: FeedbackPayload,
    user: CurrentUser,
    db: Database,
) -> dict:
    if payload.rating not in {"match", "partial", "mismatch"}:
        raise HTTPException(422, "不支持的反馈值")
    rows = await db.update(
        "insights",
        user.access_token,
        {"feedback": payload.rating},
        params={"id": f"eq.{insight_id}", "user_id": f"eq.{user.id}"},
    )
    if not rows:
        raise HTTPException(404, "Insight 不存在")
    await Telemetry(db, user.access_token, user.id).product_event(
        event_name="insight_feedback",
        properties={"insight_id": str(insight_id), "rating": payload.rating},
    )
    return rows[0]
