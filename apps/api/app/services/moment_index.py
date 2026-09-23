import asyncio
from uuid import UUID, uuid4

from app.clients.ai import BailianClient
from app.clients.supabase import SupabaseClient
from app.config import Settings
from app.domain import UserContext
from app.services.evidence_scope import eligible_moment_ids
from app.services.telemetry import Telemetry

CHUNK_SIZE = 2400
CHUNK_OVERLAP = 120
INDEX_VERSION = "raw-moment-index-v1"


def split_moment(text: str) -> list[str]:
    """Index long colloquial entries in overlapping pieces without changing the source."""
    if len(text) <= CHUNK_SIZE:
        return [text]
    step = CHUNK_SIZE - CHUNK_OVERLAP
    return [text[start : start + CHUNK_SIZE] for start in range(0, len(text), step) if text[start:]]


class MomentIndexer:
    def __init__(self, db: SupabaseClient, ai: BailianClient):
        self.db = db
        self.ai = ai

    async def index_moment(self, user: UserContext, moment_id: UUID) -> int:
        rows = await self.db.select(
            "moments",
            user.access_token,
            params={
                "select": "id,user_id,content,memory_enabled",
                "id": f"eq.{moment_id}",
                "user_id": f"eq.{user.id}",
                "limit": "1",
            },
        )
        if not rows:
            raise LookupError("Moment 不存在")
        moment = rows[0]
        if not moment["memory_enabled"]:
            return 0
        if str(moment_id) not in await eligible_moment_ids(self.db, user, {str(moment_id)}):
            return 0
        chunks = split_moment(moment["content"])
        existing = await self.db.select(
            "moment_index_chunks",
            user.access_token,
            params={
                "select": "chunk_index,content,embedding_model",
                "moment_id": f"eq.{moment_id}",
                "user_id": f"eq.{user.id}",
            },
        )
        if len(existing) == len(chunks) and all(
            any(
                row["chunk_index"] == index
                and row["content"] == chunk
                and row["embedding_model"] == self.ai.settings.embedding_model
                for row in existing
            )
            for index, chunk in enumerate(chunks)
        ):
            return len(chunks)

        trace_id = str(uuid4())
        semaphore = asyncio.Semaphore(3)

        async def embed(chunk: str) -> tuple[list[float], dict]:
            async with semaphore:
                return await self.ai.embedding(chunk)

        embedded = await asyncio.gather(*(embed(chunk) for chunk in chunks))
        await self.db.upsert(
            "moment_index_chunks",
            user.access_token,
            [
                {
                    "moment_id": str(moment_id),
                    "user_id": str(user.id),
                    "chunk_index": index,
                    "content": chunk,
                    "embedding": vector,
                    "embedding_model": self.ai.settings.embedding_model,
                    "embedding_dimension": len(vector),
                }
                for index, (chunk, (vector, _)) in enumerate(zip(chunks, embedded, strict=True))
            ],
            on_conflict="moment_id,chunk_index",
        )
        if len(existing) > len(chunks):
            await self.db.delete(
                "moment_index_chunks",
                user.access_token,
                params={
                    "moment_id": f"eq.{moment_id}",
                    "user_id": f"eq.{user.id}",
                    "chunk_index": f"gte.{len(chunks)}",
                },
            )
        telemetry = Telemetry(self.db, user.access_token, user.id)
        await asyncio.gather(
            *(
                telemetry.ai_run(
                    task_type="embedding",
                    agent_name="moment_indexer",
                    prompt_version=INDEX_VERSION,
                    metadata=metadata,
                    evidence_count=1,
                    trace_id=trace_id,
                    details={"chunk_index": index, "embedding_dimension": len(vector)},
                )
                for index, (vector, metadata) in enumerate(embedded)
            )
        )
        return len(chunks)


async def run_moment_index_job(user: UserContext, moment_id: UUID, settings: Settings) -> None:
    """Index with resources independent of the request dependency lifecycle."""
    db = SupabaseClient(settings)
    try:
        await MomentIndexer(db, BailianClient(settings)).index_moment(user, moment_id)
    finally:
        await db.aclose()
