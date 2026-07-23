# Handoff protocol: collapse the state machine onto the journals

Status: implemented 2026-07-22, revised after review round 1 (claude-fable-5)
Date: 2026-07-22
Scope: `orchestration/handoff.py`, `handoffctl.py`, `handoff_clean.py`,
`handoff_watcher.py`, `handoff_registry.py`, the worker kickoff contract
(`handoff.py:_standing_instructions`), `docs/architecture.md`, and the
`handoff-agent` skill docs.

## Goal

Remove the class of defect where a run's semantic state is *inferred* rather
than *read* — either by the worker inferring its own state from a message that
did not arrive, or by the orchestrator caching an instruction that is never
invalidated. Concretely: shrink the worker state set, delete
`control.desired_state`, and move liveness and run-termination to the layers
that actually own them (transport probe and an explicit registry marker).

Non-goals: changing the credential/lease model, doorbell delivery mechanics,
remote-host transport, or the journal file formats themselves.

## Motivating incident (2026-07-22)

Run `9bcef2fa` (Kimi worker, local cmux). Sequence, all from durable state:

1. `conclude` → review + `pause`; `control.desired_state = "pause"`; worker
   emitted `paused`.
2. `dispatch` sent a follow-up `steer`. The worker resumed to `working`, but
   **`dispatch` never reset `desired_state`** — it still read `pause`.
3. The next `conclude` hit `handoffctl.py:423`
   (`if desired == "pause": pause_already_requested = True`) and therefore sent
   the accepted review **with no lifecycle message**.
4. The worker's contract (`handoff.py:718`, frozen into the run's `kickoff.md`)
   says to follow the lifecycle message accompanying an accepted review, and to
   report `succeeded` after consuming one. With no lifecycle message it emitted
   `succeeded` — correct per contract.
5. `succeeded` is terminal to `_dispatch_local` (`handoffctl.py:266`), so all
   further instructions to a live, idle, willing worker were refused.

Aggravating factor: `handoff_registry.py:22` defines
`TERMINAL_STATES = {"succeeded", "failed", "stopped"}`, and `handoff_clean.py:79`
reaps on that set. So the stuck worker is simultaneously (a) unreachable by
`dispatch` and (b) eligible for session kill by a bulk `runs clean`, while
holding full context and undelivered work.

## Current state

### Worker states (8)

`starting`, `working`, `blocked`, `awaiting_review`, `succeeded`, `paused`,
`stopped`, `failed`. Declarable by checkpoint: `working`, `succeeded`,
`stopped`, `paused` (`handoff.py:557`). Derived: `blocked` (blocking question),
`awaiting_review` (result), `failed` (fatal error event, `handoff.py:1195-1198`).

Entry/exit conditions that matter here:

- `succeeded` (`handoff.py:1164-1170`): requires `awaiting_review` plus a
  consumed matching accepted review. **Sticky**: `handoff.py:1128-1129` allows a
  succeeded worker to checkpoint only `succeeded`/`stopped`/`paused` — never
  `working`.
- `paused` (`handoff.py:1175-1183`): requires `awaiting_review` or `succeeded`,
  a consumed matching accepted review, **and** any `pause` in the consumed
  prefix. Resume (`handoff.py:1150-1161`) requires a `steer`/`answer`/
  `supersede` **newer than the last `pause` message** — i.e. resume is keyed on
  a `pause` seq existing at all.
- `stopped` (`handoff.py:1171-1174`): requires `desired_state == "stop"` **and**
  a consumed `stop` message. Reachable *only* by orchestrator request.
- `failed`: only from the worker's own `error` event with `fatal: true`. A
  failed or stopped epoch is terminal to all further events
  (`handoff.py:1126-1127`).

Note what this means: **no worker state is reachable by a worker that dies.**
A crashed process, killed session, or dropped SSH leaves `status.json` frozen at
its last written value. `stopped` and `failed` are both cooperative
self-reports from a live worker.

### `control.desired_state`

Values `run` | `pause` | `stop` (`handoff.py:419`). Never assigned directly: it
is projected from the orchestrator's inbox writes in `_apply_inbox`
(`handoff.py:986-989`), atomically inside `send()` under the orchestrator lock,
initialized to `run` at run creation (`handoff.py:823`). The doctor
independently replays the same projection (`handoff.py:1544-1549`), and
`repair_control` rebuilds control by replaying `_apply_inbox` — so any write
that is not a pure inbox projection desynchronizes both.

