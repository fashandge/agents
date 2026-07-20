import json
import stat

import pytest

from agents.orchestration import handoff
from agents.orchestration import handoff_registry


def record(registry, *, run_id, name="weather", host=None, credential_dir="/private/run"):
    return handoff_registry.register(
        path=registry,
        run_id=run_id,
        name=name,
        run_dir=f"/state/{run_id}",
        run_uri=f"ssh://worker/state/{run_id}" if host else f"/state/{run_id}",
        host=host,
        remote_python="/opt/python" if host else None,
        transport="ssh_tmux" if host else "tmux",
        session_transport="tmux",
        handle=f"worker-{run_id}",
        agent="claude",
        model="opus",
        effort="low",
        credential_dir=None if host else credential_dir,
    )


def live_run(tmp_path, registry, *, run_id="run-one"):
    workspace = tmp_path / f"workspace-{run_id}"
    workspace.mkdir()
    kickoff = tmp_path / f"kickoff-{run_id}.md"
    kickoff.write_text("get the weather", encoding="utf-8")
    private = tmp_path / f"credentials-{run_id}"
    initialized = handoff.initialize(
        workspace=workspace,
        kickoff=kickoff,
        harness="claude",
        model="opus",
        effort="low",
        transport="tmux",
        run_dir=tmp_path / "state" / run_id,
        recovery_token_file=private / "recovery.token",
        orchestrator_token_file=private / "orchestrator.token",
        worker_token_file=private / "worker.token",
    )
    handoff_registry.register(
        path=registry,
        run_id=run_id,
        name=run_id,
        run_dir=initialized["run_dir"],
        run_uri=initialized["run_dir"],
        host=None,
        remote_python=None,
        transport="tmux",
        session_transport="tmux",
        handle=f"worker-{run_id}",
        agent="claude",
        model="opus",
        effort="low",
        credential_dir=str(private),
    )
    return {**initialized, "private": private}


def fail_run(run):
    worker = (run["private"] / "worker.token").read_bytes()
    handoff.emit(
        run["run_dir"], worker, type="error", body="fatal test failure",
        data={
            "fatal": True, "category": "test", "stage": "lookup",
            "inbox_cursor": 0, "commitments": [],
        },
    )
    assert handoff.status(run["run_dir"])["state"] == "failed"


def test_registry_is_private_resolvable_and_redacts_credentials(tmp_path):
    registry = tmp_path / "private" / "registry.json"
    registered = record(registry, run_id="run-one")

    assert registered["credential_dir"] == "/private/run"
    assert stat.S_IMODE(registry.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600
    assert json.loads(registry.read_text())["version"] == 1
    assert "credential_dir" not in handoff_registry.resolve(
        "weather", path=registry, private=False,
    )
    assert handoff_registry.resolve("run-", path=registry)["run_id"] == "run-one"


def test_registry_preserves_observed_cursor_on_reregistration(tmp_path):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    handoff_registry.update_observed("run-one", 4, path=registry)
    record(registry, run_id="run-one", name="renamed")

    assert handoff_registry.resolve("renamed", path=registry)["observed_outbox_cursor"] == 4


def test_registry_rejects_ambiguous_name(tmp_path):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    record(registry, run_id="run-two")

    with pytest.raises(handoff.HandoffError, match="ambiguous"):
        handoff_registry.resolve("weather", path=registry)


def test_registry_ownership_changes_only_through_explicit_adoption(tmp_path):
    registry = tmp_path / "registry.json"
    first_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    second_id = "d9c8bf2e-09de-40e4-b1dc-1f35d354a13b"
    record(registry, run_id="run-one")

    with pytest.raises(handoff.HandoffError, match="explicit adoption"):
        handoff_registry.register(
            path=registry,
            run_id="run-one",
            name="weather",
            run_dir="/state/run-one",
            run_uri="/state/run-one",
            host=None,
            remote_python=None,
            transport="tmux",
            session_transport="tmux",
            handle="worker-run-one",
            agent="claude",
            model="opus",
            effort="low",
            credential_dir="/private/run",
            orchestrator_id=first_id,
        )

    adopted = handoff_registry.adopt("run-one", first_id, path=registry)
    assert adopted["orchestrator_id"] == first_id
    with pytest.raises(handoff.HandoffError, match="another orchestrator"):
        handoff_registry.adopt("run-one", second_id, path=registry)


def test_forget_removes_terminal_run_record_and_preserves_run_files(tmp_path):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry)
    fail_run(run)

    result = handoff_registry.forget("run-one", path=registry)

    assert result["removed"]["run_id"] == "run-one"
    assert result["removed"]["credential_dir"] == str(run["private"])
    assert result["note"] is None
    assert handoff_registry.list_records(path=registry) == []
    # Removal is registry-only: the run and its credentials stay inspectable.
    assert handoff.status(run["run_dir"])["state"] == "failed"
    assert (run["private"] / "recovery.token").exists()
    assert (run["private"] / "worker.token").exists()


