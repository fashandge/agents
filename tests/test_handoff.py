import concurrent.futures
import json
import os
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

from agents import env
from agents.orchestration import handoff


@pytest.fixture
def run(tmp_path):
    workspace = tmp_path / "worker workspace"
    workspace.mkdir()
    kickoff = tmp_path / "kickoff ; $(touch NOPE).md"
    kickoff.write_text("Implement the task; literal $(touch PWNED).", encoding="utf-8")
    credentials = tmp_path / "private credentials"
    result = handoff.initialize(
        workspace=workspace,
        kickoff=kickoff,
        harness="codex",
        model="test-model",
        effort="high",
        transport="tmux",
        run_dir=tmp_path / "external state" / str(uuid.uuid4()),
        recovery_token_file=credentials / "recovery",
        orchestrator_token_file=credentials / "orchestrator",
        worker_token_file=credentials / "worker",
    )
    return {
        **result,
        "workspace": workspace,
        "recovery": (credentials / "recovery").read_bytes(),
        "orchestrator": (credentials / "orchestrator").read_bytes(),
        "worker": (credentials / "worker").read_bytes(),
    }


def checkpoint(run, cursor, *, state="working", message_id=None):
    return handoff.emit(
        Path(run["run_dir"]),
        run["worker"],
        type="checkpoint",
        message_id=message_id,
        body="checkpoint",
        data={
            "state": state,
            "stage": "implementation",
            "current_activity": "Working safely",
            "inbox_cursor": cursor,
            "commitments": ["preserve literal paths"],
        },
    )


def git(workspace, *args):
    result = subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env.build_env(),
    )
    return result.stdout.strip()


def git_run(tmp_path, *, dirty):
    workspace = tmp_path / "git workspace"
    workspace.mkdir()
    git(workspace, "init")
    tracked = workspace / "tracked.txt"
    tracked.write_text("committed\n")
    git(workspace, "add", "tracked.txt")
    git(
        workspace,
        "-c", "user.name=Handoff Test",
        "-c", "user.email=handoff@example.invalid",
        "commit", "-m", "initial",
    )
    if dirty:
        tracked.write_text("pre-existing user change\n")
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("Test dirty Git result publication.")
    credentials = tmp_path / "credentials"
    initialized = handoff.initialize(
        workspace=workspace,
        kickoff=kickoff,
        harness="codex",
        model="test-model",
        effort="high",
        transport="tmux",
        run_dir=tmp_path / "state" / str(uuid.uuid4()),
        recovery_token_file=credentials / "recovery",
        orchestrator_token_file=credentials / "orchestrator",
        worker_token_file=credentials / "worker",
    )
    return {
        **initialized,
        "workspace": workspace,
        "worker": (credentials / "worker").read_bytes(),
    }


def test_init_is_complete_private_and_external_to_workspace(run):
    run_dir = Path(run["run_dir"])
    assert run_dir.parent.name == "external state"
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert {path.name for path in run_dir.iterdir()} >= {
        "run.json", "kickoff.md", "control.json", "status.json",
        "inbox.jsonl", "outbox.jsonl", "progress.md", "attachments", "repairs",
    }
    assert "$(touch PWNED)" in (run_dir / "kickoff.md").read_text()
    assert "-m agents.orchestration.handoffctl" in (run_dir / "kickoff.md").read_text()
    assert handoff.sys.executable in (run_dir / "kickoff.md").read_text()
    assert "handoffctl ready --run-dir \"$HANDOFF_RUN_DIR\"" in (run_dir / "kickoff.md").read_text()
    assert "do not inspect helper source or help first" in (run_dir / "kickoff.md").read_text()
    assert "dirty Git workspace does not block work" in (run_dir / "kickoff.md").read_text()
    contract = (run_dir / "kickoff.md").read_text()
    assert "delegated worker in an orchestrator-managed run" in contract
    assert "Route scope changes, cross-worker coordination, final acceptance" in contract
    assert "orchestrator knows the kickoff but not your live progress" in contract
    assert "Every blocking question must be self-contained" in contract
    for required_context in (
        "current stage and completed work",
        "concrete evidence",
        "exact conflict",
        "decision or authority needed",
        "recommended resolution",
        "consequences of the available options",
        "actions intentionally deferred",
    ):
        assert required_context in contract
    assert run["run"]["workspace"]["path"] == str(run["workspace"])
    for kind in ("recovery", "orchestrator", "worker"):
        path = Path(run["credential_files"][kind])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_text().strip() not in (run_dir / "control.json").read_text()


