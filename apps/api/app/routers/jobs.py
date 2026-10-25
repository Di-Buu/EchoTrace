from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.dependencies import CurrentUser, Database
from app.services.tasks import get_task

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
async def active(user: CurrentUser, db: Database):
    return await db.select(
        "background_jobs",
        user.access_token,
        params={
            "select": "id,status,progress,result_id,error_code",
            "user_id": f"eq.{user.id}",
            "kind": "eq.analysis",
            "status": "in.(queued,running)",
            "order": "created_at.desc",
            "limit": "1",
        },
    )


@router.get("/{task_id}")
async def status(task_id: UUID, user: CurrentUser, db: Database):
    return await get_task(db, user, task_id)


@router.post("/{task_id}/cancel")
async def cancel(task_id: UUID, user: CurrentUser, db: Database):
    await get_task(db, user, task_id)
    return (await db.rpc("cancel_job", user.access_token, {"p_id": str(task_id)}))[0]


@router.get("/{task_id}/result")
async def result(task_id: UUID, user: CurrentUser, db: Database):
    task = await get_task(db, user, task_id)
    if task["status"] != "succeeded" or not task.get("result_id"):
        raise HTTPException(409, "结果尚未就绪")
    rows = await db.select(
        "insights",
        user.access_token,
        params={
            "select": "*,insight_evidence(moment_id,memory_id,stance)",
            "id": f"eq.{task['result_id']}",
            "user_id": f"eq.{user.id}",
            "status": "eq.active",
        },
    )
    if not rows:
        raise HTTPException(410, "来源已变化，结果已失效")
    return rows[0]