def test_forget_refuses_non_terminal_run_without_force(tmp_path):
    registry = tmp_path / "registry.json"
    live_run(tmp_path, registry)

    with pytest.raises(handoff.HandoffError, match="refusing to forget") as excinfo:
        handoff_registry.forget("run-one", path=registry)

    assert excinfo.value.exit_code == 4
    assert "starting" in str(excinfo.value)
    assert handoff_registry.resolve("run-one", path=registry)["run_id"] == "run-one"


def test_forget_force_overrides_non_terminal_refusal(tmp_path):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry)

    result = handoff_registry.forget("run-one", path=registry, force=True)

    assert "force" in result["note"]
    assert "starting" in result["note"]
    assert handoff_registry.list_records(path=registry) == []
    assert handoff.status(run["run_dir"])["state"] == "starting"


def test_forget_removes_dangling_record_and_says_so(tmp_path):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")

    result = handoff_registry.forget("run-one", path=registry)

    assert result["removed"]["run_id"] == "run-one"
    assert "dangling" in result["note"]
    assert handoff_registry.list_records(path=registry) == []


def test_forget_unknown_run_is_a_refusal(tmp_path):
    registry = tmp_path / "registry.json"

    with pytest.raises(handoff.HandoffError, match="unknown handoff run") as excinfo:
        handoff_registry.forget("run-one", path=registry)

    assert excinfo.value.exit_code == 4


def test_prune_dry_run_changes_nothing_and_reports_skips(tmp_path):
    registry = tmp_path / "registry.json"
    terminal = live_run(tmp_path, registry, run_id="run-terminal")
    fail_run(terminal)
    live_run(tmp_path, registry, run_id="run-live")
    record(registry, run_id="run-dangling")
    before = registry.read_text()

    result = handoff_registry.prune(path=registry, dry_run=True)

    assert result["dry_run"] is True
    removed = {entry["record"]["run_id"]: entry for entry in result["removed"]}
    assert set(removed) == {"run-terminal", "run-dangling"}
    assert removed["run-terminal"]["note"] is None
    assert "dangling" in removed["run-dangling"]["note"]
    assert [skip["run_id"] for skip in result["skipped"]] == ["run-live"]
    assert "starting" in result["skipped"][0]["reason"]
    assert registry.read_text() == before


def test_prune_removes_terminal_records_and_preserves_run_files(tmp_path):
    registry = tmp_path / "registry.json"
    terminal = live_run(tmp_path, registry, run_id="run-terminal")
    fail_run(terminal)
    live_run(tmp_path, registry, run_id="run-live")

    result = handoff_registry.prune(path=registry)

    assert result["dry_run"] is False
    assert [entry["record"]["run_id"] for entry in result["removed"]] == ["run-terminal"]
    assert [skip["run_id"] for skip in result["skipped"]] == ["run-live"]
    assert [item["run_id"] for item in handoff_registry.list_records(path=registry)] == ["run-live"]
    assert handoff.status(terminal["run_dir"])["state"] == "failed"
    assert (terminal["private"] / "recovery.token").exists()
    assert (terminal["private"] / "worker.token").exists()


def test_prune_filters_by_age_and_host(tmp_path):
    registry = tmp_path / "registry.json"
    old = live_run(tmp_path, registry, run_id="run-old")
    fail_run(old)
    fresh = live_run(tmp_path, registry, run_id="run-fresh")
    fail_run(fresh)
    value = json.loads(registry.read_text())
    value["runs"]["run-old"]["registered_at"] = "2020-01-01T00:00:00.000000Z"
    registry.write_text(json.dumps(value), encoding="utf-8")

    aged = handoff_registry.prune(path=registry, older_than=30)
    assert [entry["record"]["run_id"] for entry in aged["removed"]] == ["run-old"]

    record(registry, run_id="run-local", name="local")
    record(registry, run_id="run-remote", name="remote", host="oci-box")
    hosted = handoff_registry.prune(path=registry, host="oci-box")
    assert hosted["removed"] == []
    assert [skip["run_id"] for skip in hosted["skipped"]] == ["run-remote"]
    assert "remote" in hosted["skipped"][0]["reason"]
    remaining = {item["run_id"] for item in handoff_registry.list_records(path=registry)}
    assert remaining == {"run-fresh", "run-local", "run-remote"}


def test_prune_no_terminal_only_includes_live_records_with_a_note(tmp_path):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry, run_id="run-live")

    result = handoff_registry.prune(path=registry, terminal_only=False)

    assert result["skipped"] == []
    entry = result["removed"][0]
    assert entry["record"]["run_id"] == "run-live"
    assert "not known to be terminal" in entry["note"]
    assert handoff_registry.list_records(path=registry) == []
    assert handoff.status(run["run_dir"])["state"] == "starting"


