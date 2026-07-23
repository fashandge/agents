# Handoff two-round end-to-end test

A manual/agent-driven end-to-end exercise of the handoff protocol across a
**fresh orchestrator session** and **two rounds of worker work**. It is written
to be executed by an agent, so every step is a literal command with a checkable
assertion rather than prose.

## What this exercises

- Watcher bootstrap in a brand-new orchestrator session (`--transport cmux`).
- Two workers on **two different transports** at once: a local `cmux` worker and
  a remote `ssh_tmux` worker on `oci-box`.
- The doorbell → `orchestrator pending` → review → `conclude` loop.
- **Round 2 is the point of the test.** A worker that is accepted with
  `--no-integrate`, pauses, is resumed by `dispatch`, and produces a *second*
  result is the exact shape that regressed in `conclude` (fixed in `469f87a`,
  covered by `test_conclude_reviews_new_work_after_a_no_integrate_accept`).
  A single-round test cannot catch it. See the round-2 assertion below.

Round 1 alone will pass even against the broken build. Do not shorten this test.

## Preconditions

```bash
~/projects/agents/scripts/fleet_status.sh          # every checkout dirty=0
ssh -o ConnectTimeout=8 oci-box 'echo ok; which codex tmux'
<helper> runs list                                 # ideally []
```

`<helper>` throughout is:

```text
/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python -m agents.orchestration.handoffctl
```

If the box is stopped and the request authorizes using it:
`~/projects/investment/src/scripts/oci_box_ctl.sh up`.

Pick a unique run-name prefix per execution (e.g. `e2e-<MMDD>-a` / `-b`) so a
previous execution's registry records and scratch dirs can never be mistaken
for this one's.

## Procedure

### Phase 0 — fresh orchestrator session in a new cmux tab

The orchestrator under test must be a **new** Claude session, not the session
driving this document. Snapshot layout first; every later layout check compares
against this.

```bash
CMUX_QUIET=1 cmux --id-format both list-workspaces
CMUX_QUIET=1 cmux --id-format both list-pane-surfaces --workspace "$CMUX_WORKSPACE_ID"
```

Create the tab unfocused, capture its UUID, and start Claude in it:

```bash
cmux --id-format both new-surface --type terminal \
  --workspace "$CMUX_WORKSPACE_ID" --focus false      # parse the surface UUID
cmux rename-tab --workspace "$CMUX_WORKSPACE_ID" --surface <uuid> e2e-orchestrator
cmux send --workspace "$CMUX_WORKSPACE_ID" --surface <uuid> "cd ~/projects/agents && claude"
cmux send-key --workspace "$CMUX_WORKSPACE_ID" --surface <uuid> enter
```

**Submit discipline (applies to every send in this document):** agent TUIs
coalesce a same-burst text+Enter into a bracketed paste, leaving the text
unsent in the composer while looking delivered. Always send the text and the
Enter as **two separate commands** ~1 s apart, then `cmux read-screen` to
confirm the composer is empty. Re-send a bare Enter if it is not.

Wait for the Claude TUI to be ready (`read-screen` shows its composer), then
send the driving prompt as text-then-Enter. The prompt must make the new
session do the work itself — the observer never launches the workers:

> Use /handoff-agent. Spawn two workers: one Codex worker on the remote box
> `oci-box` and one Claude worker locally. Round 1: have each compute
> `<expression A>` — closed form plus an independent check with its own
> kickoff-pinned Python, scratch dir only, no repo writes, no commits. Review
> and `conclude --no-integrate` each. Round 2: once both are paused, dispatch
> each a follow-up to compute `<expression B>` and conclude those the same way.
> Report each run id, the review `reply_to`, and `consumed_through`.

Use two *different* expressions with distinct values (e.g. `12 * 12` → 144,
then `100 - 37` → 63) so a stale round-1 answer can never be mistaken for a
correct round-2 answer.

### Phase 1 — round 1

The orchestrator under test performs: watcher bootstrap, two launches, doorbell
→ `pending`, scope verification, `conclude --no-integrate` per run.

Observer assertions after round 1:

```bash
<helper> runs list                       # 2 records, both orchestrator_id = the NEW session's
<helper> runs show --run <selector>
```

For each run:

- `review_state == "accepted"`
- `review_result_id` == the message_id of that run's **round-1** result
- `outbox_cursor` == that run's outbox length
- `integration.state == "pending"` (expected: `--no-integrate` was used)
- worker status settles at `state: paused`

### Phase 2 — round 2 (the regression-sensitive round)

The orchestrator dispatches follow-up work to both paused workers and concludes
the second results.

```bash
<helper> dispatch --run <selector> --body-file <private-file>
```

Expect `message.type == "steer"`, `doorbell_sent: true`,
`orchestrator_released: true`.

**Key assertion — this is what round 2 exists for.** After the second
`conclude` on each run:

- `review.reply_to` == the **round-2** result message_id — *not* the round-1 id
- `consumed_through` == the new outbox length (it must advance past round 1)
- `control.review_result_id` == the round-2 result id

