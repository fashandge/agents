import json
import os
import stat
from pathlib import Path

import pytest

from agents.orchestration import handoff_launcher
from agents.orchestration import handoff_registry


@pytest.fixture(autouse=True)
def isolate_handoff_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(tmp_path / "registry.json"))


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_agent_argv_preserves_kickoff_metacharacters_as_one_literal_arg(tmp_path, monkeypatch):
    kickoff = tmp_path / "kickoff ; weird"
    payload = "$(touch SHOULD_NOT_EXIST); 'quoted' $HOME"
    kickoff.write_text(payload)
    monkeypatch.setattr(handoff_launcher.shutil, "which", lambda *args, **kwargs: "/bin/codex")
    argv = handoff_launcher._agent_argv(
        {"agent": "codex", "model": "m", "effort": "xhigh", "pmode": "bypassPermissions", "kickoff": str(kickoff)},
        {"PATH": "/bin"},
    )
    assert argv[-1] == payload
    assert argv[0] == "/bin/codex"
    assert all("sh -c" not in item for item in argv)
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


def test_kimi_argv_passes_explicit_default_without_putting_kickoff_on_argv(tmp_path, monkeypatch):
    kickoff = tmp_path / "kickoff"
    kickoff.write_text("literal $(touch SHOULD_NOT_EXIST)")
    monkeypatch.setattr(handoff_launcher.shutil, "which", lambda *args, **kwargs: "/bin/kimi")

    argv = handoff_launcher._agent_argv(
        {
            "agent": "kimi",
            "model": "kimi-code/k3",
            "effort": "max",
            "pmode": "bypassPermissions",
            "kickoff": str(kickoff),
        },
        {"PATH": "/bin"},
    )

    assert argv == ["/bin/kimi", "-m", "kimi-code/k3", "--yolo"]
    assert kickoff.read_text() not in argv


def test_kimi_argv_accepts_explicit_configured_k3_alias(tmp_path, monkeypatch):
    kickoff = tmp_path / "kickoff"
    kickoff.write_text("task")
    monkeypatch.setattr(handoff_launcher.shutil, "which", lambda *args, **kwargs: "/bin/kimi")

    argv = handoff_launcher._agent_argv(
        {
            "agent": "kimi", "model": "kimi-code/k3", "effort": "max",
            "pmode": "auto", "kickoff": str(kickoff),
        },
        {"PATH": "/bin"},
    )

    assert argv == ["/bin/kimi", "-m", "kimi-code/k3", "--auto"]


def test_private_wrapper_exec_sets_only_protected_handoff_environment(tmp_path, monkeypatch):
    kickoff = tmp_path / "kickoff"; kickoff.write_text("literal prompt")
    config = {
        "agent": "claude", "model": "opus", "effort": "high", "pmode": "bypassPermissions",
        "cwd": str(tmp_path), "run_dir": "/state/run ; x", "worker_token_file": "/private/token ; x",
        "kickoff": str(kickoff),
    }
    path = tmp_path / "config"; path.write_text(json.dumps(config))
    monkeypatch.setattr(handoff_launcher, "_process_env", lambda: {"PATH": "/bin", "BASE": "yes"})
    monkeypatch.setattr(handoff_launcher.shutil, "which", lambda *args, **kwargs: "/bin/claude")
    captured = {}
    monkeypatch.setattr(handoff_launcher.os, "chdir", lambda value: captured.setdefault("cwd", value))
    monkeypatch.setattr(handoff_launcher.os, "execvpe", lambda executable, argv, env: captured.update(executable=executable, argv=argv, env=env))
    handoff_launcher.exec_from_config(str(path))
    assert captured["argv"][-1] == "literal prompt"
    assert captured["env"]["HANDOFF_RUN_DIR"] == "/state/run ; x"
    assert captured["env"]["HANDOFF_WORKER_TOKEN_FILE"] == "/private/token ; x"


def test_private_wrapper_sets_kimi_effort_environment(tmp_path, monkeypatch):
    kickoff = tmp_path / "kickoff"
    kickoff.write_text("literal prompt")
    config = {
        "agent": "kimi", "model": "kimi-code/k3", "effort": "max", "pmode": "bypassPermissions",
        "cwd": str(tmp_path), "run_dir": "/state/run", "worker_token_file": "/private/token",
        "kickoff": str(kickoff),
    }
    path = tmp_path / "config"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(handoff_launcher, "_process_env", lambda: {"PATH": "/bin"})
    monkeypatch.setattr(handoff_launcher.shutil, "which", lambda *args, **kwargs: "/bin/kimi")
    captured = {}
    monkeypatch.setattr(handoff_launcher.os, "chdir", lambda value: None)
    monkeypatch.setattr(
        handoff_launcher.os,
        "execvpe",
        lambda executable, argv, env: captured.update(executable=executable, argv=argv, env=env),
    )

    handoff_launcher.exec_from_config(str(path))

    assert captured["env"]["KIMI_MODEL_THINKING_EFFORT"] == "max"


