#!/bin/zsh
# Quiet cmux remote-tmux mirroring: every tmux session on <ssh-host> ends up
# as a mirror workspace grouped directly under the calling workspace, with
# minimal visual noise.
#
#   Path A (host already connected): per-session `cmux rpc remote.tmux.attach`
#     revives/creates the mirror views with NO new window at all.
#   Path B (cold connection): `cmux ssh-tmux --no-focus` (cmux insists on a
#     dedicated window), then: minimize the window while its title is still
#     unique, move every mirrored workspace under the calling workspace, and
#     best-effort close the vacated window (cmux may refill it with a
#     placeholder; it is re-minimized so the husk sits in the Dock).
#
# Usage: cmux_ssh_tmux_quiet.sh <ssh-host> [session ...]
#   With session names, only those sessions are attached/placed; other
#   sessions on the host are left wherever they are (another orchestrator's
#   mirrors are not disturbed). With no names, all sessions are placed.
# Requires: run inside cmux (CMUX_WORKSPACE_ID), "Remote tmux" beta enabled.
# The minimize steps need cmux to have macOS Accessibility access; without it
# the script still works, the window just stays visible.
set -euo pipefail

host=${1:?usage: cmux_ssh_tmux_quiet.sh <ssh-host> [session ...]}
shift
typeset -a only_sessions
only_sessions=("$@")
after_ws=${CMUX_WORKSPACE_ID:?not inside cmux (CMUX_WORKSPACE_ID unset)}
cmux_bin=${CMUX_BIN:-cmux}
UUID_RE='[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}'

uuid_of() { echo "$1" | grep -oiE "$UUID_RE" | head -1; }

