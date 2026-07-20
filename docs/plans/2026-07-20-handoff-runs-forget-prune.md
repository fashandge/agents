# Task: add `runs forget` / `runs prune` to the handoff registry

**Status:** not started — spec for a delegated worker or an inline change.
Written 2026-07-20. **Do this only after the Phase 2 orchestrator rename
(`docs/plans/2026-07-19-orchestrator-rename-handoff.md`) has landed and been
verified**, so the key names are settled and the two changes stay separately
reviewable and revertable.

## Problem

The private run registry (`~/.local/state/agents/handoff/registry.json`,
override `HANDOFF_REGISTRY_FILE`) is append-only in practice. `handoffctl runs`
exposes only `list`, `show`, and `adopt` — there is no supported way to remove
a record. Consequences, in order of importance:

1. **One bad record breaks everything.** `handoff_registry._validate()` runs
   over *every* record on *every* read and raises on the first failure. A
   single malformed, truncated, or hand-edited entry therefore takes down
   `runs list`, `runs show`, `context`, `dispatch`, `watch`, and every launch
   simultaneously — the registry is read on all of those paths. Today the only
   remedy is hand-editing `registry.json`, which the handoff docs explicitly
   forbid and which is the worst thing to be doing while something is broken.
   A supported removal path is the escape hatch for exactly this.
2. **Unbounded growth with O(n) validation.** 25 records had accumulated within
   a few weeks of use (2026-07-20), essentially all from completed runs, and
   every one is re-validated on every registry read.
3. **Stale pointers.** Records keep `run_dir`, `run_uri`, and `credential_dir`
   values that may reference directories that no longer exist, including
   credential directories for long-finished runs.
4. **Migration surface.** The Phase 2 rename had to rewrite the key in all 25
   records; any future schema change pays the same per-record cost.

## Design

Registry-only, conservative, and non-destructive to durable evidence.

### `handoffctl runs forget --run <selector>`

- Resolves the selector the same way `runs show` does (run ID, unique prefix,
  name, handle, or URI) and removes exactly that one record.
- **Refuses when the run is not terminal** — i.e. its `status.json`/
  `control.json` show a live/awaiting state — unless `--force` is given. This
  is the key safety property: forgetting a live run orphans a running worker by
  discarding the only pointer to it.
- If the run directory is unreadable or missing, treat the run as terminal for
  this check (the record is a dangling pointer, which is exactly what should be
  removable) but say so in the output.
- Never deletes the run directory, journals, or credential files.
- Prints the removed record (public projection — `credential_dir` stays
  redacted per `handoff_registry.PRIVATE_FIELDS`).

### `handoffctl runs prune [filters] [--dry-run]`

- Bulk form of the same operation. Filters: `--older-than <days>` (against
  `registered_at`), `--terminal-only` (default true), and `--host <name>`.
- `--dry-run` lists what would be removed and changes nothing. Given this is
  the destructive-ish path, print the plan and require `--yes` for a real run.
- Applies the same non-terminal refusal per record; skipped records are
  reported with the reason rather than silently ignored.

### Implementation notes

- Add a `forget(run_id, *, path=None, force=False)` (and `prune(...)`) function
  to `orchestration/handoff_registry.py` beside `register`/`adopt`/
  `update_observed`, using the existing `_locked(path, exclusive=True)` +
  `_read_unlocked` / `_write_unlocked` pattern so writes stay atomic and
  lock-protected.
- Wire the subcommands in `handoffctl.py` next to the existing
  `args.runs_command` dispatch (`list`/`show`/`adopt`, around line 1085).
- Do not bump `REGISTRY_VERSION`; the schema is unchanged.

### Explicitly out of scope

Deleting run directories, journals, or credential/token files. That is a
genuinely destructive operation against durable evidence and deserves its own
separate, explicitly-flagged command if it is ever wanted. `forget` removes an
*index entry* only — the run remains fully inspectable by absolute
`--run-dir`.

## Consider alongside (optional)

A `runs doctor`-style validation that reports bad records **individually**
instead of failing the whole read, so a corrupt entry degrades to "24 of 25
records readable, 1 invalid: <run_id>" rather than a total outage. This
addresses problem (1) at its root; `forget` is then the remedy it points to.
Worth doing if it stays small — the strict-validation behavior itself should
not change for the normal read paths.

## Verification

1. `/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python -m pytest tests/`
   passes (baseline 175 as of Phase 1; re-baseline after Phase 2).
2. New tests in `tests/test_handoff_registry.py` and
   `tests/test_handoffctl.py` covering: forgetting a terminal run; refusing a
   non-terminal run without `--force`; `--force` overriding it; forgetting a
   dangling record whose run directory is gone; `prune --dry-run` changing
   nothing; prune filters; and that no run directory or credential file is ever
   removed.
3. Manual check against a scratch registry via `HANDOFF_REGISTRY_FILE` pointing
   at a temp copy — **never against the real
   `~/.local/state/agents/handoff/registry.json`** during development.
4. Docs: update `docs/architecture.md` (registry section) and the
   `~/skills/handoff-agent/` read-operations list, which currently enumerates
   `runs list` / `runs show` only.
