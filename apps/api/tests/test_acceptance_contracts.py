"""Real loopback model HTTP only; NOT Supabase or Docker end-to-end acceptance."""

import importlib.util
import logging
import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

import pytest

from app.clients.ai import BailianClient
from app.config import Settings
from app.domain import UserContext
from app.services.agent_runtime import AgentRuntime, Budget
from app.services.hybrid import HybridRetriever
from app.services.insights import InsightEngine

MID = "11111111-1111-1111-1111-111111111111"
OLD_MID = "33333333-3333-3333-3333-333333333333"
UID = "22222222-2222-2222-2222-222222222222"
MOCK_PATH = Path(__file__).resolve().parents[3] / "tools/mock_provider.py"


class SyntheticDatabase:
    def __init__(self):
        self.checkpoints = []
        self.commits = []

    async def control(self, action, state):
        self.checkpoints.append(dict(state))

    async def rpc(self, name, token, payload):
        if name == "search_evidence_v2":
            return [
                {
                    "moment_id": OLD_MID if payload.get("p_to") and not payload.get("p_from") else MID,
                    "channel": "vector",
                    "rank": 1,
                }
            ]
        if name == "search_weekly_summary_card":
            return []
        if name == "evidence_snapshot":
            return [
                {
                    "id": mid,
                    "user_id": UID,
                    "content": "上周买了一个蓝色水杯。",
                    "revision": 1,
                    "created_at": (datetime.now(UTC) - timedelta(days=200 if mid == OLD_MID else 10)).isoformat(),
                }
                for mid in payload["p_ids"]
            ]
        if name == "commit_insight":
            self.commits.append(payload)
            return [{"id": MID, **payload["p_insight"]}]
        raise AssertionError(name)

    async def select(self, table, token, *, params):
        assert table in {"moments", "memory_sources"}
        return (
            [
                {"id": mid, "user_id": UID, "memory_enabled": True}
                for mid in (MID, OLD_MID)
                if mid in params.get("id", "")
            ]
            if table == "moments"
            else []
        )

    async def insert(self, table, token, payload):
        assert table == "ai_runs"
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["回顾我的水杯记录", "水杯记录比以前有什么变化？"])
async def test_mock_model_full_native_protocol_over_real_http(monkeypatch, caplog, question):
    """Catch mismatches in the actual Docker mock, schemas and AI client."""
    spec = importlib.util.spec_from_file_location("acceptance_mock_provider", MOCK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("MOCK_DELAY_SECONDS", "0")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    caplog.set_level(logging.INFO)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = Settings(
            _env_file=None,
            dashscope_base_url=f"http://127.0.0.1:{server.server_port}/v1",
            dashscope_api_key="synthetic",
            chat_model="mock",
            embedding_model="mock",
        )
        ai = BailianClient(settings)
        ai.budget = Budget(24, 40000, 30)
        db = SyntheticDatabase()
        engine = InsightEngine(db, ai, HybridRetriever(db, ai), settings)
        runtime = AgentRuntime(db, ai, engine, settings)
        result = await runtime.run(UserContext(id=UUID(UID), access_token="synthetic"), question)
        assert result["body"] == "上周买了一个蓝色水杯。"
        assert len(db.commits) == 1
        assert db.commits[0]["p_versions"] == ({MID: 1, OLD_MID: 1} if "变化" in question else {MID: 1})
        assert result["query"] == question
        assert ai.budget.calls <= 24
        expected = {"search_current", "search_comparison"} if "变化" in question else {"search_current"}
        assert {row["tool"] for row in runtime.state["observations"]} == expected
        assert "上周买了一个蓝色水杯" not in caplog.text
        assert "Bearer synthetic" not in caplog.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
