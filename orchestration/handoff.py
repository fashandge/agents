"""Durable local-v1 handoff protocol.

This module is intentionally standard-library only.  Journals are authoritative;
``status.json`` and ``control.json`` are durable projections updated after the
corresponding append.  Public functions return JSON-serializable values and never
read credential environment variables.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from agents import env


PROTOCOL_VERSION = 1
LEASE_SECONDS = 180
MAX_KICKOFF = 1024 * 1024
MAX_JOURNAL_LINE = 256 * 1024
MAX_BODY = 128 * 1024
MAX_DATA = 64 * 1024
MAX_ATTACHMENTS = 128
MAX_ATTACHMENT = 32 * 1024 * 1024

INBOX_TYPES = {
    "steer", "supersede", "answer", "review", "input-changed", "base-changed",
    "integration", "orchestrator-takeover", "worker-relaunched", "stop", "pause",
}
SYSTEM_INBOX_TYPES = {"integration", "orchestrator-takeover", "worker-relaunched"}
# ``stop``/``pause`` are retired from the *writable* vocabulary but stay in
# INBOX_TYPES permanently: append-only journals make historical records
# readable forever, and a skewed older remote host may still emit them.
SENDABLE_TYPES = INBOX_TYPES - SYSTEM_INBOX_TYPES - {"stop", "pause"}
# Messages that deliver work (or changed inputs) to the worker epoch.
WORK_MESSAGE_TYPES = {"steer", "answer", "supersede", "input-changed", "base-changed"}
OUTBOX_TYPES = {"checkpoint", "question", "result", "error"}
WORKER_STATES = {
    "starting", "working", "blocked", "awaiting_review", "succeeded",
    "failed", "stopped", "paused",
}
# ``succeeded``/``stopped``/``failed`` are retired from the *writable* state
# vocabulary (new runs use only starting/working/blocked/awaiting_review/
# paused, and fatality is derived from the outbox rather than a state).  They
# stay in WORKER_STATES permanently on the read path: legacy status snapshots
# persist indefinitely, and handoff_watcher validates remote statuses against
# this set, so a skewed older remote host must remain readable, never an error.
UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
IDENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

RUN_FIELDS = {"protocol_version", "run_id", "created_at", "repo", "workspace", "worker", "inputs"}
STATUS_FIELDS = {
    "protocol_version", "run_id", "revision", "worker_epoch", "state", "stage",
    "inbox_cursor", "outbox_seq", "blocked_on", "current_activity",
    "last_progress_at", "updated_at",
}
CONTROL_FIELDS = {
    "protocol_version", "run_id", "revision", "orchestrator_epoch",
    "orchestrator_token_sha256", "recovery_token_sha256",
    "orchestrator_lease_expires_at", "worker_epoch", "worker_token_sha256",
    "review_state", "review_result_id", "last_command_seq",
    "outbox_cursor", "integration", "updated_at",
}
# Legacy control.json files may still carry ``desired_state`` (retired in
# favor of the registry finished marker).  It is accepted and ignored on the
# read path, permanently — see the validation policy in _validate_control.
LEGACY_CONTROL_FIELDS = {"desired_state"}
MESSAGE_FIELDS = {
    "protocol_version", "seq", "message_id", "writer_epoch", "ts", "type",
    "reply_to", "body", "data", "attachments",
}
ATTACHMENT_FIELDS = {"path", "sha256", "size", "git_blob", "snapshot"}
PRE_ORCHESTRATOR_RENAME_EXIT_CODE = 7


class HandoffError(Exception):
    """Protocol error with a stable CLI exit code."""

    def __init__(self, message: str, exit_code: int = 2, *, report: dict[str, Any] | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.report = report


def _pre_orchestrator_rename(found: str, path: Path) -> None:
    raise HandoffError(
        f'handoff state predates the orchestrator rename (found "{found}" in {path}). '
        f"Run: {sys.executable} scripts/migrate_handoff_state_orchestrator.py --dry-run; "
        "then run the same command without --dry-run to apply.",
        PRE_ORCHESTRATOR_RENAME_EXIT_CODE,
    )


def _fail(message: str, code: int = 2) -> None:
    raise HandoffError(message, code)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _timestamp(value: datetime.datetime | None = None) -> str:
    value = value or _now()
    return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_time(value: str) -> datetime.datetime:
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        _fail(f"invalid RFC3339 timestamp: {value!r}")
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError as exc:
        raise HandoffError(f"invalid RFC3339 timestamp: {value!r}", 2) from exc


def _uuid4(value: str, field: str = "UUID") -> str:
    if not isinstance(value, str) or not UUID4_RE.fullmatch(value):
        _fail(f"{field} must be a lowercase canonical UUIDv4")
    return value


def _counter(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        _fail(f"{field} must be a {'positive' if positive else 'non-negative'} integer")
    return value


def _string(value: Any, field: str, *, max_bytes: int | None = None, nonempty: bool = False) -> str:
    if not isinstance(value, str):
        _fail(f"{field} must be a string")
    size = len(value.encode("utf-8"))
    if nonempty and size == 0:
        _fail(f"{field} must be non-empty")
    if max_bytes is not None and size > max_bytes:
        _fail(f"{field} exceeds {max_bytes} UTF-8 bytes")
    return value


def _fields(value: Any, expected: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{name} must be an object")
    actual = set(value)
    if actual != expected:
        _fail(f"{name} fields differ: missing={sorted(expected-actual)}, unknown={sorted(actual-expected)}")
    return value


def _distinct_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        _fail(f"{field} must be a list")
    result: list[str] = []
    for item in value:
        item = _string(item, field, max_bytes=2048, nonempty=True)
        if item in result:
            _fail(f"{field} must contain distinct strings")
        result.append(item)
    return result


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json(raw: bytes, source: str) -> Any:
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid constant {value}")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HandoffError(f"invalid JSON in {source}: {exc}", 5) from exc


def _json_bytes(value: Any, *, compact: bool = False) -> bytes:
    if compact:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return text.encode("utf-8")


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Some filesystems do not permit directory fsync.
        pass


def _write_new(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, mode)


def _atomic_json(path: Path, value: Any) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def _load_json(path: Path, validator: Any) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HandoffError(f"cannot read {path}: {exc}", 6) from exc
    value = _decode_json(raw, str(path))
    try:
        if validator is _validate_control:
            validator(value, path=path)
        else:
            validator(value)
    except HandoffError as exc:
        if exc.exit_code == 2:
            raise HandoffError(f"corrupt snapshot {path}: {exc}", 5) from exc
        raise
    return value


def _append(path: Path, message: dict[str, Any]) -> None:
    raw = _json_bytes(message, compact=True)
    if len(raw) > MAX_JOURNAL_LINE:
        _fail(f"journal record exceeds {MAX_JOURNAL_LINE} bytes")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        try:
            written = 0
            while written < len(raw):
                written += os.write(fd, raw[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise HandoffError(f"cannot append {path}: {exc}", 6) from exc


@contextlib.contextmanager
def _lock(run_dir: Path, role: str, *, shared: bool = False, timeout: float = 30.0) -> Iterator[None]:
    path = run_dir / f"{role}.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise HandoffError(f"cannot open lock {path}: {exc}", 6) from exc
    operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    _fail(f"timed out acquiring {role} lock", 4)
                time.sleep(0.025)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _run_dir(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        _fail("run directory must be an absolute path")
    if not path.is_dir():
        _fail(f"run directory does not exist: {path}", 6)
    return path


def _token_hash(token: bytes) -> str:
    token = token.strip()
    if not re.fullmatch(rb"[0-9a-f]{64}", token):
        _fail("credential has invalid format", 3)
    return hashlib.sha256(token).hexdigest()


def _authorize(control: dict[str, Any], token: bytes, role: str, *, lease: bool = False) -> None:
    field = f"{role}_token_sha256"
    if not secrets.compare_digest(_token_hash(token), control[field]):
        _fail(f"invalid or stale {role} credential", 3)
    if lease and _parse_time(control["orchestrator_lease_expires_at"]) <= _now():
        _fail("orchestrator credential lease has expired", 3)


def _new_token_file(path: Path, token: bytes | None = None) -> bytes:
    path = Path(path).resolve(strict=False)
    token = token or secrets.token_hex(32).encode("ascii")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        _write_new(path, token + b"\n", 0o600)
        _fsync_dir(path.parent)
    except FileExistsError as exc:
        raise HandoffError(f"credential file already exists: {path}", 4) from exc
    except OSError as exc:
        raise HandoffError(f"cannot create credential file {path}: {exc}", 6) from exc
    return token


def _validate_run(value: Any) -> None:
    value = _fields(value, RUN_FIELDS, "run")
    if value["protocol_version"] != 1:
        _fail("unsupported protocol version")
    _uuid4(value["run_id"], "run_id")
    _parse_time(value["created_at"])
    repo = _fields(value["repo"], {"path", "remote"}, "repo")
    if not isinstance(repo["path"], str) or not Path(repo["path"]).is_absolute():
        _fail("repo.path must be absolute")
    if repo["remote"] is not None:
        _string(repo["remote"], "repo.remote")
    workspace = _fields(value["workspace"], {"path", "host", "base_commit"}, "workspace")
    if not isinstance(workspace["path"], str) or not Path(workspace["path"]).is_absolute():
        _fail("workspace.path must be absolute")
    _string(workspace["host"], "workspace.host", nonempty=True)
    if workspace["base_commit"] is not None:
        _string(workspace["base_commit"], "workspace.base_commit", nonempty=True)
    worker = _fields(value["worker"], {"harness", "model", "effort", "transport"}, "worker")
    for field in ("harness", "transport"):
        if not isinstance(worker[field], str) or not IDENT_RE.fullmatch(worker[field]):
            _fail(f"worker.{field} is not a valid identifier")
    _string(worker["model"], "worker.model", nonempty=True)
    if worker["effort"] is not None:
        _string(worker["effort"], "worker.effort")
    if not isinstance(value["inputs"], list):
        _fail("inputs must be a list")
    seen: set[str] = set()
    for entry in value["inputs"]:
        entry = _fields(entry, {"path", "sha256", "size", "git_blob"}, "input")
        _relative_path(entry["path"])
        if entry["path"] in seen:
            _fail("input paths must be unique")
        seen.add(entry["path"])
        if not isinstance(entry["sha256"], str) or not HEX_RE.fullmatch(entry["sha256"]):
            _fail("input sha256 is invalid")
        _counter(entry["size"], "input.size")
        if entry["git_blob"] is not None:
            _string(entry["git_blob"], "input.git_blob", nonempty=True)


def _validate_status(value: Any) -> None:
    value = _fields(value, STATUS_FIELDS, "status")
    if value["protocol_version"] != 1:
        _fail("unsupported protocol version")
    _uuid4(value["run_id"], "run_id")
    _counter(value["revision"], "revision", positive=True)
    _counter(value["worker_epoch"], "worker_epoch", positive=True)
    if value["state"] not in WORKER_STATES:
        _fail("invalid worker state")
    _string(value["stage"], "stage", max_bytes=256)
    _counter(value["inbox_cursor"], "inbox_cursor")
    _counter(value["outbox_seq"], "outbox_seq")
    if not isinstance(value["blocked_on"], list) or len(set(value["blocked_on"])) != len(value["blocked_on"]):
        _fail("blocked_on must be a unique list")
    for item in value["blocked_on"]:
        _uuid4(item, "blocked_on item")
    _string(value["current_activity"], "current_activity", max_bytes=2048)
    if value["last_progress_at"] is not None:
        _parse_time(value["last_progress_at"])
    _parse_time(value["updated_at"])


def _validate_control(value: Any, *, path: Path | None = None) -> None:
    if path is not None and isinstance(value, dict):
        for old, new in (
            ("coordinator_epoch", "orchestrator_epoch"),
            ("coordinator_token_sha256", "orchestrator_token_sha256"),
            ("coordinator_lease_expires_at", "orchestrator_lease_expires_at"),
        ):
            if old in value and new not in value:
                _pre_orchestrator_rename(old, path)
    fields = set(CONTROL_FIELDS)
    if isinstance(value, dict) and "desired_state" in value:
        # Legacy key: accepted (and ignored) so historical control snapshots
        # keep validating; nothing reads it anymore.
        fields |= LEGACY_CONTROL_FIELDS
    value = _fields(value, fields, "control")
    if value["protocol_version"] != 1:
        _fail("unsupported protocol version")
    _uuid4(value["run_id"], "run_id")
    for field in ("revision", "orchestrator_epoch", "worker_epoch"):
        _counter(value[field], field, positive=True)
    for field in ("orchestrator_token_sha256", "recovery_token_sha256", "worker_token_sha256"):
        if not isinstance(value[field], str) or not HEX_RE.fullmatch(value[field]):
            _fail(f"invalid {field}")
    _parse_time(value["orchestrator_lease_expires_at"])
    if "desired_state" in value and value["desired_state"] not in {"run", "stop", "pause"}:
        _fail("invalid desired_state")
    if value["review_state"] not in {"pending", "changes_requested", "superseded", "accepted"}:
        _fail("invalid review_state")
    if value["review_result_id"] is not None:
        _uuid4(value["review_result_id"], "review_result_id")
    _counter(value["last_command_seq"], "last_command_seq")
    _counter(value["outbox_cursor"], "outbox_cursor")
    integration = _fields(value["integration"], {"state", "commit", "reason"}, "integration")
    if integration["state"] not in {"pending", "integrated", "abandoned"}:
        _fail("invalid integration state")
    if integration["state"] == "pending" and (integration["commit"] is not None or integration["reason"] is not None):
        _fail("pending integration must have null commit and reason")
    if integration["state"] == "integrated":
        _string(integration["commit"], "integration.commit", nonempty=True)
        if integration["reason"] is not None:
            _fail("integrated reason must be null")
    if integration["state"] == "abandoned":
        if integration["commit"] is not None:
            _string(integration["commit"], "integration.commit", nonempty=True)
        _string(integration["reason"], "integration.reason", nonempty=True)
    _parse_time(value["updated_at"])


def _relative_path(value: Any) -> str:
    value = _string(value, "path", nonempty=True)
    if "\\" in value or "\x00" in value:
        _fail("path contains a forbidden character")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _fail("path must be normalized and workspace-relative")
    if path.as_posix() != value:
        _fail("path must use normalized POSIX syntax")
    return value


def _validate_attachment(value: Any) -> None:
    value = _fields(value, ATTACHMENT_FIELDS, "attachment")
    _relative_path(value["path"])
    if not isinstance(value["sha256"], str) or not HEX_RE.fullmatch(value["sha256"]):
        _fail("invalid attachment sha256")
    _counter(value["size"], "attachment.size")
    if value["git_blob"] is not None:
        _string(value["git_blob"], "attachment.git_blob", nonempty=True)
    if value["snapshot"] is not None:
        snapshot = _relative_path(value["snapshot"])
        if not snapshot.startswith("attachments/"):
            _fail("attachment snapshot must be under attachments/")


VERIFICATION_ITEM_SCHEMA = '{"command": str, "exit_code": int|null, "summary": str}'

EMIT_DATA_SCHEMAS = f"""Data schemas for `emit --type <kind> --data-file data.json` (fields are exact — missing or unknown fields are rejected; cursors are non-negative integers):
- checkpoint: {{"state": "working"|"paused", "stage": str, "current_activity": str, "inbox_cursor": int, "commitments": [str]}}
- question: {{"blocking": bool, "stage": str, "current_activity": str, "inbox_cursor": int, "commitments": [str]}}
- result: {{"head": str|null, "dirty": bool, "stage": str, "inbox_cursor": int, "verification": [{VERIFICATION_ITEM_SCHEMA}], "commitments": [str]}}
- error: {{"fatal": bool, "category": str, "stage": str, "inbox_cursor": int, "commitments": [str]}} (non-empty body required)
The body file carries the human-readable content (the answer, evidence, workspace state)."""


def _validate_verification(value: Any) -> None:
    if not isinstance(value, list):
        _fail(f"verification must be a list of {VERIFICATION_ITEM_SCHEMA} objects")
    for entry in value:
        if not isinstance(entry, dict):
            _fail(f"verification item must be an object {VERIFICATION_ITEM_SCHEMA}")
        entry = _fields(entry, {"command", "exit_code", "summary"}, "verification item")
        _string(entry["command"], "verification.command")
        _string(entry["summary"], "verification.summary")
        if entry["exit_code"] is not None and (isinstance(entry["exit_code"], bool) or not isinstance(entry["exit_code"], int)):
            _fail("verification.exit_code must be an integer or null")


def _validate_type_data(direction: str, kind: str, body: str, reply_to: Any, data: Any) -> None:
    if not isinstance(data, dict):
        _fail("data must be an object")
    if len(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()) > MAX_DATA:
        _fail(f"data exceeds {MAX_DATA} serialized bytes")
    if direction == "inbox":
        if kind in {"steer", "supersede", "answer"}:
            _fields(data, set(), "data")
            _string(body, "body", nonempty=True)
            if kind == "steer" and reply_to is not None:
                _fail("steer cannot set reply_to")
            if kind == "supersede" and reply_to is None:
                _fail("supersede requires reply_to")
        elif kind == "review":
            _fields(data, {"disposition"}, "data")
            if data["disposition"] not in {"accepted", "changes_requested"}:
                _fail("invalid review disposition")
            if data["disposition"] == "changes_requested":
                _string(body, "body", nonempty=True)
        elif kind == "input-changed":
            _fields(data, {"path", "sha256", "git_blob"}, "data")
            _relative_path(data["path"])
            if not isinstance(data["sha256"], str) or not HEX_RE.fullmatch(data["sha256"]):
                _fail("input-changed sha256 is invalid")
            if data["git_blob"] is not None:
                _string(data["git_blob"], "git_blob", nonempty=True)
        elif kind == "base-changed":
            _fields(data, {"base_commit"}, "data")
            _string(data["base_commit"], "base_commit", nonempty=True)
            _string(body, "body", nonempty=True)
        elif kind == "integration":
            _fields(data, {"state", "commit", "reason"}, "data")
            if data["state"] == "integrated":
                _string(data["commit"], "commit", nonempty=True)
                if data["reason"] is not None:
                    _fail("integrated reason must be null")
            elif data["state"] == "abandoned":
                if data["commit"] is not None:
                    _string(data["commit"], "commit", nonempty=True)
                _string(data["reason"], "reason", nonempty=True)
            else:
                _fail("invalid integration state")
        elif kind == "orchestrator-takeover":
            _fields(data, {"previous_epoch", "new_epoch", "reason", "forced"}, "data")
            previous = _counter(data["previous_epoch"], "previous_epoch", positive=True)
            new = _counter(data["new_epoch"], "new_epoch", positive=True)
            if new != previous + 1 or not isinstance(data["forced"], bool):
                _fail("invalid takeover epochs or forced flag")
            _string(data["reason"], "reason", nonempty=True)
        elif kind == "worker-relaunched":
            _fields(data, {"previous_epoch", "new_epoch", "reason"}, "data")
            previous = _counter(data["previous_epoch"], "previous_epoch", positive=True)
            new = _counter(data["new_epoch"], "new_epoch", positive=True)
            if new != previous + 1:
                _fail("worker relaunch epochs must be consecutive")
            _string(data["reason"], "reason", nonempty=True)
        elif kind == "stop":
            _fields(data, {"reason"}, "data")
            _string(data["reason"], "reason", nonempty=True)
        elif kind == "pause":
            _fields(data, {"reason"}, "data")
            _string(data["reason"], "reason", nonempty=True)
    else:
        if kind == "checkpoint":
            _fields(data, {"state", "stage", "current_activity", "inbox_cursor", "commitments"}, "data")
            if data["state"] not in {"working", "succeeded", "stopped", "paused"}:
                _fail("invalid checkpoint state")
            _string(data["stage"], "stage", max_bytes=256)
            _string(data["current_activity"], "current_activity", max_bytes=2048)
            _counter(data["inbox_cursor"], "inbox_cursor")
            _distinct_strings(data["commitments"], "commitments")
        elif kind == "question":
            _fields(data, {"blocking", "stage", "current_activity", "inbox_cursor", "commitments"}, "data")
            if not isinstance(data["blocking"], bool):
                _fail("blocking must be boolean")
            _string(data["stage"], "stage", max_bytes=256)
            _string(data["current_activity"], "current_activity", max_bytes=2048)
            _counter(data["inbox_cursor"], "inbox_cursor")
            _distinct_strings(data["commitments"], "commitments")
        elif kind == "result":
            _fields(data, {"head", "dirty", "stage", "inbox_cursor", "verification", "commitments"}, "data")
            if data["head"] is not None:
                _string(data["head"], "head", nonempty=True)
            if not isinstance(data["dirty"], bool):
                _fail("dirty must be boolean")
            _string(data["stage"], "stage", max_bytes=256)
            _counter(data["inbox_cursor"], "inbox_cursor")
            _validate_verification(data["verification"])
            _distinct_strings(data["commitments"], "commitments")
        elif kind == "error":
            _fields(data, {"fatal", "category", "stage", "inbox_cursor", "commitments"}, "data")
            if not isinstance(data["fatal"], bool):
                _fail("fatal must be boolean")
            _string(data["category"], "category", nonempty=True)
            _string(data["stage"], "stage", max_bytes=256)
            _counter(data["inbox_cursor"], "inbox_cursor")
            _distinct_strings(data["commitments"], "commitments")
            _string(body, "body", nonempty=True)


def _validate_message(value: Any, direction: str, *, expected_seq: int | None = None) -> None:
    value = _fields(value, MESSAGE_FIELDS, "message")
    if value["protocol_version"] != 1:
        _fail("unsupported message protocol version")
    seq = _counter(value["seq"], "seq", positive=True)
    if expected_seq is not None and seq != expected_seq:
        _fail(f"non-contiguous sequence: expected {expected_seq}, got {seq}")
    _uuid4(value["message_id"], "message_id")
    _counter(value["writer_epoch"], "writer_epoch", positive=True)
    _parse_time(value["ts"])
    allowed = INBOX_TYPES if direction == "inbox" else OUTBOX_TYPES
    if value["type"] not in allowed:
        _fail(f"invalid {direction} message type")
    if value["reply_to"] is not None:
        _uuid4(value["reply_to"], "reply_to")
    _string(value["body"], "body", max_bytes=MAX_BODY)
    if not isinstance(value["attachments"], list) or len(value["attachments"]) > MAX_ATTACHMENTS:
        _fail("attachments must be a list of at most 128 items")
    for attachment in value["attachments"]:
        _validate_attachment(attachment)
    _validate_type_data(direction, value["type"], value["body"], value["reply_to"], value["data"])


def _journal(run_dir: Path, direction: str) -> list[dict[str, Any]]:
    path = run_dir / f"{direction}.jsonl"
    role = "orchestrator" if direction == "inbox" else "worker"
    with _lock(run_dir, role, shared=True):
        return _journal_unlocked(path, direction)


def _journal_unlocked(path: Path, direction: str) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HandoffError(f"cannot read journal {path}: {exc}", 6) from exc
    if raw and not raw.endswith(b"\n"):
        _fail(f"journal has unterminated final record: {path}", 5)
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for seq, line in enumerate(raw.splitlines(), 1):
        if len(line) + 1 > MAX_JOURNAL_LINE:
            _fail(f"journal line {seq} exceeds size limit", 5)
        value = _decode_json(line, f"{path}:{seq}")
        if (
            direction == "inbox" and isinstance(value, dict)
            and value.get("type") == "coordinator-takeover"
        ):
            _pre_orchestrator_rename("coordinator-takeover", path)
        try:
            _validate_message(value, direction, expected_seq=seq)
        except HandoffError as exc:
            raise HandoffError(f"invalid journal record {path}:{seq}: {exc}", 5) from exc
        if value["message_id"] in seen_ids:
            _fail(f"duplicate message_id in {path}", 5)
        seen_ids.add(value["message_id"])
        records.append(value)
    return records


def _git(workspace: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args], capture_output=True, text=True,
            timeout=10, env=env.build_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HandoffError(f"Git probe failed: {exc}", 6) from exc
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _sanitize_remote(remote: str | None) -> str | None:
    if remote is None:
        return None
    parsed = urllib.parse.urlsplit(remote)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        if parsed.port:
            host += f":{parsed.port}"
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    # SCP-like remotes may contain user@host:path.
    if ":" in remote and "@" in remote.split(":", 1)[0]:
        return remote.split("@", 1)[1]
    return remote


def _file_metadata(workspace: Path, value: str, *, max_size: int | None = None) -> tuple[Path, dict[str, Any]]:
    relative = _relative_path(value)
    path = workspace / PurePosixPath(relative)
    try:
        cursor = workspace
        for component in PurePosixPath(relative).parts:
            cursor = cursor / component
            if cursor.is_symlink():
                _fail(f"attachment/input path contains a symlink: {relative}")
        if not path.is_file():
            _fail(f"attachment/input is not a regular non-symlink file: {relative}")
        resolved = path.resolve(strict=True)
        resolved.relative_to(workspace)
        size = resolved.stat().st_size
    except ValueError as exc:
        raise HandoffError(f"path escapes workspace: {relative}", 2) from exc
    except OSError as exc:
        raise HandoffError(f"cannot inspect workspace file {relative}: {exc}", 6) from exc
    if max_size is not None and size > max_size:
        _fail(f"file {relative} exceeds {max_size} bytes")
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    blob = _git(workspace, "ls-files", "-s", "--", relative)
    blob_id = blob.split()[1] if blob and len(blob.split()) >= 2 else None
    return resolved, {"path": relative, "sha256": digest, "size": size, "git_blob": blob_id}


def _standing_instructions() -> str:
    helper = f"{shlex.quote(sys.executable)} -m agents.orchestration.handoffctl"
    return f"""# Handoff worker contract

