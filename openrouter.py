"""Reusable OpenRouter chat completion client."""

from numpy import random

import json
from dataclasses import dataclass
from enum import Enum
import os
from typing import Any
import urllib.request

from ipylearn.utils import logging


DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_FREE_MODEL_RETRIES = 5  # default retries for free model requests before giving up


class OpenRouterModel(str, Enum):
    """Known OpenRouter model identifiers."""

    LLAMA_3_1_8B_INSTRUCT = "meta-llama/llama-3.1-8b-instruct"
    RANDOM_FREE = "random_free"


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
        free_model_retries: int = DEFAULT_FREE_MODEL_RETRIES,
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

        if model == OpenRouterModel.RANDOM_FREE:
            return self._complete_random_free(
                formatted_messages,
                temperature,
                timeout,
                retries=free_model_retries,
            )
        else:
            return self._complete(
                formatted_messages,
                model,
                temperature,
                timeout,
            )

    def _complete(
        self,
        messages: list[dict[str, str]],
        model: OpenRouterModel | str,
        temperature: float,
        timeout: float,
    ) -> str:
        payload = {
            "model": model.value if isinstance(model, OpenRouterModel) else model,
            "messages": messages,
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

    def _complete_random_free(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        timeout: float,
        retries: int = DEFAULT_FREE_MODEL_RETRIES,
    ) -> str:
        models_file = os.path.join(os.path.dirname(__file__), "data", "openrouter_free_models.txt")
        with open(models_file, "r") as f:
            models = [line.strip() for line in f if line.strip()]

        if not models:
            raise ValueError("No free models found in configuration.")

        random.shuffle(models)
        attempts = min(retries, len(models))
        
        if attempts <= 0:
            raise ValueError("Retries must be greater than 0.")

        for i, model in enumerate(models[:attempts]):
            try:
                logging.info(f"Using free model (attempt {i + 1}/{attempts}) {model}...")
                return self._complete(messages, model, temperature, timeout)
            except Exception as e:
                logging.warning(f"Error with free model {model} (attempt {i + 1}/{attempts}): {e}")
                if i == attempts - 1:
                    raise e
