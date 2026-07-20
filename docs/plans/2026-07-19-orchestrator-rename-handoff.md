# Handoff: rename "coordinator" → "orchestrator" across the handoff stack

**Status (2026-07-20):** **Phase 1 done, reviewed, and pushed. Phase 2 done —
committed locally, not yet pushed** (the orchestrator reviews and owns the push).

- Phase 1 landed via a delegated Kimi worker: agents `940aa14` (code, tests,
  `handoff_coordinator_ensure.sh` → `handoff_orchestrator_ensure.sh`) and
  `ee7ece8` (docs); skills `cd9839e` (SKILL.md + all four references).
  Both repos pushed.
- Phase 2 landed via a delegated Kimi worker on a quiescent machine (no
  watchers, no workers): agents `332ee01` (persisted-spelling code rename,
  no compat layer) plus a follow-up commit with
  `scripts/migrate_handoff_state_orchestrator.py` and the docs/skill updates;
  skills repo commit for the two handoff-agent references. The migration ran
  in the same change: backup at
  `~/.local/state/agents/handoff.pre-orchestrator-migration-20260720T174813Z`,
  94 files migrated (registry=1, watcher=10, control=38, inbox=7, lock=38),
  idempotent re-run clean. See "Phase 2 outcome" below.
- Independently verified at review: 175 tests pass; `orchestrator` and the
  `coordinator` alias resolve identically; **zero diff lines touched any
  persisted spelling**; every live `watcher.json` still uses `coordinator_id`;
  the live investment watcher survived unharmed.

Written 2026-07-19 after the handoff-agent skill restructure landed (agents
`6e23c54`, skills `b21231c`): the skill is a lean `SKILL.md` plus
`references/`, and four helper scripts live under `scripts/`.

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
project** — run `f6d7d1b2` (`phase3b-tier0-triage`, Codex on `oci-box`), whose
authoritative run directory lives on the remote host, plus local
registry/watcher state for its orchestrator session. The rename must not break
steering, watching, or reviewing that run. Therefore the work is split into two
phases.

**Correction (2026-07-20):** this section originally said Phase 2 was safe to
ship "with read-compatibility for the old field spellings." **That was wrong,**
and the Phase 1 worker caught it. Read-compat makes *new code tolerate old
state*; the actual hazard is the reverse — **long-lived old-code watcher
processes reading state that new code writes.** No reader-side compatibility in
the new code can help a process that is already running the old code. See
"Phase 2" below for what this changes.

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

## Phase 2 — persisted protocol state (DEFERRED; approach revised 2026-07-20)

**Recommendation: do not do this until the machine is quiescent, then do it
clean — with no compatibility layer at all.**

### Why the original "read-compat" plan was dropped

The Phase 1 worker showed that read-compat does not unblock anything. The
hazard is old-code *processes*, not old *files*: a watcher started before the
rename holds the old code in memory and keeps reading state that new code
writes. Writing the new spelling while such a process lives would (1) fail its
per-poll `registry.json` validation, (2) make it exit when `acknowledge`/
`dismiss` rewrites `watcher.json`, (3) fail its `control.json` validation, (4)
split lock mutual exclusion if `coordinator.lock` is renamed, and (5) fail its
journal validation on a renamed `coordinator-takeover` event. Reader-side
compatibility in the *new* code cannot fix any of these.

So Phase 2 already requires quiescence (no live runs, no old-code watchers).
And **once you have quiescence, the compat layer is unnecessary** — its only
purpose was to ship while runs were live. Skipping it avoids permanently
carrying a `_compat_field` fallback branch through the lease/token validation
paths, which is exactly where the code should stay simple. Waiting costs
nothing but time; compat costs complexity forever.

### Why it is low priority

Phase 1 already renamed everything a human sees: the CLI verb and flags, help
text, notification bodies, all Python identifiers, docs, and the skill. What
remains is invisible plumbing — JSON keys inside `control.json`, lock and
credential filenames, a state directory name, and one registry key. The benefit
is consistency for someone reading protocol files during debugging; the cost is
churn plus risk to whatever is running at the time. Treat this as opportunistic
cleanup, not scheduled work.

### Gate (both conditions)

1. `handoffctl runs list` shows no live runs (in particular the investment run
   `f6d7d1b2` has finished), **and**
2. no old-code watcher processes remain — verify with
   `pgrep -fl 'agents.orchestration.handoffctl'` and confirm any survivor was
   started after the Phase 2 code landed.

### Scope when it happens

- `control.json` fields: `coordinator_epoch`, `coordinator_token_sha256`,
  `coordinator_lease_expires_at` → `orchestrator_*`.
