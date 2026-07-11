from agents import coding_agents_cli


def test_build_gemini_command_passes_prompt_as_print_arg(monkeypatch):
    monkeypatch.setattr(coding_agents_cli, "find_gemini_bin", lambda: "/tmp/agy")

    assert coding_agents_cli.build_gemini_command(
        auto_approve=True, prompt="route these notes"
    ) == [
        "/tmp/agy",
        "--dangerously-skip-permissions",
        "--print",
        "route these notes",
    ]


def test_build_gemini_command_without_prompt_keeps_legacy_stdin_form(monkeypatch):
    monkeypatch.setattr(coding_agents_cli, "find_gemini_bin", lambda: "/tmp/agy")

    assert coding_agents_cli.build_gemini_command(auto_approve=False) == [
        "/tmp/agy",
        "--print",
        "-",
    ]


def test_run_gemini_internal_sends_prompt_as_arg_not_stdin(monkeypatch):
    monkeypatch.setattr(coding_agents_cli, "find_gemini_bin", lambda: "/tmp/agy")
    monkeypatch.setattr(coding_agents_cli.env, "build_env", lambda: {"PATH": "/usr/bin"})

    captured = {}

    class _Completed:
        stdout = '{"routings": []}'
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(coding_agents_cli.subprocess, "run", fake_run)

    result = coding_agents_cli._run_gemini_internal(
        "the prompt", model="gemini-3.5-flash", auto_approve=True
    )

    # Prompt is the --print argument; nothing is piped via stdin.
    assert captured["command"] == [
        "/tmp/agy",
        "--dangerously-skip-permissions",
        "--print",
        "the prompt",
    ]
    assert "input" not in captured["kwargs"]
    assert result.output == '{"routings": []}'
    assert result.returncode == 0
