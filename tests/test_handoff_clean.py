import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agents.orchestration import handoff
from agents.orchestration import handoff_clean
from agents.orchestration import handoff_launcher
from agents.orchestration import handoff_registry


ORCH_A = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
ORCH_B = "d9c8bf2e-09de-40e4-b1dc-1f35d354a13b"


def record(registry, *, run_id, name="weather", host=None, credential_dir="/private/run",
           run_dir=None, orchestrator_id=None, session_transport="tmux", handle=None):
    return handoff_registry.register(
        path=registry,
        run_id=run_id,
        name=name,
        run_dir=run_dir or f"/state/{run_id}",
        run_uri=f"ssh://worker/state/{run_id}" if host else f"/state/{run_id}",
        host=host,
        remote_python="/opt/python" if host else None,
        transport="ssh_tmux" if host else session_transport,
        session_transport=session_transport,
        handle=handle or f"worker-{run_id}",
        agent="claude",
        model="opus",
        effort="low",
        credential_dir=None if host else credential_dir,
        orchestrator_id=orchestrator_id,
    )


def live_run(tmp_path, registry, *, run_id="run-one", session_transport="tmux", handle=None):
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
        transport=session_transport,
        session_transport=session_transport,
        handle=handle or f"worker-{run_id}",
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
    assert handoff.is_fatal(Path(run["run_dir"])) is True


def stop_run(run):
    # Construct a pre-collapse control snapshot.  Legacy desired_state stays
    # readable and terminal for cleanup, but current code cannot write it.
    control = handoff.control_show(Path(run["run_dir"]))
    control["desired_state"] = "stop"
    handoff._atomic_json(Path(run["run_dir"]) / "control.json", control)  # noqa: SLF001
    assert handoff.control_show(run["run_dir"])["desired_state"] == "stop"


def pause_run(run):
    orchestrator = (run["private"] / "orchestrator.token").read_bytes()
    worker = (run["private"] / "worker.token").read_bytes()
    run_dir = Path(run["run_dir"])
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
        reply_to=result["message"]["message_id"], data={"disposition": "accepted"},
    )
    handoff.emit(
        run_dir, worker, type="checkpoint", body="paused",
        data={
            "state": "paused", "stage": "complete", "current_activity": "paused",
            "inbox_cursor": review["message"]["seq"], "commitments": [],
        },
    )
    assert handoff.status(run_dir)["state"] == "paused"
    assert "desired_state" not in handoff.control_show(run_dir)


def corrupt_record(registry, key="run-broken", **extra):
    value = json.loads(registry.read_text())
    value["runs"][key] = {"run_id": key, "name": "broken", **extra}
    registry.write_text(json.dumps(value), encoding="utf-8")


def pre_rename_record(registry, key="run-old"):
    """A record invalid only because it predates the orchestrator rename."""
    value = json.loads(registry.read_text())
    stale = dict(value["runs"][next(iter(value["runs"]))])
    stale["run_id"] = key
    stale["coordinator_id"] = stale.pop("orchestrator_id", None)
    value["runs"][key] = stale
    registry.write_text(json.dumps(value), encoding="utf-8")


class FakeTmux:
    """Answers tmux has-session/kill-session for a fixed set of live sessions."""

    def __init__(self, *live_handles):
        self.live = set(live_handles)
        self.calls = []

    def __call__(self, argv, *, check=True):
        self.calls.append(argv)
        if argv[:3] == ["tmux", "has-session", "-t"]:
            return subprocess.CompletedProcess(argv, 0 if argv[3] in self.live else 1, "", "")
        if argv[:3] == ["tmux", "kill-session", "-t"]:
            self.live.discard(argv[3])
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


# --- Single-run mode ---------------------------------------------------------


