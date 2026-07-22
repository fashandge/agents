#!/bin/bash
# handoff_orchestrator_ensure.sh — idempotent session-watcher bootstrap for the
# handoff-agent skill's monitored launch mode. Replaces the hand-executed
# register+start sequence: creates one private orchestrator state file (when
# needed), registers the exact orchestrator target plus owner PID, and starts
# (or reuses) the singleton watcher.
#
# Usage:
#   handoff_orchestrator_ensure.sh --transport cmux|tmux --owner-pid <pid> \
#       [--target <exact-tmux-handle>] [--state <watcher.json>] [--interval 5]
#
# Behavior:
#   * --state omitted: create $HOME/.local/state/agents/handoff/orchestrators
#     (a mode-700 parent is fine), then a private `mktemp -d .../session.XXXXXX`
#     dir (chmod 700), and use <dir>/watcher.json as the state file.
#   * --state given and the file already exists: skip registration and only run
#     `orchestrator start`, which returns started:false when the same session
#     watcher is already running — not an error.
#   * Otherwise run `orchestrator register`, then `orchestrator start`.
#
# Final stdout is exactly one JSON line:
#   {"state": "<path>", "registered": true|false, "started": true|false}
# `registered` is true only when THIS invocation performed registration.
#
# Invariants:
#   * --owner-pid must be the PID of the long-lived Claude/Codex orchestrator
#     process — never a transient tool shell's $$ or $PPID. Registration
#     captures both PID and process-start identity so PID reuse cannot
#     preserve an orphaned watcher.
#   * cmux registration resolves the orchestrator surface from CMUX_SURFACE_ID
#     in the environment; tmux registration requires --target with the exact
#     orchestrator tmux handle.
#   * Keep the printed state path private and reuse it only for the same
#     orchestrator session; pass it as --orchestrator-state to every monitored
#     handoff_agent.sh launch.
#   * This script never accepts or prints token/credential material.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: handoff_orchestrator_ensure.sh --transport cmux|tmux [--owner-pid <pid>|auto] [--target <exact-tmux-handle>] [--state <watcher.json>] [--interval 5]

Idempotent handoff session-watcher bootstrap: registers the orchestrator
target once (unless --state already exists), then starts or reuses the
singleton watcher. Prints one JSON line:
  {"state": "<path>", "registered": true|false, "started": true|false}

--owner-pid is optional: omit it (or pass "auto") to auto-detect the long-lived
orchestrator agent PID by walking process ancestry (handoff_orchestrator_pid.sh).
Pass an explicit numeric PID to override.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

PYTHON=/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python
HELPER=("$PYTHON" -m agents.orchestration.handoffctl)

transport=""
owner_pid=""
target=""
state=""
interval="5"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --transport) transport=${2:?'missing value for --transport'}; shift 2 ;;
    --owner-pid) owner_pid=${2:?'missing value for --owner-pid'}; shift 2 ;;
    --target)    target=${2:?'missing value for --target'}; shift 2 ;;
    --state)     state=${2:?'missing value for --state'}; shift 2 ;;
    --interval)  interval=${2:?'missing value for --interval'}; shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage >&2; die "unknown argument: $1" ;;
  esac
done

[[ -n "$transport" ]] || { usage >&2; die "--transport is required"; }
case "$transport" in
  cmux|tmux) ;;
  *) die "--transport must be cmux or tmux" ;;
esac

# Resolve the orchestrator PID automatically when omitted or set to "auto":
# handoff_orchestrator_pid.sh walks this call's process ancestry to the nearest
# long-lived agent (claude|codex|kimi). An explicit numeric PID always wins.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$owner_pid" || "$owner_pid" == "auto" ]]; then
  if ! owner_pid=$("$here/handoff_orchestrator_pid.sh" --pid-only 2>/dev/null); then
    die "could not auto-detect the orchestrator PID; pass --owner-pid <pid> explicitly (see handoff_orchestrator_pid.sh)"
  fi
fi
[[ "$owner_pid" =~ ^[0-9]+$ ]] || die "--owner-pid must be the numeric PID of the long-lived orchestrator process (or 'auto'), never a transient tool shell's \$\$ or \$PPID"
if [[ "$transport" == tmux && -z "$target" ]]; then
  die "--target <exact-tmux-handle> is required for --transport tmux"
fi

if [[ -z "$state" ]]; then
  base="$HOME/.local/state/agents/handoff/orchestrators"
  mkdir -p "$base"
  session_root=$(mktemp -d "$base/session.XXXXXX")
  chmod 700 "$session_root"
  state="$session_root/watcher.json"
fi

registered=false
if [[ -f "$state" ]]; then
  # Existing registration: skip register; `orchestrator start` below reuses the
  # singleton watcher and may report started:false — not an error.
  registered=false
else
  mkdir -p "$(dirname "$state")"
  register_args=(orchestrator register --state "$state" --transport "$transport" --owner-pid "$owner_pid")
  [[ -z "$target" ]] || register_args+=(--target "$target")
  "${HELPER[@]}" "${register_args[@]}" >/dev/null
  registered=true
fi

start_out=$("${HELPER[@]}" orchestrator start --state "$state" --interval "$interval")
started=$("$PYTHON" -c 'import json,sys; print("true" if json.loads(sys.argv[1]).get("started") else "false")' "$start_out")

"$PYTHON" -c 'import json,sys; print(json.dumps({"state": sys.argv[1], "registered": sys.argv[2] == "true", "started": sys.argv[3] == "true"}))' \
  "$state" "$registered" "$started"
