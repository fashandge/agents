"""Private coordinator-side registry for durable handoff runs."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator

from agents.orchestration import handoff


REGISTRY_VERSION = 1
REGISTRY_ENV = "HANDOFF_REGISTRY_FILE"
PRIVATE_FIELDS = {"credential_dir"}


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


def _validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise handoff.HandoffError("registry record must be an object", 5)
    required = {
        "run_id", "name", "run_dir", "run_uri", "host", "remote_python",
        "transport", "session_transport", "handle", "agent", "model", "effort",
        "credential_dir", "coordinator_id", "observed_outbox_cursor", "registered_at",
    }
    legacy_required = required - {"coordinator_id"}
    if set(record) == legacy_required:
        # Registry v1 predated detached coordinator ownership.  Existing
        # records remain deliberately unowned until an explicit adoption.
        record = {**record, "coordinator_id": None}
    if set(record) != required:
        raise handoff.HandoffError("registry record has invalid fields", 5)
    for field in (
        "run_id", "name", "run_dir", "run_uri", "transport", "session_transport",
        "handle", "agent", "model", "registered_at",
    ):
        if not isinstance(record[field], str) or not record[field]:
            raise handoff.HandoffError(f"registry {field} must be a non-empty string", 5)
    for field in ("host", "remote_python", "effort", "credential_dir", "coordinator_id"):
        if record[field] is not None and (not isinstance(record[field], str) or not record[field]):
            raise handoff.HandoffError(f"registry {field} must be null or a non-empty string", 5)
    if record["coordinator_id"] is not None:
        try:
            parsed = uuid.UUID(record["coordinator_id"])
        except ValueError as exc:
            raise handoff.HandoffError("registry coordinator_id must be a UUIDv4", 5) from exc
        if parsed.version != 4 or str(parsed) != record["coordinator_id"]:
            raise handoff.HandoffError(
                "registry coordinator_id must be a lowercase canonical UUIDv4", 5,
            )
    cursor = record["observed_outbox_cursor"]
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise handoff.HandoffError("registry observed_outbox_cursor must be non-negative", 5)
    return record


def _validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "runs"}:
        raise handoff.HandoffError("registry must contain version and runs", 5)
    if value["version"] != REGISTRY_VERSION or not isinstance(value["runs"], dict):
        raise handoff.HandoffError("unsupported or invalid registry", 5)
    for run_id, record in list(value["runs"].items()):
        normalized = _validate_record(record)
        if run_id != normalized["run_id"]:
            raise handoff.HandoffError("registry run key does not match record", 5)
        value["runs"][run_id] = normalized
    return value


def _read_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty()
    try:
        return _validate(json.loads(path.read_text(encoding="utf-8")))
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
    coordinator_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    registry_path = Path(path or default_path())
    with _locked(registry_path, exclusive=True):
        registry = _read_unlocked(registry_path)
        previous = registry["runs"].get(run_id)
        if (
            previous is not None
            and coordinator_id is not None
            and coordinator_id != previous["coordinator_id"]
        ):
            raise handoff.HandoffError(
                "an existing registry record can change coordinator only through explicit adoption",
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
            "coordinator_id": (
                previous["coordinator_id"] if previous else coordinator_id
            ),
            "observed_outbox_cursor": previous["observed_outbox_cursor"] if previous else 0,
            "registered_at": previous["registered_at"] if previous else _timestamp(),
        })
        registry["runs"][run_id] = record
        _write_unlocked(registry_path, registry)
        return dict(record)


def adopt(run_id: str, coordinator_id: str, *, path: Path | None = None) -> dict[str, Any]:
    """Explicitly associate an unowned run with one coordinator session."""
    if not isinstance(coordinator_id, str) or not coordinator_id:
        raise handoff.HandoffError("coordinator_id must be a non-empty string", 2)
    registry_path = Path(path or default_path())
    with _locked(registry_path, exclusive=True):
        registry = _read_unlocked(registry_path)
        if run_id not in registry["runs"]:
            raise handoff.HandoffError(f"unknown handoff run: {run_id}", 4)
        record = registry["runs"][run_id]
        owner = record["coordinator_id"]
        if owner not in {None, coordinator_id}:
            raise handoff.HandoffError(
                f"handoff run {run_id} belongs to another coordinator", 4,
            )
        record["coordinator_id"] = coordinator_id
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


def public(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in PRIVATE_FIELDS}
