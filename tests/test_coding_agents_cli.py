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


def test_run_codex_internal_uses_built_environment(monkeypatch):
    expected_env = {"PATH": "/agent-bin", "AGENT_TOKEN": "present"}
    monkeypatch.setattr(
        coding_agents_cli,
        "find_codex_binaries",
        lambda: coding_agents_cli.CodexBinaries(node="/agent-bin/node", codex="/agent-bin/codex"),
    )
    monkeypatch.setattr(coding_agents_cli.env, "build_env", lambda: expected_env)

    captured = {}

    class _Completed:
        stdout = "ok"
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(coding_agents_cli.subprocess, "run", fake_run)

    result = coding_agents_cli._run_codex_internal(
        "prompt",
        model="gpt-5.4-mini",
        reasoning_effort="high",
        auto_approve=True,
        skip_git_check=True,
        working_dir=None,
        add_dirs=None,
        output_schema=None,
        timeout=30,
    )

    assert captured["kwargs"]["env"] is expected_env
    assert result.output == "ok"


def test_run_gemini_internal_uses_temp_file_for_prompt(monkeypatch, tmp_path):
    """Prompt is written to a temp file and passed via shell substitution."""
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

    # Command runs via sh -c with shell substitution.
    assert captured["command"][0] == "sh"
    assert captured["command"][1] == "-c"
    shell_cmd = captured["command"][2]
    assert "--print" in shell_cmd
    assert "$(cat" in shell_cmd
    assert "--dangerously-skip-permissions" in shell_cmd
    # Nothing piped via stdin.
    assert "input" not in captured["kwargs"]
    assert result.output == '{"routings": []}'
    assert result.returncode == 0


def test_run_gemini_internal_cleans_up_temp_file(monkeypatch):
    """Temp file is deleted after the run, even on success."""
    monkeypatch.setattr(coding_agents_cli, "find_gemini_bin", lambda: "/tmp/agy")
    monkeypatch.setattr(coding_agents_cli.env, "build_env", lambda: {"PATH": "/usr/bin"})

    created_paths = []

    import agents.coding_agents_cli as mod
    orig_named_temp = mod.tempfile.NamedTemporaryFile

    def tracking_named_temp(*args, **kwargs):
        f = orig_named_temp(*args, **kwargs)
        created_paths.append(f.name)
        return f

    monkeypatch.setattr(mod.tempfile, "NamedTemporaryFile", tracking_named_temp)

    class _Completed:
        stdout = "ok"
        returncode = 0
        stderr = ""

    monkeypatch.setattr(coding_agents_cli.subprocess, "run", lambda *a, **kw: _Completed())

    coding_agents_cli._run_gemini_internal(
        "prompt", model="gemini-3.5-flash", auto_approve=False
    )

    import os
    for p in created_paths:
        assert not os.path.exists(p), f"temp file {p} was not cleaned up"


def test_run_gemini_internal_works_without_auto_approve(monkeypatch):
    """Command without --dangerously-skip-permissions when auto_approve=False."""
    monkeypatch.setattr(coding_agents_cli, "find_gemini_bin", lambda: "/tmp/agy")
    monkeypatch.setattr(coding_agents_cli.env, "build_env", lambda: {"PATH": "/usr/bin"})

    captured = {}

    class _Completed:
        stdout = "ok"
        returncode = 0
        stderr = ""

    monkeypatch.setattr(coding_agents_cli.subprocess, "run", lambda cmd, **kw: (captured.__setitem__("command", cmd), captured.__setitem__("kwargs", kw), _Completed())[-1])

    coding_agents_cli._run_gemini_internal(
        "prompt", model="gemini-3.5-flash", auto_approve=False
    )

    shell_cmd = captured["command"][2]
    assert "--dangerously-skip-permissions" not in shell_cmd
    assert "--print" in shell_cmd


def test_run_claude_internal_forwards_effort_flag(monkeypatch):
    """When claude_effort is set, --effort <level> is appended to the command."""
    monkeypatch.setattr(coding_agents_cli, "find_claude_bin", lambda: "/tmp/claude")
    monkeypatch.setattr(coding_agents_cli.env, "build_env", lambda: {"PATH": "/usr/bin"})

    captured = {}

    class _Completed:
        stdout = "ok"
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        return _Completed()

    monkeypatch.setattr(coding_agents_cli.subprocess, "run", fake_run)

    coding_agents_cli._run_claude_internal(
        "prompt",
        model="sonnet",
        auto_approve=True,
        use_pty_wrapper=False,
        timeout=30,
        effort="xhigh",
    )
    cmd = captured["command"]
    assert "--effort" in cmd
    assert cmd.index("--effort") < len(cmd) - 1 and cmd[cmd.index("--effort") + 1] == "xhigh"


def test_run_claude_internal_omits_effort_flag_when_none(monkeypatch):
    """When effort is None, no --effort flag is emitted (env var stays in effect)."""
    monkeypatch.setattr(coding_agents_cli, "find_claude_bin", lambda: "/tmp/claude")
    monkeypatch.setattr(coding_agents_cli.env, "build_env", lambda: {"PATH": "/usr/bin"})

    captured = {}

    class _Completed:
        stdout = "ok"
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        return _Completed()

    monkeypatch.setattr(coding_agents_cli.subprocess, "run", fake_run)

    coding_agents_cli._run_claude_internal(
        "prompt",
        model="sonnet",
        auto_approve=True,
        use_pty_wrapper=False,
        timeout=30,
        effort=None,
    )
    assert "--effort" not in captured["command"]


def test_run_claude_internal_forwards_effort_via_pty_wrapper(monkeypatch):
    """--effort is also forwarded when driving Claude through claude-pty-wrapper."""
    monkeypatch.setattr(coding_agents_cli, "find_claude_bin", lambda: "/tmp/claude")
    monkeypatch.setattr(
        coding_agents_cli, "find_claude_pty_wrapper_bin", lambda: "/tmp/claude-pty-wrapper"
    )
    monkeypatch.setattr(coding_agents_cli.env, "build_env", lambda: {"PATH": "/usr/bin"})

    captured = {}

    class _Completed:
        stdout = "ok"
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        return _Completed()

    monkeypatch.setattr(coding_agents_cli.subprocess, "run", fake_run)

    coding_agents_cli._run_claude_internal(
        "prompt",
        model="sonnet",
        auto_approve=True,
        use_pty_wrapper=True,
        timeout=30,
        effort="max",
    )
    cmd = captured["command"]
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "max"