def test_clean_single_terminal_run_removes_record_and_preserves_run(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry)
    fail_run(run)
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux("worker-run-live"))

    result = handoff_clean.clean(selector="run-one", path=registry)

    entry = result["cleaned"][0]
    assert entry["run_id"] == "run-one"
    assert entry["note"] == "worker reported a fatal error"
    assert entry["session"] == "none"
    assert entry["record_removed"] is True
    assert "credential_dir" not in entry["record"]
    assert result["dry_run"] is False
    assert handoff_registry.list_records(path=registry) == []
    # The run directory and credential files are untouched.
    assert handoff.status(run["run_dir"])["state"] == "starting"
    assert handoff.is_fatal(Path(run["run_dir"])) is True
    assert (run["private"] / "recovery.token").exists()
    assert (run["private"] / "worker.token").exists()


def test_clean_single_unknown_run_is_a_refusal(tmp_path):
    registry = tmp_path / "registry.json"

    with pytest.raises(handoff.HandoffError, match="unknown handoff run") as excinfo:
        handoff_clean.clean(selector="run-one", path=registry)

    assert excinfo.value.exit_code == 4


# --- Bulk confirmation and dry-run -------------------------------------------


def test_clean_bulk_dry_run_changes_nothing_and_reports_skips(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    terminal = live_run(tmp_path, registry, run_id="run-terminal")
    fail_run(terminal)
    live_run(tmp_path, registry, run_id="run-live")
    record(registry, run_id="run-dangling")
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux("worker-run-live"))
    before = registry.read_text()

    result = handoff_clean.clean(path=registry, dry_run=True)

    assert result["dry_run"] is True
    cleaned = {entry["run_id"]: entry for entry in result["cleaned"]}
    assert set(cleaned) == {"run-terminal", "run-dangling"}
    assert cleaned["run-terminal"]["note"] == "worker reported a fatal error"
    assert cleaned["run-terminal"]["record_removed"] is False
    assert "dangling" in cleaned["run-dangling"]["note"]
    assert [skip["run_id"] for skip in result["skipped"]] == ["run-live"]
    assert "starting" in result["skipped"][0]["reason"]
    assert registry.read_text() == before


def test_clean_bulk_removes_terminal_records_and_preserves_run_files(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    terminal = live_run(tmp_path, registry, run_id="run-terminal")
    fail_run(terminal)
    live_run(tmp_path, registry, run_id="run-live")
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux("worker-run-live"))

    result = handoff_clean.clean(path=registry)

    assert result["dry_run"] is False
    assert [entry["run_id"] for entry in result["cleaned"]] == ["run-terminal"]
    assert [skip["run_id"] for skip in result["skipped"]] == ["run-live"]
    assert [item["run_id"] for item in handoff_registry.list_records(path=registry)] == ["run-live"]
    assert handoff.status(terminal["run_dir"])["state"] == "starting"
    assert handoff.is_fatal(Path(terminal["run_dir"])) is True
    assert (terminal["private"] / "recovery.token").exists()


def test_clean_bulk_force_includes_live_records_with_a_note(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry, run_id="run-live")
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux("worker-run-live"))

    result = handoff_clean.clean(path=registry, force=True)

    assert result["skipped"] == []
    entry = result["cleaned"][0]
    assert entry["run_id"] == "run-live"
    assert "not known to be terminal" in entry["note"]
    assert handoff_registry.list_records(path=registry) == []
    assert handoff.status(run["run_dir"])["state"] == "starting"


# --- Unified terminality -----------------------------------------------------


def test_clean_desired_state_stop_counts_as_terminal(tmp_path, monkeypatch):
    """A concluded worker never emits a terminal checkpoint; desired_state stop
    must still let cleanup proceed."""
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry)
    stop_run(run)
    assert handoff.status(run["run_dir"])["state"] == "starting"
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux())

    result = handoff_clean.clean(path=registry)

    entry = result["cleaned"][0]
    assert entry["run_id"] == "run-one"
    assert "desired_state" in entry["note"]
    assert handoff_registry.list_records(path=registry) == []


