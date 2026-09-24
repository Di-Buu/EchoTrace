"""Source-ID evaluation that never invokes the product or an LLM judge."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


def load_gold() -> dict[str, list[int]]:
    return json.loads((ROOT / "retrieval_gold.json").read_text(encoding="utf-8"))


def cited_ids(workflow: str, output: Any) -> list[str]:
    if not isinstance(output, dict):
        return []
    if workflow == "companion":
        return list(dict.fromkeys(str(value) for value in output.get("evidence_moment_ids", [])))
    evidence = output.get("evidence") or output.get("insight_evidence") or []
    return list(dict.fromkeys(str(item["moment_id"]) for item in evidence if item.get("moment_id")))


def retrieved_ids(execution: dict[str, Any]) -> tuple[str, list[str]]:
    trace = execution.get("retrieval_trace")
    if trace is None:
        return "unavailable_old_result", []
    if any(item.get("error") for item in trace):
        return "telemetry_error", []
    if not trace:
        return "not_invoked", []
    captured = [item.get("retrieved_moment_ids") for item in trace]
    if any(not isinstance(ids, list) for ids in captured):
        return "telemetry_missing_ids", []
    return "captured", list(dict.fromkeys(str(value) for ids in captured for value in ids))


def score_evidence(case: dict[str, Any], result: dict[str, Any], gold: dict[str, list[int]]) -> dict[str, Any]:
    execution = result.get("execution") or {}
    seeded = execution.get("seeded_moment_ids") or []
    indices = gold[case["case_id"]]
    expected = [str(seeded[index]) for index in indices if index < len(seeded)]
    status, retrieved = retrieved_ids(execution)
    # Companion's newly saved user question is not historical context consumed by its answer.
    output = execution.get("output") or {}
    if case["workflow"] == "companion" and isinstance(output, dict):
        retrieved = [value for value in retrieved if value != str(output.get("moment_id"))]
    cited = cited_ids(case["workflow"], output)
    expected_set = set(expected)
    retrieved_hits = expected_set.intersection(retrieved)
    cited_hits = expected_set.intersection(cited)
    return {
        "case_id": case["case_id"],
        "retrieval_status": status,
        "expected_moment_ids": expected,
        "retrieved_moment_ids": retrieved if status == "captured" else None,
        "cited_moment_ids": cited,
        "retrieval_recall_at_k": (
            len(retrieved_hits) / len(expected) if expected and status == "captured"
            else 0.0 if expected and status == "not_invoked"
            else None
        ),
        "retrieval_precision_at_k": (
            len(retrieved_hits) / len(retrieved) if retrieved and status == "captured" else None
        ),
        "citation_recall": len(cited_hits) / len(expected) if expected else None,
        "citation_precision": len(cited_hits) / len(cited) if cited else None,
        "temporal_stages_covered": (
            expected_set.issubset(retrieved_hits)
            if "temporal_relation" in case["category"] and expected and status == "captured"
            else None
        ),
        "unexpected_citations": sorted(set(cited) - expected_set),
    }


def summarize_evidence(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    gold = load_gold()
    by_id = {case["case_id"]: case for case in cases}
    scored = [score_evidence(by_id[item["case_id"]], item, gold) for item in results]
    retrieval_scored = [item for item in scored if item["retrieval_recall_at_k"] is not None]
    citation_scored = [item for item in scored if item["citation_recall"] is not None]
    temporal_scored = [item for item in scored if item["temporal_stages_covered"] is not None]
    return {
        "cases": scored,
        "retrieval_scored": len(retrieval_scored),
        "retrieval_missing_trace": sum(
            item["retrieval_status"] in {"unavailable_old_result", "telemetry_error", "telemetry_missing_ids"}
            for item in scored
        ),
        "mean_retrieval_recall_at_k": (
            sum(item["retrieval_recall_at_k"] for item in retrieval_scored) / len(retrieval_scored)
            if retrieval_scored else None
        ),
        "citation_scored": len(citation_scored),
        "mean_citation_recall": (
            sum(item["citation_recall"] for item in citation_scored) / len(citation_scored)
            if citation_scored else None
        ),
        "temporal_stages_scored": len(temporal_scored),
        "temporal_all_stages_covered": sum(item["temporal_stages_covered"] for item in temporal_scored),
    }
