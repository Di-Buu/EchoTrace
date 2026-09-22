"""Small, real-call evaluation harness for model/embedding decisions.

Run from apps/api after configuring .env:
  .venv/Scripts/python ../../evaluation/run_evaluation.py --suite all
The report is written to evaluation/results/ and is intentionally git-ignored only if it contains real data.
"""

import argparse
import asyncio
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.clients.ai import AiResponseError, BailianClient
from app.config import get_settings
from app.domain import CuratorOutput, SpecialistOutput, VerifierOutput
from app.prompts import MEMORY_CURATOR_SYSTEM, PATTERN_SYSTEM, TEMPORAL_SYSTEM, VERIFIER_SYSTEM

ROOT = Path(__file__).resolve().parent


def cosine(a: list[float], b: list[float]) -> float:
    numerator = sum(x * y for x, y in zip(a, b, strict=True))
    denominator = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return numerator / denominator if denominator else 0


async def evaluate_memory(ai: BailianClient, cases: list[dict[str, Any]]) -> list[dict]:
    results = []
    for case in cases:
        output, meta = await ai.structured_chat(
            system=MEMORY_CURATOR_SYSTEM,
            user=json.dumps(
                {
                    "moment": {"id": case["id"], "content": case["moment"]},
                    "existing_memories": case.get("existing_memories", []),
                    "output_schema": {
                        "memories": [
                            {
                                "content": "string",
                                "memory_type": "event|view|interest|goal|decision|question|state",
                                "occurred_at": None,
                                "confidence": "0..1",
                                "operation": "create|update|conflict|skip",
                                "related_memory_id": "provided UUID or null",
                                "reasoning": "string",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
            schema=CuratorOutput,
            temperature=0,
        )
        kept = [item for item in output.memories if item.operation != "skip"]
        combined = " ".join(item.content for item in kept)
        passed = bool(kept) == case["should_remember"]
        expected_types = case.get("expected_types") or ([case["expected_type"]] if case.get("expected_type") else [])
        if expected_types and kept:
            passed = passed and kept[0].memory_type in expected_types
        if case.get("expected_operation") and kept:
            passed = passed and kept[0].operation == case["expected_operation"]
        if case.get("expected_related_memory_id") and kept:
            passed = passed and str(kept[0].related_memory_id) == case["expected_related_memory_id"]
        passed = passed and all(term in combined for term in case["required_terms"])
        passed = passed and not any(term in combined for term in case["forbidden_terms"])
        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "passed": passed,
                "output": output.model_dump(mode="json"),
                "meta": meta,
            }
        )
    return results


async def evaluate_retrieval(ai: BailianClient, cases: list[dict[str, Any]]) -> list[dict]:
    results = []
    for case in cases:
        query_vector, query_meta = await ai.embedding(case["query"])
        ranked = []
        for document in case["corpus"]:
            vector, _ = await ai.embedding(document["text"])
            ranked.append((document["id"], cosine(query_vector, vector)))
        ranked.sort(key=lambda item: item[1], reverse=True)
        relevant = set(case["relevant_ids"])
        first_rank = next((index + 1 for index, item in enumerate(ranked) if item[0] in relevant), None)
        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "passed": first_rank is not None and first_rank <= 3,
                "recall_at_3": int(any(item[0] in relevant for item in ranked[:3])),
                "mrr": 1 / first_rank if first_rank else 0,
                "ranking": ranked,
                "meta": query_meta,
            }
        )
    return results


async def evaluate_insight(ai: BailianClient, cases: list[dict[str, Any]]) -> list[dict]:
    results = []
    for case in cases:
        specialist_system = PATTERN_SYSTEM if case["specialist"] == "pattern" else TEMPORAL_SYSTEM
        candidate, meta = await ai.structured_chat(
            system=specialist_system,
            user=json.dumps({"question": case["question"], "evidence": case["evidence"]}, ensure_ascii=False),
            schema=SpecialistOutput,
            temperature=0,
        )
        verified, verify_meta = await ai.structured_chat(
            system=VERIFIER_SYSTEM,
            user=json.dumps(
                {
                    "question": case["question"],
                    "evidence": case["evidence"],
                    "candidate_claims": candidate.model_dump(mode="json")["claims"],
                },
                ensure_ascii=False,
            ),
            schema=VerifierOutput,
            temperature=0,
        )
        visible = [item for item in verified.claims if item.verification_status != "REJECT"]
        text = " ".join(item.claim for item in visible)
        allowed_ids = {item["moment_id"] for item in case["evidence"]}
        visible_source_ids = {str(source) for item in visible for source in item.source_moment_ids}
        # Source count is an insight-level requirement. A bounded fact may validly
        # cite one moment while the overall temporal/pattern conclusion spans many.
        enough_sources = not case.get("expect_visible", True) or (
            len(visible_source_ids) >= case["expected_min_sources"]
        )
        valid_sources = all(
            {str(source) for source in item.source_moment_ids}.issubset(allowed_ids) for item in visible
        )
        # Negative cases may safely return a bounded fact or an explicit limitation.
        # They fail only if an unsafe conclusion survives verification.
        visibility_ok = bool(visible) if case.get("expect_visible", True) else True
        safe_text = not any(term in text for term in case["forbidden_terms"])
        required_text = all(term in text for term in case.get("required_terms", []))
        grounded_claims = all(item.source_moment_ids for item in visible)
        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "specialist": case["specialist"],
                "passed": visibility_ok
                and enough_sources
                and valid_sources
                and safe_text
                and required_text
                and grounded_claims,
                "candidate": candidate.model_dump(mode="json"),
                "verified": verified.model_dump(mode="json"),
                "visible_source_count": len(visible_source_ids),
                "grounded_claims": grounded_claims and valid_sources,
                "meta": [meta, verify_meta],
            }
        )
    return results


