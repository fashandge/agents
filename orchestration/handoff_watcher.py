"""Credential-free detached watcher state for orchestrator sessions."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from agents.orchestration import handoff
from agents.orchestration import handoff_launcher
from agents.orchestration import handoff_registry


STATE_VERSION = 2
DEFAULT_RETRY_SECONDS = 30.0
DoorbellNotifier = Callable[[dict[str, Any], dict[str, int]], str | None]
"""Delivers one coalesced doorbell; returns the channel(s) that accepted it.

The returned label (for example ``terminal_input``, a ``+``-joined multi-
channel result such as ``cmux_input+cmux_notify``, or a fallback such as
``macos_notification``) is recorded as the run's ``last_doorbell_method``.
A return without an exception means only that a channel accepted the
request — never that the user saw the alert.
"""
OwnerProcessStatus = Literal["alive", "dead", "unknown"]
OwnerProcessProbe = Callable[[dict[str, Any]], OwnerProcessStatus]


def _orchestrator_id(value: Any) -> str:
    if not isinstance(value, str):
        raise handoff.HandoffError("orchestrator_id must be a UUIDv4 string", 5)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise handoff.HandoffError("orchestrator_id must be a UUIDv4 string", 5) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise handoff.HandoffError("orchestrator_id must be a lowercase canonical UUIDv4", 5)
    return value


def _target(transport: Any, target: Any) -> dict[str, str]:
    if not isinstance(target, dict):
        raise handoff.HandoffError("orchestrator target must be an object", 5)
    fields = {
        "cmux": {"workspace", "surface"},
        "tmux": {"handle"},
        "native_app": {"thread_id"},
    }
    if transport not in fields or set(target) != fields[transport]:
        raise handoff.HandoffError("orchestrator target does not match its transport", 5)
    if not all(isinstance(value, str) and value for value in target.values()):
        raise handoff.HandoffError("orchestrator target fields must be non-empty strings", 5)
    if transport == "cmux":
        try:
            surface = uuid.UUID(target["surface"])
        except ValueError as exc:
            raise handoff.HandoffError("cmux orchestrator surface must be a UUID", 5) from exc
        if str(surface) != target["surface"]:
            raise handoff.HandoffError("cmux orchestrator surface must be canonical lowercase", 5)
    return dict(target)


def _process_start_time(pid: int) -> str:
    """Return a stable process-start token from the host's POSIX ``ps``."""
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise handoff.HandoffError(
            f"cannot inspect orchestrator process {pid}: {exc}", 6,
        ) from exc
    started_at = " ".join(completed.stdout.split())
    if completed.returncode != 0 or not started_at:
        raise handoff.HandoffError(
            f"orchestrator process {pid} does not exist or cannot be inspected", 4,
        )
    return started_at


