from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

CaptureMode = Literal["capture", "chat"]
MemoryStatus = Literal["active", "superseded", "disputed", "deleted"]
VerificationStatus = Literal["PASS", "WEAK", "REJECT"]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserContext(ApiModel):
    id: UUID
    email: str | None = None
    access_token: str


class MomentCreate(ApiModel):
    content: str = Field(min_length=1, max_length=20_000)
    mode: CaptureMode = "capture"
    input_type: Literal["text"] = "text"
    memory_enabled: bool = True


class Moment(ApiModel):
    id: UUID
    user_id: UUID
    content: str
    mode: CaptureMode
    input_type: str
    memory_enabled: bool
    created_at: datetime
    thread_id: UUID | None = None


class ThreadMessageCreate(ApiModel):
    content: str = Field(min_length=1, max_length=20_000)
    thread_id: UUID | None = None
    source_moment_id: UUID | None = None
    input_type: Literal["text"] = "text"


class ThreadMessage(ApiModel):
    id: UUID
    thread_id: UUID
    role: Literal["user", "assistant"]
    content: str
    evidence_moment_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime


class ChatResponse(ApiModel):
    thread_id: UUID
    moment_id: UUID
    message: ThreadMessage
    evidence_moment_ids: list[UUID] = Field(default_factory=list)
    used_long_term_memory: bool = False


class MemoryPatch(ApiModel):
    content: str = Field(min_length=1, max_length=4_000)


class MemoryCandidate(ApiModel):
    content: str = Field(default="", max_length=4_000)
    memory_type: Literal["event", "view", "interest", "goal", "decision", "question", "state"]
    occurred_at: datetime | None = None
    confidence: float = Field(ge=0, le=1)
    operation: Literal["create", "update", "conflict", "skip"] = "create"
    related_memory_id: UUID | None = None
    reasoning: str = ""

    @model_validator(mode="after")
    def require_content_for_persisted_memory(self) -> "MemoryCandidate":
        if self.operation != "skip" and not self.content.strip():
            raise ValueError("content is required unless operation is skip")
        return self


class CuratorOutput(ApiModel):
    memories: list[MemoryCandidate] = Field(default_factory=list, max_length=8)


class RetrievedEvidence(ApiModel):
    memory_id: UUID
    memory_content: str
    memory_type: str
    confidence: float
    moment_id: UUID
    moment_content: str
    occurred_at: datetime
    score: float


class InsightQuery(ApiModel):
    question: str = Field(min_length=2, max_length=2_000)


class AgentRoute(ApiModel):
    route_type: Literal["simple", "complex"]
    specialists: list[Literal["temporal", "pattern"]] = Field(default_factory=list)
    insight_type: Literal[
        "temporal_change",
        "repeated_pattern",
        "idea_return",
        "unresolved_loop",
        "cross_record_relation",
        "fact",
    ]
    retrieval_query: str
    reasoning: str = ""


class InsightClaim(ApiModel):
    claim: str = Field(min_length=1, max_length=2_000)
    claim_type: Literal["fact", "observation", "inference"]
    source_moment_ids: list[UUID] = Field(default_factory=list)
    counter_evidence_moment_ids: list[UUID] = Field(default_factory=list)
    time_start: datetime | None = None
    time_end: datetime | None = None
    confidence: float = Field(ge=0, le=1)


class SpecialistOutput(ApiModel):
    claims: list[InsightClaim] = Field(default_factory=list, max_length=8)


class VerifiedClaim(InsightClaim):
    verification_status: VerificationStatus
    verifier_note: str


class VerifierOutput(ApiModel):
    claims: list[VerifiedClaim] = Field(default_factory=list, max_length=8)


class SynthesisOutput(ApiModel):
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=4_000)
    limitation: str | None = Field(default=None, max_length=1_000)


class ProductEvent(ApiModel):
    event_name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    session_id: str | None = Field(default=None, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)
    properties: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
