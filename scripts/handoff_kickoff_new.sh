#!/bin/bash
# handoff_kickoff_new.sh — generate a handoff worker kickoff with the
# handoff-protocol boilerplate pre-filled; supply only the task-specific
# sections.
#
# Usage:
#   handoff_kickoff_new.sh <task.md> [--name <slug>] [--out <file.md>]
#
#   <task.md>   Task-specific Markdown (Objective / Instructions / Scope and
#               constraints / Done condition — whatever applies). Inserted
#               verbatim after the title; must exist and be non-empty.
#   --name      Run name slug ([a-z0-9-]). Defaults to the task file's
#               basename, slugified (lowercase; non-alnum -> '-'; collapsed).
#   --out       Write the kickoff to <file.md> and print
#               {"name": <slug>, "out": <abs path>} on stdout. Without --out
#               the kickoff markdown itself is printed to stdout.
#
# The generated "## Handoff protocol" section is the worker contract: inbox
# discipline at startup/stage boundaries, checkpoint expectations, the exact
# `emit --type result` schema (worker should still verify against
# `handoffctl emit --help` — schemas are enforced exactly), and the
# review -> paused -> idle lifecycle. Keep this section in sync with the
# handoff-agent skill's references/worker-emit.md.
#
# Exit codes: 0 ok; 2 usage or validation error (diagnostic on stderr).
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: handoff_kickoff_new.sh <task.md> [--name <slug>] [--out <file.md>]

Generate a handoff worker kickoff: the handoff-protocol boilerplate (inbox
discipline, emit schema, review/paused contract) pre-filled around your
task-specific sections.

Inputs:
  task.md   Task-specific Markdown, inserted verbatim after the title.
            Write whatever sections apply — typical: "## Objective",
            "## Instructions", "## Scope and constraints", "## Done condition".
  --name    Run name slug [a-z0-9-] (default: task.md basename, slugified).
  --out     Write kickoff to this file; prints {"name","out"} JSON.
            Without --out, the kickoff markdown goes to stdout.

Worked example:
  cat > /tmp/task.md <<'EOF'
  ## Objective
  Summarize the top 30 posts on my X For You feed.

  ## Instructions
  Run: python -m news.src.x_home_feed.fetch --count 30 --no-fetch-articles
  Write the summary to /tmp/out/x-for-you.md.

  ## Done condition
  All 30 posts covered; summary file written; result emitted with verification.
  EOF
  handoff_kickoff_new.sh /tmp/task.md --name x-for-you-summary --out /tmp/kickoff.md
  # -> {"name": "x-for-you-summary", "out": "/tmp/kickoff.md"}
EOF
}

name=""
out=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) name=${2:?'missing value for --name'}; shift 2 ;;
    --out)  out=${2:?'missing value for --out'}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*)      usage >&2; echo "unknown argument: $1" >&2; exit 2 ;;
    *)       task_file=$1; shift ;;
  esac
done

[[ -n "${task_file:-}" ]] || { usage >&2; echo "missing <task.md> (the task-specific Markdown)" >&2; exit 2; }
[[ -f "$task_file" ]] || { echo "task file not found: $task_file" >&2; exit 2; }
[[ -s "$task_file" ]] || { echo "task file is empty: $task_file" >&2; exit 2; }

if [[ -z "$name" ]]; then
  name=$(basename "$task_file")
  name=${name%.md}; name=${name%.markdown}
fi
# Slugify: lowercase, non-alnum -> '-', collapse runs, strip edges.
# (BSD sed: no \+ in basic regex, hence -E and explicit +.)
name=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | sed -E -e 's/[^a-z0-9]+/-/g' -e 's/^-+//' -e 's/-+$//')
[[ "$name" =~ ^[a-z0-9-]+$ ]] || { echo "name must be a [a-z0-9-] slug, got: $name" >&2; exit 2; }

TEMPLATE=$(cat <<'EOF'
# __NAME__ — worker kickoff

__TASK__

## Handoff protocol

- You are a handoff worker with no parent conversation. Read this run's inbox at **startup and at every stage boundary** (before/after each stage, before the result):
  `/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python -m agents.orchestration.handoffctl read --run-dir "$HANDOFF_RUN_DIR" --journal inbox --after <cursor>`
  (`$HANDOFF_RUN_DIR` is set by the launcher; run `<helper> emit --help` for the exact payload schemas — they are enforced exactly.)
- Emit `checkpoint` events between stages so progress is visible; keep each turn bounded.
- Publish the final result with `emit --type result`: body = the complete deliverable (the orchestrator relays the body to the user), data = `{"commitments": [], "dirty": <true|false>, "head": <repo HEAD>, "inbox_cursor": <your read cursor>, "stage": "complete", "verification": [{"command", "exit_code", "summary"}]}`.
- After the orchestrator sends the accepted `review`, consume it and emit one `paused` checkpoint, then idle awaiting further instructions.
- Do not commit, push, or change shared state unless the kickoff's scope section says otherwise; report `dirty` and `head` accurately.
EOF
)

task_content=$(cat "$task_file")
kickoff=$(TEMPLATE="$TEMPLATE" NAME="$name" TASK="$task_content" /opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python - <<'PY'
import os
out = os.environ["TEMPLATE"].replace("__NAME__", os.environ["NAME"])
out = out.replace("__TASK__", os.environ["TASK"])
print(out, end="")
PY
)

if [[ -n "$out" ]]; then
  out_abs=$(cd "$(dirname "$out")" && pwd)/$(basename "$out")
  printf '%s' "$kickoff" > "$out_abs"
  /opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python - "$name" "$out_abs" <<'PY'
import json, sys
print(json.dumps({"name": sys.argv[1], "out": sys.argv[2]}))
PY
else
  printf '%s' "$kickoff"
fi
