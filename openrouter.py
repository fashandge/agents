"""Reusable OpenRouter chat completion client."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
import os
from typing import Any
import urllib.request


DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterModel(str, Enum):
    """Known OpenRouter model identifiers."""

    LLAMA_3_1_8B_INSTRUCT = "meta-llama/llama-3.1-8b-instruct"


DEFAULT_OPENROUTER_MODEL = OpenRouterModel.LLAMA_3_1_8B_INSTRUCT


@dataclass(frozen=True)
class ChatMessage:
    """Single chat message for a completion request."""

    role: str
    content: str


class OpenRouterChatClient:
    """Small generic OpenRouter chat client."""

    def __init__(self, api_key: str | None = None, *, base_url: str = DEFAULT_OPENROUTER_URL):
        if api_key is None:
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            raise ValueError("OpenRouter API key is not set.")

        self.api_key = api_key
        self.base_url = base_url

    def complete(
        self,
        *,
        messages: str | list[ChatMessage] | list[dict[str, str]] | list[str],
        model: OpenRouterModel | str = DEFAULT_OPENROUTER_MODEL,
        temperature: float = 0.2,
        timeout: float = 60,
    ) -> str:
        if isinstance(messages, str):
            formatted_messages = [{"role": "user", "content": messages}]
        elif not messages:
            formatted_messages = []
        elif isinstance(messages[0], str):
            formatted_messages = [{"role": "user", "content": m} for m in messages]
        elif isinstance(messages[0], dict):
            formatted_messages = list(messages)
        else:
            formatted_messages = [{"role": m.role, "content": m.content} for m in messages]

        payload = {
            "model": model.value if isinstance(model, OpenRouterModel) else model,
            "messages": formatted_messages,
            "temperature": temperature,
        }
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
