# Lightweight worker spawn end-to-end test

A manual/agent-driven end-to-end exercise of `scripts/spawn_worker.sh` across a
**local** and a **remote** worker, on every agent kind. It is written to be
executed by an agent, so every step is a literal command with a checkable
assertion rather than prose.

Its sibling, `handoff-two-round-e2e.md`, tests the durable protocol. This
document tests the mode that has none — so almost every assertion here is about
something that must **not** exist.

## Mode

The test runs in two modes, decided by the **driving** session's environment:

- **herdr mode** (`HERDR_ENV == "1"`): local workers are spawned as herdr tabs
  in the driving session's own workspace (`$HERDR_WORKSPACE_ID`), reported as
  `backend: herdr` with a pane handle (`w<ws>:p<n>`).
- **cmux mode** (anything else, with a reachable cmux socket): local workers are
  cmux surfaces in `$CMUX_WORKSPACE_ID`, reported as `backend: cmux` with a
  surface UUID. With neither, the backend falls through to `tmux` and the handle
  is a session name.

Remote workers are **herdr-only** in this mode, from either driving mode, and
are reported as `backend: ssh_herdr`. There is no `--remote-backend` flag: the
heavy launcher's tmux fallback exists to be reconciled against a record this
mode does not keep, so an unreachable remote herdr server is an error instead.

Commands without a mode marker work in both.

## What this exercises

- Backend autoselection, and a local worker landing in the workspace the human
  is already looking at.
- A remote worker landing in the `REMOTE_WORKERS` workspace on the host's own
  herdr server, resolved by label and created on first use.
- All four agent kinds, which take **three different delivery paths**: argv
  (claude, codex, pi), a typed file pointer (kimi), and a cleared folder-trust
  dialog (claude, codex, kimi — kimi's must clear *before* its bootstrap wait,
  since the dialog sits above the widget the pointer is typed into).
- The safe-exec boundary: the terminal receives only two quoted paths, so a
  prompt full of shell metacharacters cannot be interpreted.
- **The point of the test: nothing durable is created and the worker does not
  believe it is in a handoff run.** Phases 3 and 4 exist for this. A unit test
  mocks `os.execvpe`, so only a live worker can prove what its process
  environment actually contains — and `env.build_env()` copies `os.environ`,
  which makes an inherited `HANDOFF_RUN_DIR` a reachable leak whenever a
  lightweight worker is spawned from inside a handoff worker.

Phases 1 and 2 alone will pass against a build that quietly re-registers every
spawn. Do not shorten this test.

## Preconditions

```bash
echo "HERDR_ENV=${HERDR_ENV:-<unset>}"          # "1" → herdr mode, else cmux/tmux mode
[ "$HERDR_ENV" = "1" ] && herdr status server   # herdr mode: server must be running
~/projects/agents/scripts/fleet_status.sh       # every checkout dirty=0
ssh -o ConnectTimeout=8 oci-box 'echo ok; which claude codex kimi pi herdr'
ssh oci-box 'herdr status server'               # must report running
```

`<helper>` throughout is:

```text
/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python -m agents.orchestration.handoffctl
```

`<spawn>` throughout is `~/projects/agents/scripts/spawn_worker.sh`.

If the box is stopped and the request authorizes using it:
`~/projects/investment/src/scripts/oci_box_ctl.sh up`. **A rebooted box has no
herdr server** — it is not started by systemd or cron. Start one headlessly
before the remote phase, and say so in the result:

```bash
ssh oci-box 'nohup herdr server >/tmp/herdr-server.log 2>&1 &'
ssh oci-box 'herdr status server'               # status: running
```

If the box's `agents` checkout predates this module, the remote phase fails with
an error naming `update_agents.sh`. That is Phase 5's assertion; refresh the
host before Phase 2, and run Phase 5 first if you want to observe the error.
The script takes no host argument — it updates the checkout it runs in, so it
runs **on** the box:

```bash
ssh oci-box 'cd ~/projects/agents && ./scripts/update_agents.sh'
```

Pick a unique label prefix per execution (e.g. `sw-<MMDD>-a`) so a previous
execution's tabs and scratch dirs can never be mistaken for this one's.

## Baseline snapshot

Take this **before** the first spawn. Every Phase 3 assertion is a diff against
it, so a pre-existing run from another session is never miscounted as damage.

```bash
<helper> runs list > /tmp/sw-runs.before
find ~/.local/state/agents/handoff -maxdepth 2 | sort > /tmp/sw-handoff.before
ls ~/.local/state/agents/spawn/ | sort         > /tmp/sw-spawn.before
pgrep -f agents.orchestration.handoffctl | sort > /tmp/sw-watchers.before  # may be empty
herdr workspace list                           > /tmp/sw-ws.before          # herdr mode
herdr tab list --workspace "$HERDR_WORKSPACE_ID" > /tmp/sw-tabs.before      # herdr mode
```

