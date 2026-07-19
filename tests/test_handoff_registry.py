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
            coordinator_id=first_id,
        )

    adopted = handoff_registry.adopt("run-one", first_id, path=registry)
    assert adopted["coordinator_id"] == first_id
    with pytest.raises(handoff.HandoffError, match="another coordinator"):
        handoff_registry.adopt("run-one", second_id, path=registry)
