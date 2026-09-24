import asyncio
import hashlib
import json
import re
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.clients.ai import BailianClient
from app.clients.supabase import SupabaseClient
from app.config import Settings
from app.domain import (
    AgentRoute,
    InsightClaim,
    RetrievedEvidence,
    SpecialistOutput,
    SynthesisOutput,
    UserContext,
    VerifiedClaim,
    VerifierOutput,
)
from app.prompts import (
    ORCHESTRATOR_SYSTEM,
    ORCHESTRATOR_VERSION,
    PATTERN_SYSTEM,
    PATTERN_VERSION,
    SYNTHESIS_AUDIT_SYSTEM,
    SYNTHESIS_AUDIT_VERSION,
    SYNTHESIZER_SYSTEM,
    SYNTHESIZER_VERSION,
    TEMPORAL_SYSTEM,
    TEMPORAL_VERSION,
    VERIFIER_SYSTEM,
    VERIFIER_VERSION,
)
from app.services.retrieval import PersonalMemoryRetriever
from app.services.telemetry import Telemetry


class InsufficientEvidenceError(RuntimeError):
    pass


class SynthesisAuditCheck(BaseModel):
    segment_id: int = Field(ge=0)
    supported: bool
    source_moment_ids: list[UUID] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=500)


class SynthesisAuditOutput(BaseModel):
    supported: bool
    unsupported_phrases: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=1, max_length=1_000)
    checks: list[SynthesisAuditCheck] = Field(default_factory=list, max_length=30)


AGENT_VERSION = "insight-team-v1.3"


