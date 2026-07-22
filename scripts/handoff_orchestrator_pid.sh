#!/bin/bash
# handoff_orchestrator_pid.sh — detect the long-lived orchestrator agent PID for
# the current session by walking the process-ancestry chain.
#
# The handoff session watcher must be bound to the actual long-lived
# Claude/Codex/Kimi CLI process — never a transient tool shell's $$ or $PPID.
# When an agent runs this script (directly or through another script), that
# agent process is always an ANCESTOR of the shell the script runs in, so the
# nearest ancestor whose executable basename is a known agent is the
# orchestrator. This replaces the hand-run `ps`/parent-walk the handoff-agent
# skill previously spelled out.
#
# Scope: the three terminal CLI agents (claude, codex, kimi). The Codex desktop
# app uses a GUI, task-based protocol (not the local-v1 ancestry model), so it
# is intentionally not detected here — its "orchestrator PID" concept does not
# apply to the session watcher.
#
# Usage:
#   handoff_orchestrator_pid.sh [--from-pid <pid>] [--max-depth <n>] [--pid-only]
#
#   --from-pid <pid>   Start the ancestry walk above this PID (default: this
#                      script's own parent). Use when the caller knows the exact
#                      shell whose ancestry should be searched.
#   --max-depth <n>    Stop after inspecting <n> ancestors (default: 40).
#   --pid-only         Print just the numeric PID on success (for `$(...)`
#                      capture) instead of the JSON object.
#
# Success (stdout, one JSON line unless --pid-only):
#   {"pid": 44627, "agent": "claude", "command": "...", "depth": 2}
# Failure (stderr JSON, nonzero exit) enumerates what was searched:
#   {"error": "...", "searched_agents": [...], "inspected_pids": [...]}
#
# This script never reads, prints, or accepts any token/credential material.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: handoff_orchestrator_pid.sh [--from-pid <pid>] [--max-depth <n>] [--pid-only]

Detect the long-lived orchestrator agent PID (claude|codex|kimi) by walking the
process-ancestry chain from the calling shell. Prints one JSON line:
  {"pid": <n>, "agent": "<name>", "command": "<argv>", "depth": <k>}
or, with --pid-only, just <n>. Exits nonzero with a JSON error (naming the
agents searched and PIDs inspected) when no agent ancestor is found.
EOF
}

# Known agent executable basenames, in resolution priority order. A basename is
# compared after stripping a Node/Python script extension so a node-wrapped
# entry point (e.g. `node .../codex.js`) still resolves.
AGENTS=(claude codex kimi)

from_pid=""
max_depth=40
pid_only=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-pid)  from_pid=${2:?'missing value for --from-pid'}; shift 2 ;;
    --max-depth) max_depth=${2:?'missing value for --max-depth'}; shift 2 ;;
    --pid-only)  pid_only=true; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage >&2; echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ "$max_depth" =~ ^[0-9]+$ ]] || { echo "--max-depth must be a non-negative integer" >&2; exit 2; }
if [[ -n "$from_pid" ]]; then
  [[ "$from_pid" =~ ^[0-9]+$ ]] || { echo "--from-pid must be a numeric PID" >&2; exit 2; }
fi

# Start above the script's own parent unless the caller pinned a starting PID.
start_pid=${from_pid:-$PPID}

ppid_of() { ps -o ppid= -p "$1" 2>/dev/null | tr -d ' '; }
command_of() { ps -o command= -p "$1" 2>/dev/null; }

# basename minus a trailing script extension, so `codex.js` -> `codex`.
strip_ext() {
  local b=$1
  b=${b##*/}
  b=${b%.js}; b=${b%.mjs}; b=${b%.cjs}; b=${b%.py}
  printf '%s' "$b"
}

# Does the command line launch <agent>? Only the EXECUTABLE is inspected — the
# first token's basename, plus the script arg when the executable is a known
# interpreter (`node .../codex.js`). Scanning every arg would false-match any
# shell whose command line merely mentions the word (a snapshot path, an env
# var, or an echoed string), which is why matching is anchored to the leader.
command_launches_agent() {
  local command=$1 agent=$2
  local -a tokens=($command)
  [[ ${#tokens[@]} -gt 0 ]] || return 1
  local base
  base=$(strip_ext "${tokens[0]}")
  [[ "$base" == "$agent" ]] && return 0
  case "$base" in
    node|nodejs|deno|bun|python|python3)
      if [[ ${#tokens[@]} -ge 2 ]]; then
        [[ "$(strip_ext "${tokens[1]}")" == "$agent" ]] && return 0
      fi
      ;;
  esac
  return 1
}

inspected=()
cur=$start_pid
depth=0
while [[ -n "$cur" && "$cur" != "0" && "$cur" != "1" && "$depth" -lt "$max_depth" ]]; do
  command=$(command_of "$cur")
  if [[ -n "$command" ]]; then
    inspected+=("$cur")
    for agent in "${AGENTS[@]}"; do
      if command_launches_agent "$command" "$agent"; then
        if [[ "$pid_only" == true ]]; then
          printf '%s\n' "$cur"
        else
          # Emit JSON with the command JSON-escaped via python (no interpolation
          # of the raw argv into a shell-built JSON string).
          /opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python - "$cur" "$agent" "$command" "$depth" <<'PY'
import json, sys
pid, agent, command, depth = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
# Truncate the argv to a short identifying prefix; the full settings blob is
# noise for this purpose and costly to carry.
if len(command) > 200:
    command = command[:197] + "..."
print(json.dumps({"pid": int(pid), "agent": agent, "command": command, "depth": int(depth)}))
PY
        fi
        exit 0
      fi
    done
  fi
  cur=$(ppid_of "$cur")
  depth=$((depth + 1))
done

# No agent ancestor found: report what was searched so the caller can diagnose.
/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python - "${inspected[@]:-}" <<'PY' >&2
import json, sys
inspected = [int(p) for p in sys.argv[1:] if p]
print(json.dumps({
    "error": "no orchestrator agent process found in ancestry",
    "searched_agents": ["claude", "codex", "kimi"],
    "inspected_pids": inspected,
    "hint": "pass --from-pid <shell-pid> if the agent is not an ancestor of this shell",
}))
PY
exit 3
