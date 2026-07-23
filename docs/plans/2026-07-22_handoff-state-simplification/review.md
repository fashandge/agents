# Review log — handoff state-machine simplification

## Round 1 — claude-fable-5 (xhigh)

**VERDICT: REVISE** — 2 blockers, 7 majors, 3 minors, 1 nit.

All findings were verified against the code before disposition. No finding was
rejected; three of them corrected factual errors in my own draft.

### Blockers

- **Strict read-validation bricks existing run dirs.** `_journal` re-validates
  every record on every read (`handoff.py:641` → `_validate_message` →
  `_validate_type_data`) and `_fields` (`:147`) enforces exact key sets. Since
  journals are append-only and run dirs are kept inspectable, deleting any
  vocabulary retroactively breaks every run that ever used it — the draft's
  "map on load" covered only the status snapshot, and "accept-and-ignore for one
  release" only newly-arriving messages.
  **ACCEPTED — verified `handoff.py:641`, `:612`, `:147`.** Added a permanent
  write-strict/read-tolerant validation policy as a design section, and made it
  Phase 2, a prerequisite for any deletion. Added alternative E recording why a
  hard-error skew policy is impossible for local data.

- **"Only the integration record is terminal" recreates the dead end.**
  `conclude` auto-integrates whenever a result reports a head
  (`handoffctl.py:397-398`; `emit` forces a real HEAD for git workspaces at
  `handoff.py:1288-1296`), and the integration record resets only when a new
  result is consumed (`handoff.py:1317-1320`). The `desired_state == "pause"`
  escape at `handoff.py:1017-1022` is the *only* thing letting a parked
  integrated worker be steered — deleting it without a replacement rule
  reproduces the incident for every integrated run, and poisons the watcher's
  `concluded` predicate the same way.
  **ACCEPTED — verified all four sites.** Added an explicit deliverability rule
  (work messages deliverable whenever the worker epoch is live, regardless of
  the integration record) and an explicit orchestrator-written `finished`
  marker that both `clean` and the watcher key on. This also answers open
  question 2 in the negative.

### Majors

- **Phase 1 step 1 in `send()` desynchronizes the projection.** `desired_state`
  is a pure inbox projection; the doctor replays it independently
  (`handoff.py:1544-1549`) and `repair_control` rebuilds it via `_apply_inbox`.
  A direct `send()` write would fail the doctor's `control-projection` check on
  every resumed run and let a later repair resurrect the stale `pause`.
  **ACCEPTED — verified `handoff.py:1544-1549`.** Moved the fix into
  `_apply_inbox` with a mirrored change in the doctor's replay. This was the
  single most valuable finding: my original fix would have silently re-armed the
  exact bug it was meant to remove.

- **Phase 1 step 2 breaks conclude's crash-retry idempotence.** Gating on
  `status == "paused"` makes a conclude retried in the window after `pause` was
  sent but before the worker checkpointed `paused` (the `resume_lifecycle` path,
  `handoffctl.py:339-348`) re-send `pause` and hard-fail at
  `handoff.py:1040-1041`.
  **ACCEPTED — verified `handoffctl.py:332-348`.** Dropped step 2 entirely and
  recorded why, since step 1 makes `desired_state` trustworthy again.

- **Phase 1 step 3 does not actually unblock the wedged worker.** `succeeded` is
  sticky in the projector (`handoff.py:1128-1129`): a succeeded worker may
  checkpoint only `succeeded`/`stopped`/`paused`, never `working`. Dropping the
  CLI guard delivers a steer the worker cannot legally act on.
  **ACCEPTED — verified `handoff.py:1128-1129`.** Step 3 now also adds a
  `succeeded → working` transition on a work message newer than the accepted
  review.