def test_git_result_can_publish_from_preexisting_dirty_workspace(tmp_path):
    run = git_run(tmp_path, dirty=True)
    run_dir = Path(run["run_dir"])
    checkpoint(run, 0)

    result = handoff.emit(
        run_dir,
        run["worker"],
        type="result",
        body="completed without altering the user's existing change",
        data={
            "head": git(run["workspace"], "rev-parse", "HEAD"),
            "dirty": True,
            "stage": "done",
            "inbox_cursor": 0,
            "verification": [],
            "commitments": ["preserved pre-existing changes"],
        },
    )

    assert result["status"]["state"] == "awaiting_review"


@pytest.mark.parametrize(
    ("workspace_dirty", "reported_dirty"),
    [(False, True), (True, False)],
)
def test_git_result_rejects_incorrect_dirty_flag(tmp_path, workspace_dirty, reported_dirty):
    run = git_run(tmp_path, dirty=workspace_dirty)
    checkpoint(run, 0)

    with pytest.raises(handoff.HandoffError, match="dirty flag must match") as caught:
        handoff.emit(
            Path(run["run_dir"]),
            run["worker"],
            type="result",
            body="incorrect state",
            data={
                "head": git(run["workspace"], "rev-parse", "HEAD"),
                "dirty": reported_dirty,
                "stage": "done",
                "inbox_cursor": 0,
                "verification": [],
                "commitments": [],
            },
        )

    assert caught.value.exit_code == 4


def test_git_result_rejects_stale_head_even_when_dirty_flag_is_truthful(tmp_path):
    run = git_run(tmp_path, dirty=True)
    checkpoint(run, 0)

    with pytest.raises(handoff.HandoffError, match="name current HEAD") as caught:
        handoff.emit(
            Path(run["run_dir"]),
            run["worker"],
            type="result",
            body="stale head",
            data={
                "head": "0" * 40,
                "dirty": True,
                "stage": "done",
                "inbox_cursor": 0,
                "verification": [],
                "commitments": [],
            },
        )

    assert caught.value.exit_code == 4


def test_steer_cannot_be_consumed_across_unanswered_blocking_question(run):
    run_dir = Path(run["run_dir"])
    steer = handoff.send(run_dir, run["orchestrator"], type="steer", body="Do A")
    checkpoint(run, 0)
    question = handoff.emit(
        run_dir,
        run["worker"],
        type="question",
        body="Need a decision",
        data={
            "blocking": True, "stage": "implementation", "current_activity": "Waiting",
            "inbox_cursor": steer["message"]["seq"], "commitments": [],
        },
    )
    handoff.send(run_dir, run["orchestrator"], type="steer", body="Unrelated later steer")
    with pytest.raises(handoff.HandoffError, match="unanswered blocking") as caught:
        checkpoint(run, 2)
    assert caught.value.exit_code == 4
    handoff.send(
        run_dir, run["orchestrator"], type="answer", body="Proceed",
        reply_to=question["message"]["message_id"],
    )
    result = checkpoint(run, 3)
    assert result["status"]["state"] == "working"
    assert result["status"]["inbox_cursor"] == 3


def test_result_acceptance_success_and_integration_are_distinct(run):
    run_dir = Path(run["run_dir"])
    checkpoint(run, 0)
    result = handoff.emit(
        run_dir, run["worker"], type="result", body="done",
        data={"head": None, "dirty": True, "stage": "done", "inbox_cursor": 0, "verification": [], "commitments": []},
    )
    consumed = handoff.control_consume(run_dir, run["orchestrator"], through=2)
    assert result["status"]["state"] == "awaiting_review"
    assert consumed["control"]["review_state"] == "pending"
    assert consumed["control"]["integration"]["state"] == "pending"
    review = handoff.send(
        run_dir, run["orchestrator"], type="review",
        reply_to=result["message"]["message_id"], data={"disposition": "accepted"},
    )
    succeeded = checkpoint(run, review["message"]["seq"], state="succeeded")
    assert succeeded["status"]["state"] == "succeeded"
    assert review["control"]["review_state"] == "accepted"
    assert review["control"]["integration"]["state"] == "pending"
    integrated = handoff.control_integrate(run_dir, run["orchestrator"], commit="integration-commit")
    assert integrated["control"]["integration"]["state"] == "integrated"


