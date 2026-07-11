from pathlib import Path

from agents import claude_pty_wrapper_streaming
from agents import coding_agents_cli
from agents import coding_agents_streaming as streaming


def _config(agent, **kwargs):
    return coding_agents_cli.AgentConfig(agent=agent, **kwargs)


def test_build_codex_command_uses_config(monkeypatch):
    monkeypatch.setattr(
        streaming.coding_agents_cli,
        "find_codex_binaries",
        lambda: streaming.coding_agents_cli.CodexBinaries(
            node="/opt/homebrew/bin/node",
            codex="/opt/homebrew/bin/codex",
        ),
    )
    config = _config(
        "codex",
        model="gpt-5.5",
        codex_reasoning_effort="medium",
        codex_working_dir=Path("/tmp/project"),
    )

    command = streaming.build_codex_command(config)

    assert command == [
        "/opt/homebrew/bin/node",
        "/opt/homebrew/bin/codex",
        "exec",
        "--model",
        "gpt-5.5",
        "-c",
        'model_reasoning_effort="medium"',
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--cd",
        "/tmp/project",
        "-",
    ]


def test_stream_codex_accumulates_successful_output(monkeypatch):
    monkeypatch.setattr(streaming, "build_codex_command", lambda config: ["codex", "exec", "-"])

    def fake_pty(command, prompt, **kwargs):
        assert command == ["codex", "exec", "-"]
        assert prompt == "hello"
        assert kwargs["cwd"] == "/tmp/project"
        yield streaming.AgentStreamEvent(
            kind="log",
            payload={"stream": "pty", "text": "final answer"},
        )

    monkeypatch.setattr(streaming, "_stream_subprocess_with_pty", fake_pty)
    config = _config("codex", codex_working_dir=Path("/tmp/project"))

    events = list(streaming.stream_with_config("hello", config))

    assert [event.kind for event in events] == ["status", "log", "done"]
    result = events[-1].payload["result"]
    assert result == streaming.AgentStreamResult(
        output="final answer",
        returncode=0,
        stderr="",
        agent=coding_agents_cli.AgentType.CODEX,
    )


def test_stream_codex_pty_failure_falls_back_to_pipes(monkeypatch):
    monkeypatch.setattr(streaming, "build_codex_command", lambda config: ["codex", "exec", "-"])

    def broken_pty(command, prompt, **kwargs):
        raise OSError("no pty")
        yield

    def fake_pipes(command, prompt, **kwargs):
        yield streaming.AgentStreamEvent(
            kind="log",
            payload={"stream": "stdout", "text": "pipe answer"},
        )

    monkeypatch.setattr(streaming, "_stream_subprocess_with_pty", broken_pty)
    monkeypatch.setattr(streaming, "_stream_subprocess_with_pipes", fake_pipes)

    events = list(streaming.stream_with_config("hello", _config("codex")))

    assert [event.kind for event in events] == ["status", "status", "log", "done"]
    assert events[1].payload["message"] == "PTY unavailable; streaming Codex output via pipes."
    assert events[-1].payload["result"].output == "pipe answer"


def test_stream_claude_delegates_to_pty_wrapper_without_prompt_argv(monkeypatch):
    calls = []

    def fake_stream(prompt, *, model, auto_approve, timeout, cwd):
        calls.append(
            {
                "prompt": prompt,
                "model": model,
                "auto_approve": auto_approve,
                "timeout": timeout,
                "cwd": cwd,
            }
        )
        yield claude_pty_wrapper_streaming.ClaudePtyStreamEvent(
            kind="done",
            payload={
                "result": claude_pty_wrapper_streaming.ClaudePtyStreamResult(
                    output="claude answer",
                    returncode=0,
                    stderr="",
                )
            },
        )

    monkeypatch.setattr(streaming.claude_pty_wrapper_streaming, "stream_claude_pty", fake_stream)
    config = _config(
        "claude",
        model="claude-opus-4-6[1m]",
        codex_working_dir=Path("/tmp/project"),
    )

    events = list(streaming.stream_with_config("long\nprompt", config, timeout=42))

    assert calls == [
        {
            "prompt": "long\nprompt",
            "model": "claude-opus-4-6[1m]",
            "auto_approve": True,
            "timeout": 42,
            "cwd": Path("/tmp/project"),
        }
    ]
    assert events[-1].payload["result"].output == "claude answer"


def test_build_gemini_command_and_env(monkeypatch):
    monkeypatch.setattr(streaming.coding_agents_cli, "find_gemini_bin", lambda: "/tmp/agy")
    monkeypatch.setattr(streaming.coding_agents_cli.env, "build_env", lambda: {"PATH": "/usr/bin"})
    config = _config("gemini", model="gemini-3.1-pro")

    assert streaming.build_gemini_command(config) == [
        "/tmp/agy",
        "--dangerously-skip-permissions",
        "--print",
        "-",
    ]
    assert streaming.build_gemini_env(config) == {
        "PATH": "/usr/bin",
        "ANTIGRAVITY_MODEL": "gemini-3.1-pro",
    }