wanted() {
  [[ ${#only_sessions[@]} -eq 0 ]] && return 0
  local s
  for s in "${only_sessions[@]}"; do [[ "$s" == "$1" ]] && return 0; done
  return 1
}

# Resolve the calling window's UUID: scan each window's workspace list for
# the target workspace UUID (identify reports only short refs).
target_window=$(
  "$cmux_bin" --id-format uuids list-windows | while read -r line; do
    w=$(uuid_of "$line")
    [[ -n "$w" ]] || continue
    # Do not pipe the cmux listing into `grep -q`: with `pipefail`, grep's
    # intentional early exit can make cmux report SIGPIPE and turn this
    # successful match into a false negative.
    window_workspaces=$("$cmux_bin" --id-format uuids list-workspaces --window "$w" 2>/dev/null)
    if grep -qiF "$after_ws" <<<"$window_workspaces"; then
      echo "$w"; break
    fi
  done
)
[[ -n "$target_window" ]] || { echo "ERROR: cannot find window owning $after_ws" >&2; exit 1; }

# Place the workspace named $1 (uuid $2) under the calling workspace unless
# it is already in the target window.
anchor=$after_ws
place() {
  local ws_name=$1 ws_uuid=$2
  if "$cmux_bin" --id-format uuids list-workspaces --window "$target_window" 2>/dev/null \
      | grep -qi "$ws_uuid"; then
    echo "workspace $ws_uuid $ws_name (already placed)"
  else
    "$cmux_bin" move-workspace-to-window --workspace "$ws_uuid" --window "$target_window" >/dev/null
    "$cmux_bin" reorder-workspace --workspace "$ws_uuid" --after "$anchor" >/dev/null
    echo "workspace $ws_uuid $ws_name (placed)"
  fi
  anchor=$ws_uuid
}

# --- Path A: connection already up -> silent per-session RPC attach ---------
if sessions_json=$("$cmux_bin" rpc remote.tmux.sessions "{\"host\":\"$host\"}" 2>/dev/null); then
  typeset -a remote_sessions
  remote_sessions=("${(@f)$(print -r -- "$sessions_json" | grep -oE '"name" *: *"[^"]+"' | sed -E 's/.*: *"([^"]+)"/\1/')}")

  for session in "${remote_sessions[@]}"; do
    wanted "$session" || continue
    "$cmux_bin" rpc remote.tmux.attach "{\"host\":\"$host\",\"session\":\"$session\"}" >/dev/null 2>&1 || true
  done
  sleep 2

  # Recent cmux versions can acknowledge an RPC attach without creating a
  # visible local workspace. Only use the no-window path when every requested
  # mirror actually materialized; otherwise use ssh-tmux below.
  rpc_views_complete=true
  for session in "${remote_sessions[@]}"; do
    wanted "$session" || continue
    line=$("$cmux_bin" --id-format uuids list-workspaces 2>/dev/null \
      | grep -iE " $session( +\[selected\])?\$" | head -1 || true)
    ws_uuid=$(uuid_of "${line:-}" || true)
    if [[ -n "$ws_uuid" ]]; then
      place "$session" "$ws_uuid"
    else
      rpc_views_complete=false
      echo "workspace ? $session (RPC attach returned without a view)"
    fi
  done
  if [[ "$rpc_views_complete" == true ]]; then
    echo "mode rpc-attach (no window created)"
    exit 0
  fi
  echo "RPC attach did not materialize every requested view; falling back to ssh-tmux"
fi

# --- Path B: cold connection -> ssh-tmux window flow ------------------------
out=$("$cmux_bin" --id-format both ssh-tmux "$host" --no-focus)
mirror_window=$(uuid_of "$(echo "$out" | grep -oiE "window=$UUID_RE")")
[[ -n "$mirror_window" ]] || { echo "ERROR: ssh-tmux gave no window uuid: $out" >&2; exit 1; }

# Wait for the mirrored session workspaces to materialize.
deadline=$((SECONDS + 20))
typeset -a ws_lines
while (( SECONDS < deadline )); do
  ws_lines=("${(@f)$("$cmux_bin" --id-format uuids list-workspaces --window "$mirror_window" 2>/dev/null | grep -v '^\s*$' || true)}")
  [[ ${#ws_lines[@]} -gt 0 && -n "${ws_lines[1]// /}" ]] && break
  sleep 1
done
[[ ${#ws_lines[@]} -gt 0 ]] || { echo "ERROR: no mirror workspaces appeared" >&2; exit 1; }

# Minimize the mirror window NOW, while its title is still the selected
# mirrored session's name (after the move it becomes an ambiguous "~").
selected_name=$(printf '%s\n' "${ws_lines[@]}" | grep -F '[selected]' | head -1 \
  | sed -E 's/^\*? *[^ ]+ +//; s/ +\[selected\].*$//')
minimized=visible
if [[ -n "$selected_name" ]]; then
  if osascript -e "tell application \"System Events\" to tell process \"cmux\" to set value of attribute \"AXMinimized\" of (first window whose name is \"$selected_name\") to true" >/dev/null 2>&1; then
    minimized=minimized
  fi
fi

printf '%s\n' "${ws_lines[@]}" | while read -r line; do
  ws_uuid=$(uuid_of "$line")
  ws_name=$(echo "$line" | sed -E 's/^\*? *[^ ]+ +//; s/ +\[selected\].*$//')
  [[ -n "$ws_uuid" ]] || continue
  wanted "$ws_name" || continue
  place "$ws_name" "$ws_uuid"
done

# Best-effort close of the vacated mirror window; re-minimize the husk if
# cmux refills it with a placeholder instead of closing.
"$cmux_bin" close-window --window "$mirror_window" >/dev/null 2>&1 || true
sleep 1
if "$cmux_bin" --id-format uuids list-windows 2>/dev/null | grep -qi "$mirror_window"; then
  osascript -e 'tell application "System Events" to tell process "cmux" to set value of attribute "AXMinimized" of (first window whose name is "~") to true' >/dev/null 2>&1 || true
  echo "mirror-window $mirror_window $minimized (husk remains, minimized)"
else
  echo "mirror-window $mirror_window closed"
fi
echo "mode ssh-tmux (window created)"
