"""Readable, model-free review packet for saved product evaluation results."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _quote(value: Any) -> list[str]:
    return [f"> {line}" for line in str(value or "（无）").splitlines() or ["（无）"]]


def _answer(workflow: str, output: Any) -> str:
    if not isinstance(output, dict):
        return str(output or "")
    if workflow == "companion":
        return str((output.get("message") or {}).get("content") or "")
    return "\n".join(
        f"{label}：{output[field]}"
        for field, label in (("title", "标题"), ("body", "正文"), ("limitation", "局限"))
        if output.get(field)
    )


def render_review_packet(
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
    baseline: dict[str, Any],
    *,
    source_warning: str | None = None,
) -> str:
    case_by_id = {case["case_id"]: case for case in cases}
    pending = [row for row in results if row.get("verdict") == "review" or row.get("manual_review")]
    lines = [
        "# EchoTrace 待复核清单",
        "",
        f"共 {len(pending)} 条。此文件只排版已有输出，没有调用模型，也不自动把 review 改成 pass。",
        f"API 运行指纹：`{baseline.get('api_runtime_fingerprint') or '未记录'}`",
        f"Prompt 版本：`{json.dumps(baseline.get('prompt_versions', {}), ensure_ascii=False)}`",
        "",
        "请根据原始 Moment 判断个人事实、时间、反例和推断强度。Judge 意见只是参考；",
        "若来源版本未核实，不能把该输出算入当前版本成绩。",
        "",
    ]
    if source_warning:
        lines.extend([f"> 版本来源警告：{source_warning}", ""])
    for row in pending:
        case = case_by_id.get(row["case_id"])
        if case is None:
            continue
        execution = row.get("execution") or {}
        output = execution.get("output") or {}
        seeded = execution.get("seeded_moment_ids") or []
        source_by_id = {
            str(seeded[index]): (index + 1, item)
            for index, item in enumerate(case["history"])
            if index < len(seeded)
        }
        evidence = output.get("evidence") or output.get("insight_evidence") or []
        stances: dict[str, set[str]] = {}
        for item in evidence:
            moment_id = str(item.get("moment_id") or "")
            if moment_id:
                stances.setdefault(moment_id, set()).add(str(item.get("stance") or "support"))
        judge = (row.get("llm_judge") or {}).get("result") or {}
        failed_checks = [
            item["name"]
            for item in (row.get("rule_judge") or {}).get("checks", [])
            if not item.get("passed")
        ]
        lines.extend(
            [
                f"## {row['case_id']} — {case['description']}",
                "",
                f"- 类别：{', '.join(case['category'])}",
                f"- 自动状态：{row.get('verdict')}；Judge：{judge.get('verdict', '未运行')}",
                f"- 不通过的字面/规则检查：{', '.join(failed_checks) if failed_checks else '无'}",
                f"- 历史结果来源：{row.get('source_run') or '本次运行'}",
                f"- 预期：`{json.dumps(case['expected'], ensure_ascii=False)}`",
                "",
                "问题：",
                *_quote(case["request"]),
                "",
                "原始 Moment：",
                "",
            ]
        )
        for index, item in enumerate(case["history"], start=1):
            moment_id = str(seeded[index - 1]) if index <= len(seeded) else "未记录"
            lines.extend([f"{index}. {item['at']} · `{moment_id}`", *_quote(item["text"])])
        lines.extend(["", "AI 回答：", *_quote(_answer(case["workflow"], output)), ""])
        if stances:
            lines.append("引用的 Moment：")
            for moment_id, roles in stances.items():
                source = source_by_id.get(moment_id)
                label = f"#{source[0]}" if source else "非本 Case 原始记录"
                lines.append(f"- {label} · {', '.join(sorted(roles))} · `{moment_id}`")
            lines.append("")
        lines.extend(["Judge 理由：", *_quote(judge.get("reason") or "未运行"), ""])
        if row.get("provenance_warning"):
            lines.extend(["来源提醒：", *_quote(row["provenance_warning"]), ""])
        lines.extend(
            [
                "人工结论：□ 可接受  □ 需修改  □ 证据不足  □ 暂无法判断",
                "",
                "理由与需要追踪的问题：",
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="从已有结果生成可读的人工复核清单；不调用模型")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from evaluation.run_eval import load_cases

    run_dir = args.run_dir
    results = [
        json.loads(line)
        for line in (run_dir / "raw_results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    baseline = json.loads((run_dir / "baseline.json").read_text(encoding="utf-8"))
    audit_path = Path(__file__).resolve().parent / "runtime_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {}
    source_warning = (audit.get("unverified_runs") or {}).get(run_dir.name)
    target = run_dir / "review_packet.md"
    target.write_text(
        render_review_packet(load_cases(), results, baseline, source_warning=source_warning),
        encoding="utf-8",
    )
    print(f"已整理待复核清单：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