- You are a delegated worker in an orchestrator-managed run. Independently execute the stated task within scope; report durable progress, results, and blockers through Handoff. Route scope changes, cross-worker coordination, final acceptance, integration, and user-policy decisions to the orchestrator.
- Read `run.json`, `control.json`, and all unread inbox messages before beginning; check again after resume or compaction, at every turn or stage boundary, before an irreversible action, before a commit, and before publishing a result.
- Immediately after the initial read, run the exact `handoffctl ready` command below before substantive work. This is the worker-ready signal; do not inspect helper source or help first.
- Process inbox records in sequence and advance `inbox_cursor` only after the instruction and any durable reminder it creates are recorded.
- Treat a `supersede` message tied to the current result as a new instruction that replaces the pending review; consume it, checkpoint back to `working`, and publish a fresh result when done.
- Use `handoffctl emit` for checkpoints, questions, results, and errors. Checkpoints durably update `status.json` and `progress.md`.

{EMIT_DATA_SCHEMAS}
- Assume the orchestrator knows the kickoff but not your live progress. Every blocking question must be self-contained: state the current stage and completed work, concrete evidence, the exact conflict, the decision or authority needed, your recommended resolution, the consequences of the available options, and actions intentionally deferred. Do not rely on earlier checkpoints to supply this context.
- End the turn after a blocking question. On completion publish an exact result and await review. After a successful result publication, `handoffctl` attempts a best-effort cmux notification for cmux-launched workers; the durable result remains authoritative if notification delivery fails.
- After consuming an accepted review, emit a `paused` checkpoint and idle awaiting further instructions — a doorbell will arrive with the next one. Resume from paused only after consuming a `steer`, `answer`, `supersede`, `input-changed`, or `base-changed` newer than the accepted review: checkpoint back to `working` and continue. There is no lifecycle message and no exit checkpoint: the run ends when the orchestrator marks it finished and reaps the session.
- A dirty Git workspace does not block work or result publication. Preserve unrelated pre-existing changes and report the current `HEAD` and dirty state truthfully in every result.
- Put cross-stage commitments in checkpoint data so they survive context compaction.

