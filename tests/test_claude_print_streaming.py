from agents import claude_print_streaming as print_streaming
from agents import claude_pty_wrapper_streaming


def _fake_claude_bin(monkeypatch):
    monkeypatch.setattr(
        print_streaming.coding_agents_cli,
        "find_claude_bin",
        lambda: "/opt/homebrew/bin/claude",
    )


def test_build_command_is_claude_print_stream_json_with_verbose(monkeypatch):
    _fake_claude_bin(monkeypatch)

    command = print_streaming.build_claude_print_command(model="opus", effort="high")

    # `claude -p` rejects stream-json without --verbose.
    assert command[:7] == [
        "/opt/homebrew/bin/claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "opus",
    ]
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--strict-mcp-config" not in command
    assert "claude-pty-wrapper" not in " ".join(command)


def test_build_command_omits_optional_flags_and_can_disable_mcp(monkeypatch):
    _fake_claude_bin(monkeypatch)

    plain = print_streaming.build_claude_print_command(auto_approve=False)
    no_mcp = print_streaming.build_claude_print_command(disable_mcp=True)

    assert "--effort" not in plain
    assert "--permission-mode" not in plain
    assert no_mcp[no_mcp.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in no_mcp


def test_stream_claude_print_sends_prompt_via_stdin_and_labels_process(monkeypatch):
    _fake_claude_bin(monkeypatch)
    calls = []

    def fake_stream_command(command, *, prompt, timeout, cwd, process_label):
        calls.append(
            {"command": command, "prompt": prompt, "timeout": timeout, "cwd": cwd, "label": process_label}
        )
        yield claude_pty_wrapper_streaming.ClaudePtyStreamEvent(kind="done", payload={})

    monkeypatch.setattr(print_streaming.claude_pty_wrapper_streaming, "_stream_command", fake_stream_command)

    events = list(
        print_streaming.stream_claude_print("long\nprompt", model="opus", timeout=30, cwd="/tmp/project")
    )

    assert [event.kind for event in events] == ["status", "done"]
    assert events[0].payload["message"] == "Starting Claude via claude -p."
    assert calls[0]["prompt"] == "long\nprompt"
    assert "long\nprompt" not in calls[0]["command"]
    assert (calls[0]["timeout"], calls[0]["cwd"], calls[0]["label"]) == (30, "/tmp/project", "claude -p")


def test_stream_claude_print_reports_missing_binary(monkeypatch):
    def missing():
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr(print_streaming.coding_agents_cli, "find_claude_bin", missing)

    events = list(print_streaming.stream_claude_print("hello"))

    assert [event.kind for event in events] == ["error"]
    assert "claude unavailable: claude not found" in events[0].payload["message"]


def test_build_command_auto_mode_replaces_bypass_permissions(monkeypatch):
    _fake_claude_bin(monkeypatch)

    command = print_streaming.build_claude_print_command(model="opus", auto_mode=True)

    assert command[command.index("--permission-mode") + 1] == "auto"
    assert "bypassPermissions" not in command
