import json
import os
import shlex
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest

from agents.orchestration import handoffctl
from agents.orchestration import handoff
from agents.orchestration import handoff_registry
from agents.orchestration import handoff_watcher


PYTHON = "/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python"


def run_cli(*args, input=None):
    return subprocess.run(
        [PYTHON, "-m", "agents.orchestration.handoffctl", *map(str, args)],
        input=input,
        text=True,
        capture_output=True,
    )


def register_tmux_orchestrator(monkeypatch, capsys, state, owner_pid, *extra, command="orchestrator"):
    """Register through the real CLI parser with the tmux probe faked.

    Registration validates the target with ``tmux has-session``, and a fake
    handle like ``orchestrator:0.4`` can never name a real session.  Running
    ``main`` in process keeps the argparse-to-registration path under test
    while the fake answers the probe; subprocess-spawning steps elsewhere in
    the test are unaffected.
    """
    monkeypatch.setattr(
        handoffctl.handoff_launcher,
        "_run",
        lambda argv, check=True: subprocess.CompletedProcess(argv, 0, "", ""),
    )
    assert handoffctl.main([
        command, "register", "--state", str(state),
        "--transport", "tmux", "--target", "orchestrator:0.4",
        "--owner-pid", str(owner_pid), *extra,
    ]) == 0
    return json.loads(capsys.readouterr().out)