def test_tmux_launch_and_probe_use_literal_argv_and_reject_stuck_tui(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, check=True):
        calls.append(argv)
        if argv[1] == "has-session": return Completed(returncode=1 if len(calls) == 1 else 0)
        if argv[1] == "list-panes": return Completed(stdout="0\t123\tzsh\n")
        if argv[0] == "ps": return Completed(stdout="folder-trust-ui")
        return Completed()

    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    adapter = handoff_launcher.TmuxAdapter("tmux")
    command = "exec '/private/wrapper ; x' '/private/config $(x)'"
    handle = adapter.launch("name ; x", tmp_path, command)
    assert handle == "name ; x"
    assert ["tmux", "send-keys", "-t", "name ; x", "-l", command] in calls
    assert not adapter.probe(handle, "codex")


def test_tmux_probe_accepts_expected_agent_process(monkeypatch):
    def fake_run(argv, check=True):
        if argv[1] == "has-session": return Completed()
        if argv[1] == "list-panes": return Completed(stdout="0\t123\tnode\n")
        if argv[0] == "ps": return Completed(stdout="node /private/codex/bin/codex.js")
        return Completed()

    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    assert handoff_launcher.TmuxAdapter().probe("worker", "codex")


def test_cmux_probe_requires_surface_and_expected_process(monkeypatch):
    handle = "123e4567-e89b-42d3-a456-426614174000"
    outputs = {
        "list-pane-surfaces": Completed(stdout=f"surface {handle}\n"),
        "top": Completed(stdout=f"{handle}\tpython\tclaude --model opus\n"),
    }
    calls = []

    def fake_run(argv, check=True):
        calls.append(argv)
        command = "list-pane-surfaces" if "list-pane-surfaces" in argv else "top"
        return outputs[command]

    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    adapter = handoff_launcher.CmuxAdapter("cmux")
    assert adapter.probe(handle, "claude")
    assert ["cmux", "--id-format", "both", "list-pane-surfaces"] in calls
    assert [
        "cmux", "--id-format", "both", "top", "--all", "--processes",
        "--flat", "--format", "tsv",
    ] in calls
    outputs["top"] = Completed(stdout=f"{handle}\tzsh\tfolder trust\n")
    assert not adapter.probe(handle, "claude")


def test_cmux_launch_uses_the_inherited_orchestrator_workspace(monkeypatch, tmp_path):
    inherited_workspace = "CFE4746A-1D2E-4AB9-AA16-C4EFC3AA3336"
    handle = "123e4567-e89b-42d3-a456-426614174000"
    calls = []

    def fake_run(argv, check=True):
        calls.append(argv)
        if "new-surface" in argv:
            return Completed(stdout=f"surface {handle}\n")
        return Completed()

    monkeypatch.setenv("CMUX_WORKSPACE_ID", inherited_workspace)
    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    adapter = handoff_launcher.CmuxAdapter("cmux")

    assert adapter.launch("worker", tmp_path, "exec /private/wrapper") == handle
    adapter.send_literal(handle, "Read the kickoff")

    assert [
        "cmux", "--id-format", "both", "new-surface", "--type", "terminal",
        "--workspace", inherited_workspace, "--focus", "false",
    ] in calls
    assert [
        "cmux", "send", "--workspace", inherited_workspace, "--surface", handle,
        "exec /private/wrapper",
    ] in calls
    assert [
        "cmux", "send", "--workspace", inherited_workspace, "--surface", handle,
        "Read the kickoff",
    ] in calls
    assert ["cmux", "current-workspace"] not in calls


def test_cmux_workspace_notification_omits_an_unwritable_surface(monkeypatch):
    calls = []

    def fake_run(argv, check=True):
        calls.append(argv)
        return Completed()

    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    adapter = handoff_launcher.CmuxAdapter("cmux")
    adapter.workspace = "workspace:9"

    adapter.notify(
        None,
        title="Handoff orchestrator pending",
        body="Worker updates are ready for orchestrator review.",
    )

    assert calls == [[
        "cmux", "notify", "--title", "Handoff orchestrator pending", "--body",
        "Worker updates are ready for orchestrator review.", "--workspace", "workspace:9",
    ]]