class InsightEngine:
    def __init__(
        self,
        db: SupabaseClient,
        ai: BailianClient,
        retriever: PersonalMemoryRetriever,
        settings: Settings,
    ):
        self.db = db
        self.ai = ai
        self.retriever = retriever
        self.settings = settings

    async def answer(self, user: UserContext, question: str) -> dict:
        trace_id = str(uuid4())
        route, route_meta = await self.ai.structured_chat(
            system=ORCHESTRATOR_SYSTEM,
            user=json.dumps(
                {
                    "question": question,
                    "output_schema": {
                        "route_type": "simple|complex",
                        "specialists": ["temporal|pattern"],
                        "insight_type": (
                            "temporal_change|repeated_pattern|idea_return|unresolved_loop|cross_record_relation|fact"
                        ),
                        "retrieval_query": "string",
                        "reasoning": "string",
                    },
                },
                ensure_ascii=False,
            ),
            schema=AgentRoute,
            temperature=0,
        )
        await self._trace(
            user,
            "orchestrator",
            ORCHESTRATOR_VERSION,
            route_meta,
            trace_id=trace_id,
            details={"route_type": route.route_type, "specialist_count": len(route.specialists)},
        )
        evidence = await self.retriever.search(
            user,
            route.retrieval_query,
            limit=18,
            temporal_coverage=route.route_type == "complex" or "temporal" in route.specialists,
            trace_id=trace_id,
        )
        return await self._analyze_and_store(
            user=user,
            question=question,
            route=route,
            evidence=evidence,
            trigger_type="user_query",
            trace_id=trace_id,
        )

    async def generate_weekly(
        self,
        user: UserContext,
        evidence: list[RetrievedEvidence],
        *,
        week_start: str,
        evidence_version: str,
    ) -> dict:
        cache_key = f"weekly:{week_start}:{evidence_version}:{AGENT_VERSION}"
        cached = await self.db.select(
            "insights",
            user.access_token,
            params={
                "select": "*,insight_evidence(moment_id,memory_id,stance)",
                "user_id": f"eq.{user.id}",
                "cache_key": f"eq.{cache_key}",
                "status": "eq.active",
                "limit": "1",
            },
        )
        if cached:
            return cached[0]
        route = AgentRoute(
            route_type="complex",
            specialists=["temporal", "pattern"],
            insight_type="temporal_change",
            retrieval_query="weekly-evidence",
            reasoning="每周一次：只寻找有原始记录支持的变化或重复。",
        )
        return await self._analyze_and_store(
            user=user,
            question="本周的记录与过去相比，有没有值得回看的变化、重复或尚未解决的问题？证据不足时不生成结论。",
            route=route,
            evidence=evidence,
            trigger_type="automatic",
            cache_key=cache_key,
            evidence_version=evidence_version,
            trace_id=str(uuid4()),
        )

    async def maybe_generate_automatic(self, user: UserContext) -> dict | None:
        memories = await self.db.select(
            "memories",
            user.access_token,
            params={
                "select": "id,content,memory_type,confidence,occurred_at,updated_at",
                "status": "eq.active",
                "source_type": "eq.personal",
                "order": "occurred_at.desc.nullslast,created_at.desc",
                "limit": "40",
            },
        )
        if not memories:
            return None
        evidence = await self._evidence_from_memories(user, memories)
        if len({item.moment_id for item in evidence}) < self.settings.auto_insight_min_memories:
            return None
        evidence_version = self._evidence_version(memories)
        cache_key = f"auto:{evidence_version}:{AGENT_VERSION}"
        cached = await self.db.select(
            "insights",
            user.access_token,
            params={
                "select": "*,insight_evidence(moment_id,memory_id,stance)",
                "cache_key": f"eq.{cache_key}",
                "status": "eq.active",
                "limit": "1",
            },
        )
        if cached:
            return cached[0]

        route = AgentRoute(
            route_type="complex",
            specialists=["temporal", "pattern"],
            insight_type="repeated_pattern",
            retrieval_query="automatic-candidate",
            reasoning="规则触发：有效长期记忆与独立时刻达到阈值。",
        )
        trace_id = str(uuid4())
        try:
            return await self._analyze_and_store(
                user=user,
                question="从现有记录中找出最值得用户回看的变化、重复、回流、未闭环或跨记录联系。",
                route=route,
                evidence=evidence,
                trigger_type="automatic",
                cache_key=cache_key,
                evidence_version=evidence_version,
                trace_id=trace_id,
            )
        except InsufficientEvidenceError:
            return None

    async def _analyze_and_store(
        self,
        *,
        user: UserContext,
        question: str,
        route: AgentRoute,
        evidence: list[RetrievedEvidence],
        trigger_type: Literal["automatic", "user_query"],
        cache_key: str | None = None,
        evidence_version: str | None = None,
        trace_id: str,
    ) -> dict:
        if not evidence:
            raise InsufficientEvidenceError("现有记录不足以支持回答")
        evidence_payload = self._compact_evidence(evidence)

        if route.route_type == "simple":
            claims = await self._simple_reader(user, question, evidence_payload, trace_id)
        else:
            selected = route.specialists or ["temporal", "pattern"]
            tasks = [
                self._specialist(user, specialist, question, evidence_payload, trace_id)
                for specialist in selected
            ]
            outputs = await asyncio.gather(*tasks)
            claims = [claim for output in outputs for claim in output.claims]

        if not claims:
            raise InsufficientEvidenceError("没有形成可验证的候选观察")
        verified = await self._verify(user, question, evidence_payload, claims, trace_id)
        allowed_ids = {str(item["moment_id"]) for item in evidence_payload}
        accepted: list[VerifiedClaim] = []
        for claim in verified.claims:
            source_ids = {str(item) for item in claim.source_moment_ids}
            counter_ids = {str(item) for item in claim.counter_evidence_moment_ids}
            if not source_ids or not source_ids.issubset(allowed_ids):
                continue
            if not counter_ids.issubset(allowed_ids):
                continue
            status = claim.verification_status
            if claim.claim_type != "fact" and len(source_ids) < 2:
                status = "WEAK" if status == "PASS" else status
            if status != "REJECT":
                accepted.append(claim.model_copy(update={"verification_status": status}))
        if not accepted:
            raise InsufficientEvidenceError("候选结论未通过证据验证")

        all_statuses = {claim.verification_status for claim in accepted}
        final_status = "WEAK" if "WEAK" in all_statuses else "PASS"
        synthesis, synth_meta = await self.ai.structured_chat(
            system=SYNTHESIZER_SYSTEM,
            user=json.dumps(
                {
                    "question": question,
                    "evidence": evidence_payload,
                    "verified_claims": [item.model_dump(mode="json") for item in accepted],
                    "output_schema": {
                        "title": "string",
                        "body": "string",
                        "limitation": "string or null",
                    },
                },
                ensure_ascii=False,
            ),
            schema=SynthesisOutput,
            temperature=0.2,
            **self._quality_reasoning_options(),
        )
        await self._trace(
            user,
            "insight_synthesizer",
            SYNTHESIZER_VERSION,
            synth_meta,
            evidence_count=len(allowed_ids),
            trace_id=trace_id,
            details={"accepted_claims": len(accepted), "verification_status": final_status, "attempt": 1},
        )
        audit = await self._audit_synthesis(
            user, question, evidence_payload, accepted, synthesis, trace_id, attempt=1
        )
        if not audit.supported:
            synthesis, repair_meta = await self.ai.structured_chat(
                system=SYNTHESIZER_SYSTEM,
                user=json.dumps(
                    {
                        "question": question,
                        "evidence": evidence_payload,
                        "verified_claims": [item.model_dump(mode="json") for item in accepted],
                        "previous_synthesis": synthesis.model_dump(mode="json"),
                        "audit_feedback": {
                            "unsupported_phrases": audit.unsupported_phrases,
                            "reason": audit.reason,
                        },
                        "instruction": "只修正无据措辞，不添加新事实；保留有价值且有证据的观察。",
                        "output_schema": {
                            "title": "string",
                            "body": "string",
                            "limitation": "string or null",
                        },
                    },
                    ensure_ascii=False,
                ),
                schema=SynthesisOutput,
                temperature=0.1,
                **self._quality_reasoning_options(),
            )
            await self._trace(
                user,
                "insight_synthesizer",
                SYNTHESIZER_VERSION,
                repair_meta,
                evidence_count=len(allowed_ids),
                trace_id=trace_id,
                details={"accepted_claims": len(accepted), "verification_status": final_status, "attempt": 2},
            )
            audit = await self._audit_synthesis(
                user, question, evidence_payload, accepted, synthesis, trace_id, attempt=2
            )
            if not audit.supported:
                raise InsufficientEvidenceError("洞察成稿未通过原始记录核验")

        starts = [claim.time_start for claim in accepted if claim.time_start]
        ends = [claim.time_end for claim in accepted if claim.time_end]
        evidence_version = evidence_version or self._evidence_version_from_results(evidence)
        insight = (
            await self.db.insert(
                "insights",
                user.access_token,
                {
                    "user_id": str(user.id),
                    "insight_type": route.insight_type,
                    "trigger_type": trigger_type,
                    "query": question if trigger_type == "user_query" else None,
                    "title": synthesis.title,
                    "body": synthesis.body,
                    "limitation": synthesis.limitation,
                    "verification_status": final_status,
                    "time_start": min(starts).isoformat() if starts else None,
                    "time_end": max(ends).isoformat() if ends else None,
                    "cache_key": cache_key,
                    "evidence_version": evidence_version,
                    "agent_version": AGENT_VERSION,
                    "status": "active",
                },
            )
        )[0]

        memory_by_moment: dict[str, str] = {}
        for item in evidence:
            if item.memory_id:
                memory_by_moment.setdefault(str(item.moment_id), str(item.memory_id))
        evidence_rows = []
        for claim in accepted:
            for moment_id in claim.source_moment_ids:
                evidence_rows.append(
                    {
                        "insight_id": insight["id"],
                        "moment_id": str(moment_id),
                        "memory_id": memory_by_moment.get(str(moment_id)),
                        "user_id": str(user.id),
                        "stance": "support",
                    }
                )
            for moment_id in claim.counter_evidence_moment_ids:
                evidence_rows.append(
                    {
                        "insight_id": insight["id"],
                        "moment_id": str(moment_id),
                        "memory_id": memory_by_moment.get(str(moment_id)),
                        "user_id": str(user.id),
                        "stance": "counter",
                    }
                )
        deduped = {(row["moment_id"], row["stance"]): row for row in evidence_rows}
        if deduped:
            await self.db.insert("insight_evidence", user.access_token, list(deduped.values()))
        insight["evidence"] = list(deduped.values())
        return insight

    async def _simple_reader(
        self, user: UserContext, question: str, evidence_payload: list[dict], trace_id: str
    ) -> list[InsightClaim]:
        system = (
            "你是证据阅读器。只回答用户问题直接涉及的历史事实；不得添加证据中没有的用户信息。"
            "每个 claim 必须引用 source_moment_ids。证据不足时 claims 为空。只输出 JSON。"
        )
        output, metadata = await self.ai.structured_chat(
            system=system,
            user=json.dumps(
                {
                    "question": question,
                    "evidence": evidence_payload,
                    "output_schema": {
                        "claims": [
                            {
                                "claim": "string",
                                "claim_type": "fact|observation|inference",
                                "source_moment_ids": ["UUID"],
                                "counter_evidence_moment_ids": ["UUID"],
                                "time_start": "ISO datetime or null",
                                "time_end": "ISO datetime or null",
                                "confidence": "0..1",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
            schema=SpecialistOutput,
            temperature=0,
            **self._quality_reasoning_options(),
        )
        await self._trace(
            user,
            "evidence_reader",
            "evidence-reader-v1",
            metadata,
            trace_id=trace_id,
        )
        return output.claims

    async def _specialist(
        self,
        user: UserContext,
        specialist: Literal["temporal", "pattern"],
        question: str,
        evidence_payload: list[dict],
        trace_id: str,
    ) -> SpecialistOutput:
        is_temporal = specialist == "temporal"
        system = TEMPORAL_SYSTEM if is_temporal else PATTERN_SYSTEM
        version = TEMPORAL_VERSION if is_temporal else PATTERN_VERSION
        output, metadata = await self.ai.structured_chat(
            system=system,
            user=json.dumps(
                {
                    "question": question,
                    "evidence": evidence_payload,
                    "output_schema": {
                        "claims": [
                            {
                                "claim": "string",
                                "claim_type": "fact|observation|inference",
                                "source_moment_ids": ["UUID"],
                                "counter_evidence_moment_ids": ["UUID"],
                                "time_start": "ISO datetime or null",
                                "time_end": "ISO datetime or null",
                                "confidence": "0..1",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
            schema=SpecialistOutput,
            temperature=0,
        )
        await self._trace(
            user,
            f"{specialist}_agent",
            version,
            metadata,
            trace_id=trace_id,
        )
        return output

    async def _verify(
        self,
        user: UserContext,
        question: str,
        evidence_payload: list[dict],
        claims: list[InsightClaim],
        trace_id: str,
    ) -> VerifierOutput:
        output, metadata = await self.ai.structured_chat(
            system=VERIFIER_SYSTEM,
            user=json.dumps(
                {
                    "question": question,
                    "evidence": evidence_payload,
                    "candidate_claims": [item.model_dump(mode="json") for item in claims],
                    "output_schema": {
                        "claims": [
                            {
                                "claim": "string",
                                "claim_type": "fact|observation|inference",
                                "source_moment_ids": ["UUID"],
                                "counter_evidence_moment_ids": ["UUID"],
                                "time_start": "ISO datetime or null",
                                "time_end": "ISO datetime or null",
                                "confidence": "0..1",
                                "verification_status": "PASS|WEAK|REJECT",
                                "verifier_note": "string",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
            schema=VerifierOutput,
            temperature=0,
            **self._quality_reasoning_options(),
        )
        await self._trace(
            user,
            "evidence_verifier",
            VERIFIER_VERSION,
            metadata,
            evidence_count=len(evidence_payload),
            trace_id=trace_id,
            details={
                "pass_count": sum(item.verification_status == "PASS" for item in output.claims),
                "weak_count": sum(item.verification_status == "WEAK" for item in output.claims),
                "reject_count": sum(item.verification_status == "REJECT" for item in output.claims),
            },
        )
        return output

    async def _audit_synthesis(
        self,
        user: UserContext,
        question: str,
        evidence_payload: list[dict],
        accepted: list[VerifiedClaim],
        synthesis: SynthesisOutput,
        trace_id: str,
        *,
        attempt: int,
    ) -> SynthesisAuditOutput:
        segments = [{"segment_id": 0, "text": synthesis.title}]
        for field in (synthesis.body, synthesis.limitation or ""):
            for part in re.split(r"[。！？；\n]+", field):
                if part.strip():
                    segments.append({"segment_id": len(segments), "text": part.strip()})
        output, metadata = await self.ai.structured_chat(
            system=SYNTHESIS_AUDIT_SYSTEM,
            user=json.dumps(
                {
                    "question": question,
                    "evidence": evidence_payload,
                    "verified_claims": [item.model_dump(mode="json") for item in accepted],
                    "final_synthesis": synthesis.model_dump(mode="json"),
                    "segments_to_check": segments,
                    "output_schema": {
                        "supported": "boolean",
                        "unsupported_phrases": ["string"],
                        "reason": "string",
                        "checks": [
                            {
                                "segment_id": "integer",
                                "supported": "boolean",
                                "source_moment_ids": ["UUID"],
                                "reason": "string",
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            ),
            schema=SynthesisAuditOutput,
            temperature=0,
            **self._quality_reasoning_options(),
        )
        expected_ids = {item["segment_id"] for item in segments}
        checks = {item.segment_id: item for item in output.checks}
        allowed_ids = {item["moment_id"] for item in evidence_payload}
        complete = len(output.checks) == len(segments) and set(checks) == expected_ids
        failed_segments = [
            item["text"]
            for item in segments
            if item["segment_id"] not in checks
            or not checks[item["segment_id"]].supported
            or not checks[item["segment_id"]].source_moment_ids
            or not {
                str(source_id) for source_id in checks[item["segment_id"]].source_moment_ids
            }.issubset(allowed_ids)
        ]
        supported = output.supported and complete and not failed_segments
        if not supported:
            output = output.model_copy(
                update={
                    "supported": False,
                    "unsupported_phrases": list(
                        dict.fromkeys(output.unsupported_phrases + failed_segments)
                    )[:12],
                    "reason": output.reason if complete else f"审计未覆盖全部 {len(segments)} 个片段；{output.reason}",
                }
            )
        await self._trace(
            user,
            "insight_final_evidence_audit",
            SYNTHESIS_AUDIT_VERSION,
            metadata,
            evidence_count=len(evidence_payload),
            trace_id=trace_id,
            details={
                "supported": output.supported,
                "unsupported_count": len(output.unsupported_phrases),
                "attempt": attempt,
            },
        )
        return output

    def _quality_reasoning_options(self) -> dict:
        return {
            "enable_thinking": self.settings.use_quality_insight_reasoning,
            "max_tokens": 6000,
            "timeout_seconds": (
                self.settings.insight_ai_timeout_seconds
                if self.settings.use_quality_insight_reasoning
                else self.settings.ai_timeout_seconds
            ),
        }

    async def _evidence_from_memories(self, user: UserContext, memories: list[dict]) -> list[RetrievedEvidence]:
        memory_ids = [str(item["id"]) for item in memories]
        sources = await self.db.select(
            "memory_sources",
            user.access_token,
            params={
                "select": "memory_id,moment_id,user_id",
                "memory_id": f"in.({','.join(memory_ids)})",
                "user_id": f"eq.{user.id}",
            },
        )
        moment_ids = sorted({str(item["moment_id"]) for item in sources})
        moments = (
            await self.db.select(
                "moments",
                user.access_token,
                params={
                    "select": "id,user_id,content,created_at,memory_enabled",
                    "id": f"in.({','.join(moment_ids)})" if moment_ids else "in.()",
                    "user_id": f"eq.{user.id}",
                    "memory_enabled": "eq.true",
                },
            )
            if moment_ids
            else []
        )
        moment_map = {str(item["id"]): item for item in moments}
        memory_map = {str(item["id"]): item for item in memories}
        evidence: list[RetrievedEvidence] = []
        for source in sources:
            memory = memory_map.get(str(source["memory_id"]))
            moment = moment_map.get(str(source["moment_id"]))
            if not memory or not moment or str(moment["user_id"]) != str(user.id):
                continue
            evidence.append(
                RetrievedEvidence(
                    memory_id=memory["id"],
                    memory_content=memory["content"],
                    memory_type=memory["memory_type"],
                    confidence=memory["confidence"],
                    moment_id=moment["id"],
                    moment_content=moment["content"],
                    occurred_at=moment["created_at"],
                    score=1,
                )
            )
        return evidence

    @staticmethod
    def _compact_evidence(evidence: list[RetrievedEvidence]) -> list[dict]:
        deduped: dict[str, dict] = {}
        for item in evidence:
            key = str(item.moment_id)
            row = deduped.setdefault(
                key,
                {
                    "moment_id": key,
                    "moment_content": item.moment_content,
                    "occurred_at": item.occurred_at.isoformat(),
                    "memories": [],
                },
            )
            row["memories"].append(
                {
                    "memory_id": str(item.memory_id) if item.memory_id else None,
                    "content": item.memory_content,
                    "type": item.memory_type,
                    "confidence": item.confidence,
                }
            )
        return sorted(deduped.values(), key=lambda item: item["occurred_at"])

    @staticmethod
    def _evidence_version(memories: list[dict]) -> str:
        value = "|".join(sorted(f"{item['id']}:{item.get('updated_at', '')}" for item in memories))
        return hashlib.sha256(value.encode()).hexdigest()[:20]

    @staticmethod
    def _evidence_version_from_results(evidence: list[RetrievedEvidence]) -> str:
        value = "|".join(sorted(f"{item.memory_id}:{item.moment_id}" for item in evidence))
        return hashlib.sha256(value.encode()).hexdigest()[:20]

    async def _trace(
        self,
        user: UserContext,
        agent_name: str,
        prompt_version: str,
        metadata: dict,
        *,
        evidence_count: int = 0,
        trace_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        await Telemetry(self.db, user.access_token, user.id).ai_run(
            task_type="insight",
            agent_name=agent_name,
            prompt_version=prompt_version,
            metadata=metadata,
            evidence_count=evidence_count,
            trace_id=trace_id,
            details=details,
        )
