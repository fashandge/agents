#!/bin/bash
# handoff_agent.sh — hand a task off to an autonomous coding-agent session
# (Claude Code or Codex) in a new terminal surface, on macOS (cmux tab) or
# Linux/headless (tmux session). The kickoff prompt is auto-submitted; if a
# sibling "<prompt-file>.goal" exists, its first line (a full "/goal ..."
# command) is typed as the SECOND message — both agents have /goal, and slash
# commands must be their own message (it queues during the first turn and
# fires at the next turn boundary).
#
# Usage:
#   handoff_agent.sh <name> <prompt-file> [cwd] [options]
# Options (env override in parens):
#   --agent   claude|codex        (HANDOFF_AGENT,   default claude)
#   --backend cmux|tmux           (HANDOFF_BACKEND, default: cmux if its
#                                  socket answers, else tmux)
#   --model   <model>             (HANDOFF_MODEL,   default: claude→opus,
#                                                    codex→gpt-5.6-sol)
#   --effort  <effort>            (HANDOFF_EFFORT,  default: claude→high,
#                                                    codex→xhigh)
#   --pmode   <mode>              (HANDOFF_PMODE,   default bypassPermissions;
#                                  claude: passed to --permission-mode;
#                                  codex: bypassPermissions →
#                                  --dangerously-bypass-approvals-and-sandbox,
#                                  anything else → -a on-request)
#   --workspace <ref>             cmux only; default = current workspace
#
# After launch:
#   cmux : click the tab | read: cmux read-screen --surface <uuid> --scrollback
#          steer: cmux send --surface <uuid> '<text>' && cmux send-key --surface <uuid> enter
#   tmux : tmux attach -t <name>  | read: tmux capture-pane -p -t <name> -S -2000
#          steer: tmux send-keys -t <name> -l '<text>' && tmux send-keys -t <name> Enter
#
# Examples:
#   cd ~/projects/investment && \
#   ~/projects/agents/scripts/handoff_agent.sh phase1-impl \
#       docs/plans/2026-07-13_phase1-kickoff.md
#   handoff_agent.sh review docs/plans/x.md ~/projects/foo --agent codex --backend tmux
set -euo pipefail

CMUX=/Applications/cmux.app/Contents/Resources/bin/cmux

# ── help ─────────────────────────────────────────────────────────────────────
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] || [ $# -lt 2 ]; then
  # print the header comment block (lines 2..N until the first non-# line)
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  exit 0
fi

# ── args ─────────────────────────────────────────────────────────────────────
NAME=${1:?name}; PROMPT_FILE=${2:?prompt file}; shift 2
CWD=$PWD
if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then CWD=$1; shift; fi
AGENT=${HANDOFF_AGENT:-claude}
BACKEND=${HANDOFF_BACKEND:-}
MODEL=${HANDOFF_MODEL:-}
EFFORT=${HANDOFF_EFFORT:-}
PMODE=${HANDOFF_PMODE:-bypassPermissions}
WS=""
while [ $# -gt 0 ]; do
  case $1 in
    --agent)     AGENT=$2; shift 2;;
    --backend)   BACKEND=$2; shift 2;;
    --model)     MODEL=$2; shift 2;;
    --effort)    EFFORT=$2; shift 2;;
    --pmode)     PMODE=$2; shift 2;;
    --workspace) WS=$2; shift 2;;
    *) echo "unknown option: $1" >&2; exit 1;;
  esac
done

[ -r "$PROMPT_FILE" ] || { echo "prompt file not readable: $PROMPT_FILE" >&2; exit 1; }
[ -d "$CWD" ] || { echo "cwd not a directory: $CWD" >&2; exit 1; }
PROMPT_ABS=$(cd "$(dirname "$PROMPT_FILE")" && pwd)/$(basename "$PROMPT_FILE")
GOAL_FILE="$PROMPT_ABS.goal"