def test_cmux_orchestrator_doorbell_uses_visible_notification_not_terminal_input(monkeypatch):
    calls = []

    def fake_run(argv, check=True):
        calls.append(argv)
        return Completed()

    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    adapter = handoff_launcher.CmuxAdapter("cmux")
    adapter.workspace = "workspace:9"

    adapter.orchestrator_doorbell(
        "surface-uuid", "b071b964-8bc9-4af3-bbe7-46d6c36e27f9",
    )

    assert calls == [[
        "cmux", "notify", "--title", "Handoff orchestrator pending",
        "--body",
        "Check handoff orchestrator b071b964-8bc9-4af3-bbe7-46d6c36e27f9; "
        "pending worker outbox events are recorded.",
        "--workspace", "workspace:9", "--surface", "surface-uuid",
    ]]


def test_cmux_orchestrator_doorbell_input_requires_visible_echo(monkeypatch):
    body = (
        "Check handoff orchestrator b071b964-8bc9-4af3-bbe7-46d6c36e27f9; "
        "pending worker outbox events are recorded."
    )
    filler = "\n".join(f"transcript line {index}" for index in range(12))
    screens = {"value": f"> {body}\n{filler}\n"}
    calls = []

    def fake_run(argv, check=True):
        calls.append(argv)
        if argv[1] == "read-screen":
            return Completed(stdout=screens["value"])
        return Completed()

    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    monkeypatch.setattr(handoff_launcher.time, "sleep", lambda value: None)
    adapter = handoff_launcher.CmuxAdapter("cmux")
    adapter.workspace = "workspace:9"

    confirmed = adapter.orchestrator_doorbell_input(
        "surface-uuid", "b071b964-8bc9-4af3-bbe7-46d6c36e27f9",
    )
    assert confirmed is True
    assert [
        "cmux", "send", "--workspace", "workspace:9", "--surface", "surface-uuid", body,
    ] in calls

    # A code-mode surface accepts the write but never echoes the text.
    screens["value"] = "desktop surface without an input transcript\n"
    dropped = adapter.orchestrator_doorbell_input(
        "surface-uuid", "b071b964-8bc9-4af3-bbe7-46d6c36e27f9",
    )
    assert dropped is False


def test_tmux_orchestrator_doorbell_retains_terminal_input(monkeypatch):
    calls = []

    def fake_run(argv, check=True):
        calls.append(argv)
        return Completed()

    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    monkeypatch.setattr(handoff_launcher.time, "sleep", lambda value: None)
    adapter = handoff_launcher.TmuxAdapter("tmux")

    adapter.orchestrator_doorbell(
        "session:3.7", "b071b964-8bc9-4af3-bbe7-46d6c36e27f9",
    )

    body = (
        "Check handoff orchestrator b071b964-8bc9-4af3-bbe7-46d6c36e27f9; "
        "pending worker outbox events are recorded."
    )
    assert calls == [
        ["tmux", "send-keys", "-t", "session:3.7", "-l", body],
        ["tmux", "send-keys", "-t", "session:3.7", "Enter"],
        ["tmux", "capture-pane", "-p", "-t", "session:3.7", "-S", "-2000"],
    ]


def test_tmux_doorbell_retries_submit_when_text_sits_in_composer(monkeypatch):
    calls = []
    body = "Check handoff run 7481a493-2f1d-498e-a728-a4144990259e; inbox now through seq 4."

    def fake_run(argv, check=True):
        calls.append(argv)
        if argv[1] == "capture-pane":
            # First capture: text still pending in the composer; second: gone.
            pending = sum(1 for call in calls if call[1] == "capture-pane") == 1
            return Completed(stdout=f"transcript\n> {body}\n" if pending else "transcript\n> \n")
        return Completed()

    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    monkeypatch.setattr(handoff_launcher.time, "sleep", lambda value: None)
    adapter = handoff_launcher.TmuxAdapter("tmux")

    adapter.doorbell("worker", "7481a493-2f1d-498e-a728-a4144990259e", 4)

    assert calls == [
        ["tmux", "send-keys", "-t", "worker", "-l", body],
        ["tmux", "send-keys", "-t", "worker", "Enter"],
        ["tmux", "capture-pane", "-p", "-t", "worker", "-S", "-2000"],
        ["tmux", "send-keys", "-t", "worker", "Enter"],
    ]


def test_wait_ready_needs_live_agent_and_semantic_revision(monkeypatch):
    class Adapter:
        def __init__(self): self.probes = iter([False, True, True])
        def probe(self, handle, expected): return next(self.probes)

    revisions = iter([1, 2])
    monkeypatch.setattr(handoff_launcher.handoff, "status", lambda run_dir: {"revision": next(revisions)})
    monkeypatch.setattr(handoff_launcher.time, "sleep", lambda value: None)
    assert handoff_launcher.wait_ready(Adapter(), "h", "codex", Path("/run"), timeout=1, poll_interval=0)


