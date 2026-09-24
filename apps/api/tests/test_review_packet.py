import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from evaluation.review_packet import render_review_packet  # noqa: E402
from evaluation.run_eval import write_reports  # noqa: E402


def test_review_packet_shows_source_answer_and_rule_dispute() -> None:
    cases = [
        {
            "case_id": "choice",
            "description": "不把未决定写成循环",
            "category": ["insight_evidence"],
            "workflow": "insight",
            "history": [{"at": "2026-05-01T00:00:00Z", "text": "我还在比较两个选择。"}],
            "request": "我决定了吗？",
            "expected": {"required_terms": ["没有"]},
        }
    ]
    results = [
        {
            "case_id": "choice",
            "verdict": "review",
            "manual_review": False,
            "rule_judge": {"checks": [{"name": "required:没有", "passed": False}]},
            "llm_judge": {"result": {"verdict": "pass", "reason": "保留了不确定性"}},
            "execution": {
                "seeded_moment_ids": ["moment-1"],
                "output": {
                    "body": "你尚未做出决定。",
                    "evidence": [{"moment_id": "moment-1", "stance": "support"}],
                },
            },
        }
    ]
    text = render_review_packet(
        cases, results, {"api_runtime_fingerprint": "current-api", "prompt_versions": {"verifier": "v1.2"}}
    )
    for expected in ("我还在比较两个选择", "你尚未做出决定", "required:没有", "moment-1", "人工结论"):
        assert expected in text
    assert "共 1 条" in text


def test_review_packet_does_not_relabel_review_as_pass() -> None:
    text = render_review_packet([], [], {})
    assert "共 0 条" in text
    assert "不自动把 review 改成 pass" in text


def test_review_packet_marks_unverified_source() -> None:
    text = render_review_packet([], [], {}, source_warning="旧 API 进程")
    assert "版本来源警告：旧 API 进程" in text


def test_standard_report_also_writes_review_packet(tmp_path: Path) -> None:
    result = {
        "case_id": "evidence_conflicting_goal",
        "category": ["insight_evidence"],
        "workflow": "insight",
        "verdict": "review",
        "manual_review": True,
        "attempts": 1,
        "duration_ms": 1,
        "execution": {"output": {"body": "尚未做决定。"}, "seeded_moment_ids": []},
        "rule_judge": {"checks": []},
    }
    metrics = {
        "generated_at": "2026-09-24T00:00:00Z",
        "baseline": {
            "git": {"commit": "test", "dirty": False},
            "api_runtime_fingerprint": "test-api",
            "evaluation_fingerprint": "test-evaluation",
            "chat_model": "test",
            "embedding_model": "test",
            "prompt_versions": {},
        },
        "overall": {"total": 1, "passed": 0, "failed": 0, "review": 1, "cached": 0, "reused_historical": 0},
        "evidence_metrics": {"retrieval_scored": 0, "retrieval_missing_trace": 1, "citation_scored": 0},
        "by_category": {"insight_evidence": {"total": 1, "passed": 0, "failed": 0, "review": 1}},
    }
    write_reports(tmp_path, [result], metrics)
    packet = (tmp_path / "review_packet.md").read_text(encoding="utf-8")
    assert "evidence_conflicting_goal" in packet
    assert "尚未做决定" in packet
