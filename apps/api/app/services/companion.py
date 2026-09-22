import json
from uuid import UUID, uuid4

from app.clients.ai import BailianClient
from app.clients.supabase import SupabaseClient
from app.domain import ChatResponse, ThreadMessage, ThreadMessageCreate, UserContext
from app.prompts import COMPANION_SYSTEM, COMPANION_VERSION
from app.services.retrieval import PersonalMemoryRetriever
from app.services.telemetry import Telemetry

LONG_TERM_CUES = (
    "以前",
    "之前",
    "过去",
    "最近",
    "一直",
    "曾经",
    "上次",
    "记得",
    "又",
    "变化",
    "习惯",
)


class CompanionService:
    def __init__(self, db: SupabaseClient, ai: BailianClient, retriever: PersonalMemoryRetriever):
        self.db = db
        self.ai = ai
        self.retriever = retriever

    async def reply(self, user: UserContext, payload: ThreadMessageCreate) -> ChatResponse:
        trace_id = str(uuid4())
        thread_id, source_moment_id, reuse_source = await self._ensure_thread(user, payload)
        telemetry = Telemetry(self.db, user.access_token, user.id)
        if reuse_source:
            moment_id = source_moment_id
        else:
            moment = await self.db.insert(
                "moments",
                user.access_token,
                {
                    "user_id": str(user.id),
                    "content": payload.content,
                    "mode": "chat",
                    "input_type": payload.input_type,
                    "memory_enabled": True,
                    "thread_id": str(thread_id),
                },
            )
            moment_id = UUID(moment[0]["id"])
        await self.db.insert(
            "thread_messages",
            user.access_token,
            {
                "user_id": str(user.id),
                "thread_id": str(thread_id),
                "role": "user",
                "content": payload.content,
                "input_type": payload.input_type,
            },
        )
        history = await self.db.select(
            "thread_messages",
            user.access_token,
            params={
                "select": "role,content,created_at",
                "thread_id": f"eq.{thread_id}",
                "order": "created_at.desc",
                "limit": "14",
            },
        )
        history.reverse()
        turn_index = sum(item["role"] == "user" for item in history)
        await telemetry.product_event(
            event_name="chat_mode_used",
            properties={"thread_id": str(thread_id), "input_type": payload.input_type},
        )
        await telemetry.product_event(
            event_name="thread_message_sent",
            properties={
                "thread_id": str(thread_id),
                "turn_index": turn_index,
                "input_type": payload.input_type,
            },
        )
        if reuse_source:
            await telemetry.product_event(
                event_name="thread_started",
                properties={"thread_id": str(thread_id), "source_moment_id": str(source_moment_id)},
            )
        thread_rows = await self.db.select(
            "threads",
            user.access_token,
            params={"select": "summary", "id": f"eq.{thread_id}", "limit": "1"},
        )
        summary = thread_rows[0].get("summary") if thread_rows else None

        needs_long_term = any(cue in payload.content for cue in LONG_TERM_CUES)
        evidence = (
            await self.retriever.search(user, payload.content, limit=6, trace_id=trace_id)
            if needs_long_term
            else []
        )
        evidence_ids = list(dict.fromkeys(item.moment_id for item in evidence))
        context = {
            "thread_summary": summary,
            "personal_evidence": [item.model_dump(mode="json") for item in evidence],
            "grounding_rule": "关于用户过去的事实只能来自 personal_evidence；为空时不得声称记得过去。",
        }
        messages = [{"role": "user", "content": f"可用上下文：{json.dumps(context, ensure_ascii=False)}"}]
        messages.extend({"role": item["role"], "content": item["content"]} for item in history)
        text, metadata = await self.ai.chat(system=COMPANION_SYSTEM, messages=messages)
        inserted = await self.db.insert(
            "thread_messages",
            user.access_token,
            {
                "user_id": str(user.id),
                "thread_id": str(thread_id),
                "role": "assistant",
                "content": text,
                "input_type": "system",
                "evidence_moment_ids": [str(item) for item in evidence_ids],
            },
        )
        await self._maybe_update_summary(user, thread_id, trace_id)
        await telemetry.ai_run(
            task_type="companion",
            agent_name="companion",
            prompt_version=COMPANION_VERSION,
            metadata=metadata,
            retrieval_candidates=len(evidence),
            evidence_count=len(evidence_ids),
            tool_calls=1 if needs_long_term else 0,
            trace_id=trace_id,
            details={"used_long_term_memory": bool(evidence)},
        )
        if evidence:
            await telemetry.product_event(
                event_name="personal_memory_used",
                properties={"thread_id": str(thread_id), "evidence_count": len(evidence_ids)},
            )
        message = ThreadMessage.model_validate(inserted[0])
        return ChatResponse(
            thread_id=thread_id,
            moment_id=moment_id,
            message=message,
            evidence_moment_ids=evidence_ids,
            used_long_term_memory=bool(evidence),
        )

    async def _ensure_thread(self, user: UserContext, payload: ThreadMessageCreate) -> tuple[UUID, UUID, bool]:
        if payload.thread_id:
            rows = await self.db.select(
                "threads",
                user.access_token,
                params={
                    "select": "id,source_moment_id",
                    "id": f"eq.{payload.thread_id}",
                    "user_id": f"eq.{user.id}",
                    "limit": "1",
                },
            )
            if not rows:
                raise LookupError("Thread 不存在")
            return UUID(rows[0]["id"]), UUID(rows[0]["source_moment_id"]), False

        if payload.source_moment_id:
            source_id = payload.source_moment_id
            source_rows = await self.db.select(
                "moments",
                user.access_token,
                params={
                    "select": "id,content,input_type",
                    "id": f"eq.{source_id}",
                    "user_id": f"eq.{user.id}",
                    "limit": "1",
                },
            )
            if not source_rows:
                raise LookupError("源 Moment 不存在")
            existing_threads = await self.db.select(
                "threads",
                user.access_token,
                params={
                    "select": "id,source_moment_id",
                    "source_moment_id": f"eq.{source_id}",
                    "user_id": f"eq.{user.id}",
                    "limit": "1",
                },
            )
            if existing_threads:
                return UUID(existing_threads[0]["id"]), source_id, False
        else:
            created = await self.db.insert(
                "moments",
                user.access_token,
                {
                    "user_id": str(user.id),
                    "content": payload.content,
                    "mode": "chat",
                    "input_type": payload.input_type,
                    "memory_enabled": True,
                },
            )
            source_rows = created
            source_id = UUID(created[0]["id"])

        thread = await self.db.insert(
            "threads",
            user.access_token,
            {"user_id": str(user.id), "source_moment_id": str(source_id)},
        )
        thread_id = UUID(thread[0]["id"])
        await self.db.update(
            "moments",
            user.access_token,
            {"thread_id": str(thread_id)},
            params={"id": f"eq.{source_id}", "user_id": f"eq.{user.id}"},
        )
        return thread_id, source_id, True

    async def _maybe_update_summary(self, user: UserContext, thread_id: UUID, trace_id: str) -> None:
        messages = await self.db.select(
            "thread_messages",
            user.access_token,
            params={
                "select": "role,content",
                "thread_id": f"eq.{thread_id}",
                "order": "created_at.asc",
                "limit": "100",
            },
        )
        if len(messages) < 8 or len(messages) % 4:
            return
        summary_system = (
            "把当前 Thread 压缩为不超过 220 字的滚动摘要，保留未完成问题、用户明确事实和对话方向。"
            "不要加入推断；AI 的话不能作为用户事实。只输出摘要正文。"
        )
        summary, metadata = await self.ai.chat(
            system=summary_system,
            messages=[{"role": "user", "content": json.dumps(messages, ensure_ascii=False)}],
            temperature=0,
        )
        await self.db.update(
            "threads",
            user.access_token,
            {"summary": summary, "summary_message_count": len(messages)},
            params={"id": f"eq.{thread_id}", "user_id": f"eq.{user.id}"},
        )
        await Telemetry(self.db, user.access_token, user.id).ai_run(
            task_type="thread_summary",
            agent_name="thread_summarizer",
            prompt_version="thread-summary-v1",
            metadata=metadata,
            trace_id=trace_id,
        )
