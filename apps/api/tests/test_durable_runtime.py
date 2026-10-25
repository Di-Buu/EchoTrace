import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import Settings
from app.domain import UserContext
from app.services.agent_runtime import (
    AgentRuntime,
    Budget,
    BudgetExceeded,
    QueryPlan,
    ToolAction,
    Window,
    normalize_relative_time,
    tool_decision,
)
from app.services.hybrid import reciprocal_rank_fusion
from app.services.tasks import get_task, request_hash, submit
from app.services.telemetry import Telemetry

UID = UUID("11111111-1111-1111-1111-111111111111")
USER = UserContext(id=UID, access_token="synthetic")


def test_request_hash_order_and_content():
    assert request_hash({"a": 1, "b": 2}) == request_hash({"b": 2, "a": 1})
    assert request_hash({"a": 1}) != request_hash({"a": 2})


def test_rrf_deduplicates_each_channel_not_different_channels():
    rows = [
        {"moment_id": "a", "channel": "vector", "rank": 1},
        {"moment_id": "b", "channel": "vector", "rank": 2},
        {"moment_id": "b", "channel": "text", "rank": 1},
    ]
    assert reciprocal_rank_fusion(rows) == ["b", "a"]
    assert reciprocal_rank_fusion(rows + rows) == ["b", "a"]


def test_budget_does_not_reset_after_restart():
    budget = Budget(3, 100, 20, used_calls=2)
    budget.charge_call()
    with pytest.raises(BudgetExceeded):
        budget.charge_call()
    with pytest.raises(BudgetExceeded):
        Budget(3, 100, 0).charge_call()
    with pytest.raises(BudgetExceeded):
        Budget(3, 100, 20).charge_tokens(101)


@pytest.mark.asyncio
async def test_auth_network_failure_is_not_reported_as_wrong_password():
    from app.dependencies import get_current_user

    db = type("Db", (), {"resolve_user": AsyncMock(side_effect=TimeoutError())})()
    with pytest.raises(HTTPException) as exc:
        await get_current_user(db, "Bearer synthetic")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "rate_limit"])
