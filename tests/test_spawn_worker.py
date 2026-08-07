import base64
import json
from pathlib import Path

import pytest

from agents.orchestration import handoff
from agents.orchestration import handoff_launcher
from agents.orchestration import spawn_worker


@pytest.fixture(autouse=True)
def isolate_spawn_state(tmp_path, monkeypatch):
    # Every private directory this module creates hangs off HOME, and backend
    # autoselection reads the session's own multiplexer environment: a suite run
    # from inside herdr or cmux would otherwise spawn into the developer's real
    # workspace.  Each test opts back in to exactly what it needs.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(tmp_path / "registry.json"))
    for name in ("HERDR_ENV", "HERDR_WORKSPACE_ID", "HERDR_PANE_ID", "CMUX_WORKSPACE_ID"):
        monkeypatch.delenv(name, raising=False)
    return home


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeAdapter:
    """Records what a real adapter would have typed into the terminal."""

    def __init__(self, handle="w1:pA"):
        self.handle = handle
        self.launched = []
        self.literals = []
        self.screen = ""

    def launch(self, name, cwd, terminal_command, workspace=None):
        self.launched.append((name, str(cwd), terminal_command, workspace))
        return self.handle

    def probe(self, handle, expected_agent):
        return True

    def capture(self, handle):
        return self.screen

    def send_literal(self, handle, text):
        self.literals.append(text)
        return True

    def rescue_command(self, handle):
        return f"read {handle}"


@pytest.fixture
def adapter(monkeypatch):
    fake = FakeAdapter()
    monkeypatch.setattr(
        spawn_worker.handoff_launcher, "build_adapter",
        lambda backend, cmux_binary=None, herdr_binary=None: fake,
    )
    return fake


def write_prompt(tmp_path, text="Rename the widget helper and run the tests."):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(text)
    return prompt


def test_spawn_types_only_two_quoted_paths_and_never_the_prompt(tmp_path, adapter):
    payload = "$(touch SHOULD_NOT_EXIST); 'quoted' $HOME"
    prompt = write_prompt(tmp_path, payload)

    result = spawn_worker.spawn(
        label="rename-widget", prompt=prompt, cwd=tmp_path, agent="pi", backend="tmux",
    )

    (name, cwd, command, workspace) = adapter.launched[0]
    assert name == "rename-widget"
    assert cwd == str(tmp_path.resolve())
    assert workspace is None
    words = command.split()
    assert words[0] == "exec" and len(words) == 3
    assert words[1].endswith("launch_worker.py") and words[2].endswith("launch.json")
    # The prompt reaches the agent as argv from inside the wrapper; no fragment
    # of it may appear in text a shell will interpret.
    assert payload not in command
    assert result["handle"] == "w1:pA"
    assert result["prompt_path"] == str(prompt.resolve())
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


def test_spawn_writes_no_handoff_protocol_state(tmp_path, adapter, isolate_spawn_state):
    spawn_worker.spawn(
        label="probe", prompt=write_prompt(tmp_path), cwd=tmp_path, agent="pi", backend="tmux",
    )

    # The load-bearing promise of this mode: no run directory, no credentials,
    # no registry entry, nothing to clean up later.
    assert not (isolate_spawn_state / ".local/state/agents/handoff").exists()
    assert not Path(tmp_path / "registry.json").exists()


