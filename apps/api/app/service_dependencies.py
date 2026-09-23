from typing import Annotated

from fastapi import Depends

from app.clients.ai import BailianClient
from app.clients.supabase import SupabaseClient
from app.config import Settings, get_settings
from app.dependencies import get_db
from app.services.companion import CompanionService
from app.services.insights import InsightEngine
from app.services.memory import MemoryCurator
from app.services.moment_index import MomentIndexer
from app.services.retrieval import PersonalMemoryRetriever


def get_ai(settings: Annotated[Settings, Depends(get_settings)]) -> BailianClient:
    return BailianClient(settings)


def get_retriever(
    db: Annotated[SupabaseClient, Depends(get_db)],
    ai: Annotated[BailianClient, Depends(get_ai)],
) -> PersonalMemoryRetriever:
    return PersonalMemoryRetriever(db, ai)


def get_curator(
    db: Annotated[SupabaseClient, Depends(get_db)],
    ai: Annotated[BailianClient, Depends(get_ai)],
) -> MemoryCurator:
    return MemoryCurator(db, ai)


def get_indexer(
    db: Annotated[SupabaseClient, Depends(get_db)],
    ai: Annotated[BailianClient, Depends(get_ai)],
) -> MomentIndexer:
    return MomentIndexer(db, ai)


def get_companion(
    db: Annotated[SupabaseClient, Depends(get_db)],
    ai: Annotated[BailianClient, Depends(get_ai)],
    retriever: Annotated[PersonalMemoryRetriever, Depends(get_retriever)],
) -> CompanionService:
    return CompanionService(db, ai, retriever)


def get_insight_engine(
    db: Annotated[SupabaseClient, Depends(get_db)],
    ai: Annotated[BailianClient, Depends(get_ai)],
    retriever: Annotated[PersonalMemoryRetriever, Depends(get_retriever)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> InsightEngine:
    return InsightEngine(db, ai, retriever, settings)


AiClient = Annotated[BailianClient, Depends(get_ai)]
Curator = Annotated[MemoryCurator, Depends(get_curator)]
Indexer = Annotated[MomentIndexer, Depends(get_indexer)]
Companion = Annotated[CompanionService, Depends(get_companion)]
Insights = Annotated[InsightEngine, Depends(get_insight_engine)]