- **Deleting `failed` is under-designed; step 10 is self-contradictory.** The
  draft said to keep the watcher's fatal-tail exemption "unchanged", but half
  its predicate is `status["state"] == "failed"`, and the `events` half only
  covers newly observed events — so after the first poll a badly-dying run would
  auto-quiet. Post-fatal projection and the epoch-terminality invariant
  (`handoff.py:1126-1127`) were unspecified, and `--fatal` needs probe support.
  **ACCEPTED — verified `handoff_watcher.py:578-581`, `handoff.py:1126-1127`.**
  Added a durable fatal-flag design section covering the watcher, epoch
  terminality, post-fatal projection, and the probe row.

- **"Session gone ⇒ terminal" needs positive evidence of absence.** The draft
  claimed clean "already probes the transport"; in fact `session_alive` is
  computed for remote runs but is not a terminality input (`:78-82`), the local
  path never probes, and the local cmux code treats *unreachable* as "presumed
  gone" (`:195,284`) — which would classify every live local worker as
  reapable once promoted into `--dead`.
  **ACCEPTED — verified.** Corrected the overstated claim and added a mandatory
  positive-absence rule: transport answered + session absent = terminal;
  transport unreachable = unknown = skip. This also resolves open question 1.

- **Phase 2→4 sequencing breaks the worker contract.** The contract is generated
  by `_standing_instructions()` (`handoff.py:705-726`) — not the launcher, as the
  draft said — and frozen per-run at `initialize()` (`:830`). Removing
  `succeeded` from the schema while the contract rewrite waited for Phase 4 would
  instruct new workers to emit rejected checkpoints.
  **ACCEPTED — verified.** Contract + `EMIT_DATA_SCHEMAS` rewrite moved into
  Phase 2, with skew tolerance extended to legacy worker emissions.

- **Two Phase-2-required changes were scheduled late or missing.** (a) The
  paused→working resume rule (`handoff.py:1150-1161`) keys on a `pause` **seq**;
  a worker idled by a bare accepted review has none and could never resume —
  the very bug class this plan fixes. (b) `conclude`'s `resumable` predicate
  (`handoffctl.py:334-338`) is keyed on `succeeded` and breaks once workers
  report `paused`.
  **ACCEPTED — verified both.** Resume re-keyed onto the accepted review;
  predicate updates folded in; Phases 2 and 3 now explicitly ship as one release.

- **Legacy concluded runs lose terminality with no backfill.** Runs concluded
  with `--stop` are terminal today via the `desired_state` clause; afterwards
  they would have no marker, no fatal flag, and a live session, so `clean` would
  skip them all.
  **ACCEPTED.** Migration now backfills finished markers for
  `desired_state == "stop"` runs.

### Minors / nit

- **Wrong test citation.** The buggy-behavior assertions are in
  `test_conclude_pause_resume_and_reconclude_cycle`
  (`tests/test_handoffctl.py:1777-1857`, assertions `:1842-1848`, second
  instance `:1996`), not `:1646-1668`, which is
  `test_conclude_resumes_a_crash_between_integration_and_pause` — legitimate
  crash recovery that must be preserved.
  **ACCEPTED — verified by resolving enclosing functions; the reviewer was right
  and my citation was wrong.** Corrected, with an explicit "preserve these"
  note naming both crash-resume tests.

- **Missed consumers**: `handoffctl.py:500` (`_stop_local` guard), the doctor's
  semantic replay (`handoff.py:1539-1571`), `handoff.py:1035`
  (changes-requested-after-success), and `_unresolved_questions`' stop clause
  (`handoff.py:1091-1094`, where a consumed stop clears blocking questions — an
  invariant that would silently vanish).
  **ACCEPTED — verified.** All four enumerated in the consumer table and in the
  phase steps.

- **Skew coverage should include probe rows and CLI vocabulary**, since remote
  `conclude --stop` runs the remote host's handoffctl and `REMOTE_PROBE_PROGRAM`
  returns old-vocabulary rows.
  **ACCEPTED.** Risks section now names all three skew surfaces.

