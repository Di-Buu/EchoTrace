import evaluation.isolation as isolation
import pytest
from evaluation.evidence_metrics import load_gold, score_evidence
from evaluation.finalize_core import finalize
from evaluation.isolation import PAIRS, IsolationError, contains_foreign_data, prepare_pair
from evaluation.run_eval import core_case_ids, core_product_metrics, load_cases


def test_core_suite_is_fixed_and_reuses_existing_scenarios() -> None:
    ids = core_case_ids()
    assert len(ids) == 24
    assert len(set(ids)) == len(ids)
    assert set(ids) <= {case["case_id"] for case in load_cases()}
    assert len(PAIRS) == 5


def test_core_metrics_do_not_invent_unreviewed_semantic_scores() -> None:
    evidence = {
        "cases": [{
            "retrieval_recall_at_k": 0.5,
            "expected_moment_ids": ["a", "b"],
            "retrieved_moment_ids": ["a"],
        }],
        "retrieval_missing_trace": 1,
    }
    metrics = core_product_metrics([], evidence, [])
    assert metrics["historical_memory_recall_rate"]["value"] == 0.5
    assert metrics["insight_evidence_rate"]["value"] is None
    assert metrics["over_inference_rate"]["value"] is None
    assert metrics["cross_user_leakage_rate"]["value"] is None


def test_recall_total_is_withheld_when_positive_case_lacks_top_k() -> None:
    evidence = {
        "cases": [
            {"retrieval_recall_at_k": 0.5, "expected_moment_ids": ["a", "b"], "retrieved_moment_ids": ["a"]},
            {"retrieval_recall_at_k": None, "expected_moment_ids": ["c"], "retrieved_moment_ids": None},
        ],
        "retrieval_missing_trace": 1,
    }
    metric = core_product_metrics([], evidence, [])["historical_memory_recall_rate"]
    assert metric["value"] is None
    assert metric["observed_partial_rate"] == 0.5
    assert metric["missing_trace_cases"] == 1


def test_retrieval_not_invoked_counts_as_miss_not_missing_telemetry() -> None:
    case = next(case for case in load_cases() if case["case_id"] == "memory_stable_interest")
    result = {"execution": {
        "seeded_moment_ids": ["moment-1"], "retrieval_trace": [], "output": {},
    }}
    score = score_evidence(case, result, load_gold())
    assert score["retrieval_status"] == "not_invoked"
    assert score["retrieval_recall_at_k"] == 0


def test_foreign_id_or_marker_counts_as_leak() -> None:
    assert contains_foreign_data({"evidence": ["a-id"]}, {"a-id"}, "蓝色海鸥")
    assert contains_foreign_data({"body": "你提到蓝色海鸥"}, {"a-id"}, "蓝色海鸥")
    assert not contains_foreign_data({"body": "只见 B 的记录"}, {"a-id"}, "蓝色海鸥")


@pytest.mark.asyncio
async def test_same_account_is_rejected_before_data_reset() -> None:
    class FakeClient:
        user_id = "same-user"
        email = "test@example.com"

        async def login(self) -> None:
            pass

    with pytest.raises(IsolationError, match="两个不同"):
        await prepare_pair(FakeClient(), FakeClient())


def test_human_finalization_refuses_missing_decisions(tmp_path) -> None:
    (tmp_path / "metrics.json").write_text(
        '{"baseline":{"git":{"commit":"test"}},"core_product_metrics":{'
        '"insight_evidence_rate":{"generated_insights":1},'
        '"over_inference_rate":{},"historical_memory_recall_rate":{"value":null},'
        '"cross_user_leakage_rate":{"value":null}}}', encoding="utf-8"
    )
    (tmp_path / "core_review.csv").write_text(
        "case_id,grounded,over_inference,temporal_correct,review_notes\ncase-1,,,,\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="grounded"):
        finalize(tmp_path)


def test_human_finalization_calculates_only_completed_reviews(tmp_path) -> None:
    (tmp_path / "metrics.json").write_text(
        '{"baseline":{"git":{"commit":"test"}},"core_product_metrics":{'
        '"insight_evidence_rate":{"generated_insights":2},'
        '"over_inference_rate":{},"historical_memory_recall_rate":{"value":0.5},'
        '"cross_user_leakage_rate":{"value":0.0}}}', encoding="utf-8"
    )
    (tmp_path / "core_review.csv").write_text(
        "case_id,grounded,over_inference,temporal_correct,review_notes\n"
        "one,yes,no,yes,\n"
        "two,no,yes,no,\n", encoding="utf-8"
    )
    metrics = finalize(tmp_path)
    assert metrics["core_product_metrics"]["insight_evidence_rate"]["value"] == 0.5
    assert metrics["core_product_metrics"]["over_inference_rate"]["value"] == 0.5
    assert metrics["human_adjudication"]["temporal_correct"] == 1


def test_partial_insight_report_does_not_claim_full_recall_or_isolation(tmp_path) -> None:
    (tmp_path / "metrics.json").write_text(
        '{"baseline":{"git":{"commit":"test"}},"selection":{"complete_core":false},'
        '"core_product_metrics":{"insight_evidence_rate":{"generated_insights":1},'
        '"over_inference_rate":{},"historical_memory_recall_rate":{"value":1.0},'
        '"cross_user_leakage_rate":{"value":0.0}}}', encoding="utf-8"
    )
    (tmp_path / "core_review.csv").write_text(
        "case_id,grounded,over_inference,temporal_correct,review_notes\n"
        "one,yes,no,yes,\n", encoding="utf-8"
    )
    (tmp_path / "raw_results.jsonl").write_text(
        '{"case_id":"one","workflow":"insight","execution":{"http_status":200}}\n'
        '{"case_id":"two","workflow":"insight","execution":{"http_status":422}}\n',
        encoding="utf-8",
    )
    metrics = finalize(tmp_path)
    report = (tmp_path / "final_report.md").read_text(encoding="utf-8")
    assert metrics["insight_response_coverage"]["not_generated"] == 1
    assert "洞察有据率" in report
    assert "历史记忆召回率" not in report
    assert "跨用户串数据率" not in report


@pytest.mark.asyncio
async def test_two_isolation_workflows_dry_run_without_external_calls(monkeypatch) -> None:
    class Response:
        status_code = 200

        def json(self):
            return []

    class FakeHttp:
        async def post(self, *args, **kwargs):
            return Response()

        async def get(self, *args, **kwargs):
            return Response()

    class FakeClient:
        supabase_url = "https://test.invalid"
        anon_key = "test"
        token = "test"
        http = FakeHttp()

        def __init__(self, label):
            self.label = label
            self.user_id = label

        async def clear_data(self):
            pass

        async def seed_history(self, case_id, history):
            return [f"{self.label}-moment"]

        async def api(self, method, path, body=None):
            if path.endswith("/process"):
                return 200, {}
            if path == "/moments":
                return 200, [{"id": "b-moment"}]
            if path in {"/chat", "/insights/query"}:
                return 200, {"evidence": [{"moment_id": "b-moment"}]}
            return 404, {}

        async def retrieval_trace(self):
            return [{"retrieved_moment_ids": ["b-moment"]}]

    monkeypatch.setattr(isolation, "PAIRS", isolation.PAIRS[:2])
    results = await isolation.run_isolation_pairs(FakeClient("a"), FakeClient("b"))
    assert [item["status"] for item in results] == ["pass", "pass"]
    assert [item["workflow"] for item in results] == ["companion", "insight"]
