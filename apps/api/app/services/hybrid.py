"""Rank fusion and independently scoped time-window searches."""

import asyncio
from datetime import datetime

from app.domain import RetrievedEvidence, UserContext
from app.services.retrieval import PersonalMemoryRetriever


def reciprocal_rank_fusion(rows: list[dict], constant: int = 60) -> list[str]:
    scores: dict[str, float] = {}
    seen = set()
    for row in rows:
        key = (str(row["moment_id"]), row["channel"])
        if key in seen:
            continue
        seen.add(key)
        scores[key[0]] = scores.get(key[0], 0) + 1 / (constant + int(row["rank"]))
    return sorted(scores, key=lambda mid: (-scores[mid], mid))


class HybridRetriever(PersonalMemoryRetriever):
    async def search(
        self,
        user: UserContext,
        query: str,
        *,
        limit=12,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        temporal_coverage=False,
        trace_id=None,
        axis="record",
    ):
        if self.ai.settings.retrieval_fusion == "legacy":
            return await super().search(
                user,
                query,
                limit=limit,
                time_from=time_from,
                time_to=time_to,
                temporal_coverage=temporal_coverage,
                trace_id=trace_id,
            )
        vector, _ = await self.ai.embedding(query)
        raw, cards = await asyncio.gather(
            self.db.rpc(
                "search_evidence_v2",
                user.access_token,
                {
                    "p_query": query,
                    "p_embedding": vector,
                    "p_from": time_from.isoformat() if time_from else None,
                    "p_to": time_to.isoformat() if time_to else None,
                    "p_axis": axis,
                    "p_limit": 30,
                },
            ),
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
        fallback_ids = set()
        if axis == "event":
            # Unknown event dates remain unknown. Record dates only locate material.
            fallback = await self.db.rpc(
                "search_evidence_v2",
                user.access_token,
                {
                    "p_query": query,
                    "p_embedding": vector,
                    "p_from": time_from.isoformat() if time_from else None,
                    "p_to": time_to.isoformat() if time_to else None,
                    "p_axis": "record",
                    "p_limit": 30,
                },
            )
            snapshots = (
                await self.db.rpc(
                    "evidence_snapshot",
                    user.access_token,
                    {"p_ids": list({str(item["moment_id"]) for item in fallback})},
                )
                if fallback
                else []
            )
            fallback_ids = {str(row["id"]) for row in snapshots if row.get("event_at") is None}
            raw.extend(
                {**item, "channel": "record_fallback_" + item["channel"]}
                for item in fallback
                if str(item["moment_id"]) in fallback_ids
            )
        # Card matches expand back to raw sources, then get the same permission/time checks.
        expanded = await self._expand_summary_cards(
            user, cards, time_from if axis == "record" else None, time_to if axis == "record" else None
        )
        for rank, item in enumerate(expanded, 1):
            raw.append({"moment_id": str(item["moment_id"]), "channel": "summary", "rank": rank})
        ordered = reciprocal_rank_fusion(raw)
        self.last_trace = {
            "ranks": raw,
            "axis": axis,
            "from": time_from.isoformat() if time_from else None,
            "to": time_to.isoformat() if time_to else None,
            "record_time_fallback_ids": sorted(fallback_ids),
        }
        if not ordered:
            return []
        originals = await self.db.rpc("evidence_snapshot", user.access_token, {"p_ids": ordered})
        by_id = {str(row["id"]): row for row in originals}
        result = []
        for mid in ordered:
            if mid not in by_id:
                continue
            row = by_id[mid]
            date_text = (row.get("event_at") or row["created_at"]) if axis == "event" else row["created_at"]
            if not date_text:
                continue
            stamp = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
            if (time_from and stamp < time_from) or (time_to and stamp >= time_to):
                continue
            result.append(
                RetrievedEvidence(
                    memory_id=None,
                    memory_content=row["content"],
                    memory_type="moment",
                    confidence=1,
                    moment_id=mid,
                    moment_content=row["content"],
                    occurred_at=row["created_at"],
                    recorded_at=row["created_at"],
                    event_at=row.get("event_at"),
                    event_time_source=row.get("event_time_source", "unknown"),
                    revision=row["revision"],
                    score=1 / (60 + len(result) + 1),
                )
            )
            if len(result) >= limit:
                break
        await self._verify_owner(user, {str(item.moment_id) for item in result})
        if axis == "event":
            self.last_trace["record_time_fallback_ids"] = [
                str(item.moment_id) for item in result if item.event_at is None
            ]
        return result