- **nit: `docs/architecture.md`** also documents the deleted vocabulary.
  **ACCEPTED.** Added to Phase 4.

### Open questions — resolved

1. Deleting `stopped` is safe **given** the positive-absence rule.
2. The integration record is **not** a sufficient finished-marker; an explicit
   marker is required, readable on the owning host for remote runs.
3. Accept-and-ignore is right for remote skew but wrong for local data: read
   tolerance is permanent.

## Round 2 — claude-fable-5 (high)

**VERDICT: REVISE** — 1 blocker, 4 majors, 3 minors.

The reviewer explicitly verified the round-1 revisions and found them sound
(read-tolerant policy, deliverability rule, re-keyed resume predicate, corrected
test citations, Phase 1 steps 3-5, the dropped step 2, the Phase 2+3 same-release
constraint). The remaining findings concentrate on one root issue plus two
specification gaps.

### Blocker

- **The round-1 Phase 1 fix reproduces the incident.** Clearing the projected
  `pause` on `steer`/`answer`/`supersede` inside `_apply_inbox` means `send()`
  applies the clear to its own message (`handoff.py:1078`): the first resume
  message passes the integration guard (`handoff.py:1017-1022`) and then closes
  it, so every later work message in the resumed window fails "integration
  decision is terminal". A resumed worker that asks a blocking question can
  never be answered (`handoffctl.py:283-291`) and cannot emit a result past the
  unresolved question (`handoff.py:1191`) — a live, blocked, unreachable worker,
  shipped by the standalone hotfix. The planned single-steer regression test
  could not catch it.
  **ACCEPTED — verified `handoff.py:1078` applies `_apply_inbox` to the sent
  message.** Re-homed the clear to `review`/`supersede`, so `desired_state`
  becomes the pure inbox function "last `pause` newer than last
  `review`/`supersede`". `pause` now survives the whole resumed window (guard
  stays open) while the original bug still dies, because the second `conclude`'s
  `review` clears the stale `pause` immediately before appending its own. Both
  rejected placements are recorded in the plan with their failure modes, and a
  multi-message resumed-window regression test was added.

### Majors

- **The resume-time clear flips the watcher's `concluded` predicate**
  (`handoff_watcher.py:571-577`), advancing `dismissed_through` to the live tail
  (`:591-593`) and swallowing the resumed worker's next question and result.
  **ACCEPTED — verified.** Fixed by the same re-homing (desired stays `"pause"`
  through the resumed window); added a watcher regression test.

- **The fatal-flag design misses the ring trigger and the CLI refusals.**
  `handoff_watcher.py:624` (`ring_once = state in {"awaiting_review", "failed"}`)
  is what keeps a fatal run ringing; with `failed` deleted it would ring once via
  the newly-observed-only `error_while_working` and then go silent.
  `handoffctl.py:266` and `:496` also key on `"failed"`.
  **ACCEPTED — verified `handoff_watcher.py:624`.** All four consumers now
  enumerated, and post-fatal projection specified (fatality is not a state; the
  epoch-terminality guard re-keys onto the derived fact).

- **Fatal-flag storage reintroduces cached derived state.** No writer, no
  location, and an invalidation obligation on `worker-relaunched` — the pattern
  principle 1 bans. The fact is already journal-derivable.
  **ACCEPTED — this is a better design than mine.** Fatal is now *derived*: an
  `error` event with `fatal: true` whose `writer_epoch == control["worker_epoch"]`.
  Epoch-scoped for free, so a relaunch un-fatals the run with no explicit clear.

- **The finished marker is unreadable where the watcher needs it for remote
  runs.** `_protocol_state` (`handoff_watcher.py:471-497`) fetches only
  `control show` / `status` / `read` over SSH — never a registry — so a
  registry-only marker would never auto-quiet a remote concluded run. The
  migration backfill has the mirror-image problem.
  **ACCEPTED — verified.** `_conclude_registered` now mirrors the marker into the
  orchestrator-side record; the backfill probes the owning host for remote
  records.

