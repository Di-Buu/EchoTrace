from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.config import Settings
from app.domain import UserContext
from app.services.agent_runtime import AgentRuntime, Budget, QueryPlan, ToolAction, Window
from app.services.insights import InsufficientEvidenceError

USER = UserContext(id=UUID(int=1), access_token="synthetic")
RELATED, NOISE = str(UUID(int=2)), str(UUID(int=3))


def restored_runtime(monkeypatch, selected, plan=None, observations=None):
    now = datetime.now(UTC)
    rows = [
        {
            "id": mid,
            "revision": 1,
            "content": "合成相关记录" if mid == RELATED else "合成无关记录",
            "created_at": (now - timedelta(days=1 if mid == RELATED else 100)).isoformat(),
        }
        for mid in (RELATED, NOISE)
    ]
    db = type("Db", (), {"rpc": AsyncMock(return_value=rows), "control": AsyncMock()})()
    ai = type("Ai", (), {"settings": Settings(_env_file=None, agent_max_steps=1), "budget": Budget(24, 80000, 30)})()
    engine = type("Engine", (), {"_analyze_and_store": AsyncMock(return_value={"ok": True})})()
    state = {
        "plan": (plan or QueryPlan(kind="causality", topic="合成", current=Window())).model_dump(mode="json"),
        "ids": [RELATED, NOISE],
        "versions": {RELATED: 1, NOISE: 1},
        "observations": observations or [{"tool": "search_current", "count": 2, "ids": [RELATED, NOISE]}],
    }
    monkeypatch.setattr(
        "app.services.agent_runtime.tool_decision",
        AsyncMock(return_value=ToolAction(tool="finish", ids=selected, reason="只使用相关证据")),
    )
    return AgentRuntime(db, ai, engine, ai.settings, state), engine


@pytest.mark.asyncio
async def test_finish_selects_writing_sources_and_keeps_independent_review_pool(monkeypatch):
    runtime, engine = restored_runtime(monkeypatch, [RELATED])
    await runtime.run(USER, "合成因果问题")
    call = engine._analyze_and_store.call_args.kwargs
    assert [str(item.moment_id) for item in call["evidence"]] == [RELATED]
    assert {str(item.moment_id) for item in call["review_evidence"]} == {RELATED, NOISE}
    assert call["route"].insight_type == "fact"
    assert call["route"].specialists == ["pattern"]
    assert runtime.state["selected_ids"] == [RELATED]


@pytest.mark.asyncio
async def test_finish_cannot_select_unretrieved_id(monkeypatch):
    runtime, engine = restored_runtime(monkeypatch, [str(UUID(int=99))])
    with pytest.raises(PermissionError):
        await runtime.run(USER, "合成问题")
    engine._analyze_and_store.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_cannot_drop_the_comparison_period_when_selecting(monkeypatch):
    boundary = datetime.now(UTC) - timedelta(days=30)
    plan = QueryPlan(kind="change", topic="合成", current=Window(start=boundary), comparison=Window(end=boundary))
    runtime, engine = restored_runtime(
        monkeypatch,
        [RELATED],
        plan,
        [
            {"tool": "search_current", "count": 1, "ids": [RELATED]},
            {"tool": "search_comparison", "count": 1, "ids": [NOISE]},
        ],
    )
    with pytest.raises(InsufficientEvidenceError):
        await runtime.run(USER, "变化")
    engine._analyze_and_store.assert_not_awaited()


@pytest.mark.asyncio
async def test_causal_proof_question_does_not_force_a_historical_comparison(monkeypatch):
    initial = QueryPlan(kind="change", topic="合成", current=Window(start=datetime.now(UTC)), comparison=Window())
    ai = type(
        "Ai",
        (),
        {
            "structured_chat": AsyncMock(return_value=(initial, {})),
            "settings": Settings(_env_file=None),
            "budget": Budget(24, 80000, 30),
        },
    )()
    db = type("Db", (), {"control": AsyncMock()})()
    monkeypatch.setattr(
        "app.services.agent_runtime.tool_decision",
        AsyncMock(return_value=ToolAction(tool="insufficient", reason="合成测试停止")),
    )
    runtime = AgentRuntime(db, ai, None, ai.settings)
    with pytest.raises(InsufficientEvidenceError):
        await runtime.run(USER, "能证明这项活动让睡眠改善吗？")
    assert runtime.state["plan"]["kind"] == "causality"
    assert runtime.state["plan"]["needs_counter"] is True
    assert runtime.state["plan"]["comparison"] is None
    assert runtime.state["plan"]["current"]["start"] is None