def test_supersede_resumes_awaiting_review_and_binds_fresh_result(run):
    run_dir = Path(run["run_dir"])
    checkpoint(run, 0)
    first = handoff.emit(
        run_dir, run["worker"], type="result", body="first result",
        data={
            "head": None, "dirty": True, "stage": "done", "inbox_cursor": 0,
            "verification": [], "commitments": [],
        },
    )
    handoff.control_consume(run_dir, run["orchestrator"], through=2)

    supersede = handoff.send(
        run_dir, run["orchestrator"], type="supersede",
        reply_to=first["message"]["message_id"], body="refresh the lookup",
    )
    assert supersede["control"]["review_state"] == "superseded"
    resumed = checkpoint(run, supersede["message"]["seq"])
    assert resumed["status"]["state"] == "working"

    second = handoff.emit(
        run_dir, run["worker"], type="result", body="fresh result",
        data={
            "head": None, "dirty": True, "stage": "done",
            "inbox_cursor": supersede["message"]["seq"],
            "verification": [], "commitments": [],
        },
    )
    consumed = handoff.control_consume(
        run_dir, run["orchestrator"], through=second["message"]["seq"],
    )
    assert consumed["control"]["review_state"] == "pending"
    assert consumed["control"]["review_result_id"] == second["message"]["message_id"]
    assert handoff.doctor(run_dir)["ok"]


def test_supersede_requires_current_awaiting_review_result(run):
    run_dir = Path(run["run_dir"])
    checkpoint(run, 0)

    with pytest.raises(handoff.HandoffError, match="current result"):
        handoff.send(
            run_dir, run["orchestrator"], type="supersede",
            reply_to=str(uuid.uuid4()), body="invalid resume",
        )


def test_only_latest_review_disposition_authorizes_worker_transition(run):
    run_dir = Path(run["run_dir"])
    checkpoint(run, 0)
    result = handoff.emit(
        run_dir, run["worker"], type="result", body="candidate",
        data={"head": None, "dirty": True, "stage": "done", "inbox_cursor": 0, "verification": [], "commitments": []},
    )
    handoff.control_consume(run_dir, run["orchestrator"], through=2)
    handoff.send(
        run_dir, run["orchestrator"], type="review", reply_to=result["message"]["message_id"],
        data={"disposition": "accepted"},
    )
    changed = handoff.send(
        run_dir, run["orchestrator"], type="review", body="one more fix",
        reply_to=result["message"]["message_id"], data={"disposition": "changes_requested"},
    )
    with pytest.raises(handoff.HandoffError, match="accepted review"):
        checkpoint(run, changed["message"]["seq"], state="succeeded")
    assert checkpoint(run, changed["message"]["seq"])["status"]["state"] == "working"


def test_message_id_retry_repairs_projection_without_duplicate(run, monkeypatch):
    run_dir = Path(run["run_dir"])
    message_id = str(uuid.uuid4())
    original = handoff._atomic_json
    calls = 0

    def crash_once(path, value):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated crash after journal fsync")
        return original(path, value)

    monkeypatch.setattr(handoff, "_atomic_json", crash_once)
    with pytest.raises(OSError, match="simulated crash"):
        handoff.send(run_dir, run["orchestrator"], type="steer", body="retry me", message_id=message_id)
    monkeypatch.setattr(handoff, "_atomic_json", original)
    retried = handoff.send(run_dir, run["orchestrator"], type="steer", body="retry me", message_id=message_id)
    assert retried["message"]["seq"] == 1
    assert retried["control"]["last_command_seq"] == 1
    assert len(handoff.read(run_dir, "inbox")) == 1
    with pytest.raises(handoff.HandoffError, match="different content"):
        handoff.send(run_dir, run["orchestrator"], type="steer", body="different", message_id=message_id)