def test_stream_gemini_forwards_heartbeat_status(monkeypatch):
    """Streaming Gemini uses temp file for prompt and write_stdin=False."""
    monkeypatch.setattr(streaming.coding_agents_cli, "find_gemini_bin", lambda: "/tmp/agy")
    monkeypatch.setattr(streaming.coding_agents_cli.env, "build_env", lambda: {"PATH": "/usr/bin"})
    monkeypatch.setattr(streaming, "build_gemini_env", lambda config: {"ANTIGRAVITY_MODEL": "gemini-3.1-pro"})

    captured = {}

    def fake_pty(command, prompt, **kwargs):
        captured["command"] = command
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        assert kwargs.get("write_stdin", True) is False, "streaming gemini must not write stdin"
        assert kwargs["heartbeat_interval"] == streaming.DEFAULT_GEMINI_HEARTBEAT_INTERVAL_SECONDS
        assert kwargs["env"] == {"ANTIGRAVITY_MODEL": "gemini-3.1-pro"}
        yield streaming.AgentStreamEvent(
            kind="status",
            payload={"message": "gemini working... 5s elapsed, idle 5s"},
        )
        yield streaming.AgentStreamEvent(
            kind="log",
            payload={"stream": "pty", "text": "gemini answer"},
        )

    monkeypatch.setattr(streaming, "_stream_subprocess_with_pty", fake_pty)

    events = list(streaming.stream_with_config("hello", _config("gemini", model="gemini-3.1-pro", auto_approve=True)))

    assert [event.kind for event in events] == ["status", "status", "log", "done"]
    assert "gemini working" in str(events[1].payload["message"])
    assert events[-1].payload["result"].output == "gemini answer"
    # Command is sh -c with temp file substitution
    assert captured["command"][0] == "sh"
    assert captured["command"][1] == "-c"
    assert "--print" in captured["command"][2]
    assert "$(cat" in captured["command"][2]
    assert "--dangerously-skip-permissions" in captured["command"][2]
    assert captured["prompt"] == ""

    import os
    # Temp file should be cleaned up
    assert "$(cat" in captured["command"][2]
    # Extract temp file path and verify it's gone
    import re
    m = re.search(r'cat (\S+)\)', captured["command"][2])
    if m:
        assert not os.path.exists(m.group(1)), "temp file was not cleaned up"


def test_stream_with_config_and_fallback_clears_failed_output(monkeypatch):
    configs = [_config("codex"), _config("claude")]

    def fake_stream(prompt, config, *, timeout=None):
        if config.agent == coding_agents_cli.AgentType.CODEX:
            yield streaming.AgentStreamEvent(
                kind="log",
                payload={"stream": "pty", "text": "stale codex output"},
            )
            yield streaming.AgentStreamEvent(
                kind="error",
                payload={"message": "codex failed"},
            )
            return
        yield streaming.AgentStreamEvent(
            kind="log",
            payload={"stream": "claude", "text": "claude progress"},
        )
        yield streaming.AgentStreamEvent(
            kind="done",
            payload={
                "result": streaming.AgentStreamResult(
                    output="claude answer",
                    returncode=0,
                    stderr="",
                    agent=coding_agents_cli.AgentType.CLAUDE,
                )
            },
        )

    monkeypatch.setattr(streaming, "stream_with_config", fake_stream)

    events = list(streaming.stream_with_config_and_fallback("hello", configs))

    assert [event.kind for event in events] == ["log", "fallback", "log", "done"]
    assert events[1].payload["from_agent"] == "codex"
    assert events[1].payload["to_agent"] == "claude"
    assert events[-1].payload["result"].output == "claude answer"


def test_stream_with_config_and_fallback_errors_after_all_fail(monkeypatch):
    configs = [_config("codex"), _config("gemini")]

    def fake_stream(prompt, config, *, timeout=None):
        yield streaming.AgentStreamEvent(
            kind="error",
            payload={"message": f"{config.agent.value} failed"},
        )

    monkeypatch.setattr(streaming, "stream_with_config", fake_stream)

    events = list(streaming.stream_with_config_and_fallback("hello", configs))

    assert [event.kind for event in events] == ["fallback", "error"]
    assert "codex failed" in str(events[-1].payload["message"])
    assert "gemini failed" in str(events[-1].payload["message"])


def test_stream_close_cancels_underlying_generator(monkeypatch):
    closed = {"value": False}

    def fake_pty(command, prompt, **kwargs):
        try:
            yield streaming.AgentStreamEvent(kind="log", payload={"stream": "pty", "text": "working"})
            while True:
                yield streaming.AgentStreamEvent(kind="log", payload={"stream": "pty", "text": "still working"})
        finally:
            closed["value"] = True

    monkeypatch.setattr(streaming, "build_codex_command", lambda config: ["codex", "exec", "-"])
    monkeypatch.setattr(streaming, "_stream_subprocess_with_pty", fake_pty)

    stream = streaming.stream_with_config("hello", _config("codex"))
    next(stream)
    next(stream)
    stream.close()

    assert closed["value"] is True