A conclude that returns `review: null`, or a `reply_to`/`review_result_id`
still pointing at the round-1 result while reporting
`"disposition": "accepted"`, is the crash-recovery misroute. It is a **silent**
failure: the JSON says accepted and the worker stays `awaiting_review` forever.
Never take `"disposition": "accepted"` at face value — always read `control
show` back.

### Phase 3 — teardown

```bash
<helper> stop --run <selector>                                   # each run
<helper> runs clean --watcher-state <new session's watcher.json> --dry-run
<helper> runs clean --watcher-state <new session's watcher.json> --yes
```

Always scope with `--watcher-state`; an unscoped bulk clean considers **every**
run in the registry, including other sessions' workers. Omit `--delete-run-dir`
unless the run journals are also meant to go — they are the evidence for this
test. Close the orchestrator-under-test tab with the guarded helper only:

```bash
~/projects/agents/scripts/cmux_close_surface_safe.sh --surface <uuid>
```

## Observation

Two independent evidence channels; the durable one wins whenever they disagree.

**Durable journals (authoritative).** Terminal text is never durable state.

```bash
<helper> runs show --run <selector>
<helper> status      --run-dir <abs-run-dir>
<helper> control show --run-dir <abs-run-dir>
<helper> read --run-dir <abs-run-dir> --journal outbox --after 0
<helper> read --run-dir <abs-run-dir> --journal inbox  --after 0
<helper> doctor --run-dir <abs-run-dir>        # read-only; never pass repair flags here
```

A remote run's journals live on its owning host — run the same commands there
over SSH with that host's Python.

**Screen reads (diagnostic only).** Useful for spotting a worker stuck below
the agent loop (folder-trust dialog, permission prompt) that the journals
cannot show:

```bash
cmux read-screen --surface <uuid> --scrollback          # local worker / orchestrator tab
ssh oci-box 'tmux capture-pane -p -t <handle> -S -2000' # remote worker
```

**Scope verification.** The tasks are arithmetic, so any repo change is a bug:

```bash
ls -la <each worker's scratch dir>       # only the worker's own emit-staging files
~/projects/agents/scripts/fleet_status.sh # all checkouts still dirty=0, HEAD unchanged
```

## Pass criteria

1. Two runs registered to the **new** session's orchestrator id, one
   `transport: cmux`, one `transport: ssh_tmux`.
2. Round 1: both results correct, reviews tied to the round-1 result ids.
3. Round 2: both results correct **and** reviews tied to the **round-2** result
   ids, with `consumed_through` advanced. (The regression-sensitive check.)
4. Every `dispatch`/`conclude` reports `doorbell_sent: true` and
   `orchestrator_released: true`.
5. No worker wrote outside its scratch dir; `fleet_status.sh` unchanged
   throughout; no commits.
6. Teardown: registry empty of these runs, remote tmux session gone, local
   worker surface gone, and **every pre-existing workspace and surface still
   present**.
7. `doctor` reports ok for each run before teardown.

## Known pitfalls

- **The forced doorbell preamble is legitimate, not an attack.** When the
  orchestrator's composer already holds a human draft, the doorbell is prefixed
  with `⟦handoff doorbell⟧ Ignore everything before this sentence: it is an
  unfinished draft… quote it back verbatim…` (`FORCED_DOORBELL_PREAMBLE`,
  `orchestration/handoff_launcher.py`, added in `7b3d488`). It is a fixed
  template carrying no event bodies or credentials, and it exists so submitting
  the doorbell does not destroy the human's draft. It is injection-*shaped*,
  and a fresh orchestrator that has not read the launcher source will very
  likely report it as a prompt-injection attack — expect that false positive
  and confirm provenance with `git log -S FORCED_DOORBELL_PREAMBLE` before
  escalating.
- **Pin the repo revision.** Record `git rev-parse HEAD` on both hosts at the
  start and end of the run and diff them. Other agent sessions may commit to
  this repo mid-test; a change to `handoff_clean.py`/`handoffctl.py` under a
  running test invalidates the teardown assertions. If HEAD moved, note it in
  the result and re-run rather than reasoning about a moving target.
- **`startup_unconfirmed: true` with `worker_ready: true`** on a remote Codex
  launch is usually the 30 s ready gate racing the worker's ready checkpoint,
  not a stuck trust dialog. Probe the pane once to confirm before any rescue.
- **A repeating doorbell on a result** is cured by `orchestrator pending`,
  never by killing the watcher or closing its surface.
- **Never rename a mirrored workspace** while a remote run is live — the
  control-mode mapping is bidirectional and renaming it renames the remote tmux
  session, breaking the registered handle.
- **`runs clean` skips unmarked paused workers by design.** Mark finished with
  `stop` first; that is not a bug to work around.
- **A `git stash` "revert" of a committed fix is a no-op.** To A/B a fix, use
  `git checkout <pre-fix-sha> -- <file>` and restore with
  `git checkout HEAD -- <file>`; a stash of a clean tree silently stashes
  nothing and the "before" run is really an "after" run.