- Inbox event type `coordinator-takeover` → `orchestrator-takeover`.
- Lock file `coordinator.lock` and the `_lock(run_dir, "coordinator")` name.
- Credential filename `coordinator.token` (constructed in
  `handoff_launcher.py:618`; the *parameter* is already `orchestrator_token_file`).
  Update the two skill references that quote the filename.
- Registry / watcher-state key `coordinator_id` (`handoff_registry.py`, watcher
  cursor state, and the CLI JSON outputs that mirror it).
- The `role = "coordinator"` literal in `handoff.py` (message-direction
  derivation) — note this also renames the credential env var, because
  `handoffctl.py:76` derives it as `f"HANDOFF_{role.upper()}_TOKEN_FILE"`;
  `HANDOFF_COORDINATOR_TOKEN_FILE` becomes `HANDOFF_ORCHESTRATOR_TOKEN_FILE`
  automatically. Running sessions that exported the old name will break, which
  is another reason to wait for quiescence.
- `HANDOFF_COORDINATOR_STATE` (the `--orchestrator-state` fallback default at
  `handoff_launcher.py:928`).
- **The state directory name** `~/.local/state/agents/handoff/coordinators/`
  — missing from the original spec; existing session directories would need
  migrating or abandoning.
- The remote host's installed `agents` package must be updated in step, since
  remote runs execute owner-side operations there.

Do **not** bump `protocol_version` (`local-v1` stays); this is a spelling
migration, not a semantic change.

### Related footgun worth documenting at the same time

`HANDOFF_COORDINATOR_STATE` and `HANDOFF_COORDINATOR_TOKEN_FILE` are
process-scoped and are not set in `~/.zshenv`, `~/.zshrc`, or
`~/.config/secrets.env` — parallel orchestrator sessions stay isolated because
each has its own `mktemp`-created `session.XXXXXX/watcher.json` path and passes
it explicitly via `--orchestrator-state`. But `env.build_env()` sources
`~/.zshenv` and `~/.config/secrets.env`, so exporting either variable in those
shared files would make it effectively machine-global: a launch that omitted
`--orchestrator-state` would then silently attach its worker to another
session's watcher, sending that worker's doorbells to the wrong surface with no
error. Keep both variables per-invocation only.

## Verification

1. Full suite: `/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python -m
   pytest tests/` — must pass after each phase.
2. After Phase 1: `grep -rniE 'coordinator' orchestration/ tests/ docs/
   scripts/ CLAUDE.md ~/skills/handoff-agent/` returns only (a) the CLI aliases
   and the deliberately-kept persisted spellings listed under "Phase 1
   outcome", (b) historical transcripts in plan docs, (c) gitignored
   `__pycache__` binaries.
3. Live-run smoke test after each phase, without steering the worker:
   `handoffctl runs list`, `handoffctl orchestrator show` (and via the
   `coordinator` alias), `handoffctl orchestrator pending`, and a
   `handoffctl read`/`status` against the investment run must all still work.
4. Confirm the running watcher survived: its surface/tab is still alive and
   `orchestrator show` reports the same owner PID as before the change.

## Suggested commit split

1. Phase 1 code + tests + `handoff_coordinator_ensure.sh` rename (this repo).
   — done: `940aa14`
2. Phase 1 docs + CLAUDE.md (this repo). — done: `ee7ece8`
3. Skill core + references (separate commit in the `~/skills` repo).
   — done: `cd9839e`
4. Phase 2 (clean persisted-state rename, no compat layer) as its own commit in
   this repo, only once the two-condition gate above is satisfied.

## Phase 1 outcome (2026-07-20)

### Deliberately kept as "coordinator" (all Phase 2 scope)

Persisted/protocol spellings: `control.json` fields (`coordinator_epoch`,
`coordinator_token_sha256`, `coordinator_lease_expires_at`); inbox type
`coordinator-takeover`; role literals (locks, `_authorize`, the
message-direction derivation in `handoff.py`); `coordinator.lock`; the
`coordinator.token` filename; the registry/watcher key `coordinator_id` and the
CLI JSON outputs that mirror it; the remote-request wire keys
`retain_coordinator`/`coordinator_id` (the remote host still runs old code);
the env vars `HANDOFF_COORDINATOR_TOKEN_FILE` (derived from the role literal)
and `HANDOFF_COORDINATOR_STATE`; and the `handoff/coordinators` state
directory.

### Renamed but worth knowing

User-facing JSON keys changed: launcher/dispatch output `coordinator_released`
→ `orchestrator_released`, and the `init` output's `credential_files` key
`coordinator` → `orchestrator` (the file on disk is still `coordinator.token`).
Any external consumer of those keys must be updated. `ORCHESTRATOR_DOORBELL_TITLE`
now reads "Handoff orchestrator pending", and review notifications say
"Awaiting orchestrator review".

