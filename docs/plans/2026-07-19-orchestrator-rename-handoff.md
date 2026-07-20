# Handoff: rename "coordinator" → "orchestrator" across the handoff stack

**Status:** not started — this doc is the spec for a delegated worker (Kimi via
/delegate-first). Written 2026-07-19; revised the same day after the
handoff-agent skill restructure landed (agents `6e23c54`, skills `b21231c`):
the skill is now a lean `SKILL.md` plus `references/`, and four helper scripts
were added under `scripts/`.

## Decision and goal

The handoff protocol, CLI, watcher, docs, and the `/handoff-agent` skill use
"coordinator" and "orchestrator" interchangeably for the same role: the one
session (agent or human) that launches, steers, reviews, and integrates worker
sessions. Example of the mix in a single spot:
`orchestration/handoff_launcher.py:43` defines
`ORCHESTRATOR_DOORBELL_TITLE = "Handoff coordinator pending"`, and
`orchestrator_doorbell(handle, coordinator_id)` takes a coordinator ID.

We decided to standardize on **"orchestrator"** everywhere: it is the
industry-standard name for this pattern (orchestrator–worker), semantically
accurate (directs subordinate workers, not coordinating peers), and already the
primary term in the design doc, the `orchestration/` package name, and the
global delegate-first skill.

Goal: after this work, "coordinator" appears nowhere in the repo, the skill, or
user-facing output — except where deliberately retained for backward
compatibility (see Phase 2 read-compat and the CLI alias).

## CRITICAL constraint: one live handoff run

There is currently **one live remote worker running for the investment
project**, with its authoritative run directory on the remote worker host, plus
local registry/watcher state for its orchestrator session. The rename must not
break steering, watching, or reviewing that run. Therefore the work is split
into two phases, and **Phase 2 must not ship without read-compatibility for the
old field spellings** (or explicit confirmation that no live runs remain).

Check live runs before and after any change:

```bash
PY=/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python
$PY -m agents.orchestration.handoffctl runs list
$PY -m agents.orchestration.handoffctl coordinator show   # watcher/session state
```

Local registry: `~/.local/state/agents/handoff/registry.json` (override:
`HANDOFF_REGISTRY_FILE`). Do NOT hand-edit it; do not touch the remote host at
all.

## Phase 1 — safe now (no persisted-format changes)

Rename every "coordinator" that is *not* written to or read from durable state:

1. **Internal Python identifiers** in `orchestration/`: variables, parameters
   (`coordinator_id` → `orchestrator_id`), function/constant names, docstrings,
   comments, log/error strings. Files and current `coordinator|orchestrator`
   match counts (from `grep -ci`):
   - `orchestration/handoffctl.py` (~134)
   - `orchestration/handoff.py` (~68)
   - `orchestration/handoff_launcher.py` (~60)
   - `orchestration/handoff_watcher.py` (~48)
   - `orchestration/handoff_registry.py` (~25)
   Also fix the already-"orchestrator" names for consistency
   (`ORCHESTRATOR_DOORBELL_TITLE` value string says "coordinator").
2. **CLI surface** in `handoffctl.py`: `coordinator` subcommand →
   `orchestrator` (register/retarget/start/show/pending/dismiss), flags
   `--coordinator-id` → `--orchestrator-id`, `--coordinator-state` →
   `--orchestrator-state`, `--coordinator-token-file` →
   `--orchestrator-token-file`. **Keep `coordinator` and the old flag names as
   hidden aliases** (argparse aliases / dest mapping) so the live session's
   muscle memory, the currently-running watcher's own CLI invocations, and any
   existing shell history keep working. The running surface-hosted watcher
   process invokes the old CLI names — aliases are mandatory, not optional.
3. **Tests**: `tests/test_handoff.py`, `test_handoffctl.py`,
   `test_handoff_watcher.py`, `test_handoff_launcher.py`,
   `test_handoff_registry.py`. Rename identifiers/strings in step with the
   code; add a test that the `coordinator` CLI alias still resolves.
4. **Docs**: `docs/architecture.md`,
   `docs/2026-07-14_agent-handoff-communication-design.md`,
   `docs/plans/2026-07-18-cmux-surface-watcher-progress.md`,
   `docs/plans/2026-07-18-detached-orchestrator-watcher-kickoff.md`,
   repo `CLAUDE.md` (a.k.a. AGENTS.md). Prose only — do not "fix" historical
   command transcripts in plan docs if they record what was actually run; add a
   parenthetical instead.
