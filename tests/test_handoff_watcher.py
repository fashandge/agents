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


def coordinator_state(tmp_path, *, transport="tmux", target=None):
    state_path = tmp_path / "coordinator" / "watcher.json"
    value = handoff_watcher.initialize(
        state_path,
        transport=transport,
        target=target or {"handle": "orchestrator:0.1"},
        owner_pid=os.getpid(),
    )
    return state_path, value


def test_retarget_preserves_a_stopped_coordinator_identity_and_runs(tmp_path):
    surface = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    state_path, original = coordinator_state(
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

    assert updated["coordinator_id"] == original["coordinator_id"]
    assert updated["runs"] == {"run-1": handoff_watcher._empty_run_state()}  # noqa: SLF001
    assert updated["target"] == {"workspace": "workspace:9", "surface": surface}


def registered_run(tmp_path, registry, *, name, coordinator_id):
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
        coordinator_token_file=private / "coordinator.token",
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
        coordinator_id=coordinator_id,
    )
    return {
        **initialized,
        "worker": (private / "worker.token").read_bytes(),
        "coordinator": (private / "coordinator.token").read_bytes(),
    }


def checkpoint(run, activity):
    run_dir = Path(run["run_dir"])
    return handoff.emit(
        run_dir,
        run["worker"],
        type="checkpoint",
        body=activity,
        data={
            "state": "working",
            "stage": "implementation",
            "current_activity": activity,
            "inbox_cursor": 0,
            "commitments": [],
        },
    )


def test_watcher_is_singleton_and_isolates_coordinators(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = coordinator_state(tmp_path)
    other_path = tmp_path / "other" / "watcher.json"
    other = handoff_watcher.initialize(
        other_path,
        transport="tmux",
        target={"handle": "other:0.1"},
        owner_pid=os.getpid(),
    )
    owned = registered_run(
        tmp_path, registry, name="owned", coordinator_id=owner["coordinator_id"],
    )
    foreign = registered_run(
        tmp_path, registry, name="foreign", coordinator_id=other["coordinator_id"],
    )
    checkpoint(owned, "owned body must stay on disk")
    checkpoint(foreign, "foreign body must never surface")
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
            notifier=lambda value, coverage: calls.append((value, coverage)),
        )

    assert result["owned_runs"] == [owned["run"]["run_id"]]
    assert calls[0][1] == {owned["run"]["run_id"]: 1}
    assert foreign["run"]["run_id"] not in handoff_watcher.read(state_path)["runs"]


def test_watcher_discovers_new_worker_without_restart(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = coordinator_state(tmp_path)
    calls = []
    with handoff_watcher.instance_lock(state_path):
        first = registered_run(
            tmp_path, registry, name="first", coordinator_id=owner["coordinator_id"],
        )
        checkpoint(first, "first")
        handoff_watcher.poll(
            state_path,
            registry_path=registry,
            notifier=lambda value, coverage: calls.append(dict(coverage)),
        )
        second = registered_run(
            tmp_path, registry, name="second", coordinator_id=owner["coordinator_id"],
        )
        checkpoint(second, "second")
        handoff_watcher.poll(
            state_path,
            registry_path=registry,
            notifier=lambda value, coverage: calls.append(dict(coverage)),
        )

    assert calls == [
        {first["run"]["run_id"]: 1},
        {second["run"]["run_id"]: 1},
    ]


def test_watch_exits_when_owner_process_is_dead(tmp_path, monkeypatch):
    state_path, _ = coordinator_state(tmp_path)
    polls = []
    monkeypatch.setattr(
        handoff_watcher,
        "poll",
        lambda *args, **kwargs: polls.append((args, kwargs)),
    )

    handoff_watcher.watch(
        state_path,
        interval=0.01,
        notifier=lambda value, coverage: None,
        owner_probe=lambda owner: "dead",
    )

    assert polls == []
    assert handoff_watcher.is_running(state_path) is False


def test_watch_suppresses_work_while_owner_liveness_is_unknown(tmp_path, monkeypatch):
    state_path, _ = coordinator_state(tmp_path)
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
        notifier=lambda value, coverage: None,
        owner_probe=lambda owner: next(statuses),
    )

    assert len(polls) == 1


def test_watch_reports_each_live_poll_to_on_poll(tmp_path, monkeypatch):
    state_path, _ = coordinator_state(tmp_path)
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
        notifier=lambda value, coverage: None,
        owner_probe=lambda owner: next(statuses),
        on_poll=reported.append,
    )

    assert reported == [{"attempted": {"run-1": 2}, "errors": []}]


