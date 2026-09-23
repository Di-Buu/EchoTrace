import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4

from app.clients.ai import BailianClient
from app.clients.supabase import SupabaseClient, SupabaseError
from app.config import Settings
from app.domain import RetrievedEvidence, UserContext, WeeklyDigestOutput
from app.prompts import WEEKLY_DIGEST_SYSTEM, WEEKLY_DIGEST_VERSION
from app.services.evidence_scope import eligible_moment_ids
from app.services.insights import InsightEngine, InsufficientEvidenceError
from app.services.moment_index import split_moment
from app.services.retrieval import PersonalMemoryRetriever
from app.services.telemetry import Telemetry

REPORT_ZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_BACKLOG_WEEKS = 8
DIGEST_BATCH_CHARACTERS = 14_000


def week_start_for(value: date) -> date:
    return value - timedelta(days=value.weekday())


def last_closed_week(now: datetime | None = None) -> date:
    local = (now or datetime.now(UTC)).astimezone(REPORT_ZONE)
    return week_start_for(local.date()) - timedelta(days=7)


def week_timestamp(value: date) -> str:
    return datetime.combine(value, time.min, tzinfo=REPORT_ZONE).astimezone(UTC).isoformat()


class WeeklyReportService:
    def __init__(
        self,
        db: SupabaseClient,
        ai: BailianClient,
        retriever: PersonalMemoryRetriever,
        engine: InsightEngine,
        settings: Settings,
    ):
        self.db = db
        self.ai = ai
        self.retriever = retriever
        self.engine = engine
        self.settings = settings

    async def ensure_due(self, user: UserContext) -> tuple[list[dict], list[str]]:
        first = await self.db.select(
            "moments",
            user.access_token,
            params={
                "select": "created_at",
                "user_id": f"eq.{user.id}",
                "order": "created_at.asc",
                "limit": "1",
            },
        )
        if not first:
            return [], []
        earliest = datetime.fromisoformat(first[0]["created_at"].replace("Z", "+00:00"))
        start = week_start_for(earliest.astimezone(REPORT_ZONE).date())
        target = last_closed_week()
        if start > target:
            return [], []
        start = max(start, target - timedelta(weeks=MAX_BACKLOG_WEEKS - 1))
        existing = await self.db.select(
            "weekly_reports",
            user.access_token,
            params={"select": "id,week_start,status,updated_at", "user_id": f"eq.{user.id}", "limit": "100"},
        )
        by_week = {row["week_start"]: row for row in existing}
        reports: list[dict] = []
        scheduled: list[str] = []
        current = start
        while current <= target:
            key = current.isoformat()
            row = by_week.get(key)
            if row is None:
                try:
                    row = (
                        await self.db.insert(
                            "weekly_reports",
                            user.access_token,
                            {
                                "user_id": str(user.id),
                                "week_start": key,
                                "week_end": (current + timedelta(days=7)).isoformat(),
                                "status": "processing",
                            },
                        )
                    )[0]
                    scheduled.append(str(row["id"]))
                except SupabaseError:
                    found = await self.db.select(
                        "weekly_reports",
                        user.access_token,
                        params={
                            "select": "id,week_start,status,updated_at",
                            "user_id": f"eq.{user.id}",
                            "week_start": f"eq.{key}",
                            "limit": "1",
                        },
                    )
                    if not found:
                        raise
                    row = found[0]
            else:
                updated = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
                stuck = row["status"] == "processing" and datetime.now(UTC) - updated > timedelta(minutes=20)
                if row["status"] in {"failed", "stale"} or stuck:
                    row = (
                        await self.db.update(
                            "weekly_reports",
                            user.access_token,
                            {"status": "processing", "error_code": None},
                            params={"id": f"eq.{row['id']}", "user_id": f"eq.{user.id}"},
                        )
                    )[0]
                    scheduled.append(str(row["id"]))
            reports.append(row)
            current += timedelta(days=7)
        return reports, scheduled

    async def run(self, user: UserContext, report_id: UUID) -> None:
        try:
            await self._process(user, report_id)
        except Exception as exc:
            await self.db.update(
                "weekly_reports",
                user.access_token,
                {"status": "failed", "error_code": type(exc).__name__},
                params={"id": f"eq.{report_id}", "user_id": f"eq.{user.id}"},
            )

    async def _process(self, user: UserContext, report_id: UUID) -> None:
        reports = await self.db.select(
            "weekly_reports",
            user.access_token,
            params={
                "select": "id,week_start,week_end",
                "id": f"eq.{report_id}",
                "user_id": f"eq.{user.id}",
                "status": "eq.processing",
                "limit": "1",
            },
        )
        if not reports:
            return
        report = reports[0]
        moments = await self._week_moments(user, report["week_start"], report["week_end"])
        version = hashlib.sha256(
            "|".join(f"{item['id']}:{item['updated_at']}" for item in moments).encode()
        ).hexdigest()[:20]
        if not moments:
            await self._finish(user, report_id, "no_records", [], version)
            return

        # A single entry is still shown in the week history, but does not need
        # a model-generated summary or a deep multi-agent run.
        single_entry = len(moments) == 1
        cards = (
            [
                {
                    "topic": "本周的一条记录",
                    "summary": moments[0]["content"][:300],
                    "source_moment_ids": [str(moments[0]["id"])],
                    "counter_moment_ids": [],
                    "time_start": moments[0]["created_at"],
                    "time_end": moments[0]["created_at"],
                }
            ]
            if single_entry
            else await self._digest(user, moments)
        )
        await self.db.delete(
            "weekly_summary_cards",
            user.access_token,
            params={"report_id": f"eq.{report_id}", "user_id": f"eq.{user.id}"},
        )
        if cards and not single_entry:
            embeddings = await asyncio.gather(*(self.ai.embedding(card["summary"]) for card in cards))
            await self.db.insert(
                "weekly_summary_cards",
                user.access_token,
                [
                    {
                        "report_id": str(report_id),
                        "user_id": str(user.id),
                        "topic": card["topic"],
                        "summary": card["summary"],
                        "source_moment_ids": card["source_moment_ids"],
                        "time_start": card["time_start"],
                        "time_end": card["time_end"],
                        "embedding": vector,
                        "embedding_model": self.ai.settings.embedding_model,
                        "embedding_dimension": len(vector),
                    }
                    for card, (vector, _) in zip(cards, embeddings, strict=True)
                ],
            )
        total = await self.db.select(
            "moments",
            user.access_token,
            params={
                "select": "id",
                "user_id": f"eq.{user.id}",
                "memory_enabled": "eq.true",
                "limit": str(self.settings.weekly_insight_min_total_moments),
            },
        )
        if (
            len(moments) < self.settings.weekly_insight_min_new_moments
            or len(total) < self.settings.weekly_insight_min_total_moments
        ):
            await self._finish(user, report_id, "insufficient", cards, version)
            return

        query = "本周 " + "、".join(dict.fromkeys(card["topic"] for card in cards))[:180] + " 过去的变化"
        historical = await self.retriever.search(
            user, query, limit=14, temporal_coverage=True, trace_id=str(uuid4())
        )
        current = [
            RetrievedEvidence(
                memory_id=None,
                memory_content=moment["content"][:4000],
                memory_type="moment",
                confidence=1,
                moment_id=moment["id"],
                moment_content=split_moment(moment["content"])[0],
                occurred_at=moment["created_at"],
                score=1,
            )
            for moment in (moments[:4] + moments[-4:])
        ]
        evidence_by_id = {str(item.moment_id): item for item in historical}
        evidence_by_id.update({str(item.moment_id): item for item in current})
        try:
            insight = await self.engine.generate_weekly(
                user,
                list(evidence_by_id.values())[:18],
                week_start=report["week_start"],
                evidence_version=version,
            )
        except InsufficientEvidenceError:
            await self._finish(user, report_id, "insufficient", cards, version)
            return
        await self._finish(user, report_id, "completed", cards, version, insight_id=insight["id"])

    async def _week_moments(self, user: UserContext, start: str, end: str) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        while True:
            batch = await self.db.select(
                "moments",
                user.access_token,
                params={
                    "select": "id,content,created_at,updated_at",
                    "user_id": f"eq.{user.id}",
                    "memory_enabled": "eq.true",
                    "created_at": f"gte.{week_timestamp(date.fromisoformat(start))}",
                    "and": f"(created_at.lt.{week_timestamp(date.fromisoformat(end))})",
                    "order": "created_at.asc",
                    "limit": "100",
                    "offset": str(offset),
                },
            )
            rows.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        permitted = await eligible_moment_ids(self.db, user, {str(row["id"]) for row in rows})
        return [row for row in rows if str(row["id"]) in permitted]

    async def _digest(self, user: UserContext, moments: list[dict]) -> list[dict]:
        by_id = {str(item["id"]): item for item in moments}
        parts = [
            {
                "moment_id": str(moment["id"]),
                "created_at": moment["created_at"],
                "part_index": index,
                "content": part,
            }
            for moment in moments
            for index, part in enumerate(split_moment(moment["content"]))
        ]
        batches: list[list[dict]] = []
        current: list[dict] = []
        size = 0
        for part in parts:
            if current and size + len(part["content"]) > DIGEST_BATCH_CHARACTERS:
                batches.append(current)
                current = []
                size = 0
            current.append(part)
            size += len(part["content"])
        if current:
            batches.append(current)

        cards: list[dict] = []
        covered: set[str] = set()
        telemetry = Telemetry(self.db, user.access_token, user.id)
        for batch in batches:
            output, metadata = await self.ai.structured_chat(
                system=WEEKLY_DIGEST_SYSTEM,
                user=json.dumps(
                    {
                        "moments": batch,
                        "output_schema": {
                            "cards": [
                                {
                                    "topic": "string",
                                    "summary": "string",
                                    "source_moment_ids": ["UUID"],
                                    "counter_moment_ids": ["UUID"],
                                    "time_start": "ISO datetime or null",
                                    "time_end": "ISO datetime or null",
                                }
                            ],
                            "ungrouped_moment_ids": ["UUID"],
                        },
                    },
                    ensure_ascii=False,
                ),
                schema=WeeklyDigestOutput,
                temperature=0,
                max_tokens=2400,
                timeout_seconds=60,
            )
            allowed = {part["moment_id"] for part in batch}
            for card in output.cards:
                source_ids = {str(item) for item in card.source_moment_ids}
                counter_ids = {str(item) for item in card.counter_moment_ids}
                if not source_ids or not (source_ids | counter_ids).issubset(allowed):
                    continue
                ids = sorted(source_ids | counter_ids)
                covered.update(ids)
                timestamps = [
                    datetime.fromisoformat(by_id[item]["created_at"].replace("Z", "+00:00"))
                    for item in ids
                ]
                cards.append(
                    {
                        "topic": card.topic,
                        "summary": card.summary,
                        "source_moment_ids": ids,
                        "counter_moment_ids": sorted(counter_ids),
                        "time_start": min(timestamps).isoformat(),
                        "time_end": max(timestamps).isoformat(),
                    }
                )
            await telemetry.ai_run(
                task_type="weekly_digest",
                agent_name="weekly_evidence_digest",
                prompt_version=WEEKLY_DIGEST_VERSION,
                metadata=metadata,
                evidence_count=len(allowed),
                trace_id=str(uuid4()),
                details={"cards": len(output.cards), "batch_parts": len(batch)},
            )
        for moment_id, moment in by_id.items():
            if moment_id not in covered:
                cards.append(
                    {
                        "topic": "未归类的时刻",
                        "summary": moment["content"][:300],
                        "source_moment_ids": [moment_id],
                        "counter_moment_ids": [],
                        "time_start": moment["created_at"],
                        "time_end": moment["created_at"],
                    }
                )
        return cards

    async def _finish(
        self,
        user: UserContext,
        report_id: UUID,
        status: str,
        cards: list[dict],
        version: str,
        *,
        insight_id: str | None = None,
    ) -> None:
        await self.db.update(
            "weekly_reports",
            user.access_token,
            {
                "status": status,
                "digest": cards,
                "input_version": version,
                "insight_id": insight_id,
                "error_code": None,
            },
            params={"id": f"eq.{report_id}", "user_id": f"eq.{user.id}"},
        )
