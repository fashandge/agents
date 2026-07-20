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
        orchestrator_token_file=private / "coordinator.token",
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
    return {
        **initialized,
        "private": private,
        "worker": (private / "worker.token").read_bytes(),
        "orchestrator": (private / "coordinator.token").read_bytes(),
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
    result = handoff.emit(
        run_dir, run["worker"], type="result", body="done",
        data={
            "head": None, "dirty": False, "stage": "complete", "inbox_cursor": 0,
            "verification": [], "commitments": [],
        },
    )
    handoff.control_consume(run_dir, run["orchestrator"], through=1)
    review = handoff.send(
        run_dir, run["orchestrator"], type="review",
        reply_to=result["message"]["message_id"], data={"disposition": "accepted"},
    )
    handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="done",
        data={
            "state": "succeeded", "stage": "complete", "current_activity": "done",
            "inbox_cursor": review["message"]["seq"], "commitments": [],
        },
    )
    handoff.control_release(run_dir, run["orchestrator"])

    with pytest.raises(handoff.HandoffError, match="cannot accept a new instruction"):
        handoffctl._dispatch_registered("weather", "do something else")

    control = handoff.control_show(run_dir)
    assert handoff._parse_time(control["coordinator_lease_expires_at"]) <= handoff._now()
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
    sent = handoffctl.dispatch(_send_args(run["run_dir"], run["private"] / "coordinator.token", body))

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
        run["run_dir"], run["private"] / "coordinator.token", body, no_doorbell=True,
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
        orchestrator_token_file=private / "coordinator.token",
        worker_token_file=private / "worker.token",
    )
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(tmp_path / "registry.json"))

    body = tmp_path / "body.txt"
    body.write_text("steer body", encoding="utf-8")
    sent = handoffctl.dispatch(_send_args(initialized["run_dir"], private / "coordinator.token", body))

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


def test_cli_registers_shows_and_enforces_detached_watcher_options(tmp_path):
    state = tmp_path / "orchestrator" / "watcher.json"
    registered = run_cli(
        "orchestrator", "register", "--state", state,
        "--transport", "tmux", "--target", "orchestrator:0.4",
        "--owner-pid", os.getpid(),
    )
    assert registered.returncode == 0, registered.stderr
    value = json.loads(registered.stdout)
    assert value["target"] == {"handle": "orchestrator:0.4"}

    shown = run_cli("orchestrator", "show", "--state", state)
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == value

    conflict = run_cli(
        "watch", "--orchestrator-state", state, "--once",
    )
    assert conflict.returncode == 2
    assert "cannot be combined" in conflict.stderr


def test_cli_coordinator_alias_still_resolves(tmp_path):
    state = tmp_path / "orchestrator" / "watcher.json"
    registered = run_cli(
        "coordinator", "register", "--state", state,
        "--transport", "tmux", "--target", "orchestrator:0.4",
        "--owner-pid", os.getpid(),
        "--coordinator-id", "b071b964-8bc9-4af3-bbe7-46d6c36e27f9",
    )
    assert registered.returncode == 0, registered.stderr
    assert (
        json.loads(registered.stdout)["coordinator_id"]
        == "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    )

    shown = run_cli("coordinator", "show", "--state", state)
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == json.loads(registered.stdout)

    conflict = run_cli("watch", "--coordinator-state", state, "--once")
    assert conflict.returncode == 2
    assert "cannot be combined" in conflict.stderr


def test_cli_orchestrator_dismiss_resolves_registered_run_and_writes_cursor(
    tmp_path, monkeypatch,
):
    run = registered_run(tmp_path, monkeypatch)
    state = tmp_path / "orchestrator" / "watcher.json"
    registered = run_cli(
        "orchestrator", "register", "--state", state,
        "--transport", "tmux", "--target", "orchestrator:0.4",
        "--owner-pid", os.getpid(),
    )
    assert registered.returncode == 0, registered.stderr
    orchestrator_id = json.loads(registered.stdout)["coordinator_id"]
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
        notifier=lambda value, coverage: pytest.fail("working progress must not ring"),
    )

    dismissed = run_cli(
        "orchestrator", "dismiss", "--state", state, "--run", "weather",
    )

    assert dismissed.returncode == 0, dismissed.stderr
    assert json.loads(dismissed.stdout) == {
        "coordinator_id": orchestrator_id,
        "dismissed": {
            "run_id": run["run"]["run_id"],
            "dismissed_through": 1,
        },
    }
    assert handoff_watcher.read(state)["runs"][run["run"]["run_id"]][
        "dismissed_through"
    ] == 1


def test_cli_starts_one_detached_watcher_and_it_exits_with_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(tmp_path / "registry.json"))
    state = tmp_path / "orchestrator" / "watcher.json"
    owner = subprocess.Popen(
        [PYTHON, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        registered = run_cli(
            "orchestrator", "register", "--state", state,
            "--transport", "tmux", "--target", "orchestrator:0.4",
            "--owner-pid", owner.pid,
        )
        assert registered.returncode == 0, registered.stderr

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


def test_cli_explicitly_adopts_unowned_registry_run(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    state = tmp_path / "orchestrator" / "watcher.json"
    registered = run_cli(
        "orchestrator", "register", "--state", state,
        "--transport", "tmux", "--target", "orchestrator:0.4",
        "--owner-pid", os.getpid(),
    )
    assert registered.returncode == 0, registered.stderr

    adopted = run_cli(
        "runs", "adopt", "--run", "weather", "--orchestrator-state", state,
    )

    assert adopted.returncode == 0, adopted.stderr
    assert json.loads(adopted.stdout)["coordinator_id"] == json.loads(registered.stdout)["coordinator_id"]
    assert handoff_registry.resolve(run["run"]["run_id"])["coordinator_id"] == json.loads(registered.stdout)["coordinator_id"]