Helper: `{helper}`
Ready command: `{helper} ready --run-dir "$HANDOFF_RUN_DIR"`
Run directory: available as `HANDOFF_RUN_DIR`. Worker credential: available only through `HANDOFF_WORKER_TOKEN_FILE`.
"""


def initialize(
    *, workspace: Path, kickoff: Path, harness: str, model: str, transport: str,
    recovery_token_file: Path, orchestrator_token_file: Path, worker_token_file: Path,
    effort: str | None = None, inputs: list[Path | str] | None = None,
    state_root: Path | None = None, run_dir: Path | None = None,
) -> dict[str, Any]:
    """Create a completely initialized run directory and three credential files."""
    workspace = Path(workspace).resolve(strict=True)
    kickoff = Path(kickoff).resolve(strict=True)
    if not workspace.is_dir():
        _fail("workspace must be a directory")
    if not kickoff.is_file() or kickoff.is_symlink() or kickoff.stat().st_size > MAX_KICKOFF:
        _fail("kickoff must be a regular non-symlink file at most 1 MiB")
    if not IDENT_RE.fullmatch(harness) or not IDENT_RE.fullmatch(transport):
        _fail("harness and transport must be lowercase identifiers")
    _string(model, "model", nonempty=True)

    repo_text = _git(workspace, "rev-parse", "--show-toplevel")
    repo = Path(repo_text).resolve() if repo_text else workspace
    run_id = str(uuid.uuid4())
    if run_dir is not None:
        final = Path(run_dir)
        if not final.is_absolute():
            _fail("--run-dir must be absolute")
        final = final.resolve(strict=False)
    else:
        if state_root is None:
            configured = os.environ.get("HANDOFF_STATE_DIR")
            if configured:
                state_root = Path(configured)
            elif os.environ.get("XDG_STATE_HOME"):
                state_root = Path(os.environ["XDG_STATE_HOME"]) / "agents/handoff"
            else:
                state_root = Path.home() / ".local/state/agents/handoff"
        state_root = Path(state_root).resolve(strict=False)
        repo_key = f"{repo.name}-{hashlib.sha256(str(repo).encode()).hexdigest()[:12]}"
        final = state_root / repo_key / "runs" / run_id
    if final.exists():
        _fail(f"run directory already exists: {final}", 4)
    final.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(final.parent, 0o700)

    credential_paths = [Path(value).resolve(strict=False) for value in (
        recovery_token_file, orchestrator_token_file, worker_token_file
    )]
    if len(set(credential_paths)) != 3:
        _fail("credential file paths must be distinct")
    for path in credential_paths:
        try:
            path.relative_to(final)
        except ValueError:
            pass
        else:
            _fail("credential files must be outside the run directory")

    created_credentials: list[Path] = []
    temp = final.parent / f".{run_id}.init-{uuid.uuid4()}"
    try:
        recovery = _new_token_file(credential_paths[0]); created_credentials.append(credential_paths[0])
        orchestrator = _new_token_file(credential_paths[1]); created_credentials.append(credential_paths[1])
        worker_token = _new_token_file(credential_paths[2]); created_credentials.append(credential_paths[2])
        temp.mkdir(mode=0o700)
        os.chmod(temp, 0o700)
        created = _timestamp()
        lease = _timestamp(_parse_time(created) + datetime.timedelta(seconds=LEASE_SECONDS))
        normalized_inputs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in inputs or []:
            candidate = Path(item)
            if candidate.is_absolute():
                try:
                    relative = candidate.absolute().relative_to(workspace).as_posix()
                except ValueError as exc:
                    raise HandoffError(f"input is outside workspace: {item}", 2) from exc
            else:
                relative = PurePosixPath(str(item)).as_posix()
            _, metadata = _file_metadata(workspace, relative)
            if relative not in seen:
                normalized_inputs.append(metadata)
                seen.add(relative)
        remote = _sanitize_remote(_git(workspace, "remote", "get-url", "origin"))
        run = {
            "protocol_version": 1, "run_id": run_id, "created_at": created,
            "repo": {"path": str(repo), "remote": remote},
            "workspace": {"path": str(workspace), "host": socket.gethostname(), "base_commit": _git(workspace, "rev-parse", "HEAD")},
            "worker": {"harness": harness, "model": model, "effort": effort, "transport": transport},
            "inputs": normalized_inputs,
        }
        status_value = _initial_status(run_id, created)
        control_value = {
            "protocol_version": 1, "run_id": run_id, "revision": 1,
            "orchestrator_epoch": 1, "orchestrator_token_sha256": _token_hash(orchestrator),
            "recovery_token_sha256": _token_hash(recovery), "orchestrator_lease_expires_at": lease,
            "worker_epoch": 1, "worker_token_sha256": _token_hash(worker_token),
            "review_state": "pending", "review_result_id": None,
            "last_command_seq": 0, "outbox_cursor": 0,
            "integration": {"state": "pending", "commit": None, "reason": None}, "updated_at": created,
        }
        _validate_run(run); _validate_status(status_value); _validate_control(control_value)
        _write_new(temp / "run.json", _json_bytes(run))
        kickoff_bytes = kickoff.read_bytes()
        kickoff_content = _standing_instructions().encode() + b"\n--- submitted kickoff ---\n\n" + kickoff_bytes
        _write_new(temp / "kickoff.md", kickoff_content)
        _write_new(temp / "control.json", _json_bytes(control_value))
        _write_new(temp / "status.json", _json_bytes(status_value))
        _write_new(temp / "inbox.jsonl", b"")
        _write_new(temp / "outbox.jsonl", b"")
        _write_new(temp / "progress.md", f"# Handoff run {run_id}\n\nRun ID: `{run_id}`\n".encode())
        _write_new(temp / "orchestrator.lock", b"")
        _write_new(temp / "worker.lock", b"")
        for name in ("attachments", "repairs"):
            (temp / name).mkdir(mode=0o700)
        _fsync_dir(temp)
        os.rename(temp, final)
        _fsync_dir(final.parent)
    except BaseException:
        if temp.exists():
            shutil.rmtree(temp)
        for path in created_credentials:
            with contextlib.suppress(OSError):
                path.unlink()
        raise
    return {
        "run_dir": str(final),
        "credential_files": {"recovery": str(credential_paths[0]), "orchestrator": str(credential_paths[1]), "worker": str(credential_paths[2])},
        "run": run, "status": status_value, "control": control_value,
    }


def _initial_status(run_id: str, timestamp: str, *, epoch: int = 1, inbox_cursor: int = 0, outbox_seq: int = 0) -> dict[str, Any]:
    return {
        "protocol_version": 1, "run_id": run_id, "revision": 1, "worker_epoch": epoch,
        "state": "starting", "stage": "startup", "inbox_cursor": inbox_cursor,
        "outbox_seq": outbox_seq, "blocked_on": [],
        "current_activity": "Reading kickoff and run state", "last_progress_at": None,
        "updated_at": timestamp,
    }


def read(run_dir: Path, journal: str, *, after: int = 0, through: int | None = None) -> list[dict[str, Any]]:
    run_dir = _run_dir(run_dir)
    if journal not in {"inbox", "outbox"}:
        _fail("journal must be inbox or outbox")
    _counter(after, "after")
    records = _journal(run_dir, journal)
    through = len(records) if through is None else _counter(through, "through")
    if through > len(records):
        _fail("through exceeds journal tail", 4)
    if through <= after:
        return []
    return records[after:through]


def status(run_dir: Path) -> dict[str, Any]:
    return _load_json(_run_dir(run_dir) / "status.json", _validate_status)


def ready(run_dir: Path, token: bytes) -> dict[str, Any]:
    """Publish the canonical startup checkpoint, safely retrying the command."""
    run_dir = _run_dir(run_dir)
    run = _load_json(run_dir / "run.json", _validate_run)
    digest = bytearray(hashlib.sha256(f"{run['run_id']}:worker-ready".encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    message_id = str(uuid.UUID(bytes=bytes(digest)))
    existing = next(
        (message for message in _journal(run_dir, "outbox") if message["message_id"] == message_id),
        None,
    )
    data = existing["data"] if existing is not None else {
        "state": "working",
        "stage": "startup",
        "current_activity": "Beginning kickoff work",
        "inbox_cursor": len(_journal(run_dir, "inbox")),
        "commitments": [],
    }
    return emit(
        run_dir,
        token,
        type="checkpoint",
        message_id=message_id,
        body="Worker initialized and beginning kickoff work",
        data=data,
    )


def control_show(run_dir: Path) -> dict[str, Any]:
    return _load_json(_run_dir(run_dir) / "control.json", _validate_control)


def _attachments(run_dir: Path, message_id: str, declarations: list[str], snapshot: bool) -> list[dict[str, Any]]:
    run = _load_json(run_dir / "run.json", _validate_run)
    workspace = Path(run["workspace"]["path"])
    if len(declarations) > MAX_ATTACHMENTS:
        _fail("too many attachments")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    destination_dir = run_dir / "attachments" / message_id
    for declaration in declarations:
        source, metadata = _file_metadata(workspace, declaration, max_size=MAX_ATTACHMENT)
        if metadata["path"] in seen:
            _fail("attachment paths must be distinct")
        seen.add(metadata["path"])
        metadata["snapshot"] = None
        if snapshot:
            destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(destination_dir, 0o700)
            destination = destination_dir / f"{len(result)+1:03d}-{source.name}"
            _write_new(destination, source.read_bytes(), 0o600)
            metadata["snapshot"] = destination.relative_to(run_dir).as_posix()
        result.append(metadata)
    if snapshot:
        _fsync_dir(destination_dir)
        _fsync_dir(destination_dir.parent)
    return result


def _new_message(seq: int, message_id: str, epoch: int, kind: str, reply_to: str | None, body: str, data: dict[str, Any], attachments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "protocol_version": 1, "seq": seq, "message_id": message_id,
        "writer_epoch": epoch, "ts": _timestamp(), "type": kind,
        "reply_to": reply_to, "body": body, "data": data, "attachments": attachments,
    }


def _same_request(message: dict[str, Any], kind: str, reply_to: str | None, body: str, data: dict[str, Any], attachments: list[dict[str, Any]]) -> bool:
    return all((
        message["type"] == kind, message["reply_to"] == reply_to,
        message["body"] == body, message["data"] == data,
        message["attachments"] == attachments,
    ))


def _resolve_reply(records: list[dict[str, Any]], message_id: str | None, kind: str | None = None) -> dict[str, Any] | None:
    if message_id is None:
        return None
    for record in records:
        if record["message_id"] == message_id and (kind is None or record["type"] == kind):
            return record
    return None


def _renewed(control: dict[str, Any], now: datetime.datetime | None = None) -> None:
    now = now or _now()
    control["orchestrator_lease_expires_at"] = _timestamp(now + datetime.timedelta(seconds=LEASE_SECONDS))
    control["updated_at"] = _timestamp(now)
    control["revision"] += 1


def _apply_inbox(control: dict[str, Any], message: dict[str, Any]) -> None:
    kind = message["type"]
    if kind == "review":
        control["review_state"] = message["data"]["disposition"]
        control["review_result_id"] = message["reply_to"]
    elif kind == "supersede":
        control["review_state"] = "superseded"
        control["review_result_id"] = message["reply_to"]
    elif kind == "integration":
        control["integration"] = dict(message["data"])
    elif kind == "orchestrator-takeover":
        control["orchestrator_epoch"] = message["data"]["new_epoch"]
    elif kind == "worker-relaunched":
        control["worker_epoch"] = message["data"]["new_epoch"]
    # Legacy "stop"/"pause" records project nothing: the registry finished
    # marker replaced the desired_state projection they fed.
    control["last_command_seq"] = message["seq"]


def send(
    run_dir: Path, token: bytes, *, type: str, body: str = "", data: dict[str, Any] | None = None,
    reply_to: str | None = None, message_id: str | None = None,
    attachments: list[str] | None = None, snapshot_attachments: bool = False,
) -> dict[str, Any]:
    run_dir = _run_dir(run_dir)
    if type not in SENDABLE_TYPES:
        _fail("generic send does not accept this message type")
    data = {} if data is None else data
    body = _string(body, "body", max_bytes=MAX_BODY)
    message_id = message_id or str(uuid.uuid4()); _uuid4(message_id, "message_id")
    if reply_to is not None: _uuid4(reply_to, "reply_to")
    _validate_type_data("inbox", type, body, reply_to, data)
    with _lock(run_dir, "orchestrator"):
        control = _load_json(run_dir / "control.json", _validate_control)
        _authorize(control, token, "orchestrator", lease=True)
        inbox = _journal_unlocked(run_dir / "inbox.jsonl", "inbox")
        outbox = _journal(run_dir, "outbox")
        # Deliverability rule: work messages are deliverable whenever the
        # worker epoch is live, regardless of the integration record.  The
        # per-result review terminality below is the guard that actually
        # protects the integration invariant.
        if type == "answer" and _resolve_reply(outbox, reply_to, "question") is None:
            _fail("answer reply_to must name an outbox question", 4)
        if type in {"review", "supersede"}:
            if reply_to != control["review_result_id"] or _resolve_reply(outbox, reply_to, "result") is None:
                _fail(f"{type} must reply to the current result", 4)
            if control["integration"]["state"] != "pending":
                _fail(f"cannot {type} after integration decision", 4)
            worker_status = _load_json(run_dir / "status.json", _validate_status)
            if type == "supersede" and worker_status["state"] != "awaiting_review":
                _fail("supersede requires an awaiting_review worker", 4)
            if type == "review" and control["review_state"] == "superseded":
                _fail("superseded result cannot be reviewed", 4)
            if (
                type == "review"
                and worker_status["state"] in {"paused", "succeeded"}
                and data["disposition"] == "changes_requested"
            ):
                _fail("changes requested after an accepted review require worker rotation or a new run", 4)
        existing = _resolve_reply(inbox, message_id)
        declarations = [_relative_path(item) for item in (attachments or [])]
        if existing:
            if [item["path"] for item in existing["attachments"]] != declarations:
                _fail("message_id was reused with different attachment declarations", 4)
            if declarations and bool(existing["attachments"][0]["snapshot"]) != snapshot_attachments:
                _fail("message_id was reused with a different snapshot policy", 4)
            metadata = existing["attachments"]
        else:
            metadata = _attachments(run_dir, message_id, declarations, snapshot_attachments)
        if type == "input-changed":
            matching = [item for item in metadata if item["path"] == data["path"] and item["sha256"] == data["sha256"]]
            if not matching:
                # Current workspace file is also a valid named attachment source.
                _, current = _file_metadata(Path(_load_json(run_dir / "run.json", _validate_run)["workspace"]["path"]), data["path"])
                if current["sha256"] != data["sha256"]:
                    _fail("input-changed path/digest does not match workspace or attachment")
        if type == "base-changed":
            run = _load_json(run_dir / "run.json", _validate_run)
            if _git(Path(run["repo"]["path"]), "rev-parse", f"{data['base_commit']}^{{commit}}") is None:
                _fail("base-changed commit does not resolve in the launch repository", 4)
        if existing:
            if not _same_request(existing, type, reply_to, body, data, metadata):
                _fail("message_id was reused with different content", 4)
            if control["last_command_seq"] < existing["seq"]:
                for pending in inbox[control["last_command_seq"]:existing["seq"]]:
                    _apply_inbox(control, pending)
            _renewed(control); _atomic_json(run_dir / "control.json", control)
            return {"message": existing, "control": control}
        if control["last_command_seq"] != len(inbox):
            _fail("control projection trails inbox; retry the prior message or repair control", 4)
        message = _new_message(len(inbox)+1, message_id, control["orchestrator_epoch"], type, reply_to, body, data, metadata)
        _validate_message(message, "inbox", expected_seq=len(inbox)+1)
        _append(run_dir / "inbox.jsonl", message)
        _apply_inbox(control, message); _renewed(control)
        _atomic_json(run_dir / "control.json", control)
        return {"message": message, "control": control}


def _inbox_prefix(run_dir: Path, through: int) -> list[dict[str, Any]]:
    records = _journal(run_dir, "inbox")
    if through > len(records):
        _fail("inbox_cursor exceeds inbox tail", 4)
    return records[:through]


def _unresolved_questions(status_value: dict[str, Any], inbox: list[dict[str, Any]]) -> list[str]:
    answered = {message["reply_to"] for message in inbox if message["type"] == "answer"}
    stopped = any(message["type"] == "stop" for message in inbox)
    if stopped:
        return []
    return [message_id for message_id in status_value["blocked_on"] if message_id not in answered]


def _last_result(outbox: list[dict[str, Any]], epoch: int) -> dict[str, Any] | None:
    results = [message for message in outbox if message["writer_epoch"] == epoch and message["type"] == "result"]
    return results[-1] if results else None


def _derived_fatal(outbox: list[dict[str, Any]], worker_epoch: int) -> bool:
    """Whether the given worker epoch ended on a durable fatal error.

    The fatal fact is derived from the outbox, never stored: epoch-scoped by
    construction, so a ``worker-relaunched`` epoch bump falsifies it with no
    explicit clear.
    """
    return any(
        message["type"] == "error"
        and message["data"].get("fatal")
        and message["writer_epoch"] == worker_epoch
        for message in outbox
    )


def is_fatal(run_dir: Path) -> bool:
    """Whether the run's live worker epoch ended on a durable fatal error."""
    run_dir = _run_dir(run_dir)
    control = _load_json(run_dir / "control.json", _validate_control)
    return _derived_fatal(_journal(run_dir, "outbox"), control["worker_epoch"])


