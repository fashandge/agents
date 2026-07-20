#!/bin/bash
# tmux_run_logged.sh — run a batch command decoupled from tmux, with a live
# disposable tmux view of its log.
#
# Why two requirements at once:
#   1. VISIBLE: an ad-hoc tmux session running a batch command with output
#      redirected to a log file displays a blank pane that reads as "dead" —
#      a viewer cannot tell progress from failure.
#   2. DECOUPLED: a command run AS the tmux pane command dies with the pane.
#      cmux's remote-tmux mirror makes this a live hazard — the control-mode
#      mapping is bidirectional, so accidentally closing the mirror
#      workspace kills the remote tmux session, SIGHUPs the pane's process
#      group, and destroys hours of batch work (this happened 2026-07-20).
#
# So: the real command runs under `setsid nohup`, immune to tmux/SSH/viewer
# death; the tmux session only runs `tail -F <logfile>` and is disposable —
# closing it (directly or via a mirror workspace) kills only the tail.
#
# Usage:
#   tmux_run_logged.sh <session-name> <logfile> -- <command...>
#
# The logfile gets the command's combined output plus a final EXIT:<code>
# line. Put any completion chain (notifications, timer re-arms) INSIDE the
# command you pass, or watch for the EXIT: marker — never in the tmux
# session. Agent workers (Claude/Codex/Kimi) render their own TUI and need
# nothing extra; use this wrapper for any non-TUI batch job the user may
# want to view.
#
# If a tool is genuinely silent for long stretches, prefer a variant that
# emits periodic progress (verbose/progress flags) so the pane visibly
# advances.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: tmux_run_logged.sh <session-name> <logfile> -- <command...>

Run <command...> under setsid nohup with output to <logfile> (plus a final
EXIT:<code> line), and create a detached, disposable tmux session named
<session-name> running `tail -F <logfile>` as a live view. Closing the view
session never affects the command.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage; exit 0
fi
[[ $# -ge 3 ]] || { usage >&2; die "expected <session-name> <logfile> -- <command...>"; }

session=$1
logfile=$2
shift 2
[[ "${1:-}" == "--" ]] || { usage >&2; die "expected -- before the command"; }
shift
[[ $# -ge 1 ]] || die "no command given after --"

case "$session" in
  *:*|*.*) die "tmux session names may not contain ':' or '.'" ;;
esac
if tmux has-session -t "$session" 2>/dev/null; then
  die "tmux session already exists: $session"
fi

: > "$logfile"

printf -v cmd_quoted '%q ' "$@"
runner="${cmd_quoted}>> $(printf '%q' "$logfile") 2>&1; echo \"EXIT:\$?\" >> $(printf '%q' "$logfile")"
setsid nohup bash -c "$runner" >/dev/null 2>&1 < /dev/null &
runner_pid=$!

tmux new-session -d -s "$session" "tail -F $(printf '%q' "$logfile")"
echo "command running decoupled (setsid pgid $runner_pid); log: $logfile"
echo "view session: $session (disposable; closing it never affects the command)"
echo "attach with: tmux attach -t $session"
