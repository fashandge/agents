import base64
import json
import subprocess

import pytest

from agents.orchestration import handoff_remote_gc as gc
from agents.orchestration import handoff_launcher


def _rec(run_id, host, handle, *, python="/py"):
    return {
        "run_id": run_id, "host": host, "handle": handle,
        "run_dir": f"/remote/state/{run_id}", "remote_python": python,
    }


def test_group_by_host_partitions_by_host_and_python():
    records = [
        _rec("a", "oci-box", "ha"),
        _rec("b", "oci-box", "hb"),
        _rec("c", "other", "hc", python="/py2"),
    ]
    groups = gc._group_by_host(records)
    assert groups[("oci-box", "/py")] and len(groups[("oci-box", "/py")]) == 2
    assert groups[("other", "/py2")][0]["run_id"] == "c"


def test_remote_runs_filters_local_and_by_host(monkeypatch):
    monkeypatch.setattr(gc.handoff_registry, "list_records", lambda private: [
        _rec("remote1", "oci-box", "h1"),
        {"run_id": "local1", "host": None, "handle": "x", "run_dir": "/l", "remote_python": None},
        _rec("remote2", "box2", "h2"),
    ])
    assert {r["run_id"] for r in gc._remote_runs(None)} == {"remote1", "remote2"}
    assert {r["run_id"] for r in gc._remote_runs("oci-box")} == {"remote1"}


def _fake_ssh_returning(rows):
    def fake(host, argv, *, stdin, timeout):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")
    return fake


def test_dry_run_reports_stale_without_killing_or_forgetting(monkeypatch, capsys):
    monkeypatch.setattr(gc.handoff_registry, "list_records", lambda private: [
        _rec("done", "oci-box", "done-handle"),
        _rec("live", "oci-box", "live-handle"),
    ])
    # Probe mode: one terminal+alive (killable), one live worker (must be skipped).
    monkeypatch.setattr(handoff_launcher, "_run_ssh", _fake_ssh_returning([
        {"run_id": "done", "handle": "done-handle", "run_dir": "/r/done",
         "state": "awaiting_review", "desired_state": "stop", "session_alive": True, "terminal": True},
        {"run_id": "live", "handle": "live-handle", "run_dir": "/r/live",
         "state": "working", "desired_state": "run", "session_alive": True, "terminal": False},
    ]))
    forgets = []
    monkeypatch.setattr(gc.handoff_registry, "forget", lambda *a, **k: forgets.append((a, k)))

    rc = gc.main([])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["dry_run"] is True
    assert [s["run_id"] for s in out["stale_sessions"]] == ["done"]
    assert [s["run_id"] for s in out["skipped_live_workers"]] == ["live"]
    assert out["killed"] == [] and forgets == []


def test_yes_kills_terminal_sessions_and_forget_removes_records(monkeypatch, capsys):
    monkeypatch.setattr(gc.handoff_registry, "list_records", lambda private: [
        _rec("done", "oci-box", "done-handle"),
        _rec("live", "oci-box", "live-handle"),
    ])
    # Kill mode: the host re-verifies and reports the reaped session as killed;
    # the live worker is not terminal, so the host leaves it and reports killed absent.
    monkeypatch.setattr(handoff_launcher, "_run_ssh", _fake_ssh_returning([
        {"run_id": "done", "handle": "done-handle", "run_dir": "/r/done",
         "state": "stopped", "desired_state": "stop", "session_alive": True,
         "terminal": True, "killed": True},
        {"run_id": "live", "handle": "live-handle", "run_dir": "/r/live",
         "state": "working", "desired_state": "run", "session_alive": True, "terminal": False},
    ]))
    forgets = []
    monkeypatch.setattr(gc.handoff_registry, "forget", lambda sel, **k: forgets.append((sel, k)))

    rc = gc.main(["--yes", "--forget"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [k["run_id"] for k in out["killed"]] == ["done"]
    assert out["forgotten"] == ["done"]
    assert forgets == [("done", {"force": True, "delete_run_dir": False})]


def test_forget_requires_yes(monkeypatch, capsys):
    monkeypatch.setattr(gc.handoff_registry, "list_records", lambda private: [])
    with pytest.raises(SystemExit):
        gc.main(["--forget"])


def test_delete_run_dir_requires_forget(monkeypatch):
    monkeypatch.setattr(gc.handoff_registry, "list_records", lambda private: [])
    with pytest.raises(SystemExit):
        gc.main(["--yes", "--delete-run-dir"])


def test_probe_error_is_reported_and_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(gc.handoff_registry, "list_records", lambda private: [
        _rec("x", "oci-box", "hx"),
    ])
    def boom(host, argv, *, stdin, timeout):
        raise handoff_launcher.AdapterError("connect timed out")
    monkeypatch.setattr(handoff_launcher, "_run_ssh", boom)
    rc = gc.main([])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["probe_errors"][0]["host"] == "oci-box"
    assert out["killed"] == []


# --- Real integration of the on-host probe/kill snippet against local tmux ----

def _run_probe_program(mode, runs):
    encoded = base64.b64encode(json.dumps(runs).encode()).decode()
    result = subprocess.run(
        [handoff_launcher.sys.executable, "-c", gc.REMOTE_PROBE_PROGRAM, mode, encoded],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(subprocess.run(["which", "tmux"], capture_output=True).returncode != 0,
                    reason="tmux not available")
def test_probe_program_never_kills_a_nonterminal_live_session(tmp_path):
    # Safety property with a REAL tmux session: an unreadable/absent run state
    # (no status.json, no control.json => state None, desired_state None) is not
    # terminal, so even in kill mode the live session must be left running.
    session = "handoff-gc-selftest"
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "sleep 300"], check=True)
    try:
        runs = [{"run_id": "self", "handle": session, "run_dir": str(tmp_path / "no-run")}]
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
