import pytest
from pydantic import ValidationError

from app.clients.ai import BailianClient
from app.config import Settings
from app.domain import CuratorOutput, MemoryCandidate


def test_json_fence_is_parsed() -> None:
    client = BailianClient(Settings())
    result = client._parse_json('```json\n{"memories": []}\n```')
    assert result == {"memories": []}


def test_json_with_short_model_explanation_is_parsed() -> None:
    client = BailianClient(Settings())
    result = client._parse_json('结果如下：\n{"memories": []}\n以上为有效结果。')
    assert result == {"memories": []}


def test_skip_memory_may_have_empty_content() -> None:
    candidate = MemoryCandidate(memory_type="event", confidence=1, operation="skip")
    assert candidate.content == ""


def test_persisted_memory_requires_content() -> None:
    with pytest.raises(ValidationError):
        MemoryCandidate(memory_type="event", confidence=1, operation="create")


@pytest.mark.asyncio
async def test_chat_requests_disable_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BailianClient(Settings(dashscope_api_key="test-key", chat_model="qwen3.7-plus"))
    payloads: list[dict] = []

    async def fake_post(_: str, payload: dict) -> dict:
        payloads.append(payload)
        return {"model": "qwen3.7-plus", "choices": [{"message": {"content": '{"memories": []}'}}]}

    monkeypatch.setattr(client, "_post_json", fake_post)
    await client.structured_chat(system="test", user="test", schema=CuratorOutput)
    await client.chat(system="test", messages=[{"role": "user", "content": "test"}])

    assert all(payload["enable_thinking"] is False for payload in payloads)
    assert payloads[0]["max_tokens"] == 4000
    assert "JSON Schema" in payloads[0]["messages"][0]["content"]
    assert payloads[1]["max_tokens"] == 1200


@pytest.mark.asyncio
async def test_structured_chat_retries_schema_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BailianClient(Settings(dashscope_api_key="test-key", chat_model="qwen3.7-plus"))
    responses = iter(
        [
            {
                "usage": {"total_tokens": 10},
                "choices": [
                    {
                        "message": {
                            "content": '{"memories":[{"content":"学习摄影","memory_type":"plan","confidence":0.9}]}'
                        }
                    }
                ],
            },
            {
                "usage": {"total_tokens": 12},
                "choices": [
                    {
                        "message": {
                            "content": '{"memories":[{"content":"学习摄影","memory_type":"goal","confidence":0.9}]}'
                        }
                    }
                ],
            },
        ]
    )
    payloads: list[dict] = []

    async def fake_post(_: str, payload: dict) -> dict:
        payloads.append({**payload, "messages": [*payload["messages"]]})
        return next(responses)

    monkeypatch.setattr(client, "_post_json", fake_post)
    result, metadata = await client.structured_chat(
        system="test",
        user="test",
        schema=CuratorOutput,
    )

    assert result.memories[0].memory_type == "goal"
    assert len(payloads) == 2
    assert payloads[1]["messages"][-1]["role"] == "user"
    assert metadata["validation_retry_count"] == 1
    assert metadata["usage"]["total_tokens"] == 22
