import datetime
import os
import stat
from pathlib import Path

import pytest

from agents.orchestration import handoff
from agents.orchestration import handoff_launcher
from agents.orchestration import handoff_registry
from agents.orchestration import handoff_watcher
from agents.orchestration import handoffctl


class ComposerClear:
    """Adapter fakes default to an empty composer, i.e. send the doorbell now."""

    def composer_guard(self, handle):
        return handoff_launcher.COMPOSER_CLEAR


def orchestrator_state(tmp_path, *, transport="tmux", target=None):
    state_path = tmp_path / "orchestrator" / "watcher.json"
    value = handoff_watcher.initialize(
        state_path,
        transport=transport,
        target=target or {"handle": "orchestrator:0.1"},
        owner_pid=os.getpid(),
    )
    return state_path, value


def test_retarget_preserves_a_stopped_orchestrator_identity_and_runs(tmp_path):
    surface = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    state_path, original = orchestrator_state(
        tmp_path,
        transport="cmux",
        target={"workspace": "workspace:2", "surface": surface},
    )
    before = handoff_watcher.read(state_path)
    before["runs"] = {"run-1": handoff_watcher._empty_run_state()}  # noqa: SLF001 - state fixture
    handoff_watcher._atomic_write(state_path, before)  # noqa: SLF001 - state fixture

    updated = handoff_watcher.retarget(
        state_path,
        {"workspace": "workspace:9", "surface": surface},
    )

    assert updated["orchestrator_id"] == original["orchestrator_id"]
    assert updated["runs"] == {"run-1": handoff_watcher._empty_run_state()}  # noqa: SLF001
    assert updated["target"] == {"workspace": "workspace:9", "surface": surface}


def registered_run(tmp_path, registry, *, name, orchestrator_id):
    workspace = tmp_path / f"workspace-{name}"
    workspace.mkdir()
    kickoff = tmp_path / f"kickoff-{name}.md"
    kickoff.write_text(f"task for {name}", encoding="utf-8")
    private = tmp_path / f"private-{name}"
    initialized = handoff.initialize(
        workspace=workspace,
        kickoff=kickoff,
        harness="codex",
        model="gpt-test",
        effort="high",
        transport="tmux",
        run_dir=tmp_path / "runs" / name,
        recovery_token_file=private / "recovery.token",
        orchestrator_token_file=private / "orchestrator.token",
        worker_token_file=private / "worker.token",
    )
    handoff_registry.register(
        path=registry,
        run_id=initialized["run"]["run_id"],
        name=name,
        run_dir=initialized["run_dir"],
        run_uri=initialized["run_dir"],
        host=None,
        remote_python=None,
        transport="tmux",
        session_transport="tmux",
        handle=f"worker-{name}",
        agent="codex",
        model="gpt-test",
        effort="high",
        credential_dir=str(private),
        orchestrator_id=orchestrator_id,
    )
    return {
        **initialized,
        "worker": (private / "worker.token").read_bytes(),
        "orchestrator": (private / "orchestrator.token").read_bytes(),
    }


def worker_checkpoint(run, activity, *, state="working", inbox_cursor=0):
    run_dir = Path(run["run_dir"])
    return handoff.emit(
        run_dir,
        run["worker"],
        type="checkpoint",
        body=activity,
        data={
            "state": state,
            "stage": "implementation",
            "current_activity": activity,
            "inbox_cursor": inbox_cursor,
            "commitments": [],
        },
    )


def checkpoint(run, activity):
    return worker_checkpoint(run, activity)


def blocked(run, question="Which option should I take?"):
    return handoff.emit(
        Path(run["run_dir"]),
        run["worker"],
        type="question",
        body=question,
        data={
            "blocking": True,
            "stage": "implementation",
            "current_activity": "Awaiting orchestrator direction",
            "inbox_cursor": 0,
            "commitments": [],
        },
    )


def awaiting_review(run, body="Ready for orchestrator review"):
    return handoff.emit(
        Path(run["run_dir"]),
        run["worker"],
        type="result",
        body=body,
        data={
            "head": None,
            "dirty": False,
            "stage": "implementation",
            "inbox_cursor": 0,
            "verification": [],
            "commitments": [],
        },
    )


def nonfatal_error(run, body="Recoverable worker error"):
    return handoff.emit(
        Path(run["run_dir"]),
        run["worker"],
        type="error",
        body=body,
        data={
            "fatal": False,
            "category": "transient",
            "stage": "implementation",
            "inbox_cursor": 0,
            "commitments": [],
        },
    )