@pytest.mark.parametrize(
    ("agent", "model", "effort"),
    [
        ("claude", "opus", "high"),
        ("codex", "gpt-5.6-terra", "xhigh"),
        ("kimi", "kimi-code/k3", "max"),
        ("pi", "deepseek/deepseek-v4-flash", "max"),
    ],
)
def test_spawn_uses_the_launcher_agent_defaults(tmp_path, adapter, monkeypatch, agent, model, effort):
    # Both modes must pin the same model and effort per agent; this fails the
    # moment the lightweight path grows its own copy of the table.
    monkeypatch.setattr(
        spawn_worker.handoff_launcher, "_rescue_local_codex_folder_trust",
        lambda result, **kwargs: result.setdefault("folder_trust_rescued", False),
    )
    monkeypatch.setattr(
        spawn_worker.handoff_launcher, "_rescue_local_claude_folder_trust",
        lambda result, **kwargs: result.setdefault("folder_trust_rescued", False),
    )
    monkeypatch.setattr(spawn_worker, "_bootstrap_kimi", lambda *args, **kwargs: True)

    result = spawn_worker.spawn(
        label="defaults", prompt=write_prompt(tmp_path), cwd=tmp_path,
        agent=agent, backend="tmux",
    )

    assert (result["model"], result["effort"]) == (model, effort)
    assert result["model"] == handoff_launcher.AGENT_DEFAULTS[agent][0]
    assert result["effort"] == handoff_launcher.AGENT_DEFAULTS[agent][1]


def test_spawn_rejects_an_unknown_agent(tmp_path, adapter):
    with pytest.raises(handoff.HandoffError) as excinfo:
        spawn_worker.spawn(
            label="bad", prompt=write_prompt(tmp_path), cwd=tmp_path,
            agent="gemini", backend="tmux",
        )

    # An error that enumerates the valid set, per the agent-native CLI rules.
    assert "claude" in str(excinfo.value) and "pi" in str(excinfo.value)


def test_spawn_autoselects_the_backend_when_none_is_given(tmp_path, adapter, monkeypatch):
    monkeypatch.setattr(
        spawn_worker.handoff_launcher.shutil, "which", lambda *args, **kwargs: "/usr/bin/tmux",
    )

    result = spawn_worker.spawn(
        label="auto", prompt=write_prompt(tmp_path), cwd=tmp_path, agent="pi",
    )

    assert result["backend"] == "tmux"


def test_spawn_places_a_worker_in_an_explicit_workspace(tmp_path, adapter):
    spawn_worker.spawn(
        label="placed", prompt=write_prompt(tmp_path), cwd=tmp_path, agent="pi",
        backend="herdr", workspace="w7",
    )

    assert adapter.launched[0][3] == "w7"