def test_owner_process_status_rejects_pid_reuse(tmp_path, monkeypatch):
    _, value = coordinator_state(tmp_path)
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
    state_path, owner = coordinator_state(tmp_path)
    first = registered_run(
        tmp_path, registry, name="first", coordinator_id=owner["coordinator_id"],
    )
    second = registered_run(
        tmp_path, registry, name="second", coordinator_id=owner["coordinator_id"],
    )
    checkpoint(first, "secret first body")
    checkpoint(second, "secret second body")
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now,
    )
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=31),
    )

    assert calls == [
        {first["run"]["run_id"]: 1, second["run"]["run_id"]: 1},
        {first["run"]["run_id"]: 1, second["run"]["run_id"]: 1},
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
    state_path, owner = coordinator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", coordinator_id=owner["coordinator_id"],
    )
    checkpoint(run, "durable event")
    delivered = []

    def crash(value, coverage):
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
        notifier=lambda value, coverage: delivered.append(dict(coverage)),
    )
    assert delivered[-1] == {run["run"]["run_id"]: 1}
    assert len(delivered) == (2 if delivered_before_crash else 1)


def test_partial_consumption_redoorbells_remainder_and_full_consumption_clears(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = coordinator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", coordinator_id=owner["coordinator_id"],
    )
    checkpoint(run, "one")
    checkpoint(run, "two")
    checkpoint(run, "three")
    calls = []
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
    )
    handoff.control_consume(Path(run["run_dir"]), run["coordinator"], through=1)
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
    )
    handoff.control_consume(Path(run["run_dir"]), run["coordinator"], through=3)
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
    )

    assert calls == [
        {run["run"]["run_id"]: 3},
        {run["run"]["run_id"]: 3},
    ]
    state = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert state["control_through"] == 3
    assert state["doorbell_pending"] is False


def test_coordinator_doorbell_routes_exact_opaque_target(monkeypatch):
    coordinator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    calls = []

    class Tmux:
        def orchestrator_doorbell(self, handle, observed_id):
            calls.append(("tmux", handle, observed_id))

    class Cmux:
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, surface, observed_id):
            calls.append(("cmux_input", self.workspace, surface, observed_id))
            return True

        def orchestrator_doorbell(self, surface, observed_id):
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

    tmux_method = handoffctl._notify_coordinator({
        "coordinator_id": coordinator_id,
        "transport": "tmux",
        "target": {"handle": "session:3.7"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9})
    cmux_method = handoffctl._notify_coordinator({
        "coordinator_id": coordinator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9})
    native_method = handoffctl._notify_coordinator({
        "coordinator_id": coordinator_id,
        "transport": "native_app",
        "target": {"thread_id": "thread-exact"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9})

    assert tmux_method == "terminal_input"
    assert cmux_method == "cmux_input+cmux_notify"
    assert native_method == "native_app"
    assert calls[0] == ("tmux", "session:3.7", coordinator_id)
    assert calls[1] == ("cmux_input", "workspace:9", "surface-uuid", coordinator_id)
    assert calls[2] == ("cmux", "workspace:9", "surface-uuid", coordinator_id)
    assert calls[3][0:2] == ("native_app", "thread-exact")
    assert coordinator_id in calls[3][2]
    assert "run-secret" not in calls[3][2]
    assert "seq 9" not in calls[3][2]


def test_coordinator_cmux_falls_back_to_workspace_notification(monkeypatch):
    coordinator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    calls = []

    class Cmux:
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, handle, observed_id):
            raise handoff_launcher.AdapterError("Broken pipe")

        def orchestrator_doorbell(self, handle, observed_id):
            raise handoff_launcher.AdapterError("Broken pipe")

        def notify(self, handle, *, title, body):
            calls.append((self.workspace, handle, title, body))

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher,
        "owner_process_status",
        lambda owner: "alive",
    )

    method = handoffctl._notify_coordinator({
        "coordinator_id": coordinator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9})

    assert method == "cmux_notify_workspace"
    assert calls == [(
        "workspace:9", None, "Handoff coordinator pending",
        "Worker updates are ready for coordinator review.",
    )]