def test_clean_skips_a_paused_run_until_forced(tmp_path, monkeypatch):
    """A paused worker idles resumably and is not terminal, so
    cleanup must leave the run (and its resident session) alone unless forced."""
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry)
    pause_run(run)
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux("worker-run-one"))

    result = handoff_clean.clean(path=registry)

    assert result["cleaned"] == []
    assert [skip["run_id"] for skip in result["skipped"]] == ["run-one"]
    assert "paused" in result["skipped"][0]["reason"]
    assert len(handoff_registry.list_records(path=registry)) == 1
    assert handoff.status(run["run_dir"])["state"] == "paused"

    forced = handoff_clean.clean(path=registry, force=True)

    assert [entry["run_id"] for entry in forced["cleaned"]] == ["run-one"]
    assert "not known to be terminal" in forced["cleaned"][0]["note"]
    assert handoff_registry.list_records(path=registry) == []


def test_clean_dangling_run_dir_is_terminal(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux())

    result = handoff_clean.clean(selector="run-one", path=registry)

    assert result["cleaned"][0]["run_id"] == "run-one"
    assert "dangling" in result["cleaned"][0]["note"]
    assert handoff_registry.list_records(path=registry) == []


@pytest.mark.parametrize("fact", ["fatal", "finished", "dead"])
def test_clean_fact_filters_select_and_reap_only_matching_runs(tmp_path, monkeypatch, fact):
    registry = tmp_path / "registry.json"
    fatal_run = live_run(tmp_path, registry, run_id="run-fatal")
    fail_run(fatal_run)
    live_run(tmp_path, registry, run_id="run-finished")
    handoff_registry.mark_finished("run-finished", path=registry)
    live_run(tmp_path, registry, run_id="run-dead")
    # A missing session is a positive transport fact.  The other two sessions
    # remain live so each selective cleanup demonstrates its own predicate.
    tmux = FakeTmux("worker-run-fatal", "worker-run-finished")
    monkeypatch.setattr(handoff_launcher, "_run", tmux)

    result = handoff_clean.clean(path=registry, **{fact: True})

    assert [entry["run_id"] for entry in result["cleaned"]] == [f"run-{fact}"]
    assert result["scope"]["facts"] == [fact]
    assert {skip["run_id"] for skip in result["skipped"]} == {
        "run-fatal", "run-finished", "run-dead",
    } - {f"run-{fact}"}
    assert {item["run_id"] for item in handoff_registry.list_records(path=registry)} == {
        "run-fatal", "run-finished", "run-dead",
    } - {f"run-{fact}"}


# --- Local session killing ----------------------------------------------------


def test_clean_kills_live_tmux_session_when_terminal(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry)
    fail_run(run)
    tmux = FakeTmux("worker-run-one")
    monkeypatch.setattr(handoff_launcher, "_run", tmux)

    result = handoff_clean.clean(selector="run-one", path=registry)

    assert result["cleaned"][0]["session"] == "kill"
    assert ["tmux", "kill-session", "-t", "worker-run-one"] in tmux.calls
    assert tmux.live == set()
    assert handoff_registry.list_records(path=registry) == []


