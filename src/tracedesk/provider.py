"""A bounded chat-completions transport; tool execution lives outside the model."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from .errors import DomainError

if TYPE_CHECKING:
    from .settings import Settings

MAX_RESPONSE_BYTES = 256_000
MAX_TOOL_ARGUMENT_CHARS = 6000


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelReply:
    calls: tuple[ToolCall, ...]
    message: dict[str, Any]
    total_tokens: int | None = None


class ModelProvider(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


class ChatCompletionsProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, follow_redirects=False
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "required",
            "temperature": 0,
            "max_tokens": self.settings.max_completion_tokens,
            "stream": False,
        }
        try:
            content = await asyncio.wait_for(
                self._request(headers, payload), timeout=self.settings.request_timeout_seconds
            )
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            raise DomainError(
                "MODEL_TIMEOUT", "模型调用超过总时限，请使用新的请求重试。", 504
            ) from exc
        except httpx.RequestError as exc:
            raise DomainError(
                "MODEL_UNAVAILABLE", "无法连接模型服务，本次未生成回答。", 502
            ) from exc
        return parse_reply(content)

    async def _request(self, headers: dict[str, str], payload: dict[str, Any]) -> bytes:
        async with self.client.stream(
            "POST",
            f"{self.settings.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.settings.request_timeout_seconds,
        ) as response:
            if response.status_code != 200:
                raise DomainError(
                    "MODEL_HTTP_ERROR",
                    f"模型服务返回 HTTP {response.status_code}，本次未生成回答。",
                    502,
                )
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise DomainError("MODEL_RESPONSE_TOO_LARGE", "模型响应超过大小预算。", 502)
        return bytes(chunks)


def parse_reply(content: bytes) -> ModelReply:
    try:
        body = json.loads(content)
        if not isinstance(body, dict):
            raise ValueError("response must be an object")
        choices = body["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("expected a single choice")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError("choice must be an object")
        if choice.get("finish_reason") not in {"tool_calls", "stop"}:
            raise ValueError("response stopped before a complete tool call")
        message = choice["message"]
        if not isinstance(message, dict):
            raise ValueError("message must be an object")
        raw_calls = message["tool_calls"]
        if message.get("role") != "assistant" or not isinstance(raw_calls, list) or not raw_calls:
            raise ValueError("the provider must return structured tool calls")
        calls = []
        seen = set()
        for raw in raw_calls:
            if not isinstance(raw, dict) or not isinstance(raw.get("function"), dict):
                raise ValueError("call and function must be objects")
            call_id, name, arguments = (
                raw["id"],
                raw["function"]["name"],
                raw["function"]["arguments"],
            )
            if raw.get("type") != "function" or not all(
                isinstance(value, str) and value for value in (call_id, name, arguments)
            ):
                raise ValueError("invalid function call")
            if len(call_id) > 200 or len(name) > 80 or len(arguments) > MAX_TOOL_ARGUMENT_CHARS:
                raise ValueError("tool call is too large")
            if call_id in seen:
                raise ValueError("duplicate tool call ID")
            seen.add(call_id)
            calls.append(ToolCall(call_id, name, arguments))
        usage = body.get("usage")
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("invalid token usage")
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError) as exc:
        raise DomainError(
            "MODEL_PROTOCOL_ERROR",
            "模型没有返回完整、合法的工具调用；请确认模型支持 function calling。",
            502,
        ) from exc
    # Re-send only public assistant/tool fields, never provider-specific reasoning content.
    safe_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in calls
        ],
    }
    return ModelReply(tuple(calls), safe_message, tokens)
