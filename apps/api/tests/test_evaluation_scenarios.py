import json
from pathlib import Path


def test_formal_scenarios_are_product_focused_and_complete() -> None:
    path = Path(__file__).resolve().parents[3] / "evaluation" / "scenarios.jsonl"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert 35 <= len(cases) <= 45
    assert len({case["case_id"] for case in cases}) == len(cases)
    required = {"case_id", "description", "category", "workflow", "history", "request", "expected"}
    assert all(required.issubset(case) for case in cases)
    categories = {category for case in cases for category in case["category"]}
    assert "cross_user_isolation" not in categories
    assert {
        "long_term_memory",
        "unsupported_personal_fact",
        "temporal_relation",
        "insight_evidence",
        "over_inference",
        "bad_case_regression",
    }.issubset(categories)


def test_every_scenario_has_timestamped_history_items() -> None:
    path = Path(__file__).resolve().parents[3] / "evaluation" / "scenarios.jsonl"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    for case in cases:
        assert all({"at", "text"}.issubset(item) for item in case["history"])