def _matching_review(inbox: list[dict[str, Any]], result_id: str, disposition: str) -> bool:
    reviews = [
        message for message in inbox
        if message["type"] == "review" and message["reply_to"] == result_id
    ]
    return bool(reviews and reviews[-1]["data"]["disposition"] == disposition)


def _project_worker(status_value: dict[str, Any], message: dict[str, Any], inbox: list[dict[str, Any]], outbox_before: list[dict[str, Any]], control: dict[str, Any]) -> dict[str, Any]:
    data = message["data"]
    cursor = data["inbox_cursor"]
    if cursor < status_value["inbox_cursor"] or cursor > len(inbox):
        _fail("worker inbox cursor cannot decrease or exceed the journal tail", 4)
    prefix = inbox[:cursor]
    unresolved = _unresolved_questions(status_value, prefix)
    if cursor > status_value["inbox_cursor"] and status_value["blocked_on"] and unresolved:
        _fail("cannot advance across an unanswered blocking question", 4)
    old_state = status_value["state"]
    kind = message["type"]
    new_state = old_state
    stage = data["stage"]
    activity = status_value["current_activity"]
    blocked_on = list(status_value["blocked_on"])
    if _derived_fatal(outbox_before, status_value["worker_epoch"]):
        # A fatal error ends the worker epoch: nothing further may be emitted
        # until worker-relaunched rotates the epoch.  Keyed on the durable
        # outbox fact, never on a cached state name.
        _fail("worker epoch is terminal", 4)
    # Legacy self-reported terminal states normalize onto ``paused`` for
    # transition purposes: succeeded and stopped were both "parked after an
    # orchestrator decision", and a legacy failed epoch is caught by the
    # derived-fatal guard above (its fatal error sits in the journal).
    old = "paused" if old_state in {"succeeded", "stopped"} else old_state
    if kind == "checkpoint":
        target = data["state"]
        activity = data["current_activity"]
        if target == "working":
            if old in {"starting", "working"}:
                new_state = "working"
            elif old == "blocked" and not unresolved:
                new_state = "working"; blocked_on = []
            elif old == "awaiting_review":
                result = _last_result(outbox_before, status_value["worker_epoch"])
                changed = result is not None and _matching_review(prefix, result["message_id"], "changes_requested")
                superseded = result is not None and any(
                    item["type"] == "supersede" and item["reply_to"] == result["message_id"]
                    for item in prefix
                )
                if not changed and not superseded:
                    _fail("awaiting_review can resume only after matching changes_requested or supersede", 4)
                new_state = "working"
            elif old == "paused":
                # Resume on any work message newer than the accepted review
                # that idled the worker.  A legacy run parked by a mid-work
                # ``stop`` has no such review and stays parked.
                result = _last_result(outbox_before, status_value["worker_epoch"])
                idling_seq = max(
                    (
                        item["seq"] for item in prefix
                        if result is not None
                        and item["type"] == "review"
                        and item["reply_to"] == result["message_id"]
                        and item["data"]["disposition"] == "accepted"
                    ),
                    default=None,
                )
                resumed = idling_seq is not None and any(
                    item["type"] in WORK_MESSAGE_TYPES and item["seq"] > idling_seq
                    for item in prefix
                )
                if not resumed:
                    _fail("paused can resume only after a work message newer than the accepted review", 4)
                new_state = "working"
            else:
                _fail(f"invalid checkpoint transition {old_state} -> working", 4)
        elif target == "paused":
            if old not in {"awaiting_review", "paused"}:
                _fail("paused requires awaiting_review", 4)
            result = _last_result(outbox_before, status_value["worker_epoch"])
            if result is None or not _matching_review(prefix, result["message_id"], "accepted"):
                _fail("paused requires a consumed matching accepted review", 4)
            new_state = "paused"; blocked_on = []
        elif target == "succeeded":
            # Legacy emission, accepted for frozen pre-collapse contracts and
            # historical journals; it maps onto paused.
            if old not in {"awaiting_review", "paused"}:
                _fail("succeeded requires awaiting_review", 4)
            result = _last_result(outbox_before, status_value["worker_epoch"])
            if result is None or not _matching_review(prefix, result["message_id"], "accepted"):
                _fail("succeeded requires a consumed matching accepted review", 4)
            new_state = "paused"; blocked_on = []
        elif target == "stopped":
            # Legacy emission: it acknowledged a consumed orchestrator stop,
            # which only legacy journals contain.  It maps onto paused.
            if not any(item["type"] == "stop" for item in prefix):
                _fail("stopped requires a consumed orchestrator stop", 4)
            new_state = "paused"; blocked_on = []
    elif kind == "question":
        activity = data["current_activity"]
        if data["blocking"]:
            if old not in {"starting", "working"}:
                _fail("blocking question is not valid in the current state", 4)
            new_state = "blocked"; blocked_on.append(message["message_id"])
    elif kind == "result":
        if old not in {"starting", "working", "blocked"} or unresolved:
            _fail("result is not valid in the current state", 4)
        new_state = "awaiting_review"; blocked_on = []
        activity = "Awaiting orchestrator review"
    elif kind == "error" and data["fatal"]:
        # Fatality is derived from the journal, not stored as a state: the
        # projected state stays whatever it was.
        blocked_on = []
        activity = message["body"]
    result_status = dict(status_value)
    result_status.update({
        "revision": status_value["revision"] + 1, "state": new_state, "stage": stage,
        "inbox_cursor": cursor, "outbox_seq": message["seq"], "blocked_on": blocked_on,
        "current_activity": activity, "updated_at": message["ts"],
    })
    if not (kind == "question" and not data["blocking"]):
        result_status["last_progress_at"] = message["ts"]
    _validate_status(result_status)
    return result_status