def test_watcher_is_singleton_and_isolates_orchestrators(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    other_path = tmp_path / "other" / "watcher.json"
    other = handoff_watcher.initialize(
        other_path,
        transport="tmux",
        target={"handle": "other:0.1"},
        owner_pid=os.getpid(),
    )
    owned = registered_run(
        tmp_path, registry, name="owned", orchestrator_id=owner["orchestrator_id"],
    )
    foreign = registered_run(
        tmp_path, registry, name="foreign", orchestrator_id=other["orchestrator_id"],
    )
    awaiting_review(owned, "owned body must stay on disk")
    awaiting_review(foreign, "foreign body must never surface")
    calls = []

    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

    with handoff_watcher.instance_lock(state_path):
        with pytest.raises(handoff.HandoffError, match="already running"):
            with handoff_watcher.instance_lock(state_path):
                pass
        result = handoff_watcher.poll(
            state_path,
            registry_path=registry,
            notifier=lambda value, coverage, urgency: calls.append((value, coverage)),
        )

    assert result["owned_runs"] == [owned["run"]["run_id"]]
    assert calls[0][1] == {owned["run"]["run_id"]: 1}
    assert foreign["run"]["run_id"] not in handoff_watcher.read(state_path)["runs"]


def test_watcher_discovers_new_worker_without_restart(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    calls = []
    with handoff_watcher.instance_lock(state_path):
        first = registered_run(
            tmp_path, registry, name="first", orchestrator_id=owner["orchestrator_id"],
        )
        awaiting_review(first, "first")
        handoff_watcher.poll(
            state_path,
            registry_path=registry,
            notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
        )
        second = registered_run(
            tmp_path, registry, name="second", orchestrator_id=owner["orchestrator_id"],
        )
        awaiting_review(second, "second")
        handoff_watcher.poll(
            state_path,
            registry_path=registry,
            notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
        )

    assert calls == [
        {first["run"]["run_id"]: 1},
        {second["run"]["run_id"]: 1},
    ]


def test_watch_exits_when_owner_process_is_dead(tmp_path, monkeypatch):
    state_path, _ = orchestrator_state(tmp_path)
    polls = []
    monkeypatch.setattr(
        handoff_watcher,
        "poll",
        lambda *args, **kwargs: polls.append((args, kwargs)),
    )

    handoff_watcher.watch(
        state_path,
        interval=0.01,
        notifier=lambda value, coverage, urgency: None,
        owner_probe=lambda owner: "dead",
    )

    assert polls == []
    assert handoff_watcher.is_running(state_path) is False


def test_watch_suppresses_work_while_owner_liveness_is_unknown(tmp_path, monkeypatch):
    state_path, _ = orchestrator_state(tmp_path)
    statuses = iter(("unknown", "alive", "dead"))
    polls = []
    monkeypatch.setattr(handoff_watcher.time, "sleep", lambda interval: None)
    monkeypatch.setattr(
        handoff_watcher,
        "poll",
        lambda *args, **kwargs: polls.append((args, kwargs)),
    )

    handoff_watcher.watch(
        state_path,
        interval=0.01,
        notifier=lambda value, coverage, urgency: None,
        owner_probe=lambda owner: next(statuses),
    )

    assert len(polls) == 1


def test_watch_reports_each_live_poll_to_on_poll(tmp_path, monkeypatch):
    state_path, _ = orchestrator_state(tmp_path)
    statuses = iter(("alive", "unknown", "dead"))
    reported = []
    monkeypatch.setattr(handoff_watcher.time, "sleep", lambda interval: None)
    monkeypatch.setattr(
        handoff_watcher,
        "poll",
        lambda *args, **kwargs: {"attempted": {"run-1": 2}, "errors": []},
    )

    handoff_watcher.watch(
        state_path,
        interval=0.01,
        notifier=lambda value, coverage, urgency: None,
        owner_probe=lambda owner: next(statuses),
        on_poll=reported.append,
    )

    assert reported == [{"attempted": {"run-1": 2}, "errors": []}]


def test_owner_process_status_rejects_pid_reuse(tmp_path, monkeypatch):
    _, value = orchestrator_state(tmp_path)
    owner = value["owner_process"]
    monkeypatch.setattr(handoff_watcher.os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(
        handoff_watcher,
        "_process_start_time",
        lambda pid: "Mon Jan  1 00:00:00 2040",
    )

    assert handoff_watcher.owner_process_status(owner) == "dead"


def test_watcher_coalesces_and_never_advances_protocol_cursor(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    first = registered_run(
        tmp_path, registry, name="first", orchestrator_id=owner["orchestrator_id"],
    )
    second = registered_run(
        tmp_path, registry, name="second", orchestrator_id=owner["orchestrator_id"],
    )
    awaiting_review(first, "secret first body")
    awaiting_review(second, "secret second body")
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
        now=now,
    )
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
        now=now + datetime.timedelta(seconds=31),
    )

    # New events type one active doorbell; the backoff re-ring for the same
    # events downgrades to a passive alert.
    assert calls == [
        ({first["run"]["run_id"]: 1, second["run"]["run_id"]: 1}, "active"),
        ({first["run"]["run_id"]: 1, second["run"]["run_id"]: 1}, "passive"),
    ]
    assert handoff.control_show(Path(first["run_dir"]))["outbox_cursor"] == 0
    assert handoff.control_show(Path(second["run_dir"]))["outbox_cursor"] == 0
    assert handoff_registry.resolve(
        first["run"]["run_id"], path=registry,
    )["observed_outbox_cursor"] == 0
    assert handoff_registry.resolve(
        second["run"]["run_id"], path=registry,
    )["observed_outbox_cursor"] == 0
    state = handoff_watcher.read(state_path)
    assert all(item["doorbell_pending"] for item in state["runs"].values())


@pytest.mark.parametrize("delivered_before_crash", [False, True])
def test_watcher_recovers_both_notification_crash_windows(
    tmp_path, delivered_before_crash,
):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    awaiting_review(run, "durable event")
    delivered = []

    def crash(value, coverage, urgency):
        if delivered_before_crash:
            delivered.append(dict(coverage))
        raise KeyboardInterrupt("simulated watcher crash")

    with pytest.raises(KeyboardInterrupt):
        handoff_watcher.poll(
            state_path,
            registry_path=registry,
            notifier=crash,
        )

    after_crash = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert after_crash["observed_through"] == 1
    assert after_crash["doorbell_through"] == 0
    assert after_crash["doorbell_pending"] is True

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: delivered.append(dict(coverage)),
    )
    assert delivered[-1] == {run["run"]["run_id"]: 1}
    assert len(delivered) == (2 if delivered_before_crash else 1)


def test_partial_consumption_redoorbells_remainder_and_full_consumption_clears(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    checkpoint(run, "one")
    checkpoint(run, "two")
    awaiting_review(run, "three")
    calls = []
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
    )
    handoff.control_consume(Path(run["run_dir"]), run["orchestrator"], through=1)
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
    )
    handoff.control_consume(Path(run["run_dir"]), run["orchestrator"], through=3)
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
    )

    # A moving control cursor means an orchestrator is working the run, so the
    # remainder re-ring is a passive nudge rather than another typed prompt.
    assert calls == [
        ({run["run"]["run_id"]: 3}, "active"),
        ({run["run"]["run_id"]: 3}, "passive"),
    ]
    state = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert state["control_through"] == 3
    assert state["doorbell_pending"] is False


