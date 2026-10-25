import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

CAPTURE_PATH = Path(__file__).resolve().parents[3] / "tools/capture_synthetic_audit.py"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_owner", [True, False])
async def test_synthetic_capture_never_records_another_user(tmp_path, same_owner):
    spec = importlib.util.spec_from_file_location("capture_test", CAPTURE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    output = SimpleNamespace(model_dump=lambda **_: {"supported": False, "reason": "合成审计原因"})

    class Engine:
        async def _audit_synthesis(self, *args, **kwargs):
            calls.append(args)
            return output

    destination = tmp_path / "capture.jsonl"
    module.install_capture(Engine, str(UUID(int=1)), destination)
    user = SimpleNamespace(id=UUID(int=1 if same_owner else 2))
    synthesis = SimpleNamespace(model_dump=lambda **_: {"body": "合成成稿"})
    result = await Engine()._audit_synthesis(
        user, "合成问题", [{"moment_id": UUID(int=3)}], [], synthesis, "test", attempt=1
    )
    assert result is output and len(calls) == 1
    assert destination.exists() == same_owner
    if same_owner:
        text = destination.read_text(encoding="utf-8")
        assert "合成成稿" in text and "合成审计原因" in text
        assert "合成问题" not in text
