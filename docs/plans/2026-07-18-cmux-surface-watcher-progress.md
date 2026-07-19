# Progress: durable cmux coordinator doorbells (surface-hosted watcher)

Goal: implement the durable cmux coordinator-doorbell mechanism per
`/tmp/handoff-cmux-detached-watcher.CWXzpm/handoff.md` (default: option a,
the surface-hosted watcher), with focused tests and the full suite passing;
publish a durable handoff result when done.

## 2026-07-19T05:19Z — plan chosen

Read the handoff doc, `AGENTS.md`, and the watcher/launcher/ctl sources.
Chose option (a): `handoffctl coordinator start` gains
`--mode auto|detached|surface` (auto = surface for cmux coordinators,
detached for tmux/native-app). Surface mode reuses `CmuxAdapter.launch` to
run `handoffctl watch --coordinator-state …` in the foreground of a
dedicated in-tree cmux terminal surface, so native `cmux notify` doorbells
work without relaxing `socketControlMode`. Durable checkpoint emitted
(outbox seq 2).

## 2026-07-19T05:27Z — implemented, tests green, docs updated

- `orchestration/handoff_watcher.py`: `watch()` gained an optional
  `on_poll` callback so the hosting tab can log doorbell activity.
- `orchestration/handoffctl.py`: mode resolution (`_resolve_watcher_mode`),
  surface command builder, `_start_surface_watcher` (launch + readiness
  wait + `watcher-surface` record + rescue command on timeout), reuse path
  reports `mode`/`surface`, `--mode`/`--cmux-binary` CLI flags, and
  `_print_coordinator_poll` JSONL logging (attempts/errors only, run IDs
  and cursors only — no bodies or credentials).
- Tests: 5 new focused tests (surface hosting end-to-end with the real
  typed command executed, startup-timeout rescue, mode validation via CLI,
  poll-printer filtering, `on_poll` reporting).
- Focused files: `pytest tests/test_handoffctl.py tests/test_handoff_watcher.py
  tests/test_handoff_launcher.py` → 78 passed.
- Full suite: `pytest tests/` → 154 passed.
- Docs: `docs/architecture.md` watcher paragraph + command synopsis,
  `CLAUDE.md` orchestration bullet.

Deliberately not done (user's call, per handoff doc): restarting the live
stale incident watcher (`coordinators/session.5RHlY2`) onto the fixed code.

## 2026-07-19T05:40Z — socketControlMode evaluated; doorbells now push the agent

User asked to evaluate `socketControlMode: "notifications"` and whether
notify alerts push the orchestrator. Findings from cmux source
(SocketControlSettings.swift, SocketClientAuthorization.swift):
`"notifications"` is a legacy alias normalized to `automation` mode, whose
ACL is same-UID with the FULL command set — no notifications-only mode
exists, so adopting it grants every local process full cmux control.
Rejected. And `cmux notify` reaches only the human, never the agent's
input loop.

Consequence implemented (user-directed, next entry below): cmux coordinator doorbells now
pair an echo-confirmed typed prompt into the coordinator surface
(`CmuxAdapter.orchestrator_doorbell_input`, counts only when the text
visibly echoes — the code-mode zero-exit trap stays closed) with the
visible `cmux notify` alert; macOS fallback fires only when no channel
accepted. `last_doorbell_method` records `+`-joined accepted channels
(e.g. `cmux_input+cmux_notify`). Tests updated/added (3 new); focused
files 81 passed; full suite 157 passed. Docs updated.

## 2026-07-19T05:45Z — stale watcher restarted; live end-to-end verified

With the user's go-ahead: killed the stale old-code watcher (PID 1643,
`coordinators/session.5RHlY2`) after verifying its identity and its owner
(Codex session PID 99128, start time matching the snapshot). Relaunched via
`handoffctl coordinator start --state .../session.5RHlY2/watcher.json`:
mode `surface`, new hosting surface `8d13a4a6-…` in the coordinator's
registered workspace, singleton lock confirmed held.

Live verification: within one retry window both pending weather runs
recorded `last_doorbell_method: "cmux_input+cmux_notify"`,
`last_doorbell_error: null` — the typed doorbell visibly echoed in the
Codex coordinator surface (agent push) and the native cmux alert was
raised. This is the first real-environment confirmation of the full new
path (in-tree surface hosting + echo-confirmed typed push + visible
alert). `doorbell_pending` stays true until that coordinator consumes the
outbox, which is honest protocol state, and retries are bounded (30s).
A second watcher (PID 8673, `session.Xsm8dD`) belongs to the coordinator
monitoring this run and was left untouched.

## 2026-07-19T05:55Z — watcher tab decluttered: parked workspace + meaningful names

User asked whether the watcher could be hidden entirely (no tab) while
staying in the cmux process tree. Investigated and answered no, with
evidence: the socket ACL walks live ancestry to the cmux app; the app
durably parents only surface shells (full CLI reviewed — no background
exec); an empirical `pipe-pane` probe showed its command is spawned by the
invoking CLI process, not the app, so it dies out-of-tree with its
launcher; and background shell jobs are either reaper prey or orphaned.

Implemented the chosen alternative: watcher tabs are named
`watcher: <coordinator workspace title>` (e.g. "watcher: agents") and are
parked together in a dedicated `handoff-watchers` workspace kept at the
bottom of the sidebar (`_watcher_workspace` finds-or-creates it by title;
`new-workspace` reports no UUID, so the created workspace is re-resolved
from the inventory listing). Live watcher relocated there and verified:
`cmux_input+cmux_notify`, no errors. Focused 82 passed; full suite 158
passed. Docs updated.

## 2026-07-19T06:00Z — collision-safe names; remote-outbox question answered

Tab-name collisions (two coordinators in one workspace): functionally
harmless — all addressing is by surface UUID — but ambiguous for the
human, so `_watcher_tab_name` appends a short coordinator-ID suffix only
when the plain `watcher: <title>` is already taken in the parked
workspace. Full suite 159 passed.

Remote sessions: the watcher already polls SSH-hosted runs — the
registry's credential-free proxy record carries host/remote_python/run_dir
and `_protocol_state` reads control/outbox over `ssh -o BatchMode=yes`
(existing behavior, covered by
test_remote_watcher_reads_authoritative_host_without_credentials).
Doorbells always target the local coordinator surface regardless of the
worker host.
