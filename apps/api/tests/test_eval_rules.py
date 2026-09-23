import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from evaluation.run_eval import rule_judge  # noqa: E402


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