def registered_run(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("get the weather", encoding="utf-8")
    private = tmp_path / "credentials"
    initialized = handoff.initialize(
        workspace=workspace,
        kickoff=kickoff,
        harness="claude",
        model="opus",
        effort="low",
        transport="tmux",
        run_dir=tmp_path / "state" / "run-one",
        recovery_token_file=private / "recovery.token",
        orchestrator_token_file=private / "orchestrator.token",
        worker_token_file=private / "worker.token",
    )
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    handoff_registry.register(
        run_id=initialized["run"]["run_id"], name="weather",
        run_dir=initialized["run_dir"], run_uri=initialized["run_dir"],
        host=None, remote_python=None, transport="tmux", session_transport="tmux",
        handle="weather-worker", agent="claude", model="opus", effort="low",
        credential_dir=str(private),
    )
    # These registry fixtures model a live tmux worker unless a test
    # explicitly overrides the transport probe.  A positive nonzero
    # has-session result now means "dead" and is independently reapable.
    monkeypatch.setattr(
        handoffctl.handoff_launcher,
        "_run",
        lambda argv, check=True: subprocess.CompletedProcess(argv, 0, "", ""),
    )
    return {
        **initialized,
        "private": private,
        "worker": (private / "worker.token").read_bytes(),
        "orchestrator": (private / "orchestrator.token").read_bytes(),
    }


def test_cli_init_read_send_and_status(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kickoff = tmp_path / "kickoff"
    kickoff.write_text("task")
    run_dir = tmp_path / "state" / "run"
    private = tmp_path / "private"
    init = run_cli(
        "init", "--workspace", workspace, "--kickoff", kickoff,
        "--harness", "claude", "--model", "opus", "--transport", "tmux",
        "--run-dir", run_dir,
        "--recovery-token-file", private / "recovery",
        "--orchestrator-token-file", private / "orchestrator",
        "--worker-token-file", private / "worker",
    )
    assert init.returncode == 0, init.stderr
    assert json.loads(init.stdout)["run_dir"] == str(run_dir)
    body = tmp_path / "body"
    body.write_text("literal ; $(touch NEVER)")
    sent = run_cli(
        "send", "--run-dir", run_dir, "--type", "steer",
        "--token-file", private / "orchestrator", "--body-file", body,
    )
    assert sent.returncode == 0, sent.stderr
    assert json.loads(sent.stdout)["message"]["body"] == body.read_text()
    read = run_cli("read", "--run-dir", run_dir, "--journal", "inbox")
    assert json.loads(read.stdout)[0]["body"] == body.read_text()
    assert not (tmp_path / "NEVER").exists()


def test_cli_doctor_prints_report_and_exits_five_on_corruption(tmp_path):
    workspace = tmp_path / "w"; workspace.mkdir()
    kickoff = tmp_path / "k"; kickoff.write_text("x")
    run_dir = tmp_path / "run"; private = tmp_path / "private"
    init = run_cli(
        "init", "--workspace", workspace, "--kickoff", kickoff,
        "--harness", "codex", "--model", "m", "--transport", "tmux", "--run-dir", run_dir,
        "--recovery-token-file", private / "r", "--orchestrator-token-file", private / "c",
        "--worker-token-file", private / "w",
    )
    assert init.returncode == 0
    with (run_dir / "outbox.jsonl").open("ab") as stream:
        stream.write(b"partial")
    result = run_cli("doctor", "--run-dir", run_dir)
    assert result.returncode == 5
    assert json.loads(result.stdout)["ok"] is False


def test_cli_ready_publishes_one_idempotent_startup_checkpoint(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kickoff = tmp_path / "kickoff"
    kickoff.write_text("task")
    run_dir = tmp_path / "run"
    private = tmp_path / "private"
    init = run_cli(
        "init", "--workspace", workspace, "--kickoff", kickoff,
        "--harness", "codex", "--model", "m", "--transport", "tmux", "--run-dir", run_dir,
        "--recovery-token-file", private / "recovery", "--orchestrator-token-file", private / "orchestrator",
        "--worker-token-file", private / "worker",
    )
    assert init.returncode == 0, init.stderr

    first = run_cli("ready", "--run-dir", run_dir, "--token-file", private / "worker")
    second = run_cli("ready", "--run-dir", run_dir, "--token-file", private / "worker")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["message"]["message_id"] == second_payload["message"]["message_id"]
    assert first_payload["status"]["state"] == "working"
    assert second_payload["status"]["outbox_seq"] == 1
    outbox = run_cli("read", "--run-dir", run_dir, "--journal", "outbox")
    assert outbox.returncode == 0, outbox.stderr
    assert len(json.loads(outbox.stdout)) == 1


def test_result_emit_notifies_only_after_durable_publication(tmp_path, monkeypatch):
    token_file = tmp_path / "worker.token"
    token_file.write_bytes(b"worker-token")
    events = []
    durable_result = {"status": {"state": "awaiting_review"}}

    def fake_emit(*args, **kwargs):
        events.append("published")
        return durable_result

    monkeypatch.setattr(handoffctl.handoff, "emit", fake_emit)
    monkeypatch.setattr(
        handoffctl,
        "_notify_result_ready",
        lambda run_dir: events.append("notified"),
    )
    args = Namespace(
        command="emit",
        run_dir=Path("/durable/run"),
        token_file=token_file,
        type="result",
        message_id=None,
        reply_to=None,
        body_file=None,
        data_file=None,
        attachments_file=None,
        snapshot_attachments=False,
    )

    assert handoffctl.dispatch(args) is durable_result
    assert events == ["published", "notified"]


def test_cmux_result_notification_targets_worker_surface(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("CMUX_SURFACE_ID", "surface-uuid")
    monkeypatch.setattr(
        handoffctl.handoff,
        "_load_json",
        lambda path, validator: {"worker": {"transport": "cmux"}},
    )
    monkeypatch.setattr(
        handoffctl.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )

    handoffctl._notify_result_ready(tmp_path)

    assert calls == [
        (
            [
                "cmux", "notify", "--surface", "surface-uuid",
                "--title", "Handoff result ready",
                "--body", "Awaiting orchestrator review",
            ],
            {
                "check": False,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "timeout": 5,
            },
        )
    ]


@pytest.mark.parametrize("failure", [OSError("missing cmux"), subprocess.TimeoutExpired("cmux", 5)])
def test_cmux_result_notification_failure_is_ignored(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("CMUX_SURFACE_ID", "surface-uuid")
    monkeypatch.setattr(
        handoffctl.handoff,
        "_load_json",
        lambda path, validator: {"worker": {"transport": "cmux"}},
    )

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(handoffctl.subprocess, "run", fail)

    handoffctl._notify_result_ready(tmp_path)


def test_result_notification_skips_non_cmux_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("CMUX_SURFACE_ID", "surface-uuid")
    monkeypatch.setattr(
        handoffctl.handoff,
        "_load_json",
        lambda path, validator: {"worker": {"transport": "tmux"}},
    )
    monkeypatch.setattr(
        handoffctl.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("tmux run must not invoke cmux notify"),
    )

    handoffctl._notify_result_ready(tmp_path)


def test_fast_dispatch_supersedes_result_rings_doorbell_and_releases(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="ready",
        data={
            "state": "working", "stage": "lookup", "current_activity": "working",
            "inbox_cursor": 0, "commitments": [],
        },
    )
    result = handoff.emit(
        run_dir, run["worker"], type="result", body="old weather",
        data={
            "head": None, "dirty": True, "stage": "complete", "inbox_cursor": 0,
            "verification": [], "commitments": [],
        },
    )
    handoff.control_release(run_dir, run["orchestrator"])
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    dispatched = handoffctl._dispatch_registered("weather", "refresh SF weather")

    assert dispatched["superseded_result"] == result["message"]["message_id"]
    assert dispatched["message"]["type"] == "supersede"
    assert dispatched["doorbell_sent"] is True
    assert dispatched["orchestrator_released"] is True
    assert doorbells == [(
        "weather-worker", run["run"]["run_id"], dispatched["message"]["seq"],
    )]
    assert not list(run["private"].glob("orchestrator-dispatch-*.token"))
    resumed = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="resumed",
        data={
            "state": "working", "stage": "lookup", "current_activity": "refreshing",
            "inbox_cursor": dispatched["message"]["seq"], "commitments": [],
        },
    )
    assert resumed["status"]["state"] == "working"


def test_fast_dispatch_answers_blocking_question(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="ready",
        data={
            "state": "working", "stage": "lookup", "current_activity": "working",
            "inbox_cursor": 0, "commitments": [],
        },
    )
    question = handoff.emit(
        run_dir, run["worker"], type="question", body="Need a decision",
        data={
            "blocking": True, "stage": "lookup", "current_activity": "Waiting",
            "inbox_cursor": 0, "commitments": [],
        },
    )
    handoff.control_release(run_dir, run["orchestrator"])
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    dispatched = handoffctl._dispatch_registered("weather", "use option A")

    assert dispatched["answered_question"] == question["message"]["message_id"]
    assert dispatched["message"]["type"] == "answer"
    assert dispatched["message"]["reply_to"] == question["message"]["message_id"]
    assert dispatched["superseded_result"] is None
    assert dispatched["doorbell_sent"] is True
    assert dispatched["orchestrator_released"] is True
    assert doorbells == [(
        "weather-worker", run["run"]["run_id"], dispatched["message"]["seq"],
    )]
    assert not list(run["private"].glob("orchestrator-dispatch-*.token"))
    resumed = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="resumed",
        data={
            "state": "working", "stage": "lookup", "current_activity": "unblocked",
            "inbox_cursor": dispatched["message"]["seq"], "commitments": [],
        },
    )
    assert resumed["status"]["state"] == "working"
    assert resumed["status"]["blocked_on"] == []


def test_failed_dispatch_releases_ephemeral_orchestrator(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    handoff.emit(
        run_dir, run["worker"], type="error", body="fatal test failure",
        data={
            "fatal": True, "category": "test", "stage": "lookup",
            "inbox_cursor": 0, "commitments": [],
        },
    )
    handoff.control_release(run_dir, run["orchestrator"])

    with pytest.raises(handoff.HandoffError, match="cannot accept a new instruction"):
        handoffctl._dispatch_registered("weather", "do something else")

    control = handoff.control_show(run_dir)
    assert handoff._parse_time(control["orchestrator_lease_expires_at"]) <= handoff._now()
    assert not list(run["private"].glob("orchestrator-dispatch-*.token"))


def _send_args(run_dir, token_file, body_file, *, no_doorbell=False):
    return Namespace(
        command="send",
        run_dir=Path(run_dir),
        token_file=Path(token_file),
        type="steer",
        message_id=None,
        reply_to=None,
        body_file=Path(body_file),
        data_file=None,
        attachments_file=None,
        snapshot_attachments=False,
        no_doorbell=no_doorbell,
    )


def test_send_rings_doorbell_for_registered_run(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    body = tmp_path / "body.txt"
    body.write_text("steer body", encoding="utf-8")
    sent = handoffctl.dispatch(_send_args(run["run_dir"], run["private"] / "orchestrator.token", body))

    assert sent["doorbell_sent"] is True
    assert sent["doorbell_error"] is None
    assert doorbells == [("weather-worker", run["run"]["run_id"], sent["message"]["seq"])]


def test_send_no_doorbell_skips_ring(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            pytest.fail("--no-doorbell must not ring the doorbell")

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    body = tmp_path / "body.txt"
    body.write_text("steer body", encoding="utf-8")
    sent = handoffctl.dispatch(_send_args(
        run["run_dir"], run["private"] / "orchestrator.token", body, no_doorbell=True,
    ))

    assert sent["doorbell_sent"] is False
    assert sent["doorbell_error"] is None


def test_send_unregistered_run_skips_doorbell(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("task", encoding="utf-8")
    private = tmp_path / "credentials"
    initialized = handoff.initialize(
        workspace=workspace, kickoff=kickoff, harness="claude", model="opus",
        effort="low", transport="tmux", run_dir=tmp_path / "state" / "run",
        recovery_token_file=private / "recovery.token",
        orchestrator_token_file=private / "orchestrator.token",
        worker_token_file=private / "worker.token",
    )
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(tmp_path / "registry.json"))

    body = tmp_path / "body.txt"
    body.write_text("steer body", encoding="utf-8")
    sent = handoffctl.dispatch(_send_args(initialized["run_dir"], private / "orchestrator.token", body))

    assert sent["message"]["type"] == "steer"
    assert sent["doorbell_sent"] is False
    assert "not registered" in sent["doorbell_error"]


def test_context_and_watch_use_durable_registered_state(tmp_path, monkeypatch, capsys):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    emitted = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="started",
        data={
            "state": "working", "stage": "lookup", "current_activity": "fetching",
            "inbox_cursor": 0, "commitments": [],
        },
    )

    context = handoffctl._context_registered("weather")
    assert "get the weather" in context["kickoff"]
    assert context["unread_outbox"][0]["message_id"] == emitted["message"]["message_id"]
    args = Namespace(
        run=["weather"], once=True, timeout=None, interval=0.01,
        notify_cmux=False,
    )
    assert handoffctl._watch(args) == 0
    watched = json.loads(capsys.readouterr().out)
    assert watched["event"]["message_id"] == emitted["message"]["message_id"]
    assert handoff_registry.resolve("weather")["observed_outbox_cursor"] == 1
    assert handoff.control_show(run_dir)["outbox_cursor"] == 0


def test_remote_dispatch_uses_one_structured_ssh_request(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    handoff_registry.register(
        run_id="remote-run", name="remote-weather", run_dir="/remote/run",
        run_uri="ssh://oci-box/remote/run", host="oci-box",
        remote_python="/remote/python", transport="ssh_tmux",
        session_transport="tmux", handle="remote-worker", agent="claude",
        model="opus", effort="low", credential_dir=None,
    )
    observed = {}

    def fake_ssh(host, remote_argv, *, stdin, timeout):
        observed.update(host=host, argv=remote_argv, stdin=stdin, timeout=timeout)
        return subprocess.CompletedProcess(
            remote_argv, 0,
            stdout=json.dumps({"message": {"seq": 2}, "doorbell_sent": True}),
            stderr="",
        )

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run_ssh", fake_ssh)

    result = handoffctl._dispatch_registered("remote-weather", "literal ; $(touch NOPE)")

    assert result["doorbell_sent"] is True
    assert observed["host"] == "oci-box"
    assert observed["argv"][-5:] == ["dispatch", "--run", "remote-run", "--body-file", "-"]
    assert observed["stdin"] == "literal ; $(touch NOPE)"


def test_cli_registers_shows_and_enforces_detached_watcher_options(tmp_path, monkeypatch, capsys):
    state = tmp_path / "orchestrator" / "watcher.json"
    value = register_tmux_orchestrator(monkeypatch, capsys, state, os.getpid())
    assert value["target"] == {"handle": "orchestrator:0.4"}

    shown = run_cli("orchestrator", "show", "--state", state)
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == value

    conflict = run_cli(
        "watch", "--orchestrator-state", state, "--once",
    )
    assert conflict.returncode == 2
    assert "cannot be combined" in conflict.stderr


def test_cli_coordinator_alias_still_resolves(tmp_path, monkeypatch, capsys):
    state = tmp_path / "orchestrator" / "watcher.json"
    registered = register_tmux_orchestrator(
        monkeypatch, capsys, state, os.getpid(),
        "--coordinator-id", "b071b964-8bc9-4af3-bbe7-46d6c36e27f9",
        command="coordinator",
    )
    assert (
        registered["orchestrator_id"]
        == "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    )

    shown = run_cli("coordinator", "show", "--state", state)
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == registered

    conflict = run_cli("watch", "--coordinator-state", state, "--once")
    assert conflict.returncode == 2
    assert "cannot be combined" in conflict.stderr


def test_cli_orchestrator_dismiss_resolves_registered_run_and_writes_cursor(
    tmp_path, monkeypatch, capsys,
):
    run = registered_run(tmp_path, monkeypatch)
    state = tmp_path / "orchestrator" / "watcher.json"
    registered = register_tmux_orchestrator(monkeypatch, capsys, state, os.getpid())
    orchestrator_id = registered["orchestrator_id"]
    handoff_registry.adopt(run["run"]["run_id"], orchestrator_id)
    handoff.emit(
        Path(run["run_dir"]),
        run["worker"],
        type="checkpoint",
        body="ordinary progress",
        data={
            "state": "working",
            "stage": "implementation",
            "current_activity": "ordinary progress",
            "inbox_cursor": 0,
            "commitments": [],
        },
    )
    handoff_watcher.poll(
        state,
        notifier=lambda value, coverage, urgency: pytest.fail("working progress must not ring"),
    )

    dismissed = run_cli(
        "orchestrator", "dismiss", "--state", state, "--run", "weather",
    )

    assert dismissed.returncode == 0, dismissed.stderr
    assert json.loads(dismissed.stdout) == {
        "orchestrator_id": orchestrator_id,
        "dismissed": {
            "run_id": run["run"]["run_id"],
            "dismissed_through": 1,
        },
    }
    assert handoff_watcher.read(state)["runs"][run["run"]["run_id"]][
        "dismissed_through"
    ] == 1


def test_cli_starts_one_detached_watcher_and_it_exits_with_owner(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(tmp_path / "registry.json"))
    state = tmp_path / "orchestrator" / "watcher.json"
    owner = subprocess.Popen(
        [PYTHON, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        register_tmux_orchestrator(monkeypatch, capsys, state, owner.pid)

        started = run_cli(
            "orchestrator", "start", "--state", state, "--interval", 0.05,
        )
        assert started.returncode == 0, started.stderr
        first = json.loads(started.stdout)
        assert first["started"] is True
        assert first["running"] is True
        assert first["pid"] > 1

        reused = run_cli(
            "orchestrator", "start", "--state", state, "--interval", 0.05,
        )
        assert reused.returncode == 0, reused.stderr
        second = json.loads(reused.stdout)
        assert second["started"] is False
        assert second["running"] is True
        assert second["pid"] == first["pid"]

        owner.terminate()
        owner.wait(timeout=5)
        exitcode = state.parent / "watcher-process.exitcode"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not exitcode.exists():
            time.sleep(0.05)
        assert exitcode.read_text(encoding="utf-8") == "0"
    finally:
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=5)


def test_cmux_orchestrator_start_hosts_watcher_in_dedicated_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(tmp_path / "registry.json"))
    state = tmp_path / "orchestrator" / "watcher.json"
    handle = "01923456-89ab-4cde-8f01-23456789abcd"
    launches = []
    watchers = []
    owner = subprocess.Popen(
        [PYTHON, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        value = handoff_watcher.initialize(
            state,
            transport="cmux",
            target={"workspace": "workspace:9", "surface": "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"},
            owner_pid=owner.pid,
        )

        class RecordingAdapter:
            def __init__(self, binary):
                self.binary = binary
                self.workspace = None

            def launch(self, name, cwd, terminal_command, workspace=None):
                launches.append({
                    "binary": self.binary, "name": name,
                    "command": terminal_command, "workspace": workspace,
                })
                # Execute the exact command the surface's terminal would run so
                # the test proves it is a working watcher invocation.
                watchers.append(subprocess.Popen(
                    shlex.split(terminal_command.removeprefix("exec ")),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ))
                return handle

            def rescue_command(self, target):
                return f"read-screen {target}"

        monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", RecordingAdapter)
        hosting = "11111111-2222-4333-8444-555555555555"
        monkeypatch.setattr(handoffctl, "_watcher_workspace", lambda binary: hosting)
        monkeypatch.setattr(handoffctl, "_workspace_title", lambda binary, workspace: "agents")
        monkeypatch.setattr(
            handoffctl, "_watcher_tab_name",
            lambda binary, workspace, title, orchestrator_id: f"watcher: {title}",
        )

        first = handoffctl._start_orchestrator_watcher(
            state, 0.05, mode="auto", cmux_binary="/exact/cmux",
        )
        assert first["mode"] == "surface"
        assert first["started"] is True
        assert first["running"] is True
        assert first["surface"] == handle
        assert first["workspace"] == hosting
        assert first["orchestrator_workspace"] == "workspace:9"
        assert launches == [{
            "binary": "/exact/cmux",
            "name": "watcher: agents",
            "command": "exec " + shlex.join([
                sys.executable, "-m", "agents.orchestration.handoffctl",
                "watch", "--orchestrator-state", str(state), "--interval", "0.05",
                "--retry-seconds", "30.0",
            ]),
            "workspace": hosting,
        }]

        reused = handoffctl._start_orchestrator_watcher(
            state, 0.05, mode="auto", cmux_binary="/exact/cmux",
        )
        assert reused["started"] is False
        assert reused["running"] is True
        assert reused["mode"] == "surface"
        assert reused["surface"] == handle
        assert len(watchers) == 1

        owner.terminate()
        owner.wait(timeout=5)
        assert watchers[0].wait(timeout=10) == 0
    finally:
        for watcher in watchers:
            if watcher.poll() is None:
                watcher.terminate()
                watcher.wait(timeout=5)
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=5)


def test_surface_watcher_start_timeout_reports_rescue_command(tmp_path, monkeypatch):
    state = tmp_path / "orchestrator" / "watcher.json"
    handoff_watcher.initialize(
        state,
        transport="cmux",
        target={"workspace": "workspace:9", "surface": "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"},
        owner_pid=os.getpid(),
    )

    class InertAdapter:
        def __init__(self, binary):
            self.workspace = None

        def launch(self, name, cwd, terminal_command, workspace=None):
            return "01923456-89ab-4cde-8f01-23456789abcd"

        def rescue_command(self, target):
            return f"read-screen {target}"

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", InertAdapter)
    monkeypatch.setattr(handoffctl, "SURFACE_WATCHER_READY_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(
        handoffctl, "_watcher_workspace",
        lambda binary: "11111111-2222-4333-8444-555555555555",
    )
    monkeypatch.setattr(handoffctl, "_workspace_title", lambda binary, workspace: None)

    with pytest.raises(handoff.HandoffError, match="read-screen 01923456"):
        handoffctl._start_orchestrator_watcher(state, 0.05)


def test_watcher_workspace_reuses_or_creates_the_parked_bottom_workspace(monkeypatch):
    hosting = "11111111-2222-4333-8444-555555555555"
    orchestrator = "afa9c531-4d04-4f12-8e06-e97facc4fe2e"
    state = {"listing": (
        "  workspace:1 18AA46FA-7CC9-4676-AAC5-B190559B34C7  stock picker\n"
        f"* workspace:9 {orchestrator.upper()}  agents  [selected]\n"
    )}
    calls = []

    class Completed:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def fake_run(argv, check=True):
        calls.append(argv)
        if argv[-1] == "list-workspaces":
            return Completed(state["listing"])
        if argv[3:5] == ["new-workspace", "--name"]:
            # The real CLI reports only a short ref; the row appears in the
            # next inventory listing.
            state["listing"] += f"  workspace:12 {hosting.upper()}  handoff-watchers\n"
            return Completed("OK workspace:12\n")
        return Completed()

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run", fake_run)

    assert handoffctl._workspace_title("/exact/cmux", orchestrator.upper()) == "agents"

    created = handoffctl._watcher_workspace("/exact/cmux")
    assert created == hosting
    assert [
        "/exact/cmux", "--id-format", "both", "new-workspace",
        "--name", "handoff-watchers", "--focus", "false",
    ] in calls
    assert [
        "/exact/cmux", "reorder-workspace", "--workspace", hosting,
        "--after", orchestrator,
    ] in calls

    creations = len(calls)
    assert handoffctl._watcher_workspace("/exact/cmux") == hosting
    assert [argv for argv in calls[creations:] if "new-workspace" in argv] == []


def test_surface_watcher_mode_requires_cmux_orchestrator(tmp_path):
    state = tmp_path / "orchestrator" / "watcher.json"
    handoff_watcher.initialize(
        state,
        transport="tmux",
        target={"handle": "orchestrator:0.4"},
        owner_pid=os.getpid(),
    )

    result = run_cli("orchestrator", "start", "--state", state, "--mode", "surface")

    assert result.returncode == 2
    assert "requires a cmux orchestrator" in result.stderr


def test_watcher_tab_name_disambiguates_only_on_collision(monkeypatch):
    orchestrator_id = "de7f8206-f9c7-49c2-b1d1-a266c577cb61"
    surfaces = {"stdout": "  surface:1 UUID  other tab\n"}

    class Completed:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    monkeypatch.setattr(
        handoffctl.handoff_launcher, "_run",
        lambda argv, check=True: Completed(surfaces["stdout"]),
    )

    assert handoffctl._watcher_tab_name(
        "/exact/cmux", "workspace-uuid", "agents", orchestrator_id,
    ) == "watcher: agents"

    surfaces["stdout"] += "  surface:2 UUID  watcher: agents\n"
    assert handoffctl._watcher_tab_name(
        "/exact/cmux", "workspace-uuid", "agents", orchestrator_id,
    ) == "watcher: agents (de7f8206)"

    assert handoffctl._watcher_tab_name(
        "/exact/cmux", "workspace-uuid", None, orchestrator_id,
    ) == "watcher: orchestrator de7f8206"


def test_orchestrator_poll_printer_logs_only_doorbell_activity(capsys):
    handoffctl._print_orchestrator_poll(
        {"attempted": {}, "errors": [], "pending": {}},
    )
    assert capsys.readouterr().out == ""

    active = {"attempted": {"run-1": 3}, "errors": [], "pending": {"run-1": 3}}
    handoffctl._print_orchestrator_poll(active)
    assert json.loads(capsys.readouterr().out) == active


def test_cmux_orchestrator_registration_uses_the_orchestrator_environment(tmp_path, monkeypatch):
    surface = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    monkeypatch.setenv("CMUX_SURFACE_ID", surface.upper())
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace:orchestrator")
    monkeypatch.setattr(
        handoffctl.handoff_launcher,
        "_run",
        lambda *args, **kwargs: pytest.fail("cmux inventory should not be consulted"),
    )
    state = tmp_path / "orchestrator" / "watcher.json"
    args = Namespace(
        state=state,
        transport="cmux",
        target=None,
        surface=None,
        cmux_binary="/exact/cmux",
        owner_pid=os.getpid(),
        orchestrator_id=None,
    )

    value = handoffctl._register_orchestrator(args)

    assert value["target"] == {
        "workspace": "workspace:orchestrator",
        "surface": surface,
    }


def test_cmux_orchestrator_registration_finds_an_explicit_surface_workspace(tmp_path, monkeypatch):
    class Completed:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    surface = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"

    def fake_run(argv, check=True):
        if argv == ["/exact/cmux", "--id-format", "both", "list-workspaces"]:
            return Completed("  workspace:2 UUID two\n  workspace:9 UUID nine\n")
        if argv == [
            "/exact/cmux", "--id-format", "both", "list-pane-surfaces", "--workspace", "workspace:2",
        ]:
            return Completed("surface:2 another-surface\n")
        if argv == [
            "/exact/cmux", "--id-format", "both", "list-pane-surfaces", "--workspace", "workspace:9",
        ]:
            return Completed(f"surface:9 {surface}\n")
        raise AssertionError(argv)

    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
    monkeypatch.delenv("CMUX_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(handoffctl.handoff_launcher, "_run", fake_run)
    state = tmp_path / "orchestrator" / "watcher.json"
    args = Namespace(
        state=state,
        transport="cmux",
        target=None,
        surface=surface,
        cmux_binary="/exact/cmux",
        owner_pid=os.getpid(),
        orchestrator_id=None,
    )

    value = handoffctl._register_orchestrator(args)

    assert value["target"] == {"workspace": "workspace:9", "surface": surface}


def _tmux_register_args(state, target):
    return Namespace(
        state=state,
        transport="tmux",
        target=target,
        surface=None,
        cmux_binary="/exact/cmux",
        owner_pid=os.getpid(),
        orchestrator_id=None,
    )


def test_tmux_orchestrator_registration_auto_resolves_the_current_session(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1234,0")

    def fake_run(argv, check=True):
        if argv == ["tmux", "display-message", "-p", "#S"]:
            return subprocess.CompletedProcess(argv, 0, "orchestrator\n", "")
        raise AssertionError(argv)

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run", fake_run)
    args = _tmux_register_args(tmp_path / "orchestrator" / "watcher.json", "AUTO")

    value = handoffctl._register_orchestrator(args)

    assert value["target"] == {"handle": "orchestrator"}


def test_tmux_orchestrator_registration_auto_requires_running_inside_tmux(tmp_path, monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(
        handoffctl.handoff_launcher,
        "_run",
        lambda *args, **kwargs: pytest.fail("tmux must not be probed outside tmux"),
    )
    args = _tmux_register_args(tmp_path / "orchestrator" / "watcher.json", "auto")

    with pytest.raises(handoff.HandoffError) as excinfo:
        handoffctl._register_orchestrator(args)

    assert excinfo.value.exit_code == 2
    assert "--target auto requires running inside tmux" in str(excinfo.value)


def test_tmux_orchestrator_registration_auto_reports_a_failed_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1234,0")

    def fake_run(argv, check=True):
        raise handoffctl.handoff_launcher.AdapterError("no server running")

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run", fake_run)
    args = _tmux_register_args(tmp_path / "orchestrator" / "watcher.json", "auto")

    with pytest.raises(handoff.HandoffError) as excinfo:
        handoffctl._register_orchestrator(args)

    assert excinfo.value.exit_code == 2
    assert "--target auto requires running inside tmux" in str(excinfo.value)


def test_tmux_orchestrator_registration_rejects_an_unknown_session(tmp_path, monkeypatch):
    def fake_run(argv, check=True):
        if argv == ["tmux", "has-session", "-t", "ghost"]:
            return subprocess.CompletedProcess(argv, 1, "", "can't find session: ghost")
        raise AssertionError(argv)

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run", fake_run)
    args = _tmux_register_args(tmp_path / "orchestrator" / "watcher.json", "ghost")

    with pytest.raises(handoff.HandoffError) as excinfo:
        handoffctl._register_orchestrator(args)

    assert excinfo.value.exit_code == 2
    assert "ghost" in str(excinfo.value)
    assert "tmux ls" in str(excinfo.value)


def test_tmux_orchestrator_registration_accepts_an_existing_session(tmp_path, monkeypatch):
    def fake_run(argv, check=True):
        if argv == ["tmux", "has-session", "-t", "orchestrator:0.1"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run", fake_run)
    args = _tmux_register_args(tmp_path / "orchestrator" / "watcher.json", "orchestrator:0.1")

    value = handoffctl._register_orchestrator(args)

    assert value["target"] == {"handle": "orchestrator:0.1"}


def _tmux_retarget_args(state, target):
    return Namespace(state=state, target=target, surface=None, cmux_binary="/exact/cmux")


def test_tmux_orchestrator_retarget_replaces_the_handle(tmp_path, monkeypatch):
    state_path = tmp_path / "orchestrator" / "watcher.json"
    # A state left over from the unvalidated-registration bug: the recorded
    # handle never named a real session, so every typed doorbell failed.
    handoff_watcher.initialize(
        state_path, transport="tmux", target={"handle": "auto"}, owner_pid=os.getpid(),
    )

    def fake_run(argv, check=True):
        if argv == ["tmux", "has-session", "-t", "orchestrator:0.1"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run", fake_run)

    value = handoffctl._retarget_orchestrator(_tmux_retarget_args(state_path, "orchestrator:0.1"))

    assert value["target"] == {"handle": "orchestrator:0.1"}
    assert handoff_watcher.read(state_path)["target"] == {"handle": "orchestrator:0.1"}


def test_tmux_orchestrator_retarget_auto_resolves_the_current_session(tmp_path, monkeypatch):
    state_path = tmp_path / "orchestrator" / "watcher.json"
    handoff_watcher.initialize(
        state_path, transport="tmux", target={"handle": "stale"}, owner_pid=os.getpid(),
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1234,0")

    def fake_run(argv, check=True):
        if argv == ["tmux", "display-message", "-p", "#S"]:
            return subprocess.CompletedProcess(argv, 0, "orchestrator\n", "")
        raise AssertionError(argv)

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run", fake_run)

    value = handoffctl._retarget_orchestrator(_tmux_retarget_args(state_path, "auto"))

    assert value["target"] == {"handle": "orchestrator"}


def test_tmux_orchestrator_retarget_rejects_an_unknown_session(tmp_path, monkeypatch):
    state_path = tmp_path / "orchestrator" / "watcher.json"
    handoff_watcher.initialize(
        state_path, transport="tmux", target={"handle": "stale"}, owner_pid=os.getpid(),
    )

    def fake_run(argv, check=True):
        if argv == ["tmux", "has-session", "-t", "ghost"]:
            return subprocess.CompletedProcess(argv, 1, "", "can't find session: ghost")
        raise AssertionError(argv)

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run", fake_run)

    with pytest.raises(handoff.HandoffError) as excinfo:
        handoffctl._retarget_orchestrator(_tmux_retarget_args(state_path, "ghost"))

    assert excinfo.value.exit_code == 2
    assert "ghost" in str(excinfo.value)
    assert handoff_watcher.read(state_path)["target"] == {"handle": "stale"}


def test_cmux_orchestrator_retarget_still_uses_the_surface_environment(tmp_path, monkeypatch):
    surface = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    state_path = tmp_path / "orchestrator" / "watcher.json"
    handoff_watcher.initialize(
        state_path,
        transport="cmux",
        target={"workspace": "workspace:2", "surface": surface},
        owner_pid=os.getpid(),
    )
    monkeypatch.setenv("CMUX_SURFACE_ID", surface)
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace:9")
    monkeypatch.setattr(
        handoffctl.handoff_launcher,
        "_run",
        lambda *args, **kwargs: pytest.fail("cmux inventory should not be consulted"),
    )
    args = Namespace(state=state_path, target=None, surface=None, cmux_binary="/exact/cmux")

    value = handoffctl._retarget_orchestrator(args)

    assert value["target"] == {"workspace": "workspace:9", "surface": surface}


def test_native_app_orchestrator_retarget_is_still_rejected(tmp_path):
    state_path = tmp_path / "orchestrator" / "watcher.json"
    handoff_watcher.initialize(
        state_path, transport="native_app", target={"thread_id": "thread-1"},
        owner_pid=os.getpid(),
    )

    with pytest.raises(handoff.HandoffError) as excinfo:
        handoffctl._retarget_orchestrator(_tmux_retarget_args(state_path, "thread-2"))

    assert excinfo.value.exit_code == 2
    assert "cmux and tmux" in str(excinfo.value)
    assert handoff_watcher.read(state_path)["target"] == {"thread_id": "thread-1"}


def test_cli_explicitly_adopts_unowned_registry_run(tmp_path, monkeypatch, capsys):
    run = registered_run(tmp_path, monkeypatch)
    state = tmp_path / "orchestrator" / "watcher.json"
    registered = register_tmux_orchestrator(monkeypatch, capsys, state, os.getpid())

    adopted = run_cli(
        "runs", "adopt", "--run", "weather", "--orchestrator-state", state,
    )

    assert adopted.returncode == 0, adopted.stderr
    assert json.loads(adopted.stdout)["orchestrator_id"] == registered["orchestrator_id"]
    assert handoff_registry.resolve(run["run"]["run_id"])["orchestrator_id"] == registered["orchestrator_id"]


def fail_registered_run(run):
    handoff.emit(
        Path(run["run_dir"]), run["worker"], type="error", body="fatal test failure",
        data={
            "fatal": True, "category": "test", "stage": "lookup",
            "inbox_cursor": 0, "commitments": [],
        },
    )
    assert handoff.is_fatal(Path(run["run_dir"])) is True


def test_cli_runs_clean_single_removes_terminal_record_and_preserves_run(tmp_path, monkeypatch, capsys):
    run = registered_run(tmp_path, monkeypatch)
    fail_registered_run(run)

    assert handoffctl.main(["runs", "clean", "--run", "weather"]) == 0

    output = json.loads(capsys.readouterr().out)
    entry = output["cleaned"][0]
    assert entry["run_id"] == run["run"]["run_id"]
    assert "credential_dir" not in entry["record"]
    assert entry["note"] == "worker reported a fatal error"
    assert entry["record_removed"] is True
    assert handoff_registry.list_records() == []
    # Registry-only: the run directory and credential files are untouched.
    assert handoff.is_fatal(Path(run["run_dir"])) is True
    assert (run["private"] / "recovery.token").exists()
    assert (run["private"] / "worker.token").exists()


def test_cli_runs_clean_single_skips_live_run_until_forced(tmp_path, monkeypatch, capsys):
    run = registered_run(tmp_path, monkeypatch)

    assert handoffctl.main(["runs", "clean", "--run", "weather"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["cleaned"] == []
    assert [skip["run_id"] for skip in output["skipped"]] == [run["run"]["run_id"]]
    assert "starting" in output["skipped"][0]["reason"]
    assert handoff_registry.resolve("weather")["run_id"] == run["run"]["run_id"]

    assert handoffctl.main(["runs", "clean", "--run", "weather", "--force"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert "force" in output["cleaned"][0]["note"]
    assert handoff_registry.list_records() == []
    assert handoff.status(Path(run["run_dir"]))["state"] == "starting"


def test_cli_runs_clean_bulk_requires_confirmation_and_dry_run_changes_nothing(
    tmp_path, monkeypatch, capsys,
):
    run = registered_run(tmp_path, monkeypatch)
    fail_registered_run(run)

    assert handoffctl.main(["runs", "clean"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert len(handoff_registry.list_records()) == 1

    assert handoffctl.main(["runs", "clean", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True
    assert [entry["run_id"] for entry in plan["cleaned"]] == [run["run"]["run_id"]]
    assert "credential_dir" not in plan["cleaned"][0]["record"]
    assert plan["cleaned"][0]["record_removed"] is False
    assert plan["skipped"] == []
    assert len(handoff_registry.list_records()) == 1

    assert handoffctl.main(["runs", "clean", "--yes"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is False
    assert [entry["run_id"] for entry in result["cleaned"]] == [run["run"]["run_id"]]
    assert result["cleaned"][0]["record_removed"] is True
    assert handoff_registry.list_records() == []
    assert handoff.is_fatal(Path(run["run_dir"])) is True
    assert (run["private"] / "recovery.token").exists()


def test_cli_runs_clean_bulk_skips_live_records_with_reasons(tmp_path, monkeypatch, capsys):
    run = registered_run(tmp_path, monkeypatch)

    assert handoffctl.main(["runs", "clean", "--yes"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["cleaned"] == []
    assert [skip["run_id"] for skip in result["skipped"]] == [run["run"]["run_id"]]
    assert "starting" in result["skipped"][0]["reason"]
    assert len(handoff_registry.list_records()) == 1


def test_cli_runs_clean_rejects_run_with_bulk_flags(tmp_path, monkeypatch, capsys):
    registered_run(tmp_path, monkeypatch)

    assert handoffctl.main(["runs", "clean", "--run", "weather", "--yes"]) == 2
    assert "--dry-run/--yes apply only to bulk clean" in capsys.readouterr().err
    assert handoffctl.main(["runs", "clean", "--run", "weather", "--older-than", "30"]) == 2
    assert "bulk filters" in capsys.readouterr().err


def test_cli_runs_clean_delete_run_dir_removes_terminal_runs(tmp_path, monkeypatch, capsys):
    run = registered_run(tmp_path, monkeypatch)
    fail_registered_run(run)

    assert handoffctl.main(["runs", "clean", "--dry-run", "--delete-run-dir"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["cleaned"][0]["run_dir_deleted"] is True
    assert Path(run["run_dir"]).exists()
    assert len(handoff_registry.list_records()) == 1

    assert handoffctl.main(["runs", "clean", "--yes", "--delete-run-dir"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["cleaned"][0]["run_dir_deleted"] is True
    assert handoff_registry.list_records() == []
    assert not Path(run["run_dir"]).exists()
    assert (run["private"] / "recovery.token").exists()


def test_cli_runs_doctor_reports_invalid_records_individually(tmp_path, monkeypatch, capsys):
    registered_run(tmp_path, monkeypatch)
    registry = tmp_path / "registry.json"
    value = json.loads(registry.read_text())
    value["runs"]["run-broken"] = {"run_id": "run-broken", "name": "broken"}
    registry.write_text(json.dumps(value), encoding="utf-8")

    assert handoffctl.main(["runs", "doctor"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert len(report["valid"]) == 1
    assert report["invalid"][0]["key"] == "run-broken"
    assert "invalid fields" in report["invalid"][0]["error"]


def test_cli_runs_clean_is_the_escape_hatch_for_a_corrupt_record(tmp_path, monkeypatch, capsys):
    run = registered_run(tmp_path, monkeypatch)
    registry = tmp_path / "registry.json"
    value = json.loads(registry.read_text())
    value["runs"]["run-broken"] = {"run_id": "run-broken", "name": "broken"}
    registry.write_text(json.dumps(value), encoding="utf-8")

    assert handoffctl.main(["runs", "list"]) == 5
    assert "invalid fields" in capsys.readouterr().err

    assert handoffctl.main(["runs", "clean", "--run", "run-broken"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["cleaned"][0]["run_id"] == "run-broken"
    assert "invalid" in output["cleaned"][0]["note"]

    assert handoffctl.main(["runs", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["run_id"] for item in listed] == [run["run"]["run_id"]]


def test_cli_runs_clean_watcher_state_scopes_to_the_session(tmp_path, monkeypatch, capsys):
    adopted = registered_run(tmp_path, monkeypatch)
    fail_registered_run(adopted)
    # A second, unowned terminal record pointing at the same finished run.
    registry = tmp_path / "registry.json"
    value = json.loads(registry.read_text())
    first = next(iter(value["runs"]))
    value["runs"]["run-other"] = {**value["runs"][first], "run_id": "run-other", "name": "other"}
    registry.write_text(json.dumps(value), encoding="utf-8")
    state = tmp_path / "orchestrator" / "watcher.json"
    registered = register_tmux_orchestrator(monkeypatch, capsys, state, os.getpid())
    orchestrator_id = registered["orchestrator_id"]
    adopted_cli = run_cli("runs", "adopt", "--run", "weather", "--orchestrator-state", state)
    assert adopted_cli.returncode == 0, adopted_cli.stderr

    assert handoffctl.main(["runs", "clean", "--watcher-state", str(state), "--yes"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["scope"]["orchestrator"] == orchestrator_id
    assert [entry["run_id"] for entry in result["cleaned"]] == [adopted["run"]["run_id"]]
    assert [item["run_id"] for item in handoff_registry.list_records()] == ["run-other"]


def _emit_result(run, body="weather done"):
    return handoff.emit(
        Path(run["run_dir"]), run["worker"], type="result", body=body,
        data={
            "head": None, "dirty": False, "stage": "complete", "inbox_cursor": 0,
            "verification": [], "commitments": [],
        },
    )


def test_conclude_accepts_integrates_and_leaves_worker_to_pause(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    result = _emit_result(run)
    handoff.control_release(run_dir, run["orchestrator"])
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    concluded = handoffctl._conclude_registered(
        "weather", disposition="accepted", body=None,
        commit="deadbeef", no_integrate=False, final_stop=False,
    )

    assert concluded["takeover"]["type"] == "orchestrator-takeover"
    assert concluded["disposition"] == "accepted"
    assert concluded["consumed_through"] == 1
    assert concluded["review"]["type"] == "review"
    assert concluded["review"]["reply_to"] == result["message"]["message_id"]
    assert concluded["review"]["data"] == {"disposition": "accepted"}
    assert concluded["integration"]["data"] == {
        "state": "integrated", "commit": "deadbeef", "reason": None,
    }
    assert concluded["integration_skipped"] is False
    assert concluded["finished"] is False
    assert concluded["finished_already"] is False
    assert concluded["doorbell_sent"] is True
    assert concluded["doorbell_error"] is None
    assert concluded["orchestrator_released"] is True
    # Exactly one doorbell covers review -> integration.
    assert doorbells == [(
        "weather-worker", run["run"]["run_id"], concluded["integration"]["seq"],
    )]
    assert not list(run["private"].glob("orchestrator-conclude-*.token"))
    control = handoff.control_show(run_dir)
    assert "desired_state" not in control
    assert handoff._parse_time(control["orchestrator_lease_expires_at"]) <= handoff._now()
    # The worker drains its inbox in order and projects paused, idling
    # resumably instead of exiting.
    paused = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="paused",
        data={
            "state": "paused", "stage": "complete", "current_activity": "paused",
            "inbox_cursor": concluded["integration"]["seq"], "commitments": [],
        },
    )
    assert paused["status"]["state"] == "paused"
    assert handoff.doctor(run_dir)["ok"]


def test_conclude_stop_integrates_and_marks_finished(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    result = _emit_result(run)
    handoff.control_release(run_dir, run["orchestrator"])
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    concluded = handoffctl._conclude_registered(
        "weather", disposition="accepted", body=None,
        commit="deadbeef", no_integrate=False, final_stop=True,
    )

    assert concluded["takeover"]["type"] == "orchestrator-takeover"
    assert concluded["disposition"] == "accepted"
    assert concluded["consumed_through"] == 1
    assert concluded["review"]["type"] == "review"
    assert concluded["review"]["reply_to"] == result["message"]["message_id"]
    assert concluded["review"]["data"] == {"disposition": "accepted"}
    assert concluded["integration"]["data"] == {
        "state": "integrated", "commit": "deadbeef", "reason": None,
    }
    assert concluded["integration_skipped"] is False
    assert concluded["finished"] is True
    assert concluded["finished_already"] is False
    assert concluded["doorbell_sent"] is True
    assert concluded["doorbell_error"] is None
    assert concluded["orchestrator_released"] is True
    # Exactly one doorbell covers review -> integration; termination is the
    # registry marker rather than a worker lifecycle message.
    assert doorbells == [(
        "weather-worker", run["run"]["run_id"], concluded["integration"]["seq"],
    )]
    assert not list(run["private"].glob("orchestrator-conclude-*.token"))
    control = handoff.control_show(run_dir)
    assert "desired_state" not in control
    assert handoff._parse_time(control["orchestrator_lease_expires_at"]) <= handoff._now()
    assert handoff_registry.resolve("weather")["finished_at"] is not None
    paused = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="paused",
        data={
            "state": "paused", "stage": "complete", "current_activity": "paused",
            "inbox_cursor": concluded["integration"]["seq"], "commitments": [],
        },
    )
    assert paused["status"]["state"] == "paused"


def test_conclude_stop_retry_after_lost_response_reuses_finished_sequence(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    result = _emit_result(run)
    handoff.control_release(run_dir, run["orchestrator"])

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            pass

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    first = handoffctl._conclude_registered(
        "weather", disposition="accepted", body=None,
        commit="deadbeef", no_integrate=False, final_stop=True,
    )
    # Simulate the caller losing the successful response and retrying the
    # exact operation.  The marker was written last, so it proves that the
    # accepted review/integration sequence is already durable.
    second = handoffctl._conclude_registered(
        "weather", disposition="accepted", body=None,
        commit="deadbeef", no_integrate=False, final_stop=True,
    )

    assert first["finished_already"] is False
    assert second["finished_already"] is True
    assert second["review"]["message_id"] == first["review"]["message_id"]
    assert second["review"]["reply_to"] == result["message"]["message_id"]
    assert second["integration"]["message_id"] == first["integration"]["message_id"]
    assert handoff_registry.resolve("weather")["finished_at"] is not None


def test_conclude_skips_integration_when_result_has_no_head(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    _emit_result(run)
    handoff.control_release(run_dir, run["orchestrator"])

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            pass

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    concluded = handoffctl._conclude_registered(
        "weather", disposition="accepted", body=None,
        commit=None, no_integrate=False, final_stop=False,
    )

    assert concluded["review"]["type"] == "review"
    assert concluded["integration"] is None
    assert concluded["integration_skipped"] is True
    assert concluded["finished"] is False
    assert concluded["doorbell_sent"] is True
    control = handoff.control_show(run_dir)
    assert control["integration"]["state"] == "pending"
    assert "desired_state" not in control


def test_conclude_changes_requested_resumes_worker(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    result = _emit_result(run)
    handoff.control_release(run_dir, run["orchestrator"])
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    concluded = handoffctl._conclude_registered(
        "weather", disposition="changes_requested", body="please redo the fetch",
        commit=None, no_integrate=False,
        final_stop=False,
    )

    assert concluded["review"]["type"] == "review"
    assert concluded["review"]["reply_to"] == result["message"]["message_id"]
    assert concluded["review"]["data"] == {"disposition": "changes_requested"}
    assert concluded["review"]["body"] == "please redo the fetch"
    assert concluded["integration"] is None
    assert concluded["integration_skipped"] is False
    assert concluded["finished"] is False
    assert concluded["doorbell_sent"] is True
    assert doorbells == [(
        "weather-worker", run["run"]["run_id"], concluded["review"]["seq"],
    )]
    control = handoff.control_show(run_dir)
    assert control["review_state"] == "changes_requested"
    assert control["integration"]["state"] == "pending"
    assert "desired_state" not in control
    # The worker resumes against the changes-requested review.
    resumed = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="reworking",
        data={
            "state": "working", "stage": "complete", "current_activity": "redoing",
            "inbox_cursor": concluded["review"]["seq"], "commitments": [],
        },
    )
    assert resumed["status"]["state"] == "working"


def test_conclude_validates_flag_combinations(tmp_path, monkeypatch, capsys):
    registered_run(tmp_path, monkeypatch)
    body = tmp_path / "review.md"
    body.write_text("redo it", encoding="utf-8")

    missing_body = handoffctl.main(
        ["conclude", "--run", "weather", "--disposition", "changes-requested"],
    )
    assert missing_body == 2
    assert "--body-file" in capsys.readouterr().err

    with_commit = handoffctl.main([
        "conclude", "--run", "weather", "--disposition", "changes-requested",
        "--body-file", str(body), "--commit", "deadbeef",
    ])
    assert with_commit == 2
    assert "--disposition accepted" in capsys.readouterr().err

    with_no_integrate = handoffctl.main([
        "conclude", "--run", "weather", "--disposition", "changes-requested",
        "--body-file", str(body), "--no-integrate",
    ])
    assert with_no_integrate == 2
    assert "--disposition accepted" in capsys.readouterr().err


def test_conclude_refuses_an_active_orchestrator_lease(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    _emit_result(run)

    with pytest.raises(handoff.HandoffError, match="lease is active"):
        handoffctl._conclude_registered(
            "weather", disposition="accepted", body=None,
            commit=None, no_integrate=False,
            final_stop=False,
        )

    assert not list(run["private"].glob("orchestrator-conclude-*.token"))


def test_failed_conclude_releases_ephemeral_orchestrator(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    _emit_result(run)
    handoff.control_release(run_dir, run["orchestrator"])

    def explode(run_dir, token, *, commit):
        raise handoff.HandoffError("integration exploded", 6)

    monkeypatch.setattr(handoffctl.handoff, "control_integrate", explode)

    with pytest.raises(handoff.HandoffError, match="integration exploded"):
        handoffctl._conclude_registered(
            "weather", disposition="accepted", body=None,
            commit="deadbeef", no_integrate=False,
            final_stop=False,
        )

    control = handoff.control_show(run_dir)
    assert handoff._parse_time(control["orchestrator_lease_expires_at"]) <= handoff._now()
    assert not list(run["private"].glob("orchestrator-conclude-*.token"))


def test_conclude_resumes_after_mid_sequence_crash(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    result = _emit_result(run)
    handoff.control_consume(run_dir, run["orchestrator"], through=1)
    review = handoff.send(
        run_dir, run["orchestrator"], type="review",
        reply_to=result["message"]["message_id"],
        data={"disposition": "accepted"},
    )
    handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="done",
        data={
            "state": "paused", "stage": "complete", "current_activity": "paused",
            "inbox_cursor": review["message"]["seq"], "commitments": [],
        },
    )
    # The previous conclude crashed after the review: lease released, review
    # accepted, integration still pending.
    handoff.control_release(run_dir, run["orchestrator"])
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    concluded = handoffctl._conclude_registered(
        "weather", disposition="accepted", body=None,
        commit="deadbeef", no_integrate=False,
        final_stop=False,
    )

    assert concluded["review"] is None
    assert concluded["consumed_through"] == 1
    assert concluded["integration"]["data"]["state"] == "integrated"
    assert concluded["integration_skipped"] is False
    assert concluded["finished"] is False
    assert concluded["doorbell_sent"] is True
    assert concluded["orchestrator_released"] is True
    assert doorbells == [(
        "weather-worker", run["run"]["run_id"], concluded["integration"]["seq"],
    )]
    assert not list(run["private"].glob("orchestrator-conclude-*.token"))
    # The paused worker drains the integration record and remains idle.
    paused = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="paused",
        data={
            "state": "paused", "stage": "complete", "current_activity": "paused",
            "inbox_cursor": concluded["integration"]["seq"], "commitments": [],
        },
    )
    assert paused["status"]["state"] == "paused"


def test_conclude_resumes_a_crash_after_integration(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    result = _emit_result(run)
    handoff.control_consume(run_dir, run["orchestrator"], through=1)
    handoff.send(
        run_dir, run["orchestrator"], type="review",
        reply_to=result["message"]["message_id"],
        data={"disposition": "accepted"},
    )
    handoff.control_integrate(run_dir, run["orchestrator"], commit="deadbeef")
    # The previous conclude crashed after the integration record but before
    # returning/ringing: review accepted and integration recorded.
    handoff.control_release(run_dir, run["orchestrator"])
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    concluded = handoffctl._conclude_registered(
        "weather", disposition="accepted", body=None,
        commit="deadbeef", no_integrate=False,
        final_stop=False,
    )

    assert concluded["review"] is None
    assert concluded["integration"] is None
    assert concluded["integration_skipped"] is False
    assert concluded["finished"] is False
    assert concluded["doorbell_sent"] is True
    inbox_tail = len(handoff.read(run_dir, "inbox"))
    assert doorbells == [("weather-worker", run["run"]["run_id"], inbox_tail)]
    control = handoff.control_show(run_dir)
    assert "desired_state" not in control
    # The worker drains review, integration, and takeover in order and idles.
    paused = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="paused",
        data={
            "state": "paused", "stage": "complete", "current_activity": "paused",
            "inbox_cursor": inbox_tail, "commitments": [],
        },
    )
    assert paused["status"]["state"] == "paused"
    assert handoff.doctor(run_dir)["ok"]


def test_remote_conclude_uses_one_structured_ssh_request(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    handoff_registry.register(
        run_id="remote-run", name="remote-weather", run_dir="/remote/run",
        run_uri="ssh://oci-box/remote/run", host="oci-box",
        remote_python="/remote/python", transport="ssh_tmux",
        session_transport="tmux", handle="remote-worker", agent="claude",
        model="opus", effort="low", credential_dir=None,
    )
    observed = {}

    def fake_ssh(host, remote_argv, *, stdin, timeout):
        observed.update(host=host, argv=remote_argv, stdin=stdin, timeout=timeout)
        return subprocess.CompletedProcess(
            remote_argv, 0,
            stdout=json.dumps({"finished": False, "doorbell_sent": True}),
            stderr="",
        )

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run_ssh", fake_ssh)

    result = handoffctl._conclude_registered(
        "remote-weather", disposition="changes_requested", body="redo it ; $(touch NOPE)",
        commit=None, no_integrate=False,
        final_stop=False,
    )

    assert result["doorbell_sent"] is True
    assert observed["host"] == "oci-box"
    argv = observed["argv"]
    assert argv[0] == "/remote/python"
    assert argv[1:3] == ["-m", "agents.orchestration.handoffctl"]
    assert argv[3:6] == ["conclude", "--run", "remote-run"]
    assert argv[argv.index("--disposition") + 1] == "changes-requested"
    # The review body travels over stdin; lifecycle reasons/messages no longer
    # exist in the collapsed protocol.
    assert "--reason" not in argv
    assert argv[argv.index("--body-file") + 1] == "-"
    assert "--stop" not in argv
    assert observed["stdin"] == "redo it ; $(touch NOPE)"
    assert not (tmp_path / "NOPE").exists()


def test_remote_conclude_stop_flag_uses_one_structured_ssh_request(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    handoff_registry.register(
        run_id="remote-run", name="remote-weather", run_dir="/remote/run",
        run_uri="ssh://oci-box/remote/run", host="oci-box",
        remote_python="/remote/python", transport="ssh_tmux",
        session_transport="tmux", handle="remote-worker", agent="claude",
        model="opus", effort="low", credential_dir=None,
    )
    observed = {}

    def fake_ssh(host, remote_argv, *, stdin, timeout):
        observed.update(host=host, argv=remote_argv, stdin=stdin, timeout=timeout)
        return subprocess.CompletedProcess(
            remote_argv, 0,
            stdout=json.dumps({
                "finished": True, "finished_already": False,
                "doorbell_sent": True,
            }),
            stderr="",
        )

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run_ssh", fake_ssh)

    result = handoffctl._conclude_registered(
        "remote-weather", disposition="accepted", body=None,
        commit="deadbeef", no_integrate=False,
        final_stop=True,
    )

    assert result["doorbell_sent"] is True
    assert observed["host"] == "oci-box"
    argv = observed["argv"]
    assert argv[1:3] == ["-m", "agents.orchestration.handoffctl"]
    assert argv[3:6] == ["conclude", "--run", "remote-run"]
    assert "--stop" in argv
    assert "--reason" not in argv
    assert argv[argv.index("--commit") + 1] == "deadbeef"
    assert observed["stdin"] is None
    assert handoff_registry.resolve("remote-weather")["finished_at"] is not None


def test_conclude_pause_resume_and_reconclude_cycle(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    _emit_result(run, body="first weather")
    handoff.control_release(run_dir, run["orchestrator"])
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    concluded = handoffctl._conclude_registered(
        "weather", disposition="accepted", body=None,
        commit="deadbeef", no_integrate=False,
        final_stop=False,
    )

    assert concluded["finished"] is False
    control = handoff.control_show(run_dir)
    assert "desired_state" not in control
    inbox_types = [message["type"] for message in handoff.read(run_dir, "inbox")]
    assert inbox_types == ["orchestrator-takeover", "review", "integration"]

    paused = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="paused",
        data={
            "state": "paused", "stage": "complete", "current_activity": "paused",
            "inbox_cursor": concluded["integration"]["seq"], "commitments": [],
        },
    )
    assert paused["status"]["state"] == "paused"

    # A paused run accepts a new instruction despite the recorded integration.
    dispatched = handoffctl._dispatch_registered("weather", "refresh the forecast")
    assert dispatched["message"]["type"] == "steer"
    assert dispatched["doorbell_sent"] is True
    resumed = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="resumed",
        data={
            "state": "working", "stage": "lookup", "current_activity": "refreshing",
            "inbox_cursor": dispatched["message"]["seq"], "commitments": [],
        },
    )
    assert resumed["status"]["state"] == "working"
    second = handoff.emit(
        run_dir, run["worker"], type="result", body="fresh weather",
        data={
            "head": None, "dirty": False, "stage": "complete",
            "inbox_cursor": dispatched["message"]["seq"],
            "verification": [], "commitments": [],
        },
    )
    assert second["status"]["state"] == "awaiting_review"

    reconcluded = handoffctl._conclude_registered(
        "weather", disposition="accepted", body=None,
        commit="cafebabe", no_integrate=False,
        final_stop=False,
    )

    assert reconcluded["review"]["reply_to"] == second["message"]["message_id"]
    assert reconcluded["integration"]["data"]["commit"] == "cafebabe"
    assert reconcluded["finished"] is False
    assert "desired_state" not in handoff.control_show(run_dir)

    # The worker pauses again on the strength of the new accepted review.
    repaused = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="paused again",
        data={
            "state": "paused", "stage": "complete", "current_activity": "paused",
            "inbox_cursor": reconcluded["integration"]["seq"], "commitments": [],
        },
    )
    assert repaused["status"]["state"] == "paused"

    # A third dispatch still reaches the live, idle worker.
    third = handoffctl._dispatch_registered("weather", "once more")
    assert third["message"]["type"] == "steer"
    assert handoff.doctor(run_dir)["ok"]


def test_dispatch_recovers_a_wedged_succeeded_run(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    result = _emit_result(run)
    handoff.control_consume(run_dir, run["orchestrator"], through=1)
    review = handoff.send(
        run_dir, run["orchestrator"], type="review",
        reply_to=result["message"]["message_id"], data={"disposition": "accepted"},
    )
    handoff.control_integrate(run_dir, run["orchestrator"], commit="deadbeef")
    control = handoff.control_show(run_dir)
    control["desired_state"] = "pause"
    handoff._atomic_json(run_dir / "control.json", control)  # noqa: SLF001
    # The 2026-07-22 incident state: the worker reported success without a
    # lifecycle message, leaving a stale desired_state that made the live,
    # idle worker unreachable by dispatch and reap-eligible by runs clean.
    parked = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="done",
        data={
            "state": "succeeded", "stage": "complete", "current_activity": "done",
            "inbox_cursor": review["message"]["seq"], "commitments": [],
        },
    )
    assert parked["status"]["state"] == "paused"
    status = handoff.status(run_dir)
    status["state"] = "succeeded"
    handoff._atomic_json(run_dir / "status.json", status)  # noqa: SLF001
    handoff.control_release(run_dir, run["orchestrator"])
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    dispatched = handoffctl._dispatch_registered("weather", "keep going")
    assert dispatched["message"]["type"] == "steer"
    assert dispatched["doorbell_sent"] is True
    resumed = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="resumed",
        data={
            "state": "working", "stage": "lookup", "current_activity": "working",
            "inbox_cursor": dispatched["message"]["seq"], "commitments": [],
        },
    )
    assert resumed["status"]["state"] == "working"
    assert handoff.doctor(run_dir)["ok"]


def test_conclude_pause_resume_multi_message_window(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    _emit_result(run)
    handoff.control_release(run_dir, run["orchestrator"])
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    concluded = handoffctl._conclude_registered(
        "weather", disposition="accepted", body=None,
        commit="deadbeef", no_integrate=False,
        final_stop=False,
    )
    paused = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="paused",
        data={
            "state": "paused", "stage": "complete", "current_activity": "paused",
            "inbox_cursor": concluded["integration"]["seq"], "commitments": [],
        },
    )
    assert paused["status"]["state"] == "paused"

    # Every message in the resumed window — the first steer, the blocking
    # question's answer, and a later steer — must pass the integration guard.
    dispatched = handoffctl._dispatch_registered("weather", "refresh the forecast")
    assert dispatched["message"]["type"] == "steer"
    resumed = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="resumed",
        data={
            "state": "working", "stage": "lookup", "current_activity": "refreshing",
            "inbox_cursor": dispatched["message"]["seq"], "commitments": [],
        },
    )
    assert resumed["status"]["state"] == "working"
    question = handoff.emit(
        run_dir, run["worker"], type="question", body="which source?",
        data={
            "blocking": True, "stage": "lookup", "current_activity": "awaiting direction",
            "inbox_cursor": dispatched["message"]["seq"], "commitments": [],
        },
    )
    assert question["status"]["state"] == "blocked"
    answered = handoffctl._dispatch_registered("weather", "use the met office")
    assert answered["message"]["type"] == "answer"
    assert answered["answered_question"] == question["message"]["message_id"]
    resumed = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="resumed",
        data={
            "state": "working", "stage": "lookup", "current_activity": "refreshing",
            "inbox_cursor": answered["message"]["seq"], "commitments": [],
        },
    )
    assert resumed["status"]["state"] == "working"
    followup = handoffctl._dispatch_registered("weather", "include tomorrow")
    assert followup["message"]["type"] == "steer"
    second = handoff.emit(
        run_dir, run["worker"], type="result", body="fresh weather",
        data={
            "head": None, "dirty": False, "stage": "complete",
            "inbox_cursor": followup["message"]["seq"],
            "verification": [], "commitments": [],
        },
    )
    assert second["status"]["state"] == "awaiting_review"
    assert handoff.doctor(run_dir)["ok"]


def test_stop_requests_final_stop_for_a_paused_run(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    result = _emit_result(run)
    handoff.control_consume(run_dir, run["orchestrator"], through=1)
    review = handoff.send(
        run_dir, run["orchestrator"], type="review",
        reply_to=result["message"]["message_id"], data={"disposition": "accepted"},
    )
    handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="paused",
        data={
            "state": "paused", "stage": "complete", "current_activity": "paused",
            "inbox_cursor": review["message"]["seq"], "commitments": [],
        },
    )
    handoff.control_release(run_dir, run["orchestrator"])
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    stopped = handoffctl._stop_registered("weather")

    assert stopped["takeover"]["type"] == "orchestrator-takeover"
    assert stopped["finished"] is True
    assert stopped["finished_already"] is False
    assert stopped["doorbell_sent"] is False
    assert stopped["doorbell_error"] is None
    assert stopped["orchestrator_released"] is True
    assert doorbells == []
    assert not list(run["private"].glob("orchestrator-stop-*.token"))
    control = handoff.control_show(run_dir)
    assert "desired_state" not in control
    assert handoff._parse_time(control["orchestrator_lease_expires_at"]) <= handoff._now()
    assert handoff_registry.resolve("weather")["finished_at"] is not None
    assert handoff.status(run_dir)["state"] == "paused"
    assert handoff.doctor(run_dir)["ok"]


def test_stop_is_idempotent_for_an_already_finished_run(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    handoff.control_release(run_dir, run["orchestrator"])

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            pass

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    first = handoffctl._stop_registered("weather")
    assert first["finished"] is True
    assert first["finished_already"] is False

    second = handoffctl._stop_registered("weather")
    assert second["finished"] is True
    assert second["finished_already"] is True
    assert not list(run["private"].glob("orchestrator-stop-*.token"))


def test_stop_refuses_a_fatal_worker_epoch(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    fail_registered_run(run)
    handoff.control_release(Path(run["run_dir"]), run["orchestrator"])
    with pytest.raises(handoff.HandoffError, match="terminal"):
        handoffctl._stop_registered("weather")
    assert not list(run["private"].glob("orchestrator-stop-*.token"))


def test_remote_stop_uses_one_structured_ssh_request(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    handoff_registry.register(
        run_id="remote-run", name="remote-weather", run_dir="/remote/run",
        run_uri="ssh://oci-box/remote/run", host="oci-box",
        remote_python="/remote/python", transport="ssh_tmux",
        session_transport="tmux", handle="remote-worker", agent="claude",
        model="opus", effort="low", credential_dir=None,
    )
    observed = {}

    def fake_ssh(host, remote_argv, *, stdin, timeout):
        observed.update(host=host, argv=remote_argv, stdin=stdin, timeout=timeout)
        return subprocess.CompletedProcess(
            remote_argv, 0,
            stdout=json.dumps({
                "finished": True, "finished_already": False,
                "doorbell_sent": False,
            }),
            stderr="",
        )

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run_ssh", fake_ssh)

    result = handoffctl._stop_registered("remote-weather")

    assert result["finished"] is True
    assert observed["host"] == "oci-box"
    argv = observed["argv"]
    assert argv[0] == "/remote/python"
    assert argv[1:3] == ["-m", "agents.orchestration.handoffctl"]
    assert argv[3:6] == ["stop", "--run", "remote-run"]
    assert "--reason" not in argv
    assert observed["stdin"] is None
    assert handoff_registry.resolve("remote-weather")["finished_at"] is not None


def test_cli_conclude_defaults_to_pause_and_stop_finishes(tmp_path, monkeypatch, capsys):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    _emit_result(run)
    handoff.control_release(run_dir, run["orchestrator"])

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            pass

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    assert handoffctl.main(["conclude", "--run", "weather"]) == 0
    concluded = json.loads(capsys.readouterr().out)
    assert concluded["finished"] is False
    assert "desired_state" not in handoff.control_show(run_dir)

    # A repeated conclude safely resumes the already-durable sequence.
    assert handoffctl.main(["conclude", "--run", "weather"]) == 0
    reconcluded = json.loads(capsys.readouterr().out)
    assert reconcluded["finished"] is False

    assert handoffctl.main(["stop", "--run", "weather"]) == 0
    stopped = json.loads(capsys.readouterr().out)
    assert stopped["finished"] is True
    assert handoff_registry.resolve("weather")["finished_at"] is not None


def test_cli_conclude_stop_flag_and_stop_is_idempotent(tmp_path, monkeypatch, capsys):
    run = registered_run(tmp_path, monkeypatch)
    _emit_result(run)
    handoff.control_release(Path(run["run_dir"]), run["orchestrator"])

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            pass

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    assert handoffctl.main(["conclude", "--run", "weather", "--stop"]) == 0
    concluded = json.loads(capsys.readouterr().out)
    assert concluded["finished"] is True
    assert concluded["finished_already"] is False

    # --stop requires the accepted disposition.
    body = tmp_path / "review.md"
    body.write_text("redo it", encoding="utf-8")
    assert handoffctl.main([
        "conclude", "--run", "weather", "--disposition", "changes-requested",
        "--body-file", str(body), "--stop",
    ]) == 2
    assert "--disposition accepted" in capsys.readouterr().err

    # A lost-response retry through standalone stop is idempotent success.
    assert handoffctl.main(["stop", "--run", "weather"]) == 0
    stopped = json.loads(capsys.readouterr().out)
    assert stopped["finished_already"] is True


def test_cli_retry_seconds_is_accepted_and_threaded(tmp_path):
    state = tmp_path / "orchestrator" / "watcher.json"

    watch_args = handoffctl.build_parser().parse_args([
        "watch", "--orchestrator-state", str(state), "--retry-seconds", "45",
    ])
    assert watch_args.retry_seconds == 45.0

    start_args = handoffctl.build_parser().parse_args([
        "orchestrator", "start", "--state", str(state), "--retry-seconds", "45",
    ])
    assert start_args.retry_seconds == 45.0

    command = handoffctl._surface_watcher_command(state, 5.0, 45.0)
    assert shlex.split(command)[-2:] == ["--retry-seconds", "45.0"]

    invalid = run_cli("watch", "--orchestrator-state", state, "--retry-seconds", "0")
    assert invalid.returncode == 2
    assert "--retry-seconds must be positive" in invalid.stderr


def test_cmux_doorbell_omits_workspace_flag_when_unresolved(monkeypatch):
    calls = []

    class Completed:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def fake_run(argv, check=True):
        calls.append(argv)
        return Completed()

    monkeypatch.setattr(handoffctl.handoff_launcher, "_run", fake_run)
    record = {
        "session_transport": "cmux",
        "handle": "b071b964-8bc9-4af3-bbe7-46d6c36e27f9",
    }

    sent, error = handoffctl._ring_doorbell(record, "run-1", 3)

    assert sent is True
    assert error is None
    # _ring_doorbell builds a workspace-less adapter; the cmux CLI resolves the
    # surface's workspace server-side, so no argv may pin --workspace.
    assert calls
    for argv in calls:
        assert "--workspace" not in argv
        assert "--surface" in argv
    assert calls[0][1] == "send"
    assert calls[1][1] == "send-key"
    assert calls[2][1] == "read-screen"
