#!/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python
"""One-shot, idempotent migration of persisted handoff state to the
"orchestrator" spellings (Phase 2 of the coordinator -> orchestrator rename).

Rewrites, under ~/.local/state/agents/handoff/ (override with --state-dir):

  * registry.json          run-record key  coordinator_id -> orchestrator_id
  * */watcher.json         top-level key   coordinator_id -> orchestrator_id
  * */control.json         top-level keys  coordinator_epoch,
                           coordinator_token_sha256,
                           coordinator_lease_expires_at -> orchestrator_*
  * */inbox.jsonl          event type      "type":"coordinator-takeover"
                           -> "type":"orchestrator-takeover"
  * coordinator.lock       renamed to      orchestrator.lock

Deliberately NOT touched: coordinator.token credential files and the
coordinators/ session directory itself (registry credential_dir paths still
point there), free-text prose in kickoff.md/progress.md/message bodies, and
repair reports.

Safety: before modifying anything, the whole state tree is copied to a
timestamped sibling directory whose path is printed.  JSON files are rewritten
atomically (temp file + os.replace) with the original file mode preserved.
Re-running is a no-op: files already using the new spellings are left alone.

  --dry-run   report exactly what would change; write nothing (no backup).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path.home() / ".local/state/agents/handoff"

CONTROL_KEYS = {
    "coordinator_epoch": "orchestrator_epoch",
    "coordinator_token_sha256": "orchestrator_token_sha256",
    "coordinator_lease_expires_at": "orchestrator_lease_expires_at",
}
ID_KEY = {"coordinator_id": "orchestrator_id"}
TAKEOVER_OLD = b'"type":"coordinator-takeover"'
TAKEOVER_NEW = b'"type":"orchestrator-takeover"'

CATEGORIES = ("registry", "watcher", "control", "inbox", "lock")


def _dump(value: object, *, compact: bool) -> bytes:
    # Matches the protocol writers: registry.json is compact, watcher.json and
    # control.json are indent=2; both sort keys and keep UTF-8 literal.
    if compact:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return text.encode("utf-8")


def _migrate_mapping(mapping: dict, keymap: dict, path: Path, warnings: list[str]) -> bool:
    """Rename keymap keys in one dict.  Returns True when anything changed."""
    changed = False
    for old, new in keymap.items():
        if old not in mapping:
            continue
        if new in mapping:
            warnings.append(f"{path}: both {old!r} and {new!r} present; file left untouched")
            return False
        mapping[new] = mapping.pop(old)
        changed = True
    return changed


def _json_rewrite(path: Path, keymap: dict, *, compact: bool, per_record: bool,
                  warnings: list[str]) -> bytes | None:
    """Return the migrated file bytes, or None when the file needs no change."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"{path}: unreadable JSON ({exc}); skipped")
        return None
    if per_record:
        # No any()/all() short-circuit: every record must be migrated.
        changed = False
        for record in (value.get("runs") or {}).values():
            if isinstance(record, dict):
                changed = _migrate_mapping(record, keymap, path, warnings) or changed
    else:
        if not isinstance(value, dict):
            warnings.append(f"{path}: not a JSON object; skipped")
            return None
        changed = _migrate_mapping(value, keymap, path, warnings)
    return _dump(value, compact=compact) if changed else None


def _inbox_rewrite(path: Path, warnings: list[str]) -> bytes | None:
    raw = path.read_bytes()
    if b"coordinator-takeover" not in raw:
        return None
    # Rewrite only when every occurrence is the exact serialized type key; an
    # occurrence anywhere else (e.g. inside a message body) makes the byte
    # replacement unsafe, so the file is reported and skipped instead.
    if raw.count(b"coordinator-takeover") != raw.count(TAKEOVER_OLD):
        warnings.append(f"{path}: 'coordinator-takeover' outside the type key; skipped")
        return None
    for line in raw.splitlines():
        if TAKEOVER_OLD not in line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"{path}: unparseable journal line; skipped")
            return None
        if not isinstance(message, dict) or message.get("type") != "coordinator-takeover":
            warnings.append(f"{path}: type key mismatch on journal line; skipped")
            return None
    return raw.replace(TAKEOVER_OLD, TAKEOVER_NEW)


def _atomic_write(path: Path, payload: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(temporary).unlink()
        except OSError:
            pass
        raise


def _collect_plan(state_dir: Path, warnings: list[str]) -> list[tuple[str, Path, object]]:
    """Return (category, path, payload) items.  Payload is new bytes for
    rewrites or the destination Path for lock renames."""
    plan: list[tuple[str, Path, object]] = []
    for path in sorted(p for p in state_dir.rglob("*") if p.is_file()):
        if path.name == "registry.json" and path.parent == state_dir:
            payload = _json_rewrite(path, ID_KEY, compact=True, per_record=True, warnings=warnings)
            if payload is not None:
                plan.append(("registry", path, payload))
        elif path.name == "watcher.json":
            payload = _json_rewrite(path, ID_KEY, compact=False, per_record=False, warnings=warnings)
            if payload is not None:
                plan.append(("watcher", path, payload))
        elif path.name == "control.json":
            payload = _json_rewrite(path, CONTROL_KEYS, compact=False, per_record=False, warnings=warnings)
            if payload is not None:
                plan.append(("control", path, payload))
        elif path.name == "inbox.jsonl":
            payload = _inbox_rewrite(path, warnings)
            if payload is not None:
                plan.append(("inbox", path, payload))
        elif path.name == "coordinator.lock":
            target = path.with_name("orchestrator.lock")
            if target.exists():
                warnings.append(f"{path}: {target.name} already exists; lock left untouched")
                continue
            plan.append(("lock", path, target))
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    args = parser.parse_args()
    state_dir = args.state_dir
    if not state_dir.is_dir():
        print(f"error: state directory does not exist: {state_dir}", file=sys.stderr)
        return 2

    warnings: list[str] = []
    plan = _collect_plan(state_dir, warnings)
    counts = {category: 0 for category in CATEGORIES}
    for category, _, _ in plan:
        counts[category] += 1

    mode = "DRY RUN — would change" if args.dry_run else "changed"
    for category, path, payload in plan:
        if category == "lock":
            print(f"{mode}: {category}: {path} -> {payload}")
        else:
            print(f"{mode}: {category}: {path}")
    print("summary: " + ", ".join(f"{c}={counts[c]}" for c in CATEGORIES)
          + f" (total {len(plan)} file(s))")

    if args.dry_run or not plan:
        if not args.dry_run:
            print("nothing to migrate; no backup taken")
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        return 0

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = state_dir.with_name(f"{state_dir.name}.pre-orchestrator-migration-{stamp}")
    shutil.copytree(state_dir, backup, symlinks=True, copy_function=shutil.copy2)
    print(f"backup: {backup}")

    for category, path, payload in plan:
        if category == "lock":
            os.rename(path, payload)
        else:
            _atomic_write(path, payload)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"done: migrated {len(plan)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