def test_agent_defaults_match_handoff_policy():
    assert handoff_launcher.AGENT_DEFAULTS == {
        "claude": ("opus", "high"),
        "codex": ("gpt-5.6-terra", "xhigh"),
        "kimi": ("kimi-code/k3", "max"),
    }


def test_kimi_bootstrap_sends_resolved_kickoff_path_after_process_probe(tmp_path, monkeypatch):
    class Adapter:
        def __init__(self):
            self.sent = []

        def capture(self, handle):
            return "Welcome to Kimi Code!\n│ > │\n"

        def send_literal(self, handle, text):
            self.sent.append((handle, text))
            return True

    adapter = Adapter()
    monkeypatch.setattr(handoff_launcher, "wait_process", lambda *args, **kwargs: True)

    run_dir = tmp_path / "run with spaces"
    assert handoff_launcher.bootstrap_kimi(
        adapter, "surface", run_dir=run_dir, timeout=1,
    )
    assert adapter.sent == [(
        "surface",
        f'Read the handoff kickoff from "{run_dir}/kickoff.md" and follow it completely.',
    )]
    assert "HANDOFF_RUN_DIR" not in adapter.sent[0][1]
    assert "$" not in adapter.sent[0][1]


def test_kimi_bootstrap_waits_for_input_widget_instead_of_process_alone(tmp_path, monkeypatch):
    class Adapter:
        def __init__(self):
            self.captures = iter([
                "starting Kimi\n",
                "Welcome to Kimi Code!\n│ > │\n",
            ])
            self.sent = []

        def capture(self, handle):
            return next(self.captures)

        def send_literal(self, handle, text):
            self.sent.append((handle, text))
            return True

    adapter = Adapter()
    monkeypatch.setattr(handoff_launcher, "wait_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(handoff_launcher.time, "sleep", lambda value: None)

    run_dir = tmp_path / "run"
    assert handoff_launcher.bootstrap_kimi(
        adapter, "surface", run_dir=run_dir, timeout=1,
    )
    assert adapter.sent == [(
        "surface",
        f'Read the handoff kickoff from "{run_dir}/kickoff.md" and follow it completely.',
    )]


def test_tmux_kimi_literal_uses_line_feed_to_submit(monkeypatch):
    calls = []
    captures = iter([
        Completed(stdout="Welcome to Kimi Code!\n│ > │\n"),
        Completed(stdout="Welcome to Kimi Code!\n✨ fixed kickoff\n│ > │\n"),
    ])

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "capture-pane":
            return next(captures)
        return Completed()

    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    monkeypatch.setattr(handoff_launcher.time, "sleep", lambda value: None)

    handoff_launcher.TmuxAdapter("tmux").send_literal("worker", "fixed kickoff")

    assert calls == [
        ["tmux", "capture-pane", "-p", "-t", "worker", "-S", "-2000"],
        ["tmux", "send-keys", "-t", "worker", "-l", "fixed kickoff"],
        ["tmux", "send-keys", "-t", "worker", "C-j"],
        ["tmux", "capture-pane", "-p", "-t", "worker", "-S", "-2000"],
    ]


def test_cmux_kimi_literal_falls_back_to_enter_when_control_j_does_not_submit(monkeypatch):
    calls = []
    captures = iter([
        Completed(stdout="Welcome to Kimi Code!\n│ > │\n"),
        Completed(stdout="Welcome to Kimi Code!\n│ > fixed kickoff │\n"),
        Completed(stdout="Welcome to Kimi Code!\n✨ fixed kickoff\n│ > │\n"),
    ])

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "read-screen" in argv:
            return next(captures)
        return Completed()

    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    monkeypatch.setattr(handoff_launcher.time, "sleep", lambda value: None)
    adapter = handoff_launcher.CmuxAdapter("cmux")
    adapter.workspace = "workspace:2"

    assert adapter.send_literal("surface-id", "fixed kickoff")

    assert calls == [
        [
            "cmux", "read-screen", "--workspace", "workspace:2",
            "--surface", "surface-id", "--scrollback",
        ],
        [
            "cmux", "send", "--workspace", "workspace:2",
            "--surface", "surface-id", "fixed kickoff",
        ],
        [
            "cmux", "send-key", "--workspace", "workspace:2",
            "--surface", "surface-id", "ctrl+j",
        ],
        [
            "cmux", "read-screen", "--workspace", "workspace:2",
            "--surface", "surface-id", "--scrollback",
        ],
        [
            "cmux", "send-key", "--workspace", "workspace:2",
            "--surface", "surface-id", "enter",
        ],
        [
            "cmux", "read-screen", "--workspace", "workspace:2",
            "--surface", "surface-id", "--scrollback",
        ],
    ]


def test_cmux_kimi_literal_reports_failure_when_both_submit_keys_leave_prompt_pending(monkeypatch):
    def fake_run(argv, **kwargs):
        if "read-screen" in argv:
            return Completed(stdout="│ > fixed kickoff │\n")
        return Completed()

    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    monkeypatch.setattr(handoff_launcher.time, "sleep", lambda value: None)
    adapter = handoff_launcher.CmuxAdapter("cmux")
    adapter.workspace = "workspace:2"

    assert not adapter.send_literal("surface-id", "fixed kickoff")


def test_cmux_kimi_literal_rejects_stale_instruction_in_shell_scrollback(monkeypatch):
    instruction = 'Read the handoff kickoff from "/state/run/kickoff.md" and follow it completely.'
    stale_screen = """\
Read the handoff kickoff from HANDOFF_RUN_DIR/kickoff.md and follow it completely.
Welcome to Kimi Code!
│ > │
"""

    def fake_run(argv, **kwargs):
        if "read-screen" in argv:
            return Completed(stdout=stale_screen)
        return Completed()

    monkeypatch.setattr(handoff_launcher, "_run", fake_run)
    monkeypatch.setattr(handoff_launcher.time, "sleep", lambda value: None)
    adapter = handoff_launcher.CmuxAdapter("cmux")
    adapter.workspace = "workspace:2"

    assert not adapter.send_literal("surface-id", instruction)


def test_kimi_acceptance_rejects_new_instruction_echoed_before_tui_banner():
    instruction = 'Read the handoff kickoff from "/state/run/kickoff.md" and follow it completely.'
    screen = f"""\
{instruction}
Welcome to Kimi Code!
│ > │
"""

    assert not handoff_launcher._kimi_instruction_accepted(
        screen,
        instruction,
        previous_count=0,
    )


def test_kimi_acceptance_handles_instruction_wrapped_across_terminal_rows():
    instruction = 'Read the handoff kickoff from "/state/runs/123/kickoff.md" and follow it completely.'
    screen = """\
Welcome to Kimi Code!
✨ Read the handoff kickoff from
   "/state/ru
   ns/123/kickoff.md" and follow it completely.
│ > │
"""

    assert handoff_launcher._kimi_instruction_accepted(
        screen,
        instruction,
        previous_count=0,
    )


def test_kimi_acceptance_rejects_wrapped_instruction_still_in_input_widget():
    instruction = 'Read the handoff kickoff from "/state/runs/123/kickoff.md" and follow it completely.'
    screen = """\
Welcome to Kimi Code!
│ > Read the handoff kickoff from                                              │
│   "/state/runs/123/kickoff.md" and follow it completely.                   │
"""

    assert not handoff_launcher._kimi_instruction_accepted(
        screen,
        instruction,
        previous_count=0,
    )


def test_kimi_bootstrap_shares_timeout_between_process_and_input_ready(tmp_path, monkeypatch):
    observed = {}

    class Adapter:
        def send_literal(self, handle, text):
            return True

    def fake_wait_process(adapter, handle, expected_agent, *, timeout):
        observed["process_timeout"] = timeout
        return True

    def fake_wait_input(adapter, handle, *, timeout):
        observed["input_timeout"] = timeout
        return True

    times = iter([10.0, 25.0])
    monkeypatch.setattr(handoff_launcher.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(handoff_launcher, "wait_process", fake_wait_process)
    monkeypatch.setattr(handoff_launcher, "wait_kimi_input_ready", fake_wait_input)

    assert handoff_launcher.bootstrap_kimi(
        Adapter(), "surface", run_dir=tmp_path, timeout=120,
    )
    assert observed == {"process_timeout": 120, "input_timeout": 105.0}


def test_kimi_launch_passes_full_startup_timeout(tmp_path, monkeypatch):
    class Adapter:
        def launch(self, name, cwd, terminal_command, workspace):
            return "surface-handle"

        def rescue_command(self, handle):
            return "capture"

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kickoff = tmp_path / "kickoff"
    kickoff.write_text("lookup")
    private = tmp_path / "private"
    observed = {}
    monkeypatch.setenv("HANDOFF_CREDENTIAL_DIR", str(private))
    monkeypatch.setattr(handoff_launcher, "CmuxAdapter", lambda binary: Adapter())

    def fake_bootstrap(adapter, handle, *, run_dir, timeout):
        observed["timeout"] = timeout
        observed["run_dir"] = run_dir
        return False

    monkeypatch.setattr(handoff_launcher, "bootstrap_kimi", fake_bootstrap)

    result = handoff_launcher.launch(
        name="weather", kickoff=kickoff, cwd=workspace, agent="kimi", backend="cmux",
        state_root=tmp_path / "state", readiness_timeout=120,
    )

    assert observed["timeout"] == 120
    assert observed["run_dir"] == Path(result["run_dir"])
    assert result["kickoff_sent"] is False
    assert result["rescue_command"] == "capture"


def test_launch_only_returns_without_readiness_wait_and_releases_orchestrator(tmp_path, monkeypatch):
    class Adapter:
        def launch(self, name, cwd, terminal_command, workspace):
            return "worker-handle"

        def rescue_command(self, handle):
            return "capture"

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kickoff = tmp_path / "kickoff"
    kickoff.write_text("small task")
    private = tmp_path / "private"
    monkeypatch.setenv("HANDOFF_CREDENTIAL_DIR", str(private))
    monkeypatch.setattr(handoff_launcher, "TmuxAdapter", Adapter)
    monkeypatch.setattr(
        handoff_launcher,
        "wait_ready",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("launch-only must not wait")),
    )

    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    result = handoff_launcher.launch(
        name="fast", kickoff=kickoff, cwd=workspace, agent="codex", backend="tmux",
        state_root=tmp_path / "state", orchestrator_id=orchestrator_id,
    )

    assert result["worker_ready"] is None
    assert result["orchestrator_released"] is True
    assert result["kickoff_sent"] is True
    assert result["registry_recorded"] is True
    registered = handoff_registry.resolve(result["run_id"])
    assert registered["credential_dir"] == str(private)
    assert registered["handle"] == "worker-handle"
    assert registered["orchestrator_id"] == orchestrator_id
    assert (private / "orchestrator.token").is_file()
    control = handoff_launcher.handoff.control_show(Path(result["run_dir"]))
    assert handoff_launcher.handoff._parse_time(control["orchestrator_lease_expires_at"]) <= handoff_launcher.handoff._now()


def test_managed_launch_uses_exact_private_directory_and_retains_lease(tmp_path, monkeypatch):
    class Adapter:
        def launch(self, name, cwd, terminal_command, workspace):
            return "worker-handle"

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kickoff = tmp_path / "kickoff"
    kickoff.write_text("managed task")
    private = tmp_path / "private"
    monkeypatch.setenv("HANDOFF_CREDENTIAL_DIR", str(private))
    monkeypatch.setattr(handoff_launcher, "TmuxAdapter", Adapter)

    result = handoff_launcher.launch(
        name="managed", kickoff=kickoff, cwd=workspace, agent="claude", backend="tmux",
        state_root=tmp_path / "state", retain_orchestrator=True,
    )

    assert result["orchestrator_released"] is False
    assert {path.name for path in private.iterdir()} >= {
        "orchestrator.token", "recovery.token", "worker.token", "launch_worker.py", "launch.json",
    }


def test_backend_autoselection_prefers_reachable_cmux_from_cmux_session(tmp_path, monkeypatch):
    cmux = tmp_path / "cmux"
    cmux.write_text("")
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace:parent")
    monkeypatch.setattr(handoff_launcher, "_run", lambda argv, check=True: Completed())
    monkeypatch.setattr(
        handoff_launcher.shutil,
        "which",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("tmux fallback should not be probed")),
    )

    assert handoff_launcher._select_backend(None, str(cmux)) == "cmux"


