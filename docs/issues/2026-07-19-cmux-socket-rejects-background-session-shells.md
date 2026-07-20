# cmux socket rejects tool shells of Claude Code background-job sessions

**Date:** 2026-07-19 · **Status:** open (workarounds in place; fix is a cmux
settings decision) · **Symptom:** every `cmux` CLI call from an affected
session fails with `Error: Failed to write to socket (Broken pipe, errno 32)`,
even though the session visibly runs inside a cmux surface.

## TL;DR

cmux's default `automation.socketControlMode: "cmuxOnly"` authenticates each
socket client by **process ancestry**: the calling process must descend from
the cmux app process. A Claude Code *background-job* session executes its Bash
tool commands under a pooled `claude bg-pty-host` daemon that can be parented
to launchd (PID 1), so the ancestry walk never reaches cmux and the server
drops the connection — which surfaces as a broken pipe on write, not a clear
"unauthorized" error. "The session is inside cmux" is true but irrelevant:
what matters is the ancestry of the shell that runs the `cmux` binary.

## Full diagnosis

Observed in one session on 2026-07-19 (macOS, cmux at
`/Applications/cmux.app`, default settings — `socketControlMode` commented out
in `~/.config/cmux/cmux.json`, so `cmuxOnly` applies):

- The session's **main** `claude` process was in-tree:
  `claude (94381) → zsh → login → cmux.app (6959)`. cmux calls made while tool
  shells still descended from that tree **succeeded** (e.g. `new-surface`,
  `rename-tab`, `send`, `send-key`).
- After a session resume (`SessionStart:resume`), tool shells moved to a
  freshly claimed pool host:
  `zsh → claude bg-spare (30637) → claude bg-pty-host (30607) → launchd`.
  The pty-host's start time (21:34:44, captured incidentally by a handoff
  watcher registration) exactly separates the successful calls (all before)
  from the broken-pipe failures (all after).
- The failure is therefore **deterministic per pty-host**, not flaky: an
  in-tree host works, a launchd-orphaned host is rejected. A session can flip
  from working to broken mid-conversation when a resume (or pool rotation)
  swaps its pty-host.

Failure signature to recognize: `cmux` exits 1 with
`Failed to write to socket (Broken pipe, errno 32)` while
`/Applications/cmux.app/Contents/MacOS/cmux` is running and
`~/.local/state/cmux/cmux-501.sock` exists. Quick diagnosis: walk the current
shell's ancestry (`ps -o ppid= -p $$`, repeat); if it reaches PID 1 via
`claude bg-pty-host` without passing through `cmux.app`, this issue applies.

## Additional symptom: per-prompt hook errors

The same rejection also breaks cmux's injected Claude Code integration hooks.
Inside a cmux surface, a `claude` PATH shim
(`cmux-cli-shims/<surface>/claude` → `cmux-claude-wrapper`) launches Claude
Code with hooks that call `cmux hooks claude <event>`
(session-start/stop/notification/prompt-submit/pre-tool-use) to update the
surface UI over the socket. In an affected session every prompt therefore
logs a non-blocking `UserPromptSubmit hook error: ... Error: Not connected`
(reproducible with
`echo '{"prompt":"x"}' | cmux hooks claude prompt-submit`). Harmless — the
prompt is processed normally — but the surface loses live status updates, and
the noise disappears with remedy 1 below (the hook binary honors
`CMUX_SOCKET_PASSWORD`).

## Consequences for this repo

- `handoff_launcher`'s backend auto-selection correctly falls back to
  **tmux** for workers launched from an affected session (it probes cmux
  reachability rather than trusting `CMUX_*` env vars, which are inherited and
  still set).
- A surface-hosted orchestrator watcher (`orchestrator start --mode auto` for a
  cmux orchestrator) cannot be created from an affected session; the watcher
  must run `--mode detached`, degrading doorbells to recorded macOS
  notifications (`macos_notification` in `last_doorbell_method`). The durable
  journal remains authoritative, so nothing is lost — delivery is just less
  pushy.
- Viewer-tab hygiene: tabs created while in-tree cannot later be closed from
  the same session once it is out-of-tree (`cmux close-surface` fails the same
  way); the human closes them with ⌘W.
- `scripts/cmux_close_surface_safe.sh` and other cmux helpers fail loudly and
  harmlessly in this state (the guard listing fails before any mutation).

## Recommendations

The cmux binaries expose three `socketControlMode` values — `cmuxOnly`
(default), `password`, `allowAll` — with the password taken from `--password`,
then the `CMUX_SOCKET_PASSWORD` env var, then the password saved in Settings.

1. **Recommended: switch to `password` mode.** Set
   `automation.socketControlMode: "password"` plus a `socketPassword` in cmux
   Settings, store the password as `CMUX_SOCKET_PASSWORD` in
   `~/.config/secrets.env` (chmod 600) so scripts pick it up via
   `agents.env.build_env()` / `~/.zshenv` per the launchd conventions. Effect:
   background-job sessions, detached watchers, and launchd jobs can drive cmux
   natively, while arbitrary local processes without the password still
   cannot. Follow-ups if adopted: un-degrade the watcher guidance
   (`~/skills/handoff-agent/references/launch-details.md`) and the
   architecture note that currently says launchd-orphaned clients are simply
   rejected.
2. **Not recommended: `allowAll`** — any local process could control the
   terminal.
3. **Acceptable status quo: keep `cmuxOnly`** and rely on the existing
   degradations (tmux fallback, detached watcher, manual ⌘W cleanup), which
   all worked in practice. The cost is no native cmux automation from any
   launchd-orphaned context.

Decision owner: user (security posture). No repo code change is required for
any of the three options; option 1 only adds a secret and later doc updates.