### Accepted deviations from this spec

1. `scripts/cmux_close_surface_safe.sh` needed no edit — its single role mention
   already said "orchestrator" (the spec's match count was stale).
2. CLI aliases are not *hidden*: argparse displays
   `orchestrator (coordinator)` in the subcommand list and shows both option
   strings in help. Old invocations resolve correctly; hiding them would
   require custom argparse formatting not worth the complexity.
3. `dispatch`'s ephemeral token filename prefix was renamed to
   `orchestrator-dispatch-*` (transient, single-process lifecycle).

### Known gap, unrelated to the rename

The review acceptance for run `b9778f37` could not be sent: it was launched in
launch-only mode, so the lease was released and the credential directory is
private and redacted in `runs show`, while `dispatch` has no review-disposition
flag (for an `awaiting_review` worker it sends `supersede`). A managed run
(`HANDOFF_CREDENTIAL_DIR` + `--retain-orchestrator`) is required if the
orchestrator intends to send a formal `review` later. Worth considering as a
protocol/CLI improvement: a registry-backed review path that takes over the
lease the way `dispatch` does.

## Phase 2 outcome (2026-07-20)

Done on a quiescent machine (no watcher processes, no worker sessions), clean
rename with **no backward-compatibility layer**, code and state migration in
the same change. `protocol_version` stays `local-v1`.

Renamed in code and migrated on disk:

- `control.json` fields `coordinator_epoch` / `coordinator_token_sha256` /
  `coordinator_lease_expires_at` → `orchestrator_*` (38 files).
- Inbox event type `coordinator-takeover` → `orchestrator-takeover`,
  including the persisted events in 7 existing `inbox.jsonl` journals (the
  original migration table missed these; without it the new strict journal
  validation would reject those runs' inboxes).
- Lock `coordinator.lock` → `orchestrator.lock` (38 files) via the
  `_lock(run_dir, "orchestrator")` role literal; the same role rename moves
  the derived credential env var to `HANDOFF_ORCHESTRATOR_TOKEN_FILE`.
- Credential filename `coordinator.token` → `orchestrator.token` for new
  launches (existing token files on disk deliberately NOT renamed —
  `credential_dir` paths recorded in the registry would otherwise dangle).
- Registry / watcher-state key `coordinator_id` → `orchestrator_id`,
  including the CLI JSON outputs that mirror it (25 registry records in one
  `registry.json`, 10 `watcher.json` snapshots).
- `HANDOFF_COORDINATOR_STATE` → `HANDOFF_ORCHESTRATOR_STATE`
  (`--orchestrator-state` fallback default).
- New session dirs go to `~/.local/state/agents/handoff/orchestrators/`
  (`scripts/handoff_orchestrator_ensure.sh`); the existing `coordinators/`
  directory stays in place and its `watcher.json` files were migrated in
  place.

Migration: `scripts/migrate_handoff_state_orchestrator.py` (idempotent,
`--dry-run` support, atomic writes preserving modes). Backup before any
modification:
`~/.local/state/agents/handoff.pre-orchestrator-migration-20260720T174813Z`.
94 files changed (registry=1, watcher=10, control=38, inbox=7, lock=38); a
re-run `--dry-run` reports zero remaining changes. Verified after migration:
`handoffctl runs list` (25 runs), `runs show`, `status`, and `doctor` all
work on migrated state; the 25 pre-existing `status-projection` doctor
notices on old runs reproduce identically with the pre-rename code on the
pristine backup, so they are historical artifacts, not migration damage.

### Deliberately still "coordinator" after Phase 2

- ~~**Remote wire keys**~~ — **unfrozen 2026-07-20.** `oci-box` was updated to
  the post-rename package (its state tree migrated: 35 files, backup
  `handoff.pre-orchestrator-migration-20260720T190249Z`), so
  `REMOTE_REQUEST_FIELDS` now carries `retain_orchestrator` /
  `orchestrator_id`. The receiver additionally normalizes the two pre-rename
  spellings via `REMOTE_LEGACY_REQUEST_KEYS`: unlike on-disk state, the two
  ends of an SSH launch are updated independently, so a launcher from a
  not-yet-updated checkout would otherwise fail with an opaque "invalid remote
  launch request". That tolerance is receive-side only — senders always emit
  the new names — and it can be dropped once no stale checkouts remain.
- The **CLI aliases** kept from Phase 1 (`coordinator` subcommand alias,
  `--coordinator-id` / `--coordinator-state` / `--coordinator-token-file` /
  `--retain-coordinator` flags).
- Existing on-disk `coordinator.token` files and the `coordinators/` state
  directory name (see above), plus free-text prose mentioning the role in
  historical `kickoff.md` / `progress.md` / message bodies, and historical
  plan-doc transcripts.