async def evaluate_resilient(
    evaluator: Any,
    ai: BailianClient,
    cases: list[dict[str, Any]],
) -> list[dict]:
    """Keep a model-format failure in one case from discarding the full run."""
    results: list[dict] = []
    for case in cases:
        try:
            results.extend(await evaluator(ai, [case]))
        except AiResponseError as exc:
            results.append(
                {
                    "id": case["id"],
                    "category": case["category"],
                    "passed": False,
                    "error": str(exc),
                }
            )
    return results


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["all", "memory", "retrieval", "insight"], default="all")
    parser.add_argument("--case-id", action="append", help="Only run one or more exact case IDs")
    args = parser.parse_args()
    cases = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
    if args.case_id:
        selected = set(args.case_id)
        cases = {name: [case for case in values if case["id"] in selected] for name, values in cases.items()}
    ai = BailianClient(get_settings())
    report: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "chat_model": ai.settings.chat_model,
        "embedding_model": ai.settings.embedding_model,
    }
    if args.suite in {"all", "memory"}:
        report["memory"] = await evaluate_resilient(evaluate_memory, ai, cases["memory_cases"])
    if args.suite in {"all", "retrieval"}:
        report["retrieval"] = await evaluate_resilient(evaluate_retrieval, ai, cases["retrieval_cases"])
    if args.suite in {"all", "insight"}:
        report["insight"] = await evaluate_resilient(evaluate_insight, ai, cases["insight_cases"])
    report["summary"] = {
        name: {
            "passed": sum(item["passed"] for item in values),
            "total": len(values),
            "pass_rate": round(sum(item["passed"] for item in values) / len(values), 4) if values else 0,
        }
        for name, values in report.items()
        if isinstance(values, list)
    }
    if report.get("retrieval"):
        report["summary"]["retrieval"].update(
            {
                "recall_at_3": round(
                    sum(item.get("recall_at_3", 0) for item in report["retrieval"]) / len(report["retrieval"]), 4
                ),
                "mrr": round(
                    sum(item.get("mrr", 0) for item in report["retrieval"]) / len(report["retrieval"]), 4
                ),
            }
        )
    if report.get("insight"):
        visible_claims = [
            claim
            for item in report["insight"]
            for claim in item.get("verified", {}).get("claims", [])
            if claim["verification_status"] != "REJECT"
        ]
        grounded = sum(bool(claim["source_moment_ids"]) for claim in visible_claims)
        report["summary"]["insight"]["grounded_claim_rate"] = (
            round(grounded / len(visible_claims), 4) if visible_claims else 1.0
        )
    for name in ("memory", "retrieval", "insight"):
        if not report.get(name):
            continue
        categories = sorted({item["category"] for item in report[name]})
        report["summary"][name]["by_category"] = {
            category: {
                "passed": sum(item["passed"] for item in report[name] if item["category"] == category),
                "total": sum(1 for item in report[name] if item["category"] == category),
            }
            for category in categories
        }
    result_dir = ROOT / "results"
    result_dir.mkdir(exist_ok=True)
    output = result_dir / f"evaluation-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Report: {output}")


if __name__ == "__main__":
    asyncio.run(main())
