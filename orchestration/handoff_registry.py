"""Private orchestrator-side registry for durable handoff runs."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator

from agents.orchestration import handoff


REGISTRY_VERSION = 1
REGISTRY_ENV = "HANDOFF_REGISTRY_FILE"
PRIVATE_FIELDS = {"credential_dir"}
TERMINAL_STATES = {"succeeded", "failed", "stopped"}


def default_path() -> Path:
    configured = os.environ.get(REGISTRY_ENV)
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise handoff.HandoffError(f"{REGISTRY_ENV} must be absolute", 2)
        return path
    return Path.home() / ".local/state/agents/handoff/registry.json"


def _timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _empty() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "runs": {}}


def _is_pre_rename_record(record: Any) -> bool:
    """Whether a record is invalid only because it predates the rename.

    Such a record is recoverable in full by the migration, so removing it
    would destroy state the migration would have preserved.
    """
    return (
        isinstance(record, dict)
        and "coordinator_id" in record
        and "orchestrator_id" not in record
    )


def _validate_record(record: Any, *, path: Path | None = None) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise handoff.HandoffError("registry record must be an object", 5)
    if path is not None and "coordinator_id" in record and "orchestrator_id" not in record:
        handoff._pre_orchestrator_rename("coordinator_id", path)  # noqa: SLF001
    required = {
        "run_id", "name", "run_dir", "run_uri", "host", "remote_python",
        "transport", "session_transport", "handle", "agent", "model", "effort",
        "credential_dir", "orchestrator_id", "observed_outbox_cursor", "registered_at",
    }
    legacy_required = required - {"orchestrator_id"}
    if set(record) == legacy_required:
        # Registry v1 predated detached orchestrator ownership.  Existing
        # records remain deliberately unowned until an explicit adoption.
        record = {**record, "orchestrator_id": None}
    if set(record) != required:
        raise handoff.HandoffError("registry record has invalid fields", 5)
    for field in (
        "run_id", "name", "run_dir", "run_uri", "transport", "session_transport",
        "handle", "agent", "model", "registered_at",
    ):
        if not isinstance(record[field], str) or not record[field]:
            raise handoff.HandoffError(f"registry {field} must be a non-empty string", 5)
    for field in ("host", "remote_python", "effort", "credential_dir", "orchestrator_id"):
        if record[field] is not None and (not isinstance(record[field], str) or not record[field]):
            raise handoff.HandoffError(f"registry {field} must be null or a non-empty string", 5)
    if record["orchestrator_id"] is not None:
        try:
            parsed = uuid.UUID(record["orchestrator_id"])
        except ValueError as exc:
            raise handoff.HandoffError("registry orchestrator_id must be a UUIDv4", 5) from exc
        if parsed.version != 4 or str(parsed) != record["orchestrator_id"]:
            raise handoff.HandoffError(
                "registry orchestrator_id must be a lowercase canonical UUIDv4", 5,
            )
    cursor = record["observed_outbox_cursor"]
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise handoff.HandoffError("registry observed_outbox_cursor must be non-negative", 5)
    return record


def _validate(value: Any, *, path: Path | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "runs"}:
        raise handoff.HandoffError("registry must contain version and runs", 5)
    if value["version"] != REGISTRY_VERSION or not isinstance(value["runs"], dict):
        raise handoff.HandoffError("unsupported or invalid registry", 5)
    for run_id, record in list(value["runs"].items()):
        normalized = _validate_record(record, path=path)
        if run_id != normalized["run_id"]:
            raise handoff.HandoffError("registry run key does not match record", 5)
        value["runs"][run_id] = normalized
    return value


def _read_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty()
    try:
        return _validate(json.loads(path.read_text(encoding="utf-8")), path=path)
    except (OSError, json.JSONDecodeError) as exc:
        raise handoff.HandoffError(f"cannot read handoff registry {path}: {exc}", 5) from exc


@contextlib.contextmanager
def _locked(path: Path, *, exclusive: bool) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        with os.fdopen(descriptor, "r+b", closefd=False):
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_unlocked(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            Path(temporary).unlink()
        raise


def register(
    *, run_id: str, name: str, run_dir: str, run_uri: str, host: str | None,
    remote_python: str | None, transport: str, session_transport: str, handle: str,
    agent: str, model: str, effort: str | None, credential_dir: str | None,
    orchestrator_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    registry_path = Path(path or default_path())
    with _locked(registry_path, exclusive=True):
        registry = _read_unlocked(registry_path)
        previous = registry["runs"].get(run_id)
        if (
            previous is not None
            and orchestrator_id is not None
            and orchestrator_id != previous["orchestrator_id"]
        ):
            raise handoff.HandoffError(
                "an existing registry record can change orchestrator only through explicit adoption",
                4,
            )
        record = _validate_record({
            "run_id": run_id,
            "name": name,
            "run_dir": run_dir,
            "run_uri": run_uri,
            "host": host,
            "remote_python": remote_python,
            "transport": transport,
            "session_transport": session_transport,
            "handle": handle,
            "agent": agent,
            "model": model,
            "effort": effort,
            "credential_dir": credential_dir,
            "orchestrator_id": (
                previous["orchestrator_id"] if previous else orchestrator_id
            ),
            "observed_outbox_cursor": previous["observed_outbox_cursor"] if previous else 0,
            "registered_at": previous["registered_at"] if previous else _timestamp(),
        })
        registry["runs"][run_id] = record
        _write_unlocked(registry_path, registry)
        return dict(record)


def adopt(run_id: str, orchestrator_id: str, *, path: Path | None = None) -> dict[str, Any]:
    """Explicitly associate an unowned run with one orchestrator session."""
    if not isinstance(orchestrator_id, str) or not orchestrator_id:
        raise handoff.HandoffError("orchestrator_id must be a non-empty string", 2)
    registry_path = Path(path or default_path())
    with _locked(registry_path, exclusive=True):
        registry = _read_unlocked(registry_path)
        if run_id not in registry["runs"]:
            raise handoff.HandoffError(f"unknown handoff run: {run_id}", 4)
        record = registry["runs"][run_id]
        owner = record["orchestrator_id"]
        if owner not in {None, orchestrator_id}:
            raise handoff.HandoffError(
                f"handoff run {run_id} belongs to another orchestrator", 4,
            )
        record["orchestrator_id"] = orchestrator_id
        _write_unlocked(registry_path, registry)
        return dict(record)


def list_records(*, path: Path | None = None, private: bool = False) -> list[dict[str, Any]]:
    registry_path = Path(path or default_path())
    with _locked(registry_path, exclusive=False):
        registry = _read_unlocked(registry_path)
    records = sorted(registry["runs"].values(), key=lambda item: item["registered_at"])
    return [dict(record) if private else public(record) for record in records]


def resolve(selector: str, *, path: Path | None = None, private: bool = True) -> dict[str, Any]:
    if not isinstance(selector, str) or not selector:
        raise handoff.HandoffError("run selector must be non-empty", 2)
    records = list_records(path=path, private=True)
    exact = [
        record for record in records
        if selector in {record["run_id"], record["name"], record["handle"], record["run_uri"]}
    ]
    if not exact:
        prefix = [record for record in records if record["run_id"].startswith(selector)]
        exact = prefix
    if not exact:
        raise handoff.HandoffError(f"unknown handoff run: {selector}", 4)
    if len(exact) > 1:
        raise handoff.HandoffError(f"ambiguous handoff run selector: {selector}", 4)
    return dict(exact[0]) if private else public(exact[0])


def update_observed(run_id: str, through: int, *, path: Path | None = None) -> dict[str, Any]:
    if isinstance(through, bool) or not isinstance(through, int) or through < 0:
        raise handoff.HandoffError("observed cursor must be non-negative", 2)
    registry_path = Path(path or default_path())
    with _locked(registry_path, exclusive=True):
        registry = _read_unlocked(registry_path)
        if run_id not in registry["runs"]:
            raise handoff.HandoffError(f"unknown handoff run: {run_id}", 4)
        record = registry["runs"][run_id]
        record["observed_outbox_cursor"] = max(record["observed_outbox_cursor"], through)
        _write_unlocked(registry_path, registry)
        return dict(record)


def _assess_terminal(record: dict[str, Any]) -> tuple[bool, str | None]:
    """Decide whether a registered run is known to be over.

    Returns ``(terminal, note)``.  ``note`` explains an inferred verdict: why
    a dangling pointer counts as terminal, or why liveness cannot be ruled
    out.  A positively terminal status projection, an orchestrator-concluded
    run (``control.json`` ``desired_state`` is ``"stop"`` — a concluded
    worker stays resident and never emits its terminal checkpoint, so status
    alone would never count it finished), or a missing/unreadable run
    directory counts as terminal; anything else is treated as possibly live
    so a removal never silently orphans a running worker.
    """
    if record["host"] is not None:
        return False, f"run is remote (host {record['host']!r}); liveness cannot be verified from this host"
    run_dir = Path(record["run_dir"])
    if not run_dir.is_dir():
        return True, "run directory is missing or unreadable; record is a dangling pointer"
    try:
        state = handoff.status(run_dir)["state"]
    except handoff.HandoffError as exc:
        return False, f"run status is unreadable ({exc}); the run may still be live"
    if state in TERMINAL_STATES:
        return True, None
    try:
        desired = handoff.control_show(run_dir)["desired_state"]
    except handoff.HandoffError:
        desired = None
    if desired == "stop":
        return True, "orchestrator concluded the run (desired_state is stop)"
    return False, f"worker state {state!r} is not terminal"


def _read_lenient_unlocked(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Read the registry, tolerating per-record corruption.

    Returns the raw registry value and a mapping of registry key to validation
    error for every invalid record.  Top-level corruption — an unreadable
    file, a wrong shape, an unsupported version — remains fatal, exactly as
    on the strict read path; only individual records are tolerated, so
    ``runs clean --run`` stays usable as the escape hatch when one bad record
    breaks every strict registry read.
    """
    if not path.exists():
        return _empty(), {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise handoff.HandoffError(f"cannot read handoff registry {path}: {exc}", 5) from exc
    if not isinstance(value, dict) or set(value) != {"version", "runs"}:
        raise handoff.HandoffError("registry must contain version and runs", 5)
    if value["version"] != REGISTRY_VERSION or not isinstance(value["runs"], dict):
        raise handoff.HandoffError("unsupported or invalid registry", 5)
    invalid: dict[str, str] = {}
    for key, record in list(value["runs"].items()):
        try:
            normalized = _validate_record(record)
            if key != normalized["run_id"]:
                raise handoff.HandoffError("registry run key does not match record", 5)
        except handoff.HandoffError as exc:
            invalid[key] = str(exc)
        else:
            value["runs"][key] = normalized
    return value, invalid


def _resolve_lenient(
    registry: dict[str, Any], invalid: dict[str, str], selector: str,
) -> tuple[str, dict[str, Any] | Any, str | None]:
    """Resolve one selector over a leniently read registry.

    Valid records match exactly as :func:`resolve` does (run ID, name,
    handle, URI, then run-ID prefix); invalid records match their registry
    key or, when decipherable, their raw ``run_id``.  Returns the registry
    key, the record (raw and possibly not a dict when invalid), and the
    record's validation error or ``None``.
    """
    runs = registry["runs"]
    valid = {key: record for key, record in runs.items() if key not in invalid}
    exact = [
        record["run_id"] for record in valid.values()
        if selector in {record["run_id"], record["name"], record["handle"], record["run_uri"]}
    ]
    exact += [
        key for key in invalid
        if selector == key
        or (isinstance(runs[key], dict) and selector == runs[key].get("run_id"))
    ]
    if not exact:
        exact = [record["run_id"] for record in valid.values() if record["run_id"].startswith(selector)]
        exact += [key for key in invalid if key.startswith(selector)]
    if not exact:
        raise handoff.HandoffError(f"unknown handoff run: {selector}", 4)
    if len(exact) > 1:
        raise handoff.HandoffError(f"ambiguous handoff run selector: {selector}", 4)
    key = exact[0]
    return key, runs[key], invalid.get(key)


def _delete_run_dir(record: dict[str, Any], *, dry_run: bool = False) -> tuple[bool, str | None]:
    """Delete one run's on-disk directory; return ``(deleted, note)``.

    Deletion is deliberately narrower than the record: a remote run's
    directory lives on another host, and a local directory is removed only
    when it carries a ``status.json`` — proof it is a handoff run directory
    rather than an arbitrary path a corrupt record happens to name.  With
    ``dry_run`` the same checks run but nothing is removed, so a prune plan
    reports exactly what a real run would delete.
    """
    host = record.get("host")
    if host is not None:
        return False, f"run is remote (host {host!r}); delete its run directory on that host"
    run_dir = record.get("run_dir")
    if not isinstance(run_dir, str) or not run_dir:
        return False, "record has no usable run_dir"
    path = Path(run_dir)
    if not path.is_dir():
        return False, "run directory is already missing"
    if not (path / "status.json").is_file():
        raise handoff.HandoffError(
            f"refusing to delete {path}: no status.json; "
            "not recognizable as a handoff run directory",
            4,
        )
    if not dry_run:
        shutil.rmtree(path)
    return True, None


def validate_records(*, path: Path | None = None) -> dict[str, Any]:
    """Report per-record validity instead of failing the whole read.

    Strict validation on the normal read paths is unchanged; this is the
    diagnostic view that names the records ``runs clean --run`` can then
    remove.
    """
    registry_path = Path(path or default_path())
    with _locked(registry_path, exclusive=False):
        registry, invalid = _read_lenient_unlocked(registry_path)
    return {
        "valid": sorted(key for key in registry["runs"] if key not in invalid),
        "invalid": [
            {
                "key": key,
                "run_id": (
                    registry["runs"][key].get("run_id")
                    if isinstance(registry["runs"][key], dict) else None
                ),
                "error": error,
            }
            for key, error in invalid.items()
        ],
    }


def public(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in PRIVATE_FIELDS}