def test_clean_never_kills_session_of_a_live_run_without_force(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    live_run(tmp_path, registry)
    tmux = FakeTmux("worker-run-one")
    monkeypatch.setattr(handoff_launcher, "_run", tmux)

    result = handoff_clean.clean(path=registry)

    assert result["cleaned"] == []
    assert [skip["run_id"] for skip in result["skipped"]] == ["run-one"]
    assert not any(call[:2] == ["tmux", "kill-session"] for call in tmux.calls)
    assert tmux.live == {"worker-run-one"}
    assert len(handoff_registry.list_records(path=registry)) == 1


def test_clean_dry_run_reports_a_live_terminal_session_without_killing(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry)
    fail_run(run)
    tmux = FakeTmux("worker-run-one")
    monkeypatch.setattr(handoff_launcher, "_run", tmux)

    result = handoff_clean.clean(path=registry, dry_run=True)

    assert result["cleaned"][0]["session"] == "kill"
    assert tmux.live == {"worker-run-one"}
    assert len(handoff_registry.list_records(path=registry)) == 1


def test_clean_tmux_unreachable_skips_instead_of_presuming_absence(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry)
    fail_run(run)

    def unreachable(argv, *, check=True):
        raise handoff_launcher.AdapterError("tmux binary unavailable")

    monkeypatch.setattr(handoff_launcher, "_run", unreachable)

    result = handoff_clean.clean(selector="run-one", path=registry)

    assert result["cleaned"] == []
    assert "liveness is unknown" in result["skipped"][0]["reason"]
    assert handoff_registry.resolve("run-one", path=registry)["run_id"] == "run-one"


# --- cmux surface closing ------------------------------------------------------

TARGET = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "11111111-2222-3333-4444-555555555555"
WORKSPACE = "99999999-8888-7777-6666-555555555555"


def make_fake_cmux(*, drop_other_on_close=False):
    state = {"closed": False}

    def fake(argv, *, check=True):
        if "list-pane-surfaces" in argv:
            surfaces = [] if state["closed"] else [f"pane:1 {TARGET} worker-run-one"]
            if not (state["closed"] and drop_other_on_close):
                surfaces.append(f"pane:2 {OTHER} other")
            return subprocess.CompletedProcess(argv, 0, "\n".join(surfaces) + "\n", "")
        if "list-workspaces" in argv:
            return subprocess.CompletedProcess(argv, 0, f"workspace:1 {WORKSPACE}\n", "")
        if "close-surface" in argv:
            state["closed"] = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    return fake


def test_clean_closes_cmux_surface_with_layout_verification(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry, session_transport="cmux", handle=TARGET)
    fail_run(run)
    monkeypatch.setattr(handoff_launcher, "_run", make_fake_cmux())
    monkeypatch.setattr(handoff_clean, "_CMUX_VERIFY_SETTLE_SECONDS", 0)

    result = handoff_clean.clean(selector="run-one", path=registry)

    entry = result["cleaned"][0]
    assert entry["session"] == "kill"
    assert entry["session_note"] is None
    assert handoff_registry.list_records(path=registry) == []


def test_clean_cmux_unreachable_skips_instead_of_presuming_absence(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry, session_transport="cmux", handle=TARGET)
    fail_run(run)

    def unreachable(argv, *, check=True):
        raise handoff_launcher.AdapterError("cmux socket refused")

    monkeypatch.setattr(handoff_launcher, "_run", unreachable)

    result = handoff_clean.clean(selector="run-one", path=registry)

    assert result["cleaned"] == []
    assert "liveness is unknown" in result["skipped"][0]["reason"]
    assert handoff_registry.resolve("run-one", path=registry)["run_id"] == "run-one"


def test_clean_cmux_layout_change_beyond_target_is_a_loud_error(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry, session_transport="cmux", handle=TARGET)
    fail_run(run)
    monkeypatch.setattr(handoff_launcher, "_run", make_fake_cmux(drop_other_on_close=True))
    monkeypatch.setattr(handoff_clean, "_CMUX_VERIFY_SETTLE_SECONDS", 0)

    with pytest.raises(handoff.HandoffError, match="STOP all further layout actions") as excinfo:
        handoff_clean.clean(selector="run-one", path=registry)

    assert excinfo.value.exit_code == 1
    assert excinfo.value.report["errors"][0]["run_id"] == "run-one"
    # The surface was closed, so the cleaned run's record is still dropped.
    assert handoff_registry.list_records(path=registry) == []


# --- Run directory deletion ----------------------------------------------------


def test_clean_delete_run_dir_removes_run_files_but_keeps_credentials(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry)
    fail_run(run)
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux())

    result = handoff_clean.clean(selector="run-one", path=registry, delete_run_dir=True)

    entry = result["cleaned"][0]
    assert entry["run_dir_deleted"] is True
    assert entry["run_dir_note"] is None
    assert handoff_registry.list_records(path=registry) == []
    assert not Path(run["run_dir"]).exists()
    # Credentials live outside the run directory and survive deletion.
    assert (run["private"] / "recovery.token").exists()
    assert (run["private"] / "worker.token").exists()


def test_clean_delete_run_dir_refuses_a_directory_without_status(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    fake = tmp_path / "not-a-run"
    fake.mkdir()
    record(registry, run_id="run-one", run_dir=str(fake))
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux())

    result = handoff_clean.clean(selector="run-one", path=registry, force=True, delete_run_dir=True)

    assert result["cleaned"] == []
    assert [skip["run_id"] for skip in result["skipped"]] == ["run-one"]
    assert "refusing to delete" in result["skipped"][0]["reason"]
    assert fake.exists()
    assert handoff_registry.resolve("run-one", path=registry)["run_id"] == "run-one"


