"""Run with python -m app.worker. No user JWT is stored in jobs."""

import argparse
import asyncio
import logging
import signal
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.clients.ai import BailianClient
from app.clients.supabase import SupabaseClient, SupabaseError
from app.clients.worker_db import LeaseLost, WorkerDatabase
from app.config import get_settings
from app.domain import UserContext
from app.services.agent_runtime import AgentRuntime, Budget, BudgetExceeded
from app.services.hybrid import HybridRetriever
from app.services.insights import InsightEngine, InsufficientEvidenceError
from app.services.moment_index import split_moment
from app.services.weekly import WeeklyReportService

logger = logging.getLogger("echotrace.worker")
HEARTBEAT = Path("/tmp/echotrace-worker.heartbeat") if Path("/tmp").exists() else Path("worker.heartbeat")


async def execute(job, settings):
    db = WorkerDatabase(settings, job)
    user = UserContext(id=UUID(job["user_id"]), access_token=db.token)
    ai = BailianClient(settings)
    remaining = (datetime.fromisoformat(job["deadline_at"].replace("Z", "+00:00")) - datetime.now(UTC)).total_seconds()
    state = dict(job.get("checkpoint") or {})
    ai.budget = Budget(
        settings.agent_max_calls,
        settings.agent_max_tokens,
        max(0, remaining),
        state.get("calls", 0),
        state.get("tokens", 0),
    )
    ai.budget.semaphore = asyncio.Semaphore(settings.agent_max_parallel)
    ai.budget.max_input_characters = settings.agent_max_input_characters
    state.update(strategy=settings.analysis_strategy, fusion=settings.retrieval_fusion)
    state.update(
        model=settings.chat_model,
        embedding_model=settings.embedding_model,
        call_limit=settings.agent_max_calls,
        token_limit=settings.agent_max_tokens,
        query_prompt="query-plan-v2.1",
        action_prompt="tool-decision-v2.1",
    )

    async def persist_budget():
        state.update(calls=ai.budget.calls, tokens=ai.budget.tokens)
        if ai.budget.tool_validation_errors:
            state["tool_validation_errors"] = ai.budget.tool_validation_errors
        await db.control("checkpoint", state)

    ai.budget.persist = persist_budget

    async def work():
        async with asyncio.timeout(max(0, remaining)):
            if job["kind"] == "index":
                mid = job["payload"]["moment_id"]
                rows = await db.rpc("evidence_snapshot", user.access_token, {"p_ids": [mid]})
                if not rows or rows[0]["revision"] != job["payload"]["revision"]:
                    raise InsufficientEvidenceError("记录已变化")
                chunks = []
                for index, content in enumerate(split_moment(rows[0]["content"])):
                    vector, _ = await ai.embedding(content)
                    chunks.append(
                        {
                            "chunk_index": index,
                            "content": content,
                            "embedding": vector,
                            "embedding_model": settings.embedding_model,
                        }
                    )
                await db.rpc("put_index", user.access_token, {"chunks": chunks})
                return
            retriever = HybridRetriever(db, ai)
            engine = InsightEngine(db, ai, retriever, settings)
            if job["kind"] == "weekly":
                service = WeeklyReportService(db, ai, retriever, engine, settings)
                await service._process(user, UUID(job["payload"]["report_id"]))
            else:
                if settings.analysis_strategy == "fixed":
                    await engine.answer(user, job["payload"]["question"])
                    return
                runtime = AgentRuntime(db, ai, engine, settings, state)
                # Use one state object for runtime progress and budget reservation.
                runtime.state = state
                await runtime.run(user, job["payload"]["question"])

    async def heartbeat():
        while True:
            await asyncio.sleep(10)
            await db.control("heartbeat")
            HEARTBEAT.touch()

    action, pulse = asyncio.create_task(work()), asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait({action, pulse}, return_when=asyncio.FIRST_COMPLETED)
        if action in done:
            await action
        else:
            await pulse  # Cancellation / lost lease interrupts model request.
    except asyncio.CancelledError:
        # Leave the lease; the next worker can reclaim after expiry.
        raise
    except Exception as exc:
        permanent = isinstance(
            exc, (InsufficientEvidenceError, BudgetExceeded, ValueError, PermissionError, TimeoutError)
        )
        code = type(exc).__name__
        if isinstance(exc, BudgetExceeded):
            code = exc.code
            state.update(
                calls=ai.budget.calls,
                tokens=ai.budget.tokens,
                budget_failure={
                    "code": code,
                    "call_limit": ai.budget.max_calls,
                    "token_limit": ai.budget.max_tokens,
                    "input_character_limit": ai.budget.max_input_characters,
                },
            )
            with suppress(LeaseLost, SupabaseError):
                await db.control("checkpoint", state)
        if isinstance(exc, SupabaseError) and any(x in str(exc) for x in ("LEASE_LOST", "EVIDENCE_CHANGED")):
            permanent = True
            code = "EVIDENCE_OR_LEASE_CHANGED"
        logger.warning("job_failed task_id=%s kind=%s code=%s", job["id"], job["kind"], code)
        with suppress(LeaseLost, SupabaseError):
            await db.control("fail", {"code": code, "retryable": not permanent})
    finally:
        for task in (action, pulse):
            task.cancel()
        await asyncio.gather(action, pulse, return_exceptions=True)
        await db.aclose()


async def main():
    settings = get_settings()
    if not settings.supabase_service_role_key:
        raise RuntimeError("缺少 Worker 服务端凭据")
    db = SupabaseClient(settings.model_copy(update={"supabase_anon_key": settings.supabase_service_role_key}))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    active = None
    try:
        while not stop.is_set():
            try:
                jobs = await db.rpc(
                    "worker_claim",
                    settings.supabase_service_role_key,
                    {
                        "p_lease_seconds": settings.task_lease_seconds,
                        "p_timeout_seconds": settings.task_timeout_seconds,
                    },
                )
                HEARTBEAT.touch()
                if jobs:
                    logger.info(
                        "job_claimed task_id=%s kind=%s attempt=%s", jobs[0]["id"], jobs[0]["kind"], jobs[0]["attempts"]
                    )
                    active = asyncio.create_task(execute(jobs[0], settings))
                    stopping = asyncio.create_task(stop.wait())
                    await asyncio.wait({active, stopping}, return_when=asyncio.FIRST_COMPLETED)
                    stopping.cancel()
                    if stop.is_set():
                        active.cancel()
                    await asyncio.gather(active, stopping, return_exceptions=True)
                    continue
            except Exception as exc:
                logger.warning("worker_poll_failed code=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
            except TimeoutError:
                pass
    finally:
        if active:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        await db.aclose()
        HEARTBEAT.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    if args.health:
        raise SystemExit(0 if HEARTBEAT.exists() and time.time() - HEARTBEAT.stat().st_mtime < 90 else 1)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