def _progress_section(message: dict[str, Any], state: str) -> bytes:
    data = message["data"]
    commitments = data.get("commitments", [])
    lines = [
        "", f"## Checkpoint <!-- outbox-seq:{message['seq']} -->", "",
        f"- Timestamp: {message['ts']}", f"- State: {state}",
        f"- Stage: {data['stage']}", f"- Summary: {message['body']}",
        f"- Consumed inbox cursor: {data['inbox_cursor']}", "- Commitments:",
    ]
    lines.extend(f"  - {item}" for item in commitments)
    if not commitments: lines.append("  - None")
    return ("\n".join(lines) + "\n").encode()


def _append_progress(run_dir: Path, message: dict[str, Any], state: str) -> None:
    marker = f"<!-- outbox-seq:{message['seq']} -->".encode()
    path = run_dir / "progress.md"
    current = path.read_bytes()
    if marker in current:
        return
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        payload = _progress_section(message, state); written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _legacy_checkpoint_contract(run_dir: Path) -> bool:
    """Whether this run froze a pre-collapse worker checkpoint schema.

    Migration removes ``control.desired_state``, so that retired cache cannot
    remain the only skew marker.  The generated kickoff is immutable protocol
    evidence: old contracts contain a checkpoint schema line naming both
    ``succeeded`` and ``stopped``; current contracts do not.  Match one schema
    line rather than arbitrary kickoff prose so a user task discussing those
    words does not accidentally enable legacy writes.
    """
    try:
        lines = (run_dir / "kickoff.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(
        line.startswith("- checkpoint:")
        and '"succeeded"' in line
        and '"stopped"' in line
        for line in lines
    )