`~/.local/state/agents/spawn/` is created by the first spawn if absent. The
**root** persisting is expected; per-spawn directories inside it are not (see
Phase 3).

## Procedure

There is no orchestrator session to bootstrap, no watcher to start, and no
ordering constraint between spawns. That is itself an assertion: run the whole
document from the driving session, and never register anything.

### Phase 1 — local spawn, one per agent kind

Write four prompts to a scratch directory outside the repo. Each is a plain task
with **no role, no protocol, and no mention of an orchestrator** — that contract
belongs to the `spawn-worker` skill and is asserted in Phase 4.

Give each agent a *different* arithmetic expression with a distinct value (e.g.
`12 * 12` → 144, `100 - 37` → 63, `2 ** 10` → 1024, `999 / 3` → 333) so one
worker's answer can never be mistaken for another's.

```bash
S=$(mktemp -d)
cat > "$S/pi.md" <<'EOF'
Compute 12 * 12. Report the closed form and an independent check you run
yourself. Work only in a scratch directory; do not modify any file in this
repository and do not commit.
EOF
# ...one per agent, each with its own expression

<spawn> sw-<MMDD>-pi     "$S/pi.md"     ~/projects/agents --agent pi
<spawn> sw-<MMDD>-claude "$S/claude.md" ~/projects/agents --agent claude
<spawn> sw-<MMDD>-codex  "$S/codex.md"  ~/projects/agents --agent codex
<spawn> sw-<MMDD>-kimi   "$S/kimi.md"   ~/projects/agents --agent kimi
```

Assertions on each JSON line — the command's whole output contract:

- `backend` matches the driving mode (`herdr` / `cmux` / `tmux`), and `handle`
  has that backend's shape.
- `model` / `effort` equal `AGENT_DEFAULTS` for that agent: claude `opus`/`high`,
  codex `gpt-5.6-terra`/`xhigh`, kimi `kimi-code/k3`/`max`, pi
  `deepseek/deepseek-v4-flash`/`max`. A drift here means the lightweight path
  has grown its own copy of the table.
- `prompt_sent: true` for all four.
- claude and codex additionally report `folder_trust_rescued` (either boolean is
  a pass; `startup_unconfirmed: true` is not).
- **No `run_id`, `run_dir`, `transport`, or `registry_recorded` field exists.**
  Those are the heavy launcher's; their presence means the two paths have been
  merged.
- The command returned promptly for claude, codex, and pi. Only kimi waits, and
  only for its input widget.

Then confirm each worker actually ran and answered correctly:

```bash
herdr agent list                                                    # herdr mode
herdr pane read <handle> --source recent-unwrapped --lines 120 --format text
cmux read-screen --surface <uuid> --scrollback                      # cmux mode
tmux capture-pane -p -t <session> -S -200                           # tmux mode
```

**Kimi's assertion is specific.** Its transcript must show it being handed the
*path* to the prompt file and reading it — the prompt body must never appear as
typed input in its composer. A kimi worker that received the body directly means
the pointer delivery regressed into typing.

### Phase 2 — remote spawn

```bash
<spawn> sw-<MMDD>-remote "$S/remote.md" \
  --agent pi --remote-host oci-box \
  --remote-cwd /home/opc/projects/agents \
  --remote-python /home/opc/miniforge3/envs/ml/bin/python
```

Assertions:

- `backend: ssh_herdr`, `host: oci-box`, `handle` is a remote pane ID.
- `prompt_path` is a path **on the box**, under
  `/home/opc/.local/state/agents/spawn/`.
- The tab landed in the `REMOTE_WORKERS` workspace, not a scattered one:

```bash
ssh oci-box 'herdr workspace list'   # find the workspace whose label is REMOTE_WORKERS
ssh oci-box 'herdr tab list --workspace <that id>'   # contains sw-<MMDD>-remote
ssh oci-box 'herdr pane read <handle> --source recent-unwrapped --lines 120 --format text'
```

- On a box that had no `REMOTE_WORKERS` workspace, one was created; on a box
  that had one, no second one appeared.

### Phase 3 — nothing durable was created

Diff every baseline. This is the load-bearing phase.

Check for **additions**, not exact equality. This machine is shared: another
session may register a run or let its watcher exit while this test runs, and
neither is this test's doing. A disappearance is someone else's business; an
appearance is the failure.

```bash
<helper> runs list | jq -r '.[].name' | sort > /tmp/sw-runnames.after
comm -13 <(jq -r '.[].name' /tmp/sw-runs.before | sort) /tmp/sw-runnames.after
comm -13 /tmp/sw-handoff.before <(find ~/.local/state/agents/handoff -maxdepth 2 | sort)
comm -13 /tmp/sw-watchers.before <(pgrep -f agents.orchestration.handoffctl | sort)
```

