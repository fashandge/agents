"""Tests for coding_agents_cli.run_detached (daemonized agent runs)."""

import os
import time
from pathlib import Path

import pytest

from agents import coding_agents_cli


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} did not appear within {timeout}s")


def test_run_detached_success(tmp_path):
    base = tmp_path / "run"

    def runner():
        return coding_agents_cli.AgentResult(
            output="the answer", returncode=0, stderr="", agent=coding_agents_cli.AgentType.CODEX
        )

    info = coding_agents_cli.run_detached(runner, base)

    assert info["pid"] > 0
    _wait_for(Path(info["exitcode"]))
    assert Path(info["exitcode"]).read_text() == "0"
    assert Path(info["out"]).read_text() == "the answer"
    assert Path(info["pid_file"]).read_text() == str(info["pid"])
    # The daemon is not our child: it must not linger as a zombie we could wait on.
    with pytest.raises(ChildProcessError):
        os.waitpid(info["pid"], os.WNOHANG)


def test_run_detached_runner_exception(tmp_path):
    base = tmp_path / "boom"

    def runner():
        raise RuntimeError("kaboom")

    info = coding_agents_cli.run_detached(runner, base)

    _wait_for(Path(info["exitcode"]))
    assert Path(info["exitcode"]).read_text() == "1"
    assert not Path(info["out"]).exists()
    assert "kaboom" in Path(info["err"]).read_text()


def test_run_detached_creates_parent_dirs(tmp_path):
    base = tmp_path / "deep" / "nested" / "run"

    def runner():
        return coding_agents_cli.AgentResult(
            output="ok", returncode=0, stderr="", agent=coding_agents_cli.AgentType.CLAUDE
        )

    info = coding_agents_cli.run_detached(runner, base)
    _wait_for(Path(info["exitcode"]))
    assert Path(info["out"]).read_text() == "ok"
