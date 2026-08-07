"""Tests for the handoff kickoff generator and fleet launcher shell helpers."""

import json
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
KICKOFF = SCRIPTS / "handoff_kickoff_new.sh"
FLEET = SCRIPTS / "handoff_fleet.sh"


def run(script, *args, cwd=None):
    return subprocess.run(
        [str(script), *args], capture_output=True, text=True, cwd=cwd
    )


# ---------------------------------------------------------------------------
# handoff_kickoff_new.sh
# ---------------------------------------------------------------------------


def task_file(tmp_path, name="task.md", content="## Objective\nDo the thing.\n"):
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    return f


def test_kickoff_generates_boilerplate_and_task(tmp_path):
    task = task_file(tmp_path)
    out = tmp_path / "kickoff.md"
    r = run(KICKOFF, str(task), "--name", "my-run", "--out", str(out))
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout)
    assert result == {"name": "my-run", "out": str(out)}
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# my-run — worker kickoff")
    assert "## Objective\nDo the thing." in text
    for marker in (
        "## Handoff protocol",
        "HANDOFF_RUN_DIR",
        "emit --type result",
        "paused",
    ):
        assert marker in text


def test_kickoff_without_out_prints_markdown(tmp_path):
    task = task_file(tmp_path)
    r = run(KICKOFF, str(task))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("# task — worker kickoff")
    assert "## Handoff protocol" in r.stdout


def test_kickoff_slugifies_default_name(tmp_path):
    task = task_file(tmp_path, name="My Task!.md")
    r = run(KICKOFF, str(task))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("# my-task — worker kickoff")


def test_kickoff_missing_task_file(tmp_path):
    r = run(KICKOFF, str(tmp_path / "nope.md"))
    assert r.returncode == 2
    assert "not found" in r.stderr


def test_kickoff_empty_task_file(tmp_path):
    task = task_file(tmp_path, content="")
    r = run(KICKOFF, str(task))
    assert r.returncode == 2
    assert "empty" in r.stderr


# ---------------------------------------------------------------------------
# handoff_fleet.sh
# ---------------------------------------------------------------------------


def manifest(tmp_path, lines):
    m = tmp_path / "manifest.tsv"
    m.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return m


def fleet_manifest(tmp_path):
    """Two-row manifest whose kickoffs/repos exist, in a subdir to exercise
    relative-path resolution."""
    sub = tmp_path / "jobs"
    sub.mkdir()
    (sub / "k1.md").write_text("kickoff one", encoding="utf-8")
    (sub / "k2.md").write_text("kickoff two", encoding="utf-8")
    (sub / "repo1").mkdir()
    (sub / "repo2").mkdir()
    m = manifest(
        sub,
        [
            "# a comment",
            "",
            "job-one\tk1.md\trepo1\tpi",
            "job-two\tk2.md\trepo2",
        ],
    )
    return m, sub


def test_fleet_dry_run_plans_rows_and_resolves_paths(tmp_path):
    m, sub = fleet_manifest(tmp_path)
    r = run(FLEET, str(m), "--dry-run")
    assert r.returncode == 0, r.stderr
    lines = [json.loads(l) for l in r.stdout.strip().splitlines()]
    assert len(lines) == 3  # two rows + summary
    row1, row2, summary = lines
    assert row1["dry_run"] is True and row2["dry_run"] is True
    # relative paths resolved against the manifest's directory
    assert f"{sub}/k1.md" in row1["command"]
    assert f"{sub}/repo1" in row1["command"]
    # default agent applied to the row that omitted it
    assert "--agent pi" in row2["command"]
    assert summary == {"summary": {"rows": 2, "ok": 2, "failed": 0}}


def test_fleet_validates_all_rows_before_launching(tmp_path):
    sub = tmp_path / "jobs"
    sub.mkdir()
    (sub / "k1.md").write_text("ok", encoding="utf-8")
    (sub / "repo1").mkdir()
    m = manifest(
        sub,
        [
            "job-one\tk1.md\trepo1\tpi",
            "job-two\tmissing.md\trepo2\tpi",  # invalid row AFTER a valid one
        ],
    )
    r = run(FLEET, str(m), "--dry-run")
    assert r.returncode == 2
    assert "job-two" in r.stderr
    assert "kickoff not found" in r.stderr
    assert r.stdout == ""  # nothing planned or launched


def test_fleet_rejects_bad_agent_with_valid_set(tmp_path):
    m, sub = fleet_manifest(tmp_path)
    bad = manifest(
        sub,
        [
            "job-one\tk1.md\trepo1\tclaude",
            "job-two\tk2.md\trepo2\tturbo-claude",
        ],
    )
    r = run(FLEET, str(bad), "--dry-run")
    assert r.returncode == 2
    assert "codex, claude, kimi, pi" in r.stderr
    assert "job-two" in r.stderr


def test_fleet_rejects_bad_name_slug(tmp_path):
    m, sub = fleet_manifest(tmp_path)
    bad = manifest(sub, ["Bad Name!\tk1.md\trepo1\tpi"])
    r = run(FLEET, str(bad), "--dry-run")
    assert r.returncode == 2
    assert "[a-z0-9-]" in r.stderr