def test_worker_retry_repairs_status_without_duplicate(run, monkeypatch):
    run_dir = Path(run["run_dir"])
    message_id = str(uuid.uuid4())
    original = handoff._atomic_json

    def crash(path, value):
        raise OSError("worker crashed after append")

    monkeypatch.setattr(handoff, "_atomic_json", crash)
    with pytest.raises(OSError, match="worker crashed"):
        checkpoint(run, 0, message_id=message_id)
    monkeypatch.setattr(handoff, "_atomic_json", original)
    retried = checkpoint(run, 0, message_id=message_id)
    assert retried["status"]["outbox_seq"] == 1
    assert len(handoff.read(run_dir, "outbox")) == 1
    assert (run_dir / "progress.md").read_text().count("<!-- outbox-seq:1 -->") == 1


def test_concurrent_same_role_sends_are_contiguous(run):
    run_dir = Path(run["run_dir"])

    def one(index):
        return handoff.send(run_dir, run["orchestrator"], type="steer", body=f"message {index}")["message"]["seq"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(one, range(24)))
    assert sorted(sequences) == list(range(1, 25))
    assert [item["seq"] for item in handoff.read(run_dir, "inbox")] == list(range(1, 25))


def test_simultaneous_roles_follow_one_lock_order_without_deadlock(run):
    run_dir = Path(run["run_dir"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        sent = pool.submit(handoff.send, run_dir, run["orchestrator"], type="steer", body="parallel steer")
        emitted = pool.submit(checkpoint, run, 0)
        assert sent.result(timeout=5)["message"]["seq"] == 1
        assert emitted.result(timeout=5)["message"]["seq"] == 1


def test_snapshot_readers_never_observe_partial_atomic_replace(run):
    run_dir = Path(run["run_dir"])

    def renew_many():
        for _ in range(50):
            handoff.control_renew(run_dir, run["orchestrator"])

    revisions = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(renew_many)
        while not writer.done():
            revisions.append(handoff.control_show(run_dir)["revision"])
        writer.result(timeout=5)
    revisions.append(handoff.control_show(run_dir)["revision"])
    assert revisions == sorted(revisions)
    assert revisions[-1] == 51


def test_takeover_fences_old_orchestrator_token(run, tmp_path):
    run_dir = Path(run["run_dir"])
    handoff.control_release(run_dir, run["orchestrator"])
    replacement = tmp_path / "replacement orchestrator"
    takeover = handoff.control_takeover(
        run_dir, run["recovery"], new_token_file=replacement, reason="owner resumed",
    )
    assert takeover["control"]["coordinator_epoch"] == 2
    with pytest.raises(handoff.HandoffError) as caught:
        handoff.send(run_dir, run["orchestrator"], type="steer", body="stale writer")
    assert caught.value.exit_code == 3
    fresh = handoff.send(run_dir, replacement.read_bytes(), type="steer", body="fresh writer")
    assert fresh["message"]["writer_epoch"] == 2


def test_appended_takeover_can_be_explicitly_repaired(run, tmp_path, monkeypatch):
    run_dir = Path(run["run_dir"])
    handoff.control_release(run_dir, run["orchestrator"])
    replacement = tmp_path / "replacement after crash"
    original = handoff._atomic_json
    monkeypatch.setattr(handoff, "_atomic_json", lambda path, value: (_ for _ in ()).throw(OSError("projection crash")))
    with pytest.raises(OSError, match="projection crash"):
        handoff.control_takeover(run_dir, run["recovery"], new_token_file=replacement, reason="recover crash")
    monkeypatch.setattr(handoff, "_atomic_json", original)
    assert replacement.exists()
    assert handoff.control_show(run_dir)["coordinator_epoch"] == 1
    report = handoff.repair_control(
        run_dir, run["recovery"], recovery=True, new_token=replacement.read_bytes(),
    )
    assert report["repaired"]
    assert handoff.control_show(run_dir)["coordinator_epoch"] == 2
    handoff.send(run_dir, replacement.read_bytes(), type="steer", body="new owner works")


def test_worker_rotation_crash_repairs_control_then_status(run, tmp_path, monkeypatch):
    run_dir = Path(run["run_dir"])
    checkpoint(run, 0)
    replacement = tmp_path / "replacement worker"
    original = handoff._atomic_json
    monkeypatch.setattr(handoff, "_atomic_json", lambda path, value: (_ for _ in ()).throw(OSError("rotation crash")))
    with pytest.raises(OSError, match="rotation crash"):
        handoff.control_rotate_worker(
            run_dir, run["orchestrator"], new_token_file=replacement,
            reason="worker process died", confirmed_dead=True,
        )
    monkeypatch.setattr(handoff, "_atomic_json", original)
    handoff.repair_control(
        run_dir, run["orchestrator"], new_worker_token=replacement.read_bytes(),
    )
    report = handoff.repair_status(run_dir, replacement.read_bytes())
    assert report["repaired"]
    assert handoff.status(run_dir)["worker_epoch"] == 2
    assert checkpoint({**run, "worker": replacement.read_bytes()}, 1)["status"]["state"] == "working"


def test_partial_tail_is_explicitly_repaired_and_midfile_corruption_is_not(run):
    run_dir = Path(run["run_dir"])
    handoff.send(run_dir, run["orchestrator"], type="steer", body="valid")
    with (run_dir / "inbox.jsonl").open("ab") as stream:
        stream.write(b'{"protocol_version":1')
        stream.flush(); os.fsync(stream.fileno())
    report = handoff.doctor(run_dir)
    assert not report["ok"]
    assert any(issue["code"] == "partial-tail" for issue in report["issues"])
    repaired = handoff.repair_tail(run_dir, "inbox", run["orchestrator"])
    assert repaired["repaired"]
    assert len(handoff.read(run_dir, "inbox")) == 1
    assert list((run_dir / "repairs").glob("*-inbox-tail.bin"))

    raw = (run_dir / "inbox.jsonl").read_bytes()
    (run_dir / "inbox.jsonl").write_bytes(b"not-json\n" + raw)
    with pytest.raises(handoff.HandoffError, match="before final") as caught:
        handoff.repair_tail(run_dir, "inbox", run["orchestrator"])
    assert caught.value.exit_code == 5


def test_attachment_snapshot_is_literal_private_and_detects_tampering(run):
    run_dir = Path(run["run_dir"])
    artifact = run["workspace"] / "result ; $HOME.txt"
    artifact.write_text("safe bytes", encoding="utf-8")
    sent = handoff.send(
        run_dir, run["orchestrator"], type="steer", body="inspect literal path",
        attachments=[artifact.name], snapshot_attachments=True,
    )
    metadata = sent["message"]["attachments"][0]
    snapshot = run_dir / metadata["snapshot"]
    assert snapshot.read_text() == "safe bytes"
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    with pytest.raises(handoff.HandoffError):
        handoff.send(run_dir, run["orchestrator"], type="steer", body="escape", attachments=["../escape"])
    snapshot.write_text("tampered", encoding="utf-8")
    assert any(issue["code"] == "attachment" for issue in handoff.doctor(run_dir)["issues"])


def test_snapshot_attachment_retry_does_not_recopy_or_execute_path(run):
    run_dir = Path(run["run_dir"])
    artifact = run["workspace"] / "artifact $(touch NO).txt"
    artifact.write_text("original")
    message_id = str(uuid.uuid4())
    first = handoff.send(
        run_dir, run["orchestrator"], type="steer", body="snapshot",
        message_id=message_id, attachments=[artifact.name], snapshot_attachments=True,
    )
    artifact.write_text("later workspace contents")
    retried = handoff.send(
        run_dir, run["orchestrator"], type="steer", body="snapshot",
        message_id=message_id, attachments=[artifact.name], snapshot_attachments=True,
    )
    assert retried["message"] == first["message"]
    assert (run_dir / first["message"]["attachments"][0]["snapshot"]).read_text() == "original"
    assert not (run["workspace"] / "NO").exists()


def test_progress_repair_only_appends_missing_checkpoint_marker(run):
    run_dir = Path(run["run_dir"])
    checkpoint(run, 0)
    progress = run_dir / "progress.md"
    progress.write_text(progress.read_text().split("## Checkpoint")[0], encoding="utf-8")
    report = handoff.repair_progress(run_dir, run["worker"])
    assert report["repaired"]
    assert progress.read_text().count("<!-- outbox-seq:1 -->") == 1
    again = handoff.repair_progress(run_dir, run["worker"])
    assert not again["repaired"]