def test_coordinator_cmux_uses_macos_notification_when_cmux_notifications_fail(monkeypatch):
    coordinator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"
    calls = []

    class Cmux:
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, handle, observed_id):
            raise handoff_launcher.AdapterError("Broken pipe")

        def orchestrator_doorbell(self, handle, observed_id):
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

    method = handoffctl._notify_coordinator({
        "coordinator_id": coordinator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9})

    assert method == "macos_notification"
    assert calls == [(
        "Handoff coordinator pending",
        "Worker updates are ready for coordinator review.",
    )]


def test_coordinator_cmux_reports_combined_error_when_every_channel_fails(monkeypatch):
    coordinator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"

    class Cmux:
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, handle, observed_id):
            raise handoff_launcher.AdapterError("input rejected")

        def orchestrator_doorbell(self, handle, observed_id):
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
        handoffctl._notify_coordinator({
            "coordinator_id": coordinator_id,
            "transport": "cmux",
            "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
            "owner_process": {"pid": os.getpid(), "started_at": "test"},
        }, {"run-secret": 9})

    message = str(captured.value)
    assert "input rejected" in message
    assert "surface gone" in message
    assert "workspace gone" in message
    assert "osascript unavailable" in message


def test_coordinator_cmux_unechoed_input_does_not_count_as_delivery(monkeypatch):
    coordinator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"

    class Cmux:
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, handle, observed_id):
            # Zero-exit send with no visible echo: the code-mode surface trap.
            return False

        def orchestrator_doorbell(self, handle, observed_id):
            pass

    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl.handoff_watcher,
        "owner_process_status",
        lambda owner: "alive",
    )

    method = handoffctl._notify_coordinator({
        "coordinator_id": coordinator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9})

    assert method == "cmux_notify"


def test_coordinator_cmux_confirmed_input_survives_notification_failure(monkeypatch):
    coordinator_id = "b071b964-8bc9-4af3-bbe7-46d6c36e27f9"

    class Cmux:
        def __init__(self, binary):
            self.binary = binary
            self.workspace = None

        def orchestrator_doorbell_input(self, handle, observed_id):
            return True

        def orchestrator_doorbell(self, handle, observed_id):
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

    method = handoffctl._notify_coordinator({
        "coordinator_id": coordinator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9})

    assert method == "cmux_input"


def test_watcher_records_doorbell_channel_and_clears_it_on_failure(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = coordinator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", coordinator_id=owner["coordinator_id"],
    )
    checkpoint(run, "secret body")
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: "cmux_notify",
        now=now,
    )
    state = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert state["last_doorbell_method"] == "cmux_notify"
    assert state["last_doorbell_error"] is None

    def unavailable(value, coverage):
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
    state_path, owner = coordinator_state(tmp_path)
    value = handoff_watcher.read(state_path)
    legacy = handoff_watcher._empty_run_state()  # noqa: SLF001 - state fixture
    del legacy["last_doorbell_method"]
    value["runs"] = {"run-1": legacy}
    handoff_watcher._atomic_write(state_path, value)  # noqa: SLF001 - state fixture

    assert handoff_watcher.read(state_path)["runs"]["run-1"] == legacy


def test_pending_uses_hot_path_and_full_context_only_for_recovery(
    tmp_path, monkeypatch,
):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    state_path, owner = coordinator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", coordinator_id=owner["coordinator_id"],
    )
    first = checkpoint(run, "first")
    second = checkpoint(run, "second")
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: (_ for _ in ()).throw(
            handoff.HandoffError("push unavailable", 6)
        ),
    )
    handoff.control_consume(Path(run["run_dir"]), run["coordinator"], through=1)

    hot = handoffctl._coordinator_pending(state_path, full_context=False)
    assert hot["mode"] == "hot"
    assert hot["pending"][0]["notification"]["last_doorbell_error"] == "push unavailable"
    assert "kickoff" not in hot["pending"][0]["detail"]
    assert [event["message_id"] for event in hot["pending"][0]["detail"]["unread_outbox"]] == [
        second["message"]["message_id"],
    ]
    assert first["message"]["message_id"] not in {
        event["message_id"] for event in hot["pending"][0]["detail"]["unread_outbox"]
    }

    recovery = handoffctl._coordinator_pending(state_path, full_context=True)
    assert recovery["mode"] == "recovery"
    assert "task for worker" in recovery["pending"][0]["detail"]["kickoff"]
    assert recovery["pending"][0]["detail"]["unread_outbox"][0]["seq"] == 2