def emit(
    run_dir: Path, token: bytes, *, type: str, body: str = "", data: dict[str, Any] | None = None,
    reply_to: str | None = None, message_id: str | None = None,
    attachments: list[str] | None = None, snapshot_attachments: bool = False,
) -> dict[str, Any]:
    run_dir = _run_dir(run_dir)
    if type not in OUTBOX_TYPES:
        _fail("invalid outbox type")
    data = {} if data is None else data
    body = _string(body, "body", max_bytes=MAX_BODY)
    message_id = message_id or str(uuid.uuid4()); _uuid4(message_id, "message_id")
    if reply_to is not None: _uuid4(reply_to, "reply_to")
    _validate_type_data("outbox", type, body, reply_to, data)
    # Always acquire roles in orchestrator-then-worker order. Orchestrator sends
    # may consult outbox while holding the orchestrator lock; the inverse order
    # here would deadlock two simultaneous role operations.
    with _lock(run_dir, "orchestrator", shared=True), _lock(run_dir, "worker"):
        control = _load_json(run_dir / "control.json", _validate_control)
        _authorize(control, token, "worker")
        if (
            type == "checkpoint"
            and data["state"] in {"succeeded", "stopped"}
            and "desired_state" not in control
            and not _legacy_checkpoint_contract(run_dir)
        ):
            # Retired checkpoint states stay readable forever and remain
            # writable only for pre-collapse runs whose frozen worker
            # contract still names them.  New runs omit the legacy
            # desired_state field and must use working/paused exclusively.
            _fail("new runs cannot emit a retired checkpoint state", 4)
        status_value = _load_json(run_dir / "status.json", _validate_status)
        if status_value["worker_epoch"] != control["worker_epoch"]:
            _fail("worker snapshot epoch does not match control", 4)
        outbox = _journal_unlocked(run_dir / "outbox.jsonl", "outbox")
        inbox = _journal_unlocked(run_dir / "inbox.jsonl", "inbox")
        existing = _resolve_reply(outbox, message_id)
        declarations = [_relative_path(item) for item in (attachments or [])]
        if existing:
            if [item["path"] for item in existing["attachments"]] != declarations:
                _fail("message_id was reused with different attachment declarations", 4)
            if declarations and bool(existing["attachments"][0]["snapshot"]) != snapshot_attachments:
                _fail("message_id was reused with a different snapshot policy", 4)
            metadata = existing["attachments"]
        else:
            metadata = _attachments(run_dir, message_id, declarations, snapshot_attachments)
        if existing:
            if not _same_request(existing, type, reply_to, body, data, metadata):
                _fail("message_id was reused with different content", 4)
            if status_value["outbox_seq"] < existing["seq"]:
                for pending in outbox[status_value["outbox_seq"]:existing["seq"]]:
                    status_value = _project_worker(status_value, pending, inbox, outbox[:pending["seq"]-1], control)
                _atomic_json(run_dir / "status.json", status_value)
                for pending in outbox[:existing["seq"]]:
                    if pending["type"] == "checkpoint": _append_progress(run_dir, pending, pending["data"]["state"])
            return {"message": existing, "status": status_value}
        if status_value["outbox_seq"] != len(outbox):
            _fail("status projection trails outbox; retry the prior message or repair status", 4)
        if type == "result":
            run = _load_json(run_dir / "run.json", _validate_run)
            workspace = Path(run["workspace"]["path"])
            head = _git(workspace, "rev-parse", "HEAD")
            if head is not None:
                dirty = bool(_git(workspace, "status", "--porcelain"))
                if data["head"] != head:
                    _fail("Git result must name current HEAD", 4)
                if data["dirty"] != dirty:
                    _fail("Git result dirty flag must match the current workspace", 4)
        message = _new_message(len(outbox)+1, message_id, control["worker_epoch"], type, reply_to, body, data, metadata)
        _validate_message(message, "outbox", expected_seq=len(outbox)+1)
        projected = _project_worker(status_value, message, inbox, outbox, control)
        _append(run_dir / "outbox.jsonl", message)
        _atomic_json(run_dir / "status.json", projected)
        if type == "checkpoint": _append_progress(run_dir, message, projected["state"])
        _fsync_dir(run_dir)
        return {"message": message, "status": projected}


def control_consume(run_dir: Path, token: bytes, *, through: int) -> dict[str, Any]:
    run_dir = _run_dir(run_dir); through = _counter(through, "through")
    with _lock(run_dir, "orchestrator"):
        control = _load_json(run_dir / "control.json", _validate_control)
        _authorize(control, token, "orchestrator", lease=True)
        outbox = _journal(run_dir, "outbox")
        if through > len(outbox): _fail("through exceeds outbox tail", 4)
        if through > control["outbox_cursor"]:
            for message in outbox[control["outbox_cursor"]:through]:
                if message["type"] == "result":
                    control["review_state"] = "pending"
                    control["review_result_id"] = message["message_id"]
                    control["integration"] = {"state": "pending", "commit": None, "reason": None}
            control["outbox_cursor"] = through
        _renewed(control)
        _atomic_json(run_dir / "control.json", control)
        return {"control": control}


def control_renew(run_dir: Path, token: bytes) -> dict[str, Any]:
    run_dir = _run_dir(run_dir)
    with _lock(run_dir, "orchestrator"):
        control = _load_json(run_dir / "control.json", _validate_control)
        _authorize(control, token, "orchestrator", lease=True)
        _renewed(control); _atomic_json(run_dir / "control.json", control)
        return {"control": control}


def control_release(run_dir: Path, token: bytes) -> dict[str, Any]:
    run_dir = _run_dir(run_dir)
    with _lock(run_dir, "orchestrator"):
        control = _load_json(run_dir / "control.json", _validate_control)
        _authorize(control, token, "orchestrator", lease=True)
        now = _now(); control["orchestrator_lease_expires_at"] = _timestamp(now)
        control["updated_at"] = _timestamp(now); control["revision"] += 1
        _atomic_json(run_dir / "control.json", control)
        return {"control": control}


def _system_send(
    run_dir: Path, control: dict[str, Any], inbox: list[dict[str, Any]], *,
    kind: str, body: str, data: dict[str, Any], reply_to: str | None,
    epoch: int | None = None,
) -> dict[str, Any]:
    message = _new_message(
        len(inbox)+1, str(uuid.uuid4()), epoch or control["orchestrator_epoch"],
        kind, reply_to, body, data, [],
    )
    _validate_message(message, "inbox", expected_seq=len(inbox)+1)
    _append(run_dir / "inbox.jsonl", message)
    _apply_inbox(control, message)
    return message


def control_integrate(run_dir: Path, token: bytes, *, commit: str) -> dict[str, Any]:
    return _control_decision(run_dir, token, state="integrated", commit=commit, reason=None)


def control_abandon(run_dir: Path, token: bytes, *, reason: str, commit: str | None = None) -> dict[str, Any]:
    return _control_decision(run_dir, token, state="abandoned", commit=commit, reason=reason)


def _control_decision(run_dir: Path, token: bytes, *, state: str, commit: str | None, reason: str | None) -> dict[str, Any]:
    run_dir = _run_dir(run_dir)
    if state == "integrated": _string(commit, "commit", nonempty=True)
    else: _string(reason, "reason", nonempty=True)
    with _lock(run_dir, "orchestrator"):
        control = _load_json(run_dir / "control.json", _validate_control)
        _authorize(control, token, "orchestrator", lease=True)
        if control["integration"]["state"] != "pending": _fail("integration decision is terminal", 4)
        if control["review_result_id"] is None: _fail("no worker result is available", 4)
        if state == "integrated" and control["review_state"] != "accepted":
            _fail("integration requires acceptance of the current result", 4)
        inbox = _journal_unlocked(run_dir / "inbox.jsonl", "inbox")
        data = {"state": state, "commit": commit, "reason": reason}
        summary = f"Result {state}" if state == "integrated" else f"Result abandoned: {reason}"
        message = _system_send(run_dir, control, inbox, kind="integration", body=summary, data=data, reply_to=control["review_result_id"])
        _renewed(control); _atomic_json(run_dir / "control.json", control)
        return {"message": message, "control": control}


