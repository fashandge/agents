import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from agents.orchestration import handoffctl


PYTHON = "/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python"


def run_cli(*args, input=None):
    return subprocess.run(
        [PYTHON, "-m", "agents.orchestration.handoffctl", *map(str, args)],
        input=input,
        text=True,
        capture_output=True,
    )


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