5. **Skill** (outside the repo, its own git repo with remote): the
   `~/skills/handoff-agent/` skill is `SKILL.md` (lean core) plus four
   reference docs. Combined `coordinator|orchestrator` match counts
   (`grep -ci`): `SKILL.md` (44), `references/remote-and-viewers.md` (17),
   `references/launch-details.md` (6), `references/rescue-and-close.md` (4),
   `references/codex-app-task.md` (1). Unify all five on orchestrator,
   including the quoted CLI invocations — quote the new `orchestrator`
   subcommand since the alias keeps old invocations working anyway. Keep the
   YAML frontmatter's wording changes minimal (it controls skill triggering)
   — it currently contains neither word, so it likely needs no edit.
6. **Helper scripts** (in this repo): only
   `scripts/handoff_coordinator_ensure.sh` needs real work (18 matches:
   filename, header, flags, and its `coordinator register/start` invocations —
   follow the CLI alias rules above; update the SKILL.md/architecture.md/
   CLAUDE.md references to the new filename in the same change).
   `scripts/cmux_close_surface_safe.sh` has one prose mention to update;
   `scripts/handoff_doorbell.sh` and `scripts/tmux_run_logged.sh` contain
   neither word — leave them untouched.

Explicitly **out of scope**: the `orchestration/` package name (names the
domain, not the role — leave it), `scripts/handoff_agent.sh` and
`scripts/cmux_ssh_tmux_quiet.sh` (zero matches), and anything on the remote
worker host.

Both working trees (`~/projects/agents` and `~/skills`) are clean as of the
restructure commits above. If `git status` shows local modifications when you
start, they are the user's — work on top of them, do not revert or stash them.

## Phase 2 — persisted protocol state (GATED)

**Gate:** ship only with read-compat for old spellings, or after
`handoffctl runs list` shows no live runs. The live investment run's
`control.json`, lock files, and credential files on the remote host use the old
names, and the local registry/orchestrator-session state does too.

Rename with backward compatibility (accept old name on read, write new name):

- `control.json` fields: `coordinator_epoch`, `coordinator_token_sha256`,
  `coordinator_lease_expires_at` → `orchestrator_*`.
- Inbox event type `coordinator-takeover` → `orchestrator-takeover` (readers
  must accept both).
- Lock file `coordinator.lock` and the `_lock(run_dir, "coordinator")` name.
- Credential file naming (`coordinator` token file created by `init`).
- Registry / orchestrator-session state keys (whatever `coordinator register`
  persists — inspect `handoff_registry.py` and the watcher's cursor state for
  key names), including `last_doorbell_method` payloads if they embed the word.
- The `role = "coordinator"` string derived from message direction in
  `handoff.py:581`.

Implementation suggestion: a small `_compat_field(mapping, new, old)` helper at
the read sites, and writers always emit the new spelling. Do **not** bump
`protocol_version` (`local-v1` stays); this is a spelling migration, not a
semantic change.

## Verification

1. Full suite: `/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python -m
   pytest tests/` — must pass after each phase.
2. After Phase 1: `grep -rniE 'coordinator' orchestration/ tests/ docs/
   scripts/ CLAUDE.md ~/skills/handoff-agent/` returns only (a) deliberate
   compat aliases/read-compat code, (b) historical transcripts in plan docs.
3. Live-run smoke test after each phase, without steering the worker:
   `handoffctl runs list`, `handoffctl orchestrator show` (and via the
   `coordinator` alias), `handoffctl orchestrator pending`, and a
   `handoffctl read`/`status` against the investment run must all still work.
4. Confirm the running watcher survived: its surface/tab is still alive and
   `orchestrator show` reports the same owner PID as before the change.

## Suggested commit split

1. Phase 1 code + tests + `handoff_coordinator_ensure.sh` rename (this repo).
2. Phase 1 docs + CLAUDE.md (this repo).
3. Skill core + references (separate commit in the `~/skills` repo).
4. Phase 2 (compat-gated persisted-state rename) as its own commit in this
   repo, only if the gate is satisfied.
