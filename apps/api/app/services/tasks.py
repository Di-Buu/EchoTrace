"""Durable operations: every RPC is one short database transaction."""

import hashlib
import json
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.clients.supabase import SupabaseClient, SupabaseError
from app.domain import UserContext


def request_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


async def submit(db: SupabaseClient, user: UserContext, operation: str, payload: dict, key: str | None) -> dict:
    key = key or str(uuid4())
    if not 1 <= len(key) <= 128:
        raise HTTPException(422, "请求标识长度应为 1–128")
    try:
        rows = await db.rpc(
            "submit_operation",
            user.access_token,
            {
                "p_operation": operation,
                "p_key": key,
                "p_hash": request_hash(payload),
                "p_payload": payload,
            },
        )
    except SupabaseError as exc:
        if "IDEMPOTENCY_CONFLICT" in str(exc):
            raise HTTPException(409, "相同请求标识不能用于不同内容") from exc
        if "SOURCE_UNAVAILABLE" in str(exc):
            raise HTTPException(404, "记录不存在或已关闭长期使用") from exc
        raise
    return rows[0]


async def get_task(db: SupabaseClient, user: UserContext, task_id: UUID) -> dict:
    rows = await db.select(
        "background_jobs",
        user.access_token,
        params={
            "select": "id,kind,status,progress,attempts,error_code,result_id,created_at,updated_at",
            "id": f"eq.{task_id}",
            "user_id": f"eq.{user.id}",
            "limit": "1",
        },
    )
    if not rows:
        raise HTTPException(404, "任务不存在")
    return rows[0]
