#!/usr/bin/env python3
"""Garbage-collect stale remote handoff worker sessions.

Handoff workers are interactive agent TUIs: after a protocol ``stop`` the agent
process stays resident, so its remote ``tmux`` session lingers until something
kills it. Over time a host accumulates dead-but-attached sessions, and because
``cmux ssh-tmux`` mirrors a host WHOLE, that pile shows up as stray mirror
workspaces. This is the explicit, opt-in cleanup: for every remote run in the
registry it verifies — ON the owning host — that the run is terminal
(``handoff.status`` state in succeeded/failed/stopped) and that its tmux session
still exists, then (with ``--yes``) kills only those sessions. A run that is not
positively terminal is never touched, so a live worker can never be reaped.

Terminal state is re-verified on the host at kill time (not just at probe time),
so a run that goes live between probe and kill is skipped.

Safety: read-only by default (dry-run). ``--yes`` performs kills. ``--forget``
additionally removes the cleaned runs' registry records (like ``runs forget``);
``--delete-run-dir`` also deletes each cleaned run's directory on its host
(guarded: only a directory that still contains ``status.json``). No token or
credential material is read, printed, or accepted.

Scope: by default every remote run in the registry is considered. Narrow it with
``--host NAME``, and/or restrict to the workers a single orchestrator session
spawned with ``--orchestrator <id>`` (or ``--orchestrator-state <watcher.json>``
to derive that id from this session's watcher state).

Usage:
    python -m agents.orchestration.handoff_remote_gc \
        [--host NAME] [--orchestrator ID | --orchestrator-state PATH] \
        [--yes] [--forget] [--delete-run-dir]

Output: one JSON object on stdout describing what was (or would be) done.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from collections import defaultdict
from typing import Any

from agents.orchestration import handoff
from agents.orchestration import handoff_launcher
from agents.orchestration import handoff_registry


# Runs on the owning host. Reads each run's terminal state via the SAME
# definition the registry's prune/forget use, checks tmux session liveness, and
# — in kill mode — reaps only sessions whose run re-verifies as terminal.
REMOTE_PROBE_PROGRAM = r"""
import base64, json, shutil, subprocess, sys
from pathlib import Path
from agents.orchestration import handoff

TERMINAL = {"succeeded", "failed", "stopped"}
mode = sys.argv[1]
runs = json.loads(base64.b64decode(sys.argv[2]))
out = []
for r in runs:
    e = {"run_id": r["run_id"], "handle": r["handle"], "run_dir": r["run_dir"]}
    try:
        e["state"] = handoff.status(Path(r["run_dir"]))["state"]
    except Exception as ex:  # noqa: BLE001 - report, never crash the batch
        e["state"] = None
        e["state_error"] = str(ex)
    try:
        e["desired_state"] = handoff.control_show(Path(r["run_dir"]))["desired_state"]
    except Exception as ex:  # noqa: BLE001
        e["desired_state"] = None
    e["session_alive"] = subprocess.run(
        ["tmux", "has-session", "-t", r["handle"]],
        capture_output=True,
    ).returncode == 0
    # Terminal for gc = the worker self-reported a terminal state OR the
    # orchestrator concluded the run (desired_state stop). The latter is the
    # real case here: a concluded worker stays resident and never emits its
    # terminal checkpoint, so status alone would never let us reap it.
    terminal = e["state"] in TERMINAL or e.get("desired_state") == "stop"
    e["terminal"] = terminal
    if mode == "kill" and terminal and e["session_alive"]:
        kr = subprocess.run(
            ["tmux", "kill-session", "-t", r["handle"]],
            capture_output=True, text=True,
        )
        e["killed"] = kr.returncode == 0
        if kr.returncode != 0:
            e["kill_error"] = (kr.stderr or kr.stdout).strip()
        if e.get("killed") and r.get("delete_run_dir"):
            d = Path(r["run_dir"])
            if (d / "status.json").is_file():
                try:
                    shutil.rmtree(d)
                    e["run_dir_deleted"] = True
                except Exception as ex:  # noqa: BLE001
                    e["run_dir_deleted"] = False
                    e["delete_error"] = str(ex)
            else:
                e["run_dir_deleted"] = False
                e["delete_error"] = "refused: no status.json under run_dir"
    out.append(e)
