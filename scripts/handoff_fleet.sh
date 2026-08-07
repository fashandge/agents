#!/bin/bash
# handoff_fleet.sh — launch several handoff workers from one manifest, in one
# pass, printing one JSON line per launch plus a summary line.
#
# Usage:
#   handoff_fleet.sh <manifest.tsv> [--orchestrator-state <state>] [--agent <default>] [--dry-run]
#
# Manifest: TSV lines (tab-separated), '#' comments and blank lines ignored:
#   <name>  <kickoff.md>  <repo>  [<agent>]  [<extra handoff_agent.sh args...>]
#   - name must be a [a-z0-9-] slug (lowercase).
#   - kickoff.md and repo may be relative to the manifest's directory.
#   - agent defaults to --agent, then to "pi". Valid: codex|claude|kimi|pi.
#   - extra tokens are appended verbatim to handoff_agent.sh (e.g. --model,
#     --effort, --backend) for that row only.
#
# Every row is validated BEFORE anything is launched, so a bad manifest costs
# nothing. Then each row launches via the sibling handoff_agent.sh (herdr/cmux
# backend auto-detected). Output on stdout:
#   {"name": ..., "ok": true, "run_id": ..., "handle": ..., "agent": ..., "model": ...}
#   {"name": ..., "ok": false, "error": ...}
#   {"summary": {"rows": N, "ok": K, "failed": M}}
# --dry-run prints the planned commands instead of launching:
#   {"name": ..., "dry_run": true, "command": "..."}
#
# Exit codes: 0 all rows ok (or dry-run parsed); 1 one or more launches failed;
# 2 usage/validation error (diagnostic on stderr, nothing launched).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python

usage() {
  cat <<'EOF'
Usage: handoff_fleet.sh <manifest.tsv> [--orchestrator-state <state>] [--agent <default>] [--dry-run]

Launch several handoff workers from one TSV manifest in one pass.

Manifest (tab-separated; '#' comments and blank lines ignored):
  <name>  <kickoff.md>  <repo>  [<agent>]  [<extra handoff_agent.sh args...>]
  - name: [a-z0-9-] slug (lowercase)
  - kickoff.md / repo: absolute, or relative to the manifest's directory
  - agent: codex|claude|kimi|pi (default: --agent, then "pi")
  - extra args (e.g. --model, --effort, --backend): appended to handoff_agent.sh

Flags:
  --orchestrator-state <state>  pass to every launcher (watcher state file)
  --agent <default>             default agent for rows that omit it
  --dry-run                     validate + print planned commands, launch nothing

Worked example (manifest.tsv):
  x-for-you-summary<TAB>kickoffs/x-for-you.md<TAB>/Users/jianfuchen/projects/news<TAB>pi
  reddit-home<TAB>kickoffs/reddit.md<TAB>/Users/jianfuchen/skills<TAB>pi<TAB>--model deepseek/deepseek-v4-flash

Output: one JSON line per row, then {"summary": {"rows","ok","failed"}}.
Exit: 0 all ok; 1 any launch failed; 2 usage/validation error.
EOF
}

orchestrator_state=""
default_agent="pi"
dry_run=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --orchestrator-state) orchestrator_state=${2:?'missing value for --orchestrator-state'}; shift 2 ;;
    --agent)              default_agent=${2:?'missing value for --agent'}; shift 2 ;;
    --dry-run)            dry_run=true; shift ;;
    -h|--help)            usage; exit 0 ;;
    -*)                   usage >&2; echo "unknown argument: $1" >&2; exit 2 ;;
    *)                    manifest=$1; shift ;;
  esac
done

[[ -n "${manifest:-}" ]] || { usage >&2; echo "missing <manifest.tsv>" >&2; exit 2; }
[[ -f "$manifest" ]] || { echo "manifest not found: $manifest" >&2; exit 2; }

case "$default_agent" in
  codex|claude|kimi|pi) ;;
  *) echo "default agent must be one of: codex, claude, kimi, pi (got: $default_agent)" >&2; exit 2 ;;
esac

manifest_dir=$(cd "$(dirname "$manifest")" && pwd)