def test_orchestrator_doorbell_routes_exact_opaque_target(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    calls = []

    class Tmux(ComposerClear):
        def orchestrator_doorbell(self, handle, observed_id, *, forced=False):
            calls.append(("tmux", handle, observed_id))

    class Cmux(ComposerClear):
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, surface, observed_id, *, forced=False):
            calls.append(("cmux_input", self.workspace, surface, observed_id))
            return True

        def orchestrator_doorbell(self, surface, observed_id, *, forced=False):
            calls.append(("cmux", self.workspace, surface, observed_id))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Tmux)
    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher,
        "owner_process_status",
        lambda owner: "alive",
    )
    monkeypatch.setattr(
        handoffctl,
        "_send_native_app_doorbell",
        lambda thread_id, body: calls.append(("native_app", thread_id, body)),
    )

    tmux_method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "tmux",
        "target": {"handle": "session:3.7"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")
    cmux_method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")
    native_method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "native_app",
        "target": {"thread_id": "thread-exact"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")

    assert tmux_method == "terminal_input"
    assert cmux_method == "cmux_input+cmux_notify"
    assert native_method == "native_app"
    assert calls[0] == ("tmux", "session:3.7", orchestrator_id)
    assert calls[1] == ("cmux_input", "workspace:9", "surface-uuid", orchestrator_id)
    assert calls[2] == ("cmux", "workspace:9", "surface-uuid", orchestrator_id)
    assert calls[3][0:2] == ("native_app", "thread-exact")
    assert orchestrator_id in calls[3][2]
    assert "run-secret" not in calls[3][2]
    assert "seq 9" not in calls[3][2]


def test_orchestrator_cmux_falls_back_to_workspace_notification(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    calls = []

    class Cmux(ComposerClear):
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, handle, observed_id, *, forced=False):
            raise handoff_launcher.AdapterError("Broken pipe")

        def orchestrator_doorbell(self, handle, observed_id, *, forced=False):
            raise handoff_launcher.AdapterError("Broken pipe")

        def notify(self, handle, *, title, body):
            calls.append((self.workspace, handle, title, body))

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher,
        "owner_process_status",
        lambda owner: "alive",
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")

    assert method == "cmux_notify_workspace"
    assert calls == [(
        "workspace:9", None, "Handoff orchestrator pending",
        "Worker updates are ready for orchestrator review.",
    )]


def test_orchestrator_cmux_uses_macos_notification_when_cmux_notifications_fail(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    calls = []

    class Cmux(ComposerClear):
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, handle, observed_id, *, forced=False):
            raise handoff_launcher.AdapterError("Broken pipe")

        def orchestrator_doorbell(self, handle, observed_id, *, forced=False):
            raise handoff_launcher.AdapterError("Broken pipe")

        def notify(self, handle, *, title, body):
            raise handoff_launcher.AdapterError("Broken pipe")

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher,
        "owner_process_status",
        lambda owner: "alive",
    )
    monkeypatch.setattr(
        handoffctl,
        "_notify_macos",
        lambda title, body: calls.append((title, body)),
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")

    assert method == "macos_notification"
    assert calls == [(
        "Handoff orchestrator pending",
        "Worker updates are ready for orchestrator review.",
    )]


def test_orchestrator_cmux_reports_combined_error_when_every_channel_fails(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"

    class Cmux(ComposerClear):
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, handle, observed_id, *, forced=False):
            raise handoff_launcher.AdapterError("input rejected")

        def orchestrator_doorbell(self, handle, observed_id, *, forced=False):
            raise handoff_launcher.AdapterError("surface gone")

        def notify(self, handle, *, title, body):
            raise handoff_launcher.AdapterError("workspace gone")

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher,
        "owner_process_status",
        lambda owner: "alive",
    )

    def fail_macos(title, body):
        raise handoff_launcher.AdapterError("osascript unavailable")

    monkeypatch.setattr(handoffctl, "_notify_macos", fail_macos)

    with pytest.raises(handoff_launcher.AdapterError) as captured:
        handoffctl._notify_orchestrator({
            "orchestrator_id": orchestrator_id,
            "transport": "cmux",
            "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
            "owner_process": {"pid": os.getpid(), "started_at": "test"},
        }, {"run-secret": 9}, "active")

    message = str(captured.value)
    assert "input rejected" in message
    assert "surface gone" in message
    assert "workspace gone" in message
    assert "osascript unavailable" in message


def test_orchestrator_cmux_unechoed_input_does_not_count_as_delivery(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"

    class Cmux(ComposerClear):
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, handle, observed_id, *, forced=False):
            # Zero-exit send with no visible echo: the code-mode surface trap.
            return False

        def orchestrator_doorbell(self, handle, observed_id, *, forced=False):
            pass

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher,
        "owner_process_status",
        lambda owner: "alive",
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")

    assert method == "cmux_notify"


def test_orchestrator_cmux_confirmed_input_survives_notification_failure(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"

    class Cmux(ComposerClear):
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, handle, observed_id, *, forced=False):
            return True

        def orchestrator_doorbell(self, handle, observed_id, *, forced=False):
            raise handoff_launcher.AdapterError("surface alert gone")

        def notify(self, handle, *, title, body):
            raise handoff_launcher.AdapterError("workspace alert gone")

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher,
        "owner_process_status",
        lambda owner: "alive",
    )
    monkeypatch.setattr(
        handoffctl,
        "_notify_macos",
        lambda title, body: pytest.fail("macOS fallback must not fire after agent push"),
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")

    assert method == "cmux_input"


def test_watcher_records_doorbell_channel_and_clears_it_on_failure(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    awaiting_review(run, "secret body")
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: "cmux_notify",
        now=now,
    )
    state = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert state["last_doorbell_method"] == "cmux_notify"
    assert state["last_doorbell_error"] is None

    def unavailable(value, coverage, urgency):
        raise handoff.HandoffError("push unavailable", 6)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=unavailable,
        now=now + datetime.timedelta(seconds=31),
    )
    state = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert state["last_doorbell_method"] is None
    assert state["last_doorbell_error"] == "push unavailable"


def test_run_state_from_an_older_snapshot_without_doorbell_method(tmp_path):
    state_path, owner = orchestrator_state(tmp_path)
    value = handoff_watcher.read(state_path)
    legacy = handoff_watcher._empty_run_state()  # noqa: SLF001 - state fixture
    del legacy["last_doorbell_method"]
    value["runs"] = {"run-1": legacy}
    handoff_watcher._atomic_write(state_path, value)  # noqa: SLF001 - state fixture

    assert handoff_watcher.read(state_path)["runs"]["run-1"] == legacy


def test_run_state_from_an_older_snapshot_without_typed_error(tmp_path):
    state_path, owner = orchestrator_state(tmp_path)
    value = handoff_watcher.read(state_path)
    legacy = handoff_watcher._empty_run_state()  # noqa: SLF001 - state fixture
    del legacy["last_typed_error"]
    value["runs"] = {"run-1": legacy}
    handoff_watcher._atomic_write(state_path, value)  # noqa: SLF001 - state fixture

    assert handoff_watcher.read(state_path)["runs"]["run-1"] == legacy


def test_failed_active_doorbell_sticks_until_a_typed_ring_lands(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    awaiting_review(run, "secret body")
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)
    run_id = run["run"]["run_id"]

    def unavailable(value, coverage, urgency):
        raise handoff.HandoffError("push unavailable", 6)

    # A failed ACTIVE ring marks the typed channel as possibly dead.
    handoff_watcher.poll(
        state_path, registry_path=registry, notifier=unavailable, now=now,
    )
    state = handoff_watcher.read(state_path)["runs"][run_id]
    assert state["last_doorbell_error"] == "push unavailable"
    assert state["last_typed_error"] == "push unavailable"

    # A later successful PASSIVE banner clears the doorbell error but must not
    # mask the dead typed channel.
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: "macos_notification",
        now=now + datetime.timedelta(seconds=31),
    )
    state = handoff_watcher.read(state_path)["runs"][run_id]
    assert state["last_doorbell_error"] is None
    assert state["last_typed_error"] == "push unavailable"

    # A second run with fresh events rings ACTIVE in the same coalesced
    # doorbell; a landed typed channel clears the sticky error for every run
    # the ring covered.
    second = registered_run(
        tmp_path, registry, name="second", orchestrator_id=owner["orchestrator_id"],
    )
    awaiting_review(second, "another body")
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: "terminal_input",
        now=now + datetime.timedelta(seconds=95),
    )
    state = handoff_watcher.read(state_path)["runs"][run_id]
    assert state["last_typed_error"] is None

    # A failing PASSIVE attempt must not set the typed-channel error either.
    second_id = second["run"]["run_id"]
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=unavailable,
        now=now + datetime.timedelta(seconds=126),
    )
    state = handoff_watcher.read(state_path)["runs"][second_id]
    assert state["last_doorbell_error"] == "push unavailable"
    assert state["last_typed_error"] is None


def test_pending_uses_hot_path_and_full_context_only_for_recovery(
    tmp_path, monkeypatch,
):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    first = checkpoint(run, "first")
    second = awaiting_review(run, "second")
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: (_ for _ in ()).throw(
            handoff.HandoffError("push unavailable", 6)
        ),
    )
    handoff.control_consume(Path(run["run_dir"]), run["orchestrator"], through=1)

    hot = handoffctl._orchestrator_pending(state_path, full_context=False)
    assert hot["mode"] == "hot"
    assert hot["pending"][0]["notification"]["last_doorbell_error"] == "push unavailable"
    assert "kickoff" not in hot["pending"][0]["detail"]
    assert [event["message_id"] for event in hot["pending"][0]["detail"]["unread_outbox"]] == [
        second["message"]["message_id"],
    ]
    assert first["message"]["message_id"] not in {
        event["message_id"] for event in hot["pending"][0]["detail"]["unread_outbox"]
    }

    recovery = handoffctl._orchestrator_pending(state_path, full_context=True)
    assert recovery["mode"] == "recovery"
    assert "task for worker" in recovery["pending"][0]["detail"]["kickoff"]
    assert recovery["pending"][0]["detail"]["unread_outbox"][0]["seq"] == 2