def control_takeover(
    run_dir: Path, recovery_token: bytes, *, new_token_file: Path, reason: str,
    force: bool = False,
) -> dict[str, Any]:
    run_dir = _run_dir(run_dir); reason = _string(reason, "reason", nonempty=True)
    new_path = Path(new_token_file).resolve(strict=False)
    try:
        new_path.relative_to(run_dir)
    except ValueError: pass
    else: _fail("new credential file must be outside run directory")
    created = False
    with _lock(run_dir, "orchestrator"):
        control = _load_json(run_dir / "control.json", _validate_control)
        if not secrets.compare_digest(_token_hash(recovery_token), control["recovery_token_sha256"]):
            _fail("invalid recovery credential", 3)
        now = _now()
        if not force and _parse_time(control["orchestrator_lease_expires_at"]) > now:
            _fail("orchestrator lease is still active", 4)
        token = _new_token_file(new_path); created = True
        try:
            inbox = _journal_unlocked(run_dir / "inbox.jsonl", "inbox")
            previous = control["orchestrator_epoch"]; next_epoch = previous + 1
            data = {"previous_epoch": previous, "new_epoch": next_epoch, "reason": reason, "forced": force}
            message = _system_send(
                run_dir, control, inbox, kind="orchestrator-takeover",
                body=f"Orchestrator ownership transferred: {reason}", data=data,
                reply_to=None, epoch=next_epoch,
            )
            control["orchestrator_token_sha256"] = _token_hash(token)
            control["orchestrator_epoch"] = next_epoch
            control["orchestrator_lease_expires_at"] = _timestamp(now + datetime.timedelta(seconds=LEASE_SECONDS))
            control["updated_at"] = _timestamp(now); control["revision"] += 1
            _atomic_json(run_dir / "control.json", control)
        except BaseException:
            # Once the journal append exists the token is required for explicit repair.
            tail = _journal_unlocked(run_dir / "inbox.jsonl", "inbox")
            appended = bool(tail and tail[-1]["type"] == "orchestrator-takeover" and tail[-1]["data"].get("new_epoch") == locals().get("next_epoch"))
            if created and not appended:
                with contextlib.suppress(OSError): new_path.unlink()
            raise
        return {"message": message, "control": control, "new_token_file": str(new_path)}


def control_rotate_worker(
    run_dir: Path, token: bytes, *, new_token_file: Path, reason: str,
    confirmed_dead: bool,
) -> dict[str, Any]:
    if not confirmed_dead: _fail("worker rotation requires --confirmed-dead", 4)
    run_dir = _run_dir(run_dir); reason = _string(reason, "reason", nonempty=True)
    new_path = Path(new_token_file).resolve(strict=False)
    try: new_path.relative_to(run_dir)
    except ValueError: pass
    else: _fail("new credential file must be outside run directory")
    with _lock(run_dir, "orchestrator"):
        with _lock(run_dir, "worker"):
            control = _load_json(run_dir / "control.json", _validate_control)
            _authorize(control, token, "orchestrator", lease=True)
            if control["integration"]["state"] != "pending":
                _fail("worker rotation is not allowed after a terminal integration decision", 4)
            status_value = _load_json(run_dir / "status.json", _validate_status)
            inbox = _journal_unlocked(run_dir / "inbox.jsonl", "inbox")
            outbox = _journal_unlocked(run_dir / "outbox.jsonl", "outbox")
            worker_token = _new_token_file(new_path)
            previous = control["worker_epoch"]; next_epoch = previous + 1
            data = {"previous_epoch": previous, "new_epoch": next_epoch, "reason": reason}
            try:
                message = _system_send(
                    run_dir, control, inbox, kind="worker-relaunched",
                    body=f"Worker relaunched after confirmed death: {reason}", data=data,
                    reply_to=None,
                )
                now = _timestamp()
                control["worker_epoch"] = next_epoch
                control["worker_token_sha256"] = _token_hash(worker_token)
                _renewed(control)
                rotated_status = _initial_status(
                    control["run_id"], now, epoch=next_epoch,
                    inbox_cursor=status_value["inbox_cursor"], outbox_seq=len(outbox),
                )
                rotated_status["revision"] = status_value["revision"] + 1
                _atomic_json(run_dir / "control.json", control)
                _atomic_json(run_dir / "status.json", rotated_status)
            except BaseException:
                # As with takeover, preserve a token required to complete an appended rotation.
                if not (run_dir / "inbox.jsonl").read_bytes().endswith(_json_bytes(locals().get("message", {}), compact=True)):
                    with contextlib.suppress(OSError): new_path.unlink()
                raise
            return {"message": message, "control": control, "status": rotated_status, "new_token_file": str(new_path)}


def _issue(code: str, path: Path | str, detail: str) -> dict[str, str]:
    return {"code": code, "path": str(path), "detail": detail}


