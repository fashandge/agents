# Agent handoff & orchestration: communication design

Date: 2026-07-14 · Status: DESIGN v1.1 (implementation-ready local protocol;
remote and hook adapters remain staged follow-ups)

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

Run IDs are generated UUIDs or ULIDs, never unsanitized display names. Directories are
mode `0700`; files containing prompts or messages are mode `0600`.

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
```

`questions.jsonl` is unnecessary: questions are typed outbox events. Using a
bidirectional journal also gives worker results and failures the same delivery and
acknowledgment semantics as questions.

### 4.1 Immutable run identity and input snapshot

`run.json` contains at least:

```json
{
  "protocol_version": 1,
  "run_id": "01...",
  "created_at": "2026-07-14T19:00:00Z",
  "repo": {"path": "/abs/repo", "remote": "..."},
  "workspace": {"path": "/abs/worktree", "host": "mac", "base_commit": "..."},
  "worker": {"harness": "claude", "model": "opus", "transport": "cmux"},
  "inputs": [
    {"path": "docs/plans/example.md", "sha256": "...", "git_blob": "..."}
  ]
}
```

The launcher copies the submitted prompt to `kickoff.md`; it does not merely point at
a mutable or untracked file that may be absent from a new worktree. Referenced specs
remain workspace-relative, but `run.json` records their launch-time SHA-256 and, when
tracked, Git blob ID. A later `input-changed` message names the new digest and commit or
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
  "attachments": []
}
```

Rules:

1. Sequence numbers start at 1 and are contiguous **per journal** across writer
   takeovers. A cursor means the highest contiguous sequence durably processed, not
   merely the highest sequence observed.
2. Inbox types are `steer`, `answer`, `review`, `input-changed`, `base-changed`,
   `coordinator-takeover`, and `stop`. Outbox types are `checkpoint`, `question`,
   `result`, and `error`.
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
  "run_id": "01...",
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
`failed`, and `stopped`. `succeeded` is a worker claim, **not coordinator acceptance**.

`control.json` is orchestrator-owned:

```json
{
  "protocol_version": 1,
  "run_id": "01...",
  "revision": 5,
  "coordinator_epoch": 1,
  "coordinator_token_sha256": "...",
  "coordinator_lease_expires_at": "2026-07-14T19:06:00Z",
  "worker_epoch": 1,
  "worker_token_sha256": "...",
  "desired_state": "run",
  "review_state": "pending",
  "last_command_seq": 12,
  "outbox_cursor": 7,
  "integration": {"state": "pending", "commit": null},
  "updated_at": "2026-07-14T19:04:00Z"
}
```

`desired_state` is `run` or `stop`; `review_state` is `pending`,
`changes_requested`, or `accepted`; integration state is `pending`, `integrated`, or
`abandoned`. The orchestrator advances `outbox_cursor` only after consuming all prior
worker events. `last_command_seq` is the highest inbox command reflected in the
desired/review summary. A successor can therefore recover both sides without relying
on its conversation context.

The journals are authoritative history; JSON snapshots and `progress.md` are current or
human-readable projections. Write ordering is therefore deterministic:

- append an inbox command, then update `control.json` to reflect it;
- append an outbox event, then update `status.json`; and
- mirror `checkpoint` events into `progress.md` with an embedded outbox sequence marker.

A crash may leave a projection behind its journal, never legitimately ahead of it.
`handoffctl doctor` replays the missing records into the projection. This avoids
pretending that several independent files can be updated as one atomic transaction.

### 4.4 Write and takeover rules

All protocol mutations go through `handoffctl`; direct `echo >>` is unsupported.

1. There is one **logical writer role** per file. `handoffctl` additionally takes an
   advisory per-file lock so a human command and an orchestrator process acting for the
   same role cannot race sequence assignment.
2. A journal append is one compact UTF-8 JSON record plus newline, written under the
   lock with append mode and `fsync`. On startup, the helper validates every journal.
   A partial final line is quarantined/truncated only by an explicit repair operation;
   corruption in the middle is fatal.
