import datetime
import stat
from pathlib import Path

import pytest

from agents.orchestration import handoff
from agents.orchestration import handoff_registry
from agents.orchestration import handoff_watcher
from agents.orchestration import handoffctl


def coordinator_state(tmp_path, *, transport="tmux", target=None):
    state_path = tmp_path / "coordinator" / "watcher.json"
    value = handoff_watcher.initialize(
        state_path,
        transport=transport,
        target=target or {"handle": "orchestrator:0.1"},
    )
    return state_path, value


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

        def orchestrator_doorbell(self, surface, observed_id):
            calls.append(("cmux", self.workspace, surface, observed_id))

    monkeypatch.setattr(handoffctl.handoff_launcher, "TmuxAdapter", Tmux)
    monkeypatch.setattr(handoffctl.handoff_launcher, "CmuxAdapter", Cmux)
    monkeypatch.setattr(
        handoffctl,
        "_send_native_app_doorbell",
        lambda thread_id, body: calls.append(("native_app", thread_id, body)),
    )

    handoffctl._notify_coordinator({
        "coordinator_id": coordinator_id,
        "transport": "tmux",
        "target": {"handle": "session:3.7"},
    }, {"run-secret": 9})
    handoffctl._notify_coordinator({
        "coordinator_id": coordinator_id,
        "transport": "cmux",
        "target": {"workspace": "workspace:9", "surface": "surface-uuid"},
    }, {"run-secret": 9})
    handoffctl._notify_coordinator({
        "coordinator_id": coordinator_id,
        "transport": "native_app",
        "target": {"thread_id": "thread-exact"},
    }, {"run-secret": 9})

    assert calls[0] == ("tmux", "session:3.7", coordinator_id)
    assert calls[1] == ("cmux", "workspace:9", "surface-uuid", coordinator_id)
    assert calls[2][0:2] == ("native_app", "thread-exact")
    assert coordinator_id in calls[2][2]
    assert "run-secret" not in calls[2][2]
    assert "seq 9" not in calls[2][2]


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
