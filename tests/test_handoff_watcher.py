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
            notifier=lambda value, coverage: calls.append((value, coverage)),
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
            notifier=lambda value, coverage: calls.append(dict(coverage)),
        )
        second = registered_run(
            tmp_path, registry, name="second", orchestrator_id=owner["orchestrator_id"],
        )
        awaiting_review(second, "second")
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
        notifier=lambda value, coverage: None,
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
        notifier=lambda value, coverage: None,
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
        notifier=lambda value, coverage: None,
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
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    awaiting_review(run, "durable event")
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
        notifier=lambda value, coverage: calls.append(dict(coverage)),
    )
    handoff.control_consume(Path(run["run_dir"]), run["orchestrator"], through=1)
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
    )
    handoff.control_consume(Path(run["run_dir"]), run["orchestrator"], through=3)
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
    }, {"run-secret": 9})
    cmux_method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9})
    native_method = handoffctl._notify_orchestrator({
        "orchestrator_id": orchestrator_id,
        "transport": "native_app",
        "target": {"thread_id": "thread-exact"},
        "owner_process": {"pid": os.getpid(), "started_at": "test"},
    }, {"run-secret": 9})

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
    }, {"run-secret": 9})

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
    }, {"run-secret": 9})

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
        }, {"run-secret": 9})

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
    }, {"run-secret": 9})

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
    }, {"run-secret": 9})

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
    state_path, owner = orchestrator_state(tmp_path)
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
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    first = checkpoint(run, "first")
    second = awaiting_review(run, "second")
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: (_ for _ in ()).throw(
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
        notifier=lambda value, coverage: calls.append(dict(coverage)),
    )
    assert calls == []
    handoff_registry.adopt(
        run["run"]["run_id"], owner["orchestrator_id"], path=registry,
    )
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
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
        notifier=lambda value, coverage: calls.append(dict(coverage)),
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
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now,
    )
    handoffctl._orchestrator_pending(state_path, full_context=False)
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=61),
    )
    assert calls == [{run["run"]["run_id"]: 1}]

    # A strictly newer worker event rings again despite the earlier ack.
    nonfatal_error(run, "second")
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
        notifier=lambda value, coverage: calls.append(dict(coverage)),
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
        notifier=lambda value, coverage: calls.append(dict(coverage)),
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
        notifier=lambda value, coverage: calls.append(dict(coverage)),
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
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now,
    )
    handoff_watcher.acknowledge(state_path, {run["run"]["run_id"]: 1})
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
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
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now + datetime.timedelta(seconds=62),
    )

    assert calls == [
        {run["run"]["run_id"]: 1},
        {run["run"]["run_id"]: 1},
    ]


@pytest.mark.parametrize("state", ["succeeded", "stopped"])
def test_terminal_worker_state_does_not_ring(tmp_path, state):
    registry = tmp_path / "registry.json"
    state_path, owner = orchestrator_state(tmp_path)
    run = registered_run(
        tmp_path, registry, name="worker", orchestrator_id=owner["orchestrator_id"],
    )
    if state == "succeeded":
        result = awaiting_review(run)
        handoff.control_consume(Path(run["run_dir"]), run["orchestrator"], through=1)
        handoff.send(
            Path(run["run_dir"]),
            run["orchestrator"],
            type="review",
            data={"disposition": "accepted"},
            reply_to=result["message"]["message_id"],
        )
    else:
        handoff.send(
            Path(run["run_dir"]),
            run["orchestrator"],
            type="stop",
            body="Stop this worker.",
            data={"reason": "No longer needed"},
        )
    worker_checkpoint(run, f"worker {state}", state=state, inbox_cursor=1)
    calls = []

    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
    )

    notification = handoff_watcher.read(state_path)["runs"][run["run"]["run_id"]]
    assert calls == []
    assert notification["doorbell_pending"] is True


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
        notifier=lambda value, coverage: calls.append(dict(coverage)),
        now=now,
    )
    handoff_watcher.dismiss(state_path, {run["run"]["run_id"]: 1})
    handoff_watcher.poll(
        state_path,
        registry_path=registry,
        notifier=lambda value, coverage: calls.append(dict(coverage)),
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
        notifier=lambda value, coverage: calls.append(dict(coverage)),
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

    def deferring(value, coverage):
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
        notifier=lambda value, coverage: "cmux_input+cmux_notify",
        now=now + datetime.timedelta(seconds=2),
    )
    state = handoff_watcher.read(state_path)["runs"][run_id]
    assert state["last_doorbell_at"] is not None
    assert state["doorbell_through"] > 0
    assert state["last_doorbell_method"] == "cmux_input+cmux_notify"

    # And a delivered doorbell resumes normal backoff instead of re-ringing.
    handoff_watcher.poll(
        state_path, registry_path=registry,
        notifier=lambda value, coverage: pytest.fail("must back off after delivery"),
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
    }, {"run-secret": 9})

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
    }, {"run-secret": 9})

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
    }, {"run-secret": 9})

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
    }, {"run-secret": 9})

    assert banners == ["Handoff orchestrator pending"]
    assert method == f"{handoff_watcher.DEFERRED_INPUT}+macos_notification"
