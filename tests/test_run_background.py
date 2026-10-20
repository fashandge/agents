"""Tests for coding_agents_cli.run_background / detached_status (spawned background runs)."""

import json
import os
import time
from pathlib import Path

import pytest

from agents import coding_agents_cli

# Child code that swaps in a fake agent before running the job, so no agent binary runs.
FAKE_AGENT_CHILD = (
    "import sys; from agents import coding_agents_cli as c; "
    "c.run_with_config = lambda p, cfg, timeout=None, **kw: "
    "c.AgentResult(output=f'{cfg.model}:{cfg.claude_working_dir}:{timeout}:' + p.upper(), "
    "returncode=0 if 'fail' not in p else 3, stderr='agent warning', agent=cfg.agent); "
    "sys.exit(c.run_background_job(sys.argv[1]))"
)


def _wait_finished(base, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = coding_agents_cli.detached_status(base)
        if status["state"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("background run did not finish")


@pytest.fixture
def fake_agent(monkeypatch):
    monkeypatch.setattr(coding_agents_cli, "_BACKGROUND_CHILD_CODE", FAKE_AGENT_CHILD)


def test_background_run_records_output(fake_agent, tmp_path):
    base = tmp_path / "deep" / "run"
    config = coding_agents_cli.AgentConfig(agent="claude", model="opus", claude_working_dir=tmp_path)

    info = coding_agents_cli.run_background("hello", config, base, timeout=60)

    assert Path(info["pid_file"]).read_text() == str(info["pid"])
    status = _wait_finished(base)
    assert status["state"] == "done" and status["returncode"] == 0
    assert status["output"] == f"opus:{tmp_path}:60:HELLO"
    assert "agent warning" in Path(info["err"]).read_text()
    job = json.loads(Path(f"{base}.job").read_text())
    assert job["configs"][0]["agent"] == "claude" and job["prompt"] == "hello"


def test_background_run_failure_reports_error(fake_agent, tmp_path):
    base = tmp_path / "run"
    coding_agents_cli.run_background("please fail", coding_agents_cli.AgentConfig(agent="claude"), base)
    status = _wait_finished(base)
    assert status["state"] == "failed" and status["returncode"] == 3
    assert "agent warning" in status["error"]


def test_background_child_exception_writes_traceback(monkeypatch, tmp_path):
    monkeypatch.setattr(coding_agents_cli, "_BACKGROUND_CHILD_CODE",
                        "import sys; from agents import coding_agents_cli as c; "
                        "c.run_with_config = None; sys.exit(c.run_background_job(sys.argv[1]))")
    base = tmp_path / "run"
    coding_agents_cli.run_background("x", coding_agents_cli.AgentConfig(agent="claude"), base)
    status = _wait_finished(base)
    assert status["state"] == "failed" and status["returncode"] == 1
    assert "TypeError" in status["error"] and status["output"] == ""


def test_background_child_is_reaped(fake_agent, tmp_path):
    base = tmp_path / "run"
    info = coding_agents_cli.run_background("hi", coding_agents_cli.AgentConfig(agent="claude"), base)
    _wait_finished(base)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.waitpid(info["pid"], os.WNOHANG)
        except ChildProcessError:
            return  # already reaped by run_background's waiter thread
        time.sleep(0.05)
    raise AssertionError("child was not reaped")


def test_status_running_and_lost(tmp_path):
    base = tmp_path / "run"
    Path(f"{base}.pid").write_text(str(os.getpid()))
    assert coding_agents_cli.detached_status(base)["state"] == "running"
    Path(f"{base}.pid").write_text("999999")
    Path(f"{base}.err").write_text("killed")
    status = coding_agents_cli.detached_status(base)
    assert status["state"] == "lost" and status["error"] == "killed"


def test_status_of_missing_run(tmp_path):
    with pytest.raises(FileNotFoundError):
        coding_agents_cli.detached_status(tmp_path / "nope")


def test_fallback_chain_and_empty_configs(fake_agent, tmp_path):
    with pytest.raises(ValueError):
        coding_agents_cli.run_background("x", [], tmp_path / "run")
    base = tmp_path / "chain"
    configs = [coding_agents_cli.AgentConfig(agent="claude", model="m1"),
               coding_agents_cli.AgentConfig(agent="claude", model="m2")]
    coding_agents_cli.run_background("x", configs, base)
    # The fake only replaces run_with_config, which the fallback chain calls per config.
    assert _wait_finished(base)["output"].startswith("m1:")
