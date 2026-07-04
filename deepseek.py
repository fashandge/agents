"""Reusable DeepSeek chat completion client.

DeepSeek exposes an OpenAI-compatible chat completions endpoint. Hybrid V4 models
(``deepseek-v4-pro`` / ``deepseek-v4-flash``) reason by default; pass
``thinking=False`` (the default here) to disable the reasoning pass for faster,
cheaper completions. When thinking is enabled, its depth is controlled by
``reasoning_effort`` — the only effective values are ``"high"`` (default) and
``"max"``; DeepSeek maps legacy ``low``/``medium``/``xhigh`` onto those two.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
import os
from typing import Any
import urllib.request


DEFAULT_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


class DeepSeekModel(str, Enum):
    """Known DeepSeek model identifiers (direct-API form, no provider prefix)."""

    V4_PRO = "deepseek-v4-pro"
    V4_FLASH = "deepseek-v4-flash"
    CHAT = "deepseek-chat"
    REASONER = "deepseek-reasoner"


class ReasoningEffort(str, Enum):
    """Effective thinking-depth levels accepted by the DeepSeek API.

    DeepSeek only honors ``high`` and ``max``; legacy ``low``/``medium``/``xhigh``
    are mapped onto these by the server.
    """

    HIGH = "high"
    MAX = "max"


DEFAULT_DEEPSEEK_MODEL = DeepSeekModel.V4_FLASH


@dataclass(frozen=True)
class ChatMessage:
    """Single chat message for a completion request."""

    role: str
    content: str


class DeepSeekChatClient:
    """Small generic DeepSeek chat client."""

    def __init__(self, api_key: str | None = None, *, base_url: str = DEFAULT_DEEPSEEK_URL):
        if api_key is None:
            api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise ValueError("DeepSeek API key is not set.")

        self.api_key = api_key
        self.base_url = base_url

    def complete(
        self,
        *,
        messages: str | list[ChatMessage] | list[dict[str, str]] | list[str],
        model: DeepSeekModel | str = DEFAULT_DEEPSEEK_MODEL,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        thinking: bool = False,
        reasoning_effort: ReasoningEffort | str | None = None,
        timeout: float = 60,
    ) -> str:
        formatted_messages = _format_messages(messages)

        payload: dict[str, Any] = {
            "model": model.value if isinstance(model, DeepSeekModel) else model,
            "messages": formatted_messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if thinking:
            payload["thinking"] = {"type": "enabled"}
            if reasoning_effort is not None:
                payload["reasoning_effort"] = (
                    reasoning_effort.value
                    if isinstance(reasoning_effort, ReasoningEffort)
                    else reasoning_effort
                )
        else:
            # Disable the reasoning pass on hybrid V4 models (deepseek-v4-pro /
            # deepseek-v4-flash), which otherwise reason by default. reasoning_effort
            # is ignored here since there is no reasoning pass to size.
            payload["thinking"] = {"type": "disabled"}

        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")

        parsed: dict[str, Any] = json.loads(body)
        return parsed["choices"][0]["message"]["content"].strip()


def _format_messages(
    messages: str | list[ChatMessage] | list[dict[str, str]] | list[str],
) -> list[dict[str, str]]:
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    if not messages:
        return []
    if isinstance(messages[0], str):
        return [{"role": "user", "content": m} for m in messages]
    if isinstance(messages[0], dict):
        return list(messages)
    return [{"role": m.role, "content": m.content} for m in messages]
