"""Recalculate rule verdicts from a saved run without API or model calls."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from run_eval import (
    EvaluationError,
    build_metrics,
    load_cases,
    rule_judge,
    score_verdict,
    write_reports,
)

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="不调用模型，重新判定已保存的评测输出")
    parser.add_argument("source", type=Path, help="原始 run-时间 目录")
    args = parser.parse_args()
    source = args.source.resolve()
    raw_path = source / "raw_results.jsonl"
    baseline_path = source / "baseline.json"
    if not raw_path.is_file() or not baseline_path.is_file():
        raise EvaluationError(f"缺少原始结果或基线文件: {source}")

    cases = {item["case_id"]: item for item in load_cases()}
    results = []
    for raw in raw_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        case = cases.get(item["case_id"])
        if case is None:
            raise EvaluationError(f"当前测试集中缺少 Case: {item['case_id']}")
        execution = item.get("execution")
        rules = rule_judge(case, execution) if execution else {"passed": False, "checks": []}
        item["original_verdict"] = item["verdict"]
        item["rule_judge"] = rules
        item["verdict"] = score_verdict(
            case, execution, item.get("error"), rules, item.get("llm_judge")
        )
        item["rescored_from"] = str(source)
        results.append(item)

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["rescore_source"] = str(source)
    baseline["rescore_note"] = "仅重新运行规则；沿用原始模型输出与旧版 LLM Judge 判定"
    target = ROOT / "results" / f"rescore-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    target.mkdir(parents=True, exist_ok=False)
    metrics = build_metrics(results, baseline)
    write_reports(target, results, metrics)
    with (target / "report.md").open("a", encoding="utf-8") as report:
        report.write(
            "\n## 重新判定说明\n\n"
            f"- 原始运行：`{source}`\n"
            "- 只重算规则与汇总，没有重新运行产品 Workflow 或调用模型。\n"
            "- LLM Judge 结果沿用原始运行；裁判提示版本变化需要单独回归。\n"
            "- 这不是产品修复后的效果指标。\n"
        )
    print(json.dumps(metrics["overall"], ensure_ascii=False, indent=2))
    print(f"Report: {target / 'report.md'}")


if __name__ == "__main__":
    try:
        main()
    except EvaluationError as exc:
        parser_message = f"重算失败：{exc}"
        raise SystemExit(parser_message) from exc