**There is no code path that sets it back to `run`.** It is monotonic
(`run → pause → stop`) while worker state is not — a `steer` legitimately
resumes a paused worker.

It is also overloaded. `desired_state == "pause"` serves as both:

1. the lifecycle instruction "idle at the next stopping point"; and
2. a **permission flag** — `handoff.py:1017-1022` uses it to let a `steer`
   through after an integration decision would otherwise be terminal.

Job 2 masked job 1 going stale: steering kept working, so the run looked
healthy until `conclude` consulted the same field for job 1.

### Validation is strict on read, not just write

`_journal` re-validates **every record on every read** (`handoff.py:641` →
`_validate_message` → `_validate_type_data`), and `_fields` (`handoff.py:147`)
enforces exact key sets for `control.json`/`status.json`. Journals are
append-only and run dirs are deliberately kept inspectable after cleanup, so
historical records containing a deleted message type or state persist
indefinitely. **Any deletion from the accepted vocabulary retroactively bricks
every run dir that ever used it** — `status()`, `send()`, clean probes, and the
doctor all raise `invalid journal record`. This constraint governs the whole
migration and is why read tolerance below is permanent, not a courtesy window.

### Consumers

| Module | Uses | Purpose |
|---|---|---|
| `handoff.py` | 12 | projection, send guards, checkpoint transitions, doctor replay |
| `handoff_clean.py` | 8 | terminality (`:80`), reason text (`:298`) |
| `handoff_watcher.py` | 3 | `concluded` → auto-quiet (`:571-577`) |
| `handoff_registry.py` | 3 | **`_assess_terminal`** (`:281`, `:287-288`) — the reap gatekeeper, called from `handoff_clean.py:271` (kill-time re-verify), `:376`, `:506` |
| `handoffctl.py` | 2 | conclude idempotence guards (`:411-433`) |
| `handoff_launcher.py` | 0 | — |

Sites the first draft missed, now in scope:

- `handoffctl.py:496-501` — `_stop_local` refuses on terminal worker state and
  on `desired_state == "stop"`.
- `handoffctl.py:334-338` — `conclude`'s `resumable` predicate is keyed on
  `status == "succeeded"`; `resume_lifecycle` (`:339-348`) on
  `{"awaiting_review", "succeeded"}`. Both are crash-recovery paths.
- `handoff.py:1035` — a `changes_requested` review is refused when the worker is
  `succeeded`.
- `handoff.py:1091-1094` — `_unresolved_questions` treats a consumed `stop` as
  clearing all blocking questions. This invariant disappears with the message
  type unless deliberately re-homed.
- `handoff.py:1539-1571` — the doctor's semantic replay of control.
- `handoff.py:705-726` / `:830` — `_standing_instructions()` generates the
  worker contract (including the embedded `EMIT_DATA_SCHEMAS`) and
  `initialize()` **freezes it into each run's `kickoff.md`**. In-flight workers
  hold their contract forever.
- `docs/architecture.md` — documents the state list and desired-state-as-
  terminality.

Two facts that help:

- `handoff_watcher.py:578-581` already treats a fatal `error` **event** as
  sufficient, independent of `status.state == "failed"` — but only over
  *newly observed* events (see the fatal-flag requirement below).
- `handoff_clean.py:69-72` computes `session_alive` for remote runs. **It is not
  a terminality input** (`:78-82`), and the local path never probes at all.
  The first draft overstated this; see the positive-absence rule below.

## Design

Three principles:

1. **The journals are the only truth.** Anything derivable from inbox/outbox
   order is derived, not cached. A cache that cannot be invalidated is a bug
   with a delay fuse.
2. **Worker state answers exactly one question: whose turn is it, and what does
   the worker owe?** Not liveness, not orchestrator intent.
3. **Liveness and termination are transport/registry facts.** The state machine
   structurally cannot observe a worker that stopped being able to write.

### Validation policy: write-strict, read-tolerant (permanent)

Retiring vocabulary never removes it from the *read* path.

- **Write-strict**: new code refuses to *emit* `succeeded`/`stopped` checkpoints
  or `pause`/`stop` messages.