def test_clean_delete_run_dir_dry_run_reports_without_deleting(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    terminal = live_run(tmp_path, registry)
    fail_run(terminal)
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux())

    result = handoff_clean.clean(path=registry, dry_run=True, delete_run_dir=True)

    assert result["cleaned"][0]["run_dir_deleted"] is True
    assert Path(terminal["run_dir"]).exists()
    assert len(handoff_registry.list_records(path=registry)) == 1


# --- Bulk filters ---------------------------------------------------------------


def test_clean_bulk_filters_by_age_and_host(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    old = live_run(tmp_path, registry, run_id="run-old")
    fail_run(old)
    fresh = live_run(tmp_path, registry, run_id="run-fresh")
    fail_run(fresh)
    value = json.loads(registry.read_text())
    value["runs"]["run-old"]["registered_at"] = "2020-01-01T00:00:00.000000Z"
    registry.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux())

    aged = handoff_clean.clean(path=registry, older_than=30)
    assert [entry["run_id"] for entry in aged["cleaned"]] == ["run-old"]

    hosted = handoff_clean.clean(path=registry, host="oci-box")
    assert hosted["cleaned"] == []
    remaining = {item["run_id"] for item in handoff_registry.list_records(path=registry)}
    assert remaining == {"run-fresh"}


def test_clean_bulk_scopes_to_one_orchestrator(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-mine", orchestrator_id=ORCH_A)
    record(registry, run_id="run-theirs", orchestrator_id=ORCH_B)
    record(registry, run_id="run-orphan")
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux())

    result = handoff_clean.clean(path=registry, orchestrator_id=ORCH_A)

    assert [entry["run_id"] for entry in result["cleaned"]] == ["run-mine"]
    remaining = {item["run_id"] for item in handoff_registry.list_records(path=registry)}
    assert remaining == {"run-theirs", "run-orphan"}


def test_clean_rejects_negative_age(tmp_path):
    registry = tmp_path / "registry.json"

    with pytest.raises(handoff.HandoffError, match="older-than") as excinfo:
        handoff_clean.clean(path=registry, older_than=-1)

    assert excinfo.value.exit_code == 2


# --- Invalid and pre-rename records ----------------------------------------------


def test_clean_bulk_never_removes_invalid_records(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    corrupt_record(registry)
    pre_rename_record(registry)
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux())

    result = handoff_clean.clean(path=registry)

    assert [entry["run_id"] for entry in result["cleaned"]] == ["run-one"]
    skipped = {skip["run_id"]: skip["reason"] for skip in result["skipped"]}
    assert "invalid" in skipped["run-broken"]
    assert "runs clean --run" in skipped["run-broken"]
    assert "predates the orchestrator rename" in skipped["run-old"]
    assert "migrate_handoff_state_orchestrator.py" in skipped["run-old"]
    remaining = json.loads(registry.read_text())["runs"]
    assert set(remaining) == {"run-broken", "run-old"}


def test_clean_single_is_the_escape_hatch_for_a_corrupt_record(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    corrupt_record(registry)
    with pytest.raises(handoff.HandoffError):
        handoff_registry.list_records(path=registry)
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux())

    result = handoff_clean.clean(selector="run-broken", path=registry)

    entry = result["cleaned"][0]
    assert entry["run_id"] == "run-broken"
    assert "invalid" in entry["note"]
    assert entry["session"] == "none"
    assert [item["run_id"] for item in handoff_registry.list_records(path=registry)] == ["run-one"]


def test_clean_single_skips_a_corrupt_record_pointing_at_a_live_run(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry)
    corrupt_record(registry, run_dir=run["run_dir"], host=None)
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux())

    result = handoff_clean.clean(selector="run-broken", path=registry)

    assert result["cleaned"] == []
    assert "invalid" in result["skipped"][0]["reason"]
    assert "--force" in result["skipped"][0]["reason"]

    forced = handoff_clean.clean(selector="run-broken", path=registry, force=True)
    assert "invalid" in forced["cleaned"][0]["note"]
    assert handoff.status(run["run_dir"])["state"] == "starting"