def test_unowned_registry_run_requires_explicit_adoption(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(tmp_path, registry, name="worker", orchestrator_id=None)
    awaiting_review(run, "unowned")
    calls = []

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
    )
    assert calls == []
    handoff_registry.adopt(
        run["run"]["run_id"], owner["orchestrator_id"], path=registry,
    )
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
    )
    assert calls == [{run["run"]["run_id"]: 1}]


def test_awaiting_review_result_rings_once_then_acknowledge_stops_reringing(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    awaiting_review(run, "result body")
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
        now=now,
    )
    assert calls == [{run["run"]["run_id"]: 1}]

    # Loading the run's events records an acknowledgment cursor.
    handoffctl._orchestrator_pending(state_path, full_context=False)
    acked = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert acked["acknowledged_through"] == 1

    # A poll well past the retry interval must NOT re-ring the seen event,
    # even though the protocol outbox cursor was never advanced.
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=61),
    )
    assert calls == [{run["run"]["run_id"]: 1}]
    assert handoff_watcher.read(state_path)["runs"][
        run["run"]["run_id"]
    ]["doorbell_pending"] is True


def test_new_event_after_acknowledge_rerings(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    awaiting_review(run, "first")
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
        now=now,
    )
    handoffctl._orchestrator_pending(state_path, full_context=False)
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=61),
    )
    assert calls == [{run["run"]["run_id"]: 1}]

    # A strictly newer worker event rings again despite the earlier ack.
    nonfatal_error(run, "second")
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=122),
    )
    assert calls == [{run["run"]["run_id"]: 1}, {run["run"]["run_id"]: 2}]


def test_pending_acknowledges_each_worker_independently(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    state_path, owner = orchestrator_state(tmp_path)
    first = registered_run(
        tmp_path, registry, name="first", orchestrator_id=owner["orchestrator_id"],
    )
    second = registered_run(
        tmp_path, registry, name="second", orchestrator_id=owner["orchestrator_id"],
    )
    awaiting_review(first, "first body")
    awaiting_review(second, "second body")
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
        now=now,
    )
    assert calls == [{first["run"]["run_id"]: 1, second["run"]["run_id"]: 1}]

    # One pending call acknowledges every surfaced worker at its own cursor.
    handoffctl._orchestrator_pending(state_path, full_context=False)
    runs = handoff_watcher.read(state_path)["runs"]
    assert runs[first["run"]["run_id"]]["acknowledged_through"] == 1
    assert runs[second["run"]["run_id"]]["acknowledged_through"] == 1

    # A new event on ONLY the second worker rings only that worker.
    nonfatal_error(second, "second follow-up")
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=61),
    )
    assert calls == [
        {first["run"]["run_id"]: 1, second["run"]["run_id"]: 1},
        {second["run"]["run_id"]: 2},
    ]


def test_acknowledge_is_monotonic_and_ignores_unknown_runs(tmp_path):
    state_path, _ = orchestrator_state(tmp_path)
    value = handoff_watcher.read(state_path)
    value["runs"] = {"run-1": handoff_watcher._empty_run_state()}  # noqa: SLF001
    handoff_watcher._atomic_write(state_path, value)  # noqa: SLF001

    handoff_watcher.acknowledge(state_path, {"run-1": 5, "ghost": 9})
    state = handoff_watcher.read(state_path)
    assert state["runs"]["run-1"]["acknowledged_through"] == 5
    assert "ghost" not in state["runs"]

    # A lower cursor never regresses the acknowledgment.
    handoff_watcher.acknowledge(state_path, {"run-1": 2})
    assert handoff_watcher.read(state_path)["runs"]["run-1"]["acknowledged_through"] == 5


def test_run_state_from_a_snapshot_without_acknowledged_through(tmp_path):
    state_path, _ = orchestrator_state(tmp_path)
    value = handoff_watcher.read(state_path)
    legacy = handoff_watcher._empty_run_state()  # noqa: SLF001 - state fixture
    del legacy["acknowledged_through"]
    value["runs"] = {"run-1": legacy}
    handoff_watcher._atomic_write(state_path, value)  # noqa: SLF001 - state fixture

    assert handoff_watcher.read(state_path)["runs"]["run-1"] == legacy


def test_run_state_accepts_snapshots_with_and_without_dismissed_through():
    current = handoff_watcher._empty_run_state()  # noqa: SLF001 - state fixture
    legacy = dict(current)
    del legacy["dismissed_through"]

    assert handoff_watcher._run_state(current) == current  # noqa: SLF001 - state validation
    assert handoff_watcher._run_state(legacy) == legacy  # noqa: SLF001 - state validation


def test_working_checkpoint_does_not_ring(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    checkpoint(run, "ordinary progress")
    calls = []

    result = handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
    )

    assert calls == []
    assert result["pending"] == {run["run"]["run_id"]: 1}


def test_blocked_question_rerings_after_acknowledge_until_worker_resumes(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    question = blocked(run)
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
        now=now,
    )
    handoff_watcher.acknowledge(state_path, {run["run"]["run_id"]: 1})
    # A blocked worker keeps pushing actively, but on the backoff schedule:
    # 29s after the first ring is too early, 31s is due.
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
        now=now + datetime.timedelta(seconds=29),
    )
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
        now=now + datetime.timedelta(seconds=31),
    )

    handoff.send(
        Path(run["run_dir"]),
        run["orchestrator"],
        type="answer",
        body="Use the recommended option.",
        reply_to=question["message"]["message_id"],
    )
    worker_checkpoint(
        run, "resumed after orchestrator answer", inbox_cursor=1,
    )
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
        now=now + datetime.timedelta(seconds=62),
    )

    assert calls == [
        ({run["run"]["run_id"]: 1}, "active"),
        ({run["run"]["run_id"]: 1}, "active"),
    ]


def test_finished_run_does_not_ring(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    run_dir = Path(run["run_dir"])
    result = awaiting_review(run)
    handoff.control_consume(run_dir, run["orchestrator"], through=1)
    review = handoff.send(
        run_dir, run["orchestrator"], type="review",
        data={"disposition": "accepted"},
        reply_to=result["message"]["message_id"],
    )
    integration = handoff.control_integrate(
        run_dir, run["orchestrator"], commit="deadbeef",
    )
    worker_checkpoint(
        run, "worker paused", state="paused",
        inbox_cursor=integration["message"]["seq"],
    )
    handoff_registry.mark_finished(run["run"]["run_id"], path=registry)
    calls = []

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
    )

    notification = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert calls == []
    assert notification["doorbell_pending"] is True
    # The explicit marker auto-quiets the whole post-decision tail.
    assert notification["dismissed_through"] == notification["observed_through"]


