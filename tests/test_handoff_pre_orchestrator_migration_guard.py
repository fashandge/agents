import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents.orchestration import handoff
from agents.orchestration import handoff_registry
from agents.orchestration import handoff_watcher


def run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "agents.orchestration.handoffctl", *map(str, args)],
        text=True,
        capture_output=True,
        env=env,
    )


def initialized_run(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("migration guard test", encoding="utf-8")
    private = tmp_path / "private"
    initialized = handoff.initialize(
        workspace=workspace,
        kickoff=kickoff,
        harness="codex",
        model="gpt-test",
        effort="high",
        transport="tmux",
        run_dir=tmp_path / "run",
        recovery_token_file=private / "recovery.token",
        orchestrator_token_file=private / "orchestrator.token",
        worker_token_file=private / "worker.token",
    )
    return Path(initialized["run_dir"])


def assert_migration_error(result, *, found, path):
    assert result.returncode == handoff.PRE_ORCHESTRATOR_RENAME_EXIT_CODE
    assert result.stdout == ""
    assert (
        f'handoff state predates the orchestrator rename (found "{found}" in {path})'
        in result.stderr
    )
    assert "scripts/migrate_handoff_state_orchestrator.py --dry-run" in result.stderr
    assert "without --dry-run to apply" in result.stderr


def register_one(registry):
    handoff_registry.register(
        path=registry,
        run_id="run-one",
        name="migration-test",
        run_dir="/state/run-one",
        run_uri="/state/run-one",
        host=None,
        remote_python=None,
        transport="tmux",
        session_transport="tmux",
        handle="worker-run-one",
        agent="codex",
        model="gpt-test",
        effort="high",
        credential_dir="/private/run-one",
    )


def test_registry_old_only_id_gets_actionable_migration_exit(tmp_path):
    registry = tmp_path / "registry.json"
    register_one(registry)
    value = json.loads(registry.read_text(encoding="utf-8"))
    record = value["runs"]["run-one"]
    record["coordinator_id"] = record.pop("orchestrator_id")
    registry.write_text(json.dumps(value), encoding="utf-8")
    env = {**os.environ, "HANDOFF_REGISTRY_FILE": str(registry)}

    result = run_cli("runs", "list", env=env)

    assert_migration_error(result, found="coordinator_id", path=registry)


def test_registry_both_ids_keeps_generic_validation_error(tmp_path):
    registry = tmp_path / "registry.json"
    register_one(registry)
    value = json.loads(registry.read_text(encoding="utf-8"))
    value["runs"]["run-one"]["coordinator_id"] = None
    registry.write_text(json.dumps(value), encoding="utf-8")
    env = {**os.environ, "HANDOFF_REGISTRY_FILE": str(registry)}

    result = run_cli("runs", "list", env=env)

    assert result.returncode == 5
    assert "registry record has invalid fields" in result.stderr
    assert "migrate_handoff_state_orchestrator.py" not in result.stderr


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("coordinator_epoch", "orchestrator_epoch"),
        ("coordinator_token_sha256", "orchestrator_token_sha256"),
        ("coordinator_lease_expires_at", "orchestrator_lease_expires_at"),
    ],
)
def test_control_old_only_field_gets_actionable_migration_exit(tmp_path, old, new):
    run_dir = initialized_run(tmp_path)
    control_path = run_dir / "control.json"
    value = json.loads(control_path.read_text(encoding="utf-8"))
    value[old] = value.pop(new)
    control_path.write_text(json.dumps(value), encoding="utf-8")

    result = run_cli("control", "show", "--run-dir", run_dir)

    assert_migration_error(result, found=old, path=control_path)


def test_control_both_fields_keeps_generic_validation_error(tmp_path):
    run_dir = initialized_run(tmp_path)
    control_path = run_dir / "control.json"
    value = json.loads(control_path.read_text(encoding="utf-8"))
    value["coordinator_epoch"] = value["orchestrator_epoch"]
    control_path.write_text(json.dumps(value), encoding="utf-8")

    result = run_cli("control", "show", "--run-dir", run_dir)

    assert result.returncode == 5
    assert "control fields differ" in result.stderr
    assert "migrate_handoff_state_orchestrator.py" not in result.stderr


def test_watcher_old_only_id_gets_actionable_migration_exit(tmp_path):
    watcher_path = tmp_path / "watcher" / "watcher.json"
    handoff_watcher.initialize(
        watcher_path,
        transport="tmux",
        target={"handle": "orchestrator:0.1"},
        owner_pid=os.getpid(),
    )
    value = json.loads(watcher_path.read_text(encoding="utf-8"))
    value["coordinator_id"] = value.pop("orchestrator_id")
    watcher_path.write_text(json.dumps(value), encoding="utf-8")

    result = run_cli("orchestrator", "show", "--state", watcher_path)

    assert_migration_error(result, found="coordinator_id", path=watcher_path)


def test_watcher_both_ids_keeps_generic_validation_error(tmp_path):
    watcher_path = tmp_path / "watcher" / "watcher.json"
    value = handoff_watcher.initialize(
        watcher_path,
        transport="tmux",
        target={"handle": "orchestrator:0.1"},
        owner_pid=os.getpid(),
    )
    value["coordinator_id"] = value["orchestrator_id"]
    watcher_path.write_text(json.dumps(value), encoding="utf-8")

    result = run_cli("orchestrator", "show", "--state", watcher_path)

    assert result.returncode == 5
    assert "invalid orchestrator watcher snapshot" in result.stderr
    assert "migrate_handoff_state_orchestrator.py" not in result.stderr


def inbox_message(kind):
    return {
        "protocol_version": 1,
        "seq": 1,
        "message_id": "b071b964-8bc9-4af3-bbe7-46d6c36e27f9",
        "writer_epoch": 2,
        "ts": "2026-07-20T12:00:00.000000Z",
        "type": kind,
        "reply_to": None,
        "body": "Taking over orchestration",
        "data": {
            "previous_epoch": 1,
            "new_epoch": 2,
            "reason": "recovery",
            "forced": False,
        },
        "attachments": [],
    }


def test_inbox_old_takeover_gets_actionable_migration_exit(tmp_path):
    run_dir = initialized_run(tmp_path)
    inbox_path = run_dir / "inbox.jsonl"
    inbox_path.write_text(
        json.dumps(inbox_message("coordinator-takeover"), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    result = run_cli("read", "--run-dir", run_dir, "--journal", "inbox")

    assert_migration_error(result, found="coordinator-takeover", path=inbox_path)


def test_inbox_unrelated_bad_type_keeps_generic_validation_error(tmp_path):
    run_dir = initialized_run(tmp_path)
    inbox_path = run_dir / "inbox.jsonl"
    inbox_path.write_text(
        json.dumps(inbox_message("unknown-takeover"), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    result = run_cli("read", "--run-dir", run_dir, "--journal", "inbox")

    assert result.returncode == 5
    assert "invalid inbox message type" in result.stderr
    assert "migrate_handoff_state_orchestrator.py" not in result.stderr