def test_clean_single_pre_rename_record_points_at_the_migration(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    record(registry, run_id="run-one")
    pre_rename_record(registry)
    monkeypatch.setattr(handoff_launcher, "_run", FakeTmux())

    with pytest.raises(handoff.HandoffError) as excinfo:
        handoff_clean.clean(selector="run-old", path=registry)

    assert excinfo.value.exit_code == handoff.PRE_ORCHESTRATOR_RENAME_EXIT_CODE
    assert "predates the orchestrator rename" in str(excinfo.value)
    assert "migrate_handoff_state_orchestrator.py" in str(excinfo.value)
    # The record survives: migration restores it, deletion would not.
    assert "run-old" in json.loads(registry.read_text())["runs"]

    result = handoff_clean.clean(selector="run-old", path=registry, force=True)
    assert result["cleaned"][0]["run_id"] == "run-old"
    assert set(json.loads(registry.read_text())["runs"]) == {"run-one"}


# --- Remote runs via the SSH probe -----------------------------------------------


def remote_record(registry, run_id, *, host="oci-box", handle=None, orchestrator_id=None):
    return record(
        registry, run_id=run_id, host=host,
        handle=handle or f"remote-{run_id}", orchestrator_id=orchestrator_id,
    )


def fake_ssh_returning(rows, seen=None):
    def fake(host, argv, *, stdin, timeout):
        if seen is not None:
            seen["host"] = host
            seen["mode"] = argv[3]
            seen["payload"] = json.loads(base64.b64decode(argv[4]))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")
    return fake


def test_group_by_host_partitions_by_host_and_python():
    registry_records = [
        {"run_id": "a", "host": "oci-box", "remote_python": "/py"},
        {"run_id": "b", "host": "oci-box", "remote_python": "/py"},
        {"run_id": "c", "host": "other", "remote_python": None},
    ]
    groups = handoff_clean._group_by_host(registry_records)
    assert len(groups[("oci-box", "/py")]) == 2
    assert groups[("other", handoff_launcher.REMOTE_PYTHON_DEFAULT)][0]["run_id"] == "c"


def test_clean_remote_dry_run_reports_without_killing(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    remote_record(registry, "run-done")
    remote_record(registry, "run-live")
    monkeypatch.setattr(handoff_launcher, "_run_ssh", fake_ssh_returning([
        {"run_id": "run-done", "handle": "remote-run-done", "run_dir": "/state/run-done",
         "run_dir_present": True, "state": "awaiting_review", "desired_state": "stop",
         "session_alive": True, "terminal": True},
        {"run_id": "run-live", "handle": "remote-run-live", "run_dir": "/state/run-live",
         "run_dir_present": True, "state": "working", "desired_state": "run",
         "session_alive": True, "terminal": False},
    ]))
    before = registry.read_text()

    result = handoff_clean.clean(path=registry, dry_run=True)

    assert result["dry_run"] is True
    entry = result["cleaned"][0]
    assert entry["run_id"] == "run-done"
    assert entry["session"] == "kill"  # planned only
    assert entry["record_removed"] is False
    assert "desired_state" in entry["note"]
    assert [skip["run_id"] for skip in result["skipped"]] == ["run-live"]
    assert "not terminal" in result["skipped"][0]["reason"]
    assert registry.read_text() == before


def test_clean_remote_kill_mode_kills_and_removes_records(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    remote_record(registry, "run-done")
    remote_record(registry, "run-live")
    seen = {}
    monkeypatch.setattr(handoff_launcher, "_run_ssh", fake_ssh_returning([
        {"run_id": "run-done", "handle": "remote-run-done", "run_dir": "/state/run-done",
         "run_dir_present": True, "state": "stopped", "desired_state": "stop",
         "session_alive": True, "terminal": True, "killed": True},
        {"run_id": "run-live", "handle": "remote-run-live", "run_dir": "/state/run-live",
         "run_dir_present": True, "state": "working", "desired_state": "run",
         "session_alive": True, "terminal": False},
    ], seen))

    result = handoff_clean.clean(path=registry)

    assert seen["mode"] == "kill"
    entry = result["cleaned"][0]
    assert entry["run_id"] == "run-done"
    assert entry["session"] == "kill"
    assert entry["record_removed"] is True
    assert entry["host"] == "oci-box"
    assert [skip["run_id"] for skip in result["skipped"]] == ["run-live"]
    assert [item["run_id"] for item in handoff_registry.list_records(path=registry)] == ["run-live"]


def test_clean_remote_fact_filter_is_forwarded_before_any_kill(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    remote_record(registry, "run-finished")
    remote_record(registry, "run-fatal")
    seen = {}
    monkeypatch.setattr(handoff_launcher, "_run_ssh", fake_ssh_returning([
        {
            "run_id": "run-finished", "handle": "remote-run-finished",
            "run_dir": "/state/run-finished", "run_dir_present": True,
            "state": "paused", "session_alive": True, "terminal": True,
            "finished": True, "fatal": False, "selected": True, "killed": True,
        },
        {
            "run_id": "run-fatal", "handle": "remote-run-fatal",
            "run_dir": "/state/run-fatal", "run_dir_present": True,
            "state": "working", "session_alive": True, "terminal": True,
            "finished": False, "fatal": True, "selected": False,
        },
    ], seen))

    result = handoff_clean.clean(path=registry, finished=True)

    assert all(item["facts"] == ["finished"] for item in seen["payload"])
    assert [entry["run_id"] for entry in result["cleaned"]] == ["run-finished"]
    assert [skip["run_id"] for skip in result["skipped"]] == ["run-fatal"]
    assert handoff_registry.resolve("run-fatal", path=registry)["run_id"] == "run-fatal"


def test_clean_remote_terminal_without_session_still_cleans_the_record(tmp_path, monkeypatch):
    """A remote run whose session already exited is terminal + no session: the
    record is dropped, which prune could never do from the local host."""
    registry = tmp_path / "registry.json"
    remote_record(registry, "run-done")
    monkeypatch.setattr(handoff_launcher, "_run_ssh", fake_ssh_returning([
        {"run_id": "run-done", "handle": "remote-run-done", "run_dir": "/state/run-done",
         "run_dir_present": True, "state": "succeeded", "desired_state": "run",
         "session_alive": False, "terminal": True},
    ]))

    result = handoff_clean.clean(selector="run-done", path=registry)

    entry = result["cleaned"][0]
    assert entry["session"] == "none"
    assert entry["record_removed"] is True
    assert handoff_registry.list_records(path=registry) == []


def test_clean_remote_passes_force_and_delete_run_dir_to_the_probe(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    remote_record(registry, "run-live")
    seen = {}
    monkeypatch.setattr(handoff_launcher, "_run_ssh", fake_ssh_returning([
        {"run_id": "run-live", "handle": "remote-run-live", "run_dir": "/state/run-live",
         "run_dir_present": True, "state": "working", "desired_state": "run",
         "session_alive": True, "terminal": False, "killed": True, "run_dir_deleted": True},
    ], seen))

    result = handoff_clean.clean(path=registry, force=True, delete_run_dir=True)

    assert seen["payload"][0]["force"] is True
    assert seen["payload"][0]["delete_run_dir"] is True
    entry = result["cleaned"][0]
    assert "not known to be terminal" in entry["note"]
    assert entry["run_dir_deleted"] is True
    assert handoff_registry.list_records(path=registry) == []


def test_clean_remote_probe_error_is_reported_and_keeps_records(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    remote_record(registry, "run-x")

    def boom(host, argv, *, stdin, timeout):
        raise handoff_launcher.AdapterError("connect timed out")

    monkeypatch.setattr(handoff_launcher, "_run_ssh", boom)

    with pytest.raises(handoff.HandoffError) as excinfo:
        handoff_clean.clean(path=registry)

    assert excinfo.value.exit_code == 1
    report = excinfo.value.report
    assert report["errors"][0]["host"] == "oci-box"
    assert [skip["run_id"] for skip in report["skipped"]] == ["run-x"]
    assert len(handoff_registry.list_records(path=registry)) == 1


def test_clean_remote_scoped_by_host_and_orchestrator(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    remote_record(registry, "run-mine", orchestrator_id=ORCH_A)
    remote_record(registry, "run-theirs", orchestrator_id=ORCH_B)
    remote_record(registry, "run-other-host", host="other-box")
    seen = {}
    monkeypatch.setattr(handoff_launcher, "_run_ssh", fake_ssh_returning([], seen))

    result = handoff_clean.clean(path=registry, host="oci-box", orchestrator_id=ORCH_A)

    assert [r["run_id"] for r in seen["payload"]] == ["run-mine"]
    assert result["scope"] == {"older_than": None, "host": "oci-box", "orchestrator": ORCH_A}
    assert len(handoff_registry.list_records(path=registry)) == 3


# --- Real integration of the on-host probe/kill snippet against local tmux -----


def _run_probe_program(mode, runs):
    encoded = base64.b64encode(json.dumps(runs).encode()).decode()
    result = subprocess.run(
        [sys.executable, "-c", handoff_clean.REMOTE_PROBE_PROGRAM, mode, encoded],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(subprocess.run(["which", "tmux"], capture_output=True).returncode != 0,
                    reason="tmux not available")
def test_probe_program_never_kills_a_nonterminal_live_session(tmp_path):
    # Safety property with a REAL tmux session: an unreadable/absent run state
    # (status.json and control.json unreadable, run dir present) is not
    # terminal, so even in kill mode the live session must be left running.
    run_dir = tmp_path / "run-present-but-unreadable"
    run_dir.mkdir()
    session = "handoff-clean-selftest"
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "sleep 300"], check=True)
    try:
        runs = [{"run_id": "self", "handle": session, "run_dir": str(run_dir)}]
        probe = _run_probe_program("probe", runs)[0]
        assert probe["session_alive"] is True
        assert probe["terminal"] is False
        # Kill mode must NOT reap a non-terminal run's session.
        kill = _run_probe_program("kill", runs)[0]
        assert "killed" not in kill
        assert subprocess.run(["tmux", "has-session", "-t", session],
                              capture_output=True).returncode == 0
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)


def test_probe_program_counts_desired_state_stop_as_terminal(tmp_path):
    registry = tmp_path / "registry.json"
    run = live_run(tmp_path, registry)
    stop_run(run)

    probe = _run_probe_program("probe", [
        {"run_id": "run-one", "handle": "no-such-session", "run_dir": run["run_dir"]},
    ])[0]

    assert probe["state"] == "starting"  # non-terminal status projection
    assert probe["desired_state"] == "stop"
    assert probe["terminal"] is True


def test_probe_program_counts_a_missing_run_dir_as_terminal(tmp_path):
    probe = _run_probe_program("probe", [
        {"run_id": "gone", "handle": "no-such-session", "run_dir": str(tmp_path / "no-run")},
    ])[0]

    assert probe["run_dir_present"] is False
    assert probe["state"] is None
    assert probe["terminal"] is True


def test_probe_program_dead_filter_deletes_nonterminal_run_dir(tmp_path):
    run_dir = tmp_path / "dead-nonterminal-run"
    run_dir.mkdir()
    # Deletion has a narrow path-safety guard independent of protocol
    # readability.  Keep status.json present while deliberately leaving the
    # run nonterminal/unreadable to isolate positive session absence.
    (run_dir / "status.json").write_text("{}", encoding="utf-8")

    result = _run_probe_program("kill", [{
        "run_id": "dead-nonterminal",
        "handle": "no-such-handoff-session",
        "run_dir": str(run_dir),
        "facts": ["dead"],
        "delete_run_dir": True,
        "force": False,
    }])[0]

    assert result["session_alive"] is False
    assert result["terminal"] is False
    assert result["selected"] is True
    assert result["run_dir_deleted"] is True
    assert not run_dir.exists()
