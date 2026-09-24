"""Workflow-scoped fingerprints and conservative reuse of saved local evaluations."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_ROOT = Path(__file__).resolve().parent / "results"
RUNTIME_AUDIT = Path(__file__).resolve().parent / "runtime_audit.json"

COMMON_SOURCES = (
    "apps/api/app/clients/ai.py",
    "apps/api/app/clients/supabase.py",
    "apps/api/app/domain.py",
    "apps/api/app/services/moment_index.py",
    "apps/api/app/services/retrieval.py",
    "apps/api/app/services/evidence_scope.py",
    "supabase/migrations/202609230001_evidence_preserving_weekly_memory.sql",
)
WORKFLOW_SOURCES = {
    "companion": ("apps/api/app/services/companion.py", "apps/api/app/routers/chat.py"),
    "insight": ("apps/api/app/services/insights.py", "apps/api/app/routers/insights.py"),
    "memory_retrieval": ("apps/api/app/services/insights.py", "apps/api/app/routers/insights.py"),
}
WORKFLOW_PROMPTS = {
    "companion": ("companion",),
    "insight": ("orchestrator", "temporal", "pattern", "verifier", "synthesizer", "synthesis_audit"),
    "memory_retrieval": ("orchestrator", "temporal", "pattern", "verifier", "synthesizer", "synthesis_audit"),
}
PROMPT_ATTRIBUTES = {
    "companion": "COMPANION_SYSTEM",
    "orchestrator": "ORCHESTRATOR_SYSTEM",
    "temporal": "TEMPORAL_SYSTEM",
    "pattern": "PATTERN_SYSTEM",
    "verifier": "VERIFIER_SYSTEM",
    "synthesizer": "SYNTHESIZER_SYSTEM",
    "synthesis_audit": "SYNTHESIS_AUDIT_SYSTEM",
}
CONFIG_KEYS = (
    "chat_model",
    "embedding_model",
    "embedding_dimension",
    "execution_profile",
    "insight_thinking",
    "insight_timeout_seconds",
)
RETRIEVAL_TRACE_LINE = '                "retrieved_moment_ids": [str(item.moment_id) for item in evidence],'


def relevant_sources(workflow: str) -> tuple[str, ...]:
    return (*COMMON_SOURCES, *WORKFLOW_SOURCES[workflow])


def behavior_signature(
    case: dict[str, Any], baseline: dict[str, Any], *, include_synthesis_audit: bool = True
) -> str:
    sources = {
        path: hashlib.sha256(_behavior_bytes(path, (PROJECT_ROOT / path).read_bytes())).hexdigest()
        for path in relevant_sources(case["workflow"])
    }
    prompt_versions = baseline["prompt_versions"]
    prompt_source = ast.parse((PROJECT_ROOT / "apps/api/app/prompts.py").read_text(encoding="utf-8"))
    prompt_text = {
        target.id: node.value.value
        for node in prompt_source.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    selected_prompts = WORKFLOW_PROMPTS[case["workflow"]]
    if not include_synthesis_audit:
        selected_prompts = tuple(name for name in selected_prompts if name != "synthesis_audit")
    payload = {
        "case": case,
        "sources": sources,
        "prompts": {
            name: {
                "version": prompt_versions[name],
                "text_hash": hashlib.sha256(prompt_text[PROMPT_ATTRIBUTES[name]].encode()).hexdigest(),
            }
            for name in selected_prompts
        },
        "config": {key: baseline[key] for key in CONFIG_KEYS},
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _behavior_bytes(path: str, content: bytes) -> bytes:
    if path == "apps/api/app/services/retrieval.py":
        return content.replace((RETRIEVAL_TRACE_LINE + "\n").encode(), b"")
    return content


def _changed_paths_since(commit: str, paths: tuple[str, ...]) -> bool:
    if "apps/api/app/services/retrieval.py" in paths:
        path = "apps/api/app/services/retrieval.py"
        previous = subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
        )
        if previous.returncode or _behavior_bytes(path, previous.stdout) != _behavior_bytes(
            path, (PROJECT_ROOT / path).read_bytes()
        ):
            return True
        paths = tuple(item for item in paths if item != path)
    result = subprocess.run(
        ["git", "diff", "--quiet", commit, "--", *paths],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
    )
    return result.returncode != 0


def _legacy_compatible(case: dict[str, Any], baseline: dict[str, Any], saved: dict[str, Any]) -> bool:
    if any(baseline.get(key) != saved.get(key) for key in CONFIG_KEYS):
        return False
    current_prompts = baseline["prompt_versions"]
    old_prompts = saved.get("prompt_versions") or {}
    if any(current_prompts[name] != old_prompts.get(name) for name in WORKFLOW_PROMPTS[case["workflow"]]):
        return False
    old_git = saved.get("git") or {}
    commit = old_git.get("commit")
    # Historical runs recorded a dirty flag but not per-file dirty paths. Reuse is
    # explicitly marked provisional, never presented as a clean current baseline.
    return bool(commit and commit != "unknown") and not _changed_paths_since(
        commit, (*relevant_sources(case["workflow"]), "evaluation/scenarios.jsonl")
    )


def _unverified_runs() -> set[str]:
    try:
        return set(json.loads(RUNTIME_AUDIT.read_text(encoding="utf-8"))["unverified_runs"])
    except (OSError, ValueError, KeyError):
        return set()


def _trusted_cache(item: dict[str, Any], baseline: dict[str, Any]) -> bool:
    source = item.get("source_run")
    if source:
        return Path(source).name not in _unverified_runs()
    return bool(
        baseline.get("api_runtime_fingerprint")
        and item.get("api_runtime_fingerprint") == baseline["api_runtime_fingerprint"]
    )


def find_legacy_result(case: dict[str, Any], baseline: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    for run_dir in sorted(RESULTS_ROOT.glob("run-*/"), reverse=True):
        if run_dir.name in _unverified_runs():
            continue
        baseline_path = run_dir / "baseline.json"
        results_path = run_dir / "raw_results.jsonl"
        if not baseline_path.exists() or not results_path.exists():
            continue
        try:
            saved_baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            if not _legacy_compatible(case, baseline, saved_baseline):
                continue
            for line in results_path.read_text(encoding="utf-8").splitlines():
                item = json.loads(line)
                if (
                    item.get("case_id") == case["case_id"]
                    and item.get("workflow") == case["workflow"]
                    and item.get("verdict") in {"pass", "review"}
                    and item.get("execution")
                    and not item.get("error")
                    and (not item.get("source_run") or _trusted_cache(item, baseline))
                ):
                    return item, str(run_dir)
        except (OSError, ValueError, KeyError):
            continue
    return None


def cache_status(
    case: dict[str, Any], baseline: dict[str, Any], cache_dir: Path, *, force: bool = False
) -> dict[str, Any]:
    fingerprint = behavior_signature(case, baseline)
    cache_path = cache_dir / f"{case['case_id']}-{fingerprint[:12]}.json"
    if force:
        return {"status": "run", "reason": "显式 --force", "fingerprint": fingerprint, "path": cache_path}
    if cache_path.exists():
        try:
            item = json.loads(cache_path.read_text(encoding="utf-8"))
            # An exact workflow signature already covers its code, prompts, model
            # configuration and case data. A different whole-API fingerprint can
            # result from an unrelated workflow change and must not waste calls.
            trusted_source = not item.get("source_run") or _trusted_cache(item, baseline)
            if (
                item.get("execution")
                and not item.get("error")
                and item.get("fingerprint") == fingerprint
                and trusted_source
            ):
                reason = "工作流行为指纹一致"
                if item.get("api_runtime_fingerprint") != baseline.get("api_runtime_fingerprint"):
                    reason += "；API 全局指纹变化但本工作流相关行为未变"
                return {
                    "status": "reuse", "reason": reason,
                    "fingerprint": fingerprint, "path": cache_path, "item": item,
                    "missing_retrieval_trace": item["execution"].get("retrieval_trace") is None,
                }
        except (OSError, ValueError):
            pass
    if case["workflow"] in {"insight", "memory_retrieval"}:
        # Earlier runs of this exact API included final-text auditing before
        # its prompt was added to the evaluator's signature. Reindex only when
        # the entire loaded API runtime is identical; never bridge code changes.
        old_fingerprint = behavior_signature(case, baseline, include_synthesis_audit=False)
        old_path = cache_dir / f"{case['case_id']}-{old_fingerprint[:12]}.json"
        if old_path.exists():
            try:
                item = json.loads(old_path.read_text(encoding="utf-8"))
                if (
                    item.get("fingerprint") == old_fingerprint
                    and item.get("api_runtime_fingerprint") == baseline.get("api_runtime_fingerprint")
                    and item.get("cache_origin") == "new_run"
                    and item.get("execution")
                    and not item.get("error")
                ):
                    return {
                        "status": "reuse",
                        "reason": "API 运行指纹完全一致；仅评测签名补入成稿审计 Prompt",
                        "fingerprint": fingerprint,
                        "path": cache_path,
                        "item": item,
                        "missing_retrieval_trace": item["execution"].get("retrieval_trace") is None,
                    }
            except (OSError, ValueError):
                pass
    legacy = find_legacy_result(case, baseline)
    if legacy:
        item, source = legacy
        return {
            "status": "reuse_historical",
            "reason": "模型/相关 Prompt 版本相同，相关代码自历史提交后未变；历史运行含未定位的未提交改动，需保留来源说明",
            "fingerprint": fingerprint,
            "path": cache_path,
            "item": item,
            "source": source,
            "missing_retrieval_trace": item["execution"].get("retrieval_trace") is None,
        }
    return {
        "status": "run",
        "reason": "没有与当前工作流行为兼容的已完成结果",
        "fingerprint": fingerprint,
        "path": cache_path,
    }