def test_watcher_rings_through_a_pause_resume_window(tmp_path):
    """A paused run is not concluded: after a resume steer the watcher must
    keep ringing for the resumed worker's next question and next result."""
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    run_dir = Path(run["run_dir"])
    result = awaiting_review(run)
    handoff.control_consume(run_dir, run["orchestrator"], through=1)
    review = handoff.send(
        run_dir, run["orchestrator"], type="review",
        reply_to=result["message"]["message_id"], data={"disposition": "accepted"},
    )
    integration = handoff.control_integrate(
        run_dir, run["orchestrator"], commit="deadbeef",
    )
    worker_checkpoint(
        run, "paused", state="paused",
        inbox_cursor=integration["message"]["seq"],
    )
    steer = handoff.send(run_dir, run["orchestrator"], type="steer", body="fresh work")
    calls = []

    def notifier(value, coverage, urgency):
        calls.append((dict(coverage), urgency))

    # The paused, already-steered run is quiet but NOT concluded: nothing is
    # dismissed, so the resumed window's events still ring.
    handoff_watcher.poll(state_path, registry_path=registry, notifier=notifier)
    notification = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert calls == []
    assert notification["dismissed_through"] == 0

    worker_checkpoint(run, "resumed", inbox_cursor=steer["message"]["seq"])
    handoff.emit(
        run_dir, run["worker"], type="question", body="which option?",
        data={
            "blocking": True, "stage": "implementation",
            "current_activity": "awaiting direction",
            "inbox_cursor": steer["message"]["seq"], "commitments": [],
        },
    )
    handoff_watcher.poll(state_path, registry_path=registry, notifier=notifier)
    assert calls == [({run["run"]["run_id"]: 4}, "active")]

    question_id = handoff.status(run_dir)["blocked_on"][0]
    answer = handoff.send(
        run_dir, run["orchestrator"], type="answer",
        body="the second one", reply_to=question_id,
    )
    worker_checkpoint(run, "answered", inbox_cursor=answer["message"]["seq"])
    handoff.emit(
        run_dir, run["worker"], type="result", body="fresh result",
        data={
            "head": None, "dirty": False, "stage": "implementation",
            "inbox_cursor": answer["message"]["seq"], "verification": [], "commitments": [],
        },
    )
    handoff_watcher.poll(state_path, registry_path=registry, notifier=notifier)
    assert calls == [
        ({run["run"]["run_id"]: 4}, "active"),
        ({run["run"]["run_id"]: 6}, "active"),
    ]
    notification = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert notification["dismissed_through"] == 0


def test_dismiss_silences_blocked_worker_until_newer_event(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    blocked(run)
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
        now=now,
    )
    handoff_watcher.dismiss(state_path, {run["run"]["run_id"]: 1})
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=31),
    )
    handoff.emit(
        Path(run["run_dir"]),
        run["worker"],
        type="question",
        body="Additional context for the same decision.",
        data={
            "blocking": False,
            "stage": "implementation",
            "current_activity": "Still awaiting orchestrator direction",
            "inbox_cursor": 0,
            "commitments": [],
        },
    )
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=62),
    )

    assert calls == [
        {run["run"]["run_id"]: 1},
        {run["run"]["run_id"]: 2},
    ]
    assert handoff.control_show(Path(run["run_dir"]))["outbox_cursor"] == 0


def test_remote_watcher_reads_authoritative_host_without_credentials(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    handoff_registry.register(
        path=registry,
        run_id="remote-run",
        name="remote",
        run_dir="/remote/state/run",
        run_uri="ssh://worker/remote/state/run",
        host="worker",
        remote_python="/remote/python",
        transport="ssh_tmux",
        session_transport="tmux",
        handle="remote-worker",
        agent="codex",
        model="gpt-test",
        effort="high",
        credential_dir=None,
        orchestrator_id=owner["orchestrator_id"],
    )
    commands = []

    def remote_json(record, argv):
        commands.append((record["host"], list(argv)))
        if argv[0] == "status":
            return {"state": "blocked", "blocked_on": ["question-id"]}
        if argv[0:2] == ["control", "show"]:
            return {
                "outbox_cursor": 0,
                "worker_epoch": 1,
                "integration": {"state": "pending", "commit": None, "reason": None},
            }
        return [{"seq": 1, "type": "question", "body": "remains remote"}]

    monkeypatch.setattr(handoff_watcher, "_remote_json", remote_json)
    calls = []
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
    )

    assert calls == [{"remote-run": 1}]
    assert commands == [
        ("worker", ["control", "show", "--run-dir", "/remote/state/run"]),
        ("worker", ["status", "--run-dir", "/remote/state/run"]),
        (
            "worker",
            [
                "read", "--run-dir", "/remote/state/run", "--journal", "outbox",
                "--after", "0",
            ],
        ),
    ]
    assert "token" not in repr(commands).lower()


def test_remote_finished_marker_quiets_authoritative_tail(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    handoff_registry.register(
        path=registry,
        run_id="remote-run",
        name="remote",
        run_dir="/remote/state/run",
        run_uri="ssh://worker/remote/state/run",
        host="worker",
        remote_python="/remote/python",
        transport="ssh_tmux",
        session_transport="tmux",
        handle="remote-worker",
        agent="codex",
        model="gpt-test",
        effort="high",
        credential_dir=None,
        orchestrator_id=owner["orchestrator_id"],
    )
    handoff_registry.mark_finished("remote-run", path=registry)

    def remote_json(record, argv):
        if argv[0] == "status":
            return {"state": "awaiting_review", "blocked_on": []}
        if argv[0:2] == ["control", "show"]:
            return {
                "outbox_cursor": 0,
                "worker_epoch": 1,
                "integration": {"state": "pending", "commit": None, "reason": None},
            }
        return [{"seq": 1, "type": "result", "body": "remote result"}]

    monkeypatch.setattr(handoff_watcher, "_remote_json", remote_json)
    calls = []

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
    )

    assert calls == []
    notification = handoff_watcher.read(state_path)["runs"]["remote-run"]
    assert notification["observed_through"] == 1
    assert notification["dismissed_through"] == 1


