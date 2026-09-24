"""Optional Ragas cross-check over saved outputs; never reruns EchoTrace workflows."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from evaluation.evidence_metrics import load_gold, retrieved_ids

ROOT = Path(__file__).resolve().parent
FAITHFULNESS_CASES = {
    "memory_transient_mood",
    "memory_update_current",
    "personal_fact_supported_running",
    "personal_fact_latest_state",
    "temporal_career_change",
    "temporal_reading_habit",
    "temporal_city_preference",
    "evidence_social_counterexample",
    "evidence_conflicting_goal",
    "evidence_reference_attribution",
    "overinfer_correlation_cause",
    "regression_reasoning_counterexample",
}


def _cached_path(payload: dict[str, Any]) -> Path:
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return ROOT / "results" / "ragas-cache" / f"{digest}.json"


def _answer_text(workflow: str, output: Any) -> str:
    from evaluation.run_eval import candidate_text

    return candidate_text(workflow, output)


async def score_saved_run(run_dir: Path, *, faithfulness: bool = True) -> dict[str, Any]:
    try:
        from ragas import SingleTurnSample
        from ragas.metrics import IDBasedContextPrecision, IDBasedContextRecall
    except ImportError as exc:
        raise RuntimeError(
            "Ragas 未安装；先执行 apps/api/.venv/Scripts/python.exe -m pip install -r evaluation/requirements-ragas.txt"
        ) from exc

    from evaluation.run_eval import load_cases, load_environment

    cases = {item["case_id"]: item for item in load_cases()}
    gold = load_gold()
    rows = [
        json.loads(line)
        for line in (run_dir / "raw_results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    llm = None
    model_name = None
    if faithfulness:
        from openai import AsyncOpenAI
        from ragas.llms import llm_factory
        from ragas.metrics.collections import Faithfulness

        env = load_environment()
        model_name = env.get("CHAT_MODEL", "")
        api_key = env.get("DASHSCOPE_API_KEY", "")
        base_url = env.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        if not model_name or not api_key:
            raise RuntimeError("Faithfulness 需要已配置 CHAT_MODEL 与 DASHSCOPE_API_KEY")
        llm = Faithfulness(llm=llm_factory(model_name, client=AsyncOpenAI(api_key=api_key, base_url=base_url)))

    output_rows: list[dict[str, Any]] = []
    cache_dir = ROOT / "results" / "ragas-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        case = cases[case_id]
        execution = row.get("execution") or {}
        status, ids = retrieved_ids(execution)
        if status != "captured":
            output_rows.append({"case_id": case_id, "status": "skipped", "reason": status})
            continue
        seeded = execution.get("seeded_moment_ids") or []
        output = execution.get("output") or {}
        if case["workflow"] == "companion" and isinstance(output, dict):
            ids = [value for value in ids if value != str(output.get("moment_id"))]
        references = [str(seeded[index]) for index in gold[case_id] if index < len(seeded)]
        payload = {
            "case_id": case_id,
            "retrieved_ids": ids,
            "references": references,
            "response": _answer_text(case["workflow"], output),
            "model": model_name if faithfulness and case_id in FAITHFULNESS_CASES else None,
            "ragas_version": "0.4.3",
        }
        cache_path = _cached_path(payload)
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if not cached.get("faithfulness_error"):
                output_rows.append({**cached, "cached": True})
                continue
        result: dict[str, Any] = {"case_id": case_id, "status": "scored", "cached": False}
        if references:
            sample = SingleTurnSample(retrieved_context_ids=ids, reference_context_ids=references)
            result["id_precision"] = float(await IDBasedContextPrecision().single_turn_ascore(sample))
            result["id_recall"] = float(await IDBasedContextRecall().single_turn_ascore(sample))
        else:
            result["id_precision"] = None
            result["id_recall"] = None
        if llm and case_id in FAITHFULNESS_CASES and payload["response"] and ids:
            content = {str(seeded[index]): item["text"] for index, item in enumerate(case["history"])}
            contexts = [content[value] for value in ids if value in content]
            if contexts:
                try:
                    score = await llm.ascore(
                        user_input=case["request"],
                        response=payload["response"],
                        retrieved_contexts=contexts,
                    )
                    result["faithfulness"] = float(score.value)
                except Exception as exc:  # noqa: BLE001 - record external judge failure per Case
                    result["faithfulness_error"] = f"{type(exc).__name__}: {exc}"
        if not result.get("faithfulness_error"):
            cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        output_rows.append(result)

    (run_dir / "ragas_results.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in output_rows), encoding="utf-8"
    )
    summary = {
        "source_run": str(run_dir),
        "id_scored": sum(item.get("id_recall") is not None for item in output_rows),
        "faithfulness_scored": sum(item.get("faithfulness") is not None for item in output_rows),
        "skipped_missing_trace": sum(item.get("status") == "skipped" for item in output_rows),
        "judge_errors": sum(bool(item.get("faithfulness_error")) for item in output_rows),
        "note": "Ragas 是辅助交叉检查；旧输出缺少检索 Top-K 时不生成替代分数。",
    }
    (run_dir / "ragas_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary
