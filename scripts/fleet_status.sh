#!/bin/bash
# fleet_status.sh — one-glance answer to "where does this code live, and is any
# copy dirty or out of sync?"
#
# This repo is editable-installed, so a checkout IS the runtime and `git pull`
# is a deploy. That makes drift between machines invisible until something
# fails obscurely: a host running post-rename code against pre-rename state
# died with "registry record has invalid fields", and a delegated worker's edit
# to a checkout outside its target repo sat unnoticed and one `git checkout`
# from being lost. Both were visible in seconds — but only if you looked.
#
# Run it before delegating anywhere and after integrating a delegation.
#
# Usage:
#   fleet_status.sh [--fetch] [host:path ...]
#     --fetch   refresh remote-tracking refs first, so ahead/behind is current
#               (without it, those counts reflect the last fetch)
#     host:path inspect these checkouts instead of the defaults; use the host
#               "local" for this machine
#
# Reports per checkout: branch, HEAD, dirty file count, ahead/behind upstream,
# and — for checkouts carrying the handoff migration script — which package
# path `import agents` resolves to plus the number of state files still
# awaiting migration.
#
# Exit status is pass/fail, not data: finding drift is a successful run and
# exits 0. A nonzero exit means the probe itself could not run.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: fleet_status.sh [--fetch] [host:path ...]

Report branch/HEAD/dirty/ahead/behind for every known checkout, plus runtime
package path and pending state-migration count for handoff checkouts.
Use the host "local" for this machine. Finding drift still exits 0.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

DEFAULT_TARGETS=(
  "local:$HOME/projects/agents"
  "local:$HOME/skills"
  "oci-box:/home/opc/projects/agents"
  "oci-box:/home/opc/skills"
)

fetch_first=0
targets=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --fetch)   fetch_first=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*)        usage >&2; die "unknown argument: $1" ;;
    *)         targets+=("$1"); shift ;;
  esac
done
[[ ${#targets[@]} -gt 0 ]] || targets=("${DEFAULT_TARGETS[@]}")

# The probe runs identically on this machine and over SSH: it is fed on stdin
# to `bash -s` with the checkout path as $1, so there is one implementation to
# keep correct rather than a local copy and a remote copy that drift apart.
probe_body() {
  cat <<'PROBE'
set -u
repo=${1:?}
fetch=${2:-0}
if [ ! -d "$repo/.git" ]; then echo "  MISSING (not a git checkout)"; exit 0; fi
cd "$repo" || { echo "  UNREADABLE"; exit 0; }
[ "$fetch" = "1" ] && git fetch --quiet 2>/dev/null

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')
head=$(git rev-parse --short HEAD 2>/dev/null || echo '?')
dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
sync=""
if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
  counts=$(git rev-list --left-right --count "HEAD...@{u}" 2>/dev/null || echo "? ?")
  sync=" ahead=${counts%%	*} behind=${counts##*	}"
fi
echo "  branch=$branch head=$head dirty=$dirty$sync"
if [ "$dirty" -gt 0 ]; then
  echo "  DIRTY: $(git status --porcelain | head -4 | sed 's/^/    /' | tr '\n' '|' | sed 's/|/ | /g')"
fi

# Handoff checkouts additionally report runtime identity and state drift.
if [ -f scripts/migrate_handoff_state_orchestrator.py ]; then
  if [ -n "${HANDOFF_PYTHON:-}" ] && [ -x "${HANDOFF_PYTHON:-}" ]; then py=$HANDOFF_PYTHON
  elif [ -x /opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python ]; then py=/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python
  elif [ -x /home/opc/miniforge3/envs/ml/bin/python ]; then py=/home/opc/miniforge3/envs/ml/bin/python
  else py=$(command -v python3 || true); fi
  if [ -n "$py" ]; then
    runtime=$("$py" -c 'import agents,os;print(os.path.dirname(agents.__file__))' 2>/dev/null || echo '?')
    case "$runtime" in
      "$repo") runtime_note="runtime=this checkout" ;;
      '?')     runtime_note="runtime=UNIMPORTABLE" ;;
      *)       runtime_note="runtime=OTHER ($runtime)" ;;
    esac
    pending=$("$py" scripts/migrate_handoff_state_orchestrator.py --dry-run 2>/dev/null \
      | sed -n 's/.*total \([0-9][0-9]*\) file.*/\1/p' | tail -1)
    case "${pending:-?}" in
      0) state_note="state=migrated" ;;
      ?) state_note="state=UNKNOWN" ;;
      *) state_note="state=NEEDS MIGRATION ($pending files)" ;;
    esac
    echo "  $runtime_note $state_note"
  fi
fi
PROBE
}

for target in "${targets[@]}"; do
  host=${target%%:*}
  path=${target#*:}
  [[ "$host" != "$target" ]] || die "target must be host:path, got: $target"
  echo "== ${host}:${path}"
  if [[ "$host" == "local" ]]; then
    probe_body | bash -s -- "$path" "$fetch_first" || echo "  PROBE FAILED"
  else
    probe_body | ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" bash -s -- "$path" "$fetch_first" \
      || echo "  UNREACHABLE (or probe failed)"
  fi
done
