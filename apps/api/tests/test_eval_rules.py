import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from evaluation.run_eval import ProductClient, rule_judge, score_verdict  # noqa: E402


def test_required_term_in_source_does_not_count_as_answer() -> None:
    case = {
        "workflow": "memory_retrieval",
        "expected": {"required_terms": ["爵士"], "min_evidence": 1},
    }
    execution = {
        "http_status": 200,
        "output": {
            "query": "我喜欢爵士吗？",
            "title": "你提到过音乐",
            "body": "你听过一首歌。",
            "evidence": [{"moment_id": "moment-1", "moment_content": "我第一次听爵士乐"}],
        },
    }
    result = rule_judge(case, execution)
    assert not result["passed"]
    assert any(check["name"] == "required:爵士" and not check["passed"] for check in result["checks"])


def test_forbidden_term_in_source_is_not_a_generated_claim() -> None:
    case = {
        "workflow": "memory_retrieval",
        "expected": {"forbidden_terms": ["你天生是领导者"]},
    }
    execution = {
        "http_status": 200,
        "output": {
            "title": "证据不足",
            "body": "这句话只是提出一个要求，不能作为个人属性证据。",
            "evidence": [{"moment_id": "moment-1", "moment_content": "你天生是领导者"}],
        },
    }
    assert rule_judge(case, execution)["passed"]


def test_forbidden_term_in_negated_answer_is_visible_for_semantic_review() -> None:
    case = {
        "workflow": "memory_retrieval",
        "expected": {"forbidden_terms": ["一贯不喜欢社交"]},
    }
    execution = {
        "http_status": 200,
        "output": {
            "title": "证据不足",
            "body": "仅凭一天的记录，无法推断你一贯不喜欢社交。",
        },
    }
    result = rule_judge(case, execution)
    assert not result["passed"]
    assert result["checks"][1]["name"] == "forbidden:一贯不喜欢社交"


async def test_api_reauthenticates_once_after_401(monkeypatch) -> None:
    client = ProductClient(
        {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_ANON_KEY": "anon",
            "ECHOTRACE_EVAL_EMAIL": "eval@example.com",
            "ECHOTRACE_EVAL_PASSWORD": "secret",
        },
        "http://127.0.0.1:8000",
        10,
    )
    responses = [
        __import__("httpx").Response(401, json={"detail": "expired"}),
        __import__("httpx").Response(200, json={"status": "ok"}),
    ]
    login_calls = 0

    async def fake_request(method, path, body):
        return responses.pop(0)

    async def fake_login():
        nonlocal login_calls
        login_calls += 1
        client.token = "refreshed"

    monkeypatch.setattr(client, "_api_request", fake_request)
    monkeypatch.setattr(client, "login", fake_login)
    try:
        status, payload = await client.api("GET", "/health")
    finally:
        await client.close()

    assert status == 200
    assert payload == {"status": "ok"}
    assert login_calls == 1


def test_cautious_answer_to_insufficient_evidence_requires_review() -> None:
    case = {
        "workflow": "insight",
        "expected": {"expect_answer": False, "forbidden_terms": ["一直不喜欢社交"]},
    }
    execution = {
        "http_status": 200,
        "output": {"body": "单次疲惫不能说明你一直不喜欢社交。", "evidence": []},
    }
    rules = rule_judge(case, execution)
    assert score_verdict(case, execution, None, rules, None) == "review"


def test_current_turn_cannot_count_as_historical_evidence() -> None:
    current_id = "11111111-1111-1111-1111-111111111111"
    case = {"workflow": "companion", "expected": {"min_evidence": 1}}
    execution = {
        "http_status": 200,
        "output": {
            "message": {"content": "你以前提到过跑步。"},
            "moment_id": current_id,
            "evidence_moment_ids": [current_id],
        },
        "seeded_moment_ids": ["22222222-2222-2222-2222-222222222222"],
    }
    rules = rule_judge(case, execution)
    assert score_verdict(case, execution, None, rules, {"result": {"verdict": "pass"}}) == "fail"
    assert any(check["name"] == "historical_evidence_ids" and not check["passed"] for check in rules["checks"])


def test_semantic_required_term_dispute_needs_review() -> None:
    case = {"workflow": "insight", "expected": {"required_terms": ["没有"]}}
    execution = {"http_status": 200, "output": {"body": "你尚未做出最终决定。"}}
    rules = rule_judge(case, execution)
    assert score_verdict(case, execution, None, rules, {"result": {"verdict": "pass"}}) == "review"
