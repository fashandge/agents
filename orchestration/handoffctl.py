"""Command-line adapter for :mod:`agents.orchestration.handoff`."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from agents import coding_agents_cli
from agents.orchestration import handoff
from agents.orchestration import handoff_launcher
from agents.orchestration import handoff_registry
from agents.orchestration import handoff_watcher


RESULT_NOTIFICATION_TITLE = "Handoff result ready"
RESULT_NOTIFICATION_BODY = "Awaiting coordinator review"
RESULT_NOTIFICATION_TIMEOUT_SECONDS = 5
COORDINATOR_NOTIFICATION_TITLE = handoff_launcher.ORCHESTRATOR_DOORBELL_TITLE
COORDINATOR_NOTIFICATION_BODY = "Worker updates are ready for coordinator review."
SURFACE_WATCHER_READY_TIMEOUT_SECONDS = 20.0
WATCHER_MODES = ("auto", "detached", "surface")
WATCHER_WORKSPACE_TITLE = "handoff-watchers"
_UUID_RE = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _read_bytes(path: str | None, *, default: bytes = b"") -> bytes:
    if path is None:
        return default
    if path == "-":
        return sys.stdin.buffer.read()
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise handoff.HandoffError(f"cannot read {path}: {exc}", 6) from exc


def _read_text(path: str | None, *, default: str = "") -> str:
    raw = _read_bytes(path, default=default.encode())
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise handoff.HandoffError(f"input {path} is not UTF-8", 2) from exc


def _json_input(path: str | None, default: Any) -> Any:
    if path is None:
        return default
    raw = _read_bytes(path)
    # Reuse the strict duplicate-key parser. Input validation errors are usage
    # failures rather than corruption failures.
    try:
        return handoff._decode_json(raw, path)  # noqa: SLF001 - shared CLI parser
    except handoff.HandoffError as exc:
        raise handoff.HandoffError(str(exc), 2) from exc


def _token(args: argparse.Namespace, role: str) -> bytes:
    path = getattr(args, "token_file", None)
    if path is None:
        variable = f"HANDOFF_{role.upper()}_TOKEN_FILE"
        path = os.environ.get(variable)
        if path is None:
            raise handoff.HandoffError(f"--token-file or {variable} is required", 3)
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise handoff.HandoffError(f"cannot read credential file {path}: {exc}", 3) from exc


def _mutation_inputs(args: argparse.Namespace) -> dict[str, Any]:
    stdin_count = sum(value == "-" for value in (args.body_file, args.data_file, args.attachments_file))
    if stdin_count > 1:
        raise handoff.HandoffError("at most one input file may be '-'", 2)
    data = _json_input(args.data_file, {})
    attachments = _json_input(args.attachments_file, [])
    if not isinstance(attachments, list) or not all(isinstance(value, str) for value in attachments):
        raise handoff.HandoffError("attachments-file must contain a JSON array of strings", 2)
    return {
        "type": args.type, "message_id": args.message_id, "reply_to": args.reply_to,
        "body": _read_text(args.body_file), "data": data, "attachments": attachments,
        "snapshot_attachments": args.snapshot_attachments,
    }


def _notify_result_ready(run_dir: Path) -> None:
    """Best-effort cmux alert after the durable result is authoritative."""
    try:
        run = handoff._load_json(  # noqa: SLF001 - same-package immutable run reader
            Path(run_dir) / "run.json",
            handoff._validate_run,  # noqa: SLF001 - reject a malformed transport record
        )
        surface = os.environ.get("CMUX_SURFACE_ID")
        if run["worker"]["transport"] != "cmux" or not surface:
            return
        subprocess.run(
            [
                "cmux", "notify", "--surface", surface,
                "--title", RESULT_NOTIFICATION_TITLE,
                "--body", RESULT_NOTIFICATION_BODY,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=RESULT_NOTIFICATION_TIMEOUT_SECONDS,
        )
    except (handoff.HandoffError, OSError, subprocess.SubprocessError):
        # The outbox event and awaiting_review projection were already made
        # durable. Notification delivery is only an operator convenience.
        pass


def _remote_json(
    record: dict[str, Any], argv: list[str], *, stdin: str | None = None,
) -> dict[str, Any] | list[Any]:
    try:
        completed = handoff_launcher._run_ssh(  # noqa: SLF001 - shared safe SSH argv adapter
            record["host"],
            [record["remote_python"], "-m", "agents.orchestration.handoffctl", *argv],
            stdin=stdin,
            timeout=30,
        )
        return json.loads(completed.stdout)
    except (handoff_launcher.AdapterError, json.JSONDecodeError) as exc:
        raise handoff.HandoffError(f"remote handoff command failed: {exc}", 6) from exc


def _context_local(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    status = handoff.status(run_dir)
    control = handoff.control_show(run_dir)
    try:
        kickoff = (run_dir / "kickoff.md").read_text(encoding="utf-8")
        progress = (run_dir / "progress.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise handoff.HandoffError(f"cannot read handoff context: {exc}", 6) from exc
    return {
        "run": handoff._load_json(run_dir / "run.json", handoff._validate_run),  # noqa: SLF001
        "status": status,
        "control": control,
        "kickoff": kickoff,
        "progress": progress,
        "unread_inbox": handoff.read(run_dir, "inbox", after=status["inbox_cursor"]),
        "unread_outbox": handoff.read(run_dir, "outbox", after=control["outbox_cursor"]),
    }


def _ring_doorbell(record: dict[str, Any], run_id: str, inbox_seq: int) -> tuple[bool, str | None]:
    try:
        if record["session_transport"] == "tmux":
            adapter: Any = handoff_launcher.TmuxAdapter()
        elif record["session_transport"] == "cmux":
            adapter = handoff_launcher.CmuxAdapter(handoff_launcher.CMUX_DEFAULT)
        else:
            raise handoff.HandoffError(
                f"unsupported session transport: {record['session_transport']}", 4,
            )
        adapter.doorbell(record["handle"], run_id, inbox_seq)
        return True, None
    except (handoff.HandoffError, handoff_launcher.AdapterError) as exc:
        return False, str(exc)


def _send_with_doorbell(args: argparse.Namespace) -> dict[str, Any]:
    result = handoff.send(args.run_dir, _token(args, "coordinator"), **_mutation_inputs(args))
    doorbell_sent = False
    doorbell_error: str | None = None
    if not args.no_doorbell:
        try:
            record = handoff_registry.resolve(str(args.run_dir), private=True)
        except handoff.HandoffError:
            record = None
        if record is None:
            doorbell_error = "run is not registered; doorbell skipped"
        elif record["host"] is not None:
            doorbell_error = "run is remote; doorbell skipped"
        else:
            doorbell_sent, doorbell_error = _ring_doorbell(
                record, record["run_id"], result["message"]["seq"],
            )
    return {**result, "doorbell_sent": doorbell_sent, "doorbell_error": doorbell_error}


def _dispatch_local(record: dict[str, Any], body: str) -> dict[str, Any]:
    if record["host"] is not None or record["credential_dir"] is None:
        raise handoff.HandoffError("dispatch must execute on the run-owning host", 4)
    run_dir = Path(record["run_dir"])
    credential_dir = Path(record["credential_dir"])
    recovery_file = credential_dir / "recovery.token"
    try:
        recovery = recovery_file.read_bytes()
    except OSError as exc:
        raise handoff.HandoffError("registered recovery credential is unavailable", 3) from exc

    control = handoff.control_show(run_dir)
    if handoff._parse_time(control["coordinator_lease_expires_at"]) > handoff._now():  # noqa: SLF001
        raise handoff.HandoffError(
            "coordinator lease is active; use the current managed coordinator", 4,
        )
    token_file = credential_dir / f"coordinator-dispatch-{uuid.uuid4()}.token"
    takeover = handoff.control_takeover(
        run_dir, recovery, new_token_file=token_file,
        reason="One-shot registry dispatch after released coordinator lease",
    )
    token = token_file.read_bytes()
    released = False
    completed = False
    sent: dict[str, Any] | None = None
    superseded_result: str | None = None
    answered_question: str | None = None
    doorbell_sent = False
    doorbell_error: str | None = None
    try:
        status = handoff.status(run_dir)
        if status["state"] in {"succeeded", "failed", "stopped"}:
            raise handoff.HandoffError(
                f"worker state {status['state']} cannot accept a new instruction", 4,
            )
        if status["state"] == "awaiting_review":
            outbox = handoff.read(run_dir, "outbox")
            handoff.control_consume(run_dir, token, through=len(outbox))
            current = handoff.control_show(run_dir)
            superseded_result = current["review_result_id"]
            if superseded_result is None:
                raise handoff.HandoffError("awaiting_review run has no current result", 5)
            sent = handoff.send(
                run_dir, token, type="supersede", body=body,
                reply_to=superseded_result,
            )
        elif status["state"] == "blocked" and status["blocked_on"]:
            # A steer can never clear a blocking question: the worker cannot
            # advance its inbox cursor across an unanswered blocking question,
            # so the only deliverable dispatch here is the linked answer.
            answered_question = status["blocked_on"][-1]
            sent = handoff.send(
                run_dir, token, type="answer", body=body,
                reply_to=answered_question,
            )
        else:
            sent = handoff.send(run_dir, token, type="steer", body=body)
        doorbell_sent, doorbell_error = _ring_doorbell(
            record, record["run_id"], sent["message"]["seq"],
        )
        handoff.control_release(run_dir, token)
        released = True
        completed = True
    finally:
        if not completed and not released:
            try:
                handoff.control_release(run_dir, token)
                released = True
            except handoff.HandoffError:
                pass
        if released:
            try:
                token_file.unlink()
            except OSError:
                pass
    if sent is None:
        raise AssertionError("dispatch completed without a message")
    return {
        "run": handoff_registry.public(record),
        "takeover": takeover["message"],
        "message": sent["message"],
        "superseded_result": superseded_result,
        "answered_question": answered_question,
        "doorbell_sent": doorbell_sent,
        "doorbell_error": doorbell_error,
        "coordinator_released": released,
    }


def _dispatch_registered(
    selector: str, body: str,
) -> dict[str, Any]:
    record = handoff_registry.resolve(selector, private=True)
    if record["host"] is None:
        return _dispatch_local(record, body)
    argv = ["dispatch", "--run", record["run_id"], "--body-file", "-"]
    result = _remote_json(record, argv, stdin=body)
    if not isinstance(result, dict):
        raise handoff.HandoffError("remote dispatch returned invalid data", 6)
    return result


def _context_registered(selector: str) -> dict[str, Any]:
    record = handoff_registry.resolve(selector, private=True)
    if record["host"] is None:
        context = _context_local(Path(record["run_dir"]))
    else:
        context = _remote_json(record, ["context", "--run", record["run_id"]])
        if not isinstance(context, dict):
            raise handoff.HandoffError("remote context returned invalid data", 6)
    context["registry"] = handoff_registry.public(record)
    return context


def _read_registered_outbox(record: dict[str, Any], after: int) -> list[dict[str, Any]]:
    if record["host"] is None:
        return handoff.read(Path(record["run_dir"]), "outbox", after=after)
    result = _remote_json(
        record,
        ["read", "--run-dir", record["run_dir"], "--journal", "outbox", "--after", str(after)],
    )
    if not isinstance(result, list):
        raise handoff.HandoffError("remote outbox read returned invalid data", 6)
    return result


def _notify_watched_event(record: dict[str, Any], event: dict[str, Any]) -> None:
    surface = os.environ.get("CMUX_SURFACE_ID")
    if not surface:
        return
    try:
        subprocess.run(
            [
                "cmux", "notify", "--surface", surface,
                "--title", f"Handoff {event['type']}: {record['name']}",
                "--body", f"Outbox sequence {event['seq']} is ready",
            ],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=RESULT_NOTIFICATION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _send_native_app_doorbell(thread_id: str, body: str) -> None:
    """Provider hook for native-app messaging adapters.

    The filesystem snapshot remains pending when no installed native adapter can
    deliver to the registered thread ID.
    """
    raise handoff.HandoffError(
        f"no native-app doorbell adapter is installed for thread {thread_id}", 6,
    )


def _notify_macos(title: str, body: str) -> None:
    """Show a credential-free fallback alert when cmux notifications fail."""
    if sys.platform != "darwin":
        raise handoff_launcher.AdapterError(
            "cmux notification fallback is unavailable outside macOS",
        )
    script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise handoff_launcher.AdapterError(
            f"macOS notification fallback failed: {exc}",
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise handoff_launcher.AdapterError(
            f"macOS notification fallback failed ({result.returncode}): {detail}",
        )


def _notify_coordinator(value: dict[str, Any], coverage: dict[str, int]) -> str:
    """Deliver one coalesced, opaque coordinator doorbell.

    Returns a ``+``-joined label of every channel that accepted the attempt.
    Acceptance means the transport acknowledged the request (for cmux/tmux, a
    zero subprocess exit; for ``cmux_input``, additionally a visible echo of
    the typed text); no channel reports back that the user actually saw the
    alert, and the watcher records it with exactly that meaning.

    A cmux doorbell attempts two complementary channels: typed input into the
    coordinator surface, which is the only channel that pushes a hosted agent
    to read its pending outboxes, and the native visible alert, which reaches
    the human even when the surface does not echo input.  The attempt fails
    (and stays pending) only when every channel fails.
    """
    if handoff_watcher.owner_process_status(value["owner_process"]) != "alive":
        raise handoff.HandoffError(
            "orchestrator process ownership cannot be verified; doorbell suppressed", 6,
        )
    del coverage  # The canonical snapshot-pointer doorbell is intentionally opaque.
    coordinator_id = value["coordinator_id"]
    target = value["target"]
    if value["transport"] == "tmux":
        handoff_launcher.TmuxAdapter().orchestrator_doorbell(
            target["handle"], coordinator_id,
        )
        return "terminal_input"
    if value["transport"] == "cmux":
        adapter = handoff_launcher.CmuxAdapter(handoff_launcher.CMUX_DEFAULT)
        adapter.workspace = target["workspace"]
        accepted: list[str] = []
        failures: list[str] = []
        try:
            if adapter.orchestrator_doorbell_input(target["surface"], coordinator_id):
                accepted.append("cmux_input")
            else:
                failures.append(
                    "cmux input was accepted but never echoed on the surface",
                )
        except handoff_launcher.AdapterError as input_error:
            failures.append(f"cmux input failed ({input_error})")
        try:
            adapter.orchestrator_doorbell(target["surface"], coordinator_id)
            accepted.append("cmux_notify")
        except handoff_launcher.AdapterError as doorbell_error:
            failures.append(f"cmux surface notification failed ({doorbell_error})")
            try:
                adapter.notify(
                    None,
                    title=COORDINATOR_NOTIFICATION_TITLE,
                    body=COORDINATOR_NOTIFICATION_BODY,
                )
                accepted.append("cmux_notify_workspace")
            except handoff_launcher.AdapterError as notification_error:
                failures.append(
                    f"cmux workspace notification failed ({notification_error})",
                )
        if not accepted:
            try:
                _notify_macos(
                    COORDINATOR_NOTIFICATION_TITLE,
                    COORDINATOR_NOTIFICATION_BODY,
                )
                accepted.append("macos_notification")
            except handoff_launcher.AdapterError as macos_error:
                failures.append(f"macOS fallback failed ({macos_error})")
                raise handoff_launcher.AdapterError("; ".join(failures)) from macos_error
        return "+".join(accepted)
    if value["transport"] == "native_app":
        _send_native_app_doorbell(
            target["thread_id"],
            handoff_launcher.orchestrator_doorbell_body(coordinator_id),
        )
        return "native_app"
    raise handoff.HandoffError(f"unsupported coordinator transport: {value['transport']}", 4)


def _cmux_workspace_for_surface(binary: str, surface: str) -> str:
    """Resolve the workspace owning an exact cmux surface.

    cmux's ``current-workspace`` describes the globally selected workspace, not
    necessarily the terminal that launched this process.  Its inherited surface
    and workspace environment variables form an exact pair, so retain that pair
    when available.  An explicitly supplied surface falls back to a read-only
    scan of the workspace inventory.
    """
    inherited_surface = os.environ.get("CMUX_SURFACE_ID")
    inherited_workspace = os.environ.get("CMUX_WORKSPACE_ID")
    if (
        inherited_workspace
        and inherited_surface
        and inherited_surface.lower() == surface.lower()
    ):
        return inherited_workspace

    listed = handoff_launcher._run([  # noqa: SLF001 - exact cmux inventory lookup
        binary, "--id-format", "both", "list-workspaces",
    ]).stdout
    workspaces = [
        token
        for line in listed.splitlines()
        for token in line.split()
        if token.startswith("workspace:")
    ]
    for workspace in workspaces:
        surfaces = handoff_launcher._run(  # noqa: SLF001 - exact cmux inventory lookup
            [binary, "--id-format", "both", "list-pane-surfaces", "--workspace", workspace],
            check=False,
        )
        if surfaces.returncode == 0 and surface.lower() in surfaces.stdout.lower():
            return workspace
    raise handoff.HandoffError(
        f"cmux could not find coordinator surface {surface} in any workspace", 6,
    )


def _register_coordinator(args: argparse.Namespace) -> dict[str, Any]:
    if args.transport == "cmux":
        surface = args.surface or os.environ.get("CMUX_SURFACE_ID")
        if not surface:
            raise handoff.HandoffError(
                "cmux coordinator registration requires --surface or CMUX_SURFACE_ID", 2,
            )
        surface = surface.lower()
        workspace = _cmux_workspace_for_surface(args.cmux_binary, surface)
        target = {"workspace": workspace, "surface": surface}
    elif args.transport == "tmux":
        if not args.target:
            raise handoff.HandoffError("tmux coordinator registration requires --target", 2)
        target = {"handle": args.target}
    else:
        if not args.target:
            raise handoff.HandoffError("native-app coordinator registration requires --target", 2)
        target = {"thread_id": args.target}
    transport = "native_app" if args.transport == "native-app" else args.transport
    return handoff_watcher.initialize(
        args.state, transport=transport, target=target,
        owner_pid=args.owner_pid,
        coordinator_id=args.coordinator_id,
    )


def _retarget_coordinator(args: argparse.Namespace) -> dict[str, Any]:
    value = handoff_watcher.read(args.state)
    if value["transport"] != "cmux":
        raise handoff.HandoffError(
            "coordinator retarget currently supports cmux coordinators only", 2,
        )
    surface = args.surface or os.environ.get("CMUX_SURFACE_ID")
    if not surface:
        raise handoff.HandoffError(
            "cmux coordinator retarget requires --surface or CMUX_SURFACE_ID", 2,
        )
    surface = surface.lower()
    return handoff_watcher.retarget(
        args.state,
        {
            "workspace": _cmux_workspace_for_surface(args.cmux_binary, surface),
            "surface": surface,
        },
    )


def _resolve_watcher_mode(mode: str, transport: str) -> str:
    """Pick the hosting mode a coordinator's doorbells actually need.

    The cmux server rejects socket clients outside its own process tree under
    the default ``socketControlMode: "cmuxOnly"``, so a detached (launchd-
    orphaned) watcher can never raise a native cmux alert and every doorbell
    degrades to a transient macOS banner.  cmux coordinators therefore default
    to a watcher hosted inside a dedicated cmux terminal surface; tmux and
    native-app coordinators keep the detached daemon, which their channels
    accept.  An explicit ``detached`` request for a cmux coordinator remains
    allowed for callers that accept the degraded fallback.
    """
    if mode == "auto":
        return "surface" if transport == "cmux" else "detached"
    if mode == "surface" and transport != "cmux":
        raise handoff.HandoffError(
            "a surface-hosted watcher requires a cmux coordinator", 2,
        )
    return mode


def _cmux_workspace_rows(binary: str) -> list[dict[str, str]]:
    """Parse the workspace inventory into UUID/title rows."""
    listed = handoff_launcher._run([  # noqa: SLF001 - read-only cmux inventory
        binary, "--id-format", "both", "list-workspaces",
    ]).stdout
    rows: list[dict[str, str]] = []
    for line in listed.splitlines():
        match = re.search(rf"workspace:\d+\s+({_UUID_RE})\s+(.*)$", line)
        if not match:
            continue
        title = match.group(2).replace("[selected]", "").strip()
        rows.append({"uuid": match.group(1).lower(), "title": title})
    return rows


def _workspace_title(binary: str, workspace: str) -> str | None:
    for row in _cmux_workspace_rows(binary):
        if row["uuid"] == workspace.lower():
            return row["title"]
    return None


def _watcher_workspace(binary: str) -> str:
    """Find or create the parked workspace that hosts watcher tabs.

    Watcher tabs are utility furniture, not working context, so they live
    together in one dedicated workspace kept at the bottom of the sidebar
    instead of cluttering the coordinator's own workspace.  Closing that
    workspace kills its watchers; ``coordinator start`` relaunches them.
    """
    rows = _cmux_workspace_rows(binary)
    for row in rows:
        if row["title"] == WATCHER_WORKSPACE_TITLE:
            return row["uuid"]
    # ``new-workspace`` prints only a short ref, not a UUID, so resolve the
    # created workspace by title from a fresh inventory listing.
    handoff_launcher._run([  # noqa: SLF001 - shared safe cmux argv adapter
        binary, "--id-format", "both", "new-workspace",
        "--name", WATCHER_WORKSPACE_TITLE, "--focus", "false",
    ])
    workspace = next(
        (
            row["uuid"] for row in _cmux_workspace_rows(binary)
            if row["title"] == WATCHER_WORKSPACE_TITLE
        ),
        None,
    )
    if workspace is None:
        raise handoff.HandoffError(
            "cmux did not report the new watcher workspace in its inventory", 6,
        )
    if rows:
        # Cosmetic placement only; the watcher works wherever the tab lands.
        handoff_launcher._run([  # noqa: SLF001
            binary, "reorder-workspace", "--workspace", workspace,
            "--after", rows[-1]["uuid"],
        ], check=False)
    return workspace


def _watcher_tab_name(
    binary: str, hosting_workspace: str, coordinator_title: str | None,
    coordinator_id: str,
) -> str:
    """Name a watcher tab after what it watches, disambiguating collisions.

    Tab names are cosmetic — every programmatic path addresses surfaces by
    UUID — but two coordinators in the same workspace would otherwise produce
    identical tabs, so a short coordinator-ID suffix is added only when the
    plain name is already taken in the parked workspace.
    """
    if not coordinator_title:
        return f"watcher: coordinator {coordinator_id[:8]}"
    base = f"watcher: {coordinator_title}"
    listed = handoff_launcher._run([  # noqa: SLF001 - read-only cmux inventory
        binary, "--id-format", "both", "list-pane-surfaces",
        "--workspace", hosting_workspace,
    ], check=False)
    if listed.returncode == 0 and base in listed.stdout:
        return f"{base} ({coordinator_id[:8]})"
    return base


def _surface_watcher_command(path: Path, interval: float) -> str:
    return "exec " + shlex.join([
        sys.executable, "-m", "agents.orchestration.handoffctl",
        "watch", "--coordinator-state", str(path),
        "--interval", str(interval),
    ])


def _start_surface_watcher(
    path: Path, value: dict[str, Any], interval: float, cmux_binary: str,
) -> dict[str, Any]:
    """Host the watcher in a new in-tree cmux terminal surface.

    The surface's foreground process is a direct member of the cmux process
    tree, so its ``cmux notify`` doorbells are accepted without relaxing
    ``socketControlMode``.  The tab doubles as the watcher's visible log, and
    its lifetime is surface-bound: closing the tab kills the watcher and a
    later ``coordinator start`` relaunches it.  Tabs are named after the
    coordinator workspace they watch and parked together in the dedicated
    bottom ``handoff-watchers`` workspace rather than the working workspace.
    """
    coordinator_title = _workspace_title(cmux_binary, value["target"]["workspace"])
    hosting_workspace = _watcher_workspace(cmux_binary)
    adapter = handoff_launcher.CmuxAdapter(cmux_binary)
    handle = adapter.launch(
        _watcher_tab_name(
            cmux_binary, hosting_workspace, coordinator_title,
            value["coordinator_id"],
        ),
        path.parent,
        _surface_watcher_command(path, interval),
        hosting_workspace,
    )
    surface_record = path.parent / "watcher-surface"
    surface_record.write_text(handle + "\n", encoding="utf-8")
    os.chmod(surface_record, 0o600)
    deadline = time.monotonic() + SURFACE_WATCHER_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if handoff_watcher.is_running(path):
            return {
                "coordinator_id": value["coordinator_id"],
                "mode": "surface",
                "pid": None,
                "surface": handle,
                "workspace": hosting_workspace,
                "coordinator_workspace": value["target"]["workspace"],
                "running": True,
                "started": True,
                "state": str(path),
            }
        time.sleep(0.05)
    raise handoff.HandoffError(
        "surface-hosted coordinator watcher did not become ready; "
        f"inspect with: {adapter.rescue_command(handle)}", 6,
    )


def _start_coordinator_watcher(
    path: Path, interval: float, *,
    mode: str = "auto", cmux_binary: str = handoff_launcher.CMUX_DEFAULT,
) -> dict[str, Any]:
    """Start or reuse one singleton watcher for a coordinator state."""
    value = handoff_watcher.read(path)
    if handoff_watcher.owner_process_status(value["owner_process"]) != "alive":
        raise handoff.HandoffError("coordinator owner process is not alive", 4)
    mode = _resolve_watcher_mode(mode, value["transport"])
    output_base = path.parent / "watcher-process"
    surface_record = path.parent / "watcher-surface"
    with handoff_watcher.start_lock(path) as start_lock_descriptor:
        if handoff_watcher.is_running(path):
            pid_path = Path(f"{output_base}.pid")
            try:
                pid = int(pid_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pid = None
            try:
                surface = surface_record.read_text(encoding="utf-8").strip() or None
            except OSError:
                surface = None
            return {
                "coordinator_id": value["coordinator_id"],
                "mode": "surface" if surface else "detached",
                "pid": pid,
                "surface": surface,
                "running": True,
                "started": False,
                "state": str(path),
            }

        for suffix in ("out", "err", "pid", "exitcode"):
            Path(f"{output_base}.{suffix}").unlink(missing_ok=True)
        surface_record.unlink(missing_ok=True)

        if mode == "surface":
            return _start_surface_watcher(path, value, interval, cmux_binary)

        def runner() -> coding_agents_cli.AgentResult:
            # ``run_detached`` forks without exec. Close the daemon's inherited
            # copy so flock semantics on Linux cannot keep the short-lived
            # check-and-start lock held for the watcher's entire lifetime.
            try:
                os.close(start_lock_descriptor)
            except OSError:
                pass
            handoff_watcher.watch(
                path,
                interval=interval,
                notifier=_notify_coordinator,
            )
            return coding_agents_cli.AgentResult(
                output="",
                returncode=0,
                stderr="",
                agent=coding_agents_cli.AgentType.CODEX,
            )

        process = coding_agents_cli.run_detached(runner, output_base)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if handoff_watcher.is_running(path):
                return {
                    "coordinator_id": value["coordinator_id"],
                    "mode": "detached",
                    "pid": process["pid"],
                    "surface": None,
                    "running": True,
                    "started": True,
                    "state": str(path),
                }
            exitcode_path = Path(process["exitcode"])
            if exitcode_path.exists():
                detail = Path(process["err"]).read_text(encoding="utf-8").strip()
                raise handoff.HandoffError(
                    f"detached coordinator watcher exited during startup: {detail}", 6,
                )
            time.sleep(0.05)
    raise handoff.HandoffError("detached coordinator watcher did not become ready", 6)


def _coordinator_pending(path: Path, *, full_context: bool) -> dict[str, Any]:
    value = handoff_watcher.read(path)
    records = {
        record["run_id"]: record
        for record in handoff_registry.list_records(private=True)
        if record["coordinator_id"] == value["coordinator_id"]
    }
    pending: list[dict[str, Any]] = []
    for run_id, notification in value["runs"].items():
        if not notification["doorbell_pending"] or run_id not in records:
            continue
        record = records[run_id]
        if full_context:
            detail = _context_registered(run_id)
        elif record["host"] is None:
            run_dir = Path(record["run_dir"])
            control = handoff.control_show(run_dir)
            status = handoff.status(run_dir)
            through = min(notification["observed_through"], status["outbox_seq"])
            detail = {
                "status": status,
                "control": control,
                "unread_outbox": handoff.read(
                    run_dir, "outbox", after=control["outbox_cursor"], through=through,
                ),
            }
        else:
            control = _remote_json(
                record, ["control", "show", "--run-dir", record["run_dir"]],
            )
            status = _remote_json(record, ["status", "--run-dir", record["run_dir"]])
            if not isinstance(control, dict) or not isinstance(status, dict):
                raise handoff.HandoffError("remote hot-path state is invalid", 6)
            through = min(notification["observed_through"], status["outbox_seq"])
            detail = {
                "status": status,
                "control": control,
                "unread_outbox": _remote_json(record, [
                    "read", "--run-dir", record["run_dir"], "--journal", "outbox",
                    "--after", str(control["outbox_cursor"]), "--through", str(through),
                ]),
            }
        pending.append({
            "run": handoff_registry.public(record),
            "notification": dict(notification),
            "detail": detail,
        })
    return {
        "coordinator_id": value["coordinator_id"],
        "mode": "recovery" if full_context else "hot",
        "pending": pending,
    }


def _print_coordinator_poll(result: dict[str, Any]) -> None:
    """Log doorbell activity so a surface-hosted watcher's tab shows history.

    Quiet polls stay silent; only polls that attempted a doorbell or hit an
    error are worth a line.  The poll summary carries run IDs and cursors
    only — never event bodies or credentials.
    """
    if result["attempted"] or result["errors"]:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)


def _watch(args: argparse.Namespace) -> int:
    if getattr(args, "coordinator_state", None) is not None:
        handoff_watcher.watch(
            args.coordinator_state,
            interval=args.interval,
            notifier=_notify_coordinator,
            on_poll=_print_coordinator_poll,
        )
        return 0
    records = (
        [handoff_registry.resolve(selector, private=True) for selector in args.run]
        if args.run else handoff_registry.list_records(private=True)
    )
    started = time.monotonic()
    while True:
        for record in records:
            try:
                events = _read_registered_outbox(record, record["observed_outbox_cursor"])
                for event in events:
                    payload = {"run": handoff_registry.public(record), "event": event}
                    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)
                    if args.notify_cmux:
                        _notify_watched_event(record, event)
                    updated = handoff_registry.update_observed(record["run_id"], event["seq"])
                    record["observed_outbox_cursor"] = updated["observed_outbox_cursor"]
            except handoff.HandoffError as exc:
                print(json.dumps({
                    "run": handoff_registry.public(record),
                    "watch_error": str(exc),
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)
        if args.once:
            return 0
        if args.timeout is not None and time.monotonic() - started >= args.timeout:
            return 0
        time.sleep(args.interval)


def _add_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", required=True, type=_absolute)


def _add_token(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--token-file", type=Path)


def _add_message(parser: argparse.ArgumentParser) -> None:
    _add_run(parser); _add_token(parser)
    parser.add_argument("--type", required=True)
    parser.add_argument("--message-id")
    parser.add_argument("--reply-to")
    parser.add_argument("--body-file")
    parser.add_argument("--data-file")
    parser.add_argument("--attachments-file")
    parser.add_argument("--snapshot-attachments", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoffctl")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--workspace", required=True, type=Path)
    init.add_argument("--kickoff", required=True, type=Path)
    init.add_argument("--harness", required=True); init.add_argument("--model", required=True)
    init.add_argument("--transport", required=True); init.add_argument("--effort")
    init.add_argument("--input", action="append", default=[])
    destination = init.add_mutually_exclusive_group()
    destination.add_argument("--state-root", type=Path); destination.add_argument("--run-dir", type=_absolute)
    init.add_argument("--recovery-token-file", required=True, type=Path)
    init.add_argument("--coordinator-token-file", required=True, type=Path)
    init.add_argument("--worker-token-file", required=True, type=Path)

    read = commands.add_parser("read"); _add_run(read)
    read.add_argument("--journal", required=True, choices=("inbox", "outbox"))
    read.add_argument("--after", type=int, default=0); read.add_argument("--through", type=int)
    status = commands.add_parser("status"); _add_run(status)
    runs = commands.add_parser("runs", help="list or inspect privately registered runs")
    run_commands = runs.add_subparsers(dest="runs_command", required=True)
    run_commands.add_parser("list")
    run_show = run_commands.add_parser("show")
    run_show.add_argument("--run", required=True, help="run ID, unique prefix, name, handle, or URI")
    run_adopt = run_commands.add_parser("adopt", help="explicitly assign an unowned run")
    run_adopt.add_argument("--run", required=True, help="registered run selector")
    run_adopt.add_argument("--coordinator-state", required=True, type=_absolute)
    coordinator = commands.add_parser("coordinator", help="register or inspect a coordinator session")
    coordinator_commands = coordinator.add_subparsers(dest="coordinator_command", required=True)
    coordinator_register = coordinator_commands.add_parser("register")
    coordinator_register.add_argument("--state", required=True, type=_absolute)
    coordinator_register.add_argument("--transport", required=True, choices=("cmux", "tmux", "native-app"))
    coordinator_register.add_argument("--target", help="exact tmux handle or native-app thread ID")
    coordinator_register.add_argument("--surface", help="exact cmux orchestrator surface UUID")
    coordinator_register.add_argument("--cmux-binary", default=handoff_launcher.CMUX_DEFAULT)
    coordinator_register.add_argument(
        "--owner-pid", required=True, type=int,
        help="PID of the long-lived orchestrator process that owns this watcher",
    )
    coordinator_register.add_argument("--coordinator-id")
    coordinator_retarget = coordinator_commands.add_parser(
        "retarget", help="replace a stopped cmux coordinator's terminal target",
    )
    coordinator_retarget.add_argument("--state", required=True, type=_absolute)
    coordinator_retarget.add_argument("--surface", help="exact cmux orchestrator surface UUID")
    coordinator_retarget.add_argument("--cmux-binary", default=handoff_launcher.CMUX_DEFAULT)
    coordinator_start = coordinator_commands.add_parser(
        "start", help="start or reuse the singleton watcher for a coordinator session",
    )
    coordinator_start.add_argument("--state", required=True, type=_absolute)
    coordinator_start.add_argument("--interval", type=float, default=5.0)
    coordinator_start.add_argument(
        "--mode", choices=WATCHER_MODES, default="auto",
        help="watcher hosting: auto picks surface for cmux coordinators and detached otherwise",
    )
    coordinator_start.add_argument("--cmux-binary", default=handoff_launcher.CMUX_DEFAULT)
    coordinator_show = coordinator_commands.add_parser("show")
    coordinator_show.add_argument("--state", required=True, type=_absolute)
    coordinator_pending = coordinator_commands.add_parser("pending")
    coordinator_pending.add_argument("--state", required=True, type=_absolute)
    coordinator_pending.add_argument(
        "--full-context", action="store_true",
        help="load the full durable context bundle after resume, compaction, or takeover",
    )
    context = commands.add_parser("context", help="read durable context for a registered run")
    context.add_argument("--run", required=True, help="run ID, unique prefix, name, handle, or URI")
    fast_dispatch = commands.add_parser(
        "dispatch", help="recover, send, ring the doorbell, and release in one operation",
    )
    fast_dispatch.add_argument("--run", required=True, help="registered run selector")
    fast_dispatch.add_argument(
        "--body-file", required=True,
        help="UTF-8 instruction file, or - to read the instruction from stdin",
    )
    watch = commands.add_parser("watch", help="stream new outbox events from registered runs")
    watch.add_argument(
        "--run", action="append", default=[],
        help="registered run selector; repeat for multiple runs, omit for all",
    )
    watch.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds")
    watch.add_argument(
        "--coordinator-state", type=_absolute,
        help="private session-owned watcher snapshot for detached coordinator mode",
    )
    watch.add_argument("--timeout", type=float, help="stop after this many seconds")
    watch.add_argument("--once", action="store_true", help="read each selected outbox once and exit")
    watch.add_argument(
        "--notify-cmux", action="store_true",
        help="send a metadata-only cmux notification for each new event",
    )
    ready = commands.add_parser("ready"); _add_run(ready); _add_token(ready)
    send = commands.add_parser("send"); _add_message(send)
    send.add_argument(
        "--no-doorbell", action="store_true",
        help="append only; skip the best-effort terminal doorbell for a registered run",
    )
    emit = commands.add_parser("emit"); _add_message(emit)

    control = commands.add_parser("control")
    controls = control.add_subparsers(dest="control_command", required=True)
    show = controls.add_parser("show"); _add_run(show)
    consume = controls.add_parser("consume"); _add_run(consume); _add_token(consume); consume.add_argument("--through", required=True, type=int)
    for name in ("renew", "release"):
        item = controls.add_parser(name); _add_run(item); _add_token(item)
    integrate = controls.add_parser("integrate"); _add_run(integrate); _add_token(integrate); integrate.add_argument("--commit", required=True)
    abandon = controls.add_parser("abandon"); _add_run(abandon); _add_token(abandon); abandon.add_argument("--reason-file", required=True); abandon.add_argument("--commit")
    takeover = controls.add_parser("takeover"); _add_run(takeover)
    takeover.add_argument("--new-token-file", required=True, type=Path)
    takeover.add_argument("--recovery-token-file", required=True, type=Path)
    takeover.add_argument("--reason-file", required=True); takeover.add_argument("--force", action="store_true")
    rotate = controls.add_parser("rotate-worker"); _add_run(rotate); _add_token(rotate)
    rotate.add_argument("--new-token-file", required=True, type=Path)
    rotate.add_argument("--reason-file", required=True); rotate.add_argument("--confirmed-dead", action="store_true")

    doctor = commands.add_parser("doctor"); _add_run(doctor); _add_token(doctor)
    repairs = doctor.add_mutually_exclusive_group()
    repairs.add_argument("--repair-tail", choices=("inbox", "outbox"))
    repairs.add_argument("--repair-status", action="store_true")
    repairs.add_argument("--repair-control", action="store_true")
    repairs.add_argument("--repair-progress", action="store_true")
    doctor.add_argument("--recovery-token-file", type=Path)
    doctor.add_argument("--new-token-file", type=Path)
    doctor.add_argument("--new-worker-token-file", type=Path)
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any] | list[Any]:
    if args.command == "init":
        return handoff.initialize(
            workspace=args.workspace, kickoff=args.kickoff, harness=args.harness,
            model=args.model, transport=args.transport, effort=args.effort,
            inputs=args.input, state_root=args.state_root, run_dir=args.run_dir,
            recovery_token_file=args.recovery_token_file,
            coordinator_token_file=args.coordinator_token_file,
            worker_token_file=args.worker_token_file,
        )
    if args.command == "read": return handoff.read(args.run_dir, args.journal, after=args.after, through=args.through)
    if args.command == "status": return handoff.status(args.run_dir)
    if args.command == "runs":
        if args.runs_command == "list": return handoff_registry.list_records(private=False)
        if args.runs_command == "show": return handoff_registry.resolve(args.run, private=False)
        if args.runs_command == "adopt":
            record = handoff_registry.resolve(args.run, private=True)
            coordinator = handoff_watcher.read(args.coordinator_state)
            return handoff_registry.public(handoff_registry.adopt(
                record["run_id"], coordinator["coordinator_id"],
            ))
    if args.command == "coordinator":
        if args.coordinator_command == "register": return _register_coordinator(args)
        if args.coordinator_command == "retarget": return _retarget_coordinator(args)
        if args.coordinator_command == "start":
            if args.interval <= 0:
                raise handoff.HandoffError("--interval must be positive", 2)
            return _start_coordinator_watcher(
                args.state, args.interval,
                mode=args.mode, cmux_binary=args.cmux_binary,
            )
        if args.coordinator_command == "show": return handoff_watcher.read(args.state)
        if args.coordinator_command == "pending":
            return _coordinator_pending(args.state, full_context=args.full_context)
    if args.command == "context": return _context_registered(args.run)
    if args.command == "dispatch":
        return _dispatch_registered(args.run, _read_text(args.body_file))
    if args.command == "ready": return handoff.ready(args.run_dir, _token(args, "worker"))
    if args.command == "send": return _send_with_doorbell(args)
    if args.command == "emit":
        result = handoff.emit(args.run_dir, _token(args, "worker"), **_mutation_inputs(args))
        if args.type == "result":
            _notify_result_ready(args.run_dir)
        return result
    if args.command == "control":
        action = args.control_command
        if action == "show": return handoff.control_show(args.run_dir)
        if action == "consume": return handoff.control_consume(args.run_dir, _token(args, "coordinator"), through=args.through)
        if action == "renew": return handoff.control_renew(args.run_dir, _token(args, "coordinator"))
        if action == "release": return handoff.control_release(args.run_dir, _token(args, "coordinator"))
        if action == "integrate": return handoff.control_integrate(args.run_dir, _token(args, "coordinator"), commit=args.commit)
        if action == "abandon": return handoff.control_abandon(args.run_dir, _token(args, "coordinator"), reason=_read_text(args.reason_file), commit=args.commit)
        if action == "takeover":
            recovery = args.recovery_token_file.read_bytes()
            return handoff.control_takeover(args.run_dir, recovery, new_token_file=args.new_token_file, reason=_read_text(args.reason_file), force=args.force)
        if action == "rotate-worker":
            return handoff.control_rotate_worker(args.run_dir, _token(args, "coordinator"), new_token_file=args.new_token_file, reason=_read_text(args.reason_file), confirmed_dead=args.confirmed_dead)
    if args.command == "doctor":
        if args.repair_tail:
            role = "coordinator" if args.repair_tail == "inbox" else "worker"
            return handoff.repair_tail(args.run_dir, args.repair_tail, _token(args, role))
        if args.repair_status: return handoff.repair_status(args.run_dir, _token(args, "worker"))
        if args.repair_progress: return handoff.repair_progress(args.run_dir, _token(args, "worker"))
        if args.repair_control:
            new_token = args.new_token_file.read_bytes() if args.new_token_file else None
            new_worker_token = args.new_worker_token_file.read_bytes() if args.new_worker_token_file else None
            if args.recovery_token_file:
                return handoff.repair_control(
                    args.run_dir, args.recovery_token_file.read_bytes(), recovery=True,
                    new_token=new_token, new_worker_token=new_worker_token,
                )
            return handoff.repair_control(
                args.run_dir, _token(args, "coordinator"),
                new_token=new_token, new_worker_token=new_worker_token,
            )
        return handoff.doctor(args.run_dir)
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "watch":
            if args.interval <= 0:
                raise handoff.HandoffError("--interval must be positive", 2)
            if args.timeout is not None and args.timeout < 0:
                raise handoff.HandoffError("--timeout must be non-negative", 2)
            if args.coordinator_state is not None and (
                args.run or args.timeout is not None or args.once or args.notify_cmux
            ):
                raise handoff.HandoffError(
                    "--coordinator-state cannot be combined with --run, --timeout, --once, or --notify-cmux",
                    2,
                )
            return _watch(args)
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if args.command == "doctor" and not any((args.repair_tail, args.repair_status, args.repair_control, args.repair_progress)) and not result["ok"]:
            return 5
        return 0
    except handoff.HandoffError as exc:
        if exc.report is not None:
            print(json.dumps(exc.report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        print(f"handoffctl: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
