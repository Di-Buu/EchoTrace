from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.dependencies import CurrentUser, Database
from app.domain import InsightQuery
from app.service_dependencies import Insights
from app.services.insights import InsufficientEvidenceError
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


@router.post("/query")
async def query_insight(
    payload: InsightQuery,
    user: CurrentUser,
    db: Database,
    engine: Insights,
) -> dict:
    try:
        result = await engine.answer(user, payload.question)
        evidence_count = len(result.get("evidence", []))
        await Telemetry(db, user.access_token, user.id).product_event(
            event_name="long_term_query",
            properties={
                "route_type": "grounded",
                "has_answer": True,
                "evidence_count": evidence_count,
            },
        )
        return result
    except InsufficientEvidenceError as exc:
        await Telemetry(db, user.access_token, user.id).product_event(
            event_name="long_term_query",
            properties={"route_type": "insufficient", "has_answer": False, "evidence_count": 0},
        )
        raise HTTPException(422, "这次没能找到足够可靠的线索。") from exc


@router.post("/refresh")
async def refresh_automatic(user: CurrentUser, engine: Insights) -> dict:
    result = await engine.maybe_generate_automatic(user)
    return {"insight": result}


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