def test_kimi_is_pointed_at_the_prompt_file_rather_than_typed_the_body(tmp_path, adapter, monkeypatch):
    payload = "line one\nline two"
    prompt = write_prompt(tmp_path, payload)
    monkeypatch.setattr(handoff_launcher, "wait_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(handoff_launcher, "wait_kimi_input_ready", lambda *args, **kwargs: True)

    result = spawn_worker.spawn(
        label="kimi-run", prompt=prompt, cwd=tmp_path, agent="kimi", backend="tmux",
    )

    assert result["prompt_sent"] is True
    assert len(adapter.literals) == 1
    assert str(prompt.resolve()) in adapter.literals[0]
    assert payload not in adapter.literals[0]


def test_kimi_reports_a_rescue_command_when_delivery_is_unconfirmed(tmp_path, adapter, monkeypatch):
    monkeypatch.setattr(handoff_launcher, "wait_process", lambda *args, **kwargs: False)

    result = spawn_worker.spawn(
        label="kimi-stuck", prompt=write_prompt(tmp_path), cwd=tmp_path,
        agent="kimi", backend="tmux",
    )

    assert result["prompt_sent"] is False
    assert result["rescue_command"] == "read w1:pA"


def test_claude_startup_clears_its_folder_trust_dialog(tmp_path, adapter, monkeypatch):
    seen = {}

    def fake_rescue(result, *, adapter, handle, cwd):
        seen.update(handle=handle, cwd=cwd)
        result["folder_trust_rescued"] = True

    monkeypatch.setattr(
        spawn_worker.handoff_launcher, "_rescue_local_claude_folder_trust", fake_rescue,
    )

    result = spawn_worker.spawn(
        label="claude-run", prompt=write_prompt(tmp_path), cwd=tmp_path,
        agent="claude", backend="tmux",
    )

    assert seen == {"handle": "w1:pA", "cwd": tmp_path.resolve()}
    assert result["folder_trust_rescued"] is True
    assert result["prompt_sent"] is True


def test_wrapper_exec_leaves_no_handoff_environment_and_discards_launch_files(tmp_path, monkeypatch):
    prompt = write_prompt(tmp_path, "literal prompt")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    config = {
        "agent": "claude", "model": "opus", "effort": "high", "pmode": "bypassPermissions",
        "cwd": str(tmp_path), "kickoff": str(prompt), "private_dir": str(private_dir),
    }
    wrapper, config_path = spawn_worker._write_launch_files(private_dir, config)
    monkeypatch.setattr(spawn_worker.env, "build_env", lambda: {"PATH": "/bin", "BASE": "yes"})
    monkeypatch.setattr(handoff_launcher.shutil, "which", lambda *args, **kwargs: "/bin/claude")
    captured = {}
    monkeypatch.setattr(spawn_worker.os, "chdir", lambda value: captured.setdefault("cwd", value))
    monkeypatch.setattr(
        spawn_worker.os, "execvpe",
        lambda executable, argv, env: captured.update(executable=executable, argv=argv, env=env),
    )

    spawn_worker.exec_from_config(str(config_path))

    assert captured["argv"][-1] == "literal prompt"
    assert captured["cwd"] == str(tmp_path)
    # HANDOFF_RUN_DIR is what tells an agent it is inside a handoff run. A
    # lightweight worker must not believe it is.
    assert "HANDOFF_RUN_DIR" not in captured["env"]
    assert "HANDOFF_WORKER_TOKEN_FILE" not in captured["env"]
    assert not wrapper.exists() and not config_path.exists()
    assert prompt.exists()
    # Caller supplied the prompt, so the private directory has nothing left to
    # hold and leaves no trail behind.
    assert not private_dir.exists()


def test_wrapper_exec_keeps_a_private_directory_holding_a_piped_prompt(tmp_path, monkeypatch):
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    prompt = private_dir / "prompt.md"
    prompt.write_text("piped prompt")
    _, config_path = spawn_worker._write_launch_files(private_dir, {
        "agent": "pi", "model": "deepseek/deepseek-v4-flash", "effort": "max",
        "pmode": "bypassPermissions", "cwd": str(tmp_path),
        "kickoff": str(prompt), "private_dir": str(private_dir),
    })
    monkeypatch.setattr(spawn_worker.env, "build_env", lambda: {"PATH": "/bin"})
    monkeypatch.setattr(handoff_launcher.shutil, "which", lambda *args, **kwargs: "/bin/pi")
    monkeypatch.setattr(spawn_worker.os, "chdir", lambda value: None)
    monkeypatch.setattr(spawn_worker.os, "execvpe", lambda executable, argv, env: None)

    spawn_worker.exec_from_config(str(config_path))

    assert prompt.read_text() == "piped prompt"


def test_wrapper_exec_strips_an_inherited_handoff_run_pointer(tmp_path, monkeypatch):
    # Spawning a lightweight worker from inside a handoff worker is ordinary
    # nested delegation, and build_env() copies os.environ — so the pointers
    # have to be removed, not merely left unset.
    prompt = write_prompt(tmp_path, "task")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    _, config_path = spawn_worker._write_launch_files(private_dir, {
        "agent": "pi", "model": "deepseek/deepseek-v4-flash", "effort": "max",
        "pmode": "bypassPermissions", "cwd": str(tmp_path),
        "kickoff": str(prompt), "private_dir": str(private_dir),
    })
    monkeypatch.setattr(spawn_worker.env, "build_env", lambda: {
        "PATH": "/bin",
        "HANDOFF_RUN_DIR": "/state/someone-elses-run",
        "HANDOFF_WORKER_TOKEN_FILE": "/private/someone-elses.token",
    })
    monkeypatch.setattr(handoff_launcher.shutil, "which", lambda *args, **kwargs: "/bin/pi")
    captured = {}
    monkeypatch.setattr(spawn_worker.os, "chdir", lambda value: None)
    monkeypatch.setattr(
        spawn_worker.os, "execvpe", lambda executable, argv, env: captured.update(env=env),
    )

    spawn_worker.exec_from_config(str(config_path))

    assert "HANDOFF_RUN_DIR" not in captured["env"]
    assert "HANDOFF_WORKER_TOKEN_FILE" not in captured["env"]
    assert captured["env"]["PATH"] == "/bin"


def test_wrapper_exec_sets_kimi_effort_environment(tmp_path, monkeypatch):
    prompt = write_prompt(tmp_path, "task")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    _, config_path = spawn_worker._write_launch_files(private_dir, {
        "agent": "kimi", "model": "kimi-code/k3", "effort": "max",
        "pmode": "bypassPermissions", "cwd": str(tmp_path),
        "kickoff": str(prompt), "private_dir": str(private_dir),
    })
    monkeypatch.setattr(spawn_worker.env, "build_env", lambda: {"PATH": "/bin"})
    monkeypatch.setattr(handoff_launcher.shutil, "which", lambda *args, **kwargs: "/bin/kimi")
    captured = {}
    monkeypatch.setattr(spawn_worker.os, "chdir", lambda value: None)
    monkeypatch.setattr(
        spawn_worker.os, "execvpe",
        lambda executable, argv, env: captured.update(env=env, argv=argv),
    )

    spawn_worker.exec_from_config(str(config_path))

    assert captured["env"]["KIMI_MODEL_THINKING_EFFORT"] == "max"
    assert "task" not in captured["argv"]


def test_wrapper_exec_rejects_a_handoff_protocol_config(tmp_path):
    # The two wrappers are not interchangeable: a protocol config carries a run
    # dir and token this entry point would silently ignore.
    config_path = tmp_path / "launch.json"
    config_path.write_text(json.dumps({
        "agent": "claude", "model": "opus", "effort": "high", "pmode": "bypassPermissions",
        "cwd": str(tmp_path), "run_dir": "/state/run",
        "worker_token_file": "/private/token", "kickoff": str(write_prompt(tmp_path)),
    }))

    with pytest.raises(SystemExit):
        spawn_worker.exec_from_config(str(config_path))


def test_stdin_prompt_lands_in_a_private_directory(tmp_path, monkeypatch, isolate_spawn_state):
    monkeypatch.setattr(spawn_worker.sys, "stdin", type("S", (), {"read": staticmethod(lambda: "do the thing")})())

    prompt, private_dir = spawn_worker._resolve_prompt(Path("-"), "piped")

    assert prompt.read_text() == "do the thing"
    assert private_dir is not None and prompt.parent == private_dir
    assert private_dir.is_relative_to(isolate_spawn_state / ".local/state/agents/spawn")


def test_a_file_prompt_is_used_where_it_lives(tmp_path):
    prompt = write_prompt(tmp_path)

    resolved, private_dir = spawn_worker._resolve_prompt(prompt, "onfile")

    assert resolved == prompt.resolve()
    assert private_dir is None


def test_remote_spawn_sends_the_prompt_as_base64_over_ssh(tmp_path, monkeypatch):
    prompt = write_prompt(tmp_path, "remote task")
    sent = {}

    def fake_run_ssh(host, remote_argv, *, stdin, timeout):
        sent.update(host=host, argv=remote_argv, payload=json.loads(stdin))
        return Completed(stdout=json.dumps({"handle": "w7:p4", "backend": "herdr", "label": "r"}))

    monkeypatch.setattr(handoff_launcher, "_run_ssh", fake_run_ssh)

    result = spawn_worker.spawn_remote(
        host="oci-box", remote_python="/opt/py", label="r", prompt=prompt,
        remote_cwd=Path("/home/opc/projects/foo"), agent="pi",
    )

    assert sent["host"] == "oci-box"
    assert sent["argv"] == [
        "/opt/py", "-m", "agents.orchestration.spawn_worker", "--receive-spawn-request",
    ]
    assert base64.b64decode(sent["payload"]["prompt_b64"]).decode() == "remote task"
    assert sent["payload"]["workspace_label"] == "REMOTE_WORKERS"
    assert set(sent["payload"]) <= spawn_worker.REMOTE_REQUEST_FIELDS
    # The backend the receiver reports is namespaced, so a caller can never
    # mistake a remote handle for a local one.
    assert result["backend"] == "ssh_herdr"
    assert result["host"] == "oci-box"


def test_remote_spawn_names_the_fix_when_the_host_package_is_stale(tmp_path, monkeypatch):
    def fake_run_ssh(host, remote_argv, *, stdin, timeout):
        raise handoff_launcher.AdapterError(
            "remote SSH launch failed (1): No module named agents.orchestration.spawn_worker",
        )

    monkeypatch.setattr(handoff_launcher, "_run_ssh", fake_run_ssh)

    with pytest.raises(handoff_launcher.AdapterError) as excinfo:
        spawn_worker.spawn_remote(
            host="oci-box", remote_python="python3", label="r",
            prompt=write_prompt(tmp_path), remote_cwd=Path("/tmp"),
        )

    assert "update_agents.sh" in str(excinfo.value)


def test_remote_spawn_rejects_an_oversized_prompt(tmp_path, monkeypatch):
    prompt = tmp_path / "big.md"
    prompt.write_bytes(b"x" * (handoff.MAX_KICKOFF + 1))

    with pytest.raises(handoff.HandoffError):
        spawn_worker.spawn_remote(
            host="oci-box", remote_python="python3", label="r",
            prompt=prompt, remote_cwd=Path("/tmp"),
        )


def test_receive_spawn_request_places_the_worker_by_workspace_label(tmp_path, adapter, monkeypatch):
    monkeypatch.setattr(
        handoff_launcher, "_herdr_workspace_by_label",
        lambda label, cwd, binary=handoff_launcher.HERDR_DEFAULT: "w7" if label == "REMOTE_WORKERS" else "w9",
    )

    result = spawn_worker.receive_spawn_request({
        "label": "remote-run",
        "prompt_b64": base64.b64encode(b"remote task").decode(),
        "cwd": str(tmp_path), "agent": "pi", "model": None, "effort": None,
        "pmode": "bypassPermissions", "workspace_label": "REMOTE_WORKERS",
    })

    assert adapter.launched[0][3] == "w7"
    assert result["backend"] == "herdr"
    assert Path(result["prompt_path"]).read_text() == "remote task"


def test_receive_spawn_request_rejects_unknown_fields(tmp_path):
    with pytest.raises(handoff.HandoffError) as excinfo:
        spawn_worker.receive_spawn_request({
            "label": "r", "prompt_b64": base64.b64encode(b"t").decode(),
            "cwd": str(tmp_path), "run_dir": "/state/run",
        })

    assert "run_dir" in str(excinfo.value)


def test_receive_spawn_request_rejects_malformed_base64(tmp_path):
    with pytest.raises(handoff.HandoffError):
        spawn_worker.receive_spawn_request({
            "label": "r", "prompt_b64": "not base64!", "cwd": str(tmp_path),
        })


def test_cli_rejects_remote_combined_with_a_local_backend(tmp_path, capsys):
    exit_code = spawn_worker.main([
        "r", str(write_prompt(tmp_path)), "--remote-host", "oci-box",
        "--remote-cwd", "/tmp", "--backend", "herdr",
    ])

    assert exit_code == 2
    assert "--backend" in capsys.readouterr().err