def test_deferred_input_doorbell_retries_on_the_very_next_poll(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    awaiting_review(run, "secret body")
    run_id = run["run"]["run_id"]
    now = datetime.datetime(2026, 7, 21, tzinfo=datetime.timezone.utc)
    attempts = []

    def deferring(value, coverage, urgency):
        attempts.append(dict(coverage))
        return f"cmux_notify+{handoff_watcher.DEFERRED_INPUT}"

    handoff_watcher.poll(
        state_path, registry_path=registry, notifier=deferring, now=now,
    )
    state = handoff_watcher.read(state_path)["runs"][run_id]
    assert len(attempts) == 1
    # The human is mid-prompt, so nothing was typed and nothing is recorded as
    # attempted; only the deferral itself is visible in the method.
    assert state["last_doorbell_at"] is None
    assert state["doorbell_through"] == 0
    assert handoff_watcher.DEFERRED_INPUT in state["last_doorbell_method"]
    assert state["last_doorbell_error"] is None

    # One second later — far inside the 30s retry interval — it tries again
    # rather than backing off, because no delivery was ever attempted.
    handoff_watcher.poll(
        state_path, registry_path=registry, notifier=deferring,
        now=now + datetime.timedelta(seconds=1),
    )
    assert len(attempts) == 2

    # Once the composer clears the typed doorbell lands and is recorded.
    handoff_watcher.poll(
        state_path, registry_path=registry,
        notifier=lambda value, coverage, urgency: "cmux_input+cmux_notify",
        now=now + datetime.timedelta(seconds=2),
    )
    state = handoff_watcher.read(state_path)["runs"][run_id]
    assert state["last_doorbell_at"] is not None
    assert state["doorbell_through"] > 0
    assert state["last_doorbell_method"] == "cmux_input+cmux_notify"

    # And a delivered doorbell resumes normal backoff instead of re-ringing.
    handoff_watcher.poll(
        state_path, registry_path=registry,
        notifier=lambda value, coverage, urgency: pytest.fail("must back off after delivery"),
        now=now + datetime.timedelta(seconds=3),
    )


def test_cmux_doorbell_defers_typed_channel_while_the_human_types(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    calls = []

    class Cmux:
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def composer_guard(self, handle):
            return handoff_launcher.COMPOSER_BUSY

        def orchestrator_doorbell_input(self, handle, observed_id, *, forced=False):
            pytest.fail("a live draft must never be typed into")

        def orchestrator_doorbell(self, handle, observed_id, *, forced=False):
            calls.append(("cmux_notify", handle, observed_id))

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher, "owner_process_status", lambda owner: "alive",
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")

    # The banner still reaches the human; only the composer is left alone.
    assert method == f"{handoff_watcher.DEFERRED_INPUT}+cmux_notify"
    assert calls == [("cmux_notify", "surface-uuid", orchestrator_id)]


def test_cmux_doorbell_forces_a_parked_draft_with_the_recovery_warning(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    typed = []

    class Cmux(ComposerClear):
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def composer_guard(self, handle):
            return handoff_launcher.COMPOSER_FORCED

        def orchestrator_doorbell_input(self, handle, observed_id, *, forced=False):
            typed.append(forced)
            return True

        def orchestrator_doorbell(self, handle, observed_id, *, forced=False):
            pass

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher, "owner_process_status", lambda owner: "alive",
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")

    assert typed == [True]
    assert method == "cmux_input_forced+cmux_notify"


def test_unreadable_composer_guard_keeps_sending_the_doorbell(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    typed = []

    class Cmux(ComposerClear):
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def composer_guard(self, handle):
            raise handoff_launcher.AdapterError("read-screen failed")

        def orchestrator_doorbell_input(self, handle, observed_id, *, forced=False):
            typed.append(forced)
            return True

        def orchestrator_doorbell(self, handle, observed_id, *, forced=False):
            pass

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher, "owner_process_status", lambda owner: "alive",
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")

    assert typed == [False]
    assert method == "cmux_input+cmux_notify"


def test_deferral_does_not_stand_in_for_a_failed_visible_alert(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    banners = []

    class Cmux:
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def composer_guard(self, handle):
            return handoff_launcher.COMPOSER_BUSY

        def orchestrator_doorbell_input(self, handle, observed_id, *, forced=False):
            pytest.fail("a live draft must never be typed into")

        def orchestrator_doorbell(self, handle, observed_id, *, forced=False):
            raise handoff_launcher.AdapterError("surface alert gone")

        def notify(self, handle, *, title, body):
            raise handoff_launcher.AdapterError("workspace alert gone")

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher, "owner_process_status", lambda owner: "alive",
    )
    monkeypatch.setattr(
        handoffctl, "_notify_macos", lambda title, body: banners.append(title),
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")

    assert banners == ["Handoff orchestrator pending"]
    assert method == f"{handoff_watcher.DEFERRED_INPUT}+macos_notification"


def test_awaiting_review_types_once_per_event_cycle_then_passive(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    awaiting_review(run, "result body")
    run_id = run["run"]["run_id"]
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    for seconds in (0, 31, 91):
        handoff_watcher.poll(
            state_path,
            registry_path=registry,
            notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
            now=now + datetime.timedelta(seconds=seconds),
        )

    # The first ring of an event-cycle is typed; every backoff re-ring for the
    # same events is a passive alert, never another composer prompt.
    assert calls == [
        ({run_id: 1}, "active"),
        ({run_id: 1}, "passive"),
        ({run_id: 1}, "passive"),
    ]
    state = handoff_watcher.read(state_path)["runs"][run_id]
    assert state["doorbell_through"] == 1
    assert state["observed_through"] == 1
    assert state["doorbell_attempts"] == 3


def test_blocked_rering_backs_off_exponentially_and_resets_on_new_events(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    blocked(run)
    run_id = run["run"]["run_id"]
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    def poll_at(seconds):
        handoff_watcher.poll(
            state_path,
            registry_path=registry,
            notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
            now=now + datetime.timedelta(seconds=seconds),
        )

    # 30 -> 60 -> 120 -> 240 -> capped 300: each gap must elapse before the
    # next ring, and a poll one second early must not ring.
    poll_at(0)
    assert len(calls) == 1
    poll_at(29)
    assert len(calls) == 1
    poll_at(30)
    assert len(calls) == 2
    poll_at(89)
    assert len(calls) == 2
    poll_at(90)
    assert len(calls) == 3
    poll_at(209)
    assert len(calls) == 3
    poll_at(210)
    assert len(calls) == 4
    poll_at(449)
    assert len(calls) == 4
    poll_at(450)
    assert len(calls) == 5
    poll_at(749)
    assert len(calls) == 5
    poll_at(750)
    assert len(calls) == 6
    assert calls == [({run_id: 1}, "active")] * 6
    state = handoff_watcher.read(state_path)["runs"][run_id]
    assert state["doorbell_attempts"] == 6

    # A strictly newer event restarts the schedule from the base interval.
    handoff.emit(
        Path(run["run_dir"]),
        run["worker"],
        type="question",
        body="Additional context for the same decision.",
        data={
            "blocking": False,
            "stage": "implementation",
            "current_activity": "Still awaiting orchestrator direction",
            "inbox_cursor": 0,
            "commitments": [],
        },
    )
    poll_at(751)
    assert calls[-1] == ({run_id: 2}, "active")
    state = handoff_watcher.read(state_path)["runs"][run_id]
    assert state["doorbell_attempts"] == 1
    poll_at(780)
    assert len(calls) == 7
    poll_at(781)
    assert len(calls) == 8


def test_mixed_urgency_merges_into_one_active_call(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    blocked_run = registered_run(
        tmp_path, registry, name="blocked", orchestrator_id=owner["orchestrator_id"],
    )
    review_run = registered_run(
        tmp_path, registry, name="review", orchestrator_id=owner["orchestrator_id"],
    )
    blocked(blocked_run)
    awaiting_review(review_run, "review body")
    blocked_id = blocked_run["run"]["run_id"]
    review_id = review_run["run"]["run_id"]
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    for seconds in (0, 31):
        handoff_watcher.poll(
            state_path,
            registry_path=registry,
            notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
            now=now + datetime.timedelta(seconds=seconds),
        )

    # The second poll's review re-ring would be passive on its own, but one
    # active covered run makes the whole coalesced call active.
    assert calls == [
        ({blocked_id: 1, review_id: 1}, "active"),
        ({blocked_id: 1, review_id: 1}, "active"),
    ]


def test_watch_forwards_retry_seconds_to_poll(tmp_path, monkeypatch):
    state_path, _ = orchestrator_state(tmp_path)
    statuses = iter(("alive", "dead"))
    polls = []
    monkeypatch.setattr(handoff_watcher.time, "sleep", lambda interval: None)
    monkeypatch.setattr(
        handoff_watcher,
        "poll",
        lambda *args, **kwargs: polls.append(kwargs),
    )

    handoff_watcher.watch(
        state_path,
        interval=0.01,
        notifier=lambda value, coverage, urgency: None,
        owner_probe=lambda owner: next(statuses),
        retry_seconds=123.0,
    )

    assert len(polls) == 1
    assert polls[0]["retry_seconds"] == 123.0


def test_finished_run_stays_quiet_across_final_checkpoint(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    run_dir = Path(run["run_dir"])
    result = awaiting_review(run)
    handoff.control_consume(run_dir, run["orchestrator"], through=1)
    review = handoff.send(
        run_dir, run["orchestrator"], type="review",
        data={"disposition": "accepted"},
        reply_to=result["message"]["message_id"],
    )
    integration = handoff.control_integrate(
        run_dir, run["orchestrator"], commit="deadbeef",
    )
    worker_checkpoint(
        run, "worker paused", state="paused",
        inbox_cursor=integration["message"]["seq"],
    )
    run_id = run["run"]["run_id"]
    handoff_registry.mark_finished(run_id, path=registry)
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    for seconds in (0, 31, 62):
        handoff_watcher.poll(
            state_path,
            registry_path=registry,
            notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
            now=now + datetime.timedelta(seconds=seconds),
        )

    # Zero rings across the whole post-decision tail.
    assert calls == []
    notification = handoff_watcher.read(state_path)["runs"][run_id]
    assert notification["observed_through"] == 2
    assert notification["dismissed_through"] == 2
    assert notification["doorbell_pending"] is True

    quiet = handoffctl._orchestrator_pending(state_path, full_context=False)
    assert quiet["pending"] == []
    assert [entry["run"]["run_id"] for entry in quiet["quiet"]] == [run_id]


def test_paused_run_stays_quiet_and_rerings_on_a_new_result(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    run_dir = Path(run["run_dir"])
    result = awaiting_review(run)
    handoff.control_consume(run_dir, run["orchestrator"], through=1)
    review = handoff.send(
        run_dir, run["orchestrator"], type="review",
        data={"disposition": "accepted"},
        reply_to=result["message"]["message_id"],
    )
    integration = handoff.control_integrate(
        run_dir, run["orchestrator"], commit="deadbeef",
    )
    worker_checkpoint(
        run, "worker paused", state="paused",
        inbox_cursor=integration["message"]["seq"],
    )
    run_id = run["run"]["run_id"]
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    def poll_at(seconds):
        handoff_watcher.poll(
            state_path,
            registry_path=registry,
            notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
            now=now + datetime.timedelta(seconds=seconds),
        )

    for seconds in (0, 31, 62):
        poll_at(seconds)

    # A paused run is NOT concluded: nothing is auto-dismissed, and "paused"
    # is not a ringing state, so the idle run simply stays quiet.
    assert calls == []
    notification = handoff_watcher.read(state_path)["runs"][run_id]
    assert notification["observed_through"] == 2
    assert notification["dismissed_through"] == 0

    # Resume: a steer and a fresh result re-ring the run naturally.
    steer = handoff.send(run_dir, run["orchestrator"], type="steer", body="fresh work")
    worker_checkpoint(
        run, "resumed", state="working",
        inbox_cursor=steer["message"]["seq"],
    )
    handoff.emit(
        run_dir,
        run["worker"],
        type="result",
        body="fresh result",
        data={
            "head": None,
            "dirty": False,
            "stage": "implementation",
            "inbox_cursor": steer["message"]["seq"],
            "verification": [],
            "commitments": [],
        },
    )
    poll_at(93)

    assert calls == [({run_id: 4}, "active")]
    notification = handoff_watcher.read(state_path)["runs"][run_id]
    assert notification["dismissed_through"] == 0


def test_concluded_run_with_fatal_tail_keeps_ringing(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    run_dir = Path(run["run_dir"])
    awaiting_review(run, "result before the crash")
    handoff.emit(
        run_dir,
        run["worker"],
        type="error",
        body="fatal test failure",
        data={
            "fatal": True,
            "category": "test",
            "stage": "implementation",
            "inbox_cursor": 0,
            "commitments": [],
        },
    )
    run_id = run["run"]["run_id"]
    handoff_registry.mark_finished(run_id, path=registry)
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    for seconds in (0, 31):
        handoff_watcher.poll(
            state_path,
            registry_path=registry,
            notifier=lambda value, coverage, urgency: calls.append((dict(coverage), urgency)),
            now=now + datetime.timedelta(seconds=seconds),
        )

    # A badly-dying run is exempt from auto-quiet: it rings on new events and
    # keeps re-ringing on the backoff schedule instead of going silent.
    assert calls == [
        ({run_id: 2}, "active"),
        ({run_id: 2}, "passive"),
    ]
    notification = handoff_watcher.read(state_path)["runs"][run_id]
    assert notification["dismissed_through"] == 0


def test_worker_epoch_rotation_invalidates_observed_fatal(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    run_dir = Path(run["run_dir"])
    handoff.emit(
        run_dir,
        run["worker"],
        type="error",
        body="fatal in the first worker epoch",
        data={
            "fatal": True,
            "category": "test",
            "stage": "implementation",
            "inbox_cursor": 0,
            "commitments": [],
        },
    )
    run_id = run["run"]["run_id"]
    calls = []
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
    )
    first = handoff_watcher.read(state_path)["runs"][run_id]
    assert first["fatal_epoch"] == 1

    handoff.control_rotate_worker(
        run_dir,
        run["orchestrator"],
        new_token_file=tmp_path / "rotated-worker.token",
        reason="replace the failed worker",
        confirmed_dead=True,
    )
    handoff_registry.mark_finished(run_id, path=registry)
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
    )

    # The historical epoch-1 fatal remains recorded, but it no longer derives
    # fatality after the live worker epoch advances to 2, so the finished
    # marker can quiet the old tail.
    assert calls == [{run_id: 1}]
    current = handoff_watcher.read(state_path)["runs"][run_id]
    assert current["fatal_epoch"] == 1
    assert current["dismissed_through"] == 1


def test_legacy_failed_snapshot_rings_without_a_persisted_fatal_epoch(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    run_dir = Path(run["run_dir"])
    handoff.emit(
        run_dir,
        run["worker"],
        type="error",
        body="fatal observed by a pre-upgrade watcher",
        data={
            "fatal": True,
            "category": "test",
            "stage": "implementation",
            "inbox_cursor": 0,
            "commitments": [],
        },
    )
    status = handoff.status(run_dir)
    status["state"] = "failed"
    handoff._atomic_json(run_dir / "status.json", status)  # noqa: SLF001
    state = handoff_watcher.read(state_path)
    legacy = handoff_watcher._empty_run_state()  # noqa: SLF001
    legacy["observed_through"] = 1
    state["runs"][run["run"]["run_id"]] = legacy
    handoff_watcher._atomic_write(state_path, state)  # noqa: SLF001
    calls = []

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage, urgency: calls.append(dict(coverage)),
    )

    assert calls == [{run["run"]["run_id"]: 1}]
    current = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert current.get("fatal_epoch") is None


def test_run_state_from_a_snapshot_without_doorbell_attempts(tmp_path):
    state_path, _ = orchestrator_state(tmp_path)
    value = handoff_watcher.read(state_path)
    legacy = handoff_watcher._empty_run_state()  # noqa: SLF001 - state fixture
    del legacy["doorbell_attempts"]
    value["runs"] = {"run-1": legacy}
    handoff_watcher._atomic_write(state_path, value)  # noqa: SLF001 - state fixture

    assert handoff_watcher.read(state_path)["runs"]["run-1"] == legacy


def test_cmux_passive_doorbell_skips_the_typed_channel(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    calls = []

    class Cmux:
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def composer_guard(self, handle):
            pytest.fail("a passive doorbell must not probe the composer")

        def orchestrator_doorbell_input(self, handle, observed_id, *, forced=False):
            pytest.fail("a passive doorbell must never type input")

        def orchestrator_doorbell(self, handle, observed_id, *, forced=False):
            calls.append(("cmux_notify", handle, observed_id))

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher, "owner_process_status", lambda owner: "alive",
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "passive")

    assert method == "cmux_notify"
    assert calls == [("cmux_notify", "surface-uuid", orchestrator_id)]


def test_cmux_passive_doorbell_falls_back_to_workspace_notification(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    calls = []

    class Cmux:
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, handle, observed_id, *, forced=False):
            pytest.fail("a passive doorbell must never type input")

        def orchestrator_doorbell(self, handle, observed_id, *, forced=False):
            raise handoff_launcher.AdapterError("surface alert gone")

        def notify(self, handle, *, title, body):
            calls.append((self.workspace, handle, title, body))

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher, "owner_process_status", lambda owner: "alive",
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "passive")

    assert method == "cmux_notify_workspace"
    assert calls == [(
        "workspace:9", None, "Handoff orchestrator pending",
        "Worker updates are ready for orchestrator review.",
    )]


def test_tmux_passive_doorbell_uses_macos_banner_without_typing(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    banners = []

    class Tmux:
        def __init__(self):
            pytest.fail("a passive doorbell must never touch the terminal")

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Tmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher, "owner_process_status", lambda owner: "alive",
    )
    monkeypatch.setattr(
        handoffctl,
        "_notify_macos",
        lambda title, body: banners.append((title, body)),
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "tmux",
        "target": {"handle": "session:3.7"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "passive")

    assert method == "macos_notification"
    assert banners == [(
        "Handoff orchestrator pending",
        "Worker updates are ready for orchestrator review.",
    )]


def test_tmux_passive_doorbell_macos_failure_is_best_effort(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"

    class Tmux:
        def __init__(self):
            pytest.fail("a passive doorbell must never touch the terminal")

    def fail_macos(title, body):
        raise handoff_launcher.AdapterError("osascript unavailable")

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Tmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher, "owner_process_status", lambda owner: "alive",
    )
    monkeypatch.setattr(handoffctl, "_notify_macos", fail_macos)

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "tmux",
        "target": {"handle": "session:3.7"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "passive")

    assert method == "macos_notification_failed"


def test_orchestrator_doorbell_routes_a_herdr_pane_opaquely(monkeypatch):
    orchestrator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    calls = []

    class Herdr(ComposerClear):
        def __init__(self, binary):
            self.binary = binary

        def orchestrator_doorbell(self, pane, observed_id, *, forced=False):
            calls.append(("herdr", pane, observed_id, forced))

        def notify(self, pane, *, title, body):
            calls.append(("herdr_notify", pane, title, body))

    monkeypatch.setattr(handoffctl.handoff_launcher, "HerdrAdapter", Herdr)
    monkeypatch.setattr(
        handoffctl.handoff_watcher, "owner_process_status", lambda owner: "alive",
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "herdr",
        "target": {"pane": "w1:p2"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")

    assert method == "terminal_input"
    assert calls == [("herdr", "w1:p2", orchestrator_id, False)]
    assert "run-secret" not in str(calls)
    assert "seq 9" not in str(calls)


def test_passive_herdr_doorbell_notifies_without_typing(monkeypatch):
    calls = []

    class Herdr(ComposerClear):
        def __init__(self, binary):
            self.binary = binary

        def orchestrator_doorbell(self, pane, observed_id, *, forced=False):
            raise AssertionError("a re-reminder must never type into the composer")

        def notify(self, pane, *, title, body):
            calls.append(("notify", pane, title, body))

    monkeypatch.setattr(handoffctl.handoff_launcher, "HerdrAdapter", Herdr)
    monkeypatch.setattr(
        handoffctl.handoff_watcher, "owner_process_status", lambda owner: "alive",
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": "b071b964-8bc9-4af3-bbe7-46d6c36e27f9",
        "transport": "herdr",
        "target": {"pane": "w1:p2"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "passive")

    assert method == "herdr_notification"
    assert calls[0][0] == "notify"


def test_herdr_doorbell_defers_while_the_composer_is_busy(monkeypatch):
    class Herdr:
        def __init__(self, binary):
            self.binary = binary

        def composer_guard(self, pane):
            return handoff_launcher.COMPOSER_BUSY

        def orchestrator_doorbell(self, pane, observed_id, *, forced=False):
            raise AssertionError("a busy composer must not be typed into")

    monkeypatch.setattr(handoffctl.handoff_launcher, "HerdrAdapter", Herdr)
    monkeypatch.setattr(
        handoffctl.handoff_watcher, "owner_process_status", lambda owner: "alive",
    )

    method = handoffctl._notify_orchestrator({
        "orchestrator_id": "b071b964-8bc9-4af3-bbe7-46d6c36e27f9",
        "transport": "herdr",
        "target": {"pane": "w1:p2"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9}, "active")

    assert method == handoff_watcher.DEFERRED_INPUT


def test_herdr_orchestrator_target_requires_a_pane_id(tmp_path):
    with pytest.raises(handoff.HandoffError) as excinfo:
        orchestrator_state(tmp_path, transport="herdr", target={"pane": "session:0.1"})

    assert excinfo.value.exit_code == 5
    assert "pane ID" in str(excinfo.value)


def test_herdr_orchestrator_target_rejects_a_tmux_shaped_target(tmp_path):
    with pytest.raises(handoff.HandoffError) as excinfo:
        orchestrator_state(tmp_path, transport="herdr", target={"handle": "w1:p2"})

    assert excinfo.value.exit_code == 5


def test_herdr_orchestrator_watcher_stays_detached(tmp_path):
    # The herdr server accepts socket clients from outside its process tree, so
    # the doorbell channels a detached watcher uses all work.
    assert handoffctl._resolve_watcher_mode("auto", "herdr") == "detached"
    with pytest.raises(handoff.HandoffError):
        handoffctl._resolve_watcher_mode("surface", "herdr")
