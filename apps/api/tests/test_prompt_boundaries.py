import json
from pathlib import Path

from app.prompts import (
    PROMPT_VERSIONS,
    SYNTHESIZER_SYSTEM,
    VERIFIER_SYSTEM,
)


def test_prompt_manifest_matches_runtime_versions() -> None:
    manifest = json.loads(
        (Path(__file__).resolve().parents[3] / "evaluation" / "prompt_versions.json").read_text(
            encoding="utf-8"
        )
    )
    assert {name: item["current"] for name, item in manifest.items()} == PROMPT_VERSIONS


def test_unresolved_choice_does_not_imply_repeated_loop() -> None:
    assert "尚未决定" in VERIFIER_SYSTEM
    assert "循环" in VERIFIER_SYSTEM
    assert "REJECT" in VERIFIER_SYSTEM
    assert "没有已验证的多次往返" in SYNTHESIZER_SYSTEM
    assert "首末日期不等于该状态持续的时长" in SYNTHESIZER_SYSTEM
