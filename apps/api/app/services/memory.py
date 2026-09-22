import asyncio
import json
from datetime import datetime
from uuid import UUID, uuid4

from app.clients.ai import BailianClient
from app.clients.supabase import SupabaseClient
from app.domain import CuratorOutput, UserContext
from app.prompts import MEMORY_CURATOR_SYSTEM, MEMORY_CURATOR_VERSION
from app.services.telemetry import Telemetry


class MemoryCurator:
    def __init__(self, db: SupabaseClient, ai: BailianClient):
        self.db = db
        self.ai = ai

    async def process_moment(self, user: UserContext, moment_id: UUID) -> list[dict]:
        trace_id = str(uuid4())
        telemetry = Telemetry(self.db, user.access_token, user.id)
        moments = await self.db.select(
            "moments",
            user.access_token,
            params={
                "select": "id,user_id,content,memory_enabled,created_at,thread_id",
                "id": f"eq.{moment_id}",
                "user_id": f"eq.{user.id}",
                "limit": "1",
            },
        )
        if not moments:
            raise LookupError("Moment 不存在")
        moment = moments[0]
        if not moment["memory_enabled"]:
            return []

        existing = await self.db.select(
            "memories",
            user.access_token,
            params={
                "select": "id,memory_type,content,status,occurred_at,updated_at",
                "status": "in.(active,disputed)",
                "order": "updated_at.desc",
                "limit": "30",
            },
        )
        user_prompt = json.dumps(
            {
                "moment": {
                    "id": moment["id"],
                    "content": moment["content"],
                    "created_at": moment["created_at"],
                },
                "existing_memories": existing,
                "output_schema": {
                    "memories": [
                        {
                            "content": "string",
                            "memory_type": "event|view|interest|goal|decision|question|state",
                            "occurred_at": "ISO datetime or null",
                            "confidence": "0..1",
                            "operation": "create|update|conflict|skip",
                            "related_memory_id": "provided UUID or null",
                            "reasoning": "short internal justification",
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )
        output, metadata = await self.ai.structured_chat(
            system=MEMORY_CURATOR_SYSTEM,
            user=user_prompt,
            schema=CuratorOutput,
            temperature=0,
        )

        valid_existing_ids = {str(item["id"]) for item in existing}
        candidates = [candidate for candidate in output.memories if candidate.operation != "skip"]
        embeddings = await asyncio.gather(*(self.ai.embedding(candidate.content) for candidate in candidates))
        await asyncio.gather(
            *(
                telemetry.ai_run(
                    task_type="embedding",
                    agent_name="memory_curator_embedding",
                    prompt_version="embedding-v1",
                    metadata=embedding_meta,
                    trace_id=trace_id,
                    details={"embedding_dimension": len(vector)},
                )
                for vector, embedding_meta in embeddings
            )
        )
        written: list[dict] = []
        for candidate, (vector, _) in zip(candidates, embeddings, strict=True):
            related_id = str(candidate.related_memory_id) if candidate.related_memory_id else None
            if related_id and related_id not in valid_existing_ids:
                related_id = None
            source_moment_ids = {str(moment["id"])}
            if candidate.operation == "update" and related_id:
                previous_sources = await self.db.select(
                    "memory_sources",
                    user.access_token,
                    params={"select": "moment_id", "memory_id": f"eq.{related_id}"},
                )
                source_moment_ids.update(str(item["moment_id"]) for item in previous_sources)
            inserted = await self.db.insert(
                "memories",
                user.access_token,
                {
                    "user_id": str(user.id),
                    "memory_type": candidate.memory_type,
                    "content": candidate.content,
                    "source_type": "personal",
                    "confidence": candidate.confidence,
                    "status": "disputed" if candidate.operation == "conflict" else "active",
                    "occurred_at": (
                        candidate.occurred_at.isoformat() if candidate.occurred_at else moment["created_at"]
                    ),
                    "valid_from": moment["created_at"],
                    "supersedes_memory_id": related_id if candidate.operation == "update" else None,
                    "embedding": vector,
                    "embedding_model": self.ai.settings.embedding_model,
                    "embedding_dimension": len(vector),
                },
            )
            memory = inserted[0]
            await self.db.insert(
                "memory_sources",
                user.access_token,
                [
                    {
                        "memory_id": memory["id"],
                        "moment_id": source_moment_id,
                        "user_id": str(user.id),
                    }
                    for source_moment_id in sorted(source_moment_ids)
                ],
            )
            if candidate.operation == "update" and related_id:
                await self.db.update(
                    "memories",
                    user.access_token,
                    {"status": "superseded", "valid_to": datetime.now().astimezone().isoformat()},
                    params={"id": f"eq.{related_id}", "user_id": f"eq.{user.id}"},
                )
            written.append(memory)

        if written:
            await self.db.update(
                "insights",
                user.access_token,
                {"status": "stale"},
                params={"user_id": f"eq.{user.id}", "status": "eq.active"},
            )
        await telemetry.ai_run(
            task_type="memory_curator",
            agent_name="memory_curator",
            prompt_version=MEMORY_CURATOR_VERSION,
            metadata=metadata,
            evidence_count=1,
            trace_id=trace_id,
            details={"memory_candidates": len(output.memories), "memories_written": len(written)},
        )
        return written
