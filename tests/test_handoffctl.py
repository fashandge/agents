import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from agents.orchestration import handoffctl
from agents.orchestration import handoff
from agents.orchestration import handoff_registry


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
        coordinator_token_file=private / "coordinator.token",
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
        "coordinator": (private / "coordinator.token").read_bytes(),
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
        "--coordinator-token-file", private / "coordinator",
        "--worker-token-file", private / "worker",
    )
    assert init.returncode == 0, init.stderr
    assert json.loads(init.stdout)["run_dir"] == str(run_dir)
    body = tmp_path / "body"
    body.write_text("literal ; $(touch NEVER)")
    sent = run_cli(
        "send", "--run-dir", run_dir, "--type", "steer",
        "--token-file", private / "coordinator", "--body-file", body,
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
        "--recovery-token-file", private / "r", "--coordinator-token-file", private / "c",
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
        "--recovery-token-file", private / "recovery", "--coordinator-token-file", private / "coordinator",
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
                "--body", "Awaiting coordinator review",
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
    handoff.control_release(run_dir, run["coordinator"])
    doorbells = []

    class Adapter:
        def doorbell(self, handle, run_id, inbox_seq):
            doorbells.append((handle, run_id, inbox_seq))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Adapter)

    dispatched = handoffctl._dispatch_registered("weather", "refresh SF weather")

    assert dispatched["superseded_result"] == result["message"]["message_id"]
    assert dispatched["message"]["type"] == "supersede"
    assert dispatched["doorbell_sent"] is True
    assert dispatched["coordinator_released"] is True
    assert doorbells == [(
        "weather-worker", run["run"]["run_id"], dispatched["message"]["seq"],
    )]
    assert not list(run["private"].glob("coordinator-dispatch-*.token"))
    resumed = handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="resumed",
        data={
            "state": "working", "stage": "lookup", "current_activity": "refreshing",
            "inbox_cursor": dispatched["message"]["seq"], "commitments": [],
        },
    )
    assert resumed["status"]["state"] == "working"


def test_failed_dispatch_releases_ephemeral_coordinator(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    run_dir = Path(run["run_dir"])
    result = handoff.emit(
        run_dir, run["worker"], type="result", body="done",
        data={
            "head": None, "dirty": False, "stage": "complete", "inbox_cursor": 0,
            "verification": [], "commitments": [],
        },
    )
    handoff.control_consume(run_dir, run["coordinator"], through=1)
    review = handoff.send(
        run_dir, run["coordinator"], type="review",
        reply_to=result["message"]["message_id"], data={"disposition": "accepted"},
    )
    handoff.emit(
        run_dir, run["worker"], type="checkpoint", body="done",
        data={
            "state": "succeeded", "stage": "complete", "current_activity": "done",
            "inbox_cursor": review["message"]["seq"], "commitments": [],
        },
    )
    handoff.control_release(run_dir, run["coordinator"])

    with pytest.raises(handoff.HandoffError, match="cannot accept a new instruction"):
        handoffctl._dispatch_registered("weather", "do something else")

    control = handoff.control_show(run_dir)
    assert handoff._parse_time(control["coordinator_lease_expires_at"]) <= handoff._now()
    assert not list(run["private"].glob("coordinator-dispatch-*.token"))


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
    state = tmp_path / "coordinator" / "watcher.json"
    registered = run_cli(
        "coordinator", "register", "--state", state,
        "--transport", "tmux", "--target", "orchestrator:0.4",
    )
    assert registered.returncode == 0, registered.stderr
    value = json.loads(registered.stdout)
    assert value["target"] == {"handle": "orchestrator:0.4"}

    shown = run_cli("coordinator", "show", "--state", state)
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == value

    conflict = run_cli(
        "watch", "--coordinator-state", state, "--once",
    )
    assert conflict.returncode == 2
    assert "cannot be combined" in conflict.stderr


def test_cmux_coordinator_registration_resolves_current_workspace(tmp_path, monkeypatch):
    class Completed:
        stdout = "workspace:actual\n"

    monkeypatch.setattr(
        handoffctl.handoff_launcher,
        "_run",
        lambda argv: Completed(),
    )
    state = tmp_path / "coordinator" / "watcher.json"
    args = Namespace(
        state=state,
        transport="cmux",
        target=None,
        surface="b071b964-8bc9-4af3-bbe7-46d6c36e27f9",
        cmux_binary="/exact/cmux",
        coordinator_id=None,
    )

    value = handoffctl._register_coordinator(args)

    assert value["target"] == {
        "workspace": "workspace:actual",
        "surface": "b071b964-8bc9-4af3-bbe7-46d6c36e27f9",
    }


def test_cli_explicitly_adopts_unowned_registry_run(tmp_path, monkeypatch):
    run = registered_run(tmp_path, monkeypatch)
    state = tmp_path / "coordinator" / "watcher.json"
    registered = run_cli(
        "coordinator", "register", "--state", state,
        "--transport", "tmux", "--target", "orchestrator:0.4",
    )
    assert registered.returncode == 0, registered.stderr

    adopted = run_cli(
        "runs", "adopt", "--run", "weather", "--coordinator-state", state,
    )

    assert adopted.returncode == 0, adopted.stderr
    assert json.loads(adopted.stdout)["coordinator_id"] == json.loads(registered.stdout)["coordinator_id"]
    assert handoff_registry.resolve(run["run"]["run_id"])["coordinator_id"] == json.loads(registered.stdout)["coordinator_id"]
