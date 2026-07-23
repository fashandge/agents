"""Unified cleanup for finished handoff runs: sessions, records, run dirs.

This is the single teardown path behind ``handoffctl runs clean``.  For every
selected run it verifies — with one shared definition, locally or on the
owning host — that the run is terminal, kills the leftover worker session if
it still exists (a handoff worker is an interactive agent TUI that stays
resident after the run ends, so its tmux session or cmux surface lingers),
optionally deletes the run directory, and drops the registry record.

A run counts as terminal when the orchestrator marked it finished (the
registry ``finished_at`` marker), when its live worker epoch declared a fatal
error (derived from the outbox, never stored), when its run directory is gone
(a dangling pointer), or when a transport probe *answered* that the session
is absent (an unreachable transport yields *unknown* and skips the run —
never reaps it).  As permanent read tolerance for pre-collapse run dirs, a
legacy terminal status projection (``failed``/``stopped``) or legacy
``desired_state == "stop"`` also counts.  Terminality is re-verified at kill
time, so a run that goes live between probe and kill is never reaped; a live
(non-terminal) run's session is never killed and its record is never dropped
unless ``force`` is given.  Invalid registry records are never bulk-cleaned:
they are reported as skipped so they can be cleaned deliberately with
``runs clean --run``.

Remote runs are verified and reaped ON the owning host by
:data:`REMOTE_PROBE_PROGRAM`, which runs over SSH against the installed
agents package; their registry records are dropped locally afterward.
"""

from __future__ import annotations

import base64
import datetime
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from agents.orchestration import handoff
from agents.orchestration import handoff_launcher
from agents.orchestration import handoff_registry


