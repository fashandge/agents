# Agent handoff & orchestration: communication design

Date: 2026-07-14 · Revised: 2026-07-17 · Status: DESIGN v1.3
(normative and implemented for the local protocol plus local and single-worker SSH
launchers; hook, native-app, and multi-worker roster adapters remain staged follow-ups)

## 1. Requirements and scope

An orchestrator (an agent session or a human) hands tasks to worker sessions and
coordinates them. The design must support:

- **Any harness:** Claude Code, Codex CLI, Codex desktop app, opencode, and future
  tools.
- **Any model:** Claude, GPT, GLM, Grok, and others. No model- or harness-specific
  feature is part of the protocol's correctness boundary.
- **Multiple session transports:** cmux tabs, tmux sessions, app tasks, and remote
  sessions.
- **Orchestrator actions:** launch, inspect progress cheaply, steer, answer questions,
  stop, review, and integrate.
- **Worker actions:** checkpoint progress, ask blocking and non-blocking questions,
  consume steering, and preserve commitments across context compaction.
- **Multiple workers**, including workers on different machines.

The portable contract is a small filesystem protocol plus a standard-library
`handoffctl` helper. A conforming worker needs filesystem access to its run directory
and must follow the standing instructions in its kickoff. Hooks and native messaging
improve latency but are not required for correctness.

The protocol deliberately does **not** promise arbitrary mid-turn interruption,
exactly-once semantic execution, or automatic conflict-free code integration. It
promises ordered, durable, **at-least-once** message delivery at worker checkpoints.
Workers must keep turns/checkpoints bounded so steering latency is bounded in practice.

## 2. Evidence from the pilot

The investment Phase-1 handoff ran on 2026-07-13/14: one Claude/Opus worker in a cmux
tab, launched by `handoff_agent.sh` and orchestrated manually from another Claude
session. Every incident maps to a protocol feature:

| Incident | Lesson → feature |
|---|---|
| The orchestrator's grep matched its own request text on the worker's screen | Screen text is not state → structured status; verify **artifacts, not echoes** |
| The worker acknowledged a later doc change, but the promise lived only in context | Acknowledgments are context; only durable records survive compaction → cross-stage commitments go in `progress.md`; consumption is a journal cursor |
| A steer sat queued while the worker was mid-turn | Queued is not consumed → cursor acknowledgment; keep turns bounded |
| A foreign commit in the worker's tree risked confusion | Outside mutation of an active worker checkout must be prohibited; announce base/input changes and let the worker incorporate them |
| The worker stopped in a folder-trust TUI below the agent loop | Files cannot rescue a non-executing agent → retain a terminal/session control channel for doorbells and rescue |
| Kickoff, spec, progress, and reminders survived because they were on disk | Durable files are the source of truth; terminal and push channels are hints |

## 3. Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│ 4. INDEX (optional): SQLite/DuckDB views derived from run       │
│    journals for fleet queries. Rebuildable; never authoritative.│
├─────────────────────────────────────────────────────────────────┤
│ 3. PUSH (latency only): cmux notification, native app message,  │
│    or agents.openclaw. Carries run/message IDs, not the truth.  │
├─────────────────────────────────────────────────────────────────┤
│ 2. SESSION & STORE ADAPTERS: launch, grant run-dir access,      │
│    ring a doorbell, probe process/session liveness, and rescue. │
│    For remote runs, invoke handoffctl on the owning host via SSH.│
├─────────────────────────────────────────────────────────────────┤
│ 1. MESSAGE & STATE PLANE: versioned files written through       │
│    handoffctl. This is the authoritative protocol.              │
└─────────────────────────────────────────────────────────────────┘
```

Layer 2 never invents a second message format. A terminal doorbell contains only
"check run X after message Y". An SSH adapter may carry bytes to a remote
`handoffctl`, but the resulting journal records remain the semantic source of truth.

### 3.1 Run-directory placement

`<run-dir>` is a logical location, not `<repo>/.handoff` as seen from the
orchestrator's checkout. That distinction matters because Git worktrees have separate
working directories.

- Preferred same-host placement: a durable state root outside disposable checkouts,
  such as
  `${HANDOFF_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agents/handoff}/<repo-key>/runs/<run-id>`.
  The launch adapter grants the worker explicit read/write access and records the
  absolute path in the roster.
- Constrained-sandbox fallback: `<worker-worktree>/.handoff/runs/<run-id>`. The adapter
  records that worktree's absolute path, prohibits cleanup commands that could delete
  the run store, and archives it before worktree deletion. It must not assume that the
  orchestrator's checkout has the same `.handoff` directory.
- Remote worker: keep the authoritative run directory in a durable state root on the
  worker host and record a URI such as `ssh://host/absolute/path`. Run `handoffctl` on
  that host over SSH.

Active journals must not be synchronized through Git, rsync, Dropbox, or another
multi-master file replicator. Those systems do not preserve the protocol's append,
locking, or timely-delivery semantics.

Local-v1 run IDs are generated lowercase UUIDv4 values, never unsanitized display
names. Directories are mode `0700`; files containing prompts or messages are mode
`0600`.

## 4. v1 protocol: one worker

```text
<run-dir>/
  run.json         orchestrator-created and immutable identity/input snapshot
  kickoff.md       immutable worker contract copied at launch
  control.json     orchestrator-owned current intent/review state; atomic replace
  inbox.jsonl      orchestrator → worker ordered journal; append-only
  status.json      worker-owned current semantic state; atomic replace
  outbox.jsonl     worker → orchestrator ordered journal; append-only
  progress.md      worker-owned append-only human-readable checkpoint journal
  attachments/     optional immutable message snapshots, keyed by message ID
  repairs/         optional quarantined partial journal tails and repair reports
```

`questions.jsonl` is unnecessary: questions are typed outbox events. Using a
bidirectional journal also gives worker results and failures the same delivery and
acknowledgment semantics as questions.

### 4.1 Immutable run identity and input snapshot

`run.json` contains at least:

```json
{
  "protocol_version": 1,
  "run_id": "8df1f10b-5bcb-4f40-a557-2f77e15012ae",
  "created_at": "2026-07-14T19:00:00Z",
  "repo": {"path": "/abs/repo", "remote": "..."},
  "workspace": {"path": "/abs/worktree", "host": "mac", "base_commit": "..."},
  "worker": {"harness": "claude", "model": "opus", "transport": "cmux"},
  "inputs": [
    {"path": "docs/plans/example.md", "sha256": "...", "git_blob": "..."}
  ]
}
```

