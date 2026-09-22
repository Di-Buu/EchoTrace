import asyncio
import json
import re
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings

T = TypeVar("T", bound=BaseModel)


class AiConfigurationError(RuntimeError):
    pass


class AiResponseError(RuntimeError):
    pass


class BailianClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def embedding(self, text: str) -> tuple[list[float], dict[str, Any]]:
        self._require("embedding_model")
        started = time.perf_counter()
        payload = {"model": self.settings.embedding_model, "input": text}
        data = await self._post_json("/embeddings", payload)
        try:
            vector = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AiResponseError("Embedding 返回结构不符合预期") from exc
        return vector, {
            "model": data.get("model", self.settings.embedding_model),
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "usage": data.get("usage", {}),
        }

    async def structured_chat(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        temperature: float = 0.1,
        enable_thinking: bool = False,
        max_tokens: int = 4000,
        timeout_seconds: float | None = None,
    ) -> tuple[T, dict[str, Any]]:
        self._require("chat_model")
        started = time.perf_counter()
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
        messages = [
            {
                "role": "system",
                "content": (
                    f"{system}\n\n输出必须严格符合以下 JSON Schema。"
                    "枚举值、字段类型和必填字段均不得自行扩展：\n"
                    f"{schema_json}"
                ),
            },
            {"role": "user", "content": user},
        ]
        payload = {
            "model": self.settings.chat_model,
            "messages": messages,
            "temperature": temperature,
            "enable_thinking": enable_thinking,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        total_usage: dict[str, int | float] = {}
        for validation_attempt in range(2):
            if timeout_seconds is None:
                data = await self._post_json("/chat/completions", payload)
            else:
                data = await self._post_json(
                    "/chat/completions",
                    payload,
                    timeout_seconds=timeout_seconds,
                )
            self._add_usage(total_usage, data.get("usage", {}))
            content = self._message_content(data)
            try:
                parsed = self._parse_json(content)
                result = schema.model_validate(parsed)
            except (AiResponseError, ValidationError) as exc:
                if validation_attempt == 1:
                    raise AiResponseError("模型返回未通过结构校验") from exc
                messages.extend(
                    [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "上一次输出未通过 JSON Schema 校验。请纠正枚举值、字段类型和必填字段，"
                                "只返回完整 JSON 对象。"
                            ),
                        },
                    ]
                )
                continue
            metadata = self._metadata(data, started)
            metadata["usage"] = total_usage
            metadata["validation_retry_count"] = validation_attempt
            metadata["thinking_enabled"] = enable_thinking
            return result, metadata
        raise AiResponseError("模型返回未通过结构校验")

    async def chat(
        self, *, system: str, messages: list[dict[str, str]], temperature: float = 0.6
    ) -> tuple[str, dict[str, Any]]:
        self._require("chat_model")
        started = time.perf_counter()
        payload = {
            "model": self.settings.chat_model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": temperature,
            "enable_thinking": False,
            "max_tokens": 1200,
            "stream": False,
        }
        data = await self._post_json("/chat/completions", payload)
        return self._message_content(data), self._metadata(data, started)

    async def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not self.settings.dashscope_api_key:
            raise AiConfigurationError("DASHSCOPE_API_KEY 尚未配置")
        headers = {
            "Authorization": f"Bearer {self.settings.dashscope_api_key}",
            "Content-Type": "application/json",
        }
        response: httpx.Response | None = None
        timeout = timeout_seconds or self.settings.ai_timeout_seconds
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(3):
                try:
                    response = await client.post(
                        f"{self.settings.dashscope_base_url.rstrip('/')}{path}",
                        headers=headers,
                        json=payload,
                    )
                except httpx.TimeoutException as exc:
                    # A second long wait is enough to cover transient queueing
                    # without leaving the product UI blocked for several minutes.
                    if attempt >= 1:
                        raise AiResponseError("百炼请求超时") from exc
                    await asyncio.sleep(0.6)
                    continue
                except httpx.RequestError as exc:
                    if attempt == 2:
                        raise AiResponseError("百炼网络连接失败") from exc
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                    response.extensions["echotrace_retry_count"] = attempt
                    break
                await asyncio.sleep(0.6 * (attempt + 1))
        if response is None:
            raise AiResponseError("百炼请求未完成")
        if response.is_error:
            raise AiResponseError(f"百炼调用失败 {response.status_code}: {response.text[:800]}")
        result = response.json()
        result["_client_retry_count"] = response.extensions.get("echotrace_retry_count", 0)
        return result

    def _require(self, field: str) -> None:
        if not getattr(self.settings, field):
            raise AiConfigurationError(f"{field.upper()} 尚未配置")

    @staticmethod
    def _message_content(data: dict[str, Any]) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AiResponseError("模型返回结构不符合预期") from exc
        if isinstance(content, list):
            return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        return str(content)

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            # Some compatible models still wrap an otherwise valid JSON object
            # with a short explanation even when json_object is requested.
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                try:
                    value = json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError as nested_exc:
                    raise AiResponseError(
                        f"模型没有返回有效 JSON (length={len(cleaned)}, complete_object=true)"
                    ) from nested_exc
            else:
                raise AiResponseError(
                    f"模型没有返回有效 JSON (length={len(cleaned)}, complete_object=false)"
                ) from exc
        if not isinstance(value, dict):
            raise AiResponseError("模型 JSON 顶层必须是对象")
        return value

    def _metadata(self, data: dict[str, Any], started: float) -> dict[str, Any]:
        return {
            "model": data.get("model", self.settings.chat_model),
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "usage": data.get("usage", {}),
            "retry_count": data.get("_client_retry_count", 0),
        }

    @staticmethod
    def _add_usage(total: dict[str, int | float], usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
