import importlib.util
import json
import subprocess
from pathlib import Path

from agents.orchestration import handoff
from agents.orchestration import handoff_launcher
from agents.orchestration import handoff_registry


PYTHON = "/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python"
SCRIPT = Path(__file__).parents[1] / "scripts" / "migrate_handoff_state_journals.py"


def load_migration_module():
    spec = importlib.util.spec_from_file_location("handoff_state_journal_migration", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_finished_run(state_dir):
    workspace = state_dir / "workspace"
    workspace.mkdir(parents=True)
    kickoff = state_dir / "kickoff.md"
    kickoff.write_text("legacy migration test", encoding="utf-8")
    private = state_dir / "credentials"
    initialized = handoff.initialize(
        workspace=workspace,
        kickoff=kickoff,
        harness="claude",
        model="opus",
        effort="low",
        transport="tmux",
        run_dir=state_dir / "runs" / "run-one",
        recovery_token_file=private / "recovery.token",
        orchestrator_token_file=private / "orchestrator.token",
        worker_token_file=private / "worker.token",
    )
    run_dir = Path(initialized["run_dir"])
    worker = (private / "worker.token").read_bytes()
    orchestrator = (private / "orchestrator.token").read_bytes()
    result = handoff.emit(
        run_dir, worker, type="result", body="done",
        data={
            "head": None, "dirty": False, "stage": "complete", "inbox_cursor": 0,
            "verification": [], "commitments": [],
        },
    )
    handoff.control_consume(run_dir, orchestrator, through=result["message"]["seq"])
    review = handoff.send(
        run_dir, orchestrator, type="review",
        reply_to=result["message"]["message_id"],
        data={"disposition": "accepted"},
    )
    integration = handoff.control_integrate(run_dir, orchestrator, commit="deadbeef")
    control = handoff.control_show(run_dir)
    control["desired_state"] = "stop"
    handoff._atomic_json(run_dir / "control.json", control)  # noqa: SLF001
    handoff.emit(
        run_dir, worker, type="checkpoint", body="legacy success",
        data={
            "state": "succeeded", "stage": "complete", "current_activity": "done",
            "inbox_cursor": integration["message"]["seq"], "commitments": [],
        },
    )
    status = handoff.status(run_dir)
    status["state"] = "succeeded"
    handoff._atomic_json(run_dir / "status.json", status)  # noqa: SLF001
    handoff_registry.register(
        path=state_dir / "registry.json",
        run_id=initialized["run"]["run_id"],
        name="legacy",
        run_dir=str(run_dir),
        run_uri=str(run_dir),
        host=None,
        remote_python=None,
        transport="tmux",
        session_transport="tmux",
        handle="legacy-worker",
        agent="claude",
        model="opus",
        effort="low",
        credential_dir=str(private),
    )
    registry_path = state_dir / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    del registry["runs"][initialized["run"]["run_id"]]["finished_at"]
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    assert review["message"]["seq"] < integration["message"]["seq"]
    assert handoff.doctor(run_dir)["ok"]
    return initialized


def test_migration_maps_status_removes_desired_state_and_backfills_finished(tmp_path):
    state_dir = tmp_path / "handoff"
    run = legacy_finished_run(state_dir)
    run_dir = Path(run["run_dir"])

    preview = subprocess.run(
        [PYTHON, str(SCRIPT), "--dry-run", "--state-dir", str(state_dir)],
        capture_output=True, text=True, check=True,
    )
    preview_report = json.loads(preview.stdout)
    assert preview_report["change_count"] == 3
    assert {item["category"] for item in preview_report["changes"]} == {
        "control", "status", "registry",
    }
    assert handoff.status(run_dir)["state"] == "succeeded"
    assert handoff.control_show(run_dir)["desired_state"] == "stop"

    migrated = subprocess.run(
        [PYTHON, str(SCRIPT), "--state-dir", str(state_dir)],
        capture_output=True, text=True, check=True,
    )
    report = json.loads(migrated.stdout)
    assert report["change_count"] == 3
    assert Path(report["backup"]).is_dir()
    assert handoff.status(run_dir)["state"] == "paused"
    assert "desired_state" not in handoff.control_show(run_dir)
    record = handoff_registry.resolve("legacy", path=state_dir / "registry.json")
    assert record["finished_at"] is not None
    assert handoff.doctor(run_dir)["ok"]

    repeated = subprocess.run(
        [PYTHON, str(SCRIPT), "--state-dir", str(state_dir)],
        capture_output=True, text=True, check=True,
    )
    repeated_report = json.loads(repeated.stdout)
    assert repeated_report["change_count"] == 0
    assert repeated_report["backup"] is None


def test_remote_finished_marker_is_mirrored_from_the_owning_host(tmp_path, monkeypatch):
    migration = load_migration_module()
    state_dir = tmp_path / "handoff"
    state_dir.mkdir()
    registry = state_dir / "registry.json"
    handoff_registry.register(
        path=registry,
        run_id="remote-run", name="remote", run_dir="/remote/run",
        run_uri="ssh://worker/remote/run", host="worker",
        remote_python="/remote/python", transport="ssh_tmux",
        session_transport="tmux", handle="remote-worker", agent="codex",
        model="gpt-test", effort="high", credential_dir=None,
    )
    value = json.loads(registry.read_text(encoding="utf-8"))
    del value["runs"]["remote-run"]["finished_at"]
    registry.write_text(json.dumps(value), encoding="utf-8")
    timestamp = "2026-07-22T12:00:00.000000Z"
    monkeypatch.setattr(
        migration, "_probe_remote_finished",
        lambda record, timeout: {"finished_at": timestamp, "desired_state": None},
    )

    report = migration.migrate(state_dir, dry_run=False, remote_timeout=1.0)

    assert report["warnings"] == []
    assert handoff_registry.resolve("remote", path=registry)["finished_at"] == timestamp
    assert migration.migrate(state_dir, dry_run=True, remote_timeout=1.0)["change_count"] == 0


def test_unreachable_remote_marker_is_skipped_and_reported(tmp_path, monkeypatch):
    migration = load_migration_module()
    state_dir = tmp_path / "handoff"
    state_dir.mkdir()
    registry = state_dir / "registry.json"
    handoff_registry.register(
        path=registry,
        run_id="remote-run", name="remote", run_dir="/remote/run",
        run_uri="ssh://worker/remote/run", host="worker",
        remote_python="/remote/python", transport="ssh_tmux",
        session_transport="tmux", handle="remote-worker", agent="codex",
        model="gpt-test", effort="high", credential_dir=None,
    )
    value = json.loads(registry.read_text(encoding="utf-8"))
    del value["runs"]["remote-run"]["finished_at"]
    registry.write_text(json.dumps(value), encoding="utf-8")

    def unreachable(record, timeout):
        raise handoff_launcher.AdapterError("SSH unavailable")

    monkeypatch.setattr(migration, "_probe_remote_finished", unreachable)

    report = migration.migrate(state_dir, dry_run=True, remote_timeout=1.0)

    assert report["change_count"] == 0
    assert "owning host unreachable" in report["warnings"][0]
    assert handoff_registry.resolve("remote", path=registry)["finished_at"] is None