### Minors

- **Legacy `stopped` replay must bypass the new `paused` entry conditions** — a
  worker stopped mid-work has no consumed accepted review, so validating its
  mapped state under the new rule would fail the doctor's full-outbox replay
  (`handoff.py:1576-1583`). **ACCEPTED**, folded into the migration step.
- **Dispatch against a finished run is unspecified** — an ex-`stopped` run reads
  as `paused` with a live epoch, so `dispatch` would steer a dead session and
  report success. **ACCEPTED**: `dispatch`/`conclude` refuse a finished-marked
  run.
- **Positive absence must cover the local tmux `AdapterError` branch**
  (`handoff_clean.py:264-265`), which maps an unexecutable `tmux` binary — a real
  launchd-PATH failure mode here — to "presumed gone". **ACCEPTED**, added
  alongside the cmux paths, with the distinction that a nonzero exit from a
  *running* `tmux has-session` is genuine absence.

### Partially rejected (round 2)

- The blocker's fix note claims the re-homing "also removes conclude's
  crash-retry special case at `handoffctl.py:411-433`, since a retried conclude
  just re-sends pause legally."
  **REJECTED (sub-claim only; the fix itself is accepted).** That holds for the
  crash-*before*-pause window — the review has already cleared `pause`, so the
  retry's `pause` is legal — but not for the crash-*after*-pause window, where
  `desired == "pause"` legitimately and the `resume_lifecycle` path
  (`handoffctl.py:339-348`) would re-send `pause` into the hard failure at
  `handoff.py:1040-1041`. The guard is retained, and the plan now records which
  window it still covers.

## Round 3 — claude-fable-5 (high)

**VERDICT: REVISE** — 0 blockers, 3 majors, 2 minors, 1 nit.

The reviewer independently traced `desired_state` to every reader (send guards,
doctor replay, `stopped` entry, watcher `concluded`, clean, registry assessor,
both handoffctl guards) and confirmed the review-time clear is coherent at all
of them. It also re-verified both crash windows and confirmed the round-2
rejection of the `handoffctl.py:411-433` sub-claim, without re-raising it.

### Majors

- **`handoff_registry._assess_terminal` was never re-keyed** — it is the reap
  gatekeeper (`:281`, `:287-288`), called from `handoff_clean.py:271` (kill-time
  re-verify), `:376`, `:506`, and no phase step touched it. Post-Phase 3 a
  finished-marked local run projects `paused` with no `desired_state`, so it
  assesses non-terminal: `runs clean --finished`/`--fatal` would *select* runs it
  then *refuses* to reap without `--force`. The consumer table also mislabeled
  these lines "run records".
  **ACCEPTED — verified `handoff_registry.py:278-289` and all three call sites.**
  Added step 14 re-keying it, pinned to the same phase as step 13, and corrected
  the table label.

- **The watcher cannot compute the fatal derivation "from data it already
  fetches"** — `_protocol_state` returns only `records[after:]`
  (`handoff_watcher.py:471-497`), so after the first observing poll the event is
  gone from `events` and post-collapse `status` never reads `"failed"`. As
  specified, the derivation reproduced the newly-observed-only failure rounds
  1-2 flagged.
  **ACCEPTED — my claim was simply false; verified.** The watcher now persists
  the observed fatal `writer_epoch` in per-run state and derives
  `fatal == (stored_fatal_epoch == control["worker_epoch"])` — self-invalidating
  on relaunch, so still principle-1 clean. `clean` and the probe read the full
  outbox and evaluate directly.

