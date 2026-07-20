#!/bin/bash
# update_agents.sh — update this agents checkout and migrate persisted handoff
# state in one operator-facing command.
#
# Usage:
#   update_agents.sh [--no-pull] [--dry-run]
#   update_agents.sh --help
#
# Behavior:
#   * Default: git pull --ff-only, then run the orchestrator-rename migration.
#   * --no-pull: skip git and run only the migration.
#   * --dry-run: never pull and pass --dry-run to the migration.
#
# Invariants:
#   * The migration is idempotent and takes its own backup before any write.
#   * HANDOFF_REGISTRY_FILE, when set, selects its parent as the state tree;
#     this keeps tests and non-default installations away from the default tree.
#   * The Python interpreter is resolved portably and printed before use.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: update_agents.sh [--no-pull] [--dry-run]
       update_agents.sh --help

Pull this checkout with git pull --ff-only, then run the idempotent handoff
state migration. --no-pull migrates only. --dry-run never pulls or writes
state; it reports what the migration would change.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

pull=true
dry_run=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pull) pull=false; shift ;;
    --dry-run) pull=false; dry_run=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

if [[ -n "${HANDOFF_PYTHON:-}" ]]; then
  PYTHON=$HANDOFF_PYTHON
elif [[ -x /opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python ]]; then
  PYTHON=/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python
elif [[ -x /home/opc/miniforge3/envs/ml/bin/python ]]; then
  PYTHON=/home/opc/miniforge3/envs/ml/bin/python
else
  PYTHON=$(command -v python3) || die "no Python interpreter found; set HANDOFF_PYTHON"
fi

if [[ "$PYTHON" == */* ]]; then
  [[ -x "$PYTHON" ]] || die "Python interpreter is not executable: $PYTHON"
else
  PYTHON=$(command -v "$PYTHON") || die "Python interpreter not found: $PYTHON"
fi

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MIGRATION=("$PYTHON" "$REPO_ROOT/scripts/migrate_handoff_state_orchestrator.py")
if [[ -n "${HANDOFF_REGISTRY_FILE:-}" ]]; then
  [[ "$HANDOFF_REGISTRY_FILE" == /* ]] || die "HANDOFF_REGISTRY_FILE must be absolute"
  MIGRATION+=(--state-dir "$(dirname "$HANDOFF_REGISTRY_FILE")")
fi
if [[ "$dry_run" == true ]]; then
  MIGRATION+=(--dry-run)
fi

echo "python: $PYTHON"
if [[ "$pull" == true ]]; then
  echo "pull: running git pull --ff-only in $REPO_ROOT"
  git -C "$REPO_ROOT" pull --ff-only || die "git pull --ff-only failed"
  pull_result=completed
else
  echo "pull: skipped"
  pull_result=skipped
fi

echo "migration: running ${MIGRATION[*]}"
"${MIGRATION[@]}" || die "handoff state migration failed"
if [[ "$dry_run" == true ]]; then
  migration_result=dry-run
else
  migration_result=completed
fi
echo "update complete: pull=$pull_result migration=$migration_result"
