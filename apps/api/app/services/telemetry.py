from typing import Any
from uuid import UUID, uuid4

from app.clients.supabase import SupabaseClient


class Telemetry:
    def __init__(self, db: SupabaseClient, access_token: str, user_id: UUID):
        self.db = db
        self.access_token = access_token
        self.user_id = user_id

    async def ai_run(
        self,
        *,
        task_type: str,
        agent_name: str,
        prompt_version: str,
        metadata: dict[str, Any] | None = None,
        success: bool = True,
        failure_code: str | None = None,
        retrieval_candidates: int = 0,
        evidence_count: int = 0,
        tool_calls: int = 0,
        request_id: str | None = None,
        trace_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        meta = metadata or {}
        usage = meta.get("usage") or {}
        run_id = request_id or str(uuid4())
        payload = {
            "user_id": str(self.user_id),
            "request_id": run_id,
            "trace_id": trace_id or run_id,
            "task_type": task_type,
            "agent_name": agent_name,
            "model": meta.get("model"),
            "prompt_version": prompt_version,
            "success": success,
            "latency_ms": meta.get("latency_ms"),
            "input_tokens": usage.get("prompt_tokens") or usage.get("input_tokens"),
            "output_tokens": usage.get("completion_tokens") or usage.get("output_tokens"),
            "tool_calls": tool_calls,
            "retrieval_candidates": retrieval_candidates,
            "evidence_count": evidence_count,
            "failure_code": failure_code,
            "metadata": {
                "retry_count": meta.get("retry_count", 0),
                **(details or {}),
            },
        }
        try:
            await self.db.insert("ai_runs", self.access_token, payload)
        except Exception:
            # Observability must never turn a conservative AI failure into data loss.
            return

    async def product_event(
        self,
        *,
        event_name: str,
        properties: dict[str, Any],
        session_id: str | None = None,
        request_id: str | None = None,
        client_version: str | None = None,
    ) -> None:
        payload = {
            "user_id": str(self.user_id),
            "event_name": event_name,
            "session_id": session_id,
            "request_id": request_id or str(uuid4()),
            "client_version": client_version,
            "properties": properties,
        }
        try:
            await self.db.insert("product_events", self.access_token, payload)
        except Exception:
            # Analytics must not prevent a user from saving or receiving a response.
            return