All three must print nothing. If the second or third prints a line, check
whether any `sw-<MMDD>-*` label appears in it before blaming another session.
Specifically:

- **No new registry record** for any of the five labels.
- **No new run directory** under any `~/.local/state/agents/handoff/<workspace>/runs/`.
- **No new entry** in `~/.local/state/agents/handoff/credentials/` or
  `orchestrators/`.
- **No new watcher process.** No `orchestrator start` was ever run, so none may
  exist that did not exist before. A detached watcher forks without exec, so it
  carries its parent's `orchestrator start` command line rather than the
  watcher module's name; a surface-hosted one runs `handoffctl watch`. Matching
  on `agents.orchestration.handoffctl` catches both — matching on
  `handoff_watcher` catches neither and makes the check vacuous.

Now the per-spawn private directories:

```bash
ls ~/.local/state/agents/spawn/
find ~/.local/state/agents/spawn -type f
```

- The four local spawns passed a caller-supplied prompt file, so their private
  directories are **gone**: the wrapper unlinks the wrapper and config and then
  removes the directory, because there is nothing left to keep.
- No `launch_worker.py` or `launch.json` survives anywhere. Their survival means
  a wrapper failed before `execvpe` — check that pane for a traceback.

The piped-prompt path keeps its directory instead, which is the other half of
the same rule:

```bash
printf 'Compute 7 * 6 and report the result.\n' | \
  <spawn> sw-<MMDD>-piped - ~/projects/agents --agent pi
```

- `prompt_path` points into a new `~/.local/state/agents/spawn/sw-<MMDD>-piped-*/`
  directory that **still exists** and holds `prompt.md` at mode `0600` — the
  piped prompt has no other home, so a human can still read what was asked.
- That directory holds `prompt.md` and nothing else.

### Phase 4 — the worker does not know it is a worker

Two probes, deliberately separate: naming `HANDOFF_*` in a prompt would
contaminate the unawareness assertion.

**Probe A — unawareness.** Re-read the four Phase 1 transcripts. In none of them
may the worker:

- refer to a handoff, an orchestrator, a review, a lease, or a run id;
- run `handoffctl` or look for a run directory or inbox;
- ask a question and wait, or announce that it is publishing a result for
  review, rather than simply finishing the task.

Any of those means protocol vocabulary reached the prompt.

**Probe B — process environment.** A separate spawn whose task *is* the
environment read:

```bash
cat > "$S/env.md" <<'EOF'
Print the values of the environment variables HANDOFF_RUN_DIR and
HANDOFF_WORKER_TOKEN_FILE, or the word "unset" for each one that is not set.
Report the two lines and stop. Do not modify any file.
EOF
<spawn> sw-<MMDD>-env "$S/env.md" ~/projects/agents --agent pi
```

Both must report **unset**. This is the assertion a unit test cannot make:
`env.build_env()` starts from `os.environ.copy()`, so the wrapper explicitly
removes both keys. Run this probe again from inside a live handoff worker — the
nested-delegation case, where the spawning process genuinely has
`HANDOFF_RUN_DIR` set — and both must still report unset. A worker that inherits
a pointer will act on **another run's** protocol state.

### Phase 5 — the two error paths

Both must fail loudly and name their fix, rather than degrading silently.

**Stale remote package.** On a host whose checkout predates the module:

```bash
<spawn> sw-<MMDD>-stale "$S/pi.md" --agent pi --remote-host <stale-host> \
  --remote-cwd /home/opc/projects/agents
```

Expect a non-zero exit and a message containing both `No module named` and
`update_agents.sh`. A silent tmux fallback here would be the bug.

**Shell safety.** The prompt is never interpreted:

```bash
printf '%s\n' 'Report the literal text of this line, then stop: $(touch /tmp/SPAWN_PWNED); `id`; $HOME' \
  > "$S/meta.md"
<spawn> sw-<MMDD>-meta "$S/meta.md" ~/projects/agents --agent pi
ls /tmp/SPAWN_PWNED       # must not exist
```

Read the pane's scrollback: the shell line above the agent must be exactly
`exec <path>/launch_worker.py <path>/launch.json` and contain **no fragment of
the prompt**. The metacharacters reach the agent as one argv element.

### Phase 6 — teardown

This mode tracks nothing, so cleanup is manual by design and there is no
`runs clean` step. Close exactly the tabs this test created, by exact ID, with
snapshot/verify guards — never a sweep, never positional.

**herdr mode:**

```bash
herdr tab list --workspace "$HERDR_WORKSPACE_ID"   # find each label's tab_id
herdr tab close <tab_id>                            # one call per test tab
diff /tmp/sw-ws.before <(herdr workspace list)      # every workspace still present
# verify every OTHER pre-existing tab in /tmp/sw-tabs.before survived.
# If anything else disappeared, STOP all layout actions and report to the user.
```