3. JSON snapshots are written to a unique temp file in the same directory, flushed,
   and installed with `os.replace`; the directory is then flushed where supported.
   Revisions increase monotonically.
4. Each active coordinator and worker receives an opaque writer token. `control.json`
   stores epochs and token hashes; mutation helpers reject stale tokens. Coordinator
   takeover acquires the run lock, increments the epoch, and rotates the token after an
   explicit release or host-time lease expiry. The new coordinator then appends a
   `coordinator-takeover` event before issuing other commands.
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
- Process inbox records in sequence. Advance `inbox_cursor` only after the instruction
  and any durable reminder it creates have been recorded.
- At each checkpoint, use one helper operation to append a concise `checkpoint` event,
  update `status.json`, and mirror the same summary to `progress.md`. The outbox event
  is authoritative if the helper is interrupted between those writes.
- For a blocking question, append `question` with `blocking: true`, put that event's
  `message_id` in `blocked_on`, set `state: blocked`, then end the turn. For a
  non-blocking question, state the assumption and continue only when that is safe.
- On completion, append a `result` event containing the exact commit/HEAD, artifacts,
  and verification commands/results; set `state: awaiting_review`. Set `succeeded`
  only after an `accepted` review message. A requested change returns the worker to
  `working`.
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
2. The orchestrator checks the named artifacts and verification results at the exact
   commit; it never treats a terminal success message as evidence.
3. The orchestrator sends `review` with either requested changes or acceptance and
   updates `control.json`.
4. Only accepted output is integrated. The orchestrator records the integration commit
   in `control.json`, then requests a graceful stop and archives the run directory.

### 4.8 Security and operational boundaries

- Pass prompt/message bodies to helpers via stdin or a file descriptor, never through
  interpolated shell commands. Launchers must build argv safely and must not use
  `eval`; paths with spaces or shell metacharacters are ordinary inputs.
- Store only token hashes in run files. Supply role tokens through a protected
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

## 5. v2: multiple workers and remote hosts

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

1. **Local v1 core:** implement `handoffctl init|send|emit|status|control|doctor`, JSON
   schema validation, per-file locks, atomic snapshots, epochs/tokens, and cursor rules.
   Add tests for concurrent same-role calls, partial journal tails, replay after a crash,
   stale-token rejection, and readers observing snapshot replacement.
2. **Launcher integration:** have `handoff_agent.sh` snapshot the kickoff/inputs, export
   `HANDOFF_RUN_DIR` and the worker token, embed the standing instructions, and print
   run-dir/transport identifiers. Replace fixed sleeps with a worker-ready/session probe
   before sending a second slash-command message. Test both cmux and tmux rescue paths.
3. **Worktree pilot:** launch one worker in a disposable worktree, verify the
   orchestrator and worker share the external run path, test the in-worktree fallback,
   prohibit external branch mutation, and archive before fallback worktree cleanup.
4. **Hook adapters:** integrate Claude Code and Codex lifecycle hooks, but retain
   checkpoint polling and terminal doorbells. Maintain a conformance matrix rather than
   assuming equivalent behavior.
5. **Remote adapter:** invoke `handoffctl` over SSH against a host-local run directory;
   test disconnect/reconnect and refuse unsafe failover.
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

## 8. Remaining decisions

- Choose exact coordinator lease duration and renewal cadence. Use the run-store host's
  clock and require an explicit force-takeover flag with an audit event.
- Build a harness conformance matrix: which hooks fire on explicit prompts, automatic
  goal continuations, stop, resume, and compaction; which can add model-visible context;
  and which require trust approval.
- Decide whether Codex app tasks receive an automated task-messaging adapter or remain a
  documented manual adapter when the relevant app API is unavailable.
- Choose archive retention after `accepted` + `integrated`/`abandoned`; never garbage
  collect solely because a run is old while review or integration is pending.
- Decide whether message attachments may reference files outside the workspace. Default
  should be no; copied snapshots are safer and portable.
