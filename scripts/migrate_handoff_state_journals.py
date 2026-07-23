#!/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python
"""Migrate persisted handoff state to journal-derived lifecycle semantics.

The migration is cumulative and idempotent:

- map legacy ``succeeded``/``stopped`` status snapshots to ``paused``;
- remove the retired ``control.desired_state`` cache; and
- add/backfill each registry record's explicit ``finished_at`` marker.

Remote proxy records are reconciled from the owning host.  An unreachable
host is reported and left unmodified so a later run can retry without guessing.
Before the first write, the complete state tree is copied to a timestamped
sibling backup.  ``--dry-run`` reports the same plan without writing or taking
a backup.  Results are emitted as one JSON object on stdout.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from agents.orchestration import handoff_launcher


STATE_DIR = Path.home() / ".local/state/agents/handoff"
LEGACY_IDLE_STATES = {"succeeded", "stopped"}

REMOTE_PROBE_PROGRAM = r"""
import json
import sys
from pathlib import Path
from agents.orchestration import handoff
from agents.orchestration import handoff_registry

request = json.load(sys.stdin)
result = {"finished_at": None, "desired_state": None}
try:
    record = handoff_registry.resolve(request["run_id"])
    result["finished_at"] = record.get("finished_at")
except Exception:
    pass
try:
    control = handoff.control_show(Path(request["run_dir"]))
    result["desired_state"] = control.get("desired_state")
except Exception:
    pass
print(json.dumps(result, sort_keys=True))
"""


def _timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dump(value: object, *, compact: bool) -> bytes:
    if compact:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return text.encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            Path(temporary).unlink()
        raise


def _read_object(path: Path, warnings: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"{path}: unreadable JSON ({exc}); skipped")
        return None
    if not isinstance(value, dict):
        warnings.append(f"{path}: not a JSON object; skipped")
        return None
    return value


def _probe_remote_finished(record: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    result = handoff_launcher._run_ssh(  # noqa: SLF001 - shared SSH safety discipline
        record["host"],
        [record["remote_python"], "-c", REMOTE_PROBE_PROGRAM],
        stdin=json.dumps({"run_id": record["run_id"], "run_dir": record["run_dir"]}),
        timeout=timeout,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise handoff_launcher.AdapterError("remote migration probe returned invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"finished_at", "desired_state"}:
        raise handoff_launcher.AdapterError("remote migration probe returned invalid fields")
    if value["finished_at"] is not None and not isinstance(value["finished_at"], str):
        raise handoff_launcher.AdapterError("remote migration probe returned invalid finished_at")
    if value["desired_state"] not in {None, "run", "pause", "stop"}:
        raise handoff_launcher.AdapterError("remote migration probe returned invalid desired_state")
    return value


def _collect_plan(
    state_dir: Path, *, remote_timeout: float,
) -> tuple[list[tuple[str, Path, bytes]], list[str]]:
    warnings: list[str] = []
    plan: list[tuple[str, Path, bytes]] = []
    legacy_desired_by_run_dir: dict[str, str] = {}

    for path in sorted(state_dir.rglob("control.json")):
        value = _read_object(path, warnings)
        if value is None or "desired_state" not in value:
            continue
        desired = value.pop("desired_state")
        if desired in {"run", "pause", "stop"}:
            legacy_desired_by_run_dir[str(path.parent.resolve())] = desired
        else:
            warnings.append(f"{path}: invalid desired_state {desired!r}; key removed")
        plan.append(("control", path, _dump(value, compact=False)))

    for path in sorted(state_dir.rglob("status.json")):
        value = _read_object(path, warnings)
        if value is None or value.get("state") not in LEGACY_IDLE_STATES:
            continue
        value["state"] = "paused"
        plan.append(("status", path, _dump(value, compact=False)))

    registry_path = state_dir / "registry.json"
    if registry_path.exists():
        registry = _read_object(registry_path, warnings)
        if registry is not None and isinstance(registry.get("runs"), dict):
            changed = False
            migration_time = _timestamp()
            for raw_record in registry["runs"].values():
                if not isinstance(raw_record, dict):
                    continue
                # Presence, not truthiness, is the migration sentinel.  New
                # registries deliberately store ``finished_at: null`` for
                # active runs; rewriting that same null on every invocation
                # would make the migration non-idempotent and keep taking
                # pointless backups forever.
                if "finished_at" in raw_record:
                    continue
                host = raw_record.get("host")
                if host is None:
                    run_dir = raw_record.get("run_dir")
                    desired = None
                    if isinstance(run_dir, str):
                        desired = legacy_desired_by_run_dir.get(str(Path(run_dir).resolve()))
                    raw_record["finished_at"] = migration_time if desired == "stop" else None
                    changed = True
                    continue
                if not all(
                    isinstance(raw_record.get(field), str) and raw_record[field]
                    for field in ("run_id", "run_dir", "host", "remote_python")
                ):
                    warnings.append(
                        f"registry remote record {raw_record.get('run_id')!r}: "
                        "missing probe fields; skipped",
                    )
                    continue
                try:
                    remote = _probe_remote_finished(raw_record, timeout=remote_timeout)
                except handoff_launcher.AdapterError as exc:
                    warnings.append(
                        f"registry remote run {raw_record['run_id']} on {host}: "
                        f"owning host unreachable or invalid ({exc}); skipped",
                    )
                    continue
                raw_record["finished_at"] = (
                    remote["finished_at"]
                    or (migration_time if remote["desired_state"] == "stop" else None)
                )
                changed = True
            if changed:
                plan.append(("registry", registry_path, _dump(registry, compact=True)))
        elif registry is not None:
            warnings.append(f"{registry_path}: runs is not an object; skipped")

    return plan, warnings


def migrate(
    state_dir: Path, *, dry_run: bool, remote_timeout: float,
) -> dict[str, Any]:
    if not state_dir.is_dir():
        raise ValueError(f"state directory does not exist: {state_dir}")
    plan, warnings = _collect_plan(state_dir, remote_timeout=remote_timeout)
    changes = [
        {"category": category, "path": str(path)}
        for category, path, _ in plan
    ]
    backup: Path | None = None
    if plan and not dry_run:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = state_dir.with_name(f"{state_dir.name}.pre-journal-migration-{stamp}")
        shutil.copytree(state_dir, backup, symlinks=True, copy_function=shutil.copy2)
        for _, path, payload in plan:
            _atomic_write(path, payload)
    return {
        "dry_run": dry_run,
        "state_dir": str(state_dir),
        "backup": str(backup) if backup is not None else None,
        "change_count": len(changes),
        "changes": changes,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate handoff state to journal-derived lifecycle semantics.",
        epilog=(
            "Example: migrate_handoff_state_journals.py --dry-run "
            "--state-dir /tmp/handoff-state"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR, help="handoff state root")
    parser.add_argument(
        "--remote-timeout", type=float, default=30.0,
        help="seconds allowed for each remote owning-host reconciliation (default: 30)",
    )
    args = parser.parse_args(argv)
    if args.remote_timeout <= 0:
        parser.error("--remote-timeout must be positive")
    try:
        report = migrate(
            args.state_dir.resolve(), dry_run=args.dry_run,
            remote_timeout=args.remote_timeout,
        )
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    for warning in report["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