`handoffctl init` creates `kickoff.md` from the standing worker instructions and a
byte-for-byte copy of the submitted prompt; it does not merely point at a mutable or
untracked file that may be absent from a new worktree. Referenced specs remain
workspace-relative, but `run.json` records their launch-time SHA-256 and, when tracked,
Git blob ID. A later `input-changed` message names the new digest and commit or
explicitly supplies a snapshot. The worker acknowledges only after rereading and
recording any cross-stage consequence in `progress.md`.

`handoffctl init` constructs and validates the whole initial directory under a sibling
temporary name, flushes it, then atomically renames it to `<run-id>`. A visible final run
directory is therefore initialized, never half-created.

### 4.2 Journal envelope

Every `inbox.jsonl` and `outbox.jsonl` line has this envelope:

```json
{
  "protocol_version": 1,
  "seq": 12,
  "message_id": "uuid",
  "writer_epoch": 1,
  "ts": "2026-07-14T19:03:22Z",
  "type": "steer",
  "reply_to": null,
  "body": "Run the migration test before changing the schema.",
  "data": {},
  "attachments": []
}
```

Rules:

1. Sequence numbers start at 1 and are contiguous **per journal** across writer
   takeovers. A cursor means the highest contiguous sequence durably processed, not
   merely the highest sequence observed.
2. Inbox types are `steer`, `answer`, `review`, `input-changed`, `base-changed`,
   `integration`, `coordinator-takeover`, `worker-relaunched`, and `stop`. Outbox
   types are `checkpoint`, `question`, `result`, and `error`.
3. `reply_to` references a stable `message_id`; answers to questions must set it.
4. Artifact attachments use workspace-relative paths plus a digest. Message bodies are
   data, never shell fragments to `eval`.
5. Delivery is at least once. If a worker crashes after acting but before advancing its
   cursor, it will replay the message. Handlers therefore use `message_id` for
   idempotence. Before a non-idempotent external action, record intent keyed by that ID;
   on replay, reconcile the observed outcome instead of blindly repeating it. If an
   action provides neither an idempotency key nor a reliable reconciliation check,
   require explicit human coordination.

### 4.3 Current state and bilateral acknowledgment

`status.json` is worker-owned:

```json
{
  "protocol_version": 1,
  "run_id": "8df1f10b-5bcb-4f40-a557-2f77e15012ae",
  "revision": 8,
  "worker_epoch": 1,
  "state": "working",
  "stage": "tests",
  "inbox_cursor": 12,
  "outbox_seq": 7,
  "blocked_on": [],
  "current_activity": "Running migration tests",
  "last_progress_at": "2026-07-14T19:03:25Z",
  "updated_at": "2026-07-14T19:03:25Z"
}
```

Worker states are `starting`, `working`, `blocked`, `awaiting_review`, `succeeded`,
`failed`, and `stopped`. `succeeded` means the worker has durably consumed an accepted
review for its current result; the authoritative acceptance and integration decisions
still live in coordinator-owned state.

`control.json` is orchestrator-owned:

```json
{
  "protocol_version": 1,
  "run_id": "8df1f10b-5bcb-4f40-a557-2f77e15012ae",
  "revision": 5,
  "coordinator_epoch": 1,
  "coordinator_token_sha256": "...",
  "recovery_token_sha256": "...",
  "coordinator_lease_expires_at": "2026-07-14T19:06:00Z",
  "worker_epoch": 1,
  "worker_token_sha256": "...",
  "desired_state": "run",
  "review_state": "pending",
  "review_result_id": null,
  "last_command_seq": 12,
  "outbox_cursor": 7,
  "integration": {"state": "pending", "commit": null, "reason": null},
  "updated_at": "2026-07-14T19:04:00Z"
}
```

`desired_state` is `run` or `stop`; `review_state` is `pending`,
`changes_requested`, or `accepted`; integration state is `pending`, `integrated`, or
`abandoned`. `review_result_id` binds the review state to one exact outbox `result`
message. The orchestrator advances `outbox_cursor` only after consuming all prior
worker events. `last_command_seq` is the highest inbox command reflected in the
desired/review summary. A successor can therefore recover both sides without relying
on its conversation context.

The journals are authoritative event history; JSON snapshots and `progress.md` are
current or human-readable projections. Lease timestamps and `outbox_cursor` are
coordinator operational state rather than journal events: they are atomically durable,
but may safely regress only when restoring from backup, in which case the coordinator
reconsumes idempotently. Write ordering is deterministic:

- append an inbox command, then update `control.json` to reflect it;
- append an outbox event, then update `status.json`; and
- mirror `checkpoint` events into `progress.md` with an embedded outbox sequence marker.

A crash during an append-and-project operation may leave a projection behind its
journal, never legitimately ahead of it. `handoffctl doctor` replays the missing
records into the projection. Snapshot-only operations use one atomic replacement and
therefore have no append/projection gap. This avoids pretending that several
independent files can be updated as one atomic transaction.

### 4.4 Write and takeover rules

All protocol mutations go through `handoffctl`; direct `echo >>` is unsupported.

1. There is one **logical writer role** per file. `handoffctl` takes an advisory
   `coordinator.lock` or `worker.lock` for the entire append-and-project operation, not
   merely for one file, so two processes acting for the same role cannot interleave
   sequence assignment and projection. It rereads and revalidates the token, epoch,
   journal tail, and snapshot after acquiring the lock.
2. A journal append is one compact UTF-8 JSON record plus newline, written under the
   lock with append mode and `fsync`. On startup, the helper validates every journal.
   A partial final line is quarantined/truncated only by an explicit repair operation;
   corruption in the middle is fatal.
3. JSON snapshots are written to a unique temp file in the same directory, flushed,
   and installed with `os.replace`; the directory is then flushed where supported.
   Revisions increase monotonically.
4. Each active coordinator and worker receives an opaque writer token. A separate
   recovery token authorizes ownership recovery but is never supplied to either active
   process. `control.json` stores epochs and token hashes; mutation helpers reject stale
   tokens. Coordinator takeover requires the recovery token, acquires the coordinator
   lock, increments the epoch, and rotates the writer token after an explicit release or
   host-time lease expiry. The new coordinator appends a `coordinator-takeover` event
   before issuing other commands.
5. Worker relaunch increments the worker epoch only after the previous process is
   confirmed dead. Never relaunch a writer into the same writable worktree while the
   old session may still run; use a new worktree if death cannot be established.

These rules make stale coordinator processes fail closed at the protocol boundary.
They do not fence arbitrary direct writes to source files, which is why process death
and worktree isolation remain necessary.

### 4.5 Worker standing instructions

The launcher embeds these instructions in every kickoff:

- Read `run.json`, `control.json`, and all unread inbox messages before beginning; also
  check after resume/compaction, at every turn or stage boundary, before an irreversible
  action, before a commit, and before publishing a result.
- Immediately after that initial read, run the exact idempotent `handoffctl ready`
  command embedded in the kickoff before beginning substantive work. Do not inspect
  helper source or help first. This is the launcher/session adapter's worker-ready
  signal.
- Process inbox records in sequence. Advance `inbox_cursor` only after the instruction
  and any durable reminder it creates have been recorded.
- At each checkpoint, use one helper operation to append a concise `checkpoint` event,
  update `status.json`, and mirror the same summary to `progress.md`. The outbox event
  is authoritative if the helper is interrupted between those writes.
- A dirty Git workspace does not block work or result publication. Preserve unrelated
  pre-existing changes and report the current `HEAD` and dirty state truthfully.
- For a blocking question, append `question` with `blocking: true`, put that event's
  `message_id` in `blocked_on`, set `state: blocked`, then end the turn. For a
  non-blocking question, state the assumption and continue only when that is safe.
- On completion, append a `result` event containing the exact commit/HEAD, artifacts,
  and verification commands/results; set `state: awaiting_review`. After that durable
  publication, `handoffctl` makes a best-effort `cmux notify` call targeted at the
  worker's inherited `CMUX_SURFACE_ID` when the run transport is cmux. Notification
  failure does not change the result or state. Set `succeeded` only after an `accepted`
  review message. A requested change returns the worker to `working`.
- Never rely on remembered prose for a later-stage commitment; append it to
  the checkpoint event and its `progress.md` projection.

### 4.6 Delivery, liveness, and rescue