# ── backend autodetect ───────────────────────────────────────────────────────
if [ -z "$BACKEND" ]; then
  if [ -x "$CMUX" ] && "$CMUX" ping >/dev/null 2>&1; then BACKEND=cmux
  elif command -v tmux >/dev/null; then BACKEND=tmux
  else echo "no backend: cmux socket not answering and tmux not installed" >&2; exit 1
  fi
fi

# ── per-agent launch command ─────────────────────────────────────────────────
case $AGENT in
  claude)
    MODEL=${MODEL:-opus}; EFFORT=${EFFORT:-high}
    LAUNCH="cd $CWD && CLAUDE_EFFORT=$EFFORT claude --model $MODEL --permission-mode $PMODE \"\$(cat $PROMPT_ABS)\""
    ;;
  codex)
    MODEL=${MODEL:-gpt-5.6-sol}; EFFORT=${EFFORT:-xhigh}
    if [ "$PMODE" = bypassPermissions ]; then APPROVAL="--dangerously-bypass-approvals-and-sandbox"
    else APPROVAL="-a on-request"; fi
    LAUNCH="cd $CWD && codex -m $MODEL -c model_reasoning_effort=$EFFORT $APPROVAL \"\$(cat $PROMPT_ABS)\""
    ;;
  *) echo "unknown agent: $AGENT (claude|codex)" >&2; exit 1;;
esac

# ── launch ───────────────────────────────────────────────────────────────────
send_second_goal() {  # $1 = send-fn taking one literal-text arg; sends Enter itself
  if [ -r "$GOAL_FILE" ]; then
    sleep 10
    "$1" "$(head -1 "$GOAL_FILE")"
    echo "goal set from: $GOAL_FILE"
  fi
}

case $BACKEND in
  cmux)
    [ -n "$WS" ] || WS=$("$CMUX" current-workspace)
    OUT=$("$CMUX" --id-format both new-surface --type terminal --workspace "$WS" --focus false)
    UUID=$(echo "$OUT" | grep -oE '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' | head -1)
    [ -n "$UUID" ] || { echo "could not parse surface UUID from: $OUT" >&2; exit 1; }
    "$CMUX" rename-tab --surface "$UUID" "$NAME" >/dev/null || true
    sleep 2
    cmux_type() { "$CMUX" send --surface "$UUID" "$1"; "$CMUX" send-key --surface "$UUID" enter; }
    cmux_type "$LAUNCH"
    send_second_goal cmux_type
    cat <<EOF
Launched [$AGENT/$BACKEND]: tab "$NAME" in $WS  (surface UUID: $UUID)
  watch : click the "$NAME" tab in cmux
  read  : $CMUX read-screen --surface $UUID --scrollback
  steer : $CMUX send --surface $UUID '<text>' && $CMUX send-key --surface $UUID enter
EOF
    ;;
  tmux)
    if tmux has-session -t "$NAME" 2>/dev/null; then
      echo "tmux session '$NAME' already exists (attach or pick another name)" >&2; exit 1
    fi
    tmux new-session -d -s "$NAME" -c "$CWD"
    sleep 1
    tmux_type() { tmux send-keys -t "$NAME" -l "$1"; tmux send-keys -t "$NAME" Enter; }
    tmux_type "$LAUNCH"
    send_second_goal tmux_type
    cat <<EOF
Launched [$AGENT/$BACKEND]: tmux session "$NAME"
  watch : tmux attach -t $NAME      (detach: Ctrl-b d)
  read  : tmux capture-pane -p -t $NAME -S -2000
  steer : tmux send-keys -t $NAME -l '<text>' && tmux send-keys -t $NAME Enter
EOF
    ;;
  *) echo "unknown backend: $BACKEND (cmux|tmux)" >&2; exit 1;;
esac
echo "  cwd   : $CWD"
echo "  model : $MODEL (effort $EFFORT)   pmode: $PMODE   prompt: $PROMPT_ABS"