def _owner_process(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"pid", "started_at"}:
        raise handoff.HandoffError("invalid orchestrator owner process", 5)
    pid = value["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise handoff.HandoffError("orchestrator owner PID must be greater than one", 5)
    if not isinstance(value["started_at"], str) or not value["started_at"]:
        raise handoff.HandoffError("orchestrator owner start time must be non-empty", 5)
    return dict(value)


def capture_owner_process(pid: int) -> dict[str, Any]:
    """Capture a PID plus start token so later PID reuse is detectable."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise handoff.HandoffError("orchestrator owner PID must be greater than one", 2)
    return {"pid": pid, "started_at": _process_start_time(pid)}


def owner_process_status(owner: dict[str, Any]) -> OwnerProcessStatus:
    """Return whether the exact registered orchestrator process still exists."""
    owner = _owner_process(owner)
    pid = owner["pid"]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        pass
    except OSError:
        return "unknown"
    try:
        current_started_at = _process_start_time(pid)
    except handoff.HandoffError:
        # A transient ``ps`` failure must not kill a watcher or authorize a
        # doorbell into a target whose ownership cannot currently be proved.
        return "unknown"
    return "alive" if current_started_at == owner["started_at"] else "dead"


def _run_state(value: Any) -> dict[str, Any]:
    required = {
        "observed_through", "doorbell_through", "doorbell_pending",
        "last_doorbell_at", "control_through", "doorbell_after_cursor",
        "last_doorbell_error",
    }
    # ``last_doorbell_method``, ``acknowledged_through``, and
    # ``dismissed_through`` are optional so snapshots written before those
    # fields existed remain readable.
    optional = {"last_doorbell_method", "acknowledged_through", "dismissed_through"}
    if not isinstance(value, dict) or not required <= set(value) <= required | optional:
        raise handoff.HandoffError("invalid orchestrator run notification state", 5)
    for field in (
        "observed_through", "doorbell_through", "control_through",
        "doorbell_after_cursor",
    ):
        counter = value[field]
        if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
            raise handoff.HandoffError(f"orchestrator run {field} must be non-negative", 5)
    acknowledged = value.get("acknowledged_through", 0)
    if isinstance(acknowledged, bool) or not isinstance(acknowledged, int) or acknowledged < 0:
        raise handoff.HandoffError("orchestrator run acknowledged_through must be non-negative", 5)
    dismissed = value.get("dismissed_through", 0)
    if isinstance(dismissed, bool) or not isinstance(dismissed, int) or dismissed < 0:
        raise handoff.HandoffError("orchestrator run dismissed_through must be non-negative", 5)
    if value["doorbell_through"] > value["observed_through"]:
        raise handoff.HandoffError("doorbell cursor exceeds observed cursor", 5)
    if not isinstance(value["doorbell_pending"], bool):
        raise handoff.HandoffError("doorbell_pending must be boolean", 5)
    for field in ("last_doorbell_at", "last_doorbell_error", "last_doorbell_method"):
        if value.get(field) is not None and not isinstance(value.get(field), str):
            raise handoff.HandoffError(f"{field} must be null or a string", 5)
    if value["last_doorbell_at"] is not None:
        handoff._parse_time(value["last_doorbell_at"])  # noqa: SLF001
    return value


def _validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "version", "orchestrator_id", "transport", "target", "owner_process", "runs",
    }:
        raise handoff.HandoffError("invalid orchestrator watcher snapshot", 5)
    if value["version"] != STATE_VERSION:
        raise handoff.HandoffError("unsupported orchestrator watcher state version", 5)
    _orchestrator_id(value["orchestrator_id"])
    _target(value["transport"], value["target"])
    _owner_process(value["owner_process"])
    if not isinstance(value["runs"], dict):
        raise handoff.HandoffError("orchestrator watcher runs must be an object", 5)
    for run_id, run_state in value["runs"].items():
        if not isinstance(run_id, str) or not run_id:
            raise handoff.HandoffError("orchestrator watcher run IDs must be non-empty", 5)
        _run_state(run_state)
    return value


def _state_path(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise handoff.HandoffError("orchestrator state path must be absolute", 2)
    return path


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _write_new(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.init.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise handoff.HandoffError(
                f"orchestrator state already exists: {path}", 4,
            ) from exc
        os.chmod(path, 0o600)
        Path(temporary).unlink()
        handoff._fsync_dir(path.parent)  # noqa: SLF001
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            Path(temporary).unlink()
        raise


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        handoff._fsync_dir(path.parent)  # noqa: SLF001
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            Path(temporary).unlink()
        raise


def initialize(
    path: Path, *, transport: str, target: dict[str, str],
    owner_pid: int, orchestrator_id: str | None = None,
) -> dict[str, Any]:
    """Create one private orchestrator-session watcher snapshot."""
    path = _state_path(path)
    value = _validate({
        "version": STATE_VERSION,
        "orchestrator_id": orchestrator_id or str(uuid.uuid4()),
        "transport": transport,
        "target": target,
        "owner_process": capture_owner_process(owner_pid),
        "runs": {},
    })
    _write_new(path, value)
    return value


def retarget(path: Path, target: dict[str, str]) -> dict[str, Any]:
    """Replace a stopped orchestrator's transport target without dropping runs."""
    path = _state_path(path)
    if is_running(path):
        raise handoff.HandoffError(
            "cannot retarget a running orchestrator watcher; stop it first", 4,
        )
    value = read(path)
    value["target"] = _target(value["transport"], target)
    _atomic_write(path, value)
    return value


def read(path: Path) -> dict[str, Any]:
    path = _state_path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise handoff.HandoffError(f"cannot read orchestrator state {path}: {exc}", 6) from exc
    try:
        value = handoff._decode_json(raw, path)  # noqa: SLF001
        if isinstance(value, dict) and "coordinator_id" in value and "orchestrator_id" not in value:
            handoff._pre_orchestrator_rename("coordinator_id", path)  # noqa: SLF001
        return _validate(value)
    except handoff.HandoffError:
        raise


@contextlib.contextmanager
def instance_lock(path: Path) -> Iterator[None]:
    """Hold the orchestrator state's singleton lock for one watcher lifetime."""
    path = _state_path(path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(lock_path.parent, 0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise handoff.HandoffError(
                f"orchestrator watcher is already running for {read(path)['orchestrator_id']}", 4,
            ) from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def start_lock(path: Path) -> Iterator[int]:
    """Serialize detached watcher check-and-start operations."""
    path = _state_path(path)
    lock_path = path.with_suffix(path.suffix + ".start.lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(lock_path.parent, 0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def is_running(path: Path) -> bool:
    """Report whether a watcher currently holds this state's lifetime lock."""
    path = _state_path(path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(lock_path.parent, 0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _empty_run_state() -> dict[str, Any]:
    return {
        "observed_through": 0,
        "doorbell_through": 0,
        "doorbell_pending": False,
        "last_doorbell_at": None,
        "control_through": 0,
        "doorbell_after_cursor": 0,
        "last_doorbell_error": None,
        "last_doorbell_method": None,
        "acknowledged_through": 0,
        "dismissed_through": 0,
    }


def acknowledge(path: Path, acks: dict[str, int]) -> dict[str, Any]:
    """Record that the orchestrator has loaded each run's events through a seq.

    ``orchestrator pending`` calls this after surfacing a run's unread outbox so
    the watcher stops re-ringing events the orchestrator has already seen.  The
    cursor only advances (monotonic ``max``); a strictly newer ``observed_through``
    still doorbells.

    Lock-free by design.  ``poll`` re-derives every other run field from
    protocol truth each interval and never touches ``acknowledged_through``, so
    a lost update against a concurrent poll costs at most one extra doorbell and
    self-heals on the next ``pending`` call.  Runs absent from the snapshot are
    ignored.
    """
    path = _state_path(path)
    if not acks:
        return read(path)
    value = read(path)
    changed = False
    for run_id, through in acks.items():
        if isinstance(through, bool) or not isinstance(through, int) or through < 0:
            raise handoff.HandoffError("acknowledge cursor must be non-negative", 2)
        run_state = value["runs"].get(run_id)
        if run_state is None:
            continue
        if through > run_state.get("acknowledged_through", 0):
            run_state["acknowledged_through"] = through
            changed = True
    if changed:
        _atomic_write(path, value)
    return value


def dismiss(path: Path, dismissals: dict[str, int]) -> dict[str, Any]:
    """Record that the orchestrator has dismissed each run's events through a seq.

    ``orchestrator dismiss`` calls this without consuming the protocol outbox so
    the watcher stops ringing for the current worker state.  The cursor only
    advances (monotonic ``max``); a strictly newer ``observed_through`` still
    doorbells.

    Lock-free by design.  ``poll`` re-derives every other run field from
    protocol truth each interval and never touches ``dismissed_through``, so a
    lost update against a concurrent poll costs at most one extra doorbell and
    self-heals on the next ``dismiss`` call.  Runs absent from the snapshot are
    ignored.
    """
    path = _state_path(path)
    if not dismissals:
        return read(path)
    value = read(path)
    changed = False
    for run_id, through in dismissals.items():
        if isinstance(through, bool) or not isinstance(through, int) or through < 0:
            raise handoff.HandoffError("dismiss cursor must be non-negative", 2)
        run_state = value["runs"].get(run_id)
        if run_state is None:
            continue
        if through > run_state.get("dismissed_through", 0):
            run_state["dismissed_through"] = through
            changed = True
    if changed:
        _atomic_write(path, value)
    return value


def _remote_json(record: dict[str, Any], argv: list[str]) -> Any:
    try:
        completed = handoff_launcher._run_ssh(  # noqa: SLF001
            record["host"],
            [record["remote_python"], "-m", "agents.orchestration.handoffctl", *argv],
            stdin=None,
            timeout=30,
        )
        return json.loads(completed.stdout)
    except (handoff_launcher.AdapterError, json.JSONDecodeError) as exc:
        raise handoff.HandoffError(f"remote watcher read failed: {exc}", 6) from exc


def _protocol_state(
    record: dict[str, Any], after: int,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], int]:
    if record["host"] is None:
        run_dir = Path(record["run_dir"])
        control = handoff.control_show(run_dir)
        status = handoff.status(run_dir)
        records = handoff._journal(run_dir, "outbox")  # noqa: SLF001 - validated tail is required
        if after > len(records):
            raise handoff.HandoffError("watcher observed cursor exceeds outbox tail", 5)
        if control["outbox_cursor"] > len(records):
            raise handoff.HandoffError("control outbox cursor exceeds outbox tail", 5)
        return control, status, records[after:], len(records)
    control = _remote_json(
        record,
        ["control", "show", "--run-dir", record["run_dir"]],
    )
    status = _remote_json(record, ["status", "--run-dir", record["run_dir"]])
    events = _remote_json(
        record,
        [
            "read", "--run-dir", record["run_dir"], "--journal", "outbox",
            "--after", str(after),
        ],
    )
    if not isinstance(status, dict) or not isinstance(control, dict) or not isinstance(events, list):
        raise handoff.HandoffError("remote watcher read returned invalid data", 6)
    if status.get("state") not in handoff.WORKER_STATES or not isinstance(status.get("blocked_on"), list):
        raise handoff.HandoffError("remote watcher status is invalid", 6)
    tail = events[-1]["seq"] if events else after
    if control["outbox_cursor"] > tail:
        raise handoff.HandoffError("remote control outbox cursor exceeds observed tail", 5)
    return control, status, events, tail


def _retry_due(run_state: dict[str, Any], now: datetime.datetime, retry_seconds: float) -> bool:
    last = run_state["last_doorbell_at"]
    if last is None:
        return True
    return (now - handoff._parse_time(last)).total_seconds() >= retry_seconds  # noqa: SLF001


def poll(
    path: Path, *, registry_path: Path | None = None,
    notifier: DoorbellNotifier, retry_seconds: float = DEFAULT_RETRY_SECONDS,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Observe one dynamic registry snapshot and attempt one coalesced doorbell.

    Doorbell bookkeeping records *attempts*, never confirmed visibility: a
    notifier that returns without raising has only shown that its channel
    accepted the request (for cmux/tmux, a zero subprocess exit).  The
    channel label the notifier returns is stored as ``last_doorbell_method``
    so operators can tell a visible ``cmux notify`` alert apart from raw
    terminal input or a degraded fallback.  ``last_doorbell_error`` is null
    exactly when a channel accepted; when every channel fails, the method is
    null and the error describes the combined failure.
    """
    if retry_seconds <= 0:
        raise handoff.HandoffError("watcher retry interval must be positive", 2)
    path = _state_path(path)
    value = read(path)
    current_time = now or datetime.datetime.now(datetime.timezone.utc)
    errors: list[dict[str, str]] = []
    changed = False
    observations: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}

    records = handoff_registry.list_records(path=registry_path, private=True)
    owned = [
        record for record in records
        if record["orchestrator_id"] == value["orchestrator_id"]
    ]
    owned_ids = {record["run_id"] for record in owned}
    for record in owned:
        run_id = record["run_id"]
        run_state = value["runs"].setdefault(run_id, _empty_run_state())
        try:
            control, status, events, tail = _protocol_state(
                record, run_state["observed_through"],
            )
            if tail < run_state["observed_through"]:
                raise handoff.HandoffError("worker outbox tail regressed", 5)
            previous = dict(run_state)
            run_state["observed_through"] = tail
            run_state["control_through"] = control["outbox_cursor"]
            run_state["doorbell_pending"] = tail > control["outbox_cursor"]
            if events and events[-1]["seq"] != tail:
                raise handoff.HandoffError("worker outbox observation is not contiguous", 5)
            observations[run_id] = (status, events)
            changed = changed or run_state != previous
        except handoff.HandoffError as exc:
            errors.append({"run_id": run_id, "error": str(exc)})

    # Persist observation before any best-effort push.  This is the recovery
    # boundary for a crash before or immediately after delivery.
    if changed:
        _atomic_write(path, value)

    coverage: dict[str, int] = {}
    for run_id, run_state in value["runs"].items():
        if run_id not in owned_ids or not run_state["doorbell_pending"]:
            continue
        observation = observations.get(run_id)
        if observation is None:
            continue
        status, events = observation
        if run_state["observed_through"] <= run_state.get("dismissed_through", 0):
            continue
        new_events = run_state["observed_through"] > run_state["doorbell_through"]
        partial_consumption = (
            run_state["control_through"] > run_state["doorbell_after_cursor"]
            and run_state["control_through"] < run_state["observed_through"]
        )
        state = status["state"]
        blocking = state == "blocked" and bool(status["blocked_on"])
        ring_once = state in {"awaiting_review", "failed"}
        error_while_working = state in {"starting", "working"} and any(
            event["type"] == "error" for event in events
        )
        if not blocking and not ring_once and not error_while_working:
            continue
        if not blocking and run_state["observed_through"] <= run_state.get("acknowledged_through", 0):
            # The orchestrator has already loaded every observed event via
            # ``orchestrator pending``.  Do not re-ring until a strictly newer
            # event arrives.  Without this gate an unconsumed-but-seen result
            # doorbells forever, because ``doorbell_pending`` stays true until
            # the protocol outbox cursor advances and ``_retry_due`` re-fires
            # on every interval — even when the orchestrator has no lease to
            # consume with and has already acted on the event.
            continue
        if blocking:
            should_ring = new_events or _retry_due(run_state, current_time, retry_seconds)
        elif error_while_working:
            # A nonfatal error observed while the worker remains active is
            # actionable, but it does not become a retrying notification.
            should_ring = True
        else:
            should_ring = (
                new_events
                or partial_consumption
                or _retry_due(run_state, current_time, retry_seconds)
            )
        if should_ring:
            coverage[run_id] = run_state["observed_through"]

    if coverage:
        notification_error: str | None = None
        delivered_method: str | None = None
        try:
            delivered_method = notifier(value, coverage)
        except Exception as exc:  # Push failure never changes protocol truth.
            notification_error = str(exc)
            errors.append({"run_id": ",".join(sorted(coverage)), "error": notification_error})
        attempted_at = current_time.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        for run_id, through in coverage.items():
            run_state = value["runs"][run_id]
            run_state["doorbell_through"] = through
            run_state["doorbell_pending"] = (
                run_state["observed_through"] > run_state["control_through"]
            )
            run_state["last_doorbell_at"] = attempted_at
            run_state["doorbell_after_cursor"] = run_state["control_through"]
            run_state["last_doorbell_error"] = notification_error
            run_state["last_doorbell_method"] = (
                delivered_method if notification_error is None else None
            )
        _atomic_write(path, value)

    return {
        "orchestrator_id": value["orchestrator_id"],
        "owned_runs": [record["run_id"] for record in owned],
        "attempted": coverage,
        "pending": {
            run_id: run_state["observed_through"]
            for run_id, run_state in value["runs"].items()
            if run_state["doorbell_pending"]
        },
        "errors": errors,
    }


def watch(
    path: Path, *, interval: float = 5.0, notifier: DoorbellNotifier,
    registry_path: Path | None = None,
    owner_probe: OwnerProcessProbe = owner_process_status,
    on_poll: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Run until stopped or the exact owning orchestrator process exits."""
    if interval <= 0:
        raise handoff.HandoffError("--interval must be positive", 2)
    with instance_lock(path):
        while True:
            value = read(path)
            status = owner_probe(value["owner_process"])
            if status == "dead":
                return
            if status == "alive":
                result = poll(path, registry_path=registry_path, notifier=notifier)
                if on_poll is not None:
                    on_poll(result)
            # Unknown liveness deliberately suppresses doorbells: a transient
            # host probe failure is not evidence that the original process is
            # gone, but neither is it authority to type into its terminal.
            time.sleep(interval)
