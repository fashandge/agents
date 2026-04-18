import json
import urllib.error

import pytest

from agents import openrouter


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_openrouter_client_sends_chat_completion_request(monkeypatch):
    seen: dict = {}

    def _fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["timeout"] = timeout
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "Generated title"}}]})

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", _fake_urlopen)

    client = openrouter.OpenRouterChatClient("test-key")
    result = client.complete(
        messages=[openrouter.ChatMessage(role="user", content="hello")],
        temperature=0.1,
        timeout=12,
    )

    assert result == "Generated title"
    assert seen["url"] == openrouter.DEFAULT_OPENROUTER_URL
    assert seen["method"] == "POST"
    assert seen["timeout"] == 12
    assert seen["headers"]["Authorization"] == "Bearer test-key"
    assert seen["headers"]["Content-type"] == "application/json"
    assert seen["body"] == {
        "model": "meta-llama/llama-3.1-8b-instruct",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.1,
    }


def test_openrouter_client_uses_env_api_key_when_not_provided(monkeypatch):
    seen: dict = {}

    def _fake_urlopen(request, timeout):
        seen["headers"] = dict(request.header_items())
        return _FakeResponse({"choices": [{"message": {"content": "Generated title"}}]})

    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", _fake_urlopen)

    client = openrouter.OpenRouterChatClient()
    result = client.complete(
        messages=[openrouter.ChatMessage(role="user", content="hello")],
    )

    assert result == "Generated title"
    assert seen["headers"]["Authorization"] == "Bearer env-key"


def test_openrouter_client_accepts_dict_messages(monkeypatch):
    monkeypatch.setattr(
        openrouter.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse({"choices": [{"message": {"content": "ok"}}]}),
    )

    client = openrouter.OpenRouterChatClient("test-key")
    result = client.complete(
        messages=[{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}],
        model="model",
    )

    assert result == "ok"


def test_openrouter_client_accepts_string_messages_as_user_role(monkeypatch):
    seen: dict = {}

    def _fake_urlopen(request, timeout):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", _fake_urlopen)

    client = openrouter.OpenRouterChatClient("test-key")
    result = client.complete(
        messages=["first", "second"],
        model="model",
    )

    assert result == "ok"
    assert seen["body"]["messages"] == [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]


def test_openrouter_client_accepts_single_string_message_as_user_role(monkeypatch):
    seen: dict = {}

    def _fake_urlopen(request, timeout):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", _fake_urlopen)

    client = openrouter.OpenRouterChatClient("test-key")
    result = client.complete(messages="hello world")

    assert result == "ok"
    assert seen["body"]["messages"] == [
        {"role": "user", "content": "hello world"},
    ]


def test_openrouter_client_propagates_http_error(monkeypatch):
    def _fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            url=request.full_url,
            code=500,
            msg="boom",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", _fake_urlopen)

    client = openrouter.OpenRouterChatClient("test-key")
    with pytest.raises(urllib.error.HTTPError):
        client.complete(
            messages=[openrouter.ChatMessage(role="user", content="hello")],
            model="model",
        )