def test_unowned_registry_run_requires_explicit_adoption(tmp_path):
    registry = tmp_path / "registry.json"
    state_path, owner = coordinator_state(tmp_path)
    run = registered_run(tmp_path, registry, name="worker", coordinator_id=None)
    checkpoint(run, "unowned")
    calls = []

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
    )
    assert calls == []
    handoff_registry.adopt(
        run["run"]["run_id"], owner["coordinator_id"], path=registry,
    )
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
    )
    assert calls == [{run["run"]["run_id"]: 1}]


def test_pending_acknowledges_and_stops_reringing(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    state_path, owner = coordinator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", coordinator_id=owner["coordinator_id"],
    )
    checkpoint(run, "result body")
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now,
    )
    assert calls == [{run["run"]["run_id"]: 1}]

    # Loading the run's events records an acknowledgment cursor.
    handoffctl._coordinator_pending(state_path, full_context=False)
    acked = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert acked["acknowledged_through"] == 1

    # A poll well past the retry interval must NOT re-ring the seen event,
    # even though the protocol outbox cursor was never advanced.
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=61),
    )
    assert calls == [{run["run"]["run_id"]: 1}]
    assert handoff_watcher.read(state_path)["runs"][
        run["run"]["run_id"]
    ]["doorbell_pending"] is True


def test_new_event_after_acknowledge_rerings(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    state_path, owner = coordinator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", coordinator_id=owner["coordinator_id"],
    )
    checkpoint(run, "first")
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now,
    )
    handoffctl._coordinator_pending(state_path, full_context=False)
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=61),
    )
    assert calls == [{run["run"]["run_id"]: 1}]

    # A strictly newer worker event rings again despite the earlier ack.
    checkpoint(run, "second")
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=122),
    )
    assert calls == [{run["run"]["run_id"]: 1}, {run["run"]["run_id"]: 2}]


def test_pending_acknowledges_each_worker_independently(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("HANDOFF_REGISTRY_FILE", str(registry))
    state_path, owner = coordinator_state(tmp_path)
    first = registered_run(
        tmp_path, registry, name="first", coordinator_id=owner["coordinator_id"],
    )
    second = registered_run(
        tmp_path, registry, name="second", coordinator_id=owner["coordinator_id"],
    )
    checkpoint(first, "first body")
    checkpoint(second, "second body")
    calls = []
    now = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now,
    )
    assert calls == [{first["run"]["run_id"]: 1, second["run"]["run_id"]: 1}]

    # One pending call acknowledges every surfaced worker at its own cursor.
    handoffctl._coordinator_pending(state_path, full_context=False)
    runs = handoff_watcher.read(state_path)["runs"]
    assert runs[first["run"]["run_id"]]["acknowledged_through"] == 1
    assert runs[second["run"]["run_id"]]["acknowledged_through"] == 1

    # A new event on ONLY the second worker rings only that worker.
    checkpoint(second, "second follow-up")
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=61),
    )
    assert calls == [
        {first["run"]["run_id"]: 1, second["run"]["run_id"]: 1},
        {second["run"]["run_id"]: 2},
    ]


def test_acknowledge_is_monotonic_and_ignores_unknown_runs(tmp_path):
    state_path, _ = coordinator_state(tmp_path)
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
    state_path, _ = coordinator_state(tmp_path)
    value = handoff_watcher.read(state_path)
    legacy = handoff_watcher._empty_run_state()  # noqa: SLF001 - state fixture
    del legacy["acknowledged_through"]
    value["runs"] = {"run-1": legacy}
    handoff_watcher._atomic_write(state_path, value)  # noqa: SLF001 - state fixture

    assert handoff_watcher.read(state_path)["runs"]["run-1"] == legacy


def test_remote_watcher_reads_authoritative_host_without_credentials(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    state_path, owner = coordinator_state(tmp_path)
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
        coordinator_id=owner["coordinator_id"],
    )
    commands = []

    def remote_json(record, argv):
        commands.append((record["host"], list(argv)))
        if argv[0:2] == ["control", "show"]:
            return {"outbox_cursor": 0}
        return [{"seq": 1, "type": "question", "body": "remains remote"}]

    monkeypatch.setattr(handoff_watcher, "_remote_json", remote_json)
    calls = []
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
    )

    assert calls == [{"remote-run": 1}]
    assert commands == [
        ("worker", ["control", "show", "--run-dir", "/remote/state/run"]),
        (
            "worker",
            [
                "read", "--run-dir", "/remote/state/run", "--journal", "outbox",
                "--after", "0",
            ],
        ),
    ]
    assert "token" not in repr(commands).lower()
