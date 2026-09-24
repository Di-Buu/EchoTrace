import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from evaluation.evidence_metrics import load_gold, score_evidence  # noqa: E402
from evaluation.incremental import behavior_signature, cache_status  # noqa: E402
from evaluation.run_eval import EvaluationError, ProductClient, load_cases, run_case  # noqa: E402


def baseline(commit: str, verifier: str = "verifier-v1.1") -> dict:
    return {
        "git": {"commit": commit, "dirty": False},
        "chat_model": "qwen",
        "embedding_model": "embedding",
        "embedding_dimension": 1024,
        "execution_profile": "local_quality",
        "insight_thinking": True,
        "insight_timeout_seconds": 120,
        "prompt_versions": {
            "companion": "companion-v1.1",
            "orchestrator": "orchestrator-v1",
            "temporal": "temporal-v1",
            "pattern": "pattern-v1",
            "verifier": verifier,
            "synthesizer": "synthesizer-v1.1",
            "synthesis_audit": "synthesis-audit-v1",
        },
    }


def test_fingerprint_ignores_unrelated_git_and_prompt_changes() -> None:
    case = next(item for item in load_cases() if item["case_id"] == "personal_fact_supported_running")
    assert behavior_signature(case, baseline("old")) == behavior_signature(
        case, baseline("new", verifier="verifier-v2")
    )


def test_insight_fingerprint_changes_with_relevant_prompt() -> None:
    case = next(item for item in load_cases() if item["case_id"] == "temporal_reading_habit")
    assert behavior_signature(case, baseline("same")) != behavior_signature(
        case, baseline("same", verifier="verifier-v2")
    )


def test_gold_labels_cover_existing_cases() -> None:
    cases = load_cases()
    gold = load_gold()
    assert set(gold) == {case["case_id"] for case in cases}
    for case in cases:
        assert all(0 <= index < len(case["history"]) for index in gold[case["case_id"]])


def test_missing_old_retrieval_trace_is_not_scored_as_zero() -> None:
    case = next(item for item in load_cases() if item["case_id"] == "memory_stable_interest")
    result = {
        "execution": {
            "seeded_moment_ids": ["moment-1"],
            "output": {"evidence": [{"moment_id": "moment-1"}]},
        }
    }
    score = score_evidence(case, result, load_gold())
    assert score["retrieval_status"] == "unavailable_old_result"
    assert score["retrieval_recall_at_k"] is None
    assert score["citation_recall"] == 1


def test_retrieval_scores_ordered_ids_from_trace() -> None:
    case = next(item for item in load_cases() if item["case_id"] == "memory_update_current")
    result = {
        "execution": {
            "seeded_moment_ids": ["old", "new"],
            "retrieval_trace": [{"retrieved_moment_ids": ["new", "irrelevant", "old"]}],
            "output": {"evidence": [{"moment_id": "new"}]},
        }
    }
    score = score_evidence(case, result, load_gold())
    assert score["retrieval_recall_at_k"] == 1
    assert score["retrieval_precision_at_k"] == pytest.approx(2 / 3)
    assert score["citation_recall"] == 0.5


def test_stale_new_run_cache_is_not_reused(tmp_path: Path) -> None:
    case = next(item for item in load_cases() if item["case_id"] == "temporal_reading_habit")
    current = baseline("same")
    current["api_runtime_fingerprint"] = "current-api"
    fingerprint = behavior_signature(case, current)
    path = tmp_path / f"{case['case_id']}-{fingerprint[:12]}.json"
    path.write_text(
        json.dumps({"execution": {"http_status": 200}, "error": None, "cache_origin": "new_run"}),
        encoding="utf-8",
    )
    assert cache_status(case, current, tmp_path)["status"] == "run"