print(json.dumps(out))
"""


def _orchestrator_id_from_state(state_path: str) -> str:
    """Read the orchestrator_id from a watcher state file (handoffctl state)."""
    with open(state_path, encoding="utf-8") as handle:
        state = json.load(handle)
    orchestrator_id = state.get("orchestrator_id")
    if not orchestrator_id:
        raise ValueError(f"no orchestrator_id in {state_path}")
    return orchestrator_id


def _remote_runs(
    host_filter: str | None, orchestrator_filter: str | None,
) -> list[dict[str, Any]]:
    """Registry records for remote runs, optionally limited to one host and/or
    to the workers a single orchestrator session spawned."""
    records = handoff_registry.list_records(private=True)
    remote = [r for r in records if r.get("host")]
    if host_filter is not None:
        remote = [r for r in remote if r["host"] == host_filter]
    if orchestrator_filter is not None:
        remote = [r for r in remote if r.get("orchestrator_id") == orchestrator_filter]
    return remote


def _group_by_host(records: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group remote runs by (host, remote_python) so each host is one SSH."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        python = r.get("remote_python") or handoff_launcher.REMOTE_PYTHON_DEFAULT
        groups[(r["host"], python)].append(r)
    return groups


def _probe_host(
    host: str, python: str, runs: list[dict[str, Any]], *, mode: str, delete_run_dir: bool,
) -> list[dict[str, Any]]:
    payload = [
        {
            "run_id": r["run_id"], "handle": r["handle"], "run_dir": r["run_dir"],
            "delete_run_dir": delete_run_dir,
        }
        for r in runs
    ]
    encoded = base64.b64encode(json.dumps(payload).encode()).decode("ascii")
    result = handoff_launcher._run_ssh(  # noqa: SLF001 - shared SSH discipline
        host,
        [python, "-c", REMOTE_PROBE_PROGRAM, mode, encoded],
        stdin="",
        timeout=60.0,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise handoff_launcher.AdapterError(f"remote gc probe on {host} returned no JSON")
    return json.loads(lines[-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Garbage-collect stale remote handoff worker sessions")
    parser.add_argument("--host", help="limit to one owning host")
    parser.add_argument("--orchestrator", help="limit to runs spawned by this orchestrator id")
    parser.add_argument("--orchestrator-state", help="derive --orchestrator from a watcher state file")
    parser.add_argument("--yes", action="store_true", help="perform kills (default: dry-run)")
    parser.add_argument("--forget", action="store_true", help="also remove cleaned runs' registry records")
    parser.add_argument("--delete-run-dir", action="store_true", help="with --forget, also delete each cleaned run's directory")
    args = parser.parse_args(argv)

    if args.delete_run_dir and not args.forget:
        parser.error("--delete-run-dir requires --forget")
    if (args.forget or args.delete_run_dir) and not args.yes:
        parser.error("--forget/--delete-run-dir require --yes (they mutate state)")

    orchestrator_filter = args.orchestrator
    if args.orchestrator_state is not None:
        try:
            resolved = _orchestrator_id_from_state(args.orchestrator_state)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(f"--orchestrator-state: {exc}")
        if orchestrator_filter is not None and orchestrator_filter != resolved:
            parser.error("--orchestrator and --orchestrator-state disagree")
        orchestrator_filter = resolved

    remote = _remote_runs(args.host, orchestrator_filter)
    mode = "kill" if args.yes else "probe"
    groups = _group_by_host(remote)

    rows: list[dict[str, Any]] = []
    probe_errors: list[dict[str, str]] = []
    for (host, python), runs in groups.items():
        try:
            host_rows = _probe_host(host, python, runs, mode=mode, delete_run_dir=args.delete_run_dir)
        except handoff_launcher.AdapterError as exc:
            probe_errors.append({"host": host, "error": str(exc)})
            continue
        for row in host_rows:
            row["host"] = host
        rows.extend(host_rows)

    killable = [r for r in rows if r.get("terminal") and r.get("session_alive")]
    killed = [r for r in rows if r.get("killed")]
    skipped_live = [
        {"run_id": r["run_id"], "handle": r["handle"], "host": r["host"], "state": r.get("state")}
        for r in rows
        if r.get("session_alive") and not r.get("terminal")
    ]

    forgotten: list[str] = []
    forget_errors: list[dict[str, str]] = []
    if args.forget:
        for r in killed:
            try:
                handoff_registry.forget(r["run_id"], force=True, delete_run_dir=False)
                forgotten.append(r["run_id"])
            except handoff.HandoffError as exc:
                forget_errors.append({"run_id": r["run_id"], "error": str(exc)})

    summary = {
        "dry_run": not args.yes,
        "scope": {"host": args.host, "orchestrator": orchestrator_filter},
        "remote_runs_examined": len(rows),
        "stale_sessions": [
            {"run_id": r["run_id"], "handle": r["handle"], "host": r["host"], "state": r.get("state")}
            for r in killable
        ],
        "killed": [
            {
                "run_id": r["run_id"], "handle": r["handle"], "host": r["host"],
                "run_dir_deleted": r.get("run_dir_deleted"),
            }
            for r in killed
        ],
        "skipped_live_workers": skipped_live,
        "forgotten": forgotten,
        "probe_errors": probe_errors,
        "forget_errors": forget_errors,
    }
    print(json.dumps(summary, indent=2))
    return 0 if not (probe_errors or forget_errors) else 1


if __name__ == "__main__":
    sys.exit(main())
