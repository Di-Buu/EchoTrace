from datetime import datetime

from app.clients.ai import BailianClient
from app.clients.supabase import SupabaseClient
from app.domain import RetrievedEvidence, UserContext
from app.prompts import MEMORY_CURATOR_VERSION
from app.services.telemetry import Telemetry


class UserIsolationError(RuntimeError):
    pass


class PersonalMemoryRetriever:
    def __init__(self, db: SupabaseClient, ai: BailianClient):
        self.db = db
        self.ai = ai

    async def search(
        self,
        user: UserContext,
        query: str,
        *,
        limit: int = 8,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        trace_id: str | None = None,
    ) -> list[RetrievedEvidence]:
        active = await self.db.select(
            "memories",
            user.access_token,
            params={"select": "id", "status": "eq.active", "limit": "1"},
        )
        if not active:
            return []

        vector, metadata = await self.ai.embedding(query)
        rows = await self.db.rpc(
            "search_personal_memory",
            user.access_token,
            {
                "p_query_text": query,
                "p_query_embedding": vector,
                "p_match_count": limit,
                "p_from": time_from.isoformat() if time_from else None,
                "p_to": time_to.isoformat() if time_to else None,
            },
        )
        moment_ids = {str(row["moment_id"]) for row in rows}
        await self._verify_owner(user, moment_ids)
        evidence = [RetrievedEvidence.model_validate(row) for row in rows]
        await Telemetry(self.db, user.access_token, user.id).ai_run(
            task_type="retrieval",
            agent_name="personal_memory_rag",
            prompt_version=MEMORY_CURATOR_VERSION,
            metadata=metadata,
            retrieval_candidates=len(rows),
            evidence_count=len(moment_ids),
            tool_calls=1,
            trace_id=trace_id,
            details={
                "time_filter_applied": bool(time_from or time_to),
                "embedding_dimension": len(vector),
            },
        )
        return evidence

    async def _verify_owner(self, user: UserContext, moment_ids: set[str]) -> None:
        if not moment_ids:
            return
        encoded_ids = ",".join(sorted(moment_ids))
        rows = await self.db.select(
            "moments",
            user.access_token,
            params={
                "select": "id,user_id",
                "id": f"in.({encoded_ids})",
                "user_id": f"eq.{user.id}",
            },
        )
        returned = {str(row["id"]) for row in rows if str(row.get("user_id")) == str(user.id)}
        if returned != moment_ids:
            raise UserIsolationError("检索结果的 owner 校验失败，已终止输出")