def test_backend_autoselection_uses_tmux_outside_cmux_even_when_cmux_is_reachable(tmp_path, monkeypatch):
    cmux = tmp_path / "cmux"
    cmux.write_text("")
    monkeypatch.delenv("CMUX_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(
        handoff_launcher,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cmux must not be probed outside a cmux orchestrator session")
        ),
    )
    monkeypatch.setattr(handoff_launcher.shutil, "which", lambda *args, **kwargs: "/opt/homebrew/bin/tmux")

    assert handoff_launcher._select_backend(None, str(cmux)) == "tmux"


def test_backend_autoselection_falls_back_to_tmux_when_cmux_is_unreachable(tmp_path, monkeypatch):
    cmux = tmp_path / "cmux"
    cmux.write_text("")
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace:parent")
    monkeypatch.setattr(
        handoff_launcher,
        "_run",
        lambda argv, check=True: Completed(returncode=1, stderr="socket unavailable"),
    )
    monkeypatch.setattr(handoff_launcher.shutil, "which", lambda *args, **kwargs: "/opt/homebrew/bin/tmux")

    assert handoff_launcher._select_backend(None, str(cmux)) == "tmux"


def test_private_launch_boundary_has_restrictive_modes_and_only_fixed_paths(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    wrapper, config = handoff_launcher._private_launch_files(private, {
        "path": "literal ; $(touch nope)",
    })
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert wrapper.read_text().splitlines()[0] == f"#!{handoff_launcher.sys.executable}"
    assert "from agents.orchestration import handoff_launcher" in wrapper.read_text()
    terminal = f"exec {handoff_launcher.shlex.quote(str(wrapper))} {handoff_launcher.shlex.quote(str(config))}"
    assert "literal" not in terminal and "touch" not in terminal


def test_receive_remote_request_materializes_private_kickoff_and_uses_ssh_tmux(tmp_path, monkeypatch):
    captured = {}

    def fake_launch(**kwargs):
        captured.update(kwargs)
        captured["kickoff_text"] = kwargs["kickoff"].read_text()
        captured["goal_text"] = Path(str(kwargs["kickoff"]) + ".goal").read_text()
        return {
            "run_dir": "/remote/state/run", "handle": "worker", "transport": "ssh_tmux",
        }

    monkeypatch.setattr(handoff_launcher, "launch", fake_launch)
    request = {
        "name": "remote-worker",
        "kickoff_b64": handoff_launcher.base64.b64encode(b"literal $(touch NOPE)").decode(),
        "goal_b64": handoff_launcher.base64.b64encode(b"/goal finish the task").decode(),
        "cwd": str(tmp_path),
        "agent": "claude",
        "model": None,
        "effort": None,
        "pmode": "bypassPermissions",
        "inputs": ["docs/spec.md"],
        "state_root": str(tmp_path / "state"),
        "run_dir": None,
        "readiness_timeout": 90,
        "confirm_ready": False,
        "retain_coordinator": True,
        "credential_dir": str(tmp_path / "credentials"),
    }

    result = handoff_launcher.receive_remote_request(request)

    assert result["handle"] == "worker"
    assert captured["backend"] == "tmux"
    assert captured["recorded_transport"] == "ssh_tmux"
    assert captured["credential_dir"] == tmp_path / "credentials"
    assert captured["kickoff_text"] == "literal $(touch NOPE)"
    assert captured["goal_text"] == "/goal finish the task"


def test_launch_remote_sends_json_over_stdin_and_returns_remote_uri(tmp_path, monkeypatch):
    kickoff = tmp_path / "kickoff ; weird.md"
    kickoff.write_text("literal $(touch NOPE)")
    captured = {}

    def fake_ssh(host, remote_argv, *, stdin, timeout):
        captured.update(host=host, remote_argv=remote_argv, payload=json.loads(stdin), timeout=timeout)
        return Completed(stdout=json.dumps({
            "run_dir": "/home/opc/.local/state/run-id",
            "transport": "ssh_tmux",
            "session_transport": "tmux",
            "handle": "remote-worker",
            "agent": "claude",
            "orchestrator_released": True,
            "rescue_command": None,
        }) + "\n")

    monkeypatch.setattr(handoff_launcher, "_run_ssh", fake_ssh)
    result = handoff_launcher.launch_remote(
        host="oci-box", remote_python="/home/opc/miniforge3/envs/ml/bin/python",
        name="remote-worker", kickoff=kickoff,
        remote_cwd=Path("/home/opc/projects/investment"), agent="claude",
    )

    assert captured["host"] == "oci-box"
    assert captured["remote_argv"][-1] == "--receive-remote-request"
    assert handoff_launcher.base64.b64decode(captured["payload"]["kickoff_b64"]) == b"literal $(touch NOPE)"
    assert captured["payload"]["cwd"] == "/home/opc/projects/investment"
    assert result["run_dir"] == "ssh://oci-box/home/opc/.local/state/run-id"
    assert result["remote_run_dir"] == "/home/opc/.local/state/run-id"
    assert result["handle"] == "ssh://oci-box/tmux/remote-worker"
    assert result["remote_handle"] == "remote-worker"
    assert result["registry_recorded"] is True
    registered = handoff_registry.resolve(result["run_id"])
    assert registered["host"] == "oci-box"
    assert registered["credential_dir"] is None


def test_remote_codex_automatically_rescues_exact_folder_trust_dialog(tmp_path, monkeypatch):
    kickoff = tmp_path / "kickoff"
    kickoff.write_text("task")
    calls = []
    trust_screen = """\
> You are in /srv/repo

  Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of prompt injection.

› 1. Yes, continue
  2. No, quit

  Press enter to continue
"""

    def fake_ssh(host, remote_argv, *, stdin, timeout):
        calls.append(remote_argv)
        if remote_argv[-1] == "--receive-remote-request":
            payload = json.loads(stdin)
            assert payload["confirm_ready"] is True
            assert payload["readiness_timeout"] == 30.0
            return Completed(stdout=json.dumps({
                "run_dir": "/remote/state/run-id",
                "handle": "remote-codex",
                "agent": "codex",
                "worker_ready": False,
                "rescue_command": "capture",
            }) + "\n")
        if remote_argv[:2] == ["sh", "-c"]:
            return Completed(stdout="node /usr/local/bin/codex -m gpt-5.6-terra\n")
        if remote_argv[:2] == ["tmux", "capture-pane"]:
            if "-S" in remote_argv:
                return Completed(stdout=trust_screen)
            return Completed(stdout="Codex is reading the handoff kickoff.\n")
        if remote_argv[:2] == ["tmux", "send-keys"]:
            assert remote_argv[-1] == "C-m"
            return Completed()
        raise AssertionError(remote_argv)

    monkeypatch.setattr(handoff_launcher, "_run_ssh", fake_ssh)
    monkeypatch.setattr(handoff_launcher.time, "sleep", lambda value: None)

    result = handoff_launcher.launch_remote(
        host="oci-box", remote_python="python3", name="remote-codex",
        kickoff=kickoff, remote_cwd=Path("/srv/repo"), agent="codex",
    )

    assert result["folder_trust_rescued"] is True
    assert "startup_unconfirmed" not in result
    assert "rescue_command" not in result
    assert ["tmux", "send-keys", "-t", "remote-codex", "C-m"] in calls


def test_remote_codex_leaves_nonmatching_startup_unconfirmed(tmp_path, monkeypatch):
    kickoff = tmp_path / "kickoff"
    kickoff.write_text("task")
    calls = []

    def fake_ssh(host, remote_argv, *, stdin, timeout):
        calls.append(remote_argv)
        if remote_argv[-1] == "--receive-remote-request":
            return Completed(stdout=json.dumps({
                "run_dir": "/remote/state/run-id",
                "handle": "remote-codex",
                "agent": "codex",
                "worker_ready": False,
                "rescue_command": "capture",
            }) + "\n")
        if remote_argv[:2] == ["sh", "-c"]:
            return Completed(stdout="node /usr/local/bin/codex -m gpt-5.6-terra\n")
        if remote_argv[:2] == ["tmux", "capture-pane"]:
            return Completed(stdout="""\
> You are in /srv/other-repo
Do you trust the contents of this directory?
1. Yes, continue
2. No, quit
Press enter to continue
""")
        raise AssertionError(remote_argv)

    monkeypatch.setattr(handoff_launcher, "_run_ssh", fake_ssh)

    result = handoff_launcher.launch_remote(
        host="oci-box", remote_python="python3", name="remote-codex",
        kickoff=kickoff, remote_cwd=Path("/srv/repo"), agent="codex",
    )

    assert result["folder_trust_rescued"] is False
    assert result["startup_unconfirmed"] is True
    assert result["rescue_command"] == (
        "ssh oci-box 'tmux capture-pane -p -t remote-codex -S -2000'"
    )
    assert not any(call[:2] == ["tmux", "send-keys"] for call in calls)


def test_remote_host_rejects_ssh_option_injection(tmp_path):
    kickoff = tmp_path / "kickoff"
    kickoff.write_text("task")

    try:
        handoff_launcher.launch_remote(
            host="-oProxyCommand=touch_NOPE", remote_python="python3",
            name="worker", kickoff=kickoff, remote_cwd=Path("/srv/repo"),
        )
    except handoff_launcher.handoff.HandoffError as exc:
        assert exc.exit_code == 2
    else:
        raise AssertionError("SSH option injection host must be rejected")


def test_managed_remote_launch_requires_known_remote_credential_directory(tmp_path):
    kickoff = tmp_path / "kickoff"
    kickoff.write_text("task")

    try:
        handoff_launcher.launch_remote(
            host="oci-box", remote_python="python3", name="worker",
            kickoff=kickoff, remote_cwd=Path("/srv/repo"), retain_orchestrator=True,
        )
    except handoff_launcher.handoff.HandoffError as exc:
        assert "HANDOFF_REMOTE_CREDENTIAL_DIR" in str(exc)
    else:
        raise AssertionError("managed remote launch without a known credential directory must fail")
