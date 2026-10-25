"""Server-only adapter. Identity is bound to the leased job, never model input."""

from app.clients.supabase import SupabaseClient
from app.config import Settings


class LeaseLost(RuntimeError):
    pass


class WorkerDatabase(SupabaseClient):
    TABLES = {
        "moments",
        "threads",
        "thread_messages",
        "memories",
        "memory_sources",
        "moment_index_chunks",
        "weekly_reports",
        "weekly_summary_cards",
        "insights",
        "insight_evidence",
        "product_events",
        "ai_runs",
        "insight_candidates",
    }

    def __init__(self, settings: Settings, job: dict):
        if not settings.supabase_service_role_key:
            raise RuntimeError("Worker 需要仅服务端使用的凭据")
        super().__init__(settings.model_copy(update={"supabase_anon_key": settings.supabase_service_role_key}))
        self.job = job
        self.owner = str(job["user_id"])
        self.token = settings.supabase_service_role_key

    async def control(self, action: str, data: dict | None = None) -> dict:
        rows = await super().rpc(
            "worker_control",
            self.token,
            {
                "p_action": action,
                "p_id": self.job["id"],
                "p_lease": self.job["lease_token"],
                "p_data": data or {},
            },
        )
        if not rows:
            raise LeaseLost("任务已取消或执行租约已失效")
        return rows[0]

    async def rpc(self, function, access_token, payload):
        return await super().rpc(
            "worker_evidence_rpc",
            self.token,
            {
                "p_id": self.job["id"],
                "p_lease": self.job["lease_token"],
                "p_function": function,
                "p_payload": payload,
            },
        )

    async def select(self, table, access_token, *, params=None):
        params = params or {}
        if table not in {"moments", "memories", "memory_sources", "weekly_reports", "insights"}:
            raise PermissionError("Worker 不允许读取该资源")

        def ids(value):
            if not value:
                return None
            from uuid import UUID

            if value.startswith("eq."):
                return [str(UUID(value[3:]))]
            if value.startswith("in.(") and value.endswith(")"):
                return [str(UUID(x)) for x in value[4:-1].split(",")]
            raise ValueError("INVALID_WORKER_IDS")

        before = None
        if params.get("and"):
            value = params["and"]
            if not value.startswith("(created_at.lt.") or not value.endswith(")"):
                raise ValueError("INVALID_WORKER_TIME_FILTER")
            before = value[len("(created_at.lt.") : -1]
        after = params.get("created_at", "")
        if after and not after.startswith("gte."):
            raise ValueError("INVALID_WORKER_TIME_FILTER")
        return await super().rpc(
            "worker_read",
            self.token,
            {
                "p_id": self.job["id"],
                "p_lease": self.job["lease_token"],
                "p_resource": table,
                "p_ids": ids(params.get("id")),
                "p_moment_ids": ids(params.get("moment_id")),
                "p_after": after[4:] if after else None,
                "p_before": before,
                "p_enabled": params["memory_enabled"] == "eq.true" if "memory_enabled" in params else None,
                "p_status": params["status"][3:] if "status" in params else None,
                "p_cache": params["cache_key"][3:] if "cache_key" in params else None,
                "p_offset": int(params.get("offset", 0)),
                "p_limit": int(params.get("limit", 100)),
                "p_desc": params.get("order", "").endswith(".desc"),
            },
        )

    def scoped(self, table, payload):
        if table not in self.TABLES:
            raise PermissionError("Worker 不允许访问该表")
        rows = payload if isinstance(payload, list) else [payload]
        if any(str(row.get("user_id", self.owner)) != self.owner for row in rows):
            raise PermissionError("Worker 数据归属不匹配")
        result = [{**row, "user_id": self.owner} for row in rows]
        if table == "ai_runs":
            result = [{**row, "trace_id": str(self.job["id"])} for row in result]
        return result if isinstance(payload, list) else result[0]

    async def insert(self, table, access_token, payload):
        if table not in {"ai_runs", "product_events"} or isinstance(payload, list):
            raise PermissionError("Worker 写入必须使用专用事务")
        return await super().rpc(
            "worker_log",
            self.token,
            {
                "p_id": self.job["id"],
                "p_lease": self.job["lease_token"],
                "p_kind": table,
                "p_data": self.scoped(table, payload),
            },
        )

    async def upsert(self, table, access_token, payload, *, on_conflict):
        raise PermissionError("Worker 索引必须使用 put_index")

    async def update(self, table, access_token, payload, *, params):
        raise PermissionError("Worker 结果必须使用专用提交函数")

    async def delete(self, table, access_token, *, params):
        raise PermissionError("Worker 不允许直接删除数据")
