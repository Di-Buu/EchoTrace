import asyncio
import json
import logging
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.clients.ai import BailianClient
from app.clients.supabase import SupabaseClient, SupabaseError
from app.domain import ChatResponse, ThreadMessageCreate, UserContext
from app.prompts import COMPANION_SYSTEM, COMPANION_VERSION
from app.services.retrieval import PersonalMemoryRetriever
from app.services.telemetry import Telemetry

logger = logging.getLogger("echotrace.chat")

LONG_TERM_CUES = (
    "以前",
    "之前",
    "过去",
    "最近",
    "现在",
    "目前",
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

    async def _turn_rpc(self, user, function, payload):
        try:
            return await self.db.rpc(function, user.access_token, payload)
        except SupabaseError as exc:
            message = str(exc)
            if "IDEMPOTENCY_CONFLICT" in message:
                raise HTTPException(409, "同一个发送标识不能用于不同内容") from exc
            if "CHAT_BUSY" in message:
                raise HTTPException(409, "这条消息仍在处理，请稍候再试") from exc
            if any(code in message for code in ("CHAT_LEASE_LOST", "EVIDENCE_CHANGED")):
                raise HTTPException(409, "记录或对话已变化，请刷新后重新发送") from exc
            if "SOURCE_UNAVAILABLE" in message:
                raise HTTPException(404, "原记录或对话已被修改或删除，请重新打开") from exc
            raise

    async def reply(self, user: UserContext, payload: ThreadMessageCreate, key: str | None = None) -> ChatResponse:
        key = key or str(uuid4())
        if not 1 <= len(key) <= 128:
            raise HTTPException(422, "发送标识长度应为 1–128")
        prepared = (await self._turn_rpc(user, "chat_prepare", {
            "p_key": key, "p_payload": payload.model_dump(mode="json"),
        }))[0]
        if "cached" in prepared:
            return ChatResponse.model_validate(prepared["cached"])
        try:
            return await self._reply_prepared(user, payload, key, prepared)
        except BaseException:
            try:
                await asyncio.shield(self._turn_rpc(user, "chat_control", {
                    "p_key": key, "p_lease": prepared["lease"], "p_action": "fail", "p_data": {},
                }))
            except Exception:
                logger.warning("chat_release_failed; lease will expire")
            raise

    async def _reply_prepared(self, user, payload, key, prepared) -> ChatResponse:
        trace_id = str(uuid4())
        thread_id = UUID(prepared["thread_id"])
        moment_id = UUID(prepared["moment_id"])
        source_moment_id = moment_id
        reuse_source = prepared.get("new_thread", False)
        telemetry = Telemetry(self.db, user.access_token, user.id)
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
        retrieved = (
            await self.retriever.search(user, payload.content, limit=7, trace_id=trace_id)
            if needs_long_term
            else []
        )
        # The current turn is saved before retrieval, but it is not past evidence.
        evidence = [item for item in retrieved if item.moment_id != moment_id][:6]
        evidence_ids = list(dict.fromkeys(item.moment_id for item in evidence))
        snapshots = (
            await self.db.rpc("evidence_snapshot", user.access_token, {"p_ids": [str(x) for x in evidence_ids]})
            if evidence_ids else []
        )
        if len(snapshots) != len(evidence_ids):
            raise HTTPException(409, "历史记录已变化，请稍后重试")
        versions = {str(row["id"]): row["revision"] for row in snapshots}
        context = {
            "thread_summary": summary,
            "personal_evidence": [item.model_dump(mode="json") for item in evidence],
            "grounding_rule": "关于用户过去的事实只能来自 personal_evidence；为空时不得声称记得过去。",
        }
        messages = [{"role": "user", "content": f"可用上下文：{json.dumps(context, ensure_ascii=False)}"}]
        messages.extend({"role": item["role"], "content": item["content"]} for item in history)
        text, metadata = await self.ai.chat(system=COMPANION_SYSTEM, messages=messages)
        result = (await self._turn_rpc(user, "chat_control", {
            "p_key": key, "p_lease": prepared["lease"], "p_action": "finish",
            "p_data": {"content": text, "evidence_ids": [str(x) for x in evidence_ids], "versions": versions},
        }))[0]
        try:
            await self._maybe_update_summary(user, thread_id, trace_id)
        except Exception:
            logger.warning("chat_summary_failed; reply already committed")
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
        return ChatResponse.model_validate(result)

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
