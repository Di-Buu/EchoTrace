import asyncio
from datetime import datetime

from app.clients.ai import BailianClient
from app.clients.supabase import SupabaseClient
from app.domain import RetrievedEvidence, UserContext
from app.services.evidence_scope import eligible_moment_ids
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
        temporal_coverage: bool = False,
        trace_id: str | None = None,
    ) -> list[RetrievedEvidence]:
        vector, metadata = await self.ai.embedding(query)
        requested_count = min(30, max(limit, 24)) if temporal_coverage else limit
        common = {
            "p_query_text": query,
            "p_query_embedding": vector,
            "p_match_count": requested_count,
            "p_from": time_from.isoformat() if time_from else None,
            "p_to": time_to.isoformat() if time_to else None,
        }
        raw_rows, legacy_rows, cards = await asyncio.gather(
            self.db.rpc("search_personal_moment", user.access_token, common),
            self.db.rpc("search_personal_memory", user.access_token, common),
            self.db.rpc(
                "search_weekly_summary_card",
                user.access_token,
                {
                    "p_query_text": query,
                    "p_query_embedding": vector,
                    "p_match_count": 6,
                },
            ),
        )
        summary_rows = await self._expand_summary_cards(user, cards, time_from, time_to)
        by_moment: dict[str, dict] = {}
        for row in [*raw_rows, *legacy_rows, *summary_rows]:
            key = str(row["moment_id"])
            if key not in by_moment or float(row.get("score", 0)) > float(by_moment[key].get("score", 0)):
                by_moment[key] = row
        rows = sorted(by_moment.values(), key=lambda item: float(item.get("score", 0)), reverse=True)
        if temporal_coverage:
            rows = self._diversify_time(rows, limit)
        else:
            rows = rows[:limit]
        moment_ids = {str(row["moment_id"]) for row in rows}
        await self._verify_owner(user, moment_ids)
        evidence = [RetrievedEvidence.model_validate(row) for row in rows]
        await Telemetry(self.db, user.access_token, user.id).ai_run(
            task_type="retrieval",
            agent_name="personal_memory_rag",
            prompt_version="evidence-retrieval-v2",
            metadata=metadata,
            retrieval_candidates=len(rows),
            evidence_count=len(moment_ids),
            tool_calls=1,
            trace_id=trace_id,
            details={
                "retrieved_moment_ids": [str(item.moment_id) for item in evidence],
                "time_filter_applied": bool(time_from or time_to),
                "temporal_coverage": temporal_coverage,
                "requested_count": requested_count,
                "raw_candidates": len(raw_rows),
                "summary_candidates": len(cards),
                "embedding_dimension": len(vector),
            },
        )
        return evidence

    async def _expand_summary_cards(
        self,
        user: UserContext,
        cards: list[dict],
        time_from: datetime | None,
        time_to: datetime | None,
    ) -> list[dict]:
        ids = {str(source_id) for card in cards for source_id in card.get("source_moment_ids", [])}
        if not ids:
            return []
        moments = await self.db.select(
            "moments",
            user.access_token,
            params={
                "select": "id,user_id,content,created_at",
                "id": f"in.({','.join(sorted(ids))})",
                "user_id": f"eq.{user.id}",
                "memory_enabled": "eq.true",
            },
        )
        permitted = await eligible_moment_ids(self.db, user, ids)
        by_id = {str(item["id"]): item for item in moments}
        rows = []
        for card in cards:
            for source_id in card.get("source_moment_ids", []):
                moment = by_id.get(str(source_id))
                if not moment or str(source_id) not in permitted:
                    continue
                occurred_at = datetime.fromisoformat(moment["created_at"].replace("Z", "+00:00"))
                if time_from and occurred_at < time_from:
                    continue
                if time_to and occurred_at >= time_to:
                    continue
                rows.append(
                    {
                        "memory_id": None,
                        "memory_content": card["summary"],
                        "memory_type": "weekly_summary",
                        "confidence": 1.0,
                        "moment_id": moment["id"],
                        "moment_content": moment["content"],
                        "occurred_at": moment["created_at"],
                        "score": float(card.get("score", 0)) * 0.9,
                    }
                )
        return rows

    @staticmethod
    def _diversify_time(rows: list[dict], limit: int) -> list[dict]:
        """Keep strong semantic hits while reserving room for early/middle/recent evidence."""
        if len(rows) <= limit:
            return rows
        by_moment: dict[str, dict] = {}
        for row in rows:
            key = str(row["moment_id"])
            if key not in by_moment or float(row.get("score", 0)) > float(by_moment[key].get("score", 0)):
                by_moment[key] = row
        unique = list(by_moment.values())
        if len(unique) <= limit:
            return unique

        semantic_slots = max(3, limit // 2)
        selected = sorted(unique, key=lambda item: float(item.get("score", 0)), reverse=True)[:semantic_slots]
        selected_ids = {str(item["moment_id"]) for item in selected}
        timeline = sorted(unique, key=lambda item: str(item.get("occurred_at", "")))
        remaining = limit - len(selected)
        if remaining > 0:
            if remaining == 1:
                positions = [len(timeline) - 1]
            else:
                positions = [round(index * (len(timeline) - 1) / (remaining - 1)) for index in range(remaining)]
            for position in positions:
                candidate = timeline[position]
                key = str(candidate["moment_id"])
                if key not in selected_ids:
                    selected.append(candidate)
                    selected_ids.add(key)
            for candidate in timeline:
                if len(selected) >= limit:
                    break
                key = str(candidate["moment_id"])
                if key not in selected_ids:
                    selected.append(candidate)
                    selected_ids.add(key)
        return selected[:limit]

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
