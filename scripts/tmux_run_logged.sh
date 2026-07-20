#!/bin/bash
# tmux_run_logged.sh — run a batch command in a detached tmux session whose
# pane stays live AND whose output is captured to a logfile.
#
# Why: an ad-hoc tmux session running a batch command (a rebuild, a sweep, a
# bulk download) with output redirected to a log file displays a blank pane
# that reads as "dead" — a viewer cannot tell progress from failure. Agent
# workers (Claude/Codex/Kimi) render their own TUI and need nothing extra;
# use this wrapper for any non-TUI tmux session the user may view.
#
# Usage:
#   tmux_run_logged.sh <session-name> <logfile> -- <command...>
#
# Behavior: creates a detached tmux session running
#   <command...> 2>&1 | tee <logfile>; echo EXIT:${PIPESTATUS[0]} | tee -a <logfile>
# so output goes to both the pane and the logfile. The exit marker uses
# PIPESTATUS[0] — plain $? after a pipe would report tee's status, not the
# command's. The inner pipeline runs under bash so PIPESTATUS is available
# regardless of the tmux default shell.
#
# If a tool is genuinely silent for long stretches, prefer a variant that
# emits periodic progress (verbose/progress flags) so the pane visibly
# advances.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: tmux_run_logged.sh <session-name> <logfile> -- <command...>

Create a detached tmux session running <command...> with output tee'd to both
the pane and <logfile>, appending EXIT:<code> (from PIPESTATUS[0]) to both
when the command completes.
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

printf -v cmd_quoted '%q ' "$@"
inner="${cmd_quoted}2>&1 | tee $(printf '%q' "$logfile"); st=\${PIPESTATUS[0]}; echo \"EXIT:\$st\" | tee -a $(printf '%q' "$logfile")"
tmux new-session -d -s "$session" "bash -c $(printf '%q' "$inner")"
echo "session $session running detached; log: $logfile"
echo "attach with: tmux attach -t $session"
