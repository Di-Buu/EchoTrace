"""Offline regressions for the observed real-provider acceptance failures."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from app.clients.ai import BailianClient
from app.config import Settings
from app.domain import UserContext
from app.services.agent_runtime import AgentRuntime, Budget, BudgetExceeded, QueryPlan, ToolAction, Window
from app.services.hybrid import HybridRetriever

UID = UUID(int=1)
USER = UserContext(id=UID, access_token="synthetic")
MID = str(UUID(int=2))


@pytest.mark.asyncio
async def test_event_search_falls_back_only_for_unknown_event_dates():
    now = datetime.now(UTC)
    ids = [str(UUID(int=n)) for n in range(2, 6)]
    rows = {
        ids[0]: {"event_at": None, "created_at": now.isoformat()},
        ids[1]: {"event_at": (now - timedelta(days=100)).isoformat(), "created_at": now.isoformat()},
        ids[2]: {"event_at": now.isoformat(), "created_at": now.isoformat()},
        ids[3]: {"event_at": None, "created_at": (now - timedelta(days=100)).isoformat()},
    }
    for mid, row in rows.items():
        row.update(
            id=mid, content="合成记录", revision=1, event_time_source="user_confirmed" if row["event_at"] else "unknown"
        )

    async def rpc(name, token, payload):
        if name == "search_weekly_summary_card":
            return []
        if name == "evidence_snapshot":
            return [rows[mid] for mid in payload["p_ids"]]
        assert name == "search_evidence_v2"
        return [
            {"moment_id": mid, "channel": "vector", "rank": rank}
            for rank, mid in enumerate(ids if payload["p_axis"] == "record" else [ids[2]], 1)
        ]

    db = type("Db", (), {"rpc": AsyncMock(side_effect=rpc)})()
    ai = type("Ai", (), {"settings": Settings(_env_file=None), "embedding": AsyncMock(return_value=([1.0], {}))})()
    retriever = HybridRetriever(db, ai)
    retriever._verify_owner = AsyncMock()
    result = await retriever.search(
        USER, "合成", axis="event", time_from=now - timedelta(days=5), time_to=now + timedelta(days=1)
    )
    assert {str(item.moment_id) for item in result} == {ids[0], ids[2]}
    assert next(item for item in result if str(item.moment_id) == ids[0]).event_at is None
    retriever._verify_owner.assert_awaited_once_with(USER, {ids[0], ids[2]})
    ai.embedding.assert_awaited_once()
    assert ids[0] in retriever.last_trace["record_time_fallback_ids"]


@pytest.mark.asyncio
async def test_agent_passes_time_uncertainty_to_verification(monkeypatch):
    from app.domain import RetrievedEvidence

    actions = iter(
        [ToolAction(tool="search_current", reason="初查"), ToolAction(tool="finish", ids=[MID], reason="核查")]
    )
    monkeypatch.setattr("app.services.agent_runtime.tool_decision", AsyncMock(side_effect=lambda *_: next(actions)))
    db = type("Db", (), {"control": AsyncMock(), "rpc": AsyncMock(return_value=[{"id": MID, "revision": 1}])})()
    ai = type("Ai", (), {"budget": Budget(24, 40000, 30), "settings": Settings(_env_file=None)})()
    engine = type("Engine", (), {"_analyze_and_store": AsyncMock(return_value={"ok": True})})()
    plan = QueryPlan(kind="fact", topic="合成", current=Window(), time_axis="event")
    runtime = AgentRuntime(db, ai, engine, ai.settings, {"plan": plan.model_dump(mode="json")})
    runtime.retriever.search = AsyncMock(
        return_value=[
            RetrievedEvidence(
                memory_content="合成",
                memory_type="moment",
                confidence=1,
                moment_id=MID,
                moment_content="合成",
                occurred_at=datetime.now(UTC),
                score=1,
            )
        ]
    )
    await runtime.run(USER, "合成问题")
    assert "不能据此断言事件发生日期" in engine._analyze_and_store.call_args.kwargs["question"]
    assert runtime.state["plan"]["time_axis"] == "event"


@pytest.mark.parametrize(
    "kind,code",
    [("calls", "BUDGET_CALLS"), ("tokens", "BUDGET_TOKENS"), ("time", "BUDGET_TIME"), ("input", "BUDGET_INPUT_SIZE")],
)
def test_budget_reasons_are_distinct(kind, code):
    budget = Budget(3, 100, 0 if kind == "time" else 30)
    if kind == "calls":
        budget.calls = 3
    if kind == "tokens":
        budget.tokens = 100
    with pytest.raises(BudgetExceeded) as raised:
        budget.check_payload({"text": "x" * 50001} if kind == "input" else {})
    assert raised.value.code == code


@pytest.mark.asyncio
async def test_over_limit_provider_usage_is_persisted_before_error(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.clients.ai.httpx.AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"usage": {"total_tokens": 101}})),
            **kwargs,
        ),
    )
    ai = BailianClient(Settings(_env_file=None, dashscope_api_key="synthetic"))
    ai.budget = Budget(3, 100, 30)
    saved = []

    async def persist():
        saved.append((ai.budget.calls, ai.budget.tokens))

    ai.budget.persist = persist
    with pytest.raises(BudgetExceeded, match="Token") as raised:
        await ai._post_json("/chat/completions", {})
    assert raised.value.code == "BUDGET_TOKENS"
    assert saved[-1] == (1, 101)


@pytest.mark.asyncio
async def test_worker_keeps_budget_reason_and_usage_without_retry(monkeypatch):
    from app import worker

    db = type(
        "Db",
        (),
        {
            "token": "synthetic",
            "rpc": AsyncMock(return_value=[{"id": MID, "revision": 1, "content": "合成"}]),
            "control": AsyncMock(),
            "aclose": AsyncMock(),
        },
    )()
    ai = type("Ai", (), {})()

    async def embedding(_):
        ai.budget.charge_tokens(1001)

    ai.embedding = embedding
    monkeypatch.setattr(worker, "WorkerDatabase", lambda *_: db)
    monkeypatch.setattr(worker, "BailianClient", lambda *_: ai)
    await worker.execute(
        {
            "id": MID,
            "user_id": str(UID),
            "kind": "index",
            "deadline_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "payload": {"moment_id": MID, "revision": 1},
        },
        Settings(_env_file=None, agent_max_tokens=1000),
    )
    checkpoints = [c.args[1] for c in db.control.await_args_list if c.args[0] == "checkpoint"]
    assert checkpoints[-1]["tokens"] == 1001
    assert checkpoints[-1]["budget_failure"]["code"] == "BUDGET_TOKENS"
    assert db.control.call_args.args == ("fail", {"code": "BUDGET_TOKENS", "retryable": False})