**cmux mode:**

```bash
~/projects/agents/scripts/cmux_close_surface_safe.sh --surface <uuid>
```

**Remote:**

```bash
ssh oci-box 'herdr tab list --workspace <REMOTE_WORKERS id>'
ssh oci-box 'herdr tab close <tab_id>'
```

Leave the `REMOTE_WORKERS` workspace itself in place — it is shared with the
heavy launcher and is created once per box. Then remove this run's leftovers:

```bash
rm -rf "$S"
rm -rf ~/.local/state/agents/spawn/sw-<MMDD>-*   # the piped-prompt directory
ssh oci-box 'rm -rf ~/.local/state/agents/spawn/sw-<MMDD>-*'
```

If you started the box's herdr server for this test, leaving it running is fine
— the box self-stops on idle.

## Pass criteria

1. Five local workers (four agent kinds plus the piped-prompt spawn) landed in
   the driving session's own workspace with the mode-appropriate backend, and
   one remote worker landed in `REMOTE_WORKERS` as `ssh_herdr`.
2. Every worker answered its arithmetic correctly, and no two answers were
   confusable.
3. Model and effort matched `AGENT_DEFAULTS` for every agent; kimi received a
   path, not a prompt body.
4. **Phase 3 diffs all empty**: no registry record, no run directory, no
   credential or orchestrator entry, no watcher process. No JSON line carried a
   `run_id` or `transport`.
5. Caller-supplied-prompt spawn directories are gone; the piped-prompt directory
   survives holding only a `0600` `prompt.md`; no wrapper or config survives
   anywhere.
6. **Phase 4**: no worker referenced the protocol, and both `HANDOFF_*`
   variables read `unset` — including from a spawn made inside a live handoff
   worker.
7. Phase 5: the stale-host error names `update_agents.sh`; `/tmp/SPAWN_PWNED`
   does not exist and no prompt fragment appears in any shell line.
8. `fleet_status.sh` unchanged throughout; no commits; no worker wrote inside a
   repository.
9. Teardown: every test tab gone, and **every pre-existing workspace, tab, and
   surface still present**.

## Known pitfalls

- **Absence assertions need the baseline, and only look for additions.**
  `runs list` is never empty on a working machine and a detached watcher from
  another session is normally already running — "there are runs in the
  registry" and "a handoffctl process exists" are not failures. Compare with
  `comm -13` against the baseline, and treat only *new* entries carrying this
  test's labels as evidence.
- **The `spawn/` root persisting is not a leak.** `_spawn_root()` recreates it on
  every spawn at mode `0700`. Only per-spawn directories inside it are supposed
  to vanish.
- **A rebooted box has no herdr server**, and remote spawn is herdr-only. The
  failure is a clear `server_not_running` error from herdr, not a tmux fallback
  — do not "fix" it by reaching for `--remote-backend`, which does not exist
  here.
- **`prompt_sent: true` means delivered, not answered.** For claude, codex, and
  pi it means the prompt is on the agent's argv; the command does not wait for
  the agent to finish, and is not supposed to. Read the pane for the answer.
- **Only kimi's spawn blocks**, and only until its input widget appears. A
  `prompt_sent: false` with a `rescue_command` is kimi's startup wait expiring
  — read the pane before concluding the delivery ladder regressed. Both its
  waits share one budget, so the ceiling is `KIMI_BOOTSTRAP_TIMEOUT` total, not
  per phase; a spawn that takes visibly longer than that is a regression.
- **A worker sitting on a folder-trust dialog** means the rescue did not fire.
  All three of claude, codex, and kimi are gated. It reports
  `startup_unconfirmed: true` when the dialog named a directory other than the
  one launched into; that is deliberate — clearing an unrelated directory's
  trust dialog is not authorized. Clear it by hand and note it.
- **Kimi's trust is remembered per folder**, in
  `~/.kimi-code/workspace-trust/wd_<slug>`, so the gate only appears on the
  first spawn into a checkout and a passing run proves nothing on the second.
  To exercise it deliberately, back up and delete that file, spawn, then
  confirm `folder_trust_rescued: true` with `prompt_sent: true` in a few
  seconds rather than a two-minute stall.
- **Do not add monitoring to diagnose a stuck worker.** There is no watcher and
  no doorbell in this mode. Read the pane, or type into it directly with
  `herdr agent prompt <target> "<text>"`.
- **Pin the repo revision.** Record `git rev-parse HEAD` on both hosts at the
  start and end, and diff them. Another session committing to this repo
  mid-test invalidates the Phase 3 assertions.
- **Do not run this test's teardown against handoff runs.** The two documents
  share a machine; `runs clean` belongs to the other one and would reap workers
  this test never created.
