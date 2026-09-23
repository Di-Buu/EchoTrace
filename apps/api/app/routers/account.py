from fastapi import APIRouter

from app.dependencies import CurrentUser, Database

router = APIRouter(prefix="/account", tags=["account"])


@router.delete("/data")
async def delete_personal_data(user: CurrentUser, db: Database) -> dict:
    """Delete the signed-in user's product data while keeping the Auth account."""
    tables = (
        "ai_runs",
        "product_events",
        "weekly_summary_cards",
        "weekly_reports",
        "insight_evidence",
        "insights",
        "insight_candidates",
        "memory_sources",
        "thread_messages",
        "threads",
        "memories",
        "moment_index_chunks",
        "moments",
    )
    deleted: dict[str, int] = {}
    for table in tables:
        rows = await db.delete(
            table,
            user.access_token,
            params={"user_id": f"eq.{user.id}"},
        )
        deleted[table] = len(rows)
    return {"ok": True, "deleted": deleted}