def test_exact_workflow_cache_survives_unrelated_api_change(tmp_path: Path) -> None:
    case = next(item for item in load_cases() if item["case_id"] == "companion_avoids_old_history")
    current = baseline("same")
    current["api_runtime_fingerprint"] = "new-api"
    fingerprint = behavior_signature(case, current)
    path = tmp_path / f"{case['case_id']}-{fingerprint[:12]}.json"
    path.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "api_runtime_fingerprint": "old-api",
                "execution": {"http_status": 200},
                "error": None,
                "cache_origin": "new_run",
            }
        ),
        encoding="utf-8",
    )
    status = cache_status(case, current, tmp_path)
    assert status["status"] == "reuse"
    assert "全局指纹变化" in status["reason"]


def test_audit_signature_migration_requires_identical_api_runtime(tmp_path: Path) -> None:
    case = next(item for item in load_cases() if item["case_id"] == "temporal_reading_habit")
    current = baseline("same")
    current["api_runtime_fingerprint"] = "same-api"
    old_fingerprint = behavior_signature(case, current, include_synthesis_audit=False)
    path = tmp_path / f"{case['case_id']}-{old_fingerprint[:12]}.json"
    path.write_text(
        json.dumps(
            {
                "fingerprint": old_fingerprint,
                "api_runtime_fingerprint": "same-api",
                "cache_origin": "new_run",
                "execution": {"http_status": 200},
                "error": None,
            }
        ),
        encoding="utf-8",
    )
    assert cache_status(case, current, tmp_path)["status"] == "reuse"
    current["api_runtime_fingerprint"] = "changed-api"
    assert cache_status(case, current, tmp_path)["status"] == "run"


@pytest.mark.asyncio
async def test_runtime_guard_rejects_old_server_before_login(monkeypatch) -> None:
    class HealthResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": "ok", "service": "echotrace-api"}

    client = ProductClient({}, "http://127.0.0.1:8000", 1)

    async def old_health(*args, **kwargs):
        return HealthResponse()

    monkeypatch.setattr(client.http, "get", old_health)
    try:
        with pytest.raises(EvaluationError, match="旧代码"):
            await client.verify_runtime("current-api")
        assert not client.token
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_review_cache_never_reexecutes_product(monkeypatch, tmp_path: Path) -> None:
    case = {
        "case_id": "cached-review",
        "description": "cached",
        "category": ["insight_evidence"],
        "workflow": "insight",
        "history": [],
        "request": "question",
        "expected": {},
        "manual_review": True,
    }
    old = {
        "case_id": case["case_id"],
        "workflow": case["workflow"],
        "execution": {"http_status": 200, "output": {"body": "谨慎回答"}, "seeded_moment_ids": []},
        "verdict": "review",
        "error": None,
        "llm_judge": None,
        "manual_review": True,
        "attempts": 1,
    }
    status = {
        "status": "reuse",
        "reason": "test",
        "fingerprint": "abc",
        "path": tmp_path / "cache.json",
        "item": old,
    }
    monkeypatch.setattr("evaluation.run_eval.cache_status", lambda *args, **kwargs: status)
    result = await run_case(
        object(), object(), case, baseline=baseline("x"), cache_dir=tmp_path,
        force=False, enable_judge=False, retries=0, case_timeout=1,
    )
    assert result["cached"] is True
    assert result["verdict"] == "review"
    assert json.loads(status["path"].read_text(encoding="utf-8"))["verdict"] == "review"


@pytest.mark.asyncio
async def test_optional_ragas_scores_saved_ids_without_product_or_model(tmp_path: Path) -> None:
    pytest.importorskip("ragas")
    from evaluation.ragas_support import score_saved_run

    saved = {
        "case_id": "memory_stable_interest",
        "workflow": "memory_retrieval",
        "execution": {
            "seeded_moment_ids": ["offline-moment"],
            "retrieval_trace": [{"retrieved_moment_ids": ["offline-moment"]}],
            "output": {"body": "提过爵士乐", "evidence": [{"moment_id": "offline-moment"}]},
        },
    }
    (tmp_path / "raw_results.jsonl").write_text(
        json.dumps(saved, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    summary = await score_saved_run(tmp_path, faithfulness=False)
    assert summary["id_scored"] == 1
    assert summary["faithfulness_scored"] == 0
    result = json.loads((tmp_path / "ragas_results.jsonl").read_text(encoding="utf-8"))
    assert result["id_recall"] == 1