# Parse + validate every row before launching anything.
rows=()
row_num=0
while IFS= read -r line || [[ -n "$line" ]]; do
  row_num=$((row_num + 1))
  [[ -z "$line" ]] && continue
  [[ "$line" == \#* ]] && continue
  IFS=$'\t' read -r -a cols <<< "$line"
  name=${cols[0]:-}
  kickoff=${cols[1]:-}
  repo=${cols[2]:-}
  agent=${cols[3]:-$default_agent}
  # Join extra columns (4+) with spaces. Avoid array slices/empty-array
  # expansion: macOS bash 3.2 + set -u errors on both.
  extra=""
  for ((i = 4; i < ${#cols[@]}; i++)); do
    [[ -n "$extra" ]] && extra+=" "
    extra+=${cols[$i]}
  done
  [[ -n "$name" ]] || { echo "row $row_num: missing name (col 1)" >&2; exit 2; }
  [[ "$name" =~ ^[a-z0-9-]+$ ]] || { echo "row $row_num: name must be a [a-z0-9-] slug, got: $name" >&2; exit 2; }
  [[ -n "$kickoff" ]] || { echo "row $row_num ($name): missing kickoff path (col 2)" >&2; exit 2; }
  [[ -n "$repo" ]] || { echo "row $row_num ($name): missing repo (col 3)" >&2; exit 2; }
  case "$agent" in
    codex|claude|kimi|pi) ;;
    *) echo "row $row_num ($name): agent must be one of: codex, claude, kimi, pi (got: $agent)" >&2; exit 2 ;;
  esac
  [[ "$kickoff" = /* ]] || kickoff="$manifest_dir/$kickoff"
  [[ "$repo" = /* ]] || repo="$manifest_dir/$repo"
  [[ -f "$kickoff" ]] || { echo "row $row_num ($name): kickoff not found: $kickoff" >&2; exit 2; }
  [[ -d "$repo" ]] || { echo "row $row_num ($name): repo dir not found: $repo" >&2; exit 2; }
  rows+=("$name|$kickoff|$repo|$agent|$extra")
done < "$manifest"

[[ ${#rows[@]} -gt 0 ]] || { echo "manifest has no rows: $manifest" >&2; exit 2; }

ok_count=0
fail_count=0

for row in "${rows[@]}"; do
  IFS='|' read -r name kickoff repo agent extra <<< "$row"
  launcher_args=(--agent "$agent")
  [[ -n "$orchestrator_state" ]] && launcher_args+=(--orchestrator-state "$orchestrator_state")
  [[ -n "$extra" ]] && launcher_args+=($extra)

  if [[ "$dry_run" == true ]]; then
    $PYTHON - "$name" "$here/handoff_agent.sh $name $kickoff $repo ${launcher_args[*]}" <<'PY'
import json, sys
print(json.dumps({"name": sys.argv[1], "dry_run": True, "command": sys.argv[2]}))
PY
    ok_count=$((ok_count + 1))
    continue
  fi

  set +e
  launcher_out=$("$here/handoff_agent.sh" "$name" "$kickoff" "$repo" "${launcher_args[@]}" 2>&1)
  rc=$?
  set -e

  if [[ $rc -eq 0 ]]; then
    row_json=$($PYTHON - "$name" "$launcher_out" <<'PY'
import json, sys
name, raw = sys.argv[1], sys.argv[2]
try:
    d = json.loads(raw)
except Exception as e:
    print(json.dumps({"name": name, "ok": False, "error": f"launcher output not JSON: {e}: {raw[:200]}"}))
    raise SystemExit(0)
d["name"] = name
d["ok"] = True
print(json.dumps(d))
PY
)
    if [[ "$row_json" == *'"ok": false'* ]]; then
      fail_count=$((fail_count + 1))
    else
      ok_count=$((ok_count + 1))
    fi
    printf '%s\n' "$row_json"
  else
    fail_count=$((fail_count + 1))
    printf '{"name": %s, "ok": false, "error": %s}\n' \
      "$($PYTHON -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$name")" \
      "$($PYTHON -c 'import json,sys; print(json.dumps(sys.argv[1][:300]))' "$launcher_out")"
  fi
done

printf '{"summary": {"rows": %d, "ok": %d, "failed": %d}}\n' "${#rows[@]}" "$ok_count" "$fail_count"
[[ $fail_count -eq 0 ]]