def test_prune_rejects_negative_age(tmp_path):
    registry = tmp_path / "registry.json"

    with pytest.raises(handoff.HandoffError, match="older-than") as excinfo:
        handoff_registry.prune(path=registry, older_than=-1)

    assert excinfo.value.exit_code == 2


def test_validate_records_reports_bad_records_individually(tmp_path):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    value = json.loads(registry.read_text())
    value["runs"]["run-broken"] = {"run_id": "run-broken", "name": "broken"}
    registry.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(handoff.HandoffError):
        handoff_registry.list_records(path=registry)

    report = handoff_registry.validate_records(path=registry)

    assert report["valid"] == ["run-one"]
    assert len(report["invalid"]) == 1
    assert report["invalid"][0]["key"] == "run-broken"
    assert report["invalid"][0]["run_id"] == "run-broken"
    assert "invalid fields" in report["invalid"][0]["error"]


def corrupt_record(registry, key="run-broken", **extra):
    value = json.loads(registry.read_text())
    value["runs"][key] = {"run_id": key, "name": "broken", **extra}
    registry.write_text(json.dumps(value), encoding="utf-8")


def test_forget_removes_corrupt_record_when_strict_reads_fail(tmp_path):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    corrupt_record(registry)
    with pytest.raises(handoff.HandoffError):
        handoff_registry.list_records(path=registry)

    result = handoff_registry.forget("run-broken", path=registry)

    assert result["removed"]["run_id"] == "run-broken"
    assert "invalid" in result["note"]
    assert [item["run_id"] for item in handoff_registry.list_records(path=registry)] == ["run-one"]


def test_forget_valid_record_while_another_record_is_corrupt(tmp_path):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    corrupt_record(registry)

    result = handoff_registry.forget("run-one", path=registry)

    assert "dangling" in result["note"]
    remaining = json.loads(registry.read_text())["runs"]
    assert set(remaining) == {"run-broken"}


def pre_rename_record(registry, key="run-old"):
    """A record invalid only because it predates the orchestrator rename."""
    value = json.loads(registry.read_text())
    stale = dict(value["runs"][next(iter(value["runs"]))])
    stale["run_id"] = key
    stale["coordinator_id"] = stale.pop("orchestrator_id", None)
    value["runs"][key] = stale
    registry.write_text(json.dumps(value), encoding="utf-8")


def test_forget_refuses_pre_rename_record_and_points_at_the_migration(tmp_path):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    pre_rename_record(registry)

    with pytest.raises(handoff.HandoffError) as excinfo:
        handoff_registry.forget("run-old", path=registry)

    assert excinfo.value.exit_code == handoff.PRE_ORCHESTRATOR_RENAME_EXIT_CODE
    assert "predates the orchestrator rename" in str(excinfo.value)
    assert "migrate_handoff_state_orchestrator.py" in str(excinfo.value)
    # The record survives: migration restores it, deletion would not.
    assert "run-old" in json.loads(registry.read_text())["runs"]


def test_forget_force_still_removes_a_pre_rename_record(tmp_path):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    pre_rename_record(registry)

    result = handoff_registry.forget("run-old", path=registry, force=True)

    assert result["removed"]["run_id"] == "run-old"
    assert set(json.loads(registry.read_text())["runs"]) == {"run-one"}


def test_forget_still_reports_generic_corruption_normally(tmp_path):
    """The migration guidance must not swallow unrelated corruption."""
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    corrupt_record(registry)

    result = handoff_registry.forget("run-broken", path=registry)

    assert "predates the orchestrator rename" not in (result["note"] or "")
    assert "invalid" in result["note"]


def test_prune_points_pre_rename_records_at_the_migration(tmp_path):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    pre_rename_record(registry)

    report = handoff_registry.prune(path=registry, dry_run=True)

    reason = next(item["reason"] for item in report["skipped"] if item["run_id"] == "run-old")
    assert "predates the orchestrator rename" in reason
    assert "runs forget" not in reason


def test_forget_refuses_invalid_record_pointing_at_a_live_run_without_force(tmp_path):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry)
    corrupt_record(registry, run_dir=run["run_dir"], host=None)

    with pytest.raises(handoff.HandoffError, match="refusing to forget") as excinfo:
        handoff_registry.forget("run-broken", path=registry)

    assert excinfo.value.exit_code == 4
    assert "invalid" in str(excinfo.value)

    result = handoff_registry.forget("run-broken", path=registry, force=True)
    assert "invalid" in result["note"]
    assert handoff.status(run["run_dir"])["state"] == "starting"


def test_prune_skips_corrupt_records_with_a_reason(tmp_path):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    corrupt_record(registry)

    result = handoff_registry.prune(path=registry)

    assert [entry["record"]["run_id"] for entry in result["removed"]] == ["run-one"]
    assert [skip["run_id"] for skip in result["skipped"]] == ["run-broken"]
    assert "invalid" in result["skipped"][0]["reason"]
    remaining = json.loads(registry.read_text())["runs"]
    assert set(remaining) == {"run-broken"}
