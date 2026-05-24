import io
import json

from agents import claude_pty_wrapper_streaming as streaming


def _line(payload: dict) -> str:
    return json.dumps(payload) + "\n"


def test_format_assistant_text():
    formatted = streaming._format_stream_json_line(
        _line(
            {
                "type": "assistant",
                "session_id": "session-1",
                "message": {
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "text", "text": " world"},
                    ]
                },
            }
        )
    )

    assert formatted.display_text == "hello world"
    assert formatted.assistant_text == "hello world"
    assert formatted.session_id == "session-1"


def test_format_tool_use_as_readable_message():
    formatted = streaming._format_stream_json_line(
        _line(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "pwd"},
                        }
                    ]
                },
            }
        )
    )

    assert formatted.display_text == '[tool: Bash {"command": "pwd"}]\n'
    assert formatted.assistant_text == ""


def test_format_tool_result_as_readable_message():
    formatted = streaming._format_stream_json_line(
        _line(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": [{"type": "text", "text": "repo root\n"}],
                        }
                    ]
                },
            }
        )
    )

    assert formatted.display_text == "[tool result: repo root]\n"
    assert formatted.assistant_text == ""


def test_format_result_stores_final_output():
    formatted = streaming._format_stream_json_line(
        _line({"type": "result", "session_id": "session-2", "result": "final answer"})
    )

    assert formatted.display_text == "final answer\n"
    assert formatted.assistant_text == ""
    assert formatted.result_text == "final answer"
    assert formatted.session_id == "session-2"


def test_format_system_is_suppressed():
    formatted = streaming._format_stream_json_line(
        _line({"type": "system", "session_id": "session-3", "subtype": "init"})
    )

    assert formatted.display_text == ""
    assert formatted.assistant_text == ""
    assert formatted.session_id == "session-3"


def test_format_non_json_passthrough():
    formatted = streaming._format_stream_json_line("plain output\n")

    assert formatted.display_text == "plain output\n"
    assert formatted.assistant_text == ""


def test_build_command_uses_wrapper_not_claude_print(monkeypatch):
    monkeypatch.setattr(
        streaming.coding_agents_cli,
        "find_claude_pty_wrapper_bin",
        lambda: "/opt/homebrew/bin/claude-pty-wrapper",
    )
    monkeypatch.setattr(
        streaming.coding_agents_cli,
        "find_claude_bin",
        lambda: "/opt/homebrew/bin/claude",
    )

    command = streaming.build_claude_pty_wrapper_command(
        "hello",
        model="sonnet",
        auto_approve=True,
        timeout=30,
        cwd="/tmp/project",
    )

    assert command[0] == "/opt/homebrew/bin/claude-pty-wrapper"
    assert command[1] == "-p"
    assert "--output-format" in command
    assert "stream-json" in command
    assert "--claude-bin" in command
    assert "/opt/homebrew/bin/claude" in command
    assert "--permission-mode" in command
    assert "bypassPermissions" in command
    assert "--cwd" in command
    assert "/tmp/project" in command
    assert command[-2:] == ["--", "hello"]
    assert command[:2] != ["/opt/homebrew/bin/claude", "-p"]


def test_build_command_omits_permission_mode_when_not_auto_approve(monkeypatch):
    monkeypatch.setattr(
        streaming.coding_agents_cli,
        "find_claude_pty_wrapper_bin",
        lambda: "/opt/homebrew/bin/claude-pty-wrapper",
    )
    monkeypatch.setattr(
        streaming.coding_agents_cli,
        "find_claude_bin",
        lambda: "/opt/homebrew/bin/claude",
    )

    command = streaming.build_claude_pty_wrapper_command("hello", auto_approve=False)

    assert "--permission-mode" not in command
    assert "bypassPermissions" not in command


class _FakePopen:
    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.stdout = io.StringIO(
            _line(
                {
                    "type": "system",
                    "session_id": "session-4",
                    "subtype": "init",
                }
            )
            + _line(
                {
                    "type": "assistant",
                    "session_id": "session-4",
                    "message": {"content": [{"type": "text", "text": "hello"}]},
                }
            )
            + _line({"type": "result", "session_id": "session-4", "result": "hello"})
        )
        self.stderr = io.StringIO("diagnostic\n")
        self.returncode = 0
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_stream_claude_pty_with_mocked_popen(monkeypatch):
    monkeypatch.setattr(
        streaming.coding_agents_cli,
        "find_claude_pty_wrapper_bin",
        lambda: "/opt/homebrew/bin/claude-pty-wrapper",
    )
    monkeypatch.setattr(
        streaming.coding_agents_cli,
        "find_claude_bin",
        lambda: "/opt/homebrew/bin/claude",
    )
    monkeypatch.setattr(streaming.agents_env, "build_env", lambda: {"HOME": "/tmp"})
    monkeypatch.setattr(streaming.subprocess, "Popen", _FakePopen)

    events = list(
        streaming.stream_claude_pty(
            "hello",
            model="sonnet",
            timeout=30,
            cwd="/tmp/project",
        )
    )

    assert [event.kind for event in events] == ["status", "log", "log", "done"]
    assert events[1].payload == {"stream": "claude", "text": "hello"}
    assert events[2].payload == {"stream": "stderr", "text": "diagnostic\n"}
    result = events[-1].payload["result"]
    assert isinstance(result, streaming.ClaudePtyStreamResult)
    assert result.output == "hello"
    assert result.returncode == 0
    assert result.stderr == "diagnostic\n"
    assert result.session_id == "session-4"