async def test_http_retry_is_bounded_and_counted(monkeypatch, failure):
    import httpx

    from app.clients.ai import AiResponseError, BailianClient

    calls = []

    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        return httpx.Response(429, json={"error": "synthetic"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.clients.ai.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )
    monkeypatch.setattr("app.clients.ai.asyncio.sleep", AsyncMock())
    ai = BailianClient(Settings(_env_file=None, dashscope_api_key="synthetic"))
    ai.budget = Budget(24, 1000, 30)
    with pytest.raises(AiResponseError):
        await ai._post_json("/chat/completions", {})
    assert len(calls) == (2 if failure == "timeout" else 3)
    assert ai.budget.calls == len(calls)


@pytest.mark.asyncio
async def test_worker_adapter_does_not_allow_model_to_change_owner():
    from app.clients.worker_db import WorkerDatabase

    db = WorkerDatabase(
        Settings(_env_file=None, supabase_service_role_key="synthetic"),
        {"id": str(UID), "user_id": str(UID), "lease_token": str(UID)},
    )
    try:
        with pytest.raises(PermissionError):
            db.scoped("moments", {"user_id": str(UUID(int=2))})
        with pytest.raises(PermissionError):
            db.scoped("auth.users", {})
    finally:
        await db.aclose()


def test_time_windows_timezone_month_end_and_no_overlap():
    with pytest.raises(ValidationError):
        Window(start=datetime(2026, 1, 1))
    plan = QueryPlan(kind="change", topic="工作", current=Window())
    actual = normalize_relative_time(plan, "最近三个月比以前", datetime(2026, 5, 31, tzinfo=UTC))
    assert actual.current.start.day == 28
    assert actual.current.start.month == 2
    assert actual.comparison.end == actual.current.start


@pytest.mark.asyncio
async def test_no_telemetry_failure_escapes(caplog):
    db = type("Db", (), {"insert": AsyncMock(side_effect=RuntimeError("do not print private text"))})()
    await Telemetry(db, "token", UID).product_event(event_name="test", properties={})
    assert "telemetry_failed" in caplog.text
    assert "private text" not in caplog.text


@pytest.mark.asyncio
async def test_conflicting_idempotency_key_maps_409():
    from app.clients.supabase import SupabaseError

    db = type("Db", (), {"rpc": AsyncMock(side_effect=SupabaseError("IDEMPOTENCY_CONFLICT"))})()
    with pytest.raises(HTTPException) as exc:
        await submit(db, USER, "moment", {"content": "test"}, "same")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_guessing_task_id_is_404():
    db = type("Db", (), {"select": AsyncMock(return_value=[])})()
    with pytest.raises(HTTPException) as exc:
        await get_task(db, USER, UID)
    assert exc.value.status_code == 404
    assert db.select.call_args.kwargs["params"]["user_id"] == f"eq.{UID}"


@pytest.mark.asyncio
async def test_native_tool_call_validates_structure_and_identity():
    ai = type(
        "Ai",
        (),
        {
            "settings": Settings(_env_file=None),
            "_post_json": AsyncMock(
                return_value={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "history_action",
                                            "arguments": json.dumps(
                                                {"tool": "search_counter", "query": "恢复 工作", "reason": "检查反例"}
                                            ),
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ),
        },
    )()
    result = await tool_decision(ai, {"question": "test"})
    assert result.tool == "search_counter"
    with pytest.raises(ValidationError):
        ToolAction(tool="finish", reason="test", user_id=str(UID))


@pytest.mark.asyncio
@pytest.mark.parametrize("corrected", [True, False])
async def test_tool_argument_repair_is_bounded_and_never_executes_bad_arguments(corrected):
    from app.services.agent_runtime import ToolArgumentsError

    def response(arguments):
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "test-call",
                                "function": {"name": "history_action", "arguments": json.dumps(arguments)},
                            }
                        ]
                    }
                }
            ]
        }

    bad = response({"tool": "read_original", "ids": ["private-invalid-value"], "reason": "展开"})
    good = response({"tool": "read_original", "ids": [str(UID)], "reason": "展开"})
    payloads = []

    async def send(path, payload):
        payloads.append(json.loads(json.dumps(payload)))
        return bad if len(payloads) == 1 or not corrected else good

    budget = Budget(24, 1000, 30)
    budget.persist = AsyncMock()
    ai = type(
        "Ai", (), {"settings": Settings(_env_file=None), "budget": budget, "_post_json": AsyncMock(side_effect=send)}
    )()
    if corrected:
        result = await tool_decision(ai, {})
        assert result.ids == [UID]
    else:
        with pytest.raises(ToolArgumentsError):
            await tool_decision(ai, {})
    assert len(payloads) == 2
    assert payloads[1]["messages"][-1]["role"] == "tool"
    assert "private-invalid-value" not in json.dumps(budget.tool_validation_errors)
    assert budget.tool_validation_errors[-1]["field"] == ["ids", 0]
    assert budget.persist.await_count == (1 if corrected else 2)


@pytest.mark.asyncio
async def test_agent_changes_action_after_empty_tool_result(monkeypatch):
    from app.services.insights import InsufficientEvidenceError

    actions = iter(
        [
            ToolAction(tool="search_current", reason="初查"),
            ToolAction(tool="search_counter", query="其他说法", reason="初查为空，换线索"),
            ToolAction(tool="insufficient", reason="两次都不足"),
        ]
    )
    seen = []

    async def decide(ai, context):
        seen.append(context)
        return next(actions)

    monkeypatch.setattr("app.services.agent_runtime.tool_decision", decide)
    db = type("Db", (), {"control": AsyncMock(return_value={}), "rpc": AsyncMock(return_value=[])})()
    ai = type("Ai", (), {"settings": Settings(_env_file=None), "budget": Budget(24, 1000, 30)})()
    runtime = AgentRuntime(
        db,
        ai,
        None,
        ai.settings,
        {"plan": QueryPlan(kind="fact", topic="test", current=Window()).model_dump(mode="json")},
    )
    runtime.retriever.search = AsyncMock(return_value=[])
    with pytest.raises(InsufficientEvidenceError):
        await runtime.run(USER, "test")
    assert seen[1]["observations"][0]["count"] == 0
    assert runtime.retriever.search.await_count == 2


