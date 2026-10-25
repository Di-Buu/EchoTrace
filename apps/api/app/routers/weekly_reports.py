from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.dependencies import CurrentUser, Database
from app.service_dependencies import AiClient, Insights
from app.services.evidence_scope import eligible_moment_ids
from app.services.retrieval import PersonalMemoryRetriever
from app.services.weekly import WeeklyReportService

router = APIRouter(prefix="/weekly-reports", tags=["weekly-reports"])


@router.get("")
async def list_weekly_reports(user: CurrentUser, db: Database) -> list[dict]:
    reports = await db.select(
        "weekly_reports",
        user.access_token,
        params={
            "select": "id,week_start,week_end,status,digest,insight_id,created_at,updated_at",
            "user_id": f"eq.{user.id}",
            "order": "week_start.desc",
            "limit": "52",
        },
    )
    source_ids = {
        str(moment_id)
        for report in reports
        for card in report.get("digest", [])
        for moment_id in card.get("source_moment_ids", [])
    }
    enabled = await eligible_moment_ids(db, user, source_ids)
    for report in reports:
        if report["status"] == "stale" or any(
            str(moment_id) not in enabled
            for card in report.get("digest", [])
            for moment_id in card.get("source_moment_ids", [])
        ):
            report["status"] = "stale"
            report["digest"] = []
            report["insight_id"] = None
    return reports


@router.post("/ensure")
async def ensure_weekly_reports(
    user: CurrentUser,
    db: Database,
    ai: AiClient,
    engine: Insights,
) -> dict:
    settings = get_settings()
    if not settings.background_tasks_available:
        raise HTTPException(503, "在线周报后台任务尚未启用")
    retriever = PersonalMemoryRetriever(db, ai)
    service = WeeklyReportService(db, ai, retriever, engine, settings)
    reports, scheduled = await service.ensure_due(user)
    # A DB trigger creates the durable weekly job in the report transaction.
    return {"weeks_checked": len(reports), "scheduled": len(scheduled)}


@router.get("/{report_id}")
async def get_weekly_report(report_id: UUID, user: CurrentUser, db: Database) -> dict:
    rows = await db.select(
        "weekly_reports",
        user.access_token,
        params={
            "select": "id,week_start,week_end,status,digest,insight_id,created_at,updated_at",
            "id": f"eq.{report_id}",
            "user_id": f"eq.{user.id}",
            "limit": "1",
        },
    )
    if not rows:
        raise HTTPException(404, "周报不存在")
    report = rows[0]
    source_ids = {
        str(moment_id) for card in report.get("digest", []) for moment_id in card.get("source_moment_ids", [])
    }
    permitted = await eligible_moment_ids(db, user, source_ids)
    if report["status"] == "stale" or source_ids - permitted:
        report["status"] = "stale"
        report["digest"] = []
        report["insight_id"] = None
    return report
