"""Finalize two semantic product metrics after human review, without model calls."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path


def decision(value: str, field: str, case_id: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"yes", "no"}:
        raise ValueError(f"{case_id} 的 {field} 必须填 yes 或 no")
    return normalized == "yes"


def finalize(run_dir: Path) -> dict:
    metrics_path = run_dir / "metrics.json"
    review_path = run_dir / "core_review.csv"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    product = metrics["core_product_metrics"]
    with review_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("本次没有可人工复核的洞察，不能生成两个语义指标")
    expected_count = product["insight_evidence_rate"]["generated_insights"]
    if len(rows) != expected_count:
        raise ValueError(f"人工复核表应有 {expected_count} 条，实际 {len(rows)} 条；不得删除难判 Case")
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("core_review.csv 中存在重复 case_id")
    grounded = sum(decision(row["grounded"], "grounded", row["case_id"]) for row in rows)
    over = sum(decision(row["over_inference"], "over_inference", row["case_id"]) for row in rows)
    temporal = [
        decision(row["temporal_correct"], "temporal_correct", row["case_id"])
        for row in rows if row["temporal_correct"].strip()
    ]
    product["insight_evidence_rate"].update({
        "value": grounded / len(rows), "human_supported": grounded,
        "human_reviewed": len(rows), "pending_semantic_review": 0,
    })
    product["over_inference_rate"].update({
        "value": over / len(rows), "human_over_inferred": over,
        "human_reviewed": len(rows), "pending_semantic_review": 0,
    })
    metrics["human_adjudication"] = {
        "completed_at": datetime.now(UTC).isoformat(),
        "source": "core_review.csv",
        "temporal_correct": sum(temporal),
        "temporal_reviewed": len(temporal),
        "note": "时间判断是洞察内部检查，不作为第五个作品集主指标。",
    }
    (run_dir / "final_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    labels = (
        ("历史记忆召回率", "historical_memory_recall_rate"),
        ("洞察有据率", "insight_evidence_rate"),
        ("过度推断率", "over_inference_rate"),
        ("跨用户串数据率", "cross_user_leakage_rate"),
    )
    lines = ["# EchoTrace 核心产品风险最终结果", "", f"评测版本：{metrics['baseline']['git']['commit']}", ""]
    for label, key in labels:
        item = product[key]
        value = "证据不足，暂不报告" if item["value"] is None else f"{item['value']:.1%}"
        lines.append(f"- {label}：{value}")
    lines.extend([
        "", f"- 人工审读洞察：{len(rows)} 条",
        f"- 时间判断内部检查：{sum(temporal)}/{len(temporal)}（若未填则不统计）",
        "", "旧版本结果、缺检索 Top-K 的 Case 和未完成的 A/B 对照均未冒充本次成绩。",
    ])
    (run_dir / "final_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="离线汇总已人工复核的核心产品指标")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    try:
        result = finalize(args.run_dir)
    except (OSError, KeyError, ValueError) as exc:
        parser.exit(2, f"无法完成最终汇总：{exc}\n")
    print(json.dumps(result["core_product_metrics"], ensure_ascii=False, indent=2))