def _doctor_report(run_dir: Path) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    required_files = ["run.json", "kickoff.md", "control.json", "status.json", "inbox.jsonl", "outbox.jsonl", "progress.md", "orchestrator.lock", "worker.lock"]
    try:
        if stat.S_IMODE(run_dir.stat().st_mode) != 0o700:
            issues.append(_issue("mode", run_dir, "run directory mode must be 0700"))
    except OSError as exc:
        issues.append(_issue("io", run_dir, str(exc)))
    for name in required_files:
        path = run_dir / name
        try:
            if not path.is_file(): issues.append(_issue("missing", path, "required file is missing")); continue
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                issues.append(_issue("mode", path, "file mode must be 0600"))
        except OSError as exc: issues.append(_issue("io", path, str(exc)))
    for name in ("attachments", "repairs"):
        path = run_dir / name
        try:
            if not path.is_dir(): issues.append(_issue("missing", path, "required directory is missing")); continue
            if stat.S_IMODE(path.stat().st_mode) != 0o700:
                issues.append(_issue("mode", path, "directory mode must be 0700"))
        except OSError as exc: issues.append(_issue("io", path, str(exc)))
    run = control = status_value = None
    for name, validator in (("run.json", _validate_run), ("control.json", _validate_control), ("status.json", _validate_status)):
        try:
            value = _load_json(run_dir / name, validator)
            if (run_dir / name).read_bytes() != _json_bytes(value):
                issues.append(_issue("serialization", run_dir / name, "snapshot is not canonical sorted two-space JSON with a trailing newline"))
            if name == "run.json": run = value
            elif name == "control.json": control = value
            else: status_value = value
        except HandoffError as exc: issues.append(_issue("snapshot", run_dir / name, str(exc)))
    journals: dict[str, list[dict[str, Any]]] = {}
    for direction in ("inbox", "outbox"):
        path = run_dir / f"{direction}.jsonl"
        try: journals[direction] = _journal(run_dir, direction)
        except HandoffError as exc:
            code = "partial-tail" if "unterminated final" in str(exc) else "journal"
            issues.append(_issue(code, path, str(exc)))
    if run and control and status_value:
        for path, value in (("control.json", control), ("status.json", status_value)):
            if value["run_id"] != run["run_id"]:
                issues.append(_issue("run-id", run_dir / path, "run_id differs from run.json"))
        if control["worker_epoch"] != status_value["worker_epoch"]:
            issues.append(_issue("epoch", run_dir / "status.json", "worker epoch differs from control"))
    inbox = journals.get("inbox", []); outbox = journals.get("outbox", [])
    outbox_by_id = {item["message_id"]: item for item in outbox}
    for message in inbox:
        if message["type"] == "answer" and (message["reply_to"] not in outbox_by_id or outbox_by_id[message["reply_to"]]["type"] != "question"):
            issues.append(_issue("reference", run_dir / "inbox.jsonl", f"answer seq {message['seq']} has invalid reply_to"))
        if message["type"] in {"review", "supersede", "integration"} and (message["reply_to"] not in outbox_by_id or outbox_by_id[message["reply_to"]]["type"] != "result"):
            issues.append(_issue("reference", run_dir / "inbox.jsonl", f"{message['type']} seq {message['seq']} has invalid reply_to"))
    if control:
        if control["last_command_seq"] > len(inbox): issues.append(_issue("cursor", run_dir / "control.json", "last_command_seq exceeds inbox tail"))
        if control["outbox_cursor"] > len(outbox): issues.append(_issue("cursor", run_dir / "control.json", "outbox_cursor exceeds outbox tail"))
        if control["last_command_seq"] < len(inbox): issues.append(_issue("projection-lag", run_dir / "control.json", "control projection trails inbox"))
        if control["last_command_seq"] == len(inbox):
            expected_orchestrator_epoch = 1
            expected_worker_epoch = 1
            expected_integration = {"state": "pending", "commit": None, "reason": None}
            for message in inbox:
                if message["type"] == "orchestrator-takeover": expected_orchestrator_epoch = message["data"]["new_epoch"]
                elif message["type"] == "worker-relaunched": expected_worker_epoch = message["data"]["new_epoch"]
                elif message["type"] == "integration": expected_integration = dict(message["data"])
            consumed_results = [message for message in outbox[:control["outbox_cursor"]] if message["type"] == "result"]
            expected_result = consumed_results[-1]["message_id"] if consumed_results else None
            expected_review = "pending"
            if expected_result:
                decisions = [
                    message for message in inbox
                    if message["type"] in {"review", "supersede"} and message["reply_to"] == expected_result
                ]
                if decisions:
                    latest = decisions[-1]
                    expected_review = (
                        "superseded" if latest["type"] == "supersede"
                        else latest["data"]["disposition"]
                    )
            semantic = {
                "orchestrator_epoch": expected_orchestrator_epoch,
                "worker_epoch": expected_worker_epoch, "review_state": expected_review,
                "review_result_id": expected_result, "integration": expected_integration,
            }
            for field, expected in semantic.items():
                if control[field] != expected:
                    issues.append(_issue("control-projection", run_dir / "control.json", f"{field} does not match journal replay"))
    if status_value:
        if status_value["inbox_cursor"] > len(inbox): issues.append(_issue("cursor", run_dir / "status.json", "inbox_cursor exceeds inbox tail"))
        if status_value["outbox_seq"] > len(outbox): issues.append(_issue("cursor", run_dir / "status.json", "outbox_seq exceeds outbox tail"))
        if status_value["outbox_seq"] < len(outbox): issues.append(_issue("projection-lag", run_dir / "status.json", "status projection trails outbox"))
        if status_value["worker_epoch"] == 1 and status_value["outbox_seq"] == len(outbox) and control:
            try:
                replayed = _initial_status(run["run_id"], run["created_at"])
                for message in outbox:
                    replayed = _project_worker(replayed, message, inbox, outbox[:message["seq"]-1], control)
                comparable = STATUS_FIELDS - {"updated_at", "state"}
                mismatched = any(replayed[field] != status_value[field] for field in comparable)
                # Legacy terminal self-reports compare through their collapsed
                # meaning: succeeded/stopped both map onto paused, and a legacy
                # failed matches any state the replay reached before the fatal
                # error (fatality is derived from the journal, not a state).
                expected_state = status_value["state"]
                if expected_state in {"succeeded", "stopped"}:
                    expected_state = "paused"
                if replayed["state"] != expected_state:
                    legacy_fatal = (
                        status_value["state"] == "failed"
                        and _derived_fatal(outbox, status_value["worker_epoch"])
                    )
                    if not legacy_fatal:
                        mismatched = True
                if mismatched:
                    issues.append(_issue("status-projection", run_dir / "status.json", "status does not match full outbox replay"))
            except HandoffError as exc:
                issues.append(_issue("transition", run_dir / "outbox.jsonl", str(exc)))
    # Verify snapshots and immutable workspace attachments.
    for direction, records in journals.items():
        for message in records:
            for item in message["attachments"]:
                target = run_dir / item["snapshot"] if item["snapshot"] else (Path(run["workspace"]["path"]) / item["path"] if run else None)
                if target is None: continue
                try:
                    raw = target.read_bytes()
                    if len(raw) != item["size"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                        issues.append(_issue("attachment", target, f"digest or size mismatch for {direction} seq {message['seq']}"))
                except OSError as exc: issues.append(_issue("attachment", target, str(exc)))
    try:
        progress = (run_dir / "progress.md").read_text(encoding="utf-8")
        markers = re.findall(r"<!-- outbox-seq:(\d+) -->", progress)
        if len(markers) != len(set(markers)):
            issues.append(_issue("progress-marker", run_dir / "progress.md", "duplicate outbox sequence marker"))
        expected = {str(message["seq"]) for message in outbox if message["type"] == "checkpoint"}
        missing = sorted(expected - set(markers), key=int)
        if missing: issues.append(_issue("projection-lag", run_dir / "progress.md", f"missing checkpoint markers: {','.join(missing)}"))
    except (OSError, UnicodeError) as exc: issues.append(_issue("progress", run_dir / "progress.md", str(exc)))
    return {"ok": not issues, "repaired": False, "issues": issues, "actions": []}


def doctor(run_dir: Path) -> dict[str, Any]:
    return _doctor_report(_run_dir(run_dir))


def _write_repair_report(run_dir: Path, report: dict[str, Any], name: str) -> dict[str, Any]:
    repairs = run_dir / "repairs"; repairs.mkdir(mode=0o700, exist_ok=True); os.chmod(repairs, 0o700)
    path = repairs / f"{_timestamp().replace(':','').replace('.','')}-{name}.json"
    _write_new(path, _json_bytes(report)); _fsync_dir(repairs)
    return report


def repair_tail(run_dir: Path, journal: str, token: bytes) -> dict[str, Any]:
    run_dir = _run_dir(run_dir)
    if journal not in {"inbox", "outbox"}: _fail("journal must be inbox or outbox")
    role = "orchestrator" if journal == "inbox" else "worker"
    with _lock(run_dir, role):
        control = _load_json(run_dir / "control.json", _validate_control)
        _authorize(control, token, role, lease=(role == "orchestrator"))
        path = run_dir / f"{journal}.jsonl"; raw = path.read_bytes()
        try:
            _journal_unlocked(path, journal)
        except HandoffError:
            pass
        else: _fail("journal does not have a repairable tail", 4)
        last_newline = raw.rfind(b"\n")
        prefix = raw[:last_newline+1] if last_newline >= 0 else b""
        tail = raw[last_newline+1:]
        # If the final record has a newline, locate the last line and permit only
        # invalid syntax/UTF-8.  Semantically invalid complete records are fatal.
        if not tail and raw:
            lines = raw.splitlines(keepends=True); candidate = lines[-1]; prefix = b"".join(lines[:-1]); tail = candidate
            try:
                prefix_probe = run_dir / "repairs" / ".prefix-probe-complete"
                _write_new(prefix_probe, prefix)
                valid_prefix = _journal_unlocked(prefix_probe, journal)
                prefix_probe.unlink()
            except BaseException as exc:
                with contextlib.suppress(OSError): prefix_probe.unlink()
                raise HandoffError("corruption before final record cannot be repaired", 5) from exc
            try:
                decoded = _decode_json(candidate.rstrip(b"\n"), str(path))
                _validate_message(decoded, journal, expected_seq=len(valid_prefix) + 1)
            except HandoffError as exc:
                if "invalid JSON" not in str(exc): _fail("valid but semantically invalid tail cannot be repaired", 5)
            else: _fail("journal tail is valid", 4)
        try:
            temp_probe = run_dir / "repairs" / ".prefix-probe"
            _write_new(temp_probe, prefix)
            _journal_unlocked(temp_probe, journal)
            temp_probe.unlink()
        except BaseException as exc:
            with contextlib.suppress(OSError): temp_probe.unlink()
            raise HandoffError("corruption before final record cannot be repaired", 5) from exc
        repairs = run_dir / "repairs"; repairs.mkdir(mode=0o700, exist_ok=True)
        quarantine = repairs / f"{_timestamp().replace(':','')}-{journal}-tail.bin"
        _write_new(quarantine, tail)
        fd = os.open(path, os.O_WRONLY)
        try: os.ftruncate(fd, len(prefix)); os.fsync(fd)
        finally: os.close(fd)
        _journal_unlocked(path, journal)
        report = {"ok": True, "repaired": True, "issues": [], "actions": [_issue("tail-quarantined", quarantine, f"removed {len(tail)} trailing bytes from {journal}")]}
        return _write_repair_report(run_dir, report, f"repair-{journal}-tail")


def repair_status(run_dir: Path, token: bytes) -> dict[str, Any]:
    run_dir = _run_dir(run_dir)
    with _lock(run_dir, "orchestrator", shared=True), _lock(run_dir, "worker"):
        control = _load_json(run_dir / "control.json", _validate_control); _authorize(control, token, "worker")
        current = _load_json(run_dir / "status.json", _validate_status)
        outbox = _journal_unlocked(run_dir / "outbox.jsonl", "outbox"); inbox = _journal_unlocked(run_dir / "inbox.jsonl", "inbox")
        actions: list[dict[str, str]] = []
        if current["worker_epoch"] > control["worker_epoch"]:
            _fail("status worker epoch is ahead of control", 5)
        if current["worker_epoch"] < control["worker_epoch"]:
            baseline = max((message["seq"] for message in outbox if message["writer_epoch"] < control["worker_epoch"]), default=current["outbox_seq"])
            rotated = _initial_status(
                control["run_id"], _timestamp(), epoch=control["worker_epoch"],
                inbox_cursor=current["inbox_cursor"], outbox_seq=baseline,
            )
            rotated["revision"] = current["revision"] + 1
            current = rotated
            actions.append(_issue("status-epoch-reset", run_dir / "status.json", f"reset projection for worker epoch {control['worker_epoch']}"))
        if current["outbox_seq"] > len(outbox): _fail("status is ahead of outbox", 5)
        repaired = current
        for message in outbox[current["outbox_seq"]:]:
            if message["writer_epoch"] != current["worker_epoch"]: continue
            repaired = _project_worker(repaired, message, inbox, outbox[:message["seq"]-1], control)
        if repaired != current or actions:
            _atomic_json(run_dir / "status.json", repaired)
            if repaired != current:
                actions.append(_issue("status-replayed", run_dir / "status.json", f"replayed through outbox seq {repaired['outbox_seq']}"))
        report = {"ok": True, "repaired": bool(actions), "issues": [], "actions": actions}
        return _write_repair_report(run_dir, report, "repair-status")


def repair_control(
    run_dir: Path, token: bytes, *, recovery: bool = False,
    new_token: bytes | None = None, new_worker_token: bytes | None = None,
) -> dict[str, Any]:
    run_dir = _run_dir(run_dir)
    with _lock(run_dir, "orchestrator"):
        control = _load_json(run_dir / "control.json", _validate_control)
        inbox = _journal_unlocked(run_dir / "inbox.jsonl", "inbox")
        pending = inbox[control["last_command_seq"]:]
        lagging_takeover = bool(pending and pending[0]["type"] == "orchestrator-takeover")
        if recovery:
            if not lagging_takeover or not secrets.compare_digest(_token_hash(token), control["recovery_token_sha256"]): _fail("recovery credential is not valid for this repair", 3)
        else: _authorize(control, token, "orchestrator", lease=True)
        actions: list[dict[str, str]] = []
        for message in pending:
            _apply_inbox(control, message)
            if message["type"] == "orchestrator-takeover":
                if new_token is None:
                    _fail("repairing an appended takeover requires its new orchestrator token file", 3)
                control["orchestrator_token_sha256"] = _token_hash(new_token)
            if message["type"] == "worker-relaunched":
                if new_worker_token is None:
                    _fail("repairing an appended worker rotation requires its new worker token file", 3)
                control["worker_token_sha256"] = _token_hash(new_worker_token)
        if control["last_command_seq"] < len(inbox): _fail("control replay did not reach inbox tail", 5)
        if lagging_takeover and recovery:
            control["orchestrator_lease_expires_at"] = _timestamp(_now() + datetime.timedelta(seconds=LEASE_SECONDS))
        control["revision"] += 1; control["updated_at"] = _timestamp()
        _atomic_json(run_dir / "control.json", control)
        actions.append(_issue("control-replayed", run_dir / "control.json", f"replayed through inbox seq {control['last_command_seq']}"))
        report = {"ok": True, "repaired": True, "issues": [], "actions": actions}
        return _write_repair_report(run_dir, report, "repair-control")


def repair_progress(run_dir: Path, token: bytes) -> dict[str, Any]:
    run_dir = _run_dir(run_dir)
    with _lock(run_dir, "worker"):
        control = _load_json(run_dir / "control.json", _validate_control); _authorize(control, token, "worker")
        outbox = _journal_unlocked(run_dir / "outbox.jsonl", "outbox")
        progress = (run_dir / "progress.md").read_bytes(); actions: list[dict[str, str]] = []
        for message in outbox:
            marker = f"<!-- outbox-seq:{message['seq']} -->".encode()
            if message["type"] == "checkpoint" and marker not in progress:
                _append_progress(run_dir, message, message["data"]["state"]); progress += marker
                actions.append(_issue("progress-appended", run_dir / "progress.md", f"appended checkpoint marker {message['seq']}"))
        report = {"ok": True, "repaired": bool(actions), "issues": [], "actions": actions}
        return _write_repair_report(run_dir, report, "repair-progress")


# ``init`` is the protocol noun/CLI spelling. Keep ``initialize`` as the
# descriptive implementation name while exposing the normative library verb.
init = initialize