Hooks are an optimization, not a guarantee of mid-turn interruption. Claude Code and
current Codex releases expose lifecycle hooks including `UserPromptSubmit`, `Stop`,
and session-start/resume events ([Codex hooks documentation](https://learn.chatgpt.com/docs/hooks.md)).
Adapters should use them to surface unread sequence numbers at explicit prompt and
checkpoint boundaries. Codex `/goal` automatic continuations may bypass prompt hooks,
so the worker's standing checkpoint rule remains load-bearing.

cmux notification hooks are notification filters; they are **not** an agent inbox
injection mechanism. cmux/tmux `send` remains a doorbell that queues an explicit prompt
such as `Check HANDOFF_RUN_DIR; inbox now through seq 12.` App adapters may use native
task messaging when exposed and fall back to a manual message otherwise.

A model-written `heartbeat_ts` cannot prove that a process is alive. Hang detection
combines:

- lack of semantic change in `status.json` beyond a configured threshold; and
- a session-adapter probe (`tmux has-session`, cmux surface/process inspection, app task
  state, or remote process probe).

If the process exists but is stuck below the agent loop, rescue through layer 2. If it
is dead, resume/relaunch with the takeover rules above. Sleep, host suspension, and
network partitions can look like hangs, so liveness never automatically transfers a
single-writer resource.

### 4.7 Review and completion flow

1. The worker publishes `result` and enters `awaiting_review`.
2. For a cmux-launched worker, the helper attempts a native notification titled
   `Handoff result ready` with body `Awaiting coordinator review`. This alert is only a
   convenience; the durable result event remains authoritative if it fails.
3. The orchestrator checks the named artifacts and verification results at the exact
   commit; it never treats a terminal success message as evidence.
4. The orchestrator sends `review` with either requested changes or acceptance and
   updates `control.json`.
5. Only accepted output is integrated. The orchestrator records the integration commit
   in `control.json`, then requests a graceful stop and archives the run directory.

### 4.8 Security and operational boundaries

- Pass prompt/message bodies to helpers via stdin or a file descriptor, never through
  interpolated shell commands. Launchers must build argv safely and must not use
  `eval`; paths with spaces or shell metacharacters are ordinary inputs.
- Store only token hashes in run files. Supply role and recovery tokens through a protected
  environment/file descriptor, never a prompt, process argv, terminal doorbell, push
  notification, or roster entry.
- Sanitize credential-bearing Git remote URLs before writing `run.json`. Push messages
  contain opaque run/message IDs only; a human follows them to the protected run store.
- Validate schemas, message sizes, sequence numbers, run IDs, and attachment paths.
  Reject path traversal and workspace-external attachments unless a later policy
  explicitly allows them.
- A worker token authorizes only worker-owned mutations; it cannot append inbox commands
  or change coordinator review state. A coordinator token cannot forge worker status.
- For SSH, use normal host-key verification and invoke a fixed remote helper with
  structured stdin. Do not construct a remote shell program from message content.

### 4.9 Normative local-v1 contract

This section closes implementation choices for protocol v1. Earlier examples are
illustrative where they say "at least"; the schemas and behavior below are normative.
An implementation may add read-only presentation commands, but must not add fields,
message types, or state transitions to protocol v1 without revising this document.

#### 4.9.1 Implementation surface and serialization

The standard-library implementation lives in `agents/orchestration/handoff.py`
(library and schema logic) and `agents/orchestration/handoffctl.py` (CLI entry point).
In this repository those are the `orchestration/handoff.py` and
`orchestration/handoffctl.py` files, imported as `from agents.orchestration import
handoff` and invoked as:

```text
/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python -m agents.orchestration.handoffctl ...
```

`handoffctl` writes one JSON value to stdout on success and diagnostics to stderr. It
does not print secrets. Mutating commands accept message bodies from `--body-file`
(`-` means stdin), structured type data from `--data-file`, and attachment declarations
from `--attachments-file`; these inputs are never accepted as an interpolated shell
program. If omitted, `body` is `""`, `data` is `{}`, and `attachments` is `[]`.

All JSON is UTF-8, uses exact integer numbers for counters, rejects duplicate object
keys, and rejects unknown fields. Snapshot files use sorted keys, two-space indentation,
and a trailing newline; journal records are compact one-line JSON plus newline. Times
are UTC RFC 3339 with six fractional digits and `Z`. UUID fields are lowercase canonical
UUIDv4 strings. A journal line is at most 256 KiB; `body` is at most 128 KiB UTF-8;
`data` is at most 64 KiB serialized; there are at most 128 attachments; and attachment
snapshots are at most 32 MiB each. These are byte limits, checked before mutation.

Exit codes are stable:

| Code | Meaning |
|---:|---|
| 0 | Success |
| 2 | CLI usage error or schema/validation failure |
| 3 | Missing, invalid, expired, or stale-role credential |
| 4 | State, cursor, sequence, or ownership conflict |
| 5 | Journal or snapshot corruption requiring `doctor` or manual recovery |
| 6 | Filesystem, Git-probe, or session-adapter I/O failure |

#### 4.9.2 Complete file schemas

`run.json` contains exactly the following top-level fields:

```json
{
  "protocol_version": 1,
  "run_id": "uuid",
  "created_at": "RFC3339",
  "repo": {"path": "/canonical/repo", "remote": null},
  "workspace": {
    "path": "/canonical/worktree",
    "host": "hostname",
    "base_commit": null
  },
  "worker": {
    "harness": "claude",
    "model": "opus",
    "effort": "high",
    "transport": "cmux"
  },
  "inputs": [
    {
      "path": "docs/plans/example.md",
      "sha256": "64-lowercase-hex",
      "size": 123,
      "git_blob": null
    }
  ]
}
```

`repo.remote`, `workspace.base_commit`, and `inputs[].git_blob` are strings or `null`.
`worker.harness` is `claude`, `codex`, or `kimi` for the local launcher, while the file
schema accepts any lowercase identifier matching `[a-z][a-z0-9_-]{0,31}` for future
local adapters. `worker.transport` follows the same identifier rule. `effort` is a
string or `null`. Git remotes are sanitized by removing userinfo before storage.

`status.json` has exactly the fields shown in section 4.3. `stage` and
`current_activity` are strings of at most 256 and 2,048 UTF-8 bytes respectively;
`blocked_on` is a unique list of outbox question UUIDs; timestamps are RFC 3339 or
`null`, except `updated_at`, which is always present. Its initial values are revision
1, worker epoch 1, state `starting`, stage `startup`, both cursors zero, an empty
`blocked_on`, current activity `Reading kickoff and run state`, null
`last_progress_at`, and the initialization time as `updated_at`.

`control.json` has exactly the fields shown in section 4.3 including
`recovery_token_sha256` and `review_result_id`. Its initial values are revision 1,
both epochs 1, desired state `run`, review state `pending`, null review result, both
cursors zero, pending/null integration, and a lease expiry computed from initialization
time. Token hashes are lowercase SHA-256 hex. `integration.commit` is required and
non-null only when state is `integrated`; it is null for `pending` and optional for
`abandoned` only as a recorded already-created but deliberately unused commit.
`integration.reason` is non-empty only for `abandoned` and null otherwise.

Every journal envelope has exactly the fields in section 4.2, including `data`.
`reply_to` is a UUID or `null`; `body` is a human-readable summary; and `data` carries
machine semantics. An attachment has exactly:

```json
{
  "path": "workspace/relative/path",
  "sha256": "64-lowercase-hex",
  "size": 123,
  "git_blob": null,
  "snapshot": null
}
```

`path` is a normalized POSIX-style path relative to `workspace.path`; absolute paths,
empty components, `.`/`..`, NUL, and backslashes are rejected. The helper resolves the
path and rejects symlinks or resolved files outside the workspace. `snapshot` is null
or a run-relative path under `attachments/<message-id>/`; when requested, the helper
copies the bytes with mode `0600`, fsyncs them before the journal append, and records
the same digest and size. Attachments are immutable once referenced. v1 does not permit
workspace-external source attachments.

`send` and `emit` accept an optional caller-generated `message_id` for retry safety and
otherwise generate one before copying attachments. If that ID already exists in the
target journal with identical type, reply, body, data, and attachment declarations,
the operation is an idempotent replay: the helper repairs any lagging projection and
returns the existing record without appending. Reuse with different content exits 4.

`progress.md` begins with a run title and run ID. Each projection is an append-only
section whose heading contains the marker `<!-- outbox-seq:<N> -->`, timestamp, state,
stage, summary, consumed inbox cursor, and commitments. A sequence marker may occur
only once, allowing idempotent repair.

#### 4.9.3 Type-specific message data

All sequence and cursor values below are non-negative integers. `commitments` is a
list of distinct non-empty strings, each at most 2,048 bytes. `verification` is a list
of objects with exactly `command`, `exit_code`, and `summary`; `command` and `summary`
are strings and `exit_code` is an integer or `null` when the check could not start.

| Direction/type | Required `data` | Additional rules and projection |
|---|---|---|
| inbox `steer` | `{}` | Non-empty `body`; no `reply_to`. |
| inbox `answer` | `{}` | Non-empty `body`; `reply_to` must name an existing outbox `question`. |
| inbox `review` | `{"disposition":"accepted"|"changes_requested"}` | `reply_to` must name the current outbox `result`; updates `review_state` and `review_result_id`. Changes requested require non-empty `body`. |
| inbox `input-changed` | `{"path":str,"sha256":hex,"git_blob":str|null}` | The named attachment must match path/digest, either as a current workspace file or copied snapshot. |
| inbox `base-changed` | `{"base_commit":str}` | Commit must resolve in the launch repository when local; non-empty `body`. |
| inbox `integration` | `{"state":"integrated"|"abandoned","commit":str|null,"reason":str|null}` | Created only by `control integrate|abandon`; `reply_to` names the accepted/current result. Integrated requires a commit and null reason; abandoned requires a non-empty reason. |
| inbox `coordinator-takeover` | `{"previous_epoch":int,"new_epoch":int,"reason":str,"forced":bool}` | Created only by `control takeover`; epochs must be consecutive. |
| inbox `worker-relaunched` | `{"previous_epoch":int,"new_epoch":int,"reason":str}` | Created only by `control rotate-worker`; epochs must be consecutive. |
| inbox `stop` | `{"reason":str}` | Created by `send stop`; sets desired state `stop`. |
| outbox `checkpoint` | `{"state":"working"|"succeeded"|"stopped","stage":str,"current_activity":str,"inbox_cursor":int,"commitments":[str]}` | Advances the worker projection and appends the progress marker. Preconditions for terminal states are in 4.9.4. |
| outbox `question` | `{"blocking":bool,"stage":str,"current_activity":str,"inbox_cursor":int,"commitments":[str]}` | Blocking adds its generated message ID to `blocked_on` and sets `blocked`; non-blocking leaves the current state. |
| outbox `result` | `{"head":str|null,"dirty":bool,"stage":str,"inbox_cursor":int,"verification":[object],"commitments":[str]}` | `attachments` name artifacts. Sets `awaiting_review`; clears `blocked_on`. In a Git workspace, `head` must equal current `HEAD` and `dirty` must truthfully report whether `git status --porcelain` is non-empty. A dirty workspace does not prevent publication; the coordinator reviews the diff and must not require unrelated user changes to be stashed, discarded, or committed. |
| outbox `error` | `{"fatal":bool,"category":str,"stage":str,"inbox_cursor":int,"commitments":[str]}` | Fatal sets `failed` and clears `blocked_on`; nonfatal preserves state. Non-empty `body`. |

For all worker events, `inbox_cursor` cannot decrease or exceed the validated inbox
tail. It advances only through a contiguous prefix. `emit` refuses to advance across
an unanswered blocking question unless the new prefix contains an `answer` replying to
that question, or a `stop`. Every emitted event sets `outbox_seq` to its sequence,
increments the status revision, and updates `updated_at`; events other than a
nonblocking question also update `last_progress_at`.

`integration`, `coordinator-takeover`, and `worker-relaunched` are system-generated
inbox message types: generic `send` rejects them. Their bodies are audit summaries,
not credentials.

#### 4.9.4 State machine and coordinator invariants

The helper enforces these transitions:

| From | To | Caused by | Preconditions |
|---|---|---|---|
| `starting` | `working` | checkpoint | Worker has consumed the launch-time inbox prefix. |
| `starting`/`working`/`blocked`/`awaiting_review` | same state | nonfatal error | Cursor is valid. |
| `starting`/`working` | `blocked` | blocking question | Question ID is added to `blocked_on`. |
| `blocked` | `working` | checkpoint | Every prior blocking question has a consumed reply or stop. |
| `starting`/`working`/`blocked` | `awaiting_review` | result | Cursor is valid and no unresolved blocking question remains. |
| `awaiting_review` | `working` | checkpoint | A matching `changes_requested` review was consumed. |
| `awaiting_review`/`succeeded` | `succeeded` | checkpoint | A matching `accepted` review was consumed, remains current, and no later result exists. |
| Any state except `failed`/`stopped` | `stopped` | checkpoint | Desired state is `stop` and the corresponding stop sequence was consumed. |
| `starting`/`working`/`blocked`/`awaiting_review` | `failed` | fatal error | Always allowed. |

`failed` and `stopped` are terminal for a worker epoch. `succeeded` may only remain
`succeeded` while consuming later integration information or transition to `stopped`;
it cannot resume work or publish another result. Requested changes after `succeeded`
require a confirmed-dead worker rotation or a new run; v1 does not silently reopen a
completed epoch. A new `result` observed by `control consume` sets
`review_state` to `pending`, binds `review_result_id` to that message, and resets
integration to pending/null/null. `send review` is rejected unless it replies to that exact
result. `control integrate` is allowed only after acceptance of that result and must
append an `integration` event recording the integration commit. `abandoned` is allowed
after any result, requires a non-empty reason, and appends the corresponding event.
Integration and abandonment are terminal coordinator decisions.
After either decision, the only allowed coordinator operations are `send stop`,
`control consume|renew|release|takeover`, read operations, and repair. A second stop or
any attempt to change the integration decision exits 4.

The coordinator consumes outbox messages only via `control consume --through N`. The
helper verifies a contiguous range from the old cursor through `N`, applies the result
projection described above, then atomically advances `outbox_cursor`. Repeating the
same or a lower cursor is a successful no-op; skipping a sequence is rejected.

#### 4.9.5 Authentication, epochs, and leases

`init` generates independent 32-byte random recovery, coordinator, and worker tokens.
The recovery token is an owner/rescue credential and is never supplied to a worker or
an active coordinator process. `init` writes the tokens as raw lowercase hexadecimal
plus newline to caller-selected credential files using `O_CREAT|O_EXCL` and mode
`0600`; it writes only their SHA-256 hashes into `control.json`. Credential files must
be outside `<run-dir>`. The CLI reports their paths, never their contents. Mutating
commands accept `--token-file`; otherwise they use `HANDOFF_COORDINATOR_TOKEN_FILE`,
`HANDOFF_WORKER_TOKEN_FILE`, or, where explicitly required,
`HANDOFF_RECOVERY_TOKEN_FILE`. Tokens are never accepted directly in argv. Library
callers pass token bytes in memory.

The local-v1 coordinator lease is 180 seconds and is renewed every 60 seconds by an
active orchestrator. Every successful coordinator mutation except `release` and
`takeover` renews it; `control renew` exists for periods with no other mutations. Both
are valid only for the current unexpired coordinator token and replace expiry with
host-now plus 180 seconds. `control release` sets expiry to
host-now and records `released_at` only in the command's JSON response; a subsequent
takeover still writes the durable takeover event. This release does not stop the
worker.

`control takeover` requires the recovery token, acquires `coordinator.lock`, rereads
the host clock and token state, and succeeds only after expiry or release. `--force`
additionally permits takeover before expiry but requires a non-empty audit reason. It
calculates the next epoch, creates the rotated token in a new `O_EXCL` credential file,
appends the takeover inbox event using that next epoch, and then atomically updates
control with an expiry of host-now plus 180 seconds before returning. A crash after the
append but before the projection update is repaired by `doctor --repair-control` with
the recovery token. A failure before append
leaves protocol state unchanged and the helper removes or reports the unused token
file.

`control rotate-worker` requires the current coordinator token, the literal
`--confirmed-dead` flag, and a non-empty reason. The session adapter, not
`handoffctl`, supplies that assertion after probing the old process. It acquires both
role locks in coordinator-then-worker order, calculates the next worker epoch, creates
the rotated worker token, appends a `worker-relaunched` inbox event, then updates
control and resets status to the initial `starting` projection for the new epoch while
preserving journal sequence/cursors. A relaunch cannot reduce either journal sequence
or cursor. Status replay for the new epoch starts at that preserved outbox sequence and
considers only later outbox records carrying the new worker epoch. When process death
is uncertain, the adapter must create a new worktree and must not invoke this command.

#### 4.9.6 CLI commands

Every command except `init` requires `--run-dir ABSOLUTE_PATH`. Read commands require
no token. Mutations require the role token described above.

```text
handoffctl init --workspace PATH --kickoff FILE --harness ID --model MODEL
                --transport ID [--effort VALUE] [--input PATH ...]
                [--state-root PATH | --run-dir PATH]
                --recovery-token-file PATH
                --coordinator-token-file PATH --worker-token-file PATH

handoffctl read --run-dir PATH --journal inbox|outbox [--after N] [--through N]
handoffctl status --run-dir PATH
handoffctl ready --run-dir PATH
handoffctl control show --run-dir PATH

handoffctl send --run-dir PATH --type TYPE [--message-id UUID] [--reply-to UUID]
                [--body-file FILE] [--data-file FILE]
                [--attachments-file FILE]
                [--snapshot-attachments]
handoffctl emit --run-dir PATH --type TYPE [--message-id UUID] [--reply-to UUID]
                [--body-file FILE] [--data-file FILE]
                [--attachments-file FILE]
                [--snapshot-attachments]

handoffctl control consume --run-dir PATH --through N
handoffctl control renew|release --run-dir PATH
handoffctl control integrate --run-dir PATH --commit COMMIT
handoffctl control abandon --run-dir PATH --reason-file FILE
handoffctl control takeover --run-dir PATH --new-token-file PATH
                            --recovery-token-file PATH --reason-file FILE [--force]
handoffctl control rotate-worker --run-dir PATH --new-token-file PATH
                                 --reason-file FILE --confirmed-dead

handoffctl doctor --run-dir PATH
handoffctl doctor --run-dir PATH --repair-tail inbox|outbox
handoffctl doctor --run-dir PATH --repair-status
handoffctl doctor --run-dir PATH --repair-control
                  [--new-token-file PATH] [--new-worker-token-file PATH]
handoffctl doctor --run-dir PATH --repair-progress
```

`init` canonicalizes paths. With `--run-dir`, the supplied absolute path is the final
run directory and must not exist. Otherwise it uses:

```text
<state-root>/<repo-basename>-<sha256(canonical-repo-root)[0:12]>/runs/<uuidv4>
```

`state-root` defaults in order to `HANDOFF_STATE_DIR`, `$XDG_STATE_HOME/agents/handoff`,
and `$HOME/.local/state/agents/handoff`. The submitted kickoff must be a regular file
at most 1 MiB. `init` creates immutable `kickoff.md` by writing the standing
instructions from section 4.5, a delimiter, and the submitted file byte-for-byte.
`--input` paths are workspace-relative or resolve inside the workspace, are
deduplicated after normalization, and must be regular files.
The helper discovers the Git repository, sanitized remote, base commit, and blob IDs;
all Git fields are null when unavailable. `init` returns run and credential paths plus
the initial public snapshots.

`read` returns a JSON array and fails on any invalid journal record. `status` and
`control show` return the corresponding object. Mutations return the complete appended
envelope and resulting owned snapshot. `--after` is exclusive and defaults to zero;
`--through` is inclusive and defaults to the validated tail. An empty interval returns
`[]`. `attachments-file` is a JSON array of workspace-relative path strings; the
helper computes all stored metadata rather than trusting caller-supplied digests. At
most one of `--body-file`, `--data-file`, and `--attachments-file` may be `-`.
`ready` is a worker-token mutation that emits the fixed startup `working` checkpoint,
consumes the inbox through its validated tail, and uses a deterministic UUIDv4-shaped
message ID derived from the run ID so retrying the command is idempotent.

After `emit --type result` has durably appended the result, projected
`awaiting_review`, and fsynced the run directory, the CLI checks the immutable run
transport. For a cmux run with `CMUX_SURFACE_ID` in the worker environment, it attempts
`cmux notify --surface "$CMUX_SURFACE_ID" --title "Handoff result ready" --body
"Awaiting coordinator review"` with a five-second timeout. The command's output is
suppressed. Missing cmux context, a nonzero exit, timeout, or process-launch failure is
ignored and does not alter the successful JSON response or durable state. No native
notification is attempted for tmux transport.

Successful JSON output shapes are exact:

| Command | Output |
|---|---|
| `init` | `{"run_dir":str,"credential_files":{"recovery":str,"coordinator":str,"worker":str},"run":object,"status":object,"control":object}` |
| `read` | `[message,...]` |
| `status` / `control show` | The raw snapshot object |
| `send` | `{"message":object,"control":object}` |
| `emit` | `{"message":object,"status":object}` |
| `ready` | `{"message":object,"status":object}` |
| `control consume|renew|release` | `{"control":object}` |
| `control integrate|abandon` | `{"message":object,"control":object}` |
| `control takeover` | `{"message":object,"control":object,"new_token_file":str}` |
| `control rotate-worker` | `{"message":object,"control":object,"status":object,"new_token_file":str}` |
| `doctor` or a repair | The report object specified in 4.9.7 |

The library exposes `HandoffError` with an `exit_code` from the table in 4.9.1 and
functions mirroring each CLI noun/verb. Library calls accept `pathlib.Path` values and
token bytes, never read ambient credential environment variables, and return the same
JSON-serializable objects as the CLI. The CLI is a thin `argparse` adapter and is the
normative compatibility surface.

#### 4.9.7 Crash recovery and `doctor`

Plain `doctor` is read-only. It validates directory/file modes, schemas, run IDs,
token-hash formats, epochs, revisions, journal UTF-8 and newlines, contiguous sequences,
message references, attachment digests, cursor bounds, transition replay, and unique
progress markers. Its exact report is
`{"ok":bool,"repaired":bool,"issues":[issue,...],"actions":[action,...]}`, where an
issue/action has exactly `code`, `path`, and `detail` string fields. Read-only doctor
sets `repaired` false and `actions` empty. Detected corruption exits 5 after printing
the report.

Repair is never implicit. `--repair-tail` accepts only an invalid or unterminated final
record. Under the owning role lock and token, it copies the rejected bytes to
`repairs/<UTC>-<journal>-tail.bin`, fsyncs the copy, truncates and fsyncs the journal,
then revalidates it. Invalid UTF-8/JSON/newlines before the final record, duplicate or
skipped sequences, and valid-but-semantically-invalid records are fatal and are never
automatically removed.

`--repair-status` requires the worker token and replays current-epoch outbox events after
`status.outbox_seq`. It rejects a snapshot ahead of the outbox or inconsistent with a
full replay. `--repair-control` requires the coordinator token and replays inbox events
after `last_command_seq`; the recovery token is accepted instead when completing an
appended takeover whose epoch is not yet projected. Because the journal intentionally
does not contain credential hashes or paths, completing an appended takeover also
requires `--new-token-file`; completing an appended worker rotation requires
`--new-worker-token-file`. This is the minimal crash-recovery input needed to restore
the hashes that cannot be derived from the journal. Repair preserves the snapshot-only
lease and outbox cursor after validating them. `--repair-progress` requires the worker
token and appends only missing
checkpoint markers in sequence order. Each repair writes a JSON report under
`repairs/`, and the CLI returns that report.

Locks are advisory `fcntl.flock` files on macOS/Linux. Writers hold the role lock
through attachment copy, journal fsync, snapshot replacement, progress append, and
directory fsync. Readers do not need locks for atomic snapshots; journal readers take
a shared role lock to avoid observing an in-progress final append. Lock acquisition
has a configurable timeout, default 30 seconds, and timeout is exit 4. Lock files,
credential directories, and run files are created with an explicit restrictive mode
independent of the caller's umask.

### 4.10 Normative local launcher pilot

The pilot rewrites `scripts/handoff_agent.sh` around `handoffctl`; it does not preserve
the current interpolated `LAUNCH` string. It adds `--input`, `--state-root`, and
`--run-dir` options and prints the run directory plus transport handle, never token
contents or credential paths.

With no explicit backend override, the launcher selects cmux only when the
orchestrator process inherits `CMUX_WORKSPACE_ID` and the cmux connection responds;
the new surface is placed in that inherited workspace. Otherwise it selects tmux.
An explicit `--backend cmux|tmux` remains an operator override.

Because cmux/tmux ultimately start a shell in a terminal, the launcher writes a private
mode-`0700` Python launch wrapper and mode-`0600` JSON config outside the run directory.
The terminal receives only `exec <shell-quoted-fixed-wrapper-path> <shell-quoted-config-path>`.
The wrapper reads configuration, credentials, and `kickoff.md`, constructs the agent
argv as a list, sets `HANDOFF_RUN_DIR` and `HANDOFF_WORKER_TOKEN_FILE`, and uses
`os.execvpe`; it never invokes `eval`, `sh -c`, or shell expansion over user content.
The launcher defaults are Claude `opus/high`, Codex `gpt-5.6-terra/xhigh`, and Kimi
`kimi-code/k3`/`max`, with explicit model/effort flags taking precedence. The Kimi
alias is passed explicitly as `--model kimi-code/k3`, and its effort is passed as
`KIMI_MODEL_THINKING_EFFORT=max`. Because Kimi's prompt flag is non-interactive, its TUI
receives only a short instruction containing the resolved absolute path to the run's
`kickoff.md` after the process probe. Message bodies and kickoff contents are not typed
as terminal commands.

The kickoff prepends the standing instructions from section 4.5 and tells the worker
the fixed helper invocation plus the exact idempotent `handoffctl ready` command, but
it does not contain either token or token-file path. The worker runs `ready` immediately
after its initial protocol read, without first inspecting helper source or help.
The latter is available only through the protected environment. Before sending an
optional second `.goal` message, the adapter must satisfy both conditions:

1. the cmux surface/tmux session and expected agent process still exist; and
2. `status.json` has advanced from its initial revision, proving the agent loop executed
   the protocol instructions.

Launch-only mode is the default: after the terminal session is created (and Kimi's fixed
kickoff pointer is delivered), the launcher releases the coordinator lease and returns
without a semantic readiness poll. `--wait-ready` explicitly requests the readiness
wait, and `--retain-coordinator` preserves ownership for managed monitoring. A supplied
`HANDOFF_CREDENTIAL_DIR` is the exact mode-`0700` private directory and therefore has
fixed token filenames; the coordinator never discovers them with a filesystem search.
The Kimi TUI process probe requests cmux output with both refs and UUIDs, which makes its
returned surface UUID comparable across commands. Process discovery and the later
appearance of Kimi's actual empty input widget share the normal readiness timeout (120
seconds by default) as one startup budget before the launcher sends the run-specific
kickoff pointer. Either phase can legitimately take more than 10 seconds, and earlier
terminal input can be echoed into scrollback and dropped. Both adapters first submit
that Kimi instruction with `Ctrl-J`, because a synthetic `Enter` can become an input
newline on some versions. They require a newly
visible instruction after the TUI banner, inspect the input row after submission, and
fall back once to `Enter` when the instruction is still pending (as with Kimi 0.27.0
through cmux). If neither key produces that evidence, `kickoff_sent` is false and the
launcher reports a rescue command. Generic shell launch commands and doorbells continue
to use normal `Enter` semantics.
The opt-in
semantic readiness wait times out after 120 seconds by default. On timeout the launcher
prints a rescue command and leaves the live session and run directory intact; it does
not send the second message or rotate a writer. Doorbells contain only run ID and
highest inbox sequence. cmux and tmux adapters provide `launch`, `doorbell`, `probe`,
`capture`, and `stop` operations; only `launch`, `probe`, and the safe readiness behavior
are required in the first pilot. Hook, Codex-app, automated archival, and multi-worker
roster adapters are out of local-v1 implementation scope.

### 4.11 SSH launcher adapter

The single-worker SSH adapter reuses the same protocol without synchronizing an active
run directory. The orchestrator calls the launcher with an SSH host and an absolute
worker checkout. It sends kickoff bytes, an optional `.goal`, and typed launch options
as JSON on stdin to a fixed, shell-quoted invocation of an installed remote
`agents.orchestration.handoff_launcher`. The receiver creates a temporary private
kickoff source, runs the ordinary tmux launcher on the worker host, records transport
`ssh_tmux`, removes the source, and returns an `ssh://host/absolute/run/path` URI plus
the exact remote tmux handle. The wrapper shebang and worker `handoffctl` command use
the interpreter executing the remote launcher, so no macOS path appears in a Linux
run.

SSH key authentication, tmux, the selected agent, and a compatible `agents` package
must already exist on the worker host. Launch-only releases the remote coordinator
lease. Managed coordination additionally requires a caller-known remote private
credential directory; its path and contents are never returned in launch JSON or
reported to the user. Read, renew, send, review, doctor, doorbell, rescue, and close
actions execute against the owning host through SSH. A network partition yields
unknown state and never authorizes an unfenced replacement worker.

An explicit operator request to close, kill, or terminate named worker sessions is a
transport-level instruction, not a request for semantic protocol completion. The
coordinator resolves each exact launch handle and directly closes the cmux surface or
tmux session; it does not first take over a lease, append `stop`, or wait for an
acknowledgment. Multiple requested handles may close independently. The run can remain
nonterminal afterward and is reported as such. The protocol stop path is reserved for
requests that explicitly require graceful shutdown, progress preservation, review, or
integration.

## 5. v2: multiple workers and the cross-host roster

```text
<coordinator-state>/
  roster.jsonl       coordinator-owned event journal
  runs/<run-id>/     authoritative local run directories
  archives/<run-id>/ closed run snapshots, including sandbox-fallback runs

<remote-worker-state>/
  runs/<run-id>/     authoritative remote run directories
```

Roster events contain at least `run_id`, display name, task, run-dir URI, host,
transport handle, worktree, branch or detached HEAD, base commit, coordinator epoch,
and event type (`launched`, `observed`, `accepted`, `integrated`, `closed`,
`abandoned`). The roster is a recovery index; the per-run journals remain authoritative.

- **Hub and spoke:** workers do not write to each other's run directories. Cross-worker
  knowledge goes through orchestrator fan-out messages or committed artifacts.
- **One writable worktree per worker:** the orchestrator owns integration. Nobody else
  updates an active worker's checked-out branch/ref or dirty working tree. When the
  integration base changes, send `base-changed`; the worker incorporates it at a safe
  point. If unavoidable external filesystem mutation occurs, stop the worker first.
- **Git is artifact transport, not the live mailbox:** branches and commits move code;
  the file protocol moves control messages.
- **Remote run directory stays host-local:** the orchestrator invokes the remote
  helper over SSH and records only cached observations locally. During a partition,
  the state is unknown, not dead. Do not launch a replacement writer or reassign an
  exclusive resource until the previous holder is fenced or confirmed stopped.
- **Single-writer resources need enforcement:** a kickoff role is only a guardrail. For
  correctness-critical resources, use an OS/file lock on one host or a storage-level
  lease with a monotonically increasing fencing token. If the resource cannot validate
  a fence, never reassign it without confirmed quiescence.

An optional filesystem task pool is safe only on one filesystem. Claim with
`mkdir`/`O_CREAT|O_EXCL` through `handoffctl`, record owner and lease, and recover
expired claims explicitly. Atomic rename claims do not extend across Git sync or
multiple machines. Use a real queue or a task tool with compare-and-swap/lease semantics
when cross-machine competing consumers become necessary.

## 6. Reuse survey (checked 2026-07-14)

| Project/platform | What it provides | Fit and verdict |
|---|---|---|
| [Vibe Kanban](https://github.com/BloopAI/vibe-kanban) | Web kanban, per-task branches/workspaces, multiple coding-agent executors, review and PR flows | Broad layer-2/task UI, but heavier and opinionated. The project is sunsetting after Bloop's shutdown, so do not make it foundational. |
| [Backlog.md](https://github.com/MrLesk/Backlog.md) | Markdown task files, CLI, terminal/web kanban, and MCP | Good file-native task/board layer. Use its CLI rather than editing task files directly. It is not a live session mailbox or cross-host claim service. |
| [beads](https://github.com/steveyegge/beads) | Dependency-aware issue/agent memory with durable storage, JSONL interchange, query/index tooling, and multi-agent synchronization machinery | Strong candidate when durable task graphs and distributed memory are real needs. Its locking, cache, and sync machinery show that cross-worker JSONL is not safely “just append files.” It does not replace session delivery/rescue. |
| [claude-squad](https://github.com/smtg-ai/claude-squad) | Tmux session manager for multiple agent CLIs, isolated Git worktrees, diff/review flow | Relevant layer-2 implementation. It is tmux-centric and AGPL-3.0; evaluate rather than embed casually. It does not provide the cross-harness durable protocol defined here. |
| [Claude Code agent teams](https://code.claude.com/docs/en/agent-teams) | Native shared tasks, mailbox, teammate control, and hooks | Excellent zero-build option for an all-Claude fleet, but experimental and harness-locked, with documented resume/shutdown limitations. Use opportunistically. |
| [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents.md), [hooks](https://learn.chatgpt.com/docs/hooks.md), and [app worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees.md) | Native parallel subagent routing/steering, lifecycle hooks, and app-managed worktrees | Excellent inside Codex and useful adapter primitives. They do not provide a shared protocol for independent Claude/opencode/remote sessions. App worktrees also have ignored-file and branch-ownership constraints that justify an explicit run-dir URI. |

The remaining gap is narrower than “nobody ships messaging.” Native harnesses do ship
mailboxes and task systems. What is missing is a small, durable, **cross-harness**
contract that preserves ordered steering, bilateral acknowledgment, review state, and
recovery across independent sessions. That is the part to build.

## 7. Recommendation and implementation order

Build the thin protocol and reuse task/session products where they fit:

1. **Local v1 core:** implement `orchestration/handoff.py` and
   `handoffctl init|read|send|emit|ready|status|control|doctor` exactly as specified in 4.9,
   including schema validation, role locks, atomic snapshots, epochs/tokens, leases,
   cursor rules, and explicit repair. Add tests for concurrent same-role calls, partial
   journal tails, replay after a crash, stale-token rejection, and readers observing
   snapshot replacement.
2. **Launcher integration:** rewrite `handoff_agent.sh` around the private wrapper/config
   flow in 4.10, export only the run/token-file environment, and print run-dir/transport
   identifiers. Replace fixed sleeps with the specified worker-ready/session probe
   before sending a second slash-command message. Test literal handling and both cmux
   and tmux probe paths with mocked adapters before a live pilot.
3. **Worktree pilot:** launch one worker in a disposable worktree, verify the
   orchestrator and worker share the external run path, test the in-worktree fallback,
   prohibit external branch mutation, and archive before fallback worktree cleanup.
4. **Hook adapters:** integrate Claude Code and Codex lifecycle hooks, but retain
   checkpoint polling and terminal doorbells. Maintain a conformance matrix rather than
   assuming equivalent behavior.
5. **Remote hardening:** extend the implemented single-worker SSH adapter with
   disconnect/reconnect conformance tests and keep refusing unsafe failover.
6. **Optional board:** trial Backlog.md for simple kanban visibility or beads for task
   graphs/distributed memory. Do not conflate either with the live mailbox.
7. **Derived index later:** add SQLite only when fleet queries justify it; rebuild it
   from journals.

Acceptance tests for v1 should demonstrate:

- a steer cannot be reported consumed before its durable effects/reminder exist;
- replay after worker death is harmless and does not skip a sequence;
- worker `succeeded` cannot be confused with coordinator `accepted` or `integrated`;
- an old coordinator token cannot append after takeover;
- the coordinator can find a worker run directory in a different worktree;
- a partial final JSONL line is detected and repaired without hiding mid-file corruption;
- a stuck TUI is detected by session probe even when semantic status is stale; and
- launcher paths and message bodies containing spaces or shell metacharacters are
  passed literally, without command execution.

The part that can be built now is steps 1 and 2, their automated tests, and a local
worktree pilot when cmux or tmux is available. Steps 4–7 are deliberately excluded from
the local-v1 build and must not delay it.

## 8. Staged decisions that do not block local v1

- Build a harness conformance matrix: which hooks fire on explicit prompts, automatic
  goal continuations, stop, resume, and compaction; which can add model-visible context;
  and which require trust approval.
- Keep Codex app task orchestration outside local-v1: the `handoff-agent` skill uses
  native project-scoped task creation, waiting, reading, and follow-up messaging when
  those app controls are available, with a documented manual fallback when they are not.
- Choose archive retention after `accepted` + `integrated`/`abandoned`; never garbage
  collect solely because a run is old while review or integration is pending.
- Decide whether a future protocol version permits source attachments outside the
  workspace. Local v1 rejects them and supports protected copied snapshots instead.