- **Read-tolerant, permanently**: `_validate_type_data` and the checkpoint
  schema keep accepting the legacy vocabulary as historical records;
  `_fields` accepts a legacy `desired_state` key in `control.json` and ignores
  it. `_project_worker` maps legacy `succeeded`/`stopped` checkpoints during
  replay; `_apply_inbox` treats legacy `pause`/`stop` inbox records as no-ops
  (their effect is reconstructed by the migration, below).
- **`handoff.WORKER_STATES` permanently retains the retired states on the read
  path** (or gains a read-path union with a legacy set). It is not only a local
  validator: `handoff_watcher.py:498` rejects any remote status whose state
  falls outside it with "remote watcher status is invalid". Shrinking it would
  make a skewed older remote host reporting `succeeded`/`stopped`/`failed`
  *error* the run instead of reading it — breaking the local↔remote skew
  guarantee this policy exists to provide.
- `_doctor_report`, `repair_status`, and `repair_control` replay through the
  same tolerant path, so a legacy run still passes the doctor.

This also covers local↔remote package skew: an older remote host emitting legacy
vocabulary is read, not rejected.

### Deliverability rule (replaces the `pause` escape hatch)

**Work messages (`steer`, `answer`, `supersede`, `input-changed`,
`base-changed`) are deliverable whenever the worker epoch is live, regardless of
the integration record.** Per-result review terminality (`handoff.py:1028`)
still prevents re-reviewing an integrated result, which is the invariant the
integration guard actually exists to protect.

