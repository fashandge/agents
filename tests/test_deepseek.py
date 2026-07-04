import json
import urllib.error

import pytest

from agents import deepseek


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_deepseek_client_disables_thinking_by_default(monkeypatch):
    seen: dict = {}

    def _fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["timeout"] = timeout
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "A summary."}}]})

    monkeypatch.setattr(deepseek.urllib.request, "urlopen", _fake_urlopen)

    client = deepseek.DeepSeekChatClient("test-key")
    result = client.complete(
        messages="summarize this",
        temperature=0.2,
        max_tokens=240,
        timeout=30,
    )

    assert result == "A summary."
    assert seen["url"] == deepseek.DEFAULT_DEEPSEEK_URL
    assert seen["method"] == "POST"
    assert seen["timeout"] == 30
    assert seen["headers"]["Authorization"] == "Bearer test-key"
    assert seen["headers"]["Content-type"] == "application/json"
    assert seen["body"] == {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "summarize this"}],
        "temperature": 0.2,
        "max_tokens": 240,
        "thinking": {"type": "disabled"},
    }


def test_deepseek_client_can_enable_thinking(monkeypatch):
    seen: dict = {}

    def _fake_urlopen(request, timeout):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(deepseek.urllib.request, "urlopen", _fake_urlopen)

    client = deepseek.DeepSeekChatClient("test-key")
    client.complete(messages="hi", thinking=True)

    assert seen["body"]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in seen["body"]
    assert "max_tokens" not in seen["body"]


def test_deepseek_client_sends_reasoning_effort_when_thinking_enabled(monkeypatch):
    seen: dict = {}

    def _fake_urlopen(request, timeout):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(deepseek.urllib.request, "urlopen", _fake_urlopen)

    client = deepseek.DeepSeekChatClient("test-key")
    client.complete(
        messages="hi",
        model=deepseek.DeepSeekModel.V4_PRO,
        thinking=True,
        reasoning_effort=deepseek.ReasoningEffort.MAX,
    )

    assert seen["body"]["model"] == "deepseek-v4-pro"
    assert seen["body"]["thinking"] == {"type": "enabled"}
    assert seen["body"]["reasoning_effort"] == "max"


def test_deepseek_client_ignores_reasoning_effort_when_thinking_disabled(monkeypatch):
    seen: dict = {}

    def _fake_urlopen(request, timeout):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(deepseek.urllib.request, "urlopen", _fake_urlopen)

    client = deepseek.DeepSeekChatClient("test-key")
    client.complete(messages="hi", thinking=False, reasoning_effort="max")

    assert seen["body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in seen["body"]


def test_deepseek_client_uses_env_api_key_when_not_provided(monkeypatch):
    seen: dict = {}

    def _fake_urlopen(request, timeout):
        seen["headers"] = dict(request.header_items())
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    monkeypatch.setattr(deepseek.urllib.request, "urlopen", _fake_urlopen)

    client = deepseek.DeepSeekChatClient()
    result = client.complete(messages="hello")

    assert result == "ok"
    assert seen["headers"]["Authorization"] == "Bearer env-key"


def test_deepseek_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError):
        deepseek.DeepSeekChatClient()


def test_deepseek_client_accepts_chat_messages(monkeypatch):
    seen: dict = {}

    def _fake_urlopen(request, timeout):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(deepseek.urllib.request, "urlopen", _fake_urlopen)

    client = deepseek.DeepSeekChatClient("test-key")
    client.complete(
        messages=[
            deepseek.ChatMessage(role="system", content="rules"),
            deepseek.ChatMessage(role="user", content="hi"),
        ],
        model=deepseek.DeepSeekModel.CHAT,
    )

    assert seen["body"]["model"] == "deepseek-chat"
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "hi"},
    ]


def test_deepseek_client_propagates_http_error(monkeypatch):
    def _fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(url=request.full_url, code=429, msg="rate", hdrs=None, fp=None)

    monkeypatch.setattr(deepseek.urllib.request, "urlopen", _fake_urlopen)

    client = deepseek.DeepSeekChatClient("test-key")
    with pytest.raises(urllib.error.HTTPError):
        client.complete(messages="hello")