@pytest.mark.asyncio
async def test_agent_never_finishes_change_with_only_recent_evidence(monkeypatch):
    from app.services.insights import InsufficientEvidenceError

    monkeypatch.setattr(
        "app.services.agent_runtime.tool_decision", AsyncMock(return_value=ToolAction(tool="finish", reason="结束"))
    )
    db = type("Db", (), {"control": AsyncMock(return_value={})})()
    ai = type("Ai", (), {"settings": Settings(_env_file=None, agent_max_steps=2), "budget": Budget(24, 1000, 30)})()
    engine = type("Engine", (), {"_analyze_and_store": AsyncMock()})()
    plan = QueryPlan(kind="change", topic="test", current=Window(), comparison=Window())
    runtime = AgentRuntime(
        db,
        ai,
        engine,
        ai.settings,
        {"plan": plan.model_dump(mode="json"), "observations": [{"tool": "search_current", "count": 1}]},
    )
    with pytest.raises(InsufficientEvidenceError):
        await runtime.run(USER, "test")
    engine._analyze_and_store.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_index_only_publishes_through_atomic_rpc(monkeypatch):
    from datetime import timedelta

    from app import worker

    async def rpc(name, token, payload):
        if name == "evidence_snapshot":
            return [{"id": str(UID), "revision": 2, "content": "合成记录"}]
        assert name == "put_index"
        assert payload["chunks"][0]["embedding"] == [1.0]
        return []

    db = type(
        "Db",
        (),
        {"token": "synthetic", "rpc": AsyncMock(side_effect=rpc), "control": AsyncMock(), "aclose": AsyncMock()},
    )()
    ai = type("Ai", (), {"embedding": AsyncMock(return_value=([1.0], {}))})()
    monkeypatch.setattr(worker, "WorkerDatabase", lambda *_: db)
    monkeypatch.setattr(worker, "BailianClient", lambda *_: ai)
    await worker.execute(
        {
            "id": str(UID),
            "user_id": str(UID),
            "kind": "index",
            "deadline_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "payload": {"moment_id": str(UID), "revision": 2},
        },
        Settings(_env_file=None),
    )
    assert [call.args[0] for call in db.rpc.await_args_list] == ["evidence_snapshot", "put_index"]
    db.aclose.assert_awaited_once()
    db.control.assert_not_awaited()  # Completion belongs to put_index transaction.


@pytest.mark.asyncio
async def test_worker_rejects_changed_source_without_embedding(monkeypatch):
    from datetime import timedelta

    from app import worker

    db = type(
        "Db",
        (),
        {"token": "synthetic", "rpc": AsyncMock(return_value=[]), "control": AsyncMock(), "aclose": AsyncMock()},
    )()
    ai = type("Ai", (), {"embedding": AsyncMock()})()
    monkeypatch.setattr(worker, "WorkerDatabase", lambda *_: db)
    monkeypatch.setattr(worker, "BailianClient", lambda *_: ai)
    await worker.execute(
        {
            "id": str(UID),
            "user_id": str(UID),
            "kind": "index",
            "deadline_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "payload": {"moment_id": str(UID), "revision": 2},
        },
        Settings(_env_file=None),
    )
    ai.embedding.assert_not_awaited()
    assert db.control.call_args.args == ("fail", {"code": "InsufficientEvidenceError", "retryable": False})


@pytest.mark.asyncio
async def test_analysis_submission_is_202_without_model_execution():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.dependencies import get_current_user, get_db
    from app.routers.insights import router
    from app.service_dependencies import get_insight_engine

    app = FastAPI()
    app.include_router(router)
    db = type("Db", (), {"rpc": AsyncMock(return_value=[{"id": str(UID), "status": "queued"}])})()
    engine = type("Engine", (), {"answer": AsyncMock()})()
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_insight_engine] = lambda: engine
    with TestClient(app) as client:
        response = client.post("/insights/query", json={"question": "合成测试"}, headers={"Idempotency-Key": "test"})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    engine.answer.assert_not_awaited()