# Runs on the owning host.  Reads each run's terminal facts — the owning
# host's finished marker, the derived fatal boolean, legacy status states and
# legacy desired_state (read tolerance for pre-collapse run dirs) — checks
# tmux session liveness, and — in kill mode — reaps only sessions whose run
# re-verifies as terminal (or is explicitly forced).
REMOTE_PROBE_PROGRAM = r"""
import base64, json, shutil, subprocess, sys
from pathlib import Path
from agents.orchestration import handoff
from agents.orchestration import handoff_registry

mode = sys.argv[1]
runs = json.loads(base64.b64decode(sys.argv[2]))
out = []
for r in runs:
    e = {"run_id": r["run_id"], "handle": r["handle"], "run_dir": r["run_dir"]}
    d = Path(r["run_dir"])
    e["run_dir_present"] = d.is_dir()
    try:
        e["state"] = handoff.status(d)["state"]
    except Exception as ex:  # noqa: BLE001 - report, never crash the batch
        e["state"] = None
        e["state_error"] = str(ex)
    try:
        # Legacy key, absent from post-collapse control snapshots.
        e["desired_state"] = handoff.control_show(d).get("desired_state")
    except Exception as ex:  # noqa: BLE001
        e["desired_state"] = None
    try:
        e["fatal"] = handoff.is_fatal(d)
    except Exception as ex:  # noqa: BLE001
        e["fatal"] = None
    try:
        owning = handoff_registry.resolve(r["run_id"])
        e["finished"] = bool(owning.get("finished_at"))
    except Exception as ex:  # noqa: BLE001
        e["finished"] = None
    e["session_alive"] = subprocess.run(
        ["tmux", "has-session", "-t", r["handle"]],
        capture_output=True,
    ).returncode == 0
    # Terminal for cleanup = the orchestrator marked the run finished OR the
    # live worker epoch declared a fatal error OR a legacy terminal fact
    # (pre-collapse status or desired_state stop) OR the run directory is
    # gone (a dangling pointer).
    terminal = (
        bool(e.get("finished"))
        or bool(e.get("fatal"))
        or e["state"] in handoff_registry.TERMINAL_STATES
        or e.get("desired_state") == "stop"
        or not e["run_dir_present"]
    )
    e["terminal"] = terminal
    requested_facts = set(r.get("facts", []))
    facts = {
        "finished": bool(e.get("finished")),
        "fatal": bool(e.get("fatal")),
        "dead": e["session_alive"] is False,
    }
    e["selected"] = not requested_facts or any(facts[name] for name in requested_facts)
    # Match the caller's local gate: a transport-confirmed absent session is
    # independently reapable even when no journal/marker terminal fact exists.
    # This matters for --dead --delete-run-dir; otherwise the caller drops its
    # proxy while this owning-host probe silently leaves the requested run dir.
    reapable = terminal or e["session_alive"] is False or r.get("force")
    if mode == "kill" and e["selected"] and reapable:
        if e["session_alive"]:
            kr = subprocess.run(
                ["tmux", "kill-session", "-t", r["handle"]],
                capture_output=True, text=True,
            )
            e["killed"] = kr.returncode == 0
            if kr.returncode != 0:
                e["kill_error"] = (kr.stderr or kr.stdout).strip()
        if r.get("delete_run_dir") and e["run_dir_present"]:
            if e["session_alive"] and not e.get("killed"):
                # Never pull the run directory out from under a session that
                # is still up.
                e["run_dir_deleted"] = False
                e["delete_error"] = "refused: session is still running"
            elif (d / "status.json").is_file():
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

_CMUX_UUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
# Settle time between closing a cmux surface and verifying the rest of the
# layout survived; tests patch it to zero.
_CMUX_VERIFY_SETTLE_SECONDS = 1.0


def _group_by_host(records: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group remote runs by (host, remote_python) so each host is one SSH."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        python = record.get("remote_python") or handoff_launcher.REMOTE_PYTHON_DEFAULT
        groups[(record["host"], python)].append(record)
    return groups


def _probe_host(
    host: str, python: str, runs: list[dict[str, Any]], *,
    mode: str, force: bool, delete_run_dir: bool, facts: list[str],
) -> list[dict[str, Any]]:
    payload = [
        {
            "run_id": r["run_id"], "handle": r["handle"], "run_dir": r["run_dir"],
            "force": force, "delete_run_dir": delete_run_dir, "facts": facts,
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
        raise handoff_launcher.AdapterError(f"remote cleanup probe on {host} returned no JSON")
    return json.loads(lines[-1])


def _uuids(text: str) -> set[str]:
    return {match.lower() for match in _CMUX_UUID_RE.findall(text)}


def _cmux_list(binary: str, subject: str) -> str | None:
    """One cmux listing; ``None`` when cmux is unreachable or refuses."""
    try:
        result = handoff_launcher._run(  # noqa: SLF001 - shared cmux discipline
            [binary, "--id-format", "both", subject], check=False,
        )
    except handoff_launcher.AdapterError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _cmux_snapshot(binary: str) -> tuple[str, str] | None:
    surfaces = _cmux_list(binary, "list-pane-surfaces")
    workspaces = _cmux_list(binary, "list-workspaces")
    if surfaces is None or workspaces is None:
        return None
    return surfaces, workspaces


def _cmux_close_surface(
    handle: str, *, binary: str,
) -> tuple[str, str | None, str | None]:
    """Close exactly one cmux surface; return ``(action, note, error)``.

    Python port of ``scripts/cmux_close_surface_safe.sh``: snapshot
    workspaces+surfaces before, close only the exact UUID, then verify every
    other pre-existing surface and workspace is still present.  A layout
    change beyond the target is reported loudly in ``error`` — the operator
    must STOP all further layout actions and report to the user.  When cmux
    is unreachable the session liveness is *unknown* — never presumed gone.
    """
    if not _CMUX_UUID_RE.fullmatch(handle):
        return "none", f"handle {handle!r} is not a cmux surface UUID; no session cleanup attempted", None
    target = handle.lower()
    before = _cmux_snapshot(binary)
    if before is None:
        return "unknown", "cmux is unreachable; session liveness is unknown", None
    surfaces_before, workspaces_before = before
    surface_uuids_before = _uuids(surfaces_before)
    workspace_uuids_before = _uuids(workspaces_before)
    if target not in surface_uuids_before:
        return "none", "cmux surface is already gone", None
    # UUIDs sharing a listing line with the target (its own pane/tab entries)
    # legitimately vanish with it; exclude them from the disappearance check.
    target_line_uuids = {
        uuid
        for line in surfaces_before.splitlines() if target in line.lower()
        for uuid in _uuids(line)
    }
    close = handoff_launcher._run(  # noqa: SLF001
        [binary, "close-surface", "--surface", handle], check=False,
    )
    if close.returncode != 0:
        detail = (close.stderr or close.stdout).strip()
        return "error", f"cmux close-surface failed: {detail}", None
    time.sleep(_CMUX_VERIFY_SETTLE_SECONDS)
    after = _cmux_snapshot(binary)
    if after is None:
        return "kill", None, (
            f"closed cmux surface {handle} but could not re-list to verify the layout; "
            "STOP all further layout actions immediately and report this to the user"
        )
    surfaces_after, workspaces_after = after
    surface_uuids_after = _uuids(surfaces_after)
    workspace_uuids_after = _uuids(workspaces_after)
    missing = [
        f"surface {uuid}"
        for uuid in sorted(
            surface_uuids_before - {target} - target_line_uuids - workspace_uuids_before
        )
        if uuid not in surface_uuids_after
    ]
    missing += [
        f"workspace {uuid}"
        for uuid in sorted(workspace_uuids_before)
        if uuid not in workspace_uuids_after
    ]
    if missing:
        return "kill", None, (
            f"layout changed beyond the target surface {handle}: "
            f"{', '.join(missing)} disappeared; STOP all further layout actions "
            "immediately and report this to the user"
        )
    return "kill", None, None


def _session_absent(record: dict[str, Any], *, cmux_binary: str) -> bool | None:
    """Positive-absence session probe: ``(answered, absent)`` as one boolean.

    Returns ``True`` only when the transport ANSWERED and reported the
    session absent, ``False`` when it answered and the session exists, and
    ``None`` when the transport is unreachable — liveness is then *unknown*,
    which must skip the run, never reap it.
    """
    transport = record["session_transport"]
    handle = record["handle"]
    if transport == "tmux":
        try:
            result = handoff_launcher._run(  # noqa: SLF001 - shared tmux discipline
                ["tmux", "has-session", "-t", handle], check=False,
            )
        except handoff_launcher.AdapterError:
            # The tmux invocation itself failed (e.g. binary not executable
            # under a launchd PATH) — that is not evidence of absence.
            return None
        return result.returncode != 0
    if transport == "cmux":
        snapshot = _cmux_snapshot(cmux_binary)
        if snapshot is None:
            return None
        surfaces, _ = snapshot
        return handle.lower() not in _uuids(surfaces)
    return None


def _local_fatal(record: dict[str, Any]) -> bool:
    """Derived fatal fact for a local run; False whenever it cannot be read."""
    run_dir = record.get("run_dir")
    if not isinstance(run_dir, str) or not Path(run_dir).is_dir():
        return False
    try:
        return handoff.is_fatal(Path(run_dir))
    except handoff.HandoffError:
        return False


def _local_session(
    record: dict[str, Any], *, force: bool, dry_run: bool,
    cmux_binary: str,
) -> tuple[str, str | None, str | None]:
    """Deal with one local run's leftover session; ``(action, note, error)``.

    ``action`` is ``"kill"`` when the session was (or, with ``dry_run``,
    would be) terminated, ``"none"`` when no live session exists, ``"error"``
    when a kill was attempted and failed, and ``"unknown"`` when the
    transport is unreachable — liveness then cannot be ruled in or out, so
    the caller must skip the run rather than reap it.  A non-terminal run's
    session is never killed without ``force`` — the caller skips those runs
    before this point — and terminality is re-verified at kill time.
    """
    transport = record["session_transport"]
    handle = record["handle"]
    if transport == "tmux":
        try:
            alive = handoff_launcher._run(  # noqa: SLF001
                ["tmux", "has-session", "-t", handle], check=False,
            ).returncode == 0
        except handoff_launcher.AdapterError as exc:
            return "unknown", f"tmux is unreachable ({exc}); session liveness is unknown", None
        if not alive:
            return "none", "tmux session is not running", None
        if dry_run:
            return "kill", None, None
        if not force:
            terminal_now, note = handoff_registry._assess_terminal(record)  # noqa: SLF001
            if not terminal_now:
                return "error", f"run went live during cleanup ({note}); session left running", None
        killed = handoff_launcher._run(  # noqa: SLF001
            ["tmux", "kill-session", "-t", handle], check=False,
        )
        if killed.returncode != 0:
            return "error", f"tmux kill-session failed: {(killed.stderr or killed.stdout).strip()}", None
        return "kill", None, None
    if transport == "cmux":
        if dry_run:
            snapshot = _cmux_snapshot(cmux_binary)
            if snapshot is None:
                return "unknown", "cmux is unreachable; session liveness is unknown", None
            surfaces, _ = snapshot
            if handle.lower() not in _uuids(surfaces):
                return "none", "cmux surface is already gone", None
            return "kill", None, None
        return _cmux_close_surface(handle, binary=cmux_binary)
    return "none", f"unknown session transport {transport!r}; no session cleanup attempted", None


def _remote_terminal_note(row: dict[str, Any]) -> str | None:
    if row.get("state") in handoff_registry.TERMINAL_STATES:
        return None
    if not row.get("run_dir_present", True):
        return "run directory is missing on the owning host; record is a dangling pointer"
    if row.get("finished"):
        return "orchestrator marked the run finished"
    if row.get("fatal"):
        return "worker reported a fatal error"
    if row.get("desired_state") == "stop":
        return "orchestrator concluded the run (legacy desired_state is stop)"
    return None


def _entry(
    record: dict[str, Any], *, note: str | None, session: str,
    session_note: str | None, run_dir_deleted: bool, run_dir_note: str | None,
    record_removed: bool,
) -> dict[str, Any]:
    return {
        "run_id": record["run_id"],
        "name": record.get("name"),
        "handle": record.get("handle"),
        "host": record.get("host"),
        "note": note,
        "session": session,
        "session_note": session_note,
        "run_dir_deleted": run_dir_deleted,
        "run_dir_note": run_dir_note,
        "record_removed": record_removed,
        "record": handoff_registry.public(record),
    }


def _skip(run_id: Any, name: Any, reason: str) -> dict[str, Any]:
    return {"run_id": run_id, "name": name, "reason": reason}


def clean(
    *,
    selector: str | None = None,
    older_than: float | None = None,
    host: str | None = None,
    orchestrator_id: str | None = None,
    dead: bool = False,
    fatal: bool = False,
    finished: bool = False,
    force: bool = False,
    delete_run_dir: bool = False,
    dry_run: bool = False,
    path: Path | None = None,
    cmux_binary: str = handoff_launcher.CMUX_DEFAULT,
) -> dict[str, Any]:
    """Clean up finished runs: sessions, registry records, optional run dirs.

    With ``selector`` exactly one run is cleaned.  Without it every record
    matching the ``older_than``/``host``/``orchestrator_id`` filters is a
    candidate and ``dry_run`` previews the plan without changing anything.
    ``dead``/``fatal``/``finished`` are composable fact filters: when any are
    given, only runs matching at least one — the transport answered
    session-absent, the worker declared a fatal error, the orchestrator
    marked the run finished — are cleaned.  Runs not known to be terminal and
    not positively session-absent are skipped and reported unless ``force``
    includes them; an unreachable transport yields *unknown* and always
    skips.  Invalid records are never bulk-cleaned.  The registry is read
    under a shared lock, sessions are killed and remote hosts probed WITHOUT
    the lock (SSH probes can take seconds), and the records of the cleaned
    runs are then dropped under one exclusive lock.
    """
    if selector is not None and not selector:
        raise handoff.HandoffError("run selector must be non-empty", 2)
    if older_than is not None and older_than < 0:
        raise handoff.HandoffError("--older-than must be non-negative", 2)
    registry_path = Path(path or handoff_registry.default_path())
    with handoff_registry._locked(registry_path, exclusive=False):  # noqa: SLF001
        registry, invalid = handoff_registry._read_lenient_unlocked(registry_path)  # noqa: SLF001

    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    candidates: list[tuple[str, dict[str, Any]]] = []
    invalid_single: tuple[str, Any, str] | None = None

    if selector is not None:
        key, record, error = handoff_registry._resolve_lenient(registry, invalid, selector)  # noqa: SLF001
        if error is not None and not force and handoff_registry._is_pre_rename_record(record):  # noqa: SLF001
            # Migration restores this record intact, so send the operator there
            # rather than letting the escape hatch destroy recoverable state.
            handoff._pre_orchestrator_rename("coordinator_id", registry_path)  # noqa: SLF001
        if error is not None:
            # A corrupt record gets the same conservative liveness check its
            # raw fields allow, and no session cleanup: its transport fields
            # cannot be trusted.
            run_dir = record.get("run_dir") if isinstance(record, dict) else None
            if isinstance(run_dir, str) and run_dir:
                host_field = record.get("host")
                terminal, note = handoff_registry._assess_terminal({  # noqa: SLF001
                    "host": host_field if isinstance(host_field, str) else None,
                    "run_dir": run_dir,
                })
            else:
                terminal, note = True, "the record has no usable run_dir"
            note = f"record is invalid ({error}); {note}"
            if terminal or force:
                if not terminal:
                    note = f"cleaned with --force despite: {note}"
                invalid_single = (key, record, note)
            else:
                skipped.append(_skip(
                    key, record.get("name") if isinstance(record, dict) else None,
                    f"{note}; use --force to clean it anyway",
                ))
        else:
            candidates.append((key, record))
    else:
        for key, error in invalid.items():
            raw = registry["runs"][key]
            if handoff_registry._is_pre_rename_record(raw):  # noqa: SLF001
                reason = (
                    "record predates the orchestrator rename; run "
                    "scripts/migrate_handoff_state_orchestrator.py to restore it "
                    "instead of removing it"
                )
            else:
                reason = (
                    f"record is invalid ({error}); "
                    "remove it deliberately with runs clean --run"
                )
            skipped.append(_skip(
                key, raw.get("name") if isinstance(raw, dict) else None, reason,
            ))
        cutoff = None
        if older_than is not None:
            cutoff = handoff._now() - datetime.timedelta(days=older_than)  # noqa: SLF001 - same-package timestamp parser
        valid = [record for key, record in registry["runs"].items() if key not in invalid]
        for record in sorted(valid, key=lambda item: item["registered_at"]):
            if host is not None and record["host"] != host:
                continue
            if orchestrator_id is not None and record["orchestrator_id"] != orchestrator_id:
                continue
            if cutoff is not None and handoff._parse_time(record["registered_at"]) >= cutoff:  # noqa: SLF001
                continue
            candidates.append((record["run_id"], record))

    fact_names = [
        name
        for name, enabled in (("dead", dead), ("fatal", fatal), ("finished", finished))
        if enabled
    ]

    # Remote runs are verified and reaped on their owning hosts, one SSH per
    # (host, remote_python) group.
    remote_rows: dict[str, dict[str, Any]] = {}
    local_candidates: list[tuple[str, dict[str, Any]]] = []
    remote_candidates: list[tuple[str, dict[str, Any]]] = []
    for key, record in candidates:
        (remote_candidates if record["host"] is not None else local_candidates).append((key, record))
    for (group_host, python), runs in _group_by_host(
        [record for _, record in remote_candidates]
    ).items():
        try:
            rows = _probe_host(
                group_host, python, runs,
                mode="probe" if dry_run else "kill",
                force=force, delete_run_dir=delete_run_dir, facts=fact_names,
            )
        except handoff_launcher.AdapterError as exc:
            errors.append({"host": group_host, "error": str(exc)})
            for record in runs:
                skipped.append(_skip(
                    record["run_id"], record["name"],
                    f"cleanup probe on host {group_host!r} failed: {exc}",
                ))
            continue
        for row in rows:
            row["host"] = group_host
            remote_rows[row["run_id"]] = row

    cleaned: list[dict[str, Any]] = []
    removed_keys: list[str] = []
    fact_note = f"does not match the requested cleanup facts (--{'/--'.join(fact_names)})"

    if invalid_single is not None:
        key, record, note = invalid_single
        cleaned.append({
            "run_id": record.get("run_id") if isinstance(record, dict) else key,
            "name": record.get("name") if isinstance(record, dict) else None,
            "handle": record.get("handle") if isinstance(record, dict) else None,
            "host": record.get("host") if isinstance(record, dict) else None,
            "note": note,
            "session": "none",
            "session_note": "record is invalid; no session cleanup attempted",
            "run_dir_deleted": False,
            "run_dir_note": None,
            "record_removed": not dry_run,
            "record": (
                handoff_registry.public(record) if isinstance(record, dict) else record
            ),
        })
        removed_keys.append(key)

    for key, record in remote_candidates:
        row = remote_rows.get(record["run_id"])
        if row is None:
            continue  # probe failed; already reported as skipped
        absent = row.get("session_alive") is False
        row_facts = {
            "finished": bool(row.get("finished")),
            "fatal": bool(row.get("fatal")),
            "dead": absent,
        }
        if fact_names and not any(row_facts[name] for name in fact_names):
            skipped.append(_skip(record["run_id"], record["name"], fact_note))
            continue
        if not row.get("terminal") and not absent and not force:
            skipped.append(_skip(
                record["run_id"], record["name"],
                f"worker state {row.get('state')!r} on host {row['host']!r} is not terminal",
            ))
            continue
        note = _remote_terminal_note(row)
        if not row.get("terminal"):
            if absent:
                note = "transport on the owning host reports the session is absent"
            else:
                note = (
                    f"not known to be terminal (worker state {row.get('state')!r} "
                    f"on host {row['host']!r}); included by --force"
                )
        if row.get("killed"):
            session, session_note = "kill", None
        elif row.get("session_alive"):
            session = "error" if not dry_run else "kill"
            session_note = row.get("kill_error")
        else:
            session, session_note = "none", "no session on the owning host"
        cleaned.append(_entry(
            record, note=note, session=session, session_note=session_note,
            run_dir_deleted=bool(row.get("run_dir_deleted")),
            run_dir_note=row.get("delete_error"),
            record_removed=not dry_run,
        ))
        removed_keys.append(key)

    for key, record in local_candidates:
        # The gate is terminality OR a positively-absent session: a run whose
        # transport answered "session gone" is reaped even without a terminal
        # fact, while an unreachable transport yields unknown and skips.
        terminal, note = handoff_registry._assess_terminal(record)  # noqa: SLF001
        absent = _session_absent(record, cmux_binary=cmux_binary)
        local_facts = {
            "finished": bool(record.get("finished_at")),
            "fatal": _local_fatal(record),
            "dead": absent is True,
        }
        if fact_names and not any(local_facts[name] for name in fact_names):
            skipped.append(_skip(record["run_id"], record["name"], fact_note))
            continue
        if not terminal and absent is not True and not force:
            if absent is None:
                skipped.append(_skip(
                    record["run_id"], record["name"],
                    "session transport is unreachable; liveness is unknown",
                ))
            else:
                skipped.append(_skip(record["run_id"], record["name"], note or "not terminal"))
            continue
        if not terminal:
            if absent is True:
                note = "session transport reports the session is absent"
            else:
                note = f"not known to be terminal ({note}); included by --force"
        session, session_note, layout_error = _local_session(
            record, force=force, dry_run=dry_run,
            cmux_binary=cmux_binary,
        )
        if session in {"error", "unknown"}:
            skipped.append(_skip(
                record["run_id"], record["name"],
                session_note or "session cleanup failed",
            ))
            continue
        if layout_error is not None:
            errors.append({"run_id": record["run_id"], "error": layout_error})
        run_dir_deleted, run_dir_note = False, None
        if delete_run_dir:
            try:
                run_dir_deleted, run_dir_note = handoff_registry._delete_run_dir(  # noqa: SLF001
                    record, dry_run=dry_run,
                )
            except handoff.HandoffError as exc:
                if dry_run:
                    run_dir_note = str(exc)
                else:
                    # A refused deletion keeps the run fully registered and
                    # retryable.
                    skipped.append(_skip(record["run_id"], record["name"], str(exc)))
                    continue
        cleaned.append(_entry(
            record, note=note, session=session, session_note=session_note,
            run_dir_deleted=run_dir_deleted, run_dir_note=run_dir_note,
            record_removed=not dry_run,
        ))
        removed_keys.append(key)

    if removed_keys and not dry_run:
        with handoff_registry._locked(registry_path, exclusive=True):  # noqa: SLF001
            current, _ = handoff_registry._read_lenient_unlocked(registry_path)  # noqa: SLF001
            for key in removed_keys:
                current["runs"].pop(key, None)
            handoff_registry._write_unlocked(registry_path, current)  # noqa: SLF001

    if selector is not None:
        scope: dict[str, Any] = {"run": selector}
    else:
        scope = {"older_than": older_than, "host": host, "orchestrator": orchestrator_id}
        if fact_names:
            scope["facts"] = fact_names
    report = {
        "dry_run": dry_run,
        "scope": scope,
        "cleaned": cleaned,
        "skipped": skipped,
        "errors": errors,
    }
    if errors:
        detail = "; ".join(item["error"] for item in errors)
        raise handoff.HandoffError(
            f"{len(errors)} error(s) during cleanup: {detail}", 1, report=report,
        )
    return report
