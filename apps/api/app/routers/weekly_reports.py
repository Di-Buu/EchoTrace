from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.clients.ai import BailianClient
from app.clients.supabase import SupabaseClient
from app.config import Settings, get_settings
from app.dependencies import CurrentUser, Database
from app.domain import UserContext
from app.service_dependencies import AiClient, Insights
from app.services.evidence_scope import eligible_moment_ids
from app.services.insights import InsightEngine
from app.services.retrieval import PersonalMemoryRetriever
from app.services.weekly import WeeklyReportService

router = APIRouter(prefix="/weekly-reports", tags=["weekly-reports"])


async def run_weekly_report_job(user: UserContext, report_id: str, settings: Settings) -> None:
    db = SupabaseClient(settings)
    try:
        ai = BailianClient(settings)
        retriever = PersonalMemoryRetriever(db, ai)
        engine = InsightEngine(db, ai, retriever, settings)
        await WeeklyReportService(db, ai, retriever, engine, settings).run(user, UUID(report_id))
    finally:
        await db.aclose()


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
    background_tasks: BackgroundTasks,
) -> dict:
    settings = get_settings()
    if settings.vercel or settings.insight_execution_profile == "online_demo":
        raise HTTPException(503, "在线周报后台任务尚未启用")
    retriever = PersonalMemoryRetriever(db, ai)
    service = WeeklyReportService(db, ai, retriever, engine, settings)
    reports, scheduled = await service.ensure_due(user)
    for report_id in scheduled:
        background_tasks.add_task(run_weekly_report_job, user, report_id, settings)
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
        str(moment_id)
        for card in report.get("digest", [])
        for moment_id in card.get("source_moment_ids", [])
    }
    permitted = await eligible_moment_ids(db, user, source_ids)
    if report["status"] == "stale" or source_ids - permitted:
        report["status"] = "stale"
        report["digest"] = []
        report["insight_id"] = None
    return report
