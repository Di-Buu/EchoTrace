"""Offline paired Prompt comparison using saved outputs, not new model calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

from evaluation.run_eval import candidate_text


def read_run(path: Path) -> tuple[dict, dict[str, dict]]:
    baseline = json.loads((path / "baseline.json").read_text(encoding="utf-8"))
    results = {
        item["case_id"]: item
        for line in (path / "raw_results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
        for item in [json.loads(line)]
    }
    return baseline, results


def compare(old_dir: Path, new_dir: Path, selected: set[str] | None = None) -> dict:
    old_base, old = read_run(old_dir)
    new_base, new = read_run(new_dir)
    paired = sorted(old.keys() & new.keys())
    if selected:
        paired = [case_id for case_id in paired if case_id in selected]
    changed_prompts = {
        name: {"old": version, "new": new_base["prompt_versions"].get(name)}
        for name, version in old_base["prompt_versions"].items()
        if version != new_base["prompt_versions"].get(name)
    }
    confounders = [
        key for key in ("chat_model", "embedding_model", "execution_profile", "insight_thinking")
        if old_base.get(key) != new_base.get(key)
    ]
    cases = []
    for case_id in paired:
        before, after = old[case_id], new[case_id]
        workflow = after["workflow"]
        cases.append(
            {
                "case_id": case_id,
                "workflow": workflow,
                "old_verdict": before["verdict"],
                "new_verdict": after["verdict"],
                "old_text": candidate_text(workflow, (before.get("execution") or {}).get("output")),
                "new_text": candidate_text(workflow, (after.get("execution") or {}).get("output")),
                "old_judge": (before.get("llm_judge") or {}).get("result"),
                "new_judge": (after.get("llm_judge") or {}).get("result"),
                "human_review_required": True,
            }
        )
    return {
        "old_run": str(old_dir),
        "new_run": str(new_dir),
        "changed_prompts": changed_prompts,
        "config_confounders": confounders,
        "paired_cases": len(cases),
        "cases": cases,
        "interpretation": (
            "本文件仅展示配对证据；review 和 Judge pass 不自动证明 Prompt 改善。"
            "需要对照原始 Moment 人工判断，且不同提交的其他改动需单独披露。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="离线对照已保存的两个 Prompt 运行")
    parser.add_argument("old_run", type=Path)
    parser.add_argument("new_run", type=Path)
    parser.add_argument("--case-id", action="append")
    args = parser.parse_args()
    result = compare(args.old_run, args.new_run, set(args.case_id) if args.case_id else None)
    target = args.new_run / "prompt_comparison.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已配对 {result['paired_cases']} 条；无模型调用。结果：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