- **Finished-marker mirroring deadlocks with the conclude-refusal rule** — if a
  remote `conclude --stop` succeeds on the owning host but the response is lost
  (SSH teardown, `_remote_json` decode error, crash before the local write), the
  natural repair — retrying — is refused under "conclude refuses a
  finished-marked run". The mirror can then never be written and the run never
  auto-quiets, with no reconciliation path.
  **ACCEPTED.** Refusal scope is now non-uniform and the reasoning recorded:
  `dispatch` and non-stop `conclude` refuse; `conclude --stop`/`stop` return
  idempotent success (`finished_already: true`) and rewrite the mirror.

### Minors / nit

- **Re-homing `_unresolved_questions` onto the finished marker is infeasible** —
  `handoff.py` has no registry access, the same constraint that killed the
  stored fatal flag.
  **ACCEPTED — this was a self-inconsistency in my own revision.** The clause
  stays permanently keyed on legacy consumed `stop` records under read
  tolerance; no new source is needed since `stop` becomes unsendable and
  `dispatch` refuses finished runs.
- **`handoff_watcher.py:498` missing from the read-tolerance enumeration** — it
  rejects any remote status outside `handoff.WORKER_STATES`, so shrinking that
  set would make a skewed older remote host error rather than read.
  **ACCEPTED — verified.** The validation-policy section now states that
  `WORKER_STATES` permanently retains the retired states on the read path.
- **nit: remote backfill probe has no unreachable-host behavior.**
  **ACCEPTED.** Skip-and-report, never guess a marker; migrate owning hosts
  before the orchestrator host so the probe can succeed.

## Round 4 — claude-fable-5 (high)

**VERDICT: APPROVE** — 0 blockers, 0 majors, 3 minors, 1 nit.

The reviewer verified all six round-3 fixes against the code and resolved the
three focus questions affirmatively: the persisted-epoch mechanism is sound
across watcher restarts (epoch and cursor persist in the same atomic write,
before doorbells) and across state-file loss (the cursor resets with the file,
forcing a full outbox re-read that re-observes the event); the non-uniform
refusal is coherent for local runs because local conclude/stop write the marker
directly with no mirror to lose; and step 14 agrees with the kill-time
re-verify. It also confirmed the dropped `TERMINAL_STATES` clause is complete
for legacy data, and that `handoff.py` is *structurally* unable to import the
registry (the registry imports it) — independently confirming the round-3
revert.

All four residual findings were taken rather than deferred.

- **Stored fatal epoch has no backfill for pre-upgrade observations** — a run
  whose fatal event was observed by the old watcher has `observed_through` past
  it and no stored epoch, and `_protocol_state` never re-fetches it, so it would
  derive non-fatal and go permanently quiet at upgrade.
  **ACCEPTED.** Added an upgrade-backfill clause (legacy `failed` as a
  read-tolerant OR-term, or missing-field ⇒ unknown ⇒ one-time full-outbox read)
  plus a test.
- **`conclude --stop` marker-write ordering was unspecified** — `finished_already`
  keys on the marker alone, so a marker written before the review/integration
  records are durable would let a crash short-circuit the retry with the review
  never sent, parking the worker in `awaiting_review`.
  **ACCEPTED.** Marker write is now explicitly the final step of the sequence,
  in the refusal-scope section and step 11; the retry test asserts the review
  record exists.
- **No positive `--dead` reap test, and the local candidate gate was implicit** —
  `handoff_clean.py:506-508` skips non-`_assess_terminal`-terminal runs *before*
  any probe, so `--dead` layered over it would select nothing: the same
  select-then-refuse class step 14 fixed.
  **ACCEPTED.** Step 13 now specifies the gate as
  `_assess_terminal(record) OR positively-absent probe`, with a positive reap
  test.
- **nit: duplicate step numbering** (two step 14s). **ACCEPTED** — docs step
  renumbered to 15.

**Note on final state:** the APPROVE verdict was rendered against the plan as it
stood before these four minor edits. Per the flow-1 stop rule, minors are taken
at the session's judgment and no further round is required; the edits are
additive clarifications and one renumbering, with no design change.
