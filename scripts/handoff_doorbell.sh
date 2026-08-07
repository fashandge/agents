#!/bin/bash
# handoff_doorbell.sh — manual handoff doorbell with the submit discipline.
#
# Why this exists: agent TUIs (Claude/Codex/Kimi) coalesce a same-burst
# text+Enter into a bracketed paste, so the Enter becomes an input newline and
# the doorbell sits unsent in the composer while looking delivered. Launcher
# and `handoffctl send/dispatch` doorbells handle this automatically (settle +
# verify + retry). Use this script for the narrow manual fallback — e.g. a
# `send` that reported doorbell_sent:false and returned a durable message seq.
#
# Usage:
#   handoff_doorbell.sh --run-id <id> --seq <n> \
#     (--herdr-pane <id> | --cmux-surface <uuid> | --tmux-session <name>) \
#     [--remote-host <ssh-host>]
#
# What it does, in order:
#   1. Sends the fixed opaque text `Check handoff run <id>; inbox now through
#      seq <n>.` and then, as a SEPARATE command ~1s later, Enter:
#        cmux:        cmux send --surface <uuid> "<text>"; cmux send-key --surface <uuid> enter
#        tmux:        tmux send-keys -t <name> -l "<text>"; tmux send-keys -t <name> Enter
#        remote:      the same forms via `ssh <host> ...`
#      herdr is the exception: `herdr agent prompt` submits text and Enter in
#      one call against the pane's live bracketed-paste mode, so there is no
#      burst to split apart.
#   2. Captures the pane (`herdr pane read <id>` /
#      `cmux read-screen --surface <uuid>` /
#      `tmux capture-pane -p -t <name> -S -50`, remote via ssh) and checks the
#      bottom lines; if the doorbell text still sits in the composer, sends one
#      more bare Enter (harmless if it already submitted).
#   3. Reports every step on stdout.
#
# Invariants:
#   * The body is always the fixed opaque template above — this script never
#     accepts an arbitrary message body, steering text, or credential bytes.
#   * Never assume delivery from a zero exit code: on the worker side only an
#     advanced inbox_cursor proves receipt.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: handoff_doorbell.sh --run-id <id> --seq <n> (--herdr-pane <id> | --cmux-surface <uuid> | --tmux-session <name>) [--remote-host <ssh-host>]

Send the fixed opaque handoff doorbell text, then Enter as a SEPARATE command
~1s later (bracketed-paste submit discipline), verify the composer cleared,
and send one more bare Enter if the text still sits there. The message body
is always the fixed template: Check handoff run <id>; inbox now through seq <n>.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

run_id=""
seq=""
surface=""
session=""
pane=""
remote_host=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id)       run_id=${2:?'missing value for --run-id'}; shift 2 ;;
    --seq)          seq=${2:?'missing value for --seq'}; shift 2 ;;
    --herdr-pane)   pane=${2:?'missing value for --herdr-pane'}; shift 2 ;;
    --cmux-surface) surface=${2:?'missing value for --cmux-surface'}; shift 2 ;;
    --tmux-session) session=${2:?'missing value for --tmux-session'}; shift 2 ;;
    --remote-host)  remote_host=${2:?'missing value for --remote-host'}; shift 2 ;;
    -h|--help)      usage; exit 0 ;;
    *)              usage >&2; die "unknown argument: $1" ;;
  esac
done

[[ -n "$run_id" && -n "$seq" ]] || { usage >&2; die "--run-id and --seq are required"; }
[[ "$run_id" =~ ^[A-Za-z0-9._-]+$ ]] || die "run id contains unexpected characters: $run_id"
[[ "$seq" =~ ^[0-9]+$ ]] || die "--seq must be a non-negative integer"
targets=0
[[ -z "$pane" ]] || targets=$((targets + 1))
[[ -z "$surface" ]] || targets=$((targets + 1))
[[ -z "$session" ]] || targets=$((targets + 1))
if [[ "$targets" -ne 1 ]]; then
  { usage >&2; die "choose exactly one of --herdr-pane, --cmux-surface, or --tmux-session"; }
fi
if [[ -n "$remote_host" && -n "$surface" ]]; then
  die "--remote-host works with --herdr-pane or --tmux-session; a cmux surface is local-only"
fi

text="Check handoff run $run_id; inbox now through seq $seq."

# Run one command, locally or on the remote host over ssh. Every argument is
# %q-quoted so the remote shell re-parses it byte-identically.
run_cmd() {
  if [[ -n "$remote_host" ]]; then
    local quoted=() arg
    for arg in "$@"; do quoted+=("$(printf '%q' "$arg")"); done
    ssh "$remote_host" "${quoted[*]}"
  else
    "$@"
  fi
}

send_enter() {
  if [[ -n "$pane" ]]; then run_cmd herdr pane send-keys "$pane" enter
  elif [[ -n "$surface" ]]; then cmux send-key --surface "$surface" enter
  else run_cmd tmux send-keys -t "$session" Enter; fi
}

capture_pane() {
  if [[ -n "$pane" ]]; then
    run_cmd herdr pane read "$pane" --source recent-unwrapped --lines 50
  elif [[ -n "$surface" ]]; then cmux read-screen --surface "$surface"
  else run_cmd tmux capture-pane -p -t "$session" -S -50; fi
}

target_desc="tmux session $session"
[[ -z "$pane" ]] || target_desc="herdr pane $pane"
[[ -z "$surface" ]] || target_desc="cmux surface $surface"
[[ -z "$remote_host" ]] || target_desc="$target_desc on $remote_host"

if [[ -n "$pane" ]]; then
  # herdr submits text and Enter in one call, honoring the pane's live
  # bracketed-paste mode; there is no burst for the TUI to coalesce.
  run_cmd herdr agent prompt "$pane" "$text"
  echo "doorbell: submitted text and Enter atomically to $target_desc: $text"
else
  if [[ -n "$surface" ]]; then cmux send --surface "$surface" "$text"
  else run_cmd tmux send-keys -t "$session" -l "$text"; fi
  echo "doorbell: sent text to $target_desc: $text"
  sleep 1
  send_enter
  echo "doorbell: sent Enter as a separate command (~1s later, bracketed-paste discipline)"
fi

sleep 1
bottom=$(capture_pane | tail -n 8)
if grep -qF "$text" <<<"$bottom"; then
  send_enter
  echo "verify: doorbell text still sat in the composer; sent one more bare Enter"
else
  echo "verify: composer no longer shows the doorbell text (submitted)"
fi
echo "note: a zero exit code is not delivery proof — only the worker's advanced inbox_cursor proves receipt"