This is load-bearing. Today `conclude` auto-integrates whenever the result
reports a head (`handoffctl.py:397-398`, and `emit` *forces* a real HEAD for git
workspaces at `handoff.py:1288-1296`), and the integration record resets only
when a **new result is consumed** (`handoff.py:1317-1320). The only thing that
lets a parked-but-integrated worker be steered today is the `desired_state ==
"pause"` escape at `handoff.py:1017-1022`. Deleting that escape without this
rule would recreate the incident's dead end — live, idle, unreachable — for
every integrated run.

### Explicit `finished` marker

`conclude --stop` / `handoffctl stop` become "mark the run finished", writing an
explicit durable marker in the run record. The integration record is **not**
sufficient: it is per-result and cannot distinguish "parked between work
cycles" from "done with this run".

The marker must be readable where each consumer runs, which is two different
places:

- **`runs clean`** probes the owning host, so the marker lives in the owning
  host's registry and is carried in the probe payload.
- **`handoff_watcher.py`** does *not* read any registry for run state: for a
  remote run `_protocol_state` (`:471-497`) fetches only `control show`,
  `status`, and `read` over SSH. A marker that lives solely in the owning host's
  registry would therefore never reach the watcher, and remote concluded runs
  would never auto-quiet.

Resolution: `_conclude_registered` **mirrors the marker into the
orchestrator-side record** when the remote conclude returns, and the watcher
reads it from there alongside its SSH-fetched protocol state. The migration
backfill has the mirror-image problem — orchestrator-host records for remote
runs have no local `desired_state` to key on — so for remote records the
backfill probes the owning host and writes the mirrored value.

Both `runs clean` (terminality, via `handoff_registry._assess_terminal`) and
`handoff_watcher.py` (`concluded` → auto-quiet) key on this marker instead of
`desired_state`.

**Refusal scope, and why it is not uniform.** `dispatch` and a *non-stop*
`conclude` refuse a finished-marked run, rather than steering a dead session and
reporting success. But `conclude --stop` and `stop` against an already-finished
run are **idempotent success** (`finished_already: true`), and
`_conclude_registered` **(re)writes the mirror on that response**. A uniform
refusal deadlocks: if the remote `conclude --stop` succeeds on the owning host
and the response is lost — SSH teardown, a `_remote_json` decode error, a crash
before the local mirror write, exactly the windows `handoffctl.py:414-433`'s
existing idempotence machinery exists for — then the natural repair, retrying
the conclude, would be refused by the owning host. The mirror could never be
written, and the remote run would never auto-quiet, with no reconciliation path
(the backfill probe is one-time migration).

**Ordering constraint the short-circuit depends on:** the marker write is the
**final** step of the conclude sequence, after the review and integration
records are durable. `finished_already` is keyed on the marker alone, so a
marker written *first* would let a crash in between short-circuit the retry to
idempotent success with the accepted review never sent — parking the worker in
`awaiting_review` forever. Marker-last makes `finished_already: true` imply the
sequence completed.

### Proposed worker states (5)

| State | Meaning | Entered when |
|---|---|---|
| `starting` | launched, not yet checkpointed | run creation |
| `working` | mine to finish | checkpoint `working` |
| `blocked` | owes nothing until answered | blocking question emitted |
| `awaiting_review` | owes nothing until reviewed | result emitted |
| `paused` | idle, resident, nothing owed either way | accepted review consumed |

Deleted from the *write* vocabulary: `succeeded`, `stopped`, `failed`.

- `succeeded` → `paused`. Same physical situation; differed only by whether a
  lifecycle message accompanied the review, which is orchestrator state.
- `stopped` → nothing. Only ever the acknowledgment of an orchestrator `stop`,
  and the worker's TUI stays resident afterward anyway, so `stop` never freed
  anything. `runs clean` killing the session is what ends a worker.
- `failed` → replaced by a **durable fatal flag** (below).

Consequent projector changes, all required in the same phase as the state
change:

- `paused` entry no longer requires a consumed `pause` message — a consumed
  matching accepted review suffices.
- **Resume must be re-keyed.** `handoff.py:1150-1161` currently requires a work
  message newer than the last `pause` **seq**; a worker that paused on a bare
  accepted review has no `pause` seq and could never resume. New rule: resume on
  any work message newer than **the accepted review that idled the worker**.
- The sticky-`succeeded` guard (`handoff.py:1128-1129`) is removed with the
  state, but legacy runs still carrying `succeeded` must be able to escape it —
  see Phase 1 step 3.

### Fatal handling (replaces the `failed` state)

The fatal fact is **derived from the outbox, not stored**. A stored flag would
need a writer with registry access (the worker's `emit` path in `handoff.py` has
none, and for a remote run emit happens on the owning host) and would carry an
invalidation obligation on `worker-relaunched` — exactly the cached-derived-state
pattern principle 1 forbids.

Definition: a run is **fatal** iff its outbox contains an `error` event with
`fatal: true` whose `writer_epoch == control["worker_epoch"]`. This is durable,
crash-proof, and epoch-scoped for free — a relaunch bumps the epoch and the run
stops being fatal without anyone clearing anything.

**The watcher cannot evaluate that definition from its existing fetches.**
`_protocol_state` (`handoff_watcher.py:471-497`) returns only `records[after:]`,
so on every poll after the one that first observed the fatal `error` event the
event is gone from `events`, and post-collapse `status` never reads `"failed"`.
Specified as-is, the derivation would reproduce the exact
newly-observed-only failure this section exists to fix. Mechanism, explicitly:

- the watcher **persists the observed fatal `writer_epoch`** in its per-run
  state on the poll that first sees the event, and thereafter derives
  `fatal == (stored_fatal_epoch == control["worker_epoch"])`. This is
  self-invalidating on relaunch — the epoch bump falsifies it with no explicit
  clear — so it stays consistent with principle 1.
- **Upgrade backfill.** The mechanism survives watcher restarts and state-file
  loss, but not the upgrade itself: a run whose fatal event was already observed
  by the pre-upgrade watcher has `observed_through` past it, no stored epoch, and
  the event is never re-fetched — so it would silently derive non-fatal and go
  permanently quiet. Either keep legacy `status["state"] == "failed"` as a
  read-tolerant OR-term in the watcher's fatal fact, or treat a *missing*
  stored-epoch field as unknown and do a one-time full-outbox read. (Cleanup is
  unaffected either way, since `clean` and the probe read the full outbox.)
- `clean` and the probe program read the full outbox already and evaluate the
  definition directly.

Consumers to re-key — all of them, not just the auto-quiet exemption:

- `handoff_watcher.py:578-581` — fatal-tail auto-quiet exemption. Today's
  `events` half sees only *newly observed* events, so without the derivation
  every poll after the first loses `fatal_tail` and quiets a badly-dying run.
- `handoff_watcher.py:624` — **the ring trigger itself**
  (`ring_once = state in {"awaiting_review", "failed"}`). With `failed` deleted
  and nothing put in its place, a fatal run rings once via `error_while_working`
  (`:625-627`, also newly-observed-only) and then goes permanently silent.
- `handoffctl.py:266` (`_dispatch_local`) and `:496` (`_stop_local`) — both
  refuse on `"failed"`; once no new run can carry that state they silently stop
  refusing.
- `runs clean --fatal` and `REMOTE_PROBE_PROGRAM` — the probe row carries the
  derived fatal boolean.

**Post-fatal projection, specified:** a fatal `error` event leaves the worker's
projected state at whatever it was (`working`/`blocked`/…); fatality is not a
state. The **epoch-terminality invariant** at `handoff.py:1126-1127` (a fatal
epoch accepts no further worker emissions, forcing rotation) is preserved by
keying the guard on the derived fatal fact instead of the state name. Recovery
still requires `worker-relaunched`, as today.

### Termination and liveness

| Question | Answered by |
|---|---|
| Did the orchestrator finish with this run? | explicit `finished` marker |
| Did the worker give up? | durable fatal flag |
| Is the worker alive? | transport probe |

`runs clean` terminality becomes: **finished marker**, **or** fatal flag,
**or** run dir missing (dangling), **or** *positively-absent session*.

**Positive-absence rule (required).** "Session gone" is terminal only when the
transport **answered and reported the session absent**. A transport that is
unreachable yields *unknown*, which **skips** the run — never reaps it. The
remote SSH path already fails safe this way (`handoff_clean.py:440-447`). Two
local paths currently collapse unreachable into "presumed gone" and must be
changed to return *unknown* **before** `--dead` exists:

- `handoff_clean.py:195,284` — cmux app closed, or a client rejected by
  `socketControlMode: cmuxOnly`.
- `handoff_clean.py:264-265` — the `tmux` invocation itself raising
  `AdapterError` (binary not executable — a real launchd-PATH failure mode on
  this machine). A *nonzero exit from a running* `tmux has-session` (`:266-267`)
  is genuine absence and stays terminal.

Both are harmless today because terminality comes from journals; promoted into
`--dead` unchanged, either would reap every live local worker at once.

`paused` is never terminal on its own. Safety note: only the orchestrator can
resume a paused worker, and the orchestrator is the process running `clean`, so
"paused + I decided to clean" is exactly as safe as today's `stopped`.

### Selective cleanup

Fact filters, repeatable, composable with existing `--host`, `--older-than`,
`--watcher-state`/`--orchestrator`, `--force`, `--delete-run-dir`,
`--dry-run`/`--yes`:

```
runs clean --dead      # transport answered: session absent
runs clean --fatal     # worker declared a fatal error
runs clean --finished  # orchestrator marked the run finished
```

## Alternatives considered

**A. Minimal hotfix only** — reset the projected `desired_state` on a resume
message and unstick the dead end. Cheap, no migration. Rejected as the endpoint
because it leaves the denormalized fields and the dual-use permission flag
intact; the same bug class recurs at the next asymmetric transition.
**Adopted as Phase 1.**

**B. Collapse `stopped`/`failed` into one `terminated` state + reason field.**
Still worker-reported, so still unreachable by a worker that actually dies — it
inherits the defect it was meant to fix. Rejected.

**C. Keep `succeeded`, make it non-terminal for dispatch.** Fixes the immediate
dead end but keeps a state whose only entry condition is "a message did not
arrive". Rejected.

**D. Keep `stop` for graceful wind-down before reaping.** A `paused` worker is
already at a safe point with the same durable evidence. Rejected as redundant.

**E. Hard-error on legacy vocabulary to force a synchronized upgrade.**
Impossible for local data: append-only journals mean historical records are read
forever. Read tolerance is permanent, not a one-release courtesy. Rejected.

## Implementation

### Phase 1 — hotfix (ships alone; no format change, no migration)

1. **Reset the projection at review time, not at resume time.** In
   `_apply_inbox` (`handoff.py:986-989`), a `review` or `supersede` clears a
   projected `pause` back to `run`; `pause`/`stop` set as they do today. Mirror
   it in the doctor's `expected_desired` replay (`handoff.py:1544-1549`).
   `desired_state` becomes the pure inbox function *"is the last `pause` newer
   than the last `review`/`supersede`?"*.

   Two rejected placements, both of which reproduce the incident:

   - **In `send()`** — `desired_state` is a pure inbox projection that the doctor
     replays independently and `repair_control` rebuilds via `_apply_inbox`. A
     direct write fails the doctor's `control-projection` check on every resumed
     run and lets a later repair resurrect the stale `pause`.
   - **In `_apply_inbox` but keyed on `steer`/`answer`/`supersede`** (the round-1
     revision) — `send()` applies `_apply_inbox` to its own message
     (`handoff.py:1078`), so the first resume message passes the integration
     guard at `handoff.py:1017-1022` and then *closes it*: every later work
     message in the resumed window fails `integration decision is terminal`. A
     resumed worker that asks a blocking question could never be answered
     (`_dispatch_local`'s blocked path sends `answer`, `handoffctl.py:283-291`)
     and cannot emit a result past an unresolved question (`handoff.py:1191`) —
     the incident's dead end, reproduced by the hotfix meant to cure it. It also
     flips the watcher's `concluded` predicate (`handoff_watcher.py:571-577`) to
     true for the whole resumed window, advancing `dismissed_through` to the live
     tail (`:591-593`) and silently swallowing the resumed worker's next question
     and next result.

   Clearing at *review* time keeps `pause` set across the entire resumed window,
   so the guard stays open and the watcher keeps ringing — and the original bug
   still dies, because the second `conclude`'s `review` clears the stale `pause`
   immediately before its own `pause` is appended.
2. ~~Gate conclude's idempotence on worker state.~~ **Dropped** — with step 1,
   `desired_state` is trustworthy again, and the proposed
   `desired == "pause" and status == "paused"` predicate would break
   `conclude`'s crash-retry idempotence (`resume_lifecycle`,
   `handoffctl.py:339-348`) by re-sending `pause` into
   `handoff.py:1040-1041`.

   **The existing control-keyed guard at `handoffctl.py:411-433` is retained.**
   Step 1 makes it dead code for the crash-*before*-pause window (the review has
   already cleared `pause`, so the retry's `pause` is legal), but **not** for the
   crash-*after*-pause window: there `desired == "pause"` legitimately, and
   without the guard the retry hard-fails on "pause was already requested".
3. **Unstick already-wedged runs.** `handoffctl.py:266`: drop `succeeded` from
   dispatch's refusal set — **and** allow `succeeded → working` in the projector
   on a work message newer than the accepted review. Dropping the CLI guard
   alone is insufficient: `handoff.py:1128-1129` keeps `succeeded` sticky, so the
   steer would be delivered to a worker that cannot legally act on it. (A
   two-step `succeeded → paused → working` escape is projector-legal when an old
   `pause` sits in the prefix, but no contract clause tells a worker to do it.)
4. `handoff_registry.py:22`: remove `succeeded` from `TERMINAL_STATES` so a live
   idle worker is not reap-eligible.
5. Fix the test that asserts the buggy behavior:
   `test_conclude_pause_resume_and_reconclude_cycle`
   (`tests/test_handoffctl.py:1777-1857`; assertions at `:1842-1848`, second
   instance at `:1996`). **Preserve** the crash-recovery tests
   `test_conclude_resumes_a_crash_between_integration_and_pause` (`:1643`) and
   `test_conclude_resumes_after_mid_sequence_crash` (`:1586`) — they encode
   legitimate idempotence, not the bug.

### Phase 2 — read tolerance and contract (prerequisite for any deletion)

6. Implement the write-strict/read-tolerant validators and the tolerant replay
   paths described above, plus the legacy mappings. Land **before** any
   vocabulary is retired.
7. Rewrite `_standing_instructions()` (`handoff.py:705-726`) and its embedded
   `EMIT_DATA_SCHEMAS` for the new lifecycle, and extend skew tolerance to
   legacy *worker emissions*. This must ship with the state change, not in a
   later phase: contracts are frozen per-run at `initialize()`
   (`handoff.py:830`), so a worker launched against a new validator with an old
   contract would emit checkpoints the validator rejects.
8. Migration script (precedent: `scripts/migrate_handoff_state_orchestrator.py`):
   map `succeeded`/`stopped` status snapshots to `paused`, and **backfill the
   `finished` marker for every run with `desired_state == "stop"`**. Without the
   backfill, existing concluded-and-resident runs have no marker, no fatal
   derivation, and a live session, so `runs clean` would skip them all and
   require `--force`. For **remote** records the orchestrator host has no local
   `desired_state` to key on: probe the owning host and write the mirrored
   value. If the owning host is unreachable mid-migration, **skip and report** —
   never guess a marker value; the run stays inspectable and can be backfilled
   on a later pass. (`scripts/update_agents.sh` host ordering determines when
   the probe can succeed, so migrate the owning hosts before the orchestrator
   host.) Legacy checkpoints replay under **legacy acceptance**, not the new
   entry rules — a worker `stopped` mid-work has no consumed accepted review, so
   validating its mapped `paused` under the new entry condition would make the
   doctor's full-outbox replay (`handoff.py:1576-1583`) fail legitimate legacy
   runs.

### Phase 3 — state and message collapse (ships as one release with Phase 2)

9. Remove `succeeded`/`stopped` from the *writable* checkpoint schema; re-key
   `paused` entry and the resume predicate as specified in the design.
10. Delete `pause`/`stop` from the *writable* message vocabulary and remove
    `desired_state` and its projection; apply the deliverability rule at
    `handoff.py:1017-1022`. Decide explicitly whether the
    `changes_requested`-after-success guard (`handoff.py:1035`) gains a
    `paused`-era equivalent or is deliberately dropped.

    **`_unresolved_questions`' stop clause (`handoff.py:1091-1094`) stays keyed
    on legacy consumed `stop` inbox records** under read tolerance — *not*
    re-homed onto the finished marker, as an earlier revision proposed.
    `handoff.py` has no registry access (the same constraint that made a stored
    fatal flag unworkable), and no new source is needed: `stop` becomes
    unsendable, and `dispatch` refuses finished runs, so the clause simply keeps
    serving the legacy records it was written for.
11. `handoffctl.py`: update `conclude`'s `resumable`/`resume_lifecycle`
    predicates (`:332-348`) — keyed on `succeeded` today, they break the moment
    workers report `paused`; convert `_stop_local` (`:496-501`) into the
    finished-marker write (marker written **last**, after the review and
    integration records are durable); re-key the `"failed"` refusals at `:266`
    and `:496` onto the derived fatal fact; and apply the refusal scope —
    `dispatch` and non-stop `conclude` refuse a finished-marked run, while
    `conclude --stop`/`stop` short-circuit to idempotent success.
12. `handoff_watcher.py`: recompute `concluded` (`:571-577`) from the finished
    marker; switch **both** fatal consumers — the auto-quiet exemption
    (`:578-581`) and the ring trigger `ring_once` (`:624`) — onto the derived
    fatal fact.
13. `handoff_clean.py`: terminality from marker + fatal + dangling +
    positive-absence probe; make the local cmux (`:195,284`) and tmux
    `AdapterError` (`:264-265`) paths return *unknown* on an unreachable
    transport; add `--dead` / `--fatal` / `--finished`; extend
    `REMOTE_PROBE_PROGRAM` to carry the marker and the derived fatal boolean.
    **The local candidate gate (`handoff_clean.py:506-508`) becomes
    `_assess_terminal(record) OR positively-absent probe`** — today it skips
    non-terminal runs *before* any transport probe runs, so a `--dead` filter
    layered over the existing gate would select nothing. That is the same
    select-then-refuse failure class step 14 fixes for `--finished`/`--fatal`.
14. **`handoff_registry._assess_terminal` (`:278-289`) re-keyed onto finished
    marker + derived fatal + dangling.** This is the local reap gatekeeper,
    called from `handoff_clean.py:271` (the kill-time re-verify), `:376`, and
    `:506`. Left on `TERMINAL_STATES` + `desired_state`, a finished-marked local
    run projects `paused` with no `desired_state` and assesses as *non-terminal*:
    the kill-time re-check would veto the kill ("run went live during cleanup")
    and record removal would refuse — so `runs clean --finished` / `--fatal`
    would select runs it then refuses to reap without `--force`, defeating the
    selective-cleanup feature this plan adds. Must ship in the same phase as
    step 13.

### Phase 4 — docs

15. Update `docs/architecture.md` (state list, desired-state-as-terminality) and
    `~/skills/handoff-agent/SKILL.md` + `references/*.md`: pause/stop/conclude
    vocabulary, teardown, and the claim that a paused worker "resumes via
    `dispatch` … and a later `conclude` works".

## Testing and verification

- Regression for the incident: conclude → pause → dispatch steer → resume →
  result → conclude, asserting the second conclude idles the worker and a third
  dispatch is accepted.
- **Multi-message resumed window** (catches the round-2 blocker, which the
  single-steer regression above cannot): conclude → pause → steer → worker asks a
  blocking question → `answer` is accepted → worker resumes → second steer is
  accepted. Assert every message after the first still passes the integration
  guard.
- **Watcher through a resumed window**: after conclude → pause → steer, the
  watcher must *not* mark the run concluded, and the resumed worker's next
  blocking question and next result must both ring.
- Doctor parity: after a resume, `doctor` reports no `control-projection`
  mismatch, and `repair_control` does not resurrect `pause`.
- Crash-retry idempotence preserved: the two `conclude` crash-resume tests still
  pass unmodified.
- Wedged-run recovery: a run already at `succeeded` accepts a steer and reaches
  `working`.
- Legacy read tolerance: a run dir whose journals contain `pause`/`stop`
  messages and `succeeded`/`stopped` checkpoints loads, statuses, doctors, and
  cleans without error.
- Resume without a `pause` seq: a worker idled by a bare accepted review resumes
  on the next steer.
- `--dead` safety: transport unreachable ⇒ run skipped, not reaped (assert for
  both the SSH path and the local cmux path).
- Migration: a `desired_state == "stop"` run gains a finished marker and remains
  reapable without `--force`.
- Fatal derivation: watcher keeps ringing a fatal run across multiple polls, not
  just the poll that first observed the event; and a `worker-relaunched` epoch
  bump makes the run non-fatal with no explicit clear.
- Finished marker reaches the watcher for a **remote** run (mirrored into the
  orchestrator-side record), so a remote concluded run auto-quiets.
- `dispatch` and a non-stop `conclude` refuse a finished-marked run, while
  `conclude --stop` / `stop` against one returns idempotent success and rewrites
  the mirror (assert the lost-response retry path specifically).
- `runs clean --finished` and `--fatal` actually reap what they select — the
  kill-time `_assess_terminal` re-verify must agree with the selection filter,
  without `--force`.
- `runs clean --dead` actually reaps a run whose transport **answered
  session-absent**, without `--force` (the positive direction; the skip-on-
  unreachable direction is covered above).
- A run already fatal **before** the upgrade still rings after it — i.e. the
  watcher's stored-epoch backfill works for events observed by the old watcher.
- The lost-response `conclude --stop` retry asserts the **review record exists**,
  proving marker-last ordering rather than just idempotent success.
- Legacy replay: a run whose worker `stopped` mid-work (no accepted review) still
  passes the doctor's full-outbox replay after mapping.
- Manual: one local and one remote run through launch → result → conclude →
  dispatch → result → conclude → clean.
- Full `pytest tests/test_handoff*.py` after each phase.

## Risks and rollback

- **Phases 2+3 must ship together.** Splitting them leaves either a validator
  that rejects the shipped contract or a resume predicate that cannot fire.
- **Local/remote skew** spans three surfaces, not one: message types, worker
  emissions, and the **probe-row/CLI vocabulary** (`REMOTE_PROBE_PROGRAM`
  returns `state: "succeeded"`/`desired_state`, and remote `conclude --stop`
  runs the *remote* host's handoffctl). Read tolerance covers all three for the
  transition; `update_agents.sh` both hosts together.
- **Finished-marker durability.** If the marker write is best-effort, a finished
  run looks merely paused and never gets reaped. It must be durable and, for
  remote runs, written on the owning host. Verify before Phase 3.
- **Rollback**: Phase 1 is a pure bugfix with no format change and can ship
  alone. Phase 2's read tolerance is additive and safe to keep even if Phase 3
  is abandoned.

## Resolved open questions (round 1)

1. **Is deleting `stopped` safe given SSH probes?** Yes, *given* the
   positive-absence rule — unreachable transport skips rather than reaps, which
   the SSH path already does.
2. **Is the integration record a sufficient finished-marker?** No. It is
   per-result and cannot distinguish "parked between cycles" from "done". An
   explicit marker is required.
3. **Is accept-and-ignore the right skew policy?** Right for remote skew, wrong
   as the frame for local data: append-only journals make read tolerance
   permanent, not a one-release window.
